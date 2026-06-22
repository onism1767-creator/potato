"""render_html 单品牌专用排版 + 多品牌回归(白底绿渐变模板、英文)。

单品牌:无 Highest/Lowest 排名、无 SOV 甜甜圈,改"Mention coverage · strict"整宽面板 +
单品牌说明;hero 标签变 "Brand · mention coverage"。多品牌:排名条 + 甜甜圈照旧。
"""

from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.contracts.records import HealthSummary, Interval, Metrics, ScoreBundle
from aivis.pipeline.report import render_html

_FORBIDDEN_EXTERNAL = ("<script", "<link", 'src="http', "@import", "cdnjs", "googleapis", "unpkg")


def _iv(point: float, n: int = 60) -> Interval:
    return Interval(
        point=point, low=max(0.0, point - 0.1), high=min(1.0, point + 0.1), n=n, method="wilson"
    )


def _metrics(brand: str, cov: float, sov: float | None = None) -> Metrics:
    return Metrics(
        brand=brand,
        mention_coverage=_iv(cov),
        owned_citation_rate=_iv(0.5),
        earned_citation_rate=_iv(0.4),
        prominence=_iv(0.3),
        mention_sov=_iv(sov) if sov is not None else None,
        citation_sov=_iv(sov) if sov is not None else None,
        stability=_iv(0.9),
        n_obs=60,
        mention_coverage_expanded=_iv(cov),
        ambiguous_mention_count=0,
    )


def _bundle(brands: tuple[Metrics, ...]) -> ScoreBundle:
    return ScoreBundle(
        run_id="run-abcdef12",
        provider="anthropic",
        surface_id="anthropic_api_web_search",
        model="claude-sonnet-4-6",
        model_tier="standard",
        prompt_version="t-q-v1",
        competitor_set_version="t-b-v1",
        config_fingerprint="0" * 64,
        tier_basis="verified-offline-v1",
        stratum="US|en-US|standard",
        n_questions=24,
        n_runs=3,
        n_responses_ok=60,
        n_responses_error=0,
        generated_at="2026-06-10T00:00:00Z",
        engine_note="本结果仅代表此引擎",
        brands=brands,
        tiers=(),
        evidence=(),
        health=HealthSummary(
            citation_validity_rate=1.0,
            ambiguous_ratio=0.0,
            failure_rate=0.0,
            light="green",
            flags=(),
        ),
    )


def _cfg() -> FrozenConfig:
    return FrozenConfig(
        version="t-v1",
        prompt_version="t-q-v1",
        competitor_set_version="t-b-v1",
        focal_domain="acme.com",
        peers=(Brand(name="Acme Corp", domains=("acme.com",), is_focal=True),),
        competitors_on=False,
        questions=(Question(id="d01", intent="discovery", text="best widgets?"),),
        tier="standard",
        locale="en-US",
        region="US",
        temperature=0.0,
        n_runs=3,
        max_uses=3,
        budget_cap_usd=5.0,
    )


def test_single_brand_layout() -> None:
    h = render_html(_bundle((_metrics("Acme Corp", 0.62),)), _cfg())
    assert h.startswith("<!DOCTYPE html>") and h.rstrip().endswith("</html>")
    assert "Acme Corp" in h
    assert "Mention coverage · strict" in h  # 单品牌专用面板标题
    assert "Single brand measured" in h  # 单品牌说明
    assert "Brand · mention coverage" in h  # hero 标签(单品牌版)
    # 单品牌不应出现多品牌的排名/对比/甜甜圈
    assert "Top brand · mention coverage" not in h
    assert "Highest" not in h and "Lowest" not in h
    assert "Mention coverage ranking" not in h
    assert "stroke-dasharray" not in h  # 无甜甜圈
    low = h.lower()
    for forbidden in _FORBIDDEN_EXTERNAL:
        assert forbidden not in low, f"不应含外链/脚本: {forbidden}"


def test_single_brand_shows_wilson_interval() -> None:
    # 单品牌仍带不确定区间(Wilson 须),不给假精确(§7)
    h = render_html(_bundle((_metrics("Acme Corp", 0.62),)), _cfg())
    assert "Wilson 95% CI" in h
    assert "<svg" in h  # 内联 SVG 条(点估计 + 区间须)


def test_css_colors_not_corrupted_by_font_substitution() -> None:
    # 回归:_HTML_CSS 曾用 .replace("F", font) 把所有含 F 的十六进制色污染成非法值
    h = render_html(_bundle((_metrics("Acme Corp", 0.62),)), _cfg())
    assert "background:#FFFFFF" in h  # body 白底完好(含 F)
    assert "#F4F7F2" in h and "#EEF6E4" in h  # 面板 / 卡片绿色完好(含 F)
    assert "#8FD744" in h  # 渐变绿完好(含 F)
    assert "sans-serif4F7F2" not in h  # 损坏特征不复现(F 被替换会在此留痕)
    assert "__FONT__" not in h  # 占位符已完全替换
    assert "font-family:-apple-system" in h  # 字体确实注入了


def test_empty_evidence_and_matrix_show_notes() -> None:
    # 空 evidence/tiers 时显示说明,而非裸表头
    h = render_html(_bundle((_metrics("Acme Corp", 0.62),)), _cfg())
    assert "No cited URLs verified in this run." in h
    assert "No per-question evidence in this run." in h


def test_network_destinations_provider_aware() -> None:
    # 网络去向按 provider 渲染:anthropic 显真实 host;mock 显"0 external API calls",不假报 host
    anth = _bundle((_metrics("Acme Corp", 0.62),))
    assert "api.anthropic.com" in render_html(anth, _cfg())
    mock = anth.model_copy(update={"provider": "mock"})
    h = render_html(mock, _cfg())
    assert "mock provider" in h and "0 external API calls" in h
    assert "api.anthropic.com" not in h


def test_multi_brand_keeps_ranking_and_sov() -> None:
    brands = (_metrics("Acme Corp", 0.7, sov=0.6), _metrics("Beta Co", 0.4, sov=0.4))
    h = render_html(_bundle(brands), _cfg())
    assert "Mention coverage ranking · strict" in h
    assert "Highest" in h and "Lowest" in h
    assert "Top brand · mention coverage" in h
    assert "stroke-dasharray" in h  # 两品牌有 SOV → 甜甜圈在
    assert "Single brand measured" not in h
