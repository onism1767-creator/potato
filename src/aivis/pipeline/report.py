"""report —— 渲染层公共入口:Markdown / CSV(本文件)+ HTML(report_html,再导出)。

文件写入由 CLI 经 AnalysisStore 完成(render 纯 / write IO 分离,便于黄金快照测试)。
叙事红线(CLAUDE.md §0):头部强标"仅代表此引擎";只描述观测,不给行动保证。
拆分(可维护性):叶子件(常量/格式化/SVG)在 report_assets.py;HTML 组装在 report_html.py;
公共 import 路径不变(`from aivis.pipeline.report import render_markdown/render_csv/render_html`)。
"""

import csv
import io

from aivis.contracts.checker import CHECK_PAGE_FETCH
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import Interval, ScoreBundle, Tier
from aivis.pipeline.report_assets import (
    _LIGHT_LABEL,
    _MAX_EVIDENCE_ROWS,
    _TIER_BASIS_NOTE,
    _TIER_RANK,
    _TIER_SYMBOL,
    _fmt,
    _fmt_optional,
    _pct,
)
from aivis.pipeline.report_html import render_html

__all__ = ["render_csv", "render_html", "render_markdown"]


def _ranking_verdict(bundle: ScoreBundle) -> str:
    """F-005:显式排名可信度判语——区间重叠/样本不足/失败率高 → 拒绝强行排序。"""
    overlap = any("all-intervals-overlap" in f for f in bundle.health.flags)
    high_failure = bundle.health.light == "red" and bundle.n_responses_error > 0
    insufficient = bundle.n_runs < 3
    reasons = []
    if overlap:
        reasons.append("各品牌不确定区间重叠")
    if insufficient:
        reasons.append(f"采样不足(n_runs={bundle.n_runs}<3)")
    if high_failure:
        reasons.append(f"存在失败({bundle.n_responses_error} 条)")
    if reasons:
        return (
            f"> 🔴 **排名无法区分(not distinguishable)**:{';'.join(reasons)}。"
            "下表数字仅供观测,**不构成确定排序**。"
        )
    return "> 🟢 区间可区分:在本口径下可作谨慎排序(仍仅代表此引擎)。"


