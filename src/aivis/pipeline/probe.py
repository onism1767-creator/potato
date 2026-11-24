"""probe —— I/O 层:唯一向原始层写入的模块(🟥 CLAUDE.md §1)。

成本闸门(§6):真实运行前估算并要求人工确认;mock 估价为 0,静默通过。
幂等续跑:同一 run_id 重跑会跳过已写入的 (question, run_index),崩溃不重烧预算。
采样:固定 N = cfg.n_runs,或序贯(run_until_stable,每题稳了就停)。
并发(2026-11):可选的有上限并发(不同的题同时问);原始层仍只由主线程写。
"""

import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime

from aivis import matching
from aivis.contracts.adapter import ProviderAdapter
from aivis.contracts.config import FrozenConfig, Question
from aivis.contracts.records import RawResponse, RunLock, RunManifest
from aivis.stats.sequential import should_ask_more
from aivis.storage.raw import RawStore


class CostGateError(RuntimeError):
    """成本闸门阻断:超预算上限,或非零成本未获人工确认。"""


class BudgetStop(CostGateError):
    """运行中真实累计花费达到预算上限而停下:已完成的调用都已保存。

    这不是数据丢失:调用方可以就已有数据出一份阶段性报告(snapshot_manifest),
    也可以调高 budget_cap_usd 后用同一个 run_id 续跑(预算不算测量基线,配置锁放行)。
    """

    def __init__(
        self, message: str, *, run_id: str, done_calls: int, total_calls: int, spent_usd: float
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.done_calls = done_calls
        self.total_calls = total_calls
        self.spent_usd = spent_usd


class RunLockError(RuntimeError):
    """续跑同一 run_id 但配置/引擎与首次不一致 —— 拒绝混入同一基线(防串味)。"""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_baseline(existing: RunLock, current: RunLock, cfg: FrozenConfig) -> bool:
    """续跑校验:锁里记的是"测量基线指纹"(不含预算上限),所以调高预算续跑会放行。

    兼容旧锁(2026-11 之前写入的锁存的是含预算的全量指纹):预算没变时仍然认得。
    """
    if existing == current:
        return True
    legacy = current.model_copy(update={"config_fingerprint": cfg.fingerprint()})
    return existing == legacy


def _build_manifest(
    adapter: ProviderAdapter,
    cfg: FrozenConfig,
    raw_store: RawStore,
    run_id: str,
    *,
    timestamp: str,
    spent: float,
) -> RunManifest:
    info = adapter.info
    return RunManifest(
        run_id=run_id,
        provider=adapter.provider_name,
        surface_id=adapter.surface_id,
        model=adapter.model_name,
        tool_path=adapter.tool_path,
        tool_version=adapter.tool_version,
        locale=cfg.locale,
        region=cfg.region,
        prompt_version=cfg.prompt_version,
        competitor_set_version=cfg.competitor_set_version,
        tier=cfg.tier,
        max_uses=cfg.max_uses,
        budget_cap_usd=cfg.budget_cap_usd,
        temperature=cfg.temperature,
        n_runs=cfg.n_runs,
        timestamp=timestamp,
        search_count=sum(r.search_count for r in raw_store.read_responses(run_id)),
        config_fingerprint=cfg.fingerprint(),
        # v0.2 诚实字段:温度/区域是否真的传给了引擎、表面类型、真实累计花费(账单)
        temperature_applied=info.supports_temperature,
        region_applied=info.supports_region and bool(cfg.region),
        surface_kind=info.surface_kind,
        cost_usd_actual=round(spent, 6),
    )


def snapshot_manifest(
    adapter: ProviderAdapter,
    cfg: FrozenConfig,
    raw_store: RawStore,
    run_id: str,
    *,
    now: Callable[[], str] = utc_now,
) -> RunManifest:
    """给"还没跑完"的 run 现算一份清单,**不写入原始层**(只读)。

    用途:预算到顶停下后,就已保存的那部分数据出阶段性报告。正式清单仍只在整轮跑完时写一次。
    """
    spent = sum(adapter.actual_cost_usd(r) for r in raw_store.read_responses(run_id))
    return _build_manifest(adapter, cfg, raw_store, run_id, timestamp=now(), spent=spent)


def run_probe(
    adapter: ProviderAdapter,
    cfg: FrozenConfig,
    raw_store: RawStore,
    *,
    run_id: str | None = None,
    now: Callable[[], str] = utc_now,
    confirm_cost: Callable[[float, int], bool] | None = None,
    progress: Callable[[int, int, float], None] | None = None,
    concurrency: int = 1,
) -> RunManifest:
    """全部问题 × n_runs 的 clean-room 探测;写原始层并返回 RunManifest。

    progress(done, total, spent_usd):可选,每完成一次调用回调一次(GUI 进度条用);
    total 为上界(序贯模式可能提前停),spent_usd 为累计真实花费(mock 恒 0)。
    默认 None=静默,不改既有行为/黄金。

    concurrency:同时在途的调用数上限(默认 1 = 逐个串行,与旧行为完全一致)。>1 时不同的题
    并发发出,只改"先后"不改"内容"(每次调用仍是独立的 clean-room 调用;extract 不依赖落盘顺序)。
    已知代价:预算封顶时已发出的调用收不回,最多比串行多花 concurrency-1 次调用的钱。
    """
    if concurrency < 1:
        raise ValueError(f"concurrency 必须 ≥ 1,收到 {concurrency}")
    rid = run_id if run_id is not None else uuid.uuid4().hex
    done = raw_store.read_response_keys(rid)  # 续跑:已存在的探测不再调 provider(省钱)

    # 🟥 配置锁:续跑同 run_id 必须与首次的测量基线/引擎一致(防把不同基线串味)。
    # 在任何 provider 调用之前校验;RunManifest 运行结束才写,救不了续跑校验。
    # 指纹用 baseline_fingerprint(不含预算上限):调高预算续跑不算换基线。
    lock = RunLock(
        run_id=rid,
        config_fingerprint=cfg.baseline_fingerprint(),
        provider=adapter.provider_name,
        model=adapter.model_name,
        tool_version=adapter.tool_version,
        n_runs=cfg.n_runs,
        sequential=cfg.sequential,
        min_runs=cfg.min_runs,
        prompt_version=cfg.prompt_version,
        competitor_set_version=cfg.competitor_set_version,
    )
    existing_lock = raw_store.read_lock(rid)
    if existing_lock is not None and not _same_baseline(existing_lock, lock, cfg):
        raise RunLockError(
            f"run_id={rid!r} 续跑配置与首次不一致,拒绝混入同一基线。"
            f"\n  首次锁: {existing_lock!r}"
            f"\n  本次:   {lock!r}"
            "\n请换一个 run_id,或用与首次相同的配置/引擎续跑(只有预算上限可以改)。"
        )
    # 成本闸门按**剩余待调用数**估算(续跑不为已完成的部分重复收费)
    n_calls = sum(
        1
        for question in cfg.questions
        for run_index in range(cfg.n_runs)
        if (question.id, run_index) not in done
    )
    estimated = adapter.estimate_cost_usd(n_calls, cfg)
    if estimated > cfg.budget_cap_usd:
        raise CostGateError(
            f"预估成本 ${estimated:.2f}({n_calls} 次调用)超过预算上限 "
            f"${cfg.budget_cap_usd:.2f},已阻断。"
        )
    if estimated > 0 and (confirm_cost is None or not confirm_cost(estimated, n_calls)):
        raise CostGateError(
            f"本次运行将真实调用付费 API:约 {n_calls} 次,预估 ${estimated:.2f}。"
            "需要人工确认(--yes 或交互确认)才能继续。"
        )
    # 闸门通过后才落锁:被闸门拦下的 run_id 一次调用都没发,不该被一把锁占住(换配置还能用)
    raw_store.append_lock(lock)  # 首次写入;已存在(且一致)则跳过

    # 运行时硬封顶(CLAUDE.md §6):累加真实花费,逼近 budget_cap 即阻断。
    # 续跑时计入已完成调用的真实花费,使预算约束覆盖整条基线。
    # 🟥 在发起**下一次付费调用之前**检查:跨过阈值的那次仍被保存,但不会再多花一次
    # (尤其续跑一个已超预算的 run 时,零新付费调用)。mock 恒 0,永不触发。
    responses = raw_store.read_responses(rid)
    spent = sum(adapter.actual_cost_usd(r) for r in responses)
    # 续跑:已存响应按题分组(按 run_index 排序),用于重建提及序列、接着往下问
    existing: dict[str, list[RawResponse]] = {}
    for r in responses:
        existing.setdefault(r.question_id, []).append(r)
    for group in existing.values():
        group.sort(key=lambda r: r.run_index)
    # 提及检测口径与 extract 一致(共用 matching);仅用于"问几次"的采样控制,不参与计分
    clear_surfaces = [(b.name, tuple(s for s in (b.name, *b.aliases) if s)) for b in cfg.peers]

    def mentioned(text: str) -> frozenset[str]:
        return frozenset(
            name
            for name, surfaces in clear_surfaces
            if any(matching.name_pattern(s).search(text) for s in surfaces)
        )

    total_calls = len(cfg.questions) * cfg.n_runs  # 进度分母(上界;序贯可能提前停)
    done_calls = sum(len(g) for g in existing.values())  # 续跑:已完成计入进度
    if progress is not None:
        progress(done_calls, total_calls, spent)

    # 每题的采样状态:已问轮次(append 按序写,续跑时为连续前缀)+ 序贯模式用的提及序列
    asked = {q.id: len(existing.get(q.id, [])) for q in cfg.questions}
    # 序贯判稳只看**成功**的回答:失败的调用没有答案,不能当成"这次谁都没提到"的观测
    # (与 extract 同口径:失败响应不抽取)。失败仍占一次轮次(asked),所以上限照样封得住。
    mention_runs: dict[str, list[frozenset[str]]] = {
        q.id: (
            [mentioned(r.answer_text) for r in existing.get(q.id, []) if r.finished_ok]
            if cfg.sequential
            else []
        )
        for q in cfg.questions
    }

    def wants_more(question_id: str) -> bool:
        if asked[question_id] >= cfg.n_runs:  # n_runs:固定模式=轮次;序贯模式=上限
            return False
        # 序贯:已稳定就提前停,省调用(每题各自判断,与别的题无关)
        return not cfg.sequential or should_ask_more(
            mention_runs[question_id], min_runs=cfg.min_runs, max_runs=cfg.n_runs
        )

    # 有上限的并发:同一时刻最多 concurrency 次调用在途,且**同一题只有一次在途**
    # (每题的轮次仍按 0,1,2… 顺序发出和落盘,续跑与序贯逻辑不变;并发的是"不同的题")。
    # 调度、写原始层、累计花费、回调进度全在本(主)线程;工作线程只做 adapter.ask 这一次 I/O。
    # concurrency=1 时与改动前逐个串行完全等价(一题问完才轮到下一题)。
    ready = deque(cfg.questions)  # 等着发下一次调用的题;问完一次的题放回队首
    in_flight: dict[Future[RawResponse], Question] = {}
    over_budget = False
    failure: BaseException | None = None

    def record(question: Question, response: RawResponse) -> None:
        """(主线程)把一次已返回的调用落盘并记账 —— 钱已经花了,必须留底。"""
        nonlocal spent, done_calls
        raw_store.append_response(response)
        spent += adapter.actual_cost_usd(response)
        if cfg.sequential and response.finished_ok:
            mention_runs[question.id].append(mentioned(response.answer_text))
        asked[question.id] += 1
        done_calls += 1

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="probe") as pool:
        try:
            while True:
                while (
                    ready and len(in_flight) < concurrency and failure is None and not over_budget
                ):
                    question = ready.popleft()
                    if not wants_more(question.id):
                        continue
                    # 🟥 在发起**下一次付费调用之前**检查预算:达到上限就不再发新调用
                    if spent > 0 and spent >= cfg.budget_cap_usd:
                        over_budget = True
                        break
                    future = pool.submit(
                        adapter.ask, question, cfg, run_id=rid, run_index=asked[question.id]
                    )
                    in_flight[future] = question
                if not in_flight:
                    break
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    question = in_flight[future]
                    error = future.exception()
                    if error is not None:
                        # 不可恢复错误:不再发新调用;其余在途的等它们回来并保存(钱已花),之后再抛
                        del in_flight[future]
                        failure = failure if failure is not None else error
                        continue
                    record(question, future.result())
                    del in_flight[future]  # 落盘成功才算处理完(落盘抛错时留给下面的抢救)
                    if progress is not None:
                        progress(done_calls, total_calls, spent)
                    ready.appendleft(question)
        except BaseException:
            # 主线程被打断(Ctrl+C / 进度回调抛错 / 落盘抛错):已发出的调用钱已经花了。
            # 线程池退出时本来就要等它们回来,这里把回来的结果落盘记账,再把原异常抛出去 ——
            # 否则续跑要为同一批调用再付一次钱。抢救是尽力而为:单条失败不盖过原异常。
            for future, question in list(in_flight.items()):
                try:
                    record(question, future.result())
                except Exception:  # 该调用自己失败了 / 落盘仍不行:没什么可留的,看下一条
                    continue
            raise
    if failure is not None:
        raise failure
    if over_budget:
        raise BudgetStop(
            f"运行中真实累计花费 ${spent:.2f} 达预算上限 "
            f"${cfg.budget_cap_usd:.2f},已在第 {done_calls} 次调用后停下。"
            f"已完成部分的原始数据已保存(run_id={rid}):可以就这部分出阶段性报告,"
            f"也可以调高 budget_cap_usd 后用 --run-id {rid} 续跑(预算不算测量基线)。",
            run_id=rid,
            done_calls=done_calls,
            total_calls=total_calls,
            spent_usd=spent,
        )

    manifest = _build_manifest(adapter, cfg, raw_store, rid, timestamp=now(), spent=spent)
    raw_store.append_manifest(manifest)
    return manifest
