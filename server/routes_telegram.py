# -*- coding: utf-8 -*-
"""
server/routes_telegram.py - Telegram 资源中心（管理员）
────────────────────────────────────────────────────────────────────────────
功能：Telegram 账号登录、频道管理、频道内文件搜索、锁定资源列表。
定位：服务端只做搜索与资源信息推送，文件下载由 Telegram 客户端完成。
数据：data/telegram.json（账号/会话/频道）、data/telegram_resources.json（锁定资源）
"""
import os
import json
import time
import threading
from datetime import datetime

from flask import Blueprint, request, jsonify

from .state import DATA_DIR
from .utils import login_required, atomic_write_json

bp = Blueprint("telegram", __name__)

TG_CONFIG_FILE = os.path.join(DATA_DIR, "telegram.json")
TG_RES_FILE = os.path.join(DATA_DIR, "telegram_resources.json")

_tg_service = None
_tg_service_lock = threading.Lock()


def _svc():
    """Telegram 服务单例（内部维护独立 asyncio 事件循环）"""
    global _tg_service
    with _tg_service_lock:
        if _tg_service is None:
            from .telegram_service import TelegramService
            _tg_service = TelegramService(TG_CONFIG_FILE)
        return _tg_service


def _payload():
    """兼容 JSON 与表单两种提交方式"""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict()


def _load_resources():
    try:
        with open(TG_RES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_resources(items):
    atomic_write_json(TG_RES_FILE, items)


# ────────────── 配置 / 登录 ──────────────

@bp.route("/api/admin/telegram/overview")
@login_required
def api_tg_overview():
    """页面初始化数据：账号配置（不含 session 明文）、频道列表、锁定资源"""
    cfg = _svc().load_config()
    return jsonify({
        "success": True,
        "api_id": str(cfg.get("api_id") or ""),
        "api_hash": cfg.get("api_hash") or "",
        "phone": cfg.get("phone") or "",
        "proxy": cfg.get("proxy") or "",
        "session_set": bool((cfg.get("session") or "").strip()),
        "client_session_set": bool((cfg.get("client_session") or "").strip()),
        "channels": [c for c in cfg.get("channels", []) if isinstance(c, dict)],
        "resources": _load_resources(),
    })


@bp.route("/api/admin/telegram/config", methods=["POST"])
@login_required
def api_tg_save_config():
    d = _payload()
    svc = _svc()
    old = svc.load_config()
    api_id = str(d.get("api_id") or "").strip()
    api_hash = (d.get("api_hash") or "").strip()
    data = {
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": (d.get("phone") or "").strip(),
        "proxy": (d.get("proxy") or "").strip(),
    }
    reset = False
    # api_id / api_hash 变更后旧会话必然失效，清空重新登录
    if api_id != str(old.get("api_id") or "") or api_hash != (old.get("api_hash") or ""):
        data["session"] = ""
        reset = True
    svc.save_config(**data)
    return jsonify({"success": True,
                    "message": "配置已保存" + ("（凭证变更，需要重新登录）" if reset else "")})


@bp.route("/api/admin/telegram/send_code", methods=["POST"])
@login_required
def api_tg_send_code():
    d = _payload()
    phone = (d.get("phone") or "").strip()
    svc = _svc()
    if phone:
        svc.save_config(phone=phone)
    try:
        res = svc.send_code(phone)
        hint = (res or {}).get("delivery") or "验证码已发送"
        return jsonify({"success": True, "message": hint})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


@bp.route("/api/admin/telegram/sign_in", methods=["POST"])
@login_required
def api_tg_sign_in():
    d = _payload()
    code = (d.get("code") or "").strip()
    if not code:
        return jsonify({"success": False, "message": "请输入验证码"})
    try:
        svc = _svc()
        me = svc.sign_in((d.get("phone") or "").strip(), code, (d.get("password") or "").strip())
        svc.invalidate_status()
        return jsonify({"success": True,
                        "message": "登录成功：" + (me.get("name") or str(me.get("id") or "")),
                        "user": me})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


@bp.route("/api/admin/telegram/status")
@login_required
def api_tg_status():
    """立即返回缓存的登录状态，同时触发后台刷新（不占用 Web 线程）"""
    svc = _svc()
    snap = svc.status_snapshot()
    svc.status_refresh()
    data = snap["data"] or {}
    cfg = svc.load_config()
    return jsonify({
        "success": True,
        "pending": snap["data"] is None,
        "authorized": bool(data.get("authorized")),
        "user": data.get("user") or {},
        "error": data.get("error") or "",
        "client_session_set": bool((cfg.get("client_session") or "").strip()),
    })


@bp.route("/api/admin/telegram/logout", methods=["POST"])
@login_required
def api_tg_logout():
    try:
        _svc().logout()
        return jsonify({"success": True, "message": "已退出登录（本地会话已清除）"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


# ────────────── 扫码登录（免验证码） ──────────────

@bp.route("/api/admin/telegram/qr_start", methods=["POST"])
@login_required
def api_tg_qr_start():
    try:
        d = _payload()
        target = (d.get("target") or request.args.get("target") or "main").strip()
        if target not in ("main", "client"):
            target = "main"
        svc = _svc()
        url = svc.qr_start(target=target)
        return jsonify({"success": True, "url": url, "svg": svc.qr_svg(url), "target": target})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


@bp.route("/api/admin/telegram/qr_status")
@login_required
def api_tg_qr_status():
    try:
        return jsonify({"success": True, **_svc().qr_status()})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


@bp.route("/api/admin/telegram/qr_password", methods=["POST"])
@login_required
def api_tg_qr_password():
    pwd = (_payload().get("password") or "").strip()
    try:
        _svc().qr_password(pwd)
        return jsonify({"success": True, "message": "登录成功"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


@bp.route("/api/admin/telegram/qr_cancel", methods=["POST"])
@login_required
def api_tg_qr_cancel():
    _svc().qr_cancel()
    return jsonify({"success": True, "message": "已取消"})


# ────────────── 频道 ──────────────

@bp.route("/api/admin/telegram/channels/add", methods=["POST"])
@login_required
def api_tg_channel_add():
    value = (_payload().get("channel") or "").strip()
    if not value:
        return jsonify({"success": False, "message": "请输入频道 @username 或频道ID"})
    try:
        info = _svc().add_channel(value)
        return jsonify({"success": True,
                        "message": "频道已添加：" + (info.get("title") or info.get("value") or ""),
                        "channel": info})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300]})


@bp.route("/api/admin/telegram/channels/remove", methods=["POST"])
@login_required
def api_tg_channel_remove():
    value = (_payload().get("value") or "").strip()
    svc = _svc()
    channels = [c for c in svc.load_config().get("channels", [])
                if isinstance(c, dict) and c.get("value") != value]
    svc.save_config(channels=channels)
    return jsonify({"success": True, "message": "频道已移除"})


# ────────────── 搜索 / 锁定 ──────────────

@bp.route("/api/admin/telegram/search", methods=["POST"])
@login_required
def api_tg_search():
    d = _payload()
    channels = d.get("channels") or []
    if isinstance(channels, str):
        channels = [channels] if channels else []
    try:
        data = _svc().search((d.get("query") or "").strip(), channels, d.get("limit") or 30)
        return jsonify({"success": True, "items": data["items"],
                        "errors": data["errors"], "total": data["total"]})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)[:300], "items": [], "errors": []})


