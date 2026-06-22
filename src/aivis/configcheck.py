"""configcheck —— 配置质检员(纯函数):按起草规则书检查冻结配置是否违反测量规则。

无 I/O、无网络;输入 FrozenConfig,输出违规清单。供 CLI `validate` 子命令与(后期)
SETUP 起草闭环调用。两个检查点(docs/question-drafting-principles.md §6):
- AI 起草后  phase='draft':配比必须严格 8/6/6/4(硬错);
- 用户拍板后 phase='final':配比仅告警,反偏置等红线两阶段都硬错。

只读 cfg、不改任何东西;CLI 据 has_errors 决定退出码。规则代号见各 Violation.rule。
"""

import re
from dataclasses import dataclass
from typing import Literal

from aivis.contracts.config import Brand, FrozenConfig
from aivis.matching import mentions_surfaces  # 单一匹配口径(与 extract/verify 共用)

Severity = Literal["error", "warn"]
Phase = Literal["draft", "final"]

# 配方:发现 8 + 比较 6 + 本地 6 + 防御 4(计划书第 7 章)
RECIPE: dict[str, int] = {"discovery": 8, "comparison": 6, "local": 6, "brand_defense": 4}

# 品牌数建议上限(含焦点):仅可读性/调色板考量,告警不拦;引擎本身不设硬 cap(保持通用)。
MAX_BRANDS = 5
NEUTRAL_INTENTS = frozenset({"discovery", "comparison", "local"})

# 引导句式黑名单(软规则,仅告警):预置结论/诱导
_LEADING_PATTERNS = (
    re.compile(r"\bwhy is\b.*\bbest\b", re.IGNORECASE),
    re.compile(r"\bthe best\b.*\bis\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class Violation:
    """一条违规:severity 决定拦不拦;rule 是稳定代号(供测试断言);where 是问题 id 或 'config'。"""

    severity: Severity
    rule: str
    where: str
    message: str


def has_errors(violations: list[Violation]) -> bool:
    """有任一 error 级违规即返回 True(CLI 据此退非零)。"""
    return any(v.severity == "error" for v in violations)


def _surfaces(brand: Brand) -> tuple[str, ...]:
    """品牌的'清晰名'集合(name + aliases);不含歧义别名(那些是常见词,正则会误伤)。"""
    return (brand.name, *brand.aliases)


def _mentions(text: str, surfaces: tuple[str, ...]) -> list[str]:
    """text 中命中的清晰名(去重保序)——口径统一走 matching.mentions_surfaces。"""
    return mentions_surfaces(text, surfaces)


def validate_config(cfg: FrozenConfig, *, phase: Phase = "final") -> list[Violation]:
    """按起草规则书 §6 逐条把关,返回违规清单(空 = 全绿)。纯函数,只读 cfg。"""
    out: list[Violation] = []
    all_surfaces = tuple(s for b in cfg.peers for s in _surfaces(b))
    focal_brands = [b for b in cfg.peers if b.is_focal]
    focal_mode = cfg.focal_domain is not None

    # --- 配比 8/6/6/4:draft 硬错,final 告警 ---
    counts = dict.fromkeys(RECIPE, 0)
    for q in cfg.questions:
        counts[q.intent] = counts.get(q.intent, 0) + 1
    if counts != RECIPE:
        note = "(AI 起草必须严格)" if phase == "draft" else "(用户已改,仅提醒)"
        out.append(
            Violation(
                "error" if phase == "draft" else "warn",
                "recipe-8-6-6-4",
                "config",
                f"配比 {counts} 不是 8/6/6/4{note}",
            )
        )

    # --- 反偏置 + 空题(逐题,两阶段都硬错) ---
    for q in cfg.questions:
        if not q.text.strip():
            out.append(Violation("error", "empty", q.id, "问题文本为空"))
        if q.intent in NEUTRAL_INTENTS:
            hits = _mentions(q.text, all_surfaces)
            if hits:
                out.append(
                    Violation(
                        "error",
                        "anti-bias",
                        q.id,
                        f"中立题({q.intent})点名了品牌 {hits} —— 会诱导 AI、污染测量,必须去掉",
                    )
                )

    # --- 焦点 / peer 模式不变量 ---
    if focal_mode:
        out.extend(_check_focal_mode(cfg, focal_brands))
    else:
        out.extend(_check_peer_mode(cfg, focal_brands, all_surfaces))

    # --- 重复题(告警) ---
    seen: dict[str, str] = {}
    for q in cfg.questions:
        key = q.text.strip().lower()
        if not key:
            continue
        if key in seen:
            out.append(Violation("warn", "duplicate", q.id, f"与 {seen[key]} 文本重复"))
        else:
            seen[key] = q.id

    # --- 引导句式(软告警) ---
    for q in cfg.questions:
        if any(pat.search(q.text) for pat in _LEADING_PATTERNS):
            out.append(Violation("warn", "leading", q.id, f"疑似引导句式: {q.text!r}"))

    # --- 品牌数上限(可读性,仅告警;引擎不设硬 cap) ---
    if len(cfg.peers) > MAX_BRANDS:
        out.append(
            Violation(
                "warn",
                "too-many-brands",
                "config",
                f"品牌数 {len(cfg.peers)} 超过建议上限 {MAX_BRANDS}(含焦点)——"
                f"报告排名/调色板可读性会下降,建议精简到 ≤{MAX_BRANDS}",
            )
        )

    return out


def _check_focal_mode(cfg: FrozenConfig, focal_brands: list[Brand]) -> list[Violation]:
    out: list[Violation] = []
    if len(focal_brands) != 1:
        out.append(
            Violation(
                "error",
                "focal-count",
                "config",
                f"focal_domain 已设,但 is_focal=true 的品牌有 {len(focal_brands)} 个(应恰好 1 个)",
            )
        )
    elif cfg.focal_domain not in focal_brands[0].domains:
        out.append(
            Violation(
                "error",
                "focal-domain",
                "config",
                f"焦点品牌 {focal_brands[0].name!r} 的 domains "
                f"不含 focal_domain {cfg.focal_domain!r}",
            )
        )

    focal_surfaces = _surfaces(focal_brands[0]) if focal_brands else ()
    nonfocal_surfaces = tuple(s for b in cfg.peers if not b.is_focal for s in _surfaces(b))
    for q in cfg.questions:
        if q.intent != "brand_defense":
            continue
        if focal_surfaces and not _mentions(q.text, focal_surfaces):
            out.append(
                Violation("error", "defense-focal", q.id, "焦点模式下,品牌防御题必须点名焦点品牌")
            )
        comp_hits = _mentions(q.text, nonfocal_surfaces)
        if comp_hits:
            out.append(
                Violation(
                    "error",
                    "defense-competitor",
                    q.id,
                    f"品牌防御题点名了竞品 {comp_hits} —— 防御题只许提焦点品牌",
                )
            )
    return out


def _check_peer_mode(
    cfg: FrozenConfig, focal_brands: list[Brand], all_surfaces: tuple[str, ...]
) -> list[Violation]:
    out: list[Violation] = []
    if focal_brands:
        out.append(
            Violation(
                "error",
                "peer-no-focal",
                "config",
                f"peer 模式(focal_domain 为空)不许有 is_focal=true,却有 "
                f"{[b.name for b in focal_brands]}",
            )
        )
    for q in cfg.questions:
        if q.intent == "brand_defense" and not _mentions(q.text, all_surfaces):
            out.append(
                Violation(
                    "warn",
                    "defense-no-brand",
                    q.id,
                    "品牌防御题没点名任何已声明品牌(防御题本应点名某个品牌)",
                )
            )
    return out
