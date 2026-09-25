"""Gemini 适配器离线测试:不联网、不花钱、不需要密钥(以 SDK pydantic 类型构造真实形状的响应)。"""

import json
from typing import Any

import pytest
from google.genai import errors, types

from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import RawResponse
from aivis.providers import catalog
from aivis.providers.gemini import (
    _SPECS,
    FatalProviderError,
    GeminiAdapter,
    is_grounding_redirect,
)

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ1"
REDIRECT2 = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ2"
_REAL = {REDIRECT: "https://alphapacks.example/p", REDIRECT2: "https://reviews.example/best"}


def _fake_resolver(url: str) -> str | None:
    return _REAL.get(url)


def _response(
    *,
    text: str = "Alpha Packs leads. Reviews agree.",
    finish: str = "STOP",
    queries: list[str] | None = None,
    chunks: list[dict[str, Any]] | None = None,
    supports: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    block_reason: str | None = None,
) -> types.GenerateContentResponse:
    payload: dict[str, Any] = {
        "model_version": "gemini-3.8-flash-001",
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finish_reason": finish,
                "grounding_metadata": {
                    "web_search_queries": queries if queries is not None else ["best packs"],
                    "grounding_chunks": chunks or [],
                    "grounding_supports": supports or [],
                },
            }
        ],
        "usage_metadata": usage
        or {
            "prompt_token_count": 100,
            "candidates_token_count": 50,
            "thoughts_token_count": 20,
            "tool_use_prompt_token_count": 3000,
            "total_token_count": 3170,
        },
    }
    if block_reason:
        payload["prompt_feedback"] = {"block_reason": block_reason}
    return types.GenerateContentResponse.model_validate(payload)


def _chunk(uri: str, title: str) -> dict[str, Any]:
    return {"web": {"uri": uri, "title": title}}


def _support(text: str, start: int, end: int, idx: list[int]) -> dict[str, Any]:
    return {
        "segment": {"start_index": start, "end_index": end, "text": text},
        "grounding_chunk_indices": idx,
        "confidence_scores": [0.9] * len(idx),
    }


class FakeAdapter(GeminiAdapter):
    def __init__(
        self,
        outcomes: list[types.GenerateContentResponse | Exception],
        model: str = "gemini-3.8-flash",
    ) -> None:
        super().__init__(model=model, redirect_resolver=_fake_resolver)
        self._outcomes = list(outcomes)
        self.sent: list[tuple[str, types.GenerateContentConfig]] = []

    def _send(
        self, contents: str, config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.sent.append((contents, config))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ask(adapter: FakeAdapter, cfg: FrozenConfig) -> RawResponse:
    return adapter.ask(cfg.questions[0], cfg, run_id="r1", run_index=0)


# ---------------------------------------------------------------- 元数据与成本


def test_catalog_and_specs_agree() -> None:
    assert {m.model for m in catalog.list_models("gemini")} == set(_SPECS)
    a = GeminiAdapter(model="gemini-3.1-pro-preview", redirect_resolver=None)
    assert a.provider_name == "gemini" and a.surface_id == "gemini_generate_content_google_search"
    assert a.tool_path == "google_search" and a.info.supports_region is False
    assert GeminiAdapter(tier="standard", redirect_resolver=None).model_name == "gemini-3.8-flash"
    with pytest.raises(ValueError, match="未知 Gemini 模型"):
        GeminiAdapter(model="gemini-9", redirect_resolver=None)


def test_estimate_assumes_five_searches_per_call(demo_cfg: FrozenConfig) -> None:
    n = 10
    flash = GeminiAdapter(model="gemini-3.8-flash", redirect_resolver=None).estimate_cost_usd(
        n, demo_cfg
    )
    pro = GeminiAdapter(model="gemini-3.1-pro-preview", redirect_resolver=None).estimate_cost_usd(
        n, demo_cfg
    )
    assert 0 < flash < pro
    input_tokens = 2000 + 5 * 12000
    per_call = input_tokens / 1e6 * 0.75 + 4000 / 1e6 * 3.75 + 5 * 0.014
    assert flash == pytest.approx(n * per_call)


def test_actual_cost_counts_thoughts_and_tool_prompt(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_response(queries=["a", "b"])]), demo_cfg)
    cost = GeminiAdapter(model="gemini-3.8-flash", redirect_resolver=None).actual_cost_usd(resp)
    expected = (100 + 3000) / 1e6 * 0.75 + (50 + 20) / 1e6 * 3.75 + 2 * 0.014
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------- 成功路径