def render_markdown(bundle: ScoreBundle, cfg: FrozenConfig) -> str:
    """渲染 Markdown 报告:健康灯 / 测量表面 / 在场矩阵 / 六指标 / 证据列表 / 透明块 / 局限。"""
    lines: list[str] = []
    out = lines.append

    out("# Claude API web-search 可见度报告(代理测量)")
    out("")
    out(f"> **{bundle.engine_note}**")
    out("")

    # ---- 健康灯(总体 = 最差指标) ----
    h = bundle.health
    out(f"## 健康灯:{_LIGHT_LABEL[h.light]}")
    out("")
    out(f"- 引用有效性率:{_pct(h.citation_validity_rate)}")
    out(f"- 歧义/搁置比例:{_pct(h.ambiguous_ratio)}")
    out(f"- 失败率:{_pct(h.failure_rate)}")
    if h.flags:
        out(f"- 标记:{', '.join(h.flags)}")
    out("")

    # ---- 测量表面 ----
    out("## 测量表面(本结果仅在此口径下成立)")
    out("")
    out("| 项 | 值 |")
    out("|---|---|")
    out(f"| provider / surface | {bundle.provider} / {bundle.surface_id} |")
    out(f"| model(档位) | {bundle.model}({bundle.model_tier}) |")
    out(f"| stratum(区域\\|语言\\|档位) | {bundle.stratum} |")
    versions = f"{bundle.prompt_version} / {bundle.competitor_set_version or '—'}"
    out(f"| 题库版本 / 竞品集版本 | {versions} |")
    out(f"| 配置指纹 | `{bundle.config_fingerprint[:16]}…` |")
    out(f"| 证据口径 | {bundle.tier_basis}(只有 verified 进核心评分) |")
    out(
        f"| 观测量 | {bundle.n_questions} 题 × {bundle.n_runs} 轮 = "
        f"{bundle.n_responses_ok} 成功 + {bundle.n_responses_error} 失败 |"
    )
    out(f"| 运行 / 生成时刻 | `{bundle.run_id}` / {bundle.generated_at} |")
    out("")

    # ---- 视图①:问题 × 品牌在场矩阵 ----
    out("## 在场矩阵(每格 = 该题各轮中达到的最高证据档)")
    out("")
    out("图例:✔ verified ◐ cited ○ mentioned(仅文本) · 无")
    out("")
    brand_names = [m.brand for m in bundle.brands]
    question_ids = sorted({t.question_id for t in bundle.tiers})
    best: dict[tuple[str, str], Tier] = {}
    for t in bundle.tiers:
        key = (t.question_id, t.brand)
        if key not in best or _TIER_RANK[t.tier] > _TIER_RANK[best[key]]:
            best[key] = t.tier
    question_text = {q.id: q.text for q in cfg.questions}
    out("| 问题 | " + " | ".join(brand_names) + " |")
    out("|---" * (len(brand_names) + 1) + "|")
    for qid in question_ids:
        text = question_text.get(qid, "")
        label = f"`{qid}` {text[:40]}{'…' if len(text) > 40 else ''}"
        cells = [_TIER_SYMBOL[best.get((qid, b), "none")] for b in brand_names]
        out(f"| {label} | " + " | ".join(cells) + " |")
    out("")

    # ---- 视图②:STRICT 六指标(默认排名口径,只算品牌清晰名) ----
    out("## 六指标 · STRICT(默认排名口径:只算品牌清晰名)")
    out("")
    out(_ranking_verdict(bundle))  # F-005:显式"无法区分"判语
    out("")
    out(
        "| 品牌 | 提及覆盖率 | 自有域引用率* | earned 引用率* "
        "| 位置显著度 | 提及 SOV | 引用 SOV | 稳定性 |"
    )
    out("|---|---|---|---|---|---|---|---|")
    for m in bundle.brands:
        out(
            f"| {m.brand} | {_fmt(m.mention_coverage)} | {_fmt(m.owned_citation_rate)} | "
            f"{_fmt(m.earned_citation_rate)} | {_fmt(m.prominence)} | "
            f"{_fmt_optional(m.mention_sov)} | {_fmt_optional(m.citation_sov)} | "
            f"{_fmt(m.stability)} |"
        )
    out("")
    out(
        f"\\* 引用率只计 **verified** 档(口径 `{bundle.tier_basis}`,见下方说明);"
        "位置显著度为 range 区间,非统计区间。"
    )
    out("")

    # ---- 视图②b:EXPANDED 诊断(含疑似缩写/歧义;仅供发现,不作排名依据,F-004) ----
    if any(m.ambiguous_mention_count > 0 for m in bundle.brands):
        out("## 提及覆盖 · EXPANDED(诊断口径:额外含疑似缩写/歧义提及)")
        out("")
        out("> ⚠️ **仅供发现/诊断,不作排名依据。** 含被 strict 搁置的疑似缩写/歧义提及;")
        out("> 这些证据较弱,**绝不**与上方 strict 排名混算。")
        out("")
        out("| 品牌 | strict 覆盖率 | expanded 覆盖率 | 其中疑似缩写/歧义(搁置数) |")
        out("|---|---|---|---|")
        for m in bundle.brands:
            out(
                f"| {m.brand} | {_fmt(m.mention_coverage)} | "
                f"{_fmt(m.mention_coverage_expanded)} | {m.ambiguous_mention_count} |"
            )
        out("")

    # ---- 视图③:证据列表(含子状态:可达 / 页面提及品牌 / 核验口径,F-006a) ----
    out("## 证据列表(被引 URL 的核验事实)")
    out("")
    out(f"> 核验口径 `{bundle.tier_basis}`:{_TIER_BASIS_NOTE.get(bundle.tier_basis, '混合口径')}")
    out("")
    out("| 域名 | owned/earned | 可达 | 页面提及品牌 | 核验方式 | URL |")
    out("|---|---|---|---|---|---|")
    for e in bundle.evidence[:_MAX_EVIDENCE_ROWS]:
        owner = f"owned({e.owner_brand})" if e.owned_or_earned == "owned" else "earned"
        reach = "✓" if e.reachable else "✗"
        if e.check_method == CHECK_PAGE_FETCH:
            mentions = ", ".join(e.brands_on_page) if e.brands_on_page else "✗(未提及)"
        else:
            mentions = "—(未抓正文)"
        out(
            f"| {e.canonical_domain} | {owner} | {reach} | {mentions} | "
            f"{e.check_method} | {e.normalized_url} |"
        )
    if len(bundle.evidence) > _MAX_EVIDENCE_ROWS:
        out(f"| … | | | | | 共 {len(bundle.evidence)} 条,余略 |")
    out("")

    # ---- 透明块 ----
    out("## 工具做了什么(透明块)")
    out("")
    out(
        f"1. 用冻结题库(版本 {bundle.prompt_version})对 {bundle.provider} 的 "
        f"{bundle.surface_id} 做 clean-room 探测:{bundle.n_questions} 题 × {bundle.n_runs} 轮;"
    )
    out("2. 用确定性规则抽取品牌提及(保守计数:歧义/缩写记录但不计分)与引用归属;")
    out(f"3. 核验被引 URL(规范化 + 可达性 + 来源分类;本版口径 {bundle.tier_basis});")
    out("4. 用 Wilson 区间计算六指标;全程无 AI 判读,所有数字可回溯到原始回答。")
    out("")

    # ---- 局限 ----
    out("## 局限性")
    out("")
    out('- 代理测量:仅代表此引擎此配置此时间窗口,不是"AI 排名真相";')
    out("- 保守计数会低估(缩写/歧义被搁置,见健康灯的歧义比例);")
    out(
        f"- 本次 verified 口径 = {bundle.tier_basis}"
        f"({_TIER_BASIS_NOTE.get(bundle.tier_basis, '混合口径')});"
        "口径随核验方式动态分级,page-fetch 为最强;"
    )
    if any(e.check_method == "offline-assumed" for e in bundle.evidence):
        out("- **本次运行为离线核验(offline-assumed):可达性是假设值,非真实检查;**")
    out(f"- 固定 {bundle.n_runs} 轮采样;序贯采样(run_until_stable)为后续版本;")
    out('- 本报告只呈现观测与线索,不构成"照做就能提升 AI 提及"的保证。')
    out("")
    return "\n".join(lines)


def render_csv(bundle: ScoreBundle) -> str:
    """渲染指标 CSV(品牌 × 指标的长表,带区间与口径)。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["run_id", "stratum", "brand", "metric", "point", "low", "high", "n", "method"])

    def _row(brand: str, metric: str, interval: Interval | None) -> None:
        if interval is None:
            return
        writer.writerow(
            [
                bundle.run_id,
                bundle.stratum,
                brand,
                metric,
                f"{interval.point:.6f}",
                f"{interval.low:.6f}",
                f"{interval.high:.6f}",
                interval.n,
                interval.method,
            ]
        )

    for m in bundle.brands:
        _row(m.brand, "mention_coverage", m.mention_coverage)
        _row(m.brand, "owned_citation_rate", m.owned_citation_rate)
        _row(m.brand, "earned_citation_rate", m.earned_citation_rate)
        _row(m.brand, "prominence", m.prominence)
        _row(m.brand, "mention_sov", m.mention_sov)
        _row(m.brand, "citation_sov", m.citation_sov)
        _row(m.brand, "stability", m.stability)
    return buf.getvalue()
