"""
139云盘分享链接核心模块（从 cloud_clients.py 提取，完全独立）
- 解析分享链接
- 列出分享文件
- 获取下载直链
"""
import json
import time
import re
import base64
import hashlib
import random
import string
import os
import requests
from urllib.parse import quote
import pyaes

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS7 填充（pycryptodome pad 的等价实现，避免 Android 上 C 扩展崩溃）"""
    n = block_size - (len(data) % block_size)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS7 去填充（pycryptodome unpad 的等价实现）"""
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > block_size:
        raise ValueError("invalid PKCS7 padding")
    return data[:-n]


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC 加密（pyaes 逐块处理，等效 pycryptodome AES.new(key,CBC,iv).encrypt()）"""
    encryptor = pyaes.AESModeOfOperationCBC(key, iv)
    out = b""
    for i in range(0, len(data), 16):
        out += encryptor.encrypt(data[i:i + 16])
    return out


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC 解密（pyaes 逐块处理，等效 pycryptodome AES.new(key,CBC,iv).decrypt()）"""
    decryptor = pyaes.AESModeOfOperationCBC(key, iv)
    out = b""
    for i in range(0, len(data), 16):
        out += decryptor.decrypt(data[i:i + 16])
    return out


def _encode_uri_component(s: str) -> str:
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


