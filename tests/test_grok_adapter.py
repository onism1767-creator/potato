"""Grok(xAI)适配器离线测试:不联网、不花钱、不需要密钥。

响应形状照真机返回(2026-11-19,grok-4.3)构造:xAI 的 Responses API 与 OpenAI 同形,
另带 usage.cost_in_usd_ticks / server_side_tool_usage_details 等扩展字段。
"""

import json
from collections.abc import Callable
from typing import Any, cast

import httpx2 as httpx  # openai 3.x 基于 httpx2
import openai
import pytest
from openai._models import construct_type
from openai.types.responses import Response

from aivis.contracts.adapter import FatalProviderError, SurfaceSpec
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import RawResponse
from aivis.providers import catalog, registry
from aivis.providers import grok as grok_module
from aivis.providers.grok import _SPECS, GrokAdapter, _cited_sentence

# ---------------------------------------------------------------- 响应构造器


def _usage(**extra: Any) -> dict[str, Any]:
    return {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": 200,
        "output_tokens_details": {"reasoning_tokens": 50},
        "total_tokens": 1200,
        **extra,
    }


def _response(
    output: list[dict[str, Any]],
    *,
    status: str = "completed",
    model: str = "grok-4.3",
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
            "temperature": 0.0,
            "top_p": None,
            "usage": usage or _usage(),
        }
    )


def _search_call(status: str = "completed", action_type: str = "search") -> dict[str, Any]:
    action: dict[str, Any] = {"type": action_type}
    if action_type == "search":
        action["query"] = "best packs"
        # 搜到的结果:不是引用(保守口径,不计入 citations)
        action["sources"] = [{"type": "url", "url": "https://unused.example/result"}]
    else:
        action["url"] = "https://reviews.example/x"
    return {"id": "ws_1", "type": "web_search_call", "status": status, "action": action}


def _marked(sentences: list[tuple[str, list[str]]]) -> tuple[str, list[dict[str, Any]]]:
    """按 xAI 行内标记 `[[n]](url)` 拼正文,并给出与真机同形的标注(区间只盖标记,title=序号)。"""
    text = ""
    annotations: list[dict[str, Any]] = []
    numbers: dict[str, int] = {}
    for sentence, urls in sentences:
        text += sentence
        for url in urls:
            n = numbers.setdefault(url, len(numbers) + 1)
            marker = f"[[{n}]]({url})"
            annotations.append(
                {
                    "type": "url_citation",
                    "url": url,
                    "title": str(n),
                    "start_index": len(text),
                    "end_index": len(text) + len(marker),
                }
            )
            text += marker
        text += " "
    return text.rstrip(), annotations


def _message(text: str, annotations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": annotations or []}],
    }


class FakeAdapter(GrokAdapter):
    def __init__(self, outcomes: list[Response | Exception], model: str = "grok-4.3") -> None:
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
    request = httpx.Request("POST", "https://api.x.ai/v1/responses")
    response = httpx.Response(status, request=request)
    return cls("test error", response=response, body=None)  # type: ignore[no-any-return]


def _ask(adapter: FakeAdapter, cfg: FrozenConfig) -> RawResponse:
    return adapter.ask(cfg.questions[0], cfg, run_id="r1", run_index=0)


# ---------------------------------------------------------------- 元数据与成本


def test_catalog_specs_and_registry_agree() -> None:
    assert {m.model for m in catalog.list_models("grok")} == set(_SPECS)
    a = GrokAdapter(model="grok-4.7")
    assert a.provider_name == "grok" and a.surface_id == "grok_responses_web_search"
    assert a.tool_path == "web_search" and a.tool_version == "web_search"
    assert GrokAdapter(tier="economy").model_name == "grok-4.3"
    assert GrokAdapter().model_name == "grok-4.7"
    with pytest.raises(ValueError, match="未知 Grok 模型"):
        GrokAdapter(model="grok-9")
    b = registry.make_adapter(SurfaceSpec.parse("grok:grok-4.20-0309-reasoning"))
    assert isinstance(b, GrokAdapter) and b.model_name == "grok-4.20-0309-reasoning"
    assert registry.resolve(SurfaceSpec(provider="grok")).model == "grok-4.7"


