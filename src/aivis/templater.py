"""templater —— 纯函数:按品类用固定模板起草 24 题(档1 免费起草)。

零 I/O、零网络、确定性(可配黄金测试)。照 question-drafting-principles.md 的句式模板,
中立 20 题(发现 8 / 比较 6 / 本地 6)只填 `{c}`=品类,**绝不点名任何品牌**;防御 4 题
注入焦点品牌名 `{f}`。产出 8/6/6/4,id 沿用 scaffold 方案(d01../c01../l01../b01..)。

🟥 局限(诚实,GUI 须标注):本地/场景 6 题用**通用场景**(模板吃不到行业语境,见规则书 §4),
质量天生偏泛——GUI 应把这 6 题标黄提示用户手改得更贴行业。这正是档2/3 用 AI 起草的理由。
"""

import re
from dataclasses import dataclass

from aivis.contracts.config import Intent, Question

# 发现 8 题:覆盖入门↔资深、价格↔高端、规格↔服务、频率↔耐用的光谱(规则书 §2)
_DISCOVERY: tuple[str, ...] = (
    "What are the best {c} for beginners?",
    "Which {c} do experienced users recommend most?",
    "What are the best value {c} for the money?",
    "What are the best premium {c}?",
    "What {c} have the best build quality and durability?",
    "Which brands of {c} offer the best warranty and customer support?",
    "What are the best {c} for everyday use?",
    "What are the top-rated {c} available right now?",
)

# 比较 6 题:每题换一个多家可比的通用比较轴(规则书 §3)
_COMPARISON: tuple[str, ...] = (
    "How do the top brands of {c} compare on quality?",
    "How do popular {c} compare on price and value?",
    "Which brands of {c} have the best features, and how do they differ?",
    "How do leading {c} compare on reliability and durability?",
    "How do well-known brands of {c} compare to lesser-known ones?",
    "Which {c} offer the best balance between price and quality?",
)

# 本地/场景 6 题:通用真实场景(规则书 §4——这桶最吃行业语境,模板偏泛,须人改)
_LOCAL: tuple[str, ...] = (
    "What {c} are best for professional or commercial use?",
    "What {c} are best for home and personal use?",
    "What {c} are best for travel or portable use?",
    "What {c} hold up best under heavy, frequent use?",
    "What {c} are best for someone upgrading from an entry-level option?",
    "What {c} are recommended for demanding or specialized needs?",
)

# 防御 4 题:中立双面骨架 + 注入焦点名 {f}(规则书 §5);只提焦点、不提竞品
_DEFENSE: tuple[str, ...] = (
    "Is {f} a good {c} brand? What are its strengths and weaknesses?",
    "How does {f} compare to other brands of {c}?",
    "What do users and the community think about {f}?",
    "Are {f} products worth it?",
)

_NEUTRAL_SPECS: tuple[tuple[str, Intent, tuple[str, ...]], ...] = (
    ("d", "discovery", _DISCOVERY),
    ("c", "comparison", _COMPARISON),
    ("l", "local", _LOCAL),
)

# 本地题 id(供 GUI 标黄"建议手改":模板场景偏泛)
LOCAL_QUESTION_IDS: tuple[str, ...] = tuple(f"l{i:02d}" for i in range(1, len(_LOCAL) + 1))

# 令牌 → (前端芯片类别, 取值键):品类/品牌是锁定令牌(Step1 填一次,处处自动引用)
_TOKEN_META: dict[str, str] = {"{c}": "category", "{f}": "brand"}
_TOKEN_RE = re.compile(r"(\{[cf]\})")  # 切分时保留分隔符(令牌)本身


@dataclass(frozen=True)
class Segment:
    """题面的一段:token=None 为可编辑普通文字;token="category"/"brand" 为锁定芯片。"""

    text: str
    token: str | None = None


@dataclass(frozen=True)
class TemplateQuestion:
    """免费档模板题的分段形态:芯片(品类/品牌)锁定、其余文字可编辑。"""

    id: str
    intent: Intent
    segments: tuple[Segment, ...]

    @property
    def text(self) -> str:
        """拼回完整题面(与渲染掉令牌的旧产物逐字一致)。"""
        return "".join(s.text for s in self.segments)


def _segmentize(template: str, *, c: str, f: str) -> tuple[Segment, ...]:
    """把模板按 {c}/{f} 令牌切成「文字段 + 锁定芯片段」序列。"""
    values = {"{c}": c, "{f}": f}
    out: list[Segment] = []
    for part in _TOKEN_RE.split(template):
        if not part:
            continue  # 切分可能产生空串(令牌相邻/居首尾)
        token = _TOKEN_META.get(part)
        out.append(Segment(text=values[part], token=token) if token else Segment(text=part))
    return tuple(out)


def draft_template_segments(*, category: str, focal_name: str) -> tuple[TemplateQuestion, ...]:
    """用固定模板起草 24 题的**分段形态**(供 GUI 渲染锁定芯片)。纯函数,不花钱。

    品类/品牌是锁定令牌(填一次、处处自动注入);其余句子文字可编辑。
    其余口径同 draft_template_questions(空品类兜底 products;不静默清洗品类含品牌名)。
    """
    c = category.strip() or "products"
    f = focal_name.strip()
    out: list[TemplateQuestion] = []
    for prefix, intent, templates in _NEUTRAL_SPECS:
        for i, template in enumerate(templates, start=1):
            out.append(
                TemplateQuestion(
                    id=f"{prefix}{i:02d}", intent=intent, segments=_segmentize(template, c=c, f=f)
                )
            )
    for i, template in enumerate(_DEFENSE, start=1):
        out.append(
            TemplateQuestion(
                id=f"b{i:02d}",
                intent="brand_defense",
                segments=_segmentize(template, c=c, f=f),
            )
        )
    return tuple(out)


def draft_template_questions(*, category: str, focal_name: str) -> tuple[Question, ...]:
    """用固定模板起草 24 题(8/6/6/4),完整文字形态。纯函数,不调 API、不花钱。

    从分段形态派生(拼回令牌),产物与历史逐字一致(黄金不动)。
    """
    return tuple(
        Question(id=tq.id, intent=tq.intent, text=tq.text)
        for tq in draft_template_segments(category=category, focal_name=focal_name)
    )
