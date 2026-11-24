"""gui.api 端点逻辑单测(dict→dict,离线、零成本):draft(免费 / AI)/ validate / estimate。"""

from typing import cast

import pytest

from aivis.gui import api
from tests.test_drafter import _good_sender

_BASE = {
    "focal_name": "Acme Corp",
    "focal_domain": "acme.com",
    "category": "hiking backpacks",
    "budget_cap_usd": 13.0,
}


def _draft_questions() -> list[dict[str, str]]:
    return cast(list[dict[str, str]], api.draft({**_BASE, "draft_tier": "free"})["questions"])


def test_draft_free_returns_24_recipe() -> None:
    out = api.draft({**_BASE, "draft_tier": "free"})
    qs = out["questions"]
    assert len(qs) == 24
    from collections import Counter

    c = Counter(q["intent"] for q in qs)
    assert (c["discovery"], c["comparison"], c["local"], c["brand_defense"]) == (8, 6, 6, 4)
    assert all("hiking backpacks" in q["text"] for q in qs if q["intent"] != "brand_defense")


def test_draft_free_carries_locked_chip_segments() -> None:
    # 免费档每题带 segments;拼回 == text;品类/品牌为锁定芯片(带 token)
    qs = api.draft({**_BASE, "draft_tier": "free"})["questions"]
    for q in qs:
        segs = q["segments"]
        assert "".join(s["text"] for s in segs) == q["text"]  # 重组一致(单一输入源)
        assert any("token" not in s for s in segs)  # 至少一段可编辑文字
        cats = [s for s in segs if s.get("token") == "category"]
        assert all(s["text"] == "hiking backpacks" for s in cats)  # 品类芯片值锁定
    # 中立题必含品类芯片(且无品牌芯片);防御题必含品牌芯片
    neutral = [q for q in qs if q["intent"] != "brand_defense"]
    for q in neutral:
        tokens = {s.get("token") for s in q["segments"] if s.get("token")}
        assert tokens == {"category"}
    defense = [q for q in qs if q["intent"] == "brand_defense"]
    for q in defense:
        brands = [s for s in q["segments"] if s.get("token") == "brand"]
        assert brands and all(s["text"] == "Acme Corp" for s in brands)


def test_draft_ai_requires_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        api.draft({**_BASE, "draft_tier": "ai"})


def test_draft_ai_assembles_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    # 注入假 sender(离线、零成本),走真 draft() 公开入口的 AI 分支。
    monkeypatch.setattr(api, "_ai_sender", lambda payload: _good_sender())
    out = api.draft({**_BASE, "draft_tier": "ai", "api_key": "sk-test"})
    assert out["draft_tier"] == "ai"
    assert len(out["questions"]) == 24
    assert [c["name"] for c in out["competitors"]] == ["Beta Co", "Gamma Inc"]


def test_draft_unknown_tier() -> None:
    with pytest.raises(ValueError, match="未知起草档"):
        api.draft({**_BASE, "draft_tier": "premium"})


def test_draft_missing_focal_name() -> None:
    with pytest.raises(ValueError, match="focal_name"):
        api.draft({"category": "x", "draft_tier": "free"})


def test_validate_clean_free_draft() -> None:
    out = api.validate({**_BASE, "questions": _draft_questions()})
    assert out["has_errors"] is False
    assert out["violations"] == []


def test_validate_catches_anti_bias() -> None:
    # 把品牌名塞进一道中立题 → 反偏置硬错
    qs = _draft_questions()
    qs[0]["text"] = "What are the best Acme Corp backpacks?"
    out = api.validate({**_BASE, "questions": qs})
    assert out["has_errors"] is True
    assert any(v["rule"] == "anti-bias" for v in out["violations"])


def test_estimate_mock_is_zero_offline() -> None:
    out = api.estimate({**_BASE, "questions": _draft_questions(), "provider": "mock"})
    assert out["provider"] == "mock"
    assert out["est_usd"] == 0.0
    assert out["n_calls"] == 24 * 3


def test_estimate_anthropic_positive_and_budget_flag() -> None:
    out = api.estimate(
        {**_BASE, "questions": _draft_questions(), "provider": "anthropic", "budget_cap_usd": 1.0}
    )
    assert out["provider"] == "anthropic"
    assert out["est_usd"] > 0
    assert out["over_budget"] is True  # 24×3 调用上界远超 $1


def test_validate_missing_questions() -> None:
    with pytest.raises(ValueError, match="questions"):
        api.validate({**_BASE})


def test_tiers_overview_three_ascending() -> None:
    out = api.tiers({**_BASE, "questions": _draft_questions(), "budget_cap_usd": 13.0})
    ts = out["tiers"]
    assert [t["id"] for t in ts] == ["economy", "standard", "high"]
    assert all(t["label"] and t["blurb"] and t["model"] for t in ts)  # 文案/模型齐全
    ests = [t["est_usd"] for t in ts]
    assert ests[0] < ests[1] < ests[2]  # 省钱 < 标准 < 高质量
    assert out["n_calls"] == 24 * 3


def test_tiers_over_budget_flag() -> None:
    out = api.tiers({**_BASE, "questions": _draft_questions(), "budget_cap_usd": 1.0})
    assert all(t["over_budget"] for t in out["tiers"])  # 24×3 远超 $1


def test_draft_estimate_positive_small() -> None:
    out = api.draft_estimate({**_BASE})
    assert 0.0 < out["est_usd"] < 1.0  # 起草几分钱级别(上界)


def test_config_from_reads_sequential() -> None:
    qs = _draft_questions()
    on = api._config_from(
        {**_BASE, "sequential": True, "questions": qs},
        api._questions_from({**_BASE, "questions": qs}),
    )
    off = api._config_from(
        {**_BASE, "questions": qs}, api._questions_from({**_BASE, "questions": qs})
    )
    assert on.sequential is True and off.sequential is False  # 向导勾选透传,默认关


def test_negative_budget_rejected_as_valueerror() -> None:
    # 负预算 → FrozenConfig 的 pydantic ValidationError(是 ValueError 子类)→ server 映射 400
    out = api.draft({**_BASE, "draft_tier": "free"})
    with pytest.raises(ValueError):
        api.estimate({**_BASE, "questions": out["questions"], "budget_cap_usd": -5})


# ---------------------------------------------------------------- 配置契约收紧后的向导报错


def test_competitor_named_like_the_focal_brand_is_refused_in_plain_words() -> None:
    """把自己也填进竞品(只差大小写):以前同一处提及会被算两次;现在当场拒绝,且说人话。"""
    payload = {
        **_BASE,
        "questions": _draft_questions(),
        "competitors": [{"name": "acme corp", "domain": "elsewhere.com"}],
    }
    with pytest.raises(ValueError, match="品牌名重复") as exc:
        api.validate(payload)
    assert "validation error" not in str(exc.value) and "pydantic" not in str(exc.value)


def test_same_competitor_listed_twice_is_refused() -> None:
    rival = {"name": "Rival Co", "domain": "rival.com"}
    payload = {**_BASE, "questions": _draft_questions(), "competitors": [rival, dict(rival)]}
    with pytest.raises(ValueError, match="品牌名重复.*Rival Co"):
        api.estimate({**payload, "provider": "mock"})


def test_two_questions_with_one_id_are_refused() -> None:
    questions = _draft_questions()
    questions[1]["id"] = questions[0]["id"]
    with pytest.raises(ValueError, match="题目 id 重复") as exc:
        api.validate({**_BASE, "questions": questions})
    assert "validation error" not in str(exc.value)
