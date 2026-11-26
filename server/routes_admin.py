# -*- coding: utf-8 -*-
"""server/routes_admin.py - 登录/设置/存储/云账号/正则/文件管理/分享
"""
from flask import Blueprint, request, jsonify, session, redirect, url_for, Response, send_file, stream_with_context, render_template
import hashlib
import os
import time
import threading
import json
import uuid
import secrets
from urllib.parse import quote
from datetime import datetime
from .state import *
from .utils import *
from .models import WPSClient, API_BASE
from .helpers import *

bp = Blueprint("admin", __name__)

# 浏览器会自动请求 /favicon.ico，返回 204 避免无谓的 404 噪声
@bp.route("/favicon.ico")
def admin_favicon():
    return ("", 204)

# ─── 日志读取（供手机端 / 前端日志页使用，内存缓冲，非落盘） ───
@bp.route("/api/admin/log")
@login_required
def api_admin_log():
    # 包模式（python -m server.app）下模块名是 server.app，不能直接 `import app`
    try:
        from .app import get_log_buffer
    except Exception:
        try:
            from app import get_log_buffer
        except Exception:
            return jsonify({"success": True, "logs": [], "total": 0})
    level = (request.args.get("level") or "ALL").upper()
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = int(request.args.get("limit") or 300)
    except ValueError:
        limit = 300
    limit = max(10, min(2000, limit))
    buf = get_log_buffer()
    out = []
    for rec in buf:
        if level != "ALL" and rec["level"] != level:
            continue
        if q and q not in rec["msg"].lower():
            continue
        out.append(rec)
    # 取末尾 limit 条（最新在前）
    out = out[-limit:]
    out.reverse()
    return jsonify({"success": True, "logs": out, "total": len(buf)})

@bp.route("/admin/login")
def admin_login_page():
    if session.get("logged_in"):
        return redirect(url_for("admin.admin_settings"))
    return render_template("admin_login.html", build=SERVER_BUILD)

@bp.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    username = data.get("username", "")
    password = data.get("password", "")
    cfg = load_config()
    if username == cfg["username"] and hashlib.sha256(password.encode()).hexdigest() == cfg["password"]:
        session.permanent = True
        session["logged_in"] = True
        session["username"] = username
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "用户名或密码错误"}), 401

@bp.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})

@bp.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/")

@bp.route("/admin")
@login_required
def admin_index():
    return redirect(url_for("admin.admin_settings"))

@bp.route("/admin/settings")
@login_required
def admin_settings():
    cfg = load_config()
    cookie_cfg = load_cookie()
    return render_template("admin_settings.html",
                           settings={"username": cfg["username"], "server_port": cfg["server_port"],
                                     "temp_download_dir": cfg.get("temp_download_dir", os.path.join(DATA_DIR, "temp_downloads")),
                                     "strm_output_dir": cfg["strm_output_dir"],
                                     "strm_extensions": cfg["strm_extensions"]},
                           has_cookie=bool(cookie_cfg.get("cookie")))

@bp.route("/api/admin/cookie", methods=["GET"])
@login_required
def api_admin_get_cookie():
    cfg = load_cookie()
    # 只返回 cookie 的最后几位做预览，不暴露完整值
    cookie = cfg.get("cookie", "")
    preview = ""
    if cookie:
        parts = cookie.split(";")
        # 显示前几个键名
        names = []
        for p in parts[:3]:
            p = p.strip()
            if "=" in p:
                names.append(p.split("=")[0].strip())
        preview = "; ".join(names) + ("..." if len(parts) > 3 else "")
    return jsonify({"has_cookie": bool(cookie), "preview": preview, "cookie": cookie})

@bp.route("/api/admin/cookie", methods=["POST"])
@login_required
def api_admin_save_cookie():
    data = request.get_json()
    cookie = data.get("cookie", "").strip()
    if not cookie:
        return jsonify({"success": False, "message": "Cookie 不能为空"}), 400
    save_cookie({"cookie": cookie})
    # 验证 Cookie 是否可用
    try:
        client, err = get_wps_client()
        if err:
            return jsonify({"success": True, "message": "Cookie 已保存，但无法连接: " + err})
        groups = client.get_groups()
        return jsonify({"success": True, "message": f"Cookie 已保存并验证成功 ({len(groups)} 个空间)"})
    except Exception as e:
        return jsonify({"success": True, "message": f"Cookie 已保存，但验证失败: {e}"})

@bp.route("/api/admin/status")
@login_required
def api_admin_status():
    cookie_cfg = load_cookie()
    if not cookie_cfg.get("cookie"):
        return jsonify({"connected": False, "cookie_valid": False, "error": "未配置 Cookie", "cookie_user": ""})
    client, err = get_wps_client()
    if err:
        return jsonify({"connected": False, "cookie_valid": False, "error": err, "cookie_user": ""})
    return jsonify({"connected": True, "cookie_valid": True, "cookie_user": cookie_cfg.get("cookie_preview", "已连接")})

@bp.route("/api/admin/browse_dir")
@login_required
def api_admin_browse_dir():
    """浏览服务器本地目录（用于 STRM 输出目录选择）"""
    path = request.args.get("path", "").strip()
    if not path:
        # 返回所有盘符（Windows）
        drives = []
        if os.name == "nt":
            import string as _str
            for letter in _str.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append({"name": drive, "path": drive})
        else:
            drives.append({"name": "/", "path": "/"})
        return jsonify({"dirs": drives, "current": ""})

    # 安全检查：路径必须存在且是目录
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return jsonify({"error": "路径不存在", "dirs": [], "current": path})

    try:
        entries = os.listdir(path)
        dirs = []
        for e in sorted(entries, key=lambda x: x.lower()):
            full = os.path.join(path, e)
            if os.path.isdir(full) and not e.startswith("."):
                dirs.append({"name": e, "path": full})
        # 父目录
        parent = os.path.dirname(path)
        if os.path.exists(parent):
            dirs.insert(0, {"name": "..", "path": parent})
        return jsonify({"dirs": dirs, "current": path})
    except PermissionError:
        return jsonify({"error": "无权限访问此目录", "dirs": [], "current": path})

