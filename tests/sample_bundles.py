"""样例 ScoreBundle 构造器(供 examples/ 参考报告 + 同步测试共用,单一真相源)。

确定性、无 I/O;改了构造器或模板后跑 `python tools/regen_examples.py` 重生成 examples/。
"""

from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.contracts.records import (
    EvidenceRecord,
    HealthSummary,
    Interval,
    Metrics,
    ScoreBundle,
    TierAssignment,
)


def _iv(point: float, n: int = 72) -> Interval:
    return Interval(
        point=point, low=max(0.0, point - 0.08), high=min(1.0, point + 0.08), n=n, method="wilson"
    )


def single_brand_example() -> tuple[ScoreBundle, FrozenConfig]:
    """一个代表性的单品牌 bundle + cfg(含证据 + 逐题档位),用于参考报告与同步测试。"""
    metrics = Metrics(
        brand="Acme Corp",
        mention_coverage=_iv(0.62),
        owned_citation_rate=_iv(0.71),
        earned_citation_rate=_iv(0.45),
        prominence=_iv(0.38),
        mention_sov=None,
        citation_sov=None,
        stability=_iv(0.88),
        n_obs=72,
        mention_coverage_expanded=_iv(0.66),
        ambiguous_mention_count=2,
    )
    questions = tuple(
        Question(id=qid, intent=intent, text=text)
        for qid, intent, text in (
            ("d01", "discovery", "What are the best widgets for beginners?"),
            ("d02", "discovery", "Which widgets do professionals recommend?"),
            ("c01", "comparison", "How do top brands of widgets compare on quality?"),
            ("l01", "local", "What widgets are best for professional or commercial use?"),
            ("b01", "brand_defense", "Is Acme Corp a good widget brand?"),
        )
    )
    tiers = tuple(
        TierAssignment(brand="Acme Corp", question_id=qid, run_index=0, tier=tier)
        for qid, tier in (
            ("d01", "mentioned"),
            ("d02", "none"),
            ("c01", "cited"),
            ("l01", "mentioned"),
            ("b01", "verified"),
        )
    )
    evidence = (
        EvidenceRecord(
            citation_url="https://acme.com/about",
            normalized_url="https://acme.com/about",
            canonical_domain="acme.com",
            owned_or_earned="owned",
            owner_brand="Acme Corp",
            source_class="brand_site",
            reachable=True,
            check_method="offline-assumed",
            checked_at="2026-06-16T00:00:00Z",
        ),
        EvidenceRecord(
            citation_url="https://techreview.example/widgets",
            normalized_url="https://techreview.example/widgets",
            canonical_domain="techreview.example",
            owned_or_earned="earned",
            source_class="third_party",
            reachable=True,
            check_method="offline-assumed",
            checked_at="2026-06-16T00:00:00Z",
        ),
        EvidenceRecord(
            citation_url="https://buyersguide.example/best",
            normalized_url="https://buyersguide.example/best",
            canonical_domain="buyersguide.example",
            owned_or_earned="earned",
            source_class="third_party",
            reachable=True,
            check_method="offline-assumed",
            checked_at="2026-06-16T00:00:00Z",
        ),
    )
    bundle = ScoreBundle(
        run_id="run-abcd1234",
        provider="anthropic",
        surface_id="anthropic_api_web_search",
        model="claude-sonnet-4-6",
        model_tier="standard",
        prompt_version="acme-questions-v1",
        competitor_set_version="acme-brands-v1",
        config_fingerprint="a1b2c3d4" * 8,
        tier_basis="verified-offline-v1",
        stratum="US|en-US|standard",
        n_questions=24,
        n_runs=3,
        n_responses_ok=72,
        n_responses_error=0,
        generated_at="2026-06-16T00:00:00Z",
        engine_note="本结果仅代表此引擎",
        brands=(metrics,),
        tiers=tiers,
        evidence=evidence,
        health=HealthSummary(
            citation_validity_rate=0.93,
            ambiguous_ratio=0.12,
            failure_rate=0.0,
            light="green",
            flags=(),
        ),
    )
    cfg = FrozenConfig(
        version="acme-2026.06-v1",
        prompt_version="acme-questions-v1",
        competitor_set_version="acme-brands-v1",
        focal_domain="acme.com",
        peers=(Brand(name="Acme Corp", domains=("acme.com",), is_focal=True),),
        competitors_on=False,
        questions=questions,
        tier="standard",
        locale="en-US",
        region="US",
        temperature=0.0,
        n_runs=3,
        max_uses=3,
        budget_cap_usd=5.0,
    )
    return bundle, cfg
