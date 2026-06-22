"""matching —— 纯函数:品牌名/别名的确定性匹配(🟥 单一口径,extract/verify/configcheck 共用)。

历史上 extract 用 `[A-Za-z0-9]` 自定义词边界、configcheck 用 `\\w`,两者不一致 → 可能
"质检放过但计分命中"或反之。本模块把口径收敛为唯一来源:
  - 自定义词边界 `[A-Za-z0-9]`:品牌名可含空格/连字符(如 "Co-op"、"REI Co-op"),`\\b` 不可靠;
    下划线/CJK 等非 ASCII 视为边界(无空格语言里=可在任意位置命中,正确行为)。
  - 大小写不敏感。
零 I/O、零网络、零依赖(只用 re)。
"""

import re
from collections.abc import Iterable


def name_pattern(name: str) -> re.Pattern[str]:
    """品牌名的确定性匹配模式(唯一口径)。"""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)


def find_positions(text: str, names: Iterable[str]) -> list[tuple[int, str]]:
    """返回 (起始位置, 命中名) 列表,按位置排序;确定性。空名跳过。"""
    hits: list[tuple[int, str]] = []
    for name in names:
        if not name:
            continue
        hits.extend((m.start(), name) for m in name_pattern(name).finditer(text))
    return sorted(hits)


def mentions_surfaces(text: str, surfaces: Iterable[str]) -> list[str]:
    """text 中以唯一口径命中的 surface 列表(去重保序);供 configcheck 反偏置判定。"""
    seen: list[str] = []
    for surface in surfaces:
        if surface and surface not in seen and name_pattern(surface).search(text):
            seen.append(surface)
    return seen
