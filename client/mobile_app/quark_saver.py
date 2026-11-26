"""
夸克分享转存（项目内置实现）

参考 quark-auto-save 的转存流程，直接在项目里完成：
  1) 用分享链接换 stoken（share/sharepage/token）
  2) 列分享目录，拿 fid + share_fid_token（share/sharepage/detail）
  3) 在自己网盘建临时目录，把分享内容 save 进去（share/sharepage/save + 轮询 task）
  4) 转存完成后在自己网盘里定位文件（file/sort）
  5) 下载完成后删除文件（file/delete）

全部使用登录 cookie，不需要额外部署任何服务。
"""
import re
import time
import requests

BASE = "https://drive-pc.quark.cn"
SHARE = BASE + "/1/clouddrive/share/sharepage"
FILE = BASE + "/1/clouddrive/file"
TASK = BASE + "/1/clouddrive/task"

# 转存临时目录（自己网盘根目录下）
TMP_ROOT = "wps-cloud-tmp"

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}


class QuarkSaverError(Exception):
    pass


class QuarkSaver:
    """夸克分享 → 自己网盘 的转存器"""

    def __init__(self, cookie, timeout=30, task_timeout=900, log_cb=None):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.task_timeout = task_timeout
        self.log_cb = log_cb
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": UA,
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": self.cookie,
        })
        self._dirs = {}   # 分享ID -> 临时目录 fid

    # ──────────────────────────────────────────────
    #  基础请求
    # ──────────────────────────────────────────────
    def _log(self, msg):
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    @staticmethod
    def parse_share_id(share_url):
        url = (share_url or "").strip()
        m = re.search(r'quark\.cn/s/([A-Za-z0-9]+)', url)
        if not m:
            return {"ok": False, "msg": "无法从链接中提取夸克分享ID"}
        pwd = ""
        mp = re.search(r'[?&]pwd=([^&]+)', url)
        if mp:
            pwd = mp.group(1)
        return {"ok": True, "pwd_id": m.group(1), "pwd": pwd}

    def _req(self, method, url, params=None, body=None):
        p = dict(PARAMS)
        if params:
            p.update(params)
        try:
            if method == "GET":
                r = self._session.get(url, params=p, timeout=self.timeout)
            else:
                r = self._session.post(url, params=p, json=body or {},
                                       timeout=self.timeout)
            js = r.json()
        except Exception as e:
            raise QuarkSaverError(f"请求夸克接口失败: {e}")
        if js.get("code") not in (0, 200) and js.get("status") not in (0, 200, None):
            msg = js.get("message") or js.get("msg") or str(js)[:200]
            raise QuarkSaverError(f"夸克接口返回错误: {msg}")
        return js

    # ──────────────────────────────────────────────
    #  分享侧
    # ──────────────────────────────────────────────
    def get_stoken(self, pwd_id, passwd=""):
        js = self._req("POST", SHARE + "/token",
                       body={"pwd_id": pwd_id, "passcode": passwd or ""})
        stoken = (js.get("data") or {}).get("stoken")
        if not stoken:
            raise QuarkSaverError("获取分享 stoken 失败（分享可能已失效或提取码错误）")
        return stoken

    def list_share(self, pwd_id, stoken, pdir_fid="0"):
        js = self._req("GET", SHARE + "/detail", params={
            "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir_fid,
            "force": 0, "_page": 1, "_size": 200,
            "_fetch_banner": 0, "_fetch_share": 1, "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
        })
        items = []
        for f in (js.get("data") or {}).get("list") or []:
            items.append({
                "fid": f.get("fid") or "",
                "token": f.get("share_fid_token") or "",
                "name": f.get("file_name") or "",
                "dir": bool(f.get("dir")),
                "size": int(f.get("size") or 0),
            })
        return items

    # ──────────────────────────────────────────────
    #  自己网盘侧
    # ──────────────────────────────────────────────
    def _mkdir(self, parent_fid, name):
        js = self._req("POST", FILE, body={
            "pdir_fid": parent_fid, "file_name": name,
            "dir_path": "", "dir_init_lock": False,
        })
        fid = (js.get("data") or {}).get("fid")
        if not fid:
            raise QuarkSaverError(f"创建网盘目录失败: {name}")
        return fid

    def _ensure_dir_path(self, name):
        """确保根目录下存在 name 目录，返回 fid"""
        for f in self.own_list("0"):
            if f["dir"] and f["name"] == name:
                return f["fid"]
        return self._mkdir("0", name)

    def own_list(self, pdir_fid):
        js = self._req("GET", FILE + "/sort", params={
            "pdir_fid": pdir_fid, "_page": 1, "_size": 200,
            "_fetch_total": 1, "_sort": "file_type:asc,updated_at:desc",
        })
        items = []
        for f in (js.get("data") or {}).get("list") or []:
            items.append({
                "fid": f.get("fid") or "",
                "name": f.get("file_name") or "",
                "dir": bool(f.get("dir")),
                "size": int(f.get("size") or 0),
            })
        return items

    def save(self, pwd_id, stoken, items, to_fid):
        """把分享里的 items 保存到网盘 to_fid 目录"""
        if not items:
            raise QuarkSaverError("分享目录为空")
        js = self._req("POST", SHARE + "/save", body={
            "fid_list": [i["fid"] for i in items],
            "fid_token_list": [i["token"] for i in items],
            "to_pdir_fid": to_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        })
        task_id = (js.get("data") or {}).get("task_id")
        if not task_id:
            raise QuarkSaverError("提交转存任务失败（未返回 task_id）")
        return task_id

    def wait_task(self, task_id, timeout=None):
        deadline = time.time() + (timeout or self.task_timeout)
        while time.time() < deadline:
            js = self._req("GET", TASK, params={"task_id": task_id, "_page": 1, "_size": 100})
            data = js.get("data") or {}
            status = data.get("status")
            if status == 2:
                return {"ok": True, "msg": "转存完成"}
            if status not in (0, 1, None):
                return {"ok": False, "msg": f"转存任务失败(status={status}): {data.get('message', '')}"}
            time.sleep(2)
        return {"ok": False, "msg": "等待转存超时"}

    def find_file(self, dir_fid, name, depth=4):
        """在自己网盘目录下递归找文件"""
        target = (name or "").strip()
        if not target:
            return {"ok": False, "msg": "缺少文件名"}
        items = self.own_list(dir_fid)
        for f in items:
            if not f["dir"] and f["name"] == target:
                return {"ok": True, "fid": f["fid"], "name": f["name"],
                        "size": f["size"], "parent_fid": dir_fid}
        low = target.lower()
        for f in items:
            if not f["dir"] and f["name"].lower() == low:
                return {"ok": True, "fid": f["fid"], "name": f["name"],
                        "size": f["size"], "parent_fid": dir_fid}
        if depth <= 1:
            return {"ok": False, "msg": f"网盘目录中未找到文件: {target}"}
        for f in items:
            if f["dir"]:
                sub = self.find_file(f["fid"], target, depth - 1)
                if sub.get("ok"):
                    return sub
        return {"ok": False, "msg": f"网盘目录中未找到文件: {target}"}

    def delete(self, fids):
        if isinstance(fids, str):
            fids = [fids]
        js = self._req("POST", FILE + "/delete", body={"fids": list(fids), "exclude_fids": []})
        return {"ok": bool(js.get("code") in (0, 200)), "msg": js.get("message", "")}

    def is_empty(self, dir_fid):
        try:
            return not self.own_list(dir_fid)
        except Exception:
            return False

    # ──────────────────────────────────────────────
    #  组合流程
    # ──────────────────────────────────────────────
    def ensure_transfer(self, share_url, passwd=""):
        """把分享转存到自己网盘临时目录，返回 {"ok","dir_fid"}"""
        parsed = self.parse_share_id(share_url)
        if not parsed.get("ok"):
            return parsed
        pwd_id = parsed["pwd_id"]
        if pwd_id in self._dirs:
            return {"ok": True, "dir_fid": self._dirs[pwd_id]}
        try:
            self._log("正在获取分享信息...")
            stoken = self.get_stoken(pwd_id, passwd or parsed.get("pwd", ""))
            items = self.list_share(pwd_id, stoken)
            if not items:
                return {"ok": False, "msg": "分享目录为空或分享已失效"}
            root_fid = self._ensure_dir_path(TMP_ROOT)
            to_fid = self._mkdir(root_fid, pwd_id)
            self._log(f"正在转存 {len(items)} 个文件/目录到网盘...")
            task_id = self.save(pwd_id, stoken, items, to_fid)
            res = self.wait_task(task_id)
            if not res.get("ok"):
                return res
            if not self.own_list(to_fid):
                return {"ok": False, "msg": "转存完成但网盘目录为空，可能被风控拦截"}
            self._dirs[pwd_id] = to_fid
            return {"ok": True, "dir_fid": to_fid}
        except QuarkSaverError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            return {"ok": False, "msg": f"转存失败: {e}"}
