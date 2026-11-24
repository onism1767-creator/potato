"""api —— 本地向导端点逻辑(dict→dict,不碰 http,便于离线单测)。

2.3 先接**不花钱**的三个:draft(免费档模板起草)/ validate(质检)/ estimate(成本上界,离线)。
花钱的 run(冻结 + subprocess `aivis run`)+ AI 档起草(需 key)留下一轮。
所有函数输入 = 已解析的 JSON dict,输出 = 可 json 的 dict;坏输入抛 ValueError(server 映射 400)。
"""

import math
from typing import Any, cast

from pydantic import ValidationError

from aivis.configcheck import has_errors, validate_config
from aivis.configio import explain_config_error
from aivis.contracts.adapter import SurfaceSpec
from aivis.contracts.config import Brand, FrozenConfig, Intent, Question
from aivis.providers import catalog, registry
from aivis.providers.anthropic import TIER_INFO, estimate_draft_cost_usd
from aivis.providers.drafter import AnthropicTextSender, TextSender, draft_all
from aivis.templater import draft_template_segments

_INTENTS = ("discovery", "comparison", "local", "brand_defense")


def _str(payload: dict[str, Any], key: str, *, required: bool = False) -> str:
    val = str(payload.get(key, "") or "").strip()
    if required and not val:
        raise ValueError(f"缺少必填字段: {key}")
    return val


def _question(q: dict[str, Any]) -> Question:
    intent = str(q.get("intent", ""))
    if intent not in _INTENTS:
        raise ValueError(f"未知 intent: {intent!r}")
    text = str(q.get("text", "")).strip()
    if not text:
        raise ValueError("问题文本为空")
    return Question(id=str(q.get("id", "")), intent=cast(Intent, intent), text=text)


def _questions_from(payload: dict[str, Any]) -> tuple[Question, ...]:
    raw = payload.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("缺少 questions")
    return tuple(_question(q) for q in raw)


def _budget(payload: dict[str, Any]) -> float:
    """预算上限:必须是有限的数字(空值 / Infinity / 文字 → 干净的 400,而不是让服务端崩)。"""
    raw = payload.get("budget_cap_usd", 13.0)
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        raise ValueError("预算上限必须是一个数字") from None
    if not math.isfinite(cap):
        raise ValueError("预算上限必须是一个有限的数字")
    return cap


def _config_from(payload: dict[str, Any], questions: tuple[Question, ...]) -> FrozenConfig:
    """从向导输入(单一输入源)+ 题目组装焦点模式 FrozenConfig(内存,不落盘)。"""
    focal_name = _str(payload, "focal_name", required=True)
    focal_domain = _str(payload, "focal_domain", required=True)
    aliases = tuple(str(a).strip() for a in (payload.get("aliases") or []) if str(a).strip())
    peers = [Brand(name=focal_name, aliases=aliases, domains=(focal_domain,), is_focal=True)]
    for c in payload.get("competitors") or []:
        if not isinstance(c, dict):
            raise ValueError("competitors 的每一项必须是 {name, domain}")
        name, domain = str(c.get("name", "")).strip(), str(c.get("domain", "")).strip()
        if name and domain:
            peers.append(Brand(name=name, domains=(domain,)))
    budget = _budget(payload)
    try:
        return FrozenConfig(
            version="wizard",
            prompt_version="wizard-questions",
            competitor_set_version="wizard-brands",
            focal_domain=focal_domain,
            peers=tuple(peers),
            competitors_on=len(peers) > 1,
            questions=questions,
            tier=_str(payload, "tier") or "standard",
            locale="en-US",
            region="US",
            temperature=0.0,
            n_runs=3,
            sequential=bool(payload.get("sequential")),  # 向导勾选:序贯采样(n_runs 作上限)
            max_uses=3,
            budget_cap_usd=budget,
        )
    except ValidationError as e:
        # 契约拒绝(如竞品与自己重名、两道题同一个 id):回一句人话,不把库的英文原文甩给页面
        raise ValueError(";".join(explain_config_error(e))) from e


def draft(payload: dict[str, Any]) -> dict[str, Any]:
    """起草 24 题。两档:free=模板($0,不用 key);ai=盲起草(需 api_key,内存传、不落盘)。"""
    tier = _str(payload, "draft_tier") or "free"
    if tier == "free":
        return _draft_free(payload)
    if tier == "ai":
        return _draft_ai(payload, _ai_sender(payload))
    raise ValueError(f"未知起草档 {tier!r}(可选 free / ai)")


def _draft_free(payload: dict[str, Any]) -> dict[str, Any]:
    """免费档:模板填充 24 题($0,不用 key)。返回分段形态:品类/品牌为锁定芯片,余可编辑。"""
    questions = draft_template_segments(
        category=_str(payload, "category"), focal_name=_str(payload, "focal_name", required=True)
    )
    return {
        "draft_tier": "free",
        "questions": [
            {
                "id": q.id,
                "intent": q.intent,
                "text": q.text,  # 初始完整题面(前端编辑文字段后自行重组)
                "segments": [
                    {"text": s.text, **({"token": s.token} if s.token else {})} for s in q.segments
                ],
            }
            for q in questions
        ],
        "competitors": [],  # 免费档竞品手填;AI 档由 drafter 建议
    }


def _ai_sender(payload: dict[str, Any]) -> TextSender:
    """AI 档网络触点:key 只在本机内存(经 localhost 提交)、不落盘、不进日志(CLAUDE §5)。"""
    api_key = _str(payload, "api_key") or None
    if not api_key:
        raise ValueError("AI 档起草需要 api_key(只在本机内存、不落盘、仅发往 api.anthropic.com)")
    return AnthropicTextSender(api_key=api_key, region="US")


