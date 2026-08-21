"""
手机版任务 Worker：连接服务端 → 轮询任务 → 执行（追更检查/追更下载/普通传输）→ 上报结果。
移植自 PC 版 gui.py 的服务端交互逻辑，去掉了 tkinter 依赖，改为回调驱动：
  - log_cb(msg)            日志输出
  - status_cb(text, color) 连接状态变化
  - progress_cb(tid, name, stage, dl, total, speed) 任务进度
线程安全：所有回调在 worker 线程内调用，UI 侧需自行切主线程。
"""
import os
import re
import time
import json
import threading
import requests
from requests.exceptions import ProxyError, ConnectionError

from config_helper import (CLIENT_VERSION, load_config, get_save_dir)
from transfer_mobile import (TransferEngine, build_src_client,
                            detect_cloud_type)
from chase_engine import find_new_episodes, extract_episode
from yidong_core import YidongShareClient
from quark_core import QuarkShareClient

VIDEO_EXT = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv',
             '.webm', '.m4v', '.mpg', '.mpeg', '.ts', '.rmvb',
             '.iso', '.m2ts', '.3gp')

def _parse_video_exts(raw):
    """解析服务端随任务下发的视频格式白名单（个人资料设置）。
    兼容逗号/中文顿号/斜杠/分号/空格分隔，自动补前导点、转小写；
    为空/未配置时使用内置默认 VIDEO_EXT。"""
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
    return tuple(exts) if exts else VIDEO_EXT