class YidongShareClient:
    """139云盘分享链接客户端（只需一个账号Token即可下载任意分享）"""

    BASE = "https://yun.139.com"
    ROUTE_HOST = "https://user-njs.yun.139.com"
    SHARE_HOST = "https://share-kd-njs.yun.139.com"
    SHARE_AES_KEY = b"PVGDwmcvfs1uV3d1"
    SHARE_MCLOUD_SKEY = ("hj/tjWDc9azlXEtIrA+X6jG2fC2n/YmYStxLIrVHxhC6LB5bNWnN/"
                         "jpraPzV2UMALopZoSPTu+ImGMQr8MiIqO04TNRabSG8aQg57nYEAR7"
                         "oUXgVCfMtGPse2WkIzSUkjn8LNRzszF+6hCwhXo0CA1/vZIonJEeo+"
                         "LVUCQmo64A=")

    def __init__(self, token_str: str):
        self._raw = token_str.strip()
        self.download_referer = "https://yun.139.com/"
        self._phone = ""
        try:
            decoded = base64.b64decode(self._raw).decode("utf-8", errors="ignore")
            if decoded.startswith("pc:"):
                rest = decoded[3:]
                colon_idx = rest.index(":")
                self._phone = rest[:colon_idx]
        except Exception:
            pass

    @staticmethod
    def parse_share_url(url: str) -> dict:
        url = url.strip()
        link_id = ""
        m = re.search(r'/w/i/([A-Za-z0-9_]+)', url)
        if m:
            link_id = m.group(1)
        elif 'caiyun.139.com/w/i/' in url:
            m = re.search(r'caiyun\.139\.com/w/i/([A-Za-z0-9_]+)', url)
            if m:
                link_id = m.group(1)
        elif 'caiyun.139.com/m/i?' in url:
            m = re.search(r'caiyun\.139\.com/m/i\?([A-Za-z0-9_]+)', url)
            if m:
                link_id = m.group(1)
        if not link_id:
            return {"ok": False, "msg": "无法从分享链接中提取 linkID"}
        return {"ok": True, "linkID": link_id, "url": url}

    def _get_account_name(self) -> str:
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
    def _calc_sign(body_str: str, ts: str, rand_str: str) -> str:
        encoded = _encode_uri_component(body_str)
        sorted_str = ''.join(sorted(encoded))
        body_base64 = base64.b64encode(sorted_str.encode()).decode()
        hash1 = hashlib.md5(body_base64.encode()).hexdigest()
        hash2 = hashlib.md5(f"{ts}:{rand_str}".encode()).hexdigest()
        return hashlib.md5((hash1 + hash2).encode()).hexdigest().upper()

    def _gen_mcloud_sign(self, body_str: str) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        sign = self._calc_sign(body_str, ts, rand_str)
        return f"{ts},{rand_str},{sign}"

    def _share_post(self, path: str, data: dict) -> dict:
        """明文JSON分享接口请求"""
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
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}
            resp = r.json()
            result = resp.get("resultCode", "")
            if result and str(result) != "0":
                return {"ok": False, "msg": f"{resp.get('desc','')}(code={result})", "raw": resp}
            return {"ok": True, "data": resp.get("data", resp), "raw": resp}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_share_files(self, share_url: str, passwd: str = "",
                         folder_id: str = "root") -> dict:
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        link_id = parsed["linkID"]
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
                "pCaID": folder_id,
                "eNum": 200,
            },
            "commonAccountInfo": {"account": account_name, "accountType": 1},
        }
        result = self._share_post(
            "/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6", data)
        if not result.get("ok"):
            return result

        resp_data = result.get("data", {})
        share_data = resp_data.get("getOutLinkInfoRes", resp_data)
        if isinstance(share_data, dict) and share_data.get("result"):
            share_data = share_data.get("data", share_data)
        elif isinstance(share_data, dict):
            share_data = share_data.get("data", share_data)

        items = []
        for folder in share_data.get("caLst", []) or share_data.get("CaLst", []):
            fid = folder.get("caID") or folder.get("CaID", "")
            items.append({
                "id": fid,
                "name": folder.get("caName") or folder.get("CaName", "未知"),
                "size": 0,
                "is_dir": True,
                "path": folder.get("path", ""),
            })
        for f in share_data.get("coLst", []) or share_data.get("CoLst", []):
            fid = f.get("coID") or f.get("CoID", "")
            items.append({
                "id": fid,
                "name": f.get("coName") or f.get("CoName", "未知"),
                "size": int(f.get("coSize") or f.get("CoSize") or 0),
                "is_dir": False,
                "path": f.get("path", ""),
            })
        return {"ok": True, "items": items, "count": len(items)}

    def list_share_files_recursive(self, share_url: str, passwd: str = "",
                                   parent_id: str = "root") -> dict:
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
            sub = self.list_share_files_recursive(share_url, passwd, folder["id"])
            if sub.get("ok"):
                all_items.extend(sub.get("items", []))
        return {"ok": True, "items": all_items, "count": len(all_items)}

    def get_share_download_url(self, share_url: str, file_id: str,
                               file_name: str = "", share_path: str = "",
                               share_passwd: str = "") -> dict:
        """获取分享文件下载直链（AES加密请求）"""
        parsed = self.parse_share_url(share_url)
        if not parsed.get("ok"):
            return parsed
        link_id = parsed["linkID"]
        account_name = self._get_account_name()

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

        # AES-CBC 加密（纯 Python 实现，Android 上无 C 扩展加载问题）
        iv = os.urandom(16)
        encrypted = _aes_cbc_encrypt(self.SHARE_AES_KEY, iv,
                                     _pkcs7_pad(body_str.encode('utf-8')))
        encrypted_b64 = base64.b64encode(iv + encrypted).decode()

        mcloud_sign = self._gen_mcloud_sign(body_str)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self._raw}",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": USER_AGENT,
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
            if r.status_code >= 400:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}

            raw_resp = base64.b64decode(r.text)
            resp_iv = raw_resp[:16]
            resp_ct = raw_resp[16:]
            decrypted = _pkcs7_unpad(_aes_cbc_decrypt(self.SHARE_AES_KEY, resp_iv, resp_ct))
            resp_json = json.loads(decrypted.decode('utf-8'))

            result_code = resp_json.get("resultCode", "") or resp_json.get("code", "")
            if result_code and str(result_code) != "0":
                return {"ok": False, "msg": f"{resp_json.get('desc','')}(code={result_code})"}

            dl_url = resp_json.get("data", {}).get("redrUrl", "")
            if dl_url:
                return {"ok": True, "url": dl_url,
                        "headers": {
                            "User-Agent": USER_AGENT,
                            "Referer": "https://yun.139.com/",
                        }}
            return {"ok": False, "msg": "未获取到 redrUrl", "raw_resp": resp_json}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
