"""Gemini 适配器(v0.2):generateContent + Grounding with Google Search。

要点(CLAUDE.md §4/§5/§6):
  - 单轮 clean-room 调用:无 system_instruction、无历史;temperature 照传(Gemini 接受);
    **无区域参数**(catalog 标 supports_region=False,清单如实记 region_applied=False);
  - 引用 = grounding_supports(引用片段 segment.text)→ grounding_chunks[web.uri / title];
    只有被 support 引用到的 chunk 才算引用(未被引用的 chunk 只是搜索结果,保守计数不算);
  - 🟥 chunk 的 uri 是 Google 跳转链接(vertexaisearch.cloud.google.com/grounding-api-redirect/…),
    没有官方方法拿真实网址;owned/earned 判定全靠域名,故适配器在 probe 阶段**只读跳转头**
    (HEAD、不跟随、不抓正文)解析成真实网址;原始跳转链接原样留在 raw_json.redirects;
    解析失败保留跳转链接(verify 阶段会核为不可达/无法归属,不静默);
  - 搜索次数 = len(web_search_queries);Gemini **没有 max_uses 类参数**,估算按每调用 5 次搜索;
  - finish_reason SAFETY/PROHIBITED_CONTENT/BLOCKLIST/SPII/RECITATION 或 prompt 被拦 → refusal;
  - 成本按 provider × model 取价:token(thoughts 计入输出、tool_use_prompt 计入输入)+
    grounding $14/千次搜索(每月前 5000 次免费,保守按全额计);估算口径**未经真机校准**;
  - 密钥只经环境变量 GEMINI_API_KEY(或 GOOGLE_API_KEY)读取;鉴权/请求格式错误 → 立即中止整批。

唯一网络触点是 _send()(+ 可注入的跳转解析器);单元测试以替身覆盖,离线、零成本。
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import httpx2
from google import genai
from google.genai import errors, types

from aivis.contracts.adapter import FatalProviderError, ModelInfo
from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse
from aivis.providers import catalog

_TOOL_TYPE = "google_search"
_SURFACE_ID = "gemini_generate_content_google_search"
# Grounding with Google Search,Gemini 3.x:每月前 5000 次搜索免费,之后 $14/千次(按搜索计)。
# 保守:全部按付费计(免费额度是否用完本工具不知道)。
_GROUNDING_COST_PER_QUERY = 14.0 / 1000.0
_MAX_OUTPUT_TOKENS = 8192
# 估算口径(未校准,保守上界):无 max_uses 可钉,按每次调用 5 次搜索;每次搜索约 1.2 万输入 token
_EST_QUERIES_PER_CALL = 5
_EST_BASE_INPUT_TOKENS = 2000
_EST_INPUT_TOKENS_PER_SEARCH = 12000
_EST_OUTPUT_TOKENS = 4000  # thinking token 计入输出
_REDIRECT_HOST_SUFFIX = "vertexaisearch.cloud.google.com"
_REDIRECT_MAX_HOPS = 3
_REDIRECT_TIMEOUT_S = 8.0
_USER_AGENT = "aivis/0.2 (+https://github.com/onism1767-creator/potato)"
_REFUSAL_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


@dataclass(frozen=True)
class _ModelSpec:
    in_price: float  # $/M 输入(≤200k 上下文档位)
    out_price: float  # $/M 输出(含 thinking)


# provider × model 单价(ai.google.dev/gemini-api/docs/pricing,2026-09-22)
_SPECS: dict[str, _ModelSpec] = {
    "gemini-3.8-flash": _ModelSpec(0.75, 3.75),
    "gemini-3.7-flash": _ModelSpec(0.75, 3.75),
    "gemini-3.6-flash": _ModelSpec(0.75, 3.75),
    "gemini-3.5-flash": _ModelSpec(1.5, 9.0),
    "gemini-3.1-pro-preview": _ModelSpec(2.0, 12.0),
}

RedirectResolver = Callable[[str], str | None]


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随跳转:3xx 以 HTTPError 抛出,由调用方读 Location 头。"""

    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> Any:
        return None


