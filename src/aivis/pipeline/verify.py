"""verify —— I/O 层:唯一做被引 URL 核验的模块(🟥 CLAUDE.md §1)。

封装边界:本模块只依赖 contracts.checker.UrlChecker 接口,核验"怎么做"
全部藏在实现类里。v1 提供两个实现(都不读网页正文,无 prompt-injection 面):

  - OfflineChecker:假定可达,诚实标注 'offline-assumed'(mock 链路不联网不花钱);
  - HttpHeadChecker:HTTP HEAD 只看状态码、绝不读响应体。

第二步的真页面抓取 + 摘录支持性检查 = 新增一个 UrlChecker 实现类
(含 HTML 消毒 + 域名 allow/blocklist,见 contracts/checker.py 的红线),
本模块与 score/extract/storage 零改动。
"""

import ipaddress
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from aivis.contracts.checker import (
    CHECK_HTTP_HEAD,
    CHECK_OFFLINE,
    CHECK_PAGE_FETCH,
    CheckOutcome,
    UrlChecker,
)
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import EvidenceRecord, ExtractionResult
from aivis.matching import name_pattern  # 单一匹配口径(与 extract/configcheck 共用)
from aivis.pipeline.extract import canonical_domain, owner_of_domain

# 常量定义已上移到 contracts.checker(属契约词汇);此处再导出,兼容既有 import
__all__ = [
    "CHECK_HTTP_HEAD",
    "CHECK_OFFLINE",
    "CHECK_PAGE_FETCH",
    "HttpHeadChecker",
    "OfflineChecker",
    "PageFetchChecker",
    "html_to_text",
    "is_safe_url",
    "verify",
]

_DEFAULT_MAX_AGE_DAYS = 7.0
_USER_AGENT = "aivis/0.1 (reproducible visibility measurement; user-triggered verify)"
_MAX_REDIRECTS = 5
# 恶意/坏掉的 HTML(如几十 KB 的未闭合标签)能让标准库解析器平方级地慢:给解析设总时限
_HTML_PARSE_BUDGET_S = 5.0
_HTML_PARSE_CHUNK = 2048


# -------------------------------------------------------------------- 共享 SSRF 安全策略
# 被引 URL 来自不可信的 AI 回答(CLAUDE.md §5),所有网络核验器(head/fetch)在发请求前
# 都必须先过这关。已知限制:不做 DNS 解析钉 IP —— 一个 A 记录指向内网地址的普通域名、
# 或 DNS-rebinding,这里拦不住(留作已知边界;重定向到私网、数字混淆写法已堵)。


def _is_unsafe_host(host: str) -> bool:
    """私网/环回/链路本地/保留/未指定地址,或 localhost/.local → 视为不安全。"""
    h = host.lower().rstrip(".")  # "localhost." 这种带根点的写法与 "localhost" 等价
    if not h:
        return True
    if h == "localhost" or h.endswith((".localhost", ".local")):
        return True
    try:
        return not ipaddress.ip_address(h).is_global  # 覆盖私网/环回/链路本地/多播/保留/未指定
    except ValueError:
        pass
    # 不是合法的点分 IP,但最后一段是纯数字或 0x 开头:这是 IP 的混淆写法
    # (2130706433 / 127.1 / 0x7f.0.0.1 / 0177.0.0.1,有的系统会把它解析成 127.0.0.1)。
    # 真实域名的顶级域不会是纯数字,所以一律拒绝。
    last = h.rsplit(".", 1)[-1]
    # 其余是普通域名:放行,交给黑白名单 + 重定向逐跳复查
    return last.isdigit() or last.startswith("0x")


def is_safe_url(url: str) -> bool:
    """共享安全策略:只允许 http(s),且目标 host 非私网/环回/保留地址。"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:  # 畸形 URL(如 http://[foo]/x):解析都过不了,当然不安全
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return not _is_unsafe_host(host)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """逐跳校验重定向目标:公开 URL 跳向私网/非 http(s) 一律拦下(防重定向 SSRF)。"""

    max_redirections = _MAX_REDIRECTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not is_safe_url(newurl):
            raise urllib.error.URLError("blocked unsafe redirect target")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_opener() -> urllib.request.OpenerDirector:
    """带重定向安全校验的 opener(每跳都过 is_safe_url)。"""
    return urllib.request.build_opener(_SafeRedirectHandler())


class OfflineChecker:
    """离线模式:假定可达。method='offline-assumed' 让"这是假设"永远可审计。"""

    @property
    def method(self) -> str:
        return CHECK_OFFLINE

    def check(self, url: str) -> CheckOutcome:
        return CheckOutcome(reachable=True, method=self.method, snippet=None)


class HttpHeadChecker:
    """HTTP HEAD 可达性检查:只看状态码、绝不读响应体(内容一律不可信,CLAUDE.md §5)。

    与 PageFetchChecker 共用同一 SSRF 安全策略(is_safe_url + 重定向逐跳复查)。
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    @property
    def method(self) -> str:
        return CHECK_HTTP_HEAD

    def check(self, url: str) -> CheckOutcome:
        if not is_safe_url(url):  # 私网/非 http(s) → 不发请求
            return CheckOutcome(reachable=False, method=self.method, snippet=None)
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
        try:
            with _safe_opener().open(request, timeout=self._timeout) as resp:
                reachable = int(resp.status) < 400
        except Exception:
            reachable = False
        return CheckOutcome(reachable=reachable, method=self.method, snippet=None)


