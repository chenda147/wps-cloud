"""
多平台云盘客户端
支持: WPS | 天翼云盘 (cloud.189.cn) | 移动云盘 (和彩云/139)
"""
import json
import time
import logging
import re
import base64
import hashlib
import traceback
import random
import string
import os
import requests
from urllib.parse import quote, urljoin
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

logger = logging.getLogger(__name__)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# ─────────────────── 天翼云盘 ───────────────────
class TianyiClient:
    """天翼云盘 (cloud.189.cn) 客户端，Cookie 登录"""
    BASE = "https://cloud.189.cn"

    def __init__(self, cookie_str: str):
        self._ck = cookie_str
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.BASE + "/",
        })
        # 解析 cookie 字符串
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                self.s.cookies.set(k.strip(), v.strip(), domain=".189.cn")

    def test(self) -> dict:
        """验证 cookie 有效性"""
        try:
            r = self.s.get(f"{self.BASE}/api/open/user/getUserInfo.action", timeout=10)
            d = r.json()
            if d.get("res_code") == 0 or "account" in str(d):
                return {"ok": True, "user": d.get("account", d.get("data", {}))}
            return {"ok": False, "msg": d.get("res_message", "验证失败")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_capacity(self) -> dict:
        """获取容量信息（总容量/已用/可用，字节）"""
        import logging
        _log = logging.getLogger(__name__)
        try:
            r = self.s.get(f"{self.BASE}/api/open/user/getUserInfo.action", timeout=10)
            d = r.json()
        except Exception as e:
            return {"total": 0, "used": 0, "available": 0, "error": str(e)}

        def _extract(obj: dict) -> tuple:
            total = used = 0
            for k, v in obj.items():
                if not isinstance(v, (int, float, str)):
                    continue
                try:
                    val = int(v)
                except (ValueError, TypeError):
                    continue
                if val <= 0:
                    continue
                k_lower = k.lower()
                if any(x in k_lower for x in ('capacity', 'totalsize', 'totalspace',
                    'spacesize', 'totalspacecapacity', 'total')):
                    if val > total: total = val
                if any(x in k_lower for x in ('usesize', 'usedspace', 'usedsize',
                    'usecapacity', 'used')):
                    if val > used: used = val
            return total, used

        data = d.get("data", d)
        if isinstance(data, dict):
            total, used = _extract(data)
            if total > 0:
                return {"total": total, "used": used, "available": max(0, total - used)}
            _log.warning(f"Tianyi capacity: no fields found in keys={list(data.keys())}")
            return {"total": 0, "used": 0, "available": 0,
                    "error": f"无法解析容量(keys: {list(data.keys())})"}
        return {"total": 0, "used": 0, "available": 0, "error": "数据格式异常"}

    def list_files(self, folder_id: str = "-11", page: int = 1, size: int = 100) -> dict:
        """列举文件夹内容
        folder_id: 文件夹 ID，根目录用 "-11"
        """
        try:
            params = {
                "folderId": folder_id,
                "fileType": 0,     # 0=全部, 1=图片, 2=文档, 3=视频, 4=音乐, 5=其他
                "pageNum": page,
                "pageSize": size,
                "orderBy": 1,      # 1=按文件名, 2=按大小, 3=按时间
                "descending": True,
                "mediaType": 0,
            }
            r = self.s.post(
                f"{self.BASE}/api/open/file/listFiles.action",
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            d = r.json()
            items = []
            for f in (d.get("data") or [] if isinstance(d.get("data"), list) else []):
                is_dir = f.get("fileType") == 1 or f.get("isFolder") == True  # fileType=1 是文件夹
                items.append({
                    "id": str(f.get("id") or f.get("fileId", "")),
                    "name": f.get("name") or f.get("fileName", "未知"),
                    "size": int(f.get("fileSize") or f.get("size") or 0),
                    "is_dir": is_dir,
                    "type": "folder" if is_dir else "file",
                    "mtime": str(f.get("createTime") or f.get("lastOpTime", "")),
                })
            return {
                "ok": True,
                "items": items,
                "count": d.get("count") or d.get("total", len(items)),
                "page": page,
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_download_url(self, file_id: str) -> dict:
        """获取文件下载直链"""
        try:
            r = self.s.get(
                f"{self.BASE}/api/open/file/getFileDownloadUrl.action",
                params={"fileId": file_id},
                headers={"Accept": "application/json;charset=UTF-8"},
                timeout=15,
            )
            d = r.json()
            url = d.get("fileDownloadUrl") or d.get("data", {}).get("fileDownloadUrl")
            if url:
                return {"ok": True, "url": url}
            return {"ok": False, "msg": d.get("res_message", "获取下载链接失败")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}


# ─────────────────── 移动云盘 (和彩云 / 139) ───────────────────

def _encode_uri_component(s: str) -> str:
    """模拟 JS encodeURIComponent，AList 使用的变体"""
    r = quote(s, safe='')
    r = r.replace('+', '%20')
    r = r.replace('%21', '!')
    r = r.replace('%27', "'")
    r = r.replace('%28', '(')
    r = r.replace('%29', ')')
    r = r.replace('%2A', '*')
    return r


def _cal_sign(body: str, ts: str, rand_str: str) -> str:
    """Mcloud-Sign 签名算法 (与 AList 139 驱动完全一致)"""
    # 1. URL 编码
    encoded = _encode_uri_component(body)
    # 2. 拆分排序
    chars = sorted(encoded)
    sorted_str = ''.join(chars)
    # 3. Base64 编码
    b64 = base64.b64encode(sorted_str.encode('utf-8')).decode()
    # 4. MD5(body部分) + MD5(ts:rand)
    md5_body = hashlib.md5(b64.encode()).hexdigest()
    md5_ts_rand = hashlib.md5(f"{ts}:{rand_str}".encode()).hexdigest()
    # 5. MD5(拼接)转大写
    final = hashlib.md5((md5_body + md5_ts_rand).encode()).hexdigest().upper()
    return final


class YidongClient:
    """
    移动云盘 (中国移动和彩云 / 139 云盘)
    鉴权方式: Authorization: Basic + Mcloud-Sign 签名
    凭据格式: base64(pc:手机号:密码|1|RCS|时间戳|SSO_Token)
    参考: AList 139 驱动实现
    """
    BASE = "https://yun.139.com"
    ROUTE_HOST = "https://user-njs.yun.139.com"

    def __init__(self, token_str: str):
        self._raw = token_str.strip()
        self._phone = ""
        self._sso_token = ""
        self._personal_host = None

        # base64 解码并解析凭据
        try:
            decoded = base64.b64decode(self._raw).decode("utf-8", errors="ignore")
        except Exception:
            decoded = self._raw
        self._decoded = decoded

        # 解析: pc:手机:密码|type|RCS|timestamp|SSO_Token
        try:
            if decoded.startswith("pc:"):
                rest = decoded[3:]
                colon_idx = rest.index(":")
                self._phone = rest[:colon_idx]
                after_phone = rest[colon_idx + 1:]
                if "|" in after_phone:
                    last_pipe = after_phone.rindex("|")
                    self._sso_token = after_phone[last_pipe + 1:]
        except (ValueError, IndexError):
            pass

    def _personal_headers(self, body: str, is_route: bool = False) -> dict:
        """构建 personal cloud API 请求头 (含 Mcloud-Sign 签名) — 严格对齐 AList"""
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sign = _cal_sign(body, ts, rand_str)

        h = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self._raw}",
            "Caller": "web",
            "CMS-DEVICE": "default",
            "Content-Type": "application/json;charset=UTF-8",
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "Mcloud-Route": "001",
            "mcloud-sign": f"{ts},{rand_str},{sign}",
            "mcloud-version": "7.14.0",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk|||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
        }
        # personal 专用头
        if not is_route:
            h["X-Yun-Api-Version"] = "v1"
        return h

    def _get_personal_host(self) -> str:
        """通过路由策略获取个人云主机域名"""
        if self._personal_host:
            return self._personal_host
        try:
            body = json.dumps({
                "userInfo": {"userType": 1, "accountType": 1,
                             "accountName": self._phone},
                "modAddrType": 1,
            }, separators=(',', ':'))
            headers = self._personal_headers(body, is_route=True)
            headers["Inner-Hcy-Router-Https"] = "1"
            r = requests.post(
                f"{self.ROUTE_HOST}/user/route/qryRoutePolicy",
                data=body,
                headers=headers,
                timeout=10,
            )
            logger.warning(f"[Yidong] Route API status={r.status_code}, body={r.text[:300]}")
            if r.status_code < 400:
                d = r.json()
                # AList 响应结构: {"code":"0","data":{"routePolicyList":[...]}}
                routes = (d.get("data") or {}).get("routePolicyList") or []
                try:
                    logger.warning(f"[Yidong] Route items: {[(it.get('modName'), (it.get('httpsUrl') or '')[:30]) for it in routes]}")
                except Exception:
                    logger.warning(f"[Yidong] Route items (raw): {routes}")
                for item in routes:
                    if item.get("modName") == "personal" and item.get("httpsUrl"):
                        # AList 直接存完整 httpsUrl (含 /hcy)，
                        # 后续拼接 /file/list → /hcy/file/list
                        self._personal_host = item["httpsUrl"]
                        logger.warning(f"[Yidong] Personal host resolved: {self._personal_host}")
                        return self._personal_host
                logger.warning(f"[Yidong] No personal route found. code={d.get('code')}, msg={d.get('message','')}")
            else:
                logger.warning(f"[Yidong] Route API returned HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"[Yidong] Route API exception: {e}")
            logger.warning(f"[Yidong] Traceback:\n{traceback.format_exc()}")
        logger.warning("[Yidong] Falling back to BASE host — personal cloud will NOT work")
        return self.BASE

    def _personal_post(self, path: str, payload: dict = None) -> dict:
        """发送带签名的 personal cloud POST 请求"""
        host = self._get_personal_host()
        body = json.dumps(payload, separators=(',', ':')) if payload else ""
        headers = self._personal_headers(body)
        r = requests.post(f"{host}{path}", data=body, headers=headers, timeout=15)
        if r.status_code >= 400:
            return {"code": "-1", "message": f"HTTP {r.status_code}"}
        try:
            return r.json()
        except Exception:
            return {"code": "-1", "message": "非 JSON 响应"}

    def test(self) -> dict:
        """验证凭据有效性 — 新版个人云 POST /user/info"""
        try:
            host = self._get_personal_host()
            if host == self.BASE:
                return {"ok": False, "msg": "无法获取个人云主机"}
            r = self._personal_post("/user/info")
            if r.get("code") in ("0", 0, "0000") or r.get("success"):
                return {"ok": True, "user": r.get("data", r)}
            return {"ok": False, "msg": r.get("message", "验证失败")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_capacity(self) -> dict:
        """获取容量 — 新版个人云 POST /user/info"""
        try:
            host = self._get_personal_host()
            if host == self.BASE:
                return {"total": 0, "used": 0, "available": 0, "error": "无法获取个人云主机"}

            r = self._personal_post("/user/info")

            if r.get("code") not in ("0", 0, "0000") and not r.get("success"):
                code = r.get("code", "?")
                msg = r.get("message", "")
                return {"total": 0, "used": 0, "available": 0,
                        "error": f"code={code};msg={msg[:40]}"}

            d = r

            def _extract(obj):
                total = used = 0
                for k, v in obj.items():
                    if not isinstance(v, (int, float)) and not (isinstance(v, str) and v.isdigit()):
                        continue
                    try:
                        val = int(v)
                    except (ValueError, TypeError):
                        continue
                    if val <= 0 or val < 524288:
                        continue
                    k_lower = k.lower()
                    if any(kw in k_lower for kw in ('space', 'capacity', 'quota', 'total', 'size')):
                        if val > total:
                            total = val
                    if any(kw in k_lower for kw in ('used', 'use')):
                        if val > used:
                            used = val
                return total, used

            data = d.get("data", d) if isinstance(d.get("data"), dict) else d
            total, used = _extract(data)
            for _, v in data.items():
                if isinstance(v, dict):
                    t2, u2 = _extract(v)
                    if t2 > total: total = t2
                    if u2 > used: used = u2

            if total > 0:
                return {"total": total, "used": used, "available": max(0, total - used)}

            # 如果没匹配到容量字段，返回实际 keys 用于调试
            keys = list(data.keys())[:10]
            code = d.get("code", "?")
            msg = d.get("message", "")
            return {"total": 0, "used": 0, "available": 0,
                    "error": f"code={code};msg={msg[:40]};keys={keys}"}

        except Exception as e:
            import logging
            _log = logging.getLogger(__name__)
            _log.warning(f"Yidong capacity error: {e}")
            return {"total": 0, "used": 0, "available": 0, "error": str(e)[:80]}

    def list_files(self, folder_id: str = "root", page: int = 1, size: int = 60) -> dict:
        """列出文件 — 新版个人云 POST /file/list，对齐 AList personalGetFiles"""
        try:
            # 新版 API: 用 parentFileId + pageInfo(pageCursor/pageSize) 分页
            # folder_id: 目录 ID，根目录为 "root"
            parent_id = "root" if folder_id in ("root", "", None) else folder_id
            payload = {
                "imageThumbnailStyleList": ["Small", "Large"],
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "pageInfo": {
                    "pageCursor": "",
                    "pageSize": size,
                },
                "parentFileId": parent_id,
            }
            host = self._get_personal_host()
            body = json.dumps(payload, separators=(',', ':'))
            headers = self._personal_headers(body)
            url = f"{host}/file/list"
            logger.warning(f"[Yidong] list_files POST {url} body={body[:200]}")
            r = requests.post(url, data=body, headers=headers, timeout=30)
            logger.warning(f"[Yidong] list_files status={r.status_code} body={r.text[:500]}")
            d = r.json() if r.status_code < 400 else {}
            if not d.get("success") and d.get("code") not in ("0", 0, "0000"):
                return {"ok": False, "msg": d.get("message", "请求失败")}

            # 新版响应: data.items[], 字段为 fileId/name/size/type/updatedAt/createdAt
            data = d.get("data", {})
            items = []
            for item in data.get("items", []):
                is_dir = (item.get("type") == "folder")
                items.append({
                    "id": str(item.get("fileId") or item.get("id", "")),
                    "name": item.get("name", "未知"),
                    "size": int(item.get("size") or 0),
                    "is_dir": is_dir,
                    "type": "folder" if is_dir else "file",
                    "mtime": str(item.get("updatedAt") or item.get("modifyTime", "")),
                })
            return {
                "ok": True, "items": items,
                "count": len(items),
                "page": page,
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_download_url(self, file_id: str) -> dict:
        """获取文件下载链接 — 新版个人云 POST /file/download"""
        try:
            host = self._get_personal_host()
            payload = {"fileId": file_id}
            body = json.dumps(payload, separators=(',', ':'))
            headers = self._personal_headers(body)
            r = requests.post(f"{host}/file/download", data=body, headers=headers, timeout=15)
            d = r.json() if r.status_code < 400 else {}
            if d.get("code") not in ("0", 0, "0000"):
                return {"ok": False, "msg": d.get("message", "获取下载链接失败")}
            dl_info = d.get("data", {})
            url = dl_info.get("url") or dl_info.get("fileUrl") or dl_info.get("downloadUrl", "")
            if url:
                return {"ok": True, "url": url}
            return {"ok": False, "msg": "未找到下载地址"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ───────────── 分享链接功能 ─────────────
    SHARE_HOST = "https://share-kd-njs.yun.139.com"
    SHARE_AES_KEY = b"PVGDwmcvfs1uV3d1"  # AES-CBC 密钥
    SHARE_MCLOUD_SKEY = "hj/tjWDc9azlXEtIrA+X6jG2fC2n/YmYStxLIrVHxhC6LB5bNWnN/jpraPzV2UMALopZoSPTu+ImGMQr8MiIqO04TNRabSG8aQg57nYEAR7oUXgVCfMtGPse2WkIzSUkjn8LNRzszF+6hCwhXo0CA1/vZIonJEeo+LVUCQmo64A="

    @staticmethod
    def parse_share_url(url: str) -> dict:
        """解析分享链接，提取 linkID
        支持格式:
         - https://yun.139.com/shareweb/#/w/i/{linkID}
         - https://yun.139.com/w/share/{linkID}
         - https://caiyun.139.com/m/i?{shortCode}  (短链接直接作为 linkID)
        """
        url = url.strip()
        link_id = ""

        # yun.139.com/shareweb/#/w/i/xxx
        m = re.search(r'/w/i/([A-Za-z0-9_]+)', url)
        if m:
            link_id = m.group(1)
        # caiyun.139.com/w/i/xxx
        elif 'caiyun.139.com/w/i/' in url:
            m = re.search(r'caiyun\.139\.com/w/i/([A-Za-z0-9_]+)', url)
            if m:
                link_id = m.group(1)
        # caiyun.139.com/m/i?xxx  (短链接，直接用作 linkID)
        elif 'caiyun.139.com/m/i?' in url:
            m = re.search(r'caiyun\.139\.com/m/i\?([A-Za-z0-9_]+)', url)
            if m:
                link_id = m.group(1)

        if not link_id:
            return {"ok": False, "msg": "无法从分享链接中提取 linkID"}
        return {"ok": True, "linkID": link_id, "url": url}

    def _get_account_name(self) -> str:
        """从 Token 中提取账号（手机号）"""
        if self._phone:
            return self._phone
        try:
            decoded = base64.b64decode(self._raw).decode("utf-8", errors="ignore")
            if decoded.startswith("pc:"):
                rest = decoded[3:]
                colon_idx = rest.index(":")
                return rest[:colon_idx]
        except Exception:
            pass
        return ""

    @staticmethod
    def _encode_uri_component(s: str) -> str:
        """JS encodeURIComponent 的 Python 实现"""
        result = []
        for c in s:
            if c in "!'()* ":
                mapping = {'!': '%21', "'": '%27', '(': '%28', ')': '%29', '*': '%2A', ' ': '%20'}
                result.append(mapping[c])
            elif c.isascii() and (c.isalnum() or c in '-_.~'):
                result.append(c)
            else:
                for b in c.encode('utf-8'):
                    result.append(f'%{b:02X}')
        return ''.join(result)

    @staticmethod
    def _calc_sign(body_str: str, ts: str, rand_str: str) -> str:
        """cloud139 mcloud-sign 签名算法"""
        encoded = YidongClient._encode_uri_component(body_str)
        sorted_str = ''.join(sorted(encoded))
        body_base64 = base64.b64encode(sorted_str.encode()).decode()
        hash1 = hashlib.md5(body_base64.encode()).hexdigest()
        hash2 = hashlib.md5(f"{ts}:{rand_str}".encode()).hexdigest()
        return hashlib.md5((hash1 + hash2).encode()).hexdigest().upper()

    def _gen_mcloud_sign(self, body_str: str) -> str:
        """生成 mcloud-sign 头值"""
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        sign = self._calc_sign(body_str, ts, rand_str)
        return f"{ts},{rand_str},{sign}"

    def _share_post(self, path: str, data: dict) -> dict:
        """发送分享接口请求（明文 JSON，对齐 139 官方 API）"""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self._raw}",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": USER_AGENT,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
            "caller": "web",
            "x-m4c-caller": "PC",
            "mcloud-client": "10701",
            "mcloud-version": "7.17.2",
            "mcloud-channel": "1000101",
            "CMS-DEVICE": "default",
            "X-Deviceinfo": "||9|7.17.2|chrome|120.0.0.0|||windows 10||zh-CN|||",
        }
        try:
            r = requests.post(
                f"{self.SHARE_HOST}{path}",
                json=data,
                headers=headers,
                timeout=30,
            )
            logger.warning(f"[YidongShare] _share_post {path} status={r.status_code} body[:600]={r.text[:600]}")
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}", "raw": r.text[:300]}

            try:
                resp = r.json()
            except json.JSONDecodeError:
                return {"ok": False, "msg": "响应非 JSON", "raw": r.text[:300]}

            # 检查业务状态码
            result_code = resp.get("resultCode", "") or resp.get("result", {}).get("resultCode", "")
            if result_code and str(result_code) != "0":
                desc = resp.get("desc", "") or resp.get("result", {}).get("desc", "")
                return {"ok": False, "msg": f"{desc}(code={result_code})",
                        "raw_resp": resp}
            return {"ok": True, "data": resp}
        except json.JSONDecodeError as e:
            return {"ok": False, "msg": f"JSON解析失败: {e}", "raw": r.text[:300]}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def _share_post_signed(self, path: str, data: dict) -> dict:
        """发送带 mcloud-sign 签名的分享接口请求（dlFromOutLinkV3 等需要）"""
        body_str = json.dumps(data, separators=(',', ':'))
        mcloud_sign = self._gen_mcloud_sign(body_str)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self._raw}",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
            "caller": "web",
            "x-m4c-caller": "PC",
            "mcloud-client": "10701",
            "mcloud-version": "7.14.0",
            "mcloud-channel": "1000101",
            "mcloud-route": "001",
            "mcloud-sign": mcloud_sign,
            "CMS-DEVICE": "default",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-src": "10002",
            "x-yun-api-version": "v1",
            "x-yun-app-channel": "10000034",
            "x-yun-channel-source": "10000034",
            "x-yun-client-info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "x-yun-module-type": "100",
            "x-SvcType": "1",
            "x-yun-svc-type": "1",
            "Inner-Hcy-Router-Https": "1",
        }
        try:
            r = requests.post(
                f"{self.SHARE_HOST}{path}",
                json=data,
                headers=headers,
                timeout=30,
            )
            logger.warning(f"[YidongShare] _share_post_signed {path} status={r.status_code} body[:600]={r.text[:600]}")
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}", "raw": r.text[:300]}

            try:
                resp = r.json()
            except json.JSONDecodeError:
                return {"ok": False, "msg": "响应非 JSON", "raw": r.text[:300]}

            result_code = resp.get("resultCode", "") or resp.get("result", {}).get("resultCode", "")
            if result_code and str(result_code) != "0":
                desc = resp.get("desc", "") or resp.get("result", {}).get("desc", "")
                return {"ok": False, "msg": f"{desc}(code={result_code})",
                        "raw_resp": resp}
            return {"ok": True, "data": resp}
        except json.JSONDecodeError as e:
            return {"ok": False, "msg": f"JSON解析失败: {e}", "raw": r.text[:300]}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_share_files(self, share_url: str, passwd: str = "",
                         folder_id: str = "root") -> dict:
        """从分享链接列出文件列表"""
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        link_id = parsed["linkID"]
        # 根目录时 pCaID 传 "root"（官方前端 catalogID="root" 的行为；空串会被服务端判为 pCaID不能为空 → code=200000400）
        parent_id = "root" if folder_id in ("root", "", None) else folder_id

        account_name = self._get_account_name()
        data = {
            "getOutLinkInfoReq": {
                "account": account_name,
                "linkID": link_id,
                "passwd": passwd,
                "caSrt": 0,
                "coSrt": 0,
                "srtDr": 1,
                "bNum": 1,
                "pCaID": parent_id,
                "eNum": 200,
            },
            "commonAccountInfo": {"account": account_name, "accountType": 1},
        }
        result = self._share_post(
            "/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6",
            data,
        )
        if not result.get("ok"):
            return result

        # 解析文件列表
        resp_data = result.get("data", {})
        share_data = resp_data.get("getOutLinkInfoRes", resp_data)
        if isinstance(share_data, dict) and share_data.get("result"):
            # data 嵌套在 getOutLinkInfoRes.data 下
            share_data = share_data.get("data", share_data)
        elif isinstance(share_data, dict):
            share_data = share_data.get("data", share_data)

        items = []
        # 文件夹（id 优先用 path，与前端 pCaID=catalogID=path 的行为一致；path 为空时回退 caID）
        for folder in share_data.get("caLst", []) or share_data.get("CaLst", []):
            fid = folder.get("path") or folder.get("caID") or folder.get("CaID", "")
            items.append({
                "id": fid,
                "name": folder.get("caName") or folder.get("CaName", "未知"),
                "size": 0,
                "is_dir": True,
                "type": "folder",
                "mtime": folder.get("udTime") or folder.get("UdTime", ""),
                "path": folder.get("path", ""),  # 转存用的原始 path
            })
        # 文件
        for f in share_data.get("coLst", []) or share_data.get("CoLst", []):
            fid = f.get("coID") or f.get("CoID", "")
            items.append({
                "id": fid,
                "name": f.get("coName") or f.get("CoName", "未知"),
                "size": int(f.get("coSize") or f.get("CoSize") or 0),
                "is_dir": False,
                "type": "file",
                "mtime": f.get("udTime") or f.get("UdTime", ""),
                "path": f.get("path", ""),  # 转存用的原始 path
            })
        return {"ok": True, "items": items, "count": len(items)}

    def list_share_files_recursive(self, share_url: str, passwd: str = "",
                                   parent_id: str = "root") -> dict:
        """递归遍历分享链接下所有目录，返回所有文件（扁平列表）"""
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

        # 递归遍历每个子文件夹
        for folder in folders:
            sub = self.list_share_files_recursive(
                share_url, passwd, folder["id"]
            )
            if sub.get("ok"):
                all_items.extend(sub.get("items", []))

        return {"ok": True, "items": all_items, "count": len(all_items)}

    def _save_file_to_personal(self, share_url: str, file_path: str,
                                passwd: str = "") -> dict:
        """将分享文件转存到个人云盘根目录
        file_path: 分享 API 返回的 path 字段（非 coID）
        """
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        link_id = parsed["linkID"]

        account_name = self._get_account_name()
        need_pwd = bool(passwd)
        data = {
            "createOuterLinkBatchOprTaskReq": {
                "msisdn": account_name,
                "taskType": 1,
                "linkID": link_id,
                "needPassword": need_pwd,
                "passwd": passwd if need_pwd else "",
                "taskInfo": {
                    "linkID": link_id,
                    "needPassword": need_pwd,
                    "passwd": passwd if need_pwd else "",
                    "contentInfoList": [file_path],
                    "catalogInfoList": [],
                    "newCatalogID": "root",
                }
            }
        }
        result = self._share_post(
            "/yun-share/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask",
            data,
        )
        if not result.get("ok"):
            return {"ok": False, "msg": "转存失败: " + result.get("msg", "未知错误")}
        return {"ok": True, "msg": "转存成功"}

    def get_share_download_url(self, share_url: str, file_id: str,
                                file_name: str = "", share_path: str = "",
                                share_passwd: str = "") -> dict:
        """获取分享文件的直链下载地址（通过 dlFromOutLinkV3，AES 加密请求体）

        share_path: 从 getOutLinkInfoV6 返回的 path 字段（如 "xxxfolder/xxxfile"），必须提供
        """
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        link_id = parsed["linkID"]

        account_name = self._get_account_name()

        # 请求体（对齐浏览器 HAR 包解密结果）
        body_data = {
            "dlFromOutLinkReqV3": {
                "account": account_name,
                "linkID": link_id,
                "coIDLst": {
                    "item": [share_path] if share_path else [file_id],
                },
            },
            "commonAccountInfo": {"account": account_name, "accountType": 1},
        }

        body_str = json.dumps(body_data, separators=(',', ':'))
        logger.warning(f"[YidongShare] dlFromOutLinkV3 plaintext body: {body_str}")

        # AES-CBC 加密
        iv = os.urandom(16)
        cipher = AES.new(self.SHARE_AES_KEY, AES.MODE_CBC, iv)
        encrypted = cipher.encrypt(pad(body_str.encode('utf-8'), AES.block_size))
        encrypted_b64 = base64.b64encode(iv + encrypted).decode()

        # 生成 mcloud-sign
        mcloud_sign = self._gen_mcloud_sign(body_str)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self._raw}",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
            "caller": "web",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "mcloud-client": "10702",
            "mcloud-version": "7.13.3",
            "mcloud-channel": "1000101",
            "mcloud-route": "001",
            "mcloud-sign": mcloud_sign,
            "mcloud-skey": self.SHARE_MCLOUD_SKEY,
            "CMS-DEVICE": "default",
            "x-DeviceInfo": "||9|12.27.0|chrome|150.0.0.0|b23fe89e3d289a4c4d73d4288427d3e2||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10213406",
            "x-inner-ntwk": "2",
            "x-yun-api-version": "v1",
            "x-yun-app-channel": "10213406",
            "x-yun-channel-source": "10213406",
            "x-yun-client-info": "||9|7.13.3|chrome|150.0.0.0|b23fe89e3d289a4c4d73d4288427d3e2||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "x-yun-module-type": "100",
            "x-SvcType": "1",
            "x-yun-svc-type": "1",
            "Inner-Hcy-Router-Https": "1",
            "hcy-cool-flag": "1",
        }

        try:
            r = requests.post(
                f"{self.SHARE_HOST}/yun-share/richlifeApp/devapp/IOutLink/dlFromOutLinkV3",
                data=encrypted_b64,
                headers=headers,
                timeout=30,
            )
            logger.warning(f"[YidongShare] dlFromOutLinkV3 status={r.status_code} len={len(r.text)}")

            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}

            # 解密响应
            raw_resp = base64.b64decode(r.text)
            resp_iv = raw_resp[:16]
            resp_ct = raw_resp[16:]
            resp_cipher = AES.new(self.SHARE_AES_KEY, AES.MODE_CBC, resp_iv)
            decrypted = unpad(resp_cipher.decrypt(resp_ct), AES.block_size)
            resp_json = json.loads(decrypted.decode('utf-8'))
            logger.warning(f"[YidongShare] dlFromOutLinkV3 response: {json.dumps(resp_json, ensure_ascii=False)[:500]}")

            result_code = resp_json.get("resultCode", "") or resp_json.get("code", "")
            if result_code and str(result_code) != "0":
                return {"ok": False, "msg": f"{resp_json.get('desc','')}(code={result_code})"}

            dl_url = resp_json.get("data", {}).get("redrUrl", "")
            if dl_url:
                return {"ok": True, "url": dl_url}
            return {"ok": False, "msg": "未获取到 redrUrl", "raw_resp": resp_json}

        except Exception as e:
            logger.error(f"[YidongShare] dlFromOutLinkV3 exception: {e}")
            return {"ok": False, "msg": str(e)}


    # ─── 网页版分享浏览（无需账号）─────────

    @staticmethod
    def browse_share_web(share_url: str, passwd: str = "",
                         folder_id: str = "root") -> dict:
        """浏览分享目录 — 网页版 AES 加密方式（不需要账号）"""
        parsed = YidongClient.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        link_id = parsed["linkID"]

        # 根目录时 pCaID 传 "root"（官方前端 catalogID="root" 的行为；空串会被服务端判为 pCaID不能为空 → code=200000400）
        _parent_id = "root" if folder_id in ("root", "", None) else folder_id
        body_data = {
            "getOutLinkInfoReq": {
                "account": "",
                "linkID": link_id,
                "passwd": passwd,
                "caSrt": 0,
                "coSrt": 0,
                "srtDr": 1,
                "bNum": 1,
                "pCaID": _parent_id,
                "eNum": 200,
            },
            "commonAccountInfo": {"account": "", "accountType": 1},
        }
        body_str = json.dumps(body_data, separators=(',', ':'))

        # AES-CBC 加密请求体
        iv = os.urandom(16)
        cipher = AES.new(YidongClient.SHARE_AES_KEY, AES.MODE_CBC, iv)
        encrypted = cipher.encrypt(pad(body_str.encode('utf-8'), AES.block_size))
        encrypted_b64 = base64.b64encode(iv + encrypted).decode()

        # 生成 mcloud-sign
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        sign = YidongClient._calc_sign(body_str, ts, rand_str)
        mcloud_sign = f"{ts},{rand_str},{sign}"

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": USER_AGENT,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
            "caller": "web",
            "x-m4c-caller": "PC",
            "mcloud-client": "10702",
            "mcloud-version": "7.13.3",
            "mcloud-channel": "1000101",
            "mcloud-route": "001",
            "mcloud-sign": mcloud_sign,
            "mcloud-skey": YidongClient.SHARE_MCLOUD_SKEY,
            "CMS-DEVICE": "default",
            "x-Deviceinfo": "||9|12.27.0|chrome|150.0.0.0|b23fe89e3d289a4c4d73d4288427d3e2||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10213406",
            "x-inner-ntwk": "2",
        }

        api_url = f"{YidongClient.SHARE_HOST}/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6"
        try:
            logger.info(f"[browse_share_web] 请求 linkID={link_id} folder={folder_id} url={api_url}")
            r = requests.post(api_url, data=encrypted_b64, headers=headers, timeout=30)
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}

            # 如果返回的是纯 JSON（错误响应）
            if r.text.strip().startswith("{"):
                try:
                    err_json = json.loads(r.text)
                    code = err_json.get("resultCode") or err_json.get("code", "")
                    desc = err_json.get("desc", "") or err_json.get("message", "")
                    logger.warning(f"[browse_share_web] API 返回错误: code={code} desc={desc} linkID={link_id} resp_text={r.text[:500]}")
                    return {"ok": False, "msg": f"{desc}(code={code})" if desc else f"API error code={code}"}
                except json.JSONDecodeError:
                    pass

            # 解密 AES 响应
            raw_resp = base64.b64decode(r.text)
            resp_iv = raw_resp[:16]
            resp_ct = raw_resp[16:]
            resp_cipher = AES.new(YidongClient.SHARE_AES_KEY, AES.MODE_CBC, resp_iv)
            decrypted = unpad(resp_cipher.decrypt(resp_ct), AES.block_size)
            resp_json = json.loads(decrypted.decode('utf-8'))

            result_code = resp_json.get("resultCode", "") or resp_json.get("code", "")
            if result_code and str(result_code) != "0":
                return {"ok": False, "msg": f"{resp_json.get('desc', '')}(code={result_code})"}

            resp_data = resp_json.get("data", resp_json)
            share_data = resp_data.get("getOutLinkInfoRes", resp_data)
            if isinstance(share_data, dict) and share_data.get("result"):
                share_data = share_data.get("data", share_data)
            elif isinstance(share_data, dict):
                share_data = share_data.get("data", share_data)

            items = []
            for folder in share_data.get("caLst", []) or share_data.get("CaLst", []):
                # 子目录浏览时前端把 pCaID 设为目录的 path，这里优先用 path 作 id（path 为空时回退 caID）
                fid = folder.get("path") or folder.get("caID") or folder.get("CaID", "")
                items.append({
                    "id": fid,
                    "name": folder.get("caName") or folder.get("CaName", "未知"),
                    "size": 0,
                    "is_dir": True,
                    "type": "folder",
                    "path": folder.get("path", ""),
                })
            for f in share_data.get("coLst", []) or share_data.get("CoLst", []):
                fid = f.get("coID") or f.get("CoID", "")
                items.append({
                    "id": fid,
                    "name": f.get("coName") or f.get("CoName", "未知"),
                    "size": int(f.get("coSize") or f.get("CoSize") or 0),
                    "is_dir": False,
                    "type": "file",
                    "path": f.get("path", ""),
                })
            return {"ok": True, "items": items, "count": len(items)}

        except Exception as e:
            return {"ok": False, "msg": str(e)}


# ─────────────────── 工厂 ───────────────────
def create_cloud_client(account_type: str, credential: str):
    """根据账号类型创建对应客户端"""
    at = account_type.lower() if account_type else ""
    if at in ("wps", "kdocs"):
        from wps_cookie_dav import WPSDavClient
        return WPSDavClient(credential)
    elif at in ("tianyi", "189", "cloud189", "天翼"):
        return TianyiClient(credential)
    elif at in ("yidong", "139", "yun139", "移动", "和彩云"):
        return YidongClient(credential)
    elif at in ("quark", "夸克", "quarkcn", "panquark"):
        from .quark_client import QuarkClient
        return QuarkClient(credential)
    else:
        raise ValueError(f"不支持的云盘类型: {account_type}")
