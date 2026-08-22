# -*- coding: utf-8 -*-
"""
server/__main__.py
────────────────────────────────────────────────────────────────────────────
包入口：python -m server.app 时由本模块触发启动。
（app.py 末尾的 `if __name__ == "__main__"` 在包模式下不会执行，故启动逻辑放此处）
"""
import os
import threading
from .app import app, load_config, BASE_DIR, _unified_scheduler

if __name__ == "__main__":
    cfg = load_config()
    port = cfg.get("server_port", 58000)
    print("=" * 55)
    print("  WPS 365 云盘管理面板 v2")
    print("=" * 55)
    print(f"  首页: http://0.0.0.0:{port}/")
    print(f"  管理后台: http://0.0.0.0:{port}/admin/login")
    print(f"  默认账号: admin / admin")
    print(f"  定时调度: 追更/STRM 按各自时间表自动执行")
    print(f"  按 Ctrl+C 停止")
    print("=" * 55)
    os.makedirs(os.path.join(BASE_DIR, "templates"), exist_ok=True)
    threading.Thread(target=_unified_scheduler, daemon=True).start()
    from .tunnel_client import start_tunnel_if_configured
    start_tunnel_if_configured(cfg)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
