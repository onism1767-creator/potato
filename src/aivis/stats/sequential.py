"""sequential —— 纯函数:序贯采样停止判据(run_until_stable)。

口径:每问一次记录"这次回答提及了哪些品牌"。当所有品牌的"提及与否"在已问的各次间
完全一致(每个出现过的品牌都在每一次里出现),即认为该题已稳定、可停止加问;只要有品牌
忽提及忽不提及,就继续问,直到达上限 max_runs。零 I/O、零网络、零依赖。

为什么用"提及一致性"而非区间宽度:每题样本量很小(2~6),比例的置信区间总是很宽、几乎
永不收敛,用宽度阈值会退化成"总是问到上限";一致性判据直接对应"答案稳不稳"且确定性可测。
"""

from collections.abc import Sequence


def is_stable(mention_runs: Sequence[frozenset[str]]) -> bool:
    """已问各次的提及集合是否一致:每个出现过的品牌都在每一次出现(并集==交集)。

    空序列视为未稳定(还没问);各次都没提及任何品牌也算一致(并集==交集==空)。
    """
    if not mention_runs:
        return False
    union: set[str] = set().union(*mention_runs)
    intersection: set[str] = set(mention_runs[0]).intersection(*mention_runs)
    return union == intersection


def should_ask_more(
    mention_runs: Sequence[frozenset[str]], *, min_runs: int, max_runs: int
) -> bool:
    """是否还要再问一次:未达 min 必问;达 max 必停;之间则"未稳定就继续"。"""
    n = len(mention_runs)
    if n >= max_runs:
        return False
    if n < min_runs:
        return True
    return not is_stable(mention_runs)
