"""ProviderAdapter Protocol(CLAUDE.md §4:多引擎在架构层解决,不在调用处 if-else)。

所有引擎差异封装在各自适配器内;pipeline 禁止出现 `if provider == ...` 分支。
v0.1 只有 mock + anthropic;v0.2(2026-09)起接 openai / gemini / perplexity 四家真引擎,
2026-11 加 grok;每家一个适配器,模型目录(ModelInfo)由各适配器模块自报、providers.registry 汇总。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import RawResponse

ModelStatus = Literal["current", "legacy", "preview"]


class FatalProviderError(RuntimeError):
    """适配器抛出的不可恢复错误(缺密钥/鉴权/权限/请求格式):会逐调用复现 → 调用方中止整批,不烧预算。

    瞬时错误(限速/超时/5xx)**不**用此类:适配器把它们记成 finished_ok=False 的 RawResponse 继续跑。
    """


# answer=生成回答+引用(可测提及与引用);search_only=纯搜索结果列表,无回答文本(提及类指标不适用)
SurfaceKind = Literal["answer", "search_only"]


class ProviderInfo(BaseModel):
    """一家引擎的展示/接入元数据(GUI 密钥框、隐私文案、CLI 帮助共用的单一真相源)。"""

    model_config = ConfigDict(frozen=True)

    name: str  # anthropic / openai / gemini / perplexity / grok / mock
    label: str  # 展示名,如 "Claude (Anthropic)"
    env_key: str  # 密钥环境变量名(🟥 密钥只经环境变量读取,CLAUDE.md §5)
    endpoint: str  # 密钥唯一发往的主机,如 "api.anthropic.com"(隐私文案如实列出)


class ModelInfo(BaseModel):
    """一个可选模型(= 一个测量表面)的元数据;成本估算不在此,由适配器按 usage 计价。"""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str  # 精确 API 模型 id;Perplexity 预设档为 "preset:<name>"
    label: str
    tier: str  # economy / standard / high / flagship(分层标签,不参与计分)
    blurb: str
    status: ModelStatus = "current"
    # False = 该模型拒绝采样参数,clean-room 温度钉不住(清单如实记录 temperature_applied)
    supports_temperature: bool = True
    # False = 该 API 无区域参数,区域钉不住(清单如实记录 region_applied)
    supports_region: bool = True
    # False = 底层模型会自动更新(Perplexity 预设档);每次实际模型见 RawResponse.model
    pinned: bool = True
    surface_kind: SurfaceKind = "answer"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class SurfaceSpec(BaseModel):
    """一次运行要测的表面 = provider + model(model 为 None 时取该引擎 standard 档默认)。"""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str | None = None

    @classmethod
    def parse(cls, text: str) -> "SurfaceSpec":
        """解析 "provider" 或 "provider:model"(model 内可含冒号,如 preset:low)。"""
        provider, sep, model = text.strip().partition(":")
        if not provider:
            raise ValueError(f"非法表面标识 {text!r},应为 provider 或 provider:model")
        return cls(provider=provider, model=model or None if sep else None)


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

    @property
    def info(self) -> ModelInfo:
        """本适配器实例所测模型的目录元数据(probe 据此写清单里的诚实字段)。"""
        ...

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
