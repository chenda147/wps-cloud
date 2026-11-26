# -*- coding: utf-8 -*-
"""
server/routes_public.py
────────────────────────────────────────────────────────────────────────────
公开访问路由蓝图（无需管理员登录）：
  - 主页 / 解锁
  - 网盘文件浏览 / 面包屑 / 搜索
  - 播放 / 图片 / 下载（CDN 代理 + 302 直链）
  - 分享链接获取下载 URL
  - 调试接口（CDN 过期时间）
"""
import os
import sys
import re
import json
import time
import datetime
import mimetypes
import traceback
from urllib.parse import quote, unquote, urlparse, parse_qs

from flask import (
    Blueprint, request, jsonify, session, redirect, url_for, Response, send_file, stream_with_context,
)
from flask import render_template as _render

from .state import (
    DEFAULT_GROUP_ID, VIDEO_EXTS, IMAGE_EXTS, AUDIO_EXTS, SERVER_BUILD,
)
from .utils import (
    load_config, get_wps_client, _check_password, _get_mount_client_and_root,
    _load_storages, format_size, get_config,
)

bp = Blueprint("public", __name__)


@bp.route("/")
def public_index():
    # 后台登录（/api/login）只写 logged_in，解锁页写 admin_logged_in，这里两者都认
    if session.get("admin_logged_in") or session.get("logged_in"):
        return _render("index.html")
    return _render("page_lock.html")


@bp.route("/api/page_unlock", methods=["POST"])
def api_page_unlock():
    data = request.get_json(silent=True) or {}
    user = data.get("username", "")
    pw = data.get("password", "")
    cfg = load_config()
    if user != cfg.get("username", "admin"):
        return jsonify({"success": False, "message": "用户名或密码错误"})
    if _check_password(cfg.get("password", ""), pw):
        session.permanent = True
        session["admin_logged_in"] = True
        session["logged_in"] = True
        session["username"] = user
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "用户名或密码错误"})


@bp.route("/api/public/files")
def api_public_files():
    parent_id = request.args.get("parent_id", "").strip() or None
    mount_ctx = request.args.get("mount_ctx", "").strip()
    view = request.args.get("view", "list")

    use_mount_client = None
    effective_parent_id = parent_id

    if not parent_id:
        storages = _load_storages()
        entries = []
        for s in storages:
            mp = (s.get("mountPath", "") or "").strip().lstrip("/")
            name = s.get("name") or mp or "unnamed"
            entries.append({
                "id": f"_mount_{s['id']}",
                "name": name,
                "type": "folder",
                "size": 0, "size_display": "",
                "mtime": 0, "mtime_display": "",
                "ext": "",
                "is_video": False, "is_image": False, "is_audio": False,
            })
        return jsonify({"success": True, "files": entries, "parent_id": ""})

    if parent_id and parent_id.startswith("_mount_"):
        mount_cli, root_id = _get_mount_client_and_root(parent_id)
        if mount_cli:
            use_mount_client = mount_cli
        effective_parent_id = root_id
    elif mount_ctx and parent_id:
        mount_cli, _ = _get_mount_client_and_root(mount_ctx)
        if mount_cli:
            use_mount_client = mount_cli
        effective_parent_id = parent_id

    if use_mount_client:
        client = use_mount_client
    else:
        client, err = get_wps_client()
        if err:
            return jsonify({"error": err, "files": [], "need_cookie": True}), 200

    try:
        files = client.get_all_files(DEFAULT_GROUP_ID, effective_parent_id)
        result = []
        for f in files:
            fid = str(f.get("id", ""))
            fname = f.get("fname") or f.get("name") or "unnamed"
            ftype = f.get("ftype") or "file"
            fsize = int(f.get("fsize") or f.get("size") or 0)
            mtime = f.get("mtime") or 0
            if isinstance(mtime, str):
                try:
                    mtime = int(mtime)
                except Exception:
                    mtime = 0
            ext = os.path.splitext(fname)[1].lower() if ftype == "file" else ""
            item = {
                "id": fid, "name": fname, "type": ftype, "size": fsize,
                "size_display": format_size(fsize),
                "mtime": mtime,
                "mtime_display": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
                "ext": ext,
                "is_video": ext in VIDEO_EXTS,
                "is_image": ext in IMAGE_EXTS,
                "is_audio": ext in AUDIO_EXTS,
            }
            if item["is_video"]:
                item["stream_url"] = f"/api/public/stream/{fid}"
            if item["is_image"]:
                item["raw_url"] = f"/api/public/raw/{fid}"
            result.append(item)

        folders = sorted([x for x in result if x["type"] == "folder"], key=lambda x: x["name"].lower())
        files_ = sorted([x for x in result if x["type"] != "folder"], key=lambda x: x["name"].lower())
        return jsonify({"success": True, "files": folders + files_, "parent_id": parent_id or ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "files": [], "need_cookie": True}), 200