class ClientWorker:
    def __init__(self, log_cb=None, status_cb=None, progress_cb=None):
        self.log_cb = log_cb or (lambda *a: None)
        self.status_cb = status_cb or (lambda *a: None)
        self.progress_cb = progress_cb or (lambda *a: None)

        self._stop = threading.Event()
        self._task_semaphore = threading.Semaphore(2)
        self._task_active = 0
        self._task_active_lock = threading.Lock()
        self._running_tids = set()
        self._running_tids_lock = threading.Lock()
        self._cancel_events = {}
        self._cancel_ts = {}
        self._cancel_events_lock = threading.Lock()

        self._cfg = load_config()
        self._wps_group_id = self._cfg.get("wps_group_id", "2203072050")
        self._max_concurrent = int(self._cfg.get("max_concurrent", 2) or 2)
        # 服务端下发的网盘凭据（WPS cookie / 139 / 夸克），连接后拉取，仅临时存内存
        self._server_creds = None

    # ── 生命周期 ──

    def start(self):
        self._stop.clear()
        self._task_semaphore = threading.Semaphore(max(1, self._max_concurrent))
        threading.Thread(target=self._srv_loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ── 服务端连接 ──

    def _srv_login(self, session, url):
        user = (self._cfg.get("server_user") or "").strip()
        pw = (self._cfg.get("server_pass") or "").strip()
        if not user or not pw:
            return True
        try:
            r = session.post(f"{url}/api/login",
                             json={"username": user, "password": pw}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return True
                self.log_cb(f"登录失败: {data.get('message', '未知错误')}")
                return False
            self.log_cb(f"登录失败: HTTP {r.status_code}")
            return False
        except Exception as e:
            self.log_cb(f"登录异常: {e}")
            return False

    def _fetch_server_creds(self, session, url, client_name):
        """连接服务端后拉取已配置的网盘凭据（WPS cookie / 139 / 夸克），临时存内存。

        客户端无需再手动填写 cookie/token；旧版服务端无此接口时静默返回 None，
        继续使用本地手动配置兜底。
        """
        try:
            r = session.get(f"{url}/api/client/creds",
                            params={"client": client_name}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                if d.get("success"):
                    wps_cookie = (d.get("wps_cookie") or "").strip()
                    clouds = d.get("clouds") or []
                    if wps_cookie or clouds:
                        self._server_creds = {"wps_cookie": wps_cookie, "clouds": clouds}
                        _types = ",".join(sorted({str(c.get("type", "")) for c in clouds if c.get("type")}))
                        self.log_cb(f"已获取服务端网盘凭据（WPS: {'已配置' if wps_cookie else '未配置'}，云盘: {_types or '无'}）")
                        return self._server_creds
        except Exception:
            pass
        self._server_creds = None
        return None

    def _srv_loop(self):
        url = (self._cfg.get("server_url") or "").strip().rstrip("/")
        if not url:
            self.log_cb("请先填写服务器地址")
            self.status_cb("未配置服务器", '#FFB900')
            return
        session = requests.Session()
        if not self._srv_login(session, url):
            self.log_cb("服务器登录失败")
            self.status_cb("登录失败", '#E0412C')
            return
        client_name = (self._cfg.get("client_name") or "手机客户端").strip()
        max_conc = self._max_concurrent
        # 连接成功后自动拉取服务端网盘凭据（临时缓存），此后无需本地手动配置
        self._server_creds = self._fetch_server_creds(session, url, client_name)
        self.log_cb(f"已连接 {url}，客户端: {client_name}，并发: {max_conc}，开始轮询")
        self.status_cb(f"轮询中({max_conc}并发)", '#1F9E57')

        _fail_count = 0  # 连续失败次数：连接异常按 15→30→60s 指数退避重试，避免服务端恢复前请求风暴
        while not self._stop.is_set():
            try:
                with self._task_active_lock:
                    active_now = self._task_active
                # 槽位满时不拉任务（batch=0），避免"拉到任务→无槽位跳过→服务端
                # 冷却/重置→再推送"的刷屏；至少保留 1 个并发时正常拉取
                free_slots = max(0, max_conc - active_now)
                with self._running_tids_lock:
                    running_ids = ",".join(self._running_tids) if self._running_tids else ""
                _live_clouds = load_config().get("clouds", []) or self._cfg.get("clouds", [])
                # 服务端下发的云盘凭据也算入本客户端能力，避免服务端因 cloud_types 为空不下发任务
                if self._server_creds and self._server_creds.get("clouds"):
                    _live_clouds = list(_live_clouds) + list(self._server_creds["clouds"])
                cloud_types = ",".join(sorted(
                    {str(c.get("type", "")) for c in _live_clouds if c.get("type")}))
                params = {"client": client_name, "batch": str(free_slots),
                          "max_concurrent": str(max_conc), "check_cancel": "1",
                          "running_tids": running_ids,
                          "version": CLIENT_VERSION,
                          "cloud_types": cloud_types}
                r = session.get(f"{url}/api/client/poll", params=params, timeout=(10, 30))
                if r.status_code == 200:
                    data = r.json()
                    # ── 取消指令 ──
                    cancels = data.get("cancels", [])
                    if not cancels and isinstance(data.get("task"), dict):
                        t = data.get("task")
                        if t.get("type") == "cancel":
                            cancels = t.get("chase_ids", [t.get("chase_id", "")])
                    for chase_id in cancels:
                        if chase_id:
                            with self._cancel_events_lock:
                                _first = (chase_id not in self._cancel_events
                                          or not self._cancel_events[chase_id].is_set())
                            if _first:
                                self.log_cb(f"收到取消指令: {chase_id}")
                            self._signal_cancel(chase_id)
                    # ── 恢复指令：清除本地取消状态（重新运行追更后不再误取消） ──
                    for chase_id in data.get("resumes", []):
                        if chase_id:
                            with self._cancel_events_lock:
                                self._cancel_events.pop(chase_id, None)
                                self._cancel_ts.pop(chase_id, None)
                            self.log_cb(f"已恢复下载: {chase_id}")
                    # ── 任务列表 ──
                    tasks = data.get("tasks", [])
                    if not tasks and data.get("task") and isinstance(data["task"], dict):
                        t = data["task"]
                        if t.get("type") != "cancel":
                            tasks = [t]
                    for task in tasks:
                        self.log_cb(f"收到任务: {task.get('name','?')} [{task.get('type','?')}]")
                        self._launch_task_thread(session, url, task)
                else:
                    self.log_cb(f"服务器返回 {r.status_code}")
                    self.status_cb(f"错误({r.status_code})重试中", '#FFB900')
                    _fail_count += 1
                    self._stop.wait(10)
            except ProxyError:
                _fail_count += 1
                _delay = min(30 * _fail_count, 60)  # 30→60s 退避
                self.log_cb(f"代理连接失败，切换到直连模式，{_delay}秒后重试...")
                self.status_cb("代理错误(直连重试)", '#FFB900')
                session.close()
                session = requests.Session()
                session.trust_env = False
                if not self._srv_login(session, url):
                    self.log_cb("服务器重新登录失败")
                    self.status_cb("登录失败", '#E0412C')
                    return
                self._stop.wait(_delay)
            except ConnectionError:
                _fail_count += 1
                _delay = min(15 * (2 ** (_fail_count - 1)), 60)  # 15→30→60s 指数退避
                self.log_cb(f"连接失败，{_delay}秒后重试...")
                self.status_cb("连接失败(重试中)", '#FFB900')
                self._stop.wait(_delay)
            except Exception as e:
                _fail_count += 1
                _delay = min(10 * (2 ** (_fail_count - 1)), 60)  # 10→20→40→60s 指数退避
                self.log_cb(f"轮询出错: {e}，{_delay}秒后重试")
                self._stop.wait(_delay)
            else:
                _fail_count = 0
                self.status_cb(f"轮询中({max_conc}并发)", '#1F9E57')  # 成功轮询才恢复状态
                self._stop.wait(5)
        self.log_cb("轮询已停止")
        self.status_cb("已断开", '#848D97')

    # ── 取消机制 ──

    def _signal_cancel(self, chase_id):
        # 取消窗口自首次收到起 60 秒；服务端窗口内重复下发不会延长窗口，
        # 否则停止后重新运行会被持续误取消
        with self._cancel_events_lock:
            if chase_id not in self._cancel_events:
                self._cancel_events[chase_id] = threading.Event()
            self._cancel_events[chase_id].set()
            if chase_id not in self._cancel_ts:
                self._cancel_ts[chase_id] = time.time()

    def _is_chase_cancelled(self, chase_id, cancel_evt):
        if not cancel_evt or not cancel_evt.is_set():
            return False
        if chase_id:
            with self._cancel_events_lock:
                ts = self._cancel_ts.get(chase_id, 0)
                if ts and time.time() - ts > 60:
                    cancel_evt.clear()
                    self._cancel_ts.pop(chase_id, None)
        return cancel_evt.is_set()

    # ── 上报 ──

    def _post_task_status(self, session, url, tid, status, msg="", retries=3):
        body = {"id": tid, "status": status, "msg": msg}
        for i in range(retries):
            try:
                r = session.post(f"{url}/api/client/task_status", json=body, timeout=10)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            if i < retries - 1:
                time.sleep(1 + i * 2)
        return False

    def _post_task_progress(self, session, url, tid, stage, dl, total, speed):
        """上报运行中任务的实时进度（★7），失败静默，不重试"""
        if not tid:
            return
        try:
            session.post(f"{url}/api/client/task_status",
                         json={"id": tid, "status": "running",
                               "stage": str(stage or ""),
                               "progress": int(dl or 0), "total": int(total or 0),
                               "speed": float(speed or 0)},
                         timeout=6)
        except Exception:
            pass

    def _post_chase_done(self, session, url, chase_id, ep_num, ok, error="",
                         file_name="", retries=3):
        body = {"chase_id": chase_id, "ep_num": ep_num, "ok": ok,
                "error": error, "file_name": file_name}
        for i in range(retries):
            try:
                r = session.post(f"{url}/api/client/chase_done", json=body, timeout=10)
                if r.status_code == 200:
                    try:
                        return True, r.json()
                    except Exception:
                        return True, {}
            except Exception:
                pass
            if i < retries - 1:
                time.sleep(1 + i * 2)
        return False, None

    def _post_chase_result(self, session, url, chase_id, payload):
        try:
            resp = session.post(f"{url}/api/client/chase_result",
                                json={"chase_id": chase_id, **payload}, timeout=10)
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, str(e)

    # ── 任务分发 ──

    def _launch_task_thread(self, session, url, task):
        tid = task.get("id", "")
        ttype = task.get("type", "")
        chase_id = task.get("chase_id", "")
        with self._running_tids_lock:
            if tid and tid in self._running_tids:
                return
        acquired = self._task_semaphore.acquire(blocking=False)
        if not acquired:
            self.log_cb(f"无空闲槽位，跳过: {task.get('name','?')}")
            self._post_task_status(session, url, tid, "skipped", "槽位已满")
            return
        if tid:
            with self._running_tids_lock:
                self._running_tids.add(tid)
        with self._task_active_lock:
            self._task_active += 1

        def _worker():
            try:
                if ttype == "chase-check":
                    self._exec_chase_check(session, url, task)
                elif ttype == "chase-download":
                    self._exec_chase_download(session, url, task)
                else:
                    self._exec_normal_task(session, url, task)
            finally:
                self._task_semaphore.release()
                with self._task_active_lock:
                    self._task_active -= 1
                if tid:
                    with self._running_tids_lock:
                        self._running_tids.discard(tid)
                if tid:
                    with self._cancel_events_lock:
                        self._cancel_events.pop(tid, None)

        threading.Thread(target=_worker, daemon=True).start()

    # ── 引擎构建 ──

    def _make_engine(self, share_url, wps_parent_id=None, cloud_id=None):
        cfg = load_config()
        srv = self._server_creds or {}
        # 服务端下发的凭据优先，本地手动配置兜底（未下发或旧版服务端时）
        clouds = list(srv.get("clouds") or []) or cfg.get("clouds", []) or self._cfg.get("clouds", [])
        if not clouds and cfg.get("yidong_token"):
            clouds = [{"id": "yidong_main", "type": "yidong", "name": "139主账号",
                       "is_primary": True, "token": cfg["yidong_token"]}]
        else:
            if cfg.get("yidong_token") and not any(c.get("type") == "yidong"
                                                   for c in clouds):
                clouds = list(clouds) + [{"id": "yidong_main", "type": "yidong",
                                          "name": "139主账号", "is_primary": True,
                                          "token": cfg["yidong_token"]}]
        src = build_src_client(clouds, share_url, cloud_id)
        slow_retry = None
        try:
            _mb = float(cfg.get("upload_slow_min_mb", 0) or 0)
            _sec = float(cfg.get("upload_slow_seconds", 0) or 0)
            if _mb > 0 and _sec > 0:
                slow_retry = {"min_mb": _mb, "seconds": _sec}
        except (TypeError, ValueError):
            slow_retry = None
        # WPS cookie：服务端下发优先，本地兜底
        wps_cookie = (srv.get("wps_cookie") or "").strip() or cfg.get("wps_cookie", "")
        yidong_token = cfg.get("yidong_token", "")
        # 服务端下发了 139 凭据且本地未配置时，兜底参数也用它
        if not yidong_token and srv.get("clouds"):
            for _c in srv["clouds"]:
                if _c.get("type") == "yidong" and _c.get("token"):
                    yidong_token = _c["token"]
                    break
        return TransferEngine(
            yidong_token, wps_cookie,
            get_save_dir(),
            wps_parent_id=wps_parent_id or cfg.get("wps_parent_id", "0") or "0",
            wps_group_id=self._wps_group_id,
            src_client=src,
            upload_slow_retry=slow_retry)

    def _dl_creds_ok(self):
        """下载前置凭据检查：本地配置优先，服务端下发凭据兜底（与 _make_engine 一致）。
        返回 (yidong_token, wps_cookie, save_dir, ok)。
        修复：客户端本地未填 139 token/WPS cookie 但服务端已配置时，
        此前误报「缺少配置」导致追更下载全部失败。"""
        cfg = load_config()
        t = (cfg.get("yidong_token") or "").strip()
        c = (cfg.get("wps_cookie") or "").strip()
        d = get_save_dir()
        srv = self._server_creds or {}
        clouds = list(cfg.get("clouds") or []) + list(srv.get("clouds") or [])
        if not t and srv.get("clouds"):
            for _c in srv["clouds"]:
                if _c.get("type") == "yidong" and _c.get("token"):
                    t = _c["token"]
                    break
        if not c:
            c = (srv.get("wps_cookie") or "").strip()
        ok = (bool(t) or any(x.get("type") == "yidong" for x in clouds)) and bool(c) and bool(d)
        return t, c, d, ok

    # ── 普通任务 ──

    def _exec_normal_task(self, session, url, task):
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        rename_to = task.get("rename_to", "") or task.get("name", "")
        file_name = task.get("file_name", "")
        category = task.get("category", "电影")
        show_folder = task.get("show_folder", "")
        wps_path = task.get("wps_path", "")
        if not wps_path and show_folder:
            wps_path = f"{category}/{show_folder}"
        elif not wps_path:
            wps_path = category
        tid = task.get("id", "")
        chase_id = task.get("chase_id", "")
        ep_num = task.get("ep_num", 0)
        display_name = rename_to or file_name or "task"

        _prog_last = [0.0]
        def tcb(stage, dl, total, speed):
            self.progress_cb(tid, display_name, stage, dl, total, speed)
            # ★7 实时进度上报到服务端，节流 3 秒一次，避免高频请求
            now_t = time.time()
            if now_t - _prog_last[0] >= 3.0:
                _prog_last[0] = now_t
                self._post_task_progress(session, url, tid, stage, dl, total, speed)

        cancel_evt = threading.Event()
        if chase_id:
            with self._cancel_events_lock:
                if chase_id in self._cancel_events:
                    cancel_evt = self._cancel_events[chase_id]
                else:
                    self._cancel_events[chase_id] = cancel_evt

        try:
            t, c, d, _creds_ok = self._dl_creds_ok()
            if task.get("skip"):
                self.log_cb(f"已跳过(预览中标记为不下载): {display_name}")
                self._post_task_status(session, url, tid, "skipped", "预览中标记跳过")
                self._post_chase_done(session, url, chase_id, ep_num, True, "skipped")
                return
            if not _creds_ok:
                self._post_task_status(session, url, tid, "error", "缺少配置")
                tcb("error", 0, 0, "缺少配置(请先设置139 token 与 WPS cookie)")
                return
            if self._is_chase_cancelled(chase_id, cancel_evt):
                self.log_cb(f"本集已取消: {display_name}")
                # 取消确认仅上报一次，不重试
                self._post_chase_done(session, url, chase_id, ep_num, False, "已取消", retries=1)
                self._post_task_status(session, url, tid, "skipped", "已取消", retries=1)
                return
            engine = self._make_engine(task.get("share_url", ""),
                                       wps_parent_id=cfg.get("wps_parent_id", "0") or "0")
            if rename_to and rename_to.strip() and not (task.get("episodes_filter") or "").strip():
                try:
                    _tgt = (engine._wps_resolve_path(wps_path)
                            if wps_path and wps_path.strip() else engine.wps_parent_id)
                    if _tgt:
                        _rt = rename_to.strip()
                        _found = False
                        for _f in engine._wps_list_files(_tgt):
                            _fn = (_f.get("fname") or _f.get("name") or "").strip()
                            if _fn == _rt or _fn.startswith(_rt + "."):
                                _found = True
                                break
                        if _found:
                            self.log_cb(f"目标文件已在WPS中，跳过下载: {_rt}")
                            self._post_chase_done(session, url, chase_id, ep_num,
                                                  True, "目标文件已存在")
                            self._post_task_status(session, url, tid, "done", "目标文件已存在")
                            tcb("done", 100, 100, "目标文件已存在，跳过")
                            return
                except Exception as _e:
                    self.log_cb(f"下载前WPS探测失败(不影响下载): {_e}")
            ok, err = engine.transfer(share_url, share_passwd=share_pwd,
                                      progress_cb=tcb,
                                      cancel_check=lambda: cancel_evt.is_set(),
                                      rename_to=rename_to, wps_path=wps_path,
                                      delete_after_upload=True,
                                      file_name=file_name)
            self._post_task_status(session, url, tid, "done" if ok else "error",
                                   err or "完成")
            if not ok and err:
                tcb("error", 0, 0, err)
            else:
                tcb("done", 100, 100, "")
        except Exception as e:
            self._post_task_status(session, url, tid, "error", str(e))
            tcb("error", 0, 0, str(e))

    # ── 追更检查 ──

    def _exec_chase_check(self, session, url, task):
        """追更检查已迁移到服务端执行（浏览分享/识别集数/WPS目录探测）。
        客户端只负责 下载/上传/上报信息，收到 chase-check 任务直接确认完成，
        不再执行任何检查逻辑。"""
        tid = task.get("id", "")
        name = task.get("name", "追更检查")
        self.log_cb(f"追更检查由服务端执行，客户端跳过检查: {name}")
        self._post_task_status(session, url, tid, "done", "服务端已处理")

    # ── 追更下载 ──

    def _exec_chase_download(self, session, url, task):
        # ── 批量任务：多集流水线（上传本集时并行下载下一集），每集单独上报 ──
        if task.get("batch") and task.get("files"):
            self._exec_chase_batch(session, url, task)
            return
        chase_id = task.get("chase_id", "")
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        file_name = task.get("file_name", "")
        rename_to = task.get("rename_to", "")
        wps_path = task.get("wps_path", "")
        ep_num = task.get("ep_num", 0)
        tid = task.get("id", "")
        display_name = rename_to or file_name or "task"

        _prog_last = [0.0]
        def tcb(stage, dl, total, speed):
            self.progress_cb(tid, display_name, stage, dl, total, speed)
            # ★7 实时进度上报到服务端，节流 3 秒一次，避免高频请求
            now_t = time.time()
            if now_t - _prog_last[0] >= 3.0:
                _prog_last[0] = now_t
                self._post_task_progress(session, url, tid, stage, dl, total, speed)

        cancel_evt = threading.Event()
        if chase_id:
            with self._cancel_events_lock:
                if chase_id in self._cancel_events:
                    cancel_evt = self._cancel_events[chase_id]
                else:
                    self._cancel_events[chase_id] = cancel_evt

        try:
            t, c, d, _creds_ok = self._dl_creds_ok()
            if task.get("skip"):
                self.log_cb(f"已跳过(预览中标记为不下载): {display_name}")
                self._post_task_status(session, url, tid, "skipped", "预览中标记跳过")
                self._post_chase_done(session, url, chase_id, ep_num, True, "skipped")
                return
            if not _creds_ok:
                self._post_task_status(session, url, tid, "error", "缺少配置")
                tcb("error", 0, 0, "缺少配置(请先设置139 token 与 WPS cookie)")
                return
            if self._is_chase_cancelled(chase_id, cancel_evt):
                self.log_cb(f"本集已取消: {display_name}")
                # 取消确认仅上报一次，不重试
                self._post_chase_done(session, url, chase_id, ep_num, False, "已取消", retries=1)
                self._post_task_status(session, url, tid, "skipped", "已取消", retries=1)
                return
            engine = self._make_engine(share_url,
                                       wps_parent_id=cfg.get("wps_parent_id", "0") or "0")
            if rename_to and rename_to.strip() and not (task.get("episodes_filter") or "").strip():
                try:
                    _tgt = (engine._wps_resolve_path(wps_path)
                            if wps_path and wps_path.strip() else engine.wps_parent_id)
                    if _tgt:
                        _rt = rename_to.strip()
                        _found = False
                        for _f in engine._wps_list_files(_tgt):
                            _fn = (_f.get("fname") or _f.get("name") or "").strip()
                            if _fn == _rt or _fn.startswith(_rt + "."):
                                _found = True
                                break
                        if _found:
                            self.log_cb(f"目标文件已在WPS中，跳过下载: {_rt}")
                            self._post_chase_done(session, url, chase_id, ep_num,
                                                  True, "目标文件已存在")
                            self._post_task_status(session, url, tid, "done", "目标文件已存在")
                            tcb("done", 100, 100, "目标文件已存在，跳过")
                            return
                except Exception as _e:
                    self.log_cb(f"下载前WPS探测失败(不影响下载): {_e}")
            ok, err = engine.transfer(share_url, share_passwd=share_pwd,
                                      progress_cb=tcb,
                                      cancel_check=lambda: cancel_evt.is_set(),
                                      rename_to=rename_to, wps_path=wps_path,
                                      delete_after_upload=True,
                                      file_name=file_name)
            _cancel_err = bool(err and ("取消" in err))
            _retries = 1 if _cancel_err else 3  # 主动取消确认无需重试
            _okr, _data = self._post_chase_done(session, url, chase_id, ep_num,
                                                ok, err or "", file_name, retries=_retries)
            if _okr and _data:
                if ok:
                    current_ep = _data.get("base_ep", ep_num)
                    self.log_cb(f"追更进度已上报: {rename_to or file_name} 完成 "
                                f"(已追到第{current_ep}集)")
                else:
                    self.log_cb(f"追更失败: {rename_to or file_name} 未成功 "
                                f"(集数标记 ep={ep_num})，错误: {err or _data.get('message','')}")
            self._post_task_status(session, url, tid, "done" if ok else "error",
                                   err or "完成", retries=_retries)
            if not ok and err:
                tcb("error", 0, 0, err)
            else:
                tcb("done", 100, 100, "")
        except Exception as e:
            self._post_chase_done(session, url, chase_id, ep_num, False, str(e), file_name)
            self._post_task_status(session, url, tid, "error", str(e))
            tcb("error", 0, 0, str(e))

    def _exec_chase_batch(self, session, url, task):
        """追更批量下载：同路径多集打包为一次传输，流水线执行。
        下载 1 → 上传 1 的同时并行下载 2 → 1 上传完上传 2，同时下载 3……
        每集完成后单独上报追更进度（chase_done），服务端据此更新卡片进度。
        """
        chase_id = task.get("chase_id", "")
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        tid = task.get("id", "")
        files = task.get("files", []) or []
        display_name = task.get("name") or ("%s 等%d集" % (task.get("rename_to", ""), len(files)))

        _prog_last = [0.0]
        def tcb(stage, dl, total, speed):
            self.progress_cb(tid, display_name, stage, dl, total, speed)
            now_t = time.time()
            if now_t - _prog_last[0] >= 3.0:
                _prog_last[0] = now_t
                self._post_task_progress(session, url, tid, stage, dl, total, speed)

        cancel_evt = threading.Event()
        if chase_id:
            with self._cancel_events_lock:
                if chase_id in self._cancel_events:
                    cancel_evt = self._cancel_events[chase_id]
                else:
                    self._cancel_events[chase_id] = cancel_evt

        try:
            t, c, d, _creds_ok = self._dl_creds_ok()
            if not _creds_ok:
                self._post_task_status(session, url, tid, "error", "缺少配置")
                tcb("error", 0, 0, "缺少配置(请先设置139 token 与 WPS cookie)")
                return
            if self._is_chase_cancelled(chase_id, cancel_evt):
                self.log_cb(f"批量下载已取消: {display_name}")
                self._post_task_status(session, url, tid, "skipped", "已取消")
                return
            engine = self._make_engine(share_url,
                                       wps_parent_id=cfg.get("wps_parent_id", "0") or "0")
            # ── 逐集自检：标记跳过的 / WPS 已存在的集直接上报完成，不进入传输 ──
            file_names = []
            rename_map = {}
            todo = []
            for fi in files:
                fname = fi.get("file_name", "")
                rname = fi.get("rename_to", "")
                if fi.get("skip"):
                    self.log_cb(f"已跳过(预览中标记为不下载): {rname or fname}")
                    self._post_chase_done(session, url, chase_id, fi.get("ep_num", 0), True, "skipped")
                    continue
                if rname and rname.strip() and not (task.get("episodes_filter") or "").strip():
                    try:
                        _tgt = (engine._wps_resolve_path(fi.get("wps_path", ""))
                                if (fi.get("wps_path", "") or "").strip() else engine.wps_parent_id)
                        _rt = rname.strip()
                        _found = False
                        if _tgt:
                            for _f in engine._wps_list_files(_tgt):
                                _fn = (_f.get("fname") or _f.get("name") or "").strip()
                                if _fn == _rt or _fn.startswith(_rt + "."):
                                    _found = True
                                    break
                        if _found:
                            self.log_cb(f"目标文件已在WPS中，跳过下载: {_rt}")
                            self._post_chase_done(session, url, chase_id, fi.get("ep_num", 0),
                                                  True, "目标文件已存在")
                            continue
                    except Exception as _e:
                        self.log_cb(f"下载前WPS探测失败(不影响下载): {_e}")
                todo.append(fi)
                file_names.append(fname)
                rename_map[fname] = rname
            if not todo:
                self._post_task_status(session, url, tid, "done", "全部跳过")
                tcb("done", 100, 100, "全部跳过")
                return
            wps_path = todo[0].get("wps_path", "") or task.get("wps_path", "")
            state = {"done": 0, "failed": 0, "n": len(todo)}

            def _per_file_done(idx, name, ok, err):
                fi = todo[idx] if 0 <= idx < len(todo) else None
                _ep = fi.get("ep_num", 0) if fi else 0
                if ok:
                    state["done"] += 1
                else:
                    state["failed"] += 1
                _okr, _data = self._post_chase_done(session, url, chase_id, _ep, ok, err or "", name)
                if _okr and _data:
                    if ok:
                        self.log_cb("追更进度已上报: %s 完成 (已追到第%d集) [%d/%d]"
                                    % (name, _data.get("base_ep", _ep), state["done"], state["n"]))
                    else:
                        self.log_cb("追更失败: %s 未成功 (集数标记 ep=%d)，错误: %s"
                                    % (name, _ep, err or _data.get("message", "")))
                else:
                    self.log_cb("追更进度上报失败: %s (%s)" % (name, _data or ""))

            ok_all, err_all = engine.transfer_batch(
                share_url, share_passwd=share_pwd, file_names=file_names,
                progress_cb=tcb, cancel_check=lambda: cancel_evt.is_set(),
                rename_map=rename_map, wps_path=wps_path,
                delete_after_upload=True, prefetch=1, per_file_done_cb=_per_file_done)
            if cancel_evt.is_set():
                self._post_task_status(session, url, tid, "skipped", "已取消")
            else:
                self._post_task_status(session, url, tid, "done" if ok_all else "error",
                                       err_all or "完成")
                if ok_all:
                    tcb("done", 100, 100, "")
                else:
                    tcb("error", 0, 0, err_all or "")
        except Exception as e:
            self._post_task_status(session, url, tid, "error", str(e))
            tcb("error", 0, 0, str(e))
