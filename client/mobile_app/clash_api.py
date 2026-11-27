"""FlClash / Clash(mihomo) 外部控制器：节点延时测速与自动切换。

FlClash 默认关闭外部控制器，需要用户在左侧「工具」→「基本配置」页最底部
打开「外部控制器」开关（默认监听 127.0.0.1:9090，可设置 Secret）。

本模块只做三件事：
  1. 拉取节点列表，找出「节点选择」这类选择器分组；
  2. 并发测试分组内各节点延时，选最快的；
  3. 切换分组当前节点，并可关闭 Telegram 相关连接让 tdl 走新节点重连。

所有网络请求都走本机 127.0.0.1，超时很短；失败时只记日志，不影响下载。
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Telegram 数据中心网段：切节点后只断开这些连接，避免影响其它软件
TG_DC_PREFIXES = ("91.108.", "91.105.", "149.154.", "95.161.", "149.154.16")

# 不是叶子节点的类型（分组本身，不作为候选）
_GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay", "Compatible"}
# 不可直连的伪节点
_FAKE_TYPES = {"Direct", "Reject", "RejectDrop", "Pass"}

# 常见的「节点选择」分组名关键字，用于自动识别主分组
_GROUP_HINTS = ("节点选择", "选择", "代理", "PROXY", "Proxy", "GLOBAL", "Global",
                "手动切换", "机场", "自动选择", "🚀")


class ClashError(Exception):
    pass


class ClashController:
    def __init__(self, api="", secret="", group="", log_cb=None,
                 test_url="https://api.telegram.org/"):
        api = (api or "").strip() or "127.0.0.1:9090"
        if not re.match(r"^https?://", api, re.I):
            api = "http://" + api
        self.base = api.rstrip("/")
        self.secret = (secret or "").strip()
        self.group = (group or "").strip()
        self.test_url = test_url or "https://api.telegram.org/"
        self.log = log_cb or (lambda m: None)
        self._api_checked = False
        self._api_ok = False

    # ── 基础请求 ─────────────────────────────────────────────────
    def _request(self, method, path, body=None, timeout=5):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.secret:
            req.add_header("Authorization", "Bearer " + self.secret)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw.decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ClashError("FlClash 外部控制器要求 Secret，请在客户端填写") from e
            raise ClashError("HTTP %s: %s" % (e.code, path)) from e
        except urllib.error.URLError as e:
            raise ClashError("连接失败: %s" % (e.reason,)) from e
        except Exception as e:
            raise ClashError(str(e)) from e

    def available(self):
        """外部控制器是否可用（只探测一次，避免频繁日志刷屏）"""
        if self._api_checked:
            return self._api_ok
        self._api_checked = True
        try:
            self._request("GET", "/version", timeout=3)
            self._api_ok = True
        except ClashError as e:
            self._api_ok = False
            self.log("外部控制器不可用（%s）。请在 FlClash 左侧「工具」→「基本配置」"
                     "最底部打开「外部控制器」（默认 127.0.0.1:9090）" % e)
        return self._api_ok

    def proxies(self):
        data = self._request("GET", "/proxies", timeout=6)
        return data.get("proxies") or {}

    # ── 分组与节点 ───────────────────────────────────────────────
    def pick_group(self, proxies):
        if self.group:
            return self.group if self.group in proxies else ""
        best = ""
        best_score = -1
        for name, p in proxies.items():
            if p.get("type") not in ("Selector", "Fallback", "LoadBalance"):
                continue
            # GLOBAL 是内置的全局分组（当前值常是 DIRECT），不是机场的节点选择分组
            if name.strip().upper() == "GLOBAL":
                continue
            nodes = p.get("all") or []
            if len(nodes) < 2:
                continue
            score = len(nodes)
            if any(k in name for k in _GROUP_HINTS):
                score += 1000
            if score > best_score:
                best, best_score = name, score
        return best

    @staticmethod
    def _is_selectable(p):
        t = p.get("type", "")
        return bool(t) and t not in _GROUP_TYPES and t not in _FAKE_TYPES

    def _delay(self, name):
        q = urllib.parse.urlencode({"timeout": 3000, "url": self.test_url})
        path = "/proxies/%s/delay?%s" % (urllib.parse.quote(name, safe=""), q)
        try:
            return int(self._request("GET", path, timeout=6).get("delay") or 0)
        except Exception:
            return None

    def _test_many(self, names, workers=10):
        out = {}
        if not names:
            return out
        with ThreadPoolExecutor(max_workers=min(workers, len(names))) as ex:
            futs = {ex.submit(self._delay, n): n for n in names}
            for f in as_completed(futs):
                try:
                    out[futs[f]] = f.result()
                except Exception:
                    out[futs[f]] = None
        return out

    def select_fastest(self, improve=0.85, force=False, max_nodes=40, allow_switch=True):
        """测试分组内节点延时，必要时切换到更快的节点。

        返回 dict（group/current/best/cur_delay/best_delay/switched），
        不可用时返回 None。improve=0.85 表示新节点延时要低 15% 才切换，
        且至少低 20ms，避免在延时接近的节点之间来回抖动。
        allow_switch=False 时只测速不切换（下载速度正常时使用，避免打扰）。
        测速目标用 api.telegram.org，确保“最快”是对 Telegram 真正最快。
        """
        if not self.available():
            return None
        try:
            proxies = self.proxies()
            group = self.pick_group(proxies)
            if not group:
                self.log("未找到可切换的节点分组（可在配置里填写分组名）")
                return None
            g = proxies.get(group) or {}
            cur = g.get("now") or ""
            names = [n for n in (g.get("all") or [])
                     if n in proxies and self._is_selectable(proxies[n])]
            if not names:
                return None
            if len(names) > max_nodes:
                names = names[:max_nodes]
            targets = list(names)
            if cur and cur not in targets and cur in proxies:
                targets.append(cur)
            delays = self._test_many(targets)
            # 当前节点单次测速可能偶发超时（节点本身不差），补测一次避免误判后乱切
            if cur and delays.get(cur) is None and cur in proxies:
                d = self._delay(cur)
                if d:
                    delays[cur] = d
            ok = [(d, n) for n, d in delays.items() if d and d > 0]
            if not ok:
                self.log("节点延时测试全部失败，保持当前节点 %s" % (cur or "-"))
                return None
            ok.sort()
            best_delay, best = ok[0]
            cur_delay = delays.get(cur)
            switched = False
            if best != cur:
                if force or cur_delay is None:
                    switched = True
                else:
                    switched = (best_delay < cur_delay * improve
                                and best_delay < cur_delay - 20)
            if switched and not allow_switch:
                switched = False
                self.log("当前下载速度正常，保持节点 %s (%sms)，候选最快 %s (%sms)"
                         % (cur or "-", cur_delay or "-", best, best_delay))
            elif switched:
                put = "/proxies/%s" % urllib.parse.quote(group, safe="")
                self._request("PUT", put, {"name": best}, timeout=6)
                self.log("已切换节点：%s (%sms) → %s (%sms)"
                         % (cur or "-", cur_delay if cur_delay else "-", best, best_delay))
            else:
                self.log("保持节点：%s (%sms)，最快候选 %s (%sms)"
                         % (cur or "-", cur_delay if cur_delay else "-", best, best_delay))
            return {"group": group, "current": cur, "best": best,
                    "cur_delay": cur_delay, "best_delay": best_delay,
                    "switched": switched}
        except ClashError as e:
            self.log("节点测速失败：%s" % e)
            return None

    # ── 让新节点立即生效 ─────────────────────────────────────────
    def close_telegram_connections(self):
        """断开 Telegram DC 相关连接，促使 tdl 用新节点重新建连。

        只关 91.108/149.154/95.161 等 Telegram 网段（或进程名含 tdl）的连接，
        不影响其它软件的连接。
        """
        try:
            data = self._request("GET", "/connections", timeout=6)
        except ClashError:
            return 0
        closed = 0
        for c in (data.get("connections") or []):
            md = c.get("metadata") or {}
            ip = str(md.get("destinationIP") or "")
            proc = str(md.get("process") or "").lower()
            if ip.startswith(TG_DC_PREFIXES) or "tdl" in proc:
                cid = c.get("id")
                if not cid:
                    continue
                try:
                    self._request("DELETE", "/connections/%s"
                                  % urllib.parse.quote(str(cid), safe=""), timeout=4)
                    closed += 1
                except ClashError:
                    pass
        if closed:
            self.log("已断开 %d 条 Telegram 连接，tdl 将走新节点重连" % closed)
        return closed
