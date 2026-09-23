"""跨层数据记录契约(唯一定义处,CLAUDE.md §2;字段基准 = BUILD_SPEC §4 + 已确认补丁)。

三层数据单向流动:
  ① 原始观测 raw(RawResponse / RunManifest,只追加)
  ② 核验证据 evidence(EvidenceRecord,按 URL 去重的核验缓存,可重算)
  ③ 分析 analysis(ExtractionResult / ScoreBundle,完全派生)

时间戳一律 str(ISO 8601 UTC),由 I/O 边缘显式传入,纯层不读时钟。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 封闭错误码集合:防止自由字符串把失败率指标搞碎
# refusal(v0.2):模型/安全层拒答(Claude 的 refusal 停止原因、Gemini SAFETY 等),
# 记为失败、不当空回答
ErrorCode = Literal["timeout", "rate_limit", "tool_error", "parse_error", "refusal", "other"]

# 证据档位:none < mentioned < cited < verified;只有 verified 进核心评分(CLAUDE.md §7)
Tier = Literal["none", "mentioned", "cited", "verified"]

OwnedOrEarned = Literal["owned", "earned"]

# 引用归属依据:domain=URL 域名属于品牌自有域;cited_metadata=引用自带的
# title/cited_text 含品牌清晰名(v1 的 earned-verified 近似口径,已拍板方案 B)
AttributionBasis = Literal["domain", "cited_metadata"]

Light = Literal["green", "yellow", "red"]

# ---------------------------------------------------------------- ① 原始层


class Citation(BaseModel):
    """provider 返回的一条引用,原样保存。"""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str | None = None
    cited_text: str | None = None
    position: int = Field(ge=0)


class RawResponse(BaseModel):
    """一次探测调用的原样返回。只由 probe 写入原始层,写后不改。"""

    model_config = ConfigDict(frozen=True)

    run_id: str
    question_id: str
    run_index: int = Field(ge=0)
    answer_text: str = ""
    citations: tuple[Citation, ...] = ()
    search_count: int = Field(ge=0)
    model: str
    finished_ok: bool
    error_code: ErrorCode | None = None
    error_detail: str | None = None
    raw_json: str
    timestamp: str


class RunManifest(BaseModel):
    """一次运行的完整可追溯清单(BUILD_SPEC 附录 A + config_fingerprint)。"""

    model_config = ConfigDict(frozen=True)

    run_id: str
    provider: str
    surface_id: str
    model: str
    tool_path: str
    tool_version: str | None
    locale: str
    region: str
    prompt_version: str
    competitor_set_version: str | None
    tier: str
    max_uses: int | None
    budget_cap_usd: float
    temperature: float
    n_runs: int
    timestamp: str
    search_count: int | None = None
    # 冻结配置内容指纹:版本号靠人 bump,指纹机械暴露"改了配置没 bump"的静默基线污染
    config_fingerprint: str
    # ---- v0.2 多引擎诚实字段(带默认,旧清单照常读回;None = 该记录未记录此项)----
    # 采样温度/区域是否真的传给了引擎:部分模型拒绝 temperature、部分 API 无区域参数,
    # 此时 temperature/region 字段只是"配置值",清单必须如实标注"未生效"。
    temperature_applied: bool | None = None
    region_applied: bool | None = None
    # 表面类型:answer(回答+引用)/ search_only(纯搜索结果,无回答文本)
    surface_kind: str = "answer"
    # 本次运行真实累计花费(按 usage 计价;mock 恒 0)——账单依据
    cost_usd_actual: float | None = None


# ---------------------------------------------------------------- ② 证据层


class EvidenceRecord(BaseModel):
    """一条被引 URL 的核验事实(按 normalized_url 去重的核验缓存,可重算)。

    只记录对 URL 的客观核验,不做档位判断——三档标签是 score 层的纯函数。
    v1 最小版:不抓页面正文,snippet 恒 None(BUILD_SPEC §6)。
    """

    model_config = ConfigDict(frozen=True)

    citation_url: str
    normalized_url: str
    canonical_domain: str
    owned_or_earned: OwnedOrEarned
    owner_brand: str | None = None
    source_class: str  # v1 收缩为 'brand_site' / 'third_party',v2 细化四档优先级
    reachable: bool
    check_method: str  # CHECK_OFFLINE(可达=假设)/ CHECK_HTTP_HEAD / CHECK_PAGE_FETCH
    snippet: str | None = None  # 抓取核验下为消毒短摘录,否则 None
    # 页面正文确实出现清晰名的品牌集合(仅抓取核验有;verified 升级口径的依据)
    brands_on_page: tuple[str, ...] = ()
    checked_at: str


# ---------------------------------------------------------------- ③ 分析层(extract 产物)


class ResponseRef(BaseModel):
    """对一条原始回答的引用键 + 成败状态(score 算失败率的依据)。"""

    model_config = ConfigDict(frozen=True)

    question_id: str
    run_index: int
    finished_ok: bool
    error_code: ErrorCode | None = None


class BrandMention(BaseModel):
    """品牌 × 回答的文本提及观测(保守计数,BUILD_SPEC §4)。

    清晰命中: mentioned=True,  counted=True
    歧义命中: mentioned=True,  counted=False, reason="ambiguous_alias:<名>"(记录不计分)
    未提及:   mentioned=False, counted=True(对"缺席"的有效观测,进覆盖率分母)
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    question_id: str
    run_index: int
    brand: str
    mentioned: bool
    first_position: int | None = None
    mention_count: int = Field(ge=0)
    counted: bool
    reason: str | None = None
    answer_length: int = Field(ge=0)


