"""score 黄金场景测试:三档判定、六指标口径、健康灯红线。"""

import pytest

from aivis.contracts.checker import CHECK_OFFLINE, CHECK_PAGE_FETCH, CheckOutcome
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import (
    Citation,
    EvidenceRecord,
    ExtractionResult,
    RawResponse,
    RunManifest,
)
from aivis.pipeline.extract import extract
from aivis.pipeline.score import _is_verified, score
from aivis.pipeline.verify import verify
from tests.conftest import FIXED_NOW, make_response

BundleInputs = tuple[ExtractionResult, dict[str, EvidenceRecord]]


class _DeadLinkChecker:
    """gone.example 不可达,其余可达(构造 cited-but-not-verified 场景)。"""

    @property
    def method(self) -> str:
        return CHECK_OFFLINE

    def check(self, url: str) -> CheckOutcome:
        return CheckOutcome(reachable="gone.example" not in url, method=self.method, snippet=None)


def _evidence(
    *, reachable: bool, method: str, brands_on_page: tuple[str, ...] = ()
) -> EvidenceRecord:
    return EvidenceRecord(
        citation_url="https://x.example/p",
        normalized_url="https://x.example/p",
        canonical_domain="x.example",
        owned_or_earned="earned",
        source_class="third_party",
        reachable=reachable,
        check_method=method,
        brands_on_page=brands_on_page,
        checked_at=FIXED_NOW,
    )


def test_verified_v2_requires_brand_on_page() -> None:
    """🟥 F-006b:抓取核验下,可达但页面没提到该品牌 → 不算 verified(修软 404 误计)。"""
    # 软 404:页面可达但正文没提 Deuter
    soft404 = _evidence(reachable=True, method=CHECK_PAGE_FETCH, brands_on_page=())
    assert _is_verified("Deuter", soft404) is False
    # 真支持:页面可达且正文提到 Deuter
    good = _evidence(reachable=True, method=CHECK_PAGE_FETCH, brands_on_page=("Deuter",))
    assert _is_verified("Deuter", good) is True
    # 页面提了别的品牌,但没提 Deuter → 对 Deuter 不算
    other = _evidence(reachable=True, method=CHECK_PAGE_FETCH, brands_on_page=("Osprey",))
    assert _is_verified("Deuter", other) is False


def test_verified_offline_falls_back_to_reachable() -> None:
    """离线/HEAD 没读页面内容 → 退回可达性近似(无法声称页面提到品牌)。"""
    assert _is_verified("Deuter", _evidence(reachable=True, method=CHECK_OFFLINE)) is True
    assert _is_verified("Deuter", _evidence(reachable=False, method=CHECK_OFFLINE)) is False
    assert _is_verified("Deuter", None) is False


def _manifest(cfg: FrozenConfig, run_id: str = "run-test") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        provider="mock",
        surface_id="mock_api_web_search",
        model="mock-model-standard",
        tool_path="web_search_mock",
        tool_version="mock-1",
        locale=cfg.locale,
        region=cfg.region,
        prompt_version=cfg.prompt_version,
        competitor_set_version=cfg.competitor_set_version,
        tier=cfg.tier,
        max_uses=cfg.max_uses,
        budget_cap_usd=cfg.budget_cap_usd,
        temperature=cfg.temperature,
        n_runs=cfg.n_runs,
        timestamp=FIXED_NOW,
        search_count=6,
        config_fingerprint=cfg.fingerprint(),
    )


