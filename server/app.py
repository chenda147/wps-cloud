# -*- coding: utf-8 -*-
"""
server/app.py - 应用入口（壳）
职责：创建 Flask 应用、注册功能蓝图、启动统一调度器。
所有路由逻辑在 routes_*.py，共享业务逻辑在 helpers.py，
全局状态在 state.py，工具函数在 utils.py，WPS 客户端在 models.py。
"""
import os
import sys
import threading

from flask import Flask, render_template, jsonify

# ─── 兼容两种启动方式 ───
# 既支持 `python -m server`（包模式，相对导入可用），
# 也支持直接 `python app.py`（把 server 目录注入 sys.path 并伪装为包）。
if __package__ in (None, ""):
    # 直接运行时：_THIS_DIR = server/，父目录 = 项目根
    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    _PARENT_DIR = os.path.dirname(_THIS_DIR)
    if _PARENT_DIR not in sys.path:
        sys.path.insert(0, _PARENT_DIR)
    # 让本模块以 `server.app` 身份参与相对导入
    __package__ = os.path.basename(_THIS_DIR)
    # 把自身注册进 sys.modules，使 `.state` 等相对导入可解析
    import types
    _pkg = types.ModuleType(__package__)
    _pkg.__path__ = [_THIS_DIR]
    _pkg.__package__ = __package__
    sys.modules[__package__] = _pkg
    sys.modules[f"{__package__}.app"] = sys.modules[__name__]
    del types

# ─── 模块化分层（解耦） ───
from .state import *
from .utils import *
from .models import WPSClient, API_BASE
from .helpers import _unified_scheduler

SERVER_VERSION = "1.0.2"

# ══════════════════════════════════════════════════
# 内存日志缓冲：供前端（含手机端）实时读取最近日志
# 不落盘，仅保留最近 N 条，支持级别过滤 / 搜索
# ══════════════════════════════════════════════════
import logging as _logging
import collections as _collections

_LOG_BUFFER = _collections.deque(maxlen=800)

class _RingLogHandler(_logging.Handler):
    def emit(self, record):
        try:
            _LOG_BUFFER.append({
                "t": record.created,                       # epoch 秒
                "levelno": record.levelno,
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass

_ring = _RingLogHandler()
_ring.setFormatter(_logging.Formatter("%(message)s"))
_logging.getLogger().addHandler(_ring)
# 确保 root logger 自身也能捕获子 logger 的 WARNING+
_logging.getLogger().setLevel(_logging.DEBUG)

def get_log_buffer():
    return _LOG_BUFFER

app = Flask(__name__)
app.secret_key = __import__("secrets").token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB 上传限制

# ══════════════════════════════════════════════════
# 蓝图注册（路由按功能拆分到独立模块）
# ══════════════════════════════════════════════════
from .routes_public import bp as public_bp
from .routes_admin import bp as admin_bp
from .routes_transfer import bp as transfer_bp
from .routes_strm import bp as strm_bp
from .routes_chase import bp as chase_bp
from .routes_client import bp as client_bp

app.register_blueprint(public_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(transfer_bp)
app.register_blueprint(strm_bp)
app.register_blueprint(chase_bp)
app.register_blueprint(client_bp)

# 兜底：未匹配路由渲染首页（前端路由由 page_lock/index 处理）
@app.route("/healthz")
def healthz():
    return "ok"

# 全局异常拦截：把 500 异常转成 JSON 返回，避免 Flask 默认 HTML 错误页
# 导致前端 r.json() 解析失败而误报“网络错误”，同时在后端控制台打印真实 traceback 便于排查
import traceback as _tb
@app.errorhandler(500)
def _handle_500(exc):
    print("[500-ERROR]", repr(exc), flush=True)
    _tb.print_exc()
    return jsonify({"success": False, "message": f"服务器内部错误: {exc}"}), 500

@app.errorhandler(Exception)
def _handle_uncaught(exc):
    print("[UNCAUGHT]", repr(exc), flush=True)
    _tb.print_exc()
    return jsonify({"success": False, "message": f"服务器内部错误: {exc}"}), 500

# ══════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════
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
    threading.Thread(target=_unified_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
