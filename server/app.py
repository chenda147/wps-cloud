# -*- coding: utf-8 -*-
"""
server/app.py - 应用入口（壳）
职责：创建 Flask 应用、注册功能蓝图、启动统一调度器。
所有路由逻辑在 routes_*.py，共享业务逻辑在 helpers.py，
全局状态在 state.py，工具函数在 utils.py，WPS 客户端在 models.py。
"""
import os
import sys
import hashlib
import secrets
import threading
from datetime import timedelta

from flask import Flask, render_template, jsonify, request, session

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

def _machine_id():
    """取一个跨进程/跨重启都稳定的机器标识，用于密钥落盘失败时派生密钥。"""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except Exception:
            pass
    return os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""


def _load_or_create_secret_key():
    """持久化 session 密钥，避免每次重启后所有登录态失效。

    优先级：环境变量 SECRET_KEY > DATA_DIR/secret.key > 稳定派生密钥。
    """
    env_key = (os.environ.get("SECRET_KEY") or "").strip()
    if env_key:
        return env_key
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
    except Exception as e:
        print(f"[session] 读取 {path} 失败: {e}", flush=True)
    key = secrets.token_hex(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return key
    except Exception as e:
        # 落盘失败时退化为稳定派生密钥：宁可安全性略降，也不能让用户反复掉登录
        print(f"[session] 写入 {path} 失败({e})，改用派生密钥", flush=True)
        seed = "|".join([os.path.abspath(path), _machine_id()])
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB 上传限制
# ⚠ 关键：Flask 默认 cookie 名是 "session"，与 NAS 上其它应用（同一域名、不同端口）
# 的同名 cookie 会互相覆盖，导致每隔十几秒就被踢下线。改用独占名称隔离。
app.config["SESSION_COOKIE_NAME"] = "wps_cloud_sid"
app.config["SESSION_COOKIE_PATH"] = "/"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# ─── 认证诊断：每个受保护请求打印 cookie / session 状态，定位“登录后秒退”
# 输出到 stdout（docker logs 可见）。线上稳定后可注释掉。
@app.before_request
def _auth_diag():
    p = request.path
    if p.startswith(("/api/", "/admin/")) or p == "/" or p.startswith("/play/") or p.startswith("/img/"):
        ck_name = app.config["SESSION_COOKIE_NAME"]
        has_cookie = bool(request.cookies.get(ck_name))
        # 当前进程的密钥签名后 8 位，用来判断请求是否落在“换了密钥的另一个进程”
        sig = hashlib.sha256(app.secret_key.encode("utf-8")).hexdigest()[:8]
        print(f"[auth] {request.method} {p} "
              f"cookie={'Y' if has_cookie else 'N'} "
              f"sid={session.get('logged_in')} "
              f"adm={session.get('admin_logged_in')} "
              f"key={sig} "
              f"host={request.host} "
              f"xff={request.headers.get('X-Forwarded-For', '-')}",
              flush=True)


# ══════════════════════════════════════════════════
# 蓝图注册（路由按功能拆分到独立模块）
# ══════════════════════════════════════════════════
from .routes_public import bp as public_bp
from .routes_admin import bp as admin_bp
from .routes_transfer import bp as transfer_bp
from .routes_strm import bp as strm_bp
from .routes_chase import bp as chase_bp
from .routes_client import bp as client_bp
from .routes_telegram import bp as telegram_bp

app.register_blueprint(public_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(transfer_bp)
app.register_blueprint(strm_bp)
app.register_blueprint(chase_bp)
app.register_blueprint(client_bp)
app.register_blueprint(telegram_bp)

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
def run_server():
    cfg = load_config()
    port = int(cfg.get("server_port", 58000))
    print("=" * 55)
    print("  WPS 365 云盘管理面板 v2")
    print("=" * 55)
    print(f"  首页: http://0.0.0.0:{port}/")
    print(f"  管理后台: http://0.0.0.0:{port}/admin/login")
    print(f"  默认账号: admin / admin")
    print(f"  定时调度: 追更/STRM 按各自时间表自动执行")
    print(f"  按 Ctrl+C 停止")
    print("=" * 55)
    # 启动时清理过期的临时文件，避免磁盘被历史大文件占满
    try:
        n = cleanup_temp_dir(cfg.get("temp_download_dir") or os.path.join(DATA_DIR, "temp_downloads"))
        if n:
            print(f"[cleanup] 已清理 {n} 个过期临时文件")
    except Exception:
        pass
    # 恢复重启前未完成的客户端任务队列：否则服务端一重启，正在排队/下载的
    # 追更任务全部丢失（客户端还在跑，但队列空了 → 不再有下一集）。
    try:
        from .state import _load_client_tasks
        _load_client_tasks()
    except Exception:
        pass
    threading.Thread(target=_unified_scheduler, daemon=True).start()
    from .tunnel_client import start_tunnel_if_configured
    start_tunnel_if_configured(cfg)
    # 优先用 waitress（生产级 WSGI，避免 Flask 内置服务器的并发/稳定性问题）
    try:
        from waitress import serve
        print("  WSGI: waitress")
        serve(app, host="0.0.0.0", port=port, threads=12, channel_timeout=900, _quiet=True)
    except ImportError:
        print("  WSGI: Flask 内置服务器（建议 pip install waitress 提升稳定性）")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run_server()
