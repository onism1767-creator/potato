"""端到端骨架测试:demo 配置上 probe→extract→verify→score→report 全链路(mock,零成本)。

黄金样本:tests/fixtures/golden_e2e_bundle.json 冻结整个 ScoreBundle。
有意改动指标口径时必须连同黄金期望一起更新并在 PR 里说明(CLAUDE.md §10)。
"""

import json
from pathlib import Path

import pytest

from aivis.configio import load_frozen_config
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import ScoreBundle
from aivis.pipeline.extract import extract
from aivis.pipeline.probe import run_probe
from aivis.pipeline.report import render_csv, render_html, render_markdown
from aivis.pipeline.score import score
from aivis.pipeline.verify import CHECK_OFFLINE, OfflineChecker, verify
from aivis.providers.mock import MockAdapter
from aivis.storage.evidence import EvidenceStore
from aivis.storage.raw import RawStore

FIXED_NOW = "2026-06-10T00:00:00Z"
GOLDEN_BUNDLE = Path(__file__).parent / "fixtures" / "golden_e2e_bundle.json"
DEMO_CONFIG_DIR = Path(__file__).parent.parent / "configs" / "demo"


@pytest.fixture(scope="module")
def demo_config() -> FrozenConfig:
    return load_frozen_config(DEMO_CONFIG_DIR)


def _run_pipeline(cfg: FrozenConfig, data_dir: Path) -> ScoreBundle:
    """注入固定 run_id 与时钟 → 全链路完全确定性。"""
    raw = RawStore(data_dir / "raw")
    evidence_store = EvidenceStore(data_dir / "evidence")
    manifest = run_probe(MockAdapter(), cfg, raw, run_id="e2e-golden", now=lambda: FIXED_NOW)
    extraction = extract(raw.read_responses(manifest.run_id), cfg)
    evidence_store.save(verify(extraction, cfg, checker=OfflineChecker(), now=FIXED_NOW))
    return score(extraction, evidence_store.load(), manifest, cfg, generated_at=FIXED_NOW)


def test_e2e_matches_golden_bundle(demo_config: FrozenConfig, tmp_path: Path) -> None:
    bundle = _run_pipeline(demo_config, tmp_path)
    if not GOLDEN_BUNDLE.exists():
        pytest.fail(
            "黄金样本缺失。首次生成请运行: python tools/regen_golden.py 并人工检查输出后提交。"
        )
    golden = ScoreBundle.model_validate(json.loads(GOLDEN_BUNDLE.read_text(encoding="utf-8")))
    assert bundle == golden, "ScoreBundle 与黄金样本不一致——若是有意改口径,请同步更新黄金期望并说明"


def test_e2e_basic_invariants(demo_config: FrozenConfig, tmp_path: Path) -> None:
    bundle = _run_pipeline(demo_config, tmp_path)
    assert bundle.n_questions == 24 and bundle.n_runs == 3
    assert bundle.n_responses_ok + bundle.n_responses_error == 72
    assert len(bundle.brands) == 5
    assert {m.brand for m in bundle.brands} == {
        "Osprey",
        "Gregory",
        "Deuter",
        "REI Co-op",
        "Granite Gear",
    }
    # 每个比例指标都带区间;品牌防御题点名的品牌应有可观测提及
    for m in bundle.brands:
        assert m.mention_coverage.n == bundle.n_responses_ok
    # mock + 离线核验:证据全部诚实标注 offline-assumed
    assert bundle.evidence and all(e.check_method == CHECK_OFFLINE for e in bundle.evidence)


def test_e2e_report_renders(demo_config: FrozenConfig, tmp_path: Path) -> None:
    bundle = _run_pipeline(demo_config, tmp_path)
    md = render_markdown(bundle, demo_config)
    assert "仅代表此引擎" in md  # 叙事红线
    assert "在场矩阵" in md and "健康灯" in md and "透明块" in md
    assert "· STRICT" in md  # 默认排名口径
    assert "排名无法区分" in md  # F-005:单轮 demo 必然无法区分
    assert "verified-offline-v1" in md  # 核验口径如实标注(F-006a)
    csv_text = render_csv(bundle)
    assert csv_text.splitlines()[0].startswith("run_id,")
    # 5 品牌 × 7 行(六指标 + stability)
    assert len(csv_text.splitlines()) == 1 + 5 * 7


def test_e2e_html_report_is_offline_single_file(demo_config: FrozenConfig, tmp_path: Path) -> None:
    """HTML 报告:单文件 + 完全离线(渲染时零外部请求)+ 关键内容在。"""
    h = render_html(_run_pipeline(demo_config, tmp_path), demo_config)
    assert h.startswith("<!DOCTYPE html>") and h.rstrip().endswith("</html>")
    assert "this engine" in h  # 叙事红线(英文披露)
    assert "Osprey" in h and "Share of voice" in h
    assert "stroke-dasharray" in h  # 内联 SVG 甜甜圈(非外部图表库)
    # 🟥 离线单文件:不引任何外部资源
    low = h.lower()
    for forbidden in ("<script", "<link", 'src="http', "@import", "cdnjs", "googleapis", "unpkg"):
        assert forbidden not in low, f"HTML 报告不应含外链/脚本: {forbidden}"


def test_e2e_probe_resume_is_idempotent(demo_config: FrozenConfig, tmp_path: Path) -> None:
    """同 run_id 重跑(崩溃续跑场景)不会写重原始层。"""
    raw = RawStore(tmp_path / "raw")
    m1 = run_probe(MockAdapter(), demo_config, raw, run_id="resume", now=lambda: FIXED_NOW)
    n_after_first = len(raw.read_responses("resume"))
    m2 = run_probe(MockAdapter(), demo_config, raw, run_id="resume", now=lambda: FIXED_NOW)
    assert len(raw.read_responses("resume")) == n_after_first
    assert m1.search_count == m2.search_count