@bp.route("/api/public/auth_status")
def api_public_auth_status():
    return jsonify({"logged_in": session.get("logged_in", False),
                    "build": SERVER_BUILD})


@bp.route("/api/public/share_url/<file_id>")
def api_public_share_url(file_id):
    _sys = sys
    print(f"[SHARE] file_id={file_id!r}", flush=True, file=_sys.stderr)
    client, err = get_wps_client()
    if err:
        return jsonify({"error": err}), 500
    try:
        host = request.host_url.rstrip("/")
        mc = request.args.get("mount_ctx", "").strip()
        qs = f"?mount_ctx={mc}" if mc else ""
        f = client.get_file_info(DEFAULT_GROUP_ID, file_id)
        fname = (f.get("fname") or f.get("name") or "file") if f else "file"
        # URL 末尾带真实文件名/扩展名，媒体服务器（Emby/Jellyfin）才能识别容器格式
        proxy_url = f"{host}/stream/{file_id}/{quote(fname)}{qs}"

        cdn_url = None
        try:
            cdn_url = client.get_download_url(DEFAULT_GROUP_ID, file_id)
        except Exception:
            pass

        print(f"[SHARE] proxy_url={proxy_url} fname={fname}", flush=True, file=_sys.stderr)
        return jsonify({"url": proxy_url, "wps_url": cdn_url, "filename": fname})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp.route("/api/public/fileinfo/<file_id>")
