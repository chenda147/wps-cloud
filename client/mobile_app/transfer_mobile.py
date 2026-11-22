"""
手机版传输引擎：139/夸克分享下载 → 本地存储 → WPS上传
移植自 PC 版 transfer_engine.py，针对 Android/Kivy 环境改造：
- 7z/rar/zip 解压优先调用 APK 内打包的 7zz 静态二进制（7-Zip 26.02 arm64 静态版），
  zip/tar 同时保留标准库（zipfile/tarfile）兜底，7z 可用 py7zr 兜底
- RAR 解压依赖 7zz（7-Zip 可解 RAR4/RAR5），未找到 7zz 时明确提示
- 下载/上传逻辑与 PC 版一致（流式下载、WPS 多级路径、重命名、断点续传）
"""
import os
import time
import shutil
import zipfile
import tarfile
import subprocess
import threading
import queue as _queue
import requests
from yidong_core import YidongShareClient
from quark_core import QuarkShareClient
from wps_uploader import WPSUploader

VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
              '.m4v', '.mpg', '.mpeg', '.3gp', '.ts', '.rmvb', '.vob',
              '.divx', '.xvid', '.asf', '.f4v']

ARCHIVE_EXTS = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
                '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz',
                '.iso', '.cab'}

_VIDEO_SET = {e.lower() for e in VIDEO_EXTS}

API_BASE = "https://drive.kdocs.cn/api/v3"

# 全局上传串行锁：即使并发开多线程，同一时刻也只允许 1 个文件上传，
# 实现 流水线(下载1→上传1∥下载2→上传2∥下载3) 语义，避免多任务同时抢占 WPS 上传
_UPLOAD_LOCK = threading.Lock()


def detect_cloud_type(share_url: str) -> str:
    """根据分享链接自动识别网盘类型。Returns: 'quark' | 'yidong'"""
    u = (share_url or "").lower()
    if "quark.cn" in u:
        return "quark"
    return "yidong"


def build_src_client(clouds, share_url, cloud_id=None):
    """根据配置中的云盘连接列表，构造对应的下载源客户端。"""
    if not clouds:
        raise ValueError("未配置任何云盘连接")
    if cloud_id:
        for c in clouds:
            if c.get("id") == cloud_id:
                return _make_client(c)
        raise ValueError(f"未找到指定的云盘连接: {cloud_id}")
    ctype = detect_cloud_type(share_url)
    for c in clouds:
        if c.get("type") == ctype and c.get("is_primary"):
            return _make_client(c)
    for c in clouds:
        if c.get("type") == ctype:
            return _make_client(c)
    if ctype == "quark":
        raise ValueError(
            "未配置 quark（夸克网盘）类型的云盘连接。\n"
            "请在「夸克网盘」设置中填写夸克 Cookie 后保存，再重新检查。"
        )
    raise ValueError(
        f"未配置 {ctype} 类型的云盘连接。请在「云盘连接」中添加对应账号后重试。"
    )


def _make_client(cfg: dict):
    ctype = cfg.get("type")
    if ctype == "quark":
        cookie = cfg.get("cookie", "")
        if not cookie:
            raise ValueError(f"夸克连接「{cfg.get('name','')}」缺少 cookie")
        return QuarkShareClient(cookie)
    else:
        token = cfg.get("token", "")
        if not token:
            raise ValueError(f"139连接「{cfg.get('name','')}」缺少 token")
        return YidongShareClient(token)


