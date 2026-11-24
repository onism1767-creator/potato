"""冻结配置契约(CLAUDE.md §2):来自 questions.yml + brands.yml 的不可变对象。

FrozenConfig 必须显式传入,禁止全局可变状态或环境隐式读取。
任何配置改动 = 版本号 bump(新基线);fingerprint() 机械暴露"改了没 bump"。

写错的配置当场报错,不许静默带病运行(2026-11-23 收紧,用户拍板):
  - 四个类型都是 extra="forbid":不认识的键名(多半是拼错,如 max_use)直接拒绝;
  - 题目 id、品牌名各自不许重复(见 FrozenConfig._check_no_duplicates)。
这只拒绝坏配置;合法配置的序列化与指纹不变,已有 run 的配置锁照常对得上。
"""

import hashlib
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Intent = Literal["discovery", "comparison", "local", "brand_defense"]
Track = Literal["benchmark"]  # 'operational' v1.1 才加入


class Question(BaseModel):
    """一条冻结的探测问题(四意图桶配方,计划书第 7 章)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    intent: Intent
    text: str
    track: Track = "benchmark"


class Brand(BaseModel):
    """一个被测品牌(focal / peer / competitor 共用同一形状)。

    保守计数(CLAUDE.md §7):name + aliases 是清晰名(计分);
    ambiguous_aliases 是缩写/歧义名(命中只记录,不计分)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    aliases: tuple[str, ...] = ()
    ambiguous_aliases: tuple[str, ...] = ()
    domains: tuple[str, ...] = Field(min_length=1)
    is_focal: bool = False


class VerifyThresholds(BaseModel):
    """自动验证红线阈值(计划书 10.1;初始猜测值,拿真实数据后校准)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    citation_validity_yellow: float = 0.85
    citation_validity_red: float = 0.70
    ambiguous_yellow: float = 0.25
    ambiguous_red: float = 0.40
    failure_yellow: float = 0.05
    failure_red: float = 0.15


class FrozenConfig(BaseModel):
    """一次基线的全部冻结输入。不可变;改动即新基线。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    @model_validator(mode="after")
    def _check_no_duplicates(self) -> "FrozenConfig":
        """题目 id、品牌名各自不许重复 —— 重复不会让程序报错,只会悄悄把结果算错。

        - 题目 id:原始层按 (run_id, question_id, run_index) 去重,同 id 的第二道题会被当成
          "已经问过",一次都不会被问到(用 mock 实测过),报告里也不会有任何提示。
        - 品牌名:按匹配口径比较(不分大小写、去首尾空格)。重名的两个品牌会在同一处提及上
          各记一次,份额随之算错。
        """
        problems: list[str] = []
        id_counts = Counter(q.id for q in self.questions)
        repeated_ids = [qid for qid, n in id_counts.items() if n > 1]
        if repeated_ids:
            problems.append(f"题目 id 重复:{'、'.join(repeated_ids)}(每道题的 id 必须不同)")
        by_name: dict[str, list[str]] = {}
        for brand in self.peers:
            by_name.setdefault(brand.name.strip().casefold(), []).append(brand.name.strip())
        clashes = [" / ".join(names) for names in by_name.values() if len(names) > 1]
        if clashes:
            problems.append(f"品牌名重复:{';'.join(clashes)}(不分大小写;同一个品牌只能出现一次)")
        if problems:
            raise ValueError(";".join(problems))
        return self

    def fingerprint(self) -> str:
        """冻结配置内容的 sha256 指纹(写入 RunManifest,纯计算)。"""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

    def baseline_fingerprint(self) -> str:
        """测量基线指纹:与 fingerprint() 相同,但不含 budget_cap_usd(纯计算)。

        预算上限只决定"花到多少停",不改变问什么、怎么问 —— 所以调高预算后续跑同一个 run_id
        不算换基线(配置锁用它比对);其余任何字段改动仍会改变本指纹。
        """
        payload = self.model_dump_json(exclude={"budget_cap_usd"})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def stratum_label(self) -> str:
        """本配置所属采样分层标签;同 stratum 内才聚合比较(计划书 6.5)。"""
        parts = {"region": self.region, "locale": self.locale, "tier": self.tier}
        return "|".join(parts[s] for s in self.strata if s in parts)
