"""bootstrap_mean_interval 单测:纯函数、确定性、小样本不假精度。"""

from aivis.stats.bootstrap import bootstrap_mean_interval


def test_empty_is_insufficient() -> None:
    iv = bootstrap_mean_interval([])
    assert iv.n == 0 and iv.method == "insufficient" and (iv.low, iv.high) == (0.0, 1.0)


def test_single_value_degenerate() -> None:
    iv = bootstrap_mean_interval([0.7])
    assert iv.n == 1 and iv.method == "single"
    assert iv.low == iv.point == iv.high == 0.7  # 不假装区间


def test_point_is_mean_and_interval_brackets_it() -> None:
    iv = bootstrap_mean_interval([0.2, 0.4, 0.6, 0.8])
    assert iv.point == 0.5 and iv.method == "bootstrap"
    assert iv.low <= iv.point <= iv.high


def test_deterministic_same_input_same_interval() -> None:
    a = bootstrap_mean_interval([0.1, 0.5, 0.9, 0.3, 0.7])
    b = bootstrap_mean_interval([0.1, 0.5, 0.9, 0.3, 0.7])
    assert (a.low, a.point, a.high) == (b.low, b.point, b.high)  # 固定种子 → 可重生黄金


def test_identical_values_zero_width() -> None:
    iv = bootstrap_mean_interval([0.5, 0.5, 0.5])
    assert iv.low == iv.high == iv.point == 0.5  # 无方差 → 零宽


def test_more_spread_wider_interval() -> None:
    tight = bootstrap_mean_interval([0.49, 0.5, 0.51])
    wide = bootstrap_mean_interval([0.0, 0.5, 1.0])
    assert (wide.high - wide.low) > (tight.high - tight.low)
