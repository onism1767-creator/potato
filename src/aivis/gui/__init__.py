"""gui —— 本地网页向导(Phase 2,只监听 127.0.0.1)。

薄外壳:收集输入、调现成 CLI/库、展示报告。骨架见 server.py;端点接线(2.3)、
向导前端(2.4)后续。安全:绑 127.0.0.1 + 校验 Host 头 + 每会话 token(挡本地 CSRF/
DNS-rebinding);key 永不落盘(Phase 2 内存,Phase 3 keyring)。
"""
