"""
139→WPS 传输客户端
Fluent 风格界面，参考传票打印工具设计
"""
import os
import sys
import json
import time
import socket
import threading
import platform
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import requests
from requests.exceptions import ProxyError, ConnectionError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfer_engine import TransferEngine
from chase_engine import find_new_episodes, find_max_episode, extract_episode
from config_helper import (
    CLIENT_VERSION, CONFIG_FILE, load_config, save_config, fmt_size, now,
)

class App:
    def __init__(self, root):
        self.root = root
        if platform.system() == 'Windows':
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.root.title(f"139 → WPS 传输 v{CLIENT_VERSION}")
        self.root.geometry("820x560")
        self.root.minsize(700, 480)

        self._retrying = False
        self._srv_thread = None
        self._srv_stop = threading.Event()
        self._srv_toggle_cd = False      # 按钮防抖，防止双击竞态
        self._transfer_start = None  # 传输开始时间
        # 多线程并发
        cfg = load_config()
        self._cfg_clouds = cfg.get("clouds", [])
        self._max_concurrent = cfg.get("max_concurrent", 3)
        self._wps_group_id = cfg.get("wps_group_id", "") or "2203072050"  # 缺省回退默认群组
        self._server_creds = None  # 服务端下发的网盘凭据（WPS cookie / 139 / 夸克），连接后拉取临时存内存
        self._task_semaphore = threading.Semaphore(self._max_concurrent)
        self._task_active = 0  # 当前活跃任务数
        self._task_active_lock = threading.Lock()
        self._cancel_events = {}  # chase_id -> threading.Event
        self._cancel_ts = {}      # chase_id -> 取消指令首次收到时间戳（60秒后自动恢复）
        self._cancel_events_lock = threading.Lock()
        self._task_widgets = {}   # tid -> widget dict (任务面板)
        self._running_tids = set()  # 正在运行的任务ID，防止重复分发
        self._running_tids_lock = threading.Lock()
        self._task_last_dl = {}   # tid -> (timestamp, dl_value) 用于进度卡死检测
        self._srv_session = None  # 当前服务端会话（_srv_loop 线程维护，供进度上报）
        self._srv_url = ""        # 当前服务端地址
        self._prog_last_ts = [0.0]  # 实时进度上报节流时间戳（3 秒一次）

        self._setup_style()
        self._build_ui(cfg)
        self.root.after(5000, self._check_stale_progress)  # 启动卡死检测
        self._setup_hotkey()  # ALT+F2 隐藏/显示窗口

    # ════════════════════════════════════
    # 老板键：ALT+F2 隐藏/显示主窗口
    # 用 Win32 RegisterHotKey 全局热键，窗口隐藏(任务栏图标也消失)后
    # 仍可再次按 ALT+F2 唤回。ctypes 实现，零第三方依赖。
    # ════════════════════════════════════
    def _setup_hotkey(self):
        self._hidden = False
        self._hotkey_ok = False
        if platform.system() != 'Windows':
            return
        try:
            import ctypes
            self._user32 = ctypes.windll.user32
            self._hotkey_id = 0xB001          # 自定义热键 ID，避免冲突
            MOD_ALT = 0x0001
            VK_F2 = 0x71
            # hWnd 传 NULL：热键与当前线程消息队列关联，不依赖窗口句柄
            self._hotkey_ok = bool(self._user32.RegisterHotKey(
                None, self._hotkey_id, MOD_ALT, VK_F2))
            if self._hotkey_ok:
                self.root.after(100, self._hotkey_poll)
                self._log("已启用 ALT+F2 隐藏/显示窗口")
            else:
                self._log("警告: ALT+F2 热键注册失败(可能已被其他程序占用)")
        except Exception as e:
            self._log(f"警告: ALT+F2 热键初始化失败: {e}")

    def _hotkey_poll(self):
        """周期轮询线程消息队列，捕获 WM_HOTKEY 消息"""
        if not getattr(self, '_hotkey_ok', False):
            return
        try:
            import ctypes
            from ctypes import wintypes
            WM_HOTKEY = 0x0312
            PM_REMOVE = 0x0001
            msg = wintypes.MSG()
            while self._user32.PeekMessageW(
                    ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                if msg.wParam == self._hotkey_id:
                    self._toggle_visible()
        except Exception:
            pass
        self.root.after(100, self._hotkey_poll)

    def _toggle_visible(self):
        """ALT+F2：隐藏/显示主窗口"""
        try:
            if self._hidden:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes('-topmost', True)
                self.root.attributes('-topmost', False)
                self.root.focus_force()
                self._hidden = False
            else:
                self.root.withdraw()   # 连任务栏图标一起隐藏
                self._hidden = True
        except Exception:
            pass

    # ════════════════════════════════════
    # 样式
    # ════════════════════════════════════
    def _setup_style(self):
        self.背景色 = '#f5f5f5'
        self.主色 = '#0078D7'
        self.次色 = '#106EBE'
        self.成功色 = '#107C10'
        self.错误色 = '#D13438'
        self.文本色 = '#323130'
        self.次文本 = '#605E5C'

        s = ttk.Style()
        if 'clam' in s.theme_names():
            s.theme_use('clam')

        s.configure('.', background=self.背景色, foreground=self.文本色, font=('微软雅黑', 9))
        s.configure('Card.TFrame', background='white')
        s.configure('Card.TLabel', background='white', foreground=self.文本色, font=('微软雅黑', 9))
        s.configure('Sub.TLabel', background='white', foreground=self.次文本, font=('微软雅黑', 8))
        s.configure('Nav.TFrame', background='white')
        s.configure('Nav.TLabel', background='white', foreground=self.次文本, font=('微软雅黑', 10))
        s.configure('NavAct.TLabel', background='#EFF6FC', foreground=self.主色, font=('微软雅黑', 10, 'bold'))
        s.configure('NavBody.TFrame', background=self.背景色)
        s.configure('TaskBg.TFrame', background=self.背景色)

        s.configure('Entry.TEntry', fieldbackground='white', foreground=self.文本色,
                   borderwidth=1, relief='solid')
        s.map('Entry.TEntry', bordercolor=[('focus', self.主色)])

        s.configure('CB.TCombobox', fieldbackground='white', foreground=self.文本色,
                   background='white', borderwidth=1, relief='solid', arrowcolor=self.主色)
        s.map('CB.TCombobox', fieldbackground=[('readonly', 'white')],
              bordercolor=[('focus', self.主色)])

        s.configure('Prim.TButton', font=('微软雅黑', 9), padding=(14, 5),
                   background=self.主色, foreground='white', borderwidth=0)
        s.map('Prim.TButton', background=[('active', self.次色), ('disabled', '#C8C8C8')],
              foreground=[('disabled', '#F0F0F0')])

        s.configure('Danger.TButton', font=('微软雅黑', 9), padding=(14, 5),
                   background=self.错误色, foreground='white', borderwidth=0)
        s.map('Danger.TButton', background=[('active', '#A4262C'), ('disabled', '#C8C8C8')])

        s.configure('Sec.TButton', font=('微软雅黑', 9), padding=(10, 4))
        s.map('Sec.TButton', background=[('active', '#E5E5E5')])

        s.configure('Mod.Horizontal.TProgressbar', troughcolor='#E0E0E0', background=self.主色,
                   bordercolor='#E0E0E0', lightcolor=self.主色, darkcolor=self.主色,
                   borderwidth=0, thickness=6)

    # ════════════════════════════════════
    # 界面构建
    # ════════════════════════════════════
    def _build_ui(self, cfg):
        self.root.configure(bg=self.背景色)

        # 主容器
        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True)

        # 左侧导航
        self.nav = ttk.Frame(main, style='Nav.TFrame', width=160)
        self.nav.pack(side=tk.LEFT, fill=tk.Y)
        self.nav.pack_propagate(False)

        ttk.Label(self.nav, text="⚡  139→WPS", style='NavAct.TLabel',
                 font=('微软雅黑', 12, 'bold')).pack(pady=(16, 12), padx=12, anchor='w')

        ttk.Separator(self.nav, orient='horizontal').pack(fill=tk.X, padx=8, pady=(0, 6))

        # 右侧内容区
        self.content = ttk.Frame(main, style='NavBody.TFrame')
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12, pady=10)

        self._nav_btns = {}
        self._pages = {}
        self._cur = None
        for key, label in [("transfer","传输"), ("config","配置"),
                           ("server","服务器"), ("chase","追更"),
                           ("tasks","任务"), ("log","日志")]:
            btn = ttk.Label(self.nav, text=f"  {label}", style='Nav.TLabel',
                           cursor='hand2', padding=(12, 8))
            btn.pack(fill=tk.X, padx=4, pady=1)
            btn.bind('<Button-1>', lambda e, k=key: self._show(k))
            self._nav_btns[key] = btn
            self._pages[key] = ttk.Frame(self.content, style='NavBody.TFrame')

        # 构建各页面
        self._build_transfer(cfg)
        self._build_config(cfg)
        self._build_server(cfg)
        self._build_chase(cfg)
        self._build_tasks()
        self._build_log()

        self._show("transfer")

    def _show(self, key):
        if self._cur:
            self._pages[self._cur].pack_forget()
            self._nav_btns[self._cur].configure(style='Nav.TLabel')
        self._pages[key].pack(fill=tk.BOTH, expand=True)
        self._nav_btns[key].configure(style='NavAct.TLabel')
        self._cur = key
        if key == "chase":
            self._chase_refresh()

    # ── 通用：卡片 ──
    def _card(self, parent, title=None):
        group = ttk.LabelFrame(parent, text=f" {title} " if title else "",
                              padding=12, style='Card.TFrame')
        group.pack(fill=tk.X, pady=(0, 8))
        return group

    # ════════════════════════════════════
    # 传输页
    # ════════════════════════════════════
    def _build_transfer(self, cfg):
        page = self._pages["transfer"]

        # 任务卡片
        card = self._card(page, "任务")
        self.url_var = tk.StringVar()
        self.pwd_var = tk.StringVar()
        self.rename_var = tk.StringVar()
        self.show_folder_var = tk.StringVar()
        self.category_var = tk.StringVar(value="电影")
        self.file_name_var = tk.StringVar()
        self._share_files = []  # 分享文件列表缓存
        self._filtering = False  # 过滤中，阻止自动重命名

        # 链接 + 获取列表按钮
        f1 = ttk.Frame(card, style='Card.TFrame')
        f1.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(f1, text="分享链接", style='Sub.TLabel').pack(anchor='w')
        f1r = ttk.Frame(f1, style='Card.TFrame')
        f1r.pack(fill=tk.X, pady=(2, 0))
        ttk.Entry(f1r, textvariable=self.url_var, font=('微软雅黑', 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.fetch_btn = ttk.Button(f1r, text="获取列表", command=self._fetch_files, width=9)
        self.fetch_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # 提取码
        f2 = ttk.Frame(card, style='Card.TFrame')
        f2.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(f2, text="提取码", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(f2, textvariable=self.pwd_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 0))

        # 选择文件（可输入过滤）
        ff = ttk.Frame(card, style='Card.TFrame')
        ff.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(ff, text="选择文件", style='Sub.TLabel').pack(anchor='w')
        self.files_cb = ttk.Combobox(ff, textvariable=self.file_name_var,
                                     font=('微软雅黑', 10), style='CB.TCombobox')
        self.files_cb.pack(fill=tk.X, pady=(2, 0))
        self.files_cb.bind('<KeyRelease>', self._filter_files)
        self.files_cb.bind('<<ComboboxSelected>>', self._on_file_selected)

        # 剧名文件夹
        fs = ttk.Frame(card, style='Card.TFrame')
        fs.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(fs, text="剧名文件夹（同剧集存同一目录）", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(fs, textvariable=self.show_folder_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 0))

        # 重命名 + 类型 两列
        f3 = ttk.Frame(card, style='Card.TFrame')
        f3.pack(fill=tk.X, pady=(0, 6))
        l = ttk.Frame(f3, style='Card.TFrame')
        l.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        ttk.Label(l, text="重命名（片名）", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(l, textvariable=self.rename_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 0))

        r = ttk.Frame(f3, style='Card.TFrame')
        r.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(r, text="类型", style='Sub.TLabel').pack(anchor='w')
        ttk.Combobox(r, textvariable=self.category_var, values=["电影","电视剧","国漫","日漫"],
                    state="readonly", font=('微软雅黑', 10), style='CB.TCombobox', width=8).pack(pady=(2, 0))

        # 按钮
        bf = ttk.Frame(card, style='Card.TFrame')
        bf.pack(fill=tk.X, pady=(6, 0))
        self.start_btn = ttk.Button(bf, text="开始传输", style='Prim.TButton', command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.start_all_btn = ttk.Button(bf, text="全部传输", style='Sec.TButton',
                                        command=self._start_all)
        self.start_all_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = ttk.Button(bf, text="取消", style='Danger.TButton',
                                    command=self._cancel, state='disabled')
        self.cancel_btn.pack(side=tk.LEFT)


    # ════════════════════════════════════
    # 配置页
    # ════════════════════════════════════
    def _build_config(self, cfg):
        page = self._pages["config"]

        c1 = self._card(page, "139 云盘（主账号）")
        self.token_var = tk.StringVar(value=cfg.get("yidong_token", ""))
        ttk.Label(c1, text="Token", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c1, textvariable=self.token_var, show="•",
                 font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))

        # ── 夸克网盘（单输入框，和 139 主账号平级）──
        qk = self._card(page, "夸克网盘")
        qk_cookie = ""
        for _c in cfg.get("clouds", []):
            if _c.get("type") == "quark":
                qk_cookie = _c.get("cookie", "") or _c.get("token", "")
                break
        self.quark_cookie_var = tk.StringVar(value=qk_cookie)
        ttk.Label(qk, text="Cookie（从 pan.quark.cn 登录后 F12 复制请求里的 Cookie）",
                  style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(qk, textvariable=self.quark_cookie_var, show="•",
                 font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))

        # ── 云盘连接管理（支持 139 / 夸克 并存，自动识别）──
        # 夸克配置已通过上方单输入框卡片管理，无需独立的多账号连接管理
        self.clouds = list(cfg.get("clouds", []))

        c2 = self._card(page, "WPS 365")
        self.cookie_var = tk.StringVar(value=cfg.get("wps_cookie", ""))
        self.parent_var = tk.StringVar(value=cfg.get("wps_parent_id", "0"))
        ttk.Label(c2, text="Cookie", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c2, textvariable=self.cookie_var, show="•",
                 font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))
        ttk.Label(c2, text="文件夹ID（0=根目录）", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c2, textvariable=self.parent_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))

        c3 = self._card(page, "本地目录")
        self.dir_var = tk.StringVar(value=cfg.get("save_dir",
                             os.path.join(os.path.expanduser("~"), "Downloads")))
        ttk.Label(c3, text="下载保存位置", style='Sub.TLabel').pack(anchor='w')
        df = ttk.Frame(c3, style='Card.TFrame')
        df.pack(fill=tk.X, pady=(2, 6))
        ttk.Entry(df, textvariable=self.dir_var, font=('微软雅黑', 10)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(df, text="浏览", style='Sec.TButton', command=self._browse).pack(side=tk.RIGHT)

        ttk.Button(c3, text="保存配置", style='Prim.TButton', command=self._save).pack(anchor='w')

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d: self.dir_var.set(d)

    def _save(self):
        try:
            mc = int(self.max_conc_var.get().strip())
        except (ValueError, AttributeError):
            mc = 3
        # 同步夸克单输入框到 clouds（保留其它类型连接）
        qk = self.quark_cookie_var.get().strip()
        others = [c for c in self.clouds if c.get("type") != "quark"]
        if qk:
            existing = next((c for c in self.clouds if c.get("type") == "quark"), None)
            if existing:
                existing["cookie"] = qk
                others.append(existing)
            else:
                others.append({"id": f"quark_{int(time.time())}", "type": "quark",
                               "name": "夸克网盘", "is_primary": True, "cookie": qk})
        self.clouds = others
        # 同步 139 主账号 token 到 clouds（若 clouds 里尚无 yidong 类型连接）
        yd_token = self.token_var.get().strip()
        if yd_token and not any(c.get("type") == "yidong" for c in self.clouds):
            self.clouds.append({"id": "yidong_main", "type": "yidong",
                                "name": "139主账号", "is_primary": True, "token": yd_token})
        cfg = load_config()
        cfg.update({
            "yidong_token": self.token_var.get().strip(),
            "wps_cookie": self.cookie_var.get().strip(),
            "save_dir": self.dir_var.get().strip(),
            "wps_parent_id": self.parent_var.get().strip(),
            "clouds": self.clouds,
            "server_url": getattr(self, "srv_url_var", tk.StringVar()).get().strip(),
            "server_user": getattr(self, "srv_user_var", tk.StringVar()).get().strip(),
            "server_pass": getattr(self, "srv_pass_var", tk.StringVar()).get().strip(),
            "client_name": getattr(self, "srv_name_var", tk.StringVar()).get().strip(),
            "max_concurrent": mc,
            "upload_slow_min_mb": self._upload_slow_mb(),
            "upload_slow_seconds": self._upload_slow_sec(),
        })
        save_config(cfg)
        messagebox.showinfo("成功", "配置已保存")

    def _upload_slow_mb(self):
        """读取上传慢速阈值(MB/s)，非法/空返回0（不启用）"""
        try:
            v = float(getattr(self, "upload_slow_mb_var", tk.StringVar()).get().strip())
            return max(0.0, v)
        except (ValueError, AttributeError):
            return 0.0

    def _upload_slow_sec(self):
        """读取上传慢速持续秒数，非法/空返回0（不启用）"""
        try:
            v = float(getattr(self, "upload_slow_sec_var", tk.StringVar()).get().strip())
            return max(0.0, v)
        except (ValueError, AttributeError):
            return 0.0

    # ════════════════════════════════════
    # 服务器页
    # ════════════════════════════════════
    def _build_server(self, cfg):
        page = self._pages["config"]  # 修正：server 页
        page = self._pages["server"]

        c = self._card(page, "远程服务器")
        self.srv_url_var = tk.StringVar(value=cfg.get("server_url", "http://127.0.0.1:5000"))
        self.srv_user_var = tk.StringVar(value=cfg.get("server_user", ""))
        self.srv_pass_var = tk.StringVar(value=cfg.get("server_pass", ""))
        self.srv_name_var = tk.StringVar(value=cfg.get("client_name", socket.gethostname()))
        self.max_conc_var = tk.StringVar(value=str(cfg.get("max_concurrent", 3)))

        ttk.Label(c, text="服务器地址", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c, textvariable=self.srv_url_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))
        ttk.Label(c, text="客户端昵称（区分不同机器）", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c, textvariable=self.srv_name_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))
        ttk.Label(c, text="并发任务数（同时跑几个）", style='Sub.TLabel').pack(anchor='w')
        spin_f = ttk.Frame(c, style='Card.TFrame')
        spin_f.pack(fill=tk.X, pady=(2, 6))
        self.max_conc_spin = tk.Spinbox(spin_f, from_=1, to=10, textvariable=self.max_conc_var,
                                         font=('微软雅黑', 10), width=5)
        self.max_conc_spin.pack(side=tk.LEFT)
        ttk.Label(spin_f, text="  建议 1~5 个，看机器性能", style='Sub.TLabel').pack(side=tk.LEFT)

        # ── 上传慢速重试（可选，0=不启用）──
        self.upload_slow_mb_var = tk.StringVar(
            value=cfg.get("upload_slow_min_mb", "") if cfg.get("upload_slow_min_mb") else "")
        self.upload_slow_sec_var = tk.StringVar(
            value=cfg.get("upload_slow_seconds", "") if cfg.get("upload_slow_seconds") else "")
        ttk.Label(c, text="上传速度低于以下值自动重新上传（留空/0 = 不启用）",
                  style='Sub.TLabel').pack(anchor='w')
        slow_f = ttk.Frame(c, style='Card.TFrame')
        slow_f.pack(fill=tk.X, pady=(2, 6))
        ttk.Label(slow_f, text="阈值", style='Sub.TLabel').pack(side=tk.LEFT)
        tk.Spinbox(slow_f, from_=0.1, to=100, increment=0.1,
                   textvariable=self.upload_slow_mb_var, width=6,
                   font=('微软雅黑', 10)).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(slow_f, text="MB/s", style='Sub.TLabel').pack(side=tk.LEFT)
        ttk.Label(slow_f, text="  持续", style='Sub.TLabel').pack(side=tk.LEFT, padx=(8, 2))
        tk.Spinbox(slow_f, from_=5, to=600, increment=5,
                   textvariable=self.upload_slow_sec_var, width=6,
                   font=('微软雅黑', 10)).pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(slow_f, text="秒", style='Sub.TLabel').pack(side=tk.LEFT)
        ttk.Label(c, text="上传太慢时中断并自动重传，不重新下载（本地文件保留）",
                  style='Sub.TLabel').pack(anchor='w', pady=(0, 6))

        ttk.Label(c, text="用户名", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c, textvariable=self.srv_user_var, font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))
        ttk.Label(c, text="密码", style='Sub.TLabel').pack(anchor='w')
        ttk.Entry(c, textvariable=self.srv_pass_var, show="•",
                 font=('微软雅黑', 10)).pack(fill=tk.X, pady=(2, 6))
        ttk.Label(c, text="客户端无需公网IP，主动轮询服务器获取任务",
                 style='Sub.TLabel').pack(anchor='w', pady=(0, 6))

        bf = ttk.Frame(c, style='Card.TFrame')
        bf.pack(fill=tk.X, pady=(4, 6))
        self.srv_btn = ttk.Button(bf, text="连接服务器", style='Prim.TButton', command=self._toggle_srv)
        self.srv_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bf, text="测试连接", style='Sec.TButton', command=self._test_srv).pack(side=tk.LEFT)

        sf = ttk.Frame(c, style='Card.TFrame')
        sf.pack(fill=tk.X)
        self.srv_dot = tk.Canvas(sf, width=10, height=10, bg='white', highlightthickness=0)
        self.srv_dot.pack(side=tk.LEFT)
        self.srv_dot.create_oval(1, 1, 9, 9, fill='#848D97', outline='')
        self.srv_status_var = tk.StringVar(value="未连接")
        ttk.Label(sf, textvariable=self.srv_status_var, style='Card.TLabel',
                 foreground=self.次文本).pack(side=tk.LEFT, padx=(6, 0))

    def _srv_status(self, text, color):
        self.srv_status_var.set(text)
        self.srv_dot.delete('all')
        self.srv_dot.create_oval(1, 1, 9, 9, fill=color, outline='')

    def _test_srv(self):
        url = self.srv_url_var.get().strip().rstrip("/")
        if not url:
            messagebox.showerror("错误", "请填地址"); return
        self._srv_status("测试中...", self.主色)
        def _do():
            try:
                r = requests.get(f"{url}/api/status", timeout=10)
                if r.status_code == 200:
                    self._log("服务器连接成功")
                    self._srv_status("在线", self.成功色)
                else:
                    self._log(f"服务器返回 {r.status_code}")
                    self._srv_status("失败", self.错误色)
            except Exception as e:
                self._log(f"连接失败: {e}")
                self._srv_status("失败", self.错误色)
        threading.Thread(target=_do, daemon=True).start()

    def _toggle_srv(self):
        # 防抖：防止快速双击导致竞态
        if self._srv_toggle_cd:
            return
        self._srv_toggle_cd = True
        self.root.after(1500, lambda: setattr(self, '_srv_toggle_cd', False))

        if self._srv_thread and self._srv_thread.is_alive():
            self._srv_stop.set()
            self.srv_btn.config(text="连接服务器")
            self._srv_status("已断开", '#848D97')
            self._log("服务器已停止")
        else:
            # 连接时自动保存服务器信息并重新加载最大并发数
            self._save_silent()
            # 重新从配置文件读取 max_concurrent（可能已被修改）
            try:
                cfg = load_config()
                new_max = cfg.get("max_concurrent", 3)
                if new_max != self._max_concurrent:
                    self._max_concurrent = new_max
                    self._task_semaphore = threading.Semaphore(new_max)
            except Exception: pass
            self._srv_stop.clear()
            self._srv_thread = threading.Thread(target=self._srv_loop, daemon=True)
            self._srv_thread.start()
            self.srv_btn.config(text="断开")
            self._srv_status("连接中...", self.主色)

    def _save_silent(self):
        """保存配置不弹窗（连接服务器时调用）。

        注意：必须保留 clouds 等其它字段，不能用硬编码字段清单整体覆盖，
        否则会把用户已配置的夸克/网盘账号清空。
        """
        try:
            try:
                mc = int(self.max_conc_var.get().strip())
            except (ValueError, AttributeError):
                mc = 3
            cfg = load_config()
            cfg.update({
                "yidong_token": self.token_var.get().strip(),
                "wps_cookie": self.cookie_var.get().strip(),
                "save_dir": self.dir_var.get().strip(),
                "wps_parent_id": self.parent_var.get().strip(),
                "server_url": self.srv_url_var.get().strip(),
                "server_user": self.srv_user_var.get().strip(),
                "server_pass": self.srv_pass_var.get().strip(),
                "client_name": self.srv_name_var.get().strip(),
                "max_concurrent": mc,
                "upload_slow_min_mb": self._upload_slow_mb(),
                "upload_slow_seconds": self._upload_slow_sec(),
            })
            # 确保 clouds 不被丢弃（从内存中的 self.clouds 兜底）
            if "clouds" not in cfg and getattr(self, "clouds", None):
                cfg["clouds"] = self.clouds
            save_config(cfg)
        except Exception:
            pass

    def _srv_login(self, session, url):
        user = self.srv_user_var.get().strip()
        pw = self.srv_pass_var.get().strip()
        if not user or not pw: return True
        try:
            r = session.post(f"{url}/api/login",
                            json={"username": user, "password": pw}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return True
                self._log(f"登录失败: {data.get('message', '未知错误')}")
                return False
            self._log(f"登录失败: HTTP {r.status_code}")
            return False
        except Exception as e:
            self._log(f"登录异常: {e}")
            return False

    def _fetch_server_creds(self, session, url, client_name):
        """连接服务端后拉取已配置的网盘凭据（WPS cookie / 139 / 夸克），临时存内存。

        客户端无需再手动填写 cookie/token；旧版服务端无此接口时静默返回 None，
        继续使用本地手动配置兜底。
        """
        try:
            r = session.get(f"{url}/api/client/creds",
                            params={"client": client_name}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                if d.get("success"):
                    wps_cookie = (d.get("wps_cookie") or "").strip()
                    clouds = d.get("clouds") or []
                    if wps_cookie or clouds:
                        self._server_creds = {"wps_cookie": wps_cookie, "clouds": clouds}
                        _types = ",".join(sorted({str(c.get("type", "")) for c in clouds if c.get("type")}))
                        self._log(f"已获取服务端网盘凭据（WPS: {'已配置' if wps_cookie else '未配置'}，云盘: {_types or '无'}）")
                        return self._server_creds
        except Exception:
            pass
        self._server_creds = None
        return None

    def _srv_loop(self):
        url = self.srv_url_var.get().strip().rstrip("/")
        if not url:
            self._log("请填服务器地址")
            self.root.after(0, lambda: self.srv_btn.config(text="连接服务器")); return
        session = requests.Session()
        if not self._srv_login(session, url):
            self._log("服务器登录失败")
            self._srv_status("登录失败", self.错误色)
            self.root.after(0, lambda: self.srv_btn.config(text="连接服务器"))
            self._srv_session = None; self._srv_url = ""
            return
        client_name = self.srv_name_var.get().strip()
        max_conc = self._max_concurrent
        # 连接成功后自动拉取服务端网盘凭据（临时缓存），此后无需本地手动配置
        self._server_creds = self._fetch_server_creds(session, url, client_name)
        self._log(f"已连接 {url}，客户端: {client_name}，并发: {max_conc}，开始轮询")
        self._srv_status(f"轮询中({max_conc}并发)", self.成功色)
        self._srv_session = session; self._srv_url = url
        while not self._srv_stop.is_set():
            try:
                # 统计当前活跃任务数
                with self._task_active_lock:
                    active_now = self._task_active
                free_slots = max(1, max_conc - active_now)
                # 轮询：请求 batch 个任务 + 检查取消指令 + 上报正在运行的任务ID
                with self._running_tids_lock:
                    running_ids = ",".join(self._running_tids) if self._running_tids else ""
                # 上报本客户端支持的云盘类型（用于服务端推送前校验）。
                # 每次轮询实时从 config.json 读取，保存夸克配置后无需重启客户端即可生效。
                try:
                    _live_clouds = load_config().get("clouds", [])
                except Exception:
                    _live_clouds = self._cfg_clouds
                # 服务端下发的云盘凭据也算入本客户端能力，避免服务端因 cloud_types 为空不下发任务
                if self._server_creds and self._server_creds.get("clouds"):
                    _live_clouds = list(_live_clouds) + list(self._server_creds["clouds"])
                cloud_types = ",".join(sorted({str(c.get("type", "")) for c in _live_clouds if c.get("type")}))
                params = {"client": client_name, "batch": str(free_slots),
                          "max_concurrent": str(max_conc), "check_cancel": "1",
                          "running_tids": running_ids,
                          "version": CLIENT_VERSION,
                          "cloud_types": cloud_types}
                r = session.get(f"{url}/api/client/poll", params=params, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    # ── 处理取消指令 ──
                    cancels = data.get("cancels", [])
                    if not cancels and isinstance(data.get("task"), dict):
                        # 兼容旧格式：服务器返回单个 cancel task
                        t = data.get("task")
                        if t.get("type") == "cancel":
                            cancels = t.get("chase_ids", [t.get("chase_id", "")])
                    for chase_id in cancels:
                        if chase_id:
                            # 取消指令60秒内服务端会重复下发（防丢），仅在首次收到时打日志
                            with self._cancel_events_lock:
                                _first = (chase_id not in self._cancel_events
                                          or not self._cancel_events[chase_id].is_set())
                            if _first:
                                self._log(f"收到取消指令: {chase_id}")
                            self._signal_cancel(chase_id)
                    # ── 处理恢复指令（重新运行追更时服务端下发，清除本地取消状态） ──
                    for chase_id in data.get("resumes", []):
                        if chase_id:
                            with self._cancel_events_lock:
                                self._cancel_events.pop(chase_id, None)
                                self._cancel_ts.pop(chase_id, None)
                            self._log(f"已恢复下载: {chase_id}")
                    # ── 处理任务列表 ──
                    tasks = data.get("tasks", [])
                    if not tasks and data.get("task") and isinstance(data["task"], dict):
                        t = data["task"]
                        if t.get("type") != "cancel":
                            tasks = [t]  # 兼容旧单任务格式
                    for task in tasks:
                        self._log(f"收到任务: {task.get('name','?')} [{task.get('type','?')}]")
                        self._launch_task_thread(session, url, task)
                else:
                    self._log(f"服务器返回 {r.status_code}")
                    self._srv_status(f"错误({r.status_code})重试中", '#FFB900')
                    self._srv_stop.wait(10)
            except ProxyError:
                self._log(f"代理连接失败，切换到直连模式，30秒后重试...")
                self._srv_status("代理错误(直连重试)", '#FFB900')
                # 重建 session，绕过系统代理
                session.close()
                session = requests.Session()
                session.trust_env = False
                if not self._srv_login(session, url):
                    self._log("服务器重新登录失败")
                    self._srv_status("登录失败", self.错误色)
                    self.root.after(0, lambda: self.srv_btn.config(text="连接服务器"))
                    self._srv_session = None; self._srv_url = ""
                    return
                self._srv_session = session; self._srv_url = url
                self._srv_stop.wait(30)
            except ConnectionError:
                self._log(f"连接失败，15秒后重试...")
                self._srv_status("连接失败(重试中)", '#FFB900')
                self._srv_stop.wait(15)
                # 连接恢复后更新状态
                if not self._srv_stop.is_set():
                    self._srv_status(f"轮询中({max_conc}并发)", self.成功色)
            except Exception as e:
                self._log(f"轮询出错: {e}")
                self._srv_stop.wait(5)
            else:
                self._srv_stop.wait(5)  # 成功轮询后也等待
        self._log("轮询已停止")
        self._srv_status("已断开", '#848D97')
        self._srv_session = None; self._srv_url = ""
        self.root.after(0, lambda: self.srv_btn.config(text="连接服务器"))

    def _signal_cancel(self, chase_id):
        """通知对应追更任务取消（幂等，可重复调用）。取消窗口自首次收到起 60 秒，
        服务端窗口内重复下发不会延长窗口（否则停止后重新运行会被持续误取消）。"""
        with self._cancel_events_lock:
            if chase_id not in self._cancel_events:
                self._cancel_events[chase_id] = threading.Event()
            self._cancel_events[chase_id].set()
            if chase_id not in self._cancel_ts:
                self._cancel_ts[chase_id] = time.time()

    def _is_chase_cancelled(self, chase_id, cancel_evt):
        """取消指令 60 秒内有效（与服务端清理窗口一致），过期自动恢复该追更的下载。
        返回 True 表示仍处于取消状态（应跳过本集）。"""
        if not cancel_evt or not cancel_evt.is_set():
            return False
        if chase_id:
            with self._cancel_events_lock:
                ts = self._cancel_ts.get(chase_id, 0)
                if ts and time.time() - ts > 60:
                    cancel_evt.clear()
                    self._cancel_ts.pop(chase_id, None)
        return cancel_evt.is_set()

    def _post_task_status(self, session, url, tid, status, msg="", retries=3):
        """上报任务终态，带重试。终态上报失败会致服务端任务残留 running，
        超时回收后重新下发同一任务（客户端反复收到重复任务）。"""
        body = {"id": tid, "status": status, "msg": msg}
        for i in range(retries):
            try:
                r = session.post(f"{url}/api/client/task_status", json=body, timeout=10)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            if i < retries - 1:
                time.sleep(1 + i * 2)
        return False

    def _report_task_progress(self, tid, stage, dl, total, speed):
        """实时进度上报服务端（★7，3 秒节流）。
        仅服务端派发的任务（tid 在 _running_tids）会上报；手动传输任务（本地 mid）不上报。
        失败静默，不影响下载流程。"""
        srv = getattr(self, "_srv_session", None)
        url = getattr(self, "_srv_url", "")
        if not srv or not url or not tid:
            return
        with self._running_tids_lock:
            if tid not in self._running_tids:
                return
        now_t = time.time()
        if now_t - self._prog_last_ts[0] < 3.0:
            return
        self._prog_last_ts[0] = now_t
        try:
            srv.post(f"{url}/api/client/task_status",
                     json={"id": tid, "status": "running",
                           "stage": str(stage or ""),
                           "progress": int(dl or 0), "total": int(total or 0),
                           "speed": float(speed or 0)},
                     timeout=6)
        except Exception:
            pass

    def _post_chase_done(self, session, url, chase_id, ep_num, ok, error="", file_name="", retries=3):
        """上报追更集完成状态，带重试（chase_done 丢失会导致漏集误判/重复补漏）。
        返回 (success, data)，调用点忽略返回值即可。"""
        body = {"chase_id": chase_id, "ep_num": ep_num, "ok": ok,
                "error": error, "file_name": file_name}
        for i in range(retries):
            try:
                r = session.post(f"{url}/api/client/chase_done", json=body, timeout=10)
                if r.status_code == 200:
                    try:
                        return True, r.json()
                    except Exception:
                        return True, {}
            except Exception:
                pass
            if i < retries - 1:
                time.sleep(1 + i * 2)
        return False, None

    def _launch_task_thread(self, session, url, task):
        """在线程池中启动一个任务"""
        tid = task.get("id", "")
        ttype = task.get("type", "")
        chase_id = task.get("chase_id", "")
        # ── 去重：已在线程中运行的任务忽略重复下发 ──
        with self._running_tids_lock:
            if tid and tid in self._running_tids:
                return
        # ── 检查槽位 ──
        acquired = self._task_semaphore.acquire(blocking=False)
        if not acquired:
            self._log(f"无空闲槽位，跳过: {task.get('name','?')}")
            self._post_task_status(session, url, tid, "skipped", "槽位已满")
            return
        # ── 标记为运行中 ──
        if tid:
            with self._running_tids_lock:
                self._running_tids.add(tid)
        with self._task_active_lock:
            self._task_active += 1
        def _worker():
            try:
                if ttype == "chase-check":
                    self._exec_chase_check(session, url, task)
                elif ttype == "chase-download":
                    self._exec_chase_download(session, url, task)
                else:
                    self._exec_normal_task(session, url, task)
            finally:
                self._task_semaphore.release()
                with self._task_active_lock:
                    self._task_active -= 1
                # 清理运行中标记
                if tid:
                    with self._running_tids_lock:
                        self._running_tids.discard(tid)
                # 清理取消事件：仅清理普通任务（task- 前缀）的临时事件；
                # 追更任务(chase_id)使用共享事件，不能每集清除，否则下一集
                # 会持有新的未set事件，导致服务端取消信号失效。
                if tid:
                    with self._cancel_events_lock:
                        self._cancel_events.pop(tid, None)
        threading.Thread(target=_worker, daemon=True).start()

    def _make_engine(self, share_url, wps_parent_id=None, cloud_id=None):
        """按分享链接自动选择下载源（139 / 夸克），构造传输引擎。上传侧不变。"""
        from transfer_engine import build_src_client, TransferEngine
        cfg = load_config()
        srv = self._server_creds or {}
        # 服务端下发的凭据优先，本地手动配置兜底（未下发或旧版服务端时）
        clouds = list(srv.get("clouds") or []) or cfg.get("clouds", []) or list(self._cfg_clouds)
        if not clouds and cfg.get("yidong_token"):
            # 兼容旧配置：没配 clouds 时，用原 yidong_token 兜底
            clouds = [{"id": "yidong_main", "type": "yidong", "name": "139主账号",
                       "is_primary": True, "token": cfg["yidong_token"]}]
        else:
            # clouds 非空（如只配了夸克）但顶层仍有 yidong_token 时，
            # 也要把顶层 139 token 作为 yidong 连接补进去，否则 build_src_client 找不到 yidong 类型会报缺配
            if cfg.get("yidong_token") and not any(c.get("type") == "yidong" for c in clouds):
                clouds = list(clouds) + [{"id": "yidong_main", "type": "yidong",
                                           "name": "139主账号", "is_primary": True,
                                           "token": cfg["yidong_token"]}]
        src = build_src_client(clouds, share_url, cloud_id)
        # 上传慢速重试配置：upload_slow_min_mb(MB/s) + upload_slow_seconds(秒)，同时>0才启用
        slow_retry = None
        try:
            _mb = float(cfg.get("upload_slow_min_mb", 0) or 0)
            _sec = float(cfg.get("upload_slow_seconds", 0) or 0)
            if _mb > 0 and _sec > 0:
                slow_retry = {"min_mb": _mb, "seconds": _sec}
        except (TypeError, ValueError):
            slow_retry = None
        # WPS cookie：服务端下发优先，本地兜底
        wps_cookie = (srv.get("wps_cookie") or "").strip() or cfg.get("wps_cookie", "")
        yidong_token = cfg.get("yidong_token", "")
        # 服务端下发了 139 凭据且本地未配置时，兜底参数也用它
        if not yidong_token and srv.get("clouds"):
            for _c in srv["clouds"]:
                if _c.get("type") == "yidong" and _c.get("token"):
                    yidong_token = _c["token"]
                    break
        return TransferEngine(
            yidong_token, wps_cookie,
            cfg.get("save_dir", ""),
            wps_parent_id=wps_parent_id or cfg.get("wps_parent_id", "0") or "0",
            wps_group_id=self._wps_group_id,
            src_client=src,
            upload_slow_retry=slow_retry)

    def _dl_creds_ok(self):
        """下载前置凭据检查：本地配置优先，服务端下发凭据兜底（与 _make_engine 一致）。
        返回 (yidong_token, wps_cookie, save_dir, ok)。"""
        cfg = load_config()
        t = (cfg.get("yidong_token") or "").strip()
        c = (cfg.get("wps_cookie") or "").strip()
        d = (cfg.get("save_dir") or "").strip()
        srv = self._server_creds or {}
        clouds = list(cfg.get("clouds") or []) + list(srv.get("clouds") or [])
        if not t and srv.get("clouds"):
            for _c in srv["clouds"]:
                if _c.get("type") == "yidong" and _c.get("token"):
                    t = _c["token"]
                    break
        if not c:
            c = (srv.get("wps_cookie") or "").strip()
        ok = (bool(t) or any(x.get("type") == "yidong" for x in clouds)) and bool(c) and bool(d)
        return t, c, d, ok

    def _exec_normal_task(self, session, url, task):
        """原有的普通传输任务（在线程中执行）"""
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        rename_to = task.get("rename_to", "") or task.get("name", "")
        file_name = task.get("file_name", "")
        category = task.get("category", "电影")
        show_folder = task.get("show_folder", "")
        wps_path = task.get("wps_path", "")
        if not wps_path and show_folder:
            wps_path = f"{category}/{show_folder}"
        elif not wps_path:
            wps_path = category
        tid = task.get("id", "")
        chase_id = task.get("chase_id", "")
        display_name = rename_to or file_name or "task"
        # 创建进度回调绑定 task_id
        tcb = lambda stage, dl, total, speed: self._on_task_progress(
            tid, display_name, stage, dl, total, speed)
        # 为该任务创建取消事件
        cancel_evt = threading.Event()
        if chase_id:
            with self._cancel_events_lock:
                if chase_id in self._cancel_events:
                    cancel_evt = self._cancel_events[chase_id]
                else:
                    self._cancel_events[chase_id] = cancel_evt

        try:
            _dl_ok = self._dl_creds_ok()[3]
            if task.get("skip"):
                self._log(f"已跳过(预览中标记为不下载): {display_name}")
                self._post_task_status(session, url, tid, "skipped", "预览中标记跳过")
                self._post_chase_done(session, url, chase_id, task.get("ep_num", 0), True, "skipped")
                return
            if not _dl_ok:
                self._post_task_status(session, url, tid, "error", "缺少配置")
                tcb("error", 0, 0, "缺少配置"); return
            # 若本集启动前已被取消，直接跳过（停止后不继续下载下一集）
            # 取消指令60秒后自动恢复，避免"一键停止"后该追更永远无法再下载
            if self._is_chase_cancelled(chase_id, cancel_evt):
                self._log(f"本集已取消: {display_name}")
                # 取消确认仅上报一次，不重试（服务端收到即知，无需防丢重试）
                self._post_chase_done(session, url, chase_id, task.get("ep_num", 0), False, "已取消", retries=1)
                self._post_task_status(session, url, tid, "skipped", "已取消", retries=1)
                return
            engine = self._make_engine(
                task.get("share_url", ""),
                wps_parent_id=self.parent_var.get().strip() or "0")
            # ── 下载前自检：WPS 目标目录是否已存在该集文件 ──
            # 上次上传成功但终态上报丢失（task_status 重试失败）时，服务端会把任务
            # 超时回收重新派发；这里发现目标文件已在 WPS 则直接跳过下载并上报完成，
            # 从源头杜绝同一客户端重复下载已存在的集（如斩神 S02E11 反复下载）。
            # 指定了集数(episodes_filter)的任务视为用户手动强制下载，跳过本自检。
            if rename_to and rename_to.strip() and not (task.get("episodes_filter") or "").strip():
                try:
                    _tgt = (engine._wps_resolve_path(wps_path)
                            if wps_path and wps_path.strip() else engine.wps_parent_id)
                    if _tgt:
                        _rt = rename_to.strip()
                        _found = False
                        for _f in engine._wps_list_files(_tgt):
                            _fn = (_f.get("fname") or _f.get("name") or "").strip()
                            if _fn == _rt or _fn.startswith(_rt + "."):
                                _found = True
                                break
                        if _found:
                            self._log(f"目标文件已在WPS中，跳过下载: {_rt}")
                            self._post_chase_done(session, url, chase_id, ep_num, True, "目标文件已存在")
                            self._post_task_status(session, url, tid, "done", "目标文件已存在")
                            tcb("done", 100, 100, "目标文件已存在，跳过")
                            return
                except Exception as _e:
                    self._log(f"下载前WPS探测失败(不影响下载): {_e}")
            ok, err = engine.transfer(share_url, share_passwd=share_pwd,
                                     progress_cb=tcb,
                                     cancel_check=lambda: cancel_evt.is_set(),
                                     rename_to=rename_to, wps_path=wps_path,
                                     delete_after_upload=True,
                                     file_name=file_name)
            self._post_task_status(session, url, tid, "done" if ok else "error", err or "完成")
            if not ok and err:
                tcb("error", 0, 0, err)
            else:
                tcb("done", 100, 100, "")
        except Exception as e:
            self._post_task_status(session, url, tid, "error", str(e))
            tcb("error", 0, 0, str(e))

    def _post_chase_result(self, session, url, chase_id, payload):
        """上报追更检查结果；成功返回 (True, "")，失败返回 (False, 原因)。
        上报失败会让服务端收不到新集列表、不会创建下载任务，因此绝不静默吞掉。
        """
        try:
            resp = session.post(f"{url}/api/client/chase_result",
                                json={"chase_id": chase_id, **payload}, timeout=10)
            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, str(e)

    def _exec_chase_check(self, session, url, task):
        """追更检查：拉取分享文件列表，提取集数，上报新剧集（在线程中执行）"""
        chase_id = task.get("chase_id", "")
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        base_ep = task.get("base_ep", 0)
        ep_pattern = task.get("ep_pattern", "")
        rename_template = task.get("rename_template", "")
        tid = task.get("id", "")
        name = task.get("name", "追更检查")
        sub_path = task.get("sub_path", "").strip().rstrip("/") + "/" if task.get("sub_path", "").strip() else ""

        # 新一轮检查开始前，重置该追更的取消事件（清掉上一轮可能残留的 set）
        if chase_id:
            with self._cancel_events_lock:
                self._cancel_events.pop(chase_id, None)

        self._log(f"追更检查: {name} (当前第{base_ep}集) [regex={ep_pattern or '自动识别'}]" + (f", 子目录: {sub_path}" if sub_path else ""))
        final_status = "done"
        final_msg = "追更检查完成"

        try:
            t = self.token_var.get().strip()
            c = self.cookie_var.get().strip()
            d = self.dir_var.get().strip() or "."
            try:
                engine = self._make_engine(task.get("share_url", ""))
            except Exception as ee:
                hint = ""
                if "quark" in str(ee) or "夸克" in str(ee):
                    hint = "（这不是推送问题，是本客户端未配置夸克网盘：请在客户端「夸克网盘」卡片填写夸克 Cookie 并保存）"
                self._log(f"追更检查异常: {ee} {hint}".strip())
                ok_r, msg_r = self._post_chase_result(
                    session, url, chase_id, {"episodes": [], "error": str(ee)})
                if not ok_r:
                    self._log(f"追更检查异常结果上报失败: {msg_r}")
                final_status = "error"
                final_msg = str(ee)
                return

            # 获取分享文件列表
            files, err = engine.get_share_files(share_url, share_pwd)
            if err:
                self._log(f"追更检查失败: {err}")
                ok_r, msg_r = self._post_chase_result(
                    session, url, chase_id, {"episodes": [], "error": err})
                if not ok_r:
                    self._log(f"追更检查失败结果上报失败: {msg_r}")
                final_status = "error"
                final_msg = err
                return

            # 按 sub_path 过滤文件
            if sub_path:
                filtered = [f for f in files if (f.get("path", "") + "/").startswith(sub_path)
                            or (f.get("name", "") + "/").startswith(sub_path)]
                self._log(f"子目录过滤: {len(files)} → {len(filtered)} 个文件 (路径以 '{sub_path}' 开头)")
                files = filtered

            category = task.get("category", "")
            is_movie = "电影" in category

            # 提取所有集数，同一集号保留最大的文件
            all_eps = {}
            _ep_sizes = {}
            for f in files:
                fname = f.get("name", "")
                ep_num, _ = extract_episode(fname, ep_pattern)
                if ep_num is None:
                    continue
                key = str(ep_num)
                if key not in all_eps or f.get("size", 0) > _ep_sizes.get(key, 0):
                    all_eps[key] = fname
                    _ep_sizes[key] = f.get("size", 0)

            if is_movie:
                # 电影模式：分享里的每个视频文件当作一部作品整体处理，不依赖集数正则
                movie_files = []
                VIDEO_EXT = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv',
                             '.webm', '.m4v', '.mpg', '.mpeg', '.ts', '.rmvb')
                idx = 0
                for f in files:
                    fname = f.get("name", "")
                    if fname.lower().endswith(VIDEO_EXT):
                        # 用负数序列避免与剧集 ep_num 冲突（如 -1,-2...）
                        neg = -(idx + 1)
                        all_eps[str(neg)] = fname
                        movie_files.append(fname)
                        idx += 1
                new = list(range(-1, -(idx + 1), -1))  # [-1,-2,...]
                self._log(f"追更检查(电影): 共{len(files)}个文件, 发现{len(movie_files)}个电影文件: {movie_files[:10]}{'...' if len(movie_files)>10 else ''}")
                # 上报结果（含 movie_files 供服务端按文件名去重）
                ok_r, msg_r = self._post_chase_result(
                    session, url, chase_id,
                    {"episodes": new, "siblings": all_eps,
                     "movie_files": movie_files, "is_movie": True})
                if not ok_r:
                    self._log(f"追更检查结果上报失败: {msg_r}")
                    final_status = "error"
                    final_msg = f"结果上报失败: {msg_r}"
            else:
                # 剧集模式：按集数匹配
                new = [ep_num for ep_num, _ in find_new_episodes(files, base_ep, ep_pattern)]
                self._log(f"追更检查完成: 共{len(files)}个文件, 发现{len(new)}个新集数: {new[:10]}{'...' if len(new)>10 else ''}")
                # 探测 WPS 目标目录：列出重命名后的文件清单随结果上报，
                # 服务端据此反查"WPS 里实际已存在的集"，漏集判定不再只依赖 chase_done 上报
                wps_files = []
                try:
                    wps_path = task.get("wps_path", "").strip()
                    if wps_path:
                        folder_id = engine._wps_resolve_path(wps_path)
                        if folder_id:
                            for f in engine._wps_list_files(folder_id):
                                if f.get("ftype") == "folder":
                                    continue
                                fn = (f.get("fname") or f.get("name") or "").strip()
                                if fn:
                                    wps_files.append(fn)
                            self._log(f"WPS目录探测: {wps_path} 共{len(wps_files)}个文件")
                except Exception as e:
                    self._log(f"WPS目录探测失败(跳过,不影响检查): {e}")
                # 上报结果（失败不能静默丢弃：服务端收不到集数列表就不会创建下载任务）
                ok_r, msg_r = self._post_chase_result(
                    session, url, chase_id, {"episodes": new, "siblings": all_eps,
                                             "wps_files": wps_files})
                if not ok_r:
                    self._log(f"追更检查结果上报失败: {msg_r}")
                    final_status = "error"
                    final_msg = f"结果上报失败: {msg_r}"
        except Exception as e:
            self._log(f"追更检查异常: {e}")
            ok_r, msg_r = self._post_chase_result(
                session, url, chase_id, {"episodes": [], "error": str(e)})
            if not ok_r:
                self._log(f"追更检查异常结果上报失败: {msg_r}")
        finally:
            self._post_task_status(session, url, tid, final_status, final_msg)

    def _exec_chase_download(self, session, url, task):
        """追更下载：下载+上传某一集，完成后上报追更进度（在线程中执行）"""
        # ── 批量任务：多集流水线（上传本集时并行下载下一集），每集单独上报 ──
        if task.get("batch") and task.get("files"):
            self._exec_chase_batch(session, url, task)
            return
        chase_id = task.get("chase_id", "")
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        file_name = task.get("file_name", "")
        rename_to = task.get("rename_to", "")
        wps_path = task.get("wps_path", "")
        ep_num = task.get("ep_num", 0)
        tid = task.get("id", "")
        display_name = rename_to or file_name or "task"
        # 创建进度回调绑定 task_id
        tcb = lambda stage, dl, total, speed: self._on_task_progress(
            tid, display_name, stage, dl, total, speed)
        # 为该追更任务创建取消事件
        cancel_evt = threading.Event()
        if chase_id:
            with self._cancel_events_lock:
                if chase_id in self._cancel_events:
                    cancel_evt = self._cancel_events[chase_id]
                else:
                    self._cancel_events[chase_id] = cancel_evt

        try:
            _dl_ok = self._dl_creds_ok()[3]
            if task.get("skip"):
                self._log(f"已跳过(预览中标记为不下载): {display_name}")
                self._post_task_status(session, url, tid, "skipped", "预览中标记跳过")
                self._post_chase_done(session, url, chase_id, ep_num, True, "skipped")
                return
            if not _dl_ok:
                self._post_task_status(session, url, tid, "error", "缺少配置")
                tcb("error", 0, 0, "缺少配置"); return
            # 若本集启动前已被取消，直接跳过（停止后不继续下载下一集）
            # 取消指令60秒后自动恢复，避免"一键停止"后该追更永远无法再下载
            if self._is_chase_cancelled(chase_id, cancel_evt):
                self._log(f"本集已取消: {display_name}")
                # 取消确认仅上报一次，不重试（服务端收到即知，无需防丢重试）
                self._post_chase_done(session, url, chase_id, task.get("ep_num", 0), False, "已取消", retries=1)
                self._post_task_status(session, url, tid, "skipped", "已取消", retries=1)
                return
            engine = self._make_engine(
                task.get("share_url", ""),
                wps_parent_id=self.parent_var.get().strip() or "0")
            # ── 下载前自检：WPS 目标目录是否已存在该集文件 ──
            # 上次上传成功但终态上报丢失（task_status 重试失败）时，服务端会把任务
            # 超时回收重新派发；这里发现目标文件已在 WPS 则直接跳过下载并上报完成，
            # 从源头杜绝同一客户端重复下载已存在的集（如斩神 S02E11 反复下载）。
            # 指定了集数(episodes_filter)的任务视为用户手动强制下载，跳过本自检。
            if rename_to and rename_to.strip() and not (task.get("episodes_filter") or "").strip():
                try:
                    _tgt = (engine._wps_resolve_path(wps_path)
                            if wps_path and wps_path.strip() else engine.wps_parent_id)
                    if _tgt:
                        _rt = rename_to.strip()
                        _found = False
                        for _f in engine._wps_list_files(_tgt):
                            _fn = (_f.get("fname") or _f.get("name") or "").strip()
                            if _fn == _rt or _fn.startswith(_rt + "."):
                                _found = True
                                break
                        if _found:
                            self._log(f"目标文件已在WPS中，跳过下载: {_rt}")
                            self._post_chase_done(session, url, chase_id, ep_num, True, "目标文件已存在")
                            self._post_task_status(session, url, tid, "done", "目标文件已存在")
                            tcb("done", 100, 100, "目标文件已存在，跳过")
                            return
                except Exception as _e:
                    self._log(f"下载前WPS探测失败(不影响下载): {_e}")
            ok, err = engine.transfer(share_url, share_passwd=share_pwd,
                                     progress_cb=tcb,
                                     cancel_check=lambda: cancel_evt.is_set(),
                                     rename_to=rename_to, wps_path=wps_path,
                                     delete_after_upload=True,
                                     file_name=file_name)
            # 上报追更进度（带重试，防止终态丢失导致服务端重复下发/漏集误判）
            _cancel_err = bool(err and ("取消" in err))
            _retries = 1 if _cancel_err else 3  # 主动取消确认无需重试（用户要求）
            _okr, _data = self._post_chase_done(session, url, chase_id, ep_num, ok, err or "", file_name, retries=_retries)
            if _okr and _data:
                if ok:
                    current_ep = _data.get("base_ep", ep_num)
                    self._log(f"追更进度已上报: {rename_to or file_name} 完成 (已追到第{current_ep}集)")
                else:
                    self._log(f"追更失败: {rename_to or file_name} 未成功 (集数标记 ep={ep_num})，错误: {err or _data.get('message','')}")
            self._post_task_status(session, url, tid, "done" if ok else "error", err or "完成", retries=_retries)
            if not ok and err:
                tcb("error", 0, 0, err)
            else:
                tcb("done", 100, 100, "")
        except Exception as e:
            self._post_chase_done(session, url, chase_id, ep_num, False, str(e), file_name)
            self._post_task_status(session, url, tid, "error", str(e))
            tcb("error", 0, 0, str(e))

    def _exec_chase_batch(self, session, url, task):
        """追更批量下载：同路径多集打包为一次传输，流水线执行。
        下载 1 → 上传 1 的同时并行下载 2 → 1 上传完上传 2，同时下载 3……
        每集完成后单独上报追更进度（chase_done），服务端据此更新卡片进度。
        """
        chase_id = task.get("chase_id", "")
        share_url = task.get("share_url", "")
        share_pwd = task.get("share_passwd", "")
        tid = task.get("id", "")
        files = task.get("files", []) or []
        display_name = task.get("name") or ("%s 等%d集" % (task.get("rename_to", ""), len(files)))
        # 创建进度回调绑定 task_id（主进度显示当前正在传输的集）
        tcb = lambda stage, dl, total, speed: self._on_task_progress(
            tid, display_name, stage, dl, total, speed)
        # 为该追更任务创建取消事件
        cancel_evt = threading.Event()
        if chase_id:
            with self._cancel_events_lock:
                if chase_id in self._cancel_events:
                    cancel_evt = self._cancel_events[chase_id]
                else:
                    self._cancel_events[chase_id] = cancel_evt
        try:
            _dl_ok = self._dl_creds_ok()[3]
            if not _dl_ok:
                self._post_task_status(session, url, tid, "error", "缺少配置")
                tcb("error", 0, 0, "缺少配置"); return
            if self._is_chase_cancelled(chase_id, cancel_evt):
                self._log(f"批量下载已取消: {display_name}")
                self._post_task_status(session, url, tid, "skipped", "已取消")
                return
            engine = self._make_engine(share_url, wps_parent_id=self.parent_var.get().strip() or "0")
            # ── 逐集自检：标记跳过的 / WPS 已存在的集直接上报完成，不进入传输 ──
            file_names = []
            rename_map = {}
            todo = []
            for fi in files:
                fname = fi.get("file_name", "")
                rname = fi.get("rename_to", "")
                if fi.get("skip"):
                    self._log(f"已跳过(预览中标记为不下载): {rname or fname}")
                    self._post_chase_done(session, url, chase_id, fi.get("ep_num", 0), True, "skipped")
                    continue
                # 与单集任务一致：上次终态上报丢失时，目标文件已在 WPS 则直接跳过
                if rname and rname.strip() and not (task.get("episodes_filter") or "").strip():
                    try:
                        _tgt = (engine._wps_resolve_path(fi.get("wps_path", ""))
                                if (fi.get("wps_path", "") or "").strip() else engine.wps_parent_id)
                        _rt = rname.strip()
                        _found = False
                        if _tgt:
                            for _f in engine._wps_list_files(_tgt):
                                _fn = (_f.get("fname") or _f.get("name") or "").strip()
                                if _fn == _rt or _fn.startswith(_rt + "."):
                                    _found = True
                                    break
                        if _found:
                            self._log(f"目标文件已在WPS中，跳过下载: {_rt}")
                            self._post_chase_done(session, url, chase_id, fi.get("ep_num", 0), True, "目标文件已存在")
                            continue
                    except Exception as _e:
                        self._log(f"下载前WPS探测失败(不影响下载): {_e}")
                todo.append(fi)
                file_names.append(fname)
                rename_map[fname] = rname
            if not todo:
                self._post_task_status(session, url, tid, "done", "全部跳过")
                tcb("done", 100, 100, "全部跳过")
                return
            wps_path = todo[0].get("wps_path", "") or task.get("wps_path", "")
            state = {"done": 0, "failed": 0, "n": len(todo)}

            def _per_file_done(idx, name, ok, err):
                fi = todo[idx] if 0 <= idx < len(todo) else None
                _ep = fi.get("ep_num", 0) if fi else 0
                if ok:
                    state["done"] += 1
                else:
                    state["failed"] += 1
                _okr, _data = self._post_chase_done(session, url, chase_id, _ep, ok, err or "", name)
                if _okr and _data:
                    if ok:
                        self._log("追更进度已上报: %s 完成 (已追到第%d集) [%d/%d]"
                                  % (name, _data.get("base_ep", _ep), state["done"], state["n"]))
                    else:
                        self._log("追更失败: %s 未成功 (集数标记 ep=%d)，错误: %s"
                                  % (name, _ep, err or _data.get("message", "")))
                else:
                    self._log("追更进度上报失败: %s (%s)" % (name, _data or ""))

            ok_all, err_all = engine.transfer_batch(
                share_url, share_passwd=share_pwd, file_names=file_names,
                progress_cb=tcb, cancel_check=lambda: cancel_evt.is_set(),
                rename_map=rename_map, wps_path=wps_path,
                delete_after_upload=True, prefetch=1, per_file_done_cb=_per_file_done)
            if cancel_evt.is_set():
                self._post_task_status(session, url, tid, "skipped", "已取消")
            else:
                self._post_task_status(session, url, tid, "done" if ok_all else "error", err_all or "完成")
                if ok_all:
                    tcb("done", 100, 100, "")
                else:
                    tcb("error", 0, 0, err_all or "")
        except Exception as e:
            self._post_task_status(session, url, tid, "error", str(e))
            tcb("error", 0, 0, str(e))

    # ════════════════════════════════════
    # 追更页
    # ════════════════════════════════════
    def _build_chase(self, cfg):
        page = self._pages["chase"]
        self._chase_tree = ttk.Treeview(page, columns=("name","ep","result"), show="headings",
                                         style='Card.Treeview', height=10)
        self._chase_tree.heading("name", text="片名")
        self._chase_tree.heading("ep", text="追到集数")
        self._chase_tree.heading("result", text="上次检查")
        self._chase_tree.column("name", minwidth=120, width=200, stretch=True)
        self._chase_tree.column("ep", minwidth=60, width=80, anchor="center", stretch=True)
        self._chase_tree.column("result", minwidth=200, width=360, stretch=True)
        self._chase_tree.pack(fill=tk.BOTH, expand=True)

        bb = ttk.Frame(page)
        bb.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bb, text="刷新状态", command=self._chase_refresh, width=12).pack(side=tk.LEFT)
        ttk.Label(bb, text="从服务器同步追更状态", style='Sub.TLabel',
                  foreground=self.次文本).pack(side=tk.LEFT, padx=(10, 0))

    def _chase_refresh(self):
        """从服务器同步追更状态（后台线程，不阻塞UI）"""
        self._chase_tree.delete(*self._chase_tree.get_children())
        self._chase_tree.insert("", "end", values=("正在刷新...", "", ""))
        threading.Thread(target=self._chase_refresh_thread, daemon=True).start()

    def _chase_refresh_thread(self):
        url = self.srv_url_var.get().strip().rstrip("/")
        if not url:
            self._chase_tree_insert([("未配置服务器", "", "")])
            return
        user = self.srv_user_var.get().strip()
        pw = self.srv_pass_var.get().strip()
        try:
            session = requests.Session()
            if user and pw:
                session.post(f"{url}/api/login", json={"username": user, "password": pw}, timeout=10)
            r = session.get(f"{url}/api/admin/chase", timeout=10)
            if r.status_code != 200:
                self._chase_tree_insert([("请先登录服务器", "", "")])
                return
            tasks = r.json().get("tasks", [])
            if not tasks:
                self._chase_tree_insert([("暂无追更任务", "", "")])
                return
            rows = []
            for t in tasks:
                name = t.get("name", "")
                ep = str(t.get("base_ep", 0))
                result = t.get("last_result", "未检查")
                enabled = "✓" if t.get("enabled") else "✗ 已停用"
                rows.append((f"{enabled} {name}", f"第{ep}集", result))
            self._chase_tree_insert(rows)
        except Exception as e:
            self._chase_tree_insert([("连接失败", str(e), "")])

    def _chase_tree_insert(self, rows):
        """主线程更新 Treeview"""
        def _update():
            self._chase_tree.delete(*self._chase_tree.get_children())
            for row in rows:
                self._chase_tree.insert("", "end", values=row)
        self.root.after(0, _update)

    # ════════════════════════════════════
    # 任务面板
    # ════════════════════════════════════
    def _build_tasks(self):
        page = self._pages["tasks"]
        # 滚动容器
        canvas = tk.Canvas(page, bg=self.背景色, highlightthickness=0)
        scrollbar = ttk.Scrollbar(page, orient='vertical', command=canvas.yview)
        self._task_container = ttk.Frame(canvas, style='TaskBg.TFrame')
        self._task_container.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._task_container, anchor='nw',
                            tags="task_inner")
        # Canvas 宽度跟随页面变化
        def _on_canvas_configure(event):
            canvas.itemconfig("task_inner", width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)
        # 鼠标滚轮
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _on_task_progress(self, tid, name, stage, dl, total, speed):
        """每个任务独立的进度回调（在线程中调用，主线程更新 UI）"""
        self._report_task_progress(tid, stage, dl, total, speed)
        def _u():
            w = self._ensure_task_widget(tid, name)
            names = {"list":"获取列表","download":"下载中","extract":"解压中","rename":"处理中",
                    "upload":"上传中","done":"完成","error":"失败"}
            w['stage_var'].set(names.get(stage, stage))
            if total > 0 and dl >= 0:
                pct = min(100, int(dl / total * 100 + 0.5))
                w['pb']['value'] = pct
                w['detail_var'].set(f"{fmt_size(dl)} / {fmt_size(total)}  ·  {pct}%")
            elif dl > 0:
                w['detail_var'].set(fmt_size(dl))
            if speed:
                w['speed_var'].set(speed)
            # 记录最后有数据变化的时间（用于停滞检测）
            self._task_last_dl[tid] = (time.time(), dl)
            if stage == "done":
                w['pb']['value'] = 100
                w['stage_var'].set("完成")
                w['speed_var'].set("")
                self.root.after(8000, lambda t=tid: self._remove_task_widget(t))
            elif stage == "error":
                w['pb']['value'] = 0
                w['stage_var'].set("失败")
                if speed:
                    w['speed_var'].set(str(speed))
                self.root.after(8000, lambda t=tid: self._remove_task_widget(t))
        self.root.after(0, _u)

    def _ensure_task_widget(self, tid, name):
        """获取或创建任务卡片，返回 widget dict"""
        if tid in self._task_widgets:
            return self._task_widgets[tid]
        # 卡片容器
        card = ttk.Frame(self._task_container, style='Card.TFrame')
        card.pack(fill=tk.X, padx=4, pady=(0, 6))
        # 名称栏
        hdr = ttk.Frame(card, style='Card.TFrame')
        hdr.pack(fill=tk.X, padx=10, pady=(8, 2))
        ttk.Label(hdr, text=name[:50], style='Card.TLabel',
                 font=('微软雅黑', 10, 'bold')).pack(side=tk.LEFT)
        # 进度条
        pb = ttk.Progressbar(card, style='Mod.Horizontal.TProgressbar', mode='determinate')
        pb.pack(fill=tk.X, padx=10, pady=(4, 2))
        # 信息栏
        info = ttk.Frame(card, style='Card.TFrame')
        info.pack(fill=tk.X, padx=10, pady=(0, 8))
        stage_var = tk.StringVar(value="就绪")
        detail_var = tk.StringVar(value="")
        speed_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=stage_var, style='Sub.TLabel').pack(side=tk.LEFT)
        ttk.Label(info, textvariable=detail_var, style='Sub.TLabel').pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(info, textvariable=speed_var, style='Card.TLabel',
                 foreground=self.主色, font=('微软雅黑', 9, 'bold')).pack(side=tk.RIGHT)
        w = {'card': card, 'pb': pb, 'stage_var': stage_var,
             'detail_var': detail_var, 'speed_var': speed_var}
        self._task_widgets[tid] = w
        # 自动跳转到任务页
        if self._cur != "tasks":
            self._show("tasks")
        return w

    def _remove_task_widget(self, tid):
        """移除任务卡片"""
        if tid not in self._task_widgets:
            return
        w = self._task_widgets.pop(tid)
        w['card'].destroy()
        self._task_last_dl.pop(tid, None)  # 清理卡死检测记录

    def _check_stale_progress(self):
        """每5秒检查一次，如果任务超过15秒无进度则标记为卡住"""
        STALE_SECONDS = 30
        now = time.time()
        stale_tids = []
        for tid, (ts, dl_val) in list(self._task_last_dl.items()):
            w = self._task_widgets.get(tid)
            if not w:
                continue
            stage = w['stage_var'].get()
            if stage in ("完成", "失败"):
                continue
            if now - ts > STALE_SECONDS:
                w['speed_var'].set("卡住?")
                stale_tids.append(tid)
        # 把已恢复的任务速度清掉（不在卡住列表里的任务可能有旧标记）
        # 不清掉，因为正常 callbacks 会覆盖 speed
        self.root.after(5000, self._check_stale_progress)

    # ════════════════════════════════════
    # 日志页
    # ════════════════════════════════════
    def _build_log(self):
        page = self._pages["log"]
        bar = ttk.Frame(page)
        bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(bar, text="清空日志", style='Sec.TButton', command=self._clear_log).pack(side=tk.LEFT)

        self.log_text = scrolledtext.ScrolledText(page, height=20, wrap=tk.WORD,
                                                 font=('Consolas', 9), background='white',
                                                 relief='solid', borderwidth=1)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_config('time', foreground='#0078D7')
        self.log_text.tag_config('info', foreground='#323130')
        self.log_text.tag_config('warn', foreground='#FFB900')
        self.log_text.tag_config('error', foreground='#D13438')
        self.log_text.tag_config('success', foreground='#107C10')
        self.log_text.configure(state='disabled')

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state='disabled')

    def _log(self, msg):
        self.root.after(0, lambda: self._log_ui(msg))

    def _log_ui(self, msg):
        if '失败' in msg or '错误' in msg or 'error' in msg.lower():
            tag = 'error'
        elif '完成' in msg or '成功' in msg or 'done' in msg.lower():
            tag = 'success'
        elif '警告' in msg or '重试' in msg or '跳过' in msg:
            tag = 'warn'
        else:
            tag = 'info'
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, f"{now()}  ", 'time')
        self.log_text.insert(tk.END, f"{msg}\n", tag)
        # 超过2000行就删旧的
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > 2000:
            self.log_text.delete('1.0', f'{lines - 2000}.0')
        self.log_text.see(tk.END)
        self.log_text.configure(state='disabled')

    # ════════════════════════════════════
    # 传输逻辑
    # ════════════════════════════════════
    def _fetch_files(self):
        """获取分享链接中的文件列表"""
        url = self.url_var.get().strip()
        if not url:
            self._log_ui("请输入分享链接")
            return
        pwd = self.pwd_var.get().strip()
        self.fetch_btn.config(state='disabled', text="获取中...")
        def _run():
            try:
                from transfer_engine import TransferEngine
                t = self.token_var.get().strip()
                c = self.cookie_var.get().strip()
                d = self.dir_var.get().strip() or "."
                engine = self._make_engine(url)
                files, err = engine.get_share_files(url, pwd)
                def _cb():
                    self.fetch_btn.config(state='normal', text="获取列表")
                    if err:
                        self._log_ui(f"获取失败: {err}")
                        return
                    self._share_files = files
                    names = [f.get("name", "") for f in files]
                    self.files_cb['values'] = names
                    if names:
                        self.files_cb.current(0)
                        self._on_file_selected()
                        self._log_ui(f"获取到 {len(files)} 个文件")
                    else:
                        self._log_ui("没有文件")
                self.root.after(0, _cb)
            except Exception as e:
                def _err(): self.fetch_btn.config(state='normal', text="获取列表"); self._log_ui(f"获取失败: {e}")
                self.root.after(0, _err)
        threading.Thread(target=_run, daemon=True).start()

    def _filter_files(self, event=None):
        """输入过滤：按相关性排序匹配的文件"""
        self._filtering = True
        kw = self.file_name_var.get().strip()
        if not kw or not self._share_files:
            self.files_cb['values'] = [f.get("name", "") for f in self._share_files]
            self._filtering = False
            return
        # 按匹配度排序：精确 > 开头 > 中间包含 > 其他包含
        exact = []; start = []; middle = []; rest = []
        kl = kw.lower()
        for f in self._share_files:
            n = f.get("name", "")
            nl = n.lower()
            if nl == kl:
                exact.append(n)
            elif nl.startswith(kl):
                start.append(n)
            elif kw in n:
                middle.append(n)
            elif kl in nl:
                rest.append(n)
        # 各组内自然排序
        import re
        def _num_key(s):
            parts = re.split(r'(\d+)', s)
            return [int(p) if p.isdigit() else p.lower() for p in parts]
        for g in [exact, start, middle, rest]:
            g.sort(key=_num_key)
        self.files_cb['values'] = exact + start + middle + rest
        # 延迟重置，避免本次事件链中触发 on_file_selected
        self.root.after(100, lambda: setattr(self, '_filtering', False))

    def _on_file_selected(self, *args):
        """文件选中时，自动提取剧名+集数"""
        if self._filtering:
            return
        name = self.file_name_var.get().strip()
        if name:
            base, _ = os.path.splitext(name)
            import re
            # 尝试匹配剧集模式：S01E01 / E01 / 第01集 / - 01 等
            ep_pattern = r'(S\d{1,3}E\d{1,4}|[Ee]\d{1,4}|第\d{1,4}集|[\[\(]\d{1,4}[\]\)]|\s+-\s+\d{1,4}|\s+\d{2,4}\b)'
            m = re.search(ep_pattern, base)
            if m:
                show = base[:m.start()].strip().rstrip('.-_[]()（） ')
                if show:
                    self.show_folder_var.set(show)
                self.rename_var.set(base)
            else:
                # 无剧集标识，电影：不放子文件夹
                self.show_folder_var.set("")
                self.rename_var.set(base)

    def _start(self):
        t = self.token_var.get().strip()
        c = self.cookie_var.get().strip()
        d = self.dir_var.get().strip()
        u = self.url_var.get().strip()
        if not t: messagebox.showerror("错误", "请先在配置页设置 139 Token"); return
        if not c: messagebox.showerror("错误", "请先在配置页设置 WPS Cookie"); return
        if not d: messagebox.showerror("错误", "请先在配置页选择保存目录"); return
        if not u: messagebox.showerror("错误", "请填分享链接"); return

        rename = self.rename_var.get().strip()
        category = self.category_var.get().strip()
        show_folder = self.show_folder_var.get().strip()
        if show_folder:
            wps_path = f"{category}/{show_folder}"
        else:
            wps_path = category

        self._busy = True
        self._retrying = False
        self.start_btn.config(state='disabled')
        self.cancel_btn.config(state='normal')
        mid = f"manual_{int(time.time())}"
        self._log(f"开始: {u}")
        self._log(f"WPS路径: {wps_path}")
        self._manual_cancel_evt = threading.Event()

        def _run():
            display = rename or os.path.basename(u.replace("?","").split("?")[0]) or d
            tcb = lambda stage, dl, total, speed: self._on_task_progress(
                mid, display, stage, dl, total, speed)
            try:
                engine = self._make_engine(
                    u,
                    wps_parent_id=self.parent_var.get().strip() or "0")
                ok, err = engine.transfer(u, share_passwd=self.pwd_var.get().strip(),
                                         progress_cb=tcb,
                                         cancel_check=lambda: self._manual_cancel_evt.is_set(),
                                         rename_to=rename, wps_path=wps_path,
                                         delete_after_upload=True,
                                         file_name=self.file_name_var.get().strip())
                if not ok and err:
                    tcb("error", 0, 0, err)
                else:
                    tcb("done", 100, 100, "")
            except Exception as e:
                tcb("error", 0, 0, str(e))
            finally:
                self._busy = False
                self.root.after(0, self._done)
        self._transfer_start = time.time()
        threading.Thread(target=_run, daemon=True).start()

    def _start_all(self):
        """全部传输：分享内所有文件走流水线，下载与上传并行（上传1时下载2）"""
        t = self.token_var.get().strip()
        c = self.cookie_var.get().strip()
        d = self.dir_var.get().strip()
        u = self.url_var.get().strip()
        if not t: messagebox.showerror("错误", "请先在配置页设置 139 Token"); return
        if not c: messagebox.showerror("错误", "请先在配置页设置 WPS Cookie"); return
        if not d: messagebox.showerror("错误", "请先在配置页选择保存目录"); return
        if not u: messagebox.showerror("错误", "请填分享链接"); return

        category = self.category_var.get().strip()
        show_folder = self.show_folder_var.get().strip()
        if show_folder:
            wps_path = f"{category}/{show_folder}"
        else:
            wps_path = category

        self._busy = True
        self._retrying = False
        self.start_btn.config(state='disabled')
        self.start_all_btn.config(state='disabled')
        self.cancel_btn.config(state='normal')
        mid = f"manual_{int(time.time())}"
        self._log(f"开始全部传输（流水线: 上传与下载并行）: {u}")
        self._log(f"WPS路径: {wps_path}")
        self._manual_cancel_evt = threading.Event()

        def _run():
            display = f"全部文件 → {wps_path or 'WPS'}"
            tcb = lambda stage, dl, total, speed: self._on_task_progress(
                mid, display, stage, dl, total, speed)
            try:
                engine = self._make_engine(
                    u,
                    wps_parent_id=self.parent_var.get().strip() or "0")
                ok, err = engine.transfer_batch(
                    u, share_passwd=self.pwd_var.get().strip(),
                    progress_cb=tcb,
                    cancel_check=lambda: self._manual_cancel_evt.is_set(),
                    wps_path=wps_path,
                    delete_after_upload=True,
                    prefetch=1)
                if not ok and err:
                    tcb("error", 0, 0, err)
                else:
                    tcb("done", 100, 100, "")
            except Exception as e:
                tcb("error", 0, 0, str(e))
            finally:
                self._busy = False
                self.root.after(0, self._done)
        self._transfer_start = time.time()
        threading.Thread(target=_run, daemon=True).start()

    def _cancel(self):
        """取消所有正在运行的任务"""
        self._log("取消中（所有活跃任务）...")
        # 取消服务端驱动的任务
        with self._cancel_events_lock:
            for evt in self._cancel_events.values():
                evt.set()
            self._cancel_events.clear()
        # 取消手动传输任务
        if hasattr(self, '_manual_cancel_evt'):
            self._manual_cancel_evt.set()
        # 清理任务卡片
        for tid in list(self._task_widgets.keys()):
            self._remove_task_widget(tid)
        # 清理运行中标记
        with self._running_tids_lock:
            self._running_tids.clear()

    def _done(self):
        self.start_btn.config(state='normal')
        self.start_all_btn.config(state='normal')
        self.cancel_btn.config(state='disabled')


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
