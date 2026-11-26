# -*- coding: utf-8 -*-
"""
server/telegram_service.py
────────────────────────────────────────────────────────────────────────────
Telegram 频道资源服务（Telethon 用户账号 / MTProto）：
  - 账号登录（验证码 + 两步验证密码），会话持久化到 data/telegram.json
  - 频道解析与添加（@username / -100xxx / t.me 链接）
  - 频道内文件搜索（服务端只搜索并推送元信息）
  - 生成 t.me 消息链接，实际下载由 Telegram 客户端完成

实现要点：Flask 是同步模型，这里在独立线程里跑一个 asyncio 事件循环，
所有协程通过 run_coroutine_threadsafe 提交，接口保持同步调用。
"""
import os
import re
import json
import time
import datetime
import base64
import asyncio
import threading
from urllib.parse import urlparse

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import PeerChannel, Channel, Chat, InputMessagesFilterDocument
    from telethon.errors import (
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        FloodWaitError,
        AuthKeyUnregisteredError,
        AuthKeyDuplicatedError,
        UnauthorizedError,
    )
    # 会话被服务端作废的相关异常（"The key is not registered in the system" 即 AuthKeyUnregisteredError）
    AUTH_ERRORS = (AuthKeyUnregisteredError, AuthKeyDuplicatedError, UnauthorizedError)
    TELETHON_OK = True
    TELETHON_ERR = ""
except Exception as _e:  # 未安装 telethon 时后台页面仍可打开
    AUTH_ERRORS = ()
    TELETHON_OK = False
    TELETHON_ERR = str(_e)


def size_text(size):
    """字节数格式化为易读大小"""
    try:
        size = float(size or 0)
    except Exception:
        return "-"
    if size <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx else f"{int(size)} {units[idx]}"


