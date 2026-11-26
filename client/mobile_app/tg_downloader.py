"""Telegram 独立登录与文件下载（客户端侧）。

设计要点：
  - 客户端使用自己的 Telegram 登录态（独立 StringSession），与服务端账号会话隔离，
    避免同一 auth key 在多处并发使用被 Telegram 判定重复登录而互相踢线。
  - 登录采用官方二维码流程（exportLoginToken 轮询），无需短信验证码。
  - 下载直接走 MTProto，不经过服务端中转。
"""
import os
import time
import datetime
import asyncio


class TgError(Exception):
    """Telegram 相关错误（带中文提示）"""


def _import_telethon():
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient, StringSession
    except ImportError as e:
        raise TgError("Telegram 模块缺失（%s），请更新到最新版客户端" % e)


class TgDownloader:
    def __init__(self, api_id, api_hash, session_str="", proxy=None):
        try:
            self.api_id = int(api_id)
        except (TypeError, ValueError):
            raise TgError("api_id 必须是数字")
        self.api_hash = (api_hash or "").strip()
        self.session_str = (session_str or "").strip()
        self.proxy = proxy or None
        if not self.api_hash:
            raise TgError("未配置 api_hash")

    def _new_client(self):
        TelegramClient, StringSession = _import_telethon()
        return TelegramClient(
            session=StringSession(self.session_str),
            api_id=self.api_id,
            api_hash=self.api_hash,
            proxy=self.proxy,
            timeout=60,
            connection_retries=3,
        )

    # ────────────── 二维码登录 ──────────────

    def login_with_qr(self, qr_png_path, timeout=300, on_url=None):
        """阻塞执行二维码登录。

        返回 (session_string, error)。成功时 error 为空字符串。
        qr_png_path：二维码图片保存路径（每 30 秒刷新覆盖），供用户扫描。
        """
        return asyncio.run(self._login_with_qr(qr_png_path, timeout, on_url))

    async def _login_with_qr(self, qr_png_path, timeout, on_url):
        client = self._new_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                return client.session.save(), ""
            qr = await client.qr_login()
            self._save_qr_png(qr.url, qr_png_path)
            if on_url:
                try:
                    on_url(qr.url)
                except Exception:
                    pass
            deadline = time.time() + timeout
            while time.time() < deadline:
                left = 30
                try:
                    left = int((qr.expires - datetime.datetime.now(
                        tz=datetime.timezone.utc)).total_seconds()) + 3
                except Exception:
                    pass
                left = max(5, min(left, 45))
                try:
                    await qr.wait(timeout=left)
                    return client.session.save(), ""
                except asyncio.TimeoutError:
                    try:
                        await qr.recreate()
                        self._save_qr_png(qr.url, qr_png_path)
                        if on_url:
                            try:
                                on_url(qr.url)
                            except Exception:
                                pass
                    except Exception as e:
                        return "", "刷新二维码失败：%s" % e
            return "", "二维码已过期，请重新生成"
        except Exception as e:
            return "", "登录失败：%s" % e
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _save_qr_png(url, path):
        """把 tg:// 链接保存为二维码 PNG（无 qrcode 库时降级为写入文本）。"""
        try:
            import qrcode
            img = qrcode.make(url)
            img.save(path)
        except Exception:
            with open(path + ".txt", "w", encoding="utf-8") as f:
                f.write(url)

    # ────────────── 下载 ──────────────

    def download(self, channel_value, msg_id, dest_path, progress_cb=None, cancel_evt=None,
                 log_cb=None):
        """下载指定频道的消息文件到 dest_path。返回 (ok, error)。"""
        return asyncio.run(self._download(channel_value, msg_id, dest_path,
                                          progress_cb, cancel_evt, log_cb))

    async def _download(self, channel_value, msg_id, dest_path, progress_cb, cancel_evt,
                        log_cb=None):
        def _log(msg):
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass

        client = self._new_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return False, "客户端 Telegram 尚未登录，请先在设置里扫码登录"
            _log("已连接 Telegram")
            try:
                entity = await client.get_entity(channel_value)
            except Exception as e:
                return False, "无法解析频道（请先在客户端加入该频道）：%s" % e
            try:
                msg = await client.get_messages(entity, ids=int(msg_id))
            except Exception as e:
                return False, "获取消息失败：%s" % e
            if not msg:
                return False, "该消息不存在或已删除"
            if not getattr(msg, "file", None):
                return False, "该消息不包含可下载文件"
            _log("开始下载消息 %s" % msg_id)

            def _cb(cur, tot):
                if progress_cb:
                    try:
                        progress_cb(cur, tot)
                    except Exception:
                        pass
                if cancel_evt is not None and cancel_evt.is_set():
                    raise TgError("已取消")

            d = os.path.dirname(os.path.abspath(dest_path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            try:
                await client.download_media(msg, file=dest_path, progress_callback=_cb)
            except TgError:
                return False, "已取消"
            except Exception as e:
                return False, "下载失败：%s" % e
            _log("下载结束：%s" % dest_path)
            return True, ""
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
