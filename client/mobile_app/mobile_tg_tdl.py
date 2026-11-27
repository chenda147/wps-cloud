"""手机端 Telegram 下载器（内置 tdl，Android/Kivy）。

与 PC 客户端同一套方案：
  - tdl 把文件切片、跨多个 DC 连接池并发拉取（-t 线程 + --pool 连接池），
    代理带宽能叠加到 3MB/s 以上；单条 MTProto 连接只有 0.1~0.3MB/s。
  - 稳定临时目录 + --continue 断点续传：失败保留分片，下次从断点继续。
  - 登录：tdl login -T qr 的 ANSI 二维码在本模块解析成 0/1 矩阵，
    由 Kivy 界面渲染成图片给用户扫（无需 root、无需模拟点击官方 Telegram）。
  - 代理沿用 PC：本地 http 代理自动改 socks5（MTProto 必须）。

登录态用 bolt 存储，目录固定在应用私有目录 tdl_data/；
登录成功写 login_ok 缓存（600 秒），避免每个任务都去连一次 Telegram。
"""
import os
import re
import sys
import time
import shutil
import hashlib
import threading
import subprocess

from config_helper import BASE_DIR

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# tdl 二维码由 █ ▀ ▄ 和空格拼成，逐字符展开成上下两个像素
_BLOCKS = {"\u2588": (1, 1), "\u2580": (1, 0), "\u2584": (0, 1), " ": (0, 0)}

# 进度行形如：... 2.9% [........] [113.00 MB in 35.2s; ~ETA: 28m46s; 3.21 MB/s]
_PCT_RE = re.compile(r"([\d.]+)%\s*\[[^\]]*\]\s*\[([^\]]*)\]")
_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB)\s+in\b", re.IGNORECASE)
_SPEED_RE = re.compile(r"([\d.]+)\s*([KMG]?B)/s", re.IGNORECASE)
_UNIT = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}

# bolt 是单写库，同一会话目录同一时间只允许一个 tdl 进程
_SESSION_LOCK = threading.RLock()
# 登录态缓存时长（秒）
_LOGIN_TTL = 600

# 登录状态（与界面约定）
S_STARTING = 0     # 正在连接 Telegram 建 auth key
S_WAITING = 1      # 二维码已显示，等用户扫
S_CONFIRMING = 2   # 已扫码，正在迁移 DC（保留）
S_SUCCESS = 3
S_EXPIRED = 4
S_FAILED = 5


class TdlError(Exception):
    """tdl 相关错误（带中文提示）"""


def _normalize_proxy(p, log=lambda m: None):
    """把本地 http 代理自动改成 socks5（MTProto 走 HTTP CONNECT 极易 EOF）。"""
    p = (p or "").strip()
    if not p:
        return p
    low = p.lower()
    if low.startswith("http://"):
        host = re.sub(r"^https?://", "", p).split("/")[0].split(":")[0].lower()
        local = host in ("127.0.0.1", "localhost", "::1") \
            or host.startswith("192.168.") or host.startswith("10.") \
            or host.startswith("172.")
        if local:
            new = "socks5://" + p[len("http://"):]
            log("检测到 http 代理，MTProto 必须走 socks5，已自动改用：%s" % new)
            return new
        log("警告：远端 http 代理跑 MTProto 容易断连，建议改用 socks5://")
    return p


