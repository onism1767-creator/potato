"""Anthropic 适配器 v0.2:六模型目录一致性、显式 model、采样参数省略、refusal、按模型计价(离线)。"""

from typing import Any

import pytest
from anthropic.types import Message

from aivis.contracts.config import FrozenConfig
from aivis.providers import catalog
from aivis.providers.anthropic import _SPECS, AnthropicAdapter


def _message(stop_reason: str = "end_turn", model: str = "claude-sonnet-5") -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "answer", "citations": None}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )


class _Fake(AnthropicAdapter):
    def __init__(self, outcomes: list[Message], **kw: Any) -> None:
        super().__init__(**kw)
        self._outcomes = list(outcomes)
        self.sent: list[dict[str, Any]] = []

    def _send(self, params: dict[str, Any]) -> Message:
        self.sent.append(params)
        return self._outcomes.pop(0)


def test_every_catalog_model_has_a_price_spec() -> None:
    """目录里的每个 Claude 模型都必须有计价/参数规格(加模型忘加单价 → 这里红,不静默估 0)。"""
    ids = {m.model for m in catalog.list_models("anthropic")}
    assert ids == set(_SPECS)
    for m in catalog.list_models("anthropic"):
        assert _SPECS[m.model].sampling == m.supports_temperature, m.model


def test_explicit_model_overrides_tier() -> None:
    a = AnthropicAdapter(model="claude-fable-5-1")
    assert a.model_name == "claude-fable-5-1" and a.info.tier == "flagship"
    with pytest.raises(ValueError, match="未知 Claude 模型"):
        AnthropicAdapter(model="claude-9")


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-5", "claude-fable-5-1"])
def test_new_models_omit_temperature_and_use_larger_max_tokens(
    demo_cfg: FrozenConfig, model: str
) -> None:
    a = _Fake([_message(model=model)], model=model)
    a.ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    params = a.sent[0]
    assert "temperature" not in params
    assert params["max_tokens"] == 8192
    assert params["tools"][0]["type"] == "web_search_20250305"


def test_refusal_stop_reason_recorded_as_failure(demo_cfg: FrozenConfig) -> None:
    """安全层拒答(HTTP 200 + refusal)= 失败,不是空回答,也不切换模型作答。"""
    a = _Fake([_message(stop_reason="refusal", model="claude-fable-5-1")], model="claude-fable-5-1")
    resp = a.ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    assert not resp.finished_ok and resp.error_code == "refusal"
    assert "refusal" in (resp.error_detail or "")
    assert len(a.sent) == 1  # 没有 fallback 二次调用


def test_fable_priced_at_its_own_rate(demo_cfg: FrozenConfig) -> None:
    n = 10
    fable = AnthropicAdapter(model="claude-fable-5-1").estimate_cost_usd(n, demo_cfg)
    sonnet5 = AnthropicAdapter(model="claude-sonnet-5").estimate_cost_usd(n, demo_cfg)
    sonnet46 = AnthropicAdapter(model="claude-sonnet-4-6").estimate_cost_usd(n, demo_cfg)
    assert fable > sonnet46 > sonnet5  # $10/$50 > $3/$15 > $2/$10
    input_tokens = 2000 + 3 * 12000
    per_call = input_tokens / 1e6 * 10.0 + 2000 / 1e6 * 50.0 + 3 * 0.01
    assert fable == pytest.approx(n * per_call)
