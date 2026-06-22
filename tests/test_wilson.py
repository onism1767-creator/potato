"""Wilson 区间单元测试(对照公开参考值)。"""

import pytest

from aivis.stats.wilson import wilson_interval


def test_known_value_5_of_10() -> None:
    # 参考值:p=0.5, n=10, z=1.96 → 约 [0.2366, 0.7634]
    interval = wilson_interval(5, 10)
    assert interval.point == pytest.approx(0.5)
    assert interval.low == pytest.approx(0.2366, abs=1e-3)
    assert interval.high == pytest.approx(0.7634, abs=1e-3)
    assert interval.method == "wilson"
    assert interval.n == 10


def test_zero_successes_lower_bound_is_zero() -> None:
    interval = wilson_interval(0, 20)
    assert interval.point == 0.0
    assert interval.low == 0.0
    assert 0.0 < interval.high < 0.2


def test_all_successes_upper_bound_is_one() -> None:
    interval = wilson_interval(20, 20)
    assert interval.point == 1.0
    assert interval.high == 1.0
    assert 0.8 < interval.low < 1.0


def test_n_zero_returns_insufficient_full_interval() -> None:
    """n=0 → [0,1] 完全不确定,不给假精确(CLAUDE.md §7)。"""
    interval = wilson_interval(0, 0)
    assert (interval.low, interval.high) == (0.0, 1.0)
    assert interval.method == "insufficient"


def test_interval_narrows_with_more_samples() -> None:
    narrow = wilson_interval(50, 100)
    wide = wilson_interval(5, 10)
    assert (narrow.high - narrow.low) < (wide.high - wide.low)


def test_invalid_counts_raise() -> None:
    with pytest.raises(ValueError):
        wilson_interval(5, 3)
    with pytest.raises(ValueError):
        wilson_interval(-1, 3)
