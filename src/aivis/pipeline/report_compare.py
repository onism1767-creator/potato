"""report_compare —— 多表面(多引擎)并列对比报告(渲染纯函数;Markdown + 单文件 HTML)。

🟥 叙事红线(CLAUDE.md §0/§7):各表面**只并列、不混总分、不跨引擎排名**;每格数字都带 Wilson 区间;
每个表面仅代表其自身引擎与配置。search_only 表面(纯搜索结果)提及类指标标"不适用"。
账单 = 各表面清单里的真实累计花费(cost_usd_actual)之和。
"""

import html

from aivis.contracts.records import Interval, ScoreBundle
from aivis.pipeline.report_assets import _HTML_CSS, _LIGHT_LABEL, _fmt

_NOTE_ZH = (
    "各表面仅代表其自身引擎与配置(represents that engine only):"
    "本页**只并列、不混总分、不跨引擎排名**;每个数字为 Wilson 95% 区间。"
)
_NOTE_EN = (
    "Each surface represents that engine and configuration only. Columns are placed side by side: "
    "no combined score, no cross-engine ranking. Every number is a Wilson 95% interval."
)
_NA = "不适用(n/a)"


def _label(b: ScoreBundle) -> str:
    return f"{b.provider}:{b.model}"


def _cell(b: ScoreBundle, iv: Interval, *, mention: bool) -> str:
    if mention and b.surface_kind == "search_only":
        return _NA
    return _fmt(iv)


def _cost(b: ScoreBundle) -> str:
    return "—" if b.cost_usd_actual is None else f"${b.cost_usd_actual:.4f}"


def _total_cost(bundles: list[ScoreBundle]) -> float:
    return sum(b.cost_usd_actual or 0.0 for b in bundles)


def _brand_names(bundles: list[ScoreBundle]) -> list[str]:
    names: list[str] = []
    for b in bundles:
        for m in b.brands:
            if m.brand not in names:
                names.append(m.brand)
    return names


def _metric(b: ScoreBundle, brand: str, field: str) -> Interval | None:
    for m in b.brands:
        if m.brand == brand:
            iv = getattr(m, field)
            return iv if isinstance(iv, Interval) else None
    return None


def _fingerprint_warning(bundles: list[ScoreBundle]) -> str | None:
    fps = {b.config_fingerprint for b in bundles}
    if len(fps) <= 1:
        return None
    return "⚠️ 各表面的配置指纹不一致(题库/竞品集/参数不同),并列数字不可直接对照。"


def _yesno(v: bool | None) -> str:
    return "—" if v is None else ("✓" if v else "✗")


# ---------------------------------------------------------------- Markdown


def render_comparison_markdown(bundles: list[ScoreBundle]) -> str:
    """并列对比(Markdown):表面清单与账单 / 提及覆盖率 / 自有域引用率 / earned 引用率。"""
    if not bundles:
        raise ValueError("至少需要一个表面")
    lines: list[str] = []
    out = lines.append
    out("# 多引擎并列对比 · AI web-search 可见度(代理测量)")
    out("")
    out(f"> **{_NOTE_ZH}**")
    out("")
    warn = _fingerprint_warning(bundles)
    if warn:
        out(f"> {warn}")
        out("")

    out("## 表面清单与账单")
    out("")
    out(
        "| # | 表面(provider:model) | surface_id | 表面类型 | 成功/失败 | 健康灯 | 证据口径 "
        "| 温度生效 | 区域生效 | 实际花费 |"
    )
    out("|---|---|---|---|---|---|---|---|---|---|")
    for i, b in enumerate(bundles, 1):
        out(
            f"| {i} | {_label(b)} | {b.surface_id} | {b.surface_kind} | "
            f"{b.n_responses_ok}/{b.n_responses_error} | {_LIGHT_LABEL[b.health.light]} | "
            f"{b.tier_basis} | {_yesno(b.temperature_applied)} | {_yesno(b.region_applied)} | "
            f"{_cost(b)} |"
        )
    out(f"| **合计** | | | | | | | | | **${_total_cost(bundles):.4f}** |")
    out("")
    out(
        "温度/区域生效 = 该引擎是否真的接受了配置里钉住的采样温度 / 区域参数(✗ = 钉不住,如实标注)。"
    )
    out("")

    brands = _brand_names(bundles)
    header = "| 品牌 | " + " | ".join(_label(b) for b in bundles) + " |"
    sep = "|---" * (len(bundles) + 1) + "|"
    for title, field, is_mention in (
        ("提及覆盖率(strict,只算清晰名)", "mention_coverage", True),
        ("自有域引用率(verified)", "owned_citation_rate", False),
        ("earned 引用率(verified)", "earned_citation_rate", False),
    ):
        out(f"## {title}")
        out("")
        out(header)
        out(sep)
        for brand in brands:
            cells = []
            for b in bundles:
                iv = _metric(b, brand, field)
                cells.append("—" if iv is None else _cell(b, iv, mention=is_mention))
            out(f"| {brand} | " + " | ".join(cells) + " |")
        out("")
    out("## 引擎备注")
    out("")
    for b in bundles:
        out(f"- **{_label(b)}**:{b.engine_note}")
    out("")
    out('> 本页只呈现观测,不构成跨引擎排名,也不构成"照做就能提升 AI 提及"的保证。')
    out("")
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML


