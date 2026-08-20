# -*- coding: utf-8 -*-
"""server/routes_strm.py - STRM 任务与调度
"""
from flask import Blueprint, request, jsonify, session, redirect, url_for, Response, send_file, stream_with_context
import os
import time
import threading
import traceback
from datetime import datetime
from .state import *
from .utils import *
from .models import WPSClient, API_BASE
from .helpers import _task_status, _collect_media_files, get_wps_client, _resolve_folder_id

bp = Blueprint("strm", __name__)

@bp.route("/api/admin/strm_tasks", methods=["GET"])
@login_required
def api_admin_strm_list():
    tasks = _load_strm_tasks()
    # 确保每个任务都有 standard 字段
    for t in tasks:
        t.setdefault("enabled", True)
        t.setdefault("schedule", "")
        t.setdefault("category", "")
    return jsonify({"success": True, "tasks": tasks})

@bp.route("/api/admin/strm_tasks", methods=["POST"])
@login_required
def api_admin_strm_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "请填写任务名称"}), 400
    tasks = _load_strm_tasks()
    tasks.append({
        "name": name,
        "category": data.get("category", "").strip(),
        "source_id": data.get("source_id", "").strip(),
        "source_path": data.get("source_path", "").strip(),
        "output_dir": data.get("output_dir", "strm_output").strip(),
        "schedule": data.get("schedule", "").strip(),
        "enabled": True,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_run": None,
    })
    _save_strm_tasks(tasks)
    return jsonify({"success": True, "message": "任务已创建"})

@bp.route("/api/admin/strm_tasks/<int:idx>", methods=["PUT"])
@login_required
def api_admin_strm_update(idx):
    tasks = _load_strm_tasks()
    if idx < 0 or idx >= len(tasks):
        return jsonify({"success": False, "message": "任务不存在"}), 404
    data = request.get_json() or {}
    t = tasks[idx]
    if "name" in data and data["name"].strip():
        t["name"] = data["name"].strip()
    if "category" in data:
        t["category"] = data.get("category", "").strip()
    if "source_id" in data:
        t["source_id"] = data["source_id"].strip()
    if "source_path" in data:
        t["source_path"] = data.get("source_path", "").strip()
    if "output_dir" in data and data["output_dir"].strip():
        t["output_dir"] = data["output_dir"].strip()
    if "schedule" in data:
        t["schedule"] = data.get("schedule", "").strip()
    if "enabled" in data:
        t["enabled"] = bool(data["enabled"])
    _save_strm_tasks(tasks)
    return jsonify({"success": True, "message": "任务已更新"})

@bp.route("/api/admin/strm_tasks/<int:idx>", methods=["DELETE"])
@login_required
def api_admin_strm_delete(idx):
    tasks = _load_strm_tasks()
    if idx < 0 or idx >= len(tasks):
        return jsonify({"success": False, "message": "任务不存在"}), 404
    tasks.pop(idx)
    _save_strm_tasks(tasks)
    return jsonify({"success": True, "message": "任务已删除"})

@bp.route("/api/admin/strm_tasks/<int:idx>/toggle", methods=["POST"])
@login_required
def api_admin_strm_toggle(idx):
    tasks = _load_strm_tasks()
    if idx < 0 or idx >= len(tasks):
        return jsonify({"success": False, "message": "任务不存在"}), 404
    tasks[idx]["enabled"] = not tasks[idx].get("enabled", True)
    _save_strm_tasks(tasks)
    return jsonify({"success": True, "enabled": tasks[idx]["enabled"]})

@bp.route("/api/admin/strm_tasks/<int:idx>/run", methods=["POST"])
@login_required
def api_admin_strm_run(idx):
    try:
        tasks = _load_strm_tasks()
        if idx < 0 or idx >= len(tasks):
            return jsonify({"success": False, "message": "任务不存在"}), 404
        t = tasks[idx]
        if _task_status.get("running"):
            return jsonify({"success": False, "message": "已有任务在运行中"}), 400

        output_dir = t.get("output_dir") or os.path.join(DATA_DIR, "strm_output")
        cfg = load_config()
        extensions = cfg.get("strm_extensions",
            [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".iso", ".rmvb", ".webm"])
        source_id = t.get("source_id") or None
        # 仅过滤 Windows 文件名非法字符，其余（含 = + : , 等元数据符号）原样保留，
        # 避免把 [tmdbid=251600] 里的 '=' 等字符误删
        _ILLEGAL = set('\\/:*?"<>|\t\n\r')
        def _safe_name(s):
            s = (s or "").replace("（", "(").replace("）", ")")
            s = "".join(c for c in s if c not in _ILLEGAL)
            return s.strip().rstrip(".")  # 去掉首尾空格、末尾点（Windows 不允许）
        raw_name = (t.get("name") or "task")
        task_name = _safe_name(raw_name)

        # 构建代理基础 URL（优先使用配置中的 strm_base_url）
        strm_base = cfg.get("strm_base_url", "").strip()
        if not strm_base:
            strm_base = request.host_url.rstrip("/")
        url_prefix = "/r" if cfg.get("strm_redirect_mode") else "/stream"

        os.makedirs(output_dir, exist_ok=True)
        _task_status.update({"running": True, "progress": 0, "total": 0, "current": "扫描中...", "errors": []})
    except Exception as _e:
        traceback.print_exc()
        return jsonify({"success": False, "message": f"启动失败: {type(_e).__name__}: {_e}"}), 500

    def run():
        client, err = get_wps_client()
        if err:
            _task_status["errors"].append(str(err))
            _task_status["running"] = False
            return
        try:
            media_files = _collect_media_files(client, source_id, extensions)
            _task_status["total"] = len(media_files)
            for i, (f, rel_path) in enumerate(media_files):
                _task_status["progress"] = i
                fname = f.get("fname") or f.get("name") or "unnamed"
                fid = f.get("id")
                _task_status["current"] = fname
                try:
                    strm_url = f"{strm_base}{url_prefix}/{fid}"
                    safe = _safe_name(fname)
                    dl_dir = os.path.join(output_dir, task_name, rel_path)
                    os.makedirs(dl_dir, exist_ok=True)
                    sp = os.path.join(dl_dir, os.path.splitext(safe)[0] + ".strm")
                    if cfg.get("strm_incremental", True) and os.path.exists(sp):
                        continue  # 增量模式：跳过已存在的 STRM
                    with open(sp, "w", encoding="utf-8") as sf:
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
        # 更新 last_run
        tasks2 = _load_strm_tasks()
        if idx < len(tasks2):
            tasks2[idx]["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_strm_tasks(tasks2)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "count": "-", "message": "STRM 生成任务已启动"})
