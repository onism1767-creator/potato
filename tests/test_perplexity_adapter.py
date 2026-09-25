"""Perplexity 适配器离线测试:Agent API(sonar / 预设档)+ Search API,不联网、不花钱、不需要密钥。"""

import json
from typing import Any

import httpx
import perplexity
import pytest
from perplexity.generated.api import ResponseCreateResponse, SearchCreateResponse

from aivis.contracts.adapter import FatalProviderError
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import RawResponse
from aivis.providers import catalog
from aivis.providers.perplexity import _SPECS, PerplexityAdapter, PerplexitySearchAdapter

# ---------------------------------------------------------------- 响应构造器


def _usage(total_cost: float | None = 0.0031, invocations: int | None = 2) -> dict[str, Any]:
    u: dict[str, Any] = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}
    if total_cost is not None:
        u["cost"] = {
            "currency": "USD",
            "input_cost": 0.0001,
            "output_cost": 0.001,
            "tool_calls_cost": 0.002,
            "total_cost": total_cost,
        }
    if invocations is not None:
        u["tool_calls_details"] = {"web_search": {"invocation": invocations}}
    return u


def _search_item(results: list[tuple[int, str, str]]) -> dict[str, Any]:
    return {
        "type": "search_results",
        "queries": ["best packs"],
        "results": [
            {"id": i, "title": t, "url": u, "snippet": "…", "date": None, "last_updated": None}
            for i, t, u in results
        ],
    }


def _message(
    text: str, annotations: list[dict[str, Any]] | None, status: str = "completed"
) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "output_text", "text": text}
    if annotations is not None:
        part["annotations"] = annotations
    return {"type": "message", "id": "m1", "role": "assistant", "status": status, "content": [part]}


def _cite(url: str, title: str, start: int, end: int) -> dict[str, Any]:
    return {
        "type": "url_citation",
        "url": url,
        "title": title,
        "start_index": start,
        "end_index": end,
    }


def _response(
    output: list[dict[str, Any]],
    *,
    status: str = "completed",
    model: str = "perplexity/sonar",
    usage: dict[str, Any] | None = None,
) -> ResponseCreateResponse:
    return ResponseCreateResponse.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 1,
            "model": model,
            "status": status,
            "output": output,
            "usage": usage if usage is not None else _usage(),
        }
    )


class FakeAgent(PerplexityAdapter):
    def __init__(self, outcomes: list[Any], model: str = "perplexity/sonar") -> None:
        super().__init__(model=model)
        self._outcomes = list(outcomes)
        self.sent: list[dict[str, Any]] = []

    def _send(self, params: dict[str, Any]) -> ResponseCreateResponse:
        self.sent.append(params)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[no-any-return]


class FakeSearch(PerplexitySearchAdapter):
    def __init__(self, outcomes: list[Any]) -> None:
        super().__init__()
        self._outcomes = list(outcomes)
        self.sent: list[dict[str, Any]] = []

    def _send(self, params: dict[str, Any]) -> SearchCreateResponse:
        self.sent.append(params)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[no-any-return]


