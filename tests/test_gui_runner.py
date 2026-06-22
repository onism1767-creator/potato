"""gui.runner 测试:冻结 round-trip + mock run 端到端(subprocess,零成本)+ key 守卫。"""

import time
from pathlib import Path
from typing import Any

import pytest

from aivis.configio import load_frozen_config
from aivis.gui.api import _config_from, _questions_from
from aivis.gui.runner import Runner, _run_command, start_run, write_wizard_config

_PAYLOAD: dict[str, Any] = {
    "focal_name": "Acme Corp",
    "focal_domain": "acme.com",
    "category": "widgets",
    "provider": "mock",
    "questions": [
        {"id": "d01", "intent": "discovery", "text": "best widgets?"},
        {"id": "b01", "intent": "brand_defense", "text": "Is Acme Corp any good?"},
    ],
}


def test_write_wizard_config_roundtrips(tmp_path: Path) -> None:
    # 冻结 → 读回必须等价(否则 aivis run 读不了向导冻结的配置)
    cfg = _config_from(_PAYLOAD, _questions_from(_PAYLOAD))
    write_wizard_config(tmp_path / "config", cfg)
    assert load_frozen_config(tmp_path / "config") == cfg


def _wait(runner: Runner, timeout: float = 90.0) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        st = runner.status()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.2)
    return runner.status()


def test_mock_run_end_to_end(tmp_path: Path) -> None:
    # 真起 aivis run 子进程(mock,零成本)→ 轮询到 done → report.html 落盘
    runner = Runner()
    out = start_run(runner, _PAYLOAD, tmp_path)
    assert out["state"] == "running" and out["job_id"]
    st = _wait(runner)
    assert st["state"] == "done", f"run 未成功: {st}"
    assert st["report_path"] and Path(st["report_path"]).exists()
    assert Path(st["report_path"]).read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_anthropic_requires_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="api_key"):
        start_run(Runner(), {**_PAYLOAD, "provider": "anthropic"}, tmp_path)


def test_status_idle_before_start() -> None:
    assert Runner().status() == {"state": "idle"}


def test_run_command_uses_python_module(tmp_path: Path) -> None:
    # 子进程:python -m aivis.cli ... --tier T ... run --yes(便携包里 sys.executable=官方 python)
    cmd = _run_command(
        config_dir=tmp_path / "c", data_dir=tmp_path / "d", provider="mock", tier="economy"
    )
    assert cmd[1:3] == ["-m", "aivis.cli"]
    assert cmd[-2:] == ["run", "--yes"] and "mock" in cmd
    assert "--tier" in cmd and cmd[cmd.index("--tier") + 1] == "economy"  # GUI 选档传到子进程
