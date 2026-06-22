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
