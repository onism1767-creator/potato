"""score —— 🟥 纯函数层:三档判定 + 六指标 + Wilson 区间 + 健康灯。

禁止网络/磁盘/时钟(generated_at 由 I/O 边缘显式传入)。
三档口径(CLAUDE.md §7):mentioned → cited → verified,只有 verified 进核心评分。
档位判定放在本层(而非 verify)的理由见 ARCHITECTURE.md:它是核心计分规则,
必须可重算、可黄金测试;v2 升级 earned 摘录核验时只改这里,证据层数据原样复用。
"""

from collections.abc import Iterable, Mapping

from aivis.contracts.checker import CHECK_HTTP_HEAD, CHECK_OFFLINE, CHECK_PAGE_FETCH
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import (
    EvidenceRecord,
    ExtractionResult,
    HealthSummary,
    Interval,
    Light,
    Metrics,
    RunManifest,
    ScoreBundle,
    Tier,
    TierAssignment,
)
from aivis.stats.bootstrap import bootstrap_mean_interval
from aivis.stats.wilson import wilson_interval

# verified 口径随核验方式动态命名(诚实标注证据强度,F-006)
_TIER_BASIS = {
    CHECK_PAGE_FETCH: "verified-page-v2",  # 可达 + 页面正文确实提到该品牌(最强)
    CHECK_HTTP_HEAD: "verified-reachable-v1",  # 可达=HTTP 状态码(未读正文)
    CHECK_OFFLINE: "verified-offline-v1",  # 可达=离线假设(最弱)
}
_TIER_BASIS_MIXED = "verified-mixed"
_TIER_BASIS_NONE = "verified-none"

_SEARCH_ONLY_NOTE = (
    "此表面为纯搜索结果列表、无生成回答:提及类指标不适用(恒为 0),"
    "引用类指标 = 结果链接(pure search results, no generated answer)。"
)


def _engine_note(manifest: RunManifest) -> str:
    """叙事红线(CLAUDE.md §0):每份结果必须标注 surface_id 与"仅代表此引擎"。"""
    note = (
        "本结果仅代表此引擎(represents this engine only):"
        f"{manifest.provider} / {manifest.surface_id} / {manifest.model} "
        "web-search 可见度(代理测量),不是 AI 排名真相。"
    )
    if manifest.surface_kind == "search_only":
        note += " " + _SEARCH_ONLY_NOTE
    return note


_INSUFFICIENT = Interval(point=0.0, low=0.0, high=1.0, n=0, method="insufficient")

_ResponseKey = tuple[str, int]  # (question_id, run_index)


def _is_verified(brand: str, record: EvidenceRecord | None) -> bool:
    """verified 口径(F-006 升级):

    - 抓取核验(page-fetch):可达 **且** 页面正文确实提到该品牌(brands_on_page)。
      直接修掉"软 404 页可达但根本没提品牌"被误计的问题。
    - 离线/HEAD:没读页面内容,退回"可达性近似"(口径更弱,tier_basis 如实标注)。
    """
    if record is None or not record.reachable:
        return False
    if record.check_method == CHECK_PAGE_FETCH:
        return brand in record.brands_on_page
    return True  # offline/head:可达性近似(无法声称页面提到品牌)


def _tier_basis(records: Iterable[EvidenceRecord]) -> str:
    """按本次运行**实际用到**的核验方式给 verified 口径动态命名。

    🟥 只看 used_evidence(被本次引用命中的证据),不扫整个证据缓存——否则缓存里
    残留的别种核验方式(如旧的 page-fetch)会把本次纯 offline 的报告误标成 mixed。
    """
    methods = {e.check_method for e in records}
    if not methods:
        return _TIER_BASIS_NONE
    if len(methods) == 1:
        return _TIER_BASIS.get(next(iter(methods)), _TIER_BASIS_MIXED)
    return _TIER_BASIS_MIXED


_KeySets = dict[str, set[_ResponseKey]]  # brand -> 命中该档的回答键集合


