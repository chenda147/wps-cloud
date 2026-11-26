"""手机本机（无需 adb）用 root 模拟点击官方 Telegram 下载。

策略与 PC 端 client/phone_download.py 完全一致：
  打开 t.me/<channel>/<msg> → 判断视频气泡左上角有没有下载图标
    · 有图标 + 缓存里没有在增长的文件 → 未下载：点一次下载按钮
    · 有图标 + 唯一文件在增长        → 下载中：等待
    · 无图标                        → 已下载：直接从缓存取
  缓存文件（<dc>_<file_id>[.temp]）的大小判断进度，完成后用 root cp 复制到应用目录。

与 PC 版的区别：
  1) 所有命令在本机用 `su -c` 执行，不需要 adb；
  2) 截图用 raw screencap 并按字节直接解析（不依赖 PIL/Pillow，p4a 打包更稳）。
"""

import json
import os
import subprocess
import time

TG_PKGS = ("org.telegram.messenger.web", "org.telegram.messenger",
           "org.telegram.messenger.beta", "tw.nekomimi.nekogram")
_TG_ACTIVE = None   # 已探测到的官方 Telegram 包名

STATE_DONE = "done"
STATE_DOWNLOADING = "downloading"
STATE_NOT_DOWNLOADED = "not_downloaded"
STATE_UNKNOWN = "unknown"

# 缓存的视频最小体积（判断"已下载"时排除缩略图/小文件）
_MIN_DONE = 2 * 1048576

_SU_CANDIDATES = ("/system/bin/su", "/system/xbin/su", "/debug_ramdisk/su",
                  "/sbin/su", "su")
_su_path = None      # 探测成功后的 su 路径（失败不缓存，下次重新探测）
_last_err = ""


# ────────────────────────── root 命令执行 ──────────────────────────

def _clean_env():
    """p4a 进程会带 LD_LIBRARY_PATH / LD_PRELOAD 指向应用私有库，
    直接 exec 系统二进制可能因链接不上而失败，这里清掉再执行 su。"""
    env = dict(os.environ)
    for k in ("LD_PRELOAD", "LD_LIBRARY_PATH"):
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
            # 授权框在等用户点击，继续试其他路径只会重复弹框
            _last_err = "等待 root 授权超时（请在手机弹框里点“允许”）"
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
    """探测运行环境，返回 (可用, Telegram包名, 说明文字)。
    说明文字用于在客户端日志里直接显示问题原因。"""
    su = _find_su()
    if not su:
        return False, None, ("root 授权不可用：%s（首次会弹 root 授权框，"
                             "请在手机上点“允许”）" % (_last_err or "su 未找到"))
    pkg = _tg_pkg()
    if not pkg:
        return False, None, "未找到官方 Telegram（已查：%s）" % ",".join(TG_PKGS)
    return True, pkg, "root=%s，Telegram=%s" % (su, pkg)


def available():
    """本机模拟点击是否可用：有 root + 已安装官方 Telegram。"""
    return probe()[0]


def _foreground_line():
    """当前前台窗口信息（用于确认 Telegram 是否真的被拉起）。"""
    code, out, _ = _su("dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'",
                       timeout=25)
    if not out.strip():
        code, out, _ = _su("dumpsys activity activities 2>/dev/null | grep -E 'mResumedActivity|topResumedActivity'",
                           timeout=25)
    line = out.strip().splitlines()[0] if out.strip() else ""
    return line[:160]


def _self_pkg():
    """本应用包名（进程名一般就是包名，服务进程会带 :service 后缀）。"""
    try:
        with open("/proc/self/cmdline", "rb") as f:
            name = f.read().split(b"\0")[0].decode("utf-8", "ignore").strip()
        if name:
            return name.split(":")[0]
    except Exception:
        pass
    return "org.wps.transfer.wpstransfer"