def _scenario_responses() -> list[RawResponse]:
    """3 题 × 2 轮:Alpha 干净提及 + owned/earned 引用;Bravo 只有歧义;q3 有一轮失败。"""
    owned = Citation(url="https://alphapacks.example/p", title="Alpha Packs | Shop", position=0)
    earned = Citation(
        url="https://reviews.example/best", title="Alpha Packs field review", position=1
    )
    dead = Citation(url="https://gone.example/404", title="Alpha Packs old page", position=2)
    return [
        # q1:两轮都干净提及 Alpha,带 owned+earned 引用 → verified
        make_response(
            question_id="q1",
            run_index=0,
            answer_text="Alpha Packs leads the pack.",
            citations=(owned, earned),
        ),
        make_response(
            question_id="q1", run_index=1, answer_text="Alpha Packs again.", citations=(owned,)
        ),
        # q2:轮0 提及 Alpha(无引用);轮1 只有 Bravo 的歧义别名 → 搁置
        make_response(question_id="q2", run_index=0, answer_text="Try Alpha Packs."),
        make_response(question_id="q2", run_index=1, answer_text="Bravo is decent, they say."),
        # q3:轮0 引用了死链;轮1 工具失败
        make_response(
            question_id="q3", run_index=0, answer_text="Alpha Packs maybe.", citations=(dead,)
        ),
        make_response(
            question_id="q3",
            run_index=1,
            answer_text="",
            finished_ok=False,
            error_code="tool_error",
        ),
    ]


@pytest.fixture()
def bundle_inputs(demo_cfg: FrozenConfig) -> BundleInputs:
    extraction = extract(_scenario_responses(), demo_cfg)
    records = verify(extraction, demo_cfg, checker=_DeadLinkChecker(), now=FIXED_NOW)
    return extraction, {r.normalized_url: r for r in records}