@bp.route("/api/admin/telegram/lock", methods=["POST"])
@login_required
def api_tg_lock():
    d = _payload()
    chat_id = str(d.get("chat_id") or "").strip()
    msg_id = str(d.get("msg_id") or "").strip()
    if not chat_id or not msg_id:
        return jsonify({"success": False, "message": "缺少资源标识"})
    items = _load_resources()
    for it in items:
        if str(it.get("chat_id")) == chat_id and str(it.get("msg_id")) == msg_id:
            return jsonify({"success": True, "message": "该资源已在锁定列表"})
    item = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "name": (d.get("name") or "").strip(),
        "size_text": (d.get("size_text") or "").strip(),
        "mime": (d.get("mime") or "").strip(),
        "date": (d.get("date") or "").strip(),
        "link": (d.get("link") or "").strip(),
        "channel_title": (d.get("channel_title") or "").strip(),
        "channel_value": (d.get("channel_value") or "").strip(),
        "caption": (d.get("caption") or "").strip(),
        "locked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    items.append(item)
    _save_resources(items)
    # 锁定即下发下载任务给客户端：服务端不下载文件，客户端用自己的 Telegram 登录态
    # 下载后直接上传云盘；服务端仅负责派发与进度跟踪。
    task_id = _enqueue_tg_download(item)
    return jsonify({"success": True, "message": "已锁定，已推送给客户端下载",
                    "item": item, "task_id": task_id})


def _enqueue_tg_download(item):
    """把锁定的 Telegram 资源加入客户端任务队列，返回任务 id。"""
    try:
        from . import helpers as _h
        tid = "tg-" + os.urandom(6).hex()
        name = (item.get("name") or "").strip() or (
            "%s #%s" % (item.get("channel_title") or "TG资源", item.get("msg_id")))
        task = {
            "id": tid,
            "type": "tg-download",
            "name": name,
            "file_name": (item.get("name") or "").strip(),
            "chat_id": item.get("chat_id") or "",
            "msg_id": item.get("msg_id") or "",
            "channel_title": item.get("channel_title") or "",
            "channel_value": item.get("channel_value") or "",
            "size_text": item.get("size_text") or "",
            "mime": item.get("mime") or "",
            "link": item.get("link") or "",
            "wps_path": "",
            "status": "pending",
            "warn": "",
            "created_at": time.time(),
            "run_after": 0,
        }
        _h._CLIENT_TASKS[tid] = task
        _h._CLIENT_QUEUE.append(tid)
        _h._save_client_tasks()
        return tid
    except Exception:
        return ""


@bp.route("/api/admin/telegram/unlock", methods=["POST"])
@login_required
def api_tg_unlock():
    d = _payload()
    chat_id = str(d.get("chat_id") or "").strip()
    msg_id = str(d.get("msg_id") or "").strip()
    items = _load_resources()
    remain = [it for it in items
              if not (str(it.get("chat_id")) == chat_id and str(it.get("msg_id")) == msg_id)]
    _save_resources(remain)
    return jsonify({"success": True, "message": "已取消锁定"})
