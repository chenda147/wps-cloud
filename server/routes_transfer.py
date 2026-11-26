# -*- coding: utf-8 -*-
"""server/routes_transfer.py - 转存/上传/WPS三步上传/relay
"""
from flask import Blueprint, request, jsonify, session, redirect, url_for, Response, send_file, stream_with_context
import os
import json
import time
import threading
import uuid
from .state import *
from .utils import *
from .models import WPSClient, API_BASE
from .helpers import *

bp = Blueprint("transfer", __name__)

@bp.route("/api/admin/transfer/source_files", methods=["GET"])
@login_required
def api_transfer_source_files():
    """浏览源账号文件列表（WPS / 天翼 / 移动）或分享链接解析"""
    account_id = request.args.get("account_id", "").strip()
    parent_id = request.args.get("parent_id", "").strip()
    share_url = request.args.get("share_url", "").strip()
    share_passwd = request.args.get("share_passwd", "").strip()
    if not account_id:
        return jsonify({"success": False, "message": "请选择账号"}), 400

    accts = _load_cloud_accounts()
    acc = next((a for a in accts if a.get("id") == account_id), None)
    if not acc:
        return jsonify({"success": False, "message": "账号不存在"}), 404

    client = _get_cloud_account_client(account_id)
    if not client:
        return jsonify({"success": False, "message": "账号凭据未配置"}), 400

    acc_type = acc.get("type", "wps")

    try:
        # ── 分享链接模式（yidong / quark 支持） ──
        if share_url and acc_type in ("yidong", "quark"):
            recursive = request.args.get("recursive", "0") == "1"
            if recursive:
                result = client.list_share_files_recursive(share_url, share_passwd, parent_id or "root")
            else:
                result = client.list_share_files(share_url, share_passwd, parent_id or "root")
            if not result.get("ok"):
                return jsonify({"success": False, "message": result.get("msg", "分享链接解析失败")}), 400
            entries = []
            for f in result.get("items", []):
                entries.append({
                    "id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "size": f.get("size", 0),
                    "is_dir": f.get("is_dir", False),
                    "mtime": f.get("mtime", ""),
                    "path": f.get("path", ""),  # 转存用
                })
            return jsonify({"success": True, "files": entries,
                            "total": result.get("count", len(entries)),
                            "is_share": True})

        if acc_type == "wps":
            # 复用原有 WPS 逻辑
            files = client.get_all_files(DEFAULT_GROUP_ID, parent_id or None)
            entries = []
            for f in files:
                fid = str(f.get("id") or "")
                fname = f.get("fname") or f.get("name") or ""
                is_dir = (f.get("ftype") == "folder" or f.get("ftype") == "dir")
                entries.append({
                    "id": fid, "name": fname, "size": int(f.get("fsize") or f.get("size") or 0),
                    "is_dir": is_dir, "mtime": f.get("mtime"), "ftype": f.get("ftype"),
                })
            return jsonify({"success": True, "files": entries})

        else:
            # 天翼 / 移动 使用统一接口
            folder_id = parent_id or "root"
            page = int(request.args.get("page", 1))
            result = client.list_files(folder_id, page=page)
            if not result.get("ok"):
                return jsonify({"success": False, "message": result.get("msg", "获取失败")}), 400
            entries = []
            for f in result.get("items", []):
                entries.append({
                    "id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "size": f.get("size", 0),
                    "is_dir": f.get("is_dir", False),
                    "mtime": f.get("mtime", ""),
                    "path": f.get("path", ""),  # 转存用
                })
            return jsonify({"success": True, "files": entries,
                            "total": result.get("count", len(entries))})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/transfer/download_url", methods=["GET"])
