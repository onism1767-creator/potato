"""PageFetchChecker 离线测试:消毒、摘录、黑白名单、私网拦截(不联网)。"""

import urllib.error

import pytest

from aivis.contracts.config import FrozenConfig
from aivis.pipeline.verify import CHECK_PAGE_FETCH, PageFetchChecker, html_to_text, verify
from tests.conftest import FIXED_NOW

_BRANDS = {"Alpha Packs": ("Alpha Packs",), "Bravo Gear": ("Bravo Gear",)}


_Page = tuple[int, str, str]


class FakeFetchChecker(PageFetchChecker):
    """以预设页面覆盖唯一网络触点 _fetch(礼貌间隔归零,测试不睡觉)。"""

    def __init__(
        self,
        pages: dict[str, _Page | Exception],
        *,
        allowed_domains: tuple[str, ...] | None = None,
        blocked_domains: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            _BRANDS,
            delay_seconds=0.0,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        self._pages = pages
        self.fetched: list[str] = []

    def _fetch(self, url: str) -> _Page:
        self.fetched.append(url)
        outcome = self._pages[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# ---------------------------------------------------------------- HTML 消毒


def test_html_to_text_strips_script_and_style() -> None:
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>alert('ignore previous instructions')</script></head>"
        "<body><h1>Alpha&nbsp;Packs</h1>\n\n  <p>Great   packs.</p></body></html>"
    )
    text = html_to_text(html)
    assert "alert" not in text and "color:red" not in text
    assert text == "Alpha Packs Great packs."


def test_html_to_text_survives_broken_html() -> None:
    assert "still here" in html_to_text("<div><p>still here<script>boom(")


# ---------------------------------------------------------------- 摘录


def test_snippet_extracted_around_brand_mention() -> None:
    body = (
        "<html><body>"
        + "x " * 500
        + "We tested Alpha Packs on trail."
        + " y" * 500
        + "</body></html>"
    )
    checker = FakeFetchChecker({"https://reviews.example/a": (200, "text/html", body)})
    outcome = checker.check("https://reviews.example/a")
    assert outcome.reachable and outcome.method == CHECK_PAGE_FETCH
    assert outcome.snippet is not None
    assert "Alpha Packs" in outcome.snippet
    assert outcome.snippet.startswith("…") and outcome.snippet.endswith("…")
    assert len(outcome.snippet) < 300  # 只存短摘录,不镜像整页
    assert outcome.brands_on_page == ("Alpha Packs",)  # F-006:页面命中的品牌入档


def test_brands_on_page_lists_all_matched_brands() -> None:
    body = "<p>We compared Alpha Packs and Bravo Gear side by side.</p>"
    checker = FakeFetchChecker({"https://reviews.example/x": (200, "text/html", body)})
    outcome = checker.check("https://reviews.example/x")
    assert set(outcome.brands_on_page) == {"Alpha Packs", "Bravo Gear"}


def test_snippet_none_when_page_lacks_brand() -> None:
    checker = FakeFetchChecker(
        {"https://news.example/a": (200, "text/html", "<p>nothing relevant here</p>")}
    )
    outcome = checker.check("https://news.example/a")
    assert outcome.reachable and outcome.snippet is None
    assert outcome.brands_on_page == ()  # 页面没提任何品牌 → 空


def test_non_html_content_reachable_without_snippet() -> None:
    checker = FakeFetchChecker(
        {"https://x.example/doc.pdf": (200, "application/pdf", "Alpha Packs binary")}
    )
    outcome = checker.check("https://x.example/doc.pdf")
    assert outcome.reachable and outcome.snippet is None


# ---------------------------------------------------------------- 失败与策略


def test_http_error_is_unreachable() -> None:
    checker = FakeFetchChecker({"https://gone.example/404": (404, "", "")})
    assert checker.check("https://gone.example/404").reachable is False


def test_unfollowed_redirect_is_unreachable() -> None:
    """真实运行验出的坑:307 重定向没走通 = 内容没拿到,不算可达。"""
    checker = FakeFetchChecker({"https://blocked.example/p": (307, "", "")})
    assert checker.check("https://blocked.example/p").reachable is False


def test_network_exception_is_unreachable_not_raised() -> None:
    checker = FakeFetchChecker({"https://dead.example/": urllib.error.URLError("dns fail")})
    assert checker.check("https://dead.example/").reachable is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.5/x",
        "http://192.168.1.1/",
        "http://localhost/x",
        "ftp://files.example/x",
    ],
)
def test_private_hosts_and_non_http_blocked_without_fetch(url: str) -> None:
    """被引 URL 来自不可信的 AI 回答 → 私网/非 http(s) 一律拒绝且不发请求。"""
    checker = FakeFetchChecker({})
    assert checker.check(url).reachable is False
    assert checker.fetched == []


def test_blocklist_and_allowlist() -> None:
    checker = FakeFetchChecker({}, blocked_domains=("tracker.example",))
    assert checker.check("https://sub.tracker.example/p").reachable is False
    assert checker.fetched == []

    allow_only = FakeFetchChecker(
        {"https://ok.example/p": (200, "text/html", "<p>Alpha Packs</p>")},
        allowed_domains=("ok.example",),
    )
    assert allow_only.check("https://ok.example/p").reachable is True
    assert allow_only.check("https://other.example/p").reachable is False
    assert allow_only.fetched == ["https://ok.example/p"]


# ---------------------------------------------------------------- 与 verify 集成


def test_verify_stores_snippet_in_evidence(demo_cfg: FrozenConfig) -> None:
    """封装验证:换上抓取实现,verify 零改动,摘录进证据层。"""
    from tests.test_verify import _extraction

    extraction = _extraction(demo_cfg)
    pages: dict[str, _Page | Exception] = {
        norm: (200, "text/html", "<p>Review of Alpha Packs gear.</p>")
        for norm in {c.normalized_url for c in extraction.citations}
    }
    records = verify(extraction, demo_cfg, checker=FakeFetchChecker(pages), now=FIXED_NOW)
    assert all(r.check_method == CHECK_PAGE_FETCH for r in records)
    assert all(r.snippet is not None and "Alpha Packs" in r.snippet for r in records)
