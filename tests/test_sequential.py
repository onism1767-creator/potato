"""序贯采样停止判据(stats/sequential)单测:纯函数、确定性。"""

from aivis.stats.sequential import is_stable, should_ask_more


def fs(*names: str) -> frozenset[str]:
    return frozenset(names)


def test_empty_is_not_stable() -> None:
    assert is_stable([]) is False


def test_all_same_is_stable() -> None:
    assert is_stable([fs("A", "B"), fs("A", "B"), fs("A", "B")]) is True


def test_all_empty_is_stable() -> None:
    # 各次都没提及任何品牌 = 一致
    assert is_stable([fs(), fs(), fs()]) is True


def test_inconsistent_brand_is_not_stable() -> None:
    # B 时有时无 → 不稳
    assert is_stable([fs("A", "B"), fs("A")]) is False


def test_single_run_is_stable_shape() -> None:
    assert is_stable([fs("A")]) is True


def test_should_ask_more_below_min() -> None:
    # 未达 min_runs 必继续问(即便当前看着一致)
    assert should_ask_more([fs("A")], min_runs=2, max_runs=6) is True


def test_should_ask_more_stops_when_stable() -> None:
    # 达 min 且一致 → 停
    assert should_ask_more([fs("A"), fs("A")], min_runs=2, max_runs=6) is False


def test_should_ask_more_continues_when_unstable() -> None:
    assert should_ask_more([fs("A", "B"), fs("A")], min_runs=2, max_runs=6) is True


def test_should_ask_more_caps_at_max() -> None:
    # 即便仍不稳,达 max_runs 必停
    runs = [fs("A", "B"), fs("A"), fs("A", "B"), fs("A"), fs("A", "B"), fs("A")]
    assert should_ask_more(runs, min_runs=2, max_runs=6) is False
