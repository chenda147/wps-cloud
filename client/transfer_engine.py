"""
传输引擎：139分享下载 → 本地磁盘 → WPS上传
- 流式下载，支持进度/速度/断点续传
- 下载完自动上传 WPS
- 支持 WPS 多级路径自动创建文件夹
- 支持下载完成后重命名
- 支持压缩包自动解压并上传其中的视频文件
"""
import os
import sys
import time
import shutil
import threading
import queue as _queue
import zipfile
import tarfile
import subprocess
import rarfile
import requests
from yidong_core import YidongShareClient
from quark_core import QuarkShareClient
from wps_uploader import WPSUploader

VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
              '.m4v', '.mpg', '.mpeg', '.3gp', '.ts', '.rmvb', '.vob',
              '.divx', '.xvid', '.asf', '.f4v']

# 调用外部解压工具（7z/UnRAR）时隐藏控制台黑窗（Windows）
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_SUBPROCESS_HIDE = dict(creationflags=_CREATE_NO_WINDOW) if sys.platform.startswith('win') else {}

# 全局上传串行锁：即使并发开多线程，同一时刻也只允许 1 个文件上传，
# 实现 流水线(下载1→上传1∥下载2→上传2∥下载3) 语义，避免多任务同时抢占 WPS 上传
_UPLOAD_LOCK = threading.Lock()

ARCHIVE_EXTS = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
                '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz',
                '.iso', '.cab', '.arj', '.lzh', '.lha'}

# 视频格式集合（小写，含点号）
_VIDEO_SET = {e.lower() for e in VIDEO_EXTS}

API_BASE = "https://drive.kdocs.cn/api/v3"


def detect_cloud_type(share_url: str) -> str:
    """根据分享链接自动识别网盘类型。

    Returns: 'quark' | 'yidong'
    """
    u = (share_url or "").lower()
    if "quark.cn" in u:
        return "quark"
    return "yidong"


