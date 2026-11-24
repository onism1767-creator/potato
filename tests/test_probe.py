"""probe 成本闸门测试(CLAUDE.md §6:mock 零成本静默,真钱必须确认/封顶)。"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.contracts.records import RawResponse
from aivis.pipeline.probe import BudgetStop, CostGateError, RunLockError, run_probe
from aivis.providers.mock import MockAdapter
from aivis.storage.raw import RawStore

FIXED_NOW = "2026-06-10T00:00:00Z"


class _PricyAdapter(MockAdapter):
    """测试用:行为同 mock,但谎称要花钱(只用于演练闸门)。"""

    def __init__(self, cost: float) -> None:
        super().__init__()
        self._cost = cost

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return self._cost


class _RealCostAdapter(MockAdapter):
    """估算说免费、但每次真花 $0.50:模拟"估算低估 → 运行时按真实累计硬封顶"。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return 0.0

    def actual_cost_usd(self, response: RawResponse) -> float:
        return 0.50

    def ask(self, question, cfg, *, run_id, run_index):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


class _CountingAdapter(MockAdapter):
    """统计真实调用次数(验证续跑不重复花钱;一次调用 = 一次潜在付费)。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def ask(self, question, cfg, *, run_id, run_index):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


class _CrashAfterAdapter(MockAdapter):
    """成功 N 次后崩溃,模拟"同配置中途崩"(用于诚实的续跑测试,不靠换配置)。"""

    def __init__(self, crash_after: int) -> None:
        super().__init__()
        self.calls = 0
        self._crash_after = crash_after

    def ask(self, question, cfg, *, run_id, run_index):  # type: ignore[no-untyped-def]
        if self.calls >= self._crash_after:
            raise RuntimeError("simulated mid-run crash")
        self.calls += 1
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


def test_mock_runs_without_confirmation(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    manifest = run_probe(
        MockAdapter(), demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW
    )
    assert manifest.search_count is not None and manifest.search_count > 0
    assert manifest.config_fingerprint == demo_cfg.fingerprint()


def test_over_budget_is_blocked(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    with pytest.raises(CostGateError, match="预算上限"):
        run_probe(_PricyAdapter(99.0), demo_cfg, RawStore(tmp_path), run_id="r")


def test_paid_run_requires_confirmation(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    with pytest.raises(CostGateError, match="人工确认"):
        run_probe(_PricyAdapter(0.5), demo_cfg, RawStore(tmp_path), run_id="r")


def test_runtime_budget_cap_aborts_and_preserves_partial(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    """B:估算放行(谎称 0),但运行中真实累计花费达 budget_cap 即阻断,已完成的保留。"""
    # demo_cfg: budget_cap=1.0,3 题 × 2 轮 = 6 次;每次真花 0.5 → 第 2 次后达 1.0 阻断
    store = RawStore(tmp_path)
    with pytest.raises(BudgetStop, match="预算上限"):
        run_probe(_RealCostAdapter(), demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)
    assert len(RawStore(tmp_path).read_responses("r")) == 2  # 已完成的原始数据保留(可续跑)


def test_resume_over_budget_makes_zero_new_paid_calls(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    """续跑一个已超预算的 run:在发起调用前就阻断,零新付费调用(不多花一次)。"""
    store = RawStore(tmp_path)
    with pytest.raises(CostGateError):
        run_probe(_RealCostAdapter(), demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)

    resume = _RealCostAdapter()  # 同 cap=1.0,prior 已花满 → 应零调用
    with pytest.raises(CostGateError):
        run_probe(resume, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert resume.calls == 0


def test_paid_run_with_confirmation_passes(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    seen: list[tuple[float, int]] = []

    def confirm(cost: float, n_calls: int) -> bool:
        seen.append((cost, n_calls))
        return True

    run_probe(
        _PricyAdapter(0.5),
        demo_cfg,
        RawStore(tmp_path),
        run_id="r",
        now=lambda: FIXED_NOW,
        confirm_cost=confirm,
    )
    assert seen == [(0.5, len(demo_cfg.questions) * demo_cfg.n_runs)]


def test_resume_makes_zero_provider_calls(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """🟥 F-002:同 run_id 续跑,已完成的探测**不再调 provider**(省钱,不只是去重存储)。"""
    store = RawStore(tmp_path)
    n_total = len(demo_cfg.questions) * demo_cfg.n_runs

    first = _CountingAdapter()
    run_probe(first, demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)
    assert first.calls == n_total  # 首次:全量调用

    second = _CountingAdapter()
    run_probe(second, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert second.calls == 0  # 续跑:零调用(全部已存在)
    assert len(RawStore(tmp_path).read_responses("r")) == n_total  # 存储未变多


def test_partial_resume_only_calls_missing(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """部分续跑:**同配置**中途崩溃 → 续跑只补缺失的那几次调用(不靠换配置模拟)。"""
    store = RawStore(tmp_path)
    # 同一 demo_cfg 跑,但成功 1 次后崩 → 只存了 1 条
    crashing = _CrashAfterAdapter(crash_after=1)
    with pytest.raises(RuntimeError, match="crash"):
        run_probe(crashing, demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)
    assert len(RawStore(tmp_path).read_responses("r")) == 1

    # 用**相同**配置续跑同 run_id(配置锁通过):应只补 (全量 - 1) 次
    resume = _CountingAdapter()
    run_probe(resume, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert resume.calls == len(demo_cfg.questions) * demo_cfg.n_runs - 1


def test_resume_with_changed_config_rejected(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """🟥 配置锁:续跑同 run_id 但配置变了 → 开跑前报错,绝不混入同一基线。"""
    store = RawStore(tmp_path)
    run_probe(MockAdapter(), demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)
    changed = demo_cfg.model_copy(update={"prompt_version": "tampered-v2"})
    with pytest.raises(RunLockError, match="run_id"):
        run_probe(MockAdapter(), changed, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)


def test_same_config_resume_passes_lock(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """同配置续跑:配置锁放行,不报错(正常续跑路径)。"""
    store = RawStore(tmp_path)
    run_probe(MockAdapter(), demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)
    # 同配置再跑(全已完成)→ 锁一致,正常返回 manifest
    m = run_probe(MockAdapter(), demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert m.run_id == "r"


def test_resume_cost_gate_estimates_remaining(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """续跑时成本闸门按**剩余**调用数估算,不为已完成部分重复收费。"""
    store = RawStore(tmp_path)
    run_probe(MockAdapter(), demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)

    seen: list[tuple[float, int]] = []

    def confirm(cost: float, n_calls: int) -> bool:
        seen.append((cost, n_calls))
        return True

    run_probe(
        _PricyAdapter(0.0),  # 0 成本:静默通过,但记录 n_calls
        demo_cfg,
        RawStore(tmp_path),
        run_id="r",
        now=lambda: FIXED_NOW,
        confirm_cost=confirm,
    )
    # 全部已完成 → 剩余 0 次 → estimate=0 → 不触发确认(seen 为空)
    assert seen == []


# ---------------------------------------------------------------- 序贯采样(run_until_stable)


class _MentionAdapter(MockAdapter):
    """按 run_index 控制回答里提到哪些品牌(测序贯停止);可选 N 次后崩(测续跑)。"""

    def __init__(
        self, brands_for: Callable[[int], list[str]], crash_after: int | None = None
    ) -> None:
        super().__init__()
        self._brands_for = brands_for
        self._crash_after = crash_after
        self.calls = 0

    def ask(self, question, cfg, *, run_id, run_index):  # type: ignore[no-untyped-def]
        if self._crash_after is not None and self.calls >= self._crash_after:
            raise RuntimeError("simulated crash")
        self.calls += 1
        text = "Recommended: " + "; ".join(
            f"{n} is a great pick" for n in self._brands_for(run_index)
        )
        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text=text,
            citations=(),
            search_count=1,
            model="mock-model-standard",
            finished_ok=True,
            error_code=None,
            error_detail=None,
            raw_json="{}",
            timestamp=FIXED_NOW,
        )


def _seq_cfg() -> FrozenConfig:
    """单题两品牌的序贯配置:每题最少 2 次、最多 6 次、稳定即停。"""
    return FrozenConfig(
        version="seq-v1",
        prompt_version="seq-q-v1",
        competitor_set_version="seq-b-v1",
        focal_domain=None,
        peers=(
            Brand(name="Alpha Packs", domains=("alphapacks.example",)),
            Brand(name="Bravo Gear", domains=("bravogear.example",)),
        ),
        competitors_on=False,
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


def _both(_i: int) -> list[str]:
    return ["Alpha Packs", "Bravo Gear"]


def _flaky(i: int) -> list[str]:  # Bravo 仅偶数轮出现 → 忽有忽无
    return ["Alpha Packs", "Bravo Gear"] if i % 2 == 0 else ["Alpha Packs"]


def test_sequential_stops_early_when_stable(tmp_path: Path) -> None:
    # 每次都提到同样两个品牌 → 一致 → 达 min_runs(2)即停
    adapter = _MentionAdapter(_both)
    run_probe(adapter, _seq_cfg(), RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert adapter.calls == 2  # 1 题 × 2 轮(min),而非固定 6


def test_sequential_runs_to_max_when_unstable(tmp_path: Path) -> None:
    # Bravo 忽有忽无 → 永不稳 → 问到上限 max_runs(6)
    adapter = _MentionAdapter(_flaky)
    run_probe(adapter, _seq_cfg(), RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert adapter.calls == 6  # 1 题 × 上限 6 轮


def test_sequential_resume_rebuilds_and_continues(tmp_path: Path) -> None:
    # 不稳配置中途崩(成功 2 次)→ 续跑从已存的 2 次重建提及序列、接着问到上限
    with pytest.raises(RuntimeError, match="crash"):
        run_probe(
            _MentionAdapter(_flaky, crash_after=2),
            _seq_cfg(),
            RawStore(tmp_path),
            run_id="r",
            now=lambda: FIXED_NOW,
        )
    assert len(RawStore(tmp_path).read_responses("r")) == 2

    resume = _MentionAdapter(_flaky)
    run_probe(resume, _seq_cfg(), RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert resume.calls == 4  # 续跑补到 max=6(已存 2 + 新 4)
    assert len(RawStore(tmp_path).read_responses("r")) == 6


def test_probe_writes_complete_manifest(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    manifest = run_probe(MockAdapter(), demo_cfg, store, run_id="r", now=lambda: FIXED_NOW)
    assert store.read_manifests() == [manifest]
    responses = store.read_responses("r")
    assert len(responses) == len(demo_cfg.questions) * demo_cfg.n_runs
    assert all(isinstance(r, RawResponse) for r in responses)
    assert isinstance(demo_cfg.questions[0], Question)


def test_manifest_records_v02_honesty_fields(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    """清单如实记录:温度/区域是否生效、表面类型、真实花费(mock 恒 0)。"""
    raw = RawStore(tmp_path / "raw")
    manifest = run_probe(
        MockAdapter(), demo_cfg, raw, run_id="m", now=lambda: "2026-01-01T00:00:00Z"
    )
    assert manifest.temperature_applied is True and manifest.region_applied is True
    assert manifest.surface_kind == "answer" and manifest.cost_usd_actual == 0.0


def test_mock_adapter_accepts_explicit_model() -> None:
    """显式 model 优先于 tier;目录内 id 反推 tier,未知 id 也接受(mock 不挑模型)。"""
    a = MockAdapter(model="mock-model-high")
    assert a.model_name == "mock-model-high" and a.info.tier == "high"
    b = MockAdapter(tier="economy")
    assert b.model_name == "mock-model-economy" and b.info.provider == "mock"


# ---------------------------------------------------------------- 并发探测(有上限)
#
# 一题同一时刻只有一次调用在途(每题的轮次仍按 0,1,2… 顺序落盘,续跑/序贯逻辑不变);
# 并发的是"不同的题"。原始层仍只由主线程写。默认 concurrency=1 = 原来的逐个串行。


class _SlowAdapter(MockAdapter):
    """每次调用睡一小会儿,并记录同时在途的调用数(证明真的并发了、且没超上限)。"""

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__()
        self._delay = delay
        self._guard = threading.Lock()
        self._active: set[str] = set()
        self.calls = 0
        self.max_in_flight = 0
        self.same_question_overlap = False

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        with self._guard:
            self.calls += 1
            if question.id in self._active:
                self.same_question_overlap = True
            self._active.add(question.id)
            self.max_in_flight = max(self.max_in_flight, len(self._active))
        try:
            time.sleep(self._delay)
            return self._answer(question, cfg, run_id=run_id, run_index=run_index)
        finally:
            with self._guard:
                self._active.discard(question.id)

    def _answer(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


class _FailOneQuestionAdapter(_SlowAdapter):
    """指定的那一题立刻抛错,其它题照常(慢)完成:模拟并发中途某次调用出不可恢复错误。"""

    def __init__(self, bad_question_id: str) -> None:
        super().__init__(delay=0.3)
        self._bad = bad_question_id

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        if question.id == self._bad:
            # 不睡就抛:保证它比其它(睡 0.3 秒的)调用先回来,测试时序是确定的
            with self._guard:
                self.calls += 1
            raise RuntimeError("simulated fatal error")
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


class _SlowPaidAdapter(_SlowAdapter):
    """估算说免费、每次真花 $0.50(同 _RealCostAdapter),但调用有耗时,能真的同时在途。"""

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return 0.0

    def actual_cost_usd(self, response: RawResponse) -> float:
        return 0.50


def _keyed(store: RawStore) -> dict[tuple[str, int], tuple[str, str]]:
    return {
        (r.question_id, r.run_index): (r.answer_text, r.raw_json) for r in store.read_responses("r")
    }


def test_concurrent_probe_yields_same_data_as_serial(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    """并发只改"先后",不改"内容":同一配置串行跑和并发跑,记录逐条相同,清单也相同。"""
    serial, parallel = RawStore(tmp_path / "s"), RawStore(tmp_path / "p")
    m1 = run_probe(MockAdapter(), demo_cfg, serial, run_id="r", now=lambda: FIXED_NOW)
    m2 = run_probe(
        MockAdapter(), demo_cfg, parallel, run_id="r", now=lambda: FIXED_NOW, concurrency=3
    )
    assert _keyed(serial) == _keyed(parallel)
    assert len(_keyed(parallel)) == len(demo_cfg.questions) * demo_cfg.n_runs
    assert m1 == m2


def test_default_is_serial_one_call_at_a_time(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    adapter = _SlowAdapter(delay=0.01)
    run_probe(adapter, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert adapter.max_in_flight == 1
    # 串行时落盘顺序就是"按题、按轮"(与改动前完全一致)
    order = [(r.question_id, r.run_index) for r in RawStore(tmp_path).read_responses("r")]
    assert order == [(q.id, i) for q in demo_cfg.questions for i in range(demo_cfg.n_runs)]


def test_concurrency_overlaps_but_respects_the_limit(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    adapter = _SlowAdapter()
    run_probe(
        adapter, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW, concurrency=2
    )
    assert adapter.max_in_flight == 2  # 真的同时在途,且不超过上限(共 3 题)
    assert not adapter.same_question_overlap  # 同一题不会两次调用同时在途
    # 每题的轮次仍按 0,1,… 顺序落盘(续跑靠"已问轮次是连续前缀")
    seen: dict[str, list[int]] = {}
    for r in RawStore(tmp_path).read_responses("r"):
        seen.setdefault(r.question_id, []).append(r.run_index)
    assert all(runs == list(range(demo_cfg.n_runs)) for runs in seen.values())
    assert len(seen) == len(demo_cfg.questions)


def test_concurrency_reports_progress_for_every_call(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    ticks: list[tuple[int, int]] = []
    run_probe(
        _SlowAdapter(delay=0.01),
        demo_cfg,
        RawStore(tmp_path),
        run_id="r",
        now=lambda: FIXED_NOW,
        progress=lambda done, total, _spent: ticks.append((done, total)),
        concurrency=3,
    )
    total = len(demo_cfg.questions) * demo_cfg.n_runs
    assert ticks == [(i, total) for i in range(total + 1)]  # 0/6 起,每完成一次 +1,不丢不重


def test_concurrent_budget_cap_keeps_in_flight_calls_then_blocks(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    """预算封顶在并发下:达到上限后不再发新调用;已发出去的那几次(钱已花)照常保存。
    所以最多比串行多出 concurrency-1 次调用——这是并发的已知代价,如实测试钉住。"""
    # cap=1.0,每次真花 0.5:串行在第 2 次后阻断(存 2 次)。3 路并发首批 3 次同时发出;
    # 它们若同时回来存 3 次,若有一次先回来(花费才 0.5,未到上限)那题会接着发下一次 → 存 4 次。
    # 两种时序都合法,界是"串行的 2 次 + 最多 concurrency-1 次已在途的"。
    adapter = _SlowPaidAdapter()
    with pytest.raises(BudgetStop, match="预算上限"):
        run_probe(
            adapter, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW, concurrency=3
        )
    saved = len(RawStore(tmp_path).read_responses("r"))
    assert saved == adapter.calls  # 每一次付费调用都落了盘,没有"花了钱却没留底"
    assert 3 <= saved <= 2 + (3 - 1)

    resume = _SlowPaidAdapter()  # 续跑已超预算的 run:零新调用
    with pytest.raises(CostGateError):
        run_probe(
            resume, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW, concurrency=3
        )
    assert resume.calls == 0


def test_concurrent_failure_saves_other_in_flight_calls_then_raises(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    """一次调用抛出不可恢复错误时:不再发新调用;其它已在途的调用等它们回来并保存(钱已花),
    然后把原错误抛出去。"""
    bad = demo_cfg.questions[1].id
    adapter = _FailOneQuestionAdapter(bad)
    with pytest.raises(RuntimeError, match="simulated fatal error"):
        run_probe(
            adapter, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW, concurrency=3
        )
    saved = RawStore(tmp_path).read_responses("r")
    assert adapter.calls == 3  # 首批 3 题各 1 次;出错后没有再发新调用
    assert sorted(r.question_id for r in saved) == sorted(
        q.id for q in demo_cfg.questions if q.id != bad
    )


def test_sequential_sampling_works_per_question_under_concurrency(tmp_path: Path) -> None:
    """序贯采样的"稳了就停"是每题各自的判断,并发下不受别的题影响。"""
    cfg = _seq_cfg().model_copy(
        update={
            "questions": tuple(
                Question(id=f"q{i}", intent="discovery", text=f"Best packs {i}?")
                for i in range(1, 4)
            )
        }
    )
    stable = _MentionAdapter(_both)
    run_probe(
        stable, cfg, RawStore(tmp_path / "a"), run_id="r", now=lambda: FIXED_NOW, concurrency=3
    )
    assert len(RawStore(tmp_path / "a").read_responses("r")) == 3 * 2  # 每题 min_runs=2 即停
    flaky = _MentionAdapter(_flaky)
    run_probe(
        flaky, cfg, RawStore(tmp_path / "b"), run_id="r", now=lambda: FIXED_NOW, concurrency=3
    )
    assert len(RawStore(tmp_path / "b").read_responses("r")) == 3 * 6  # 每题问到上限 6


def test_concurrency_must_be_at_least_one(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        run_probe(MockAdapter(), demo_cfg, RawStore(tmp_path), run_id="r", concurrency=0)
    assert RawStore(tmp_path).read_responses("r") == []  # 参数不合法:一次调用都没发
