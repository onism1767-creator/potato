"""drafter 单元测试:AI 档起草,全程假 sender(离线、零成本)。

覆盖:happy 24 题组装 + 过 validate;🟥 执行顺序①中立②竞品③防御 + 中立 prompt 无焦点名
(隔离盲的机械证据);markdown 围栏容忍;有界重试(成功/耗尽);竞品排除焦点 + 去重 + 封顶。
"""

import json
from typing import Any

import anthropic
import pytest

from aivis.configcheck import has_errors, validate_config
from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.providers.drafter import AnthropicTextSender, DraftError, draft_all

_NEUTRAL_GOOD = json.dumps(
    {
        "discovery": [
            "What are the best widgets for beginners?",
            "Which widgets do professionals recommend?",
            "What are the best budget widgets?",
            "What are the best premium widgets?",
            "What widgets are most durable?",
            "Which widgets have the best support?",
            "What widgets are best for daily use?",
            "What are the top-rated widgets?",
        ],
        "comparison": [
            "How do top widget brands compare on quality?",
            "How do widgets compare on price?",
            "Which widgets have the best features?",
            "How do widgets compare on reliability?",
            "How do mainstream widgets compare to niche ones?",
            "Which widgets balance price and quality?",
        ],
        "local": [
            "What widgets are best for professional use?",
            "What widgets are best for home use?",
            "What widgets are best for travel?",
            "What widgets handle heavy use?",
            "What widgets suit an upgrade from entry-level?",
            "What widgets fit specialized needs?",
        ],
    }
)
_DEFENSE_GOOD = json.dumps(
    {
        "brand_defense": [
            "Is Acme Corp a good brand? What are its strengths and weaknesses?",
            "How does Acme Corp compare to peers?",
            "What do users think about Acme Corp?",
            "Are Acme Corp products worth it?",
        ]
    }
)
_COMPETITOR_GOOD = json.dumps(
    {
        "competitors": [
            {"name": "Beta Co", "domain": "beta.com"},
            {"name": "Gamma Inc", "domain": "gamma.com"},
        ]
    }
)


class FakeSender:
    """按调用类型(web_search→竞品 / 含 'defense'→防御 / 否则中立)回放预设响应,并记录每次调用。"""

    def __init__(self, *, neutral: list[str], defense: list[str], competitor: list[str]) -> None:
        self._q = {"neutral": neutral, "defense": defense, "competitor": competitor}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, system: str | None, user: str, use_web_search: bool) -> str:
        kind = (
            "competitor"
            if use_web_search
            else ("defense" if "defense" in user.lower() else "neutral")
        )
        self.calls.append({"kind": kind, "user": user})
        q = self._q[kind]
        return q.pop(0) if len(q) > 1 else q[0]


def _good_sender() -> FakeSender:
    return FakeSender(
        neutral=[_NEUTRAL_GOOD], defense=[_DEFENSE_GOOD], competitor=[_COMPETITOR_GOOD]
    )


