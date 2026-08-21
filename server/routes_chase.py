# -*- coding: utf-8 -*-
"""server/routes_chase.py - 追更系统/预览/客户端上报
"""
from flask import Blueprint, request, jsonify, session, redirect, url_for, Response, send_file, stream_with_context
import re
import os
import time
import uuid
import logging
from datetime import datetime
from .state import *
from .utils import *
from .models import WPSClient, API_BASE
from .helpers import *

bp = Blueprint("chase", __name__)

# 目录搜索结果短时缓存：关键词 -> (时间戳, 结果列表)
_DIR_SEARCH_CACHE = {}

def _compute_next_check(schedule, last_check):
    """根据 schedule 与上次检查时间，推算下次检查时间（ISO 字符串），用于前端倒计时。
    支持：DAILY:HH:MM / HOURLY:MI / INTERVAL:秒 / 其它(含 cron)->回退 None。
    """
    now = datetime.now()
    sched = (schedule or "").strip().upper()
    base = None
    if last_check:
        try:
            base = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
        except Exception:
            base = None
    if base is None:
        base = now
    try:
        if sched.startswith("DAILY:"):
            hh, mm = sched.split(":", 1)[1].split(":")
            hh, mm = int(hh), int(mm)
            nxt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                from datetime import timedelta
                nxt += timedelta(days=1)
            return nxt.strftime("%Y-%m-%d %H:%M:%S")
        if sched.startswith("HOURLY:"):
            mi = int(sched.split(":", 1)[1])
            from datetime import timedelta
            nxt = base.replace(minute=mi % 60, second=0, microsecond=0) + timedelta(hours=1)
            return nxt.strftime("%Y-%m-%d %H:%M:%S")
        if sched.startswith("INTERVAL:"):
            secs = int(sched.split(":", 1)[1])
            from datetime import timedelta
            nxt = base + timedelta(seconds=secs)
            return nxt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def _broadcast_chase_cancel(chase_id, client_name="", cancel_running=False):
    """向目标客户端（或全部在线客户端）下发取消指令，并清理队列中相关 pending 任务。
    cancel_running=True 时同时把 running 中的同追更任务置为 cancelled（避免只清
    pending 后，正在下载的任务仍继续、并被续扫/调度重新派发造成死循环）。
    取消指令带时间戳存留：poll 下发后不删除，60秒内重复下发（客户端幂等处理），
    防止 poll 响应丢失导致取消指令一次性丢失、任务继续下载/上传。"""
    client_name = (client_name or "").strip()
    if not client_name:
        # 没有指定客户端，尝试发给所有在线客户端
        for cn in list(_CLIENT_REGISTRY.keys()):
            _CANCEL_REQUESTS.setdefault(cn, {})[chase_id] = time.time()
    else:
        _CANCEL_REQUESTS.setdefault(client_name, {})[chase_id] = time.time()
    # 同时取消队列中的相关任务
    for tid in list(_CLIENT_QUEUE):
        t = _CLIENT_TASKS.get(tid, {})
        if t.get("chase_id") != chase_id:
            continue
        if t.get("status") == "pending":
            t["status"] = "cancelled"
            if tid in _CLIENT_QUEUE:
                _CLIENT_QUEUE.remove(tid)
        elif cancel_running and t.get("status") == "running":
            t["status"] = "cancelled"


def _send_chase_resume_signals(chase_id, chase):
    """清除该追更的取消指令/取消集记录，并向（可能收到过取消指令的）客户端下发恢复信号。

    与停止时的「广播取消」对称：未指定 target_client 且无取消记录（如服务端重启后
    状态丢失）时，广播给所有在线客户端。否则客户端本地取消事件（60秒窗口）残留，
    重新运行后新任务会被客户端误取消，导致流水线断在某一集。"""
    if not chase_id:
        return
    _resume_clients = set()
    for _cn in list(_CANCEL_REQUESTS.keys()):
        if chase_id in _CANCEL_REQUESTS.get(_cn, {}):
            del _CANCEL_REQUESTS[_cn][chase_id]
            _resume_clients.add(_cn)
            if not _CANCEL_REQUESTS[_cn]:
                del _CANCEL_REQUESTS[_cn]
    _tc = (chase.get("target_client") or "").strip()
    if _tc:
        _resume_clients.add(_tc)
    elif not _resume_clients:
        _resume_clients.update(_CLIENT_REGISTRY.keys())
    for _cn in _resume_clients:
        _RESUME_REQUESTS.setdefault(_cn, set()).add(chase_id)
    # 清除"最近被取消"的集记录：重新运行后允许重新补建下载
    _CANCELED_EPS.pop(chase_id, None)
    # 派发冷却 10 秒：给客户端一个 poll 周期接收「恢复指令」，清除本地取消状态
    _CHASE_RESUME_COOLDOWN[chase_id] = time.time() + 10


@bp.route("/api/admin/chase/<chase_id>/stop", methods=["POST"])
@login_required
def api_admin_chase_stop(chase_id):
    """停止追更任务：完全停止（无恢复），通知客户端取消下载并停止自动调度/续扫。

    仅广播取消会导致死循环（服务端调度器/自动续扫仍按 enabled 持续派发
    新的检查与下载任务，客户端取消后又收到新任务再取消）。因此停止必须
    把追更置为 stopped，使调度器与自动续扫跳过它，从源头止住重复派发。
    停止是终态：用户需通过"立即运行"或重新启用才能再次触发。
    """
    tasks = _load_chase()
    chase = next((t for t in tasks if (t.get("id") or t.get("chase_id")) == chase_id), None)
    if not chase:
        return jsonify({"success": False, "message": "追更任务不存在"}), 404
    client_name = (chase.get("target_client") or "").strip()
    chase["stopped"] = True
    _save_chase(tasks)
    _broadcast_chase_cancel(chase_id, client_name, cancel_running=True)
    return jsonify({"success": True, "message": f"已完全停止追更并下发停止指令{'到' + client_name if client_name else ''}"})


@bp.route("/api/admin/chase/<chase_id>/reset", methods=["POST"])
@login_required
def api_admin_chase_reset(chase_id):
    """清除"已停止"标记，使追更重新可以调度/续扫/手动运行（不自动触发检查）。"""
    tasks = _load_chase()
    chase = next((t for t in tasks if (t.get("id") or t.get("chase_id")) == chase_id), None)
    if not chase:
        return jsonify({"success": False, "message": "追更任务不存在"}), 404
    chase["stopped"] = False
    _save_chase(tasks)
    # ── 清除该追更的取消指令并下发"恢复"信号：防止客户端取消窗口(60s)内
    # 重新运行后被误取消（停止时客户端已 set 本地取消事件，必须显式恢复）──
    _send_chase_resume_signals(chase_id, chase)
    logging.getLogger("routes_chase").info(
        f"[追更重置] {chase.get('name', chase_id)} 冷却至 "
        f"{time.strftime('%H:%M:%S', time.localtime(time.time() + 10))}，"
        f"10秒内不派发任务")
    return jsonify({"success": True, "message": f"已清除停止标记（可重新运行）：{chase.get('name', chase_id)}"})