def _aggregate_mentions(
    extraction: ExtractionResult, brand_names: list[str]
) -> tuple[_KeySets, _KeySets]:
    """按品牌聚合 strict(清晰名)与 expanded(疑似缩写/歧义)提及键集合(F-004,分开)。"""
    clean: _KeySets = {b: set() for b in brand_names}
    ambiguous: _KeySets = {b: set() for b in brand_names}
    for m in extraction.mentions:
        if not m.mentioned:
            continue
        key = (m.question_id, m.run_index)
        if m.counted:
            clean[m.brand].add(key)
        else:  # mentioned 但未计入 strict → 仅进 expanded 诊断
            ambiguous[m.brand].add(key)
    return clean, ambiguous


def _aggregate_citations(
    extraction: ExtractionResult, evidence: Mapping[str, EvidenceRecord], brand_names: list[str]
) -> tuple[_KeySets, _KeySets, _KeySets, _KeySets, set[str]]:
    """聚合 cited / owned-verified / earned-verified / verified 键集合 + 缺失证据 URL。"""
    cited: _KeySets = {b: set() for b in brand_names}
    owned_verified: _KeySets = {b: set() for b in brand_names}
    earned_verified: _KeySets = {b: set() for b in brand_names}
    missing: set[str] = set()
    for c in extraction.citations:
        if c.brand is None or not c.counted:
            continue
        key = (c.question_id, c.run_index)
        cited[c.brand].add(key)
        if c.normalized_url not in evidence:
            missing.add(c.normalized_url)
            continue
        if _is_verified(c.brand, evidence.get(c.normalized_url)):
            if c.owned_or_earned == "owned":
                owned_verified[c.brand].add(key)
            elif c.owned_or_earned == "earned":
                earned_verified[c.brand].add(key)
    verified: _KeySets = {b: owned_verified[b] | earned_verified[b] for b in brand_names}
    return cited, owned_verified, earned_verified, verified, missing


def _build_tiers(
    brand_names: list[str],
    ok_keys: list[_ResponseKey],
    verified_keys: _KeySets,
    cited_keys: _KeySets,
    clean_mention_keys: _KeySets,
) -> list[TierAssignment]:
    """三档判定(纯;v2 升级核验口径只改 _is_verified,这里不变)。"""
    tiers: list[TierAssignment] = []
    for brand in brand_names:
        for key in sorted(ok_keys):
            tier: Tier = "none"
            if key in verified_keys[brand]:
                tier = "verified"
            elif key in cited_keys[brand]:
                tier = "cited"
            elif key in clean_mention_keys[brand]:
                tier = "mentioned"
            tiers.append(
                TierAssignment(brand=brand, question_id=key[0], run_index=key[1], tier=tier)
            )
    return tiers


def _build_metrics(
    extraction: ExtractionResult,
    brand_names: list[str],
    n_ok: int,
    sov_enabled: bool,
    clean_mention_keys: _KeySets,
    ambiguous_mention_keys: _KeySets,
    owned_verified_keys: _KeySets,
    earned_verified_keys: _KeySets,
    verified_keys: _KeySets,
    ok_keys: list[_ResponseKey],
) -> list[Metrics]:
    """六指标 + Wilson 区间(纯)。"""
    total_mention_events = sum(len(clean_mention_keys[b]) for b in brand_names)
    total_verified_events = sum(len(verified_keys[b]) for b in brand_names)
    metrics: list[Metrics] = []
    for brand in brand_names:
        prominences = sorted(
            1.0 - m.first_position / m.answer_length
            for m in extraction.mentions
            if m.brand == brand
            and m.mentioned
            and m.counted
            and m.first_position is not None
            and m.answer_length > 0
        )
        # 位置显著度是连续值的均值,用 bootstrap 给区间(确定性,小样本不假装精度)
        prominence = bootstrap_mean_interval(prominences) if prominences else _INSUFFICIENT
        expanded_keys = clean_mention_keys[brand] | ambiguous_mention_keys[brand]
        metrics.append(
            Metrics(
                brand=brand,
                mention_coverage=wilson_interval(len(clean_mention_keys[brand]), n_ok),
                owned_citation_rate=wilson_interval(len(owned_verified_keys[brand]), n_ok),
                earned_citation_rate=wilson_interval(len(earned_verified_keys[brand]), n_ok),
                prominence=prominence,
                mention_coverage_expanded=wilson_interval(len(expanded_keys), n_ok),
                ambiguous_mention_count=len(ambiguous_mention_keys[brand]),
                mention_sov=(
                    wilson_interval(len(clean_mention_keys[brand]), total_mention_events)
                    if sov_enabled
                    else None
                ),
                citation_sov=(
                    wilson_interval(len(verified_keys[brand]), total_verified_events)
                    if sov_enabled
                    else None
                ),
                stability=_stability(clean_mention_keys[brand], ok_keys),
                n_obs=n_ok,
            )
        )
    return metrics


