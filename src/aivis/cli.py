"""cli —— 🟥 薄如纸:子命令只做编排转发,所有逻辑在可被 import 的库里。

子命令:probe / extract / verify / score / report(五原语)+ run(依次全跑)。
默认 mock、默认离线核验:不花钱、不联网即可端到端出报告。
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from aivis.configcheck import has_errors, validate_config
from aivis.configio import load_frozen_config
from aivis.contracts.adapter import FatalProviderError, ProviderAdapter, SurfaceSpec
from aivis.contracts.checker import UrlChecker
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import RunManifest
from aivis.pipeline.extract import extract
from aivis.pipeline.probe import (
    BudgetStop,
    CostGateError,
    RunLockError,
    run_probe,
    snapshot_manifest,
    utc_now,
)
from aivis.pipeline.report import render_csv, render_html, render_markdown
from aivis.pipeline.report_compare import render_comparison_html, render_comparison_markdown
from aivis.pipeline.score import score
from aivis.pipeline.verify import (
    HttpHeadChecker,
    OfflineChecker,
    PageFetchChecker,
    verify,
)
from aivis.providers import catalog, registry
from aivis.scaffold import build_scaffold, write_scaffold
from aivis.storage.analysis import AnalysisStore
from aivis.storage.evidence import EvidenceStore
from aivis.storage.raw import RawStore

DEFAULT_CONCURRENCY = 4  # probe 同时在途的调用数上限(--concurrency;1 = 逐个串行)
EXIT_BUDGET_STOP = 6  # run 跑到了头,但至少一个表面因预算到顶停下(只有阶段性报告)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"必须是 ≥ 1 的整数,收到 {text!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aivis",
        description="AI web-search 可见度(代理测量;Claude / OpenAI / Gemini / Perplexity / Grok)"
        "——可复现的提及/引用测量框架",
    )
    parser.add_argument("--config-dir", type=Path, default=Path("configs/demo"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--provider",
        choices=sorted(catalog.PROVIDERS),
        default="mock",
        help="引擎(默认 mock 零成本);真引擎需对应环境变量里的密钥且过成本闸门(见 aivis models)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="精确模型 id(aivis models 列出全部);缺省取该引擎 --tier 档的默认模型",
    )
    parser.add_argument(
        "--tier",
        default="standard",
        help="模型档位便利选项:economy / standard / high(/ flagship);显式 --model 优先",
    )
    parser.add_argument(
        "--surfaces",
        default=None,
        help="逗号分隔的 provider:model 列表(如 openai:gpt-6-luna,gemini:gemini-3.8-flash);"
        "run / estimate 对每个表面各跑一次,最后出并列对比报告与账单(只并列、不混总分)",
    )
    parser.add_argument("--run-id", default=None, help="指定运行 ID(默认:probe 新建/其余取最新)")
    parser.add_argument("--yes", action="store_true", help="非交互确认成本闸门")
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=DEFAULT_CONCURRENCY,
        help=f"probe 同时在途的调用数上限(默认 {DEFAULT_CONCURRENCY};1 = 逐个串行)。"
        "并发的是不同的题,只改先后不改内容;被引擎限速(失败数变多)就调小。"
        "预算封顶时已发出的调用收不回,最多多花 N-1 次调用的钱",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="序贯采样:答案稳就少问、飘就多问(每题 min_runs~n_runs 自适应,省钱);默认固定 n_runs",
    )
    parser.add_argument(
        "--check",
        choices=["offline", "head", "fetch"],
        default="offline",
        help=(
            "verify 核验方式:offline=离线假设(默认,不联网);"
            "head=HTTP HEAD 只看状态码;fetch=真页面抓取+消毒摘录(用户触发)"
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="verify 忽略证据缓存、强制重新核验所有 URL("
        "改了核验逻辑/口径后必须用,否则会复用旧语义的脏证据)",
    )
    parser.add_argument(
        "--phase",
        choices=["draft", "final"],
        default="final",
        help="validate 检查点:draft=AI 起草后(配比严格);final=用户拍板后(配比仅提醒)",
    )
    parser.add_argument(
        "command",
        choices=[
            "probe",
            "extract",
            "verify",
            "score",
            "report",
            "run",
            "validate",
            "init",
            "estimate",
            "models",
            "gui",
        ],
    )
    parser.add_argument("--port", type=int, default=8765, help="gui: 本地向导端口(默认 8765)")
    parser.add_argument("--no-open", action="store_true", help="gui: 不自动打开浏览器")
    # init 专用(其它子命令忽略)
    parser.add_argument("name", nargs="?", default=None, help="init: 配置名(写到 <out>/<name>)")
    parser.add_argument("--out", type=Path, default=Path("configs"), help="init: 输出根目录")
    parser.add_argument("--focal-name", default=None, help="init: 焦点品牌全名")
    parser.add_argument("--focal-domain", default=None, help="init: 焦点品牌官网域名")
    parser.add_argument("--category", default=None, help="init: 品类/行业描述(可选)")
    return parser


def _surface_specs(args: argparse.Namespace) -> list[SurfaceSpec]:
    """--surfaces(多表面)优先;否则单表面 = --provider(+ --model / --tier)。"""
    if args.surfaces:
        return [SurfaceSpec.parse(s) for s in str(args.surfaces).split(",") if s.strip()]
    return [SurfaceSpec(provider=args.provider, model=args.model)]


def _build_adapters(args: argparse.Namespace) -> list[ProviderAdapter] | None:
    """按表面构造适配器(引擎名分支只在 providers.registry);表面/模型写错 → 干净报错返回 None。"""
    try:
        return [registry.make_adapter(spec, tier=args.tier) for spec in _surface_specs(args)]
    except (ValueError, NotImplementedError) as e:
        print(f"[表面/模型错误] {e}(用 aivis models 查看可选表面)", file=sys.stderr)
        return None


def _make_adapter(args: argparse.Namespace) -> ProviderAdapter:
    return registry.make_adapter(
        SurfaceSpec(provider=args.provider, model=args.model), tier=args.tier
    )


def _resolve_run_id(args: argparse.Namespace, raw: RawStore) -> str:
    if args.run_id:
        return str(args.run_id)
    manifests = raw.read_manifests()
    if not manifests:
        raise SystemExit("原始层还没有任何运行,请先执行: aivis probe")
    return manifests[-1].run_id


def _cmd_probe(
    args: argparse.Namespace,
    cfg: FrozenConfig,
    raw: RawStore,
    *,
    adapter: ProviderAdapter | None = None,
    preconfirmed: bool = False,
    run_id: str | None = None,
) -> str:
    def confirm(cost: float, n_calls: int) -> bool:
        if args.yes or preconfirmed:
            return True
        answer = input(f"预估成本 ${cost:.2f}({n_calls} 次真实调用),继续?[y/N] ")
        return answer.strip().lower() == "y"

    def progress(done: int, total: int, spent: float) -> None:
        # 机器可解析进度行(GUI runner 据此画进度条/估剩余时间/累计花费);flush 保证管道下实时
        print(f"PROGRESS probe {done}/{total} {spent:.4f}", flush=True)

    manifest = run_probe(
        adapter if adapter is not None else _make_adapter(args),
        cfg,
        raw,
        run_id=run_id if run_id is not None else args.run_id,
        confirm_cost=confirm,
        progress=progress,
        concurrency=args.concurrency,
    )
    print(f"probe 完成: run_id={manifest.run_id}  search_count={manifest.search_count}")
    return manifest.run_id


def _ask_new_cap(args: argparse.Namespace, cfg: FrozenConfig, stop: BudgetStop) -> float | None:
    """预算到顶后让用户自己选:输入更高的上限 = 接着跑;回车 = 停在这里。非交互(--yes/管道)不问。"""
    if args.yes or not sys.stdin.isatty():
        return None
    print(
        f"已花 ${stop.spent_usd:.2f}(上限 ${cfg.budget_cap_usd:.2f}),"
        f"完成 {stop.done_calls}/{stop.total_calls} 次调用。"
    )
    answer = input(
        "要接着跑,请输入新的预算上限(美元);直接回车 = 停在这里,就已有数据出阶段性报告: "
    ).strip()
    if not answer:
        return None
    try:
        new_cap = float(answer)
    except ValueError:
        new_cap = float("nan")
    if not new_cap > cfg.budget_cap_usd:  # 也挡住 nan
        print(f"  新上限必须是大于 ${cfg.budget_cap_usd:.2f} 的数字;按停在这里处理。")
        return None
    return new_cap


def _probe_with_budget_choice(
    args: argparse.Namespace,
    cfg: FrozenConfig,
    raw: RawStore,
    adapter: ProviderAdapter,
    *,
    preconfirmed: bool,
) -> tuple[str, FrozenConfig, BudgetStop | None]:
    """探测一个表面。预算到顶时由用户决定:调高上限接着跑,或停在这里(返回这次的 BudgetStop)。"""
    run_id: str | None = None
    while True:
        try:
            run_id = _cmd_probe(
                args, cfg, raw, adapter=adapter, preconfirmed=preconfirmed, run_id=run_id
            )
            return run_id, cfg, None
        except BudgetStop as stop:
            print(f"[预算到顶] {stop}", file=sys.stderr)
            new_cap = _ask_new_cap(args, cfg, stop)
            if new_cap is None:
                return stop.run_id, cfg, stop
            # 只改预算上限:测量基线不变,同一个 run_id 接着问(配置锁放行)
            cfg = FrozenConfig.model_validate(cfg.model_dump() | {"budget_cap_usd": new_cap})
            run_id, preconfirmed = stop.run_id, True  # 用户刚亲手给了新上限 = 已确认继续


def _cmd_extract(run_id: str, cfg: FrozenConfig, raw: RawStore, analysis: AnalysisStore) -> None:
    responses = raw.read_responses(run_id)
    path = analysis.write_extraction(extract(responses, cfg))
    print(f"extract 完成: {path}")


def _cmd_verify(
    args: argparse.Namespace,
    run_id: str,
    cfg: FrozenConfig,
    analysis: AnalysisStore,
    evidence_store: EvidenceStore,
) -> None:
    extraction = analysis.read_extraction(run_id)
    checker: UrlChecker
    if args.check == "fetch":
        brands = {b.name: (b.name, *b.aliases) for b in cfg.peers}  # 只用清晰名,不含歧义别名
        checker = PageFetchChecker(brands=brands)
    elif args.check == "head":
        checker = HttpHeadChecker()
    else:
        checker = OfflineChecker()
    cache = None if args.refresh else evidence_store.load()  # --refresh:忽略缓存,强制重核
    new_records = verify(extraction, cfg, checker=checker, now=utc_now(), cache=cache)
    evidence_store.save(new_records)
    suffix = "(强制重核)" if args.refresh else ""
    print(f"verify 完成: 新核验 {len(new_records)} 条 URL(方式: {checker.method}){suffix}")


def _cmd_score(
    run_id: str,
    cfg: FrozenConfig,
    raw: RawStore,
    analysis: AnalysisStore,
    evidence_store: EvidenceStore,
    *,
    partial_manifest: RunManifest | None = None,
) -> None:
    """partial_manifest:这轮没跑完时现算的清单(未入原始层)—— 出的是带标记的阶段性结果。"""
    manifest = partial_manifest or next(
        (m for m in raw.read_manifests() if m.run_id == run_id), None
    )
    if manifest is None:
        raise SystemExit(
            f"run_id={run_id} 还没有运行清单(这轮探测没跑完,或 run_id 写错了)。"
            f"用相同配置执行 aivis probe --run-id {run_id} 跑完后再打分。"
        )
    bundle = score(
        analysis.read_extraction(run_id),
        evidence_store.load(),
        manifest,
        cfg,
        generated_at=utc_now(),
        partial=partial_manifest is not None,
    )
    path = analysis.write_bundle(bundle)
    print(f"score 完成: {path}  健康灯={bundle.health.light}")


def _cmd_report(run_id: str, cfg: FrozenConfig, analysis: AnalysisStore) -> None:
    bundle = analysis.read_bundle(run_id)
    md_path = analysis.write_report_file(run_id, "report.md", render_markdown(bundle, cfg))
    csv_path = analysis.write_report_file(run_id, "metrics.csv", render_csv(bundle))
    html_path = analysis.write_report_file(run_id, "report.html", render_html(bundle, cfg))
    print(f"REPORT {html_path}", flush=True)  # 机器可解析(GUI 据此列出各表面报告)
    print(f"report 完成: {md_path}\n           {csv_path}\n           {html_path}")


def _cmd_init(args: argparse.Namespace) -> int:
    if not (args.name and args.focal_name and args.focal_domain):
        print(
            "[init] 需要: <name> --focal-name <品牌全名> --focal-domain <域名> [--category <品类>]",
            file=sys.stderr,
        )
        return 2
    files = build_scaffold(
        name=args.name,
        focal_name=args.focal_name,
        focal_domain=args.focal_domain,
        category=args.category,
        date_tag=datetime.now(UTC).strftime("%Y.%m"),
    )
    target = args.out / args.name
    try:
        write_scaffold(target, files)
    except FileExistsError as e:
        print(f"[init] {e}", file=sys.stderr)
        return 2
    print(f"init 完成: {target}")
    print(f"  下一步: 填 TODO 题(前 20 题别点名品牌)→ aivis validate --config-dir {target}")
    return 0


def _cmd_estimate(args: argparse.Namespace, cfg: FrozenConfig) -> int:
    """开跑前的预算估计:零成本、不联网、不调 API;多表面时逐个列出并合计(每表面各自受上限约束)。"""
    adapters = _build_adapters(args)
    if adapters is None:
        return 2
    n_calls = len(cfg.questions) * cfg.n_runs
    print(f"[预算估计] {args.config_dir}")
    print(f"  调用数: {len(cfg.questions)} 题 × {cfg.n_runs} 轮 = {n_calls} 次(每个表面)")
    print(f"  预算上限 budget_cap_usd(每个表面各自受限): ${cfg.budget_cap_usd:.2f}")
    total = 0.0
    for adapter in adapters:
        est = adapter.estimate_cost_usd(n_calls, cfg)
        total += est
        flag = "  [!] 超上限,会被开跑前闸门阻断" if est > cfg.budget_cap_usd else "  [OK]"
        print(
            f"  - {adapter.provider_name}:{adapter.model_name}  预估(保守上界) ≈ ${est:.2f}{flag}"
        )
    print(f"  合计(保守上界): ${total:.2f}")
    print("  注: 真实通常低于上界; 运行中按真实花费硬封顶。")
    if all(a.provider_name == "mock" for a in adapters):
        print("  (当前 mock 估为 0;看真实价请加 --provider/--model 或 --surfaces,见 aivis models)")
    return 0


def _cmd_models(args: argparse.Namespace, cfg: FrozenConfig) -> int:
    """列出全部可测表面(模型目录)+ 按当前配置的预估成本(零成本、不联网)。"""
    n_calls = len(cfg.questions) * cfg.n_runs
    print(f"[模型目录] {len(catalog.MODELS)} 个表面;预估按当前配置 {n_calls} 次调用/表面(保守上界)")
    for provider, pinfo in catalog.PROVIDERS.items():
        key = pinfo.env_key or "(不需要)"
        print(
            f"\n{pinfo.label}  --provider {provider}  密钥环境变量: {key}  只发往: {pinfo.endpoint}"
        )
        for m in catalog.list_models(provider):
            adapter = registry.make_adapter(SurfaceSpec(provider=provider, model=m.model))
            est = adapter.estimate_cost_usd(n_calls, cfg)
            flags = []
            if not m.supports_temperature:
                flags.append("no-temperature")
            if not m.supports_region:
                flags.append("no-region")
            if not m.pinned:
                flags.append("unpinned")
            if m.surface_kind == "search_only":
                flags.append("search-only")
            print(
                f"  {m.model:<26} {m.label:<36} {m.tier:<9} {m.status:<8} "
                f"≈ ${est:>7.2f}  {' '.join(flags)}"
            )
    return 0


def _cmd_run(
    args: argparse.Namespace,
    cfg: FrozenConfig,
    raw: RawStore,
    analysis: AnalysisStore,
    evidence_store: EvidenceStore,
) -> int:
    """run:单表面 = 五段依次全跑;多表面(--surfaces)= 每个表面独立 run_id 各跑一遍,
    最后出并列对比报告(只并列、不混总分)+ 账单。开跑前查齐密钥、列出各表面预估与合计并确认一次。"""
    adapters = _build_adapters(args)
    if adapters is None:
        return 2
    multi = args.surfaces is not None
    if args.run_id and len(adapters) > 1:
        print(
            "[参数错误] 一次跑多个表面时不支持 --run-id 续跑(每个表面独立 run_id);"
            "续跑请一次只指定一个表面",
            file=sys.stderr,
        )
        return 2
    missing = registry.missing_env_keys(a.provider_name for a in adapters)
    if missing:
        print(
            f"[引擎错误] 缺少密钥环境变量: {', '.join(missing)}"
            "(密钥只经环境变量读取,不写代码/配置;GUI 密钥框会自动注入)",
            file=sys.stderr,
        )
        return 4
    if multi:
        n_calls = len(cfg.questions) * cfg.n_runs
        ests = [(a, a.estimate_cost_usd(n_calls, cfg)) for a in adapters]
        total = sum(e for _, e in ests)
        print(
            f"[多表面运行] {len(adapters)} 个表面,每个 {n_calls} 次调用;"
            f"预算上限每表面 ${cfg.budget_cap_usd:.2f}"
        )
        for a, e in ests:
            flag = "  [!] 超上限,该表面会被闸门阻断" if e > cfg.budget_cap_usd else ""
            print(f"  - {a.provider_name}:{a.model_name}  ≈ ${e:.2f}{flag}")
        print(f"  合计(保守上界) ≈ ${total:.2f}")
        if any(e > cfg.budget_cap_usd for _, e in ests):
            # 不许"前面的表面花完钱,轮到超上限的那个才整批中止":开跑前就整批拦下,一分钱不花
            raise CostGateError(
                "上面标 [!] 的表面预估超过预算上限,本次一个表面都没有跑(没有花钱)。"
                "请调高 budget_cap_usd,或把它们从 --surfaces 里去掉后重试。"
            )
        if total > 0 and not args.yes:
            answer = input(f"以上合计预估 ${total:.2f},继续?[y/N] ")
            if answer.strip().lower() != "y":
                raise CostGateError("用户未确认多表面运行的合计预估,已取消。")
    bundles = []
    budget_stops = 0
    for i, adapter in enumerate(adapters, 1):
        label = f"{adapter.provider_name}:{adapter.model_name}"
        if multi:
            print(f"PROGRESS surface {i}/{len(adapters)} {label}", flush=True)
        # 每个表面各自受预算上限约束:一个表面到顶停下,不连累别的表面(它们照常跑)
        run_id, used_cfg, stop = _probe_with_budget_choice(
            args, cfg, raw, adapter, preconfirmed=multi
        )
        _cmd_extract(run_id, used_cfg, raw, analysis)
        _cmd_verify(args, run_id, used_cfg, analysis, evidence_store)
        _cmd_score(
            run_id,
            used_cfg,
            raw,
            analysis,
            evidence_store,
            partial_manifest=snapshot_manifest(adapter, used_cfg, raw, run_id) if stop else None,
        )
        _cmd_report(run_id, used_cfg, analysis)
        if stop is not None:
            budget_stops += 1
            # 机器可解析(GUI 据此提示"看阶段性报告 / 调高上限接着跑")
            print(
                f"BUDGET_STOP {label} {run_id} {stop.done_calls}/{stop.total_calls} "
                f"{stop.spent_usd:.4f}",
                flush=True,
            )
            print(
                f"  上面是阶段性报告(只覆盖已问到的 {stop.done_calls}/{stop.total_calls} 次)。"
                f"要跑完:调高 budget_cap_usd 后执行 aivis run --provider {adapter.provider_name} "
                f"--model {adapter.model_name} --run-id {run_id}"
            )
        bundles.append(analysis.read_bundle(run_id))
    if multi:
        stamp = utc_now().replace("-", "").replace(":", "")
        cid = f"{stamp}-{bundles[0].run_id[:8]}"
        md = analysis.write_comparison(cid, "comparison.md", render_comparison_markdown(bundles))
        page = analysis.write_comparison(cid, "comparison.html", render_comparison_html(bundles))
        print("账单(真实累计花费,按各表面清单):")
        spent = 0.0
        for b in bundles:
            cost = b.cost_usd_actual or 0.0
            spent += cost
            print(f"BILL {b.provider}:{b.model} {cost:.4f}", flush=True)
        print(f"  合计 ${spent:.4f}")
        print(f"COMPARISON {page}", flush=True)
        print(f"对比报告: {md}\n          {page}")
    return EXIT_BUDGET_STOP if budget_stops else 0


def _cmd_validate(args: argparse.Namespace, cfg: FrozenConfig) -> int:
    violations = validate_config(cfg, phase=args.phase)
    print(f"[校验] {args.config_dir}  phase={args.phase}")
    if not violations:
        print("  OK 无问题")
        return 0
    for v in violations:
        mark = "ERROR" if v.severity == "error" else "WARN "
        print(f"  [{mark}] {v.rule}  ({v.where}) {v.message}")
    n_err = sum(1 for v in violations if v.severity == "error")
    print(f"  结论: {n_err} 个硬错, {len(violations) - n_err} 个告警")
    return 5 if has_errors(violations) else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "init":  # 生成配置,此时还没有配置可加载
        return _cmd_init(args)
    if args.command == "gui":  # 本地向导:自己收集输入,不需要预存配置
        from aivis.gui.server import serve

        serve(port=args.port, open_browser=not args.no_open)
        return 0
    try:  # 缺文件/坏 YAML/形状不对 → 干净报错,不吐裸 traceback
        cfg = load_frozen_config(args.config_dir)
        if args.sequential and not cfg.sequential:  # CLI 开关覆盖:序贯采样(n_runs 作上限)
            cfg = FrozenConfig.model_validate(cfg.model_dump() | {"sequential": True})
    except (FileNotFoundError, ValueError, ValidationError) as e:
        print(f"[配置错误] 无法加载 {args.config_dir}: {e}", file=sys.stderr)
        return 2
    if args.command == "validate":  # 纯质检,不需要任何 store
        return _cmd_validate(args, cfg)
    if args.command == "estimate":  # 预算估计,零成本不联网
        return _cmd_estimate(args, cfg)
    if args.command == "models":  # 模型目录 + 预估,零成本不联网
        return _cmd_models(args, cfg)
    raw = RawStore(args.data_dir / "raw")
    evidence_store = EvidenceStore(args.data_dir / "evidence")
    analysis = AnalysisStore(args.data_dir / "analysis")

    try:
        if args.command == "probe":
            _cmd_probe(args, cfg, raw)
        elif args.command == "extract":
            _cmd_extract(_resolve_run_id(args, raw), cfg, raw, analysis)
        elif args.command == "verify":
            _cmd_verify(args, _resolve_run_id(args, raw), cfg, analysis, evidence_store)
        elif args.command == "score":
            _cmd_score(_resolve_run_id(args, raw), cfg, raw, analysis, evidence_store)
        elif args.command == "report":
            _cmd_report(_resolve_run_id(args, raw), cfg, analysis)
        else:  # run:单表面五段全跑 / 多表面串跑 + 并列对比
            return _cmd_run(args, cfg, raw, analysis, evidence_store)
    except CostGateError as e:
        print(f"[成本闸门] {e}", file=sys.stderr)
        return 2
    except RunLockError as e:
        print(f"[配置锁] {e}", file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(f"[未实现] {e}", file=sys.stderr)
        return 3
    except FatalProviderError as e:
        print(f"[引擎错误] {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