def _api_error(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.perplexity.ai/v1/agent")
    response = httpx.Response(status, request=request)
    return cls("test error", response=response, body=None)  # type: ignore[no-any-return]


def _ask(adapter: PerplexityAdapter | PerplexitySearchAdapter, cfg: FrozenConfig) -> RawResponse:
    return adapter.ask(cfg.questions[0], cfg, run_id="r1", run_index=0)


# ---------------------------------------------------------------- 目录一致性 / 成本


def test_catalog_agent_models_have_specs_and_search_is_search_only() -> None:
    agent_ids = {m.model for m in catalog.list_models("perplexity") if m.model != "search"}
    assert agent_ids == set(_SPECS)
    assert catalog.find_model("perplexity", "search").surface_kind == "search_only"
    a = PerplexityAdapter(model="preset:low")
    assert a.provider_name == "perplexity" and a.surface_id == "perplexity_agent_web_search"
    assert a.info.pinned is False
    assert PerplexityAdapter(tier="economy").model_name == "perplexity/sonar"
    with pytest.raises(ValueError, match="未知 Perplexity"):
        PerplexityAdapter(model="sonar-pro")
    s = PerplexitySearchAdapter()
    assert s.surface_id == "perplexity_search_api" and s.tool_path == "search_api"
    assert s.info.surface_kind == "search_only" and s.model_name == "search"


def test_estimates_positive_and_search_api_flat(demo_cfg: FrozenConfig) -> None:
    n = 10
    sonar = PerplexityAdapter(model="perplexity/sonar").estimate_cost_usd(n, demo_cfg)
    input_tokens = 2000 + 3 * 12000
    per_call = input_tokens / 1e6 * 0.25 + 4000 / 1e6 * 2.5 + 3 * 0.0025
    assert sonar == pytest.approx(n * per_call)
    assert PerplexitySearchAdapter().estimate_cost_usd(n, demo_cfg) == pytest.approx(n * 0.005)


def test_actual_cost_prefers_api_reported_total(demo_cfg: FrozenConfig) -> None:
    resp = _ask(
        FakeAgent([_response([_message("x", [])], usage=_usage(total_cost=0.0123))]), demo_cfg
    )
    assert PerplexityAdapter().actual_cost_usd(resp) == pytest.approx(0.0123)
    # 无 cost 字段 → 回退 token 单价 + 每次搜索工具费
    resp = _ask(
        FakeAgent([_response([_message("x", [])], usage=_usage(total_cost=None, invocations=2))]),
        demo_cfg,
    )
    expected = 1000 / 1e6 * 0.25 + 200 / 1e6 * 2.5 + 2 * 0.0025
    assert PerplexityAdapter(model="perplexity/sonar").actual_cost_usd(resp) == pytest.approx(
        expected
    )


# ---------------------------------------------------------------- Agent API 成功路径


def test_parses_annotations_search_count_and_records_actual_model(demo_cfg: FrozenConfig) -> None:
    text = "Alpha Packs leads. Reviews agree."
    resp = _ask(
        FakeAgent(
            [
                _response(
                    [
                        _search_item([(1, "Alpha Packs", "https://alphapacks.example/p")]),
                        _message(
                            text,
                            [
                                _cite("https://alphapacks.example/p", "Alpha Packs", 0, 18),
                                _cite("https://reviews.example/best", "Best packs", 19, 33),
                            ],
                        ),
                    ],
                    model="openai/gpt-5.6-luna",
                )
            ],
            model="preset:low",
        ),
        demo_cfg,
    )
    assert resp.finished_ok and resp.answer_text == text
    assert [c.url for c in resp.citations] == [
        "https://alphapacks.example/p",
        "https://reviews.example/best",
    ]
    assert (
        resp.citations[0].cited_text == "Alpha Packs leads."
        and resp.citations[1].title == "Best packs"
    )
    assert resp.search_count == 2  # usage.tool_calls_details.web_search.invocation
    assert resp.model == "openai/gpt-5.6-luna"  # 预设档:如实记录实际底层模型
    assert json.loads(resp.raw_json)["response"]["id"] == "resp_1"


def test_marker_fallback_maps_to_search_results(demo_cfg: FrozenConfig) -> None:
    """无 url_citation 标注时,[n] / [web:n] 标记映射结果 id;未被标记的结果不算引用。"""
    resp = _ask(
        FakeAgent(
            [
                _response(
                    [
                        _search_item(
                            [
                                (1, "Alpha", "https://alphapacks.example/p"),
                                (2, "Bravo", "https://bravogear.example/q"),
                                (3, "Unused", "https://unused.example/z"),
                            ]
                        ),
                        _message("Alpha Packs leads [1]. Bravo follows [web:2].", None),
                    ],
                    usage=_usage(invocations=None),
                )
            ]
        ),
        demo_cfg,
    )
    assert [c.url for c in resp.citations] == [
        "https://alphapacks.example/p",
        "https://bravogear.example/q",
    ]
    assert resp.search_count == 1  # 无 invocation 明细 → 按 search_results 项数


def test_request_params_agent(demo_cfg: FrozenConfig) -> None:
    adapter = FakeAgent([_response([_message("x", [])])], model="preset:medium")
    _ask(adapter, demo_cfg)
    p = adapter.sent[0]
    assert p["preset"] == "medium" and "model" not in p
    assert p["input"] == demo_cfg.questions[0].text and "instructions" not in p
    assert p["store"] is False and p["max_steps"] == demo_cfg.max_uses
    assert p["tools"] == [{"type": "web_search", "user_location": {"country": "US"}}]
    assert p["temperature"] == demo_cfg.temperature
    explicit = FakeAgent([_response([_message("x", [])])])
    _ask(explicit, demo_cfg)
    assert explicit.sent[0]["model"] == "perplexity/sonar" and "preset" not in explicit.sent[0]


# ---------------------------------------------------------------- Agent API 失败路径


def test_non_completed_status_is_failure(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAgent([_response([_message("x", [])], status="failed")]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    resp = _ask(FakeAgent([_response([_message("x", [], status="failed")])]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"


def test_transient_errors_recorded_and_auth_aborts(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAgent([_api_error(perplexity.RateLimitError, 429)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "rate_limit"
    request = httpx.Request("POST", "https://api.perplexity.ai/v1/agent")
    resp = _ask(FakeAgent([perplexity.APITimeoutError(request=request)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "timeout"
    resp = _ask(FakeAgent([_api_error(perplexity.InternalServerError, 500)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    with pytest.raises(FatalProviderError, match="AuthenticationError"):
        _ask(FakeAgent([_api_error(perplexity.AuthenticationError, 401)]), demo_cfg)


def test_missing_api_key_fails_fast(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(FatalProviderError, match="PERPLEXITY_API_KEY"):
        _ask(PerplexityAdapter(), demo_cfg)
    with pytest.raises(FatalProviderError, match="PERPLEXITY_API_KEY"):
        _ask(PerplexitySearchAdapter(), demo_cfg)


# ---------------------------------------------------------------- Search API


def test_search_api_results_become_citations_with_empty_answer(demo_cfg: FrozenConfig) -> None:
    sr = SearchCreateResponse.model_validate(
        {
            "id": "s1",
            "results": [
                {
                    "title": "Alpha Packs",
                    "url": "https://alphapacks.example/p",
                    "snippet": "Alpha…",
                },
                {"title": "Best packs", "url": "https://reviews.example/best", "snippet": "Tested"},
            ],
        }
    )
    adapter = FakeSearch([sr])
    resp = _ask(adapter, demo_cfg)
    assert resp.finished_ok and resp.answer_text == ""  # 纯搜索:无回答文本
    assert [c.url for c in resp.citations] == [
        "https://alphapacks.example/p",
        "https://reviews.example/best",
    ]
    assert resp.citations[1].cited_text == "Tested" and resp.search_count == 1
    assert adapter.sent[0] == {
        "query": demo_cfg.questions[0].text,
        "max_results": 10,
        "country": "US",
    }
    assert PerplexitySearchAdapter().actual_cost_usd(resp) == pytest.approx(0.005)


def test_search_api_rate_limit_costs_nothing(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeSearch([_api_error(perplexity.RateLimitError, 429)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "rate_limit"
    assert PerplexitySearchAdapter().actual_cost_usd(resp) == 0.0