class TransferEngine:
    """单文件传输引擎（手机版）"""

    def __init__(self, yidong_token, wps_cookie, save_dir,
                 wps_parent_id="0", wps_group_id="2203072050",
                 src_client=None, upload_slow_retry=None):
        if src_client is not None:
            self.src = src_client
        else:
            self.src = YidongShareClient(yidong_token)
        self.wps = WPSUploader(wps_cookie, wps_group_id, slow_retry=upload_slow_retry)
        self.save_dir = save_dir
        self.wps_parent_id = wps_parent_id
        self.wps_group_id = wps_group_id

    # ── WPS 文件夹路径管理 ──

    def _wps_list_files(self, parent_id):
        import json as _json
        files, offset, seen = [], 0, set()
        for _ in range(100):
            params = {"offset": offset, "count": 500}
            if parent_id and str(parent_id) not in ("0", "None", ""):
                params["parentid"] = str(parent_id)
            try:
                r = self.wps.s.get(f"{API_BASE}/groups/{self.wps_group_id}/files",
                                   params=params, timeout=20)
                if r.status_code != 200:
                    break
                batch = r.json().get("files", [])
                # 按 id 去重：部分账号会忽略 offset 反复返回同一批，避免死循环
                new_items = [f for f in batch if str(f.get("id")) not in seen]
                if not new_items:
                    break
                for f in new_items:
                    seen.add(str(f.get("id")))
                files.extend(new_items)
                if len(batch) < 500:
                    break
                offset += 500
            except Exception:
                break
        return files

    def _wps_find_folder(self, parent_id, folder_name):
        for f in self._wps_list_files(parent_id):
            if f.get("ftype") == "folder":
                fname = (f.get("fname") or f.get("name") or "").strip()
                if fname == folder_name:
                    return str(f.get("id", ""))
        return None

    def _wps_create_folder(self, parent_id, folder_name):
        import json as _json
        DRIVE_V5 = "https://drive.kdocs.cn"
        try:
            gid = int(self.wps_group_id) if str(self.wps_group_id).isdigit() else self.wps_group_id
        except (ValueError, TypeError):
            gid = self.wps_group_id
        pid = int(parent_id) if parent_id and str(parent_id).strip().isdigit() else 0

        h_json = {"content-type": "application/json", "origin": "https://www.kdocs.cn",
                  "accept": "*/*", "Referer": "https://www.kdocs.cn/", "X-Csrf-Token": self.wps.csrf}

        body1 = {"key": "", "groupid": gid, "parentid": pid, "parent_path": [],
                 "name": folder_name, "ftype": "folder",
                 "isUpNewVer": False, "size": 0, "sha1": "", "store": 0,
                 "csrfmiddlewaretoken": self.wps.csrf}
        try:
            r = self.wps.s.post(f"{DRIVE_V5}/api/v5/files/file",
                                data=_json.dumps(body1, ensure_ascii=False).encode("utf-8"),
                                headers=h_json, timeout=15)
            if r.status_code in (200, 201):
                data = r.json() if r.text else {}
                fid = data.get("file", {}).get("id") if isinstance(data.get("file"), dict) else None
                return str(fid) if fid else str(data.get("id", ""))
        except Exception:
            pass

        body2 = {"key": "", "groupid": gid, "parentid": pid, "parent_id": str(parent_id or ""),
                 "name": folder_name, "ftype": "folder", "isUpNewVer": False,
                 "size": 0, "sha1": "", "store": 0, "csrfmiddlewaretoken": self.wps.csrf}
        try:
            r = self.wps.s.post(f"{DRIVE_V5}/api/v5/files/file",
                                data=_json.dumps(body2, ensure_ascii=False).encode("utf-8"),
                                headers=h_json, timeout=15)
            if r.status_code in (200, 201):
                data = r.json() if r.text else {}
                fid = data.get("file", {}).get("id") if isinstance(data.get("file"), dict) else None
                return str(fid) if fid else str(data.get("id", ""))
        except Exception:
            pass

        body3 = {"name": folder_name, "parentid": pid, "groupid": gid,
                 "csrfmiddlewaretoken": self.wps.csrf}
        try:
            r = self.wps.s.post(f"{DRIVE_V5}/api/v5/files/folder",
                                data=_json.dumps(body3, ensure_ascii=False).encode("utf-8"),
                                headers=h_json, timeout=15)
            if r.status_code in (200, 201):
                data = r.json() if r.text else {}
                fid = data.get("file", {}).get("id") if isinstance(data.get("file"), dict) else None
                return str(fid) if fid else str(data.get("id", ""))
        except Exception:
            pass

        try:
            r = self.wps.s.post(f"{API_BASE}/groups/{self.wps_group_id}/files",
                                json={"name": folder_name, "ftype": "folder",
                                      "parent_id": str(parent_id or ""),
                                      "csrfmiddlewaretoken": self.wps.csrf},
                                headers=h_json, timeout=15)
            if r.status_code in (200, 201):
                data = r.json() if r.text else {}
                fid = data.get("file", {}).get("id") if isinstance(data.get("file"), dict) else None
                return str(fid) if fid else str(data.get("id", ""))
        except Exception:
            pass

        return None

    def _wps_resolve_path(self, path_str, log_cb=None):
        if not path_str or not path_str.strip():
            return self.wps_parent_id
        parts = [p.strip() for p in path_str.replace("\\", "/").split("/") if p.strip()]
        if not parts:
            return self.wps_parent_id
        current_id = self.wps_parent_id
        for part in parts:
            existing = self._wps_find_folder(current_id, part)
            if not existing:
                return None
            current_id = existing
        return current_id

    def _wps_ensure_path(self, path_str, log_cb=None):
        if not path_str or not path_str.strip():
            return self.wps_parent_id
        parts = [p.strip() for p in path_str.replace("\\", "/").split("/") if p.strip()]
        if not parts:
            return self.wps_parent_id
        current_id = self.wps_parent_id
        for part in parts:
            existing = self._wps_find_folder(current_id, part)
            if existing:
                current_id = existing
                if log_cb:
                    log_cb(f"找到文件夹: {part} (id={existing})")
            else:
                new_id = self._wps_create_folder(current_id, part)
                if new_id:
                    current_id = new_id
                    if log_cb:
                        log_cb(f"创建文件夹: {part} (id={new_id})")
                else:
                    if log_cb:
                        log_cb(f"警告: 创建文件夹失败 '{part}'，文件将上传到上级目录")
                    break
        return current_id

    def get_share_files(self, share_url, share_passwd=""):
        result = self.src.list_share_files_recursive(share_url, share_passwd)
        if not result.get("ok"):
            return None, result.get("msg", "获取文件列表失败")
        files = [f for f in result.get("items", []) if not f.get("is_dir")]
        if not files:
            return None, "分享链接中没有文件"
        files.sort(key=lambda x: x.get("size", 0), reverse=True)
        return files, None

    def _pick_file(self, files, file_name=""):
        if not files:
            return None
        if file_name and file_name.strip():
            fn = file_name.strip()
            exact = [f for f in files if f.get("name", "") == fn]
            if exact:
                return max(exact, key=lambda f: f.get("size", 0))
            fuzzy = [f for f in files if fn in f.get("name", "") or f.get("name", "") in fn]
            if fuzzy:
                return max(fuzzy, key=lambda f: f.get("size", 0))
            return None
        return files[0]

    def download_file(self, share_url, file_info, share_passwd="",
                      progress_cb=None, cancel_check=None):
        """下载单个文件到本地存储（流式 + 断点续传）"""
        file_name = file_info.get("name", "download")
        file_size = file_info.get("size", 0)
        file_id = file_info.get("id", "")
        share_path = file_info.get("path", "")

        dl_result = self.src.get_share_download_url(
            share_url, file_id, file_name, share_path, share_passwd)
        if not dl_result.get("ok"):
            return None, f"获取下载链接失败: {dl_result.get('msg')}"

        cdn_url = dl_result["url"]
        local_path = os.path.join(self.save_dir, file_name)
        os.makedirs(self.save_dir, exist_ok=True)

        downloaded = 0
        if os.path.exists(local_path):
            downloaded = os.path.getsize(local_path)
            if downloaded >= file_size and file_size > 0:
                if progress_cb:
                    progress_cb(downloaded, file_size, "已完成(跳过)")
                return local_path, None

        from requests.adapters import HTTPAdapter
        import socket as _socket

        referer = getattr(self.src, "download_referer", "https://pan.quark.cn/")
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
            "Origin": referer.rstrip("/"),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        headers.update(dl_result.get("headers", {}))
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"

        max_retry = 30
        retry = 0
        current_url = cdn_url

        while downloaded < file_size and retry <= max_retry:
            if cancel_check and cancel_check():
                return None, "已取消"
            try:
                session = requests.Session()
                adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1)
                try:
                    adapter.socket_options = [
                        (_socket.SOL_SOCKET, _socket.SO_RCVBUF, 8 * 1024 * 1024),
                        (_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1),
                        (_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1),
                    ]
                except Exception:
                    pass
                session.mount('https://', adapter)
                session.mount('http://', adapter)

                r = session.get(current_url, headers=headers, stream=True,
                                timeout=(30, 60), allow_redirects=True)
                if r.status_code not in (200, 206):
                    r.close()
                    retry += 1
                    dl_result = self.src.get_share_download_url(
                        share_url, file_id, file_name, share_path, share_passwd)
                    if dl_result.get("ok"):
                        current_url = dl_result["url"]
                        headers.update(dl_result.get("headers", {}))
                        if downloaded > 0:
                            headers["Range"] = f"bytes={downloaded}-"
                    time.sleep(2)
                    continue

                mode = "ab" if downloaded > 0 else "wb"
                t0 = time.time()
                last_cb = 0
                start_downloaded = downloaded
                if progress_cb:
                    progress_cb(downloaded, file_size, "开始下载")

                with open(local_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                        if cancel_check and cancel_check():
                            r.close()
                            return None, "已取消"
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if progress_cb and now - last_cb > 0.5:
                                el = max(0.1, now - t0)
                                spd = (downloaded - start_downloaded) / el
                                progress_cb(downloaded, file_size,
                                            f"{spd/1024/1024:.1f}MB/s")
                                last_cb = now
                r.close()
                retry = 0
            except Exception as e:
                retry += 1
                if retry > max_retry:
                    return None, f"下载失败(重试{retry}次): {e}"
                try:
                    dl_result = self.src.get_share_download_url(
                        share_url, file_id, file_name, share_path, share_passwd)
                    if dl_result.get("ok"):
                        current_url = dl_result["url"]
                        headers.update(dl_result.get("headers", {}))
                        if downloaded > 0:
                            headers["Range"] = f"bytes={downloaded}-"
                except Exception:
                    pass
                time.sleep(2)

        if downloaded < file_size:
            return None, f"下载不完整: {downloaded}/{file_size}"

        if progress_cb:
            progress_cb(downloaded, file_size, "下载完成")
        return local_path, None

    def upload_file(self, local_path, file_name, progress_cb=None, cancel_check=None):
        if cancel_check and cancel_check():
            return False, "已取消"
        file_size = os.path.getsize(local_path)

        def _upload_progress(pct, speed):
            if progress_cb:
                if pct == -1:
                    progress_cb(-1, file_size, speed)
                    return
                uploaded = int(file_size * pct / 100 + 0.5)
                progress_cb(uploaded, file_size, f"上传 {pct}% {speed}")
            if cancel_check and cancel_check():
                raise Exception("已取消")

        status, result = self.wps.upload_file_from_disk(
            local_path, file_name, self.wps_parent_id, _upload_progress, cancel_check)
        if status in (200, 201, 204):
            return True, None
        return False, result.get("error", "上传失败")

    # ── 压缩包检测 & 解压（纯 Python，无外部工具） ──

    @staticmethod
    def _is_archive(filepath):
        name = os.path.basename(filepath).lower()
        for ae in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz'):
            if name.endswith(ae):
                return True
        ext = os.path.splitext(name)[1]
        if ext in ARCHIVE_EXTS:
            return True
        try:
            with open(filepath, 'rb') as f:
                head = f.read(8)
            if head[:4] == b'PK\x03\x04':
                return True
            if head[:4] in (b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01'):
                return True
            if head[:6] == b"7z\xbc\xaf'\x1c":
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _find_7zz():
        """定位 APK 内打包的 7zz 静态二进制（用于解压 7z/rar/zip）。
        查找顺序：应用目录 tools/7zz → 脚本同目录 7zz → ANDROID_PRIVATE → 系统 PATH。"""
        cands = []
        here = os.path.dirname(os.path.abspath(__file__))
        cands.append(os.path.join(here, "tools", "7zz"))
        cands.append(os.path.join(here, "7zz"))
        private = os.environ.get("ANDROID_PRIVATE", "")
        if private:
            cands.append(os.path.join(private, "files", "app", "tools", "7zz"))
            cands.append(os.path.join(private, "files", "app", "7zz"))
        for c in cands:
            if os.path.isfile(c):
                try:
                    os.chmod(c, 0o755)
                except Exception:
                    pass
                return c
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, "7zz")
            if os.path.isfile(p):
                return p
        return ""

    @staticmethod
    def _extract_with_7zz(seven_path, archive_path, extract_dir):
        """调用 `7zz x` 解压（支持 7z/rar/zip 等）。成功返回 True。"""
        try:
            cmd = [seven_path, "x", archive_path, f"-o{extract_dir}", "-y"]
            sub = subprocess.run(cmd, capture_output=True, timeout=1800)
            return sub.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _extract_and_find_videos(archive_path, progress_cb=None, _depth=0):
        """解压压缩包并返回其中的视频文件列表（支持嵌套递归）。
        Returns: (video_files, error, extract_dir)
        """
        if _depth > 3:
            return [], "压缩包嵌套层数过深（>3层），已中止", ""
        archive_name = os.path.basename(archive_path)
        extract_dir = os.path.join(os.path.dirname(archive_path),
                                   f"_extract_{os.path.splitext(archive_name)[0]}")
        os.makedirs(extract_dir, exist_ok=True)

        if progress_cb:
            progress_cb("extract", 0, 0, f"正在解压: {archive_name}")

        try:
            name_lower = archive_name.lower()
            ext = os.path.splitext(archive_name)[1].lower()

            # ── ZIP（标准库优先，失败用 7zz 兜底）──
            if name_lower.endswith('.zip') or ext == '.zip':
                zip_ok = False
                try:
                    with zipfile.ZipFile(archive_path, 'r') as zf:
                        for member in zf.infolist():
                            member_path = os.path.normpath(member.filename)
                            if member_path.startswith('..') or os.path.isabs(member_path):
                                continue
                        zf.extractall(extract_dir)
                    zip_ok = True
                except Exception:
                    zip_ok = False
                if not zip_ok:
                    seven = TransferEngine._find_7zz()
                    if seven and TransferEngine._extract_with_7zz(
                            seven, archive_path, extract_dir):
                        if progress_cb:
                            progress_cb("extract", 0, 0, "zipfile 失败，改用 7zz 解压成功")
                        zip_ok = True
                if not zip_ok:
                    return [], "zip 解压失败（标准库与 7zz 均失败，可能加密或已损坏）", extract_dir

            # ── TAR ──
            elif name_lower.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz',
                                       '.tgz', '.tbz2', '.txz')):
                if not os.path.isfile(archive_path):
                    return [], f"文件不存在: {archive_path}", extract_dir
                if os.path.getsize(archive_path) == 0:
                    return [], "压缩包文件为空（0 字节），可能下载失败", extract_dir
                if progress_cb:
                    progress_cb("extract", 0, 0, "正在 tarfile 解压...")
                tar_ok = False
                for mode in ('r:', 'r:gz', 'r:bz2', 'r:xz', 'r:*'):
                    try:
                        with tarfile.open(archive_path, mode) as tf:
                            def _safe_members():
                                for m in tf.getmembers():
                                    p = os.path.normpath(m.name)
                                    if p.startswith('..') or os.path.isabs(p):
                                        continue
                                    yield m
                            tf.extractall(extract_dir, members=list(_safe_members()))
                        tar_ok = True
                        if progress_cb:
                            progress_cb("extract", 1, 1, f"tarfile({mode}) 解压完成")
                        break
                    except Exception:
                        continue
                if not tar_ok:
                    return [], "解压失败: tarfile 所有模式均失败", extract_dir

            # ── 7z（7zz 二进制优先，py7zr 兜底）──
            elif ext == '.7z':
                seven = TransferEngine._find_7zz()
                seven_ok = False
                if seven:
                    if progress_cb:
                        progress_cb("extract", 0, 0, "正在用 7zz 解压 .7z ...")
                    seven_ok = TransferEngine._extract_with_7zz(
                        seven, archive_path, extract_dir)
                if seven_ok:
                    pass
                else:
                    try:
                        import py7zr
                    except ImportError:
                        return [], ("解压失败: 未找到 7zz 且缺少 py7zr 库，"
                                    "无法解压 .7z 文件"), extract_dir
                    try:
                        with py7zr.SevenZipFile(archive_path, 'r', password='') as zf:
                            zf.extractall(extract_dir)
                    except Exception as e:
                        return [], f"7z 解压失败: {e}", extract_dir

            # ── RAR（用打包的 7zz 静态二进制解压，可解 RAR4/RAR5）──
            elif name_lower.endswith('.rar') or ext == '.rar':
                seven = TransferEngine._find_7zz()
                if not seven:
                    return [], ("RAR 解压失败: 手机端未打包 7zz 解压器，"
                                "请使用 PC 客户端处理该压缩包，或改用 zip/7z 格式分享。"), extract_dir
                if progress_cb:
                    progress_cb("extract", 0, 0, "正在用 7zz 解压 .rar ...")
                if not TransferEngine._extract_with_7zz(seven, archive_path, extract_dir):
                    return [], "RAR 解压失败（7zz 返回错误，可能已加密或格式过新）", extract_dir

            # ── 其他格式 ──
            elif ext in ('.iso', '.cab'):
                return [], f"手机端暂不支持 {ext} 格式解压，请使用 PC 客户端处理。", extract_dir
            else:
                return [], f"不支持的压缩格式: {ext}", extract_dir

            # 查找视频文件
            video_files = TransferEngine._find_videos(extract_dir)
            if not video_files:
                nested = TransferEngine._find_archives(extract_dir)
                if nested:
                    all_videos = []
                    total_nested = len(nested)
                    for i, np in enumerate(nested):
                        if progress_cb:
                            progress_cb("extract", i, total_nested,
                                        f"解压嵌套 [{i+1}/{total_nested}]: {os.path.basename(np)}")
                        nv, ne, _ = TransferEngine._extract_and_find_videos(
                            np, progress_cb, _depth + 1)
                        if nv:
                            all_videos.extend(nv)
                    if all_videos:
                        if progress_cb:
                            progress_cb("extract", 1, 1,
                                        f"递归解压完成，找到 {len(all_videos)} 个视频")
                        return all_videos, None, extract_dir
                return [], "压缩包内未找到视频文件", extract_dir

            if progress_cb:
                progress_cb("extract", 1, 1, f"解压完成，找到 {len(video_files)} 个视频")
            return video_files, None, extract_dir

        except Exception as e:
            return [], f"解压异常: {e}", extract_dir

    @staticmethod
    def _find_videos(root_dir):
        results = []
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in _VIDEO_SET:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root_dir)
                    results.append((full, rel))
        return results

    @staticmethod
    def _find_archives(root_dir):
        archives = []
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if TransferEngine._is_archive(full):
                    archives.append(full)
        return archives

    # ── 核心传输流程 ──

    def transfer(self, share_url, share_passwd="", progress_cb=None,
                 cancel_check=None, upload_after_download=True,
                 rename_to="", wps_path="", delete_after_upload=False,
                 file_name=""):
        """完整传输：获取文件 → 下载 → (重命名) → 上传 → (删除本地文件)"""
        if progress_cb:
            progress_cb("list", 0, 0, "获取文件列表...")
        files, err = self.get_share_files(share_url, share_passwd)
        if err:
            if progress_cb:
                progress_cb("error", 0, 0, err)
            return False, err

        f = self._pick_file(files, file_name)
        if not f:
            if progress_cb:
                progress_cb("error", 0, 0, f"未找到文件: {file_name}")
            return False, f"未找到文件: {file_name}"
        file_size = f.get("size", 0)
        orig_name = f.get("name", "file")

        try:
            for entry in os.listdir(self.save_dir):
                if entry.startswith('_extract_') and os.path.isdir(
                        os.path.join(self.save_dir, entry)):
                    shutil.rmtree(os.path.join(self.save_dir, entry), ignore_errors=True)
        except Exception:
            pass

        if rename_to and rename_to.strip():
            rt = rename_to.strip()
            _, o_ext = os.path.splitext(orig_name)
            _, n_ext = os.path.splitext(rt)
            if not n_ext and o_ext:
                rt = rt + o_ext
            pre_check = os.path.join(self.save_dir, rt)
            if os.path.exists(pre_check) and os.path.getsize(pre_check) > 0:
                if progress_cb:
                    progress_cb("download", os.path.getsize(pre_check), file_size,
                                "已有本地文件，跳过下载")
                video_files = [(pre_check, rt)]
                for entry in os.listdir(self.save_dir):
                    ep = os.path.join(self.save_dir, entry)
                    if entry.startswith("_extract_") and os.path.isdir(ep):
                        try:
                            shutil.rmtree(ep, ignore_errors=True)
                        except Exception:
                            pass
                return self._do_upload(video_files, progress_cb,
                                       upload_after_download, rename_to,
                                       wps_path, delete_after_upload,
                                       cancel_check)

        if progress_cb:
            progress_cb("download", 0, file_size, "开始下载...")

        def _dl_cb(dl, total, speed):
            if progress_cb:
                progress_cb("download", dl, total, speed)

        local_path, err = self.download_file(
            share_url, f, share_passwd, _dl_cb, cancel_check)
        if err:
            if progress_cb:
                progress_cb("error", 0, 0, f"下载失败: {err}")
            return False, err

        extract_dir = None
        video_files = [(local_path, orig_name)]

        if self._is_archive(local_path):
            vfs, ext_err, extract_dir = self._extract_and_find_videos(local_path, progress_cb)
            if ext_err:
                if progress_cb:
                    progress_cb("error", 0, 0, f"解压失败: {ext_err}")
                try:
                    os.remove(local_path)
                except Exception:
                    pass
                if extract_dir and os.path.isdir(extract_dir):
                    shutil.rmtree(extract_dir, ignore_errors=True)
                return False, ext_err
            if not vfs:
                if progress_cb:
                    progress_cb("error", 0, 0, "压缩包内未找到视频文件")
                try:
                    os.remove(local_path)
                except Exception:
                    pass
                if extract_dir and os.path.isdir(extract_dir):
                    shutil.rmtree(extract_dir, ignore_errors=True)
                return False, "压缩包内未找到视频文件"
            try:
                os.remove(local_path)
            except Exception:
                pass
            video_files = vfs
            if progress_cb:
                progress_cb("extract", 1, 1,
                            f"解压完成, {len(video_files)} 个视频" +
                            (" (仅上传首个)" if not rename_to and len(video_files) > 1 else ""))

        ok, err = self._do_upload(video_files, progress_cb, upload_after_download,
                                  rename_to, wps_path, delete_after_upload, cancel_check)

        if extract_dir and os.path.isdir(extract_dir):
            try:
                shutil.rmtree(extract_dir)
                if progress_cb:
                    progress_cb("rename", 0, 0, "已清理解压临时文件")
            except Exception:
                pass

        return ok, err

    def transfer_batch(self, share_url, share_passwd="", file_names=None, progress_cb=None,
                       cancel_check=None, rename_map=None, wps_path="",
                       delete_after_upload=True, prefetch=1, per_file_done_cb=None):
        """流水线批量传输：下载与上传并行（上传本集时并行下载下一集）。

        以 prefetch=1 为例：
         下载 1 完成 → 开始上传 1
         上传 1 期间 → 并行下载 2（下载完放入队列等待）
         1 上传完   → 开始上传 2，同时继续下载 3 ……
        per_file_done_cb(idx, file_name, ok, err)：每个文件处理结束后调用一次
        （下载失败/解压失败/上传完成均触发），用于逐文件上报状态。
        """
        if progress_cb:
            progress_cb("list", 0, 0, "获取文件列表...")
        files, err = self.get_share_files(share_url, share_passwd)
        if err:
            if progress_cb:
                progress_cb("error", 0, 0, err)
            return False, err

        items = []
        for fn in (file_names or []):
            f = self._pick_file(files, fn)
            if f:
                items.append(f)
        if not items:
            return False, "未找到指定的文件"

        total = len(items)
        q = _queue.Queue(maxsize=max(1, prefetch))
        results = []                       # [(name, err)]
        lock = threading.Lock()
        download_done = threading.Event()
        # 流水线闸门：下载 n 必须等“上传线程取走 n-1（开始上传）”后才开始，
        # 实现 下载1 → (上传1∥下载2) → (上传2∥下载3)…，同一时刻最多 1 个下载 + 1 个上传
        gate = threading.Semaphore(1)

        def _check_cancel():
            return bool(cancel_check and cancel_check())

        # ── 下载线程：下载/解压 → 放入上传队列（受闸门约束，不提前预取下一个文件）──
        def _downloader():
            try:
                for idx, f in enumerate(items):
                    if _check_cancel():
                        return
                    name = f.get("name", "download")
                    seq = "[%d/%d] %s" % (idx + 1, total, name)

                    def _dcb(dl, sz, speed):
                        if progress_cb:
                            progress_cb("download", dl, sz, "%s %s" % (seq, speed))

                    gate.acquire()  # 等上一个文件开始上传后，本文件才开始下载
                    _queued = False
                    try:
                        local_path, derr = self.download_file(
                            share_url, f, share_passwd, _dcb, cancel_check)
                        if derr:
                            with lock:
                                results.append((name, "下载失败: %s" % derr))
                            if progress_cb:
                                progress_cb("error", 0, 0, "%s 下载失败: %s" % (seq, derr))
                            if per_file_done_cb:
                                per_file_done_cb(idx, name, False, "下载失败: %s" % derr)
                            continue

                        extract_dir = None
                        vfs = [(local_path, name)]
                        if self._is_archive(local_path):
                            if progress_cb:
                                progress_cb("extract", 0, 0, "%s 检测到压缩包，解压中..." % seq)
                            vfs2, ext_err, extract_dir = self._extract_and_find_videos(
                                local_path, progress_cb)
                            if ext_err:
                                with lock:
                                    results.append((name, "解压失败: %s" % ext_err))
                                if per_file_done_cb:
                                    per_file_done_cb(idx, name, False, "解压失败: %s" % ext_err)
                                try:
                                    os.remove(local_path)
                                except Exception:
                                    pass
                                if extract_dir and os.path.isdir(extract_dir):
                                    shutil.rmtree(extract_dir, ignore_errors=True)
                                continue
                            if not vfs2:
                                with lock:
                                    results.append((name, "压缩包内未找到视频文件"))
                                if per_file_done_cb:
                                    per_file_done_cb(idx, name, False, "压缩包内未找到视频文件")
                                try:
                                    os.remove(local_path)
                                except Exception:
                                    pass
                                if extract_dir and os.path.isdir(extract_dir):
                                    shutil.rmtree(extract_dir, ignore_errors=True)
                                continue
                            try:
                                os.remove(local_path)
                            except Exception:
                                pass
                            vfs = vfs2

                        rn = (rename_map or {}).get(name, "")
                        q.put((idx, name, rn, vfs, extract_dir))
                        _queued = True
                    finally:
                        # 未入队（下载/解压失败、异常）时放行闸门，让下一个文件继续
                        if not _queued:
                            gate.release()
            finally:
                download_done.set()

        # ── 上传线程：从队列取已下载文件依次上传 ──
        def _uploader():
            try:
                while True:
                    try:
                        idx, name, rn, vfs, extract_dir = q.get(timeout=0.5)
                    except _queue.Empty:
                        if download_done.is_set() and q.empty():
                            return
                        continue
                    gate.release()  # 取走即开始上传，放行下一个文件开始下载
                    seq = "[%d/%d] %s" % (idx + 1, total, name)

                    def _ucb(stage, dl, sz, speed):
                        if progress_cb:
                            txt = "%s %s" % (seq, speed) if speed else seq
                            progress_cb(stage, dl, sz, txt)

                    ok, uerr = self._do_upload(
                        vfs, _ucb, True, rn, wps_path, delete_after_upload, cancel_check)
                    if extract_dir and os.path.isdir(extract_dir):
                        shutil.rmtree(extract_dir, ignore_errors=True)
                    if not ok:
                        with lock:
                            results.append((name, uerr or "上传失败"))
                    if per_file_done_cb:
                        per_file_done_cb(idx, name, ok, uerr or "")
            finally:
                pass

        t_dl = threading.Thread(target=_downloader, daemon=True)
        t_ul = threading.Thread(target=_uploader, daemon=True)
        t_dl.start()
        t_ul.start()
        t_dl.join()
        t_ul.join()

        if _check_cancel():
            if progress_cb:
                progress_cb("error", 0, 0, "已取消")
            return False, "已取消"

        if results:
            summary = "%d 个文件失败: %s" % (len(results), "; ".join(
                "%s(%s)" % (n, e) for n, e in results[:5]))
            if len(results) > 5:
                summary += " 等共 %d 个" % len(results)
            if progress_cb:
                progress_cb("error", 0, 0, summary)
            return False, summary

        if progress_cb:
            progress_cb("done", total, total, "全部完成: %d 个文件" % total)
        return True, None

    def _do_upload(self, video_files, progress_cb, upload_after_download,
                   rename_to, wps_path, delete_after_upload, cancel_check):
        """执行上传阶段（全局串行锁：同一时刻只有 1 个文件在上传）"""
        with _UPLOAD_LOCK:
            return self._do_upload_locked(
                video_files, progress_cb, upload_after_download,
                rename_to, wps_path, delete_after_upload, cancel_check)

    def _do_upload_locked(self, video_files, progress_cb, upload_after_download,
                          rename_to, wps_path, delete_after_upload, cancel_check):
        all_ok = True
        last_err = None
        uploaded_count = 0

        for idx, (vf_path, vf_name) in enumerate(video_files):
            if cancel_check and cancel_check():
                all_ok = False
                last_err = "已取消"
                break

            upload_name = vf_name
            cur_path = vf_path
            if idx == 0 and rename_to and rename_to.strip() and rename_to.strip() != vf_name:
                rt = rename_to.strip()
                _, orig_ext = os.path.splitext(vf_name)
                _, new_ext = os.path.splitext(rt)
                if not new_ext and orig_ext:
                    rt = rt + orig_ext
                if rt != vf_name:
                    new_path = os.path.join(os.path.dirname(vf_path), rt)
                    try:
                        if os.path.exists(new_path):
                            os.remove(new_path)
                        shutil.move(vf_path, new_path)
                        cur_path = new_path
                        upload_name = rt
                        if progress_cb:
                            progress_cb("rename", 0, 0, f"重命名: {vf_name} → {rt}")
                    except Exception as e:
                        if progress_cb:
                            progress_cb("error", 0, 0, f"重命名失败: {e}")
                        all_ok = False
                        last_err = f"重命名失败: {e}"
                        continue

            prefix = f"[{idx+1}/{len(video_files)}] " if len(video_files) > 1 else ""

            target_parent_id = self.wps_parent_id
            if wps_path and wps_path.strip():
                if progress_cb:
                    progress_cb("rename", 0, 0, f"{prefix}定位WPS路径: {wps_path}")
                target_parent_id = self._wps_ensure_path(wps_path, lambda m: (
                    progress_cb("rename", 0, 0, m) if progress_cb else None))

            if not upload_after_download:
                uploaded_count += 1
                continue

            if progress_cb:
                progress_cb("upload", 0, 0, f"{prefix}开始上传 → {upload_name}")

            def _ul_cb(ul, total, speed):
                if progress_cb:
                    progress_cb("upload", ul, total, speed)

            old_parent = self.wps_parent_id
            self.wps_parent_id = target_parent_id
            try:
                ok, err2 = self.upload_file(cur_path, upload_name, _ul_cb, cancel_check)
            finally:
                self.wps_parent_id = old_parent

            if not ok:
                if progress_cb:
                    progress_cb("error", 0, 0, f"{prefix}上传失败: {err2}")
                all_ok = False
                last_err = err2
                continue

            uploaded_count += 1

            if delete_after_upload and cur_path and os.path.exists(cur_path):
                try:
                    os.remove(cur_path)
                except Exception:
                    pass

        if all_ok:
            if progress_cb:
                msg = f"传输完成: {uploaded_count} 个文件"
                progress_cb("done", uploaded_count, uploaded_count, msg)
            return True, None
        else:
            if progress_cb and uploaded_count > 0:
                progress_cb("done", uploaded_count, len(video_files),
                            f"部分完成: {uploaded_count}/{len(video_files)}")
            return uploaded_count > 0, last_err