def test_parses_supports_into_citations_and_resolves_redirects(demo_cfg: FrozenConfig) -> None:
    resp = _ask(
        FakeAdapter(
            [
                _response(
                    queries=["best packs", "alpha packs review"],
                    chunks=[
                        _chunk(REDIRECT, "alphapacks.example"),
                        _chunk(REDIRECT2, "reviews.example"),
                        _chunk(
                            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/UNUSED",
                            "x",
                        ),
                    ],
                    supports=[
                        _support("Alpha Packs leads.", 0, 18, [0]),
                        _support("Reviews agree.", 19, 33, [1]),
                    ],
                )
            ]
        ),
        demo_cfg,
    )
    assert resp.finished_ok and resp.error_code is None
    assert resp.answer_text == "Alpha Packs leads. Reviews agree."
    assert [c.url for c in resp.citations] == [
        "https://alphapacks.example/p",
        "https://reviews.example/best",
    ]  # 跳转链接已解析成真实网址;未被 support 引用的第三个 chunk 不算引用(保守)
    assert resp.citations[0].cited_text == "Alpha Packs leads."
    assert resp.citations[0].title == "alphapacks.example"
    assert [c.position for c in resp.citations] == [0, 1]
    assert resp.search_count == 2 and resp.model == "gemini-3.8-flash-001"
    raw = json.loads(resp.raw_json)
    assert raw["redirects"] == {REDIRECT: _REAL[REDIRECT], REDIRECT2: _REAL[REDIRECT2]}
    # 原始返回里保留的是原始跳转链接(原始层不改写 provider 数据)
    chunk_uris = [
        c["web"]["uri"]
        for c in raw["response"]["candidates"][0]["grounding_metadata"]["grounding_chunks"]
    ]
    assert chunk_uris[0] == REDIRECT


def test_unresolvable_redirect_keeps_original_url(demo_cfg: FrozenConfig) -> None:
    unknown = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/NOPE"
    resp = _ask(
        FakeAdapter(
            [_response(chunks=[_chunk(unknown, "t")], supports=[_support("x", 0, 1, [0])])]
        ),
        demo_cfg,
    )
    assert resp.citations[0].url == unknown  # 保留原链接,verify 会如实核为无法归属,不静默丢弃
    assert json.loads(resp.raw_json)["redirects"] == {unknown: None}


def test_redirect_resolution_is_cached_per_adapter(demo_cfg: FrozenConfig) -> None:
    calls: list[str] = []

    def counting(url: str) -> str | None:
        calls.append(url)
        return _REAL.get(url)

    adapter = FakeAdapter(
        [
            _response(
                chunks=[_chunk(REDIRECT, "t")],
                supports=[_support("a", 0, 1, [0]), _support("b", 1, 2, [0])],
            )
        ]
    )
    adapter._resolver = counting
    resp = _ask(adapter, demo_cfg)
    assert len(resp.citations) == 2 and calls == [REDIRECT]


def test_request_config_pins_cleanroom_surface(demo_cfg: FrozenConfig) -> None:
    adapter = FakeAdapter([_response()])
    _ask(adapter, demo_cfg)
    contents, config = adapter.sent[0]
    assert contents == demo_cfg.questions[0].text
    assert config.system_instruction is None
    assert config.temperature == demo_cfg.temperature
    assert config.tools and config.tools[0].google_search is not None  # type: ignore[union-attr]


def test_is_grounding_redirect() -> None:
    assert is_grounding_redirect(REDIRECT)
    assert not is_grounding_redirect("https://alphapacks.example/p")
    assert not is_grounding_redirect("not a url")


# ---------------------------------------------------------------- 失败路径


def test_safety_finish_is_refusal(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_response(text="", finish="SAFETY")]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "refusal"


def test_blocked_prompt_is_refusal(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_response(block_reason="SAFETY")]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "refusal"
    assert "prompt blocked" in (resp.error_detail or "")


def test_rate_limit_and_server_error_recorded(demo_cfg: FrozenConfig) -> None:
    resp = _ask(
        FakeAdapter([errors.ClientError(429, {"error": {"message": "slow down"}})]), demo_cfg
    )
    assert not resp.finished_ok and resp.error_code == "rate_limit"
    resp = _ask(FakeAdapter([errors.ServerError(503, {"error": {"message": "busy"}})]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"


def test_auth_error_aborts_whole_batch(demo_cfg: FrozenConfig) -> None:
    with pytest.raises(FatalProviderError, match="HTTP 401"):
        _ask(FakeAdapter([errors.ClientError(401, {"error": {"message": "bad key"}})]), demo_cfg)


def test_missing_api_key_fails_fast(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(FatalProviderError, match="GEMINI_API_KEY"):
        GeminiAdapter(model="gemini-3.8-flash", redirect_resolver=None).ask(
            demo_cfg.questions[0], demo_cfg, run_id="r1", run_index=0
        )
