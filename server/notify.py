# -*- coding: utf-8 -*-
"""server/notify.py - 通知推送（Server酱 / Telegram / 企业微信 Webhook）

配置存于 config.json 的 "notify" 段：
    {
        "serverchan_key": "SCT...",              # Server酱 SendKey
        "telegram_bot_token": "123456:AA...",    # Telegram Bot Token
        "telegram_chat_id": "123456789",         # Telegram 接收 Chat ID
        "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
        "enabled": true,                          # 总开关
        "on_new_eps": true,                       # 追更到新集
        "on_download": true,                      # 下载成功
        "on_fail": true,                          # 下载失败/检查失败
    }

发送失败不抛异常（静默降级），避免影响追更主流程。
"""
import threading
import time

import requests

from .utils import load_config

_HTTP_TIMEOUT = 10  # 秒


def _get_notify_cfg():
    cfg = load_config().get("notify", {}) or {}
    return cfg


def send_notify(title, message, event="on_new_eps", is_fail=False):
    """统一入口：向所有已配置渠道推送一条通知。

    event: on_new_eps / on_download / on_fail（对应 config 开关）
    is_fail: 失败类消息（标题会加 ⚠️，并总是尝试发送，便于及时感知）
    """
    nc = _get_notify_cfg()
    if not nc.get("enabled", True):
        return False
    switch_key = "on_fail" if is_fail else event
    if not nc.get(switch_key, True):
        return False
    # 后台线程发送，不阻塞追更主流程
    t = threading.Thread(target=_send_all, args=(title, message, nc), daemon=True)
    t.start()
    return True


def _send_all(title, message, nc):
    sent = False
    if nc.get("serverchan_key"):
        try:
            _send_serverchan(nc["serverchan_key"], title, message)
            sent = True
        except Exception:
            pass
    if nc.get("telegram_bot_token") and nc.get("telegram_chat_id"):
        try:
            _send_telegram(nc["telegram_bot_token"], nc["telegram_chat_id"], title, message)
            sent = True
        except Exception:
            pass
    if nc.get("wecom_webhook"):
        try:
            _send_wecom(nc["wecom_webhook"], title, message)
            sent = True
        except Exception:
            pass
    if not sent:
        # 没有任何渠道配置成功时，记录到日志方便排查（不抛异常）
        try:
            import logging
            logging.getLogger(__name__).warning("[notify] 发送失败或无可用渠道: %s", title)
        except Exception:
            pass


def _send_serverchan(key, title, message):
    """Server酱 (sct.ftqq.com)"""
    url = f"https://sctapi.ftqq.com/{key}.send"
    payload = {"title": title, "desp": message}
    resp = requests.post(url, data=payload, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") not in (0, None):
        raise RuntimeError(f"serverchan error: {data}")


def _send_telegram(bot_token, chat_id, title, message):
    """Telegram Bot API"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": f"<b>{title}</b>\n{message}", "parse_mode": "HTML"}
    resp = requests.post(url, json=payload, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")


def _send_wecom(webhook, title, message):
    """企业微信机器人 Webhook"""
    # 企业微信 markdown 最多 4096 字符，截断
    text = f"**{title}**\n{message}"[:4000]
    payload = {"msgtype": "markdown", "markdown": {"content": text}}
    resp = requests.post(webhook, json=payload, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"wecom error: {data}")


def test_send(serverchan_key="", telegram_bot_token="", telegram_chat_id="", wecom_webhook=""):
    """后台界面"发送测试"用：用临时配置发一条测试消息，同步返回结果。"""
    nc = {
        "enabled": True,
        "serverchan_key": serverchan_key or "",
        "telegram_bot_token": telegram_bot_token or "",
        "telegram_chat_id": telegram_chat_id or "",
        "wecom_webhook": wecom_webhook or "",
    }
    _send_all("WPS 追更通知测试 ✅", "这是一条测试通知，收到即表示通知配置正常。", nc)
    return True
