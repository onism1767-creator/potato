"""examples/ 参考报告与 render_html 输出保持同步(golden,防模板悄悄漂移)。

有意改模板/构造器时:跑 `python tools/regen_examples.py` 重生成 + 人工核对 diff 再提交。
"""

from pathlib import Path

from aivis.pipeline.report import render_html
from aivis.storage.analysis import AnalysisStore
from tests.sample_bundles import single_brand_example

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "single_brand_report.html"


def test_single_brand_example_in_sync() -> None:
    bundle, cfg = single_brand_example()
    expected = render_html(bundle, cfg)
    assert _EXAMPLE.exists(), (
        "examples/single_brand_report.html 缺失,运行 python tools/regen_examples.py"
    )
    actual = _EXAMPLE.read_text(encoding="utf-8")
    assert actual == expected, (
        "examples/single_brand_report.html 与 render_html 输出不一致——"
        "若有意改模板,运行 python tools/regen_examples.py 重生成并人工核对后提交"
    )


def test_html_auto_updates_with_data() -> None:
    # 数据驱动(自动更新):换数据 → HTML 跟着变,新数据出现在 HTML 里(非静态死模板)
    bundle, cfg = single_brand_example()
    h1 = render_html(bundle, cfg)
    assert "62%" in h1
    m = bundle.brands[0]
    changed_metric = m.model_copy(
        update={
            "brand": "Zenith Labs",
            "mention_coverage": m.mention_coverage.model_copy(
                update={"point": 0.31, "low": 0.23, "high": 0.39}
            ),
        }
    )
    changed = bundle.model_copy(update={"brands": (changed_metric,)})
    h2 = render_html(changed, cfg)
    assert h1 != h2
    assert "Zenith Labs" in h2 and "31%" in h2


def test_bundle_store_roundtrip_then_render(tmp_path: Path) -> None:
    # 自动导入:写入分析层 → 读回无损 → 渲染一致 → report.html 落盘含真实数据
    bundle, cfg = single_brand_example()
    store = AnalysisStore(tmp_path / "analysis")
    store.write_bundle(bundle)
    loaded = store.read_bundle(bundle.run_id)
    assert loaded == bundle  # 导入无损(写入=读回)
    assert render_html(loaded, cfg) == render_html(bundle, cfg)  # 读回渲染与原始一致
    path = store.write_report_file(bundle.run_id, "report.html", render_html(loaded, cfg))
    written = path.read_text(encoding="utf-8")
    assert "Acme Corp" in written and "62%" in written  # 落盘报告含真实数据