def score(
    extraction: ExtractionResult,
    evidence: Mapping[str, EvidenceRecord],
    manifest: RunManifest,
    cfg: FrozenConfig,
    *,
    generated_at: str,
    partial: bool = False,
) -> ScoreBundle:
    """由 ①(经 extract)+ ② 完全派生指标(纯;同输入恒同输出)。

    partial=True:这轮没跑完(预算到顶停下)就出的阶段性结果 —— 指标只覆盖已问到的那部分
    (按题目顺序,靠后的题可能一次都没问),健康灯至少为黄并带 partial-run 标记,报告如实标注。
    """
    if extraction.run_id != manifest.run_id:
        raise ValueError(
            f"extraction.run_id={extraction.run_id} 与 manifest.run_id={manifest.run_id} 不一致"
        )

    ok_keys: list[_ResponseKey] = [
        (r.question_id, r.run_index) for r in extraction.response_refs if r.finished_ok
    ]
    n_ok = len(ok_keys)
    n_error = len(extraction.response_refs) - n_ok
    brand_names = [b.name for b in cfg.peers]
    sov_enabled = len(brand_names) >= 2  # 无同行/竞品时 SOV 整体移除(计划书第 4 章)

    # 聚合各档键集合 → 三档判定 → 六指标(均为纯辅助函数;score 只编排)
    clean_mention_keys, ambiguous_mention_keys = _aggregate_mentions(extraction, brand_names)
    cited_keys, owned_verified_keys, earned_verified_keys, verified_keys, missing_evidence_urls = (
        _aggregate_citations(extraction, evidence, brand_names)
    )
    tiers = _build_tiers(brand_names, ok_keys, verified_keys, cited_keys, clean_mention_keys)
    metrics = _build_metrics(
        extraction,
        brand_names,
        n_ok,
        sov_enabled,
        clean_mention_keys,
        ambiguous_mention_keys,
        owned_verified_keys,
        earned_verified_keys,
        verified_keys,
        ok_keys,
    )

    health = _health(
        extraction,
        evidence,
        metrics,
        cfg,
        n_ok,
        n_error,
        missing_evidence_urls,
        mentions_apply=manifest.surface_kind != "search_only",
        partial=partial,
    )

    used_evidence = tuple(
        sorted(
            {
                evidence[c.normalized_url]
                for c in extraction.citations
                if c.normalized_url in evidence
            },
            key=lambda e: e.normalized_url,
        )
    )

    return ScoreBundle(
        run_id=manifest.run_id,
        provider=manifest.provider,
        surface_id=manifest.surface_id,
        model=manifest.model,
        model_tier=manifest.tier,
        prompt_version=manifest.prompt_version,
        competitor_set_version=manifest.competitor_set_version,
        config_fingerprint=manifest.config_fingerprint,
        tier_basis=_tier_basis(used_evidence),
        stratum=cfg.stratum_label(),
        n_questions=len(cfg.questions),
        n_runs=cfg.n_runs,
        n_responses_ok=n_ok,
        n_responses_error=n_error,
        generated_at=generated_at,
        engine_note=_engine_note(manifest),
        brands=tuple(metrics),
        tiers=tuple(tiers),
        evidence=used_evidence,
        health=health,
        surface_kind=manifest.surface_kind,
        cost_usd_actual=manifest.cost_usd_actual,
        temperature_applied=manifest.temperature_applied,
        region_applied=manifest.region_applied,
    )


