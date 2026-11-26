# -*- coding: utf-8 -*-
"""
server/__main__.py
────────────────────────────────────────────────────────────────────────────
包入口：python -m server.app 时由本模块触发启动。
（app.py 末尾的 `if __name__ == "__main__"` 在包模式下不会执行，故启动逻辑放此处）
"""
import os
from .app import app, run_server

if __name__ == "__main__":
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"), exist_ok=True)
    run_server()