def _focal_cfg(questions: tuple[Question, ...]) -> FrozenConfig:
    return FrozenConfig(
        version="t-v1",
        prompt_version="t-q-v1",
        competitor_set_version="t-b-v1",
        focal_domain="acme.com",
        peers=(Brand(name="Acme Corp", domains=("acme.com",), is_focal=True),),
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


def test_draft_all_happy_assembles_and_validates() -> None:
    result = draft_all(_good_sender(), category="widgets", focal_name="Acme Corp")
    ids = [q.id for q in result.questions]
    assert ids[:8] == [f"d{i:02d}" for i in range(1, 9)]
    assert ids[8:14] == [f"c{i:02d}" for i in range(1, 7)]
    assert ids[14:20] == [f"l{i:02d}" for i in range(1, 7)]
    assert ids[20:] == [f"b{i:02d}" for i in range(1, 5)]
    assert len(result.questions) == 24
    cfg = _focal_cfg(result.questions)
    assert not has_errors(validate_config(cfg, phase="draft"))
    assert not has_errors(validate_config(cfg, phase="final"))


def test_execution_order_and_blind_neutral() -> None:
    sender = _good_sender()
    draft_all(sender, category="widgets", focal_name="Acme Corp")
    # 🟥 顺序:中立 → 竞品 → 防御
    assert [c["kind"] for c in sender.calls] == ["neutral", "competitor", "defense"]
    neutral_call = next(c for c in sender.calls if c["kind"] == "neutral")
    defense_call = next(c for c in sender.calls if c["kind"] == "defense")
    # 隔离盲的机械证据:中立 prompt 不含焦点名;防御 prompt 含焦点名
    assert "Acme Corp" not in neutral_call["user"]
    assert "Acme Corp" in defense_call["user"]


def test_markdown_fence_tolerated() -> None:
    fenced = "```json\n" + _NEUTRAL_GOOD + "\n```"
    sender = FakeSender(neutral=[fenced], defense=[_DEFENSE_GOOD], competitor=[_COMPETITOR_GOOD])
    result = draft_all(sender, category="widgets", focal_name="Acme Corp")
    assert len(result.questions) == 24


def test_bounded_retry_succeeds_after_bad() -> None:
    sender = FakeSender(
        neutral=["not json at all", _NEUTRAL_GOOD],
        defense=[_DEFENSE_GOOD],
        competitor=[_COMPETITOR_GOOD],
    )
    result = draft_all(sender, category="widgets", focal_name="Acme Corp")
    assert len(result.questions) == 24


def test_bounded_retry_exhausts_raises() -> None:
    bad = json.dumps({"discovery": ["only one"], "comparison": [], "local": []})
    sender = FakeSender(
        neutral=[bad, bad, bad], defense=[_DEFENSE_GOOD], competitor=[_COMPETITOR_GOOD]
    )
    with pytest.raises(DraftError):
        draft_all(sender, category="widgets", focal_name="Acme Corp")


def test_competitor_excludes_focal_dedup_and_cap() -> None:
    comp = json.dumps(
        {
            "competitors": [
                {"name": "Acme Corp", "domain": "acme.com"},  # 焦点自己 → 排除
                {"name": "Beta Co", "domain": "beta.com"},
                {"name": "beta co", "domain": "beta.com"},  # 重复(大小写)→ 去重
                {"name": "Gamma Inc", "domain": "gamma.com"},
                {"name": "NoDomain"},  # 缺域名 → 跳过
            ]
        }
    )
    sender = FakeSender(neutral=[_NEUTRAL_GOOD], defense=[_DEFENSE_GOOD], competitor=[comp])
    result = draft_all(sender, category="widgets", focal_name="Acme Corp", k_competitors=2)
    names = [c.name for c in result.competitors]
    assert names == ["Beta Co", "Gamma Inc"]  # 排除焦点/去重/缺域名,且封顶 2


# ---------------------------------------------------------------- AnthropicTextSender key 来源


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessages:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def create(self, **kwargs: Any) -> Any:
        return type("_Msg", (), {"content": [_FakeBlock('{"ok": 1}')]})()


class _FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        _CAPTURED.update(kwargs)
        self.messages = _FakeMessages(_CAPTURED)


_CAPTURED: dict[str, Any] = {}


def test_sender_uses_explicit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式传入的 key(GUI 经内存提交)应优先用于建 client,即便没有环境变量。
    _CAPTURED.clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    out = AnthropicTextSender(api_key="sk-explicit")(system=None, user="hi", use_web_search=False)
    assert out == '{"ok": 1}'
    assert _CAPTURED["api_key"] == "sk-explicit"


def test_sender_falls_back_to_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _CAPTURED.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    AnthropicTextSender()(system=None, user="hi", use_web_search=False)
    assert _CAPTURED["api_key"] == "sk-from-env"


def test_sender_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(DraftError, match="ANTHROPIC_API_KEY"):
        AnthropicTextSender()(system=None, user="x", use_web_search=False)