def test_honesty_flags_no_region_but_temperature() -> None:
    """xAI web_search 无区域参数 → 全部 supports_region=False;温度可传 → True。"""
    models = catalog.list_models("grok")
    assert models and all(not m.supports_region for m in models)
    assert all(m.supports_temperature and m.pinned for m in models)
    assert all(m.surface_kind == "answer" for m in models)
    assert catalog.PROVIDERS["grok"].env_key == "XAI_API_KEY"
    assert catalog.PROVIDERS["grok"].endpoint == "api.x.ai"


def test_estimate_is_model_aware_upper_bound(demo_cfg: FrozenConfig) -> None:
    n = 10
    cheap = GrokAdapter(model="grok-4.3").estimate_cost_usd(n, demo_cfg)
    main = GrokAdapter(model="grok-4.7").estimate_cost_usd(n, demo_cfg)
    assert 0 < cheap < main
    # 按"轮"估:max_uses=3 轮 × (4 万输入 + 3 次计费工具调用),输出 5000
    input_tokens = 2000 + 3 * 40000
    per_call = input_tokens / 1e6 * 1.25 + 5000 / 1e6 * 2.5 + 9 * 0.005
    assert cheap == pytest.approx(n * per_call)


# 真机冒烟账单(2026-11-19,configs/smoke:1 题 1 轮,max_uses=3;另加两次探针调用)
_REAL_COST_PER_CALL = {
    "grok-4.20-0309-non-reasoning": [0.0763],
    "grok-4.20-0309-reasoning": [0.0794],
    "grok-4.3": [0.0479, 0.0457],
    "grok-4.5": [0.1232],
    "grok-4.6": [0.1359],
    "grok-4.7": [0.1970, 0.1572],
}


def test_estimate_covers_every_real_smoke_call(demo_cfg: FrozenConfig) -> None:
    """估算必须是真实花费的上界(real-run-lessons Bug#4 的教训:低估会冲破预算)。
    第一版照搬"每搜索 1.2 万 token"口径,被真机打穿(6 个表面里 5 个超估算)——这是回归守卫。"""
    assert demo_cfg.max_uses == 3
    for model, costs in _REAL_COST_PER_CALL.items():
        est = GrokAdapter(model=model).estimate_cost_usd(1, demo_cfg)
        assert est > max(costs), model


def test_actual_cost_prefers_api_reported_ticks(demo_cfg: FrozenConfig) -> None:
    """真机数字:478,943,500 ticks = $0.04789435(1e10 ticks = $1)。"""
    usage = _usage(
        input_tokens=23227,
        input_tokens_details={"cached_tokens": 128, "cache_write_tokens": 0},
        output_tokens=1598,
        cost_in_usd_ticks=478943500,
        server_side_tool_usage_details={"web_search_calls": 3},
    )
    resp = _ask(FakeAdapter([_response([_search_call(), _message("x")], usage=usage)]), demo_cfg)
    adapter = GrokAdapter(model="grok-4.3")
    assert adapter.actual_cost_usd(resp) == pytest.approx(0.04789435)
    # 自报花费与"单价表 + 工具费"的回退算法应当对得上(单价表没抄错的证据)
    raw = json.loads(resp.raw_json)
    del raw["response"]["usage"]["cost_in_usd_ticks"]
    fallback = adapter.actual_cost_usd(resp.model_copy(update={"raw_json": json.dumps(raw)}))
    assert fallback == pytest.approx(0.04789435, abs=1e-6)
    assert raw["web_search_calls"] == 3  # 计费调用数取 xAI 自报,不是输出项数(这里只有 1 项)


