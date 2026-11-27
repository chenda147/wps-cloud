"""手机端 Telegram 下载器（内置 tdl，Android/Kivy）。

与 PC 客户端同一套方案：
  - tdl 把文件切片、跨多个 DC 连接池并发拉取（-t 线程 + --pool 连接池），
    代理带宽能叠加到 3MB/s 以上；单条 MTProto 连接只有 0.1~0.3MB/s。
  - 稳定临时目录 + --continue 断点续传：失败保留分片，下次从断点继续。
  - 登录与 PC 一致：手机号 + 验证码（tdl login -T code）。
    tdl 的 code 登录依赖 survey，必须跑在真终端上，这里用 os.openpty() 给它一个
    伪终端，后台线程读提示、界面把手机号/验证码/两步验证密码写回。
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

# 进度行形如：... 2.9% [........] [113.00 MB in 35.2s; ~ETA: 28m46s; 3.21 MB/s]
_PCT_RE = re.compile(r"([\d.]+)%\s*\[[^\]]*\]\s*\[([^\]]*)\]")
_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB)\s+in\b", re.IGNORECASE)
_SPEED_RE = re.compile(r"([\d.]+)\s*([KMG]?B)/s", re.IGNORECASE)
_UNIT = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}

# bolt 是单写库，同一会话目录同一时间只允许一个 tdl 进程
_SESSION_LOCK = threading.RLock()
# 登录态缓存时长（秒）
_LOGIN_TTL = 600


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


class TgTdl:
    """tdl 封装：登录态检查 / 手机号验证码登录 / 下载（含续传与进度解析）。"""

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
        self.last_output = "Telegram 未登录，请到「连接」页用手机号+验证码登录"
        self.log(self.last_output)
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


class TgCodeLogin:
    """用伪终端（os.openpty）驱动 tdl login -T code 的手机号 + 验证码登录。

    与 PC 端 TdlCodeLogin 行为一致：tdl 的 code 登录走 survey 库，必须有真终端；
    这里给它一个伪终端，后台线程识别“手机号 / 验证码 / 两步验证密码”提示，
    通过 on_event 通知界面，界面再调 send() 把用户输入写回。

    on_event 回调在后台线程触发，Kivy 需 Clock.schedule_once 切回 UI 线程。
    事件：
      ("prompt", "phone")      tdl 在等手机号
      ("prompt", "code")       tdl 在等验证码（输错会再次触发，可重输）
      ("prompt", "password")   tdl 在等两步验证(2FA)密码
      ("log", text)            清理后的输出片段
      ("error", text)          识别到的 Telegram 错误（友好文案）
      ("done", ok, msg)        流程结束
    """

    # Telegram 错误码 → 友好提示（按出现顺序，先匹配先生效）
    _ERRORS = [
        ("PHONE_CODE_INVALID", "验证码错误，请重新获取"),
        ("PHONE_CODE_EXPIRED", "验证码已过期，请重新获取"),
        ("PHONE_CODE_EMPTY", "请输入验证码"),
        ("PHONE_NUMBER_INVALID", "手机号格式不正确"),
        ("PHONE_NUMBER_OCCUPIED", "该手机号已被其它账号占用"),
        ("SESSION_PASSWORD_NEEDED", "账号开启了两步验证，需要输入云密码"),
        ("PASSWORD_HASH_INVALID", "两步验证密码错误"),
        ("FLOOD_WAIT", "操作过于频繁，请稍后再试"),
        ("AUTH_TOKEN_EXPIRED", "登录令牌过期，请重试"),
    ]

    def __init__(self, dl, on_event):
        self.dl = dl
        self.on_event = on_event
        self._proc = None
        self._master = None
        self._th = None
        self._buf = ""
        self._new = ""
        self._closed = False
        self._latch = None

    def _emit(self, *ev):
        try:
            self.on_event(*ev)
        except Exception:
            pass

    def start(self):
        try:
            master, slave = os.openpty()
        except Exception as e:
            raise TdlError("当前设备不支持伪终端（openpty 失败）：%s" % e)
        try:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        except Exception:
            pass
        args = [self.dl.tdl, "login", "-T", "code"] + self.dl._base_args()
        try:
            self._proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave,
                                          env=self.dl._env(), cwd=self.dl.home,
                                          close_fds=True)
        finally:
            try:
                os.close(slave)
            except Exception:
                pass
        self._master = master
        try:
            os.set_blocking(master, False)
        except Exception:
            pass
        self._th = threading.Thread(target=self._reader, daemon=True, name="tdl-code-login")
        self._th.start()

    def send(self, text):
        if self._master is None:
            return
        try:
            os.write(self._master, (str(text).strip() + "\r").encode("utf-8"))
        except Exception as e:
            self._emit("log", "写入失败：%s" % e)

    def close(self):
        self._closed = True
        p = self._proc
        try:
            if p is not None and p.poll() is None:
                p.kill()
        except Exception:
            pass
        try:
            if self._master is not None:
                os.close(self._master)
        except Exception:
            pass
        self._master = None

    def _reader(self):
        import select
        last_log = 0.0
        while not self._closed:
            try:
                r, _, _ = select.select([self._master], [], [], 0.2)
            except Exception:
                break
            if r:
                try:
                    data = os.read(self._master, 8192)
                except OSError:
                    data = b""
                if data:
                    txt = data.decode("utf-8", "replace")
                    self._buf += txt
                    self._new += txt
                    self._scan()
                    now = time.time()
                    if now - last_log > 0.6:
                        last_log = now
                        self._emit("log", _clean(self._buf[-1200:]))
            if self._proc is not None and self._proc.poll() is not None:
                try:
                    while True:
                        data = os.read(self._master, 8192)
                        if not data:
                            break
                        txt = data.decode("utf-8", "replace")
                        self._buf += txt
                        self._new += txt
                        self._scan()
                except Exception:
                    pass
                break
        # 结束：以 is_logged_in 为准判定成功（tdl 已退出，不会抢 bolt 锁）
        ok = False
        try:
            ok = self.dl.is_logged_in(use_cache=False)
        except Exception:
            ok = False
        msg = "" if ok else self._diagnose()
        self._emit("done", ok, msg)

    def _scan(self):
        seg = _clean(self._new).lower()
        up = self._new.upper()
        # 先处理错误：出错即解锁，让后续同类型提示（如验证码错误后重输）能再次触发
        for key, friendly in self._ERRORS:
            if key in up:
                self._latch = None
                self._emit("error", friendly)
        # survey 会把旧提示残留在回显里，取“位置最靠后（最新出现）”的提示为准
        candidates = (
            ("phone", seg.rfind("enter your phone number")),
            ("code", seg.rfind("enter code")),
        )
        i_pass = max(
            seg.rfind("password"), seg.rfind("session_password_needed"),
            seg.rfind("two-factor"), seg.rfind("2fa"),
        )
        candidates += (("password", i_pass),)
        detected = None
        best = -1
        for name, idx in candidates:
            if idx > best:
                best = idx
                detected = name
        if detected and detected != self._latch:
            self._latch = detected
            self._emit("prompt", detected)
        self._new = self._new[-300:]

    def _diagnose(self):
        up = self._buf.upper()
        for key, friendly in self._ERRORS:
            if key in up:
                return friendly
        tail = _clean(self._buf[-300:]).replace("\n", " ").strip()
        try:
            ec = self._proc.returncode if self._proc is not None else None
        except Exception:
            ec = None
        if ec not in (0, None, -1):
            return "tdl 退出码 %s：%s" % (ec, tail[:120])
        return tail or "登录未成功"
