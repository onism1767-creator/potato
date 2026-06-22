"""共享 SSRF 安全策略测试(F-003):两个网络核验器共用同一把关 + 重定向防护。"""

import pytest

from aivis.pipeline.verify import (
    CHECK_HTTP_HEAD,
    HttpHeadChecker,
    _SafeRedirectHandler,
    is_safe_url,
)

_BRANDS = ("Alpha Packs",)


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://www.example.com/x", True),
        ("http://example.com", True),
        ("ftp://files.example/x", False),  # 非 http(s)
        ("file:///etc/passwd", False),
        ("http://localhost/x", False),
        ("http://127.0.0.1/admin", False),  # 环回
        ("http://10.0.0.5/x", False),  # 私网 A
        ("http://172.16.0.1/x", False),  # 私网 B
        ("http://192.168.1.1/x", False),  # 私网 C
        ("http://169.254.169.254/latest/meta-data/", False),  # 云元数据(链路本地)
        ("http://[::1]/x", False),  # IPv6 环回
        ("http://internal.local/x", False),  # .local
        ("http://user:pass@127.0.0.1/x", False),  # userinfo 混淆,仍按 host 判私网
    ],
)
def test_is_safe_url(url: str, safe: bool) -> None:
    assert is_safe_url(url) is safe


def test_head_checker_blocks_unsafe() -> None:
    """HttpHeadChecker 现在共用安全策略:私网/非 http(s) 判不可达。"""
    checker = HttpHeadChecker()
    assert checker.check("http://127.0.0.1/admin").reachable is False
    assert checker.check("ftp://x/y").reachable is False
    assert checker.check("http://169.254.169.254/").reachable is False


def test_page_fetch_blocks_unsafe_without_fetch() -> None:
    from tests.test_page_fetch import FakeFetchChecker

    checker = FakeFetchChecker({})
    for url in ("http://10.0.0.1/x", "http://localhost/x", "ftp://x/y", "http://[fe80::1]/x"):
        assert checker.check(url).reachable is False
    assert checker.fetched == []  # 一个请求都没发


def test_safe_redirect_handler_rejects_private_target() -> None:
    """重定向到私网/非 http(s) 被拦下(防重定向 SSRF)。"""
    import urllib.request

    handler = _SafeRedirectHandler()
    req = urllib.request.Request("https://a.example/x")
    with pytest.raises(urllib.error.URLError, match="unsafe redirect"):
        handler.redirect_request(
            req=req,
            fp=None,
            code=302,
            msg="",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )


def test_safe_redirect_handler_allows_public_target() -> None:
    """公开→公开的重定向放行(返回一个新 Request)。"""
    handler = _SafeRedirectHandler()
    import urllib.request

    req = urllib.request.Request("https://a.example/x")
    result = handler.redirect_request(
        req=req, fp=None, code=302, msg="", headers={}, newurl="https://b.example/y"
    )
    assert result is not None and result.full_url == "https://b.example/y"


def test_max_redirects_bounded() -> None:
    assert _SafeRedirectHandler.max_redirections <= 5


def test_both_checkers_share_policy() -> None:
    """同一个不安全 URL,head 与 fetch 都判不可达(策略真的共享)。"""
    from tests.test_page_fetch import FakeFetchChecker

    unsafe = "http://192.168.0.1/admin"
    assert HttpHeadChecker().check(unsafe).reachable is False
    assert FakeFetchChecker({}).check(unsafe).reachable is False
    assert HttpHeadChecker().method == CHECK_HTTP_HEAD
