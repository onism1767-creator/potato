"""`python -m aivis`:无参数起 GUI 向导,有参数走 CLI(同 app.main 分流)。"""

from aivis.app import main

if __name__ == "__main__":
    main()