class _TextExtractor(HTMLParser):
    """规则层 HTML 消毒:只取可见文本,丢弃 script/style 等;不解释任何内容。"""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    """HTML → 消毒后的纯文本(空白折叠);残缺 HTML 尽力解析,绝不抛异常。

    分块喂给解析器并检查总时限:被引页面是不可信内容,几十 KB 的未闭合标签就能让标准库
    解析器跑上几十秒到几小时(纯 CPU,socket 超时管不到)。超时就停,用已解析出的部分。
    """
    parser = _TextExtractor()
    deadline = time.monotonic() + _HTML_PARSE_BUDGET_S
    try:
        for start in range(0, len(html), _HTML_PARSE_CHUNK):
            parser.feed(html[start : start + _HTML_PARSE_CHUNK])
            if time.monotonic() > deadline:
                break
        else:
            parser.close()
    except Exception:
        pass
    return re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()


class PageFetchChecker:
    """真页面抓取核验(BUILD_SPEC §8 step 11):GET + HTML 消毒 + 品牌提及短摘录。

    🟥 安全红线(CLAUDE.md §5):
      - 网页内容一律不可信:只做规则层文本抽取与匹配,绝不把生 HTML 当指令;
      - 只存短摘录(snippet),不镜像整页;
      - 域名 allow/blocklist + 私网地址拦截;非 http(s) 协议一律拒绝;
      - 有界抓取:超时 + 字节上限 + 礼貌间隔;仅用户触发,不是后台爬虫。

    snippet 语义:消毒文本中首个品牌清晰名命中处的 ±window 字符窗口;
    brands_on_page = 正文里出现清晰名的**品牌**集合(口径与 extract 一致,
    供 score 升级 verified 口径:可达 + 页面真的提到该品牌)。
    """

    def __init__(
        self,
        brands: Mapping[str, Sequence[str]],
        *,
        timeout: float = 15.0,
        total_timeout: float = 30.0,
        max_bytes: int = 512_000,
        snippet_window: int = 120,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] = (),
        delay_seconds: float = 0.5,
    ) -> None:
        # {规范品牌名: (用于匹配的清晰名/别名, ...)};只用清晰名,不含歧义别名
        self._brands = {name: tuple(t for t in terms if t) for name, terms in brands.items()}
        self._timeout = timeout  # 单次 socket 操作的超时
        self._total_timeout = total_timeout  # 读完整个正文的总时限(防"每秒吐 1 字节"拖死)
        self._max_bytes = max_bytes
        self._window = snippet_window
        self._allowed = tuple(d.lower() for d in allowed_domains) if allowed_domains else None
        self._blocked = tuple(d.lower() for d in blocked_domains)
        self._delay = delay_seconds

    @property
    def method(self) -> str:
        return CHECK_PAGE_FETCH

    def check(self, url: str) -> CheckOutcome:
        if not self._policy_allows(url):
            return CheckOutcome(reachable=False, method=self.method, snippet=None)
        try:
            status, content_type, body = self._fetch(url)
        except Exception:
            return CheckOutcome(reachable=False, method=self.method, snippet=None)
        # 可达 = 内容真的取到了(2xx)。HTTPError 路径上的 3xx 意味着自动重定向
        # 没走通(常见于反爬),内容并未取得 → 不算可达(真实运行中验过的坑)
        reachable = 200 <= status < 300
        brands_on_page: tuple[str, ...] = ()
        snippet = None
        if reachable and "html" in content_type.lower():
            brands_on_page, snippet = self._analyze_page(html_to_text(body))
        return CheckOutcome(
            reachable=reachable,
            method=self.method,
            snippet=snippet,
            brands_on_page=brands_on_page,
        )

    # -------------------------------------------------------------- 内部

    def _policy_allows(self, url: str) -> bool:
        if not is_safe_url(url):  # 共享 SSRF 策略:scheme + 私网拦截
            return False
        domain = canonical_domain(url)
        if any(domain == b or domain.endswith("." + b) for b in self._blocked):
            return False
        if self._allowed is not None:
            return any(domain == a or domain.endswith("." + a) for a in self._allowed)
        return True

    def _fetch(self, url: str) -> tuple[int, str, str]:
        """唯一网络触点(测试以替身覆盖)。返回 (状态码, Content-Type, 有界正文)。
        重定向由 _safe_opener 逐跳校验,跳向私网/非 http(s) 会被拦下记为不可达。"""
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with _safe_opener().open(request, timeout=self._timeout) as resp:
                status = int(resp.status)
                content_type = str(resp.headers.get("Content-Type", ""))
                charset = resp.headers.get_content_charset() or "utf-8"
                # 分段读 + 总时限:socket 超时只管"多久没数据",管不住慢速滴漏的响应
                deadline = time.monotonic() + self._total_timeout
                parts: list[bytes] = []
                remaining = self._max_bytes
                while remaining > 0:
                    part = resp.read1(min(65536, remaining))
                    if not part:
                        break
                    parts.append(part)
                    remaining -= len(part)
                    if time.monotonic() > deadline:
                        raise TimeoutError("page fetch exceeded the total time budget")
                raw = b"".join(parts)
        except urllib.error.HTTPError as e:
            return int(e.code), "", ""
        finally:
            time.sleep(self._delay)  # 礼貌间隔:不对同批 URL 连环轰
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:  # 页面声明了 Python 不认识的编码:按 utf-8 读,别把能打开的页面记成不可达
            text = raw.decode("utf-8", errors="replace")
        return status, content_type, text

    def _analyze_page(self, text: str) -> tuple[tuple[str, ...], str | None]:
        """返回 (页面命中清晰名的品牌集合, 首个命中处的消毒短摘录)。"""
        found: list[str] = []
        best: tuple[int, str] | None = None
        for brand_name, terms in self._brands.items():
            hit: tuple[int, str] | None = None
            for term in terms:
                m = name_pattern(term).search(text)
                if m is not None and (hit is None or m.start() < hit[0]):
                    hit = (m.start(), m.group())
            if hit is not None:
                found.append(brand_name)
                if best is None or hit[0] < best[0]:
                    best = hit
        snippet = None
        if best is not None:
            start = max(0, best[0] - self._window)
            end = min(len(text), best[0] + len(best[1]) + self._window)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            snippet = f"{prefix}{text[start:end].strip()}{suffix}"
        return tuple(found), snippet


