"""预算到顶之后:能看阶段性报告、能由用户决定是否调高上限接着跑(2026-11 终审)。

修复前:提示"调高预算后续跑",但预算被算进配置锁的指纹 → 续跑被拒;这轮也没有清单 → 出不了报告,
已花钱问到的数据两头都用不上。现在:预算不算测量基线(可调高续跑);停下时就已有数据出一份
带醒目标记的阶段性报告;交互终端里问用户要不要继续。全部离线、零成本。
"""

import sys
from pathlib import Path

import pytest

from aivis import app, cli
from aivis.contracts.adapter import ProviderAdapter, SurfaceSpec
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import EvidenceRecord, RawResponse, RunLock
from aivis.gui.runner import write_wizard_config
from aivis.pipeline.extract import extract
from aivis.pipeline.probe import BudgetStop, RunLockError, run_probe, snapshot_manifest
from aivis.pipeline.report import render_html, render_markdown
from aivis.pipeline.score import score
from aivis.providers import registry
from aivis.providers.mock import MockAdapter
from aivis.storage.raw import RawStore
from tests.conftest import FIXED_NOW, make_response
from tests.test_report_html import _bundle, _cfg, _metrics
from tests.test_report_surface_fields import _search_bundle
from tests.test_score import _manifest


class _Paid(MockAdapter):
    """估算说免费、每次真花 $0.50:让运行中的硬上限真的触发。"""

    calls = 0

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return 0.0

    def actual_cost_usd(self, response: RawResponse) -> float:
        return 0.50

    def ask(self, question, cfg, *, run_id, run_index):  # type: ignore[no-untyped-def]
        type(self).calls += 1
        return super().ask(question, cfg, run_id=run_id, run_index=run_index)


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    _Paid.calls = 0


def _with_cap(cfg: FrozenConfig, cap: float) -> FrozenConfig:
    return FrozenConfig.model_validate(cfg.model_dump() | {"budget_cap_usd": cap})


# ---------------------------------------------------------------- 契约:基线指纹


def test_baseline_fingerprint_ignores_only_the_budget(demo_cfg: FrozenConfig) -> None:
    raised = _with_cap(demo_cfg, 50.0)
    assert raised.fingerprint() != demo_cfg.fingerprint()  # 全量指纹照旧会变(清单里如实记)
    assert raised.baseline_fingerprint() == demo_cfg.baseline_fingerprint()
    other = FrozenConfig.model_validate(demo_cfg.model_dump() | {"n_runs": 5})
    assert other.baseline_fingerprint() != demo_cfg.baseline_fingerprint()  # 别的改动仍算换基线


# ---------------------------------------------------------------- probe:调高预算可续跑


