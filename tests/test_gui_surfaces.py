"""GUI 多引擎(v0.2):/api/models 目录 + 多选表面运行 + 各表面报告/对比页/账单 + 密钥前置校验。

全部走 mock / 离线:零成本、不联网、不需要密钥。
"""

import json
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aivis.gui import api
from aivis.gui.runner import Runner, _resolve_surfaces, _run_command, start_run
from aivis.gui.server import GuiServer, build_server
from tests.test_gui import _req


@pytest.fixture()
def server() -> Iterator[GuiServer]:
    """与 test_gui 同形的本地服务 fixture(临时端口;本文件自建,避免跨文件导入 fixture)。"""
    s = build_server(port=0)
    thread = threading.Thread(target=s.serve_forever, daemon=True)
    thread.start()
    yield s
    s.shutdown()
    s.server_close()
    shutil.rmtree(s.work_root, ignore_errors=True)


_BASE: dict[str, Any] = {
    "focal_name": "Acme Corp",
    "focal_domain": "acme.com",
    "category": "widgets",
    "budget_cap_usd": 13.0,
    "questions": [
        {"id": "d01", "intent": "discovery", "text": "best widgets?"},
        {"id": "b01", "intent": "brand_defense", "text": "Is Acme Corp any good?"},
    ],
}


def _wait(runner: Runner, timeout: float = 120.0) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        st = runner.status()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.2)
    return runner.status()


# ---------------------------------------------------------------- /api/models


def test_models_lists_all_real_providers_with_estimates() -> None:
    out = api.models(_BASE)
    names = [p["name"] for p in out["providers"]]
    assert names == [
        "anthropic",
        "openai",
        "gemini",
        "perplexity",
        "grok",
    ]  # mock 不在"真实测量"清单里
    assert out["n_calls"] == 2 * 3 and out["budget_cap_usd"] == 13.0
    total = sum(len(p["models"]) for p in out["providers"])
    assert total == 27  # 6 + 6 + 5 + 4 + 6
    for p in out["providers"]:
        assert p["env_key"] and p["endpoint"]
        for m in p["models"]:
            assert m["key"] == f"{p['name']}:{m['model']}" and m["est_usd"] >= 0
            assert set(m) >= {"label", "blurb", "status", "supports_temperature", "pinned"}
    search = next(m for p in out["providers"] for m in p["models"] if m["model"] == "search")
    assert search["surface_kind"] == "search_only"


def test_models_over_budget_flag() -> None:
    out = api.models({**_BASE, "budget_cap_usd": 0.001})
    assert all(m["over_budget"] for p in out["providers"] for m in p["models"])


# ---------------------------------------------------------------- 载荷解析(离线)


def test_resolve_surfaces_mock_real_and_legacy() -> None:
    cfg = api._config_from(_BASE, api._questions_from(_BASE))
    assert _resolve_surfaces({**_BASE, "provider": "mock"}, cfg) == (
        ["mock:mock-model-standard"],
        {},
    )
    surfaces, env = _resolve_surfaces(
        {
            **_BASE,
            "provider": "real",
            "surfaces": ["openai:gpt-6-luna", "perplexity:search", "perplexity:preset:low"],
            "api_keys": {"openai": "sk-o", "perplexity": "pplx-x"},
        },
        cfg,
    )
    assert surfaces == ["openai:gpt-6-luna", "perplexity:search", "perplexity:preset:low"]
    assert env == {"OPENAI_API_KEY": "sk-o", "PERPLEXITY_API_KEY": "pplx-x"}  # 一家一把,共用
    # 旧接口(provider=anthropic + api_key + tier)仍可用
    surfaces, env = _resolve_surfaces({**_BASE, "provider": "anthropic", "api_key": "sk-ant"}, cfg)
    assert surfaces == ["anthropic:claude-sonnet-4-6"] and env == {"ANTHROPIC_API_KEY": "sk-ant"}


def test_resolve_surfaces_rejects_missing_key_empty_and_unknown() -> None:
    cfg = api._config_from(_BASE, api._questions_from(_BASE))
    with pytest.raises(ValueError, match="api_key"):
        _resolve_surfaces(
            {**_BASE, "provider": "real", "surfaces": ["gemini:gemini-3.8-flash"], "api_keys": {}},
            cfg,
        )
    with pytest.raises(ValueError, match="至少选择"):
        _resolve_surfaces({**_BASE, "provider": "real", "surfaces": []}, cfg)
    with pytest.raises(ValueError, match="无模型"):
        _resolve_surfaces(
            {
                **_BASE,
                "provider": "real",
                "surfaces": ["openai:gpt-9"],
                "api_keys": {"openai": "k"},
            },
            cfg,
        )


def test_run_command_passes_surfaces_not_keys(tmp_path: Path) -> None:
    cmd = _run_command(
        config_dir=tmp_path,
        data_dir=tmp_path,
        surfaces=["mock:mock-model-economy", "mock:mock-model-high"],
    )
    assert "--surfaces" in cmd and "mock:mock-model-economy,mock:mock-model-high" in cmd
    assert "run" in cmd and "--yes" in cmd
    assert not any("KEY" in c or "sk-" in c for c in cmd)  # 密钥绝不进命令行


# ---------------------------------------------------------------- 端到端(mock 子进程,零成本)


def test_multi_surface_mock_run_reports_and_bill(tmp_path: Path) -> None:
    runner = Runner()
    payload = {
        **_BASE,
        "provider": "real",
        "surfaces": ["mock:mock-model-economy", "mock:mock-model-high"],
        "api_keys": {},
    }
    out = start_run(runner, payload, tmp_path)
    assert out["state"] == "running"
    st = _wait(runner)
    assert st["state"] == "done", f"run 未成功: {st}"
    assert st["surface_total"] == 2 and st["surfaces"] == payload["surfaces"]
    assert len(st["report_paths"]) == 2 and all(Path(p).exists() for p in st["report_paths"])
    assert st["comparison_path"] and Path(st["comparison_path"]).exists()
    assert st["report_path"] == st["comparison_path"]  # 多表面:默认打开并列对比
    assert [b["label"] for b in st["bills"]] == payload["surfaces"]
    assert st["spent_total"] == 0.0 and st["percent"] == 100
    assert st["provider"] == "mock"  # 全 mock 表面 → 前端按免费演示提示成本


def test_server_report_by_surface_index(server: GuiServer) -> None:
    body = {
        **_BASE,
        "provider": "real",
        "surfaces": ["mock:mock-model-economy", "mock:mock-model-high"],
        "api_keys": {},
    }
    code, _ = _req(server, "/api/run", method="POST", token=server.token, body=body)
    assert code == 200
    state: dict[str, Any] = {}
    end = time.time() + 120
    while time.time() < end:
        _, s = _req(server, "/api/status", token=server.token)
        state = json.loads(s)
        if state["state"] in ("done", "error"):
            break
        time.sleep(0.3)
    assert state.get("state") == "done", f"run e2e 未成功: {state}"
    code, page = _req(server, "/report", token=server.token)
    assert code == 200 and "Multi-engine comparison" in page  # 默认 = 并列对比页
    code, page = _req(server, "/report?surface=1", token=server.token)
    assert code == 200 and "mock-model-high" in page  # 第 2 个表面的单表面报告
    code, _ = _req(server, "/report?surface=9", token=server.token)
    assert code == 404
    code, text = _req(server, "/api/models", method="POST", token=server.token, body=_BASE)
    assert code == 200 and len(json.loads(text)["providers"]) == 5