def _is_fresh(record: EvidenceRecord, now: str, max_age_days: float) -> bool:
    try:
        checked = datetime.fromisoformat(record.checked_at.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False
    return current - checked <= timedelta(days=max_age_days)


def verify(
    extraction: ExtractionResult,
    cfg: FrozenConfig,
    *,
    checker: UrlChecker,
    now: str,
    cache: Mapping[str, EvidenceRecord] | None = None,
    max_age_days: float = _DEFAULT_MAX_AGE_DAYS,
) -> list[EvidenceRecord]:
    """核验本次 extraction 涉及的全部唯一 URL,返回**新产生**的核验记录。

    缓存复用:cache 中同方式(checker.method)且未过期的记录直接跳过
    (同一 URL 跨问题/跨轮反复出现,核验一次即可;限速友好,真抓取时省钱)。

    🟥 缓存语义约定(真实运行踩过的坑):
    复用只看 check_method 字符串 + 时间,**不察觉"同名方式但输出语义变了"**。
    所以改了某核验器的输出含义后(如给 page-fetch 加 brands_on_page),
    必须传 cache=None 强制重核(CLI 用 --refresh),否则会复用旧语义的脏证据。
    """
    cache = cache or {}
    seen: dict[str, str] = {}  # normalized_url -> 首见原样 URL
    for c in extraction.citations:
        seen.setdefault(c.normalized_url, c.url)

    new_records: list[EvidenceRecord] = []
    for norm in sorted(seen):
        cached = cache.get(norm)
        if (
            cached is not None
            and cached.check_method == checker.method
            and _is_fresh(cached, now, max_age_days)
        ):
            continue
        domain = next(c.canonical_domain for c in extraction.citations if c.normalized_url == norm)
        owner = owner_of_domain(domain, cfg.peers)
        outcome = checker.check(norm)
        new_records.append(
            EvidenceRecord(
                citation_url=seen[norm],
                normalized_url=norm,
                canonical_domain=domain,
                owned_or_earned="owned" if owner is not None else "earned",
                owner_brand=owner.name if owner is not None else None,
                source_class="brand_site" if owner is not None else "third_party",
                reachable=outcome.reachable,
                check_method=outcome.method,
                snippet=outcome.snippet,
                brands_on_page=outcome.brands_on_page,
                checked_at=now,
            )
        )
    return new_records
