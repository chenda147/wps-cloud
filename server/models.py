# -*- coding: utf-8 -*-
"""
server/models.py
────────────────────────────────────────────────────────────────────────────
数据模型 / 外部服务客户端：
  - WPSClient：封装 WPS 365 云盘 API（浏览/下载/上传/重命名/建目录/搜索等）

仅包含与 WPS 服务交互的逻辑，不含 Flask 路由，依赖 server.state 的上传进度工具。
"""
import os
import json
import time
import hashlib

from .state import _UploadProgressReader, _update_task

API_BASE = "https://drive.kdocs.cn/api/v3"

_HASH_CHUNK = 1024 * 1024   # 计算哈希时的分块大小
_UPLOAD_CHUNK = 256 * 1024  # 上传时的分块大小


def _hash_file(path):
    """分块读取文件计算 md5/sha1，避免把大文件读入内存。"""
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
    return md5.hexdigest(), sha1.hexdigest()


class _FileProgressReader:
    """从磁盘文件分块读取并上报进度（内存占用恒定，适合大文件上传）。"""

    def __init__(self, path, task_id, total_size):
        self._fp = open(path, "rb")
        self._task_id = task_id
        self._total = total_size
        self._pos = 0
        self._t0 = time.time()

    def __len__(self):
        # requests 依赖 __len__ 推断 Content-Length，避免改用 chunked 传输
        return self._total

    def read(self, size=-1):
        if size is None or size < 0:
            size = _UPLOAD_CHUNK
        chunk = self._fp.read(size)
        if chunk:
            self._pos += len(chunk)
            elapsed = max(time.time() - self._t0, 0.01)
            _update_task(self._task_id, uploaded=self._pos, speed=self._pos / elapsed)
        return chunk

    def close(self):
        try:
            self._fp.close()
        except Exception:
            pass