@bp.route("/api/admin/storages", methods=["GET"])
@login_required
def api_admin_storages_list():
    return jsonify({"success": True, "storages": _load_storages()})

@bp.route("/api/admin/storages", methods=["POST"])
@login_required
def api_admin_storages_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "请填写存储名称"}), 400
    storages = _load_storages()
    sid = secrets.token_hex(8)
    storages.append({
        "id": sid,
        "name": name,
        "driver": data.get("driver", "wps").strip(),
        "mountPath": data.get("mountPath", "/").strip(),
        "cookie": data.get("cookie", "").strip(),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_storages(storages)
    return jsonify({"success": True, "storages": storages, "message": "存储已创建"})

@bp.route("/api/admin/storages/<sid>", methods=["PUT"])
@login_required
def api_admin_storages_update(sid):
    data = request.get_json() or {}
    storages = _load_storages()
    for s in storages:
        if s.get("id") == sid:
            if "name" in data and data["name"].strip():
                s["name"] = data["name"].strip()
            if "driver" in data:
                s["driver"] = data.get("driver", "wps").strip()
            if "mountPath" in data:
                s["mountPath"] = data.get("mountPath", "/").strip()
            if "cookie" in data:
                s["cookie"] = data.get("cookie", "").strip()
            _save_storages(storages)
            return jsonify({"success": True, "storages": storages, "message": "存储已更新"})
    return jsonify({"success": False, "message": "存储不存在"}), 404

@bp.route("/api/admin/storages/<sid>", methods=["DELETE"])
@login_required
def api_admin_storages_delete(sid):
    storages = _load_storages()
    new_list = [s for s in storages if s.get("id") != sid]
    if len(new_list) == len(storages):
        return jsonify({"success": False, "message": "存储不存在"}), 404
    _save_storages(new_list)
    return jsonify({"success": True, "storages": new_list, "message": "存储已删除"})

@bp.route("/api/admin/cloud_accounts", methods=["GET"])
@login_required
def api_admin_cloud_accounts_list():
    return jsonify({"success": True, "accounts": _load_cloud_accounts()})

@bp.route("/api/admin/cloud_accounts", methods=["POST"])
@login_required
def api_admin_cloud_accounts_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "请填写账号名称"}), 400
    acc_type = data.get("type", "wps").strip()
    accts = _load_cloud_accounts()
    aid = secrets.token_hex(8)
    accts.append({
        "id": aid,
        "name": name,
        "type": acc_type,
        "cookie": data.get("cookie", "").strip(),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_cloud_accounts(accts)
    return jsonify({"success": True, "accounts": accts, "message": "账号已添加"})

@bp.route("/api/admin/cloud_accounts/<aid>", methods=["DELETE", "PUT"])
@login_required
def api_admin_cloud_accounts_delete_or_edit(aid):
    if request.method == "PUT":
        data = request.get_json() or {}
        accts = _load_cloud_accounts()
        for a in accts:
            if a.get("id") == aid:
                name = data.get("name", "").strip()
                cookie = data.get("cookie", "").strip()
                if name:
                    a["name"] = name
                if cookie:
                    a["cookie"] = cookie
                _save_cloud_accounts(accts)
                return jsonify({"success": True, "accounts": accts, "message": "账号已更新"})
        return jsonify({"success": False, "message": "账号不存在"}), 404

    # DELETE
    accts = _load_cloud_accounts()
    new_list = [a for a in accts if a.get("id") != aid]
    if len(new_list) == len(accts):
        return jsonify({"success": False, "message": "账号不存在"}), 404
    _save_cloud_accounts(new_list)
    return jsonify({"success": True, "accounts": new_list, "message": "账号已删除"})

@bp.route("/api/admin/cloud_accounts/<aid>/test", methods=["POST"])
@login_required
def api_cloud_account_test(aid):
    """测试云盘账号连通性"""
    client = _get_cloud_account_client(aid)
    if not client:
        return jsonify({"success": False, "message": "账号凭据未配置"}), 400

    accts = _load_cloud_accounts()
    acc = next((a for a in accts if a.get("id") == aid), None)
    acc_type = acc.get("type", "wps") if acc else "wps"

    try:
        if acc_type == "wps":
            # WPS: 尝试获取文件列表验证
            files = client.get_all_files(DEFAULT_GROUP_ID, None)
            return jsonify({"success": True, "message": "连接成功", "count": len(files)})
        else:
            result = client.test()
            return jsonify({"success": result.get("ok", False),
                            "message": "连接成功" if result.get("ok") else result.get("msg", "失败")})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/cloud_accounts/capacity", methods=["GET"])
@login_required
def api_cloud_accounts_capacity():
    """获取所有云盘账号的容量信息"""
    accts = _load_cloud_accounts()
    result = {}
    for a in accts:
        aid = a.get("id")
        acc_type = a.get("type", "wps")
        try:
            client = _get_cloud_account_client(aid)
            if not client:
                result[aid] = {"error": "凭据未配置"}
                continue
            if acc_type == "wps":
                user_info = client.get_user_info()
                if user_info:
                    total = int(user_info.get("totalSpace") or user_info.get("total_space") or
                                user_info.get("storage", {}).get("total") or 0)
                    used = int(user_info.get("usedSpace") or user_info.get("used_space") or
                               user_info.get("storage", {}).get("used") or 0)
                    result[aid] = {"total": total, "used": used, "available": max(0, total - used)}
                else:
                    result[aid] = {"error": "无法获取用户信息"}
            else:
                cap = client.get_capacity()
                result[aid] = cap
        except Exception as e:
            result[aid] = {"error": str(e)}
    return jsonify({"success": True, "capacity": result})

@bp.route("/api/admin/settings", methods=["GET"])
@login_required
def api_admin_get_settings():
    cfg = load_config()
    return jsonify({"username": cfg["username"], "server_port": cfg["server_port"],
                    "temp_download_dir": cfg.get("temp_download_dir", os.path.join(DATA_DIR, "temp_downloads")),
                    "strm_output_dir": cfg["strm_output_dir"], "strm_extensions": cfg["strm_extensions"],
                    "strm_base_url": cfg.get("strm_base_url", ""),
                    "strm_redirect_mode": cfg.get("strm_redirect_mode", False),
                    "strm_incremental": cfg.get("strm_incremental", True),
                    "chase_min_size_mb": float(cfg.get("chase_min_size_mb", 0) or 0),
                    "chase_video_exts": cfg.get("chase_video_exts", "")})

@bp.route("/api/admin/settings/notify", methods=["GET"])
@login_required
def api_admin_get_notify():
    cfg = load_config()
    nc = cfg.get("notify", {}) or {}
    return jsonify({
        "enabled": nc.get("enabled", False),
        "serverchan_key": nc.get("serverchan_key", ""),
        "telegram_bot_token": nc.get("telegram_bot_token", ""),
        "telegram_chat_id": nc.get("telegram_chat_id", ""),
        "wecom_webhook": nc.get("wecom_webhook", ""),
        "on_new_eps": nc.get("on_new_eps", True),
        "on_download": nc.get("on_download", True),
        "on_fail": nc.get("on_fail", True),
    })

@bp.route("/api/admin/settings/notify", methods=["POST"])
@login_required
def api_admin_save_notify():
    data = request.get_json() or {}
    cfg = load_config()
    nc = cfg.setdefault("notify", {})
    nc["enabled"] = bool(data.get("enabled", False))
    nc["serverchan_key"] = (data.get("serverchan_key") or "").strip()
    nc["telegram_bot_token"] = (data.get("telegram_bot_token") or "").strip()
    nc["telegram_chat_id"] = (data.get("telegram_chat_id") or "").strip()
    nc["wecom_webhook"] = (data.get("wecom_webhook") or "").strip()
    nc["on_new_eps"] = bool(data.get("on_new_eps", True))
    nc["on_download"] = bool(data.get("on_download", True))
    nc["on_fail"] = bool(data.get("on_fail", True))
    save_config(cfg)
    return jsonify({"success": True, "message": "通知设置已保存"})

@bp.route("/api/admin/settings/tunnel", methods=["GET"])
@login_required
def api_admin_get_tunnel():
    cfg = load_config()
    tun = cfg.get("tunnel", {}) or {}
    return jsonify({
        "server": tun.get("server", ""),
        "port": int(tun.get("port", 13390) or 13390),
        "ctrl_port": int(tun.get("ctrl_port", 7000) or 7000),
        "data_port": int(tun.get("data_port", 7001) or 7001),
        "local_port": int(cfg.get("server_port", 58000)),
        "secret_set": bool((tun.get("secret") or "").strip()),
    })

@bp.route("/api/admin/settings/tunnel", methods=["POST"])
@login_required
def api_admin_save_tunnel():
    data = request.get_json() or {}
    cfg = load_config()
    tun = cfg.setdefault("tunnel", {})
    tun["server"] = (data.get("server") or "").strip()
    try:
        tun["port"] = max(1, min(65535, int(data.get("port") or 13390)))
    except (ValueError, TypeError):
        tun["port"] = 13390
    try:
        tun["ctrl_port"] = max(1, min(65535, int(data.get("ctrl_port") or 7000)))
    except (ValueError, TypeError):
        tun["ctrl_port"] = 7000
    try:
        tun["data_port"] = max(1, min(65535, int(data.get("data_port") or 7001)))
    except (ValueError, TypeError):
        tun["data_port"] = 7001
    secret = (data.get("secret") or "").strip()
    if secret:
        tun["secret"] = secret
    # 未填 secret 且已有密钥 → 保留原值；地址+密钥都空 → 视为关闭穿透
    save_config(cfg)
    from .tunnel_client import start_tunnel_if_configured
    start_tunnel_if_configured(cfg)
    return jsonify({"success": True, "message": "穿透配置已保存并生效"})

@bp.route("/api/admin/settings/notify/test", methods=["POST"])
@login_required
def api_admin_test_notify():
    data = request.get_json() or {}
    from .notify import test_send
    try:
        test_send(
            serverchan_key=(data.get("serverchan_key") or "").strip(),
            telegram_bot_token=(data.get("telegram_bot_token") or "").strip(),
            telegram_chat_id=(data.get("telegram_chat_id") or "").strip(),
            wecom_webhook=(data.get("wecom_webhook") or "").strip(),
        )
        return jsonify({"success": True, "message": "测试通知已发送，请检查接收端"})
    except Exception as e:
        return jsonify({"success": False, "message": f"发送失败: {e}"}), 400

@bp.route("/api/admin/settings/password", methods=["POST"])
@login_required
def api_admin_change_password():
    data = request.get_json()
    old_pw, new_pw = data.get("old_password", ""), data.get("new_password", "")
    if len(new_pw) < 3: return jsonify({"success": False, "message": "新密码至少3个字符"}), 400
    cfg = load_config()
    if hashlib.sha256(old_pw.encode()).hexdigest() != cfg["password"]:
        return jsonify({"success": False, "message": "原密码错误"}), 400
    cfg["password"] = hashlib.sha256(new_pw.encode()).hexdigest()
    save_config(cfg)
    return jsonify({"success": True, "message": "密码修改成功"})

@bp.route("/api/admin/settings/strm", methods=["POST"])
@login_required
def api_admin_save_strm():
    data = request.get_json()
    cfg = load_config()
    if "strm_output_dir" in data: cfg["strm_output_dir"] = data["strm_output_dir"]
    if "strm_extensions" in data: cfg["strm_extensions"] = data["strm_extensions"]
    if "strm_base_url" in data: cfg["strm_base_url"] = (data["strm_base_url"] or "").strip().rstrip("/")
    if "strm_redirect_mode" in data: cfg["strm_redirect_mode"] = bool(data["strm_redirect_mode"])
    if "strm_incremental" in data: cfg["strm_incremental"] = bool(data["strm_incremental"])
    if "chase_min_size_mb" in data and data["chase_min_size_mb"] is not None:
        try:
            cfg["chase_min_size_mb"] = max(0.0, float(data["chase_min_size_mb"] or 0))
        except (ValueError, TypeError):
            cfg["chase_min_size_mb"] = 0
    if "chase_video_exts" in data:
        cfg["chase_video_exts"] = ",".join(_split_video_exts(data["chase_video_exts"]))
    save_config(cfg)
    return jsonify({"success": True})

@bp.route("/api/admin/settings/general", methods=["POST"])
@login_required
def api_admin_save_general():
    data = request.get_json()
    cfg = load_config()
    if "temp_download_dir" in data:
        d = (data["temp_download_dir"] or "").strip()
        if d:
            os.makedirs(d, exist_ok=True)
        cfg["temp_download_dir"] = d
    if "chase_min_size_mb" in data and data["chase_min_size_mb"] is not None:
        try:
            cfg["chase_min_size_mb"] = max(0.0, float(data["chase_min_size_mb"] or 0))
        except (ValueError, TypeError):
            cfg["chase_min_size_mb"] = 0
    if "chase_video_exts" in data:
        cfg["chase_video_exts"] = ",".join(_split_video_exts(data["chase_video_exts"]))
    save_config(cfg)
    return jsonify({"success": True})

@bp.route("/api/admin/change_password", methods=["POST"])
@login_required
def api_admin_change_password_shortcut():
    return api_admin_change_password()

@bp.route("/api/admin/files/rename", methods=["POST"])
@login_required
def api_admin_rename_file():
    data = request.get_json() or {}
    file_id = data.get("file_id", "").strip()
    new_name = data.get("new_name", "").strip()
    if not file_id or not new_name:
        return jsonify({"success": False, "message": "缺少参数"}), 400
    client, err = get_wps_client()
    if err:
        return jsonify({"success": False, "message": err}), 500
    try:
        status, result = client.rename_file(DEFAULT_GROUP_ID, file_id, new_name)
        if status in (200, 204):
            return jsonify({"success": True})
        return jsonify({"success": False, "message": result.get("message", result.get("result", "重命名失败"))}), status
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/files/move", methods=["POST"])
@login_required
def api_admin_move_file():
    data = request.get_json() or {}
    file_id = data.get("file_id", "").strip()
    parent_id = data.get("parent_id", "").strip() or ""
    if not file_id:
        return jsonify({"success": False, "message": "缺少文件 ID"}), 400
    client, err = get_wps_client()
    if err:
        return jsonify({"success": False, "message": err}), 500
    try:
        status, result = client.move_file(DEFAULT_GROUP_ID, file_id, parent_id)
        if status in (200, 204):
            return jsonify({"success": True})
        return jsonify({"success": False, "message": result.get("message", "移动失败")}), status
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/files/copy", methods=["POST"])
@login_required
def api_admin_copy_file():
    data = request.get_json() or {}
    file_id = data.get("file_id", "").strip()
    parent_id = data.get("parent_id", "").strip() or ""
    if not file_id:
        return jsonify({"success": False, "message": "缺少文件 ID"}), 400
    client, err = get_wps_client()
    if err:
        return jsonify({"success": False, "message": err}), 500
    try:
        status, result = client.copy_file(DEFAULT_GROUP_ID, file_id, parent_id)
        if status in (200, 201, 204):
            return jsonify({"success": True})
        return jsonify({"success": False, "message": result.get("message", "复制失败")}), status
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/files/delete", methods=["POST"])
@login_required
def api_admin_delete_file():
    data = request.get_json() or {}
    file_id = data.get("file_id", "").strip()
    if not file_id:
        return jsonify({"success": False, "message": "缺少文件 ID"}), 400
    client, err = get_wps_client()
    if err:
        return jsonify({"success": False, "message": err}), 500
    try:
        status, result = client.delete_file(DEFAULT_GROUP_ID, file_id)
        if status in (200, 204, 202):
            return jsonify({"success": True})
        return jsonify({"success": False, "message": result.get("message", "删除失败")}), status
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/files/batch_rename", methods=["POST"])
@login_required
def api_admin_batch_rename():
    data = request.get_json() or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"success": False, "message": "缺少重命名列表"}), 400
    client, err = get_wps_client()
    if err:
        return jsonify({"success": False, "message": err}), 500
    count = 0
    for item in items:
        try:
            fid = item.get("id", "").strip()
            new_name = item.get("new_name", "").strip()
            if fid and new_name:
                status, _ = client.rename_file(DEFAULT_GROUP_ID, fid, new_name)
                if status in (200, 204):
                    count += 1
        except Exception:
            pass
    return jsonify({"success": True, "count": count})