def find_tdl():
    """定位 APK 内打包的 tdl 静态二进制（arm64），并补执行权限。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "tools", "tdl"),
             os.path.join(BASE_DIR, "tools", "tdl")]
    private = os.environ.get("ANDROID_PRIVATE", "")
    if private:
        cands.append(os.path.join(private, "files", "app", "tools", "tdl"))
        cands.append(os.path.join(private, "files", "tools", "tdl"))
    cands.append(os.path.join(os.path.dirname(here), "tools", "tdl"))
    for p in cands:
        if os.path.isfile(p):
            try:
                os.chmod(p, 0o755)
            except Exception:
                pass
            return p
    return shutil.which("tdl") or ""


def default_session_dir():
    d = os.path.join(BASE_DIR, "tdl_data")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def build_link(channel, msg_id, link=""):
    """拼出 tdl 可识别的 t.me 链接：优先任务自带 link，其次频道名，最后内部 ID。"""
    lk = str(link or "").strip()
    if lk.startswith("http://") or lk.startswith("https://"):
        base = lk.rstrip("/")
        if re.search(r"/\d+$", base):
            return base
        mid = str(msg_id or "").strip()
        return base + "/" + mid if mid else base
    ch = str(channel or "").strip()
    mid = str(msg_id or "").strip()
    if not ch:
        return ""
    if ch.startswith("http://") or ch.startswith("https://"):
        base = ch.rstrip("/")
        if re.search(r"/\d+$", base):
            return base
        return base + "/" + mid if mid else base
    ch = re.sub(r"^(t\.me/|telegram\.me/|www\.t\.me/)", "", ch.lstrip("@").strip(), flags=re.I)
    if not ch:
        return ""
    if ch.startswith("-100"):
        ch = ch[4:]
    if ch.isdigit():
        return "https://t.me/c/%s/%s" % (ch, mid) if mid else ""
    return "https://t.me/%s/%s" % (ch, mid) if mid else "https://t.me/%s" % ch


def _clean(text):
    return _ANSI.sub("", text).replace("\x08", "")


def last_qr_matrix(text):
    """从 tdl 输出里取出最后一帧二维码，返回二维 0/1 矩阵（无有效帧返回 None）。

    tdl 每次重画二维码的格式是「打印 N 行 + 连发 N 个光标上移(ESC[A)」，
    不按光标上移切帧会把多次刷新拼成一张图。
    """
    text = _ANSI.sub(lambda m: m.group(0) if m.group(0).endswith("A") else "", text)
    frames = re.split(r"(?:\x1b\[[0-9]*A)+", text)
    block_lines = None
    for seg in reversed(frames):
        lines = [ln.rstrip("\r") for ln in seg.split("\n")]
        qr = [ln for ln in lines if ln.strip() and all(ch in _BLOCKS for ch in ln)]
        # 只接受画完的帧：完整帧末行只有 ▀/空格（底部空白边）
        if len(qr) >= 10 and all(ch in (" ", "\u2580") for ch in qr[-1]):
            block_lines = qr
            break
    if not block_lines:
        return None
    width = max(len(ln) for ln in block_lines)
    rows = []
    for ln in block_lines:
        ln = ln.ljust(width)
        top, bot = [], []
        for ch in ln:
            t, b = _BLOCKS.get(ch, (0, 0))
            top.append(t)
            bot.append(b)
        rows.append(top)
        rows.append(bot)
    while rows and not any(rows[-1]):
        rows.pop()
    while rows and not any(rows[0]):
        rows.pop(0)
    if not rows or len(rows[0]) < 21:
        return None
    w = len(rows[0])
    if len(rows) > w:
        rows = rows[-w:]
    return rows


class TgTdl:
    """tdl 封装：登录态检查 / 扫码登录 / 下载（含续传与进度解析）。"""

    def __init__(self, proxy="", threads=16, pool=16, session_dir="", log_cb=None):
        self.tdl = find_tdl()
        if not self.tdl:
            raise TdlError("未找到 tdl 可执行文件（应打包在 APK 的 tools/tdl）")
        self.log = log_cb or (lambda m: None)
        self.proxy = _normalize_proxy(proxy, self.log)
        try:
            self.threads = max(1, min(int(threads or 16), 32))
        except (TypeError, ValueError):
            self.threads = 16
        try:
            self.pool = max(1, min(int(pool or 16), 32))
        except (TypeError, ValueError):
            self.pool = 16
        self.session_dir = session_dir or default_session_dir()
        try:
            os.makedirs(self.session_dir, exist_ok=True)
        except Exception:
            pass
        # tdl 的 HOME/.tdl（配置与日志）也放在应用私有目录，避免污染系统目录
        self.home = os.path.join(BASE_DIR, "tdl_home")
        try:
            os.makedirs(self.home, exist_ok=True)
        except Exception:
            pass
        self.last_output = ""
        self._ok_file = os.path.join(self.home, "login_ok")

    # ── 登录态缓存 ─────────────────────────────────────────────
    def _cached_ok(self):
        try:
            return (time.time() - os.path.getmtime(self._ok_file)) < _LOGIN_TTL
        except Exception:
            return False

    def _touch_ok(self):
        try:
            with open(self._ok_file, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    def _clear_ok(self):
        try:
            os.remove(self._ok_file)
        except Exception:
            pass

    # ── 进程与参数 ─────────────────────────────────────────────
    def _env(self):
        e = dict(os.environ)
        e["HOME"] = self.home
        e["TMPDIR"] = self.home
        return e

    def _base_args(self):
        args = ["--storage", "type=bolt,path=%s" % self.session_dir]
        if self.proxy:
            args += ["--proxy", self.proxy]
        return args

    def _popen(self, args):
        return subprocess.Popen([self.tdl] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=self._env(),
                                cwd=self.home)

    @staticmethod
    def _pump(proc, buf, limit=400000):
        def _run():
            try:
                while True:
                    b = proc.stdout.read(4096)
                    if not b:
                        break
                    buf.extend(b)
                    if len(buf) > limit:
                        del buf[:len(buf) - limit]
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    # ── 登录态 ─────────────────────────────────────────────────
    def is_logged_in(self, timeout=60, use_cache=True):
        if use_cache and self._cached_ok():
            return True
        with _SESSION_LOCK:
            buf = bytearray()
            try:
                p = self._popen(["chat", "ls"] + self._base_args())
            except Exception as e:
                self.last_output = str(e)
                return False
            self._pump(p, buf)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
                self.last_output = "登录态检查超时"
                return False
            self.last_output = _clean(bytes(buf).decode("utf-8", "replace")).strip()
            if p.returncode == 0:
                self._touch_ok()
                return True
            self._clear_ok()
            return False

    def ensure_login(self, timeout=60):
        if self.is_logged_in(timeout=timeout):
            return True
        self.last_output = "Telegram 未登录，请到「连接」页点「TG 登录」扫码"
        self.log(self.last_output)
        return False

    def login_qr(self, on_qr=None, on_state=None, cancel_evt=None, timeout=300):
        """拉起 tdl 扫码登录，阻塞到成功/失败。

        on_qr(matrix)      每次二维码刷新时回调（0/1 二维数组）
        on_state(state,msg) 状态变化：starting/waiting/confirming/success/expired/failed
        cancel_evt         取消事件（用户关闭弹窗）
        """
        def emit(st, text=""):
            try:
                if on_state:
                    on_state(st, text)
            except Exception:
                pass

        if self._cached_ok():
            emit(S_SUCCESS, "当前已登录，无需扫码")
            return True
        emit(S_STARTING, "正在连接 Telegram 生成二维码…")
        deadline = time.time() + timeout
        ok = False
        _SESSION_LOCK.acquire()
        try:
            first = True
            while time.time() < deadline:
                if cancel_evt is not None and cancel_evt.is_set():
                    emit(S_FAILED, "已取消")
                    return False
                buf = bytearray()
                try:
                    p = self._popen(["login", "-T", "qr"] + self._base_args())
                except Exception as e:
                    emit(S_FAILED, "启动 tdl 登录失败：%s" % e)
                    self.last_output = str(e)
                    return False
                self._pump(p, buf)
                last_key = None
                last_emit = 0.0
                while p.poll() is None and time.time() < deadline:
                    if cancel_evt is not None and cancel_evt.is_set():
                        try:
                            p.kill()
                        except Exception:
                            pass
                        emit(S_FAILED, "已取消")
                        return False
                    now = time.time()
                    if on_qr and now - last_emit >= 0.4:
                        txt = bytes(buf[-30000:]).decode("utf-8", "replace")
                        m = last_qr_matrix(txt)
                        if m:
                            key = hash(tuple(tuple(r) for r in m))
                            if key != last_key:
                                last_key = key
                                try:
                                    on_qr(m)
                                except Exception:
                                    pass
                                emit(S_WAITING, "请用手机 Telegram 扫码登录")
                        last_emit = now
                    time.sleep(0.3)
                if p.poll() is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    emit(S_FAILED, "登录超时，请重试")
                    return False
                out = _clean(bytes(buf).decode("utf-8", "replace"))
                up = out.upper()
                self.last_output = out[-400:].strip()
                if p.returncode == 0 and "LOGIN SUCCESSFULLY" in up:
                    ok = True
                    break
                if "EXPIRED" in up and ("TOKEN" in up or "LOGIN" in up):
                    # 二维码过期：tdl 自动退出，换一张新码继续等
                    emit(S_EXPIRED, "二维码已过期，正在换新码…")
                    self.log("[tdl] 二维码过期，自动换新码重试")
                    time.sleep(1.2)
                    first = False
                    continue
                emit(S_FAILED, (self.last_output.replace("\n", " ")[:160] or "登录失败"))
                return False
        finally:
            _SESSION_LOCK.release()
        if ok:
            self._touch_ok()
            emit(S_SUCCESS, "登录成功")
            self.log("tdl 扫码登录成功")
            return True
        emit(S_FAILED, "登录超时，请重试")
        return False

    # ── 下载 ───────────────────────────────────────────────────
    def download(self, url, dest, progress_cb=None, cancel_evt=None):
        """下载单个 t.me 链接到 dest（同一会话目录串行，bolt 不支持并发写）。"""
        with _SESSION_LOCK:
            return self._download_one(url, dest, progress_cb, cancel_evt)

    def _download_one(self, url, dest, progress_cb, cancel_evt):
        if not url:
            return False, "缺少 Telegram 链接"
        # 稳定临时目录 + --continue：同一链接失败后从断点续传，不丢分片
        _key = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
        tmpdir = os.path.join(os.path.dirname(dest) or ".", "_tdl_dl", _key)
        try:
            os.makedirs(tmpdir, exist_ok=True)
        except Exception:
            pass
        args = ["dl", "-u", url, "-t", str(self.threads), "--pool", str(self.pool),
                "-d", tmpdir, "--continue"] + self._base_args()
        self.log("tdl 开始：%s (线程 %d / 连接池 %d)" % (url, self.threads, self.pool))
        p = None
        _done = False
        try:
            p = self._popen(args)
        except Exception as e:
            return False, "启动 tdl 失败：%s" % e
        buf = bytearray()
        self._pump(p, buf)
        last_cb = 0.0
        try:
            while True:
                if p.poll() is not None:
                    break
                if cancel_evt is not None and cancel_evt.is_set():
                    try:
                        p.kill()
                        p.wait(timeout=10)
                    except Exception:
                        pass
                    return False, "已取消"
                time.sleep(0.3)
                now = time.time()
                if progress_cb and now - last_cb >= 0.5:
                    txt = _clean(bytes(buf[-4000:]).decode("utf-8", "replace"))
                    info = self._parse_progress(txt)
                    if info:
                        last_cb = now
                        try:
                            progress_cb(*info)
                        except Exception:
                            pass
            tail = _clean(bytes(buf).decode("utf-8", "replace"))[-400:].strip()
            self.last_output = tail
            if p.returncode != 0:
                up = tail.upper()
                if any(k in up for k in ("AUTH_KEY", "SESSION_REVOKED", "AUTH_TOKEN",
                                         "USER_DEACTIVATED", "NOT AUTHORIZED")):
                    self._clear_ok()
                return False, "tdl 退出码 %s：%s" % (p.returncode, tail.replace("\n", " ")[:200])
            self._touch_ok()

            files, best = [], None
            want = os.path.basename(dest)
            for dp, _, fns in os.walk(tmpdir):
                for fn in fns:
                    if fn.endswith(".tmp"):
                        continue
                    files.append(os.path.join(dp, fn))
            if not files:
                return False, "tdl 未产出文件（链接无效或需要重新登录）"
            for f in files:
                if os.path.basename(f).endswith(want):
                    best = f
                    break
            if not best:
                best = max(files, key=lambda x: os.path.getsize(x))
            if os.path.getsize(best) <= 0:
                return False, "tdl 产出文件为空"
            try:
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                if os.path.exists(dest):
                    os.remove(dest)
                shutil.move(best, dest)
                _done = True
            except Exception as e:
                return False, "移动下载文件失败：%s" % e
            return True, ""
        finally:
            try:
                if p is not None and p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)
            except Exception:
                pass
            if _done:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass

    @staticmethod
    def _parse_progress(seg):
        """从进度片段取 (已下载字节, 总字节, 速度MB/s)；tdl 只给百分比+已下载量，
        用 done/百分比反推总大小，让任务卡片能显示「已下载/总大小·百分比」。"""
        pct = None
        detail = ""
        for m in _PCT_RE.finditer(seg):
            pct, detail = m.group(1), m.group(2)
        if pct is None:
            return None
        done = 0
        m = _SIZE_RE.search(detail)
        if m:
            done = int(float(m.group(1)) * _UNIT.get(m.group(2).upper(), 1))
        speed_mb = 0.0
        m = _SPEED_RE.search(detail)
        if m:
            speed_mb = float(m.group(1)) * _UNIT.get(m.group(2).upper(), 1) / 1048576.0
        total = 0
        try:
            p = float(pct)
            if done > 0 and p > 0:
                total = done if p >= 100 else int(done * 100.0 / p)
        except (TypeError, ValueError):
            total = 0
        if total < done:
            total = done
        return done, total, speed_mb