class CitationAttribution(BaseModel):
    """一条引用到品牌的归属(extract 纯函数产物;BUILD_SPEC §6 把 URL 归属划给 extract)。

    brand=None 表示无法归属(counted=False, reason="unattributed"),记录但不计分。
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    question_id: str
    run_index: int
    url: str
    normalized_url: str
    canonical_domain: str
    brand: str | None
    owned_or_earned: OwnedOrEarned | None = None
    basis: AttributionBasis | None = None
    counted: bool
    reason: str | None = None


class ExtractionResult(BaseModel):
    """extract(纯)的完整输出:score 的输入之一,进分析层存档。"""

    model_config = ConfigDict(frozen=True)

    run_id: str
    response_refs: tuple[ResponseRef, ...]
    mentions: tuple[BrandMention, ...] = ()
    citations: tuple[CitationAttribution, ...] = ()


# ---------------------------------------------------------------- ③ 分析层(score 产物)


class Interval(BaseModel):
    """带不确定区间的指标值(§7:每个数字都以区间呈现)。

    n=0 时约定 point=0, low=0, high=1, method="insufficient"(完全不确定,不给假精确)。
    """

    model_config = ConfigDict(frozen=True)

    point: float
    low: float
    high: float
    n: int = Field(ge=0)
    method: str


class TierAssignment(BaseModel):
    """品牌 × 回答的证据档位(score 纯函数判定,输入=BrandMention+CitationAttribution+Evidence)。"""

    model_config = ConfigDict(frozen=True)

    brand: str
    question_id: str
    run_index: int
    tier: Tier


class Metrics(BaseModel):
    """单品牌六指标(BUILD_SPEC §4 原文;口径备注见 ARCHITECTURE.md)。

    strict(默认排名口径)只算清晰名;expanded(诊断口径)额外含疑似缩写/歧义提及,
    两者并列、绝不混算——strict 字段不受 expanded 影响(F-004)。
    """

    model_config = ConfigDict(frozen=True)

    brand: str
    mention_coverage: Interval  # strict:仅清晰名(默认排名依据)
    owned_citation_rate: Interval
    earned_citation_rate: Interval
    prominence: Interval
    mention_sov: Interval | None = None
    citation_sov: Interval | None = None
    stability: Interval  # 比例(稳定问题占比)+ Wilson 区间
    n_obs: int = Field(ge=0)
    # expanded 诊断口径(含疑似缩写/歧义提及;仅供发现,不作排名依据,F-004)
    mention_coverage_expanded: Interval
    ambiguous_mention_count: int = Field(ge=0)


class HealthSummary(BaseModel):
    """自动验证健康灯(计划书 10.1 红线阈值表,确定性计算)。"""

    model_config = ConfigDict(frozen=True)

    citation_validity_rate: float
    ambiguous_ratio: float
    failure_rate: float
    light: Light
    flags: tuple[str, ...] = ()


class ScoreBundle(BaseModel):
    """score(纯)的输出容器:完全由 ①+② 派生,report 只读它。"""

    model_config = ConfigDict(frozen=True)

    run_id: str
    provider: str
    surface_id: str
    model: str
    model_tier: str
    prompt_version: str
    competitor_set_version: str | None
    config_fingerprint: str
    tier_basis: str  # 'verified-minimal-v1':v1 的 verified 近似口径标注
    stratum: str  # 'region|locale|tier';v1 单一 stratum,跨 stratum 只并列不混总分
    n_questions: int
    n_runs: int
    n_responses_ok: int
    n_responses_error: int
    generated_at: str  # 由 I/O 边缘显式传入,score 不读时钟
    engine_note: str  # "本结果仅代表此引擎"(CLAUDE.md §0 叙事红线)
    brands: tuple[Metrics, ...]
    tiers: tuple[TierAssignment, ...]
    evidence: tuple[EvidenceRecord, ...]
    health: HealthSummary
    # ---- v0.2 多引擎(从 RunManifest 派生,报告/对比页用)----
    surface_kind: str = "answer"  # search_only 时提及类指标不适用,报告如实标注
    cost_usd_actual: float | None = None  # 本表面真实花费(账单)


class RunLock(BaseModel):
    """run_id 的配置锁(append-only,首次探测前写入)。

    续跑同一 run_id 时,当前配置/引擎必须与首次完全一致,否则拒绝——防止把不同
    配置(题库/竞品集/模型/轮次)的结果混进同一 run_id,污染同一条基线(串味)。
    RunManifest 在运行结束才写,救不了"续跑时校验",故单独在开跑前落这把锁。
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    config_fingerprint: str
    provider: str
    model: str
    tool_version: str | None
    n_runs: int
    sequential: bool = False  # 续跑一致性:序贯/固定不能混入同一基线
    min_runs: int = 2
    prompt_version: str
    competitor_set_version: str | None
