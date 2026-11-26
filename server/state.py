# -*- coding: utf-8 -*-
"""
server/state.py
────────────────────────────────────────────────────────────────────────────
全局状态容器 + 持久化函数 + 上传进度类。

把原本散落在 app.py 各处的全局可变状态（客户端注册表、任务队列、预览结果、
追更批处理、上传进度等）以及文件持久化逻辑集中到本模块，便于：
  - 统一维护全局状态，避免循环依赖
  - 各路由/蓝图模块通过 `from server.state import ...` 使用
  - 更新时快速定位"状态相关"问题（不混入路由逻辑）

注意：本模块只放"状态 + 存读"，不放任何 Flask 路由。
所有实现的字段/签名严格与原 app.py 保持一致，确保行为不变。
"""
import os
import json
import threading
import uuid

# ── 基础路径（与原 app.py 顶部常量保持一致） ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "config", "config.json"))
COOKIE_FILE = os.environ.get("COOKIE_FILE", os.path.join(DATA_DIR, "cookie_config.json"))

# 追更 / STRM 持久化文件（路径与原 app.py 内定义一致）
CHASE_FILE = os.path.join(DATA_DIR, "chase_tasks.json")
CLIENT_TASKS_FILE = os.path.join(DATA_DIR, "client_tasks.json")
_CLIENT_TASKS_LOCK = threading.RLock()
STRM_FILE = os.path.join(DATA_DIR, "strm_tasks.json")
STRM_TASKS_FILE = STRM_FILE  # 向后兼容别名
STORAGES_FILE = os.path.join(DATA_DIR, "storages.json")
CLOUD_ACCOUNTS_FILE = os.path.join(DATA_DIR, "cloud_accounts.json")
CLOUD_CONFIG_FILE = os.path.join(DATA_DIR, "cloud_config.json")

# ── 公共常量（与原 app.py 顶部常量保持一致） ──
DEFAULT_GROUP_ID = "2203072050"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".webm",
              ".rmvb", ".mpg", ".mpeg", ".3gp", ".ogv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".ape"}

# 构建标记：每次修复“掉登录”问题时递增。用户在登录页 / 401 提示里看到这串，
# 即可确认当前运行的是否为最新代码（避免改了代码但镜像没重建的假象）。
SERVER_BUILD = "20261124-auth-2"

# 挂载根目录缓存 {storage_id: real_folder_id}
_mount_root_cache = {}


# ══════════════════════════════════════════════════
# 上传进度追踪（与原 app.py 实现一致）
# ══════════════════════════════════════════════════
_upload_tasks = {}  # {task_id: {name, total, uploaded, stage, done, success, error}}
_upload_lock = threading.Lock()


class _UploadProgressReader:
    """可分块读取的 byte reader，每读一块回调进度（原 app.py 实现）"""
    def __init__(self, data, task_id, total_size):
        self._data = data
        self._pos = 0
        self._total = total_size
        self._task_id = task_id

    def __len__(self):
        return self._total

    def read(self, size=-1):
        if self._pos >= self._total:
            return b""
        end = self._pos + size if size > 0 else self._total
        if end > self._total:
            end = self._total
        chunk = self._data[self._pos:end]
        self._pos = end
        with _upload_lock:
            t = _upload_tasks.get(self._task_id)
            if t:
                t["uploaded"] = self._pos
                import time as _t
                now = _t.time()
                elapsed = max(now - t.get("_t0", now), 0.01)
                t["speed"] = self._pos / elapsed
        return chunk

    def close(self):
        pass


def _update_task(task_id, **kw):
    with _upload_lock:
        if task_id not in _upload_tasks:
            import time as _t
            _upload_tasks[task_id] = {"_t0": _t.time()}
        _upload_tasks[task_id].update(kw)


def _cleanup_old_tasks():
    """清理 10 分钟前的已完成任务"""
    import time as _t
    now = _t.time()
    with _upload_lock:
        stale = [tid for tid, t in _upload_tasks.items()
                 if t.get("done") and now - t.get("_t0", now) > 600]
        for tid in stale:
            del _upload_tasks[tid]


# ══════════════════════════════════════════════════
# 客户端轮询 / 任务队列相关状态
# ══════════════════════════════════════════════════
_CLIENT_TASKS = {}     # task_id -> {任务详情}
_CLIENT_QUEUE = []     # 待执行队列 [task_id, ...]
_CLIENT_REGISTRY = {}  # client_name -> {last_seen, ip, max_concurrent, ...}
_chase_batch_pending = {}  # chase_id -> set(ep_num, ...) 待下载集数（完成后触发 STRM）
_chase_batch_lock = threading.Lock()
_CHASE_FILE_LOCK = threading.RLock()  # 追更任务文件 读-改-写 全局锁（防多线程并发覆盖导致丢失更新/漏集误判）
_PREVIEW_RESULTS = {}  # preview_id -> {status, files, matched, error}
_PREVIEW_LOCK = threading.Lock()
_CANCEL_REQUESTS = {}  # client_name -> {chase_id: ts}（带时间戳，下发后保留60秒供重复下发，防止 poll 响应丢失导致取消指令永远丢失）
_RESUME_REQUESTS = {}  # client_name -> set(chase_id)（重新运行追更时下发"恢复"信号，清除客户端取消状态，防止停止后60秒窗口内新任务被误取消）
_CANCELED_EPS = {}     # chase_id -> {ep: ts}：记录"最近被取消"的集，60秒内漏集补建/失败重试时排除，防止停止后重新运行时"取消→补建→再取消"刷屏
_CHASE_RESUME_COOLDOWN = {}  # chase_id -> 截止ts：停止后重新运行时60秒内不派发任务，等客户端取消窗口自然过期，防止旧客户端(不处理恢复信号)取消状态残留导致新任务被取消刷屏
_CLIENT_ACTIVE = {}    # client_name -> 当前活跃任务数
# 任务派发临界区互斥锁：多客户端并发轮询（Flask threaded=True）时，
# 防止两台客户端同时读到同一 pending 任务并重复下发（任务下发重叠）。
_CLIENT_DISPATCH_LOCK = threading.Lock()
_stream_cache = {}     # url -> (timestamp, content) 简单缓存
_QUARK_CLIENTS = {}    # cookie -> QuarkClient 实例缓存
_YIDONG_CLIENTS = {}   # cookie -> YidongClient 缓存
# 定时调度状态
scheduler_lock = threading.Lock()
_scheduler_state = {}
_trigger_lock = threading.Lock()
_trigger_state = {}


