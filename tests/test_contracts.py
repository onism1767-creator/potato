"""契约层测试:不可变性、必填字段、配置指纹。"""

import pytest
from pydantic import ValidationError

from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import RunManifest


def test_frozen_config_is_immutable(demo_cfg: FrozenConfig) -> None:
    with pytest.raises(ValidationError):
        demo_cfg.locale = "zh-CN"  # type: ignore[misc]


def test_run_manifest_requires_traceability_fields() -> None:
    """RunManifest 可追溯字段缺一不可(CLAUDE.md §2)。"""
    with pytest.raises(ValidationError):
        RunManifest.model_validate({"run_id": "r1", "provider": "mock"})


def test_fingerprint_changes_when_config_changes(demo_cfg: FrozenConfig) -> None:
    """指纹机械暴露"改了配置没 bump 版本"的静默基线污染。"""
    changed = demo_cfg.model_copy(update={"temperature": 0.7})
    assert demo_cfg.fingerprint() != changed.fingerprint()
    assert demo_cfg.fingerprint() == demo_cfg.fingerprint()


def test_stratum_label(demo_cfg: FrozenConfig) -> None:
    assert demo_cfg.stratum_label() == "US|en-US|standard"


# ---------------------------------------------------------------- v0.2 多引擎契约


def test_surface_spec_parse_provider_and_model() -> None:
    from aivis.contracts.adapter import SurfaceSpec

    assert SurfaceSpec.parse("openai:gpt-6-luna") == SurfaceSpec(
        provider="openai", model="gpt-6-luna"
    )
    assert SurfaceSpec.parse("anthropic") == SurfaceSpec(provider="anthropic", model=None)
    # model 内部可含冒号(Perplexity 预设档 preset:low)
    assert SurfaceSpec.parse("perplexity:preset:low") == SurfaceSpec(
        provider="perplexity", model="preset:low"
    )
    with pytest.raises(ValueError, match="表面标识"):
        SurfaceSpec.parse(":x")


def test_model_info_key_and_defaults() -> None:
    from aivis.contracts.adapter import ModelInfo

    m = ModelInfo(
        provider="gemini", model="gemini-3.8-flash", label="G", tier="standard", blurb="b"
    )
    assert m.key == "gemini:gemini-3.8-flash"
    assert m.supports_temperature and m.supports_region and m.pinned
    assert m.surface_kind == "answer" and m.status == "current"


def test_old_manifest_json_without_v02_fields_still_loads() -> None:
    """旧 JSONL 清单缺 v0.2 字段 → 默认 None/answer(不破坏已有原始层)。"""
    old = {
        "run_id": "r",
        "provider": "anthropic",
        "surface_id": "s",
        "model": "claude-sonnet-4-6",
        "tool_path": "web_search",
        "tool_version": "web_search_20250305",
        "locale": "en-US",
        "region": "US",
        "prompt_version": "p",
        "competitor_set_version": None,
        "tier": "standard",
        "max_uses": 3,
        "budget_cap_usd": 1.0,
        "temperature": 0.0,
        "n_runs": 3,
        "timestamp": "2026-01-01T00:00:00Z",
        "config_fingerprint": "abc",
    }
    m = RunManifest.model_validate(old)
    assert m.temperature_applied is None and m.region_applied is None
    assert m.surface_kind == "answer" and m.cost_usd_actual is None


def test_error_code_includes_refusal() -> None:
    from typing import get_args

    from aivis.contracts.records import ErrorCode

    assert "refusal" in get_args(ErrorCode)
