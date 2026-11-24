"""`python -m aivis` 统一入口:无参数起 GUI 向导,有参数走 CLI。

便携包的启动脚本即 `python -m aivis`(无参)→ 起本地向导并开浏览器。
"""

import sys


def main() -> None:
    if len(sys.argv) > 1:  # 带参数 → CLI
        from aivis.cli import main as cli_main

        raise SystemExit(cli_main())  # 把 CLI 的退出码带出去(脚本/CI 才判得出失败)
    else:  # 无参数(双击启动脚本)→ 起本地向导并开浏览器
        from aivis.gui.server import serve

        serve(open_browser=True)


if __name__ == "__main__":
    main()
