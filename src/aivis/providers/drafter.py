"""drafter —— AI 档起草(providers 层,唯一可 import anthropic 处之一)。

照 docs/drafting-review-principles.md §4 定稿 prompt + 🟥 执行顺序:
  ① 中立 20 题(真盲:prompt 不含焦点名)→ ② 竞品建议(web_search,排除焦点)
  → ③ 防御 4 题(注入焦点名)。
顺序保证"隔离盲":中立题在拿到焦点/竞品名之前先生成。

网络触点收敛在 `TextSender`(Protocol):测试用假 sender 覆盖,离线、零成本。
本模块只做"调 AI + 解析 + 组装",**不**跑 validate_config(那是组装成 FrozenConfig 后、
GUI 层的安全网);这里只做轻量自检(配比/JSON 合法)+ 有界重试。
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import Message

from aivis import matching
from aivis.contracts.config import Intent, Question

_MAX_ATTEMPTS = 3  # 有界重试:JSON 不合法/配比不对则重起草,bounded run(CLAUDE.md §5)
_COMP_DEFAULT_K = 8  # 竞品建议上限(略多于 ≤5 品牌,供用户筛)
_TOOL_TYPE = "web_search_20250305"  # 经典版(ARCHITECTURE §9)
# 端点钉死官方地址(与 providers/anthropic.py 同理):用户在向导里输入的密钥不许被环境里的
# ANTHROPIC_BASE_URL 带到中转站去
_BASE_URL = "https://api.anthropic.com"


class DraftError(RuntimeError):
    """AI 起草失败(多次重试仍未产出合格结果 / 缺 key 等)。"""


@dataclass(frozen=True)
class CompetitorSuggestion:
    name: str
    domain: str


@dataclass(frozen=True)
class DraftResult:
    """一次 AI 起草产物 + 审核包原料(docs/drafting-review-principles.md §7)。"""

    questions: tuple[Question, ...]  # 24 题:d01../c01../l01../b01..
    competitors: tuple[CompetitorSuggestion, ...]
    raw: dict[str, Any]  # 三次调用的原始解析 JSON,供人工审核复核


class TextSender(Protocol):
    """唯一网络触点抽象:发一段 user prompt(可选开 web_search),返回回答正文。"""

    def __call__(self, *, system: str | None, user: str, use_web_search: bool) -> str: ...


# ---------------------------------------------------------------- 定稿 prompt（§4）
# 注:用 .replace 注入变量(prompt 内含字面 JSON 花括号,不能用 str.format)。

_NEUTRAL_PROMPT = """You are drafting neutral market-research questions about a product category —
the kind a real buyer asks a search assistant. You are deliberately NOT given any
specific brand. Do not ask for one.

Category: {category}
Language/locale: {locale}

Draft EXACTLY: discovery 8, comparison 6, local 6.
- discovery: open "what/which is best/recommended for…", spread across buyer
  sub-personas (beginner, experienced/pro, budget, premium, spec/size,
  service/warranty, usage scenario, durability). No two ask the same thing.
- comparison: "how do top brands compare on X" — each a DIFFERENT axis that
  MULTIPLE brands genuinely compete on.
- local: six DISTINCT, realistic in-category scenarios real buyers face.

HARD RULES:
- NEVER name any brand, company, or product line.
- Do not stack qualifiers that only ONE brand could satisfy (covert bias).
- Real buyer voice; one intent per question; no presupposed answer ("why is X best").
- Stay strictly inside the category "{category}".

Return ONLY valid JSON, no markdown, no comments, exactly this shape:
{"discovery":["..."],"comparison":["..."],"local":["..."]}"""

_DEFENSE_PROMPT = """Draft EXACTLY 4 brand-defense questions about ONE specific brand.

Brand: {focal_name}
Category: {category}
Language/locale: {locale}

Cover: overall quality, how it compares to peers, community/user opinion, value for money.

HARD RULES:
- Each question names ONLY {focal_name}. NEVER name a competitor.
- Framing TWO-SIDED and neutral — explicitly invite BOTH strengths and weaknesses.
  Never one-sided ("why is X best" / "is X bad").
