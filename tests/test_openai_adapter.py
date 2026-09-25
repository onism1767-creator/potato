"""OpenAI 适配器离线测试:不联网、不花钱、不需要密钥(以 SDK pydantic 类型构造真实形状的响应)。"""

import json
from typing import Any

import httpx2 as httpx  # openai 3.x 基于 httpx2
import openai
import pytest
from openai.types.responses import Response

from aivis.contracts.adapter import FatalProviderError
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import RawResponse
from aivis.providers import catalog
from aivis.providers.openai import _SPECS, OpenAIAdapter

# ---------------------------------------------------------------- 响应构造器


def _response(
    output: list[dict[str, Any]],
    *,
    status: str = "completed",
    model: str = "gpt-6-luna",
    usage: dict[str, Any] | None = None,
    incomplete_reason: str | None = None,
) -> Response:
    return Response.model_validate(
        {
            "id": "resp_test",
            "object": "response",
            "created_at": 1_700_000_000,
            "model": model,
            "status": status,
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [{"type": "web_search"}],
            "error": None,
            "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
            "instructions": None,
            "metadata": None,
            "temperature": None,
            "top_p": None,
            "usage": usage
            or {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 200,
                "output_tokens_details": {"reasoning_tokens": 50},
                "total_tokens": 1200,
            },
        }
    )


def _search_call(status: str = "completed", action_type: str = "search") -> dict[str, Any]:
    action: dict[str, Any] = {"type": action_type}
    if action_type == "search":
        action["query"] = "best packs"
    else:
        action["url"] = "https://reviews.example/x"
    return {"id": "ws_1", "type": "web_search_call", "status": status, "action": action}


def _message(text: str, annotations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": annotations or []}],
    }


def _cite(url: str, title: str, start: int, end: int) -> dict[str, Any]:
    return {
        "type": "url_citation",
        "url": url,
        "title": title,
        "start_index": start,
        "end_index": end,
    }


class FakeAdapter(OpenAIAdapter):
    def __init__(self, outcomes: list[Response | Exception], model: str = "gpt-6-luna") -> None:
        super().__init__(model=model)
        self._outcomes = list(outcomes)
        self.sent_params: list[dict[str, Any]] = []

    def _send(self, params: dict[str, Any]) -> Response:
        self.sent_params.append(params)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _api_error(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request)
    return cls("test error", response=response, body=None)  # type: ignore[no-any-return]


def _ask(adapter: FakeAdapter, cfg: FrozenConfig) -> RawResponse:
    return adapter.ask(cfg.questions[0], cfg, run_id="r1", run_index=0)


# ---------------------------------------------------------------- 元数据与成本


def test_catalog_and_specs_agree() -> None:
    assert {m.model for m in catalog.list_models("openai")} == set(_SPECS)
    a = OpenAIAdapter(model="gpt-6-astra")
    assert a.provider_name == "openai" and a.surface_id == "openai_responses_web_search"
    assert a.tool_path == "web_search" and a.info.tier == "flagship"
    assert OpenAIAdapter(tier="standard").model_name == "gpt-6-sol"
    with pytest.raises(ValueError, match="未知 OpenAI 模型"):
        OpenAIAdapter(model="gpt-9")


def test_estimate_is_model_aware_upper_bound(demo_cfg: FrozenConfig) -> None:
    n = 10
    luna = OpenAIAdapter(model="gpt-6-luna").estimate_cost_usd(n, demo_cfg)
    astra = OpenAIAdapter(model="gpt-6-astra").estimate_cost_usd(n, demo_cfg)
    assert 0 < luna < astra
    input_tokens = 2000 + 3 * 12000
    per_call = input_tokens / 1e6 * 0.1 + 4000 / 1e6 * 0.5 + 3 * 0.01
    assert luna == pytest.approx(n * per_call)


def test_actual_cost_uses_cached_rate_and_tool_calls(demo_cfg: FrozenConfig) -> None:
    resp = _ask(
        FakeAdapter(
            [
                _response(
                    [_search_call(), _search_call(action_type="open_page"), _message("x")],
                    usage={
                        "input_tokens": 10000,
                        "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 0},
                        "output_tokens": 1000,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 11000,
                    },
                )
            ]
        ),
        demo_cfg,
    )
    cost = OpenAIAdapter(model="gpt-6-luna").actual_cost_usd(resp)
    # 未命中 6000×$0.1 + 命中 4000×$0.01 + 输出 1000×$0.5 + 2 次工具调用×$0.01(open_page 也计)
    expected = 6000 / 1e6 * 0.1 + 4000 / 1e6 * 0.01 + 1000 / 1e6 * 0.5 + 2 * 0.01
    assert cost == pytest.approx(expected)
    assert resp.search_count == 1  # 只有 search 动作算"搜索次数"


