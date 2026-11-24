"""终审(2026-11)在引擎适配器层查出的问题的回归测试。全部离线:不联网、不花钱、不需要真密钥。

覆盖:Gemini 并发建客户端只建一个 + 超时 + 传输层错误记失败;四家解析异常不崩整批;
空回答五家统一记失败(花费照记);端点钉死官方地址(不被环境里的中转站地址带走);
起草器把 API 错误转成 DraftError、品类含品牌名时拒绝。
"""

import threading
import time
import urllib.request
from types import SimpleNamespace
from typing import Any

import anthropic as anthropic_sdk
import httpx
import httpx2
import openai as openai_sdk
import perplexity as perplexity_sdk
import pytest
from google import genai

from aivis.contracts.config import FrozenConfig
from aivis.providers import drafter
from aivis.providers import perplexity as perplexity_mod
from aivis.providers.anthropic import AnthropicAdapter
from aivis.providers.gemini import GeminiAdapter, resolve_grounding_redirect
from aivis.providers.openai import OpenAIAdapter
from tests import test_anthropic_adapter as ta
from tests import test_gemini_adapter as tg
from tests import test_openai_adapter as to
from tests import test_perplexity_adapter as tp

_RELAY = "https://relay.example"

# ---------------------------------------------------------------- Gemini:并发只建一个客户端


def test_gemini_builds_exactly_one_client_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认并发 4:几个线程同时首次调用。以前各建一个、后者覆盖前者,被覆盖的一被回收就关掉
    仍在使用的连接池 → 首批调用整批中止。现在加锁,只建一个。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    built: list[object] = []

    class _SlowClient:
        def __init__(self, **_kwargs: Any) -> None:
            time.sleep(0.05)  # 放大竞争窗口:不加锁时 8 个线程都会进来各建一个
            built.append(self)

    monkeypatch.setattr(genai, "Client", _SlowClient)
    adapter = GeminiAdapter()
    got: list[object] = []
    start = threading.Barrier(8)

    def grab() -> None:
        start.wait()
        got.append(adapter._get_client())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
    assert len(got) == 8 and all(c is built[0] for c in got)


def test_gemini_client_has_a_timeout_and_uses_the_wizard_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "wizard-key")  # 向导注入的是这一把
    monkeypatch.setenv("GOOGLE_API_KEY", "some-other-account")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", _RELAY)
    api = GeminiAdapter()._get_client()._api_client
    assert api.api_key == "wizard-key"
    assert api._http_options.timeout == 600_000  # 毫秒;SDK 默认 None = 永不超时
    assert str(api._http_options.base_url).startswith("https://generativelanguage.googleapis.com")


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("Server disconnected without sending a response"),
        httpx2.ReadError("connection reset"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_gemini_transport_errors_are_failures_not_crashes(
    exc: Exception, demo_cfg: FrozenConfig
) -> None:
    """瞬时网络错误记为失败、整批继续;以前只接了超时和连不上,连接被重置会崩掉整批。"""
    result = tg._ask(tg.FakeAdapter([exc]), demo_cfg)
    assert result.finished_ok is False and result.error_code == "timeout"
    assert type(exc).__name__ in (result.error_detail or "")


def test_gemini_redirect_resolver_survives_a_bad_status_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    class _Opener:
        def open(self, *_args: Any, **_kwargs: Any) -> Any:
            raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a: _Opener())
    assert resolve_grounding_redirect(tg.REDIRECT) is None  # 以前这个异常会冲出解析器


# ---------------------------------------------------------------- 解析异常:留底记失败,不崩整批


def _weird() -> Any:
    """一个形状完全出乎意料的"返回"(SDK 不校验时可能出现)。"""
    return SimpleNamespace(
        output=None,
        content=None,
        candidates=[SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=5))],
        prompt_feedback=None,
        status="completed",
        stop_reason="end_turn",
        model="x",
        usage=SimpleNamespace(server_tool_use=None),
    )


