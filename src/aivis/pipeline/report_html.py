"""report_html —— 单文件、离线、零外链的 HTML 报告(白底绿渐变 dashboard 模板,英文)。

公共入口 render_html 由 report.py 再导出(保持 `from aivis.pipeline.report import render_html`)。
所有外部/AI 数据(品牌名/问题/域名/URL)均 HTML 转义(防注入);叶子件(CSS/SVG/格式化)在
report_assets.py。数据全由 ScoreBundle 驱动(非静态)。
"""

import html

from aivis.contracts.checker import CHECK_PAGE_FETCH
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import HealthSummary, Metrics, ScoreBundle, Tier
from aivis.pipeline.report_assets import (
    _BRAND_RAMP,
    _HTML_CSS,
    _MAX_EVIDENCE_ROWS,
    _TIER_HTML,
    _TIER_RANK,
    _bar_svg,
    _donut_svg,
    _fmt_optional_pi,
    _help,
    _iv_int,
    _pi,
    _yesno,
)

# "怎么读这个指标"短引导(英文,与报告一致;静态文案)
_H_COVERAGE = "Share of answers that name this brand by a clear name. Higher = more visible."
_H_VALIDITY = "Share of cited URLs that were reachable. Low = many dead or invalid links."
_H_AMBIG = "Mentions parked as ambiguous (abbreviations) — recorded but not scored."
_H_FAILURE = "Errored probe calls. Overall health = worst of validity / ambiguity / failure."
_H_RANKING = "Bar = estimate, whisker = Wilson 95% CI. Overlapping bars = not distinguishable."
_H_SOV = "This brand's share of all mentions across the measured brand set."
_H_SUPPORT = (
    "Earned cite = answer has a verified third-party citation. "
    "SOV = share among measured brands. Stability = consistency across rounds (1.0 = identical)."
)
_H_MATRIX = "Highest evidence tier reached per question: verified > cited > mentioned."
_H_EVIDENCE = (
    "Per cited URL: owned vs earned, reachable, whether the page mentions the brand, method."
)

# API 去向主机:与 providers.catalog.PROVIDERS 的 endpoint 一致(test_report_surface_fields
# 断言两者相等);报告层不 import providers 包,以免拖入 SDK
_PROVIDER_HOST = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
    "perplexity": "api.perplexity.ai",
    "grok": "api.x.ai",
}
_SEARCH_ONLY_NOTE_EN = (
    "Search-only surface: this engine returns search results (title / URL / snippet) with no "
    "generated answer, so mention coverage, prominence, mention SOV and stability do not apply. "
    "Citation metrics = whether a brand appears in the result links."
)

# 健康灯按状态取语义色(与 GUI 一致:绿 / 琥珀 / 红),不再恒绿误导。
_HEALTH_LIGHT_COLOR = {"green": "#4E9A06", "yellow": "#B8860B", "red": "#C0392B"}
# 非绿时把驱动灯色的 flag 翻成人话,贴在灯旁(避免"红灯却看不出为什么")。静态文案。
_HEALTH_FLAG_REASON = {
    "citation-validity-red": "many cited links unreachable",
    "citation-validity-yellow": "some cited links unreachable",
    "ambiguous-red": "many mentions ambiguous",
    "ambiguous-yellow": "some mentions ambiguous",
    "failure-red": "many probe calls failed",
    "failure-yellow": "some probe calls failed",
    "all-intervals-overlap:ranking-indicative-only": "ranking not statistically distinguishable",
    "partial-run:budget-cap": "partial run — stopped at the budget cap",
}


def _health_reason(health: HealthSummary) -> str:
    """把驱动健康灯转黄/红的 flag 翻成简短英文原因(逗点分隔);无则空串。"""
    return " · ".join(_HEALTH_FLAG_REASON[f] for f in health.flags if f in _HEALTH_FLAG_REASON)


