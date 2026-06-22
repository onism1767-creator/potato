"""templater 单元测试:档1 免费模板起草 24 题开箱即合规。

核心断言:8/6/6/4 配比、id 方案、中立题不点名焦点、防御题点名焦点;
组装成焦点模式 FrozenConfig 后过 validate(draft 与 final)0 硬错;
品类含品牌名时反偏置规则会捕获(与 scaffold 同口径)。
"""

from collections import Counter

import pytest

from aivis.configcheck import has_errors, validate_config
from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.templater import draft_template_questions, draft_template_segments


def _focal_cfg(
    questions: tuple[Question, ...], *, focal_name: str, focal_domain: str
) -> FrozenConfig:
    return FrozenConfig(
        version="t-v1",
        prompt_version="t-questions-v1",
        competitor_set_version="t-brands-v1",
        focal_domain=focal_domain,
        peers=(Brand(name=focal_name, domains=(focal_domain,), is_focal=True),),
        competitors_on=True,
        questions=questions,
        tier="standard",
        locale="en-US",
        region="US",
        temperature=0.0,
        n_runs=3,
        max_uses=3,
        budget_cap_usd=5.0,
    )


def test_recipe_8_6_6_4() -> None:
    qs = draft_template_questions(category="hiking backpacks", focal_name="Acme Corp")
    c = Counter(q.intent for q in qs)
    assert (c["discovery"], c["comparison"], c["local"], c["brand_defense"]) == (8, 6, 6, 4)
    assert len(qs) == 24


def test_id_scheme() -> None:
    qs = draft_template_questions(category="CRM software", focal_name="Acme")
    ids = [q.id for q in qs]
    assert ids[:8] == [f"d{i:02d}" for i in range(1, 9)]
    assert ids[8:14] == [f"c{i:02d}" for i in range(1, 7)]
    assert ids[14:20] == [f"l{i:02d}" for i in range(1, 7)]
    assert ids[20:] == [f"b{i:02d}" for i in range(1, 5)]


def test_category_substituted() -> None:
    qs = draft_template_questions(category="hiking backpacks", focal_name="Acme")
    assert all("hiking backpacks" in q.text for q in qs if q.intent != "brand_defense")


def test_neutral_omit_focal_defense_name_focal() -> None:
    qs = draft_template_questions(category="hiking backpacks", focal_name="Acme Corp")
    neutral = [q for q in qs if q.intent != "brand_defense"]
    defense = [q for q in qs if q.intent == "brand_defense"]
    assert all("Acme Corp" not in q.text for q in neutral)
    assert defense and all("Acme Corp" in q.text for q in defense)


def test_drafted_config_validates_clean_both_phases() -> None:
    qs = draft_template_questions(category="hiking backpacks", focal_name="Acme Corp")
    cfg = _focal_cfg(qs, focal_name="Acme Corp", focal_domain="acme.com")
    assert not has_errors(validate_config(cfg, phase="draft")), "免费模板草稿不应有 draft 硬错"
    assert not has_errors(validate_config(cfg, phase="final")), "免费模板草稿不应有 final 硬错"


def test_empty_category_falls_back() -> None:
    qs = draft_template_questions(category="   ", focal_name="Acme")
    assert all(q.text.strip() for q in qs)
    assert any("products" in q.text for q in qs)


def test_category_with_brand_name_triggers_anti_bias() -> None:
    # 回归(与 scaffold 同口径):品类含焦点名 → 中立题会点名 → 反偏置硬错,交用户改
    qs = draft_template_questions(category="Acme-style CRM", focal_name="Acme")
    cfg = _focal_cfg(qs, focal_name="Acme", focal_domain="acme.com")
    violations = validate_config(cfg, phase="final")
    assert any(v.rule == "anti-bias" for v in violations)


