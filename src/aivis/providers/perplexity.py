"""Perplexity 适配器(v0.2):Agent API(回答 + 引用)与 Search API(纯搜索结果)。

背景:Sonar 聊天接口 2026-09-27 停服,官方迁移到 Agent API(`/v1/agent`)。目录映射:
  - `perplexity/sonar`  → Agent API 显式 model(可钉死);
  - `preset:low`        → 官方迁移表 sonar-pro 的替代(底层 OpenAI 模型、自动更新、**不可钉**);
  - `preset:medium`     → sonar-reasoning-pro 的替代(同上);
  - `search`            → Search API:只返回结果列表(title/url/snippet),**无回答文本**,
                          提及类指标不适用(surface_kind=search_only,报告如实标注)。

要点(CLAUDE.md §4/§5/§6):
  - Agent API 单轮 clean-room:无 instructions;`store=False`;`max_steps`=max_uses(bounded);
    `web_search` 工具带 user_location.country=region;temperature 照传(文档接受);
  - 引用 = message 输出项 output_text.annotations[url_citation](起止位置 → cited_text);
    若无标注但正文有 [n] / [web:n] 标记,则按标记映射 search_results 的结果 id(保守:未被标记引用的
    结果只是搜索结果,不算引用);搜索次数取 usage.tool_calls_details.web_search.invocation;
  - 成本优先取 API 自报的 usage.cost.total_cost(最准);缺失时按 token 单价 + $0.0025/次搜索回退;
    预设档底层模型/单价不可控,估算按 OpenAI 等价模型取价并标注未校准;
  - Search API 每请求 $5/千次固定价,无 token 费;
  - 密钥只经环境变量 PERPLEXITY_API_KEY 读取(两个表面共用一把);鉴权/请求格式错误 → 立即中止整批。

唯一网络触点是各类的 _send();单元测试以替身覆盖,离线、零成本。
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import perplexity
from perplexity.generated.api import ResponseCreateResponse, SearchCreateResponse

from aivis.contracts.adapter import FatalProviderError, ModelInfo
from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse
from aivis.providers import catalog

_AGENT_SURFACE_ID = "perplexity_agent_web_search"
_SEARCH_SURFACE_ID = "perplexity_search_api"
_AGENT_TOOL = "web_search"
_SEARCH_MODEL_ID = "search"
_WEB_SEARCH_COST_PER_INVOCATION = 0.0025  # Agent API web_search 工具 $0.0025/次
_SEARCH_API_COST_PER_REQUEST = 5.0 / 1000.0  # Search API $5/千次请求,无 token 费
_SEARCH_API_MAX_RESULTS = 10
_MAX_OUTPUT_TOKENS = 8192
# 估算口径(未校准,保守上界):每次搜索约 1.2 万输入 token;推理 token 计入输出
_EST_BASE_INPUT_TOKENS = 2000
_EST_INPUT_TOKENS_PER_SEARCH = 12000
_EST_OUTPUT_TOKENS = 4000
_MARKER_RE = re.compile(r"\[(?:web:)?(\d+)\]")


@dataclass(frozen=True)
class _ModelSpec:
    in_price: float  # $/M 输入(回退/估算用;真实计价优先取 API 自报 cost)
    out_price: float
    preset: str | None = None  # 预设档名;None = 显式 model


# docs.perplexity.ai/docs/agent-api/models(2026-09-22):perplexity/sonar $0.25/$2.50;
# 预设 low/medium 底层为 openai/gpt-5.6-luna,按其 OpenAI 公开价 $0.2/$1.2 估算(标注未校准)
_SPECS: dict[str, _ModelSpec] = {
    "perplexity/sonar": _ModelSpec(0.25, 2.5),
    "preset:low": _ModelSpec(0.2, 1.2, preset="low"),
    "preset:medium": _ModelSpec(0.2, 1.2, preset="medium"),
}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _client() -> perplexity.Perplexity:
    if not os.environ.get("PERPLEXITY_API_KEY"):
        raise FatalProviderError(
            "未检测到 PERPLEXITY_API_KEY 环境变量。"
            "API 密钥只经环境变量读取(CLAUDE.md §5),不支持写入代码或配置文件。"
        )
    return perplexity.Perplexity()


def _classify(exc: Exception) -> ErrorCode | None:
    """SDK 异常 → 错误码;None = 不可恢复(调用方转 FatalProviderError)。"""
    if isinstance(
        exc,
        perplexity.AuthenticationError
        | perplexity.PermissionDeniedError
        | perplexity.BadRequestError
        | perplexity.NotFoundError,
    ):
        return None
    if isinstance(exc, perplexity.RateLimitError):
        return "rate_limit"
    if isinstance(exc, perplexity.APITimeoutError | perplexity.APIConnectionError):
        return "timeout"
    return "other"


def _failure(
    question: Question,
    run_id: str,
    run_index: int,
    model: str,
    error_code: ErrorCode,
    exc: Exception,
) -> RawResponse:
    """瞬时错误:显式记为失败并保留上下文,流水线继续(部分失败仍出报告)。"""
    return RawResponse(
        run_id=run_id,
        question_id=question.id,
        run_index=run_index,
        answer_text="",
        citations=(),
        search_count=0,
        model=model,
        finished_ok=False,
        error_code=error_code,
        error_detail=f"{exc.__class__.__name__}: {exc}"[:500],
        raw_json=json.dumps({"response": None}),
        timestamp=_utc_now(),
    )


# ====================================================================== Agent API


class PerplexityAdapter:
    """Perplexity Agent API web_search 探测(实现 contracts.adapter.ProviderAdapter)。"""

    def __init__(self, model: str | None = None, *, tier: str = "standard") -> None:
        if model is None:
            model = catalog.default_model("perplexity", tier)
        if model not in _SPECS:
            raise ValueError(f"未知 Perplexity Agent 模型 {model!r},可选: {sorted(_SPECS)}")
        self._model = model
        self._spec = _SPECS[model]
        self._client: perplexity.Perplexity | None = None

    @property
    def provider_name(self) -> str:
        return "perplexity"

    @property
    def surface_id(self) -> str:
        return _AGENT_SURFACE_ID

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def tool_path(self) -> str:
        return "web_search"

    @property
    def tool_version(self) -> str | None:
        return _AGENT_TOOL

    @property
    def info(self) -> ModelInfo:
        return catalog.find_model("perplexity", self._model)

    # ------------------------------------------------------------------ 成本

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        searches = cfg.max_uses if cfg.max_uses is not None else 1
        input_tokens = _EST_BASE_INPUT_TOKENS + searches * _EST_INPUT_TOKENS_PER_SEARCH
        token_cost = (
            input_tokens / 1e6 * self._spec.in_price
            + _EST_OUTPUT_TOKENS / 1e6 * self._spec.out_price
        )
        return n_calls * (token_cost + searches * _WEB_SEARCH_COST_PER_INVOCATION)

    def actual_cost_usd(self, response: RawResponse) -> float:
        """优先 API 自报 usage.cost.total_cost;缺失回退 token 单价 + 每次搜索工具费。"""
        try:
            usage = (json.loads(response.raw_json).get("response") or {}).get("usage") or {}
        except (ValueError, TypeError, AttributeError):
            return 0.0
        cost = usage.get("cost") or {}
        total = cost.get("total_cost")
        if isinstance(total, int | float) and total >= 0:
            return float(total)
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        return (
            inp / 1e6 * self._spec.in_price
            + out / 1e6 * self._spec.out_price
            + response.search_count * _WEB_SEARCH_COST_PER_INVOCATION
        )

    # ------------------------------------------------------------------ 探测

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        try:
            response = self._send(self._build_params(question, cfg))
        except perplexity.APIError as e:
            code = _classify(e)
            if code is None:
                raise FatalProviderError(
                    f"不可恢复的 Perplexity API 错误({e.__class__.__name__}),已中止整批以免烧预算。"
                    f"详情: {e}"
                ) from e
            return _failure(question, run_id, run_index, self._model, code, e)
        return self._parse(question, run_id, run_index, response)

    def _send(self, params: dict[str, Any]) -> ResponseCreateResponse:
        """唯一网络触点(测试以替身覆盖)。"""
        if self._client is None:
            self._client = _client()
        return cast(ResponseCreateResponse, self._client.responses.create(**params))

    def _build_params(self, question: Question, cfg: FrozenConfig) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": _AGENT_TOOL}
        if cfg.region:
            tool["user_location"] = {"country": cfg.region}
        params: dict[str, Any] = {
            "input": question.text,  # clean-room:无 instructions、无历史
            "tools": [tool],
            "store": False,
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
            "temperature": cfg.temperature,
        }
        if self._spec.preset is not None:
            params["preset"] = self._spec.preset
        else:
            params["model"] = self._model
        if cfg.max_uses is not None:
            params["max_steps"] = cfg.max_uses  # bounded run
        return params

    # ------------------------------------------------------------------ 解析

    def _parse(
        self, question: Question, run_id: str, run_index: int, response: ResponseCreateResponse
    ) -> RawResponse:
        answer_parts: list[str] = []
        annotated: list[Citation] = []
        results_by_id: dict[int, Any] = {}
        n_search_items = 0
        message_failed = False
        position = 0
        for item in response.output:
            if item.type == "search_results":
                n_search_items += 1
                for r in item.results:
                    results_by_id.setdefault(int(r.id), r)
            elif item.type == "message":
                if item.status == "failed":
                    message_failed = True
                for part in item.content:
                    if part.type != "output_text":
                        continue
                    answer_parts.append(part.text)
                    for ann in part.annotations or []:
                        if ann.type != "url_citation" or not ann.url:
                            continue
                        cited = None
                        if ann.start_index is not None and ann.end_index is not None:
                            s, e = ann.start_index, ann.end_index
                            if 0 <= s <= e <= len(part.text):
                                cited = part.text[s:e] or None
                        annotated.append(
                            Citation(
                                url=ann.url,
                                title=ann.title or None,
                                cited_text=cited,
                                position=position,
                            )
                        )
                        position += 1
        answer_text = " ".join(answer_parts)
        citations = annotated
        if not citations and results_by_id:
            # 无标注:按正文 [n] / [web:n] 标记映射结果 id(只算被标记引用的结果,保守)
            for m in _MARKER_RE.finditer(answer_text):
                hit = results_by_id.get(int(m.group(1)))
                if hit is None:
                    continue
                citations.append(
                    Citation(
                        url=hit.url, title=hit.title or None, cited_text=None, position=position
                    )
                )
                position += 1

        usage = response.usage
        invocations = None
        if usage is not None and usage.tool_calls_details:
            ws = usage.tool_calls_details.get("web_search")
            invocations = ws.invocation if ws is not None else None
        search_count = invocations if invocations is not None else n_search_items

        finished_ok = True
        error_code: ErrorCode | None = None
        error_detail: str | None = None
        if response.status != "completed":
            finished_ok, error_code = False, "other"
            err = response.error.message if response.error is not None else None
            error_detail = f"response.status={response.status} error={err}"
        elif message_failed:
            finished_ok, error_code = False, "other"
            error_detail = "message item status=failed"

        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text=answer_text,
            citations=tuple(citations),
            search_count=search_count,
            model=response.model or self._model,  # 预设档:这里记录实际底层模型(不可钉的诚实记录)
            finished_ok=finished_ok,
            error_code=error_code,
            error_detail=error_detail,
            raw_json=json.dumps(
                {"response": response.model_dump(mode="json")}, ensure_ascii=False, sort_keys=True
            ),
            timestamp=_utc_now(),
        )


# ====================================================================== Search API


class PerplexitySearchAdapter:
    """Perplexity Search API:纯搜索结果表面(无回答文本;实现 contracts.adapter.ProviderAdapter)。

    口径(报告如实标注):answer_text 恒空 → 提及类指标不适用;每条结果 = 一条引用
    (cited_text = 结果摘要,title = 结果标题);search_count 恒 1(一次请求 = 一次搜索)。
    """

    def __init__(self) -> None:
        self._client: perplexity.Perplexity | None = None

    @property
    def provider_name(self) -> str:
        return "perplexity"

    @property
    def surface_id(self) -> str:
        return _SEARCH_SURFACE_ID

    @property
    def model_name(self) -> str:
        return _SEARCH_MODEL_ID

    @property
    def tool_path(self) -> str:
        return "search_api"

    @property
    def tool_version(self) -> str | None:
        return "search"

    @property
    def info(self) -> ModelInfo:
        return catalog.find_model("perplexity", _SEARCH_MODEL_ID)

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return n_calls * _SEARCH_API_COST_PER_REQUEST

    def actual_cost_usd(self, response: RawResponse) -> float:
        # 固定价:每次成功请求 $0.005;失败(无响应)不计
        try:
            has_response = json.loads(response.raw_json).get("response") is not None
        except (ValueError, TypeError, AttributeError):
            return 0.0
        return _SEARCH_API_COST_PER_REQUEST if has_response else 0.0

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        params: dict[str, Any] = {"query": question.text, "max_results": _SEARCH_API_MAX_RESULTS}
        if cfg.region:
            params["country"] = cfg.region
        try:
            response = self._send(params)
        except perplexity.APIError as e:
            code = _classify(e)
            if code is None:
                raise FatalProviderError(
                    f"不可恢复的 Perplexity API 错误({e.__class__.__name__}),已中止整批以免烧预算。"
                    f"详情: {e}"
                ) from e
            return _failure(question, run_id, run_index, _SEARCH_MODEL_ID, code, e)
        citations = tuple(
            Citation(url=r.url, title=r.title or None, cited_text=r.snippet or None, position=i)
            for i, r in enumerate(response.results)
        )
        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text="",  # 纯搜索:无生成回答(surface_kind=search_only)
            citations=citations,
            search_count=1,
            model=_SEARCH_MODEL_ID,
            finished_ok=True,
            raw_json=json.dumps(
                {"response": response.model_dump(mode="json")}, ensure_ascii=False, sort_keys=True
            ),
            timestamp=_utc_now(),
        )

    def _send(self, params: dict[str, Any]) -> SearchCreateResponse:
        """唯一网络触点(测试以替身覆盖)。"""
        if self._client is None:
            self._client = _client()
        return self._client.search.create(**params)
