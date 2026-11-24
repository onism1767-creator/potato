"""Anthropic 适配器(第二步,BUILD_SPEC §8 step 10)。

要点(CLAUDE.md §4/§5/§6):
  - 调 web_search 工具,引用始终开启;clean-room 单轮调用,pinned 区域/温度;
  - 工具报错以 HTTP 200 + error block 返回 → finished_ok=False(不是空回答);
  - 限速/超时等瞬时错误记为失败继续跑;鉴权/请求格式错误会逐调用复现 → 立即中止整批;
  - 成本按 provider × model × tool_path 取价,搜索次数按 max_uses 取上界(保守估计);
  - API 密钥只经环境变量读取(SDK 默认),本模块不触碰、不记录密钥本身;
  - SDK 自带 429/5xx 指数退避重试(默认 2 次),不另造重试轮子。

唯一网络触点是 _send();单元测试以替身覆盖它,离线、零成本。
"""

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import anthropic
from anthropic.types import Message

from aivis.contracts.adapter import FatalProviderError, ModelInfo
from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse
from aivis.providers import catalog

# 工具版本(2026-06 真实 API 对照实验定论,见 ARCHITECTURE.md §9):
# 必须用经典版 web_search_20250305——它把来源作为内联 web_search_result_location
# 引用挂在答案文本上(带 cited_text/title/url),正是本项目"被引用"测量的依据。
# 新版 web_search_20260209 带"动态过滤"(模型写代码筛结果),实测内联引用恒为 0
# (12 vs 0 的对照),且过滤代码会偶发崩溃——会让核心引用指标失效,故不用。
_TOOL_TYPE = "web_search_20250305"
# 端点钉死官方地址:环境里的 ANTHROPIC_BASE_URL(常是为别的工具设的中转站)不许把密钥和问题
# 带到别处 —— 清单、目录和界面都写着"只发往 api.anthropic.com"
_BASE_URL = "https://api.anthropic.com"

# 三档便利映射(`--tier`);显式 `model=` 优先。standard 沿用 2026-06 经真实 API 验证的 Sonnet 4.6。
_TIER_MODELS = {
    "economy": "claude-haiku-4-5",
    "standard": "claude-sonnet-4-6",
    "high": "claude-opus-4-8",
    "flagship": "claude-fable-5-1",
}


@dataclass(frozen=True)
class _ModelSpec:
    """单模型计价/请求参数(provider × model × tool_path;2026-09 官方 pricing)。"""

    in_price: float  # $/M 输入 token
    out_price: float  # $/M 输出 token(含 thinking)
    cache_read_mult: float = 0.1  # 缓存读取价 = 输入价 × 此倍率
    sampling: bool = True  # False:模型拒绝 temperature(传了 400)
    max_tokens: int = 4096  # 思考常开的 5.x 系列给更大上限(思考 token 计入输出)
    tool_type: str = _TOOL_TYPE  # 5.x 是否接受经典版工具待真机验证;不接受会显式报错不静默


_SPECS: dict[str, _ModelSpec] = {
    "claude-haiku-4-5": _ModelSpec(1.0, 5.0),
    "claude-sonnet-4-6": _ModelSpec(3.0, 15.0),
    "claude-sonnet-5": _ModelSpec(2.0, 10.0, sampling=False, max_tokens=8192),
    "claude-opus-4-8": _ModelSpec(5.0, 25.0, sampling=False),
    "claude-opus-5": _ModelSpec(5.0, 25.0, sampling=False, max_tokens=8192),
    "claude-fable-5-1": _ModelSpec(
        10.0, 50.0, cache_read_mult=0.025, sampling=False, max_tokens=8192
    ),
}


@dataclass(frozen=True)
class TierInfo:
    """一个 run 档位的展示元数据(GUI 三档卡沿用;完整目录见 providers.catalog)。"""

    id: str  # economy / standard / high
    model: str  # 对应模型 id
    label: str  # 展示名
    blurb: str  # 一句话定位/意义


