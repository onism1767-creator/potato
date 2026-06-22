"""stats —— 全纯统计函数(禁网络/磁盘/时钟,由 import-linter 保证)。

v1 只有 wilson;bootstrap(位置区间)与 sequential(run_until_stable)是后期项。
"""

from aivis.stats.wilson import wilson_interval

__all__ = ["wilson_interval"]