- Real buyer voice; one intent each.

Return ONLY valid JSON, no markdown, no comments:
{"brand_defense":["..."]}"""

_COMPETITOR_PROMPT = """Using web search, list the most prominent REAL competitor brands in the
"{category}" category, EXCLUDING {focal_name} (the user's own brand).
Give each brand's common name and primary official website domain.
Only real, well-known brands in this category. Do not invent.

Return ONLY valid JSON, no markdown, no comments (at most {k} items):
{"competitors":[{"name":"...","domain":"example.com"}]}"""

_NEUTRAL_KEYS: tuple[tuple[str, Intent, str], ...] = (
    ("d", "discovery", "discovery"),
    ("c", "comparison", "comparison"),
    ("l", "local", "local"),
)


# ---------------------------------------------------------------- 编排


def draft_all(
    sender: TextSender,
    *,
    category: str,
    focal_name: str,
    locale: str = "en-US",
    k_competitors: int = _COMP_DEFAULT_K,
) -> DraftResult:
    """按 §4 顺序起草全部 24 题 + 竞品建议。纯编排,网络在 sender 内。"""
    if focal_name.strip() and matching.mentions_surfaces(category, [focal_name.strip()]):
        # 品类里带着焦点品牌名(如"Acme 登山包")会被原样写进"盲起草"的中立题提示词,
        # 盲就不盲了。在花钱调用之前拦下,让用户把品类改成不含品牌名的说法。
        raise ValueError(
            f"品类 {category!r} 里包含了你的品牌名 {focal_name!r}。"
            '请把品类改成不带品牌名的通用说法(例如只写"登山包"),否则中立题就不是盲起草了。'
        )
    neutral = _draft_neutral(sender, category=category, locale=locale)  # ① 盲
    competitors = _suggest_competitors(  # ② 竞品(排除焦点)
        sender, category=category, focal_name=focal_name, k=k_competitors
    )
    defense = _draft_defense(  # ③ 防御(注入焦点名)
        sender, category=category, focal_name=focal_name, locale=locale
    )
    questions = _questions_from(neutral, defense)
    raw: dict[str, Any] = {
        "neutral": neutral,
        "defense": {"brand_defense": defense},
        "competitors": [{"name": c.name, "domain": c.domain} for c in competitors],
    }
    return DraftResult(questions=questions, competitors=competitors, raw=raw)


def _draft_neutral(sender: TextSender, *, category: str, locale: str) -> dict[str, list[str]]:
    user = _NEUTRAL_PROMPT.replace("{category}", category).replace("{locale}", locale)

    def check(data: Any) -> None:
        counts = (
            len(_str_list(data["discovery"])),
            len(_str_list(data["comparison"])),
            len(_str_list(data["local"])),
        )
        if counts != (8, 6, 6):
            raise DraftError(f"中立题配比应 8/6/6,实得 {counts}")

    data = _call_json(sender, system=None, user=user, use_web_search=False, check=check)
    return {
        "discovery": _str_list(data["discovery"]),
        "comparison": _str_list(data["comparison"]),
        "local": _str_list(data["local"]),
    }


def _draft_defense(sender: TextSender, *, category: str, focal_name: str, locale: str) -> list[str]:
    user = (
        _DEFENSE_PROMPT.replace("{focal_name}", focal_name)
        .replace("{category}", category)
        .replace("{locale}", locale)
    )

    def check(data: Any) -> None:
        if len(_str_list(data["brand_defense"])) != 4:
            raise DraftError("防御题须恰为 4 条")

    data = _call_json(sender, system=None, user=user, use_web_search=False, check=check)
    return _str_list(data["brand_defense"])


def _suggest_competitors(
    sender: TextSender, *, category: str, focal_name: str, k: int
) -> tuple[CompetitorSuggestion, ...]:
    user = (
        _COMPETITOR_PROMPT.replace("{category}", category)
        .replace("{focal_name}", focal_name)
        .replace("{k}", str(k))
    )

    def check(data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("competitors"), list):
            raise DraftError("competitors 须为数组")

    data = _call_json(sender, system=None, user=user, use_web_search=True, check=check)
    return _parse_competitors(data, focal_name=focal_name, k=k)


# ---------------------------------------------------------------- 解析/组装


def _call_json(
    sender: TextSender,
    *,
    system: str | None,
    user: str,
    use_web_search: bool,
    check: Callable[[Any], None],
) -> Any:
    """调 sender → 解析 JSON → check;失败有界重试(bounded run)。"""
    last: Exception | None = None
    for _ in range(_MAX_ATTEMPTS):
        text = sender(system=system, user=user, use_web_search=use_web_search)
        try:
            data = _extract_json(text)
            check(data)
            return data
        except (DraftError, ValueError, KeyError, TypeError) as e:
            last = e
    raise DraftError(f"AI 起草 {_MAX_ATTEMPTS} 次仍未产出合格结果: {last}")


def _extract_json(text: str) -> Any:
    """从回答正文里抠出首个 {...} JSON 对象(容忍模型偶尔加 markdown 围栏/前后语)。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise DraftError("AI 输出未含 JSON 对象")
    return json.loads(text[start : end + 1])


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise DraftError("期望字符串数组")
    return [str(x).strip() for x in value]


def _parse_competitors(data: Any, *, focal_name: str, k: int) -> tuple[CompetitorSuggestion, ...]:
    items = data.get("competitors") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DraftError("competitors 须为数组")
    out: list[CompetitorSuggestion] = []
    seen: set[str] = set()
    focal_key = focal_name.strip().lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        domain = str(item.get("domain", "")).strip()
        key = name.lower()
        if not name or not domain or key == focal_key or key in seen:
            continue  # 跳过空/焦点自己/重复
        seen.add(key)
        out.append(CompetitorSuggestion(name=name, domain=domain))
        if len(out) >= k:
            break
    return tuple(out)


def _questions_from(neutral: dict[str, list[str]], defense: list[str]) -> tuple[Question, ...]:
    out: list[Question] = []
    for prefix, intent, key in _NEUTRAL_KEYS:
        for i, text in enumerate(neutral[key], start=1):
            out.append(Question(id=f"{prefix}{i:02d}", intent=intent, text=text.strip()))
    for i, text in enumerate(defense, start=1):
        out.append(Question(id=f"b{i:02d}", intent="brand_defense", text=text.strip()))
    return tuple(out)


# ---------------------------------------------------------------- 真 sender(唯一网络处)


class AnthropicTextSender:
    """真 Anthropic messages 调用(实现 TextSender)。唯一网络触点,测试以假 sender 替代。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 2048,
        region: str = "US",
    ) -> None:
        # api_key 可由调用方在内存里显式传入(GUI:用户经 localhost 提交,不落盘);
        # 不传则回落到环境变量。两种来源都不入日志(CLAUDE.md §5)。
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._region = region
        self._client: anthropic.Anthropic | None = None  # 惰性:构造不要求 key

    def __call__(self, *, system: str | None, user: str, use_web_search: bool) -> str:
        if self._client is None:
            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise DraftError(
                    "未检测到 ANTHROPIC_API_KEY(AI 档起草需 key)。"
                    "密钥经显式参数或环境变量读取(CLAUDE.md §5)。"
                )
            self._client = anthropic.Anthropic(api_key=key, base_url=_BASE_URL)
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            params["system"] = system
        if use_web_search:
            tool: dict[str, Any] = {"type": _TOOL_TYPE, "name": "web_search", "max_uses": 3}
            if self._region:
                tool["user_location"] = {"type": "approximate", "country": self._region}
            params["tools"] = [tool]
        try:
            msg = cast(Message, self._client.messages.create(**params))
        except anthropic.APIError as e:
            # 密钥不对 / 网络不通 / 限速:转成 DraftError,向导才能回一句人话
            # (否则异常一路冲出去,浏览器只看到连接断开、界面永远停在"起草中")
            raise DraftError(f"调用 Anthropic 起草失败({e.__class__.__name__}):{e}") from e
        return "".join(b.text for b in msg.content if b.type == "text")