class WPSClient:
    def __init__(self, session, csrf_token):
        self.s = session
        self.csrf = csrf_token

    def get_user_info(self):
        """获取 WPS 用户空间信息"""
        try:
            r = self.s.get(f"{API_BASE}/user", timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def get_files(self, group_id, parent_id=None, offset=0, count=200):
        params = {"offset": offset, "count": count}
        if parent_id:
            params["parentid"] = parent_id
        r = self.s.get(f"{API_BASE}/groups/{group_id}/files", params=params, timeout=20)
        return r.json().get("files", []) if r.status_code == 200 else []

    def get_all_files(self, group_id, parent_id=None):
        all_files, offset, page, seen, pages = [], 0, 200, set(), 0
        # 部分企业账号的分页参数会被服务端忽略，导致重复返回同一页；按 id 去重并限制页数
        while pages < 200:
            batch = self.get_files(group_id, parent_id, offset, page)
            added = 0
            for f in batch:
                fid = f.get("id")
                if fid in seen:
                    continue
                seen.add(fid)
                all_files.append(f)
                added += 1
            pages += 1
            if len(batch) < page or added == 0:
                break
            offset += page
        return all_files

    def get_file_info(self, group_id, file_id):
        dl_h = {"origin": "https://www.kdocs.cn", "accept": "*/*"}
        r = self.s.get(f"{API_BASE}/groups/{group_id}/files/{file_id}",
                       headers=dl_h, timeout=10)
        if r.status_code == 200:
            return r.json().get("file", r.json())
        # fallback
        for f in self.get_all_files(group_id):
            if str(f.get("id")) == str(file_id):
                return f
        return None

    def get_download_url(self, group_id, file_id):
        import sys as _sys
        dl_h = {
            "origin": "https://www.kdocs.cn",
            "referer": "https://www.kdocs.cn/",
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
        }
        r = self.s.get(f"{API_BASE}/groups/{group_id}/files/{file_id}/download",
                       headers=dl_h, timeout=15)
        if r.status_code != 200:
            print(f"[DOWNLOAD] status={r.status_code} body={r.text[:500]}", flush=True, file=_sys.stderr)
            raise Exception(f"获取下载链接失败: HTTP {r.status_code}")
        data = r.json()
        info = data.get("fileinfo", data)
        url = info.get("url") or data.get("url") or data.get("download_url")
        if not url:
            print(f"[DOWNLOAD] no url in: {json.dumps(data, ensure_ascii=False)[:500]}", flush=True, file=_sys.stderr)
            raise Exception(f"无法解析下载链接: {data}")
        return url

    def _headers(self, extra=None):
        h = {
            "X-Csrf-Token": self.csrf,
            "Origin": "https://365.kdocs.cn",
            "Referer": "https://365.kdocs.cn/",
        }
        if extra:
            h.update(extra)
        return h

    def delete_file(self, group_id, file_id):
        r = self.s.delete(f"{API_BASE}/groups/{group_id}/files/{file_id}", headers=self._headers(), timeout=15)
        return r.status_code, r.json() if r.text else {}

    def rename_file(self, group_id, file_id, new_name):
        r = self.s.patch(f"{API_BASE}/groups/{group_id}/files/{file_id}",
                         headers=self._headers({"Content-Type": "application/json"}),
                         json={"fname": new_name}, timeout=15)
        if r.status_code == 405:
            r = self.s.put(f"{API_BASE}/groups/{group_id}/files/{file_id}",
                           headers=self._headers({"Content-Type": "application/json"}),
                           json={"fname": new_name}, timeout=15)
        return r.status_code, r.json() if r.text else {}

    def batch_rename(self, group_id, items):
        results = []
        for item in items:
            code, resp = self.rename_file(group_id, item["id"], item["new_name"])
            results.append({"id": item["id"], "new_name": item["new_name"], "status": code, "resp": resp})
        return results

    # ═══════════════════════════════════════════════════════════
    #  文件上传 — 使用 v5 API 三步上传（与 wps-netdisk 一致）
    # ═══════════════════════════════════════════════════════════

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

    def upload_file(self, group_id, parent_id, file_data, file_name, file_size):
        """v5 三步上传: 创建任务 → 直传 KS3 → 提交元数据"""
        import sys as _sys
        DRIVE_V5 = "https://drive.kdocs.cn"
        try:
            pid = int(parent_id) if parent_id and str(parent_id).strip() else 0
        except ValueError:
            pid = 0
        md5_sum = hashlib.md5(file_data).hexdigest()
        sha1_sum = hashlib.sha1(file_data).hexdigest()
        try:
            gid = int(group_id)
        except ValueError:
            gid = group_id

        for attempt in range(1, 5):
            try:
                t0 = time.time()
                print(f"[UPLOAD] raw parent_id={parent_id!r} resolved pid={pid!r}", flush=True, file=_sys.stderr)
                body_1 = {
                    "groupid": gid, "parentid": pid, "parent_path": [],
                    "size": file_size, "name": file_name, "req_by_internal": False,
                    "client_stores": "ks3", "contenttype": "application/octet-stream",
                    "startswithfilename": file_name, "successactionstatus": 201,
                    "group_id": gid, "parent_id": pid, "file_id": 0,
                    "with_rapid": True, "tried_store": [],
                    "md5": md5_sum, "csrfmiddlewaretoken": self.csrf,
                }
                v5_h = {"content-type": "application/json", "origin": "https://www.kdocs.cn", "accept": "*/*"}
                print(f"[UPLOAD] step1: PUT {DRIVE_V5}/api/v5/files/upload/create_update", flush=True, file=_sys.stderr)
                r1 = self.s.put(f"{DRIVE_V5}/api/v5/files/upload/create_update",
                                data=json.dumps(body_1, ensure_ascii=False).encode("utf-8"),
                                headers=v5_h, timeout=60)
                r1.raise_for_status()
                plan = r1.json()
                print(f"[UPLOAD] step1 OK: store={plan.get('store','?')}", flush=True, file=_sys.stderr)

                store = plan.get("store", "ks3")
                headers2 = dict(plan["request"]["headers"])
                print(f"[UPLOAD] step2: PUT {plan['url'][:80]}...", flush=True, file=_sys.stderr)
                step2_timeout = max(60, int(file_size / 1024 / 500))
                step2_timeout = min(step2_timeout, 7200)
                r2 = self.s.put(plan["url"], data=file_data, headers=headers2,
                                timeout=(30, step2_timeout))
                r2.raise_for_status()
                spec = plan.get("response", {})
                key = self._extract_response(spec.get("args_key"), r2)
                etag = self._extract_response(spec.get("args_etag"), r2) or r2.headers.get("ETag")
                if not key:
                    key = sha1_sum
                print(f"[UPLOAD] step2 OK: key={key[:32] if key else '?'}...", flush=True, file=_sys.stderr)

                body_3 = {
                    "key": key, "groupid": gid, "parentid": pid, "parent_path": [],
                    "name": file_name, "isUpNewVer": False,
                    "etag": f'"{md5_sum}"', "store": store,
                    "size": file_size, "sha1": sha1_sum,
                    "apiErrorInfo": None, "csrfmiddlewaretoken": self.csrf,
                }
                print(f"[UPLOAD] step3: POST {DRIVE_V5}/api/v5/files/file", flush=True, file=_sys.stderr)
                r3 = self.s.post(f"{DRIVE_V5}/api/v5/files/file",
                                 data=json.dumps(body_3, ensure_ascii=False).encode("utf-8"),
                                 headers=v5_h, timeout=60)
                r3.raise_for_status()
                res = r3.json()
                if res.get("result") != "ok":
                    print(f"[UPLOAD] step3 FAIL: {json.dumps(res, ensure_ascii=False)[:500]}", flush=True, file=_sys.stderr)
                    return 500, {"error": f"提交失败: {res.get('msg', res)}"}
                dt = time.time() - t0
                mb = file_size / 1024 / 1024
                print(f"[UPLOAD] OK! {file_name} {mb:.1f}MB {dt:.1f}s ({mb/dt:.2f}MB/s) store={store}", flush=True, file=_sys.stderr)
                return 200, res
            except Exception as e:
                print(f"[UPLOAD] attempt {attempt}/4 FAIL: {e}", flush=True, file=_sys.stderr)
                if attempt >= 4:
                    return 500, {"error": str(e)}
                time.sleep(min(2 ** attempt, 15))

    def upload_file_with_progress(self, group_id, parent_id, file_data, file_name, file_size,
                                  task_id, file_path=None):
        """v5 三步上传并上报进度。

        file_path 不为空时从磁盘分块读取（推荐，大文件不占内存）；
        否则沿用原来的 bytes 模式。"""
        import sys as _sys
        DRIVE_V5 = "https://drive.kdocs.cn"
        try:
            pid = int(parent_id) if parent_id and str(parent_id).strip() else 0
        except ValueError:
            pid = 0
        if file_path:
            _update_task(task_id, stage="hash")
            md5_sum, sha1_sum = _hash_file(file_path)
        else:
            md5_sum = hashlib.md5(file_data).hexdigest()
            sha1_sum = hashlib.sha1(file_data).hexdigest()
        try:
            gid = int(group_id)
        except ValueError:
            gid = group_id

        for attempt in range(1, 5):
            try:
                t0 = time.time()
                _update_task(task_id, stage="step1")
                body_1 = {
                    "groupid": gid, "parentid": pid, "parent_path": [],
                    "size": file_size, "name": file_name, "req_by_internal": False,
                    "client_stores": "ks3", "contenttype": "application/octet-stream",
                    "startswithfilename": file_name, "successactionstatus": 201,
                    "group_id": gid, "parent_id": pid, "file_id": 0,
                    "with_rapid": True, "tried_store": [],
                    "md5": md5_sum, "csrfmiddlewaretoken": self.csrf,
                }
                v5_h = {"content-type": "application/json", "origin": "https://www.kdocs.cn", "accept": "*/*"}
                r1 = self.s.put(f"{DRIVE_V5}/api/v5/files/upload/create_update",
                                data=json.dumps(body_1, ensure_ascii=False).encode("utf-8"),
                                headers=v5_h, timeout=60)
                if r1.status_code != 200:
                    # 暴露 WPS 返回的真实错误信息，便于定位 InvalidArgument
                    try:
                        _detail = r1.json()
                    except Exception:
                        _detail = r1.text[:500]
                    _cookie_has_sid = "wps_sid" in self.s.cookies.get_dict()
                    print(f"[UPLOAD] step1 HTTP {r1.status_code}: {_detail}", flush=True, file=_sys.stderr)
                    print(f"[UPLOAD] diag: pid={pid!r} gid={gid!r} csrf_head={str(self.csrf)[:8]!r} "
                          f"cookie_sid={_cookie_has_sid} url={r1.url}", flush=True, file=_sys.stderr)
                    print(f"[UPLOAD] diag body_1={json.dumps(body_1, ensure_ascii=False)}", flush=True, file=_sys.stderr)
                    raise Exception(f"创建上传任务失败({r1.status_code}): {_detail}")
                r1.raise_for_status()
                plan = r1.json()

                store = plan.get("store", "ks3")
                headers2 = dict(plan["request"]["headers"])
                step2_timeout = max(60, int(file_size / 1024 / 500))
                step2_timeout = min(step2_timeout, 7200)
                _update_task(task_id, stage="step2")
                if file_path:
                    reader = _FileProgressReader(file_path, task_id, file_size)
                    try:
                        r2 = self.s.put(plan["url"], data=reader, headers=headers2,
                                        timeout=(30, step2_timeout))
                    finally:
                        reader.close()
                else:
                    reader = _UploadProgressReader(file_data, task_id, file_size)
                    r2 = self.s.put(plan["url"], data=reader, headers=headers2,
                                    timeout=(30, step2_timeout))
                r2.raise_for_status()
                spec = plan.get("response", {})
                key = self._extract_response(spec.get("args_key"), r2)
                etag = self._extract_response(spec.get("args_etag"), r2) or r2.headers.get("ETag")
                if not key:
                    key = sha1_sum

                _update_task(task_id, stage="step3")
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
                r3.raise_for_status()
                res = r3.json()
                if res.get("result") != "ok":
                    _update_task(task_id, done=True, success=False, error=f"提交失败: {res.get('msg', res)}")
                    return 500, {"error": f"提交失败: {res.get('msg', res)}"}
                dt = time.time() - t0
                mb = file_size / 1024 / 1024
                print(f"[UPLOAD] OK! {file_name} {mb:.1f}MB {dt:.1f}s ({mb/dt:.2f}MB/s) store={store}", flush=True, file=_sys.stderr)
                _update_task(task_id, done=True, success=True)
                return 200, res
            except Exception as e:
                print(f"[UPLOAD] attempt {attempt}/4 FAIL: {e}", flush=True, file=_sys.stderr)
                if attempt >= 4:
                    _update_task(task_id, done=True, success=False, error=str(e))
                    return 500, {"error": str(e)}
                time.sleep(min(2 ** attempt, 15))

    def create_folder(self, group_id, parent_id, folder_name):
        import sys as _sys
        DRIVE_V5 = "https://drive.kdocs.cn"
        gid = int(group_id) if str(group_id).isdigit() else group_id
        pid = int(parent_id) if parent_id and str(parent_id).strip() else 0
        h_json = {"content-type": "application/json", "origin": "https://www.kdocs.cn",
                  "accept": "*/*", "Referer": "https://www.kdocs.cn/", "X-Csrf-Token": self.csrf}

        body1 = {"key": "", "groupid": gid, "parentid": pid, "parent_path": [],
                 "name": folder_name, "ftype": "folder",
                 "isUpNewVer": False, "size": 0, "sha1": "", "store": 0,
                 "csrfmiddlewaretoken": self.csrf}
        r = self.s.post(f"{DRIVE_V5}/api/v5/files/file",
                        data=json.dumps(body1, ensure_ascii=False).encode("utf-8"),
                        headers=h_json, timeout=15)
        print(f"[MKDIR-1] v5/file name status={r.status_code} body={r.text[:400]}", flush=True, file=_sys.stderr)
        if r.status_code in (200, 201):
            return r.status_code, r.json() if r.text else {}

        body2 = {"key": "", "groupid": gid, "parentid": pid, "parent_id": parent_id or "",
                 "name": folder_name, "ftype": "folder", "isUpNewVer": False,
                 "size": 0, "sha1": "", "store": 0, "csrfmiddlewaretoken": self.csrf}
        r = self.s.post(f"{DRIVE_V5}/api/v5/files/file",
                        data=json.dumps(body2, ensure_ascii=False).encode("utf-8"),
                        headers=h_json, timeout=15)
        print(f"[MKDIR-2] v5/file parent_id status={r.status_code} body={r.text[:400]}", flush=True, file=_sys.stderr)
        if r.status_code in (200, 201):
            return r.status_code, r.json() if r.text else {}

        body3 = {"name": folder_name, "parentid": pid, "groupid": gid,
                 "csrfmiddlewaretoken": self.csrf}
        r = self.s.post(f"{DRIVE_V5}/api/v5/files/folder",
                        data=json.dumps(body3, ensure_ascii=False).encode("utf-8"),
                        headers=h_json, timeout=15)
        print(f"[MKDIR-3] v5/folder POST status={r.status_code} body={r.text[:400]}", flush=True, file=_sys.stderr)
        if r.status_code in (200, 201):
            return r.status_code, r.json() if r.text else {}

        r = self.s.post(f"{API_BASE}/groups/{group_id}/files",
                        json={"name": folder_name, "ftype": "folder", "parent_id": parent_id or "",
                              "csrfmiddlewaretoken": self.csrf},
                        headers=h_json, timeout=15)
        print(f"[MKDIR-4] v3 name status={r.status_code} body={r.text[:400]}", flush=True, file=_sys.stderr)
        if r.status_code in (200, 201):
            return r.status_code, r.json() if r.text else {}

        r = self.s.post(f"{API_BASE}/groups/{group_id}/files",
                        data={"name": folder_name, "ftype": "folder", "parent_id": parent_id or ""},
                        headers={"X-Csrf-Token": self.csrf, "Origin": "https://www.kdocs.cn",
                                 "Referer": "https://www.kdocs.cn/"},
                        timeout=15)
        print(f"[MKDIR-5] v3 form status={r.status_code} body={r.text[:400]}", flush=True, file=_sys.stderr)
        if r.status_code in (200, 201):
            return r.status_code, r.json() if r.text else {}

        try:
            opt = self.s.options(f"{API_BASE}/groups/{group_id}/files", headers=h_json, timeout=10)
            allow = opt.headers.get("Allow", "")
            print(f"[MKDIR-6] OPTIONS Allow={allow}", flush=True, file=_sys.stderr)
        except Exception:
            pass
        return r.status_code, r.json() if r.text else {}

    def move_file(self, group_id, file_id, new_parent_id):
        r = self.s.patch(f"{API_BASE}/groups/{group_id}/files/{file_id}",
                         headers=self._headers({"Content-Type": "application/json"}),
                         json={"parent_id": new_parent_id or ""}, timeout=15)
        return r.status_code, r.json() if r.text else {}

    def copy_file(self, group_id, file_id, target_parent_id):
        try:
            r = self.s.post(f"{API_BASE}/groups/{group_id}/files/{file_id}/copy",
                            headers=self._headers({"Content-Type": "application/json"}),
                            json={"parent_id": target_parent_id or ""}, timeout=15)
            return r.status_code, r.json() if r.text else {}
        except Exception:
            r = self.s.post(f"{API_BASE}/groups/{group_id}/files",
                            headers=self._headers({"Content-Type": "application/json"}),
                            json={"file_id": file_id, "parent_id": target_parent_id or ""}, timeout=15)
            return r.status_code, r.json() if r.text else {}

    def search_files(self, group_id, query, parent_id=None, max_depth=5, max_results=200):
        results = []
        self._search_recurse(group_id, query.lower(), parent_id, 0, max_depth, results, max_results)
        return results

    def _search_recurse(self, group_id, query, parent_id, depth, max_depth, results, max_results):
        if depth > max_depth or len(results) >= max_results:
            return
        try:
            files = self.get_all_files(group_id, parent_id)
            for f in files:
                if len(results) >= max_results:
                    return
                name = (f.get("fname") or f.get("name") or "").lower()
                ftype = f.get("ftype") or "file"
                if query in name:
                    results.append(f)
                if ftype == "folder":
                    self._search_recurse(group_id, query, f.get("id"), depth + 1, max_depth, results, max_results)
        except Exception:
            pass

    def get_groups(self):
        all_groups, offset, seen, pages = [], 0, set(), 0
        # 企业账号下 /groups 会忽略 offset、反复返回同一批组，必须去重并限制页数，
        # 否则会无限请求且接口永不返回。
        while pages < 100:
            r = self.s.get(f"{API_BASE}/groups", params={"count": 200, "offset": offset}, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            groups = data.get("groups") or []
            added = 0
            for g in groups:
                gid = g.get("id")
                if gid in seen:
                    continue
                seen.add(gid)
                all_groups.append(g)
                added += 1
            pages += 1
            if not groups or data.get("has_next") is not True or added == 0:
                break
            offset += len(groups)
        return all_groups
