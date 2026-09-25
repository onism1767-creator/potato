"""单表面报告(v0.2):诚实字段进报告、search_only 表面提及类指标标不适用、去 Claude 专属措辞、
网络去向主机与目录一致。"""

from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import ScoreBundle
from aivis.pipeline.report import render_html, render_markdown
from aivis.pipeline.report_html import _PROVIDER_HOST
from aivis.providers import catalog
from tests.test_report_html import _bundle, _metrics


def _search_bundle() -> ScoreBundle:
    return _bundle((_metrics("Alpha Packs", 0.0), _metrics("Bravo Gear", 0.0, 0.5))).model_copy(
        update={
            "provider": "perplexity",
            "surface_id": "perplexity_search_api",
            "model": "search",
            "surface_kind": "search_only",
            "temperature_applied": False,
            "region_applied": True,
            "cost_usd_actual": 0.1234,
        }
    )


def test_markdown_shows_honesty_fields_and_bill(demo_cfg: FrozenConfig) -> None:
    md = render_markdown(_search_bundle(), demo_cfg)
    assert "# AI web-search 可见度报告" in md and "Claude API" not in md
    assert "| 温度 / 区域是否生效 | ✗ / ✓" in md
    assert "| 实际花费(账单) | $0.1234 |" in md
    assert "| 表面类型 | search_only(纯搜索结果" in md


def test_markdown_search_only_marks_mention_metrics_na(demo_cfg: FrozenConfig) -> None:
    md = render_markdown(_search_bundle(), demo_cfg)
    row = next(ln for ln in md.splitlines() if ln.startswith("| Alpha Packs |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    # 列:品牌 | 提及覆盖率 | 自有域引用率 | earned 引用率 | 位置显著度 | 提及 SOV |
    #     引用 SOV | 稳定性
    assert cells[1] == "不适用" and cells[4] == "不适用" and cells[5] == "不适用"
    assert cells[7] == "不适用"
    assert "[" in cells[2] and "[" in cells[3]  # 引用类指标照常带区间
    assert "纯搜索结果列表" in md


def test_markdown_answer_surface_has_no_na(demo_cfg: FrozenConfig) -> None:
    md = render_markdown(_bundle((_metrics("Alpha Packs", 0.6, 0.5),)), demo_cfg)
    row = next(ln for ln in md.splitlines() if ln.startswith("| Alpha Packs |"))
    assert "不适用" not in row and "纯搜索结果列表" not in md
    assert "| 温度 / 区域是否生效 | — / —" in md  # 旧运行未记录 → —


def test_html_generic_title_banner_and_hosts(demo_cfg: FrozenConfig) -> None:
    page = render_html(_search_bundle(), demo_cfg)
    assert "AI Visibility Dashboard" in page and "<title>AI visibility report · perplexity" in page
    assert "Claude" not in page
    assert "Search-only surface" in page and ">n/a<" in page
    assert "api.perplexity.ai" in page
    assert "Temperature / region applied" in page and "✗ / ✓" in page
    assert "$0.1234" in page


def test_provider_hosts_match_catalog() -> None:
    """报告层不 import providers 包(避免拖入 SDK),主机表靠本测试与目录锁定一致。"""
    expected = {k: v.endpoint for k, v in catalog.PROVIDERS.items() if k != "mock"}
    assert expected == _PROVIDER_HOST
