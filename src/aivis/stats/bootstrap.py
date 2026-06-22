"""bootstrap —— 纯函数:对均值做自助重采样的不确定区间(给非比例类数字配范围)。

比例类指标用 Wilson(wilson.py);均值类(如位置显著度 prominence,连续值的平均)解析区间
不好给,用 bootstrap:把观测值有放回重采样 N 次、每次重算均值,取百分位作区间。

🟥 确定性(CLAUDE §1.2:纯层禁未播种随机):固定种子 → 同输入永远同区间(黄金可重生)。
小样本不假装精度:n=0 → insufficient([0,1]);n=1 → 退化为点(low=high=point)。
"""

import random
from collections.abc import Sequence

from aivis.contracts.records import Interval

_DEFAULT_RESAMPLES = 2000
_DEFAULT_SEED = 1729  # 固定种子:纯层确定性
_DEFAULT_CI = 0.95


def _percentile(sorted_vals: list[float], q: float) -> float:
    """已排序序列的分位数(线性插值);q∈[0,1]。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])


def bootstrap_mean_interval(
    values: Sequence[float],
    *,
    n_resamples: int = _DEFAULT_RESAMPLES,
    seed: int = _DEFAULT_SEED,
    ci: float = _DEFAULT_CI,
) -> Interval:
    """values 的均值的 bootstrap 百分位区间(确定性,method='bootstrap')。"""
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return Interval(point=0.0, low=0.0, high=1.0, n=0, method="insufficient")
    point = sum(vals) / n
    if n == 1:
        return Interval(point=point, low=point, high=point, n=1, method="single")
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += vals[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo_q = (1.0 - ci) / 2.0
    return Interval(
        point=point,
        low=_percentile(means, lo_q),
        high=_percentile(means, 1.0 - lo_q),
        n=n,
        method="bootstrap",
    )
