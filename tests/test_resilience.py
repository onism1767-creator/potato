"""终审(2026-11)查出的"花了钱但数据丢了 / 一条坏数据卡死全盘"类问题的回归测试。

覆盖:探测被打断时在途调用的抢救、序贯采样不把失败当观测、被闸门拦下不留锁、
原始层/证据层半截行、畸形被引 URL、配置域名清洗。全部离线、零成本。
"""

import threading
import time
import warnings
from pathlib import Path

import pytest

from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.contracts.records import Citation, RawResponse
from aivis.pipeline.extract import canonical_domain, extract, normalize_url, owner_of_domain
from aivis.pipeline.probe import CostGateError, run_probe
from aivis.providers.mock import MockAdapter
from aivis.storage.evidence import EvidenceStore
from aivis.storage.raw import RawStore
from tests.conftest import FIXED_NOW, make_response

# ---------------------------------------------------------------- 探测被打断:在途调用不许丢


class _SlowCounting(MockAdapter):
    """每次调用睡一小会儿(保证真的同时在途)并数调用次数。"""

    def __init__(self) -> None:
        super().__init__()
        self._guard = threading.Lock()
        self.calls = 0

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        with self._guard:
            self.calls += 1
        time.sleep(0.05)
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


def test_interrupt_keeps_every_call_already_sent(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """主线程被打断(这里用进度回调里的 Ctrl+C 模拟):已发出的调用钱已花,必须全部落盘。"""
    adapter = _SlowCounting()

    def interrupt_after_first(done: int, _total: int, _spent: float) -> None:
        if done >= 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_probe(
            adapter,
            demo_cfg,
            RawStore(tmp_path),
            run_id="r",
            now=lambda: FIXED_NOW,
            progress=interrupt_after_first,
            concurrency=3,
        )
    stored = RawStore(tmp_path).read_responses("r")
    assert adapter.calls >= 2  # 确实有调用同时在途
    assert len(stored) == adapter.calls  # 发出几次就存了几次:续跑不会为它们再付一次钱

    resume = _SlowCounting()
    run_probe(resume, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert resume.calls == 6 - len(stored)  # 3 题 × 2 轮,只补没问过的


# ---------------------------------------------------------------- 序贯采样:失败不是观测


class _ScriptedAdapter(MockAdapter):
    """按 run_index 决定这次调用成功(提到 Alpha)还是失败(空回答 + finished_ok=False)。"""

    def __init__(self, fail_on: set[int]) -> None:
        super().__init__()
        self._fail_on = fail_on
        self.calls = 0

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        self.calls += 1
        failed = run_index in self._fail_on
        return make_response(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text="" if failed else "Alpha Packs is a great pick",
            finished_ok=not failed,
            error_code="timeout" if failed else None,
        )


def _seq_cfg() -> FrozenConfig:
    return FrozenConfig(
        version="seq-v1",
        prompt_version="seq-q-v1",
        competitor_set_version="seq-b-v1",
        peers=(
            Brand(name="Alpha Packs", domains=("alphapacks.example",)),
            Brand(name="Bravo Gear", domains=("bravogear.example",)),
        ),
        questions=(Question(id="q1", intent="discovery", text="Best packs?"),),
        tier="standard",
        locale="en-US",
        region="US",
        temperature=0.0,
        n_runs=6,
        sequential=True,
        min_runs=2,
        max_uses=3,
        budget_cap_usd=999.0,
    )


def test_sequential_does_not_call_all_failures_stable(tmp_path: Path) -> None:
    """每次都失败 = 一条有效观测都没有:不能在 min_runs 就判"稳定"收工,要问到上限为止。"""
    adapter = _ScriptedAdapter(fail_on={0, 1, 2, 3, 4, 5})
    run_probe(adapter, _seq_cfg(), RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert adapter.calls == 6  # 修复前是 2:把两次失败当成"两次都没提到任何品牌"


def test_sequential_one_transient_failure_does_not_force_max(tmp_path: Path) -> None:
    """成功的回答完全一致、只有第 2 次瞬时失败:再问一次凑够两条成功观测就该停。"""
    adapter = _ScriptedAdapter(fail_on={1})
    run_probe(adapter, _seq_cfg(), RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert adapter.calls == 3  # 修复前是 6:失败的空回答让它永远"不稳"


def test_sequential_resume_ignores_stored_failures(tmp_path: Path) -> None:
    """续跑重建提及序列时同样只看成功的回答。"""
    store = RawStore(tmp_path)
    cfg = _seq_cfg()
    first = _ScriptedAdapter(fail_on={0, 1, 2, 3, 4, 5})
    run_probe(first, cfg, store, run_id="r", now=lambda: FIXED_NOW)
    assert len(RawStore(tmp_path).read_responses("r")) == 6  # 已到上限,续跑零调用
    again = _ScriptedAdapter(fail_on=set())
    run_probe(again, cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert again.calls == 0


# ---------------------------------------------------------------- 被闸门拦下不留锁


class _Pricy(MockAdapter):
    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return 99.0


def test_run_blocked_by_cost_gate_leaves_no_lock(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """开跑前就被闸门拦下的 run_id 一次调用都没发:不该被锁占住,换个配置还能用同一个 run_id。"""
    with pytest.raises(CostGateError):
        run_probe(_Pricy(), demo_cfg, RawStore(tmp_path), run_id="r")
    assert RawStore(tmp_path).read_lock("r") is None
    smaller = FrozenConfig.model_validate(demo_cfg.model_dump() | {"n_runs": 1})
    run_probe(MockAdapter(), smaller, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert len(RawStore(tmp_path).read_responses("r")) == 3


# ---------------------------------------------------------------- 半截行(写到一半被杀)


def test_raw_store_survives_a_torn_last_line(tmp_path: Path) -> None:
    """进程在写一行时被杀会留下半截末行:不能让它把整个数据目录(含别的 run)锁死。"""
    store = RawStore(tmp_path)
    store.append_response(make_response(run_index=0))
    path = tmp_path / "raw_responses.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write('{"run_id": "run-test", "question_id": "q1", "run_ind')  # 没写完、没换行

    with pytest.warns(RuntimeWarning, match="已跳过"):
        reopened = RawStore(tmp_path)
        assert [r.run_index for r in reopened.read_responses()] == [0]  # 完整的那条还在

    # 之后再追加:新记录不能被粘在半截行后面一起报废
    assert reopened.append_response(make_response(run_index=1)) is True
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        assert [r.run_index for r in RawStore(tmp_path).read_responses()] == [0, 1]
    # 只追加:原有内容(含那半截残行)一个字节都没被改写
    assert path.read_text(encoding="utf-8").count('"run_ind\n') == 1


def test_evidence_store_survives_a_torn_last_line(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    (tmp_path / "evidence.jsonl").write_text('{"citation_url": "https://x.exa', encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="已跳过"):
        assert store.load() == {}


# ---------------------------------------------------------------- 畸形被引 URL(不可信数据)


@pytest.mark.parametrize("bad", ["http://[foo]/x", "http://[::1/x"])
def test_malformed_cited_url_does_not_crash_extract(bad: str, demo_cfg: FrozenConfig) -> None:
    """引擎给的引用 URL 不可信:一条畸形链接不能让已付费的整轮出不了报告。"""
    assert normalize_url(bad) == bad  # 解析失败原样返回(docstring 的承诺)
    assert canonical_domain(bad) == ""
    resp = make_response(
        answer_text="Alpha Packs is great.",
        citations=(Citation(url=bad, title="Alpha Packs review", position=0),),
    )
    result = extract([resp], demo_cfg)
    assert len(result.citations) == 1
    assert result.citations[0].owned_or_earned != "owned"  # 归不到任何品牌的自有域


# ---------------------------------------------------------------- 配置里的域名清洗


@pytest.mark.parametrize(
    "configured",
    [
        "acme.com",
        "https://www.acme.com/",
        "HTTP://ACME.com/shop?x=1",
        " acme.com/ ",
        "www.acme.com",
    ],
)
def test_owned_domain_tolerates_pasted_urls(configured: str) -> None:
    """用户把整条网址贴进域名框很常见:清洗后照样认得自家官网(否则 owned 会被错算成 earned)。"""
    brand = Brand(name="Acme", domains=(configured,))
    assert owner_of_domain("acme.com", [brand]) is brand
    assert owner_of_domain("shop.acme.com", [brand]) is brand
    assert owner_of_domain("notacme.com", [brand]) is None  # 仿冒域名仍不认


def test_blank_domain_owns_nothing() -> None:
    brand = Brand(name="Acme", domains=("",))
    assert owner_of_domain("", [brand]) is None  # 无法解析的 URL 不能被判给"空域名"的品牌
    assert owner_of_domain("anything.example", [brand]) is None
