"""report_assets —— 报告渲染的叶子件:常量 + 数字格式化 + 内联 SVG(纯,无 I/O)。

被 report.py(markdown)与 report_html.py(html)共用,放在底层避免二者循环依赖。
白底 + 绿色渐变仪表盘主题(与 GUI 同皮:渐变 #8FD744→#54A012 / 强调 #4E9A06 /
面板 #F4F7F2 / 浅绿卡片 #EEF6E4);品牌按提及覆盖率排名取深→浅一种绿色梯度
(颜色编码"份额大小"而非彩虹)。语义色保留:cited 蓝 / verified 青绿。
"""

import math

from aivis.contracts.records import Interval, Tier

# ---- 共享常量 ----
_LIGHT_LABEL = {"green": "🟢 绿", "yellow": "🟡 黄", "red": "🔴 红"}
_TIER_SYMBOL: dict[Tier, str] = {"verified": "✔", "cited": "◐", "mentioned": "○", "none": "·"}
_TIER_RANK: dict[Tier, int] = {"none": 0, "mentioned": 1, "cited": 2, "verified": 3}
_MAX_EVIDENCE_ROWS = 60

_TIER_BASIS_NOTE = {
    "verified-page-v2": "可达 + 页面正文确实提到该品牌(最强)",
    "verified-reachable-v1": "可达 = HTTP 状态码(未读页面正文)",
    "verified-offline-v1": "可达 = 离线假设(最弱,未联网)",
    "verified-mixed": "本次运行混用了多种核验方式",
    "verified-none": "无证据",
}

# 品牌排名色阶:深绿(份额高)→ 浅绿(份额低);颜色编码份额大小,非彩虹分类。
_BRAND_RAMP = (
    "#2F5A0C",
    "#3C6E0F",
    "#4E9A06",
    "#54A012",
    "#6FB01F",
    "#7CC22E",
    "#8FD744",
    "#B6E182",
)
_TIER_HTML: dict[Tier, tuple[str, str]] = {
    "verified": ("✔", "#0F6E56"),
    "cited": ("◐", "#185FA5"),
    "mentioned": ("○", "#9AA38F"),
    "none": ("·", "#C3D2B0"),
}
_HTML_FONT = (
    '-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,'
    '"PingFang SC","Microsoft YaHei",sans-serif'
)
# 🟥 占位符用 __FONT__ 而非裸 "F":.replace("F",…) 会污染所有含 F 的十六进制色(见回归测试)
_HTML_CSS = (
    "*{box-sizing:border-box;}"
    "body{margin:0;color:#2C3327;font-size:14px;line-height:1.6;font-family:__FONT__;"
    "background:#FFFFFF;background-image:"
    "radial-gradient(820px 480px at 92% -8%,rgba(141,213,72,.20),transparent 60%),"
    "radial-gradient(680px 520px at -6% 112%,rgba(110,195,55,.14),transparent 55%);"
    "background-attachment:fixed;}"
    ".wrap{max-width:880px;margin:24px auto;padding:24px 22px;}"
    ".titlebar{background:linear-gradient(135deg,#8FD744 0%,#54A012 100%);color:#fff;"
    "border-radius:22px;padding:16px 24px;margin-bottom:16px;display:flex;"
    "justify-content:space-between;align-items:center;box-shadow:0 12px 28px rgba(84,160,18,.22);}"
    ".panel{background:#F4F7F2;border-radius:18px;padding:16px 18px;margin-bottom:14px;"
    "border:1px solid rgba(120,180,60,.12);box-shadow:0 8px 30px rgba(60,110,30,.06);}"
    ".mc{background:#EEF6E4;border-radius:14px;padding:13px 15px;}"
    ".big{font-size:26px;font-weight:500;color:#3C6E0F;line-height:1.2;}"
    ".lbl{font-size:13px;color:#7E8B76;}"
    "h1{font-size:20px;font-weight:500;margin:0;}"
    "h2{font-size:16px;font-weight:500;margin:0 0 12px;}"
    "table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed;}"
    "th{font-weight:500;color:#7E8B76;text-align:left;}td,th{padding:7px 6px;}"
    ".bd{border-bottom:.5px solid #E0EBD4;}"
    ".g4{display:grid;grid-template-columns:1.25fr 1fr 1fr 1fr;gap:12px;}"
    ".g2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}"
    ".sp{display:grid;grid-template-columns:1.5fr 1fr;gap:14px;}"
    ".pill{background:linear-gradient(135deg,#8FD744,#54A012);color:#fff;font-size:12px;"
    "padding:6px 14px;border-radius:14px;font-weight:500;}"
    ".note{display:flex;gap:8px;background:#EEF6E4;border:1px solid #DBEBC7;"
    "border-radius:12px;padding:10px 13px;}"
    ".hlp{margin-top:5px;}"
    ".hlp summary{cursor:pointer;font-size:11px;color:#6E8A3E;list-style:none;}"
    ".hlp summary::-webkit-details-marker{display:none;}"
    ".hlpb{font-size:12px;color:#5C6555;background:#EEF6E4;border-radius:8px;"
    "padding:7px 10px;margin-top:5px;}"
    "@media(max-width:620px){.g4,.g2,.sp{grid-template-columns:1fr;}}"
).replace("__FONT__", _HTML_FONT)