@login_required
def api_transfer_download_url():
    """获取源文件下载链接（WPS / 天翼 / 移动 / 分享链接）"""
    account_id = request.args.get("account_id", "").strip()
    file_id = request.args.get("file_id", "").strip()
    share_url = request.args.get("share_url", "").strip()
    share_passwd = request.args.get("share_passwd", "").strip()
    file_name = request.args.get("file_name", "").strip()
    share_path = request.args.get("share_path", "").strip()
    if not account_id or not file_id:
        return jsonify({"success": False, "message": "缺少参数"}), 400

    accts = _load_cloud_accounts()
    acc = next((a for a in accts if a.get("id") == account_id), None)
    if not acc:
        return jsonify({"success": False, "message": "账号不存在"}), 404

    client = _get_cloud_account_client(account_id)
    if not client:
        return jsonify({"success": False, "message": "账号凭据未配置"}), 400

    acc_type = acc.get("type", "wps")

    try:
        # ── 分享链接模式（yidong / quark 支持） ──
        if share_url and acc_type in ("yidong", "quark"):
            result = client.get_share_download_url(share_url, file_id,
                                                    file_name=file_name,
                                                    share_path=share_path,
                                                    share_passwd=share_passwd)
            if result.get("ok"):
                return jsonify({"success": True, "url": result["url"]})
            return jsonify({"success": False, "message": result.get("msg", "获取分享下载链接失败")}), 400

        if acc_type == "wps":
            cdn_url = client.get_download_url(DEFAULT_GROUP_ID, file_id)
            return jsonify({"success": True, "url": cdn_url})
        else:
            result = client.get_download_url(file_id)
            if result.get("ok"):
                return jsonify({"success": True, "url": result["url"]})
            return jsonify({"success": False, "message": result.get("msg", "获取失败")}), 400

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/api/admin/transfer/wps_upload_plan", methods=["POST"])
@login_required
def api_wps_upload_plan():
    """WPS 三步上传：Step1 — 创建上传计划，返回 KS3 URL 给浏览器直传"""
    data = request.get_json() or {}
    target_storage = data.get("target_storage", "").strip()
    file_name = data.get("file_name", "").strip()
    file_size = data.get("file_size", 0)
    parent_id = data.get("parent_id", "").strip() or ""
    # 浏览器计算的哈希（可选，没有就传空字符串）
    md5_hex = data.get("md5", "").strip()
    sha1_hex = data.get("sha1", "").strip()

    if not target_storage or not file_name or not file_size:
        return jsonify({"success": False, "message": "缺少参数"}), 400

    storages = _load_storages()
    storage = next((s for s in storages if s.get("id") == target_storage), None)
    if not storage:
        return jsonify({"success": False, "message": "目标存储不存在"}), 404
    cookie = (storage.get("cookie", "") or "").strip()
    if not cookie:
        return jsonify({"success": False, "message": "目标存储未配置 Cookie"}), 400

    try:
        wps_sess = build_session(cookie)
        wps_csrf = get_csrf(cookie)
        wps_client = WPSClient(wps_sess, wps_csrf)
    except Exception as e:
        return jsonify({"success": False, "message": f"WPS 连接失败: {e}"}), 400

    mp = (storage.get("mountPath", "") or "").strip().lstrip("/")
    target_pid = parent_id if parent_id else ""
    if mp and not target_pid:
        resolved = _resolve_folder_id(mp)
        if resolved:
            target_pid = resolved
    if not target_pid:
        target_pid = "0"

    # 用假哈希创建计划（没有真哈希也能创建上传任务，提交时会填充正确的）
    md5_sum = md5_hex or "d41d8cd98f00b204e9800998ecf8427e"  # 空文件MD5
    sha1_sum = sha1_hex or "da39a3ee5e6b4b0d3255bfef95601890afd80709"  # 空文件SHA1

    DRIVE_V5 = "https://drive.kdocs.cn"
    try:
        pid = int(target_pid) if target_pid and str(target_pid).strip() else 0
        gid = int(DEFAULT_GROUP_ID)
    except ValueError:
        pid = target_pid
        gid = DEFAULT_GROUP_ID

    body_1 = {
        "groupid": gid, "parentid": pid, "parent_path": [],
        "size": file_size, "name": file_name, "req_by_internal": False,
        "client_stores": "ks3", "contenttype": "application/octet-stream",
        "startswithfilename": file_name, "successactionstatus": 201,
        "group_id": gid, "parent_id": pid, "file_id": 0,
        "with_rapid": True, "tried_store": [],
        "md5": md5_sum, "csrfmiddlewaretoken": wps_client.csrf,
    }
    v5_h = {"content-type": "application/json", "origin": "https://www.kdocs.cn", "accept": "*/*"}
    import sys as _sys2
    try:
        r1 = wps_client.s.put(f"{DRIVE_V5}/api/v5/files/upload/create_update",
                              data=json.dumps(body_1, ensure_ascii=False).encode("utf-8"),
                              headers=v5_h, timeout=30)
        if r1.status_code != 200:
            try:
                _detail = r1.json()
            except Exception:
                _detail = r1.text[:500]
            print(f"[UPLOAD_PLAN] step1 HTTP {r1.status_code}: {_detail}", flush=True, file=_sys2.stderr)
            return jsonify({"success": False, "message": f"创建上传计划失败({r1.status_code}): {_detail}"}), 500
        r1.raise_for_status()
        plan = r1.json()
    except Exception as e:
        return jsonify({"success": False, "message": f"创建上传计划失败: {e}"}), 500

    # 清理超过 2 小时仍未提交的上传计划，避免内存泄漏
    _now = time.time()
    for _pid in [k for k, v in list(_UPLOAD_PLANS.items()) if _now - (v.get("_ts") or 0) > 7200]:
        _UPLOAD_PLANS.pop(_pid, None)

    plan_id = uuid.uuid4().hex[:12]
    _UPLOAD_PLANS[plan_id] = {
        "_ts": _now,
        "plan": plan,
        "wps_sess_cookie": cookie,
        "target_pid": target_pid,
        "file_name": file_name,
        "file_size": file_size,
        "md5_hex": md5_hex,
        "sha1_hex": sha1_hex,
    }

    ks3_url = plan.get("url", "")
    ks3_headers = plan.get("request", {}).get("headers", {})
    spec = plan.get("response", {})
    args_key = spec.get("args_key", "")
    args_etag = spec.get("args_etag", "")

    return jsonify({
        "success": True,
        "plan_id": plan_id,
        "ks3_url": ks3_url,
        "ks3_headers": ks3_headers,
        "args_key": args_key,
        "args_etag": args_etag,
    })

