"""scaffold 单元测试:aivis init 生成的焦点模式骨架开箱即合规。

核心断言:生成物能 load_frozen_config + validate --phase final 0 硬错;
配比 8/6/6/4;防御题点名焦点、中立题不含焦点名;焦点品牌标好;拒绝覆盖。
"""

from collections import Counter
from pathlib import Path

import pytest

from aivis.configcheck import has_errors, validate_config
from aivis.configio import load_frozen_config
from aivis.scaffold import build_scaffold, write_scaffold


def _written(tmp_path: Path) -> Path:
    files = build_scaffold(
        name="acme",
        focal_name="Acme Corp",
        focal_domain="acme.com",
        category="CRM software",
        date_tag="2026.06",
    )
    target = tmp_path / "acme"
    write_scaffold(target, files)
    return target


def test_scaffold_loads_and_validates_clean(tmp_path: Path) -> None:
    cfg = load_frozen_config(_written(tmp_path))
    violations = validate_config(cfg, phase="final")
    assert not has_errors(violations), f"骨架不应有硬错: {violations}"


def test_scaffold_recipe_8_6_6_4(tmp_path: Path) -> None:
    cfg = load_frozen_config(_written(tmp_path))
    c = Counter(q.intent for q in cfg.questions)
    assert (c["discovery"], c["comparison"], c["local"], c["brand_defense"]) == (8, 6, 6, 4)


def test_defense_questions_name_focal(tmp_path: Path) -> None:
    cfg = load_frozen_config(_written(tmp_path))
    defense = [q for q in cfg.questions if q.intent == "brand_defense"]
    assert defense and all("Acme Corp" in q.text for q in defense)


def test_neutral_questions_omit_focal_name(tmp_path: Path) -> None:
    cfg = load_frozen_config(_written(tmp_path))
    neutral = [q for q in cfg.questions if q.intent != "brand_defense"]
    assert all("Acme Corp" not in q.text for q in neutral)


def test_focal_brand_marked(tmp_path: Path) -> None:
    cfg = load_frozen_config(_written(tmp_path))
    focal = [b for b in cfg.peers if b.is_focal]
    assert len(focal) == 1
    assert cfg.focal_domain == "acme.com"
    assert "acme.com" in focal[0].domains


def test_write_refuses_existing_dir(tmp_path: Path) -> None:
    files = build_scaffold(
        name="acme",
        focal_name="Acme Corp",
        focal_domain="acme.com",
        category=None,
        date_tag="2026.06",
    )
    (tmp_path / "acme").mkdir()
    with pytest.raises(FileExistsError):
        write_scaffold(tmp_path / "acme", files)


def test_category_containing_focal_name_stays_clean(tmp_path: Path) -> None:
    # 回归(压力测试发现):品类描述里带了焦点名,中立占位也不该点名品牌
    files = build_scaffold(
        name="acme",
        focal_name="Acme",
        focal_domain="acme.com",
        category="Acme-style CRM",
        date_tag="2026.06",
    )
    target = tmp_path / "acme"
    write_scaffold(target, files)
    cfg = load_frozen_config(target)
    assert not has_errors(validate_config(cfg, phase="final"))


def test_focal_name_colliding_with_placeholder_word_stays_clean(tmp_path: Path) -> None:
    # 回归(压力测试发现):焦点名恰为占位里的词(如 "Discovery")也不该误触反偏置
    files = build_scaffold(
        name="disc",
        focal_name="Discovery",
        focal_domain="disc.com",
        category=None,
        date_tag="2026.06",
    )
    target = tmp_path / "disc"
    write_scaffold(target, files)
    cfg = load_frozen_config(target)
    assert not has_errors(validate_config(cfg, phase="final"))


def test_no_category_still_valid(tmp_path: Path) -> None:
    files = build_scaffold(
        name="acme",
        focal_name="Acme Corp",
        focal_domain="acme.com",
        category=None,
        date_tag="2026.06",
    )
    target = tmp_path / "acme"
    write_scaffold(target, files)
    cfg = load_frozen_config(target)
    assert not has_errors(validate_config(cfg, phase="final"))
