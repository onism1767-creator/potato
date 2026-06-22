"""构建 Windows「下载即用」便携包(嵌入式 Python + 一键 .bat),绕过 Smart App Control。

为什么这样:Smart App Control 拦截**自制未签名 exe**,但放行**微软/python.org 签名的
官方 python.exe**。所以可执行的是官方 embeddable Python,我们的代码只是它加载的数据 ——
不触发 SAC,也无需代码签名证书。便携包里有真 python.exe,GUI「跑测量」的子进程走普通
`python -m aivis.cli` 分支即可(非冻结)。

产物:dist/AI-Visibility-Easy-Tool-windows.zip
  解压后双击 AI-Visibility-Easy-Tool.bat → 起本地向导 + 开浏览器。key 仍只在本机。

用法(dev venv 内,需联网下载 embeddable + 依赖):python tools/build_portable.py
"""

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "AI-Visibility-Easy-Tool"
BUILD = ROOT / "build" / "portable"
STAGE = BUILD / NAME
# embeddable 版本对齐当前解释器,保证 pip 解析的扩展轮子(如 pydantic_core)与之匹配
PYVER = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
EMBED_URL = f"https://www.python.org/ftp/python/{PYVER}/python-{PYVER}-embed-amd64.zip"
CACHE = BUILD / f"python-{PYVER}-embed-amd64.zip"

_BAT = """@echo off
cd /d "%~dp0"
echo Starting AI Visibility Easy Tool. A browser tab will open shortly.
echo Keep this window open while you use the tool; close it to stop.
".\\python\\python.exe" -m aivis
pause
"""

_README_ZH = """AI Visibility Easy Tool —— 本地可见度测量工具(便携版)

【怎么用】
1. 先把这个文件夹整个解压出来(不要在压缩包里直接双击运行)。
2. 双击  AI-Visibility-Easy-Tool.bat
3. 会弹出一个黑色窗口并自动打开浏览器;按网页里的 3 步操作即可。
4. 用完直接关掉那个黑色窗口就停止了。

【隐私 / API key 安全(可自行核验,不是口头保证)】
- 全程只在你自己电脑上运行(127.0.0.1),没有任何云端服务器。
- 免费档不需要 key、零成本。
- 只有选「AI 起草」或「真实测量」时才需要你自己的 Anthropic API key:
  key 只存在内存里,只发往 api.anthropic.com(你自己的账号),
  绝不写入硬盘、不写日志、不发往任何开发者服务器。
- 开源 MIT,可自行审阅代码。

【打不开?】
- 浏览器没自动开:手动打开黑窗里显示的地址(形如 http://127.0.0.1:8765/)。
- 本工具用的是微软签名的官方 Python(不是自制 exe),所以不会被「智能应用控制」拦。
"""

_README_EN = """AI Visibility Easy Tool — local visibility measurement (portable)

[How to use]
1. Unzip this whole folder first (do not run it from inside the .zip).
2. Double-click  AI-Visibility-Easy-Tool.bat
3. A small black window opens and your browser launches automatically;
   follow the 3 steps on the page.
4. When you're done, just close that black window to stop.

[Privacy / API key safety (verifiable, not just a promise)]
- Everything runs only on your own computer (127.0.0.1). There is no cloud server.
- The free tier needs no key and costs nothing.
- A key is only needed if you choose "AI drafting" or a "real measurement":
  your key stays in memory only, is sent only to api.anthropic.com (your own account),
  and is never written to disk, never logged, and never sent to any developer server.
- Open source (MIT) — you can audit the code yourself.

[Won't open?]
- If the browser didn't open, manually visit the address shown in the black window (e.g. http://127.0.0.1:8765/).
- This tool runs on Microsoft-signed official Python (not a self-built exe),
  so Smart App Control will not block it.
"""


def _fetch_embed() -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    if not CACHE.exists():
        print(f"下载 embeddable Python: {EMBED_URL}")
        urllib.request.urlretrieve(EMBED_URL, CACHE)
    return CACHE


def _enable_site_and_app(python_dir: Path) -> None:
    """改 embeddable 的 ._pth:启用 import site + 把同级 app/ 加进 sys.path。"""
    pth = next(python_dir.glob("python*._pth"))
    lines = [ln.replace("#import site", "import site") for ln in pth.read_text().splitlines()]
    if "..\\app" not in lines:
        lines.append("..\\app")
    if "import site" not in [ln.strip() for ln in lines]:
        lines.append("import site")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    python_dir = STAGE / "python"
    python_dir.mkdir(parents=True)
    with zipfile.ZipFile(_fetch_embed()) as z:  # 1) 官方 embeddable python
        z.extractall(python_dir)
    _enable_site_and_app(python_dir)  # 2) 让它能 import 我们的 app/

    app = STAGE / "app"  # 3) 项目 + 依赖装进 app/(纯目录,embeddable 据 ._pth import)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-build-isolation",
            "--target",
            str(app),
            str(ROOT),
        ],
        check=True,
    )

    (STAGE / f"{NAME}.bat").write_text(_BAT, encoding="utf-8")  # 4) 启动脚本 + 中英说明各一份
    (STAGE / "README-简体中文.txt").write_text(_README_ZH, encoding="utf-8")
    (STAGE / "README-English.txt").write_text(_README_EN, encoding="utf-8")

    out_base = ROOT / "dist" / f"{NAME}-windows"  # 5) 打包 zip
    out_base.parent.mkdir(exist_ok=True)
    zip_path = out_base.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(out_base), "zip", root_dir=BUILD, base_dir=NAME)
    print(f"OK -> {zip_path}  ({zip_path.stat().st_size // (1024 * 1024)} MB)")


if __name__ == "__main__":
    main()