def render_html(bundle: ScoreBundle, cfg: FrozenConfig) -> str:
    """渲染单文件、离线、零外链的 HTML 报告(白底绿渐变 dashboard 模板)。

    所有外部/AI 数据(品牌名/问题/域名/URL)均 HTML 转义(防注入);无 <script>/外链/CDN。
    """
    e = html.escape
    p: list[str] = []
    ranked = sorted(bundle.brands, key=lambda m: m.mention_coverage.point, reverse=True)
    color_of = {m.brand: _BRAND_RAMP[min(i, len(_BRAND_RAMP) - 1)] for i, m in enumerate(ranked)}
    h = bundle.health

    p.append('<div class="wrap">')
    # 头部
    p.append(
        '<div class="titlebar"><span style="font-size:15px;font-weight:600;">'
        'aivis</span><span style="font-size:20px;font-weight:600;">'
        "AI Visibility Dashboard</span></div>"
    )
    p.append(
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        'margin-bottom:16px;"><div><div style="font-size:19px;font-weight:500;">'
        f"{e(bundle.provider)} · {e(bundle.model)}</div>"
        f'<div class="lbl" style="margin-top:3px;">Run {e(bundle.generated_at)} · '
        f"clean-room · {e(bundle.stratum)}</div></div>"
        f'<span class="pill" style="white-space:nowrap;">{e(bundle.prompt_version)}</span></div>'
    )
    p.append(
        '<div class="note" style="margin-bottom:20px;"><span style="font-size:12.5px;'
        'color:#4A6B23;line-height:1.5;">Represents this engine &amp; configuration only — '
        'a reproducible proxy measurement, not "AI ranking truth", '
        "and not a guarantee of improvement.</span></div>"
    )
    search_only = bundle.surface_kind == "search_only"
    if search_only:
        p.append(
            '<div class="note" style="margin-bottom:20px;color:#8A5A00;">'
            f"{e(_SEARCH_ONLY_NOTE_EN)}</div>"
        )
    # 这轮没跑完(预算到顶)就出的阶段性报告:必须在最显眼处说清,不能被当成完整基线
    if any(f.startswith("partial-run") for f in h.flags):
        asked = bundle.n_responses_ok + bundle.n_responses_error
        p.append(
            '<div class="note" style="margin-bottom:20px;color:#8A5A00;">'
            f"<b>Partial run.</b> This run stopped at the budget cap after {asked} of up to "
            f"{bundle.n_questions * bundle.n_runs} calls. Every number below covers only the "
            "questions asked so far (later questions may not have been asked at all), so it is "
            "not a complete baseline. Raise the cap and resume the same run to finish it.</div>"
        )
    # 离线核验:可达性与 verified 档是假设值,不能让 100% 看起来像真的查过
    offline = any(ev.check_method == "offline-assumed" for ev in bundle.evidence)
    if offline:
        p.append(
            '<div class="note" style="margin-bottom:20px;color:#8A5A00;">'
            "Cited links were <b>not fetched</b> in this run (offline mode): reachability, "
            "citation validity and the &ldquo;verified&rdquo; tier are <b>assumed</b>, not "
            "checked. Run with <code>--check fetch</code> to really fetch each cited page.</div>"
        )
    # 英雄 + 健康三卡
    hero = ranked[0]
    # 全部区间重叠 = 排名无法区分(§7.5):不许再写"第一名 / 领先几个点"这种假精确的话
    overlap = any("all-intervals-overlap" in f for f in h.flags)
    gap = ""
    if len(ranked) > 1:
        if overlap:
            gap = '<span class="lbl">not distinguishable from the others</span>'
        else:
            d = round((hero.mention_coverage.point - ranked[1].mention_coverage.point) * 100)
            gap = f'<span class="lbl">+{d}pt ahead of #2</span>'
    light_en = {"green": "green", "yellow": "yellow", "red": "red"}[h.light]
    if len(ranked) == 1:
        lbl_top = "Brand · mention coverage"
    elif search_only:
        lbl_top = "Mention coverage · not applicable"
    elif overlap:
        lbl_top = "Highest point estimate · mention coverage"
    else:
        lbl_top = "Top brand · mention coverage"
    validity_lbl = "Citation validity · assumed" if offline else "Citation validity"
    hreason = _health_reason(h)
    hreason_html = (
        f'<div style="font-size:11px;color:#7E8B76;margin-top:2px;'
        f'line-height:1.35;">{hreason}</div>'
        if hreason
        else ""
    )
    p.append(
        '<div class="g4" style="margin-bottom:20px;align-items:start;">'
        f'<div><div class="lbl">{lbl_top}</div>'
        f'<div class="big" style="font-size:30px;margin-top:6px;">{e(hero.brand)}</div>'
        f'<div style="display:flex;align-items:baseline;gap:8px;margin-top:2px;">'
        f'<span class="big">{"n/a" if search_only else _pi(hero.mention_coverage.point)}'
        f"</span>{'' if search_only else gap}</div>"
        f"{_help(_H_COVERAGE)}</div>"
        f'<div class="mc"><div class="lbl">{validity_lbl}</div>'
        f'<div class="big" style="margin-top:4px;">{_pi(h.citation_validity_rate)}</div>'
        f"{_help(_H_VALIDITY)}</div>"
        f'<div class="mc"><div class="lbl">Ambiguous / parked</div>'
        f'<div class="big" style="margin-top:4px;">{_pi(h.ambiguous_ratio)}</div>'
        f"{_help(_H_AMBIG)}</div>"
        f'<div class="mc"><div class="lbl">Failure rate</div>'
        f'<div class="big" style="margin-top:4px;">{_pi(h.failure_rate)}</div>'
        f'<div style="font-size:12px;color:{_HEALTH_LIGHT_COLOR[h.light]};margin-top:2px;">'
        f"Health · {light_en}</div>{hreason_html}{_help(_H_FAILURE)}</div></div>"
    )
    # 排名条 + SOV(多品牌)/ 单品牌专用排版
    if search_only:  # 没有回答正文就谈不上"提及":不画提及排名(否则全是 0% 的假排名)
        p.append(
            '<div class="panel"><h2>Mention coverage</h2><div class="lbl">Not applicable: a '
            "search-only surface returns no answer text, so no brand can be &ldquo;mentioned"
            "&rdquo;. See the citation metrics below.</div></div>"
        )
    else:
        p.append(_brand_overview_html(ranked, hero, color_of))
    # 其余指标表
    na = '<td class="bd">n/a</td>'
    rows = "".join(
        f'<tr><td class="bd">{e(m.brand)}</td>'
        f'<td class="bd">{_pi(m.earned_citation_rate.point)} '
        f'<span style="color:#9AA38F;">{_iv_int(m.earned_citation_rate)}</span></td>'
        + (na if search_only else f'<td class="bd">{_fmt_optional_pi(m.mention_sov)}</td>')
        + f'<td class="bd">{_fmt_optional_pi(m.citation_sov)}</td>'
        + (
            na
            if search_only
            else f'<td class="bd">{_pi(m.stability.point)} '
            f'<span style="color:#9AA38F;">{_iv_int(m.stability)}</span></td>'
        )
        + "</tr>"
        for m in ranked
    )
    p.append(
        f'<div class="panel"><h2>Supporting metrics · strict</h2>{_help(_H_SUPPORT)}'
        "<table><thead><tr>"
        "<th>Brand</th><th>Earned cite</th><th>Mention SOV</th><th>Cite SOV</th>"
        "<th>Stability</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    # 在场矩阵
    p.append(_matrix_html(bundle, cfg))
    # 网络去向 + 测量表面
    api_calls = bundle.n_responses_ok + bundle.n_responses_error
    fetched = len(
        {ev.canonical_domain for ev in bundle.evidence if ev.check_method == CHECK_PAGE_FETCH}
    )
    cited = len({ev.canonical_domain for ev in bundle.evidence})
    if cfg.sequential:  # 序贯采样:每题问到稳定即停,轮数不是固定的
        observations = (
            f"{bundle.n_questions} questions · up to {bundle.n_runs} rounds each · "
            f"{bundle.n_responses_ok} ok"
        )
        rounds = f"adaptive sampling (up to {bundle.n_runs} rounds per question)"
    else:
        observations = f"{bundle.n_questions} × {bundle.n_runs} = {bundle.n_responses_ok} ok"
        rounds = f"fixed {bundle.n_runs} rounds"
    # API 去向按 provider 渲染,不硬编码 Anthropic(mock 不真联网;未来新引擎用 surface_id)
    if bundle.provider == "mock":
        api_row = (
            '<tr><td style="color:#7E8B76;">mock provider</td>'
            '<td style="text-align:right;font-weight:500;color:#4E9A06;">'
            "0 external API calls</td></tr>"
        )
    else:
        host = e(_PROVIDER_HOST.get(bundle.provider, bundle.surface_id))
        api_row = (
            f'<tr><td style="color:#7E8B76;">{host}</td>'
            f'<td style="text-align:right;font-weight:500;">{api_calls} calls</td></tr>'
        )
    p.append(
        '<div class="g2"><div class="panel"><h2>Network destinations</h2><table>'
        f"{api_row}"
        f'<tr><td style="color:#7E8B76;">Cited domains (fetched {fetched})</td>'
        f'<td style="text-align:right;font-weight:500;">{cited}</td></tr></table></div>'
        '<div class="panel"><h2>Measurement surface</h2><table>'
        f'<tr><td style="color:#7E8B76;">Question / competitor set</td>'
        f'<td style="text-align:right;">'
        f"{e(bundle.prompt_version)} / {e(bundle.competitor_set_version or '—')}</td></tr>"
        f'<tr><td style="color:#7E8B76;">Observations</td><td style="text-align:right;">'
        f"{observations}</td></tr>"
        f'<tr><td style="color:#7E8B76;">Evidence basis</td><td style="text-align:right;">'
        f"{e(bundle.tier_basis)}</td></tr>"
        f'<tr><td style="color:#7E8B76;">Config fingerprint</td><td style="text-align:right;">'
        f"{e(bundle.config_fingerprint[:12])}…</td></tr>"
        f'<tr><td style="color:#7E8B76;">Surface kind</td><td style="text-align:right;">'
        f"{e(bundle.surface_kind)}</td></tr>"
        f'<tr><td style="color:#7E8B76;">Temperature / region applied</td>'
        f'<td style="text-align:right;">{e(_yesno(bundle.temperature_applied))} / '
        f"{e(_yesno(bundle.region_applied))}</td></tr>"
        f'<tr><td style="color:#7E8B76;">Actual cost (bill)</td><td style="text-align:right;">'
        f"{'—' if bundle.cost_usd_actual is None else f'${bundle.cost_usd_actual:.4f}'}"
        "</td></tr></table></div></div>"
    )
    # 证据链
    p.append(_evidence_html(bundle))
    # 透明声明
    p.append(
        '<div style="font-size:11.5px;color:#9AA38F;line-height:1.6;padding:0 2px;">'
        "Deterministic counting · conservative counting (abbreviations / ambiguous names "
        "recorded, not scored) · Wilson intervals · no AI judgment · "
        f"{rounds} · observations and leads only</div>"
    )
    p.append("</div>")

    body = "\n".join(p)
    title = f"AI visibility report · {e(bundle.provider)} · {e(bundle.run_id[:8])}"
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"<title>{title}</title>\n<style>{_HTML_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def _brand_overview_html(ranked: list[Metrics], hero: Metrics, color_of: dict[str, str]) -> str:
    """提及覆盖概览:多品牌 = 排名条 + SOV 甜甜圈;单品牌 = 专用排版(无排名/无甜甜圈)。"""
    if len(ranked) == 1:
        return _single_brand_overview_html(hero, color_of)
    e = html.escape
    last = ranked[-1]
    bars = [
        '<div class="sp"><div class="panel"><h2>Mention coverage ranking · strict</h2>'
        + _help(_H_RANKING)
    ]
    bars.append(
        '<div style="display:flex;gap:24px;margin-bottom:14px;">'
        '<div><div class="lbl">Highest</div>'
        '<div style="font-size:20px;font-weight:500;color:#3C6E0F;">'
        f'{_pi(hero.mention_coverage.point)} <span style="font-size:13px;color:#7E8B76;">'
        f"{e(hero.brand)}</span></div></div>"
        f'<div><div class="lbl">Lowest</div><div style="font-size:20px;font-weight:500;">'
        f'{_pi(last.mention_coverage.point)} <span style="font-size:13px;color:#7E8B76;">'
        f"{e(last.brand)}</span></div></div></div>"
        '<div style="display:flex;flex-direction:column;gap:11px;">'
    )
    for m in ranked:
        iv = m.mention_coverage
        bars.append(
            '<div style="display:flex;align-items:center;gap:10px;">'
            f'<span style="width:84px;font-size:13px;flex-shrink:0;">{e(m.brand)}</span>'
            f"{_bar_svg(iv.point, iv.low, iv.high, color_of[m.brand])}"
            f'<span style="font-size:13px;font-weight:500;">{_pi(iv.point)}</span></div>'
        )
    bars.append(
        '</div><div style="font-size:11.5px;color:#9AA38F;margin-top:12px;">'
        f"Bar = point estimate · whisker = Wilson 95% CI (n={hero.mention_coverage.n})"
        "</div></div>"
    )
    segs = [(m.brand, m.mention_sov.point, color_of[m.brand]) for m in ranked if m.mention_sov]
    if len(segs) >= 2:
        legend = "".join(
            f'<span style="display:block;"><span style="display:inline-block;width:10px;'
            f'height:10px;border-radius:2px;background:{c};margin-right:7px;"></span>'
            f"{e(name)} {round(v * 100)}%</span>"
            for name, v, c in segs
        )
        sov = (
            f'<div class="panel"><h2>Share of voice</h2>{_help(_H_SOV)}'
            '<div style="display:flex;justify-content:center;margin-bottom:10px;">'
            f"{_donut_svg(segs)}</div>"
            f'<div style="font-size:12.5px;display:flex;flex-direction:column;gap:6px;">'
            f"{legend}</div></div>"
        )
    else:
        sov = (
            f'<div class="panel"><h2>Share of voice</h2>{_help(_H_SOV)}'
            '<p class="lbl">No competitor set — SOV not applicable.</p></div>'
        )
    return "".join(bars) + sov + "</div>"


def _single_brand_overview_html(hero: Metrics, color_of: dict[str, str]) -> str:
    """单品牌:整宽一块面板,只呈现该品牌提及覆盖(点估计 + Wilson 区间须),无排名/无 SOV。"""
    e = html.escape
    iv = hero.mention_coverage
    return (
        f'<div class="panel"><h2>Mention coverage · strict</h2>{_help(_H_COVERAGE)}'
        '<div style="display:flex;align-items:baseline;gap:10px;margin-bottom:14px;">'
        f'<span class="big" style="font-size:30px;">{_pi(iv.point)}</span>'
        f'<span class="lbl">{e(hero.brand)}</span></div>'
        '<div style="display:flex;align-items:center;gap:10px;">'
        f'<span style="width:84px;font-size:13px;flex-shrink:0;">{e(hero.brand)}</span>'
        f"{_bar_svg(iv.point, iv.low, iv.high, color_of[hero.brand])}"
        f'<span style="font-size:13px;font-weight:500;">{_pi(iv.point)}</span></div>'
        '<div style="font-size:11.5px;color:#9AA38F;margin-top:12px;">'
        f"Bar = point estimate · whisker = Wilson 95% CI (n={iv.n})</div>"
        '<div class="note" style="margin-top:14px;">'
        '<span style="font-size:12.5px;color:#4A6B23;line-height:1.5;">'
        "Single brand measured — no competitor share-of-voice. "
        "Add competitors to compare against peers.</span></div></div>"
    )


def _matrix_html(bundle: ScoreBundle, cfg: FrozenConfig) -> str:
    esc = html.escape
    brand_names = [m.brand for m in bundle.brands]
    question_ids = sorted({t.question_id for t in bundle.tiers})
    if not question_ids:
        return (
            '<div class="panel"><h2>Presence matrix</h2>'
            '<p class="lbl">No per-question evidence in this run.</p></div>'
        )
    best: dict[tuple[str, str], Tier] = {}
    for t in bundle.tiers:
        key = (t.question_id, t.brand)
        if key not in best or _TIER_RANK[t.tier] > _TIER_RANK[best[key]]:
            best[key] = t.tier
    qtext = {q.id: q.text for q in cfg.questions}
    legend = (
        f'<span style="color:{_TIER_HTML["verified"][1]};">✔</span> verified · '
        f'<span style="color:{_TIER_HTML["cited"][1]};">◐</span> cited · '
        f'<span style="color:{_TIER_HTML["mentioned"][1]};">○</span> mentioned'
    )
    head = "".join(f"<th>{esc(b[:6])}</th>" for b in brand_names)
    body = []
    for qid in question_ids:
        text = qtext.get(qid, "")
        label = f"{esc(qid)} {esc(text[:38])}{'…' if len(text) > 38 else ''}"
        cells = ""
        for b in brand_names:
            sym, col = _TIER_HTML[best.get((qid, b), "none")]
            cells += f'<td class="bd" style="text-align:center;color:{col};">{sym}</td>'
        body.append(f'<tr><td class="bd" style="color:#7E8B76;">{label}</td>{cells}</tr>')
    return (
        '<div class="panel"><h2>Presence matrix <span style="font-size:13px;color:#7E8B76;'
        'font-weight:400;">· highest evidence tier per question</span></h2>'
        + _help(_H_MATRIX)
        + f'<div style="font-size:12px;color:#7E8B76;margin-bottom:8px;">Legend {legend}</div>'
        f'<table style="font-size:12px;"><thead><tr><th>Question</th>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _evidence_html(bundle: ScoreBundle) -> str:
    if not bundle.evidence:
        return (
            '<div class="panel"><h2>Evidence chain</h2>'
            '<p class="lbl">No cited URLs verified in this run.</p></div>'
        )
    esc = html.escape
    rows = []
    for ev in bundle.evidence[:_MAX_EVIDENCE_ROWS]:
        owner = f"owned({esc(ev.owner_brand or '')})" if ev.owned_or_earned == "owned" else "earned"
        reach = "✓" if ev.reachable else "✗"
        reach_color = "#4E9A06" if ev.reachable else "#C0392B"
        if ev.check_method == CHECK_PAGE_FETCH:
            mention = ", ".join(esc(b) for b in ev.brands_on_page) if ev.brands_on_page else "✗"
        else:
            mention = "—"
        rows.append(
            f'<tr><td class="bd" style="color:#185FA5;">{esc(ev.canonical_domain)}</td>'
            f'<td class="bd">{owner}</td>'
            f'<td class="bd" style="color:{reach_color};">{reach}</td>'
            f'<td class="bd">{esc(mention)}</td>'
            f'<td class="bd" style="color:#9AA38F;">{esc(ev.check_method)}</td></tr>'
        )
    more = ""
    if len(bundle.evidence) > _MAX_EVIDENCE_ROWS:
        more = (
            f'<div class="lbl" style="margin-top:6px;">'
            f"{len(bundle.evidence)} total, rest omitted</div>"
        )
    return (
        f'<div class="panel"><h2>Evidence chain <span style="font-size:13px;color:#7E8B76;'
        f'font-weight:400;">· {esc(bundle.tier_basis)}</span></h2>'
        + _help(_H_EVIDENCE)
        + "<table><thead><tr>"
        "<th>Domain</th><th>Source</th><th>Reachable</th><th>Mentions</th><th>Method</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>{more}</div>"
    )
