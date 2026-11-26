"""
夸克网盘分享链接核心模块
- 解析分享链接（https://pan.quark.cn/s/xxxx）
- 列出分享文件（sharepage/detail，支持分页/嵌套）
- 通过项目内置转存器（quark_saver）把分享转存进自己网盘，再从自己网盘取直链
- 下载完成后删除网盘里的临时文件

鉴权方式：登录 cookie（一个 cookie 通吃所有夸克分享，类似 139 的单个 token）
分享文件的直链很难直接解析，因此转存交给 QAS，本项目只做"取链 + 下载 + 清理"。

接口参考社区成熟实现（quark-auto-save / QuarkPanTool），均为夸克非公开分享 API。
"""
import re
import os
import sys
import time
import json
import random
import threading
import requests

from quark_saver import QuarkSaver

BASE = "https://drive-pc.quark.cn"
SHARE_HOST = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage"
FILE_HOST = "https://drive-pc.quark.cn/1/clouddrive/file"

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
UA_QUARK_APP = "quark-cloud-drive/2.5.56 Electron/18.3.5.12"

COMMON_PARAMS = {
    "pr": "ucpro",
    "fr": "pc",
    "uc_param_str": "",
}

# 视频扩展名（与服务器 _server_fetch_share 一致）：用于修正夸克把单文件视频误标为目录的问题
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts",
              ".webm", ".rmvb", ".mpg", ".mpeg", ".3gp", ".ogv", ".iso"}


def _ts13() -> int:
    return int(time.time() * 1000)


def _rand_dt() -> int:
    return random.randint(200, 9999)