@bp.route("/api/admin/chase/<chase_id>/retry", methods=["POST"])
@login_required
def api_admin_chase_retry(chase_id):
    """失败/缺失重试：清除连续失败计数并立即触发一次检查（针对缺失集补漏）。"""
    tasks = _load_chase()
    chase = next((t for t in tasks if (t.get("id") or t.get("chase_id")) == chase_id), None)
    if not chase:
        return jsonify({"success": False, "message": "追更任务不存在"}), 404
    if chase.get("stopped"):
        return jsonify({"success": False, "message": "任务已完全停止，请先重置"}), 400
    chase["consecutive_failures"] = 0
    chase["last_error_type"] = ""
    chase["last_error"] = ""
    _save_chase(tasks)
    # 重试视为恢复：清除客户端取消状态残留（幂等）
    _send_chase_resume_signals(chase_id, chase)
    # 复用检查派发逻辑
    try:
        _create_chase_check_task(chase, manual=True)
        ok = True
    except Exception as e:
        ok = False
        return jsonify({"success": False, "message": f"重试派发失败：{e}"}), 500
    name = chase.get("name", chase_id)
    return jsonify({"success": True, "message": f"已清除失败计数并触发检查：{name}"})


def _auto_progress_ep_from_wps(cli, wps_path):
    """「已追至」留空时：取 WPS 目标目录（含 Season N / SN 子目录）已存最大集数。
    找不到任何集数文件返回 0（任务从第 0 集起正常追更）。"""
    num_re = re.compile(r'[Ee](\d+)|第\s*(\d+)\s*集|(\d+)\s*集')
    max_ep = 0

    def _scan(files):
        _m = 0
        for f in files:
            nm = (f.get("fname") or f.get("name") or "").strip()
            mm = num_re.search(nm)
            if mm:
                _m = max(_m, int(mm.group(1) or mm.group(2) or mm.group(3)))
        return _m

    if not cli or not wps_path:
        return 0
    try:
        folder_id = _resolve_folder_id_with_client(cli, wps_path)
    except Exception:
        return 0
    if folder_id is None:
        return 0
    try:
        root_files = cli.get_all_files(DEFAULT_GROUP_ID, folder_id)
    except Exception:
        return 0
    max_ep = max(max_ep, _scan(root_files))
    # 集文件常放在季子目录里，顺带扫描一层
    for f in root_files:
        nm = (f.get("fname") or f.get("name") or "").strip()
        if re.match(r'^[Ss]eason\s*\d+$', nm) or re.match(r'^[Ss]\d+$', nm):
            try:
                sub_id = _resolve_folder_id_with_client(cli, f"{wps_path}/{nm}")
                if sub_id is not None:
                    max_ep = max(max_ep, _scan(cli.get_all_files(DEFAULT_GROUP_ID, sub_id)))
            except Exception:
                pass
    return max_ep


def _query_wps_missing(cli, wps_path, start_ep=1, season_hint=0, upper_hint=0):
    """按 WPS 目录反查缺失集（仅对比 WPS 已存，不依赖分享源）。
    支持全季扫描：season_hint<=0 时自动探测根目录 + 所有 Season N 子目录，
    返回 seasons 数组（每季一组缺失），同时保留顶层兼容字段（合并汇总）。
    返回 (result_dict, None) 或 (None, error_str)。
    """
    if not cli:
        return None, "未配置 WPS 客户端"
    if not wps_path:
        return None, "尚无法确定 WPS 目标路径"

    # 通用集数提取正则
    num_re = re.compile(r'[Ee](\d+)|第\s*(\d+)\s*集|(\d+)\s*集')

    def extract_eps(names):
        eps = set()
        for name in names:
            mm = num_re.search(name or "")
            if mm:
                eps.add(int(mm.group(1) or mm.group(2) or mm.group(3)))
        return sorted(eps)

    def _fname(f):
        return (f.get("fname") or f.get("name") or "").strip()

    start_ep = max(1, int(start_ep))
    upper_hint = int(upper_hint or 0)
    share_note = "仅对比 WPS 已存（不依赖分享源）"
    season_hint = int(season_hint or 0)

    # —— 1) 拉取 WPS 根目录（目标路径）文件与子目录 ——
    try:
        folder_id = _resolve_folder_id_with_client(cli, wps_path)
    except Exception as e:
        return None, f"解析 WPS 路径失败：{e}"
    root_files = []
    if folder_id is not None:
        try:
            root_files = cli.get_all_files(DEFAULT_GROUP_ID, folder_id)
        except Exception as e:
            return None, f"列举 WPS 文件失败：{e}"
    root_eps = extract_eps([_fname(f) for f in root_files])

    # 识别根目录下的 Season N / SN 子目录
    sub_dirs = []          # (season_num, folder_name)
    seen = set()
    for f in root_files:
        nm = _fname(f)
        mm = re.match(r'^[Ss]eason\s*(\d+)$', nm) or re.match(r'^[Ss](\d+)$', nm)
        if mm:
            s = int(mm.group(1))
            if s not in seen:
                seen.add(s)
                sub_dirs.append((s, nm))
    sub_dirs.sort(key=lambda x: x[0])

    # 指定季时，把目标季子目录放最前（兼容旧行为：优先探测目标季）
    if season_hint > 0:
        sub_dirs = [(s, nm) for s, nm in sub_dirs if s == season_hint] + \
                   [(s, nm) for s, nm in sub_dirs if s != season_hint]

    # —— 2) 逐目录构建每季的缺失结果 ——
    def build_season_result(season, path, files, use_upper_hint=True):
        eps = extract_eps([_fname(f) for f in files])
        # 只要 WPS 有已存集数文件，就一律以 WPS 实际最大集为应追上限。
        # 不能用 progress_ep（「已追至」）当上限：它会被下载进度持续推高
        # （如 WPS 只有 20 集、下载已到 50 集时，total_known 会被虚报成 50），
        # 与 WPS 实际不符。progress_ep 仅在 WPS 目录无已存集数时兜底推断缺失范围。
        if eps:
            upper = eps[-1]
        else:
            upper = max(upper_hint, 0)
        if not eps:
            missing = [e for e in range(start_ep, upper + 1)] if upper >= start_ep else []
        else:
            present_set = set(eps)
            if upper < start_ep:
                upper = start_ep
            missing = [e for e in range(start_ep, int(upper) + 1) if e not in present_set]
        return {
            "season": season,
            "scanned_path": path,
            "present_eps": eps,
            "missing_eps": missing,
            "total_known": int(upper),
            "file_count": len(files),
        }

    seasons_result = []
    # 根目录有集数文件 → 作为「根目录/不分季」一组（仅在无子目录时使用，避免重复统计）
    # 注意：若根目录本身存放集数文件（单季不分目录），且无 Season 子目录，直接视为第 0 组。
    # 若有 Season 子目录，根目录通常只放说明文件，此时不再把根目录算作一组。
    if sub_dirs:
        # 只扫描各 Season 子目录
        for s, nm in sub_dirs:
            sub_path = f"{wps_path}/{nm}"
            try:
                sub_id = _resolve_folder_id_with_client(cli, sub_path)
            except Exception:
                sub_id = None
            if sub_id is None:
                continue
            try:
                sub_files = cli.get_all_files(DEFAULT_GROUP_ID, sub_id)
            except Exception:
                continue
            seasons_result.append(build_season_result(s, sub_path, sub_files, use_upper_hint=False))
        # 子目录均无有效集数时，回退用根目录（用进度兜底）
        if not seasons_result:
            seasons_result.append(build_season_result(season_hint, wps_path, root_files))
    else:
        # 无 Season 子目录：根目录即目标季目录
        seasons_result.append(build_season_result(season_hint, wps_path, root_files))

    # —— 3) 汇总 ——
    all_present = []
    all_missing = []
    total_known = 0
    total_files = 0
    for r in seasons_result:
        all_present.extend(r["present_eps"])
        all_missing.extend(r["missing_eps"])
        total_known += r["total_known"]
        total_files += r["file_count"]

    # 完全无集数信息（也无已追上限）→ 空提示
    if not all_present and total_known <= 0:
        return {
            "success": True,
            "wps_path": wps_path,
            "scanned_path": wps_path,
            "present_eps": [],
            "missing_eps": [],
            "total_known": 0,
            "start_ep": start_ep,
            "file_count": total_files,
            "share_note": share_note,
            "seasons": seasons_result,
            "empty_hint": "WPS 目录为空或未检测到已存集数文件（文件名需含 E01 / 第1集 等）。"
                          "请确认路径是否正确，或在「已追至」填写已知集数上限后重试。",
        }, None

    multi_season = len(seasons_result) > 1
    scanned_path = wps_path
    if len(seasons_result) == 1:
        scanned_path = seasons_result[0]["scanned_path"]

    return {
        "success": True,
        "wps_path": wps_path,
        "scanned_path": scanned_path,
        "present_eps": all_present,
        "missing_eps": all_missing,
        "total_known": total_known,
        "start_ep": start_ep,
        "file_count": total_files,
        "share_note": share_note,
        "seasons": seasons_result,
        "multi_season": multi_season,
    }, None