def test_actual_cost_fallback_cached_and_long_context(demo_cfg: FrozenConfig) -> None:
    usage = _usage(
        input_tokens=10000,
        input_tokens_details={"cached_tokens": 4000, "cache_write_tokens": 0},
        output_tokens=1000,
    )
    calls = [_search_call(), _search_call(action_type="open_page"), _message("x")]
    resp = _ask(FakeAdapter([_response(calls, usage=usage)]), demo_cfg)
    adapter = GrokAdapter(model="grok-4.7")
    # 未命中 6000×$2 + 命中 4000×$0.5 + 输出 1000×$6 + 2 次工具调用×$0.005(open_page 也计)
    base = 6000 / 1e6 * 2.0 + 4000 / 1e6 * 0.5 + 1000 / 1e6 * 6.0
    assert adapter.actual_cost_usd(resp) == pytest.approx(base + 2 * 0.005)
    assert resp.search_count == 1  # 只有 search 动作算"搜索次数"
    # 提示 ≥ 20 万 token:整次请求 token 费翻倍,工具费不变
    big = _usage(input_tokens=200_000, output_tokens=1000)
    resp = _ask(FakeAdapter([_response([_message("x")], usage=big)]), demo_cfg)
    assert adapter.actual_cost_usd(resp) == pytest.approx(
        2 * (200_000 / 1e6 * 2.0 + 1000 / 1e6 * 6.0)
    )


def test_actual_cost_bad_json_and_failure_are_zero(demo_cfg: FrozenConfig) -> None:
    adapter = GrokAdapter(model="grok-4.3")
    failed = _ask(FakeAdapter([_api_error(openai.RateLimitError, 429)]), demo_cfg)
    assert adapter.actual_cost_usd(failed) == 0.0
    assert adapter.actual_cost_usd(failed.model_copy(update={"raw_json": "not json"})) == 0.0


# ---------------------------------------------------------------- 成功路径


def test_parses_answer_citations_and_search_count(demo_cfg: FrozenConfig) -> None:
    text, anns = _marked(
        [
            (
                "**Alpha Packs leads the field.** Reviewers agree it is comfortable.",
                ["https://reviews.example/best", "https://alphapacks.example/p"],
            ),
            ("Bravo Gear is the budget pick.", ["https://reviews.example/best"]),
        ]
    )
    usage = _usage(server_side_tool_usage_details={"web_search_calls": 2})
    resp = _ask(
        FakeAdapter(
            [_response([_search_call(), _search_call(), _message(text, anns)], usage=usage)]
        ),
        demo_cfg,
    )
    assert resp.finished_ok and resp.error_code is None
    assert resp.answer_text == text
    assert [c.url for c in resp.citations] == [
        "https://reviews.example/best",
        "https://alphapacks.example/p",
        "https://reviews.example/best",
    ]
    assert [c.position for c in resp.citations] == [0, 1, 2]
    # 一句话挂两个来源:两条引用共用同一句论述;片段里不含任何标记或 URL
    assert resp.citations[0].cited_text == "Reviewers agree it is comfortable."
    assert resp.citations[1].cited_text == "Reviewers agree it is comfortable."
    assert resp.citations[2].cited_text == "Bravo Gear is the budget pick."
    assert all(c.title is None for c in resp.citations)  # 序号不是页面标题
    assert "unused.example" not in {c.url for c in resp.citations}  # 搜到未引 ≠ 引用
    assert resp.search_count == 2 and resp.model == "grok-4.3"
    raw = json.loads(resp.raw_json)
    assert raw["response"]["id"] == "resp_test" and raw["web_search_calls"] == 2


def test_zero_width_annotations_are_browsed_sources_not_citations(demo_cfg: FrozenConfig) -> None:
    """真机(grok-4.7,两次):回答不写行内标记,xAI 仍返回一批零宽标注(start=end=0,title=url)
    ——那是代理浏览过的全部来源,不是回答引用的来源。保守计数:不算引用;原样留在 raw_json。"""
    browsed = [
        {"type": "url_citation", "url": u, "title": u, "start_index": 0, "end_index": 0}
        for u in ("https://shop.example/atmos", "https://alphapacks.example/p")
    ]
    resp = _ask(
        FakeAdapter([_response([_search_call(), _message("Alpha Packs is solid.", browsed)])]),
        demo_cfg,
    )
    assert resp.finished_ok and resp.answer_text == "Alpha Packs is solid."
    assert resp.citations == ()
    raw = json.loads(resp.raw_json)
    kept = raw["response"]["output"][1]["content"][0]["annotations"]
    assert [a["url"] for a in kept] == [b["url"] for b in browsed]  # 审计线索不丢
    # 混合形态:行内标记照常算,零宽的照常不算
    text, anns = _marked([("Alpha Packs is solid.", ["https://reviews.example/best"])])
    resp = _ask(FakeAdapter([_response([_message(text, anns + browsed)])]), demo_cfg)
    assert [c.url for c in resp.citations] == ["https://reviews.example/best"]


