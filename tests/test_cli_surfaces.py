"""CLI 多引擎(v0.2):--model / --surfaces 多表面串跑 + 并列对比 + 账单 / models 目录 / 密钥前置检查。

全部走 mock 或离线路径:零成本、不联网、不需要密钥。
"""

from pathlib import Path

import pytest

from aivis import cli
from aivis.storage.raw import RawStore


def test_run_surfaces_produces_per_surface_reports_and_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    rc = cli.main(
        [
            "run",
            "--config-dir",
            "configs/demo",
            "--data-dir",
            str(data),
            "--surfaces",
            "mock:mock-model-economy,mock:mock-model-high",
            "--yes",
        ]
    )
    assert rc == 0
    reports = sorted((data / "analysis").glob("*/report.md"))
    assert len(reports) == 2  # 每个表面独立 run_id + 报告
    manifests = RawStore(data / "raw").read_manifests()
    assert {m.model for m in manifests} == {"mock-model-economy", "mock-model-high"}
    assert all(m.cost_usd_actual == 0.0 and m.surface_kind == "answer" for m in manifests)
    compare = list((data / "analysis" / "compare").glob("*/comparison.md"))
    assert len(compare) == 1 and compare[0].with_suffix(".html").exists()
    md = compare[0].read_text(encoding="utf-8")
    assert "mock:mock-model-economy" in md and "mock:mock-model-high" in md
    assert "只并列、不混总分" in md
    out = capsys.readouterr().out
    assert "PROGRESS surface 1/2 mock:mock-model-economy" in out
    assert "PROGRESS surface 2/2 mock:mock-model-high" in out
    assert "BILL mock:mock-model-economy 0.0000" in out and "COMPARISON " in out
    assert "合计 $0.0000" in out


def test_run_single_surface_with_explicit_model(tmp_path: Path) -> None:
    data = tmp_path / "data"
    rc = cli.main(
        [
            "run",
            "--config-dir",
            "configs/demo",
            "--data-dir",
            str(data),
            "--provider",
            "mock",
            "--model",
            "mock-model-high",
        ]
    )
    assert rc == 0
    manifests = RawStore(data / "raw").read_manifests()
    assert len(manifests) == 1 and manifests[0].model == "mock-model-high"
    assert not (data / "analysis" / "compare").exists()  # 单表面不出对比页


def test_run_refuses_unknown_surface_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        ["run", "--config-dir", "configs/demo", "--data-dir", str(tmp_path), "--surfaces", "bing:x"]
    )
    assert rc == 2
    assert "表面/模型错误" in capsys.readouterr().err


def test_run_checks_env_keys_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺密钥在开跑前(任何付费调用之前)就拦下,并点名缺哪个环境变量。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    rc = cli.main(
        [
            "run",
            "--config-dir",
            "configs/demo",
            "--data-dir",
            str(tmp_path),
            "--surfaces",
            "openai:gpt-6-luna,gemini:gemini-3.8-flash",
            "--yes",
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err and "GEMINI_API_KEY" in err
    assert not (tmp_path / "raw").exists() or not list((tmp_path / "raw").glob("*.jsonl"))


def test_run_surfaces_rejects_run_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(
        [
            "run",
            "--config-dir",
            "configs/demo",
            "--data-dir",
            str(tmp_path),
            "--surfaces",
            "mock:mock-model-high",
            "--run-id",
            "x",
        ]
    )
    assert rc == 2 and "--run-id" in capsys.readouterr().err


def test_estimate_surfaces_lists_each_and_total(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(
        [
            "estimate",
            "--config-dir",
            "configs/demo",
            "--surfaces",
            "openai:gpt-6-luna,gemini:gemini-3.8-flash,perplexity:search",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "openai:gpt-6-luna" in out and "gemini:gemini-3.8-flash" in out
    assert "perplexity:search" in out and "合计(保守上界)" in out


def test_models_command_lists_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["models", "--config-dir", "configs/demo"]) == 0
    out = capsys.readouterr().out
    assert "24 个表面" in out
    for needle in ("gpt-6-luna", "claude-fable-5-1", "gemini-3.8-flash", "preset:low", "search"):
        assert needle in out
    assert "no-region" in out and "unpinned" in out and "search-only" in out
    assert "PERPLEXITY_API_KEY" in out and "api.openai.com" in out
