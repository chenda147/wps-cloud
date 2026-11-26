"""手机本机（无需 adb）用 root 模拟点击官方 Telegram 下载。

与 PC 端 client/phone_download.py 策略完全一致：
  打开 t.me/<channel>/<msg> → uiautomator dump 当前窗口 → 解析控件树 →
    · 找到 content-desc 含 "Download"/"下载" 的按钮 → 未下载：点击一次
    · 找到 content-desc 含 "Downloading"/"正在下载"/"%" → 下载中：等待
    · 以上都没有、且缓存里有已完成文件 → 已下载：直接从缓存取
  等待完成：留在 TG 前台用 UI dump 看进度环消失 + .temp 改名 + 体积达标。

v1.0.9 关键改动（取代 v1.0.8 截图+raw 字节解析方案）：
  - 废弃 screencap + raw 字节亮度分析（不同 DPI/分辨率/主题下定位极不稳定）
  - 改用 root 直接执行 `uiautomator dump` 拿 AccessibilityNodeInfo 树
  - 点击坐标用节点 bounds 中心（[left,top][right,bottom] 算术中心），零偏移零设备依赖
  - 状态判断基于 content-desc / text（官方 Telegram 不混淆这些字段）
  - 下载完成判据三选一：.temp 改名 / UI 进度环消失 / 体积达标+稳定
  - 仍优先留在 TG 前台等待（最多 4 分钟，避免 APP 被系统冻结），超时切回 APP
"""

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

TG_PKGS = ("org.telegram.messenger.web", "org.telegram.messenger",
           "org.telegram.messenger.beta", "tw.nekomimi.nekogram")
_TG_ACTIVE = None

STATE_DONE = "done"
STATE_DOWNLOADING = "downloading"
STATE_NOT_DOWNLOADED = "not_downloaded"
STATE_UNKNOWN = "unknown"

# 缓存的视频最小体积（判断"已下载"时排除缩略图/小文件）
_MIN_DONE = 2 * 1048576

# 只认领真正的文件缓存：<dc>_<id> 或 <dc>_<id>.temp
# 视频流式播放还会生成 <dc>_<id>_64.preload 以及 .parts，必须排除
_RE_CACHE = re.compile(r"^\d+_\d+(\.temp)?$")


def _is_cache_name(name):
    return bool(_RE_CACHE.match(name or ""))


_SU_CANDIDATES = ("/system/bin/su", "/system/xbin/su", "/debug_ramdisk/su",
                  "/sbin/su", "su")
_su_path = None
_last_err = ""


# ────────────────────────── root 命令执行 ──────────────────────────

def _clean_env():
    """p4a 进程会带 LD_LIBRARY_PATH / LD_PRELOAD 指向应用私有库，
    直接 exec 系统二进制可能因链接不上而失败，这里清掉再执行 su。"""
    env = dict(os.environ)
    for k in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"):
        env.pop(k, None)
    env["PATH"] = "/system/bin:/system/xbin:/product/bin:/sbin:" + env.get("PATH", "")
    return env


def _exec(args, timeout):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           env=_clean_env())
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "ignore"),
                (p.stderr or b"").decode("utf-8", "ignore"))
    except subprocess.TimeoutExpired:
        return 124, "", "等待 root 授权超时"
    except Exception as e:
        return 1, "", str(e)


def _find_su(timeout=60):
    """探测可用的 su。首次调用会在手机上弹出 root 授权框，需用户点"允许"，
    因此超时给足 60 秒；失败不缓存，下次任务会重新探测。"""
    global _su_path, _last_err
    if _su_path:
        return _su_path
    cands = [c for c in _SU_CANDIDATES if c == "su" or os.path.exists(c)]
    if not cands:
        cands = ["su"]
    errs = []
    for cand in cands:
        code, out, err = _exec([cand, "-c", "id"], timeout)
        if "uid=0" in out:
            _su_path = cand
            _last_err = ""
            return cand
        if code == 124:
            _last_err = "等待 root 授权超时（请在手机弹框里点'允许'）"
            return None
        msg = (err or out).strip().replace("\n", " ")[:60]
        errs.append("%s -> %s" % (cand, msg or ("退出码 %d" % code)))
    _last_err = "；".join(errs) or "未找到 su"
    return None


