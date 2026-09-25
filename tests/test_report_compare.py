"""多引擎并列对比报告(渲染纯函数)测试:并列不混总分、search_only 标不适用、账单合计、确定性。"""

import json
from pathlib import Path

import pytest

from aivis.contracts.records import ScoreBundle
from aivis.pipeline.report_compare import render_comparison_html, render_comparison_markdown

GOLDEN = Path(__file__).parent / "fixtures" / "golden_e2e_bundle.json"


def _bundle(**update: object) -> ScoreBundle:
    base = ScoreBundle.model_validate(json.loads(GOLDEN.read_text(encoding="utf-8")))
    return base.model_copy(update=update)


def _two() -> list[ScoreBundle]:
    return [
        _bundle(
            run_id="r-a",
            provider="openai",
            model="gpt-6-luna",
            surface_id="openai_responses_web_search",
            cost_usd_actual=0.1234,
            temperature_applied=False,
            region_applied=True,
        ),
        _bundle(
            run_id="r-b",
            provider="perplexity",
            model="search",
            surface_id="perplexity_search_api",
            surface_kind="search_only",
            cost_usd_actual=0.05,
            temperature_applied=False,
            region_applied=True,
        ),
    ]


def test_markdown_lists_surfaces_side_by_side_with_bill() -> None:
    md = render_comparison_markdown(_two())
    assert "只并列、不混总分、不跨引擎排名" in md
    assert "openai:gpt-6-luna" in md and "perplexity:search" in md
    assert "$0.1234" in md and "$0.0500" in md and "**$0.1734**" in md  # 账单合计
    assert "不适用(n/a)" in md  # search_only 表面的提及覆盖率标不适用
    # 引用率对 search_only 照常算(不是 n/a):该表面的 earned 引用率格子必须是区间
    lines = [ln for ln in md.splitlines() if ln.startswith("| ") and "n/a" not in ln]
    assert any("[" in ln and "]" in ln for ln in lines)
    assert "温度生效" in md and "✗" in md  # 诚实字段进表


def test_markdown_warns_when_fingerprints_differ() -> None:
    a, b = _two()
    md = render_comparison_markdown([a, b.model_copy(update={"config_fingerprint": "other"})])
    assert "配置指纹不一致" in md
    assert "配置指纹不一致" not in render_comparison_markdown([a, b])


def test_html_is_single_file_offline_and_escaped() -> None:
    bundles = _two()
    bundles[0] = bundles[0].model_copy(update={"model": "gpt-6-luna<script>x</script>"})
    page = render_comparison_html(bundles)
    assert page.startswith("<!DOCTYPE html>") and "<script>" not in page
    assert "&lt;script&gt;" in page and "Multi-engine comparison" in page
    assert "http://" not in page.replace("http://www.w3.org", "") and "cdn" not in page.lower()
    assert "$0.1734" in page


def test_rendering_is_deterministic_and_rejects_empty() -> None:
    bundles = _two()
    assert render_comparison_markdown(bundles) == render_comparison_markdown(_two())
    assert render_comparison_html(bundles) == render_comparison_html(_two())
    with pytest.raises(ValueError):
        render_comparison_markdown([])
