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
from aivis.contracts.adapter import ProviderAdapter
from aivis.contracts.checker import UrlChecker
from aivis.contracts.config import FrozenConfig
from aivis.pipeline.extract import extract
from aivis.pipeline.probe import CostGateError, RunLockError, run_probe, utc_now
from aivis.pipeline.report import render_csv, render_html, render_markdown
from aivis.pipeline.score import score
from aivis.pipeline.verify import (
    HttpHeadChecker,
    OfflineChecker,
    PageFetchChecker,
    verify,
)
from aivis.providers.anthropic import AnthropicAdapter, FatalProviderError
from aivis.providers.mock import MockAdapter
from aivis.scaffold import build_scaffold, write_scaffold
from aivis.storage.analysis import AnalysisStore
from aivis.storage.evidence import EvidenceStore
from aivis.storage.raw import RawStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aivis",
        description="Claude API web-search 可见度(代理测量)——可复现的提及/引用测量框架",
    )
    parser.add_argument("--config-dir", type=Path, default=Path("configs/demo"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--provider",
        choices=["mock", "anthropic"],
        default="mock",
        help="默认 mock(零成本);anthropic 需 ANTHROPIC_API_KEY 且过成本闸门",
    )
    parser.add_argument(
        "--tier", default="standard", help="模型档位:economy(Haiku)/ standard(Sonnet)/ high(Opus)"
    )
    parser.add_argument("--run-id", default=None, help="指定运行 ID(默认:probe 新建/其余取最新)")
    parser.add_argument("--yes", action="store_true", help="非交互确认成本闸门")
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


def _make_adapter(args: argparse.Namespace) -> ProviderAdapter:
    if args.provider == "anthropic":
        return AnthropicAdapter(tier=args.tier)
    return MockAdapter(tier=args.tier)


def _resolve_run_id(args: argparse.Namespace, raw: RawStore) -> str:
    if args.run_id:
        return str(args.run_id)
    manifests = raw.read_manifests()
    if not manifests:
        raise SystemExit("原始层还没有任何运行,请先执行: aivis probe")
    return manifests[-1].run_id


def _cmd_probe(args: argparse.Namespace, cfg: FrozenConfig, raw: RawStore) -> str:
    def confirm(cost: float, n_calls: int) -> bool:
        if args.yes:
            return True
        answer = input(f"预估成本 ${cost:.2f}({n_calls} 次真实调用),继续?[y/N] ")
        return answer.strip().lower() == "y"

    def progress(done: int, total: int, spent: float) -> None:
        # 机器可解析进度行(GUI runner 据此画进度条/估剩余时间/累计花费);flush 保证管道下实时
        print(f"PROGRESS probe {done}/{total} {spent:.4f}", flush=True)

    manifest = run_probe(
        _make_adapter(args), cfg, raw, run_id=args.run_id, confirm_cost=confirm, progress=progress
    )
    print(f"probe 完成: run_id={manifest.run_id}  search_count={manifest.search_count}")
    return manifest.run_id


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
) -> None:
    manifest = next(m for m in raw.read_manifests() if m.run_id == run_id)
    bundle = score(
        analysis.read_extraction(run_id),
        evidence_store.load(),
        manifest,
        cfg,
        generated_at=utc_now(),
    )
    path = analysis.write_bundle(bundle)
    print(f"score 完成: {path}  健康灯={bundle.health.light}")


def _cmd_report(run_id: str, cfg: FrozenConfig, analysis: AnalysisStore) -> None:
    bundle = analysis.read_bundle(run_id)
    md_path = analysis.write_report_file(run_id, "report.md", render_markdown(bundle, cfg))
    csv_path = analysis.write_report_file(run_id, "metrics.csv", render_csv(bundle))
    html_path = analysis.write_report_file(run_id, "report.html", render_html(bundle, cfg))
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
    """开跑前的预算估计:零成本、不联网、不调 API,只算保守上界并对比预算上限。"""
    adapter = _make_adapter(args)
    n_calls = len(cfg.questions) * cfg.n_runs
    est = adapter.estimate_cost_usd(n_calls, cfg)
    print(f"[预算估计] {args.config_dir}")
    print(f"  provider/model: {adapter.provider_name} / {adapter.model_name}")
    print(f"  调用数: {len(cfg.questions)} 题 × {cfg.n_runs} 轮 = {n_calls} 次")
    print(f"  预估成本(保守上界): ${est:.2f}")
    print(f"  预算上限 budget_cap_usd: ${cfg.budget_cap_usd:.2f}")
    if est > cfg.budget_cap_usd:
        print(f"  [!] 超上限 ${est - cfg.budget_cap_usd:.2f} —— 此运行会被开跑前闸门阻断")
    else:
        print(f"  [OK] 在预算内(余量 ${cfg.budget_cap_usd - est:.2f})")
    print("  注: 真实通常低于上界; 运行中按真实花费硬封顶。")
    if args.provider == "mock":
        print(
            "  (当前 mock 估为 0;看真实价请加 --provider anthropic [--tier economy|standard|high])"
        )
    return 0


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
        else:  # run:五段依次全跑
            run_id = _cmd_probe(args, cfg, raw)
            _cmd_extract(run_id, cfg, raw, analysis)
            _cmd_verify(args, run_id, cfg, analysis, evidence_store)
            _cmd_score(run_id, cfg, raw, analysis, evidence_store)
            _cmd_report(run_id, cfg, analysis)
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
