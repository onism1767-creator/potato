"""configio —— I/O 边缘的小模块:questions.yml + brands.yml → FrozenConfig。

只做读取与组装;形状校验全部由 contracts(pydantic)完成,这里不写业务规则。
校验不过时,explain_config_error 把库的英文原文翻成"哪一项:为什么"(规则仍在契约里,这里只管说清楚)。
"""

import difflib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from aivis.contracts.config import Brand, FrozenConfig, Question, VerifyThresholds

QUESTIONS_FILE = "questions.yml"
BRANDS_FILE = "brands.yml"

# 契约里全部合法的键名(从类型本身取,不另抄一份);只用来给拼错的键名找"最像的那个"。
_KNOWN_KEYS = sorted(
    {
        key
        for model in (FrozenConfig, Brand, Question, VerifyThresholds)
        for key in model.model_fields
    }
)


def _where(loc: tuple[int | str, ...]) -> str:
    """错误位置 → 人读得懂的路径,如 peers[第 1 项].domains(顶层的整体性问题返回空串)。"""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[第 {part + 1} 项]"
        else:
            out += ("." if out else "") + part
    return out


def explain_config_error(err: ValidationError) -> list[str]:
    """把契约的校验错误翻成逐条的"哪一项:为什么";键名拼错时附上最接近的合法键名(有才附)。"""
    lines: list[str] = []
    for item in err.errors():
        where = _where(item["loc"])
        if item["type"] == "extra_forbidden":
            key = str(item["loc"][-1])
            close = difflib.get_close_matches(key, _KNOWN_KEYS, n=1)
            hint = f"(是不是想写 {close[0]}?)" if close and close[0] != key else ""
            why = f"未知的配置项{hint}"
        elif item["type"] == "missing":
            why = "缺少必填项"
        else:
            why = str(item["msg"]).removeprefix("Value error, ")
        lines.append(f"{where}:{why}" if where else why)
    return lines


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射: {path}")
    return data


def load_frozen_config(config_dir: Path) -> FrozenConfig:
    """从配置目录加载并冻结配置(契约校验形状,非法配置当场报错:ValueError,逐条写明哪一项、为什么)。"""
    merged = _load_yaml(config_dir / QUESTIONS_FILE) | _load_yaml(config_dir / BRANDS_FILE)
    try:
        return FrozenConfig.model_validate(merged)
    except ValidationError as e:
        problems = explain_config_error(e)
        listing = "\n".join(f"  - {p}" for p in problems)
        raise ValueError(f"配置有 {len(problems)} 处问题:\n{listing}") from e
