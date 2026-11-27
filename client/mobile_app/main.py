"""
WPS 传输工具 · 手机版（Kivy 安卓客户端）
功能：
  - 连接服务端，轮询领取任务（追更检查/追更下载/普通传输）
  - 139 / 夸克分享 → 手机本地 → WPS 上传
  - 云盘连接配置（139 token / 夸克 cookie / WPS cookie）
打包：buildozer android debug（详情见 README）

崩溃日志：本文件顶部注册了全局异常钩子，任何未捕获的 Python 异常都会：
  1) 打印到 stderr（Android 上即 logcat，可用 `adb logcat -s python` 查看）
  2) 写入应用私有目录 crash.log（debug 包可用 `adb shell run-as <包名> cat files/crash.log` 读取）
"""
import os
import sys
import time
import json
import threading

# ── 启动即注册全局崩溃钩子（必须在其他 import 之前，才能捕获 import 阶段异常）──
def _dump_crash(exc_info):
    try:
        tb = ''.join(__import__('traceback').format_exception(*exc_info))
    except Exception:
        tb = repr(exc_info)
    try:
        sys.stderr.write('\n===== UNCAUGHT EXCEPTION =====\n' + tb + '\n')
        sys.stderr.flush()
    except Exception:
        pass
    try:
        base = os.environ.get('ANDROID_PRIVATE') or os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, 'crash.log'), 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n' + tb + '\n')
    except Exception:
        pass