def test_unexpected_response_shape_is_recorded_not_raised(demo_cfg: FrozenConfig) -> None:
    results = [
        ta._ask(ta.FakeAdapter([_weird()]), demo_cfg),
        to._ask(to.FakeAdapter([_weird()]), demo_cfg),
        tg._ask(tg.FakeAdapter([_weird()]), demo_cfg),
        tp._ask(tp.FakeAgent([_weird()]), demo_cfg),
        tp._ask(tp.FakeSearch([SimpleNamespace(results=5)]), demo_cfg),
    ]
    for r in results:
        assert r.finished_ok is False and r.error_code == "parse_error"
        assert r.error_detail  # 留了原因


def test_parse_failure_keeps_the_raw_payload_so_cost_is_counted(demo_cfg: FrozenConfig) -> None:
    """已付费的返回解析不了时原样留底:花费才算得出来(以前既不留底也不记账)。"""

    class _BreaksInParse(to.FakeAdapter):
        def _parse(self, *args: Any, **kwargs: Any) -> Any:
            raise TypeError("annotation without position")

    adapter = _BreaksInParse([to._response([to._message("Alpha Packs is great.")])])
    result = to._ask(adapter, demo_cfg)
    assert result.error_code == "parse_error"
    assert '"usage"' in result.raw_json
    assert adapter.actual_cost_usd(result) > 0


# ---------------------------------------------------------------- 空回答:五家统一记失败


def test_empty_answers_are_failures_on_every_engine(demo_cfg: FrozenConfig) -> None:
    """没有回答正文不是"所有品牌都没被提到"的观测。以前只有 Grok 记失败,另外四家记成功。"""
    claude_empty = ta.FakeAdapter([ta._message(content=[])])
    claude_thinking_only = ta.FakeAdapter([ta._message(content=[], stop_reason="max_tokens")])
    openai_empty = to.FakeAdapter([to._response([to._search_call()])])
    gemini_empty = tg.FakeAdapter([tg._response(text="")])
    gemini_cut = tg.FakeAdapter([tg._response(text="", finish="MAX_TOKENS")])
    pplx_empty = tp.FakeAgent([tp._response([tp._search_item([(1, "t", "https://x.example/a")])])])
    cases: list[tuple[Any, Any]] = [
        (claude_empty, ta._ask),
        (claude_thinking_only, ta._ask),
        (openai_empty, to._ask),
        (gemini_empty, tg._ask),
        (gemini_cut, tg._ask),
        (pplx_empty, tp._ask),
    ]
    for adapter, ask in cases:
        result = ask(adapter, demo_cfg)
        assert result.finished_ok is False, type(adapter).__mro__[1].__name__
        assert result.error_code == "other" and "没有回答正文" in (result.error_detail or "")
        assert adapter.actual_cost_usd(result) > 0  # 钱已经花了:失败也照记


def test_normal_answers_are_still_successes(demo_cfg: FrozenConfig) -> None:
    text = "Alpha Packs is great."
    assert ta._ask(ta.FakeAdapter([ta._message(content=[ta._text_block(text)])]), demo_cfg)
    ok = [
        ta._ask(ta.FakeAdapter([ta._message(content=[ta._text_block(text)])]), demo_cfg),
        to._ask(to.FakeAdapter([to._response([to._message(text)])]), demo_cfg),
        tg._ask(tg.FakeAdapter([tg._response(text=text)]), demo_cfg),
        tp._ask(tp.FakeAgent([tp._response([tp._message(text, None)])]), demo_cfg),
    ]
    assert all(r.finished_ok and r.answer_text.strip() == text for r in ok)


def test_search_only_surface_is_allowed_to_have_no_answer(demo_cfg: FrozenConfig) -> None:
    """Perplexity Search API 本来就没有正文(surface_kind=search_only):不算空回答失败。"""
    from perplexity.generated.api import SearchCreateResponse

    response = SearchCreateResponse.model_validate(
        {"id": "s1", "results": [{"title": "t", "url": "https://x.example/a", "snippet": "s"}]}
    )
    result = tp._ask(tp.FakeSearch([response]), demo_cfg)
    assert result.finished_ok is True and result.answer_text == ""