def _draft_ai(payload: dict[str, Any], sender: TextSender) -> dict[str, Any]:
    """AI 档:drafter 盲起草 24 题 + 竞品建议。sender 抽出以便离线单测。"""
    result = draft_all(
        sender,
        category=_str(payload, "category", required=True),  # 盲起草必须有品类
        focal_name=_str(payload, "focal_name", required=True),
    )
    return {
        "draft_tier": "ai",
        "questions": [{"id": q.id, "intent": q.intent, "text": q.text} for q in result.questions],
        "competitors": [{"name": c.name, "domain": c.domain} for c in result.competitors],
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """对(可能已编辑的)题目 + 品牌跑 configcheck,返回违规清单(前端据此标黄/红)。"""
    cfg = _config_from(payload, _questions_from(payload))
    violations = validate_config(cfg, phase="final")
    return {
        "has_errors": has_errors(violations),
        "violations": [
            {"severity": v.severity, "rule": v.rule, "where": v.where, "message": v.message}
            for v in violations
        ],
    }


def estimate(payload: dict[str, Any]) -> dict[str, Any]:
    """成本上界(离线、不花钱、不调 API):n 次调用 × 单价表。

    surfaces(向导"真实测量"勾选的模型列表)→ 逐个估算并合计;否则按 provider(默认 mock)
    + 档位估一个。适配器一律经 registry 构造(引擎名分支只在那里,CLAUDE.md §4.3)。
    """
    cfg = _config_from(payload, _questions_from(payload))
    n_calls = len(cfg.questions) * cfg.n_runs
    surfaces = [str(s).strip() for s in (payload.get("surfaces") or []) if str(s).strip()]
    if surfaces:
        adapters = [registry.make_adapter(SurfaceSpec.parse(s)) for s in surfaces]
    else:
        provider = _str(payload, "provider") or "mock"
        if provider not in catalog.PROVIDERS:
            raise ValueError("请先选择要测的 AI 模型(surfaces 为空)")
        adapters = [
            registry.make_adapter(SurfaceSpec(provider=provider, model=None), tier=cfg.tier)
        ]
    estimates = [a.estimate_cost_usd(n_calls, cfg) for a in adapters]
    return {
        "provider": adapters[0].provider_name if len(adapters) == 1 else "multiple",
        "model": ", ".join(a.model_name for a in adapters),
        "n_calls": n_calls,
        "est_usd": round(sum(estimates), 2),
        "budget_cap_usd": cfg.budget_cap_usd,
        "over_budget": any(e > cfg.budget_cap_usd for e in estimates),  # 每个表面各自受限
    }


def models(payload: dict[str, Any]) -> dict[str, Any]:
    """全部可测表面(全部真引擎,按目录顺序)+ 按当前配置的预估(离线、不花钱)。

    GUI"选测哪些 AI"的数据源:每个模型带 label / blurb / status / 诚实标记 / 预估 / 是否超预算;
    每家带密钥环境变量名与唯一发往的主机(隐私文案如实列出)。
    """
    cfg = _config_from(payload, _questions_from(payload))
    n_calls = len(cfg.questions) * cfg.n_runs
    providers: list[dict[str, Any]] = []
    for name, pinfo in catalog.PROVIDERS.items():
        if name == "mock":
            continue
        entries: list[dict[str, Any]] = []
        for m in catalog.list_models(name):
            adapter = registry.make_adapter(SurfaceSpec(provider=name, model=m.model))
            est = adapter.estimate_cost_usd(n_calls, cfg)
            entries.append(
                {
                    "key": m.key,
                    "model": m.model,
                    "label": m.label,
                    "tier": m.tier,
                    "blurb": m.blurb,
                    "status": m.status,
                    "supports_temperature": m.supports_temperature,
                    "supports_region": m.supports_region,
                    "pinned": m.pinned,
                    "surface_kind": m.surface_kind,
                    "est_usd": round(est, 2),
                    "over_budget": est > cfg.budget_cap_usd,
                }
            )
        providers.append(
            {
                "name": name,
                "label": pinfo.label,
                "env_key": pinfo.env_key,
                "endpoint": pinfo.endpoint,
                "models": entries,
            }
        )
    return {"n_calls": n_calls, "budget_cap_usd": cfg.budget_cap_usd, "providers": providers}


def tiers(payload: dict[str, Any]) -> dict[str, Any]:
    """三档 run 的成本总览(离线、不花钱):每档 label/model/blurb + 本配置预估 + 是否超预算。"""
    cfg = _config_from(payload, _questions_from(payload))
    n_calls = len(cfg.questions) * cfg.n_runs
    out: list[dict[str, Any]] = []
    for info in TIER_INFO:
        adapter = registry.make_adapter(SurfaceSpec(provider="anthropic", model=info.model))
        est = adapter.estimate_cost_usd(n_calls, cfg)
        out.append(
            {
                "id": info.id,
                "label": info.label,
                "model": info.model,
                "blurb": info.blurb,
                "est_usd": round(est, 2),
                "over_budget": est > cfg.budget_cap_usd,
            }
        )
    return {"n_calls": n_calls, "budget_cap_usd": cfg.budget_cap_usd, "tiers": out}


def draft_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    """AI 起草这一步的成本上界(离线、不花钱):中立+防御 2 次无搜索 + 竞品 1 次 web_search。"""
    return {"est_usd": round(estimate_draft_cost_usd(), 2)}