def test_raising_the_budget_resumes_the_same_run(demo_cfg: FrozenConfig, tmp_path: Path) -> None:
    # demo_cfg:上限 $1.0,3 题 × 2 轮 = 6 次;每次 $0.5 → 第 2 次后到顶
    with pytest.raises(BudgetStop) as stopped:
        run_probe(_Paid(), demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    stop = stopped.value
    assert (stop.run_id, stop.done_calls, stop.total_calls) == ("r", 2, 6)
    assert stop.spent_usd == pytest.approx(1.0)
    assert RawStore(tmp_path).read_manifests() == []  # 没跑完:原始层里没有清单

    manifest = run_probe(
        _Paid(), _with_cap(demo_cfg, 99.0), RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW
    )
    assert _Paid.calls == 6  # 前 2 次没有重问:一共只付了 6 次的钱
    assert len(RawStore(tmp_path).read_responses("r")) == 6
    assert manifest.cost_usd_actual == pytest.approx(3.0)  # 清单记整条基线的真实花费
    assert manifest.budget_cap_usd == 99.0


def test_resume_still_rejects_any_other_config_change(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    with pytest.raises(BudgetStop):
        run_probe(_Paid(), demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    changed = FrozenConfig.model_validate(
        demo_cfg.model_dump() | {"budget_cap_usd": 99.0, "temperature": 0.7}
    )
    with pytest.raises(RunLockError):  # 借"调预算"夹带别的改动 → 仍然拒绝串味
        run_probe(_Paid(), changed, RawStore(tmp_path), run_id="r")


def test_lock_written_before_this_change_still_resumes(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    """旧锁存的是含预算的全量指纹:预算没变时照样认得,老数据不会因为升级而续不了。"""
    store = RawStore(tmp_path)
    adapter = MockAdapter()
    store.append_lock(
        RunLock(
            run_id="r",
            config_fingerprint=demo_cfg.fingerprint(),  # 旧写法
            provider=adapter.provider_name,
            model=adapter.model_name,
            tool_version=adapter.tool_version,
            n_runs=demo_cfg.n_runs,
            sequential=demo_cfg.sequential,
            min_runs=demo_cfg.min_runs,
            prompt_version=demo_cfg.prompt_version,
            competitor_set_version=demo_cfg.competitor_set_version,
        )
    )
    run_probe(adapter, demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    assert len(RawStore(tmp_path).read_responses("r")) == 6


def test_snapshot_manifest_is_not_written_to_the_raw_store(
    demo_cfg: FrozenConfig, tmp_path: Path
) -> None:
    with pytest.raises(BudgetStop):
        run_probe(_Paid(), demo_cfg, RawStore(tmp_path), run_id="r", now=lambda: FIXED_NOW)
    store = RawStore(tmp_path)
    snap = snapshot_manifest(_Paid(), demo_cfg, store, "r", now=lambda: FIXED_NOW)
    assert snap.run_id == "r" and snap.cost_usd_actual == pytest.approx(1.0)
    assert RawStore(tmp_path).read_manifests() == []  # 只读:原始层仍然只由 run_probe 写


# ---------------------------------------------------------------- score:阶段性标记 / 纯搜索表面


def _all_alpha(cfg: FrozenConfig) -> list[RawResponse]:
    return [
        make_response(question_id=q.id, run_index=i, answer_text="Alpha Packs is the pick.")
        for q in cfg.questions
        for i in range(cfg.n_runs)
    ]


def test_partial_result_is_flagged_and_never_green(demo_cfg: FrozenConfig) -> None:
    extraction = extract(_all_alpha(demo_cfg), demo_cfg)
    full = score(extraction, {}, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    part = score(
        extraction, {}, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW, partial=True
    )
    assert full.health.light == "green" and "partial-run:budget-cap" not in full.health.flags
    assert part.health.light == "yellow" and "partial-run:budget-cap" in part.health.flags
    assert part.brands == full.brands  # 只加标记,不动任何指标数字


def test_search_only_surface_is_not_marked_indistinguishable(demo_cfg: FrozenConfig) -> None:
    """纯搜索表面没有回答正文:提及覆盖恒为 0、区间必然全等,不能据此把健康灯判红。"""
    responses = [
        make_response(question_id=q.id, run_index=i, answer_text="")
        for q in demo_cfg.questions
        for i in range(demo_cfg.n_runs)
    ]
    extraction = extract(responses, demo_cfg)
    answer = score(extraction, {}, _manifest(demo_cfg), demo_cfg, generated_at=FIXED_NOW)
    assert any("all-intervals-overlap" in f for f in answer.health.flags)  # 回答型表面照旧判
    search = _manifest(demo_cfg).model_copy(update={"surface_kind": "search_only"})
    result = score(extraction, {}, search, demo_cfg, generated_at=FIXED_NOW)
    assert not any("all-intervals-overlap" in f for f in result.health.flags)
    assert result.health.light == "green"


# ---------------------------------------------------------------- 报告:如实标注


def _flagged(*flags: str):  # type: ignore[no-untyped-def]
    base = _bundle((_metrics("Acme Corp", 0.7, sov=0.6), _metrics("Beta Co", 0.4, sov=0.4)))
    return base.model_copy(
        update={"health": base.health.model_copy(update={"light": "red", "flags": flags})}
    )


def test_partial_report_carries_a_prominent_banner() -> None:
    bundle = _flagged("partial-run:budget-cap")
    assert "Partial run." in render_html(bundle, _cfg())
    assert "阶段性报告(这轮没跑完)" in render_markdown(bundle, _cfg())
    complete = _bundle((_metrics("Acme Corp", 0.7),))
    assert "Partial run." not in render_html(complete, _cfg())
    assert "阶段性报告" not in render_markdown(complete, _cfg())


def test_html_does_not_rank_when_intervals_overlap() -> None:
    """排名无法区分时(§7.5),HTML 不许再写"第一名 / 领先 N 个点"。"""
    h = render_html(_flagged("all-intervals-overlap:ranking-indicative-only"), _cfg())
    assert "Top brand" not in h and "ahead of #2" not in h
    assert "not distinguishable from the others" in h
    assert "LEADER" not in h
    clear = render_html(
        _bundle((_metrics("Acme Corp", 0.7, sov=0.6), _metrics("Beta Co", 0.4, sov=0.4))), _cfg()
    )
    assert "Top brand · mention coverage" in clear and "+30pt ahead of #2" in clear


def test_html_says_when_links_were_only_assumed_reachable() -> None:
    offline = EvidenceRecord(
        citation_url="https://r.example/a",
        normalized_url="https://r.example/a",
        canonical_domain="r.example",
        owned_or_earned="earned",
        source_class="third_party",
        reachable=True,
        check_method="offline-assumed",
        checked_at=FIXED_NOW,
    )
    base = _bundle((_metrics("Acme Corp", 0.62),))
    h = render_html(base.model_copy(update={"evidence": (offline,)}), _cfg())
    assert "not fetched" in h and "Citation validity · assumed" in h
    fetched = offline.model_copy(update={"check_method": "page-fetch"})
    h2 = render_html(base.model_copy(update={"evidence": (fetched,)}), _cfg())
    assert "not fetched" not in h2 and "Citation validity · assumed" not in h2


def test_reports_describe_adaptive_sampling_truthfully() -> None:
    bundle = _bundle((_metrics("Acme Corp", 0.62),))
    fixed_html = render_html(bundle, _cfg())
    assert "fixed 3 rounds" in fixed_html and "24 × 3 = 60 ok" in fixed_html
    seq_cfg = FrozenConfig.model_validate(_cfg().model_dump() | {"sequential": True})
    seq_html = render_html(bundle, seq_cfg)
    assert "fixed 3 rounds" not in seq_html and "adaptive sampling (up to 3 rounds" in seq_html
    assert "24 × 3" not in seq_html
    seq_md = render_markdown(bundle, seq_cfg)
    assert "序贯采样:每题最少 2 轮、最多 3 轮" in seq_md and "固定 3 轮采样" not in seq_md
    assert "固定 3 轮采样" in render_markdown(bundle, _cfg())


def test_search_only_html_has_no_mention_ranking() -> None:
    h = render_html(_search_bundle(), _cfg())
    assert "Mention coverage ranking" not in h and "Top brand" not in h
    assert "Highest" not in h and "Lowest" not in h
    assert "Not applicable" in h


def test_network_panel_makes_no_unverifiable_claim() -> None:
    assert "Anywhere else" not in render_html(_bundle((_metrics("Acme Corp", 0.62),)), _cfg())


# ---------------------------------------------------------------- CLI:阶段性报告 + 用户选择


def _use_paid_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    def make(spec: SurfaceSpec, *, tier: str = "standard") -> ProviderAdapter:
        return _Paid(model=spec.model)

    monkeypatch.setattr(registry, "make_adapter", make)


def _run(cfg_dir: Path, data: Path, *extra: str) -> int:
    return cli.main(
        ["run", "--config-dir", str(cfg_dir), "--data-dir", str(data), "--concurrency", "1", *extra]
    )


def test_cli_budget_stop_writes_partial_report_then_resumes(
    demo_cfg: FrozenConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_paid_adapter(monkeypatch)
    cfg_dir, data = tmp_path / "cfg", tmp_path / "data"
    write_wizard_config(cfg_dir, demo_cfg)

    rc = _run(cfg_dir, data, "--yes")
    assert rc == cli.EXIT_BUDGET_STOP
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("BUDGET_STOP "))
    _, label, run_id, progress, spent = line.split()
    assert (label, progress, spent) == ("mock:mock-model-standard", "2/6", "1.0000")
    report = data / "analysis" / run_id / "report.html"
    assert "Partial run." in report.read_text(encoding="utf-8")  # 已花钱的数据看得到
    assert RawStore(data / "raw").read_manifests() == []

    # 用户决定继续:调高上限,同一个 run_id 接着跑完
    write_wizard_config(cfg_dir, _with_cap(demo_cfg, 99.0))
    assert _run(cfg_dir, data, "--run-id", run_id, "--yes") == 0
    assert _Paid.calls == 6  # 没有为前 2 次重复付费
    assert "Partial run." not in report.read_text(encoding="utf-8")  # 报告已换成完整版
    assert [m.run_id for m in RawStore(data / "raw").read_manifests()] == [run_id]


def test_cli_asks_the_user_whether_to_continue(
    demo_cfg: FrozenConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """交互终端里预算到顶:问用户;给出更高的上限就在同一条命令里接着跑完。"""
    _use_paid_adapter(monkeypatch)
    cfg_dir, data = tmp_path / "cfg", tmp_path / "data"
    write_wizard_config(cfg_dir, demo_cfg)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    asked: list[str] = []

    def answer(prompt: str = "") -> str:
        asked.append(prompt)
        return "99"

    monkeypatch.setattr("builtins.input", answer)
    assert _run(cfg_dir, data) == 0
    assert len(asked) == 1 and "新的预算上限" in asked[0]
    assert _Paid.calls == 6
    assert len(RawStore(data / "raw").read_manifests()) == 1


def test_cli_user_can_choose_to_stop(
    demo_cfg: FrozenConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_paid_adapter(monkeypatch)
    cfg_dir, data = tmp_path / "cfg", tmp_path / "data"
    write_wizard_config(cfg_dir, demo_cfg)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")  # 直接回车 = 停在这里
    assert _run(cfg_dir, data) == cli.EXIT_BUDGET_STOP
    assert _Paid.calls == 2  # 一次都没多问


def test_cli_blocks_whole_batch_when_one_surface_is_over_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """多表面里有一个预估超上限:开跑前整批拦下。不许前面的表面花完钱、轮到它才中止。"""

    class _Pricy(MockAdapter):
        def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
            return 99.0

    def make(spec: SurfaceSpec, *, tier: str = "standard") -> ProviderAdapter:
        return (_Pricy if spec.model == "mock-model-high" else MockAdapter)(model=spec.model)

    monkeypatch.setattr(registry, "make_adapter", make)
    rc = cli.main(
        [
            "run",
            "--config-dir",
            "configs/demo",
            "--data-dir",
            str(tmp_path),
            "--surfaces",
            "mock:mock-model-economy,mock:mock-model-high",
            "--yes",
        ]
    )
    assert rc == 2
    assert "一个表面都没有跑" in capsys.readouterr().err
    assert not list((tmp_path / "raw").glob("raw_responses.jsonl"))


# ---------------------------------------------------------------- CLI:干净的失败


def test_score_without_a_manifest_explains_instead_of_crashing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["score", "--data-dir", str(tmp_path), "--run-id", "nope"])
    assert "还没有运行清单" in str(exit_info.value)


def test_python_m_aivis_passes_the_exit_code_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """便携包的入口是 `python -m aivis`:失败必须带出非零退出码(以前恒为 0)。"""
    monkeypatch.setattr(sys, "argv", ["aivis", "validate", "--config-dir", str(tmp_path / "x")])
    with pytest.raises(SystemExit) as exit_info:
        app.main()
    assert exit_info.value.code == 2
