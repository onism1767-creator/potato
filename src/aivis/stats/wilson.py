"""Wilson score 区间(纯函数)。比例类指标的不确定区间一律用它(计划书 6.7.5)。"""

import math

from aivis.contracts.records import Interval

_Z_95 = 1.959963984540054  # 95% 双侧正态分位数


def wilson_interval(successes: int, n: int, z: float = _Z_95) -> Interval:
    """successes/n 的 Wilson score 区间。

    n=0 时返回完全不确定区间 [0, 1],method="insufficient"(不给假精确)。
    """
    if successes < 0 or n < 0 or successes > n:
        raise ValueError(f"非法计数: successes={successes}, n={n}")
    if n == 0:
        return Interval(point=0.0, low=0.0, high=1.0, n=0, method="insufficient")
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denom
    return Interval(
        point=p,
        low=max(0.0, center - half),
        high=min(1.0, center + half),
        n=n,
        method="wilson",
    )
