"""
夸克网盘分享链接客户端（服务端版）
- 解析分享链接（https://pan.quark.cn/s/xxxx）
- 列出分享文件（sharepage/detail，支持分页/嵌套）
- 获取下载直链（file/download，返回 download_url）
- 账号验证 / 容量查询（走个人云盘接口）

鉴权方式：登录 cookie（一个 cookie 通吃所有夸克分享）
直链有效期较短且可能限速，因此采用"下载时即时取链"。

接口参考社区成熟实现（quark-auto-save / QuarkPanTool），均为夸克非公开分享 API。
"""
import re
import time
import json
import random
import requests

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


def _ts13() -> int:
    return int(time.time() * 1000)


def _rand_dt() -> int:
    return random.randint(200, 9999)


class QuarkClient:
    """夸克网盘分享链接客户端（服务端），接口与 TianyiClient / YidongClient 对齐"""

    def __init__(self, cookie: str):
        self.cookie = (cookie or "").strip()
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
        m = re.search(r'pan\.quark\.cn/s/([A-Za-z0-9]+)', url)
        if not m:
            m = re.search(r'quark\.cn/s/([A-Za-z0-9]+)', url)
        if not m:
            return {"ok": False, "msg": "无法从链接中提取夸克分享ID"}
        pwd_id = m.group(1)
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
                    is_dir = bool(f.get("dir", False)) or f.get("file_type") == 1 \
                        or f.get("type") == "dir"
                    items.append({
                        "id": f.get("fid") or f.get("file_id", ""),
                        "name": f.get("file_name") or f.get("name", "未知"),
                        "size": int(f.get("size", 0) or 0),
                        "is_dir": is_dir,
                        "type": "folder" if is_dir else "file",
                        "path": f.get("fid") or f.get("file_id", ""),
                    })
                total = data.get("_total", 0)
                fetched = len(items)
                if len(file_list) < 50 or (total and fetched >= total):
                    break
                page += 1
                if page > 50:
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
    #  获取下载直链
    # ──────────────────────────────────────────────
    def get_share_download_url(self, share_url: str, file_id: str,
                               file_name: str = "", share_path: str = "",
                               share_passwd: str = "") -> dict:
        """获取夸克分享文件下载直链 download_url。

        夸克下载流程：file/download (POST, 传 fid) -> 返回 download_url。
        风控码 23018 时换 UA 重试一次。
        """
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
            # 分享文件需携带 pwd_id / stoken
            body = {"fids": [fid], "resolutions": "2"}
            if stoken:
                body["pwd_id"] = pwd_id
                body["stoken"] = stoken
            try:
                r = self._session.post(
                    f"{FILE_HOST}/download", params=params,
                    json=body, headers=h, timeout=30)
                if r.status_code >= 400:
                    return {"ok": False, "msg": f"HTTP {r.status_code}"}
                return r.json()
            except Exception as e:
                return {"ok": False, "msg": str(e)}

        js = _post(UA)
        if isinstance(js, dict) and js.get("code") == 23018:
            js = _post(UA_QUARK_APP)

        if not isinstance(js, dict):
            return {"ok": False, "msg": "下载接口返回异常"}

        if js.get("code") not in (0, 200) and js.get("status") not in (0, 200, None):
            return {"ok": False, "msg": js.get("message", "获取直链失败")}

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
        url = dl.get("download_url") or dl.get("url", "")
        if not url:
            return {"ok": False, "msg": "未获取到 download_url", "raw": js}
        return {"ok": True, "url": url, "size": int(dl.get("size", 0) or 0)}

    # ──────────────────────────────────────────────
    #  兼容性接口（与 TianyiClient / YidongClient 对齐）
    # ──────────────────────────────────────────────
    def get_download_url(self, file_id: str, share_url: str = "",
                         share_passwd: str = "") -> dict:
        """统一下载链路口：支持"通过分享链接取直链"的便捷封装"""
        return self.get_share_download_url(
            share_url, file_id, share_passwd=share_passwd,
            share_path=file_id)

    def list_files(self, folder_id: str = "0", page: int = 1, size: int = 50) -> dict:
        """夸克没有"个人云盘文件浏览"需求（仅用于分享链接），
        这里返回空列表以兼容工厂统一调用。"""
        return {"ok": True, "items": [], "count": 0, "page": page}

    def test(self) -> dict:
        """验证 cookie 有效性 —— 通过分享 token 接口探测"""
        try:
            # 用一个最简探测：访问个人云盘用户信息接口
            url = f"{FILE_HOST}/info"
            params = dict(COMMON_PARAMS)
            r = self._session.get(
                url, params=params,
                headers={"Cookie": self.cookie},
                timeout=15,
            )
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}
            d = r.json()
            if d.get("status") == 200 or d.get("code") in (0, 200, None):
                return {"ok": True,
                        "user": d.get("data", {}).get("nickname")
                                or d.get("data", {}).get("user_name", "")}
            return {"ok": False, "msg": d.get("message", "验证失败")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_capacity(self) -> dict:
        """获取账号容量信息"""
        try:
            url = f"{FILE_HOST}/info"
            params = dict(COMMON_PARAMS)
            r = self._session.get(
                url, params=params,
                headers={"Cookie": self.cookie},
                timeout=15,
            )
            if r.status_code >= 400:
                return {"total": 0, "used": 0, "available": 0,
                        "error": f"HTTP {r.status_code}"}
            d = r.json()
            data = d.get("data", {})
            total = int(data.get("total_space") or data.get("totalSpace") or 0)
            used = int(data.get("used_space") or data.get("usedSpace") or 0)
            if total > 0:
                return {"total": total, "used": used, "available": max(0, total - used)}
            return {"total": 0, "used": 0, "available": 0, "error": "无容量信息"}
        except Exception as e:
            return {"total": 0, "used": 0, "available": 0, "error": str(e)[:80]}

    def get_user_info(self) -> dict:
        """获取用户信息（与 WPS 客户端接口对齐，供容量页使用）"""
        try:
            url = f"{FILE_HOST}/info"
            params = dict(COMMON_PARAMS)
            r = self._session.get(
                url, params=params,
                headers={"Cookie": self.cookie},
                timeout=15,
            )
            if r.status_code >= 400:
                return None
            d = r.json()
            if d.get("status") == 200 or d.get("code") in (0, 200, None):
                return d.get("data", {})
            return None
        except Exception:
            return None
