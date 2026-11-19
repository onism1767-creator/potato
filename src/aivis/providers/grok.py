"""Grok(xAI)适配器(2026-11 接入):xAI Responses API + `web_search` 服务端工具。

xAI 的 Responses API 与 OpenAI 的同形,官方文档示例即用 `openai` SDK 指向 `https://api.x.ai/v1`,
故本适配器复用已在白名单里的 `openai` SDK(不新增依赖,CLAUDE.md §3),只换主机与密钥。

要点(CLAUDE.md §4/§5/§6;对照 docs.x.ai 与真机返回核实,2026-11-19):
  - 单轮 clean-room 调用:无 instructions、无历史;`store=False`(xAI 默认留存 30 天,显式关掉);
  - 只开 `web_search`,不开 `x_search`:本表面测的是"联网搜索回答",X 站内搜索是另一个表面;
  - 引用 = `message` 输出项里 `output_text.annotations[type=url_citation]` 中**有位置**的那些。
    真机观察:行内引用的标注区间只盖住标记 `[[n]](url)`,`title` 只是序号 "n" 不是页面标题
    (→ 记 None);故 `cited_text` 取这串标记**之前**、它所支撑的那句话(不含任何标记/URL,
    以免别的来源 URL 里的品牌词被误当成本句论述);
  - 🟥 保守计数:回答没写行内标记时(真机 grok-4.7 两次皆如此),xAI 仍会塞一批**零宽**标注
    (start_index == end_index == 0,title == url)——那是代理"浏览过的全部来源"(一次 48~53 条),
    不是回答引用的来源,**不算引用**(与 Perplexity 适配器"未被标记引用的结果不算引用"同口径);
    同理 `web_search_call.action.sources` 也不算。原样都留在 raw_json 可审计;
  - `max_turns` = cfg.max_uses(bounded run;xAI 的请求侧上限是"代理轮数",经 extra_body 传)。
    它卡的是轮数**不是工具调用数**:一轮可并行多次搜索,且打开网页(open_page)同样按次计费
    ——真机 max_turns=3 时计费调用 3~8 次。`max_tool_calls` 真机验证被 xAI 忽略(不传,
    不假装有这个上限);`include: ["inline_citations"]` 被 400 拒绝(Responses API 上默认即开)。
    硬封顶靠 probe 按真实花费执行;
  - xAI 的 web_search **没有区域参数**(目录标 supports_region=False,
    清单如实记 region_applied=False);
  - temperature 可传且被回显(目录标 supports_temperature=True);推理强度用各模型默认,不干预;
  - 成本优先取 API 自报的 `usage.cost_in_usd_ticks`(1e10 ticks = $1;真机核对与"token 单价 +
    $5/千次搜索"分毫不差);缺失时按 provider × model 单价回退
    (含缓存分级与 ≥20 万 token 长上下文翻倍);
  - 密钥只经环境变量 XAI_API_KEY 读取;xAI 对坏密钥返 400、未知模型返 404 → 均立即中止整批;
  - 失败如实记(CLAUDE.md §9.4):`completed` 却没有回答文本记 other、拒答记 refusal(不做
    fallback)、返回形状解析不了记 parse_error——后两种钱已花,原始返回留底、花费照记。

唯一网络触点是 _send();单元测试以替身覆盖它,离线、零成本。
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import openai
from openai.types.responses import Response

from aivis.contracts.adapter import FatalProviderError, ModelInfo
from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse
from aivis.providers import catalog

_BASE_URL = "https://api.x.ai/v1"
_ENV_KEY = "XAI_API_KEY"
_TOOL_TYPE = "web_search"
_SURFACE_ID = "grok_responses_web_search"
# web_search $5 / 1k calls(docs.x.ai/developers/pricing,2026-11-19;按调用计,不按来源计)
_SEARCH_COST_PER_CALL = 5.0 / 1000.0
_TICKS_PER_USD = 1e10  # usage.cost_in_usd_ticks 的单位
_LONG_CONTEXT_TOKENS = 200_000  # 提示达到此阈值:整次请求所有 token 按 2 倍单价
_MAX_OUTPUT_TOKENS = 8192
_TIMEOUT_SECONDS = 900.0  # xAI 文档建议推理模型客户端超时 ≥ 900 秒
# 估算口径(保守上界,按"轮"计——max_uses 传给 max_turns,卡的是轮数)。
# 真机校准(2026-11-19,6 个模型共 8 次调用,max_turns=3):计费工具调用 3~8 次、
# 输入 2.1 万~11.1 万 token(其中最多 6 万命中缓存,估算一律按未命中价)、输出最多 4,212。
# 取每轮 3 次工具调用 / 4 万输入、输出 5000:对这 8 次真实调用都是上界(实际为估算的 22%~62%)。
# 样本小,不保证永远盖住——运行中的硬封顶仍由 probe 按真实花费执行。
_EST_BASE_INPUT_TOKENS = 2000
_EST_INPUT_TOKENS_PER_TURN = 40000
_EST_TOOL_CALLS_PER_TURN = 3
_EST_OUTPUT_TOKENS = 5000


@dataclass(frozen=True)
class _ModelSpec:
    in_price: float  # $/M 输入
    out_price: float  # $/M 输出(含推理 token)
    cached_price: float  # $/M 缓存命中输入


# provider × model 单价(GET api.x.ai/v1/language-models 自报 + docs.x.ai/developers/pricing,
# 2026-11-19;<20 万 token 提示的档)
_SPECS: dict[str, _ModelSpec] = {
    "grok-4.20-0309-non-reasoning": _ModelSpec(1.25, 2.5, 0.2),
    "grok-4.20-0309-reasoning": _ModelSpec(1.25, 2.5, 0.2),
    "grok-4.3": _ModelSpec(1.25, 2.5, 0.2),
    "grok-4.5": _ModelSpec(2.0, 6.0, 0.3),
    "grok-4.6": _ModelSpec(2.0, 6.0, 0.5),
    "grok-4.7": _ModelSpec(2.0, 6.0, 0.5),
}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# 断句点:西文句末标点后须跟空白(小数点、缩写里的点不断);中文句末标点后通常不跟空格,直接断
# ——否则上一句(说别的品牌的)会漏进本句的引用片段,被 extract 多记一条 earned
_SENTENCE_BREAK = re.compile(r"""[.!?]["'*_)\]]*\s+|[。！？]["'”’」』）)\]*_]*\s*|\n+""")
_TRAILING_STOP = re.compile(r"""[.!?。！？]["'”’」』）)\]*_]*\Z""")  # 句子自己的句末标点
_CITED_MAX_CHARS = 400


def _cited_sentence(text: str, spans: list[tuple[int, int]], start: int, end: int) -> str | None:
    """标注区间 → 引用片段。区间是 `[[n]](url)` 标记时,取这串标记之前它所支撑的那句话。

    spans = 本段文字里全部标注区间:连续标记(一句话挂多个来源,紧挨或只隔空格)共用同一句;
    窗口内更早的标记换成换行(既当断句点,又把别的来源 URL 挡在片段之外)。
    标记写在句末标点之后时仍归紧前面那一句;另起一行的标记、或前面只剩标点 → None(保守,不猜)。
    """
    if not (0 <= start <= end <= len(text)):
        return None
    span = text[start:end]
    if not span.startswith("[["):
        return span or None  # 非标记形态(如关闭了行内引用):区间本身就是被引文字
    ends = {e: s for s, e in spans if s < e}
    run_start = start
    while True:  # 退到这串连续标记的起点(ends[j] < j,必然终止)
        j = run_start
        while j > 0 and text[j - 1] in " \t":
            j -= 1
        if j not in ends:
            break
        run_start = ends[j]
    lo = max(0, run_start - _CITED_MAX_CHARS)
    pieces: list[str] = []
    cursor = lo
    for s, e in sorted(spans):
        if e <= cursor or s >= run_start:
            continue
        pieces.append(text[cursor : max(s, cursor)])
        pieces.append("\n")
        cursor = e
    pieces.append(text[cursor:run_start])
    head = "".join(pieces).rstrip(" \t")
    stop = _TRAILING_STOP.search(head)
    core = head[: stop.start()] if stop else head  # 句子自己的句末标点不当断句点
    breaks = list(_SENTENCE_BREAK.finditer(core))
    sentence = head[breaks[-1].end() :] if breaks else head
    sentence = sentence.strip().lstrip(",;:、，；：").strip()
    return sentence if any(ch.isalnum() for ch in sentence) else None


class GrokAdapter:
    """真 xAI Responses API web_search 探测(实现 contracts.adapter.ProviderAdapter)。"""

    def __init__(self, model: str | None = None, *, tier: str = "standard") -> None:
        if model is None:
            model = catalog.default_model("grok", tier)
        if model not in _SPECS:
            raise ValueError(f"未知 Grok 模型 {model!r},可选: {sorted(_SPECS)}")
        self._model = model
        self._spec = _SPECS[model]
        self._client: openai.OpenAI | None = None  # 惰性创建:构造不要求密钥

    @property
    def provider_name(self) -> str:
        return "grok"

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
        return catalog.find_model("grok", self._model)

    # ------------------------------------------------------------------ 成本

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        """保守上界:按 max_uses 满额轮数,每轮 3 次计费工具调用 + 4 万输入 token(真机校准)。"""
        turns = cfg.max_uses if cfg.max_uses is not None else 1
        input_tokens = _EST_BASE_INPUT_TOKENS + turns * _EST_INPUT_TOKENS_PER_TURN
        token_cost = (
            input_tokens / 1e6 * self._spec.in_price
            + _EST_OUTPUT_TOKENS / 1e6 * self._spec.out_price
        )
        tool_cost = turns * _EST_TOOL_CALLS_PER_TURN * _SEARCH_COST_PER_CALL
        return n_calls * (token_cost + tool_cost)

    def actual_cost_usd(self, response: RawResponse) -> float:
        """优先 API 自报 usage.cost_in_usd_ticks;缺失回退 token 单价(缓存分级)+ 每次搜索工具费。"""
        try:
            data = json.loads(response.raw_json)
            usage = (data.get("response") or {}).get("usage") or {}
            ticks = usage.get("cost_in_usd_ticks")
            # 自报 0 不采信:token 明明用了却报 0 时回退按单价算(宁可多记,硬封顶才靠得住)
            if isinstance(ticks, int | float) and not isinstance(ticks, bool) and ticks > 0:
                return float(ticks) / _TICKS_PER_USD
            tool_calls = int(data.get("web_search_calls") or 0)
            input_total = int(usage.get("input_tokens") or 0)
            cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
            output = int(usage.get("output_tokens") or 0)
        except (ValueError, TypeError, AttributeError):
            return 0.0
        token_cost = (
            max(0, input_total - cached) / 1e6 * self._spec.in_price
            + cached / 1e6 * self._spec.cached_price
            + output / 1e6 * self._spec.out_price
        )
        if input_total >= _LONG_CONTEXT_TOKENS:
            token_cost *= 2  # 长上下文档:整次请求所有 token 翻倍
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
            openai.BadRequestError,  # xAI 对坏密钥返 400(真机核实)
            openai.NotFoundError,  # 未知模型 / 该 team 无权访问
        ) as e:
            raise FatalProviderError(
                f"不可恢复的 xAI API 错误({e.__class__.__name__}),已中止整批以免烧预算。详情: {e}"
            ) from e
        except openai.RateLimitError as e:
            return self._failure(question, run_id, run_index, "rate_limit", e)
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            return self._failure(question, run_id, run_index, "timeout", e)
        except openai.APIStatusError as e:
            return self._failure(question, run_id, run_index, "other", e)
        try:
            return self._parse(question, run_id, run_index, response)
        except (TypeError, ValueError, AttributeError) as e:
            # SDK 不校验 xAI 的返回;形状出乎意料时钱已经花了:原样留底、记为失败、花费照记,
            # 不让异常冲出去(否则这次调用既没留底也没进账,整批还会崩在这里)
            return self._failure(question, run_id, run_index, "parse_error", e, response=response)

    def _send(self, params: dict[str, Any]) -> Response:
        """唯一网络触点(测试以替身覆盖)。"""
        if self._client is None:
            key = os.environ.get(_ENV_KEY)
            if not key:
                raise FatalProviderError(
                    f"未检测到 {_ENV_KEY} 环境变量。"
                    "API 密钥只经环境变量读取(CLAUDE.md §5),不支持写入代码或配置文件。"
                )
            client = openai.OpenAI(api_key=key, base_url=_BASE_URL, timeout=_TIMEOUT_SECONDS)
            # SDK 构造时会从环境读一批给 OpenAI 用的东西并随每个请求发出:OPENAI_ORG_ID /
            # OPENAI_PROJECT_ID(账号标识),以及 OPENAI_CUSTOM_HEADERS 里的任意请求头——后者连
            # Authorization 都能顶替掉(那样发给 x.ai 的就成了 OpenAI 的凭据)。一律清掉,不带给 x.ai。
            # _custom_headers 是 SDK 私有属性(没有公开开关),由离线测试钉住:SDK 改名测试会红
            client.organization = None
            client.project = None
            client._custom_headers = {}
            self._client = client
        return cast(Response, self._client.responses.create(**params))

    def _build_params(self, question: Question, cfg: FrozenConfig) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._model,
            "input": question.text,  # clean-room:无 instructions、无历史
            "tools": [{"type": _TOOL_TYPE}],  # 无区域参数可传(supports_region=False)
            "store": False,  # xAI 默认留存 30 天,显式关掉
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
            "temperature": cfg.temperature,
        }
        if cfg.max_uses is not None:
            # bounded run:xAI 专有参数(openai SDK 签名里没有),经 extra_body 原样进请求体
            params["extra_body"] = {"max_turns": cfg.max_uses}
        return params

    # ------------------------------------------------------------------ 解析

    def _parse(
        self, question: Question, run_id: str, run_index: int, response: Response
    ) -> RawResponse:
        answer_parts: list[str] = []
        citations: list[Citation] = []
        n_search_actions = 0  # action.type == "search" 的项
        n_tool_items = 0  # 所有 web_search_call 项(浏览页面等也计工具费,保守)
        tool_failed = False
        refusals: list[str] = []
        position = 0
        for item in response.output:
            if item.type == "web_search_call":
                n_tool_items += 1
                if getattr(item.action, "type", None) == "search":
                    n_search_actions += 1
                if item.status == "failed":
                    tool_failed = True
            elif item.type == "message":
                for part in item.content:
                    if part.type == "refusal":
                        refusals.append(part.refusal)
                        continue
                    if part.type != "output_text":
                        continue
                    answer_parts.append(part.text)
                    # 零宽标注 = 代理浏览过的来源清单,不是回答引用的来源(保守:不算引用)
                    anns = [
                        a
                        for a in part.annotations or []
                        if a.type == "url_citation" and a.end_index > a.start_index
                    ]
                    spans = [(a.start_index, a.end_index) for a in anns]
                    for ann in anns:
                        title = ann.title or None
                        if title is not None and title.isdigit():
                            title = None  # xAI 的 title 只是引用序号,不是页面标题
                        citations.append(
                            Citation(
                                url=ann.url,
                                title=title,
                                cited_text=_cited_sentence(
                                    part.text, spans, ann.start_index, ann.end_index
                                ),
                                position=position,
                            )
                        )
                        position += 1

        answer_text = " ".join(answer_parts)
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
        elif refusals:
            # 拒答记为失败,不做 fallback(与 CLAUDE.md §4.4 对 Anthropic 的要求同口径)
            finished_ok, error_code = False, "refusal"
            error_detail = ("refusal: " + " ".join(refusals))[:500]
        elif not answer_text.strip():
            # completed 却没有任何回答文本(只搜不答等):是失败,不是"空回答"(CLAUDE.md §9.4)
            finished_ok, error_code = False, "other"
            error_detail = "response.status=completed 但没有回答文本"

        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text=answer_text,
            citations=tuple(citations),
            search_count=n_search_actions,
            model=response.model or self._model,
            finished_ok=finished_ok,
            error_code=error_code,
            error_detail=error_detail,
            raw_json=self._dump_raw(response, self._billed_search_calls(response, n_tool_items)),
            timestamp=_utc_now(),
        )

    @staticmethod
    def _dump_raw(response: Response, billed_calls: int) -> str:
        return json.dumps(
            {
                "response": response.model_dump(mode="json", warnings=False),
                "web_search_calls": billed_calls,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _billed_search_calls(response: Response, n_tool_items: int) -> int:
        """计费用的搜索调用数:优先 xAI 自报的 server_side_tool_usage_details.web_search_calls
        (SDK 类型里没有的扩展字段),缺失回退输出里的 web_search_call 项数。"""
        usage = response.usage.model_dump(mode="json") if response.usage is not None else {}
        reported = (usage.get("server_side_tool_usage_details") or {}).get("web_search_calls")
        if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
            return reported
        return n_tool_items

    def _failure(
        self,
        question: Question,
        run_id: str,
        run_index: int,
        error_code: ErrorCode,
        exc: Exception,
        *,
        response: Response | None = None,
    ) -> RawResponse:
        """显式记为失败并保留上下文,流水线继续(部分失败仍出报告)。

        response=None:请求没拿到返回(限速/超时/5xx),花费记 0;
        给了 response:返回拿到了但解析不了——原样留底,花费仍按它的 usage 记账。
        """
        raw_json = json.dumps({"response": None, "web_search_calls": 0})
        if response is not None:
            n_tool_items = sum(
                1
                for item in response.output or []
                if getattr(item, "type", None) == "web_search_call"
            )
            raw_json = self._dump_raw(response, self._billed_search_calls(response, n_tool_items))
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
            raw_json=raw_json,
            timestamp=_utc_now(),
        )
