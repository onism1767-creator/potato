"""被引网页核验的加固(2026-11 终审;只影响 --check head / --check fetch)。

被引 URL 和网页内容都是不可信数据:畸形 URL、IP 混淆写法、恶意 HTML、慢速滴漏的响应、
乱写的编码,都不能让核验崩溃、卡死或打到本机/内网。测试只用本机回环上的假服务器,不出网。
"""

import http.server
import threading
import time
from collections.abc import Iterator

import pytest

from aivis.pipeline import verify as verify_mod
from aivis.pipeline.verify import HttpHeadChecker, PageFetchChecker, html_to_text, is_safe_url

_BRANDS = {"Alpha Packs": ("Alpha Packs",)}


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost./x",  # 带根点的 localhost(以前能绕过)
        "http://foo.localhost/x",
        "http://2130706433/x",  # 127.0.0.1 的十进制写法
        "http://127.1/x",
        "http://0x7f.0.0.1/x",
        "http://0177.0.0.1/x",
        "http://017700000001/x",
        "http://[foo]/x",  # 畸形:以前直接抛 ValueError
        "http://[::1/x",
    ],
)
def test_obfuscated_or_malformed_hosts_are_unsafe_and_never_raise(url: str) -> None:
    assert is_safe_url(url) is False
    # 两个核验器都按"不可达"如实记录,不发请求、不抛异常(契约:check 不许向外抛)
    assert HttpHeadChecker().check(url).reachable is False
    assert PageFetchChecker(_BRANDS, delay_seconds=0).check(url).reachable is False


@pytest.mark.parametrize("url", ["https://example.com/a", "https://shop.example.co.uk/x?y=1"])
def test_ordinary_domains_are_still_allowed(url: str) -> None:
    assert is_safe_url(url) is True


def test_hostile_html_cannot_stall_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """几十 KB 的未闭合标签会让标准库解析器平方级地慢(实测 25KB ≈ 23 秒):有总时限兜底。"""
    monkeypatch.setattr(verify_mod, "_HTML_PARSE_BUDGET_S", 0.3)
    hostile = "Alpha Packs review. " + "<a " * 40_000  # ≈120KB
    start = time.monotonic()
    text = html_to_text(hostile)
    assert time.monotonic() - start < 5.0  # 没有时限时这里要跑好几分钟
    assert "Alpha Packs review" in text  # 时限之前解析出的正文仍然可用


def test_normal_html_is_parsed_completely() -> None:
    body = "<html><body>" + "<p>Alpha Packs is great.</p>" * 500 + "<p>THE END</p></body></html>"
    assert html_to_text(body).endswith("THE END")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/bogus-charset":
            body = b"<html><body>Alpha Packs review</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=x-not-a-codec")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/drip":  # 声称有 50 万字节,每 0.05 秒只吐 1 个
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "500000")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture()
def loopback() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_unknown_charset_falls_back_instead_of_marking_unreachable(loopback: str) -> None:
    # 直接调 _fetch(唯一网络触点):回环地址过不了 check 的安全策略,这正是策略该做的
    checker = PageFetchChecker(_BRANDS, delay_seconds=0, timeout=3)
    status, content_type, text = checker._fetch(loopback + "/bogus-charset")
    assert status == 200 and "html" in content_type
    assert "Alpha Packs review" in text  # 以前:LookupError → 被记成"不可达"


def test_slow_drip_response_hits_the_total_time_budget(loopback: str) -> None:
    """socket 超时只管"多久没数据";每隔一会儿吐 1 字节的响应以前能把一个 URL 拖上几小时。"""
    checker = PageFetchChecker(_BRANDS, delay_seconds=0, timeout=3, total_timeout=0.4)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        checker._fetch(loopback + "/drip")
    assert time.monotonic() - start < 3.0