def _su(cmd, timeout=60):
    """以 root 执行一条 shell 命令，返回 (code, stdout, stderr)。"""
    su = _find_su()
    if not su:
        return 1, "", "未获取 root 权限（%s）" % (_last_err or "su 不可用")
    return _exec([su, "-c", cmd], timeout)


def _tg_pkg():
    """返回已安装的官方 Telegram 包名（未安装返回 None）。"""
    global _TG_ACTIVE
    if _TG_ACTIVE:
        return _TG_ACTIVE
    for pkg in TG_PKGS:
        code, out, _ = _su("pm path %s" % pkg, timeout=20)
        if "package:" in out:
            _TG_ACTIVE = pkg
            return pkg
    return None


def _cache_dir():
    pkg = _tg_pkg()
    return "/sdcard/Android/data/%s/cache" % pkg if pkg else ""


def probe():
    """探测运行环境，返回 (可用, Telegram包名, 说明文字)。"""
    su = _find_su()
    if not su:
        return False, None, ("root 授权不可用：%s（首次会弹 root 授权框，"
                             "请在手机上点'允许'）" % (_last_err or "su 未找到"))
    pkg = _tg_pkg()
    if not pkg:
        return False, None, "未找到官方 Telegram（已查：%s）" % ",".join(TG_PKGS)
    return True, pkg, "root=%s，Telegram=%s" % (su, pkg)


def available():
    return probe()[0]


def _foreground_line():
    code, out, _ = _su("dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'",
                       timeout=25)
    if not out.strip():
        code, out, _ = _su("dumpsys activity activities 2>/dev/null | grep -E 'mResumedActivity|topResumedActivity'",
                           timeout=25)
    line = out.strip().splitlines()[0] if out.strip() else ""
    return line[:160]


def _self_pkg():
    try:
        with open("/proc/self/cmdline", "rb") as f:
            name = f.read().split(b"\0")[0].decode("utf-8", "ignore").strip()
        if name:
            return name.split(":")[0]
    except Exception:
        pass
    return "org.wps.transfer.wpstransfer"