@bp.route("/api/admin/transfer/wps_upload_commit", methods=["POST"])
@login_required
def api_wps_upload_commit():
    """WPS 三步上传：Step3 — 浏览器上传 KS3 完成后，提交文件元数据"""
    data = request.get_json() or {}
    plan_id = data.get("plan_id", "").strip()
    key = data.get("key", "").strip()
    etag = data.get("etag", "").strip()

    plan_info = _UPLOAD_PLANS.pop(plan_id, None)
    if not plan_info:
        return jsonify({"success": False, "message": "上传计划不存在或已过期"}), 404

    plan = plan_info["plan"]
    file_name = plan_info["file_name"]
    file_size = plan_info["file_size"]
    target_pid = plan_info["target_pid"]
    store = plan.get("store", "ks3")
    
    # 重新构建 WPS 客户端
    cookie = plan_info["wps_sess_cookie"]
    try:
        wps_sess = build_session(cookie)
        wps_csrf = get_csrf(cookie)
        wps_client = WPSClient(wps_sess, wps_csrf)
    except Exception as e:
        return jsonify({"success": False, "message": f"WPS 重连失败: {e}"}), 500

    try:
        pid = int(target_pid) if target_pid and str(target_pid).strip() else 0
        gid = int(DEFAULT_GROUP_ID)
    except ValueError:
        pid = target_pid
        gid = DEFAULT_GROUP_ID

    DRIVE_V5 = "https://drive.kdocs.cn"
    md5_hex = plan_info.get("md5_hex", "") or "d41d8cd98f00b204e9800998ecf8427e"
    sha1_hex = plan_info.get("sha1_hex", "") or "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    if not key:
        # 尝试从 KS3 etag 推断 key（通常是 SHA1 或路径的一部分）
        key = plan.get("response", {}).get("args_key", "")
        if not key:
            key = sha1_hex

    final_etag = etag or f'"{md5_hex}"'
    body_3 = {
        "key": key, "groupid": gid, "parentid": pid, "parent_path": [],
        "name": file_name, "isUpNewVer": False,
        "etag": final_etag, "store": store,
        "size": file_size, "sha1": sha1_hex,
        "apiErrorInfo": None, "csrfmiddlewaretoken": wps_client.csrf,
    }
    v5_h = {"content-type": "application/json", "origin": "https://www.kdocs.cn", "accept": "*/*"}
    try:
        r3 = wps_client.s.post(f"{DRIVE_V5}/api/v5/files/file",
                               data=json.dumps(body_3, ensure_ascii=False).encode("utf-8"),
                               headers=v5_h, timeout=30)
        r3.raise_for_status()
        res = r3.json()
        if res.get("result") != "ok":
            return jsonify({"success": False, "message": f"提交失败: {res.get('msg', res)}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"提交元数据失败: {e}"}), 500

    return jsonify({"success": True, "message": f"上传成功: {file_name}"})

@bp.route("/api/admin/transfer/stream_fetch", methods=["GET"])
@login_required
def api_stream_fetch():
    """浏览器通过同源Flask路由拉取CDN数据，流式返回。同源无CORS限制。"""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "message": "缺少 url"}), 400
    headers_raw = request.args.get("headers", "")
    extra_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://yun.139.com/",
    }
    if headers_raw:
        try:
            extra_headers.update(json.loads(headers_raw))
        except:
            pass
    try:
        r = requests.get(url, stream=True, timeout=(15, 600),
                         allow_redirects=True, headers=extra_headers)
        r.raise_for_status()
    except Exception as e:
        return jsonify({"success": False, "message": f"CDN连接失败: {e}"}), 502

    def generate():
        _sys = sys
        try:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        except GeneratorExit:
            pass
        except Exception as e:
            print(f"[stream_fetch] 流中断: {e}", flush=True, file=_sys.stderr)
        finally:
            r.close()

    resp = Response(stream_with_context(generate()),
                    content_type=r.headers.get("Content-Type", "application/octet-stream"))
    resp.headers["Cache-Control"] = "no-store"
    # 强制浏览器下载（而不是在线预览/导航）
    fname = request.args.get("name", "").strip()
    if not fname:
        # 从 URL 里尝试提取
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(url).query)
            cd = qs.get("response-content-disposition", [""])[0]
            if "filename" in cd:
                fname = unquote(cd.split("filename''")[-1].split("&")[0])
        except Exception:
            pass
    if fname:
        try:
            from urllib.parse import quote
            resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(fname)
        except Exception:
            resp.headers["Content-Disposition"] = 'attachment; filename="download"'
    else:
        resp.headers["Content-Disposition"] = 'attachment; filename="download"'
    cl = r.headers.get("Content-Length", "")
    if cl:
        resp.headers["Content-Length"] = cl
    return resp

