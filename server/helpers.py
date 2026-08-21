# -*- coding: utf-8 -*-

"""server/helpers.py - 共享业务逻辑层（从 app.py 迁出的辅助函数与状态）。"""



import json
import os
import sys
import time
import base64
import hashlib
import secrets
import threading
import uuid
import traceback
import logging
from functools import wraps

logger = logging.getLogger(__name__)
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, stream_with_context
import re
import requests
from .state import *
from .utils import *
from .models import WPSClient, API_BASE
from .routes_public import bp as public_bp



from .state import *
from .utils import *
from .models import WPSClient, API_BASE




"""
WPS 365 云盘管理面板 - Flask 应用 v3
界面: AList 风格文件浏览器 | 搜索 | 视频/图片/音频预览 | Cookie 管理
"""
SERVER_VERSION = "1.0.2"
_BEIJING_TZ = timezone(timedelta(hours=8))
def _beijing_now():
    """返回北京时间"""
    return datetime.now(_BEIJING_TZ)
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
        "strm_redirect_mode": False,     # CDN 302直链模式（NAS只做跳转，视频流量不经过NAS）
        "strm_incremental": True,        # 增量生成：跳过已存在的 STRM 文件
        "chase_min_size_mb": 0,          # 追更预览/推送：低于该大小(MB)的文件不处理（0=不过滤）
        "chase_video_exts": "",          # 追更预览/推送：视频格式白名单（逗号分隔，空=内置默认）
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in default.items():
                cfg.setdefault(k, v)
            return cfg
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(default, f, indent=2, ensure_ascii=False)
    return default
def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
def load_cookie():
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
def save_cookie(data):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
def build_session(cookie_str):
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
    cookie_cfg = load_cookie()
    if not cookie_cfg or not cookie_cfg.get("cookie"):
        return None, "未配置 Cookie，请在管理后台设置"
    s = build_session(cookie_cfg["cookie"])
    csrf = get_csrf(cookie_cfg["cookie"])
    return WPSClient(s, csrf), None
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("admin.admin_login_page"))
        return f(*args, **kwargs)
    return decorated
