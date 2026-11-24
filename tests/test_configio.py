"""configio:配置文件读不进来时,报错要点名是哪一项、为什么(不吐 pydantic 的英文原文)。"""

import shutil
from pathlib import Path

import pytest

from aivis.configio import load_frozen_config

ROOT = Path(__file__).resolve().parent.parent


def _demo_copy(tmp_path: Path, filename: str, old: str, new: str) -> Path:
    """把 demo 配置抄一份到临时目录,并把其中一个文件里的 old 换成 new(只换第一处)。"""
    target = tmp_path / "config"
    shutil.copytree(ROOT / "configs" / "demo", target)
    path = target / filename
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


def _error(config_dir: Path) -> str:
    with pytest.raises(ValueError) as exc:
        load_frozen_config(config_dir)
    message = str(exc.value)
    assert "validation error" not in message and "pydantic" not in message  # 不吐库的原文
    return message


def test_misspelled_key_is_named_with_the_closest_valid_key(tmp_path: Path) -> None:
    message = _error(_demo_copy(tmp_path, "questions.yml", "max_uses: 3", "max_use: 3"))
    assert "max_use:未知的配置项" in message and "是不是想写 max_uses" in message


def test_misspelled_nested_key_says_where(tmp_path: Path) -> None:
    message = _error(_demo_copy(tmp_path, "brands.yml", "    domains:", "    domain:"))
    assert "peers[第 1 项].domain:未知的配置项" in message and "是不是想写 domains" in message
    assert "peers[第 1 项].domains:缺少必填项" in message  # 拼错的那一项同时也就是"没填"


def test_duplicate_question_id_is_explained(tmp_path: Path) -> None:
    message = _error(_demo_copy(tmp_path, "questions.yml", 'id: "d02"', 'id: "d01"'))
    assert "题目 id 重复" in message and "d01" in message


def test_duplicate_brand_name_is_explained(tmp_path: Path) -> None:
    message = _error(_demo_copy(tmp_path, "brands.yml", 'name: "Gregory"', 'name: "osprey"'))
    assert "品牌名重复" in message and "osprey" in message


def test_unknown_key_with_no_close_match_gets_no_made_up_suggestion(tmp_path: Path) -> None:
    message = _error(_demo_copy(tmp_path, "questions.yml", "max_uses: 3", "zzzz: 3"))
    assert "zzzz:未知的配置项" in message and "是不是想写" not in message


def test_shipped_configs_still_load() -> None:
    for name in ("demo", "smoke"):
        assert load_frozen_config(ROOT / "configs" / name).questions