__all__ = [
    "BASE_DIR", "DATA_DIR", "CONFIG_FILE", "COOKIE_FILE", "CHASE_FILE",
    "STRM_FILE", "STRM_TASKS_FILE", "_upload_tasks", "_upload_lock", "_UploadProgressReader",
    "_update_task", "_cleanup_old_tasks",
    "_CLIENT_TASKS", "_CLIENT_QUEUE", "_CLIENT_REGISTRY", "_chase_batch_pending",
    "_chase_batch_lock", "_CHASE_FILE_LOCK", "_PREVIEW_RESULTS", "_PREVIEW_LOCK", "_CANCEL_REQUESTS", "_RESUME_REQUESTS", "_CANCELED_EPS", "_CHASE_RESUME_COOLDOWN",
    "_CLIENT_ACTIVE", "_CLIENT_DISPATCH_LOCK", "_stream_cache", "_QUARK_CLIENTS", "_YIDONG_CLIENTS",
    "scheduler_lock", "_scheduler_state", "_trigger_lock", "_trigger_state",
    "_load_chase", "_save_chase", "_load_strm_tasks", "_save_strm_tasks",
    "CLIENT_TASKS_FILE", "_save_client_tasks", "_load_client_tasks", "_CLIENT_TASKS_LOCK",
    "DEFAULT_GROUP_ID", "VIDEO_EXTS", "IMAGE_EXTS", "AUDIO_EXTS",
    "_mount_root_cache", "CLOUD_CONFIG_FILE", "SERVER_BUILD",
]


# ══════════════════════════════════════════════════
# 追更任务持久化
# ══════════════════════════════════════════════════
def _load_chase():
    with _CHASE_FILE_LOCK:
        data = []
        if os.path.exists(CHASE_FILE):
            try:
                with open(CHASE_FILE, "r", encoding="utf-8") as f:
                    data = json.loads(f.read()) or []
            except Exception:
                pass
        # 字段归一化：保证每条均有 id / chase_id，避免下游 KeyError
        # （兼容历史数据中只用其中一种字段或缺字段的条目）
        for t in data:
            if not isinstance(t, dict):
                continue
            cid = t.get("id") or t.get("chase_id") or ""
            if not cid:
                cid = str(uuid.uuid4())
            t["id"] = cid
            t["chase_id"] = cid
        return data


def _save_chase(tasks):
    with _CHASE_FILE_LOCK:
        from . import utils as _utils
        _utils.atomic_write_json(CHASE_FILE, tasks)


# ══════════════════════════════════════════════════
# 客户端任务队列持久化（防止服务重启丢失待下载任务）
# ══════════════════════════════════════════════════
def _save_client_tasks():
    with _CLIENT_TASKS_LOCK:
        from . import utils as _utils
        payload = {
            "tasks": list(_CLIENT_TASKS.values()),
            "queue": list(_CLIENT_QUEUE),
        }
        try:
            _utils.atomic_write_json(CLIENT_TASKS_FILE, payload)
        except Exception:
            pass


def _load_client_tasks():
    """启动时恢复未完成任务；running 视为未完成，重新置为 pending。"""
    with _CLIENT_TASKS_LOCK:
        if not os.path.exists(CLIENT_TASKS_FILE):
            return
        try:
            with open(CLIENT_TASKS_FILE, "r", encoding="utf-8") as f:
                payload = json.loads(f.read()) or {}
        except Exception:
            return
        tasks = payload.get("tasks") or []
        _CLIENT_TASKS.clear()
        _CLIENT_QUEUE.clear()
        for t in tasks:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            if t.get("status") in ("done", "error", "failed", "cancelled", "skipped"):
                continue
            if t.get("status") == "running":
                t["status"] = "pending"
                t["polled_by"] = ""
                t.pop("polled_at", None)
            _CLIENT_TASKS[t["id"]] = t
        for tid in payload.get("queue") or []:
            if tid in _CLIENT_TASKS and tid not in _CLIENT_QUEUE:
                _CLIENT_QUEUE.append(tid)
        for tid in _CLIENT_TASKS:
            if tid not in _CLIENT_QUEUE:
                _CLIENT_QUEUE.append(tid)
        if _CLIENT_TASKS:
            import logging
            logging.getLogger(__name__).info("已恢复 %d 个客户端任务", len(_CLIENT_TASKS))


# ══════════════════════════════════════════════════
# STRM 任务持久化
# ══════════════════════════════════════════════════
def _load_strm_tasks():
    if os.path.exists(STRM_FILE):
        try:
            with open(STRM_FILE, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []


def _save_strm_tasks(tasks):
    from . import utils as _utils
    _utils.atomic_write_json(STRM_FILE, tasks)