class TelegramService:
    """Telegram 资源服务单例（内部维护独立事件循环线程）"""

    def __init__(self, config_file):
        self.config_file = config_file
        self._lock = threading.RLock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._client = None
        self._client_key = None
        self._phone_hash = None
        self._phone = ""
        # 登录状态缓存：状态检测在后台刷新，接口立即返回，避免长时间占用 Web 线程
        self._status_cache = None
        self._status_ts = 0.0
        self._status_busy = False
        self._search_busy = False
        # 扫码登录状态（免验证码）：{client, qr, state, url, error, ...}
        self._qr = None
        self._qr_task = None

    # ────────────── 基础 ──────────────

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout=120):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def load_config(self):
        with self._lock:
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
            return {}

    def save_config(self, **kwargs):
        with self._lock:
            data = self.load_config()
            data.update(kwargs)
            tmp = self.config_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.config_file)
            try:
                os.chmod(self.config_file, 0o600)
            except Exception:
                pass
            return data

    @staticmethod
    def _build_proxy(proxy_url):
        """把 socks5://host:port 或 http://user:pass@host:port 转成 Telethon 需要的字典"""
        proxy_url = (proxy_url or "").strip()
        if not proxy_url:
            return None
        try:
            p = urlparse(proxy_url if "://" in proxy_url else "socks5://" + proxy_url)
            scheme = (p.scheme or "socks5").lower()
            if scheme == "socks":
                scheme = "socks5"
            if scheme not in ("socks5", "socks4", "http"):
                return None
            proxy = {"proxy_type": scheme, "addr": p.hostname, "port": p.port, "rdns": True}
            if p.username:
                proxy["username"] = p.username
            if p.password:
                proxy["password"] = p.password
            return proxy
        except Exception:
            return None

    async def _get_client(self):
        if not TELETHON_OK:
            raise RuntimeError(f"未安装 telethon，请先 pip install telethon（{TELETHON_ERR}）")
        cfg = self.load_config()
        try:
            api_id = int(str(cfg.get("api_id") or "").strip())
        except Exception:
            api_id = 0
        api_hash = (cfg.get("api_hash") or "").strip()
        if not api_id or not api_hash:
            raise RuntimeError("请先填写 api_id 和 api_hash 并保存")
        session_str = (cfg.get("session") or "").strip()
        proxy_url = (cfg.get("proxy") or "").strip()
        key = (api_id, api_hash, session_str, proxy_url)

        if self._client is not None and self._client_key == key:
            if not self._client.is_connected():
                await self._client.connect()
            return self._client

        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        client = TelegramClient(
            StringSession(session_str), api_id, api_hash,
            proxy=self._build_proxy(proxy_url),
            device_model="WPSCloudPanel", system_version="Linux",
            app_version="1.0", timeout=30,
        )
        await client.connect()
        self._client = client
        self._client_key = key
        return client

    # ────────────── 登录 ──────────────

    @staticmethod
    def _delivery_desc(sent):
        """根据 Telegram 返回的发送方式，给出验证码去哪儿看的明确提示"""
        t = type(getattr(sent, "type", None)).__name__
        mapping = {
            "SentCodeTypeApp": "已通过 Telegram 应用内消息发送。请在【已登录该手机号账号的 Telegram 设备】上，打开官方账号「Telegram」(777000) 的对话查看，这不是短信",
            "SentCodeTypeSms": "已通过短信发送到该手机号，请查看手机短信（注意拦截/垃圾箱）",
            "SentCodeTypeCall": "将以电话语音播报验证码，请注意接听来电",
            "SentCodeTypeFlashCall": "将以闪信方式来电显示验证码，注意看来电号码",
            "SentCodeTypeMissedCall": "将响铃一声后挂断，验证码为来电号码后几位",
            "SentCodeTypeEmailCode": "已发送到该账号绑定的邮箱，请查收",
            "SentCodeTypeFragmentSms": "已通过短信发送到该手机号",
        }
        return mapping.get(t, f"已发送（发送方式：{t}）")

    async def _send_code(self, phone):
        phone = (phone or "").strip()
        if not phone:
            raise RuntimeError("请先填写手机号（含国家区号，例如 +8613800000000）")
        client = await self._get_client()
        try:
            sent = await client.send_code_request(phone)
        except AUTH_ERRORS:
            # 旧会话已被作废，清掉后用一个全新会话重新申请验证码
            await self._reset_session(client)
            client = await self._get_client()
            sent = await client.send_code_request(phone)
        self._phone_hash = sent.phone_code_hash
        self._phone = phone
        print(f"[tg] send_code phone={phone} type={type(getattr(sent, 'type', None)).__name__}", flush=True)
        return sent

    async def _sign_in(self, phone, code, password):
        phone = (phone or self._phone or "").strip()
        code = (code or "").strip().replace(" ", "")
        if not self._phone_hash:
            raise RuntimeError("请先点击「发送验证码」")
        client = await self._get_client()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=self._phone_hash)
        except SessionPasswordNeededError:
            if not (password or "").strip():
                raise RuntimeError("该账号开启了两步验证，请填写密码")
            await client.sign_in(password=password.strip())
        except AUTH_ERRORS:
            await self._reset_session(client)
            raise RuntimeError("登录已失效，请重新点击「发送验证码」")
        if not await client.is_user_authorized():
            raise RuntimeError("登录未完成，请重试")
        self.save_config(session=StringSession.save(client.session))
        self._phone_hash = None
        me = await client.get_me()
        return self._user_info(me)

    @staticmethod
    def _user_info(me):
        name = " ".join(x for x in [getattr(me, "first_name", "") or "",
                                    getattr(me, "last_name", "") or ""] if x)
        return {
            "id": getattr(me, "id", ""),
            "name": name,
            "username": getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", "") or "",
        }

    async def _status(self):
        if not TELETHON_OK:
            return {"telethon": False, "authorized": False,
                    "error": f"未安装 telethon: {TELETHON_ERR}"}
        try:
            client = await self._get_client()
            if not await client.is_user_authorized():
                return {"telethon": True, "authorized": False}
            # is_user_authorized() 只检查本地 session 是否存在 auth key，不联网；
            # 会话被服务端作废时它仍返回 True，必须调用 get_me() 实际验证。
            try:
                me = await client.get_me()
            except AUTH_ERRORS:
                await self._reset_session(client)
                return {"telethon": True, "authorized": False, "expired": True,
                        "error": "登录已失效：该会话已被 Telegram 作废（常见于在其它设备/客户端终止了会话），请重新登录"}
            if me is None:
                await self._reset_session(client)
                return {"telethon": True, "authorized": False, "expired": True,
                        "error": "登录已失效：会话无效，请重新登录"}
            return {"telethon": True, "authorized": True, "user": self._user_info(me)}
        except Exception as e:
            return {"telethon": True, "authorized": False, "error": str(e)}

    async def _reset_session(self, client=None):
        """会话已被 Telegram 服务端作废时，清掉本地 client 与 session，避免反复报错"""
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        self._client = None
        self._client_key = None
        try:
            self.save_config(session="")
        except Exception:
            pass

    # ────────────── 扫码登录（免验证码） ──────────────

    def _qr_log(self, msg):
        try:
            p = os.path.join(os.path.dirname(self.config_file), "qr_debug.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write(datetime.datetime.now().strftime("%m-%d %H:%M:%S") + " " + str(msg) + "\n")
        except Exception:
            pass

    @staticmethod
    def qr_svg(url):
        """把 tg://login?token=... 渲染成 SVG 字符串，前端可直接插入页面"""
        if not url:
            return ""
        try:
            import io
            import qrcode
            import qrcode.image.svg
            img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                              box_size=10, border=2)
            buf = io.BytesIO()
            img.save(buf)
            return buf.getvalue().decode("utf-8")
        except Exception:
            return ""

    def qr_start(self, timeout=45, target="main"):
        """生成登录二维码链接，后台自动轮询扫码结果。

        target="main"   登录服务端搜索用的主会话（session）
        target="client" 登录下发给客户端下载用的独立会话（client_session）
        """
        return self._submit(self._qr_start_async(target), timeout)

    def qr_status(self):
        q = self._qr
        if not q:
            return {"state": "idle"}
        state = q.get("state")
        url = q.get("url") or ""
        left = 0
        try:
            exp = q.get("expires") or q.get("qr").expires
            left = max(0, int((exp - datetime.datetime.now(tz=datetime.timezone.utc)).total_seconds()))
        except Exception:
            pass
        return {
            "state": state,
            "target": q.get("target") or "main",
            "url": url,
            "error": q.get("error") or "",
            "left": left,
            "svg": self.qr_svg(url) if state == "pending" else "",
        }

    def qr_password(self, password, timeout=45):
        return self._submit(self._qr_password_async(password), timeout)

    def qr_cancel(self, timeout=10):
        try:
            self._submit(self._qr_cancel_async(), timeout)
        except Exception:
            pass

    async def _qr_start_async(self, target="main"):
        cfg = self.load_config()
        try:
            api_id = int(str(cfg.get("api_id") or "").strip())
        except Exception:
            api_id = 0
        api_hash = (cfg.get("api_hash") or "").strip()
        proxy_url = (cfg.get("proxy") or "").strip()
        if not api_id or not api_hash:
            raise RuntimeError("请先填写 api_id 和 api_hash 并保存")

        # 结束上一次未完成的扫码会话
        old = self._qr
        self._qr = None
        if self._qr_task is not None:
            try:
                self._qr_task.cancel()
            except Exception:
                pass
            self._qr_task = None
        if old and old.get("client"):
            try:
                await old["client"].disconnect()
            except Exception:
                pass

        # 只对主会话做"已登录"拦截；下载会话允许重复扫码覆盖
        if target != "client":
            try:
                if self._client is not None and await self._client.is_user_authorized():
                    raise RuntimeError("当前已登录，如需切换账号请先退出登录")
            except RuntimeError:
                raise
            except Exception:
                pass

        client = TelegramClient(
            StringSession(""), api_id, api_hash,
            proxy=self._build_proxy(proxy_url),
            device_model="WPSCloudPanel", system_version="Linux",
            app_version="1.0", timeout=30,
        )
        await client.connect()
        qr = await client.qr_login()
        qstate = {
            "client": client, "qr": qr, "state": "pending",
            "url": qr.url, "error": "", "ts": time.time(),
            "api_id": api_id, "api_hash": api_hash, "proxy_url": proxy_url,
            "expires": qr.expires, "target": target,
        }
        self._qr = qstate
        self._qr_task = asyncio.ensure_future(self._qr_wait_async(qstate))
        self._qr_log("START target=%s api_id=%s url=%s" % (target, api_id, qr.url[:60]))
        return qr.url

    async def _qr_wait_async(self, qstate):
        """按 Telethon 官方机制等待扫码：依靠 UpdateLoginToken 事件唤醒，
        只在手机确认后才查询一次登录结果，避免频繁 export 干扰登录状态。"""
        qr = qstate["qr"]
        deadline = time.time() + 300
        while time.time() < deadline:
            if self._qr is not qstate or qstate.get("state") != "pending":
                return

            left = 30
            try:
                left = int((qstate["expires"] - datetime.datetime.now(tz=datetime.timezone.utc)).total_seconds()) + 3
            except Exception:
                pass
            left = max(5, min(left, 45))

            try:
                await qr.wait(timeout=left)
                self._qr_log("SUCCESS via wait")
                self._qr_finish(qstate)
                return
            except SessionPasswordNeededError:
                qstate["state"] = "password_needed"
                self._qr_log("password_needed")
                return
            except asyncio.TimeoutError:
                # 二维码到期或未收到确认 → 重建（前端自动刷新）
                try:
                    await qr.recreate()
                    qstate["url"] = qr.url
                    qstate["expires"] = qr.expires
                    self._qr_log("recreate token")
                except Exception as e:
                    qstate["error"] = type(e).__name__
                    self._qr_log("recreate EXC %s: %s" % (type(e).__name__, str(e)[:100]))
                # 兜底：可能已授权但事件没到
                try:
                    if await qstate["client"].is_user_authorized():
                        self._qr_log("SUCCESS via is_user_authorized")
                        self._qr_finish(qstate)
                        return
                except Exception:
                    pass
                continue
            except Exception as e:
                qstate["error"] = type(e).__name__
                self._qr_log("wait EXC %s: %s" % (type(e).__name__, str(e)[:100]))
                await asyncio.sleep(1)
                continue

        if self._qr is qstate and qstate.get("state") == "pending":
            qstate["state"] = "expired"

    def _qr_finish(self, qstate):
        """扫码成功后落盘会话：主会话接管搜索客户端，下载会话只保存供客户端使用"""
        client = qstate["client"]
        target = qstate.get("target") or "main"
        try:
            sess = client.session.save()
            if target == "client":
                self.save_config(client_session=sess)
            else:
                self.save_config(session=sess)
        except Exception as e:
            qstate["error"] = str(e)[:160]
            qstate["state"] = "error"
            return
        if target == "client":
            # 下载会话不占用服务端连接，保存后立即断开
            try:
                self._loop.create_task(client.disconnect())
            except Exception:
                pass
            qstate["state"] = "success"
            self._qr_log("FINISH client_session saved len=%s" % len(sess or ""))
            return
        self._client = client
        self._client_key = (qstate["api_id"], qstate["api_hash"], sess, qstate["proxy_url"])
        qstate["state"] = "success"
        self.invalidate_status()
        self._qr_log("FINISH saved len=%s" % len(sess or ""))

    async def _qr_password_async(self, password):
        q = self._qr
        if not q or q.get("state") != "password_needed":
            raise RuntimeError("当前没有等待两步验证密码的扫码登录")
        password = (password or "").strip()
        if not password:
            raise RuntimeError("请输入两步验证密码")
        client = q["client"]
        await client.sign_in(password=password)
        sess = client.session.save()
        target = q.get("target") or "main"
        if target == "client":
            self.save_config(client_session=sess)
            q["state"] = "success"
            try:
                self._loop.create_task(client.disconnect())
            except Exception:
                pass
            return
        self.save_config(session=sess)
        self._client = client
        self._client_key = (q["api_id"], q["api_hash"], sess, q["proxy_url"])
        q["state"] = "success"
        self.invalidate_status()

    async def _qr_cancel_async(self):
        q = self._qr
        self._qr = None
        if self._qr_task is not None:
            try:
                self._qr_task.cancel()
            except Exception:
                pass
            self._qr_task = None
        if q and q.get("client"):
            try:
                await q["client"].disconnect()
            except Exception:
                pass

    async def _logout(self):
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._client_key = None
        self.save_config(session="")

    # ────────────── 频道与搜索 ──────────────

    @staticmethod
    def _peer(value):
        """把 @username / -100xxx / t.me 链接转成 Telethon 可解析的 peer"""
        v = (value or "").strip()
        if not v:
            raise RuntimeError("频道标识为空")
        if "t.me/" in v:
            tail = v.split("t.me/", 1)[1].strip("/")
            if tail.startswith("c/"):
                parts = tail.split("/")
                if len(parts) >= 2 and parts[1].isdigit():
                    return PeerChannel(int(parts[1]))
                raise RuntimeError("无法识别的频道链接")
            if tail.startswith("+"):
                raise RuntimeError("暂不支持邀请链接，请先在 Telegram 里加入该频道，再填 @username 或频道ID")
            v = "@" + tail.split("/")[0].split("?")[0]
        if re.fullmatch(r"-?\d+", v):
            n = int(v)
            return PeerChannel(abs(n)) if n < 0 else n
        if not v.startswith("@"):
            v = "@" + v
        return v

    async def _resolve(self, value):
        client = await self._get_client()
        try:
            entity = await client.get_entity(self._peer(value))
        except AUTH_ERRORS:
            # "The key is not registered in the system" 就是走到这里
            await self._reset_session(client)
            self.invalidate_status()
            raise RuntimeError(
                "登录已失效：该会话已被 Telegram 作废（常见于在其它设备/客户端终止了会话，"
                "或换了 api_id/api_hash），请重新登录后再添加频道")
        if not isinstance(entity, (Channel, Chat)):
            raise RuntimeError("该标识不是频道/群组")
        return entity

    async def _add_channel(self, value):
        entity = await self._resolve(value)
        info = {
            "value": (value or "").strip(),
            "chat_id": getattr(entity, "id", None),
            "title": getattr(entity, "title", "") or "",
            "username": getattr(entity, "username", "") or "",
        }
        cfg = self.load_config()
        channels = [c for c in cfg.get("channels", []) if isinstance(c, dict)]
        for i, c in enumerate(channels):
            if c.get("value") == info["value"] or (info["chat_id"] and c.get("chat_id") == info["chat_id"]):
                channels[i] = info
                break
        else:
            channels.append(info)
        self.save_config(channels=channels)
        return info

    @staticmethod
    def _message_info(msg, entity):
        """从消息里提取资源元信息（文件名/大小/时间/t.me 链接）"""
        f = msg.file
        name = (getattr(f, "name", None) or "").strip()
        mime = (getattr(f, "mime_type", None) or "").strip()
        size = int(getattr(f, "size", 0) or 0)
        ext = (getattr(f, "ext", None) or "").strip()
        if not name:
            base = f"msg_{msg.id}"
            if msg.photo:
                name = base + (ext or ".jpg")
            elif msg.video:
                name = base + (ext or ".mp4")
            elif msg.audio:
                name = base + (ext or ".mp3")
            else:
                name = base + ext
        chat_id = getattr(entity, "id", None)
        username = getattr(entity, "username", None)
        if username:
            link = f"https://t.me/{username}/{msg.id}"
        elif chat_id:
            link = f"https://t.me/c/{chat_id}/{msg.id}"
        else:
            link = ""
        caption = (msg.message or "").strip().replace("\n", " ")
        if len(caption) > 120:
            caption = caption[:120] + "..."
        return {
            "msg_id": msg.id,
            "name": name,
            "size": size,
            "size_text": size_text(size),
            "mime": mime,
            "date": msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "",
            "link": link,
            "chat_id": chat_id,
            "channel_title": getattr(entity, "title", "") or "",
            "channel_username": username or "",
            "caption": caption,
        }

    async def _search(self, query, channel_values, limit):
        client = await self._get_client()
        cfg = self.load_config()
        if not (channel_values or []):
            channel_values = [c.get("value") for c in cfg.get("channels", [])
                              if isinstance(c, dict) and c.get("value")]
        if not channel_values:
            raise RuntimeError("请先添加至少一个频道")

        try:
            limit = int(limit)
        except Exception:
            limit = 30
        limit = max(1, min(limit, 200))

        results = []
        errors = []
        seen = set()
        for value in channel_values:
            try:
                entity = await self._resolve(value)
            except Exception as e:
                errors.append(f"{value}: {e}")
                continue
            try:
                q = (query or "").strip()
                found_any = False
                if q:
                    iterator = client.iter_messages(entity, search=q, limit=max(limit * 3, 60))
                else:
                    iterator = client.iter_messages(entity, limit=max(limit * 3, 60))
                async for msg in iterator:
                    if not getattr(msg, "file", None):
                        continue
                    key = (getattr(entity, "id", None), msg.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    item = self._message_info(msg, entity)
                    item["channel_value"] = value
                    results.append(item)
                    found_any = True

                # Telegram 服务端对中文 / hashtag 的全文搜索支持很差，
                # 例如 "#JOJO的奇妙冒险飙马野郎" 里搜 "JOJO" 会无结果。服务端无结果时本地兜底匹配。
                if q and not found_any:
                    scan = min(max(limit * 10, 200), 400)
                    terms = [t for t in q.lower().split() if t]
                    async for msg in client.iter_messages(entity, limit=scan):
                        if not getattr(msg, "file", None):
                            continue
                        text = (getattr(msg, "message", "") or "").lower()
                        if terms and not all(t in text for t in terms):
                            continue
                        key = (getattr(entity, "id", None), msg.id)
                        if key in seen:
                            continue
                        seen.add(key)
                        item = self._message_info(msg, entity)
                        item["channel_value"] = value
                        results.append(item)
            except FloodWaitError as e:
                errors.append(f"{value}: 请求过于频繁，请 {e.seconds} 秒后再试")
            except Exception as e:
                errors.append(f"{value}: {e}")

        results.sort(key=lambda x: x.get("date", ""), reverse=True)
        return {"items": results[:limit], "errors": errors, "total": len(results)}

    # ────────────── 同步封装 ──────────────

    def send_code(self, phone=None, timeout=30):
        sent = self._submit(self._send_code(phone), timeout)
        return {"sent": True, "delivery": self._delivery_desc(sent)}

    def sign_in(self, phone, code, password="", timeout=30):
        return self._submit(self._sign_in(phone, code, password), timeout)

    def status_snapshot(self):
        """返回缓存的登录状态，调用方不会被网络阻塞"""
        return {"data": self._status_cache, "ts": self._status_ts, "busy": self._status_busy}

    def invalidate_status(self):
        """登录/退出后让缓存失效，下次查询会重新检测"""
        self._status_cache = None

    def status_refresh(self):
        """在事件循环里后台刷新登录状态，立即返回"""
        if self._status_busy:
            return False
        self._status_busy = True

        async def _job():
            try:
                data = await self._status()
            except Exception as e:
                data = {"telethon": TELETHON_OK, "authorized": False, "error": str(e)}
            self._status_cache = data
            self._status_ts = time.time()
            self._status_busy = False

        try:
            asyncio.run_coroutine_threadsafe(_job(), self._loop)
            return True
        except Exception:
            self._status_busy = False
            return False

    def logout(self, timeout=15):
        result = self._submit(self._logout(), timeout)
        self._status_cache = {"telethon": TELETHON_OK, "authorized": False}
        self._status_ts = time.time()
        return result

    def add_channel(self, value, timeout=30):
        return self._submit(self._add_channel(value), timeout)

    def search(self, query, channels=None, limit=30, timeout=75):
        """同一时间只允许一个搜索，避免并发搜索把 Web 线程占满"""
        if self._search_busy:
            raise RuntimeError("上一个搜索还在进行中，请稍候再试")
        self._search_busy = True
        try:
            return self._submit(self._search(query, channels or [], limit), timeout)
        finally:
            self._search_busy = False