@bp.route("/api/admin/chase/wps_status_by_path", methods=["GET"])
@login_required
def api_admin_chase_wps_status_by_path():
    """按 WPS 路径直接反查缺失（无需先保存追更任务）。
    查询参数: wps_path(必填), start_ep(可选,默认1), season(可选,季提示),
              progress_ep(可选,追至上限), chase_id(可选,写回 wps_eps 校准)
    """
    wps_path = (request.args.get("wps_path") or "").strip()
    cli, _ = get_wps_client()
    start_ep = request.args.get("start_ep", 1, type=int)
    season_hint = request.args.get("season", 0, type=int)
    upper_hint = request.args.get("progress_ep", 0, type=int)
    result, err = _query_wps_missing(cli, wps_path, start_ep, season_hint, upper_hint)
    if err:
        return jsonify({"success": False, "message": err}), 500
    chase_id = (request.args.get("chase_id") or "").strip()
    if chase_id and result:
        # 查缺失结果 = WPS 实际文件快照，写回任务的 wps_eps（替换而非累积）。
        # 这样卡片「已传」与漏集判定都以 WPS 实际为准，不受 done_eps 虚高
        # （上传路径异常时记录在案但 WPS 实际无文件）影响。
        _present = result.get("present_eps") or []
        try:
            with _CHASE_FILE_LOCK:
                _tasks2 = _load_chase()
                for _t2 in _tasks2:
                    if _t2.get("id") == chase_id:
                        _present_set = set(int(x) for x in _present)
                        _t2["wps_eps"] = sorted(_present_set)
                        # ★ done_eps 虚高校准：done_eps 是客户端历史确认（可能上传
                        # 路径异常/文件被移走删除，记录在案但 WPS 实际无文件）。
                        # 查缺失=WPS 实际快照，剔除 done_eps 中 WPS 实际不存在的集，
                        # 否则主流程 check 的并集判定(wps_eps|done_eps)会永远挡住
                        # 这些集的补追，表现为"缺失的集(如5-30)不派发，只派最后一集"。
                        _de = set(int(x) for x in (_t2.get("done_eps") or []))
                        _de &= _present_set
                        _t2["done_eps"] = sorted(_de)
                        _save_chase(_tasks2)
                        break
        except Exception:
            pass
    return jsonify(result)


@bp.route("/api/admin/chase/<chase_id>/wps_status", methods=["GET"])
@login_required
def api_admin_chase_wps_status(chase_id):
    """反查缺失：仅以 WPS 已存集数为准（不依赖分享源）。
    支持从指定集数 start_ep 起对比；新建任务未运行时也可按字段计算路径直接反查。
    """
    tasks = _load_chase()
    chase = next((t for t in tasks if (t.get("id") or t.get("chase_id")) == chase_id), None)
    if not chase:
        return jsonify({"success": False, "message": "追更任务不存在"}), 404
    wps_path = (chase.get("wps_path") or "").strip()
    if not wps_path:
        # 尚无持久化路径时，按任务字段计算（新建任务未运行时也可直接反查）
        wps_path = _calc_chase_wps_path(chase)
    cli, _ = get_wps_client()
    start_ep = request.args.get("start_ep", 1, type=int)
    # season 参数优先（编辑弹窗里用户可能刚改了季数还没保存），否则用任务存值，再兜底 ep_pattern
    season_arg = request.args.get("season", None)
    if season_arg is not None and season_arg != "":
        try:
            season_hint = int(season_arg or 0)
        except (ValueError, TypeError):
            season_hint = 0
        # 表单季数与保存路径里的季数不一致时，按表单季数实时重算扫描路径（无需先保存）
        if _path_season(wps_path) != season_hint:
            wps_path = _with_season(wps_path, season_hint)
    else:
        season_hint = int(chase.get("season") or 0)
        ep_pattern = chase.get("ep_pattern", "")
        if ep_pattern:
            sp = re.search(r'[Ss](\d+)', ep_pattern)
            if sp:
                try:
                    season_hint = int(sp.group(1))
                except Exception:
                    pass
    result, err = _query_wps_missing(cli, wps_path, start_ep, season_hint,
                                     int(chase.get("progress_ep") or 0))
    if err:
        return jsonify({"success": False, "message": err}), 500
    return jsonify(result)


