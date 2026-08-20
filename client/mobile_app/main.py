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
