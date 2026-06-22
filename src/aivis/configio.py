"""configio —— I/O 边缘的小模块:questions.yml + brands.yml → FrozenConfig。

只做读取与组装;形状校验全部由 contracts(pydantic)完成,这里不写业务规则。
"""

from pathlib import Path
from typing import Any

import yaml

from aivis.contracts.config import FrozenConfig

QUESTIONS_FILE = "questions.yml"
BRANDS_FILE = "brands.yml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射: {path}")
    return data


def load_frozen_config(config_dir: Path) -> FrozenConfig:
    """从配置目录加载并冻结配置(pydantic 校验形状,非法配置当场报错)。"""
    merged = _load_yaml(config_dir / QUESTIONS_FILE) | _load_yaml(config_dir / BRANDS_FILE)
    return FrozenConfig.model_validate(merged)
