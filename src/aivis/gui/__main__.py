"""`python -m aivis.gui` 及打包 exe 的入口:起本地向导并自动开浏览器。

双击成品 exe 即走到这里 —— 启动只监听 127.0.0.1 的网页向导(免费档零成本可跑通)。
"""

from aivis.gui.server import serve


def main() -> None:
    serve(open_browser=True)


if __name__ == "__main__":
    main()
