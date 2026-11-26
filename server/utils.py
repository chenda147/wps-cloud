# -*- coding: utf-8 -*-
"""
server/utils.py
────────────────────────────────────────────────────────────────────────────
纯函数工具集（无全局可变状态、无 Flask 路由）：
  - 配置 / Cookie 读写
  - 会话构造（WPS 365）
  - 重命名模板渲染
  - 集数提取（服务端版，供预览用）

这些函数被各路由/蓝图模块复用，集中于此避免散落在 app.py。
"""
import os
import json
import re
import time
import hashlib

from .state import CONFIG_FILE, COOKIE_FILE
import tempfile


# ══════════════════════════════════════════════════
# 原子写 JSON（存储健壮性 ★14）
#   先写临时文件 → fsync → os.replace 原子替换目标文件，
#   避免进程崩溃 / 并发写入导致目标文件被截断或半写（数据损坏）。
#   写前保留一份 .bak 备份，便于极端情况下人工恢复。
# ══════════════════════════════════════════════════
def atomic_write_json(path, data, indent=2, keep_backup=True):
    """原子地写入 JSON 文件。path 为绝对路径。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # 保留备份（仅当目标已存在且非空）
        if keep_backup and os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                bak = path + ".bak"
                if os.path.exists(bak):
                    os.remove(bak)
                import shutil
                shutil.copy2(path, bak)
            except Exception:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


# ══════════════════════════════════════════════════
# 配置管理
# ══════════════════════════════════════════════════
from server.state import DATA_DIR


def load_config():
    default = {
        "username": "admin",
        "password": hashlib.sha256("admin".encode()).hexdigest(),
        "server_port": 58000,
        "temp_download_dir": os.path.join(DATA_DIR, "temp_downloads"),
        "strm_output_dir": os.path.join(DATA_DIR, "strm_output"),
        "strm_extensions": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
                            ".m4v", ".ts", ".iso", ".rmvb", ".webm", ".mpg", ".mpeg"],
        "page_password": "",              # 主页访问密码（空=无需密码）
        "strm_base_url": "",              # STRM 代理地址（空=自动用 127.0.0.1:端口）
        "strm_redirect_mode": False,      # CDN 302直链模式（NAS只做跳转，视频流量不经过NAS）
        "strm_incremental": True,         # 增量生成：跳过已存在的 STRM 文件
        "chase_min_size_mb": 0,           # 追更预览/推送：低于该大小(MB)的文件不处理（0=不过滤）
        "chase_video_exts": "",           # 追更预览/推送：视频格式白名单（逗号分隔，空=内置默认）
        "notify": {                       # 通知推送（Server酱/Telegram/企业微信）
            "enabled": False,
            "serverchan_key": "",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "wecom_webhook": "",
            "on_new_eps": True,
            "on_download": True,
            "on_fail": True,
        },
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in default.items():
                cfg.setdefault(k, v)
            return cfg
    atomic_write_json(CONFIG_FILE, default)
    return default


def save_config(cfg):
    atomic_write_json(CONFIG_FILE, cfg)


def load_cookie():
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cookie(data):
    atomic_write_json(COOKIE_FILE, data)


# ══════════════════════════════════════════════════
# WPS 会话
# ══════════════════════════════════════════════════
def build_session(cookie_str):
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0",
        "Accept": "application/json",
        "Referer": "https://365.kdocs.cn/latest",
        "x-requested-with": "XMLHttpRequest",
    })
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, _, v = item.partition("=")
            s.cookies.set(k.strip(), v.strip(), domain=".kdocs.cn")
    return s


def get_csrf(cookie_str):
    for item in cookie_str.split(";"):
        item = item.strip()
        if item.startswith("csrf="):
            return item.split("=", 1)[1]
    return ""


def get_wps_client():
    from .models import WPSClient
    cookie_cfg = load_cookie()
    if not cookie_cfg or not cookie_cfg.get("cookie"):
        return None, "未配置 Cookie，请在管理后台设置"
    s = build_session(cookie_cfg["cookie"])
    csrf = get_csrf(cookie_cfg["cookie"])
    return WPSClient(s, csrf), None


# ══════════════════════════════════════════════════
# 重命名模板渲染
# ══════════════════════════════════════════════════
def _apply_rename_template(ep_num, rename_template, fallback_name, season=0, tmdb_id="", original_filename=""):
    """
    根据模板生成重命名结果。
    支持的占位符：
      {name} / {fallback}  剧名（fallback_name）
      {ep} / {ep:02d} / {ep:03d}  集数（可补零）
      {season} / {season:02d} / {s} / {s:02d}  季
      {tmdb_id} / {tmdb}    TMDB ID（未在模板中显式放置时自动追加 [tmdbid=xxx]）
      {orig}               原始文件名（去扩展名）
    若模板为空或渲染后仍无有效内容，退回默认命名 {剧名} SxxExx。
    """
    # 提取原始文件扩展名
    ext = os.path.splitext(original_filename)[1].lower() if original_filename else ""
    ARTICLE_EXT = {'.tar', '.rar', '.7z', '.zip', '.iso', '.cab', '.arj', '.lzh', '.lha'}
    if rename_template and rename_template.strip():
        template = rename_template.strip()
        # 先替换 {name} / {fallback}
        if fallback_name:
            template = template.replace("{name}", fallback_name)
            template = template.replace("{fallback}", fallback_name)
        # 替换 {orig}
        template = template.replace("{orig}", os.path.splitext(original_filename)[0])
        # 替换 {season} / {season:02d} / {s} / {s:02d}
        def _season_repl(m):
            pad = int(m.group(1)) if m.group(1) else 0
            return str(season).zfill(pad) if pad else str(season)
        template = re.sub(r'\{season(?::(\d+)d)?\}', _season_repl, template)
        template = re.sub(r'\{s(?::(\d+)d)?\}', _season_repl, template)
        # 替换 {tmdb_id} / {tmdb}，未在模板中显式放置则自动追加 (Jellyfin 格式)
        if tmdb_id:
            has_placeholder = "{tmdb}" in template or "{tmdb_id}" in template or "[tmdbid=" in template
            if has_placeholder:
                template = template.replace("{tmdb_id}", f"[tmdbid={tmdb_id}]")
                template = template.replace("{tmdb}", f"[tmdbid={tmdb_id}]")
            else:
                template = template.replace("{tmdb_id}", "")
                template = template.replace("{tmdb}", "")
                # 自动追加 TMDB ID
                template += " [tmdbid=" + tmdb_id + "]"
        else:
            template = template.replace("{tmdb_id}", "")
            template = template.replace("{tmdb}", "")
        # 替换 {ep} / {ep:02d} / {ep:03d}
        try:
            result = template.format(ep=ep_num)
        except (ValueError, KeyError):
            result = template.replace("{ep}", str(ep_num))
        # 确定目标扩展名（压缩包内是视频，使用 .mkv）
        if result:
            has_vext = result.rstrip('.').lower().endswith(
                ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
                 '.m4v', '.mpg', '.mpeg', '.3gp', '.ts', '.rmvb', '.vob',
                 '.divx', '.xvid', '.asf', '.f4v'))
            if not has_vext:
                result += '.mkv' if ext in ARTICLE_EXT else ext
        return result
    season_str = f" S{season:02d}" if season > 0 else ""
    result = f"{fallback_name}{season_str}E{ep_num:02d}" if fallback_name else f"E{ep_num:02d}"
    if result:
        has_vext = result.rstrip('.').lower().endswith(
            ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
             '.m4v', '.mpg', '.mpeg', '.3gp', '.ts', '.rmvb', '.vob',
             '.divx', '.xvid', '.asf', '.f4v'))
        if not has_vext:
            result += '.mkv' if ext in ARTICLE_EXT else ext
    return result


# ══════════════════════════════════════════════════
# 服务端集数提取（用于预览）
# ══════════════════════════════════════════════════
def _server_extract_episode(filename, ep_pattern="", is_movie=False):
    """
    服务端版集数提取，与客户端 chase_engine.py 逻辑一致，用于预览匹配结果。
    返回 (ep_num or None, matched_text or None)

    is_movie=True（电影/单文件分类）时：
    - 跳过会把"年份(如2024)""结尾纯数字(如 .185)""方括号数字"等
      误判为集数的规则，仅识别明确的剧集标记（第N集/E(\\d+) 等）。
    - 因为电影是整部单文件，不存在"第几集"概念。
    """
    name = os.path.splitext(filename)[0]

    # 用户自定义正则（最高优先级）
    if ep_pattern and ep_pattern.strip():
        m = re.search(ep_pattern.strip(), name)
        if m:
            try:
                return int(m.group(1)), m.group(0)
            except (ValueError, IndexError):
                pass
        return None, None

    if is_movie:
        # 电影/单文件：只识别明确的剧集标记，避免年份/结尾数字被当集数
        patterns = [
            (r'S\d+E(\d+)', 1),             # S01E185
            (r'[Ee](\d+)', 1),              # E185 / e185
            (r'第\s*(\d+)\s*集', 1),         # 第185集
            (r'第\s*(\d+)\s*话', 1),         # 第185话
            (r'第\s*(\d+)\s*回', 1),         # 第185回
        ]
    else:
        patterns = [
            (r'S\d+E(\d+)', 1),             # S01E185
            (r'[Ee](\d+)', 1),              # E185 / e185
            (r'第\s*(\d+)\s*集', 1),         # 第185集
            (r'第\s*(\d+)\s*话', 1),         # 第185话
            (r'第\s*(\d+)\s*回', 1),         # 第185回
            (r'[\[\(（](\d+)[\]\)）]', 1),   # (185) [185]
            (r'\s+-\s+(\d+)\b', 1),          #  - 185
            (r'\.(\d{2,4})$', 1),            # .185
            (r'_(\d{2,4})$', 1),             # _185
            (r'^(\d+)$', 1),                 # 纯数字文件名
        ]
    # 弱格式（括号/横线/结尾数字/纯数字）可能把年份误识别成集数，
    # 例如 "九门 (2026) [tmdbid=271016].mp4" 的 (2026)。命中年份时跳过，
    # 继续尝试下一格式，避免误报出 2026 这类虚高集数。
    weak_formats = {
        r'[\[\(（](\d+)[\]\)）]',
        r'\s+-\s+(\d+)\b',
        r'\.(\d{2,4})$',
        r'_(\d{2,4})$',
        r'^(\d+)$',
    }
    for pat, group in patterns:
        m = re.search(pat, name)
        if m:
            try:
                ep = int(m.group(group))
                if pat in weak_formats and 1900 <= ep <= 2100:
                    continue
                return ep, m.group(0)
            except (ValueError, IndexError):
                continue
    return None, None


# ══════════════════════════════════════════════════
# 共享辅助函数（被各路由蓝图复用，集中于此避免散落在 app.py）
# ══════════════════════════════════════════════════
from functools import wraps
from flask import session, redirect, url_for, request, jsonify

from .state import (
    DATA_DIR, DEFAULT_GROUP_ID, STORAGES_FILE, CLOUD_ACCOUNTS_FILE, _mount_root_cache,
    SERVER_BUILD,
)
from .models import WPSClient


def get_config():
    """配置读取别名（修复原 app.py 中 debug 接口调用 get_config 的 NameError）"""
    return load_config()


def _check_password(stored, given):
    """兼容明文密码和 sha256 哈希"""
    if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
        import hashlib as _hl
        return stored == _hl.sha256(given.encode()).hexdigest()
    return stored == given


def _auth_diag_info():
    """401 时附带的诊断信息：cookie 是否带到、session 里有什么、当前构建版本。
    前端拿到后展示给用户，可一眼区分‘cookie 没带’还是‘密钥/进程变了’。"""
    try:
        from flask import current_app
        ck_name = current_app.config.get("SESSION_COOKIE_NAME", "session")
    except Exception:
        ck_name = "wps_cloud_sid"
    return {
        "build": SERVER_BUILD,
        "cookie_present": bool((request.cookies or {}).get(ck_name)),
        "cookie_name": ck_name,
        "session_keys": sorted(session.keys()),
        "logged_in": bool(session.get("logged_in")),
        "admin_logged_in": bool(session.get("admin_logged_in")),
        "host": request.host,
        "path": request.path,
    }


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("admin_logged_in") and not session.get("logged_in"):
            diag = _auth_diag_info()
            print(f"[auth-401] cookie={diag['cookie_present']} "
                  f"sid={diag['logged_in']} adm={diag['admin_logged_in']} "
                  f"keys={diag['session_keys']} host={diag['host']} "
                  f"path={diag['path']} build={diag['build']}", flush=True)
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "message": "未登录", "_diag": diag}), 401
            return redirect(url_for("admin.admin_login_page"))
        return f(*a, **k)
    return wrapper


def format_size(size):
    if not size or size <= 0:
        return "0 B"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {u}"
        size /= 1024
    return f"{size:.1f} PB"


def _load_storages():
    if os.path.exists(STORAGES_FILE):
        try:
            with open(STORAGES_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []


def _save_storages(storages):
    atomic_write_json(STORAGES_FILE, storages)


def _load_cloud_accounts():
    if os.path.exists(CLOUD_ACCOUNTS_FILE):
        try:
            with open(CLOUD_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []


def _save_cloud_accounts(accts):
    atomic_write_json(CLOUD_ACCOUNTS_FILE, accts)


def _resolve_folder_id_with_client(client, mount_path):
    """用指定 client 将挂载路径解析为真实文件夹 ID"""
    if not mount_path:
        return None
    if len(mount_path) >= 16 and all(c in '0123456789abcdef' for c in mount_path.lower()):
        return mount_path
    parts = [p.strip() for p in mount_path.strip().split("/") if p.strip()]
    if not parts:
        return None
    cur = None
    for part in parts:
        try:
            found = False
            for f in client.get_all_files(DEFAULT_GROUP_ID, cur):
                if f.get("ftype") == "folder" and (f.get("fname") or f.get("name") or "").strip() == part:
                    cur = str(f.get("id", ""))
                    found = True
                    break
            if not found:
                return None
        except Exception:
            return None
    return cur


def save_upload_to_temp(file_storage, temp_dir, chunk_size=1024 * 1024):
    """把上传流分块写入临时文件，返回 (path, size)。

    避免 `file.read()` 把整个大文件读进内存（手机容器/小内存机器上会 OOM）。"""
    os.makedirs(temp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="upload_", suffix=".part", dir=temp_dir)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = file_storage.stream.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        raise
    return path, size


def cleanup_temp_dir(path, max_age_sec=72 * 3600):
    """删除临时目录中超过 max_age_sec 的残留文件，返回删除数量。"""
    removed = 0
    try:
        now = time.time()
        for name in os.listdir(path):
            fp = os.path.join(path, name)
            if not os.path.isfile(fp):
                continue
            try:
                if now - os.path.getmtime(fp) > max_age_sec:
                    os.remove(fp)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def _get_mount_client_and_root(mount_ctx_or_storage_id):
    """根据挂载上下文，返回 (WPSClient, real_root_id)。失败返回 (None, None)。"""
    sid = mount_ctx_or_storage_id[7:] if mount_ctx_or_storage_id.startswith("_mount_") else mount_ctx_or_storage_id
    storages = _load_storages()
    for s in storages:
        if s["id"] == sid:
            store_cookie = s.get("cookie", "")
            mp = (s.get("mountPath", "") or "").strip().lstrip("/")
            if store_cookie:
                try:
                    sess = build_session(store_cookie)
                    csrf2 = get_csrf(store_cookie)
                    cli = WPSClient(sess, csrf2)
                except Exception:
                    return None, None
            else:
                cli, _ = get_wps_client()
                if not cli:
                    return None, None
            if sid not in _mount_root_cache:
                if mp:
                    rid = _resolve_folder_id_with_client(cli, mp)
                    if rid:
                        _mount_root_cache[sid] = rid
                else:
                    _mount_root_cache[sid] = None  # None = 挂载在根目录
            return cli, _mount_root_cache.get(sid)
    return None, None


# 显式导出（含下划线函数）：import * 默认跳过下划线名，
# 但本模块的函数需被 app.py / 蓝图模块共享，故在 __all__ 中列出。
__all__ = [
    "load_config", "save_config", "load_cookie", "save_cookie",
    "build_session", "get_csrf", "get_wps_client",
    "_apply_rename_template", "_server_extract_episode",
    "get_config", "_check_password", "login_required", "format_size",
    "_load_storages", "_save_storages", "_load_cloud_accounts", "_save_cloud_accounts",
    "_resolve_folder_id_with_client", "_get_mount_client_and_root",
    "atomic_write_json", "save_upload_to_temp", "cleanup_temp_dir",
]