STORAGES_FILE = os.path.join(DATA_DIR, "storages.json")
def _load_storages():
    if os.path.exists(STORAGES_FILE):
        try:
            with open(STORAGES_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []
def _save_storages(storages):
    with open(STORAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(storages, f, ensure_ascii=False, indent=2)
def _resolve_folder_id(mount_path):
    """将挂载路径（如 /166）解析为 WPS 实际文件夹 ID。
    在根目录下按名称匹配文件夹，支持多级路径逐层查找。
    找不到则返回 None，调用方应回退到根目录。"""
    if not mount_path:
        return None
    # 如果看起来像 WPS 的长 ID（16位以上hex），直接使用
    if len(mount_path) >= 16 and all(c in '0123456789abcdef' for c in mount_path.lower()):
        return mount_path
    client, err = get_wps_client()
    if err:
        return None
    # 按 / 拆分，逐层查找
    parts = [p.strip() for p in mount_path.strip().split("/") if p.strip()]
    if not parts:
        return None
    current_parent = None
    for part in parts:
        try:
            files = client.get_all_files(DEFAULT_GROUP_ID, current_parent)
            found = None
            for f in files:
                if f.get("ftype") == "folder" and (f.get("fname") or f.get("name") or "").strip() == part:
                    found = str(f.get("id", ""))
                    break
            if not found:
                return None
            current_parent = found
        except Exception:
            return None
    return current_parent
CLOUD_ACCOUNTS_FILE = os.path.join(DATA_DIR, "cloud_accounts.json")
def _load_cloud_accounts():
    if os.path.exists(CLOUD_ACCOUNTS_FILE):
        try:
            with open(CLOUD_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []
def _save_cloud_accounts(accts):
    with open(CLOUD_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accts, f, ensure_ascii=False, indent=2)
def _get_cloud_account_client(aid):
    """根据云盘账号 ID 构建对应客户端"""
    accts = _load_cloud_accounts()
    for a in accts:
        if a.get("id") == aid:
            credential = a.get("cookie", "").strip()
            acc_type = a.get("type", "wps")
            if not credential:
                return None
            try:
                if acc_type == "wps":
                    # 保留原有 WPS 客户端逻辑
                    sess = build_session(credential)
                    csrf2 = get_csrf(credential)
                    return WPSClient(sess, csrf2)
                else:
                    from .cloud_clients import create_cloud_client
                    return create_cloud_client(acc_type, credential)
            except Exception as e:
                logger.warning(f"创建云盘客户端失败: {e}")
                return None
    return None
_UPLOAD_PLANS = {}  # plan_id → {plan, wps_client, target_pid, file_name, md5_hex, sha1_hex}
_TRANSFER_RELAY = {}  # task_id → {name, total, downloaded, stage, done, success, error}
def _refresh_cdn_url(account_id, client, acc_type, share_url, file_id, file_name, share_path, share_passwd):
    """刷新 CDN 下载链接（用于重试）"""
    if share_url and acc_type in ("yidong", "quark"):
        result = client.get_share_download_url(share_url, file_id,
                                                file_name=file_name, share_path=share_path,
                                                share_passwd=share_passwd)
        if result.get("ok"):
            return result["url"]
        raise Exception(f"刷新下载链接失败: {result.get('msg')}")
    elif acc_type == "wps":
        return client.get_download_url(DEFAULT_GROUP_ID, file_id)
    else:
        result = client.get_download_url(file_id)
        if result.get("ok"):
            return result["url"]
        raise Exception(f"刷新下载链接失败: {result.get('msg')}")
def _resolve_parent_id(parent_id):
    """将虚拟 mount ID（_mount_xxx）解析为 WPS 文件夹 ID，并返回 (resolved_id, client)"""
    if not parent_id or not parent_id.startswith("_mount_"):
        return parent_id, None  # 普通 ID，client 稍后通过 get_wps_client 获取
    storage_id = parent_id[7:]
    cli, rid = _get_mount_client_and_root(parent_id)
    if cli:
        return rid or "", cli
    # fallback: 主客户端
    storages = _load_storages()
    for s in storages:
        if s["id"] == storage_id:
            mp = (s.get("mountPath", "") or "").strip().lstrip("/")
            if mp:
                resolved = _resolve_folder_id(mp)
                return resolved or "", None
    return parent_id, None
_task_status = {"running": False, "progress": 0, "total": 0, "current": "", "errors": []}
_scheduler_strm_running = set()  # 调度器正在运行的任务索引，允许多任务并行
def _collect_media_files(client, parent_id, extensions, rel_path=""):
    result = []
    try:
        files = client.get_all_files(DEFAULT_GROUP_ID, parent_id)
        for f in files:
            ftype = f.get("ftype") or "file"
            fid = f.get("id")
            fname = f.get("fname") or f.get("name") or ""
            if ftype == "folder":
                result.extend(_collect_media_files(client, fid, extensions, os.path.join(rel_path, fname.replace("（", "(").replace("）", ")"))))
            else:
                ext = os.path.splitext(fname)[1].lower()
                if ext in extensions:
                    result.append((f, rel_path.replace("（", "(").replace("）", ")")))
    except Exception as e:
        _task_status["errors"].append(f"扫描出错: {e}")
    return result
def format_size(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024: return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"
def get_mime(filename):
    ext = os.path.splitext(filename)[1].lower()
    mime_map = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
        ".webm": "video/webm", ".m4v": "video/mp4", ".ts": "video/mp2t",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    }
    return mime_map.get(ext, "application/octet-stream")
def _client_active_count(client_name):
    """统计某客户端当前 running 状态的任务数"""
    count = 0
    for tid in list(_CLIENT_QUEUE):
        t = _CLIENT_TASKS.get(tid)
        if t and t.get("status") == "running" and t.get("polled_by") == client_name:
            count += 1
    return count
REGEX_FILE = os.path.join(DATA_DIR, "regex_patterns.json")
DEFAULT_REGEX_PATTERNS = [
    {"id": "regex-auto", "name": "自动识别", "pattern": "", "desc": "留空则自动识别常见格式", "preset": True},
    {"id": "regex-ep", "name": "EP(数字)", "pattern": ".*EP(\\\\d+).*", "desc": "匹配 EP01, EP02 等格式", "preset": True},
    {"id": "regex-sxexx", "name": "SxxExx", "pattern": "S\\\\d+E(\\\\d+)", "desc": "匹配 S01E01 等格式", "preset": True},
    {"id": "regex-num-ext", "name": "数字.mp4/mkv", "pattern": "(\\\\d+)\\\\.(mp4|mkv)", "desc": "匹配 1.mkv, 13.mp4 等格式", "preset": True},
    {"id": "regex-e-num", "name": "E(数字)", "pattern": "E(\\\\d+)", "desc": "匹配 E01, E186 等格式", "preset": True},
    {"id": "regex-ch-ji", "name": "第X集", "pattern": "第(\\\\d+)集", "desc": "匹配中文 第1集 格式", "preset": True},
    {"id": "regex-ch-hua", "name": "第X话", "pattern": "第(\\\\d+)话", "desc": "匹配中文 第1话 格式", "preset": True},
    {"id": "regex-ch-hui", "name": "第X回", "pattern": "第(\\\\d+)回", "desc": "匹配中文 第1回 格式", "preset": True},
    {"id": "regex-ep-2160", "name": "EP数字 2160p", "pattern": ".*EP(\\\\d+).*2160.*", "desc": "匹配带2160p分辨率的EP文件", "preset": True},
]
def _load_regex():
    if os.path.exists(REGEX_FILE):
        try:
            with open(REGEX_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    # 首次加载：写入默认预设
    _save_regex(list(DEFAULT_REGEX_PATTERNS))
    return list(DEFAULT_REGEX_PATTERNS)
def _save_regex(patterns):
    with open(REGEX_FILE, "w", encoding="utf-8") as f:
        json.dump(patterns, f, ensure_ascii=False, indent=2)
CLOUD_CONFIG_FILE = os.path.join(DATA_DIR, "cloud_config.json")
def _load_cloud():
    if os.path.exists(CLOUD_CONFIG_FILE):
        try:
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []
def _save_cloud(configs):
    with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)
def _server_detect_cloud_type(share_url):
    """根据分享链接识别云盘类型（与客户端 transfer_engine.detect_cloud_type 一致）"""
    if not share_url:
        return ""
    if "pan.quark.cn" in share_url or "quark.cn" in share_url:
        return "quark"
    if "pan.baidu" in share_url or "baidu.com" in share_url:
        return "baidu"
    return "yidong"
def _check_client_cloud_support(client_name, share_url):
    """推送前置校验：目标客户端是否已上报支持该分享链接类型的云盘账号。

    返回 (ok, msg)：
      ok=False 且 msg 非空 -> 客户端缺少对应云盘类型，不应下发。
      客户端未上报 cloud_types（老版本）-> 跳过校验，返回 (True, "")。
    """
    if not client_name:
        return True, ""  # 广播任务，无法预校验
    info = _CLIENT_REGISTRY.get(client_name)
    if not info:
        return True, ""  # 客户端不在线，交由运行时报错
    ctypes = info.get("cloud_types")
    if not ctypes:
        return True, ""  # 老客户端未上报，兼容放行
    need = _server_detect_cloud_type(share_url)
    if need and need not in ctypes:
        return False, (
            f"客户端「{client_name}」未配置 {need} 类型的云盘账号（仅支持："
            f"{', '.join(sorted(ctypes))}）。请在客户端「{ '夸克网盘' if need=='quark' else need }」"
            f"卡片填写对应 Cookie 后保存，再重新运行。"
        )
    return True, ""
def _global_min_size_mb():
    """全局追更文件最小大小(MB)，来自个人资料设置；0=不过滤"""
    try:
        return float(load_config().get("chase_min_size_mb", 0) or 0)
    except (ValueError, TypeError):
        return 0

def _split_video_exts(raw):
    """把用户配置的视频格式白名单拆分成规范化扩展名列表。

    兼容多种分隔方式：逗号 / 中文顿号 / 斜杠 / 分号 / 空格，例如
    "mp4,mkv.avi"、"mp4/mkv/avi"、"mp4、mkv avi" 均可；
    自动补前导点、转小写、去重（如 "mp4" 与 ".mp4" 视为同一格式）。
    """
    if isinstance(raw, (list, tuple)):
        tokens = [str(x) for x in raw]
    else:
        tokens = re.split(r"[,，、;/；\s]+", str(raw or ""))
    exts = []
    for tk in tokens:
        tk = tk.strip().lower()
        if not tk:
            continue
        if not tk.startswith("."):
            tk = "." + tk
        if tk not in exts:
            exts.append(tk)
    return exts

def _global_video_exts():
    """追更识别的视频格式白名单（个人资料设置）；空/未配置时返回内置默认。
    只允许这些格式的文件参与集数识别/下载，彻底过滤 jpg/png/txt 等附件。"""
    _DEFAULT = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv", ".wmv",
                ".rmvb", ".mpg", ".mpeg", ".webm", ".iso", ".m2ts", ".m4v", ".3gp")
    try:
        raw = load_config().get("chase_video_exts", "")
    except Exception:
        return _DEFAULT
    exts = _split_video_exts(raw)
    return tuple(exts) if exts else _DEFAULT

def _global_video_exts_str():
    """下发到客户端的视频格式白名单（逗号分隔字符串）"""
    return ",".join(_global_video_exts())
_SVR_CHECK_RUNNING = {}  # chase_id → 启动时间戳(epoch秒)，服务端追更检查去重（带超时，防卡死残留）

def _create_chase_check_task(chase, manual=False):
    """触发一次追更检查（服务端后台执行，不再下发客户端检查任务）。
    manual=True: 手动"立即运行"，从 base_ep 开始重新查
    manual=False: 自动调度，只查 progress_ep 之后的新集
    """
    if not chase or not chase.get("id"):
        return None
    cid = chase["id"]
    cname = chase.get("name", "追更")
    # ── 去重（带超时）：同一追更已有检查在跑时不重复触发。
    # 网络探测(分享浏览/WPS目录)慢时检查线程可能持续数分钟；若无超时，
    # 一旦旧线程卡住未结束，_SVR_CHECK_RUNNING 残留 → 后续所有手动/自动
    # 检查全被挡掉 → 点「运行」毫无反应。超过 CHECK_TIMEOUT 视为卡死，
    # 允许重新触发（新线程接管，旧线程最终自行结束并清理）。
    _CHECK_TIMEOUT = 300  # 秒：正常检查应在此时间内完成
    _last = _SVR_CHECK_RUNNING.get(cid, 0)
    if _last and time.time() - _last < _CHECK_TIMEOUT:
        logger.info(f"追更检查[{cname}] 已有检查进行中({int(time.time()-_last)}s)，本次触发跳过")
        return None
    _SVR_CHECK_RUNNING[cid] = time.time()
    logger.info(f"追更检查[{cname}] 触发检查 manual={manual}")
    threading.Thread(
        target=_run_server_chase_check, args=(chase, manual), daemon=True).start()
    return None


def _run_server_chase_check(chase, manual=False):
    """服务端后台执行追更检查（替代客户端 chase-check 任务）：
    浏览分享 → 格式/大小过滤 → 识别集数 → WPS目录探测 →
    复用 _process_chase_result_locked 校准进度并派发下载任务。
    """
    cid = chase.get("id", "")
    cname = chase.get("name", "追更")
    _ck_ts = _SVR_CHECK_RUNNING.get(cid, 0)  # 记录本次检查的标记时间戳，结束时按值清理
    # ── 已停止的追更：即使检查在停止前已被触发，也立即退出，不再浏览/派发 ──
    if chase.get("stopped"):
        logger.info(f"追更检查[{cname}] 追更已停止，跳过检查")
        # 该 return 在 try 外，finally 不会执行，必须手动清理去重标记，防止残留阻塞后续检查
        if _SVR_CHECK_RUNNING.get(cid) == _ck_ts:
            _SVR_CHECK_RUNNING.pop(cid, None)
        return
    try:
        # ── 与下载任务一致的 WPS 目标路径，并持久化到追更任务 ──
        _wps_path0 = _persist_chase_wps_path(chase, cid, cname)

        share_url = chase.get("share_url", "")
        share_pwd = chase.get("share_passwd", "")
        sub_path = chase.get("sub_path", "").strip().rstrip("/")
        ep_pattern = chase.get("ep_pattern", "")
        category = chase.get("category", "国漫")
        is_movie = "电影" in category
        base_ep = chase.get("base_ep", 0) or 0
        prog = chase.get("progress_ep", 0) or 0
        check_from = base_ep if manual else max(base_ep, prog)

        # ── 1. 服务端浏览分享 ──
        files, err = _server_browse_chase_share(share_url, share_pwd, sub_path)
        if err:
            logger.info(f"追更检查[{cname}] 分享浏览失败: {err}")
            # 即使分享浏览失败，也探测 WPS 目录校准 wps_eps（WPS 实际文件快照）。
            # 否则 wps_eps 一直为空 → 前端「已传」退化 done_eps 虚高、
            # 漏集判定误把缺失集当已存在，点开始也收不到下载任务。
            _wp_probe = _server_probe_wps_dir(_wps_path0)
            _submit_server_chase_result(cid, {"episodes": [], "error": err,
                                              "wps_files": _wp_probe})
            return

        # ── 2. 格式 + 大小过滤（与客户端原检查逻辑一致）──
        video_exts = _global_video_exts()
        _video_only = [f for f in files
                       if (f.get("name", "") or "").lower().endswith(video_exts)]
        if len(_video_only) != len(files):
            logger.info(f"追更检查[{cname}] 格式过滤: {len(files)} → {len(_video_only)} 个文件")
        files = _video_only
        min_size_mb = _global_min_size_mb()
        if min_size_mb > 0:
            _min_bytes = int(min_size_mb * 1024 * 1024)
            _big_enough = [f for f in files
                           if not f.get("size") or f.get("size", 0) >= _min_bytes]
            if len(_big_enough) != len(files):
                logger.info(f"追更检查[{cname}] 大小过滤: {len(files)} → {len(_big_enough)} 个文件")
            files = _big_enough

        # ── 3. 识别集数（服务端，与客户端逻辑一致）──
        # 「指定集数」带季（如 S02E01-S02E08）时，分享内可能同时含 S01/S02
        # 同集号文件（如 S01E05 与 S02E05 都识别为 5）。此时季优先：
        # 保留季号匹配目标季的文件，避免大文件把目标季的同集号挤掉，
        # 否则后续按季筛选时反查季号全是 1，导致「无匹配集」不下发。
        _filter_season = 0
        _efs = (chase.get("episodes_filter") or "").strip()
        if _efs:
            try:
                _wm = parse_episodes_filter(_efs)
                for _s in _wm:
                    if _s > 0:
                        _filter_season = _s
                        break
            except Exception:
                pass
        all_eps = {}
        _ep_sizes = {}
        _ep_season = {}
        for f in files:
            fname = f.get("name", "")
            ep_num, _ = _server_extract_episode(fname, ep_pattern, is_movie=is_movie)
            if ep_num is None:
                continue
            key = str(ep_num)
            fseason = 0
            if _filter_season:
                _m = re.search(r'[Ss](\d{1,2})[Ee]\d+', fname)
                if not _m:
                    _m = re.search(r'[Ss](\d{1,2})', fname)
                fseason = int(_m.group(1)) if _m else 0
                cur_season = _ep_season.get(key, 0)
                if fseason == _filter_season:
                    # 目标季文件：无论 size 大小都优先（覆盖其他季）
                    if cur_season != _filter_season or f.get("size", 0) > _ep_sizes.get(key, 0):
                        all_eps[key] = fname
                        _ep_sizes[key] = f.get("size", 0)
                        _ep_season[key] = _filter_season
                    continue
                # 非目标季：已有目标季文件时跳过，否则按 size 兜底竞争
                if cur_season == _filter_season:
                    continue
                if key not in all_eps or f.get("size", 0) > _ep_sizes.get(key, 0):
                    all_eps[key] = fname
                    _ep_sizes[key] = f.get("size", 0)
                    _ep_season[key] = fseason
                continue
            # 无季筛选：同集号保留 size 大的（原逻辑）
            if key not in all_eps or f.get("size", 0) > _ep_sizes.get(key, 0):
                all_eps[key] = fname
                _ep_sizes[key] = f.get("size", 0)

        if is_movie:
            movie_files = []
            idx = 0
            for f in files:
                fname = f.get("name", "")
                if fname.lower().endswith(video_exts):
                    neg = -(idx + 1)
                    all_eps[str(neg)] = fname
                    movie_files.append(fname)
                    idx += 1
            new = list(range(-1, -(idx + 1), -1))
            logger.info(f"追更检查[{cname}](电影): 共{len(files)}个文件, 发现{len(movie_files)}个电影文件")
            _submit_server_chase_result(cid, {
                "episodes": new, "siblings": all_eps,
                "movie_files": movie_files, "is_movie": True})
        else:
            new = sorted({int(k) for k in all_eps if _is_ep_number(k, check_from)})
            logger.info(f"追更检查[{cname}]: 共{len(files)}个文件, 发现{len(new)}个新集数: {new[:10]}")
            wps_files = _server_probe_wps_dir(_wps_path0)
            logger.info(f"追更检查[{cname}]: WPS目录探测: {_wps_path0} 共{len(wps_files)}个文件")
            # ── 自动查缺失补档：WPS 实际缺失、但分享源存在的集并入本次派发 ──
            # 覆盖"WPS 没有但分享链接里有"的漏传/缺集场景（如历史 done_eps 虚高
            # 导致 5-30 被误判已存在）。以 WPS 实际文件快照为准，不信任 done_eps。
            if wps_files:
                _miss = _auto_backfill_missing(cid, all_eps, wps_files,
                                               ep_pattern, is_movie)
                if _miss:
                    _miss_sorted = sorted(_miss)
                    new = sorted(set(new) | _miss)
                    logger.info(f"追更检查[{cname}]: 查缺失补档新增 {len(_miss)} 个缺失集"
                                f"(分享有/WPS无): {_miss_sorted[:10]}，本次派发 {len(new)} 集")
            _submit_server_chase_result(cid, {
                "episodes": new, "siblings": all_eps, "wps_files": wps_files})
    except Exception as e:
        logger.info(f"追更检查[{cname}] 异常: {e}")
        try:
            _submit_server_chase_result(cid, {"episodes": [], "error": str(e)})
        except Exception:
            pass
    finally:
        # 仅当仍标记为自己发起的时间戳才清理，避免误删超时后重新触发的新检查标记
        if _SVR_CHECK_RUNNING.get(cid) == _ck_ts:
            _SVR_CHECK_RUNNING.pop(cid, None)
        logger.info(f"追更检查[{cname}] 检查结束")


def _is_ep_number(k, check_from):
    """判断分享内集号是否为有效的新集数（>check_from，且排除年份等误判）。"""
    try:
        ep = int(k)
    except (ValueError, TypeError):
        return False
    if ep <= check_from or (1900 <= ep <= 2100):
        return False
    return True


def _auto_backfill_missing(cid, all_eps, wps_files, ep_pattern="", is_movie=False):
    """自动查缺失补档：以 WPS 实际文件快照为准，找出「分享源有、WPS 无」的集号。

    同时（与手动「查缺失」接口一致）在锁内把追更任务的 wps_eps/done_eps 校准为
    WPS 实际快照——否则 done_eps 虚高（上传路径异常/文件被移走，记录在案但 WPS
    实际无文件）会在 _process_chase_result_locked 的并集判定(wps_eps|done_eps)中
    永久挡住缺失集补追（表现为"缺失集不派发，只派最后一集"）。

    wps_files 为空（探测失败或目录真空）时返回空集，避免把全部集误判为缺失全推。
    """
    if not wps_files:
        return set()
    present = set()
    for fn in wps_files:
        ep, _ = _server_extract_episode(fn, ep_pattern, is_movie=is_movie)
        if ep is not None:
            try:
                present.add(int(ep))
            except (ValueError, TypeError):
                pass
    # 校准任务快照（锁内写回）
    if cid:
        try:
            with _CHASE_FILE_LOCK:
                _tasks2 = _load_chase()
                for _t2 in _tasks2:
                    if _t2.get("id") == cid:
                        _t2["wps_eps"] = sorted(present)
                        _de = set(int(x) for x in (_t2.get("done_eps") or []))
                        _de &= present
                        _t2["done_eps"] = sorted(_de)
                        _save_chase(_tasks2)
                        break
        except Exception:
            pass
    missing = set()
    for k in all_eps:
        try:
            ep = int(k)
        except (ValueError, TypeError):
            continue
        if ep <= 0 or (1900 <= ep <= 2100):  # 电影负号/年份误判排除
            continue
        if ep not in present:
            missing.add(ep)
    return missing


def _persist_chase_wps_path(chase, cid, cname):
    """计算追更的 WPS 目标路径并持久化到追更任务，返回该路径。"""
    _season = chase.get("season", 0)
    _ep_pattern0 = chase.get("ep_pattern", "")
    if _ep_pattern0:
        _sp = re.search(r'[Ss](\d+)', _ep_pattern0)
        if _sp:
            try:
                _season = int(_sp.group(1))
            except Exception:
                pass
    _show_folder0 = chase.get("show_folder", cname)
    _category0 = chase.get("category", "国漫")
    _base_path0 = f"{_category0}/{_show_folder0}" if _show_folder0 else _category0
    _wps_path0 = f"{_base_path0}/Season {_season}" if _season > 0 else _base_path0
    try:
        with _CHASE_FILE_LOCK:
            _cts2 = _load_chase()
            for _tt in _cts2:
                if _tt.get("id") == cid:
                    _tt["wps_path"] = _wps_path0
                    break
            _save_chase(_cts2)
    except Exception:
        pass
    return _wps_path0


def _server_browse_chase_share(share_url, share_pwd, sub_path):
    """服务端浏览分享目录，返回 (files, err)。
    网页版优先，失败回退已配置云盘账号方式（与预览接口一致）。"""
    if not share_url:
        return [], "缺少分享链接"
    try:
        result = _browse_share_recursive(share_url, share_pwd, "root", sub_path)
        if result.get("ok") and result.get("items"):
            return result["items"], ""
        if "pan.quark.cn" in share_url or "quark.cn" in share_url:
            from .quark_client import QuarkClient
            accounts = _get_all_quark_accounts()
            for acc in accounts:
                client = QuarkClient(acc.get("cookie", ""))
                result = _browse_share_recursive(
                    share_url, share_pwd, "root", sub_path,
                    lambda u, p, pid: client.list_share_files(u, p, pid))
                if result.get("ok") and result.get("items"):
                    return result["items"], ""
            return [], result.get("msg", "分享链接解析失败") + (
                "（无可用云盘账号）" if not accounts else "（账号方式也失败）")
        from .cloud_clients import YidongClient
        accounts = _get_all_yidong_accounts()
        for acc in accounts:
            client = YidongClient(acc.get("cookie", ""))
            result = _browse_share_recursive(
                share_url, share_pwd, "root", sub_path,
                lambda u, p, pid: client.list_share_files(u, p, pid))
            if result.get("ok") and result.get("items"):
                return result["items"], ""
        return [], result.get("msg", "分享链接解析失败") + (
            "（无可用云盘账号）" if not accounts else "（账号方式也失败）")
    except Exception as e:
        return [], str(e)


def _server_probe_wps_dir(wps_path):
    """服务端探测 WPS 目标目录下的视频文件名（供漏集自愈），失败返回空列表。"""
    if not wps_path:
        return []
    try:
        cli, err = get_wps_client()
        if err or not cli:
            return []
        folder_id = _resolve_folder_id_with_client(cli, wps_path)
        if not folder_id:
            return []
        video_exts = _global_video_exts()
        out = []
        for f in cli.get_all_files(DEFAULT_GROUP_ID, folder_id) or []:
            if f.get("ftype") == "folder":
                continue
            fn = (f.get("fname") or f.get("name") or "").strip()
            if fn and fn.lower().endswith(video_exts):
                out.append(fn)
        return out
    except Exception:
        return []


def _submit_server_chase_result(chase_id, data):
    """锁内复用 _process_chase_result_locked 处理服务端检查结果并派发下载任务。"""
    try:
        from .routes_client import _process_chase_result_locked
    except Exception as e:
        logger.info(f"追更检查结果处理模块加载失败: {e}")
        return
    episodes = data.get("episodes") or []
    siblings = data.get("siblings") or {}
    error = data.get("error") or ""
    try:
        with _CHASE_FILE_LOCK:
            _process_chase_result_locked(data, chase_id, episodes, error, siblings)
    except Exception as e:
        logger.info(f"追更检查结果处理失败: {e}")
def _unified_scheduler():
    """统一后台调度：追更 + STRM，每分钟检查一次各自的时间表

    追更调度支持两种格式：
      - DAILY:HH:MM  每天固定时刻检查（默认 DAILY:01:00）
      - HOURS:N      每 N 小时检查一次（如 HOURS:12）
    错过补偿：任务从未检查过(新建/重启后首次)立即触发；距上次检查超过
    周期(DAILY 取 1.5 倍周期)自动补检，避免服务停机/重启导致漏检。
    """
    _chase_catchup_ts = {}  # chase_id -> 补偿触发时间戳，防止补偿重复下发
    while True:
        try:
            now = _beijing_now()
            current_hhmm = now.strftime("%H:%M")
            now_ts = time.time()

            # ── 追更任务 ──
            chase_tasks = _load_chase()
            for t in chase_tasks:
                if not t.get("enabled") or not t.get("share_url"):
                    continue
                if t.get("stopped"):  # 已完全停止的追更不再自动调度
                    continue
                sched = t.get("schedule", "DAILY:01:00")  # 默认凌晨1点
                period_sec = 24 * 3600
                is_time_match = False
                if sched.startswith("DAILY:"):
                    target = sched[6:]  # e.g. "07:00"
                    is_time_match = (target == current_hhmm)
                    catchup_sec = int(period_sec * 1.5)  # 36h 未检查视为错过
                elif sched.startswith("WEEKLY:"):
                    # WEEKLY:MON:01:00 每周固定时刻
                    parts = sched.split(":")
                    target = (parts[2] + ":" + parts[3]) if len(parts) >= 4 else sched[7:]
                    week_day = (parts[1] if len(parts) >= 2 else "").upper()
                    is_time_match = (target == current_hhmm and
                                     now.strftime("%A").upper()[:3] == week_day)
                    catchup_sec = 8 * 24 * 3600  # 超8天未检查视为错过
                elif sched.startswith("HOURS:"):
                    try:
                        period_sec = max(1, int(sched[6:].strip() or "0")) * 3600
                    except Exception:
                        period_sec = 24 * 3600
                    catchup_sec = period_sec  # 超过一个完整间隔即补检
                else:
                    continue
                # 避免重复：1分钟内不重复检查
                last = t.get("last_check", "")
                last_ts = 0
                if last:
                    try:
                        lt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                        last_ts = (now - lt).total_seconds()
                        if last_ts < 60:
                            continue
                    except Exception:
                        pass
                # ── 错过补偿：从未检查立即首检 / 超过周期未检查补检 ──
                # 失败任务会更新 last_check 并走 ★2 指数退避重试，不会被此规则
                # 高频轰炸；此规则只兜底"停机错过 / 新建后未到计划时刻"的场景。
                _need_catchup = (not last) or (last_ts >= catchup_sec)
                if _need_catchup:
                    cid = t.get("id")
                    if now_ts - _chase_catchup_ts.get(cid, 0) >= 600:  # 10分钟去重
                        _chase_catchup_ts[cid] = now_ts
                        _create_chase_check_task(t)
                    continue
                if is_time_match:
                    _create_chase_check_task(t)

            # ── STRM 任务 ──
            strm_tasks = _load_strm_tasks()
            for idx, t in enumerate(strm_tasks):
                if not t.get("enabled"):
                    continue
                sched = t.get("schedule", "")
                if not sched.startswith("DAILY:"):
                    continue
                target = sched[6:]
                if target != current_hhmm:
                    continue
                if idx in _scheduler_strm_running:
                    continue
                last = t.get("last_run", "")
                if last:
                    try:
                        lt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                        if (now - lt).total_seconds() < 60:
                            continue
                    except Exception:
                        pass
                # 后台触发 STRM 任务
                _trigger_strm_task(t, idx)

            # 每分钟检查一次
            time.sleep(60 - now.second)
        except Exception:
            time.sleep(30)
def _trigger_strm_for_chase(chase_task):
    """
    追更批次完成后，自动触发对应分类的 STRM 任务。
    chase_task: 追更卡片数据（含 category 字段）
    匹配规则：
      - STRM 任务未设置分类(兼容旧数据) → 通配，触发
      - STRM 任务分类与追更分类一致 → 触发
      - 追更无分类 → 触发所有启用的 STRM 任务
    """
    category = (chase_task.get("category") or "").strip()
    chase_name = chase_task.get("name", "")

    strm_tasks = _load_strm_tasks()
    triggered = 0
    for idx, st in enumerate(strm_tasks):
        if not st.get("enabled", True):
            continue
        st_category = (st.get("category") or "").strip()

        # 匹配策略：
        # - 追更无分类：触发所有启用的 STRM 任务
        # - STRM 未设分类（旧数据）：视为通配，触发
        # - 否则：STRM 分类与追更分类精确一致才触发
        match = False
        if not category:
            match = True
        elif not st_category:
            match = True
        elif st_category == category:
            match = True

        if match:
            _trigger_strm_task(st, idx)
            triggered += 1

    if triggered > 0:
        print(f"[追更→STRM] 「{chase_name}」({category}) 追更完成，已触发 {triggered} 个 STRM 任务")
def _trigger_strm_task(t, idx):
    """后台触发 STRM 任务（无 request context）"""
    cfg = load_config()
    output_dir = t.get("output_dir") or os.path.join(DATA_DIR, "strm_output")
    extensions = cfg.get("strm_extensions",
        [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".iso", ".rmvb", ".webm"])
    source_id = t.get("source_id") or None
    raw_name = (t.get("name") or "task").replace("（", "(").replace("）", ")")
    # 仅过滤 Windows 文件名非法字符，其余（含 = + : , 等元数据符号）原样保留，
    # 避免把 [tmdbid=251600] 里的 '=' 等字符误删
    _ILLEGAL = set('\\/:*?"<>|\t\n\r')
    def _safe_name(s):
        s = (s or "").replace("（", "(").replace("）", ")")
        s = "".join(c for c in s if c not in _ILLEGAL)
        return s.strip().rstrip(".")  # 去掉首尾空格、末尾点（Windows 不允许）
    task_name = _safe_name(raw_name)
    strm_base = cfg.get("strm_base_url", "").strip()
    url_prefix = "/r" if cfg.get("strm_redirect_mode") else "/stream"

    os.makedirs(output_dir, exist_ok=True)
    _scheduler_strm_running.add(idx)
    status = {"running": True, "progress": 0, "total": 0, "current": "扫描中...", "errors": []}

    def run():
        client, err = get_wps_client()
        if err:
            status["errors"].append(str(err))
            status["running"] = False
            _scheduler_strm_running.discard(idx)
            return
        try:
            media_files = _collect_media_files(client, source_id, extensions)
            status["total"] = len(media_files)
            for i, (f, rel_path) in enumerate(media_files):
                status["progress"] = i
                fname = f.get("fname") or f.get("name") or "unnamed"
                fid = f.get("id")
                status["current"] = fname
                try:
                    strm_url = f"{strm_base}{url_prefix}/{fid}"
                    fname_fixed = fname.replace("（", "(").replace("）", ")")
                    # 仅过滤 Windows 文件名非法字符，保留 = + : , [tmdbid=xxx] 等元数据符号
                    safe = _safe_name(fname_fixed)
                    dl_dir = os.path.join(output_dir, task_name, rel_path)
                    os.makedirs(dl_dir, exist_ok=True)
                    sp = os.path.join(dl_dir, os.path.splitext(safe)[0] + ".strm")
                    if cfg.get("strm_incremental", True) and os.path.exists(sp):
                        continue
                    with open(sp, "w", encoding="utf-8") as sf:
                        sf.write(strm_url)
                except Exception as e:
                    status["errors"].append(f"{fname}: {e}")
            status["progress"] = len(media_files)
        except Exception as e:
            status["errors"].append(str(e))
        finally:
            status["running"] = False
            _scheduler_strm_running.discard(idx)
        # 更新 last_run
        tasks2 = _load_strm_tasks()
        if idx < len(tasks2):
            tasks2[idx]["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_strm_tasks(tasks2)

    threading.Thread(target=run, daemon=True).start()


def parse_episodes_filter(filter_str):
    """解析「指定集数」字符串 → {season: set(ep)}。

    返回 dict：season 为 0 表示「不分季（任意季）」，大于 0 表示指定季。
    支持格式（逗号/分号/空白分隔，可混合）：
      "1-8"                → {0: {1..8}}
      "1,2,5-8"            → {0: {1,2,5..8}}
      "S02E01-S02E08"      → {2: {1..8}}
      "S02E05,S02E07"      → {2: {5,7}}
      "S01E02,S02E05-S02E08" → {1: {2}, 2: {5..8}}
    """
    result = {}
    if not filter_str:
        return result
    for part in re.split(r'[,;\s]+', filter_str):
        part = part.strip()
        if not part:
            continue
        # 带季范围：S02E01-S02E08 / S02E01-E08 / S02E01-08
        m = re.match(r'^[Ss](\d+)[Ee](\d+)-[Ss]?(\d+)?[Ee]?(\d+)$', part)
        if m:
            s1, e1 = int(m.group(1)), int(m.group(2))
            s2 = int(m.group(3)) if m.group(3) else s1
            e2 = int(m.group(4))
            if s1 != s2:
                continue  # 跨季范围（如 S01E02-S02E05）不支持，忽略
            result.setdefault(s1, set()).update(range(e1, e2 + 1))
            continue
        # 带季单集：S02E05
        m = re.match(r'^[Ss](\d+)[Ee](\d+)$', part)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            result.setdefault(s, set()).add(e)
            continue
        # 纯集数范围：5-8
        m = re.match(r'^(\d+)-(\d+)$', part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            result.setdefault(0, set()).update(range(a, b + 1))
            continue
        # 纯集数：3
        if part.isdigit():
            result.setdefault(0, set()).add(int(part))
    return result


def _build_chase_file_info(chase, ep_num, siblings=None, manual=None):
    """构建单个剧集文件的传输信息（单集任务与批量任务共用）。
    返回 {ep_num, file_name, rename_to, wps_path, category, show_folder,
          is_movie, manual_ep, skip, name}。"""
    if siblings is None:
        siblings = {}
    if manual is None:
        manual = {}
    template = chase.get("rename_template", chase["name"] + "E{ep}")
    season = chase.get("season", 0)
    ep_pattern = chase.get("ep_pattern", "")
    # ① 优先从 ep_pattern 中提取季别（如 S02E(\d+) → 2，最权威）
    if ep_pattern:
        sp = re.search(r'[Ss](\d+)', ep_pattern)
        if sp:
            try:
                season = int(sp.group(1))
            except Exception:
                pass
    # ② 其次从文件名中提取季别（精确匹配 SxxExx，避免文件名中其他 Sxx 干扰）
    file_name = siblings.get(str(ep_num), "")
    season_from_file = re.search(r'[Ss](\d{2})E\d+', file_name)
    if not season_from_file:
        season_from_file = re.search(r'[Ss](\d{2})', file_name)
    if season_from_file:
        season = int(season_from_file.group(1))
    else:
        # ③ 文件无季标记（如 "01.mp4"/"第01集.mp4"）：
        #    优先取「指定集数」中的唯一季（如 S02E01-S02E08 → 2）；
        #    否则默认 S01 —— 与预览 test_pattern/preview_files 保持一致，
        #    避免模板渲染出 S00、WPS 落到根目录，与同季其他文件（Season 1）不一致。
        try:
            _ef = (chase.get("episodes_filter") or "").strip()
            if _ef:
                _wm = parse_episodes_filter(_ef)
                _slist = sorted(k for k in _wm if k > 0)
                if len(_slist) == 1:
                    season = _slist[0]
        except Exception:
            pass
        if season <= 0:
            season = 1
    tmdb_id = chase.get("tmdb_id", "")
    # 处理 {name}、{season}、{tmdb_id} 占位符
    year = chase.get("year", "")
    full_name = chase.get("name", "") + (" (" + year + ")" if year else "")
    template = template.replace("{name}", full_name)
    def _season_repl(m):
        pad = int(m.group(1)) if m.group(1) else 0
        return str(season).zfill(pad) if pad else str(season)
    template = re.sub(r'\{season(?::(\d+)d)?\}', _season_repl, template)
    template = re.sub(r'\{s(?::(\d+)d)?\}', _season_repl, template)
    # {tmdb_id} / {tmdb} 替换；未显式放置则自动追加 (Jellyfin 格式)
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
        template = template.replace("{tmdb-{tmdb_id}}", "")
        template = template.replace("{tmdb}", "")
    try:
        rename_to = template.format(ep=ep_num)
    except (ValueError, KeyError):
        rename_to = template.replace("{ep}", str(ep_num))
    # 确定目标扩展名（压缩包内是视频，应使用视频扩展名）
    ARTICLE_EXT = {'.tar', '.rar', '.7z', '.zip', '.iso', '.cab', '.arj', '.lzh', '.lha'}
    ext = os.path.splitext(file_name)[1].lower()
    has_vext = rename_to.rstrip('.').lower().endswith(
        ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
         '.m4v', '.mpg', '.mpeg', '.3gp', '.ts', '.rmvb', '.vob',
         '.divx', '.xvid', '.asf', '.f4v'))
    if not has_vext:
        rename_to += '.mkv' if ext in ARTICLE_EXT else ext
    # 手动重命名（预览中可单独编辑的名字）优先于模板生成
    manual_renames = chase.get("manual_renames") or {}
    if manual_renames.get(file_name):
        rename_to = manual_renames[file_name]

    show_folder = chase.get("show_folder", chase["name"])
    category = chase.get("category", "国漫")
    base_path = f"{category}/{show_folder}" if show_folder else category
    # WPS 目标路径：季数>0 时带 Season N 子目录
    wps_path = f"{base_path}/Season {season}" if season > 0 else base_path

    return {
        "ep_num": ep_num,
        "file_name": siblings.get(str(ep_num), ""),
        "rename_to": rename_to,
        "wps_path": wps_path,
        "category": category,
        "show_folder": show_folder,
        "is_movie": "电影" in category,
        "manual_ep": manual.get("ep") if manual.get("ep") is not None else None,
        "skip": bool(manual.get("skip", False)),
        "name": rename_to,
    }


def _record_chase_downloaded_ep(chase_id, ep_num):
    """把某一集记为「已派发(已下载)」：写入下载批次计数器与持久化的 downloaded_eps"""
    with _chase_batch_lock:
        _chase_batch_pending.setdefault(chase_id, set()).add(int(ep_num))
    try:
        with _CHASE_FILE_LOCK:
            _cts = _load_chase()
            for _t in _cts:
                if _t.get("id") == chase_id:
                    _dl = _t.setdefault("downloaded_eps", [])
                    if int(ep_num) not in _dl:
                        _dl.append(int(ep_num))
                        _dl.sort()
                    break
            _save_chase(_cts)
    except Exception:
        pass


def _create_chase_download_task(chase, ep_num, siblings=None, manual=None, run_after=None):
    """为某一集创建客户端下载任务
    manual: 来自 manual_episodes 的条目 {ep, skip}，skip=True 时本函数仍会创建任务，
            但在下发前由调用方过滤；此处仅把信息带入任务供客户端参考。
    run_after: 可选时间戳（秒），任务在此时间之后才允许被派发，用于失败重试的指数退避。
    """
    if siblings is None:
        siblings = {}
    if manual is None:
        manual = {}
    tid = uuid.uuid4().hex[:12]
    fi = _build_chase_file_info(chase, ep_num, siblings, manual)
    task = {
        "id": tid,
        "type": "chase-download",
        "chase_id": chase["id"],
        "name": fi["name"],
        "share_url": chase["share_url"],
        "share_passwd": chase.get("share_passwd", ""),
        "file_name": fi["file_name"],
        "rename_to": fi["rename_to"],
        "wps_path": fi["wps_path"],
        "category": fi["category"],
        "is_movie": fi["is_movie"],
        "show_folder": fi["show_folder"],
        "ep_num": fi["ep_num"],
        "manual_ep": fi["manual_ep"],
        "skip": fi["skip"],
        "target_client": chase.get("target_client", ""),
        "sub_path": chase.get("sub_path", ""),
        "episodes_filter": chase.get("episodes_filter", ""),
        "min_size_mb": _global_min_size_mb(),
        "video_exts": _global_video_exts_str(),
        "status": "pending",
        "warn": "",
        "created_at": time.time(),
        "run_after": run_after or 0,  # 延迟派发时间戳（指数退避用）
    }
    _CLIENT_TASKS[tid] = task
    _CLIENT_QUEUE.append(tid)
    _record_chase_downloaded_ep(chase["id"], ep_num)


def _create_chase_download_batch_tasks(chase, items, run_after=None):
    """为同一追更的一批剧集创建下载任务，实现「上传本集时并行下载下一集」的流水线。

    items: 可迭代的 (ep_num, siblings, manual) 三元组，顺序即下载顺序。
    按 WPS 目标路径连续分组：组内 >1 集时打包为一个批量任务（batch=True，
    客户端用批量流水线执行：下载1→上传1的同时下载2→……，每集完成单独上报）；
    单集或跨路径仍创建单集任务，保持原有行为与兼容性。
    """
    groups = []  # [(wps_path, [(ep_num, siblings, manual, fi)])]
    for ep_num, siblings, manual in items:
        fi = _build_chase_file_info(chase, ep_num, siblings, manual)
        if groups and groups[-1][0] == fi["wps_path"]:
            groups[-1][1].append((ep_num, siblings, manual, fi))
        else:
            groups.append((fi["wps_path"], [(ep_num, siblings, manual, fi)]))
    created = 0
    for _wps_path, g in groups:
        if len(g) == 1:
            ep_num, siblings, manual, _fi = g[0]
            _create_chase_download_task(chase, ep_num, siblings, manual, run_after)
            created += 1
            continue
        tid = uuid.uuid4().hex[:12]
        first = g[0][3]
        task = {
            "id": tid,
            "type": "chase-download",
            "chase_id": chase["id"],
            "batch": True,
            "name": f"{first['name']} 等{len(g)}集",
            "share_url": chase["share_url"],
            "share_passwd": chase.get("share_passwd", ""),
            "files": [f[3] for f in g],
            # 兼容字段：旧客户端不识别 batch 时，按单集任务处理首集
            "file_name": first["file_name"],
            "rename_to": first["rename_to"],
            "wps_path": first["wps_path"],
            "category": first["category"],
            "is_movie": first["is_movie"],
            "show_folder": first["show_folder"],
            "ep_num": first["ep_num"],
            "manual_ep": first["manual_ep"],
            "skip": first["skip"],
            "target_client": chase.get("target_client", ""),
            "sub_path": chase.get("sub_path", ""),
            "episodes_filter": chase.get("episodes_filter", ""),
            "min_size_mb": _global_min_size_mb(),
            "video_exts": _global_video_exts_str(),
            "status": "pending",
            "warn": "",
            "created_at": time.time(),
            "run_after": run_after or 0,
        }
        _CLIENT_TASKS[tid] = task
        _CLIENT_QUEUE.append(tid)
        for _ep_num, _sib, _man, _fi in g:
            _record_chase_downloaded_ep(chase["id"], _ep_num)
        created += 1
    return created


def _get_all_yidong_accounts():
    """获取所有可用的139云盘(yidong)账号，合并云盘账号和云盘配置两个数据源，主账号优先"""
    accounts = []
    seen = set()

    # 来源1：云盘账号管理 (type: "yidong")
    for acc in _load_cloud_accounts():
        if acc.get("type") == "yidong" and acc.get("cookie"):
            aid = acc.get("id", "")
            if aid and aid not in seen:
                accounts.append(acc)
                seen.add(aid)

    # 来源2：云盘配置管理 (type: "139" 即移动云盘)
    for cfg in _load_cloud():
        if (cfg.get("type") == "139" or cfg.get("type") == "yidong") and (
                cfg.get("cookie") or cfg.get("token")):
            cid = cfg.get("id", "")
            if cid and cid not in seen:
                accounts.append({
                    "id": cid,
                    "name": cfg.get("name", f"云盘账号-{cid[:8]}"),
                    "type": "yidong",
                    "cookie": (lambda s: s[6:] if s.startswith("Basic ") else s)(cfg.get("cookie") or cfg.get("token", "")),
                    "is_primary": cfg.get("is_primary", False),
                })
                seen.add(cid)

    # 主账号优先
    primary = next((a for a in accounts if a.get("is_primary")), None)
    if primary:
        accounts = [primary] + [a for a in accounts if a.get("id") != primary.get("id")]

    return accounts
def _get_all_quark_accounts():
    """获取所有可用的夸克网盘(quark)账号，合并云盘账号和云盘配置两个数据源，主账号优先"""
    accounts = []
    seen = set()

    # 来源1：云盘账号管理 (type: "quark")
    for acc in _load_cloud_accounts():
        if acc.get("type") == "quark" and acc.get("cookie"):
            aid = acc.get("id", "")
            if aid and aid not in seen:
                accounts.append(acc)
                seen.add(aid)

    # 来源2：云盘配置管理 (type: "quark")
    for cfg in _load_cloud():
        if (cfg.get("type") == "quark") and (cfg.get("cookie") or cfg.get("token")):
            cid = cfg.get("id", "")
            if cid and cid not in seen:
                accounts.append({
                    "id": cid,
                    "name": cfg.get("name", f"夸克账号-{cid[:8]}"),
                    "type": "quark",
                    "cookie": cfg.get("cookie") or cfg.get("token", ""),
                    "is_primary": cfg.get("is_primary", False),
                })
                seen.add(cid)

    # 主账号优先
    primary = next((a for a in accounts if a.get("is_primary")), None)
    if primary:
        accounts = [primary] + [a for a in accounts if a.get("id") != primary.get("id")]

    return accounts
def _browse_share_recursive(share_url, share_passwd, parent_id="root", sub_path="", browse_func=None):
    """递归浏览分享目录，支持网页版和账号版，支持子目录过滤
    browse_func(url, passwd, parent_id) -> dict {ok, items[]}
    """
    if browse_func is None:
        if "pan.quark.cn" in share_url or "quark.cn" in share_url:
            from .quark_client import QuarkClient
            browse_func = lambda u, p, pid: QuarkClient("").list_share_files(u, p, pid)
        else:
            from .cloud_clients import YidongClient
            browse_func = lambda u, p, pid: YidongClient.browse_share_web(u, p, pid)

    result = browse_func(share_url, share_passwd, parent_id)
    if not result.get("ok"):
        return result

    all_items = []
    folders = []
    # 夸克分享中单文件可能被误标 is_dir=True，需结合扩展名判断真实文件
    video_exts = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv", ".wmv",
                  ".rmvb", ".mpg", ".mpeg", ".webm", ".iso", ".m2ts")
    for item in result.get("items", []):
        name = (item.get("name") or "").lower()
        is_video_file = name.endswith(video_exts)
        if item.get("is_dir") and not is_video_file:
            folders.append(item)
        else:
            # 如果指定了子目录过滤，只保留匹配路径的文件
            item_path = item.get("path", "")
            if sub_path and sub_path.strip():
                normalized_sub = sub_path.strip().strip("/")
                normalized_item = item_path.strip().strip("/")
                # 文件路径必须位于子目录下（以 sub_path/ 开头或正好等于 sub_path）
                if normalized_item.startswith(normalized_sub + "/") or normalized_item == normalized_sub:
                    all_items.append(item)
                # 不保留根目录文件（避免混入其他目录的文件）
            else:
                all_items.append(item)

    for folder in folders:
        sub_result = _browse_share_recursive(
            share_url, share_passwd, folder["id"], sub_path, browse_func
        )
        if sub_result.get("ok"):
            all_items.extend(sub_result.get("items", []))

    return {"ok": True, "items": all_items, "count": len(all_items)}
def _maybe_rescan_chase(chase):
    """下载完成一集后，若可能还有更新则立即续扫（追到最新为止）。
    带冷却，防止分享未更新时无限空扫。
    """
    cid = chase.get("id")
    if not cid:
        return
    if chase.get("stopped"):  # 已完全停止的追更不自动续扫，避免停止后死循环派发
        return
    now = time.time()
    last = chase.get("_auto_rescan_at", 0)
    cooldown = 120  # 两次自动续扫最小间隔（秒）
    if now - last < cooldown:
        return
    # 标记冷却时间，避免同一批并发下载多次触发
    chase["_auto_rescan_at"] = now
    _create_chase_check_task(chase, manual=False)

# 显式导出：helpers 通过 `from .helpers import *` 被多个蓝图模块引入，
# 而 `import *` 默认不会导入下划线开头的名字，故在此统一列出。
__all__ = [
    "_beijing_now",
    "_load_storages",
    "_save_storages",
    "_resolve_folder_id",
    "_load_cloud_accounts",
    "_save_cloud_accounts",
    "_get_cloud_account_client",
    "_refresh_cdn_url",
    "_resolve_parent_id",
    "_collect_media_files",
    "_client_active_count",
    "_load_regex",
    "_save_regex",
    "_load_cloud",
    "_save_cloud",
    "_server_detect_cloud_type",
    "_check_client_cloud_support",
    "_global_min_size_mb",
    "_global_video_exts",
    "_global_video_exts_str",
    "_split_video_exts",
    "_create_chase_check_task",
    "_SVR_CHECK_RUNNING",
    "_unified_scheduler",
    "_trigger_strm_for_chase",
    "_trigger_strm_task",
    "_create_chase_download_task",
    "_create_chase_download_batch_tasks",
    "_get_all_yidong_accounts",
    "_get_all_quark_accounts",
    "_browse_share_recursive",
    "_maybe_rescan_chase",
    "parse_episodes_filter",
]
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
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