def test_actual_cost_bad_json_is_zero() -> None:
    resp = RawResponse(
        run_id="r",
        question_id="q1",
        run_index=0,
        answer_text="",
        citations=(),
        search_count=0,
        model="gpt-6-luna",
        finished_ok=True,
        raw_json="not json",
        timestamp="2026-09-22T00:00:00Z",
    )
    assert OpenAIAdapter(model="gpt-6-luna").actual_cost_usd(resp) == 0.0


# ---------------------------------------------------------------- 成功路径


def test_parses_answer_citations_and_search_count(demo_cfg: FrozenConfig) -> None:
    text = "Alpha Packs leads the field. Reviewers agree."
    message = _message(
        text,
        [
            _cite("https://alphapacks.example/p", "Alpha Packs | Shop", 0, 27),
            _cite("https://reviews.example/best", "Best packs 2026", 28, 44),
        ],
    )
    resp = _ask(FakeAdapter([_response([_search_call(), _search_call(), message])]), demo_cfg)
    assert resp.finished_ok and resp.error_code is None
    assert resp.answer_text == text
    assert [c.url for c in resp.citations] == [
        "https://alphapacks.example/p",
        "https://reviews.example/best",
    ]
    assert resp.citations[0].cited_text == "Alpha Packs leads the field"
    assert resp.citations[1].title == "Best packs 2026"
    assert [c.position for c in resp.citations] == [0, 1]
    assert resp.search_count == 2 and resp.model == "gpt-6-luna"
    raw = json.loads(resp.raw_json)
    assert raw["response"]["id"] == "resp_test" and raw["web_search_calls"] == 2


def test_request_params_pin_cleanroom_surface(demo_cfg: FrozenConfig) -> None:
    """clean-room:无 instructions;不留存;区域钉住;max_uses → max_tool_calls;推理模型不传温度。"""
    adapter = FakeAdapter([_response([_message("x")])])
    _ask(adapter, demo_cfg)
    params = adapter.sent_params[0]
    assert "instructions" not in params and params["store"] is False
    assert params["input"] == demo_cfg.questions[0].text
    assert params["tools"][0] == {
        "type": "web_search",
        "user_location": {"type": "approximate", "country": "US"},
    }
    assert params["max_tool_calls"] == demo_cfg.max_uses
    assert "temperature" not in params


# ---------------------------------------------------------------- 失败路径


def test_failed_search_call_is_tool_error(demo_cfg: FrozenConfig) -> None:
    resp = _ask(
        FakeAdapter([_response([_search_call(status="failed"), _message("partial")])]), demo_cfg
    )
    assert not resp.finished_ok and resp.error_code == "tool_error"
    assert resp.answer_text == "partial"  # 保留上下文作审计


def test_incomplete_status_recorded_as_failure(demo_cfg: FrozenConfig) -> None:
    resp = _ask(
        FakeAdapter(
            [
                _response(
                    [_message("cut")], status="incomplete", incomplete_reason="max_output_tokens"
                )
            ]
        ),
        demo_cfg,
    )
    assert not resp.finished_ok and resp.error_code == "other"
    assert "max_output_tokens" in (resp.error_detail or "")


def test_rate_limit_and_timeout_recorded(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_api_error(openai.RateLimitError, 429)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "rate_limit"
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = _ask(FakeAdapter([openai.APITimeoutError(request=request)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "timeout"
    resp = _ask(FakeAdapter([_api_error(openai.InternalServerError, 500)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"


def test_auth_error_aborts_whole_batch(demo_cfg: FrozenConfig) -> None:
    with pytest.raises(FatalProviderError, match="AuthenticationError"):
        _ask(FakeAdapter([_api_error(openai.AuthenticationError, 401)]), demo_cfg)


def test_missing_api_key_fails_fast(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(FatalProviderError, match="OPENAI_API_KEY"):
        OpenAIAdapter(model="gpt-6-luna").ask(
            demo_cfg.questions[0], demo_cfg, run_id="r1", run_index=0
        )


def test_cited_span_expands_link_marker_to_preceding_sentence() -> None:
    """真机观察:标注区间只盖住 "([site](url))" 链接标记 → cited_text 补上它支撑的那句话。"""
    from aivis.providers.openai import _cited_span

    text = (
        "Intro sentence. The Osprey Atmos is the top pick ([osprey.com](https://osprey.com/x)). Nx."
    )
    start = text.index("([osprey.com]")
    end = text.index("). Nx.") + 1
    assert _cited_span(text, start, end) == (
        "The Osprey Atmos is the top pick ([osprey.com](https://osprey.com/x))"
    )
    # 区间本身就是论述句 → 原样返回;越界 → None
    assert _cited_span(text, 0, 15) == "Intro sentence."
    assert _cited_span(text, 5, 999) is None
