"""有意更新黄金样本时运行本脚本(然后人工检查 diff 再提交,CLAUDE.md §10)。

用法: .venv\\Scripts\\python.exe tools\\regen_golden.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from aivis.configio import load_frozen_config  # noqa: E402
from test_e2e_mock import DEMO_CONFIG_DIR, GOLDEN_BUNDLE, _run_pipeline  # noqa: E402


def main() -> None:
    cfg = load_frozen_config(DEMO_CONFIG_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = _run_pipeline(cfg, Path(tmp))
    GOLDEN_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_BUNDLE.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"黄金样本已写入: {GOLDEN_BUNDLE}")
    print(
        f"  健康灯={bundle.health.light}  ok={bundle.n_responses_ok}  "
        f"evidence={len(bundle.evidence)}"
    )
    print("请人工检查内容合理后再提交。")


if __name__ == "__main__":
    main()