# ---------------------------------------------------------------- 端点钉死官方地址


def test_keys_only_go_to_the_official_endpoints_even_with_relay_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境里为别的工具设的中转站地址(*_BASE_URL)不许把密钥和问题带到别处。"""
    for name in ("ANTHROPIC", "OPENAI", "PERPLEXITY"):
        monkeypatch.setenv(f"{name}_API_KEY", "test-key")
        monkeypatch.setenv(f"{name}_BASE_URL", _RELAY)
    clients: dict[str, Any] = {
        "api.anthropic.com": AnthropicAdapter()._get_client(),
        "api.openai.com": OpenAIAdapter()._get_client(),
        "api.perplexity.ai": perplexity_mod._client(),
    }
    for host, client in clients.items():
        assert client.base_url.host == host
    sender = drafter.AnthropicTextSender(api_key="wizard-key")
    with pytest.raises(drafter.DraftError):  # 没有网络:连不上 → DraftError(见下一个测试)
        _fail_fast(sender)
    assert sender._client is not None and sender._client.base_url.host == "api.anthropic.com"


def _fail_fast(sender: drafter.AnthropicTextSender) -> None:
    """让起草器建好客户端后立刻失败(不联网):把 SDK 的发送换成直接抛连接错误。"""
    original = anthropic_sdk.Anthropic

    class _NoNetwork(original):  # type: ignore[misc, valid-type]
        @property
        def messages(self) -> Any:
            def create(**_params: Any) -> Any:
                raise anthropic_sdk.APIConnectionError(
                    request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                )

            return SimpleNamespace(create=create)

    anthropic_sdk.Anthropic = _NoNetwork  # type: ignore[misc]
    try:
        sender(system=None, user="hi", use_web_search=False)
    finally:
        anthropic_sdk.Anthropic = original  # type: ignore[misc]


def test_pinned_endpoints_match_the_sdk_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉的地址必须就是各家 SDK 自己的默认值(防手滑写错 / SDK 改默认值时这里会红)。"""
    for name in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "PERPLEXITY_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GOOGLE_GEMINI_BASE_URL", raising=False)
    for name in ("ANTHROPIC", "OPENAI", "PERPLEXITY", "GEMINI"):
        monkeypatch.setenv(f"{name}_API_KEY", "test-key")
    assert AnthropicAdapter()._get_client().base_url == anthropic_sdk.Anthropic().base_url
    assert OpenAIAdapter()._get_client().base_url == openai_sdk.OpenAI().base_url
    assert perplexity_mod._client().base_url == perplexity_sdk.Perplexity().base_url
    ours = GeminiAdapter()._get_client()._api_client._http_options.base_url
    assert ours == genai.Client(api_key="test-key")._api_client._http_options.base_url


# ---------------------------------------------------------------- 起草器


def test_drafter_turns_api_errors_into_draft_errors() -> None:
    """密钥不对 / 网络不通:转成 DraftError(RuntimeError),向导才能回一句人话而不是断开连接。"""
    sender = drafter.AnthropicTextSender(api_key="wizard-key")
    with pytest.raises(drafter.DraftError, match="APIConnectionError"):
        _fail_fast(sender)
    assert issubclass(drafter.DraftError, RuntimeError)


def test_drafting_refuses_a_category_that_names_the_focal_brand() -> None:
    """品类里带着品牌名,"盲起草"的中立题提示词里就会出现它 —— 在花钱调用之前拦下。"""
    calls: list[str] = []

    def sender(*, system: str | None, user: str, use_web_search: bool) -> str:
        calls.append(user)
        return "{}"

    with pytest.raises(ValueError, match="品牌名"):
        drafter.draft_all(sender, category="Acme Corp trail backpacks", focal_name="Acme Corp")
    assert calls == []  # 一次调用都没发