def _bring_app_front():
    """把本应用切回前台。应用在后台会被 Android 冻结（cached app freezer），
    冻结后轮询/上传全部停摆，所以点击完 Telegram 必须马上切回来。"""
    pkg = _self_pkg()
    _su("am start -n %s/org.kivy.android.PythonActivity" % pkg, timeout=25)
    time.sleep(1.5)
    fg = _foreground_line()
    if pkg in fg:
        return True
    # 组件名不匹配时退回 LAUNCHER 入口
    _su("monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg, timeout=25)
    time.sleep(1.5)
    fg = _foreground_line()
    return pkg in fg


# ────────────────────────── 截图（raw）解析 ──────────────────────────

def _screencap(path):
    code, out, err = _su("screencap > '%s'" % path, timeout=40)
    try:
        return code == 0 and os.path.getsize(path) > 10000
    except Exception:
        return False


def _read_shot(path):
    """读取 raw screencap：返回 (data, w, h, bpp, off)；失败返回 None。

    raw 格式头部为 width/height/format（部分版本多 4 字节 colorSpace），
    像素紧接着按 4 字节/像素（RGBA_8888）。用文件总长反推头部长度，
    兼容 12/16 字节两种头部。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if len(data) < 16:
        return None
    w = int.from_bytes(data[0:4], "little")
    h = int.from_bytes(data[4:8], "little")
    if not (0 < w <= 10000 and 0 < h <= 10000):
        return None
    total = w * h
    for bpp in (4, 3):
        off = len(data) - total * bpp
        if off in (12, 16):
            return data, w, h, bpp, off
    return None


def _lum(data, i):
    return (data[i] * 299 + data[i + 1] * 587 + data[i + 2] * 114) // 1000


def _find_video_bubble(shot):
    """按大片深色区域估计视频气泡的 (left, top, right, bottom)（同 PC 版）。"""
    data, w, h, bpp, off = shot
    x_step = max(1, w // 135)
    y_step = max(1, h // 300)
    n_x = len(range(0, w, x_step))
    rows = []
    for y in range(int(h * 0.25), int(h * 0.8), y_step):
        base = off + y * w * bpp
        dark = 0
        for x in range(0, w, x_step):
            if _lum(data, base + x * bpp) < 40:
                dark += 1
        rows.append((y, dark))
    if not rows:
        return None
    max_dark = max(r[1] for r in rows)
    if max_dark < n_x * 0.5:
        return None
    ys = [y for y, d in rows if d > max_dark * 0.75]
    return 16, min(ys), w - 16, max(ys)


def _bright_pixels(shot, box, threshold=180):
    data, w, h, bpp, off = shot
    left, top, right, bottom = box
    left, top = max(0, left), max(0, top)
    right, bottom = min(w, right), min(h, bottom)
    cnt = 0
    for y in range(top, bottom, 2):
        base = off + y * w * bpp
        for x in range(left, right, 2):
            if _lum(data, base + x * bpp) >= threshold:
                cnt += 1
    return cnt


def _has_badge_and_box(shot):
    """返回 (气泡左上角是否有图标, 气泡 box)。None 表示无法判断。"""
    data, w, h, bpp, off = shot
    # 采样整体亮度，全黑/熄屏直接放弃
    total = 0
    n = 0
    for y in range(0, h, max(1, h // 16)):
        base = off + y * w * bpp
        for x in range(0, w, max(1, w // 16)):
            total += _lum(data, base + x * bpp)
            n += 1
    if n and total / float(n) < 25:
        return None, None
    box = _find_video_bubble(shot)
    if not box:
        return None, None
    left, top = box[0], box[1]
    badge_box = (left + 20, top + 15, left + 150, top + 120)
    samples = ((badge_box[2] - badge_box[0]) // 2) * ((badge_box[3] - badge_box[1]) // 2)
    return _bright_pixels(shot, badge_box) > max(12, int(samples * 0.004)), box


# ────────────────────────── Telegram 缓存探测 ──────────────────────────

def _cache_list():
    """返回 {文件名: 字节数}（Telegram 缓存目录）。"""
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
    """把服务端下发的 size_text（如 "2.1 GB" / "2064.0 MB"）解析为字节数近似值。"""
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


def _detect_state(has_badge, files, growing, mapped, expect_bytes=0):
    """返回 (state, target)。"""
    if mapped and mapped in files:
        if mapped.endswith(".temp"):
            return STATE_DOWNLOADING, mapped
        return STATE_DONE, mapped
    if has_badge is None:
        return STATE_UNKNOWN, None
    if not has_badge:
        done = [p for p, s in files.items()
                if not p.endswith(".temp") and s > _MIN_DONE]
        if not done:
            return STATE_UNKNOWN, None
        # 有预期大小时优先按体积匹配，避免同一聊天里多个已下载视频认错文件
        if expect_bytes > 0:
            tol = max(int(expect_bytes * 0.05), 20 * 1048576)
            matched = [p for p in done if abs(files[p] - expect_bytes) <= tol]
            if matched:
                return STATE_DONE, min(matched, key=lambda p: abs(files[p] - expect_bytes))
        return STATE_DONE, max(done, key=lambda p: files[p])
    if len(growing) == 1:
        return STATE_DOWNLOADING, list(growing)[0]
    return STATE_NOT_DOWNLOADED, None


def _wait_target(target, log, progress, deadline, cancel=None):
    """轮询缓存文件直到写完（.temp 消失），返回最终文件名或 None。"""
    final = target[:-5] if target.endswith(".temp") else target
    prev = 0
    stable = 0
    last_t = time.time()
    tick = 0
    while time.time() < deadline:
        if cancel is not None and cancel.is_set():
            log("[本机TG] 已取消")
            return None
        time.sleep(3)
        tick += 1
        size = _stat_one(final)
        if size is not None and size > 0:
            log("[本机TG] 下载完成：%s（%.2f MB）" % (final, size / 1048576.0))
            if progress:
                progress(size, 0)
            return final
        size = _stat_one(target)
        if size is None:
            continue
        now = time.time()
        if progress and size > prev:
            progress(size, (size - prev) / max(now - last_t, 0.1) / 1048576.0)
        last_t = now
        if size == prev:
            stable += 1
            if stable >= 60:  # 约 3 分钟无增长，认为下载已中断
                log("[本机TG] 下载停滞超过 3 分钟，放弃")
                return None
        else:
            stable = 0
            if tick % 5 == 0:
                log("[本机TG] 已下载 %.2f MB" % (size / 1048576.0))
        prev = size
    return None


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


# ────────────────────────── 主流程 ──────────────────────────

def download(channel, msg_id, dest_path, log=print, progress=None, cancel=None,
             timeout_min=90, size_text=""):
    """在手机上打开消息 → 按状态下载 → 复制到 dest_path。

    返回 (dest_path, error)：成功时 error 为空串；失败返回 (None, 原因)。
    progress(done_bytes, speed_mb_s) 为进度回调。
    """
    ok, pkg, detail = probe()
    if not ok:
        return None, detail
    cdir = _cache_dir()
    log("[本机TG] 环境正常：%s" % detail)

    channel = str(channel or "").lstrip("@")
    if not channel or not msg_id:
        return None, "缺少频道或消息 ID"
    expect_bytes = _parse_size_text(size_text)
    link = "https://t.me/%s/%s" % (channel, msg_id)
    dest_dir = os.path.dirname(dest_path) or "."
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass

    mpath = os.path.join(dest_dir, ".phone_tg_map.json")
    key = "%s/%s" % (channel, msg_id)
    mapping = _load_map(mpath)
    mapped = mapping.get(key)

    log("[本机TG] 打开：%s" % link)
    _su("svc power stayon true", timeout=20)
    _su("input keyevent 224", timeout=20)
    _su("wm dismiss-keyguard", timeout=20)
    time.sleep(0.5)
    start_cmd = ("am start -W --user 0 -a android.intent.action.VIEW "
                 "-d '%s' -p %s" % (link, pkg))

    shot_path = os.path.join(dest_dir, "_tg_shot.raw")
    target = None
    box = None
    tapped = False
    for attempt in range(2):
        _su(start_cmd, timeout=40)
        time.sleep(2.5)

        shot = None
        if _screencap(shot_path):
            shot = _read_shot(shot_path)
        else:
            log("[本机TG] 截图失败（root 授权或 screencap 不可用）")
        has_badge, box = (None, None)
        if shot:
            has_badge, box = _has_badge_and_box(shot)
        files = _cache_list()
        growing = set()
        if has_badge and not mapped:
            # 只有"左上角有图标"时才需要采样增长（2 秒），用来区分 下载中/未下载
            files, growing = _snapshot_growth(rounds=1, gap=2)
        state, target = _detect_state(has_badge, files, growing, mapped, expect_bytes)
        if cancel is not None and cancel.is_set():
            _bring_app_front()
            return None, "已取消"

        if state == STATE_DONE:
            log("[本机TG] 状态：已下载（%s）" % target)
            break
        if state == STATE_DOWNLOADING:
            log("[本机TG] 状态：下载中（%s）" % target)
            break
        if state == STATE_UNKNOWN:
            log("[本机TG] 状态无法识别（视频未显示或屏幕未亮），重试 %d/2" % (attempt + 1))
            time.sleep(1)
            continue

        # 未下载：只点一次视频气泡左上角的下载按钮（不重试连点，避免暂停下载）
        log("[本机TG] 状态：未下载 → 点击一次下载按钮")
        left, top = box[0], box[1]
        _su("input tap %d %d" % (left + 70, top + 65), timeout=20)
        log("[本机TG] 已点击 (%d,%d)，切回本应用继续轮询进度"
            % (left + 70, top + 65))
        tapped = True
        break

    # 立刻切回本应用：应用在后台会被系统冻结，导致整个下载/上传流程卡死；
    # 切回后 Telegram 仍在后台继续下载，我们只通过 su 读缓存文件大小来判断进度
    _bring_app_front()
    try:
        os.remove(shot_path)
    except Exception:
        pass

    if tapped:
        # 确认点击是否真的触发了下载（10 秒内缓存必须出现新文件或在增长）
        for _ in range(10):
            if cancel is not None and cancel.is_set():
                return None, "已取消"
            time.sleep(1)
            now = _cache_list()
            new = [p for p in now if p not in files]
            if new:
                target = max(new, key=lambda p: now[p])
                break
            grew = [p for p in now if now[p] - files.get(p, 0) > 512 * 1024]
            if grew:
                target = max(grew, key=lambda p: now[p] - files.get(p, 0))
                break
        if target:
            log("[本机TG] 已开始下载：%s" % target)
        else:
            log("[本机TG] 点击后 10 秒内缓存未增长（可能点击未命中）")

    if not target:
        return None, "未能启动下载（视频未显示或点击无效）"

    if mapping.get(key) != target:
        mapping[key] = target
        _save_map(mpath, mapping)

    final = _wait_target(target, log, progress, time.time() + timeout_min * 60, cancel)
    if not final:
        return None, "等待下载完成超时"

    src = "%s/%s" % (cdir, final)
    code, out, err = _su("cp -f '%s' '%s' && chmod 666 '%s'" % (src, dest_path, dest_path),
                         timeout=3600)
    if code != 0:
        return None, "复制文件失败：%s" % (err or out or "cp error")[:200]
    try:
        if os.path.getsize(dest_path) <= 0:
            return None, "复制后文件为空"
    except Exception:
        return None, "复制后文件不存在"

    log("[本机TG] 已保存：%s（%.2f MB）"
        % (dest_path, os.path.getsize(dest_path) / 1048576.0))
    return dest_path, ""