def test_tier_ladder(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    extraction, evidence = bundle_inputs
    result = score(extraction, evidence, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    tiers = {(t.brand, t.question_id, t.run_index): t.tier for t in result.tiers}
    assert tiers[("Alpha Packs", "q1", 0)] == "verified"  # 可达引用
    assert tiers[("Alpha Packs", "q2", 0)] == "mentioned"  # 仅文本提及
    assert tiers[("Alpha Packs", "q3", 0)] == "cited"  # 引用了但死链 → 不到 verified
    assert tiers[("Bravo Gear", "q2", 1)] == "none"  # 歧义搁置不进档
    assert ("Alpha Packs", "q3", 1) not in tiers  # 失败轮不产生档位


def test_six_metrics_values(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    extraction, evidence = bundle_inputs
    result = score(extraction, evidence, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    alpha = next(m for m in result.brands if m.brand == "Alpha Packs")
    bravo = next(m for m in result.brands if m.brand == "Bravo Gear")
    # 5 条成功回答;Alpha 干净提及 4 条(q1×2, q2r0, q3r0)
    assert alpha.n_obs == 5
    assert alpha.mention_coverage.point == pytest.approx(4 / 5)
    # owned verified:q1×2(alphapacks.example 可达);q3r0 死链不算
    assert alpha.owned_citation_rate.point == pytest.approx(2 / 5)
    # earned verified:q1r0(reviews.example 标题含清晰名且可达)
    assert alpha.earned_citation_rate.point == pytest.approx(1 / 5)
    # Bravo:无干净提及,无引用
    assert bravo.mention_coverage.point == 0.0
    assert bravo.owned_citation_rate.point == 0.0
    # SOV:提及事件 4+0 → Alpha 100%
    assert alpha.mention_sov is not None and alpha.mention_sov.point == pytest.approx(1.0)
    # 稳定性:q1 两轮一致;q2 两轮不一致(r0 提及/r1 未提及);q3 只剩一轮 → 2/3(点值)
    assert alpha.stability.point == pytest.approx(2 / 3)
    # 区间纪律:每个比例指标都带区间
    assert alpha.mention_coverage.low < alpha.mention_coverage.point < alpha.mention_coverage.high


def test_strict_vs_expanded_modes(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    """🟥 F-004:Bravo 仅靠疑似缩写"Bravo"被提及 → strict 不计,expanded 计入,两者分开。"""
    extraction, evidence = bundle_inputs
    result = score(extraction, evidence, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    bravo = next(m for m in result.brands if m.brand == "Bravo Gear")
    assert bravo.mention_coverage.point == 0.0  # strict:清晰名 0 次
    assert bravo.ambiguous_mention_count == 1  # q2r1 "Bravo is decent"(疑似缩写)
    assert bravo.mention_coverage_expanded.point == pytest.approx(1 / 5)  # expanded 计入
    # Alpha 全是清晰名 → strict == expanded(疑似缩写不改变它)
    alpha = next(m for m in result.brands if m.brand == "Alpha Packs")
    assert alpha.mention_coverage_expanded.point == alpha.mention_coverage.point


def test_report_renders_strict_expanded_and_verdict(
    demo_cfg: FrozenConfig, bundle_inputs: BundleInputs
) -> None:
    """报告:STRICT 在前、EXPANDED 诊断段、显式"无法区分"判语都出现。"""
    from aivis.pipeline.report import render_markdown

    extraction, evidence = bundle_inputs
    result = score(extraction, evidence, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    md = render_markdown(result, demo_cfg)
    assert "STRICT" in md and "EXPANDED" in md  # 两种口径并列
    assert "不作排名依据" in md  # expanded 明确标注不进排名
    assert "排名无法区分" in md  # F-005 判语(单轮 + 区间重叠)
    assert "页面提及品牌" in md  # F-006a 证据子状态列
    # STRICT 段必须在 EXPANDED 段之前(默认口径在前)
    assert md.index("· STRICT") < md.index("· EXPANDED")


def test_health_light_and_flags(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    extraction, evidence = bundle_inputs
    result = score(extraction, evidence, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    h = result.health
    # 3 个唯一 URL,1 个死链 → 2/3 ≈ 0.667 < 0.70 红线
    assert h.citation_validity_rate == pytest.approx(2 / 3)
    assert "citation-validity-red" in h.flags
    # 失败率 1/6 ≈ 0.167 > 0.15 红线
    assert h.failure_rate == pytest.approx(1 / 6)
    assert "failure-red" in h.flags
    assert h.light == "red"
    # 歧义比例:提及记录 5 条(Alpha4 + Bravo1),搁置 1 → 0.2(黄线 0.25 以下)
    assert h.ambiguous_ratio == pytest.approx(1 / 5)


def test_bundle_traceability_fields(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    extraction, evidence = bundle_inputs
    manifest = _manifest(demo_cfg)
    result = score(extraction, evidence, manifest, demo_cfg, generated_at=FIXED_NOW)
    assert result.config_fingerprint == demo_cfg.fingerprint()
    assert result.tier_basis == "verified-offline-v1"  # _DeadLinkChecker 用 offline 方式
    assert result.stratum == "US|en-US|standard"
    assert "仅代表此引擎" in result.engine_note
    assert result.generated_at == FIXED_NOW  # 时钟由 I/O 边缘注入,score 不自己读


def test_tier_basis_ignores_unused_cache_evidence(
    demo_cfg: FrozenConfig, bundle_inputs: BundleInputs
) -> None:
    """🟥 口径只按本次实际用到的证据:缓存里残留的别种核验方式不该污染本次 tier_basis。"""
    extraction, evidence = bundle_inputs  # 本次全是 offline 核验
    polluted = dict(evidence)
    polluted["https://unused.example/p"] = EvidenceRecord(
        citation_url="https://unused.example/p",
        normalized_url="https://unused.example/p",
        canonical_domain="unused.example",
        owned_or_earned="earned",
        source_class="third_party",
        reachable=True,
        check_method=CHECK_PAGE_FETCH,  # 旧缓存里的 page-fetch,但本次没引用命中
        brands_on_page=("Whatever",),
        checked_at=FIXED_NOW,
    )
    result = score(extraction, polluted, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    assert result.tier_basis == "verified-offline-v1"  # 不是 verified-mixed


def test_run_id_mismatch_rejected(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    extraction, evidence = bundle_inputs
    with pytest.raises(ValueError, match="run_id"):
        score(
            extraction,
            evidence,
            _manifest(demo_cfg, run_id="other"),
            demo_cfg,
            generated_at=FIXED_NOW,
        )


def test_score_is_deterministic(demo_cfg: FrozenConfig, bundle_inputs: BundleInputs) -> None:
    extraction, evidence = bundle_inputs
    manifest = _manifest(demo_cfg)
    a = score(extraction, evidence, manifest, demo_cfg, generated_at=FIXED_NOW)
    b = score(extraction, evidence, manifest, demo_cfg, generated_at=FIXED_NOW)
    assert a == b