def _bring_app_front():
    """把本应用切回前台。APP 在后台会被 Android 冻结（cached app freezer），
    冻结后轮询/上传全部停摆，所以等待超时必须切回来。"""
    pkg = _self_pkg()
    _su("am start -n %s/org.kivy.android.PythonActivity" % pkg, timeout=25)
    time.sleep(1.5)
    fg = _foreground_line()
    if pkg in fg:
        return True
    _su("monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg, timeout=25)
    time.sleep(1.5)
    fg = _foreground_line()
    return pkg in fg


# ────────────────────────── uiautomator dump（v1.0.9 新增）─────────────────────────

# uiautomator dump 临时路径（root 写入，本进程用 cat 读回避免权限问题）
_UI_DUMP = "/sdcard/.wps_transfer_ui.xml"

# 多路径兜底：部分 ROM 把 uiautomator 放在非标准位置
_UI_BINS = ("uiautomator", "/system/bin/uiautomator",
            "/system/xbin/uiautomator", "/product/bin/uiautomator")


def _ui_dump():
    """root 执行 uiautomator dump，返回 (ok, xml_string)。
    dump 只拿当前前台窗口的 View 树，所以调用方必须确保 TG 在前台。"""
    last_err = ""
    for binp in _UI_BINS:
        code, out, err = _su("%s dump '%s' 2>&1" % (binp, _UI_DUMP), timeout=20)
        if code == 0 or "UI hierchical dump" in (out + err) or "wrote to" in (out + err).lower():
            # dump 成功，用 cat 读回（su 写的文件本进程可能没权限 open）
            c2, xml, _ = _su("cat '%s'" % _UI_DUMP, timeout=10)
            if c2 == 0 and "<hierarchy" in xml:
                return True, xml
            last_err = "dump ok 但 cat 失败"
        else:
            last_err = (err or out).strip()[:80]
    # 最后兜底：直接尝试本进程 open（有些机型 uiautomator dump 到 /data/local/tmp 权限不同）
    try:
        with open(_UI_DUMP, "r", encoding="utf-8", errors="ignore") as f:
            xml = f.read()
        if "<hierarchy" in xml:
            return True, xml
    except Exception:
        pass
    return False, last_err


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _ui_parse(xml_string):
    """解析 uiautomator dump 的 XML，返回节点 list。
    每个节点 dict: text, desc, cls, res, left/top/right/bottom/cx/cy/w/h, raw。"""
    try:
        root = ET.fromstring(xml_string)
    except Exception:
        return []
    nodes = []
    for node in root.iter("node"):
        raw = node.attrib
        text = (raw.get("text") or "").strip()
        desc = (raw.get("content-desc") or "").strip()
        cls = (raw.get("class") or "").strip()
        res = (raw.get("resource-id") or "").strip()
        bounds_str = raw.get("bounds", "[0,0][0,0]")
        m = _BOUNDS_RE.search(bounds_str)
        if m:
            left, top, right, bottom = map(int, m.groups())
        else:
            left = top = right = bottom = 0
        w, h = right - left, bottom - top
        nodes.append({
            "text": text, "desc": desc, "cls": cls, "res": res,
            "left": left, "top": top, "right": right, "bottom": bottom,
            "cx": (left + right) // 2, "cy": (top + bottom) // 2,
            "w": w, "h": h, "raw": raw,
        })
    return nodes


def _ui_tap(node_or_coords):
    """点击：接受 dict node 或 (cx, cy)。返回 (code, out, err)。"""
    if isinstance(node_or_coords, dict):
        cx, cy = node_or_coords["cx"], node_or_coords["cy"]
    else:
        cx, cy = node_or_coords
    return _su("input tap %d %d" % (cx, cy), timeout=10)


def _ui_filter(nodes, desc_kw=None, text_kw=None, res_kw=None, cls_kw=None,
               min_w=20, min_h=20):
    """按关键字过滤节点。每个 kw 是 list，任一 substring 匹配即通过（大小写不敏感）。
    返回 list，按面积从小到大排（图标按钮面积通常小）。"""
    out = []
    for n in nodes:
        if n["w"] < min_w or n["h"] < min_h:
            continue
        ok = True
        for field, kws in (("desc", desc_kw), ("text", text_kw),
                           ("res", res_kw), ("cls", cls_kw)):
            if not kws:
                continue
            val = n.get(field, "") or ""
            if not any(kw.lower() in val.lower() for kw in kws):
                ok = False
                break
        if ok:
            out.append(n)
    out.sort(key=lambda n: n["w"] * n["h"])
    return out


# ────────────────────────── Telegram 控件状态识别 ──────────────────────────

# 下载按钮（未下载时的图标）
_DL_BTN = ["download", "下载", "ic_download", "btn_download", "get", "保存"]
# 下载中（进度环/ProgressBar 的 content-desc）
_DLING = ["downloading", "正在下载", "in progress", "进行中", "downloading…",
          "downloading..."]
# 已下载后的播放按钮
_PLAY = ["play", "播放", "打开", "open", "video"]
# 进度百分比特征
_PCT_RE = re.compile(r"\d+\s*%")


def _detect_tg_ui_state(nodes, log=None):
    """根据 UI 控件树判断当前视频气泡的下载状态。
    返回 (state, action_node)：
      state: STATE_DONE / STATE_DOWNLOADING / STATE_NOT_DOWNLOADED / STATE_UNKNOWN
      action_node: 需要点击的节点（仅 NOT_DOWNLOADED 时有值）
    """
    if not nodes:
        return STATE_UNKNOWN, None

    # 1. 下载中（进度环）—— 最优先，避免"下载按钮"和"下载中"同时出现时误判
    for n in nodes:
        full = (n.get("desc", "") + " " + n.get("text", ""))
        if any(kw.lower() in full.lower() for kw in _DLING):
            if log:
                log("[本机TG] UI：下载中节点 desc='%s' text='%s'"
                    % (n.get("desc", "")[:40], n.get("text", "")[:40]))
            return STATE_DOWNLOADING, None
        if _PCT_RE.search(full):
            if log:
                log("[本机TG] UI：检测到进度百分比 '%s'" % full[:40])
            return STATE_DOWNLOADING, None
        if "progress" in (n.get("cls") or "").lower() and full:
            if log:
                log("[本机TG] UI：ProgressBar 控件 desc='%s'" % full[:40])
            return STATE_DOWNLOADING, None

    # 2. 下载按钮（未下载）
    dl_btns = _ui_filter(nodes, desc_kw=_DL_BTN, text_kw=_DL_BTN)
    if dl_btns:
        btn = dl_btns[0]
        if log:
            log("[本机TG] UI：下载按钮 bounds=[%d,%d][%d,%d] desc='%s'"
                % (btn["left"], btn["top"], btn["right"], btn["bottom"],
                   btn.get("desc", "")[:30]))
        return STATE_NOT_DOWNLOADED, btn

    # 3. 播放按钮（已下载）
    play_btns = _ui_filter(nodes, desc_kw=_PLAY, text_kw=_PLAY)
    if play_btns:
        if log:
            log("[本机TG] UI：播放按钮 → 已下载")
        return STATE_DONE, None

    return STATE_UNKNOWN, None


# ────────────────────────── Telegram 缓存探测 ──────────────────────────

def _cache_list():
    cdir = _cache_dir()
    if not cdir:
        return {}
    code, out, _ = _su("cd '%s' && stat -c '%%s %%n' * 2>/dev/null" % cdir,
                       timeout=30)
    files = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            name = parts[1].strip()
            if name and not name.endswith("/"):
                files[name] = int(parts[0])
    return files


def _stat_one(name):
    cdir = _cache_dir()
    if not cdir:
        return None
    code, out, _ = _su("stat -c '%%s' '%s/%s' 2>/dev/null" % (cdir, name),
                       timeout=20)
    out = out.strip()
    return int(out) if out.isdigit() else None


def _snapshot_growth(rounds=2, gap=3):
    files = _cache_list()
    growing = set()
    for _ in range(rounds):
        time.sleep(gap)
        now = _cache_list()
        for p, s in now.items():
            if s - files.get(p, 0) > 512 * 1024:
                growing.add(p)
        files = now
    return files, growing


def _parse_size_text(text):
    try:
        parts = str(text or "").strip().split()
        if len(parts) != 2:
            return 0
        val = float(parts[0])
        unit = parts[1].upper()
        mul = {"B": 1, "KB": 1024, "MB": 1048576,
               "GB": 1073741824, "TB": 1099511627776}.get(unit, 0)
        return int(val * mul) if mul else 0
    except Exception:
        return 0


def _load_map(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_map(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ────────────────────────── 等待下载完成 ──────────────────────────

def _find_cache_target(files, mapped, expect_bytes):
    """在缓存文件里找"这一集对应的文件"（下载中时先返回 mapped 或最大 temp，
    已下载时返回最匹配预期体积的正式文件）。"""
    if mapped and mapped in files:
        return mapped
    temps = [p for p in files if _is_cache_name(p) and p.endswith(".temp")]
    if temps:
        return max(temps, key=lambda p: files[p])
    dones = [p for p in files
             if _is_cache_name(p) and not p.endswith(".temp")
             and files[p] > _MIN_DONE]
    if not dones:
        return None
    if expect_bytes > 0:
        tol = max(int(expect_bytes * 0.05), 20 * 1048576)
        matched = [p for p in dones if abs(files[p] - expect_bytes) <= tol]
        if matched:
            return min(matched, key=lambda p: abs(files[p] - expect_bytes))
    return max(dones, key=lambda p: files[p])


def _wait_in_tg(target, expect_bytes, log, progress, deadline, cancel=None,
                max_bg_s=240):
    """留在 Telegram 前台等下载完成（UI dump 精准 + 文件系统兜底）。
    返回 (final_name, need_fg)：need_fg=True 表示超时需要切回 APP。"""
    final = target[:-5] if target.endswith(".temp") else target
    started = time.time()
    prev = 0
    stable = 0
    tick = 0
    while time.time() < deadline:
        if cancel is not None and cancel.is_set():
            log("[本机TG] 已取消")
            return None, False
        if time.time() - started > max_bg_s:
            log("[本机TG] 已在 Telegram 界面等待 %d 秒，切回应用继续等"
                % int(time.time() - started))
            return None, True
        time.sleep(4)
        tick += 1
        s_tmp = _stat_one(target) if final != target else None
        s_fin = _stat_one(final)
        size = s_tmp if s_tmp is not None else s_fin
        if size is None:
            continue
        if progress and size > prev:
            progress(size, (size - prev) / 4.0 / 1048576.0)
        if size == prev:
            stable += 1
        else:
            stable = 0
            if tick % 4 == 0:
                log("[本机TG] 已下载 %.2f MB" % (size / 1048576.0))

        renamed = (final != target) and s_tmp is None and s_fin is not None
        if renamed:
            log("[本机TG] 下载完成（.temp 已改名）：%s（%.2f MB）"
                % (final, size / 1048576.0))
            if progress:
                progress(size, 0)
            return final, False

        # UI dump 看进度环是否消失（每 ~12 秒一次）
        if tick % 3 == 0:
            ok_d, xml = _ui_dump()
            if ok_d:
                nodes = _ui_parse(xml)
                state, _ = _detect_tg_ui_state(nodes)
                if state == STATE_DONE:
                    log("[本机TG] 下载完成（UI 进度环消失）：%s（%.2f MB）"
                        % (final, size / 1048576.0))
                    if progress:
                        progress(size, 0)
                    return final, False

        if expect_bytes > 0:
            if size >= expect_bytes * 0.97 and stable >= 2:
                log("[本机TG] 下载完成：%s（%.2f MB / 预期 %.0f MB）"
                    % (final, size / 1048576.0, expect_bytes / 1048576.0))
                if progress:
                    progress(size, 0)
                return final, False
        elif size > _MIN_DONE and stable >= 3:
            log("[本机TG] 下载完成：%s（%.2f MB）" % (final, size / 1048576.0))
            if progress:
                progress(size, 0)
            return final, False

        if stable >= 60:
            log("[本机TG] 下载停滞（当前 %.2f MB），放弃" % (size / 1048576.0))
            return None, False
        prev = size
    return None, False


def _wait_in_app(target, expect_bytes, log, progress, deadline, cancel=None):
    """已切回本应用前台（不会被冻结），纯文件系统轮询兜底。"""
    final = target[:-5] if target.endswith(".temp") else target
    prev = 0
    stable = 0
    tick = 0
    while time.time() < deadline:
        if cancel is not None and cancel.is_set():
            log("[本机TG] 已取消")
            return None
        time.sleep(3)
        tick += 1
        s_tmp = _stat_one(target) if final != target else None
        s_fin = _stat_one(final)
        size = s_tmp if s_tmp is not None else s_fin
        if size is None:
            continue
        if progress and size > prev:
            progress(size, (size - prev) / 3.0 / 1048576.0)
        if size == prev:
            stable += 1
        else:
            stable = 0
            if tick % 10 == 0:
                log("[本机TG] 已下载 %.2f MB（APP 前台等待中）"
                    % (size / 1048576.0))

        renamed = (final != target) and s_tmp is None and s_fin is not None
        if renamed:
            log("[本机TG] 下载完成（.temp 已改名）：%s（%.2f MB）"
                % (final, size / 1048576.0))
            if progress:
                progress(size, 0)
            return final

        if expect_bytes > 0:
            if size >= expect_bytes * 0.97 and stable >= 3:
                log("[本机TG] 下载完成：%s（%.2f MB / 预期 %.0f MB）"
                    % (final, size / 1048576.0, expect_bytes / 1048576.0))
                if progress:
                    progress(size, 0)
                return final
        elif size > _MIN_DONE and stable >= 4:
            log("[本机TG] 下载完成：%s（%.2f MB）" % (final, size / 1048576.0))
            if progress:
                progress(size, 0)
            return final

        if stable >= 60:
            log("[本机TG] 下载停滞（当前 %.2f MB），放弃" % (size / 1048576.0))
            return None
        prev = size
    return None


# ────────────────────────── 主流程 ──────────────────────────

def download(channel, msg_id, dest_path, log=print, progress=None, cancel=None,
             timeout_min=90, size_text="", link=""):
    """在手机上打开消息 → UI dump 判状态 → 点下载按钮（bounds 中心）→
    留在 TG 前台等 UI 进度环消失 + .temp 改名 → 超时切回 APP 继续等 → 复制到 dest_path。

    返回 (dest_path, error)：成功 error 为空串；失败 (None, 原因)。
    """
    ok, pkg, detail = probe()
    if not ok:
        return None, detail
    log("[本机TG] 环境正常：%s" % detail)

    channel = str(channel or "").lstrip("@")
    msg_id = str(msg_id or "").strip()
    # 候选链接：服务端给的优先，私有频道去掉 -100 前缀重试，最后自己拼
    cands = []
    given = str(link or "").strip()
    if given.startswith("http") and "t.me" in given:
        cands.append(given)
        if "/c/-100" in given:
            cands.append(given.replace("/c/-100", "/c/"))
    if channel and msg_id:
        built = "https://t.me/%s/%s" % (channel, msg_id)
        if built not in cands:
            cands.append(built)
    if not cands:
        return None, "缺少消息链接"
    expect_bytes = _parse_size_text(size_text)
    dest_dir = os.path.dirname(dest_path) or "."
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass

    mpath = os.path.join(dest_dir, ".phone_tg_map.json")
    key = cands[0]
    mapping = _load_map(mpath)
    mapped = mapping.get(key)

    _su("svc power stayon true", timeout=20)
    _su("input keyevent 224", timeout=20)
    _su("wm dismiss-keyguard", timeout=20)
    time.sleep(0.5)

    target = None
    tapped = False
    # 最多 3 次尝试（deep link 可能 1-2 次就能定位到气泡）
    for attempt in range(3):
        lk = cands[min(attempt, len(cands) - 1)]
        log("[本机TG] 打开：%s" % lk)
        _su("am start -W --user 0 -a android.intent.action.VIEW -d '%s' -p %s"
            % (lk, pkg), timeout=40)
        # 给 TG 充足时间渲染 View 树
        time.sleep(4.5)

        if cancel is not None and cancel.is_set():
            _bring_app_front()
            return None, "已取消"

        ok_d, xml = _ui_dump()
        if not ok_d:
            log("[本机TG] UI dump 失败（%s），重试 %d/3" % (xml[:60], attempt + 1))
            time.sleep(1.5)
            continue
        nodes = _ui_parse(xml)
        ui_state, tap_node = _detect_tg_ui_state(nodes, log=log)

        # 文件系统辅助判断（UI 拿不准时兜底）
        files = _cache_list()
        growing = set()
        if ui_state == STATE_NOT_DOWNLOADED and not mapped:
            files, growing = _snapshot_growth(rounds=1, gap=2)

        # 兜底：UI 说 UNKNOWN 但缓存里其实有 mapped 目标
        if ui_state == STATE_UNKNOWN and mapped and mapped in files:
            ui_state = STATE_DONE
            target = mapped

        if cancel is not None and cancel.is_set():
            _bring_app_front()
            return None, "已取消"

        if ui_state == STATE_DONE:
            target = _find_cache_target(files, mapped, expect_bytes)
            log("[本机TG] 状态：已下载（%s）" % (target or "未找到文件"))
            break

        if ui_state == STATE_DOWNLOADING:
            if len(growing) == 1:
                target = list(growing)[0]
            else:
                target = _find_cache_target(files, mapped, expect_bytes)
            log("[本机TG] 状态：下载中（%s）" % (target or "未知"))
            break

        if ui_state == STATE_UNKNOWN:
            log("[本机TG] 状态无法识别（视频气泡未显示），重试 %d/3"
                % (attempt + 1))
            time.sleep(1.5)
            continue

        # 未下载 → 点击下载按钮（用 bounds 中心，零偏移）
        if tap_node:
            cx, cy = tap_node["cx"], tap_node["cy"]
            log("[本机TG] 状态：未下载 → 点击 (%d,%d)" % (cx, cy))
            _ui_tap(tap_node)
            tapped = True
            break
        # 兜底：_detect_tg_ui_state 因面积过滤没找到，但原始过滤能找到
        all_dl = _ui_filter(nodes, desc_kw=_DL_BTN, text_kw=_DL_BTN,
                            min_w=10, min_h=10)
        if all_dl:
            tap_node = all_dl[0]
            cx, cy = tap_node["cx"], tap_node["cy"]
            log("[本机TG] 状态：未下载（兜底）→ 点击 (%d,%d)" % (cx, cy))
            _ui_tap(tap_node)
            tapped = True
            break

    if tapped:
        # 确认点击真的触发了下载（看缓存增长 + UI 变下载中，最多 15 秒）
        for _ in range(15):
            if cancel is not None and cancel.is_set():
                _bring_app_front()
                return None, "已取消"
            time.sleep(1)
            now = _cache_list()
            new = [p for p in now if p not in files and _is_cache_name(p)]
            if new:
                big = [p for p in new if now[p] > 1048576]
                target = max(big or new, key=lambda p: now[p])
                break
            grew = [p for p in now
                    if _is_cache_name(p) and now[p] - files.get(p, 0) > 512 * 1024]
            if grew:
                target = max(grew, key=lambda p: now[p] - files.get(p, 0))
                break
            # UI dump 看看有没有变成下载中
            if _ % 5 == 0:
                ok_d, xml = _ui_dump()
                if ok_d:
                    nodes = _ui_parse(xml)
                    s2, _ = _detect_tg_ui_state(nodes)
                    if s2 == STATE_DOWNLOADING:
                        # UI 确认下载中，target 继续找
                        pass
        if target:
            log("[本机TG] 已开始下载：%s" % target)
        else:
            log("[本机TG] 点击后 15 秒内未确认下载（可能点击未命中或缓存目录不可读）")

    if not target:
        _bring_app_front()
        return None, "未能启动下载（视频未显示或点击无效）"

    if mapping.get(key) != target:
        mapping[key] = target
        _save_map(mpath, mapping)

    if expect_bytes:
        log("[本机TG] 等待下载完成：%s（预期 %.0f MB）"
            % (target, expect_bytes / 1048576.0))
    else:
        log("[本机TG] 等待下载完成：%s" % target)

    deadline = time.time() + timeout_min * 60

    # 阶段 A：留在 TG 前台等（UI dump + 文件系统，最多 4 分钟）
    final, need_fg = _wait_in_tg(target, expect_bytes, log, progress,
                                 deadline, cancel, max_bg_s=240)
    if final:
        _bring_app_front()
    elif need_fg:
        # 超时，切回 APP 继续文件系统轮询
        _bring_app_front()
        final = _wait_in_app(target, expect_bytes, log, progress,
                             deadline, cancel)
    else:
        _bring_app_front()
        return None, "等待下载完成超时"

    if not final:
        _bring_app_front()
        return None, "等待下载完成超时"

    # 确保在前台再做复制（避免后台复制被冻结）
    _bring_app_front()

    cdir = _cache_dir()
    src = "%s/%s" % (cdir, final)
    code, out, err = _su("cp -f '%s' '%s' && chmod 666 '%s'" % (src, dest_path, dest_path),
                         timeout=3600)
    if code != 0:
        return None, "复制文件失败：%s" % (err or out or "cp error")[:200]
    try:
        got = os.path.getsize(dest_path)
    except Exception:
        return None, "复制后文件不存在"
    if got <= 0:
        return None, "复制后文件为空"
    if expect_bytes and got < expect_bytes * 0.85:
        return None, "下载文件不完整（%.2f MB / 预期 %.0f MB）" % (
            got / 1048576.0, expect_bytes / 1048576.0)

    log("[本机TG] 已保存：%s（%.2f MB）"
        % (dest_path, os.path.getsize(dest_path) / 1048576.0))
    return dest_path, ""
