"""ProviderAdapter Protocol(CLAUDE.md §4:多引擎在架构层解决,不在调用处 if-else)。

所有引擎差异封装在各自适配器内;pipeline 禁止出现 `if provider == ...` 分支。
第一版只实现 mock;anthropic 第二步接;OpenAI / Gemini 仅由本接口预留,不写实现。
"""

from typing import Protocol

from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import RawResponse


class ProviderAdapter(Protocol):
    """统一的引擎适配器接口。

    比 BUILD_SPEC §5 多 run_id / run_index 两个关键字参数(RawResponse 必含,
    出生时即完整)、元数据属性(probe 写 RunManifest 的来源)和成本估算
    (成本闸门 model-aware,§6.3 禁止静态单价——价格逻辑封装在适配器内)。
    """

    @property
    def provider_name(self) -> str: ...

    @property
    def surface_id(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def tool_path(self) -> str: ...

    @property
    def tool_version(self) -> str | None: ...

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        """单次 clean-room 探测;工具报错必须记为 finished_ok=False,不当空回答。"""
        ...

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        """开跑前的保守上界估算(mock 恒 0);硬封顶由 probe 按真实累计花费执行。"""
        ...

    def actual_cost_usd(self, response: RawResponse) -> float:
        """一次真实调用的实际花费(从 response 的 usage 计价;mock 恒 0)。

        供 probe 运行中累加、逼近 budget_cap 即阻断(CLAUDE.md §6:超预算则阻断/降级)。
        """
        ...
