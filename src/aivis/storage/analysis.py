"""分析层 ③:完全派生的产物(extraction / score bundle / 报告文件),可随版本重算。

目录布局:analysis/<run_id>/extraction.json | score.json | report.md | metrics.csv
多表面并列对比:analysis/compare/<compare_id>/comparison.md | comparison.html
"""

from pathlib import Path

from aivis.contracts.records import ExtractionResult, ScoreBundle

_EXTRACTION_FILE = "extraction.json"
_SCORE_FILE = "score.json"


class AnalysisStore:
    """分析产物存储(JSON 文件,按 run_id 分目录;派生数据,允许重写)。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def write_extraction(self, extraction: ExtractionResult) -> Path:
        path = self._run_dir(extraction.run_id) / _EXTRACTION_FILE
        path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
        return path

    def read_extraction(self, run_id: str) -> ExtractionResult:
        path = self._run_dir(run_id) / _EXTRACTION_FILE
        return ExtractionResult.model_validate_json(path.read_text(encoding="utf-8"))

    def write_bundle(self, bundle: ScoreBundle) -> Path:
        path = self._run_dir(bundle.run_id) / _SCORE_FILE
        path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        return path

    def read_bundle(self, run_id: str) -> ScoreBundle:
        path = self._run_dir(run_id) / _SCORE_FILE
        return ScoreBundle.model_validate_json(path.read_text(encoding="utf-8"))

    def write_report_file(self, run_id: str, filename: str, content: str) -> Path:
        """写报告产物(report.md / metrics.csv 等渲染结果)。"""
        path = self._run_dir(run_id) / filename
        path.write_text(content, encoding="utf-8")
        return path

    def write_comparison(self, compare_id: str, filename: str, content: str) -> Path:
        """写多表面并列对比产物(analysis/compare/<compare_id>/comparison.md|.html)。"""
        d = self._root / "compare" / compare_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        path.write_text(content, encoding="utf-8")
        return path

    def _run_dir(self, run_id: str) -> Path:
        d = self._root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d
