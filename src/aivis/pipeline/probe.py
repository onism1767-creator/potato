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


class RunLockError(RuntimeError):
    """续跑同一 run_id 但配置/引擎与首次不一致 —— 拒绝混入同一基线(防串味)。"""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # 🟥 配置锁:续跑同 run_id 必须与首次配置/引擎完全一致(防把不同基线串味)。
    # 在任何 provider 调用之前校验 + 落锁;RunManifest 运行结束才写,救不了续跑校验。
    lock = RunLock(
        run_id=rid,
        config_fingerprint=cfg.fingerprint(),
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
    if existing_lock is not None and existing_lock != lock:
        raise RunLockError(
            f"run_id={rid!r} 续跑配置与首次不一致,拒绝混入同一基线。"
            f"\n  首次锁: {existing_lock!r}"
            f"\n  本次:   {lock!r}"
            "\n请换一个 run_id,或用与首次完全相同的配置/引擎续跑。"
        )
    raw_store.append_lock(lock)  # 首次写入;已存在(且一致)则跳过
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
    mention_runs: dict[str, list[frozenset[str]]] = {
        q.id: [mentioned(r.answer_text) for r in existing.get(q.id, [])] if cfg.sequential else []
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
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="probe") as pool:
        while True:
            while ready and len(in_flight) < concurrency and failure is None and not over_budget:
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
                question = in_flight.pop(future)
                error = future.exception()
                if error is not None:
                    # 不可恢复错误:不再发新调用;其余在途的等它们回来并保存(钱已花),之后再抛
                    failure = failure if failure is not None else error
                    continue
                response = future.result()
                raw_store.append_response(response)
                spent += adapter.actual_cost_usd(response)
                if cfg.sequential:
                    mention_runs[question.id].append(mentioned(response.answer_text))
                asked[question.id] += 1
                done_calls += 1
                if progress is not None:
                    progress(done_calls, total_calls, spent)
                ready.appendleft(question)
    if failure is not None:
        raise failure
    if over_budget:
        n_done = len(raw_store.read_responses(rid))
        raise CostGateError(
            f"运行中真实累计花费 ${spent:.2f} 达预算上限 "
            f"${cfg.budget_cap_usd:.2f},已在第 {n_done} 次调用后阻断。"
            f"已完成部分原始数据已保存(run_id={rid}),"
            f"提高 budget_cap_usd 后用 --run-id {rid} 续跑。"
        )

    total_search = sum(r.search_count for r in raw_store.read_responses(rid))
    info = adapter.info
    manifest = RunManifest(
        run_id=rid,
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
        timestamp=now(),
        search_count=total_search,
        config_fingerprint=cfg.fingerprint(),
        # v0.2 诚实字段:温度/区域是否真的传给了引擎、表面类型、真实累计花费(账单)
        temperature_applied=info.supports_temperature,
        region_applied=info.supports_region and bool(cfg.region),
        surface_kind=info.surface_kind,
        cost_usd_actual=round(spent, 6),
    )
    raw_store.append_manifest(manifest)
    return manifest