@bp.route("/api/admin/chase/stop_all", methods=["POST"])
@login_required
def api_admin_chase_stop_all():
    """一键停止：完全停止所有追更任务 + 向客户端下发取消指令，清理排队任务"""
    tasks = _load_chase()
    stopped = []
    for t in tasks:
        cid = t.get("id")
        if not cid:
            continue
        t["stopped"] = True
        _broadcast_chase_cancel(cid, (t.get("target_client") or ""), cancel_running=True)
        stopped.append(t.get("name", cid))
    if stopped:
        _save_chase(tasks)
    return jsonify({"success": True, "message": f"已完全停止 {len(stopped)} 个追更任务并下发停止指令",
                    "stopped": stopped})

def _path_season(p):
    """从 WPS 路径中解析出季数（/Season N 或 /S2 结尾式），无季返回 0"""
    if not p:
        return 0
    m = re.search(r'/Season\s+(\d+)', p, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    m = re.search(r'/(?:S)(\d{1,2})(?:$|/)', p, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def _with_season(p, season):
    """按目标季数重算路径：替换 /Season N 或在末尾追加；season<=0 时移除季目录"""
    if not p:
        return p
    if season <= 0:
        return re.sub(r'/Season\s+\d+', '', p, flags=re.IGNORECASE)
    if re.search(r'/Season\s+\d+', p, re.IGNORECASE):
        return re.sub(r'/Season\s+\d+', f'/Season {season}', p, flags=re.IGNORECASE)
    return f"{p.rstrip('/')}/Season {season}"


def _calc_chase_wps_path(chase):
    """按追更任务字段计算 WPS 目标路径：{类型}/{剧名文件夹}[/Season N]。
    与 helpers.py 中检查/下载任务的路径生成逻辑保持一致。
    """
    season = int(chase.get("season", 0) or 0)
    ep_pattern = chase.get("ep_pattern", "") or ""
    if ep_pattern:
        sp = re.search(r'[Ss](\d+)', ep_pattern)
        if sp:
            try:
                season = int(sp.group(1))
            except Exception:
                pass
    show_folder = (chase.get("show_folder") or "").strip() or (chase.get("name") or "")
    category = (chase.get("category") or "国漫").strip()
    base = f"{category}/{show_folder}" if show_folder else category
    return f"{base}/Season {season}" if season > 0 else base


@bp.route("/api/admin/chase/search_wps_dirs", methods=["GET"])
@login_required
def api_admin_chase_search_wps_dirs():
    """按关键词搜索 WPS 云盘中的同名文件夹，返回 {path, id} 列表。
    用于「添加追更」时输入片名 → 下拉选择已有文件夹 → 自动回填季/年份/片名/类型。
    结果按关键词短时缓存（30 秒），避免重复全盘扫描。
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "message": "请输入片名关键词"}), 400
    now = time.time()
    cached = _DIR_SEARCH_CACHE.get(q)
    if cached and now - cached[0] < 30:
        return jsonify({"success": True, "dirs": cached[1]})
    cli, err = get_wps_client()
    if err:
        return jsonify({"success": False, "message": err}), 400
    results = []
    def _walk(parent_id, path, depth):
        if depth > 5 or len(results) >= 50:
            return
        try:
            files = cli.get_all_files(DEFAULT_GROUP_ID, parent_id)
        except Exception:
            return
        for f in files:
            name = (f.get("fname") or f.get("name") or "").strip()
            if not name:
                continue
            ftype = f.get("ftype") or "file"
            cur = f"{path}/{name}" if path else name
            if ftype == "folder":
                if q.lower() in name.lower():
                    results.append({"path": cur, "id": str(f.get("id", ""))})
                _walk(f.get("id"), cur, depth + 1)
    _walk(None, "", 0)
    if len(_DIR_SEARCH_CACHE) > 200:
        _DIR_SEARCH_CACHE.clear()
    _DIR_SEARCH_CACHE[q] = (time.time(), results)
    return jsonify({"success": True, "dirs": results})


@bp.route("/api/admin/chase", methods=["GET"])
@login_required
def api_admin_chase_list():
    tasks = _load_chase()
    # 统计每个追更任务当前是否有进行中的客户端任务（pending/running）
    active_chase = {}
    for tid in list(_CLIENT_QUEUE):
        task = _CLIENT_TASKS.get(tid)
        if task and task.get("status") in ("pending", "running"):
            cid = task.get("chase_id")
            if cid:
                active_chase[cid] = True
    for t in tasks:
        t.setdefault("enabled", True)
        # 兼容旧数据：曾经的 paused 字段迁移为 stopped（完全停止，无恢复）
        if t.get("paused") is True and t.get("stopped") is not True:
            t["stopped"] = True
        t.setdefault("stopped", False)
        t.setdefault("base_ep", 0)
        t.setdefault("season", 0)
        t.setdefault("ep_pattern", "")
        t.setdefault("rename_template", "")
        t.setdefault("show_folder", "")
        t.setdefault("category", "国漫")
        t.setdefault("target_client", "")
        t.setdefault("sub_path", "")
        t.setdefault("schedule", "DAILY:01:00")
        t.setdefault("year", "")
        t.setdefault("last_check", "")
        t.setdefault("last_result", "")
        t.setdefault("history", [])  # 结构化检查/下载历史
        t.setdefault("episodes_filter", "")  # 指定集数筛选，如 "1,2,3"
        t.setdefault("created_at", "")
        # ── 失败预警字段 ──
        t.setdefault("last_success_ts", "")
        t.setdefault("last_fail_ts", "")
        t.setdefault("consecutive_failures", 0)
        t.setdefault("last_error_type", "")
        t.setdefault("last_error", "")
        t.setdefault("downloaded_eps", [])  # 已下载到本地的集（与 done_eps=已上传到WPS 区分）
        # 下次检查时间（用于前端倒计时）：由 schedule + 上次检查时间推算
        t["next_check"] = _compute_next_check(t.get("schedule", ""), t.get("last_check", ""))
        # 预警级别：error=红色(连续失败或Cookie问题) / warn=黄色(有失败记录) / normal
        _cf = t.get("consecutive_failures", 0)
        _le = t.get("last_error_type", "")
        if _cf >= 3 or _le in ("cookie_invalid", "share_invalid"):
            t["warn_level"] = "error"
        elif _cf > 0 or _le:
            t["warn_level"] = "warn"
        else:
            t["warn_level"] = "normal"
        # active：队列中有该追更的进行中任务，或服务端检查线程正在执行（检查中视为运行中）
        t["active"] = bool(active_chase.get(t.get("id"))) or bool(_SVR_CHECK_RUNNING.get(t.get("id")))
    return jsonify({"tasks": tasks})

@bp.route("/api/admin/chase", methods=["POST"])
@login_required
def api_admin_chase_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "片名必填"}), 400
    with _CHASE_FILE_LOCK:
        tasks = _load_chase()
        cid = uuid.uuid4().hex[:8]
        category = data.get("category", "国漫").strip()
        rename_template = data.get("rename_template", name + "E{ep}").strip()
        # 电影类型无季集，清洗模板中的季集部分
        if "电影" in category:
            import re as _re
            rename_template = _re.sub(r'\s*-\s*S\{season:0*2d\}E\{ep:0*2d\}', '', rename_template)
            rename_template = _re.sub(r'\s*-\s*E\{ep:0*2d\}', '', rename_template)
            rename_template = _re.sub(r'\s*-\s*第\{ep:0*2d\}[话集回]', '', rename_template)
            rename_template = _re.sub(r'\s*S\{season:0*2d\}E\{ep:0*2d\}', '', rename_template)
            rename_template = _re.sub(r'\s*E\{ep:0*2d\}', '', rename_template)
            rename_template = rename_template.strip()
        tasks.append({
            "id": cid,
            "name": name,
            "share_url": data.get("share_url", "").strip(),
            "share_passwd": data.get("share_passwd", "").strip(),
            "base_ep": int(data.get("base_ep", 0)),
            "season": int(data.get("season", 1)),
            "ep_pattern": data.get("ep_pattern", "").strip(),
            "rename_template": rename_template,
            "show_folder": data.get("show_folder", name).strip(),
            "category": category,
            "tmdb_id": data.get("tmdb_id", "").strip(),
            "target_client": data.get("target_client", "").strip(),
            "sub_path": data.get("sub_path", "").strip(),
            "year": data.get("year", "").strip(),
            "schedule": data.get("schedule", "DAILY:01:00").strip(),
            "episodes_filter": data.get("episodes_filter", "").strip(),
            "enabled": True,
            "last_check": "",
            "last_result": "",
            "history": [],
            "last_success_ts": "",
            "last_fail_ts": "",
            "consecutive_failures": 0,
            "last_error_type": "",
            "last_error": "",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        tasks[-1]["wps_path"] = _calc_chase_wps_path(tasks[-1])
        _save_chase(tasks)
        return jsonify({"success": True, "message": "追更任务已创建", "id": cid})

@bp.route("/api/admin/chase/<cid>", methods=["PUT"])
@login_required
def api_admin_chase_update(cid):
    data = request.get_json() or {}
    auto_ctx = {}  # 「已追至」留空时需要自动取 WPS 最大集的上下文
    with _CHASE_FILE_LOCK:
        tasks = _load_chase()
        for t in tasks:
            if t.get("id") == cid:
                for k in ("name", "share_url", "share_passwd", "ep_pattern",
                          "rename_template", "show_folder", "category", "tmdb_id",
                          "target_client", "sub_path", "year", "schedule",
                          "episodes_filter"):
                    if k in data and data[k] is not None:
                        t[k] = data[k].strip() if isinstance(data[k], str) else data[k]
                if "base_ep" in data:
                    t["base_ep"] = int(data["base_ep"])
                if "progress_ep" in data and data["progress_ep"] is not None:
                    t["progress_ep"] = int(data["progress_ep"])
                if "season" in data:
                    t["season"] = int(data["season"])
                if "enabled" in data:
                    t["enabled"] = bool(data["enabled"])
                t["wps_path"] = _calc_chase_wps_path(t)
                if "progress_ep" in data and data["progress_ep"] is None:
                    # 已追至留空 → 自动取 WPS 目标目录已存最大集数（锁外查询，避免持锁网络请求）
                    auto_ctx["wps_path"] = t["wps_path"]
                else:
                    _save_chase(tasks)
                    return jsonify({"success": True, "message": "已更新"})
                break
        else:
            return jsonify({"success": False, "message": "任务不存在"}), 404
    # 锁外查询 WPS（单次目录列举，通常 <2s；失败则回退为 0）
    auto_ep = 0
    try:
        cli, _ = get_wps_client()
        auto_ep = _auto_progress_ep_from_wps(cli, auto_ctx["wps_path"])
    except Exception:
        pass
    with _CHASE_FILE_LOCK:
        for t in tasks:
            if t.get("id") == cid:
                t["progress_ep"] = auto_ep
                t["wps_path"] = _calc_chase_wps_path(t)
                break
        _save_chase(tasks)
    return jsonify({"success": True, "message": "已更新",
                    "auto_progress_ep": auto_ep,
                    "note": "已追至留空，已自动取 WPS 已存最大集数"})

@bp.route("/api/admin/chase/<cid>", methods=["DELETE"])
@login_required
def api_admin_chase_delete(cid):
    with _CHASE_FILE_LOCK:
        tasks = _load_chase()
        tasks = [t for t in tasks if t.get("id") != cid]
        _save_chase(tasks)
        return jsonify({"success": True, "message": "已删除"})

@bp.route("/api/admin/chase/<cid>/toggle", methods=["POST"])
@login_required
def api_admin_chase_toggle(cid):
    with _CHASE_FILE_LOCK:
        tasks = _load_chase()
        for t in tasks:
            if t.get("id") == cid:
                t["enabled"] = not t.get("enabled", True)
                _save_chase(tasks)
                return jsonify({"success": True, "enabled": t["enabled"]})
        return jsonify({"success": False, "message": "任务不存在"}), 404

@bp.route("/api/admin/chase/<cid>/check", methods=["POST"])
@login_required
def api_admin_chase_manual_check(cid):
    """手动触发追更检查（从 base_ep 开始重新扫描）"""
    tasks = _load_chase()
    for t in tasks:
        if t.get("id") == cid:
            if t.get("stopped"):
                return jsonify({"success": False, "message": "任务已完全停止，请先重置"}), 400
            # 手动运行视为恢复：清除客户端取消状态残留（幂等），防止流水线断在某一集
            _send_chase_resume_signals(cid, t)
            _create_chase_check_task(t, manual=True)
            return jsonify({"success": True, "message": "追更检查已触发（服务端执行）"})
    return jsonify({"success": False, "message": "任务不存在"}), 404

@bp.route("/api/admin/chase/run_all", methods=["POST"])
@login_required
def api_admin_chase_run_all():
    """一键运行：为所有启用且无进行中任务的追更任务批量创建检查任务"""
    tasks = _load_chase()
    # 统计有进行中任务（pending/running）的追更，避免重复下发
    active_chase = set()
    for tid in list(_CLIENT_QUEUE):
        task = _CLIENT_TASKS.get(tid)
        if task and task.get("status") in ("pending", "running") and task.get("chase_id"):
            active_chase.add(task.get("chase_id"))
    created, skipped = [], []
    for t in tasks:
        t.setdefault("enabled", True)
        if not t.get("enabled"):
            skipped.append({"id": t.get("id"), "name": t.get("name", ""), "reason": "已停用"})
            continue
        if t.get("stopped"):
            skipped.append({"id": t.get("id"), "name": t.get("name", ""), "reason": "已停止"})
            continue
        if t.get("id") in active_chase:
            skipped.append({"id": t.get("id"), "name": t.get("name", ""), "reason": "已有任务进行中"})
            continue
        try:
            # 一键运行视为恢复：清除可能残留的客户端取消状态（幂等）
            _send_chase_resume_signals(t.get("id") or t.get("chase_id") or "", t)
            _create_chase_check_task(t, manual=True)
            created.append(t.get("name", t.get("id", "")))
        except Exception as e:
            skipped.append({"id": t.get("id"), "name": t.get("name", ""), "reason": f"创建失败: {e}"})
    msg = f"已触发 {len(created)} 个追更检查（服务端执行）" + (f"，跳过 {len(skipped)} 个" if skipped else "")
    return jsonify({"success": True, "message": msg, "created": created, "skipped": skipped})

@bp.route("/api/admin/chase/browse_share", methods=["GET"])
@login_required
def api_admin_chase_browse_share():
    """浏览分享链接目录结构，优先尝试网页版（无需账号），失败则回退到已有云盘账号"""
    share_url = request.args.get("share_url", "").strip()
    share_passwd = request.args.get("share_passwd", "").strip()
    parent_id = request.args.get("parent_id", "root").strip()

    if not share_url:
        return jsonify({"success": False, "message": "请输入分享链接"}), 400

    # 根据分享链接域名自动识别网盘类型
    is_quark = "pan.quark.cn" in share_url or "quark.cn" in share_url

    try:
        if is_quark:
            from .quark_client import QuarkClient

            # 夸克分享链接无需账号即可浏览目录（用默认会话解析）
            client = QuarkClient("")
            result = client.list_share_files(share_url, share_passwd, parent_id)

            # 若默认会话失败，回退到已配置的夸克账号 cookie
            if not result.get("ok"):
                quark_accounts = _get_all_quark_accounts()
                if quark_accounts:
                    acc = quark_accounts[0]
                    client = QuarkClient(acc.get("cookie", ""))
                    result = client.list_share_files(share_url, share_passwd, parent_id)
        else:
            from .cloud_clients import YidongClient

            # 方式1：尝试网页版 AES 加密（不需要账号）
            result = YidongClient.browse_share_web(share_url, share_passwd, parent_id)

            # 方式2：如果网页版失败，尝试使用已配置的 139 云盘账号（合并两个数据源）
            if not result.get("ok"):
                yidong_accounts = _get_all_yidong_accounts()
                if not yidong_accounts:
                    return jsonify({"success": False, "message": result.get("msg", "分享链接解析失败") + "（无可用云盘账号，请在「云盘配置」中添加 139 云盘）"}), 400

                # 用主账号（_get_all_yidong_accounts 已将主账号排到第一位）
                acc = yidong_accounts[0]
                client = YidongClient(acc.get("cookie", ""))
                result = client.list_share_files(share_url, share_passwd, parent_id)

        if not result.get("ok"):
            return jsonify({"success": False, "message": result.get("msg", "分享链接解析失败")}), 400

        entries = []
        for f in result.get("items", []):
            entries.append({
                "id": f.get("id", ""),
                "name": f.get("name", ""),
                "size": f.get("size", 0),
                "is_dir": f.get("is_dir", False),
                "path": f.get("path", ""),
            })
        return jsonify({"success": True, "files": entries,
                        "total": result.get("count", len(entries)),
                        "is_share": True})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": f"浏览失败: {str(e)}"}), 500

@bp.route("/api/admin/chase/preview_files", methods=["POST"])
@login_required
def api_admin_chase_preview_files():
    """
    从分享链接直接预览文件列表（服务端处理，无需客户端）。
    数据: { share_url, share_passwd, ep_pattern, rename_template, chase_name }
    返回: { success, total, matched_count, matched }
    """
    data = request.get_json() or {}
    share_url = data.get("share_url", "").strip()
    share_passwd = data.get("share_passwd", "").strip()
    ep_pattern = data.get("ep_pattern", "").strip()
    rename_template = data.get("rename_template", "").strip()
    chase_name = data.get("chase_name", "").strip()
    season = int(data.get("season", 0))
    base_ep = int(data.get("base_ep", 0))
    sub_path = data.get("sub_path", "").strip()
    tmdb_id = data.get("tmdb_id", "").strip()
    category = data.get("category", "").strip()
    chase_id = data.get("chase_id", "").strip()
    # 已有文件列表：编辑模板/正则后立即重预览时传入，无需重新拉分享链接
    file_names = data.get("file_names") or []
    file_sizes = data.get("file_sizes") or []
    # 低于该大小(MB)的文件不参与预览（过滤 jpg/png/txt 等小文件被误识别）——全局配置（个人资料）
    try:
        min_size_mb = float(load_config().get("chase_min_size_mb", 0) or 0)
    except (ValueError, TypeError):
        min_size_mb = 0
    is_movie = "电影" in category

    # 已保存的手动集数/跳过/重命名设置（持久化）
    manual_episodes = {}
    manual_renames = {}
    if chase_id:
        for t in _load_chase():
            if t.get("id") == chase_id:
                manual_episodes = t.get("manual_episodes") or {}
                manual_renames = t.get("manual_renames") or {}
                break

    if not share_url and not file_names:
        return jsonify({"success": False, "message": "请输入分享链接"}), 400

    try:
        if share_url:
            is_quark = "pan.quark.cn" in share_url or "quark.cn" in share_url

            if is_quark:
                from .quark_client import QuarkClient
                # 夸克分享链接无需账号即可预览
                result = _browse_share_recursive(share_url, share_passwd, "root", sub_path)
                if not result.get("ok"):
                    quark_accounts = _get_all_quark_accounts()
                    for acc in quark_accounts:
                        client = QuarkClient(acc.get("cookie", ""))
                        browse_func = lambda u, p, pid: client.list_share_files(u, p, pid)
                        result = _browse_share_recursive(share_url, share_passwd, "root", sub_path, browse_func)
                        if result.get("ok"):
                            break
                    if not result.get("ok"):
                        return jsonify({"success": False, "message": result.get("msg", "分享链接解析失败") + (
                            "（无可用云盘账号）" if not quark_accounts else "（账号方式也失败）"
                        )}), 400
            else:
                from .cloud_clients import YidongClient

                # 方式1：尝试网页版 AES 加密（不需要账号）
                result = _browse_share_recursive(share_url, share_passwd, "root", sub_path)

                # 方式2：如果网页版失败，尝试使用已配置的 139 云盘账号
                if not result.get("ok"):
                    yidong_accounts = _get_all_yidong_accounts()
                    for acc in yidong_accounts:
                        client = YidongClient(acc.get("cookie", ""))
                        browse_func = lambda u, p, pid: client.list_share_files(u, p, pid)
                        result = _browse_share_recursive(share_url, share_passwd, "root", sub_path, browse_func)
                        if result.get("ok"):
                            break
                    if not result.get("ok"):
                        return jsonify({"success": False, "message": result.get("msg", "分享链接解析失败") + (
                            "（无可用云盘账号）" if not yidong_accounts else "（账号方式也失败）"
                        )}), 400

            files = result.get("items", [])
        else:
            # 已有文件列表：编辑模板/正则后立即重预览，无需重新拉分享链接
            _names = file_names if isinstance(file_names, list) else []
            _sizes = file_sizes if isinstance(file_sizes, list) else []
            files = []
            for _i, _n in enumerate(_names):
                _sz = 0
                if _i < len(_sizes):
                    try:
                        _sz = int(_sizes[_i] or 0)
                    except (ValueError, TypeError):
                        _sz = 0
                files.append({"name": _n, "is_dir": False, "size": _sz})
            if not files:
                return jsonify({"success": False, "message": "请提供文件名列表"}), 400
        # 文件格式过滤：只保留真实视频文件，彻底过滤 jpg/png/txt 等附件
        # （如 "网易爆米花下载二维码.png"、被误命名的 ".jpg" 等）被误识别为剧集。
        # 夸克分享中单文件可能被误标 is_dir=True，故不再信任 is_dir 标志，一律按扩展名判断。
        # 白名单来自个人资料设置 chase_video_exts（逗号分隔），空则用内置默认。
        video_exts = _global_video_exts()
        file_items = [f for f in files
                      if (f.get("name") or "").lower().endswith(video_exts)]
        filtered_format = len(files) - len(file_items)
        # 低于阈值大小的视频（预告/花絮等）不参与预览；size 未知(=0)的文件保留
        filtered_small = 0
        if min_size_mb > 0:
            _min_bytes = int(min_size_mb * 1024 * 1024)
            _kept = [f for f in file_items
                     if not f.get("size") or f.get("size", 0) >= _min_bytes]
            filtered_small = len(file_items) - len(_kept)
            file_items = _kept
        matched = []

        for f in file_items:
            fname = f.get("name", "")
            # 先取自动识别集数；若之前已手动设置过，用 manual 覆盖
            ep_num, matched_text = _server_extract_episode(fname, ep_pattern, is_movie=is_movie)

            # 合并已保存的手动集数/跳过设置（按文件名匹配）
            manual = manual_episodes.get(fname, {}) if manual_episodes else {}
            skipped = bool(manual.get("skip", False))
            if manual.get("ep") is not None:
                ep_num = int(manual["ep"])
                if not matched_text:
                    matched_text = f"EP{ep_num:02d}"

            # 从文件名提取季别（与下载任务一致，优先文件名而非表单值）
            file_season = season
            _sf = re.search(r'[Ss](\d{2})', fname)
            if _sf:
                file_season = int(_sf.group(1))
            elif file_season <= 0:
                # 原文件无季标识且未显式指定季：默认按 S01 处理（避免模板渲染出 S00）
                file_season = 1

            # 无集数文件（如电影）也尝试生成重命名：fallback 用文件名本身
            fallback_name = chase_name if ep_num is not None else os.path.splitext(fname)[0]

            # 电影分类：不按 base_ep 过滤（电影整部下载，无"第几集"概念）
            # 剧集分类：仅显示大于 base_ep 的集数
            show_as_new = is_movie or (ep_num is not None and ep_num > base_ep)
            show_as_old = (not is_movie) and ep_num is not None and ep_num <= base_ep

            if show_as_new:
                renamed = None
                if rename_template:
                    renamed = _apply_rename_template(
                        ep_num if ep_num is not None else 1, rename_template,
                        fallback_name=fallback_name if ep_num is not None else chase_name,
                        season=file_season,
                        tmdb_id=tmdb_id,
                        original_filename=fname,
                    )
                # 手动重命名优先（预览中可单独编辑的名字）
                if manual_renames.get(fname):
                    renamed = manual_renames[fname]
                matched.append({
                    "filename": fname,
                    "ep_num": ep_num,
                    "season": file_season,
                    "matched_text": matched_text or (f"EP{ep_num:02d}" if ep_num is not None else ""),
                    "renamed": renamed,
                    "size": f.get("size", 0),
                    "is_movie": is_movie,
                    "skip": skipped,
                    "manual_ep": manual.get("ep"),
                })
            elif show_as_old:
                # 不超过 base_ep 的标记为已追
                matched.append({
                    "filename": fname,
                    "ep_num": ep_num,
                    "season": file_season,
                    "matched_text": "",
                    "renamed": None,
                    "size": f.get("size", 0),
                    "is_movie": False,
                    "skip": skipped,
                    "manual_ep": manual.get("ep"),
                })
            else:
                # 无集数文件：仅电影分类（整部单文件下载）才按模板生成重命名预览；
                # 剧集分类下正则匹配失败的文件（如剧场版/预告/花絮）不会下载，
                # 与客户端行为一致：不生成 E01 重命名、不参与推送，预览显示「—」。
                renamed = None
                if is_movie and rename_template:
                    try:
                        renamed = _apply_rename_template(
                            1, rename_template,
                            fallback_name=fallback_name,
                            season=file_season,
                            tmdb_id=tmdb_id,
                            original_filename=fname,
                        )
                    except Exception:
                        renamed = None
                # 仅电影分类应用历史手动重命名：剧集分类下正则匹配失败的文件
                # 不参与下载，保存过的手动重命名（如修复前误生成的 E01）也不生效
                if manual_renames.get(fname) and is_movie:
                    renamed = manual_renames[fname]
                matched.append({
                    "filename": fname,
                    "ep_num": None,
                    "season": file_season,
                    "matched_text": "",
                    "renamed": renamed,
                    "size": f.get("size", 0),
                    "is_movie": is_movie,
                    "skip": skipped,
                    "manual_ep": manual.get("ep"),
                })

        return jsonify({
            "success": True,
            "total": len(file_items),
            "filtered_count": filtered_format + filtered_small,
            "filtered_format_count": filtered_format,
            "matched_count": sum(1 for m in matched if m["ep_num"] is not None and (is_movie or m["ep_num"] > base_ep)),
            "is_movie": is_movie,
            "matched": matched,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": f"预览失败: {str(e)}"}), 500

@bp.route("/api/admin/chase/apply_preview", methods=["POST"])
@login_required
def api_admin_chase_apply_preview():
    """
    保存分享预览中手动设置的集数/跳过/重命名（持久化进追更任务）。
    数据: { chase_id, manual_episodes: { filename: {ep: int|null, skip: bool} },
           manual_renames: { filename: 重命名后名字 } }
    返回: { success, saved_count, rename_count }
    """
    data = request.get_json() or {}
    chase_id = data.get("chase_id", "").strip()
    manual_in = data.get("manual_episodes", {}) or {}
    renames_in = data.get("manual_renames", {}) or {}
    if not chase_id:
        return jsonify({"success": False, "message": "缺少 chase_id"}), 400

    # 规整：ep 为空字符串/None → 清除手动集数；skip 布尔
    norm = {}
    for fname, v in manual_in.items():
        if not isinstance(v, dict):
            continue
        ep = v.get("ep")
        try:
            ep = int(ep) if ep not in (None, "", "null") else None
        except (ValueError, TypeError):
            ep = None
        norm[fname] = {"ep": ep, "skip": bool(v.get("skip", False))}

    # 规整：只保留非空、与原文件名不同的重命名
    norm_renames = {}
    for fname, new_name in renames_in.items():
        if not isinstance(new_name, str):
            continue
        new_name = new_name.strip()
        if new_name and new_name != fname:
            norm_renames[fname] = new_name

    # 预览快照：保存时把最近一次预览的匹配结果一并持久化，
    # 下次打开预览弹窗无需重新从分享链接拉取，直接恢复上次结果
    snap = data.get("preview_snapshot") or {}
    if isinstance(snap, dict) and isinstance(snap.get("matched"), list) and snap.get("matched"):
        try:
            season_snap = int(snap.get("season", 0))
        except (ValueError, TypeError):
            season_snap = 0
        try:
            base_ep_snap = int(snap.get("base_ep", 0))
        except (ValueError, TypeError):
            base_ep_snap = 0
        snapshot = {
            "params": {
                "share_url": snap.get("share_url", ""),
                "sub_path": snap.get("sub_path", ""),
                "ep_pattern": snap.get("ep_pattern", ""),
                "rename_template": snap.get("rename_template", ""),
                "season": season_snap,
                "base_ep": base_ep_snap,
            },
            "matched": snap["matched"],
            "total": snap.get("total", 0),
            "matched_count": snap.get("matched_count", 0),
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        snapshot = None

    with _CHASE_FILE_LOCK:
        tasks = _load_chase()
        for t in tasks:
            if t.get("id") == chase_id:
                t["manual_episodes"] = norm
                t["manual_renames"] = norm_renames
                if snapshot:
                    t["preview_cache"] = snapshot
                _save_chase(tasks)
                return jsonify({
                    "success": True,
                    "saved_count": len(norm),
                    "rename_count": len(norm_renames),
                })
        return jsonify({"success": False, "message": "未找到该追更任务"}), 404


@bp.route("/api/admin/chase/<cid>/preview_cache", methods=["GET"])
@login_required
def api_admin_chase_preview_cache(cid):
    """获取任务已保存的预览快照，供打开预览弹窗时自动恢复"""
    with _CHASE_FILE_LOCK:
        tasks = _load_chase()
        for t in tasks:
            if t.get("id") == cid:
                cache = t.get("preview_cache")
                if not cache or not cache.get("matched"):
                    return jsonify({"success": True, "cache": None})
                # 文件格式过滤：历史快照可能保存了格式过滤生效前的匹配结果
                # （jpg/png/txt 等附件被误识别为剧集），恢复显示前与实时预览
                # 使用同一白名单过滤，保证打开预览弹窗看到的与实时预览一致。
                video_exts = _global_video_exts()
                _matched = [m for m in cache.get("matched") or []
                            if (m.get("filename") or "").lower().endswith(video_exts)]
                # 修复历史快照中"无集数文件被硬编码渲染为 E01"的脏数据。
                # 旧代码曾把 is_movie 硬编码为 True，快照里的 is_movie 不可信，
                # 须以任务真实分类为准：剧集分类下正则未匹配的文件
                # （剧场版/预告/花絮等）不会下载，恢复时不展示重命名结果。
                task_is_movie = "电影" in (t.get("category") or "")
                _matched = [
                    dict(m, renamed=None, is_movie=False)
                    if m.get("ep_num") is None and not task_is_movie
                    else m
                    for m in _matched
                ]
                cache = dict(cache)
                cache["matched"] = _matched
                cache["total"] = len(_matched)
                cache["matched_count"] = len(_matched)
                return jsonify({"success": True, "cache": cache})
        return jsonify({"success": False, "message": "任务不存在"}), 404

@bp.route("/api/admin/chase/preview_result/<preview_id>", methods=["GET"])
@login_required
def api_admin_chase_preview_result(preview_id):
    """获取预览结果"""
    result = _PREVIEW_RESULTS.get(preview_id)
    if not result:
        return jsonify({"success": False, "message": "预览任务不存在或已过期"}), 404
    return jsonify({"success": True, **result})

@bp.route("/api/admin/chase/test_pattern", methods=["POST"])
@login_required
def api_admin_chase_test_pattern():
    """
    本地测试正则/重命名模板（无需客户端），用户粘贴文件名列表即可测试。
    数据: { files: [str, ...], ep_pattern, rename_template, chase_name }
    """
    data = request.get_json() or {}
    files = data.get("files", [])
    ep_pattern = data.get("ep_pattern", "").strip()
    rename_template = data.get("rename_template", "").strip()
    chase_name = data.get("chase_name", "").strip()
    chase_year = data.get("chase_year", "").strip()
    full_name = chase_name + (" (" + chase_year + ")" if chase_year else "")
    season = int(data.get("season", 0))
    tmdb_id = data.get("tmdb_id", "").strip()

    if not files:
        return jsonify({"success": False, "message": "请提供文件名列表"}), 400

    matched = []
    for fname in files:
        ep_num, matched_text = _server_extract_episode(fname, ep_pattern)
        if ep_num is not None:
            # 季数处理与正式预览 preview_files 保持一致：
            # 源文件名含 S## 优先；季数 <=0 且源文件无季标识时默认按 S01（避免模板渲染出 S00）
            file_season = season
            _sf = re.search(r'[Ss](\d{2})', fname)
            if _sf:
                file_season = int(_sf.group(1))
            elif file_season <= 0:
                file_season = 1
            renamed = _apply_rename_template(ep_num, rename_template, full_name or "Show", file_season, tmdb_id, fname)
            matched.append({
                "filename": fname,
                "ep_num": ep_num,
                "matched_text": matched_text or "",
                "renamed": renamed,
            })
        else:
            matched.append({
                "filename": fname,
                "ep_num": None,
                "matched_text": "",
                "renamed": None,
            })

    return jsonify({
        "success": True,
        "total": len(files),
        "matched_count": sum(1 for m in matched if m["ep_num"] is not None),
        "matched": matched,
    })
