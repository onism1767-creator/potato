"""gui server 骨架测试:只听 127.0.0.1 + Host 守卫 + token 守卫(离线,临时端口)。"""

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping

import pytest

from aivis.gui.server import GuiServer, build_server


@pytest.fixture()
def server() -> Iterator[GuiServer]:
    s = build_server(port=0)  # 临时端口
    thread = threading.Thread(target=s.serve_forever, daemon=True)
    thread.start()
    yield s
    s.shutdown()
    s.server_close()
    shutil.rmtree(s.work_root, ignore_errors=True)


def _req(
    s: GuiServer,
    path: str,
    *,
    method: str = "GET",
    host: str | None = None,
    token: str | None = None,
    body: Mapping[str, object] | None = None,
) -> tuple[int, str]:
    port = s.server_address[1]
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if token is not None:
        headers["X-Aivis-Token"] = token
    data: bytes | None = None
    if method == "POST":
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, headers=headers, data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_binds_localhost_only(server: GuiServer) -> None:
    assert server.server_address[0] == "127.0.0.1"


def test_serves_index_with_token(server: GuiServer) -> None:
    code, body = _req(server, "/")
    assert code == 200
    assert "aivis" in body
    assert server.token in body  # token 嵌进首页供前端调 /api 带回


def test_index_is_wizard(server: GuiServer) -> None:
    # 3 步向导页(非占位):有品牌输入框 + 调 /api/draft 的脚本
    _, body = _req(server, "/")
    assert 'id="focal_name"' in body and 'id="category"' in body
    assert "/api/draft" in body and "/api/run" in body


def test_report_token_and_404_before_run(server: GuiServer) -> None:
    code, _ = _req(server, "/report", token=server.token)  # 带 token,但还没跑 → 404
    assert code == 404
    code, _ = _req(server, "/report")  # 不带 token → 403
    assert code == 403


def test_healthz_ok(server: GuiServer) -> None:
    code, body = _req(server, "/healthz")
    assert code == 200
    assert json.loads(body)["status"] == "ok"


def test_bad_host_rejected(server: GuiServer) -> None:
    # 🟥 DNS-rebinding 守卫:Host 非 127.0.0.1/localhost → 403
    code, _ = _req(server, "/", host="evil.example")
    assert code == 403


def test_post_ping_requires_token(server: GuiServer) -> None:
    # 🟥 CSRF 守卫:改动类 POST 无 token → 403;带正确 token → 200
    code, _ = _req(server, "/api/ping", method="POST")
    assert code == 403
    code, body = _req(server, "/api/ping", method="POST", token=server.token)
    assert code == 200
    assert json.loads(body)["pong"] is True


def test_unknown_path_404(server: GuiServer) -> None:
    code, _ = _req(server, "/nope")
    assert code == 404


def test_draft_endpoint_round_trip(server: GuiServer) -> None:
    # 真 HTTP:无 token → 403;带 token + body → 200 出 24 题(server 路由 + _read_json 通)
    body = {"focal_name": "Acme Corp", "category": "widgets", "draft_tier": "free"}
    code, _ = _req(server, "/api/draft", method="POST", body=body)
    assert code == 403  # 无 token
    code, text = _req(server, "/api/draft", method="POST", token=server.token, body=body)
    assert code == 200
    assert len(json.loads(text)["questions"]) == 24


def test_run_status_report_e2e(server: GuiServer) -> None:
    # 全 HTTP 端到端:POST /api/run(mock)→ 轮询 /api/status 到 done → GET /report 出 HTML
    body = {
        "focal_name": "Acme Corp",
        "focal_domain": "acme.com",
        "category": "widgets",
        "provider": "mock",
        "questions": [
            {"id": "d01", "intent": "discovery", "text": "best widgets?"},
            {"id": "b01", "intent": "brand_defense", "text": "Is Acme Corp good?"},
        ],
    }
    code, _ = _req(server, "/api/run", method="POST", token=server.token, body=body)
    assert code == 200
    state: dict[str, object] = {}
    end = time.time() + 90
    while time.time() < end:
        _, s = _req(server, "/api/status", token=server.token)
        state = json.loads(s)
        if state["state"] in ("done", "error"):
            break
        time.sleep(0.3)
    assert state.get("state") == "done", f"run e2e 未成功: {state}"
    code, rep = _req(server, "/report", token=server.token)
    assert code == 200 and rep.startswith("<!DOCTYPE html>")


def test_draft_bad_input_400(server: GuiServer) -> None:
    # 缺 focal_name → api 抛 ValueError → server 映射 400
    code, _ = _req(
        server, "/api/draft", method="POST", token=server.token, body={"draft_tier": "free"}
    )
    assert code == 400


def test_tiers_round_trip(server: GuiServer) -> None:
    # 三档成本总览经 HTTP:带 token + questions → 200,返回三档升序
    body = {
        "focal_name": "Acme Corp",
        "focal_domain": "acme.com",
        "category": "widgets",
        "budget_cap_usd": 13.0,
        "questions": [{"id": "d01", "intent": "discovery", "text": "best widgets?"}],
    }
    code, text = _req(server, "/api/tiers", method="POST", token=server.token, body=body)
    assert code == 200
    ids = [t["id"] for t in json.loads(text)["tiers"]]
    assert ids == ["economy", "standard", "high"]


def test_draft_estimate_round_trip(server: GuiServer) -> None:
    body = {"focal_name": "Acme Corp", "focal_domain": "acme.com", "category": "widgets"}
    code, text = _req(server, "/api/draft_estimate", method="POST", token=server.token, body=body)
    assert code == 200 and json.loads(text)["est_usd"] > 0


def test_draft_ai_without_key_400(server: GuiServer) -> None:
    # AI 档无 api_key → ValueError → 干净 400(不触网、非 500),前端可显消息
    body = {"focal_name": "Acme Corp", "category": "widgets", "draft_tier": "ai"}
    code, text = _req(server, "/api/draft", method="POST", token=server.token, body=body)
    assert code == 400
    assert "api_key" in json.loads(text)["error"]