def _stability(mention_keys: set[_ResponseKey], ok_keys: list[_ResponseKey]) -> Interval:
    """v1 稳定性:同一问题跨轮"清晰提及结果一致"的问题占比 + Wilson 区间(它是比例)。"""
    by_question: dict[str, list[bool]] = {}
    for q, ri in ok_keys:
        by_question.setdefault(q, []).append((q, ri) in mention_keys)
    if not by_question:
        return _INSUFFICIENT
    stable = sum(1 for outcomes in by_question.values() if len(set(outcomes)) <= 1)
    return wilson_interval(stable, len(by_question))


def _health(
    extraction: ExtractionResult,
    evidence: Mapping[str, EvidenceRecord],
    metrics: list[Metrics],
    cfg: FrozenConfig,
    n_ok: int,
    n_error: int,
    missing_evidence_urls: set[str],
    *,
    mentions_apply: bool = True,
    partial: bool = False,
) -> HealthSummary:
    """自动验证健康灯(计划书 10.1):总体 = 最差指标,确定性计算。

    mentions_apply=False(纯搜索表面,没有回答正文):提及类指标不适用,不拿它判"排名无法区分"。
    """
    flags: list[str] = []

    unique_urls = {c.normalized_url for c in extraction.citations}
    if unique_urls:
        reachable = sum(1 for u in unique_urls if u in evidence and evidence[u].reachable)
        validity = reachable / len(unique_urls)
    else:
        validity = 1.0
        flags.append("no-citations")
    if missing_evidence_urls:
        flags.append(f"missing-evidence:{len(missing_evidence_urls)}")

    mentioned_records = [m for m in extraction.mentions if m.mentioned]
    shelved = [m for m in mentioned_records if not m.counted]
    ambiguous_ratio = len(shelved) / len(mentioned_records) if mentioned_records else 0.0

    total = n_ok + n_error
    failure_rate = n_error / total if total else 1.0

    t = cfg.verify_thresholds
    light: Light = "green"

    def _worsen(level: Light) -> None:
        nonlocal light
        order = {"green": 0, "yellow": 1, "red": 2}
        if order[level] > order[light]:
            light = level

    if validity < t.citation_validity_red:
        _worsen("red")
        flags.append("citation-validity-red")
    elif validity < t.citation_validity_yellow:
        _worsen("yellow")
        flags.append("citation-validity-yellow")
    if ambiguous_ratio > t.ambiguous_red:
        _worsen("red")
        flags.append("ambiguous-red")
    elif ambiguous_ratio > t.ambiguous_yellow:
        _worsen("yellow")
        flags.append("ambiguous-yellow")
    if failure_rate > t.failure_red:
        _worsen("red")
        flags.append("failure-red")
    elif failure_rate > t.failure_yellow:
        _worsen("yellow")
        flags.append("failure-yellow")

    # 全部品牌的提及覆盖区间存在公共交集 → 无法区分排名(计划书 10.1:红"排名仅供参考")
    # 纯搜索表面没有回答正文:提及覆盖恒为 0、区间必然全等,拿它判红会把引用数据也一并说成"仅供参考"。
    coverages = [m.mention_coverage for m in metrics if m.mention_coverage.n > 0]
    if (
        mentions_apply
        and len(coverages) >= 2
        and max(c.low for c in coverages) <= min(c.high for c in coverages)
    ):
        _worsen("red")
        flags.append("all-intervals-overlap:ranking-indicative-only")

    if partial:  # 没跑完的阶段性结果:只覆盖已问到的部分,不能当成完整基线看
        _worsen("yellow")
        flags.append("partial-run:budget-cap")

    return HealthSummary(
        citation_validity_rate=validity,
        ambiguous_ratio=ambiguous_ratio,
        failure_rate=failure_rate,
        light=light,
        flags=tuple(flags),
    )
