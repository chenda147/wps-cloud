"""
手机版客户端配置与通用工具模块
负责：配置文件读写（Android 私有目录）、字节格式化、时间字符串、版本号。
纯函数实现，不依赖 tkinter / Kivy，便于单独测试与维护。
"""
import os
import sys
import json
import time

# 客户端版本号（每次发布更新时递增）。用于服务端识别客户端能力（如支持的云盘类型）。
CLIENT_VERSION = "1.0.9"
APP_NAME = "WPS传输工具"


def _detect_base_dir():
    """返回配置存储目录：
    - Android: 应用私有目录（files）
    - 桌面调试: 脚本所在目录下的 .wps_mobile
    """
    try:
        android_private = os.environ.get("ANDROID_PRIVATE", "")
        if android_private:
            return os.path.join(android_private, "files")
    except Exception:
        pass
    try:
        from kivy.utils import platform
        if platform == "android":
            from android.storage import app_storage_path
            return app_storage_path()
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wps_mobile")


BASE_DIR = _detect_base_dir()
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def get_save_dir():
    """下载临时目录：优先用配置 save_dir（自定义路径），否则默认应用私有目录 downloads。
    上传完成后自动删除。Android 上自定义公共目录（如 /sdcard/Download）需在系统设置
    授予“所有文件访问权限”（MANAGE_EXTERNAL_STORAGE），否则可能写入/删除失败。"""
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    d = (cfg.get("save_dir") or "").strip()
    if not d:
        d = os.path.join(BASE_DIR, "downloads")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        # 自定义路径不可写时回退到应用私有目录，保证下载不中断
        d = os.path.join(BASE_DIR, "downloads")
        os.makedirs(d, exist_ok=True)
    return d


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_config():
    cfg = _read_json(CONFIG_FILE) or {}
    return cfg


def save_config(cfg):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def fmt_size(b):
    if not b or b <= 0:
        return "0 B"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def now():
    return time.strftime("%H:%M:%S")