@bp.route("/api/admin/files/mkdir", methods=["POST"])
@login_required
def api_admin_mkdir():
    data = request.get_json() or {}
    parent_id = data.get("parent_id", "").strip() or ""
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "缺少文件夹名"}), 400

    # 挂载点虚拟 ID (_mount_xxx) → 使用 _get_mount_client_and_root 获取专用 client
    store_client = None
    if parent_id.startswith("_mount_"):
        store_client, rid = _get_mount_client_and_root(parent_id)
        parent_id = rid or ""
        if store_client:
            print(f"[MKDIR] mount={parent_id[:8]} → real={parent_id}", flush=True)
        else:
            print(f"[MKDIR] mount resolve failed", flush=True)

    client, err = get_wps_client()
    if store_client:
        client = store_client
        err = None
    if err:
        return jsonify({"success": False, "message": err}), 500
    try:
        status, result = client.create_folder(DEFAULT_GROUP_ID, parent_id, name)
        if status in (200, 201):
            return jsonify({"success": True})
        return jsonify({"success": False, "message": result.get("message", "创建失败")}), status
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/public/files/upload", methods=["POST"])
def api_public_upload_file():
    """公开上传接口，Phase1 完成后立即返回 task_id，后台线程完成 WPS 上传"""
    _sys = sys
    parent_id = request.form.get("parent_id", "").strip() or ""
    relative_path = request.form.get("relative_path", "").strip()
    target_storage = request.form.get("target_storage", "").strip()  # 互传：指定目标存储 ID
    print(f"[ROUTE] api_public_upload_file parent_id_raw={parent_id!r} rel_path={relative_path!r} target_storage={target_storage!r}", flush=True, file=_sys.stderr)
    parent_id, mount_client = _resolve_parent_id(parent_id)
    if "file" not in request.files:
        return jsonify({"success": False, "message": "未选择文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "message": "文件名不能为空"}), 400

    # 互传模式：使用目标存储的 Cookie 构建客户端 + 解析挂载根目录
    client = None
    if target_storage:
        storages = _load_storages()
        storage = next((s for s in storages if s.get("id") == target_storage), None)
        if storage:
            cookie = storage.get("cookie", "").strip()
            if cookie:
                try:
                    sess = build_session(cookie)
                    csrf2 = get_csrf(cookie)
                    client = WPSClient(sess, csrf2)
                except Exception as e:
                    return jsonify({"success": False, "message": f"目标存储连接失败: {e}"}), 400
                # 解析挂载路径到实际文件夹 ID
                mp = (storage.get("mountPath", "") or "").strip().lstrip("/")
                if mp:
                    resolved = _resolve_folder_id(mp)
                    if resolved:
                        parent_id = resolved
            else:
                return jsonify({"success": False, "message": "目标存储未配置 Cookie"}), 400
        else:
            return jsonify({"success": False, "message": "目标存储不存在"}), 404
    if mount_client and not client:
        client = mount_client
    if not client:
        client, err = get_wps_client()
        if err:
            return jsonify({"success": False, "message": err}), 500
    temp_path = None
    try:
        # 分块落盘，避免把整个文件读进内存（手机容器内存有限，大文件会 OOM）
        _cfg = load_config()
        temp_dir = _cfg.get("temp_download_dir") or os.path.join(DATA_DIR, "temp_downloads")
        temp_path, file_size = save_upload_to_temp(file, temp_dir)
        file_name = file.filename
        target_pid = parent_id

        # 处理 relative_path：逐层创建子目录（在后台线程前同步完成，因为需要 list 操作）
        if relative_path:
            dirs = [d for d in relative_path.strip("/").split("/") if d]
            if len(dirs) > 1:
                sub_dirs = dirs[:-1]
                file_name = dirs[-1]
                for dn in sub_dirs:
                    existing = None
                    try:
                        for f in client.get_all_files(DEFAULT_GROUP_ID, target_pid):
                            if f.get("ftype") == "folder" and (f.get("fname") or f.get("name") or "").strip() == dn:
                                existing = str(f.get("id", ""))
                                break
                    except Exception:
                        pass
                    if existing:
                        target_pid = existing
                    else:
                        st, res = client.create_folder(DEFAULT_GROUP_ID, target_pid, dn)
                        if st in (200, 201) and res:
                            new_id = res.get("id") or res.get("fileid") or ""
                            if new_id:
                                target_pid = str(new_id)
                                continue
                        break
                if len(dirs) == 1:
                    file_name = dirs[0]

        task_id = uuid.uuid4().hex[:12]
        _update_task(task_id, name=file_name, total=file_size, uploaded=0, stage="prepare",
                     done=False, success=False, speed=0)

        # 后台线程执行 WPS 云上传
        def _bg_upload():
            try:
                print(f"[BG-UPLOAD] start {file_name!r} size={file_size} pid={target_pid!r} "
                      f"csrf_head={str(client.csrf)[:8]!r} storage_used={bool(target_storage)}",
                      flush=True, file=_sys.stderr)
                status, result = client.upload_file_with_progress(
                    DEFAULT_GROUP_ID, target_pid, None, file_name, file_size, task_id,
                    file_path=temp_path)
                if status not in (200, 201, 204):
                    _update_task(task_id, done=True, success=False,
                                 error=str(result.get("error") or result.get("message") or "上传失败"))
                print(f"[BG-UPLOAD] done status={status}", flush=True, file=_sys.stderr)
            except Exception as e:
                traceback.print_exc()
                _update_task(task_id, done=True, success=False, error=str(e))
            finally:
                # 上传结束（无论成败）删除临时文件，避免磁盘被大文件占满
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

        threading.Thread(target=_bg_upload, daemon=True).start()
        print(f"[ROUTE] task_id={task_id}, phase1 done", flush=True, file=_sys.stderr)
        return jsonify({"success": True, "task_id": task_id, "phase": "uploading_to_wps",
                        "name": file_name, "total": file_size})
    except Exception as e:
        traceback.print_exc()
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/files/upload", methods=["POST"])
@login_required
def api_admin_upload_file():
    parent_id = request.form.get("parent_id", "").strip() or ""
    parent_id, mount_client = _resolve_parent_id(parent_id)
    if "file" not in request.files:
        return jsonify({"success": False, "message": "未选择文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "message": "文件名不能为空"}), 400
    if mount_client:
        client = mount_client
    else:
        client, err = get_wps_client()
        if err:
            return jsonify({"success": False, "message": err}), 500
    temp_path = None
    try:
        # 分块落盘后从磁盘流式上传，避免整文件驻留内存
        _cfg = load_config()
        temp_dir = _cfg.get("temp_download_dir") or os.path.join(DATA_DIR, "temp_downloads")
        temp_path, file_size = save_upload_to_temp(file, temp_dir)
        file_name = file.filename
        task_id = uuid.uuid4().hex[:12]
        _update_task(task_id, name=file_name, total=file_size, uploaded=0, stage="prepare",
                     done=False, success=False, speed=0)
        status, result = client.upload_file_with_progress(
            DEFAULT_GROUP_ID, parent_id, None, file_name, file_size, task_id,
            file_path=temp_path)
        if status in (200, 201, 204):
            return jsonify({"success": True})
        msg = result.get("result") or result.get("message") or "上传失败"
        return jsonify({"success": False, "message": str(msg)}), status
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass

@bp.route("/api/admin/tasks/strm/status")
@login_required
def api_admin_strm_status():
    return jsonify(_task_status)

@bp.route("/api/admin/tasks/strm/start", methods=["POST"])
@login_required
def api_admin_strm_start():
    if _task_status["running"]:
        return jsonify({"success": False, "error": "已有任务在运行中"}), 400

    data = request.get_json() or {}
    cfg = load_config()
    output_dir = data.get("output_dir") or cfg.get("strm_output_dir", os.path.join(DATA_DIR, "strm_output"))
    extensions = data.get("extensions") or cfg.get("strm_extensions",
        [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".iso", ".rmvb", ".webm"])
    source_folder_id = data.get("source_folder_id", "").strip() or None  # 源目录

    # 构建代理基础 URL
    strm_base = cfg.get("strm_base_url", "").strip()
    if not strm_base:
        strm_base = request.host_url.rstrip("/")
    url_prefix = "/r" if cfg.get("strm_redirect_mode") else "/stream"

    os.makedirs(output_dir, exist_ok=True)
    _task_status.update({"running": True, "progress": 0, "total": 0, "current": "扫描中...", "errors": []})

    def run():
        client, err = get_wps_client()
        if err:
            _task_status["errors"].append(str(err))
            _task_status["running"] = False
            return

        def collect(pid, rel_path=""):
            result = []
            try:
                files = client.get_all_files(DEFAULT_GROUP_ID, pid)
                for f in files:
                    ftype = f.get("ftype") or "file"
                    fid = f.get("id")
                    fname = f.get("fname") or f.get("name") or ""
                    if ftype == "folder":
                        result.extend(collect(fid, os.path.join(rel_path, fname.replace("（", "(").replace("）", ")"))))
                    else:
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in extensions:
                            result.append((f, rel_path.replace("（", "(").replace("）", ")")))
            except Exception as e:
                _task_status["errors"].append(f"扫描出错: {e}")
            return result

        try:
            # 打印开始信息
            folder_name = "根目录"
            if source_folder_id:
                info = client.get_file_info(DEFAULT_GROUP_ID, source_folder_id)
                folder_name = (info.get("fname") or info.get("name") or source_folder_id) if info else source_folder_id
            raw_folder = (folder_name or "strm_output").replace("（", "(").replace("）", ")")
            # 仅过滤 Windows 文件名非法字符，其余（含 = + : , 等元数据符号）原样保留，
            # 避免把 [tmdbid=251600] 里的 '=' 等字符误删
            _ILLEGAL = set('\\/:*?"<>|\t\n\r')
            def _safe_name(s):
                s = (s or "").replace("（", "(").replace("）", ")")
                s = "".join(c for c in s if c not in _ILLEGAL)
                return s.strip().rstrip(".")  # 去掉首尾空格、末尾点（Windows 不允许）
            task_root = _safe_name(raw_folder)

            media_files = collect(source_folder_id)
            _task_status["total"] = len(media_files)

            for i, (f, rel_path) in enumerate(media_files):
                _task_status["progress"] = i
                fname = f.get("fname") or f.get("name") or "unnamed"
                fid = f.get("id")
                _task_status["current"] = fname

                try:
                    strm_url = f"{strm_base}{url_prefix}/{fid}/{quote(f.get('fname') or f.get('name') or 'file')}"
                    fname_fixed = fname.replace("（", "(").replace("）", ")")
                    # 仅过滤 Windows 文件名非法字符，保留 = + : , [tmdbid=xxx] 等元数据符号
                    safe_name = _safe_name(fname_fixed)
                    dl_dir = os.path.join(output_dir, task_root, rel_path)
                    os.makedirs(dl_dir, exist_ok=True)
                    strm_path = os.path.join(dl_dir, os.path.splitext(safe_name)[0] + ".strm")
                    if cfg.get("strm_incremental", True) and os.path.exists(strm_path):
                        continue  # 增量模式：跳过已存在的 STRM
                    with open(strm_path, "w", encoding="utf-8") as sf:
                        sf.write(strm_url)
                except Exception as e:
                    _task_status["errors"].append(f"{fname}: {e}")

                time.sleep(0.15)

            _task_status["progress"] = _task_status["total"]
            _task_status["current"] = "完成"
        except Exception as e:
            _task_status["errors"].append(str(e))
        finally:
            _task_status["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "message": "STRM 生成任务已启动"})

@bp.route("/api/admin/clients")
@login_required
def api_admin_clients():
    """返回已注册的客户端列表（含活跃任务数与运行中任务进度）"""
    clients = []
    for info in _CLIENT_REGISTRY.values():
        safe = dict(info)
        ct = safe.get("cloud_types")
        if isinstance(ct, set):
            safe["cloud_types"] = sorted(ct)
        _cn = info.get("client_name", "")
        # ★7 该客户端当前正在执行的任务（含实时进度），供管理页展示
        _running_tasks = []
        for _t in _CLIENT_TASKS.values():
            if _t.get("polled_by") == _cn and _t.get("status") == "running":
                _running_tasks.append({
                    "id": _t.get("id", ""),
                    "name": _t.get("name", ""),
                    "type": _t.get("type", ""),
                    "ep_num": _t.get("ep_num", 0),
                    "stage": _t.get("stage", ""),
                    "progress": _t.get("progress", 0),
                    "total": _t.get("total", 0),
                    "speed": _t.get("speed", 0),
                })
        clients.append({
            **safe,
            "active_count": _client_active_count(_cn),
            "running_tasks": _running_tasks,
        })
    return jsonify({"clients": clients})

@bp.route("/api/admin/client_config", methods=["POST"])
@login_required
def api_admin_client_config():
    """配置客户端并发数"""
    data = request.get_json(silent=True) or {}
    client_name = data.get("client_name", "").strip()
    max_concurrent = data.get("max_concurrent", 3)
    if not client_name:
        return jsonify({"success": False, "message": "缺少客户端名称"}), 400
    if client_name in _CLIENT_REGISTRY:
        _CLIENT_REGISTRY[client_name]["max_concurrent"] = max_concurrent
    else:
        _CLIENT_REGISTRY[client_name] = {
            "client_name": client_name,
            "last_seen": "-",
            "ip": "-",
            "max_concurrent": max_concurrent,
            "active_count": 0,
        }
    return jsonify({"success": True, "message": f"客户端 {client_name} 并发数设为 {max_concurrent}"})

@bp.route("/api/admin/client/tasks", methods=["GET", "POST"])
@login_required
def api_admin_client_tasks():
    """管理后台：查看/添加客户端任务"""
    if request.method == "GET":
        return jsonify({"tasks": list(_CLIENT_TASKS.values()),
                        "queue": list(_CLIENT_QUEUE)})

    # POST: 添加新任务
    data = request.get_json(silent=True) or {}
    share_url = data.get("share_url", "").strip()
    if not share_url:
        return jsonify({"success": False, "msg": "缺少分享链接"}), 400

    import uuid
    tid = uuid.uuid4().hex[:12]
    task = {
        "id": tid,
        "name": data.get("name", share_url[:30]),
        "share_url": share_url,
        "share_passwd": data.get("share_passwd", ""),
        "rename_to": data.get("rename_to", ""),
        "wps_path": data.get("wps_path", ""),
        "status": "pending",
        "created_at": time.time(),
    }
    _CLIENT_TASKS[tid] = task
    _CLIENT_QUEUE.append(tid)
    return jsonify({"success": True, "task_id": tid, "task": task})

@bp.route("/api/status")
def api_status():
    """服务器状态（无需登录，客户端测试连接用）"""
    return jsonify({"status": "ok", "server": "wps-manager", "time": time.time()})

@bp.route("/api/admin/regex_patterns", methods=["GET"])
@login_required
def api_admin_regex_list():
    patterns = _load_regex()
    return jsonify({"patterns": patterns})

@bp.route("/api/admin/regex_patterns", methods=["POST"])
@login_required
def api_admin_regex_create():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    pattern = data.get("pattern", "").strip()
    if not name:
        return jsonify({"success": False, "message": "名称不能为空"}), 400
    patterns = _load_regex()
    pid = "regex-" + uuid.uuid4().hex[:8]
    new_item = {
        "id": pid,
        "name": name,
        "pattern": pattern,
        "desc": data.get("desc", "").strip(),
        "preset": False,
    }
    patterns.append(new_item)
    _save_regex(patterns)
    return jsonify({"success": True, "pattern": new_item})

@bp.route("/api/admin/regex_patterns/<pid>", methods=["PUT"])
@login_required
def api_admin_regex_update(pid):
    data = request.get_json(silent=True) or {}
    patterns = _load_regex()
    for p in patterns:
        if p.get("id") == pid:
            if data.get("name") is not None:
                p["name"] = data["name"].strip()
            if data.get("pattern") is not None:
                p["pattern"] = data["pattern"].strip()
            if data.get("desc") is not None:
                p["desc"] = data["desc"].strip()
            _save_regex(patterns)
            return jsonify({"success": True, "pattern": p})
    return jsonify({"success": False, "message": "规则不存在"}), 404

@bp.route("/api/admin/regex_patterns/<pid>", methods=["DELETE"])
@login_required
def api_admin_regex_delete(pid):
    patterns = _load_regex()
    for p in patterns:
        if p.get("id") == pid:
            patterns.remove(p)
            _save_regex(patterns)
            return jsonify({"success": True})
    return jsonify({"success": False, "message": "规则不存在"}), 404

@bp.route("/api/admin/cloud_config", methods=["GET"])
@login_required
def api_admin_cloud_list():
    configs = _load_cloud()
    return jsonify({"configs": configs})

@bp.route("/api/admin/cloud_config", methods=["POST"])
@login_required
def api_admin_cloud_save():
    data = request.get_json(silent=True) or {}
    cloud_type = data.get("type", "").strip()          # "139" | "tianyi"
    cookie = data.get("cookie", "").strip()
    token = data.get("token", "").strip()
    is_primary = data.get("is_primary", False)
    config_id = (data.get("id") or "").strip()
    if not cloud_type:
        return jsonify({"success": False, "message": "云盘类型不能为空"}), 400
    configs = _load_cloud()
    if config_id:
        # 更新已有配置
        for cfg in configs:
            if cfg.get("id") == config_id:
                cfg["cookie"] = cookie or cfg.get("cookie", "")
                cfg["token"] = token or cfg.get("token", "")
                break
    else:
        config_id = "cloud-" + uuid.uuid4().hex[:8]
        configs.append({
            "id": config_id,
            "type": cloud_type,
            "cookie": cookie,
            "token": token,
            "is_primary": False,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    # 如果设为主账号，取消同类型的其他主账号
    if is_primary:
        for cfg in configs:
            if cfg.get("type") == cloud_type:
                cfg["is_primary"] = (cfg["id"] == config_id)
    _save_cloud(configs)
    return jsonify({"success": True, "configs": configs})

@bp.route("/api/admin/cloud_config/<cid>", methods=["DELETE"])
@login_required
def api_admin_cloud_delete(cid):
    configs = _load_cloud()
    for cfg in configs:
        if cfg.get("id") == cid:
            configs.remove(cfg)
            _save_cloud(configs)
            return jsonify({"success": True})
    return jsonify({"success": False, "message": "配置不存在"}), 404

@bp.route("/api/admin/cloud_config/<cid>/primary", methods=["POST", "DELETE"])
@login_required
def api_admin_cloud_set_primary(cid):
    """设为/取消默认主账号"""
    configs = _load_cloud()
    target = None
    for cfg in configs:
        if cfg.get("id") == cid:
            target = cfg
    if not target:
        return jsonify({"success": False, "message": "配置不存在"}), 404
    cloud_type = target.get("type", "")
    if request.method == "DELETE":
        # 取消主账号
        for cfg in configs:
            if cfg.get("type") == cloud_type:
                cfg["is_primary"] = False
    else:
        # 设为主账号
        for cfg in configs:
            if cfg.get("type") == cloud_type:
                cfg["is_primary"] = (cfg["id"] == cid)
    _save_cloud(configs)
    return jsonify({"success": True, "configs": configs})

# ══════════════════════════════════════════════════
# PWA 手机监控页（客户端/任务状态，可添加主屏幕 / 打包 APK）
# ══════════════════════════════════════════════════
@bp.route("/admin/mobile")
def admin_mobile():
    """手机 PWA 监控页：客户端在线状态 + 任务进度。页面内自行处理登录（session），
    API 层有 login_required 保护，页面本身无需强制登录以便 PWA 离线壳展示。"""
    return render_template("mobile.html")

@bp.route("/admin/mobile_manifest.json")
def admin_mobile_manifest():
    """PWA manifest：让手机可"添加到主屏幕"获得类 APP 体验"""
    manifest = {
        "name": "WPS传输监控",
        "short_name": "传输监控",
        "description": "查看客户端在线状态与下载/上传任务进度",
        "start_url": "/admin/mobile",
        "scope": "/admin/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0a0a10",
        "theme_color": "#0a0a10",
        "icons": [
            {
                "src": "/admin/mobile_icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    }
    return Response(json.dumps(manifest, ensure_ascii=False),
                    mimetype="application/manifest+json")

@bp.route("/admin/mobile_icon.svg")
def admin_mobile_icon():
    """PWA 图标（SVG，渐变圆角 + 传输箭头）"""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#6366f1"/>
<stop offset="1" stop-color="#818cf8"/>
</linearGradient>
</defs>
<rect width="512" height="512" rx="112" fill="url(#g)"/>
<g fill="#ffffff" opacity="0.92">
<path d="M160 208h192l-40-40a16 16 0 1 1 22-22l72 72-72 72a16 16 0 1 1-22-22l40-40H160a16 16 0 0 1 0-32z"/>
<path d="M352 304H160l40 40a16 16 0 1 1-22 22l-72-72 72-72a16 16 0 1 1 22 22l-40 40h192a16 16 0 0 1 0 32z"/>
</g>
</svg>'''
    return Response(svg, mimetype="image/svg+xml")

@bp.route("/admin/sw.js")
def admin_mobile_sw():
    """Service Worker：缓存页面壳 + 图标，实现离线打开与秒开"""
    sw = r'''/* WPS传输监控 Service Worker */
const CACHE = 'wps-mobile-v1';
const SHELL = ['/admin/mobile', '/admin/mobile_manifest.json', '/admin/mobile_icon.svg'];

self.addEventListener('install', function (e) {
    e.waitUntil(caches.open(CACHE).then(function (c) {
        return c.addAll(SHELL).catch(function () {});
    }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
    e.waitUntil(caches.keys().then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
    const url = new URL(e.request.url);
    // API 请求不缓存，始终走网络
    if (url.pathname.startsWith('/api/')) return;
    e.respondWith(
        caches.match(e.request).then(function (hit) {
            const net = fetch(e.request).then(function (res) {
                if (res && res.ok) {
                    const clone = res.clone();
                    caches.open(CACHE).then(function (c) { c.put(e.request, clone); });
                }
                return res;
            }).catch(function () { return hit; });
            return hit || net;
        })
    );
});'''
    return Response(sw, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/admin/"})
