"""
WPS 365 上传模块 — 支持大文件流式上传
- Cookie 登录
- v5 三步上传：创建任务 → requests.put() 流式上传KS3 → 提交元数据
- 不限制文件大小，内存占用固定
"""
import json
import time
import os
import sys
import hashlib
import requests

API_BASE = "https://drive.kdocs.cn/api/v3"
DRIVE_V5 = "https://drive.kdocs.cn"
DEFAULT_GROUP_ID = "2203072050"

CHUNK_SIZE = 8 * 1024 * 1024  # 8MB 分块


class SlowUploadError(Exception):
    """上传速度持续过低，需中断本次上传并重新上传（本地文件已下载，无需重新下载）"""


def fmt_file_size(b):
    if not b or b <= 0:
        return "0 B"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


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


class WPSUploader:
    """WPS 365 文件上传器 — 流式分块，支持大文件"""

    def __init__(self, cookie_str, group_id=DEFAULT_GROUP_ID, slow_retry=None):
        """
        Args:
            slow_retry: 可选 dict，如 {"min_mb": 0.5, "seconds": 30}。
                        上传速度持续低于 min_mb(MB/s) 超过 seconds 秒时，
                        中断本次上传并自动重新上传（本地文件已下载，不重新下载）。
                        None 或空 dict 表示不启用。
        """
        self.s = build_session(cookie_str)
        self.csrf = get_csrf(cookie_str)
        self.group_id = group_id
        self.slow_retry = slow_retry or {}

    @staticmethod
    def _extract_response(spec, resp):
        if not spec:
            return None
        where, _, name = spec.partition(".")
        if where == "header":
            return resp.headers.get(name)
        if where == "body":
            try:
                return resp.json().get(name)
            except ValueError:
                return None
        return None

    def _requests_put_stream(self, url, headers, file_path, file_size,
                             progress_cb=None, cancel_check=None, slow_retry=None):
        """用 requests.put() 直接从磁盘流式上传，与服务端同模式
        通过 __len__ 让 requests 正确设置 Content-Length，避免 chunked encoding
        
        Args:
            slow_retry: 可选 dict，如 {"min_mb": 0.5, "seconds": 30}。上传速度持续
                        低于 min_mb(MB/s) 超过 seconds 秒时抛 SlowUploadError 中断上传。
        
        Returns:
            (status_code, response_headers_dict, response_body_str)
        """
        f = open(file_path, "rb")

        class _ProgressReader:
            def __init__(self, fp, fsize, cb, cc, slow_cfg=None):
                self._f = fp
                self._size = fsize
                self._cb = cb
                self._cc = cc
                self._pos = 0
                self._t0 = time.time()
                self._last_cb = 0
                # 慢速检测：记录低速率区间的起点，持续超时抛异常
                self._slow_cfg = slow_cfg or {}
                self._slow_win_start = 0   # 低速窗口起始字节
                self._slow_win_t = 0.0     # 低速窗口起始时间
                self._slow_ts = 0.0        # 最近一次确认低速的时间
                self._last_win_pos = 0
                self._last_win_t = 0.0

            def __len__(self):
                return self._size

            def _check_slow(self, now):
                cfg = self._slow_cfg
                if not cfg:
                    return
                try:
                    min_bps = float(cfg.get("min_mb", 0)) * 1024 * 1024
                    secs = float(cfg.get("seconds", 0))
                except (TypeError, ValueError):
                    return
                if min_bps <= 0 or secs <= 0:
                    return
                # 距上次采样至少 2 秒再评估一次窗口速率，避免瞬时波动误判
                if now - self._last_win_t < 2.0:
                    return
                dt = now - self._last_win_t
                if dt <= 0:
                    self._last_win_t = now
                    self._last_win_pos = self._pos
                    return
                seg_bps = (self._pos - self._last_win_pos) / dt
                self._last_win_t = now
                self._last_win_pos = self._pos
                if seg_bps < min_bps:
                    if self._slow_win_t == 0:
                        self._slow_win_t = now
                        self._slow_win_start = self._pos
                    else:
                        # 窗口内平均速率也低于阈值才累计，防止中间短暂回升被算入低速
                        win_bps = (self._pos - self._slow_win_start) / max(0.1, now - self._slow_win_t)
                        if win_bps < min_bps and now - self._slow_win_t >= secs:
                            raise SlowUploadError(
                                f"上传速度持续低于 {cfg.get('min_mb')}MB/s "
                                f"超过 {int(secs)} 秒，重新上传")
                else:
                    # 速率恢复，重置低速窗口
                    self._slow_win_t = 0
                    self._slow_win_start = 0

            def read(self, size=-1):
                if self._cc and self._cc():
                    raise Exception("已取消")
                if size is None or size <= 0:
                    size = 8 * 1024 * 1024
                chunk = self._f.read(size)
                if chunk:
                    self._pos += len(chunk)
                    now = time.time()
                    self._check_slow(now)
                    if self._cb and now - self._last_cb > 0.5:
                        pct = min(100, int(self._pos / self._size * 100))
                        elapsed = now - self._t0
                        spd = self._pos / max(0.1, elapsed)
                        self._cb(pct, f"{spd/1024/1024:.1f}MB/s")
                        self._last_cb = now
                return chunk

            def close(self):
                self._f.close()

        reader = _ProgressReader(f, file_size, progress_cb, cancel_check, slow_retry)
        headers.pop("Transfer-Encoding", None)
        headers.pop("transfer-encoding", None)
        try:
            r = self.s.put(url, data=reader, headers=headers, timeout=600, stream=True)
            body = r.text
            return r.status_code, dict(r.headers), body
        finally:
            reader.close()

    def _http_put_stream(self, url, headers, file_path, file_size,
                         progress_cb=None, cancel_check=None, slow_retry=None):
        # 兼容旧接口，转发到新实现
        return self._requests_put_stream(url, headers, file_path, file_size,
                                         progress_cb, cancel_check, slow_retry)

    def upload_file_from_disk(self, file_path, file_name, parent_id="0",
                              progress_cb=None, cancel_check=None):
        """从磁盘文件上传到 WPS，流式分块，支持任意大小

        Args:
            file_path: 本地文件路径
            file_name: 上传后的文件名
            parent_id: WPS 父文件夹ID
            progress_cb: 回调函数(percent, speed_str)
            cancel_check: 返回True则取消

        Returns:
            (status_code, result_dict)
        """
        file_size = os.path.getsize(file_path)

        # 计算 MD5 和 SHA1（锁外并行计算，避免阻塞排队任务）
        md5_sum = hashlib.md5()
        sha1_sum = hashlib.sha1()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024 * 1024)
                if not chunk:
                    break
                md5_sum.update(chunk)
                sha1_sum.update(chunk)
        md5_sum = md5_sum.hexdigest()
        sha1_sum = sha1_sum.hexdigest()

        try:
            pid = int(parent_id) if parent_id and str(parent_id).strip() else 0
        except ValueError:
            pid = 0
        print(f"[UPLOAD] raw parent_id={parent_id!r} resolved pid={pid!r}", flush=True, file=sys.stderr)
        try:
            gid = int(self.group_id)
        except (ValueError, TypeError):
            gid = int(self.group_id) if str(self.group_id).strip().isdigit() else int(DEFAULT_GROUP_ID)

        for attempt in range(1, 6):
            # 用户取消：立即中断，不再重试
            if cancel_check and cancel_check():
                return 499, {"error": "[Canceled] 已取消"}
            if attempt > 1 and progress_cb:
                progress_cb(-1, f"重试中({attempt}/5)...")
            try:
                t0 = time.time()
                # 步骤1: 创建上传任务
                body_1 = {
                    "groupid": gid, "parentid": pid, "parent_path": [],
                    "size": file_size, "name": file_name, "req_by_internal": False,
                    "client_stores": "ks3", "contenttype": "application/octet-stream",
                    "startswithfilename": file_name, "successactionstatus": 201,
                    "group_id": gid, "parent_id": pid, "file_id": 0,
                    "with_rapid": True, "tried_store": [],
                    "md5": md5_sum, "csrfmiddlewaretoken": self.csrf,
                }
                v5_h = {"content-type": "application/json",
                        "origin": "https://www.kdocs.cn",
                        "referer": "https://www.kdocs.cn/",
                        "accept": "*/*"}
                r1 = self.s.put(f"{DRIVE_V5}/api/v5/files/upload/create_update",
                                data=json.dumps(body_1, ensure_ascii=False).encode("utf-8"),
                                headers=v5_h, timeout=60)
                if r1.status_code >= 400:
                    try:
                        err = r1.json()
                        err_msg = f"{err.get('result','')}: {err.get('msg','')}"
                    except Exception:
                        err_msg = r1.text[:200]
                    if r1.status_code == 403 and 'fileSizeLimit' in r1.text:
                        return 403, {"error": f"文件大小超过WPS限制 ({fmt_file_size(file_size)})"}
                    raise Exception(f"创建上传任务失败({r1.status_code}): {err_msg}")
                plan = r1.json()

                # 步骤2: 流式上传到 KS3（requests.put() + 文件对象，自动设置 Content-Length）
                store = plan.get("store", "ks3")
                headers2 = dict(plan["request"]["headers"])
                headers2["Content-Length"] = str(file_size)

                t_put_start = time.time()
                r2_status, r2_headers, r2_body = self._http_put_stream(
                    plan["url"], headers2, file_path, file_size,
                    progress_cb, cancel_check, slow_retry=self.slow_retry)
                t_put_end = time.time()
                if r2_status >= 400:
                    raise Exception(f"KS3上传失败({r2_status}): {r2_body[:500]}")

                # 步骤3: 提交元数据
                spec = plan.get("response", {})
                # 从 r2_headers 提取 key 和 etag
                key = None
                etag = r2_headers.get("ETag") or r2_headers.get("etag") or ""
                if spec:
                    key_spec = spec.get("args_key", "")
                    if key_spec.startswith("header."):
                        key = r2_headers.get(key_spec.replace("header.", ""))
                    elif key_spec.startswith("body."):
                        try:
                            r2_json = json.loads(r2_body)
                            key = r2_json.get(key_spec.replace("body.", ""))
                        except Exception:
                            pass
                if not key:
                    key = ""

                # 优先使用 KS3 返回的 ETag，客户端 MD5 作为备用
                server_etag = r2_headers.get("ETag") or r2_headers.get("etag") or ""
                # KS3 的 ETag 通常带引号，如 "d41d8cd98f00b204e9800998ecf8427e"
                if server_etag and not server_etag.startswith('"'):
                    server_etag = f'"{server_etag}"'

                body_3 = {
                    "key": key, "groupid": gid, "parentid": pid, "parent_path": [],
                    "name": file_name, "isUpNewVer": False,
                    "etag": f'"{md5_sum}"', "store": store,
                    "size": file_size, "sha1": sha1_sum,
                    "apiErrorInfo": None, "csrfmiddlewaretoken": self.csrf,
                }
                r3 = self.s.post(f"{DRIVE_V5}/api/v5/files/file",
                                 data=json.dumps(body_3, ensure_ascii=False).encode("utf-8"),
                                 headers=v5_h, timeout=60)
                if r3.status_code >= 400:
                    try:
                        err_json = r3.json()
                        err_detail = json.dumps(err_json, ensure_ascii=False)[:500]
                    except Exception:
                        err_detail = r3.text[:500]
                    raise Exception(f"提交元数据失败({r3.status_code}): {err_detail}")
                r3.raise_for_status()
                res = r3.json()
                if res.get("result") != "ok":
                    return 500, {"error": f"提交失败: {res.get('msg', res)}"}

                if progress_cb:
                    progress_cb(100, "完成")
                dt = time.time() - t0
                mb = file_size / 1024 / 1024
                return 200, {"success": True, "size": file_size,
                             "time": dt, "speed": f"{mb/dt:.1f}MB/s"}
            except SlowUploadError as e:
                # 上传速度持续过低：中断本次上传，重新上传（本地文件已在，无需重新下载）。
                # 最多慢速重试 3 次，仍不恢复则按失败处理。
                if progress_cb:
                    progress_cb(-1, f"上传速度过慢({attempt}/3)，重新上传...")
                if cancel_check and cancel_check():
                    return 499, {"error": "[Canceled] 已取消"}
                if attempt >= 3:
                    return 500, {"error": f"[SlowUploadError] {e}"}
                time.sleep(5)
            except Exception as e:
                err_type = type(e).__name__
                err_detail = str(e)
                # 把详细错误通过进度回调显示出来
                if progress_cb:
                    progress_cb(-1, f"第{attempt}次失败[{err_type}]: {err_detail[:200]}")
                # 用户取消/已取消：立即中断，不再重试
                if cancel_check and (cancel_check() or "取消" in err_detail):
                    return 499, {"error": "[Canceled] 已取消"}
                if attempt >= 5:
                    return 500, {"error": f"[{err_type}] {err_detail}"}
                wait = min(2 ** attempt, 15)
                time.sleep(wait)


    def test(self):
        """测试Cookie是否有效"""
        try:
            r = self.s.get(f"{API_BASE}/user", timeout=10)
            if r.status_code == 200:
                return {"ok": True, "user": r.json()}
            return {"ok": False, "msg": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