def test_brands_of_phrasing_reads_naturally() -> None:
    # 复数名词品类下,"{c} brands" 会别扭 → 这 5 句改用 "brands of {c}"
    drafted = draft_template_questions(category="hiking backpacks", focal_name="Acme")
    qs = {q.id: q.text for q in drafted}
    for qid in ("d06", "c01", "c03", "c05", "b02"):
        assert "brands of hiking backpacks" in qs[qid]
    assert all("backpacks brands" not in t for t in qs.values())


def test_deterministic_repeatable() -> None:
    # 纯函数:同输入→逐字相同输出(无随机/无时钟),保证可多次运行结果一致
    a = draft_template_questions(category="hiking backpacks", focal_name="Acme Corp")
    b = draft_template_questions(category="hiking backpacks", focal_name="Acme Corp")
    assert a == b
    assert [q.text for q in a] == [q.text for q in b]


# 泛用性:差异极大的品类都应产出 8/6/6/4 且组装后过 validate 两阶段 0 硬错
@pytest.mark.parametrize(
    "category",
    [
        "hiking backpacks",
        "CRM software",
        "specialty coffee beans",
        "running shoes",
        "project management SaaS",
        "electric toothbrushes",
        "稀有矿泉水",  # 非英文品类也不应崩
    ],
)
def test_generality_across_categories(category: str) -> None:
    qs = draft_template_questions(category=category, focal_name="Acme Corp")
    c = Counter(q.intent for q in qs)
    assert (c["discovery"], c["comparison"], c["local"], c["brand_defense"]) == (8, 6, 6, 4)
    cfg = _focal_cfg(qs, focal_name="Acme Corp", focal_domain="acme.com")
    assert not has_errors(validate_config(cfg, phase="draft"))
    assert not has_errors(validate_config(cfg, phase="final"))


def test_focal_name_with_special_chars_safe() -> None:
    # 焦点名含正则/格式特殊字符不应使 format 或 validate 崩
    for focal in ("A+B (Pro)", "Brand{X}", "C&D Co.", "Foo.Bar"):
        qs = draft_template_questions(category="widgets", focal_name=focal)
        assert len([q for q in qs if q.intent == "brand_defense"]) == 4
        assert all(focal in q.text for q in qs if q.intent == "brand_defense")


# ---------------------------------------------------------------- 分段形态(锁定芯片)


def test_segments_text_matches_full_questions() -> None:
    # 分段拼回的题面必须与完整形态逐字一致(派生关系,黄金不动的保证)
    segs = draft_template_segments(category="hiking backpacks", focal_name="Acme Corp")
    full = draft_template_questions(category="hiking backpacks", focal_name="Acme Corp")
    assert len(segs) == 24
    assert [(t.id, t.intent, t.text) for t in segs] == [(q.id, q.intent, q.text) for q in full]


def test_neutral_chips_are_category_only() -> None:
    # 中立题(d/c/l)只含品类芯片,绝不含品牌芯片(隔离盲 + 反偏置)
    segs = draft_template_segments(category="hiking backpacks", focal_name="Acme Corp")
    for tq in segs:
        if tq.intent == "brand_defense":
            continue
        tokens = {s.token for s in tq.segments if s.token}
        assert tokens == {"category"}
        chip = next(s for s in tq.segments if s.token == "category")
        assert chip.text == "hiking backpacks"


def test_defense_chips_include_brand_and_category() -> None:
    # 防御题含品牌芯片(注入焦点名);其值为焦点名;品类芯片视模板而定
    segs = draft_template_segments(category="hiking backpacks", focal_name="Acme Corp")
    defense = [tq for tq in segs if tq.intent == "brand_defense"]
    assert len(defense) == 4
    for tq in defense:
        brand_chips = [s for s in tq.segments if s.token == "brand"]
        assert brand_chips and all(s.text == "Acme Corp" for s in brand_chips)


def test_editable_text_segments_have_no_token() -> None:
    # 普通文字段(可编辑)token 必须为 None;每题至少有一段普通文字
    segs = draft_template_segments(category="widgets", focal_name="Acme")
    for tq in segs:
        assert any(s.token is None for s in tq.segments)
        assert all(s.token in (None, "category", "brand") for s in tq.segments)