class QuarkShareClient:
    """夸克网盘分享链接客户端（登录 cookie 即可下载任意夸克分享）"""

    def __init__(self, cookie: str, saver=None):
        self.cookie = (cookie or "").strip()
        self.saver = saver if saver is not None else QuarkSaver(self.cookie)
        self._transferred = {}
        self._lock = threading.Lock()
        self.download_referer = "https://pan.quark.cn/"
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": UA,
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    # ──────────────────────────────────────────────
    #  分享链接解析
    # ──────────────────────────────────────────────
    @staticmethod
    def parse_share_url(url: str) -> dict:
        url = (url or "").strip()
        # 形如 https://pan.quark.cn/s/xxxxxxxx 或带 ?pwd=xxx
        m = re.search(r'pan\.quark\.cn/s/([A-Za-z0-9]+)', url)
        if not m:
            m = re.search(r'quark\.cn/s/([A-Za-z0-9]+)', url)
        if not m:
            return {"ok": False, "msg": "无法从链接中提取夸克分享ID"}
        pwd_id = m.group(1)
        # 提取码（若有）
        pwd = ""
        mp = re.search(r'[?&]pwd=([^&]+)', url)
        if mp:
            pwd = mp.group(1)
        return {"ok": True, "pwd_id": pwd_id, "pwd": pwd, "url": url}

    # ──────────────────────────────────────────────
    #  获取 stoken（访问分享内容的前置令牌）
    # ──────────────────────────────────────────────
    def _get_stoken(self, pwd_id: str, passcode: str) -> dict:
        # 夸克 token 接口为 POST，JSON body 传 pwd_id / passcode
        json_body = {"pwd_id": pwd_id, "passcode": passcode or ""}
        try:
            r = self._session.post(
                f"{SHARE_HOST}/token", params=COMMON_PARAMS,
                json=json_body, timeout=30)
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}
            js = r.json()
            if js.get("status") != 200 and js.get("code", 200) != 0:
                return {"ok": False, "msg": js.get("message", "获取 stoken 失败")}
            data = js.get("data", {})
            stoken = data.get("stoken") or data.get("token", "")
            if not stoken:
                return {"ok": False, "msg": "获取 stoken 失败（链接失效或提取码错误）"}
            return {"ok": True, "stoken": stoken}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ──────────────────────────────────────────────
    #  列出分享文件（单层）
    # ──────────────────────────────────────────────
    def list_share_files(self, share_url: str, passwd: str = "",
                         folder_id: str = "0") -> dict:
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        pwd_id = parsed["pwd_id"]
        # folder_id == "root" 时当作根目录 0
        if folder_id in ("root", "", None):
            folder_id = "0"

        stoken_res = self._get_stoken(pwd_id, passwd or parsed.get("pwd", ""))
        if not stoken_res.get("ok"):
            return stoken_res
        stoken = stoken_res["stoken"]

        items = []
        page = 1
        while True:
            params = dict(COMMON_PARAMS)
            params.update({
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": folder_id,
                "force": "0",
                "_page": page,
                "_size": "50",
                "_sort": "file_type:asc,updated_at:desc",
                "__dt": _rand_dt(),
                "__t": _ts13(),
            })
            try:
                r = self._session.get(
                    f"{SHARE_HOST}/detail", params=params, timeout=30)
                if r.status_code >= 400:
                    return {"ok": False, "msg": f"HTTP {r.status_code}"}
                js = r.json()
                if js.get("status") != 200 and js.get("code", 200) != 0:
                    return {"ok": False, "msg": js.get("message", "列出文件失败")}
                data = js.get("data", {})
                file_list = data.get("list", [])
                for f in file_list:
                    fname = f.get("file_name") or f.get("name", "")
                    _ext = os.path.splitext(fname)[1].lower()
                    # 夸克单文件分享（.mp4 等）常被误标为目录，按扩展名兜底修正
                    is_video_file = _ext in VIDEO_EXTS
                    is_dir = (bool(f.get("dir", False)) or f.get("file_type") == 1 \
                        or f.get("type") == "dir") and not is_video_file
                    items.append({
                        "id": f.get("fid") or f.get("file_id", ""),
                        "name": f.get("file_name") or f.get("name", "未知"),
                        "size": int(f.get("size", 0) or 0),
                        "is_dir": is_dir,
                        "path": f.get("fid") or f.get("file_id", ""),
                    })
                # 翻页
                total = data.get("_total", 0)
                fetched = len(items)
                if len(file_list) < 50 or (total and fetched >= total):
                    break
                page += 1
                if page > 50:  # 安全上限
                    break
            except Exception as e:
                return {"ok": False, "msg": str(e)}
        return {"ok": True, "items": items, "count": len(items)}

    # ──────────────────────────────────────────────
    #  递归列出分享文件（含子目录）
    # ──────────────────────────────────────────────
    def list_share_files_recursive(self, share_url: str, passwd: str = "",
                                   parent_id: str = "0") -> dict:
        result = self.list_share_files(share_url, passwd, parent_id)
        if not result.get("ok"):
            return result
        all_items = []
        folders = []
        for item in result.get("items", []):
            if item.get("is_dir"):
                folders.append(item)
            else:
                all_items.append(item)
        for folder in folders:
            sub = self.list_share_files_recursive(
                share_url, passwd, folder["id"])
            if sub.get("ok"):
                all_items.extend(sub.get("items", []))
        return {"ok": True, "items": all_items, "count": len(all_items)}

    # ──────────────────────────────────────────────
    #  转存到自己网盘 → 取直链
    # ──────────────────────────────────────────────
    def _ensure_transfer(self, share_url: str, share_passwd: str = "") -> dict:
        """确保分享已转存到自己网盘，返回 {"ok","dir_fid"}。同一分享只转存一次。"""
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        pwd_id = parsed["pwd_id"]
        with self._lock:
            if pwd_id in self._transferred:
                return {"ok": True, "dir_fid": self._transferred[pwd_id]}
            res = self.saver.ensure_transfer(share_url, share_passwd)
            if not res.get("ok"):
                return res
            self._transferred[pwd_id] = res["dir_fid"]
            return res

    def _own_download_url(self, fid: str) -> dict:
        """用自己网盘的 fid 取直链（无需 stoken）"""
        params = dict(COMMON_PARAMS)
        params.update({"sys": "win32", "ve": "2.5.56", "ut": "", "guid": ""})
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Accept-Language": "zh-CN",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "Cookie": self.cookie,
        }
        try:
            r = self._session.post(f"{FILE_HOST}/download", params=params,
                                   json={"fids": [fid], "resolutions": "2"},
                                   headers=headers, timeout=30)
            js = r.json()
        except Exception as e:
            return {"ok": False, "msg": f"获取自己网盘直链失败: {e}"}
        if js.get("code") not in (0, 200) and js.get("status") not in (0, 200, None):
            return {"ok": False, "msg": js.get("message", "获取自己网盘直链失败")}
        data = js.get("data", {})
        dl = data[0] if isinstance(data, list) and data else data
        if isinstance(dl, dict) and not dl.get("download_url") and isinstance(dl.get("data"), dict):
            dl = dl["data"]
        url = (dl or {}).get("download_url") or (dl or {}).get("url") or ""
        if not url:
            return {"ok": False, "msg": f"未获取到 download_url: {str(js)[:300]}"}
        return {"ok": True, "url": url,
                "headers": {
                    "User-Agent": headers["User-Agent"],
                    "Referer": "https://pan.quark.cn/",
                    "Cookie": self.cookie,
                }}

    def _get_url_via_qas(self, share_url: str, file_name: str,
                         share_passwd: str = "") -> dict:
        tr = self._ensure_transfer(share_url, share_passwd)
        if not tr.get("ok"):
            return {"ok": False, "msg": f"转存失败: {tr.get('msg')}"}
        found = self.saver.find_file(tr["dir_fid"], file_name)
        if not found.get("ok"):
            return {"ok": False, "msg": found.get("msg", "网盘中未找到文件")}
        res = self._own_download_url(found["fid"])
        if res.get("ok"):
            res["fid"] = found["fid"]
        return res

    def cleanup(self, share_url: str, file_name: str = "") -> dict:
        """下载完成后删除网盘里的临时文件（进回收站，仍占容量，需定期清空）"""
        if not file_name:
            return {"ok": False, "msg": "无需清理"}
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        dir_fid = self._transferred.get(parsed["pwd_id"])
        if not dir_fid:
            return {"ok": False, "msg": "未转存过，无需清理"}
        found = self.saver.find_file(dir_fid, file_name)
        if not found.get("ok"):
            return found
        res = self.saver.delete([found["fid"]])
        try:
            if res.get("ok") and self.saver.is_empty(dir_fid):
                self.saver.delete([dir_fid])
        except Exception:
            pass
        return res

    # ──────────────────────────────────────────────
    #  获取下载直链
    # ──────────────────────────────────────────────
    def get_share_download_url(self, share_url: str, file_id: str,
                               file_name: str = "", share_path: str = "",
                               share_passwd: str = "") -> dict:
        """获取夸克分享文件下载直链 download_url。

        流程：把分享转存到自己网盘 → 在自己网盘定位文件 → file/download 取直链。
        未传文件名时退回原逻辑（直接对分享文件取直链）。
        """
        if file_name:
            return self._get_url_via_qas(share_url, file_name, share_passwd)

        fid = share_path or file_id
        if not fid:
            return {"ok": False, "msg": "缺少文件 fid"}

        # 分享文件下载必须先获取 stoken（否则 file/download 不返回 download_url）
        parsed = self.parse_share_url(share_url) if share_url else None
        stoken = ""
        pwd_id = ""
        if parsed and parsed.get("ok"):
            pwd_id = parsed["pwd_id"]
            stoken_res = self._get_stoken(pwd_id, share_passwd or parsed.get("pwd", ""))
            if not stoken_res.get("ok"):
                return stoken_res
            stoken = stoken_res["stoken"]

        params = dict(COMMON_PARAMS)
        params.update({"sys": "win32", "ve": "2.5.56", "ut": "", "guid": ""})

        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Accept-Language": "zh-CN",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "Cookie": self.cookie,
        }

        def _post(ua):
            h = dict(headers)
            h["User-Agent"] = ua
            # 夸克 file/download 标准用法：fids + 分享 pwd_id/stoken 放 body（不放 query）
            body = {"fids": [fid], "resolutions": "2"}
            if stoken:
                body["pwd_id"] = pwd_id
                body["stoken"] = stoken
            try:
                r = self._session.post(
                    f"{FILE_HOST}/download", params=params,
                    json=body, headers=h, timeout=30)
                if r.status_code >= 400:
                    # 仍然尝试解析 JSON，保留 code/message 便于识别风控码(如23018)
                    try:
                        _j = r.json()
                        _j["_http"] = r.status_code
                        return _j
                    except Exception:
                        return {"ok": False, "msg": f"HTTP {r.status_code}",
                                "raw_text": r.text[:500]}
                try:
                    return r.json()
                except Exception:
                    return {"ok": False, "msg": "返回非JSON",
                            "raw_text": r.text[:500]}
            except Exception as e:
                return {"ok": False, "msg": str(e)}

        js = _post(UA)
        # 风控重试
        if isinstance(js, dict) and js.get("code") == 23018:
            js = _post(UA_QUARK_APP)

        if not isinstance(js, dict):
            raw = js.get("raw_text", "") if isinstance(js, dict) else ""
            return {"ok": False, "msg": "下载接口返回异常", "raw_text": raw}

        if js.get("code") not in (0, 200) and js.get("status") not in (0, 200, None):
            code = js.get("code")
            msg = js.get("message", "获取直链失败")
            if code == 23018:
                msg = ("夸克风控/下载限制(23018)：通常是账号未登录或游客态被限制单文件大小，"
                       "请用完整登录的夸克 Cookie（含 kps 字段）重试；若在浏览器能正常下载，"
                       "可复制该次请求的完整 Cookie 填入客户端")
            return {"ok": False, "msg": msg, "raw_text": str(js)[:500]}

        data = js.get("data", {})
        if isinstance(data, list):
            dl = data[0] if data else {}
        else:
            dl = data
        # 兼容 data 内再次嵌套 data 的情况
        if not dl and isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                dl = inner[0] if inner else {}
            elif isinstance(inner, dict):
                dl = inner
        # 兼容更多返回结构：data 可能是 {list:[...]}
        if not dl and isinstance(data, dict):
            lst = data.get("list") or data.get("file_list")
            if isinstance(lst, list) and lst:
                dl = lst[0]
        # 兼容 data[0].data 嵌套
        if not dl and isinstance(data, list) and data:
            _sub = data[0]
            if isinstance(_sub, dict):
                _inner = _sub.get("data")
                if isinstance(_inner, list) and _inner:
                    dl = _inner[0]
                elif isinstance(_inner, dict):
                    dl = _inner
        url = (dl.get("download_url") or dl.get("url") or dl.get("durl")
               or dl.get("downloadUrl") or "")
        if not url:
            # 把接口真实返回带出来，便于定位
            snippet = str(js)[:600]
            print(f"[QUARK-DL] 未取到直链 fid={fid!r} 返回={snippet}", file=sys.stderr, flush=True)
            return {"ok": False,
                    "msg": f"未获取到 download_url (code={js.get('code')}, status={js.get('status')}, message={js.get('message')})",
                    "raw_text": snippet}
        return {"ok": True, "url": url,
                "headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Referer": "https://pan.quark.cn/",
                    "Cookie": self.cookie,
                }}