def _tier_info(tier: str) -> TierInfo:
    info = catalog.find_model("anthropic", _TIER_MODELS[tier])
    return TierInfo(id=tier, model=info.model, label=info.label, blurb=info.blurb)


# 三档说明(GUI 档位卡 + 估价;顺序 = 便宜 → 标准 → 高质量)
TIER_INFO: tuple[TierInfo, ...] = tuple(_tier_info(t) for t in ("economy", "standard", "high"))
_SEARCH_COST_PER_USE = 10.0 / 1000.0  # web search $10/千次
# Anthropic 提示缓存价位:写 1.25× 基础输入价;读按模型倍率(真实计价用)
_CACHE_WRITE_MULT = 1.25

# 成本估算(2026-06 真实全量运行校准):web_search 把搜索结果喂回 + pause_turn 续传
# 重发上下文,每次 web 搜索约带来 1.2 万输入 token(旧版写死 4000 严重低估——实测每次
# 调用 ~2 万输入,$4.64 估算 vs $7.27 真实)。
_EST_BASE_INPUT_TOKENS = 2000
_EST_INPUT_TOKENS_PER_SEARCH = 12000
_EST_OUTPUT_TOKENS = 2000

_MAX_CONTINUATIONS = 3  # pause_turn 续传上限:bounded run,不许无上限深搜(CLAUDE.md §5)


