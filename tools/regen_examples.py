"""重新生成 examples/ 下的参考报告(改了模板/构造器后跑一次,人工核对 diff 再提交)。

用法: python tools/regen_examples.py
单一真相源是 tests/sample_bundles.py;test_examples.py 断言 examples/ 与 render_html 同步。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests.sample_bundles import single_brand_example  # noqa: E402

from aivis.pipeline.report import render_html  # noqa: E402


def main() -> None:
    bundle, cfg = single_brand_example()
    out = _ROOT / "examples" / "single_brand_report.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render_html(bundle, cfg), encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
