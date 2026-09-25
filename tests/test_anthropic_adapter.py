"""Anthropic 适配器离线测试:不联网、不花钱、不需要密钥。

用 SDK 的 pydantic 类型构造真实形状的响应,以替身覆盖唯一网络触点 _send()。
"""

import json
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message

from aivis.contracts.adapter import FatalProviderError
from aivis.contracts.config import FrozenConfig
from aivis.providers.anthropic import AnthropicAdapter

# ---------------------------------------------------------------- 响应构造器


def _message(
    *,
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    web_search_requests: int = 1,
    model: str = "claude-sonnet-4-6",
) -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "server_tool_use": {
                    "web_search_requests": web_search_requests,
                    "web_fetch_requests": 0,
                },
            },
        }
    )


def _text_block(text: str, citations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "text", "text": text, "citations": citations}


def _citation(url: str, title: str | None, cited_text: str) -> dict[str, Any]:
    return {
        "type": "web_search_result_location",
        "url": url,
        "title": title,
        "cited_text": cited_text,
        "encrypted_index": "idx",
    }


class FakeAdapter(AnthropicAdapter):
    """以预设响应/异常序列覆盖唯一网络触点。"""

    def __init__(self, outcomes: list[Message | Exception], tier: str = "standard") -> None:
        super().__init__(tier=tier)
        self._outcomes = list(outcomes)
        self.sent_params: list[dict[str, Any]] = []

    def _send(self, params: dict[str, Any]) -> Message:
        self.sent_params.append(params)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _api_error(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls("test error", response=response, body=None)  # type: ignore[no-any-return]


def _ask(adapter: FakeAdapter, cfg: FrozenConfig) -> Any:
    return adapter.ask(cfg.questions[0], cfg, run_id="r1", run_index=0)


# ---------------------------------------------------------------- 元数据与成本


def test_metadata_and_tier_mapping() -> None:
    standard = AnthropicAdapter(tier="standard")
    assert standard.provider_name == "anthropic"
    assert standard.surface_id == "anthropic_api_web_search"
    assert standard.tool_path == "web_search"
    assert standard.model_name == "claude-sonnet-4-6"  # 监控默认便宜档(计划书 §6.4)
    assert AnthropicAdapter(tier="high").model_name == "claude-opus-4-8"
    assert AnthropicAdapter(tier="economy").model_name == "claude-haiku-4-5"  # 省钱档
    with pytest.raises(ValueError, match="档位"):
        AnthropicAdapter(tier="nope")


def test_economy_tier_is_cheapest() -> None:
    from aivis.providers.anthropic import TIER_INFO

    # 三档元数据齐全、顺序 = 便宜→标准→高质量、id 与适配器一致
    assert [t.id for t in TIER_INFO] == ["economy", "standard", "high"]
    for t in TIER_INFO:
        assert t.label and t.blurb  # 文案非空(GUI 展示)
        assert AnthropicAdapter(tier=t.id).model_name == t.model


def test_economy_cheaper_than_standard_cheaper_than_high(demo_cfg: FrozenConfig) -> None:
    n = 72
    eco = AnthropicAdapter(tier="economy").estimate_cost_usd(n, demo_cfg)
    std = AnthropicAdapter(tier="standard").estimate_cost_usd(n, demo_cfg)
    high = AnthropicAdapter(tier="high").estimate_cost_usd(n, demo_cfg)
    assert eco < std < high  # Haiku $1/$5 < Sonnet $3/$15 < Opus $5/$25


def test_cost_estimate_is_model_aware_upper_bound(demo_cfg: FrozenConfig) -> None:
    """token 费 + max_uses 满额搜索费的保守上界(输入按每搜索校准),按档位取价。"""
    n_calls = 72
    standard = AnthropicAdapter(tier="standard").estimate_cost_usd(n_calls, demo_cfg)
    input_tokens = 2000 + 3 * 12000  # base + max_uses=3 × 每搜索 12k
    per_call = input_tokens / 1e6 * 3.0 + 2000 / 1e6 * 15.0 + 3 * 0.01
    assert standard == pytest.approx(n_calls * per_call)
    high = AnthropicAdapter(tier="high").estimate_cost_usd(n_calls, demo_cfg)
    assert high > standard  # 旗舰档更贵


def test_actual_cost_from_usage(demo_cfg: FrozenConfig) -> None:
    """actual_cost_usd 从 raw_json 真实 usage 计价(运行时封顶依据)。"""
    import json

    from aivis.contracts.records import RawResponse

    resp = RawResponse(
        run_id="r",
        question_id="q1",
        run_index=0,
        answer_text="x",
        citations=(),
        search_count=2,
        model="claude-sonnet-4-6",
        finished_ok=True,
        error_code=None,
        error_detail=None,
        raw_json=json.dumps(
            {"messages": [{"usage": {"input_tokens": 10000, "output_tokens": 1000}}]}
        ),
        timestamp="2026-06-10T00:00:00Z",
    )
    cost = AnthropicAdapter(tier="standard").actual_cost_usd(resp)
    expected = 10000 / 1e6 * 3.0 + 1000 / 1e6 * 15.0 + 2 * 0.01
    assert cost == pytest.approx(expected)


def test_actual_cost_bad_json_is_zero(demo_cfg: FrozenConfig) -> None:
    from aivis.contracts.records import RawResponse

    resp = RawResponse(
        run_id="r",
        question_id="q1",
        run_index=0,
        answer_text="",
        citations=(),
        search_count=0,
        model="claude-sonnet-4-6",
        finished_ok=True,
        error_code=None,
        error_detail=None,
        raw_json="not json",
        timestamp="2026-06-10T00:00:00Z",
    )
    assert AnthropicAdapter(tier="standard").actual_cost_usd(resp) == 0.0


# ---------------------------------------------------------------- 成功路径


def test_parses_answer_citations_and_search_count(demo_cfg: FrozenConfig) -> None:
    message = _message(
        content=[
            {
                "type": "server_tool_use",
                "id": "st_1",
                "name": "web_search",
                "input": {"query": "best packs"},
            },
            _text_block(
                "Alpha Packs leads.",
                [_citation("https://alphapacks.example/p", "Alpha Packs | Shop", "Alpha…")],
            ),
            _text_block(
                "Reviews agree.",
                [_citation("https://reviews.example/best", None, "tested on trail")],
            ),
        ],
        web_search_requests=2,
    )
    resp = _ask(FakeAdapter([message]), demo_cfg)
    assert resp.finished_ok and resp.error_code is None
    assert resp.answer_text == "Alpha Packs leads. Reviews agree."
    assert [c.url for c in resp.citations] == [
        "https://alphapacks.example/p",
        "https://reviews.example/best",
    ]
    assert [c.position for c in resp.citations] == [0, 1]
    assert resp.citations[0].cited_text == "Alpha…"
    assert resp.search_count == 2
    assert resp.model == "claude-sonnet-4-6"
    raw = json.loads(resp.raw_json)  # 原样返回完整入档
    assert len(raw["messages"]) == 1


def test_request_params_pin_cleanroom_surface(demo_cfg: FrozenConfig) -> None:
    """clean-room:无系统提示;pinned 温度/区域;max_uses 上限传给工具。"""
    adapter = FakeAdapter([_message(content=[_text_block("x")])])
    _ask(adapter, demo_cfg)
    params = adapter.sent_params[0]
    assert "system" not in params
    assert params["temperature"] == demo_cfg.temperature
    tool = params["tools"][0]
    assert tool["type"] == "web_search_20250305"  # 经典版:内联引用直出(见适配器注释)
    assert tool["max_uses"] == demo_cfg.max_uses
    assert tool["user_location"] == {"type": "approximate", "country": "US"}


def test_opus_tier_omits_sampling_params(demo_cfg: FrozenConfig) -> None:
    """Opus 4.8 已移除采样参数,传 temperature 会 400 → 必须省略。"""
    adapter = FakeAdapter(
        [_message(content=[_text_block("x")], model="claude-opus-4-8")], tier="high"
    )
    _ask(adapter, demo_cfg)
    assert "temperature" not in adapter.sent_params[0]


def test_pause_turn_resumes_bounded(demo_cfg: FrozenConfig) -> None:
    paused = _message(
        content=[_text_block("part one.")], stop_reason="pause_turn", web_search_requests=2
    )
    done = _message(content=[_text_block("part two.")], web_search_requests=1)
    adapter = FakeAdapter([paused, done])
    resp = _ask(adapter, demo_cfg)
    assert len(adapter.sent_params) == 2
    assert resp.finished_ok
    assert resp.answer_text == "part one. part two."
    assert resp.search_count == 3  # 跨续传累计


def test_pause_turn_limit_recorded_as_failure(demo_cfg: FrozenConfig) -> None:
    """续传上限后仍未完成 → bounded run,按失败记录,不无上限深搜。"""
    paused = _message(content=[_text_block("…")], stop_reason="pause_turn")
    adapter = FakeAdapter([paused] * 4)
    resp = _ask(adapter, demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    assert "pause_turn" in (resp.error_detail or "")


# ---------------------------------------------------------------- 失败路径


def test_tool_error_block_is_failure_not_empty_answer(demo_cfg: FrozenConfig) -> None:
    """HTTP 200 + error block → finished_ok=False;正文保留作审计但不进指标。"""
    message = _message(
        content=[
            _text_block("partial answer"),
            {
                "type": "web_search_tool_result",
                "tool_use_id": "st_1",
                "content": {
                    "type": "web_search_tool_result_error",
                    "error_code": "max_uses_exceeded",
                },
            },
        ]
    )
    resp = _ask(FakeAdapter([message]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "tool_error"
    assert "max_uses_exceeded" in (resp.error_detail or "")
    assert resp.answer_text == "partial answer"  # 保留上下文,extract 会跳过失败响应


def test_rate_limit_recorded_after_sdk_retries(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_api_error(anthropic.RateLimitError, 429)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "rate_limit"


def test_timeout_recorded(demo_cfg: FrozenConfig) -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = _ask(FakeAdapter([anthropic.APITimeoutError(request=request)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "timeout"


def test_server_error_recorded(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_api_error(anthropic.InternalServerError, 500)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"


def test_auth_error_aborts_whole_batch(demo_cfg: FrozenConfig) -> None:
    """鉴权错误会逐调用复现 → 立即中止,不烧 71 次无效调用。"""
    with pytest.raises(FatalProviderError, match="AuthenticationError"):
        _ask(FakeAdapter([_api_error(anthropic.AuthenticationError, 401)]), demo_cfg)


def test_missing_api_key_fails_fast(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无密钥时在首次真实调用前给出明确指引(密钥只经环境变量)。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = AnthropicAdapter()  # 构造不要求密钥(测试/估价无需联网)
    with pytest.raises(FatalProviderError, match="ANTHROPIC_API_KEY"):
        adapter.ask(demo_cfg.questions[0], demo_cfg, run_id="r1", run_index=0)