def estimate_draft_cost_usd(tier: str = "standard") -> float:
    """AI 起草成本上界(用同一单价表,不臆造):中立 + 防御 2 次无搜索调用 + 竞品 1 次
    web_search 调用(max_uses=3)。drafter 默认用 standard(Sonnet)。"""
    spec = _SPECS[_TIER_MODELS[tier]]
    in_price, out_price = spec.in_price, spec.out_price
    no_search = _EST_BASE_INPUT_TOKENS / 1e6 * in_price + _EST_OUTPUT_TOKENS / 1e6 * out_price
    search = (
        (_EST_BASE_INPUT_TOKENS + 3 * _EST_INPUT_TOKENS_PER_SEARCH) / 1e6 * in_price
        + _EST_OUTPUT_TOKENS / 1e6 * out_price
        + 3 * _SEARCH_COST_PER_USE
    )
    return 2 * no_search + search


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AnthropicAdapter:
    """真 Claude API web_search 探测(实现 contracts.adapter.ProviderAdapter)。"""

    def __init__(self, tier: str = "standard", *, model: str | None = None) -> None:
        if model is None:
            if tier not in _TIER_MODELS:
                raise ValueError(f"未知模型档位 {tier!r},可选: {sorted(_TIER_MODELS)}")
            model = _TIER_MODELS[tier]
        if model not in _SPECS:
            raise ValueError(f"未知 Claude 模型 {model!r},可选: {sorted(_SPECS)}")
        self._model = model
        self._spec = _SPECS[model]
        self._client: anthropic.Anthropic | None = None  # 惰性创建:构造不要求密钥
        self._client_lock = threading.Lock()  # 并发探测时多个线程同时首次调用,只建一个

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def surface_id(self) -> str:
        return "anthropic_api_web_search"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def tool_path(self) -> str:
        return "web_search"

    @property
    def tool_version(self) -> str | None:
        return self._spec.tool_type

    @property
    def info(self) -> ModelInfo:
        return catalog.find_model("anthropic", self._model)

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        """保守上界:按 max_uses 满额搜索 + 每搜索约 1.2 万输入 token(实测校准)计费。

        真实通常更低(模型常不用满 max_uses);硬封顶由 probe 按真实累计花费执行。
        """
        in_price, out_price = self._spec.in_price, self._spec.out_price
        searches_per_call = cfg.max_uses if cfg.max_uses is not None else 1
        input_tokens = _EST_BASE_INPUT_TOKENS + searches_per_call * _EST_INPUT_TOKENS_PER_SEARCH
        token_cost = input_tokens / 1e6 * in_price + _EST_OUTPUT_TOKENS / 1e6 * out_price
        return n_calls * (token_cost + searches_per_call * _SEARCH_COST_PER_USE)

    def actual_cost_usd(self, response: RawResponse) -> float:
        """从 raw_json 真实 usage 计价(含缓存分级);供 probe 运行中累计封顶。"""
        in_price, out_price = self._spec.in_price, self._spec.out_price
        try:
            messages = json.loads(response.raw_json).get("messages", [])
        except (ValueError, TypeError):
            return 0.0
        reg = cwrite = cread = out = 0
        for msg in messages:
            usage = msg.get("usage") or {}
            reg += usage.get("input_tokens") or 0
            cwrite += usage.get("cache_creation_input_tokens") or 0
            cread += usage.get("cache_read_input_tokens") or 0
            out += usage.get("output_tokens") or 0
        input_billed = reg + cwrite * _CACHE_WRITE_MULT + cread * self._spec.cache_read_mult
        token_cost = input_billed / 1e6 * in_price + out / 1e6 * out_price
        return token_cost + response.search_count * _SEARCH_COST_PER_USE

    # ------------------------------------------------------------------ 探测

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        request_messages: list[dict[str, Any]] = [
            {"role": "user", "content": question.text}  # clean-room:无系统提示、无历史
        ]
        responses: list[Message] = []
        try:
            response = self._send(self._build_params(request_messages, cfg))
            responses.append(response)
            # 服务端工具循环达上限时返回 pause_turn:原样回传续跑,次数有界
            continuations = 0
            while response.stop_reason == "pause_turn" and continuations < _MAX_CONTINUATIONS:
                request_messages = [
                    request_messages[0],
                    {"role": "assistant", "content": response.content},
                ]
                response = self._send(self._build_params(request_messages, cfg))
                responses.append(response)
                continuations += 1
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.BadRequestError,
            anthropic.NotFoundError,
        ) as e:
            raise FatalProviderError(
                f"不可恢复的 API 错误({e.__class__.__name__}),已中止整批以免烧预算。详情: {e}"
            ) from e
        except anthropic.RateLimitError as e:
            return self._failure(question, run_id, run_index, responses, "rate_limit", e)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            return self._failure(question, run_id, run_index, responses, "timeout", e)
        except anthropic.APIStatusError as e:
            return self._failure(question, run_id, run_index, responses, "other", e)

        try:
            return self._parse(question, run_id, run_index, responses)
        except (TypeError, ValueError, AttributeError, KeyError, IndexError) as e:
            # 返回形状出乎意料时钱已经花了:原样留底、记为失败、花费照记,
            # 不让异常冲出去(否则这次调用既没留底也没进账,整批还会崩在这里)
            try:
                return self._failure(question, run_id, run_index, responses, "parse_error", e)
            except (TypeError, ValueError, AttributeError, KeyError, IndexError):
                # 连留底都做不了(形状坏到导不出来):至少如实记一条失败,不崩整批
                return self._failure(question, run_id, run_index, [], "parse_error", e)

    def _send(self, params: dict[str, Any]) -> Message:
        """唯一网络触点(测试以替身覆盖)。"""
        return cast(Message, self._get_client().messages.create(**params))

    def _get_client(self) -> anthropic.Anthropic:
        with self._client_lock:
            if self._client is None:
                key = os.environ.get("ANTHROPIC_API_KEY")
                if not key:
                    raise FatalProviderError(
                        "未检测到 ANTHROPIC_API_KEY 环境变量。"
                        "API 密钥只经环境变量读取(CLAUDE.md §5),不支持写入代码或配置文件。"
                    )
                self._client = anthropic.Anthropic(api_key=key, base_url=_BASE_URL)
            return self._client

    def _build_params(self, messages: list[dict[str, Any]], cfg: FrozenConfig) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": self._spec.tool_type, "name": "web_search"}
        if cfg.max_uses is not None:
            tool["max_uses"] = cfg.max_uses
        if cfg.region:
            tool["user_location"] = {"type": "approximate", "country": cfg.region}
        params: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self._spec.max_tokens,
            "messages": messages,
            "tools": [tool],
        }
        if self._spec.sampling:
            params["temperature"] = cfg.temperature  # pinned(clean-room 要求)
        return params

    # ------------------------------------------------------------------ 解析

    def _parse(
        self, question: Question, run_id: str, run_index: int, responses: list[Message]
    ) -> RawResponse:
        answer_parts: list[str] = []
        citations: list[Citation] = []
        search_count = 0
        tool_error: str | None = None
        position = 0
        for message in responses:
            server_tool_use = message.usage.server_tool_use
            if server_tool_use is not None and server_tool_use.web_search_requests:
                search_count += server_tool_use.web_search_requests
            for block in message.content:
                if block.type == "text":
                    answer_parts.append(block.text)
                    for c in block.citations or []:
                        if c.type == "web_search_result_location":
                            citations.append(
                                Citation(
                                    url=c.url,
                                    title=c.title,
                                    cited_text=c.cited_text,
                                    position=position,
                                )
                            )
                            position += 1
                elif block.type == "web_search_tool_result" and not isinstance(block.content, list):
                    tool_error = block.content.error_code

        finished_ok = True
        error_code: ErrorCode | None = None
        error_detail: str | None = None
        if tool_error is not None:
            # HTTP 200 + error block:记为失败,正文保留作审计但不进指标
            finished_ok, error_code = False, "tool_error"
            error_detail = f"web_search error block: {tool_error}"
        elif responses[-1].stop_reason == "pause_turn":
            finished_ok, error_code = False, "other"
            error_detail = f"pause_turn 续传 {_MAX_CONTINUATIONS} 次后仍未完成,按失败记录"
        elif responses[-1].stop_reason == "refusal":
            # 安全层拒答(HTTP 200 + stop_reason=refusal;Fable/Opus 5 等):记为失败、不当空回答;
            # 也**不做**服务端 fallback——换模型作答会让"此表面"的测量失真。
            finished_ok, error_code = False, "refusal"
            details = getattr(responses[-1], "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            error_detail = f"refusal(category={category})"
        elif not " ".join(answer_parts).strip():
            # 没有任何回答正文(思考把输出额度用完 / 搜完就结束这一轮):是失败,
            # 不是一次"所有品牌都没被提到"的观测(五家同口径;CLAUDE.md §9.4)
            finished_ok, error_code = False, "other"
            error_detail = f"没有回答正文(stop_reason={responses[-1].stop_reason})"

        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text=" ".join(answer_parts),
            citations=tuple(citations),
            search_count=search_count,
            model=responses[-1].model if responses else self.model_name,
            finished_ok=finished_ok,
            error_code=error_code,
            error_detail=error_detail,
            raw_json=self._dump_raw(responses),
            timestamp=_utc_now(),
        )

    def _failure(
        self,
        question: Question,
        run_id: str,
        run_index: int,
        responses: list[Message],
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
            search_count=sum(
                m.usage.server_tool_use.web_search_requests
                for m in responses
                if m.usage.server_tool_use is not None
                and m.usage.server_tool_use.web_search_requests
            ),
            model=self.model_name,
            finished_ok=False,
            error_code=error_code,
            error_detail=f"{exc.__class__.__name__}: {exc}"[:500],
            raw_json=self._dump_raw(responses),
            timestamp=_utc_now(),
        )

    @staticmethod
    def _dump_raw(responses: list[Message]) -> str:
        """provider 原样返回,进原始层(只追加,唯一真相源)。"""
        return json.dumps(
            {"messages": [m.model_dump(mode="json") for m in responses]},
            ensure_ascii=False,
            sort_keys=True,
        )