def build_src_client(clouds, share_url, cloud_id=None):
    """根据配置中的云盘连接列表，构造对应的下载源客户端。

    Args:
        clouds: 云盘连接配置列表，每项含 type/name/is_primary/token或cookie/id
        share_url: 分享链接（用于自动识别类型）
        cloud_id: 指定使用某个连接（可选，优先级高于自动选主账号）
    Returns:
        下载源客户端对象（YidongShareClient 或 QuarkShareClient）
    """
    if not clouds:
        raise ValueError("未配置任何云盘连接")

    if cloud_id:
        for c in clouds:
            if c.get("id") == cloud_id:
                return _make_client(c)
        raise ValueError(f"未找到指定的云盘连接: {cloud_id}")

    ctype = detect_cloud_type(share_url)
    # 优先匹配该类型的主账号
    for c in clouds:
        if c.get("type") == ctype and c.get("is_primary"):
            return _make_client(c)
    # 退化：该类型任意连接
    for c in clouds:
        if c.get("type") == ctype:
            return _make_client(c)
    if ctype == "quark":
        raise ValueError(
            "未配置 quark（夸克网盘）类型的云盘连接。\n"
            "该分享链接是夸克网盘（pan.quark.cn），请在客户端界面「夸克网盘」"
            "卡片中填写夸克 Cookie 后保存，再重新检查。"
        )
    raise ValueError(
        f"未配置 {ctype} 类型的云盘连接。请在客户端「云盘连接」中添加对应账号后重试。"
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
    """单文件传输引擎

    下载源（139 / 夸克）通过 src_client 注入，上传侧（WPS）不变。
    """

    def __init__(self, yidong_token, wps_cookie, save_dir,
                 wps_parent_id="0", wps_group_id="2203072050",
                 src_client=None, upload_slow_retry=None):
        """
        Args:
            yidong_token: 139云盘账号Token（base64编码），兼容老调用方式
            wps_cookie: WPS 365 Cookie字符串
            save_dir: 本地保存目录
            wps_parent_id: WPS目标文件夹ID（根目录为0）
            wps_group_id: WPS group ID
            src_client: 下载源客户端（YidongShareClient / QuarkShareClient）。
                        若未传则回退用 yidong_token 构造 139 客户端（向后兼容）。
            upload_slow_retry: 可选 dict，如 {"min_mb": 0.5, "seconds": 30}。
                        上传速度持续低于 min_mb(MB/s) 超过 seconds 秒时中断并重新上传。
        """
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
        """列出 WPS 指定文件夹下的文件/文件夹（parent_id 为 None/0 表示根目录）"""
        import json as _json
        files, offset = [], 0
        while True:
            params = {"offset": offset, "count": 500}
            if parent_id and str(parent_id) not in ("0", "None", ""):
                params["parentid"] = str(parent_id)
            try:
                r = self.wps.s.get(f"{API_BASE}/groups/{self.wps_group_id}/files",
                                   params=params, timeout=20)
                if r.status_code != 200:
                    break
                batch = r.json().get("files", [])
                files.extend(batch)
                if len(batch) < 500:
                    break
                offset += 500
            except Exception:
                break
        return files

    def _wps_find_folder(self, parent_id, folder_name):
        """在 parent_id 下查找名为 folder_name 的文件夹，返回 id(str) 或 None"""
        for f in self._wps_list_files(parent_id):
            if f.get("ftype") == "folder":
                fname = (f.get("fname") or f.get("name") or "").strip()
                if fname == folder_name:
                    return str(f.get("id", ""))
        return None

    def _wps_create_folder(self, parent_id, folder_name):
        """在 parent_id 下创建文件夹，参照 app.py 的 4 种尝试方式，返回新文件夹 id 或 None"""
        import json as _json
        DRIVE_V5 = "https://drive.kdocs.cn"
        try:
            gid = int(self.wps_group_id) if str(self.wps_group_id).isdigit() else self.wps_group_id
        except (ValueError, TypeError):
            gid = self.wps_group_id
        pid = int(parent_id) if parent_id and str(parent_id).strip().isdigit() else 0

        h_json = {"content-type": "application/json", "origin": "https://www.kdocs.cn",
                  "accept": "*/*", "Referer": "https://www.kdocs.cn/", "X-Csrf-Token": self.wps.csrf}

        # 尝试1: v5/files/file POST
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

        # 尝试2: v5/files/file 用 parent_id 替代 parentid
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

        # 尝试3: v5/files/folder POST
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

        # 尝试4: v3 JSON
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
        """只读解析 WPS 路径：逐级查找，不存在则返回 None（不创建目录）。
        用于检查阶段列出 WPS 目录里重命名后的文件，避免误建目录。
        """
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
        """确保 WPS 上存在指定路径，逐级查找或创建，返回最末级文件夹 ID

        Args:
            path_str: 如 "电视/国漫/王者归来"，用 / 或 \\ 分隔
            log_cb: 可选日志回调
        Returns:
            folder_id（失败时返回原始 parent_id）
        """
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
        """获取分享链接中的所有文件，按体积降序排列"""
        result = self.src.list_share_files_recursive(share_url, share_passwd)
        if not result.get("ok"):
            return None, result.get("msg", "获取文件列表失败")

        files = [f for f in result.get("items", []) if not f.get("is_dir")]
        if not files:
            return None, "分享链接中没有文件"

        # 按体积降序排列
        files.sort(key=lambda x: x.get("size", 0), reverse=True)
        return files, None

    def _pick_file(self, files, file_name=""):
        """从文件列表中按文件名匹配，同名时选容量最大的，不传则取最大的"""
        if not files:
            return None
        if file_name and file_name.strip():
            fn = file_name.strip()
            exact = [f for f in files if f.get("name", "") == fn]
            if exact:
                # 同名文件优选最大的（如 EP08.mkv 8G vs EP08.mkv 4G）
                return max(exact, key=lambda f: f.get("size", 0))
            # 模糊匹配：包含关系
            fuzzy = [f for f in files if fn in f.get("name", "") or f.get("name", "") in fn]
            if fuzzy:
                return max(fuzzy, key=lambda f: f.get("size", 0))
            return None
        return files[0]

    def download_file(self, share_url, file_info, share_passwd="",
                      progress_cb=None, cancel_check=None):
        """下载单个文件到本地磁盘
        
        Args:
            share_url: 分享链接
            file_info: 文件信息 dict {id, name, size, path}
            share_passwd: 分享密码
            progress_cb: 回调(downloaded, total, speed_str)
            cancel_check: 返回True则取消下载
        
        Returns:
            (local_path, error_msg)
        """
        file_name = file_info.get("name", "download")
        file_size = file_info.get("size", 0)
        file_id = file_info.get("id", "")
        share_path = file_info.get("path", "")

        # 获取下载直链
        dl_result = self.src.get_share_download_url(
            share_url, file_id, file_name, share_path, share_passwd)
        if not dl_result.get("ok"):
            return None, f"获取下载链接失败: {dl_result.get('msg')}"

        cdn_url = dl_result["url"]
        final_path = os.path.join(self.save_dir, file_name)
        # 临时文件：先写 xxx.part，下载完成后再改名，避免半成品被误当成品
        local_path = final_path + ".part"
        os.makedirs(self.save_dir, exist_ok=True)

        # 流式下载，支持断点续传
        downloaded = 0
        if os.path.exists(final_path) and file_size > 0 and os.path.getsize(final_path) >= file_size:
            # 最终文件已存在且完整（上次已完成改名），直接跳过
            if progress_cb:
                progress_cb(file_size, file_size, "已完成(跳过)")
            return final_path, None
        if os.path.exists(local_path):
            downloaded = os.path.getsize(local_path)
            if downloaded > file_size and file_size > 0:
                # 源文件变小（分享链接被更新过），残留的 .part 无效，删除重下
                try:
                    os.remove(local_path)
                except Exception:
                    pass
                downloaded = 0
            if downloaded >= file_size and file_size > 0:
                # 上次已下完但未及改名（进程中途退出），补做改名
                try:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.replace(local_path, final_path)
                    if progress_cb:
                        progress_cb(downloaded, file_size, "已完成(跳过)")
                    return final_path, None
                except Exception:
                    pass

        # 单连接流式下载（稳定优先）。之前的多线程 Range 方案在夸克 CDN 不稳时会
        # 整体僵死、进度卡住，故回退为简单可靠的流式下载，并通过大分片 + 大接收缓冲
        # 拉高单连接吞吐。
        from requests.adapters import HTTPAdapter
        import socket as _socket

        referer = getattr(self.src, "download_referer", "https://pan.quark.cn/")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
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

                if downloaded > 0 and r.status_code == 200:
                    # 服务器忽略 Range 返回了全量内容：若继续 "ab" 追加会把
                    # 已下载部分与全量内容拼接导致文件损坏，这里从头重写
                    downloaded = 0
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass
                mode = "ab" if downloaded > 0 else "wb"
                t0 = time.time()
                last_cb = 0
                start_downloaded = downloaded
                if progress_cb:
                    progress_cb(downloaded, file_size, "开始下载")

                with open(local_path, mode) as f:
                    # 4MB 分片：单连接也能吃满带宽，又不至于内存爆
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
                retry = 0  # 成功一轮，重置
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

        # 下载完成：临时文件 .part 改名为最终文件名（原子操作，避免半成品）
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
            os.replace(local_path, final_path)
        except Exception as e:
            return None, f"文件改名失败: {e}"

        if progress_cb:
            progress_cb(downloaded, file_size, "下载完成")
        # 下载完成后删除网盘里的临时转存文件
        try:
            _cleanup = getattr(self.src, "cleanup", None)
            if _cleanup:
                _cleanup(share_url, file_name=file_name)
        except Exception:
            pass
        return final_path, None

    def upload_file(self, local_path, file_name, progress_cb=None,
                    cancel_check=None):
        """上传本地文件到 WPS

        Returns:
            (success, error_msg)
        """
        if cancel_check and cancel_check():
            return False, "已取消"

        file_size = os.path.getsize(local_path)

        def _upload_progress(pct, speed):
            if progress_cb:
                if pct == -1:  # 重试通知，不重置进度
                    progress_cb(-1, file_size, speed)
                    return
                # 用 round 避免浮点截断导致百分比倒退
                uploaded = int(file_size * pct / 100 + 0.5)
                progress_cb(uploaded, file_size, f"上传 {pct}% {speed}")
            if cancel_check and cancel_check():
                raise Exception("已取消")

        status, result = self.wps.upload_file_from_disk(
            local_path, file_name, self.wps_parent_id, _upload_progress, cancel_check)

        if status in (200, 201, 204):
            return True, None

        # ── ★8 重名策略：检测"同名/已存在"类错误时自动改名重传一次 ──
        err = result.get("error", "上传失败")
        if any(k in err for k in ("同名", "已存在", "存在相同", "重复文件",
                                  "名称冲突", "名字冲突", "duplicate", "already exists")):
            base, ext = os.path.splitext(file_name)
            alt_name = f"{base}_{int(time.time())}{ext}"
            if progress_cb:
                progress_cb(-1, file_size, f"检测到同名文件，自动改名: {alt_name}")
            status2, result2 = self.wps.upload_file_from_disk(
                local_path, alt_name, self.wps_parent_id, _upload_progress, cancel_check)
            if status2 in (200, 201, 204):
                return True, None
            err = f"{err}；改名后仍失败: {result2.get('error', '上传失败')}"
        return False, err

    # ═══════════════════════════════════════════════════════
    #  压缩包检测 & 解压
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _find_archive_tool():
        """查找可用的压缩工具，优先捆绑的便携版，其次系统 PATH。
        Returns:
            (tool_path, tool_type)  或 (None, None)
            tool_type: '7z' | 'unrar'
        """
        candidates = []

        # 1. PyInstaller 打包后 _MEIPASS 中的捆绑工具
        if getattr(sys, 'frozen', False):
            for exe in ('7z.exe', '7za.exe', 'unrar.exe', 'UnRAR.exe'):
                p = os.path.join(sys._MEIPASS, exe)
                if os.path.isfile(p):
                    candidates.append(p)

        # 2. 可执行文件同目录（开发环境 & 打包后都可）
        try:
            app_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        except Exception:
            app_dir = os.getcwd()
        for exe in ('7z.exe', '7za.exe', 'unrar.exe', 'UnRAR.exe'):
            p = os.path.join(app_dir, exe)
            if os.path.isfile(p) and p not in candidates:
                candidates.append(p)

        # 3. 当前工作目录
        for exe in ('7z.exe', '7za.exe', 'unrar.exe', 'UnRAR.exe'):
            p = os.path.join(os.getcwd(), exe)
            if os.path.isfile(p) and p not in candidates:
                candidates.append(p)

        # 判断工具类型
        for p in candidates:
            name = os.path.basename(p).lower()
            if name.startswith('7z'):
                return p, '7z'
            if 'unrar' in name:
                return p, 'unrar'

        # 4. 常见安装目录硬编码兜底（用户已装 7-Zip 必中）
        for base in ('C:\\Program Files\\7-Zip',
                     'C:\\Program Files (x86)\\7-Zip',
                     'D:\\Program Files\\7-Zip'):
            p = os.path.join(base, '7z.exe')
            if os.path.isfile(p):
                return p, '7z'

        # 5. 系统 PATH 中的 7z
        for exe in ('7z', '7za', '7zr'):
            p = shutil.which(exe)
            if p:
                return p, '7z'

        # 6. 系统 PATH 中的 unrar
        p = shutil.which('unrar')
        if p:
            return p, 'unrar'

        return None, None

    @staticmethod
    def _get_unrar_path():
        """查找捆绑的 UnRAR.exe（供 rarfile 库使用）"""
        exe_names = ('UnRAR.exe', 'unrar.exe', 'UnRAR.dll', 'unrar')
        dirs = []

        # PyInstaller 打包后
        if getattr(sys, 'frozen', False):
            dirs.append(sys._MEIPASS)

        # 同目录
        try:
            app_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        except Exception:
            app_dir = os.getcwd()
        dirs.append(app_dir)

        # 当前目录
        dirs.append(os.getcwd())

        for d in dirs:
            for name in exe_names:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p

        # 系统 PATH
        for name in ('unrar', 'UnRAR.exe'):
            p = shutil.which(name)
            if p:
                return p

        return None

    @staticmethod
    def _is_archive(filepath):
        """检测文件是否为压缩包（按扩展名 + 简单魔数）"""
        name = os.path.basename(filepath).lower()
        # 复合扩展名优先匹配
        for ae in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz'):
            if name.endswith(ae):
                return True
        ext = os.path.splitext(name)[1]
        if ext in ARCHIVE_EXTS:
            return True
        # 魔数兜底
        try:
            with open(filepath, 'rb') as f:
                head = f.read(8)
            if head[:4] == b'PK\x03\x04':      # ZIP
                return True
            if head[:4] == b'Rar!\x1a\x07\x00': # RAR 4.x
                return True
            if head[:4] == b'Rar!\x1a\x07\x01': # RAR 5.x
                return True
            if head[:6] == b"7z\xbc\xaf'\x1c":     # 7z
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _extract_and_find_videos(archive_path, progress_cb=None, _depth=0):
        """解压压缩包并返回其中的视频文件列表（支持嵌套压缩包递归解压）。
        Returns:
            (video_files, error, extract_dir)
            video_files: [(full_path, filename), ...] 成功时非空
            error: 失败原因字符串
            extract_dir: 解压目录（调用方负责清理）
        """
        if _depth > 3:
            return [], "压缩包嵌套层数过深（>3层），已中止", ""
        archive_name = os.path.basename(archive_path)
        # 在同目录下创建解压子目录
        extract_dir = os.path.join(os.path.dirname(archive_path),
                                   f"_extract_{os.path.splitext(archive_name)[0]}")
        os.makedirs(extract_dir, exist_ok=True)

        if progress_cb:
            progress_cb("extract", 0, 0, f"正在解压: {archive_name}")

        try:
            name_lower = archive_name.lower()
            ext = os.path.splitext(archive_name)[1].lower()

            # ── ZIP ──
            if name_lower.endswith('.zip') or ext == '.zip':
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    # 安全检测：防止路径穿越
                    for member in zf.infolist():
                        member_path = os.path.normpath(member.filename)
                        if member_path.startswith('..') or os.path.isabs(member_path):
                            continue  # skip suspicious paths
                    zf.extractall(extract_dir)

            # ── TAR ──
            elif name_lower.endswith(('.tar', '.tar.gz', '.tar.bz2', '.tar.xz',
                                       '.tgz', '.tbz2', '.txz')):
                if not os.path.isfile(archive_path):
                    return [], f"文件不存在: {archive_path}", extract_dir
                if os.path.getsize(archive_path) == 0:
                    return [], "压缩包文件为空（0 字节），可能下载失败", extract_dir

                # 优先用捆绑的 7z（兼容性最好），tarfile 作为后备
                tool_path, _ = TransferEngine._find_archive_tool()
                if progress_cb:
                    if tool_path:
                        progress_cb("extract", 0, 0, f"已找到 7z: {tool_path}")
                    else:
                        progress_cb("extract", 0, 0, "未找到 7z（捆绑/系统PATH均无），将仅用 tarfile")
                tar_ok = False
                tar_err = ""

                # 方法1：7z CLI（如果可用）
                if tool_path:
                    if progress_cb:
                        progress_cb("extract", 0, 0, f"正在 7z 解压: {archive_name}")
                    try:
                        proc = subprocess.Popen(
                            [tool_path, 'x', '-y', f'-o{extract_dir}', archive_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=os.path.dirname(tool_path),  # 确保 7z.dll 能被找到
                            **_SUBPROCESS_HIDE)
                        t0 = time.time()
                        while proc.poll() is None:
                            elapsed = int(time.time() - t0)
                            if elapsed > 3600:
                                proc.kill()
                                return [], "解压超时（文件可能过大或损坏）", extract_dir
                            time.sleep(3)
                        if proc.returncode == 0:
                            tar_ok = True
                            if progress_cb:
                                progress_cb("extract", 1, 1, "7z 解压完成")
                        else:
                            stderr = proc.stderr.read()
                            tar_err = f"7z 解压失败: {stderr[:200]}"
                    except Exception as ce:
                        tar_err = f"7z 解压异常: {ce}"

                # 方法2：tarfile（7z 不可用或失败时）
                if not tar_ok:
                    if progress_cb:
                        progress_cb("extract", 0, 0,
                                    f"尝试 tarfile…" + (f"({tar_err})" if tar_err else ""))
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
                    return [], f"解压失败: {tar_err or 'tarfile 所有模式均失败'}", extract_dir

            # ── 7z (py7zr) ──
            elif ext == '.7z':
                try:
                    import py7zr
                except ImportError:
                    pass
                else:
                    with py7zr.SevenZipFile(archive_path, 'r', password='') as zf:
                        zf.extractall(extract_dir)
                    video_files = TransferEngine._find_videos(extract_dir)
                    if video_files:
                        if progress_cb:
                            progress_cb("extract", 1, 1, f"解压完成，找到 {len(video_files)} 个视频")
                        return video_files, None, extract_dir
                # py7zr 失败 → 走 CLI 兜底

            # ── RAR（rarfile + 捆绑 UnRAR.exe，零安装）──
            if name_lower.endswith('.rar') or ext == '.rar':
                unrar_path = TransferEngine._get_unrar_path()
                if unrar_path:
                    rarfile.UNRAR_TOOL = unrar_path
                try:
                    with rarfile.RarFile(archive_path) as rf:
                        rf.extractall(extract_dir)
                    video_files = TransferEngine._find_videos(extract_dir)
                    if video_files:
                        if progress_cb:
                            progress_cb("extract", 1, 1, f"解压完成，找到 {len(video_files)} 个视频")
                        return video_files, None, extract_dir
                    # RAR 里没视频，继续走下面统一查找
                except rarfile.RarCannotExec as e:
                    if progress_cb:
                        progress_cb("extract", 0, 0, "RAR 解压需外部工具，尝试 CLI 兜底…")
                except Exception:
                    pass  # 解压失败走 CLI 兜底

            # ── RAR / 7z / ISO / CAB / ARJ  CLI 兜底 ──
            if name_lower.endswith(('.rar', '.7z', '.iso', '.cab', '.arj',
                                     '.lzh', '.lha')) or ext in ('.rar', '.7z', '.iso', '.cab', '.arj', '.lzh', '.lha'):
                tool_path, tool_type = TransferEngine._find_archive_tool()
                if tool_path:
                    if tool_type == '7z':
                        cmd = [tool_path, 'x', '-y', f'-o{extract_dir}', archive_path]
                    else:  # unrar
                        cmd = [tool_path, 'x', '-y', archive_path, extract_dir + os.sep]
                    try:
                        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, text=True,
                                                **_SUBPROCESS_HIDE)
                        t0 = time.time()
                        last_report = 0
                        while proc.poll() is None:
                            elapsed = int(time.time() - t0)
                            if elapsed > 3600:          # 1 小时超时
                                proc.kill()
                                return [], "解压超时（文件可能过大或损坏）", extract_dir
                            if progress_cb and elapsed - last_report >= 5:
                                progress_cb("extract", 0, 0, f"解压中... ({elapsed}s)")
                                last_report = elapsed
                            time.sleep(3)
                        _, stderr_out = proc.communicate(timeout=10)
                        if proc.returncode != 0:
                            err = (stderr_out or "").strip()
                            return [], f"解压失败: {err[:200]}", extract_dir
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        return [], "解压超时（文件可能过大或损坏）", extract_dir
                else:
                    hint = ("未找到解压工具。RAR 格式请将 UnRAR.exe 放入程序目录；"
                            "其他格式请安装 7-Zip。"
                            "\nUnRAR 下载: https://www.rarlab.com/rar_add.htm")
                    return [], hint, extract_dir

            # 其他格式已经通过 ZIP/TAR 处理，到这里说明格式不在支持列表中
            if not name_lower.endswith(('.zip', '.tar', '.tar.gz', '.tar.bz2', '.tar.xz',
                                         '.tgz', '.tbz2', '.txz', '.rar', '.7z', '.iso',
                                         '.cab', '.arj', '.lzh', '.lha')) and ext not in (
                                         '.zip', '.rar', '.7z', '.iso', '.cab', '.arj', '.lzh', '.lha'):
                return [], f"不支持的压缩格式: {ext}", extract_dir

            # 查找视频文件
            video_files = TransferEngine._find_videos(extract_dir)
            if not video_files:
                # 未找到视频 → 扫描嵌套压缩包并递归解压
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
        """递归查找目录下所有视频文件，返回 [(完整路径, 相对路径/文件名), ...]"""
        results = []
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in _VIDEO_SET:
                    full = os.path.join(dirpath, fn)
                    # 用相对于 extract_dir 的路径作为带路径的文件名
                    rel = os.path.relpath(full, root_dir)
                    results.append((full, rel))
        return results

    @staticmethod
    def _find_archives(root_dir):
        """扫描目录下所有嵌套压缩包，返回文件路径列表"""
        archives = []
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if TransferEngine._is_archive(full):
                    archives.append(full)
        return archives

    # ═══════════════════════════════════════════════════════
    #  核心传输流程
    # ═══════════════════════════════════════════════════════

    def transfer_batch(self, share_url, share_passwd="", file_names=None, progress_cb=None,
                       cancel_check=None, rename_map=None, wps_path="",
                       delete_after_upload=True, prefetch=1, per_file_done_cb=None):
        """流水线批量传输：下载与上传并行，下载不等待上传。

        以 prefetch=1 为例（对应“上传1时下载2”的场景）：
         下载 1 完成 → 开始上传 1
         上传 1 期间 → 并行下载 2（下载完放入队列等待）
         1 上传完   → 开始上传 2，同时继续下载 3
         ……
        Args:
            file_names: 指定要传输的文件名列表（顺序即传输顺序），None=分享内全部文件
            rename_map: {原文件名: 重命名名}，可选
            prefetch:   预下载缓冲数（已下载待上传的文件最多几个），默认 1
            per_file_done_cb: 可选回调(idx, file_name, ok, err)。每个文件处理结束后
                调用一次（下载失败/解压失败/上传完成均触发），用于逐文件上报状态。
        Returns:
            (all_ok, err_summary)
        """
        if progress_cb:
            progress_cb("list", 0, 0, "获取文件列表...")
        files, err = self.get_share_files(share_url, share_passwd)
        if err:
            if progress_cb:
                progress_cb("error", 0, 0, err)
            return False, err

        if file_names:
            items = []
            for fn in file_names:
                f = self._pick_file(files, fn)
                if f:
                    items.append(f)
            if not items:
                return False, "未找到指定的文件"
        else:
            items = [f for f in files if f.get("name")]

        if not items:
            if progress_cb:
                progress_cb("error", 0, 0, "没有可传输的文件")
            return False, "没有可传输的文件"

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
                    # ── 本地已有下载好的目标文件（上次上传失败/中断遗留）：
                    # 跳过重新下载，直接进入上传队列。上传失败不会删除本地文件，
                    # 若这里再从头下载，重传时会白白重下一整集。
                    _rn0 = (rename_map or {}).get(name, "")
                    _expect = f.get("size") or 0
                    _cand = os.path.join(self.save_dir, _rn0) if _rn0 else ""
                    if (_cand and os.path.exists(_cand) and os.path.getsize(_cand) > 0
                            and (not _expect or os.path.getsize(_cand) == _expect)):
                        _csz = os.path.getsize(_cand)
                        if progress_cb:
                            progress_cb("download", _csz, _csz, "%s 本地已存在，跳过下载" % seq)
                        # 先提示“已下载完成，等待上传”，再入队（上传线程取走时会清空）
                        if progress_cb:
                            progress_cb("queued", 0, 0, "%s 已下载完成，等待上传" % seq)
                        q.put((idx, name, _rn0, [(_cand, _rn0)], None, True))
                        _queued = True
                        continue
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
                        # 先提示“已下载完成，等待上传”，再入队（上传线程取走时会清空）
                        if progress_cb:
                            progress_cb("queued", 0, 0, "%s 已下载完成，等待上传" % seq)
                        q.put((idx, name, rn, vfs, extract_dir, True))
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
                        _item = q.get(timeout=0.5)
                        idx, name, rn, vfs, extract_dir = _item[:5]
                        _gate_held = _item[5] if len(_item) > 5 else True
                    except _queue.Empty:
                        if download_done.is_set() and q.empty():
                            return
                        continue
                    if _gate_held:
                        gate.release()  # 取走即开始上传，放行下一个文件开始下载
                    if progress_cb:
                        progress_cb("queued", 0, 0, "")  # 已开始上传，清空“等待上传”
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

    def transfer(self, share_url, share_passwd="", progress_cb=None,
                 cancel_check=None, upload_after_download=True,
                 rename_to="", wps_path="", delete_after_upload=False,
                 file_name=""):
        """完整传输：获取文件 → 下载 → (重命名) → 上传 → (删除本地文件)

        Args:
            progress_cb: 回调(stage, downloaded, total, speed_str)
                         stage: "list"|"download"|"rename"|"upload"|"done"|"error"
            cancel_check: 返回True取消
            upload_after_download: 是否下载后自动上传
            rename_to: 下载完成后重命名的文件名（含扩展名），空则不重命名
            wps_path: WPS 存放路径，如 "国漫/王者归来"，空则用 wps_parent_id
            delete_after_upload: 上传成功后删除本地文件
            file_name: 指定要传输的文件名（精确匹配），空则取最大的文件

        Returns:
            (success, error_msg)
        """
        # 1. 获取文件列表
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

        # 1.5 清理上次失败遗留的临时解压目录
        try:
            for entry in os.listdir(self.save_dir):
                if entry.startswith('_extract_') and os.path.isdir(
                        os.path.join(self.save_dir, entry)):
                    shutil.rmtree(os.path.join(self.save_dir, entry),
                                  ignore_errors=True)
        except Exception:
            pass

        # 1.6 检查本地是否已有命名好的目标文件（断连续传场景）
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
                # 直接跳到上传，构造 video_files
                video_files = [(pre_check, rt)]
                # 上传前清理被覆盖的旧临时目录（如果有）
                for entry in os.listdir(self.save_dir):
                    ep = os.path.join(self.save_dir, entry)
                    if entry.startswith("_extract_") and os.path.isdir(ep):
                        try:
                            shutil.rmtree(ep, ignore_errors=True)
                        except Exception:
                            pass
                # 跳转到上传阶段
                return self._do_upload(video_files, progress_cb,
                                       upload_after_download, rename_to,
                                       wps_path, delete_after_upload,
                                       cancel_check)

        # 2. 下载
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

        # 2.5 检测并解压压缩包
        extract_dir = None
        video_files = [(local_path, orig_name)]  # 非压缩包直接用原文件

        if self._is_archive(local_path):
            vfs, ext_err, extract_dir = self._extract_and_find_videos(local_path, progress_cb)
            if ext_err:
                if progress_cb:
                    progress_cb("error", 0, 0, f"解压失败: {ext_err}")
                # 清理损坏的压缩包与残留解压目录：否则下次重试时 download_file
                # 会因本地文件大小匹配而跳过重新下载，导致反复解压失败
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
            # 删掉原始压缩包，节省空间
            try:
                os.remove(local_path)
            except Exception:
                pass
            video_files = vfs
            if progress_cb:
                progress_cb("extract", 1, 1,
                            f"解压完成, {len(video_files)} 个视频" +
                            (" (仅上传首个)" if not rename_to and len(video_files) > 1 else ""))

        # ── 上传 ──
        ok, err = self._do_upload(video_files, progress_cb, upload_after_download,
                                  rename_to, wps_path, delete_after_upload, cancel_check)

        # 清理解压临时目录
        if extract_dir and os.path.isdir(extract_dir):
            try:
                shutil.rmtree(extract_dir)
                if progress_cb:
                    progress_cb("rename", 0, 0, "已清理解压临时文件")
            except Exception:
                pass

        return ok, err

    def _do_upload(self, video_files, progress_cb, upload_after_download,
                   rename_to, wps_path, delete_after_upload, cancel_check):
        """执行上传阶段（可被 transfer 和断连续传复用）

        全局串行：持有 _UPLOAD_LOCK 保证同一时刻只有 1 个文件在上传，
        配合 transfer_batch 的 gate 闸门实现严格的流水线（上传1∥下载2）。
        """
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

            # 重命名（仅第一个或单文件时应用 rename_to）
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

            # 多文件时显示进度
            prefix = f"[{idx+1}/{len(video_files)}] " if len(video_files) > 1 else ""

            # WPS 路径
            target_parent_id = self.wps_parent_id
            if wps_path and wps_path.strip():
                if progress_cb:
                    progress_cb("rename", 0, 0, f"{prefix}定位WPS路径: {wps_path}")
                target_parent_id = self._wps_ensure_path(wps_path, lambda m: (
                    progress_cb("rename", 0, 0, m) if progress_cb else None))

            # 上传
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

            # 删除本地文件
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