def render_comparison_html(bundles: list[ScoreBundle]) -> str:
    """并列对比(单文件离线 HTML,零外链;所有外部数据 HTML 转义)。"""
    if not bundles:
        raise ValueError("至少需要一个表面")
    e = html.escape
    p: list[str] = []
    p.append('<div class="wrap">')
    p.append(
        '<div class="titlebar"><span style="font-size:15px;font-weight:600;">aivis</span>'
        '<span style="font-size:20px;font-weight:600;">Multi-engine comparison</span></div>'
    )
    p.append(
        '<div class="note" style="margin:12px 0 20px;"><span style="font-size:12.5px;'
        f'color:#4A6B23;line-height:1.5;">{e(_NOTE_EN)}</span></div>'
    )
    warn = _fingerprint_warning(bundles)
    if warn:
        p.append(f'<div class="note" style="margin-bottom:16px;color:#C0392B;">{e(warn)}</div>')

    p.append('<h2 style="font-size:16px;margin:18px 0 8px;">Surfaces &amp; bill</h2>')
    p.append('<table class="tbl"><thead><tr>')
    for h in (
        "#",
        "Surface",
        "surface_id",
        "Kind",
        "OK / failed",
        "Health",
        "Evidence basis",
        "Temp applied",
        "Region applied",
        "Actual cost",
    ):
        p.append(f"<th>{e(h)}</th>")
    p.append("</tr></thead><tbody>")
    for i, b in enumerate(bundles, 1):
        p.append(
            f"<tr><td>{i}</td><td>{e(_label(b))}</td><td>{e(b.surface_id)}</td>"
            f"<td>{e(b.surface_kind)}</td><td>{b.n_responses_ok} / {b.n_responses_error}</td>"
            f"<td>{e(b.health.light)}</td><td>{e(b.tier_basis)}</td>"
            f"<td>{e(_yesno(b.temperature_applied))}</td><td>{e(_yesno(b.region_applied))}</td>"
            f"<td>{e(_cost(b))}</td></tr>"
        )
    p.append(
        f'<tr><td colspan="9"><b>Total</b></td><td><b>${_total_cost(bundles):.4f}</b></td></tr>'
    )
    p.append("</tbody></table>")

    brands = _brand_names(bundles)
    for title, field, is_mention in (
        ("Mention coverage (strict)", "mention_coverage", True),
        ("Owned citation rate (verified)", "owned_citation_rate", False),
        ("Earned citation rate (verified)", "earned_citation_rate", False),
    ):
        p.append(f'<h2 style="font-size:16px;margin:18px 0 8px;">{e(title)}</h2>')
        p.append('<table class="tbl"><thead><tr><th>Brand</th>')
        for b in bundles:
            p.append(f"<th>{e(_label(b))}</th>")
        p.append("</tr></thead><tbody>")
        for brand in brands:
            p.append(f"<tr><td>{e(brand)}</td>")
            for b in bundles:
                iv = _metric(b, brand, field)
                cell = "—" if iv is None else _cell(b, iv, mention=is_mention)
                p.append(f"<td>{e(cell)}</td>")
            p.append("</tr>")
        p.append("</tbody></table>")

    p.append('<h2 style="font-size:16px;margin:18px 0 8px;">Engine notes</h2><ul>')
    for b in bundles:
        p.append(f"<li><b>{e(_label(b))}</b>: {e(b.engine_note)}</li>")
    p.append("</ul>")
    p.append(
        '<div class="note" style="margin-top:20px;">Observations only: no cross-engine ranking, '
        "and no guarantee that any action will raise AI mentions.</div>"
    )
    p.append("</div>")
    body = "".join(p)
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>aivis · multi-engine comparison</title><style>{_HTML_CSS}"
        ".tbl{border-collapse:collapse;width:100%;font-size:12.5px;margin-bottom:8px;}"
        ".tbl th,.tbl td{border:1px solid #D9E4CF;padding:6px 8px;text-align:left;}"
        ".tbl th{background:#F1F8E8;font-weight:600;}"
        f"</style></head><body>{body}</body></html>"
    )
