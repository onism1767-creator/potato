"""configcheck 黄金/单元测试:质检员按起草规则书逐条把关。

覆盖:demo 干净 · smoke 仅配比告警 · 反偏置硬错 · 配比按 phase 分严重度 ·
focal/peer 模式不变量。规则代号见 src/aivis/configcheck.py。
"""

from pathlib import Path

from aivis.configcheck import Violation, has_errors, validate_config
from aivis.configio import load_frozen_config
from aivis.contracts.config import Brand, FrozenConfig, Question

ROOT = Path(__file__).resolve().parents[1]


def _cfg(
    questions: list[Question],
    peers: list[Brand],
    *,
    focal_domain: str | None = None,
) -> FrozenConfig:
    return FrozenConfig(
        version="t",
        prompt_version="t",
        competitor_set_version="t",
        focal_domain=focal_domain,
        peers=tuple(peers),
        competitors_on=False,
        questions=tuple(questions),
        tier="standard",
        locale="en-US",
        region="US",
        temperature=0.0,
        n_runs=1,
        max_uses=3,
        budget_cap_usd=1.0,
    )


def _rules(violations: list[Violation]) -> set[str]:
    return {v.rule for v in violations}


# ---- 真实配置:demo 全绿,smoke 仅配比告警 ----


def test_demo_config_is_clean() -> None:
    cfg = load_frozen_config(ROOT / "configs" / "demo")
    violations = validate_config(cfg, phase="final")
    assert violations == [], f"demo 配置不应有任何违规,实际: {violations}"


def test_smoke_config_only_warns_on_recipe() -> None:
    cfg = load_frozen_config(ROOT / "configs" / "smoke")
    violations = validate_config(cfg, phase="final")
    assert not has_errors(violations)
    assert any(v.rule == "recipe-8-6-6-4" and v.severity == "warn" for v in violations)


def test_too_many_brands_warns_not_blocks() -> None:
    """品牌数 >5(含焦点)→ 告警(可读性),不拦;引擎不设硬 cap。"""
    q = [Question(id="d01", intent="discovery", text="best widgets?")]
    six = [Brand(name=f"Brand{i}", domains=(f"b{i}.com",)) for i in range(6)]
    v6 = validate_config(_cfg(q, six), phase="final")
    assert "too-many-brands" in _rules(v6)
    assert not has_errors(v6)  # 仅告警
    five = [Brand(name=f"Brand{i}", domains=(f"b{i}.com",)) for i in range(5)]
    assert "too-many-brands" not in _rules(validate_config(_cfg(q, five), phase="final"))


# ---- 反偏置:中立题点名品牌 = 硬错 ----


def test_brand_name_in_neutral_question_is_error() -> None:
    cfg = _cfg(
        [Question(id="q1", intent="discovery", text="Which Osprey pack is best?")],
        [Brand(name="Osprey", domains=("osprey.com",))],
    )
    violations = validate_config(cfg, phase="final")
    assert any(v.rule == "anti-bias" and v.severity == "error" for v in violations)


def test_alias_in_neutral_question_is_error() -> None:
    cfg = _cfg(
        [Question(id="q1", intent="comparison", text="How do AlphaPacks compare?")],
        [Brand(name="Alpha", aliases=("AlphaPacks",), domains=("alpha.com",))],
    )
    assert any(v.rule == "anti-bias" for v in validate_config(cfg))


# ---- 配比:draft 硬错 / final 告警 ----


def test_recipe_severity_depends_on_phase() -> None:
    cfg = _cfg(
        [Question(id="q1", intent="discovery", text="Best packs around?")],
        [Brand(name="X", domains=("x.com",))],
    )
    draft = validate_config(cfg, phase="draft")
    final = validate_config(cfg, phase="final")
    assert any(v.rule == "recipe-8-6-6-4" and v.severity == "error" for v in draft)
    assert any(v.rule == "recipe-8-6-6-4" and v.severity == "warn" for v in final)


# ---- focal 模式不变量 ----


def test_focal_mode_requires_exactly_one_focal() -> None:
    cfg = _cfg(
        [Question(id="q1", intent="discovery", text="Best packs around?")],
        [Brand(name="Acme", domains=("acme.com",))],  # 没标 is_focal
        focal_domain="acme.com",
    )
    assert "focal-count" in _rules(validate_config(cfg))


def test_focal_defense_must_name_focal() -> None:
    cfg = _cfg(
        [Question(id="d1", intent="brand_defense", text="Is this brand any good?")],
        [Brand(name="Acme", domains=("acme.com",), is_focal=True)],
        focal_domain="acme.com",
    )
    assert any(v.rule == "defense-focal" and v.severity == "error" for v in validate_config(cfg))


def test_focal_defense_naming_competitor_is_error() -> None:
    cfg = _cfg(
        [Question(id="d1", intent="brand_defense", text="Is Acme better than Rival?")],
        [
            Brand(name="Acme", domains=("acme.com",), is_focal=True),
            Brand(name="Rival", domains=("rival.com",)),
        ],
        focal_domain="acme.com",
    )
    assert any(v.rule == "defense-competitor" for v in validate_config(cfg))


# ---- peer 模式不变量 ----


def test_peer_mode_forbids_focal_flag() -> None:
    cfg = _cfg(
        [Question(id="q1", intent="discovery", text="Best packs around?")],
        [Brand(name="X", domains=("x.com",), is_focal=True)],
        focal_domain=None,
    )
    assert any(v.rule == "peer-no-focal" and v.severity == "error" for v in validate_config(cfg))