@bp.route("/api/admin/transfer/relay", methods=["POST"])
@login_required
def api_transfer_relay():
    """服务器端从源 CDN 下载到临时文件，再上传到目标 WPS 存储"""
    _sys = sys
    data = request.get_json() or {}
    account_id = data.get("account_id", "").strip()
    file_id = data.get("file_id", "").strip()
    share_url = data.get("share_url", "").strip()
    share_passwd = data.get("share_passwd", "").strip()
    file_name = data.get("file_name", "").strip()
    share_path = data.get("share_path", "").strip()
    target_storage = data.get("target_storage", "").strip()
    parent_id_input = data.get("parent_id", "").strip() or ""

    if not account_id or not file_id or not target_storage:
        return jsonify({"success": False, "message": "缺少 account_id / file_id / target_storage"}), 400

    # ── 获取源下载链接 ──
    accts = _load_cloud_accounts()
    acc = next((a for a in accts if a.get("id") == account_id), None)
    if not acc:
        return jsonify({"success": False, "message": "账号不存在"}), 404
    client = _get_cloud_account_client(account_id)
    if not client:
        return jsonify({"success": False, "message": "账号凭据未配置"}), 400
    acc_type = acc.get("type", "wps")

    try:
        if share_url and acc_type in ("yidong", "quark"):
            result = client.get_share_download_url(share_url, file_id,
                                                    file_name=file_name, share_path=share_path,
                                                    share_passwd=share_passwd)
            if not result.get("ok"):
                return jsonify({"success": False, "message": result.get("msg", "获取下载链接失败")}), 400
            cdn_url = result["url"]
            file_size = result.get("size", 0)
        elif acc_type == "wps":
            cdn_url = client.get_download_url(DEFAULT_GROUP_ID, file_id)
            # WPS 需要额外获取 size
            try:
                info = client.get_file_info(DEFAULT_GROUP_ID, file_id)
                file_size = info.get("size", 0) if info else 0
            except Exception:
                file_size = 0
        else:
            result = client.get_download_url(file_id)
            if not result.get("ok"):
                return jsonify({"success": False, "message": result.get("msg", "获取下载链接失败")}), 400
            cdn_url = result["url"]
            file_size = result.get("size", 0)

        if not file_name:
            return jsonify({"success": False, "message": "缺少文件名"}), 400
        if not file_size:
            file_size = 1  # 避免除零
    except Exception as e:
        return jsonify({"success": False, "message": f"获取源链接失败: {e}"}), 500

    # ── 解析目标存储 ──
    storages = _load_storages()
    storage = next((s for s in storages if s.get("id") == target_storage), None)
    if not storage:
        return jsonify({"success": False, "message": "目标存储不存在"}), 404
    cookie = (storage.get("cookie", "") or "").strip()
    if not cookie:
        return jsonify({"success": False, "message": "目标存储未配置 Cookie"}), 400
    try:
        wps_sess = build_session(cookie)
        wps_csrf = get_csrf(cookie)
        wps_client = WPSClient(wps_sess, wps_csrf)
    except Exception as e:
        return jsonify({"success": False, "message": f"目标存储连接失败: {e}"}), 400
    # 解析挂载路径
    mp = (storage.get("mountPath", "") or "").strip().lstrip("/")
    target_pid = parent_id_input if parent_id_input else ""
    if mp and not target_pid:
        resolved = _resolve_folder_id(mp)
        if resolved:
            target_pid = resolved
    if not target_pid:
        target_pid = "0"

    # ── 创建任务 ──
    task_id = uuid.uuid4().hex[:12]
    _TRANSFER_RELAY[task_id] = {
        "name": file_name, "total": file_size, "downloaded": 0,
        "stage": "download", "done": False, "success": False, "error": ""
    }

    # ── 后台执行：下载 → 上传 ──
    def _bg_relay():
        cfg = load_config()
        temp_dir = cfg.get("temp_download_dir") or os.path.join(DATA_DIR, "temp_downloads")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, f"{task_id}_{file_name}")
        current_url = cdn_url
        retry_count = 0
        MAX_RETRY = 20

        try:
            # ─── 阶段 1: 从 CDN 下载到临时文件 ───
            downloaded = 0
            while downloaded < file_size and retry_count <= MAX_RETRY:
                try:
                    # 使用 Python requests（无 CORS 限制）
                    hdrs = {"User-Agent": "Mozilla/5.0"}
                    if downloaded > 0:
                        hdrs["Range"] = f"bytes={downloaded}-"
                    r = requests.get(current_url, headers=hdrs, stream=True,
                                     timeout=(30, 300), allow_redirects=True)
                    # 检查状态码（200=全量, 206=断点续传）
                    if r.status_code not in (200, 206):
                        retry_count += 1
                        current_url = _refresh_cdn_url(account_id, client, acc_type, share_url,
                                                        file_id, file_name, share_path, share_passwd)
                        continue

                    mode = "ab" if downloaded > 0 else "wb"
                    with open(temp_path, mode) as f:
                        for chunk in r.iter_content(chunk_size=256 * 1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                t = _TRANSFER_RELAY.get(task_id)
                                if t:
                                    t["downloaded"] = downloaded
                                    t["stage"] = "download"
                    retry_count = 0  # 成功一轮，重置计数
                    print(f"[RELAY] download chunk done, total={downloaded}/{file_size}", flush=True, file=_sys.stderr)
                except Exception as e:
                    retry_count += 1
                    print(f"[RELAY] download retry {retry_count}/{MAX_RETRY}: {e}", flush=True, file=_sys.stderr)
                    if retry_count > MAX_RETRY:
                        raise
                    # 刷新 CDN 链接
                    try:
                        current_url = _refresh_cdn_url(account_id, client, acc_type, share_url,
                                                        file_id, file_name, share_path, share_passwd)
                    except Exception:
                        pass
                    time.sleep(2)

            if downloaded < file_size:
                raise Exception(f"下载未完成: {downloaded}/{file_size}")

            # ─── 阶段 2: 上传到 WPS ───
            t = _TRANSFER_RELAY.get(task_id)
            if t:
                t["stage"] = "upload"

            # 直接从磁盘分块上传，避免把整个文件读进内存（手机容器内存有限）
            actual_size = os.path.getsize(temp_path) if os.path.exists(temp_path) else file_size
            status, result = wps_client.upload_file_with_progress(
                DEFAULT_GROUP_ID, target_pid, None, file_name, actual_size, task_id,
                file_path=temp_path)
            if status not in (200, 201, 204):
                raise Exception(str(result.get("error") or result.get("message") or "上传失败"))

            t = _TRANSFER_RELAY.get(task_id)
            if t:
                t["done"] = True
                t["success"] = True
                t["stage"] = "done"
            print(f"[RELAY] complete {file_name}", flush=True, file=_sys.stderr)
            # 下载并上传完成后，删除夸克网盘里的临时转存文件
            if share_url and hasattr(client, "cleanup"):
                try:
                    client.cleanup(share_url, file_name=file_name)
                except Exception:
                    pass

        except Exception as e:
            traceback.print_exc()
            t = _TRANSFER_RELAY.get(task_id)
            if t:
                t["done"] = True
                t["success"] = False
                t["error"] = str(e)
        finally:
            # 成功后立即删除临时文件，避免大文件长期占用磁盘；
            # 失败时保留便于排查，由启动清理任务删除超过 72 小时的残留。
            t = _TRANSFER_RELAY.get(task_id)
            if t and t.get("success"):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    threading.Thread(target=_bg_relay, daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "name": file_name, "total": file_size})

@bp.route("/api/admin/transfer/relay_progress", methods=["GET"])
@login_required
def api_transfer_relay_progress():
    """查询中转任务进度"""
    task_id = request.args.get("task_id", "").strip()
    t = _TRANSFER_RELAY.get(task_id)
    if not t:
        return jsonify({"success": False, "message": "任务不存在"}), 404
    return jsonify({"success": True, "task": t})

@bp.route("/api/upload/progress/<task_id>")
def api_upload_progress(task_id):
    _cleanup_old_tasks()
    with _upload_lock:
        t = _upload_tasks.get(task_id)
    if not t:
        return jsonify({"done": True, "success": False, "error": "任务不存在或已过期"})
    # 只返回前端需要的字段
    return jsonify({
        "done": t.get("done", False),
        "success": t.get("success", False),
        "stage": t.get("stage", ""),
        "name": t.get("name", ""),
        "total": t.get("total", 0),
        "uploaded": t.get("uploaded", 0),
        "speed": t.get("speed", 0),
        "error": t.get("error", ""),
    })