def api_public_fileinfo(file_id):
    client, err = get_wps_client()
    mount_ctx = request.args.get("mount_ctx", "").strip()
    if mount_ctx and mount_ctx.startswith("_mount_"):
        mount_cli, _ = _get_mount_client_and_root(mount_ctx)
        if mount_cli:
            client = mount_cli
    if err and not client:
        return jsonify({"error": err}), 500
    try:
        info = client.get_file_info(DEFAULT_GROUP_ID, file_id)
        if not info:
            return jsonify({"error": "文件不存在"}), 404
        return jsonify({
            "id": str(info.get("id", "")),
            "name": info.get("fname") or info.get("name") or "",
            "size": int(info.get("fsize") or info.get("size") or 0),
            "parent_id": str(info.get("pid") or info.get("parent_id") or ""),
            "ftype": info.get("ftype") or "file",
            "mtime": info.get("mtime") or 0,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/play/<file_id>")
def page_play(file_id):
    mount_ctx = request.args.get("mount_ctx", "")
    name = request.args.get("name", "视频")
    pid = request.args.get("pid", "")
    size = request.args.get("size", "0")
    return _render("play.html", file_id=file_id, mount_ctx=mount_ctx, name=name, pid=pid, size=size)


@bp.route("/img/<file_id>")
def page_img(file_id):
    mount_ctx = request.args.get("mount_ctx", "")
    name = request.args.get("name", "图片")
    pid = request.args.get("pid", "")
    return _render("img.html", file_id=file_id, mount_ctx=mount_ctx, name=name, pid=pid)


def _find_parent_dir(client, group_id, target_id, start_parent=None, max_scan=2000):
    """层序遍历目录树，查找 target_id 的直接父目录。
    返回 (父目录名, 父目录id)；找不到返回 None。
    start_parent=None 表示群组根；传入挂载根可缩小扫描范围。"""
    from collections import deque
    queue = deque([start_parent])
    visited = set()
    scanned = 0
    while queue and scanned < max_scan:
        pid = queue.popleft()
        if pid in visited:
            continue
        visited.add(pid)
        try:
            items = client.get_all_files(group_id, pid)
        except Exception:
            continue
        scanned += 1
        for f in items:
            if str(f.get("id")) == str(target_id):
                return (f.get("fname") or f.get("name") or "", pid)
            if (f.get("ftype") or "file") == "folder":
                queue.append(str(f.get("id")))
    return None


@bp.route("/api/public/breadcrumbs")
def api_public_breadcrumbs():
    parent_id = request.args.get("parent_id", "").strip() or None
    mount_ctx = request.args.get("mount_ctx", "").strip()

    if not parent_id:
        return jsonify({"breadcrumbs": [{"name": "我的企业文档", "id": ""}]})

    if parent_id.startswith("_mount_"):
        storage_id = parent_id[7:]
        storages = _load_storages()
        mount_name = parent_id
        for s in storages:
            if s["id"] == storage_id:
                mount_name = s.get("name") or s.get("mountPath", "").strip("/") or parent_id
                break
        return jsonify({"breadcrumbs": [
            {"name": "我的企业文档", "id": ""},
            {"name": mount_name, "id": parent_id}
        ]})

    mount_name = ""
    if mount_ctx and mount_ctx.startswith("_mount_"):
        sid = mount_ctx[7:]
        storages = _load_storages()
        for s in storages:
            if s["id"] == sid:
                mount_name = s.get("name") or s.get("mountPath", "").strip("/") or ""
                break
    mount_root_id = None
    if mount_ctx and mount_ctx.startswith("_mount_"):
        _, rid = _get_mount_client_and_root(mount_ctx)
        mount_root_id = rid

    if mount_root_id and str(parent_id) == str(mount_root_id):
        if mount_name:
            return jsonify({"breadcrumbs": [
                {"name": "我的企业文档", "id": ""},
                {"name": mount_name, "id": mount_ctx}
            ]})
        return jsonify({"breadcrumbs": [{"name": "我的企业文档", "id": ""}]})

    client, err = get_wps_client()
    if mount_ctx and mount_ctx.startswith("_mount_"):
        mount_cli, _ = _get_mount_client_and_root(mount_ctx)
        if mount_cli:
            client = mount_cli
    if err and not client:
        return jsonify({"error": err}), 500

    chain = []
    visited = set()
    cur = parent_id
    while cur:
        if cur in visited:
            break
        visited.add(cur)
        if mount_root_id and str(cur) == str(mount_root_id):
            break
        # 优先用文件详情接口拿 parentid 逐级向上（O(深度)），失败再全树扫描
        parent = None
        info = None
        try:
            info = client.get_file_info(DEFAULT_GROUP_ID, cur)
        except Exception:
            info = None
        if info:
            parent = info.get("parentid") or info.get("parent_id") or info.get("parent")
        if parent is not None:
            chain.append({"name": info.get("fname") or info.get("name") or "", "id": str(cur)})
            cur = str(parent)
            continue
        found = _find_parent_dir(client, DEFAULT_GROUP_ID, cur, mount_root_id)
        if not found:
            break
        chain.append({"name": found[0], "id": str(cur)})
        cur = found[1]

    crumbs = [{"name": "我的企业文档", "id": ""}]
    if mount_name:
        crumbs.append({"name": mount_name, "id": mount_ctx})
    while chain:
        crumbs.append(chain.pop())
    return jsonify({"breadcrumbs": crumbs})


@bp.route("/api/public/search")
def api_public_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"files": []})
    client, err = get_wps_client()
    if err:
        return jsonify({"error": err, "files": []}), 200
    try:
        files = client.search_files(DEFAULT_GROUP_ID, q, max_depth=10, max_results=200)
        result = []
        for f in files:
            fid = str(f.get("id", ""))
            fname = f.get("fname") or f.get("name") or "unnamed"
            ftype = f.get("ftype") or "file"
            fsize = int(f.get("fsize") or f.get("size") or 0)
            mtime = f.get("mtime") or 0
            if isinstance(mtime, str):
                try:
                    mtime = int(mtime)
                except Exception:
                    mtime = 0
            ext = os.path.splitext(fname)[1].lower() if ftype == "file" else ""
            result.append({
                "id": fid, "name": fname, "type": ftype, "size": fsize,
                "size_display": format_size(fsize),
                "mtime": mtime,
                "mtime_display": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
                "ext": ext,
                "is_video": ext in VIDEO_EXTS,
                "is_image": ext in IMAGE_EXTS,
                "is_audio": ext in AUDIO_EXTS,
            })
        folders = sorted([x for x in result if x["type"] == "folder"], key=lambda x: x["name"].lower())
        files_ = sorted([x for x in result if x["type"] != "folder"], key=lambda x: x["name"].lower())
        return jsonify({"success": True, "files": folders + files_})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "files": []})