def test_request_params_pin_cleanroom_surface(demo_cfg: FrozenConfig) -> None:
    """clean-room:无 instructions;不留存;只开 web_search(无区域参数、不开 x_search);
    max_uses → max_turns(xAI 专有,经 extra_body);温度照传;
    不传 max_tool_calls(真机验证 xAI 忽略它,传了就是假装有上限)。"""
    adapter = FakeAdapter([_response([_message("x")])])
    _ask(adapter, demo_cfg)
    params = adapter.sent_params[0]
    assert "instructions" not in params and params["store"] is False
    assert params["model"] == "grok-4.3" and params["input"] == demo_cfg.questions[0].text
    assert params["tools"] == [{"type": "web_search"}]
    assert params["extra_body"] == {"max_turns": demo_cfg.max_uses}
    assert params["temperature"] == demo_cfg.temperature
    assert "max_tool_calls" not in params


def test_client_targets_xai_only_and_drops_openai_identity(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """密钥取自 XAI_API_KEY(不是 OPENAI_API_KEY),主机是 api.x.ai;
    本机若配了 OpenAI 的组织/项目 ID,不许作为请求头带给 xAI。"""
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-be-used")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-private")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-private")
    # SDK 还会把这个环境变量里的任意请求头并进每个请求,连 Authorization 都能被它顶替
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS", "X-Leak-Tenant: acme\nAuthorization: Bearer sk-openai-ambient"
    )
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(401, json={"error": "stop here"})

    real_client = openai.OpenAI

    def fake_client(**kw: Any) -> openai.OpenAI:
        return real_client(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(openai, "OpenAI", fake_client)
    with pytest.raises(FatalProviderError):
        GrokAdapter(model="grok-4.3").ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    assert seen["host"] == "api.x.ai"
    assert seen["headers"]["authorization"] == "Bearer xai-test-not-a-real-key"
    assert "openai-organization" not in seen["headers"]
    assert "openai-project" not in seen["headers"]
    assert "x-leak-tenant" not in seen["headers"]
    assert seen["body"]["max_turns"] == demo_cfg.max_uses  # extra_body 真的进了请求体
    assert seen["body"]["store"] is False and "instructions" not in seen["body"]


def test_one_request_per_ask_no_silent_retries_and_bounded_wait(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次 ask 只发一次请求:SDK 默认会自动重试 2 次,重试的那几次若已在服务端计费,
    花费进不了账(只记成一次 $0 的失败)。出错就如实记一次失败,由流水线继续/续跑。
    流式接收下客户端超时 = 多久收不到数据(120 秒),不再是"整次调用必须在 N 秒内做完"。"""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(500, json={"error": "upstream hiccup"})

    clients = _wire(monkeypatch, handler)
    resp = _real_ask(demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    assert len(attempts) == 1
    assert clients[0].max_retries == 0 and clients[0].timeout == 120.0


# ---------------------------------------------------------------- 流式接收
#
# 真机核实(2026-11-19,grok-4.3):流式下 1.5 秒到首个事件、全程 23 秒、事件间最长空档 5.5 秒;
# 终止事件 response.completed 带完整 response(引用标注、cost_in_usd_ticks、计费调用数俱全)。


def _wire(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[openai.OpenAI]:
    """让适配器真实走 SDK → 假传输层(不联网);返回它建出来的客户端供断言。"""
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-a-real-key")
    clients: list[openai.OpenAI] = []
    real_client = openai.OpenAI

    def fake_client(**kw: Any) -> openai.OpenAI:
        client = real_client(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", fake_client)
    return clients


def _real_ask(cfg: FrozenConfig) -> RawResponse:
    return GrokAdapter(model="grok-4.3").ask(cfg.questions[0], cfg, run_id="r", run_index=0)


def _sse(events: list[dict[str, Any]]) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8")
    )


def _final_event(kind: str, response: Response) -> dict[str, Any]:
    return {"type": kind, "sequence_number": 99, "response": response.model_dump(mode="json")}


_PROGRESS_EVENTS: list[dict[str, Any]] = [
    {"type": "response.web_search_call.searching", "sequence_number": 1, "item_id": "ws_1"},
    {
        "type": "response.output_text.delta",
        "sequence_number": 2,
        "item_id": "msg_1",
        "output_index": 1,
        "content_index": 0,
        "delta": "Osprey",
        "logprobs": [],
    },
]


def test_stream_returns_the_full_final_response(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流式接收:中途事件只用来保持连接,结果取终止事件带的完整 response,解析与非流式同一条路。"""
    text, anns = _marked([("Osprey is the top pick", ["https://a.example/1"])])
    final = _response(
        [_search_call(), _message(text, anns)], usage=_usage(cost_in_usd_ticks=491_092_500)
    )
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _sse([*_PROGRESS_EVENTS, _final_event("response.completed", final)])

    _wire(monkeypatch, handler)
    adapter = GrokAdapter(model="grok-4.3")
    resp = adapter.ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    assert seen["body"]["stream"] is True and seen["body"]["max_turns"] == demo_cfg.max_uses
    assert resp.finished_ok and resp.answer_text == text and resp.search_count == 1
    assert [(c.url, c.cited_text) for c in resp.citations] == [
        ("https://a.example/1", "Osprey is the top pick")
    ]
    assert adapter.actual_cost_usd(resp) == pytest.approx(0.04910925)


def test_stream_incomplete_is_recorded_with_its_response_and_cost(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = _response(
        [_message("cut")],
        status="incomplete",
        incomplete_reason="max_output_tokens",
        usage=_usage(cost_in_usd_ticks=200_000_000),
    )
    _wire(monkeypatch, lambda request: _sse([_final_event("response.incomplete", cut)]))
    adapter = GrokAdapter(model="grok-4.3")
    resp = adapter.ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    assert not resp.finished_ok and resp.error_code == "other"
    assert "max_output_tokens" in (resp.error_detail or "")
    assert adapter.actual_cost_usd(resp) == pytest.approx(0.02)  # 被截断的那次也花了钱,照记


def test_stream_failed_event_is_recorded(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = _response([], status="failed")
    _wire(monkeypatch, lambda request: _sse([_final_event("response.failed", failed)]))
    resp = _real_ask(demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    assert "status=failed" in (resp.error_detail or "")


def test_stream_cut_off_before_final_event_is_a_failure(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连接中途断了(只收到进度事件,没等到终止事件):记为失败,不许当成空回答。"""
    _wire(monkeypatch, lambda request: _sse(_PROGRESS_EVENTS))
    resp = _real_ask(demo_cfg)
    assert not resp.finished_ok and resp.error_code == "timeout"
    assert resp.answer_text == "" and "APIConnectionError" in (resp.error_detail or "")


def test_stream_error_event_is_recorded(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = {"type": "error", "sequence_number": 3, "error": {"message": "upstream exploded"}}
    _wire(monkeypatch, lambda request: _sse([*_PROGRESS_EVENTS, error]))
    resp = _real_ask(demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    assert "upstream exploded" in (resp.error_detail or "")


def test_stream_total_deadline_stops_an_endless_stream(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一直有数据但迟迟不结束:空闲超时管不住,靠单次调用总时长上限(900 秒)兜底。"""
    final = _response([_message("late")])
    _wire(
        monkeypatch,
        lambda request: _sse([*_PROGRESS_EVENTS, _final_event("response.completed", final)]),
    )
    ticks = iter([0.0, 901.0, 902.0, 903.0, 904.0])
    monkeypatch.setattr(grok_module, "_monotonic", lambda: next(ticks))
    resp = _real_ask(demo_cfg)
    assert not resp.finished_ok and resp.error_code == "timeout"
    assert "APITimeoutError" in (resp.error_detail or "")


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


def test_completed_without_answer_text_is_failure_not_empty_answer(
    demo_cfg: FrozenConfig,
) -> None:
    """status=completed 但没有任何回答文本(只搜了没答):记为失败,不当成"空回答"(CLAUDE.md §9.4)。"""
    adapter = FakeAdapter(
        [_response([_search_call()], usage=_usage(cost_in_usd_ticks=123_000_000))]
    )
    resp = _ask(adapter, demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"
    assert resp.answer_text == "" and resp.search_count == 1
    # 钱已经花了:原始返回留底,花费照记
    assert adapter.actual_cost_usd(resp) == pytest.approx(0.0123)


def test_refusal_is_recorded_as_failure(demo_cfg: FrozenConfig) -> None:
    refusal = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "refusal", "refusal": "I can't help with that."}],
    }
    resp = _ask(FakeAdapter([_response([refusal])]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "refusal"
    assert "can't help" in (resp.error_detail or "")


def test_unexpected_response_shape_is_recorded_not_raised(demo_cfg: FrozenConfig) -> None:
    """SDK 不校验 xAI 的返回:形状出乎意料时(这里是标注缺位置)钱已经花了——
    原样留底、记为 parse_error、花费照记,不许异常冲出 ask 让这次调用既没留底也没记账。"""
    payload = _response(
        [_message("Osprey wins.", [])], usage=_usage(cost_in_usd_ticks=456_000_000)
    ).model_dump(mode="json")
    payload["output"][0]["content"][0]["annotations"] = [
        {
            "type": "url_citation",
            "url": "https://a.example/1",
            "title": "1",
            "start_index": None,
            "end_index": None,
        }
    ]
    adapter = FakeAdapter([cast(Response, construct_type(type_=Response, value=payload))])
    resp = _ask(adapter, demo_cfg)
    assert not resp.finished_ok and resp.error_code == "parse_error"
    assert resp.citations == () and "TypeError" in (resp.error_detail or "")
    assert "Osprey wins." in resp.raw_json  # 原始返回留底可审计
    assert adapter.actual_cost_usd(resp) == pytest.approx(0.0456)


def test_actual_cost_edge_cases_stay_conservative(demo_cfg: FrozenConfig) -> None:
    # API 自报 0 但 token 明明用了:不信这个 0,回退按单价算(宁可多记)
    adapter = FakeAdapter(
        [_response([_search_call(), _message("ok")], usage=_usage(cost_in_usd_ticks=0))]
    )
    resp = _ask(adapter, demo_cfg)
    assert adapter.actual_cost_usd(resp) == pytest.approx(
        1000 / 1e6 * 1.25 + 200 / 1e6 * 2.5 + 0.005
    )
    # usage 里混进非数字:记 0,不许异常冲出去把 probe 的记账打断
    garbled = resp.model_copy(
        update={
            "raw_json": json.dumps(
                {"response": {"usage": {"input_tokens": "n/a"}}, "web_search_calls": "?"}
            )
        }
    )
    assert adapter.actual_cost_usd(garbled) == 0.0


def test_rate_limit_and_timeout_recorded(demo_cfg: FrozenConfig) -> None:
    resp = _ask(FakeAdapter([_api_error(openai.RateLimitError, 429)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "rate_limit"
    request = httpx.Request("POST", "https://api.x.ai/v1/responses")
    resp = _ask(FakeAdapter([openai.APITimeoutError(request=request)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "timeout"
    resp = _ask(FakeAdapter([_api_error(openai.InternalServerError, 500)]), demo_cfg)
    assert not resp.finished_ok and resp.error_code == "other"


def test_bad_key_and_unknown_model_abort_whole_batch(demo_cfg: FrozenConfig) -> None:
    """真机核实:xAI 对坏密钥返 400、未知模型返 404 —— 都会逐调用复现,必须中止整批。"""
    with pytest.raises(FatalProviderError, match="BadRequestError"):
        _ask(FakeAdapter([_api_error(openai.BadRequestError, 400)]), demo_cfg)
    with pytest.raises(FatalProviderError, match="NotFoundError"):
        _ask(FakeAdapter([_api_error(openai.NotFoundError, 404)]), demo_cfg)
    with pytest.raises(FatalProviderError, match="AuthenticationError"):
        _ask(FakeAdapter([_api_error(openai.AuthenticationError, 401)]), demo_cfg)


def test_missing_api_key_fails_fast(
    demo_cfg: FrozenConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-be-used")  # 有别家的 key 也不许借用
    with pytest.raises(FatalProviderError, match="XAI_API_KEY"):
        GrokAdapter(model="grok-4.3").ask(demo_cfg.questions[0], demo_cfg, run_id="r1", run_index=0)


# ---------------------------------------------------------------- 引用片段


def test_cited_sentence_shapes() -> None:
    text, anns = _marked(
        [
            ("Intro line.", []),
            ("Osprey is the top pick", ["https://a.example/1"]),
            (", while Deuter is cheaper.", ["https://deuter.example/2", "https://a.example/1"]),
        ]
    )
    spans = [(a["start_index"], a["end_index"]) for a in anns]
    # 句中标记:片段就是标记前的那半句
    assert _cited_sentence(text, spans, *spans[0]) == "Osprey is the top pick"
    # 前一个标记是断句点,且它的 URL 不会漏进后一句的片段
    assert _cited_sentence(text, spans, *spans[1]) == "while Deuter is cheaper."
    assert _cited_sentence(text, spans, *spans[2]) == "while Deuter is cheaper."
    # 区间不是标记(关闭行内引用等形态)→ 原样返回;越界 → None
    assert _cited_sentence(text, [], 0, 11) == "Intro line."
    assert _cited_sentence(text, spans, 5, 9999) is None


def _spans_of(text: str, *markers: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for marker in markers:
        start = text.index(marker, cursor)
        spans.append((start, start + len(marker)))
        cursor = start + len(marker)
    return spans


_M1 = "[[1]](https://a.example/1)"
_M2 = "[[2]](https://b.example/2)"


def test_cited_sentence_breaks_cjk_sentences_without_whitespace() -> None:
    # 中文句末标点后通常不跟空格:上一句(说的是别的品牌)不许漏进本句的引用片段,
    # 否则 extract 会把这条引用也记给上一句的品牌(违反保守计数)
    text = f"甲品牌在入门档口碑最好。乙品牌的续航更长{_M1}。"
    spans = _spans_of(text, _M1)
    assert _cited_sentence(text, spans, *spans[0]) == "乙品牌的续航更长"
    # 标记写在句号之后:仍只归它紧前面那一句
    text = f"甲品牌在入门档口碑最好！乙品牌的续航更长。{_M1}"
    spans = _spans_of(text, _M1)
    assert _cited_sentence(text, spans, *spans[0]) == "乙品牌的续航更长。"


def test_cited_sentence_marker_after_full_stop_and_spaced_markers() -> None:
    # 标记在句号+空格之后,且两个标记之间隔了空格:同一串,共用紧前面那一句
    text = f"Alpha is the budget pick. Beta lasts longer. {_M1} {_M2} Gamma is new."
    spans = _spans_of(text, _M1, _M2)
    assert _cited_sentence(text, spans, *spans[0]) == "Beta lasts longer."
    assert _cited_sentence(text, spans, *spans[1]) == "Beta lasts longer."


def test_cited_sentence_without_a_supported_sentence_is_none() -> None:
    # 另起一行的标记:前面没有可归属的句子 → None(保守,不把上一行猜给它)
    text = f"Alpha is cheap {_M1}\n{_M2} Beta is durable."
    spans = _spans_of(text, _M1, _M2)
    assert _cited_sentence(text, spans, *spans[1]) is None
    text = f"Alpha is cheap.\n{_M1} Beta is durable."
    spans = _spans_of(text, _M1)
    assert _cited_sentence(text, spans, *spans[0]) is None
    # 标记前只剩标点 → None(片段里得有字)
    text = f"Alpha is cheap {_M1}. {_M2}"
    spans = _spans_of(text, _M1, _M2)
    assert _cited_sentence(text, spans, *spans[1]) is None


def test_cited_sentence_window_is_bounded() -> None:
    long_sentence = "word " * 200 + "Alpha Packs wins"
    text, anns = _marked([(long_sentence, ["https://a.example/1"])])
    spans = [(a["start_index"], a["end_index"]) for a in anns]
    cited = _cited_sentence(text, spans, *spans[0])
    assert cited is not None and len(cited) <= 400 and cited.endswith("Alpha Packs wins")
