"""OpenAI 适配器(v0.2):Responses API + `web_search` 服务端工具。

要点(CLAUDE.md §4/§5/§6):
  - 单轮 clean-room 调用:无 instructions、无历史;`store=False` 不在 OpenAI 侧留存;
  - 引用 = `message` 输出项里 `output_text.annotations[type=url_citation]`(url/title/起止位置),
    `cited_text` 取回答里被标注的那段;`web_search_call` 项计搜索次数,其 status=failed → 失败;
  - `max_tool_calls` = cfg.max_uses(bounded run);`user_location.country` = cfg.region;
  - 目录里的 GPT-5.6 / GPT-6 系列均为推理模型,不接受 temperature(catalog 已标注);
  - 成本按 provider × model 取价:token(含缓存分级)+ web search $10/千次;估算参数**未经真机校准**,
    先沿用 Anthropic 的每搜索输入 token 口径作保守上界,真机跑后校准;
  - 密钥只经环境变量 OPENAI_API_KEY 读取;鉴权/权求格式错误逐调用复现 → 立即中止整批。

唯一网络触点是 _send();单元测试以替身覆盖它,离线、零成本。
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import openai
from openai.types.responses import Response

from aivis.contracts.adapter import ModelInfo
from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse
from aivis.providers import catalog

_TOOL_TYPE = "web_search"
_SURFACE_ID = "openai_responses_web_search"
# web search $10 / 1k calls(所有模型;2026-09 官方 pricing)
_SEARCH_COST_PER_CALL = 10.0 / 1000.0
_MAX_OUTPUT_TOKENS = 8192
# 估算口径(未校准,保守上界):每次搜索约带回 1.2 万输入 token;推理 token 计入输出,给 4000
_EST_BASE_INPUT_TOKENS = 2000
_EST_INPUT_TOKENS_PER_SEARCH = 12000
_EST_OUTPUT_TOKENS = 4000


@dataclass(frozen=True)
class _ModelSpec:
    in_price: float  # $/M 输入
    out_price: float  # $/M 输出(含推理 token)
    cached_price: float  # $/M 缓存命中输入
    sampling: bool = False  # 推理模型:温度参数不可用


# provider × model 单价(developers.openai.com/api/docs/pricing,2026-09-22;5.6 系列为促销价)
_SPECS: dict[str, _ModelSpec] = {
    "gpt-6-astra": _ModelSpec(10.0, 50.0, 1.0),
    "gpt-6-sol": _ModelSpec(2.0, 10.0, 0.2),
    "gpt-6-luna": _ModelSpec(0.1, 0.5, 0.01),
    "gpt-5.6-sol": _ModelSpec(4.0, 20.0, 0.4),
    "gpt-5.6-terra": _ModelSpec(2.0, 12.0, 0.2),
    "gpt-5.6-luna": _ModelSpec(0.2, 1.2, 0.02),
}


class FatalProviderError(RuntimeError):
    """会在每次调用复现的不可恢复错误(鉴权/权限/请求格式)→ 中止整批,不烧预算。"""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpenAIAdapter:
    """真 OpenAI Responses API web_search 探测(实现 contracts.adapter.ProviderAdapter)。"""

    def __init__(self, model: str | None = None, *, tier: str = "standard") -> None:
        if model is None:
            model = catalog.default_model("openai", tier)
        if model not in _SPECS:
            raise ValueError(f"未知 OpenAI 模型 {model!r},可选: {sorted(_SPECS)}")
        self._model = model
        self._spec = _SPECS[model]
        self._client: openai.OpenAI | None = None  # 惰性创建:构造不要求密钥

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def surface_id(self) -> str:
        return _SURFACE_ID

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def tool_path(self) -> str:
        return "web_search"

    @property
    def tool_version(self) -> str | None:
        return _TOOL_TYPE

    @property
    def info(self) -> ModelInfo:
        return catalog.find_model("openai", self._model)

    # ------------------------------------------------------------------ 成本

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        """保守上界:按 max_uses 满额搜索计工具费 + 每搜索 1.2 万输入 token(未校准口径)。"""
        searches = cfg.max_uses if cfg.max_uses is not None else 1
        input_tokens = _EST_BASE_INPUT_TOKENS + searches * _EST_INPUT_TOKENS_PER_SEARCH
        token_cost = (
            input_tokens / 1e6 * self._spec.in_price
            + _EST_OUTPUT_TOKENS / 1e6 * self._spec.out_price
        )
        return n_calls * (token_cost + searches * _SEARCH_COST_PER_CALL)

    def actual_cost_usd(self, response: RawResponse) -> float:
        """从 raw_json 的 usage 真实计价:未命中输入 + 缓存命中输入 + 输出(含推理)+ 工具调用费。"""
        try:
            data = json.loads(response.raw_json)
            usage = data.get("response", {}).get("usage") or {}
            tool_calls = int(data.get("web_search_calls") or 0)
        except (ValueError, TypeError, AttributeError):
            return 0.0
        input_total = int(usage.get("input_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        token_cost = (
            max(0, input_total - cached) / 1e6 * self._spec.in_price
            + cached / 1e6 * self._spec.cached_price
            + output / 1e6 * self._spec.out_price
        )
        return token_cost + tool_calls * _SEARCH_COST_PER_CALL

    # ------------------------------------------------------------------ 探测

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        try:
            response = self._send(self._build_params(question, cfg))
        except (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.BadRequestError,
            openai.NotFoundError,
        ) as e:
            raise FatalProviderError(
                f"不可恢复的 OpenAI API 错误({e.__class__.__name__}),已中止整批以免烧预算。"
                f"详情: {e}"
            ) from e
        except openai.RateLimitError as e:
            return self._failure(question, run_id, run_index, "rate_limit", e)
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            return self._failure(question, run_id, run_index, "timeout", e)
        except openai.APIStatusError as e:
            return self._failure(question, run_id, run_index, "other", e)
        return self._parse(question, run_id, run_index, response)

    def _send(self, params: dict[str, Any]) -> Response:
        """唯一网络触点(测试以替身覆盖)。"""
        if self._client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                raise FatalProviderError(
                    "未检测到 OPENAI_API_KEY 环境变量。"
                    "API 密钥只经环境变量读取(CLAUDE.md §5),不支持写入代码或配置文件。"
                )
            self._client = openai.OpenAI()
        return cast(Response, self._client.responses.create(**params))

    def _build_params(self, question: Question, cfg: FrozenConfig) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": _TOOL_TYPE}
        if cfg.region:
            tool["user_location"] = {"type": "approximate", "country": cfg.region}
        params: dict[str, Any] = {
            "model": self._model,
            "input": question.text,  # clean-room:无 instructions、无历史
            "tools": [tool],
            "store": False,  # 不在 OpenAI 侧留存对话
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
        }
        if cfg.max_uses is not None:
            params["max_tool_calls"] = cfg.max_uses  # bounded run
        if self._spec.sampling:
            params["temperature"] = cfg.temperature
        return params

    # ------------------------------------------------------------------ 解析

    def _parse(
        self, question: Question, run_id: str, run_index: int, response: Response
    ) -> RawResponse:
        answer_parts: list[str] = []
        citations: list[Citation] = []
        search_count = 0  # 真正的搜索动作数
        web_search_calls = 0  # 所有 web_search_call 项(open_page/find_in_page 也计工具费,保守)
        tool_failed = False
        position = 0
        for item in response.output:
            if item.type == "web_search_call":
                web_search_calls += 1
                action_type = getattr(item.action, "type", None)
                if action_type == "search":
                    search_count += 1
                if item.status == "failed":
                    tool_failed = True
            elif item.type == "message":
                for part in item.content:
                    if part.type != "output_text":
                        continue
                    answer_parts.append(part.text)
                    for ann in part.annotations or []:
                        if ann.type != "url_citation":
                            continue
                        start, end = ann.start_index, ann.end_index
                        cited = (
                            part.text[start:end] if 0 <= start <= end <= len(part.text) else None
                        )
                        citations.append(
                            Citation(
                                url=ann.url,
                                title=ann.title or None,
                                cited_text=cited or None,
                                position=position,
                            )
                        )
                        position += 1

        finished_ok = True
        error_code: ErrorCode | None = None
        error_detail: str | None = None
        if response.status != "completed":
            finished_ok, error_code = False, "other"
            reason = getattr(response.incomplete_details, "reason", None)
            err = response.error.message if response.error is not None else None
            error_detail = f"response.status={response.status} reason={reason} error={err}"
        elif tool_failed:
            finished_ok, error_code = False, "tool_error"
            error_detail = "web_search_call status=failed"

        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text=" ".join(answer_parts),
            citations=tuple(citations),
            search_count=search_count,
            model=response.model or self._model,
            finished_ok=finished_ok,
            error_code=error_code,
            error_detail=error_detail,
            raw_json=self._dump_raw(response, web_search_calls),
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
            raw_json=json.dumps({"response": None, "web_search_calls": 0}),
            timestamp=_utc_now(),
        )

    @staticmethod
    def _dump_raw(response: Response, web_search_calls: int) -> str:
        """provider 原样返回,进原始层(只追加,唯一真相源)。"""
        return json.dumps(
            {"response": response.model_dump(mode="json"), "web_search_calls": web_search_calls},
            ensure_ascii=False,
            sort_keys=True,
        )
