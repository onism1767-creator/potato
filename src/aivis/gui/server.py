"""server —— 本地向导后端骨架(2.2):只听 127.0.0.1,Host 守卫 + 每会话 token。

骨架范围:起服务、服务占位首页、/healthz、token 保护的 /api/ping。
端点接线(起草/validate/冻结/estimate/run 轮询)= 2.3;真向导前端 = 2.4。

安全(分发文档 §3):
- 绑 127.0.0.1(不对外暴露);
- 每个请求校验 Host 头(只认 127.0.0.1/localhost[:port])→ 挡 DNS-rebinding;
- 每会话随机 token,改动类 /api POST 必须带 X-Aivis-Token → 挡本地恶意网页 CSRF;
- key 永不落盘(后续步骤在内存里传子进程 env)。
"""

import json
import secrets
import sys
import tempfile
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from aivis.gui import api
from aivis.gui import runner as runner_mod
from aivis.gui.runner import Runner

_DEFAULT_PORT = 8765
# 前端静态页随包分发(package-data);便携包里也在 app/aivis/gui/static/
_STATIC_DIR = Path(__file__).parent / "static"

_INTERNAL_ERROR = "向导内部出错了(细节没有回传给浏览器)。请重试一次;仍不行请关掉向导窗口重新启动。"
# 向导的请求体都是几 KB 的 JSON;声明得比这还大的一律不读、直接拒绝
_MAX_BODY_BYTES = 1_000_000

# 不花钱端点(2.3):dict→dict 逻辑在 gui.api;run(花钱/subprocess)留下一轮
_POST_ENDPOINTS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "/api/draft": api.draft,
    "/api/validate": api.validate,
    "/api/estimate": api.estimate,
    "/api/tiers": api.tiers,  # 三档 run 成本总览(旧接口,保留)
    "/api/models": api.models,  # 各家全部表面 + 预估("选测哪些 AI")
    "/api/draft_estimate": api.draft_estimate,  # AI 起草这一步的成本上界
}


class GuiServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 每会话 token / 占位首页 / 允许的 Host 集合。"""

    daemon_threads = True
    # Windows 上 SO_REUSEADDR 的含义是"允许别的进程并绑同一个端口":双击两次启动脚本会悄悄起两个
    # 服务、两个 token,连接落到哪个由系统决定(页面随机 403、在跑的任务被影子化)。Windows 上关掉,
    # 第二个实例会老实报"端口被占用";其它系统保留(那里它只是允许重启后立刻复用端口)。
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, server_address: tuple[str, int], token: str, index_html: str) -> None:
        super().__init__(server_address, _Handler)
        self.token = token
        self.index_html = index_html
        self.runner = Runner()  # 单任务 run 管理
        self.work_root = Path(tempfile.mkdtemp(prefix="aivis_gui_"))  # 冻结配置/数据(本机)
        port = self.server_address[1]
        self.allowed_hosts = {
            "127.0.0.1",
            "localhost",
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "aivis-gui/0.3"

    @property
    def _gui(self) -> GuiServer:
        return cast(GuiServer, self.server)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self._gui.allowed_hosts

    def _token_ok(self) -> bool:
        # 常量时间比较,避免 token 计时侧信道(按字节比:头里带非 ASCII 字符时照样拒绝,不抛错)
        given = self.headers.get("X-Aivis-Token", "").encode("utf-8", "replace")
        return secrets.compare_digest(given, self._gui.token.encode("utf-8"))

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send(403, "text/plain; charset=utf-8", b"forbidden host")
            return
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", self._gui.index_html.encode("utf-8"))
        elif self.path == "/healthz":
            self._json(200, {"status": "ok"})
        elif self.path == "/api/status":  # 读任务状态(token 守卫:防本地页偷读)
            if not self._token_ok():
                self._json(403, {"error": "bad or missing token"})
            else:
                self._json(200, self._gui.runner.status())
        elif self.path.split("?", 1)[0] == "/report":  # 看报告(token 守卫;?surface=N 看第 N 个表面)
            if not self._token_ok():
                self._json(403, {"error": "bad or missing token"})
                return
            st = self._gui.runner.status()
            query = parse_qs(urlparse(self.path).query)
            rp = st.get("report_path")
            if "surface" in query:
                paths = st.get("report_paths") or []
                try:
                    rp = paths[int(query["surface"][0])]
                except (ValueError, IndexError):
                    rp = None
            if rp and not Path(rp).resolve().is_relative_to(self._gui.work_root.resolve()):
                rp = None  # 只读本次向导自己工作目录里的报告(路径来自子进程输出,多一道保险)
            if not rp or not Path(rp).exists():
                self._send(404, "text/plain; charset=utf-8", b"no report yet")
            else:
                body = Path(rp).read_text(encoding="utf-8").encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:
        # 先把请求体收完,再做任何回应(包括拒绝)。不读就回、随即关连接的话,系统会向对方发
        # "连接重置",对方有时收不到这句回应、只看到连接断开(本机实测每 100 次有 1~2 次)。
        raw = self._read_body()
        if raw is None:
            self._json(400, {"error": "请求体过大或长度不合法"})
            return
        if not self._host_ok():
            self._send(403, "text/plain; charset=utf-8", b"forbidden host")
            return
        if not self._token_ok():
            self._json(403, {"error": "bad or missing token"})
            return
        if self.path == "/api/ping":  # 骨架健康探针(token 守卫已验)
            self._json(200, {"pong": True})
            return
        if self.path == "/api/run":  # 有状态:冻结 + 起 run 子进程(key 走 env)
            try:
                self._json(
                    200,
                    runner_mod.start_run(self._gui.runner, _parse_json(raw), self._gui.work_root),
                )
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception:
                self._json(500, {"error": _INTERNAL_ERROR})
            return
        endpoint = _POST_ENDPOINTS.get(self.path)
        if endpoint is None:
            self._json(404, {"error": "not found"})
            return
        try:
            self._json(200, endpoint(_parse_json(raw)))
        except ValueError as e:  # 含 pydantic ValidationError(其为 ValueError 子类)
            self._json(400, {"error": str(e)})
        except RuntimeError as e:  # 上游失败(如 AI 档起草 DraftError):非客户端错,返 502
            self._json(502, {"error": str(e)})
        except Exception:
            # 其余一切(磁盘满、类型错、没料到的 SDK 异常…):回一句固定的话。
            # 以前这里不接 → 连接直接断开、前端永远停在"处理中";也不回传异常文本(可能带敏感信息)
            self._json(500, {"error": _INTERNAL_ERROR})

    def _read_body(self) -> bytes | None:
        """读完请求体(只读不解析);长度不合法或超过 _MAX_BODY_BYTES 返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 <= length <= _MAX_BODY_BYTES:
            return None
        return self.rfile.read(length) if length else b""

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 本地动态页:禁缓存,避免浏览器/重启后拿到旧 index.html(更新即生效)
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # 静默:不往 stderr 刷访问日志
        pass


def _parse_json(raw: bytes) -> dict[str, Any]:
    """请求体 → JSON 对象(空体当作 {});不是对象或不是合法 JSON → ValueError(do_POST 映射 400)。"""
    if not raw:
        return {}
    data = json.loads(raw)  # JSONDecodeError 是 ValueError 子类
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return cast(dict[str, Any], data)


def _render_index(token: str) -> str:
    """读静态 3 步向导页(gui/static/index.html)并注入每会话 token(meta)。"""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__AIVIS_TOKEN__", token)


def build_server(host: str = "127.0.0.1", port: int = _DEFAULT_PORT) -> GuiServer:
    """构造(不启动)本地向导服务;token 每次新随机。测试用 port=0 取临时端口。"""
    token = secrets.token_urlsafe(32)
    return GuiServer((host, port), token, _render_index(token))


def serve(*, port: int = _DEFAULT_PORT, open_browser: bool = True) -> None:
    """启动本地向导(阻塞至 Ctrl-C);只绑 127.0.0.1。"""
    try:
        server = build_server("127.0.0.1", port)
    except (OSError, OverflowError) as e:
        # 端口被占(多半是向导已经在运行)或端口号不合法:说人话,不吐 traceback
        url = f"http://127.0.0.1:{port}/"
        print(
            f"无法在端口 {port} 启动向导({e.__class__.__name__})。"
            f"如果向导已经在运行,直接在浏览器打开 {url} 即可;否则换一个端口:aivis gui --port 8766",
            flush=True,
        )
        if open_browser and isinstance(e, OSError):
            webbrowser.open(url)
        return
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"aivis 本地向导运行中: {url}  (Ctrl-C 退出)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()