def _excepthook(exc_type, exc_value, exc_tb):
    _dump_crash((exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    _dump_crash((args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _excepthook
if hasattr(threading, 'excepthook'):
    threading.excepthook = _thread_excepthook
# ── 崩溃钩子结束 ──

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.checkbox import CheckBox
from kivy.core.window import Window
from kivy.metrics import dp

# ── 中文字体（Kivy 默认 Roboto 不含中文，Android 上会显示方块乱码）──
# NotoSansSC.ttf 随 APK 打包（fonts/ 目录），注册后设为全局默认字体。
# 注意：必须是 TTF（TrueType 轮廓）！OTF(CFF) 在 Android SDL2_ttf 上加载失败会
#       静默回退 Roboto 导致中文全变方块。本文件由 fontTools 从可变字体实例化而来
#       （OFL 开源协议，约 10MB，含简体中文全量字形）。
def _setup_cn_font():
    """注册中文字体为全局默认；任何失败都打印到 stderr（logcat 可见），不静默。"""
    from kivy.core.text import LabelBase
    from kivy.resources import resource_add_path

    base = os.path.dirname(os.path.abspath(__file__))
    priv = os.environ.get('ANDROID_PRIVATE', '')
    candidates = [
        os.path.join(base, 'fonts', 'NotoSansSC.ttf'),
        os.path.join(priv, 'app', 'fonts', 'NotoSansSC.ttf'),
        os.path.join(priv, 'files', 'app', 'fonts', 'NotoSansSC.ttf'),
    ]
    font_path = next((p for p in candidates if os.path.exists(p)), None)
    if not font_path:
        sys.stderr.write('[FONT] NotoSansSC.ttf not found in: %r\n' % (candidates,))
        return
    try:
        resource_add_path(os.path.dirname(font_path))
        LabelBase.register(name='NotoSansSC', fn_regular=font_path)
        # 通过 KV 规则覆盖默认字体，让 Label（含 Button 等子类）和 TextInput
        # 全部使用中文字体。注意：不能直接给 Label.font_name 类属性赋字符串，
        # 那会破坏 Kivy 的 Property 描述符，导致 AliasProperty.link_deps 崩溃。
        Builder.load_string('''
<Label>:
    font_name: 'NotoSansSC'
<TextInput>:
    font_name: 'NotoSansSC'
''')
        sys.stderr.write('[FONT] registered NotoSansSC: %s\n' % (font_path,))
    except Exception as e:
        sys.stderr.write('[FONT] register failed: %r\n' % (e,))


_setup_cn_font()
# ── 中文字体结束 ──

from config_helper import (load_config, save_config, fmt_size,
                           get_save_dir, CLIENT_VERSION)
from worker import ClientWorker

# 深色主题
PRIMARY = '#1F9E57'
DANGER = '#E0412C'
WARN = '#FFB900'
GRAY = '#848D97'
BG = '#0F1420'
CARD = '#1A2233'
TEXT = '#E8EDF5'

KV = '''
<RootBox>:
    orientation: 'vertical'
    padding: dp(6)
    spacing: dp(6)
    canvas.before:
        Color:
            rgba: 0.059, 0.078, 0.125, 1
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        size_hint_y: None
        height: dp(52)
        padding: [dp(10), 0]
        spacing: dp(8)
        Label:
            text: 'WPS传输工具 · 手机版'
            bold: True
            font_size: '18sp'
            color: 0.91, 0.93, 0.96, 1
            halign: 'left'
        Label:
            id: status_label
            text: '未连接'
            font_size: '13sp'
            color: 0.52, 0.55, 0.59, 1
            halign: 'right'
        Button:
            id: connect_btn
            text: '开始'
            size_hint_x: None
            width: dp(72)
            height: dp(40)
            background_color: 0.12, 0.62, 0.34, 1
            color: 1, 1, 1, 1
            on_release: app.toggle_connect()
'''

Builder.load_string(KV)


class RootBox(BoxLayout):
    pass


class SettingsPage(BoxLayout):
    """连接设置 + 云盘配置"""
    def __init__(self, app_ref, **kwargs):
        super().__init__(orientation='vertical', **kwargs)
        self.app = app_ref
        cfg = load_config()

        # 内容整体放入 ScrollView：设置项较多，小屏/字体换行后可能超屏，需要可滚动
        self.body = BoxLayout(orientation='vertical', padding=dp(12), spacing=dp(10),
                              size_hint_y=None)
        self.body.bind(minimum_height=self.body.setter('height'))
        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(self.body)
        self.add_widget(scroll)

        self.url_input = self._row('服务器地址(含端口)', cfg.get('server_url', ''))
        self.user_input = self._row('服务端账号(可空)', cfg.get('server_user', ''))
        self.pass_input = self._row('服务端密码(可空)', cfg.get('server_pass', ''))
        self.name_input = self._row('客户端名称', cfg.get('client_name', '手机客户端'))
        self.conc_input = self._row('最大并发', str(cfg.get('max_concurrent', 2)))

        self._sep('云盘连接（下载源）')
        self.yidong_input = self._row('139 Token', self._first_cloud('yidong', 'token'), multiline=True)
        self.quark_input = self._row('夸克 Cookie', self._first_cloud('quark', 'cookie'), multiline=True)

        self._sep('WPS 上传')
        self.wps_input = self._row('WPS Cookie', cfg.get('wps_cookie', ''), multiline=True)
        self.parent_input = self._row('WPS 目标目录ID', cfg.get('wps_parent_id', '0'))

        self._sep('Telegram 下载（内置 tdl，手机号 + 验证码登录）')
        self.tg_proxy_input = self._row('代理（如 socks5://127.0.0.1:7890）', cfg.get('tg_proxy', ''))
        self.tdl_threads_input = self._row('下载线程数', str(cfg.get('tdl_threads', 16)))
        self.tdl_pool_input = self._row('连接池大小', str(cfg.get('tdl_pool', 16)))
        self.tg_switch_box = self._check_row('自动测速并切换最快节点（需开 FlClash 外部控制器）',
                                            bool(cfg.get('tg_auto_switch', True)))
        self.clash_api_input = self._row('Clash API 地址', cfg.get('tg_clash_api', '127.0.0.1:9090'))
        self.clash_secret_input = self._row('Clash API Secret（可空）', cfg.get('tg_clash_secret', ''))
        self.clash_group_input = self._row('节点分组名（可空=自动识别）', cfg.get('tg_clash_group', ''))
        # TG 登录直接内联在连接页（不用弹窗，减少出错环节）：
        # 填手机号 → 发送验证码 → 填验证码 → 登录；账号开了两步验证再填密码。
        self.tg_phone_input = TextInput(text=cfg.get('tg_phone', ''), multiline=False,
                                        hint_text='手机号（国内填 11 位，自动补 +86）',
                                        size_hint_y=None, height=dp(44), font_size='14sp')
        self.body.add_widget(self.tg_phone_input)

        tg_btn_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        tg_send_btn = Button(text='发送验证码', background_color=(0.18, 0.42, 0.68, 1),
                             color=(1, 1, 1, 1))
        # 注意：按钮回调里只做转发，所有耗时/子进程操作都在 App 的后台线程里做
        tg_send_btn.bind(on_release=lambda *a: self.app.tg_login_start())
        tg_ok_btn = Button(text='登录', background_color=(0.12, 0.62, 0.34, 1),
                           color=(1, 1, 1, 1))
        tg_ok_btn.bind(on_release=lambda *a: self.app.tg_login_submit())
        tg_btn_row.add_widget(tg_send_btn)
        tg_btn_row.add_widget(tg_ok_btn)
        self.body.add_widget(tg_btn_row)

        self.tg_code_input = TextInput(multiline=False, hint_text='验证码', input_type='number',
                                       size_hint_y=None, height=dp(44), font_size='14sp')
        self.tg_pwd_input = TextInput(multiline=False, password=True,
                                      hint_text='两步验证密码（账号开启 2FA 时才需要）',
                                      size_hint_y=None, height=dp(44), font_size='14sp')
        self.body.add_widget(self.tg_code_input)
        self.body.add_widget(self.tg_pwd_input)

        self.tg_status = Label(
            text='未登录：填手机号 → 点「发送验证码」→ 收到码后填入 → 点「登录」',
            font_size='12sp', color=(0.72, 0.79, 0.86, 1), halign='left', valign='top',
            size_hint_y=None, height=dp(46))
        self.tg_status.bind(width=lambda *a: setattr(self.tg_status, 'text_size',
                                                     (self.tg_status.width, None)))
        self.body.add_widget(self.tg_status)

        tg_tip = Label(text='登录态保存在应用私有目录，只需登录一次；若点击后闪退，'
                             '请把 /sdcard/Download/wpstransfer_tg.log 发给开发者定位。',
                       font_size='11sp', color=(0.52, 0.55, 0.59, 1), halign='left', valign='top',
                       size_hint_y=None)
        tg_tip.bind(width=lambda *a: setattr(tg_tip, 'text_size', (tg_tip.width, None)))
        tg_tip.bind(texture_size=lambda *a: setattr(tg_tip, 'height', tg_tip.texture_size[1]))
        self.body.add_widget(tg_tip)

        self._sep('下载存储')
        self.dir_input = self._row('下载临时目录(可空)', cfg.get('save_dir', ''))
        dir_tip = Label(text='可空：默认存应用私有目录，上传后自动清理。自定义如 /sdcard/Download '
                             '需在系统设置中授予“所有文件访问权限”。',
                        font_size='11sp', color=(0.52, 0.55, 0.59, 1), halign='left', valign='top',
                        size_hint_y=None)
        dir_tip.bind(width=lambda *a: setattr(dir_tip, 'text_size', (dir_tip.width, None)))
        dir_tip.bind(texture_size=lambda *a: setattr(dir_tip, 'height', dir_tip.texture_size[1]))
        self.body.add_widget(dir_tip)

        btn = Button(text='保存配置', size_hint_y=None, height=dp(48),
                     background_color=(0.12, 0.62, 0.34, 1), color=(1, 1, 1, 1))
        btn.bind(on_release=self._save)
        self.body.add_widget(btn)

        tip = Label(text='提示：139 Token / 夸克 Cookie / WPS Cookie 与 PC 客户端配置一致，'
                          '可从电脑端复制粘贴。保存后「开始」连接服务端即可领取任务。',
                    font_size='12sp', color=(0.52, 0.55, 0.59, 1), halign='left', valign='top',
                    size_hint_y=None)
        # 高度随内容自适应：text_size 宽度跟随控件宽度，高度由纹理自动撑开，
        # 避免固定 height 导致文字换行后溢出/重叠。
        tip.bind(width=lambda *a: setattr(tip, 'text_size', (tip.width, None)))
        tip.bind(texture_size=lambda *a: setattr(tip, 'height', tip.texture_size[1]))
        self.body.add_widget(tip)

    def _first_cloud(self, ctype, key):
        for c in load_config().get('clouds', []):
            if c.get('type') == ctype:
                return c.get(key, '')
        return ''

    def set_tg_status(self, text, err=False):
        """更新 TG 登录状态文字（只能在 UI 线程调用）。"""
        self.tg_status.text = text
        self.tg_status.color = (1, 0.45, 0.36, 1) if err else (0.72, 0.79, 0.86, 1)

    def _row(self, title, default='', multiline=False):
        lbl = Label(text=title, font_size='13sp', color=(0.8, 0.85, 0.9, 1),
                    halign='left', size_hint_y=None, height=dp(24))
        self.body.add_widget(lbl)
        inp = TextInput(text=str(default), multiline=multiline,
                        size_hint_y=None,
                        height=dp(84) if multiline else dp(44),
                        background_color=(0.10, 0.14, 0.22, 1),
                        foreground_color=(0.91, 0.93, 0.96, 1),
                        cursor_color=(0.4, 0.8, 0.6, 1),
                        hint_text_color=(0.4, 0.44, 0.5, 1))
        self.body.add_widget(inp)
        return inp

    def _check_row(self, title, default=False):
        """带复选框的设置行（用于开关类配置）。"""
        box = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(6))
        lbl = Label(text=title, font_size='13sp', color=(0.8, 0.85, 0.9, 1),
                    halign='left', valign='middle')
        lbl.bind(size=lambda w, v: setattr(w, 'text_size', (w.width, None)))
        cb = CheckBox(active=bool(default), size_hint_x=None, width=dp(44),
                      color=(0.30, 0.78, 0.55, 1))
        box.add_widget(lbl)
        box.add_widget(cb)
        self.body.add_widget(box)
        return cb

    def _sep(self, text):
        lbl = Label(text=text, bold=True, font_size='13sp',
                    color=(0.30, 0.78, 0.55, 1), halign='left',
                    size_hint_y=None, height=dp(28))
        self.body.add_widget(lbl)

    def _save(self, *_):
        clouds = []
        yt = self.yidong_input.text.strip()
        if yt:
            clouds.append({'id': 'yidong_main', 'type': 'yidong',
                           'name': '139主账号', 'is_primary': True, 'token': yt})
        qc = self.quark_input.text.strip()
        if qc:
            clouds.append({'id': 'quark_main', 'type': 'quark',
                           'name': '夸克主账号', 'is_primary': True, 'cookie': qc})
        cfg = load_config()
        cfg.update({
            'server_url': self.url_input.text.strip(),
            'server_user': self.user_input.text.strip(),
            'server_pass': self.pass_input.text.strip(),
            'client_name': self.name_input.text.strip() or '手机客户端',
            'max_concurrent': int(self.conc_input.text.strip() or 2),
            'clouds': clouds,
            'wps_cookie': self.wps_input.text.strip(),
            'wps_parent_id': self.parent_input.text.strip() or '0',
            'save_dir': self.dir_input.text.strip(),
            'tg_proxy': self.tg_proxy_input.text.strip(),
            'tdl_threads': int(self.tdl_threads_input.text.strip() or 16),
            'tdl_pool': int(self.tdl_pool_input.text.strip() or 16),
            'tg_auto_switch': bool(self.tg_switch_box.active),
            'tg_clash_api': self.clash_api_input.text.strip() or '127.0.0.1:9090',
            'tg_clash_secret': self.clash_secret_input.text.strip(),
            'tg_clash_group': self.clash_group_input.text.strip(),
        })
        save_config(cfg)
        self.app.notify('配置已保存')
        self.app.restart_worker()


class TasksPage(BoxLayout):
    """任务列表"""
    def __init__(self, app_ref, **kwargs):
        super().__init__(orientation='vertical', spacing=dp(6), **kwargs)
        self.app = app_ref
        self.tasks = {}

        self.scroll = ScrollView(size_hint=(1, 1))
        self.list_box = GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        self.list_box.bind(minimum_height=self.list_box.setter('height'))
        self.scroll.add_widget(self.list_box)
        self.add_widget(self.scroll)

    def upsert(self, tid, name, stage, dl, total, speed):
        """线程安全：UI 线程调用 upsert_ui"""
        Clock.schedule_once(lambda dt: self._upsert_ui(tid, name, stage, dl, total, speed))

    def _upsert_ui(self, tid, name, stage, dl, total, speed):
        if tid in self.tasks:
            box, bar, lbl = self.tasks[tid]
            lbl.text = f"{name}\n{self._stage_text(stage, dl, total, speed)}"
            if total and dl is not None:
                bar.max = max(1, total)
                bar.value = min(total, max(0, dl))
        else:
            box = BoxLayout(orientation='vertical', padding=dp(8), spacing=dp(4),
                            size_hint_y=None, height=dp(86))
            from kivy.graphics import Color, Rectangle
            with box.canvas.before:
                Color(rgba=(0.10, 0.14, 0.22, 1))
                bg = Rectangle(pos=box.pos, size=box.size)
            box.bind(pos=lambda w, v, bg=bg: setattr(bg, 'pos', v),
                     size=lambda w, v, bg=bg: setattr(bg, 'size', v))
            bar = ProgressBar(max=100, value=0, size_hint_y=None, height=dp(10))
            lbl = Label(text=f"{name}", font_size='13sp',
                        color=(0.91, 0.93, 0.96, 1), halign='left',
                        size_hint_y=None, height=dp(50))
            box.add_widget(lbl)
            box.add_widget(bar)
            self.list_box.add_widget(box)
            self.tasks[tid] = (box, bar, lbl)
        # 清理已完成/失败任务，避免列表无限增长
        self._cleanup()

    @staticmethod
    def _stage_text(stage, dl, total, speed):
        if stage == 'list':
            return '获取文件列表...'
        if stage == 'download':
            if total and dl:
                pct = min(100, dl * 100 / total)
                return f"下载中 {pct:.1f}%  {fmt_size(dl)}/{fmt_size(total)}  {speed}"
            return f"下载中 {speed}"
        if stage == 'extract':
            return f"解压中... {speed}"
        if stage == 'upload':
            if total and dl and dl > 0:
                pct = min(100, dl * 100 / total)
                return f"上传中 {pct:.1f}%  {fmt_size(dl)}/{fmt_size(total)}  {speed}"
            return f"上传中 {speed}"
        if stage == 'rename':
            return f"整理中... {speed}"
        if stage == 'done':
            return "完成"
        if stage == 'error':
            return f"失败: {speed}"
        return speed or ''

    def _cleanup(self):
        now = time.time()
        stale = [tid for tid, (_, _, lbl) in self.tasks.items()
                 if '完成' in lbl.text or '失败' in lbl.text]
        # 保留最近 12 个，其余移除
        while len(self.tasks) > 12 and stale:
            tid = stale.pop(0)
            if tid in self.tasks:
                box, _, _ = self.tasks.pop(tid)
                try:
                    self.list_box.remove_widget(box)
                except Exception:
                    pass


class LogsPage(BoxLayout):
    """日志页"""
    def __init__(self, app_ref, **kwargs):
        super().__init__(orientation='vertical', padding=dp(8), **kwargs)
        self.app = app_ref
        self.log_text = Label(text='', font_size='12sp', color=(0.80, 0.85, 0.90, 1),
                              halign='left', valign='top', text_size=(None, None))
        self.log_text.bind(size=lambda *a: setattr(self.log_text, 'text_size',
                                                   (self.log_text.width, None)))
        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(self.log_text)
        self.add_widget(scroll)

    def append(self, msg):
        Clock.schedule_once(lambda dt: self._append_ui(msg))

    def _append_ui(self, msg):
        ts = time.strftime('%H:%M:%S')
        cur = self.log_text.text
        self.log_text.text = (cur + f"\n[{ts}] {msg}" if cur else f"[{ts}] {msg}")
        # 限制行数
        lines = self.log_text.text.split('\n')
        if len(lines) > 300:
            self.log_text.text = '\n'.join(lines[-300:])


class WpsTransferApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.worker = None
        self.connected = False
        # TG 登录状态（连接页内联操作）
        self._tg_login_stage = 'idle'
        self._tg_login_cl = None
        self._tg_login_phone = ''

    def build(self):
        self.title = 'WPS传输工具'
        root = RootBox()
        tabs = TabbedPanel(do_default_tab=False, background_color=(0.06, 0.08, 0.13, 1),
                           tab_height=dp(40))
        self.settings_page = SettingsPage(self)
        self.tasks_page = TasksPage(self)
        self.logs_page = LogsPage(self)

        tab1 = TabbedPanelItem(text='连接')
        tab1.add_widget(self.settings_page)
        tab2 = TabbedPanelItem(text='任务')
        tab2.add_widget(self.tasks_page)
        tab3 = TabbedPanelItem(text='日志')
        tab3.add_widget(self.logs_page)
        tabs.add_widget(tab1)
        tabs.add_widget(tab2)
        tabs.add_widget(tab3)
        root.add_widget(tabs)
        self._root = root
        self.status_label = root.ids.status_label
        self.connect_btn = root.ids.connect_btn
        return root

    # ── 日志 / 状态 ──

    def log(self, msg):
        self.logs_page.append(str(msg))

    def set_status(self, text, color='#848D97'):
        Clock.schedule_once(lambda dt: self._status_ui(text, color))

    def _status_ui(self, text, color):
        self.status_label.text = text
        try:
            r, g, b = int(color[1:3], 16) / 255, int(color[3:5], 16) / 255, int(color[5:7], 16) / 255
            self.status_label.color = (r, g, b, 1)
        except Exception:
            pass

    def notify(self, msg):
        popup = Popup(title='提示', content=Label(text=msg, color=(0.9, 0.93, 0.96, 1)),
                      size_hint=(0.8, 0.4))
        close = Button(text='确定', size_hint_y=None, height=dp(44))
        close.bind(on_release=popup.dismiss)
        popup.content.add_widget(close)
        popup.open()

    # ── 连接 / 断开 ──

    def toggle_connect(self):
        if self.connected:
            self._stop_worker()
        else:
            self._start_worker()

    def _start_worker(self):
        cfg = load_config()
        if not cfg.get('server_url'):
            self.notify('请先在「连接」页填写服务器地址')
            return
        self.log(f'配置目录: {get_save_dir()}')
        self.log(f'版本: {CLIENT_VERSION}')
        self.worker = ClientWorker(log_cb=self.log, status_cb=self.set_status,
                                   progress_cb=self.tasks_page.upsert)
        self.connected = True
        self.connect_btn.text = '停止'
        self.connect_btn.background_color = (0.88, 0.25, 0.17, 1)
        self.worker.start()

    def _stop_worker(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.connected = False
        self.connect_btn.text = '开始'
        self.connect_btn.background_color = (0.12, 0.62, 0.34, 1)
        self.set_status('未连接', GRAY)

    def restart_worker(self):
        was = self.connected
        if self.connected:
            self._stop_worker()
        if was:
            Clock.schedule_once(lambda dt: self._start_worker(), 0.5)

    # ── Telegram 登录（手机号 + 验证码，直接在「连接」页操作，不用弹窗）──

    def _tg_status_ui(self, text, err=False):
        Clock.schedule_once(lambda dt: self.settings_page.set_tg_status(text, err))

    def _tg_login_stop(self):
        cl = getattr(self, '_tg_login_cl', None)
        if cl is not None:
            try:
                cl.close()
            except Exception:
                pass
        self._tg_login_cl = None

    def tg_login_start(self):
        """点「发送验证码」：后台启动 tdl login -T code，出现手机号提示时自动填回。"""
        sp = self.settings_page
        phone = (sp.tg_phone_input.text or '').strip()
        # 手机号规范化：允许只填 11 位（自动补 +86），也允许带空格/横线；
        # 国外号码请以 + 开头。
        _digits = "".join(ch for ch in phone if ch.isdigit())
        if phone.startswith("+") and _digits:
            phone = "+" + _digits
        elif _digits.startswith("86") and len(_digits) == 13:
            phone = "+" + _digits
        elif _digits:
            phone = "+86" + _digits
        else:
            phone = ""
        try:
            sp.tg_phone_input.text = phone
        except Exception:
            pass
        if not phone:
            sp.set_tg_status('请输入手机号（国内填 11 位即可，自动加 +86）', True)
            return
        if getattr(self, '_tg_login_stage', 'idle') in ('starting', 'sending_code', 'verifying'):
            return
        cfg = load_config()
        try:
            cfg['tg_phone'] = phone
            save_config(cfg)
        except Exception:
            pass
        self._tg_login_stop()
        self._tg_login_phone = phone
        self._tg_login_stage = 'starting'
        sp.set_tg_status('正在启动 tdl 登录…')

        def _run():
            import mobile_tg_tdl as mtdl
            try:
                mtdl.log_step('点击发送验证码：开始登录流程')
                dl = mtdl.TgTdl(proxy=(cfg.get('tg_proxy') or '').strip(),
                                threads=cfg.get('tdl_threads') or 16,
                                pool=cfg.get('tdl_pool') or 16,
                                log_cb=self.log)
                mtdl.log_step('TgTdl 初始化完成')
                if dl.is_logged_in(use_cache=True):
                    self._tg_login_stage = 'done'
                    self._tg_status_ui('当前已登录，无需再次登录')
                    return
                cl = mtdl.TgCodeLogin(dl, lambda *ev: Clock.schedule_once(
                    lambda dt: self._on_tg_login_event(ev)))
                self._tg_login_cl = cl
                cl.start()
                mtdl.log_step('tdl login 已启动，等待手机号提示')
            except Exception as e:
                mtdl.log_step('登录启动异常: %r' % (e,))
                self._tg_login_stage = 'failed'
                self._tg_status_ui('登录启动失败：%s' % (e,), True)

        threading.Thread(target=_run, daemon=True, name='tg-login').start()

    def tg_login_submit(self):
        """点「登录」：按当前阶段把验证码或两步验证密码写回 tdl。"""
        sp = self.settings_page
        code = (sp.tg_code_input.text or '').strip()
        pwd = (sp.tg_pwd_input.text or '').strip()
        cl = getattr(self, '_tg_login_cl', None)
        if cl is None:
            sp.set_tg_status('请先点「发送验证码」', True)
            return
        stage = getattr(self, '_tg_login_stage', 'idle')
        if stage == 'want_pass':
            if not pwd:
                sp.set_tg_status('账号开启了两步验证，请输入密码', True)
                return
            cl.send(pwd)
            self._tg_login_stage = 'verifying'
            sp.set_tg_status('正在验证两步验证密码…')
        elif stage in ('want_code', 'sending_code', 'verifying'):
            if not code:
                sp.set_tg_status('请输入验证码', True)
                return
            cl.send(code)
            self._tg_login_stage = 'verifying'
            sp.set_tg_status('正在登录…')
        elif stage == 'want_phone':
            phone = getattr(self, '_tg_login_phone', '')
            if phone:
                cl.send(phone)
                self._tg_login_stage = 'sending_code'
                sp.set_tg_status('已提交手机号，正在发送验证码…')
        else:
            sp.set_tg_status('请先点「发送验证码」', True)

    def _on_tg_login_event(self, ev):
        sp = self.settings_page
        kind = ev[0] if ev else ''
        if kind == 'prompt':
            p = ev[1]
            if p == 'phone':
                self._tg_login_stage = 'want_phone'
                phone = getattr(self, '_tg_login_phone', '')
                cl = getattr(self, '_tg_login_cl', None)
                if phone and cl is not None:
                    cl.send(phone)
                    self._tg_login_stage = 'sending_code'
                    sp.set_tg_status('已提交手机号，正在发送验证码…')
                else:
                    sp.set_tg_status('请输入手机号')
            elif p == 'code':
                self._tg_login_stage = 'want_code'
                sp.set_tg_status('验证码已发送，请在 Telegram 查收后填入，再点「登录」')
            elif p == 'password':
                self._tg_login_stage = 'want_pass'
                sp.set_tg_status('账号开启了两步验证，请填密码后点「登录」')
        elif kind == 'error':
            sp.set_tg_status(str(ev[1]), True)
        elif kind == 'done':
            ok, msg = ev[1], ev[2]
            self._tg_login_stage = 'done' if ok else 'failed'
            sp.set_tg_status('登录成功，可以开始使用了' if ok else ('登录失败：' + (msg or '请重试')), not ok)

    def on_stop(self):
        self._stop_worker()


if __name__ == '__main__':
    try:
        WpsTransferApp().run()
    except SystemExit:
        raise
    except BaseException:
        _dump_crash(sys.exc_info())
        raise
