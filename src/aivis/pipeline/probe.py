"""probe —— I/O 层:唯一向原始层写入的模块(🟥 CLAUDE.md §1)。

成本闸门(§6):真实运行前估算并要求人工确认;mock 估价为 0,静默通过。
幂等续跑:同一 run_id 重跑会跳过已写入的 (question, run_index),崩溃不重烧预算。
v1 采样固定 N = cfg.n_runs(序贯 run_until_stable 是后期项)。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from aivis import matching
from aivis.contracts.adapter import ProviderAdapter
from aivis.contracts.config import FrozenConfig
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
) -> RunManifest:
    """全部问题 × n_runs 的 clean-room 探测;写原始层并返回 RunManifest。

    progress(done, total, spent_usd):可选,每完成一次调用回调一次(GUI 进度条用);
    total 为上界(序贯模式可能提前停),spent_usd 为累计真实花费(mock 恒 0)。
    默认 None=静默,不改既有行为/黄金。
    """
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
    for question in cfg.questions:
        prior = existing.get(question.id, [])
        asked = len(prior)  # 已问轮次(append 按序写,续跑时为连续前缀)
        mention_runs = [mentioned(r.answer_text) for r in prior] if cfg.sequential else []
        while asked < cfg.n_runs:  # n_runs:固定模式=轮次;序贯模式=上限
            if cfg.sequential and not should_ask_more(
                mention_runs, min_runs=cfg.min_runs, max_runs=cfg.n_runs
            ):
                break  # 已稳定:提前停,省调用
            if spent > 0 and spent >= cfg.budget_cap_usd:
                n_done = len(raw_store.read_responses(rid))
                raise CostGateError(
                    f"运行中真实累计花费 ${spent:.2f} 达预算上限 "
                    f"${cfg.budget_cap_usd:.2f},已在第 {n_done} 次调用后阻断。"
                    f"已完成部分原始数据已保存(run_id={rid}),"
                    f"提高 budget_cap_usd 后用 --run-id {rid} 续跑。"
                )
            response = adapter.ask(question, cfg, run_id=rid, run_index=asked)
            raw_store.append_response(response)
            spent += adapter.actual_cost_usd(response)
            if cfg.sequential:
                mention_runs.append(mentioned(response.answer_text))
            asked += 1
            done_calls += 1
            if progress is not None:
                progress(done_calls, total_calls, spent)

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
