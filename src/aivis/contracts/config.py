"""冻结配置契约(CLAUDE.md §2):来自 questions.yml + brands.yml 的不可变对象。

FrozenConfig 必须显式传入,禁止全局可变状态或环境隐式读取。
任何配置改动 = 版本号 bump(新基线);fingerprint() 机械暴露"改了没 bump"。
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Intent = Literal["discovery", "comparison", "local", "brand_defense"]
Track = Literal["benchmark"]  # 'operational' v1.1 才加入


class Question(BaseModel):
    """一条冻结的探测问题(四意图桶配方,计划书第 7 章)。"""

    model_config = ConfigDict(frozen=True)

    id: str
    intent: Intent
    text: str
    track: Track = "benchmark"


class Brand(BaseModel):
    """一个被测品牌(focal / peer / competitor 共用同一形状)。

    保守计数(CLAUDE.md §7):name + aliases 是清晰名(计分);
    ambiguous_aliases 是缩写/歧义名(命中只记录,不计分)。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    aliases: tuple[str, ...] = ()
    ambiguous_aliases: tuple[str, ...] = ()
    domains: tuple[str, ...] = Field(min_length=1)
    is_focal: bool = False


class VerifyThresholds(BaseModel):
    """自动验证红线阈值(计划书 10.1;初始猜测值,拿真实数据后校准)。"""

    model_config = ConfigDict(frozen=True)

    citation_validity_yellow: float = 0.85
    citation_validity_red: float = 0.70
    ambiguous_yellow: float = 0.25
    ambiguous_red: float = 0.40
    failure_yellow: float = 0.05
    failure_red: float = 0.15


class FrozenConfig(BaseModel):
    """一次基线的全部冻结输入。不可变;改动即新基线。"""

    model_config = ConfigDict(frozen=True)

    version: str  # 整体配置版本(展示用)
    prompt_version: str  # 题库版本(进 RunManifest;题库改版单独 bump)
    competitor_set_version: str | None  # 竞品集版本(同上)
    focal_domain: str | None = None  # demo 中立模式 = None
    peers: tuple[Brand, ...] = Field(min_length=1)
    competitors_on: bool = False
    questions: tuple[Question, ...] = Field(min_length=1)
    tier: str  # 模型档位(如 'standard')
    locale: str
    region: str
    temperature: float  # clean-room 探测要求 pinned temperature
    n_runs: int = Field(ge=1)  # 固定模式=每题轮次;序贯模式=每题轮次上限(max_runs)
    sequential: bool = False  # True=序贯采样 run_until_stable(稳即停);False=固定 n_runs
    min_runs: int = Field(default=2, ge=1)  # 序贯模式下每题至少问几次(仅 sequential 时生效)
    max_uses: int | None = None  # 单次调用 web_search 上限(成本闸门依据)
    strata: tuple[str, ...] = ("region", "locale", "tier")
    verify_thresholds: VerifyThresholds = VerifyThresholds()
    budget_cap_usd: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_sequential_bounds(self) -> "FrozenConfig":
        """序贯模式下 min_runs 不能超过 n_runs(上限);固定模式不约束(min_runs 被忽略)。"""
        if self.sequential and self.min_runs > self.n_runs:
            raise ValueError(f"序贯采样 min_runs={self.min_runs} 不能大于上限 n_runs={self.n_runs}")
        return self

    def fingerprint(self) -> str:
        """冻结配置内容的 sha256 指纹(写入 RunManifest,纯计算)。"""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

    def stratum_label(self) -> str:
        """本配置所属采样分层标签;同 stratum 内才聚合比较(计划书 6.5)。"""
        parts = {"region": self.region, "locale": self.locale, "tier": self.tier}
        return "|".join(parts[s] for s in self.strata if s in parts)