_VIDEO_MIME = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".rmvb": "video/vnd.rn-realvideo",
    ".webm": "video/webm",
}


def _cdn_filename(cdn_url):
    """从 WPS 直链的 response-content-disposition 参数中取真实文件名。"""
    try:
        qs = parse_qs(urlparse(cdn_url).query)
        disp = (qs.get("response-content-disposition") or [""])[0]
        # WPS 会做多层 URL 编码，统一解两次再提取
        disp = unquote(unquote(disp))
        m = re.search(r"filename\*?\s*=\s*([^;]+)", disp, re.I)
        if not m:
            return ""
        name = m.group(1).strip().strip('"')
        # 去掉 RFC5987 的 charset'lang' 前缀（如 UTF-8''）
        name = re.sub(r"^[A-Za-z0-9_-]+'[A-Za-z0-9_-]*'", "", name)
        return unquote(name).strip()
    except Exception:
        return ""


def _video_content_type(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return _VIDEO_MIME.get(ext) or mimetypes.guess_type(filename or "")[0] or ""


def _proxy_cdn(file_id, mount_ctx, as_attachment=False, filename=None):
    """从 WPS API 获取 CDN 直链，再用 Flask 代理请求（带正确 Referer），支持 Range。"""
    client, err = get_wps_client()
    if mount_ctx and mount_ctx.startswith("_mount_"):
        mount_cli, _ = _get_mount_client_and_root(mount_ctx)
        if mount_cli:
            client = mount_cli
    if err and not client:
        return jsonify({"error": err}), 500
    try:
        cdn_url = client.get_download_url(DEFAULT_GROUP_ID, file_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.kdocs.cn/",
        "Origin": "https://www.kdocs.cn",
    }
    range_header = request.headers.get("Range")
    if range_header:
        proxy_headers["Range"] = range_header

    try:
        r = client.s.get(cdn_url, headers=proxy_headers, stream=True, timeout=(30, 3600), allow_redirects=True)
    except Exception as e:
        return jsonify({"error": f"请求CDN失败: {e}"}), 502

    cdn_name = _cdn_filename(cdn_url)
    content_type = r.headers.get("Content-Type", "")
    if not content_type or content_type in ("application/octet-stream", "binary/octet-stream", "application/unknown"):
        content_type = _video_content_type(cdn_name) or content_type or "application/octet-stream"
    resp_headers = {"Content-Type": content_type, "Accept-Ranges": "bytes"}

    if r.status_code == 206:
        status_code = 206
        resp_headers["Content-Range"] = r.headers.get("Content-Range", "")
        resp_headers["Content-Length"] = r.headers.get("Content-Length", "0")
    elif r.status_code == 200:
        status_code = 200
        clen = r.headers.get("Content-Length")
        if clen:
            resp_headers["Content-Length"] = clen
        if range_header:
            try:
                range_val = range_header.replace("bytes=", "").split("-")
                start = int(range_val[0]) if range_val[0] else 0
                end = int(range_val[1]) if len(range_val) > 1 and range_val[1] else None
                total = int(clen) if clen else None
                if total and end is None:
                    end = total - 1
            except (ValueError, IndexError):
                pass
    else:
        return jsonify({"error": f"CDN返回状态码 {r.status_code}"}), 502

    if as_attachment and filename:
        resp_headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    elif cdn_name:
        # 内联播放时把真实文件名带出去，Emby/播放器据此识别容器格式
        resp_headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(cdn_name)}"

    # Emby 探测常用 HEAD：只回头部，避免把整个文件拉一遍
    if request.method == "HEAD":
        r.close()
        return Response(status=status_code, headers=resp_headers)

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return Response(stream_with_context(generate()), status=status_code, headers=resp_headers)