def is_grounding_redirect(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == _REDIRECT_HOST_SUFFIX or host.endswith("." + _REDIRECT_HOST_SUFFIX)


def resolve_grounding_redirect(url: str) -> str | None:
    """只读跳转头把 Google grounding 跳转链接解析成真实网址(HEAD、不跟随、不抓正文)。

    最多 _REDIRECT_MAX_HOPS 跳且只在 Google 跳转域内跟进;最终真实网址**不发起请求**
    (那是 verify 的职责,且要过 SSRF 守卫)。解析不到返回 None。
    """
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(_REDIRECT_MAX_HOPS):
        if not is_grounding_redirect(current):
            return current
        request = urllib.request.Request(
            current, method="HEAD", headers={"User-Agent": _USER_AGENT}
        )
        try:
            with opener.open(request, timeout=_REDIRECT_TIMEOUT_S):
                return None  # 2xx 且无跳转:拿不到真实网址
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400 and e.headers.get("Location"):
                current = str(e.headers.get("Location"))
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
    return current if not is_grounding_redirect(current) else None


class GeminiAdapter:
    """真 Gemini API Google Search grounding 探测(实现 contracts.adapter.ProviderAdapter)。"""

    def __init__(
        self,
        model: str | None = None,
        *,
        tier: str = "standard",
        redirect_resolver: RedirectResolver | None = resolve_grounding_redirect,
    ) -> None:
        if model is None:
            model = catalog.default_model("gemini", tier)
        if model not in _SPECS:
            raise ValueError(f"未知 Gemini 模型 {model!r},可选: {sorted(_SPECS)}")
        self._model = model
        self._spec = _SPECS[model]
        self._resolver = redirect_resolver
        self._redirect_cache: dict[str, str | None] = {}
        self._client: genai.Client | None = None  # 惰性创建:构造不要求密钥

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def surface_id(self) -> str:
        return _SURFACE_ID

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def tool_path(self) -> str:
        return "google_search"

    @property
    def tool_version(self) -> str | None:
        return _TOOL_TYPE

    @property
    def info(self) -> ModelInfo:
        return catalog.find_model("gemini", self._model)

    # ------------------------------------------------------------------ 成本

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        """保守上界:每次调用按 5 次搜索计 grounding 费 + 每搜索 1.2 万输入 token(未校准口径)。"""
        input_tokens = _EST_BASE_INPUT_TOKENS + _EST_QUERIES_PER_CALL * _EST_INPUT_TOKENS_PER_SEARCH
        token_cost = (
            input_tokens / 1e6 * self._spec.in_price
            + _EST_OUTPUT_TOKENS / 1e6 * self._spec.out_price
        )
        return n_calls * (token_cost + _EST_QUERIES_PER_CALL * _GROUNDING_COST_PER_QUERY)

    def actual_cost_usd(self, response: RawResponse) -> float:
        """从 raw_json 的 usage_metadata 真实计价 + 按搜索次数计 grounding 费。"""
        try:
            data = json.loads(response.raw_json)
            usage = (data.get("response") or {}).get("usage_metadata") or {}
        except (ValueError, TypeError, AttributeError):
            return 0.0
        prompt = int(usage.get("prompt_token_count") or 0)
        tool_prompt = int(usage.get("tool_use_prompt_token_count") or 0)
        candidates = int(usage.get("candidates_token_count") or 0)
        thoughts = int(usage.get("thoughts_token_count") or 0)
        token_cost = (prompt + tool_prompt) / 1e6 * self._spec.in_price + (
            candidates + thoughts
        ) / 1e6 * self._spec.out_price
        return token_cost + response.search_count * _GROUNDING_COST_PER_QUERY

    # ------------------------------------------------------------------ 探测

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        try:
            response = self._send(question.text, self._build_config(cfg))
        except errors.ClientError as e:
            if e.code in (400, 401, 403, 404):
                raise FatalProviderError(
                    f"不可恢复的 Gemini API 错误(HTTP {e.code}),已中止整批以免烧预算。"
                    f"详情: {e.message}"
                ) from e
            if e.code == 429:
                return self._failure(question, run_id, run_index, "rate_limit", e)
            return self._failure(question, run_id, run_index, "other", e)
        except errors.APIError as e:  # ServerError 等
            return self._failure(question, run_id, run_index, "other", e)
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx2.TimeoutException,
            httpx2.ConnectError,
            TimeoutError,
            ConnectionError,
        ) as e:
            return self._failure(question, run_id, run_index, "timeout", e)
        return self._parse(question, run_id, run_index, response)

    def _send(
        self, contents: str, config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        """唯一网络触点(测试以替身覆盖)。"""
        if self._client is None:
            if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
                raise FatalProviderError(
                    "未检测到 GEMINI_API_KEY(或 GOOGLE_API_KEY)环境变量。"
                    "API 密钥只经环境变量读取(CLAUDE.md §5),不支持写入代码或配置文件。"
                )
            self._client = genai.Client()
        return self._client.models.generate_content(
            model=self._model, contents=contents, config=config
        )

    @staticmethod
    def _build_config(cfg: FrozenConfig) -> types.GenerateContentConfig:
        # clean-room:无 system_instruction;温度钉住;grounding 工具开;无区域参数可传
        return types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=cfg.temperature,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )

    # ------------------------------------------------------------------ 解析

    def _resolve(self, uri: str) -> str:
        """跳转链接 → 真实网址(带实例级缓存);解析不到保留原链接(verify 会如实核为无法归属)。"""
        if self._resolver is None or not is_grounding_redirect(uri):
            return uri
        if uri not in self._redirect_cache:
            self._redirect_cache[uri] = self._resolver(uri)
        resolved = self._redirect_cache[uri]
        return resolved if resolved else uri

    def _parse(
        self,
        question: Question,
        run_id: str,
        run_index: int,
        response: types.GenerateContentResponse,
    ) -> RawResponse:
        answer_parts: list[str] = []
        citations: list[Citation] = []
        search_count = 0
        redirects: dict[str, str | None] = {}
        finish = None
        candidate = response.candidates[0] if response.candidates else None
        if candidate is not None:
            finish = candidate.finish_reason.name if candidate.finish_reason else None
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if part.text and not part.thought:
                        answer_parts.append(part.text)
            gm = candidate.grounding_metadata
            if gm is not None:
                search_count = len(gm.web_search_queries or [])
                chunks = list(gm.grounding_chunks or [])
                position = 0
                for support in gm.grounding_supports or []:
                    segment_text = support.segment.text if support.segment else None
                    for idx in support.grounding_chunk_indices or []:
                        if not 0 <= idx < len(chunks):
                            continue
                        web = chunks[idx].web
                        if web is None or not web.uri:
                            continue
                        url = self._resolve(web.uri)
                        if is_grounding_redirect(web.uri):
                            redirects[web.uri] = url if url != web.uri else None
                        citations.append(
                            Citation(
                                url=url,
                                title=web.title or None,
                                cited_text=segment_text or None,
                                position=position,
                            )
                        )
                        position += 1

        finished_ok = True
        error_code: ErrorCode | None = None
        error_detail: str | None = None
        block = getattr(response.prompt_feedback, "block_reason", None)
        if block:
            finished_ok, error_code = False, "refusal"
            error_detail = f"prompt blocked: {getattr(block, 'name', block)}"
        elif candidate is None:
            finished_ok, error_code = False, "other"
            error_detail = "no candidates in response"
        elif finish in _REFUSAL_FINISH:
            finished_ok, error_code = False, "refusal"
            error_detail = f"finish_reason={finish}"
        elif finish not in (None, "STOP", "MAX_TOKENS"):
            finished_ok, error_code = False, "other"
            error_detail = f"finish_reason={finish}"

        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text="".join(answer_parts),
            citations=tuple(citations),
            search_count=search_count,
            model=response.model_version or self._model,
            finished_ok=finished_ok,
            error_code=error_code,
            error_detail=error_detail,
            raw_json=self._dump_raw(response, redirects),
            timestamp=_utc_now(),
        )

    def _failure(
        self, question: Question, run_id: str, run_index: int, error_code: ErrorCode, exc: Exception
    ) -> RawResponse:
        """瞬时错误:显式记为失败并保留上下文,流水线继续(部分失败仍出报告)。"""
        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text="",
            citations=(),
            search_count=0,
            model=self._model,
            finished_ok=False,
            error_code=error_code,
            error_detail=f"{exc.__class__.__name__}: {exc}"[:500],
            raw_json=json.dumps({"response": None, "redirects": {}}),
            timestamp=_utc_now(),
        )

    @staticmethod
    def _dump_raw(response: types.GenerateContentResponse, redirects: dict[str, str | None]) -> str:
        """provider 原样返回(含原始跳转链接)+ 跳转解析结果,进原始层(只追加,唯一真相源)。"""
        payload = response.model_dump(mode="json", exclude_none=True)
        payload.pop("sdk_http_response", None)  # 不存响应头(可能含请求元数据),正文已完整
        return json.dumps(
            {"response": payload, "redirects": redirects}, ensure_ascii=False, sort_keys=True
        )