def _help(text: str) -> str:
    """点击展开的"怎么读这个指标"小注(原生 <details>,零脚本、离线;text 为内部静态文案)。"""
    return (
        '<details class="hlp"><summary>how to read</summary>'
        f'<div class="hlpb">{text}</div></details>'
    )


# ---- markdown 数字格式化 ----
def _yesno(v: bool | None) -> str:
    """诚实字段三态:None=未记录(旧运行)/ True=生效 / False=该引擎钉不住此参数。"""
    return "—" if v is None else ("✓" if v else "✗")


def _pct(x: float) -> str:
    return f"{100.0 * x:.1f}%"


def _fmt(interval: Interval) -> str:
    if interval.method == "insufficient":
        return "—(样本不足)"
    return f"{_pct(interval.point)} [{_pct(interval.low)}, {_pct(interval.high)}] (n={interval.n})"


def _fmt_optional(interval: Interval | None) -> str:
    return "不适用" if interval is None else _fmt(interval)


# ---- html 数字格式化 ----
def _pi(x: float) -> str:
    return f"{round(100.0 * x)}%"


def _iv_int(iv: Interval) -> str:
    if iv.method == "insufficient":
        return "样本不足"
    return f"[{round(100 * iv.low)}–{round(100 * iv.high)}]"


def _fmt_optional_pi(interval: Interval | None) -> str:
    return "n/a" if interval is None else _pi(interval.point)


# ---- 内联 SVG(零依赖、离线) ----
def _bar_svg(point: float, low: float, high: float, color: str) -> str:
    """水平条:实心=点估计,细须=Wilson 区间。轨道宽 150。"""
    w = 150.0
    fill, wl, wh = round(point * w, 1), round(low * w, 1), round(high * w, 1)
    return (
        f'<svg width="166" height="18" role="img" aria-label="{round(point * 100)}%">'
        '<rect x="0" y="6" width="150" height="6" rx="3" fill="#E6EFDC"/>'
        f'<rect x="0" y="6" width="{fill}" height="6" rx="3" fill="{color}"/>'
        f'<line x1="{wl}" y1="9" x2="{wh}" y2="9" stroke="#3C6E0F" stroke-width="1.5"/>'
        f'<line x1="{wl}" y1="5" x2="{wl}" y2="13" stroke="#3C6E0F" stroke-width="1.5"/>'
        f'<line x1="{wh}" y1="5" x2="{wh}" y2="13" stroke="#3C6E0F" stroke-width="1.5"/></svg>'
    )


def _donut_svg(segs: list[tuple[str, float, str]]) -> str:
    """内联 SVG 甜甜圈;segs=[(label, value, color)]。零依赖、离线。"""
    circ = 2 * math.pi * 54.0
    total = sum(v for _, v, _ in segs) or 1.0
    parts = [
        '<svg width="140" height="140" viewBox="0 0 140 140" '
        'role="img" aria-label="Share of voice">',
        '<circle cx="70" cy="70" r="54" fill="none" stroke="#E6EFDC" stroke-width="22"/>',
    ]
    off = 0.0
    for _, value, color in segs:
        seg = round(value / total * circ, 1)
        parts.append(
            f'<circle cx="70" cy="70" r="54" fill="none" stroke="{color}" stroke-width="22" '
            f'stroke-dasharray="{seg} {round(circ - seg, 1)}" stroke-dashoffset="{round(-off, 1)}" '
            'transform="rotate(-90 70 70)"/>'
        )
        off += seg
    lead = round(segs[0][1] / total * 100)
    parts.append(
        '<text x="70" y="66" text-anchor="middle" font-size="11" fill="#7E8B76">LARGEST</text>'
        '<text x="70" y="84" text-anchor="middle" font-size="17" font-weight="500" '
        f'fill="#3C6E0F">{lead}%</text></svg>'
    )
    return "".join(parts)