@bp.route("/api/public/stream/<file_id>")
@bp.route("/api/public/stream/<file_id>/<path:name>")
def api_public_stream(file_id, name=None):
    mount_ctx = request.args.get("mount_ctx", "").strip()
    return _proxy_cdn(file_id, mount_ctx, as_attachment=False)


@bp.route("/api/public/raw/<file_id>")
def api_public_raw(file_id):
    mount_ctx = request.args.get("mount_ctx", "").strip()
    return _proxy_cdn(file_id, mount_ctx, as_attachment=False)


@bp.route("/api/public/download/<file_id>")
def api_public_download(file_id):
    mount_ctx = request.args.get("mount_ctx", "").strip()
    file_name = request.args.get("name", "file")
    return _proxy_cdn(file_id, mount_ctx, as_attachment=True, filename=file_name)


@bp.route("/stream/<file_id>")
@bp.route("/stream/<file_id>/<path:name>")
def public_stream_shortcut(file_id, name=None):
    mount_ctx = request.args.get("mount_ctx", "")
    return _proxy_cdn(file_id, mount_ctx, as_attachment=False)


@bp.route("/raw/<file_id>")
def public_raw_shortcut(file_id):
    mount_ctx = request.args.get("mount_ctx", "")
    return _proxy_cdn(file_id, mount_ctx, as_attachment=False)


@bp.route("/dl/<file_id>")
def public_dl_shortcut(file_id):
    mount_ctx = request.args.get("mount_ctx", "")
    file_name = request.args.get("name", "file")
    return _proxy_cdn(file_id, mount_ctx, as_attachment=True, filename=file_name)


@bp.route("/r/<file_id>")
@bp.route("/r/<file_id>/<path:name>")
def public_redirect_cdn(file_id, name=None):
    """获取 WPS CDN 签名直链并 302 跳转，NAS 只处理一个轻量请求"""
    client, err = get_wps_client()
    mount_ctx = request.args.get("mount_ctx", "")
    if mount_ctx and mount_ctx.startswith("_mount_"):
        mount_cli, _ = _get_mount_client_and_root(mount_ctx)
        if mount_cli:
            client = mount_cli
    if err or not client:
        return jsonify({"error": err or "无可用客户端"}), 500
    try:
        cdn_url = client.get_download_url(DEFAULT_GROUP_ID, file_id)
        return redirect(cdn_url, code=302)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/debug/cdn-expiry")
def debug_cdn_expiry():
    """调试用：获取一个CDN链接并显示过期时间"""
    cfg = get_config()
    if cfg["username"] == "admin":
        return jsonify({"error": "请先配置WPS账号"}), 400
    client, err = get_wps_client()
    if err or not client:
        return jsonify({"error": err or "无可用客户端"}), 500
    try:
        files = client.get_files(DEFAULT_GROUP_ID, count=1)
        if not files:
            return jsonify({"error": "WPS云盘中没有任何文件"}), 404
        fid = files[0]["id"]
        fname = files[0].get("fname", files[0].get("name", "未知"))
        cdn_url = client.get_download_url(DEFAULT_GROUP_ID, fid)

        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(cdn_url)
        qs = parse_qs(parsed.query)
        expires_vals = qs.get("Expires", [])
        if expires_vals:
            exp_ts = int(expires_vals[0])
            now_ts = int(time.time())
            remain_seconds = exp_ts - now_ts
            exp_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp_ts))
            now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts))
            if remain_seconds > 86400:
                remain_str = f"{remain_seconds // 86400} 天 {remain_seconds % 86400 // 3600} 小时"
            elif remain_seconds > 3600:
                remain_str = f"{remain_seconds // 3600} 小时 {remain_seconds % 3600 // 60} 分钟"
            else:
                remain_str = f"{remain_seconds // 60} 分钟"
            return jsonify({
                "file_name": fname,
                "file_id": fid,
                "now": now_time,
                "expires_at": exp_time,
                "expires_ts": exp_ts,
                "remain": remain_str,
                "remain_seconds": remain_seconds,
            })
        else:
            return jsonify({
                "file_name": fname,
                "file_id": fid,
                "cdn_url": cdn_url[:120] + "...",
                "note": "URL中没有 Expires 参数"
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
