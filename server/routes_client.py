# -*- coding: utf-8 -*-
"""server/routes_client.py - 客户端通信/心跳/任务下发
"""
from flask import Blueprint, request, jsonify, session, redirect, url_for, Response, send_file, stream_with_context
import time
import re
from datetime import datetime
from .state import *
from .state import _CLIENT_DISPATCH_LOCK  # 任务派发临界区互斥锁（防多客户端重复下发）
from .utils import *
from .models import WPSClient, API_BASE
from .helpers import *
from .notify import send_notify

bp = Blueprint("client", __name__)

_CHASE_HISTORY_LIMIT = 30  # 每个追更任务保留的历史记录条数

# 追更识别的视频格式白名单改由个人资料设置 chase_video_exts 提供（逗号分隔），
# 运行时经 _global_video_exts() 读取；只允许白名单格式参与集数识别/反解析，
# 彻底过滤 "网易爆米花下载二维码.png"、"xxx.txt" 等附件被误当成剧集。

# ══════════════════════════════════════════════════
# STRM 触发幂等（★13）：chase_done 重复上报不重复生成 STRM
#   chase_id → 最近一次成功触发 STRM 的时间戳（秒）
#   冷却期内重复触发（批次完成逻辑抖动/重复上报）直接跳过，
#   避免重复整轮扫描 + 重复写 .strm（即便 strm_incremental 已跳过存在文件，
#   重复扫描仍会浪费 IO 并重复 os.makedirs）。
# ══════════════════════════════════════════════════
_STRM_TRIGGERED_AT = {}
_STRM_RETRIGGER_COOLDOWN = 300  # 秒

# 客户端错误 → 可操作类型，用于追更卡片预警提示
_CHASE_ERROR_HINTS = {
    "cookie_invalid": "Cookie/登录可能失效，请到客户端更新云盘配置",
    "share_invalid": "分享链接失效或提取码错误，请检查追更配置",
    "rate_limited": "触发限流/风控，已自动退避重试",
    "network": "网络异常，请检查客户端网络",
    "other": "未知错误，请查看客户端日志",
}


def _classify_chase_error(err):
    """把客户端上报的错误文本归类为可操作类型（cookie/share/限流/网络/其他）"""
    if not err:
        return "other"
    e = str(err).lower()
    # 限流/风控优先（数字特征最明确）
    if any(k in e for k in ("429", "限流", "频繁", "风控", "23018", "too many", "太频繁", "访问过于频繁")):
        return "rate_limited"
    # 认证/凭证类
    if any(k in e for k in ("cookie", "登录", "未授权", "401", "403", "token", "游客", "stoken", "credential", "未登录")):
        return "cookie_invalid"
    # 分享链接类
    if any(k in e for k in ("链接失效", "提取码", "不存在", "已失效", "分享已取消", "share", "被删除", "过期")):
        return "share_invalid"
    # 网络类
    if any(k in e for k in ("timeout", "超时", "连接", "network", "socket", "refused", "reset", "dns", "unreachable")):
        return "network"
    return "other"


def _trigger_strm_for_chase_idempotent(t):
    """带幂等守卫的 STRM 触发（★13）。

    仅在以下情况真正触发整轮 STRM 扫描：
      1) 该追更从未触发过；或
      2) 距上次触发已超过冷却期（_STRM_RETRIGGER_COOLDOWN）；或
      3) 本次 done_eps 数量相比上次触发时有明显新增（>0 集）。
    否则跳过，避免 chase_done 重复上报 / 批次抖动导致重复生成 STRM。
    """
    global _STRM_TRIGGERED_AT
    cid = t.get("chase_id", "") or t.get("id", "")
    if not cid:
        # 无 chase_id 无法去重，退化为直接触发
        _trigger_strm_for_chase(t)
        return
    now_ts = time.time()
    last = _STRM_TRIGGERED_AT.get(cid, 0)
    prev_done = _STRM_TRIGGERED_AT.get(cid + "::done", 0)
    cur_done = len(t.get("done_eps", []) or t.get("wps_eps", []) or [])
    if last and (now_ts - last) < _STRM_RETRIGGER_COOLDOWN and cur_done <= prev_done:
        # 冷却期内且无新集，跳过重复触发
        return
    _trigger_strm_for_chase(t)
    _STRM_TRIGGERED_AT[cid] = now_ts
    _STRM_TRIGGERED_AT[cid + "::done"] = cur_done


def _append_chase_history(t, ev_type, status, detail, eps=None):
    """向追更任务追加一条结构化历史记录（时间/类型/状态/详情/涉及集数）。

    所有 last_result 写入点都应同步调用，使卡片不再只有一行结果文本，
    而能展开看到最近每次检查/下载的完整轨迹。调用方须持有 _CHASE_FILE_LOCK。
    """
    hist = t.setdefault("history", [])
    hist.append({
        "ts": _beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": ev_type,      # check / download / calibrate / forced / filter
        "status": status,     # ok / info / warn / error
        "detail": detail,
        "eps": sorted(set(int(e) for e in (eps or []) if str(e).lstrip('-').isdigit())),
    })
    # 只保留最近 N 条，避免历史无限膨胀
    if len(hist) > _CHASE_HISTORY_LIMIT:
        del hist[:len(hist) - _CHASE_HISTORY_LIMIT]


@bp.route("/api/client/creds")
def api_client_creds():
    """客户端连接时拉取服务端已配置的网盘凭据（WPS cookie / 139 token / 夸克 cookie）。

    客户端无需再手动填写这些凭据：连接服务端成功后自动拉取，仅临时保存在客户端内存。
    安全策略：服务端配置了后台账号时，要求客户端已完成 /api/login（与后台共用 session 校验）；
    未配置账号（家庭/内网场景）时宽松放行，与 /api/client/poll 一致。
    """
    client_name = (request.args.get("client", "") or "").strip()
    if not client_name:
        return jsonify({"success": False, "message": "缺少客户端标识"}), 400
    _cfg = load_config()
    if _cfg.get("username") and not session.get("logged_in"):
        return jsonify({"success": False, "message": "未登录"}), 401
    try:
        wps_cookie = (load_cookie() or {}).get("cookie", "") or ""
    except Exception:
        wps_cookie = ""
    clouds = []
    try:
        for acc in _get_all_yidong_accounts():
            clouds.append({"id": acc.get("id") or "yidong_main", "type": "yidong",
                           "name": acc.get("name") or "139云盘",
                           "token": acc.get("cookie") or ""})
        for acc in _get_all_quark_accounts():
            clouds.append({"id": acc.get("id") or "quark_main", "type": "quark",
                           "name": acc.get("name") or "夸克网盘",
                           "cookie": acc.get("cookie") or ""})
    except Exception:
        clouds = []
    return jsonify({"success": True, "wps_cookie": wps_cookie, "clouds": clouds})


@bp.route("/api/client/poll")
def api_client_poll():
    """客户端轮询获取任务（支持批量取N个任务 + 检查取消指令）"""
    client_name = request.args.get("client", "").strip()
    check_cancel = request.args.get("check_cancel", "0") == "1"
    batch = int(request.args.get("batch", "1"))  # 客户端请求的任务数（并发槽位）
    client_max_concurrent = int(request.args.get("max_concurrent", "3"))
    client_version = request.args.get("version", "").strip()
    cloud_types = request.args.get("cloud_types", "").strip()
    now = time.time()
    # 注册/更新客户端（含并发能力）
    if client_name:
        info = _CLIENT_REGISTRY.get(client_name, {})
        info.update({
            "client_name": client_name,
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": request.remote_addr,
            "max_concurrent": info.get("max_concurrent", client_max_concurrent),
            "version": client_version or info.get("version", ""),
            "cloud_types": set(cloud_types.split(",")) if cloud_types else info.get("cloud_types", set()),
            # 注意：registry 内部用 set 便于校验，但返回 JSON 前需转 list，否则 jsonify 报错导致客户端管理页 500
        })
        _CLIENT_REGISTRY[client_name] = info
    # ── 超时回收 ──
    # 只按客户端断连回收：客户端超过 5 分钟没心跳才重置它的运行中任务
    for tid in list(_CLIENT_QUEUE):
        task = _CLIENT_TASKS.get(tid)
        if task and task.get("status") == "running":
            pb = task.get("polled_by", "")
            client_last = _CLIENT_REGISTRY.get(pb, {}).get("_last_ts", 0)
            if now - client_last > 300:
                task["status"] = "pending"
    # ── pending 无人认领超时：超过 10 分钟仍无客户端 poll 领取的任务视为死任务，
    # 直接标记失败并从队列移除，避免 active 状态永久占用导致「立即运行/检查更新」
    # 按钮长期置灰、新任务排不进去 ──
    for tid in list(_CLIENT_QUEUE):
        task = _CLIENT_TASKS.get(tid)
        if task and task.get("status") == "pending":
            if now - (task.get("created_at") or 0) > 600:
                task["status"] = "failed"
                task["result_msg"] = "任务排队超时无人执行，已自动清理（客户端可能离线）"
                task["finished_at"] = time.time()
                if tid in _CLIENT_QUEUE:
                    _CLIENT_QUEUE.remove(tid)
    # 更新客户端活跃数（在超时回收之后重新统计）
    if client_name:
        _CLIENT_REGISTRY[client_name]["_last_ts"] = now
        _CLIENT_REGISTRY[client_name]["active_count"] = _client_active_count(client_name)
        _CLIENT_ACTIVE[client_name] = _client_active_count(client_name)
    # ── 断连恢复同步：客户端上报当前正在运行的任务ID，服务端重置丢失的任务 ──
    if client_name:
        running_raw = request.args.get("running_tids", "")
        running_set = set(filter(None, running_raw.split(","))) if running_raw else set()
        for tid in list(_CLIENT_QUEUE):
            task = _CLIENT_TASKS.get(tid)
            if task and task.get("status") == "running" and task.get("polled_by") == client_name:
                if tid not in running_set:
                    task["status"] = "pending"
                    if _CLIENT_ACTIVE.get(client_name, 0) > 0:
                        _CLIENT_ACTIVE[client_name] -= 1
        # ── 超时回收：running 任务若超过阈值仍无进展且客户端已不再执行，重置为 pending，
        #    避免脏任务永久占用并发槽位导致后续任务永远分配不出去 ──
        RUNNING_TIMEOUT = 600  # 秒
        for tid in list(_CLIENT_QUEUE):
            task = _CLIENT_TASKS.get(tid)
            if task and task.get("status") == "running" and task.get("polled_by") == client_name:
                polled_at = task.get("polled_at", 0)
                if now - polled_at > RUNNING_TIMEOUT and tid not in running_set:
                    task["timeout_recovered"] = True
                    task["timeout_count"] = task.get("timeout_count", 0) + 1
                    if task.get("timeout_count") >= 3:
                        # ★9 连续3次超时：任务疑似卡死或客户端反复丢弃，
                        # 直接标记失败，避免无限重派空转；附带失败通知
                        task["status"] = "failed"
                        task["result_msg"] = "任务连续3次超时无进展，疑似卡死，已标记失败（可手动重试）"
                        task["finished_at"] = time.time()
                        if _CLIENT_QUEUE.count(tid):
                            _CLIENT_QUEUE.remove(tid)
                        try:
                            send_notify(f"⚠️ 任务反复超时: {task.get('name','?')}",
                                        f"{task['result_msg']}", is_fail=True)
                        except Exception:
                            pass
                    else:
                        task["status"] = "pending"
                    if _CLIENT_ACTIVE.get(client_name, 0) > 0:
                        _CLIENT_ACTIVE[client_name] -= 1
    # ── 先检查取消指令 ──
    cancel_list = []
    if client_name and client_name in _CANCEL_REQUESTS:
        cancels = _CANCEL_REQUESTS[client_name]
        now_ts = time.time()
        for cid, cts in list(cancels.items()):
            if now_ts - cts > 60:
                # 取消指令超过60秒自动清理（客户端早已收到，重复下发无意义）
                del cancels[cid]
                continue
            cancel_list.append(cid)
        if not cancels:
            del _CANCEL_REQUESTS[client_name]
    # ── 恢复指令（重新运行追更时下发，清除客户端本地取消状态） ──
    resume_list = []
    if client_name and client_name in _RESUME_REQUESTS:
        resume_set = _RESUME_REQUESTS[client_name]
        resume_list = list(resume_set)
        resume_set.clear()
        if not resume_set:
            del _RESUME_REQUESTS[client_name]
        # 同步清除该追更的取消指令：否则 60 秒窗口内取消又被重复下发，覆盖恢复信号
        cancels = _CANCEL_REQUESTS.get(client_name)
        if cancels:
            for cid in resume_list:
                cancels.pop(cid, None)
            if not cancels:
                _CANCEL_REQUESTS.pop(client_name, None)
    # ── 任务派发临界区：必须串行执行 ──
    # 多客户端并发轮询时（Flask threaded=True 下为并发线程），若无互斥保护，
    # 两台客户端会同时读到同一个 pending 任务、同时把它置为 running 并各自
    # 分配，造成同一集被多台客户端重复下载（任务下发重叠）。故把
    # 「终态自检 + 收集可分配任务 + 计算可分配数 + 改状态分配」整段包进
    # _CLIENT_DISPATCH_LOCK，一次只有一个轮询线程在派发。
    with _CLIENT_DISPATCH_LOCK:
        # ── 追更下载任务派发前自检：该集已成功上传则直接终结，避免重复下载 ──
        # 客户端下载完成后若终态上报丢失（网络抖动/服务端重启导致 task_status 3 次
        # 重试失败），任务会卡在 running，随后被"断连恢复同步"重置为 pending 重新
        # 派发，同一客户端就会重复下载已存在的集（如斩神 S02E11 反复推送）。
        # 此处用服务端持久化的 done_eps/wps_eps 兜底：该集已确认落在 WPS，就不再把
        # 它派发下载，直接终结任务。
        if client_name and _CLIENT_QUEUE:
            try:
                with _CHASE_FILE_LOCK:
                    _cts = _load_chase()
                _chase_done_map = {}
                _chase_force_map = {}  # 指定了集数(episodes_filter)的追更：强制下载，不因已存在而终结
                _chase_stopped_map = {}  # 已完全停止的追更：任务一律终结，绝不再派发
                for _ct in _cts:
                    _de = set(_ct.get("done_eps", []) or [])
                    _de |= set(_ct.get("wps_eps", []) or [])
                    _chase_done_map[_ct.get("id", "")] = _de
                    if (_ct.get("episodes_filter") or "").strip():
                        _chase_force_map[_ct.get("id", "")] = True
                    if _ct.get("stopped"):
                        _chase_stopped_map[_ct.get("id", "")] = True
                for _tid in list(_CLIENT_QUEUE):
                    _task = _CLIENT_TASKS.get(_tid)
                    if not (_task and _task.get("type") == "chase-download"):
                        continue
                    _cid0 = _task.get("chase_id", "")
                    # 兜底：所属追更已停止 → 任务直接终结，防止任何漏网路径反复派发
                    if _chase_stopped_map.get(_cid0):
                        _old0 = _task.get("status", "")
                        _task["status"] = "cancelled"
                        _task["result_msg"] = "追更已停止"
                        _task["finished_at"] = time.time()
                        if _old0 == "running" and _task.get("polled_by"):
                            _cn0 = _task["polled_by"]
                            if _CLIENT_ACTIVE.get(_cn0, 0) > 0:
                                _CLIENT_ACTIVE[_cn0] -= 1
                        if _tid in _CLIENT_QUEUE:
                            _CLIENT_QUEUE.remove(_tid)
                        continue
                    if _task.get("batch"):
                        # 批量任务：内部各集状态可能不一致（首集已完成不代表整批完成），
                        # 交由客户端批量执行时逐集自检跳过，这里不自动终结。
                        continue
                    _cid = _task.get("chase_id", "")
                    _epn = _task.get("ep_num", 0)
                    if not (_cid and isinstance(_epn, int) and _epn > 0):
                        continue
                    if not _chase_force_map.get(_cid) and _epn in _chase_done_map.get(_cid, set()):
                        _old = _task.get("status", "")
                        _task["status"] = "done"
                        _task["result_msg"] = "目标已存在，无需下载"
                        _task["finished_at"] = time.time()
                        if _old == "running" and _task.get("polled_by"):
                            _cn = _task["polled_by"]
                            if _CLIENT_ACTIVE.get(_cn, 0) > 0:
                                _CLIENT_ACTIVE[_cn] -= 1
                        if _tid in _CLIENT_QUEUE:
                            _CLIENT_QUEUE.remove(_tid)
            except Exception:
                pass
        # ── 收集可分配的任务（只匹配本客户端名；指定了 target_client 的任务不匹配则不分发） ──
        matched_tasks = []
        for tid in list(_CLIENT_QUEUE):
            task = _CLIENT_TASKS.get(tid)
            if task and task.get("status") == "pending":
                # 该集最近被用户停止/取消（60 秒内）：不派发。即使任务已被 check
                # 一次性创建进队列，只要该集上报过取消，就拒绝派发，防止刷屏
                _ceps1 = task.get("ep_num")
                if _ceps1 is not None:
                    _cet1 = _CANCELED_EPS.get(task.get("chase_id", ""), {}).get(_ceps1, 0)
                    if _cet1 and now - _cet1 < 60:
                        continue
                # 停止后重新运行：10 秒冷却期内不派发，给客户端一个 poll 周期接收恢复指令；
                # 旧客户端由取消不重试 + _CANCELED_EPS 60 秒保护兜底，不刷屏
                _cool0 = _CHASE_RESUME_COOLDOWN.get(task.get("chase_id", ""), 0)
                if _cool0:
                    if now < _cool0:
                        continue
                    _CHASE_RESUME_COOLDOWN.pop(task.get("chase_id", ""), None)
                # 前置校验失败的任务：每次轮询重新校验，客户端补齐云盘配置后即可下发
                if task.get("warn"):
                    ok, wmsg = _check_client_cloud_support(client_name, task.get("share_url", ""))
                    if ok:
                        task["warn"] = ""
                    else:
                        continue
                # 指数退避：任务未到 run_after 时间戳则暂不派发
                if task.get("run_after") and now < task["run_after"]:
                    continue
                # 指定了 target_client 但与本客户端名不匹配的任务：坚决不分发给本客户端，
                # 留待目标客户端领取（修复过去 unmatched 仍被重新加入分配列表导致
                # 一台客户端抢走指定给另一台的任务）。
                # ★9 例外：目标客户端离线超过 5 分钟 → 解除指定，允许任意在线客户端
                # 领取，避免任务因目标客户端永久离线而永远挂起。
                # 先处理"目标离线解绑"再决定是否匹配：否则任务明明已解绑，
                # 本客户端却因旧 target_client 值被误拦到下一轮才能领取。
                target = task.get("target_client", "")
                if target:
                    _tr = _CLIENT_REGISTRY.get(target)
                    _tr_ts = _tr.get("_last_ts", 0) if _tr else 0
                    if not _tr or now - _tr_ts > 300:
                        task["target_client"] = ""
                        target = ""
                if not target or target == client_name:
                    matched_tasks.append(tid)
        # ── 计算可分配数量：不超过 batch 且不超过客户端 max_concurrent - 当前活跃数 ──
        max_available = batch
        if client_name:
            active_now = _CLIENT_ACTIVE.get(client_name, 0)
            client_max = _CLIENT_REGISTRY.get(client_name, {}).get("max_concurrent", 3)
            max_available = min(batch, max(client_max - active_now, 0))
        # ── 分配任务：只分配匹配本客户端名的任务（锁内二次校验 status，杜绝并发重复下发） ──
        assigned = []
        remaining = matched_tasks
        for tid in remaining:
            if len(assigned) >= max_available:
                break
            task = _CLIENT_TASKS.get(tid)
            if task and task.get("status") == "pending":
                task["status"] = "running"
                task["polled_at"] = now
                task["polled_by"] = client_name
                assigned.append(task)
                if client_name:
                    _CLIENT_ACTIVE[client_name] = _CLIENT_ACTIVE.get(client_name, 0) + 1
    result = {"tasks": assigned, "cancels": cancel_list, "resumes": resume_list, "task": None}
    # 兼容旧版客户端：单任务模式
    if len(assigned) == 1 and not cancel_list:
        result["task"] = assigned[0]
    elif cancel_list and not assigned:
        result["task"] = {"type": "cancel", "chase_ids": cancel_list}
    return jsonify(result)

@bp.route("/api/client/task_status", methods=["POST"])
def api_client_task_status():
    """客户端上报任务执行结果（含运行中实时进度）"""
    data = request.get_json(silent=True) or {}
    tid = data.get("id", "")
    status = data.get("status", "")
    msg = data.get("msg", "")
    if tid in _CLIENT_TASKS:
        # ── 运行中：实时进度上报（★7），不参与终态流转，只更新展示字段 ──
        if status == "running":
            for _k in ("progress", "total", "speed", "stage"):
                if _k in data:
                    _CLIENT_TASKS[tid][_k] = data.get(_k)
            _CLIENT_TASKS[tid]["progress_ts"] = time.time()
            return jsonify({"success": True})
        old_status = _CLIENT_TASKS[tid].get("status", "")
        client_name = _CLIENT_TASKS[tid].get("polled_by", "")
        # "skipped" 说明客户端槽位满了，任务实际并未被取走，不要覆盖 running 状态；
        # 但主动取消（msg 含"已取消"）时任务已被客户端取走且用户停止了该追更：
        # 若不移除，任务会被"断连恢复同步"重置为 pending 反复派发，客户端又因
        # cancel_evt 反复取消 → 形成"一键停止后任务每5秒反复下发"死循环。
        if status == "skipped" and old_status == "running":
            if msg and "取消" in msg:
                _CLIENT_TASKS[tid]["status"] = "cancelled"
                _CLIENT_TASKS[tid]["result_msg"] = msg
                _CLIENT_TASKS[tid]["finished_at"] = time.time()
                if tid in _CLIENT_QUEUE:
                    _CLIENT_QUEUE.remove(tid)
                if client_name and _CLIENT_ACTIVE.get(client_name, 0) > 0:
                    _CLIENT_ACTIVE[client_name] -= 1
                return jsonify({"success": True})
            return jsonify({"success": True, "ignored": "still_running"})
        _CLIENT_TASKS[tid]["status"] = status
        _CLIENT_TASKS[tid]["result_msg"] = msg
        _CLIENT_TASKS[tid]["finished_at"] = time.time()
        # ── 追更下载任务完成：幂等记入 done_eps（双保险）──
        # 客户端成功路径先报 chase_done 再报 task_status(done)，正常走到这里时
        # done_eps 已含该集；此处兜底：若 chase_done 网络丢失/被并发覆盖，
        # 也能保证漏集补追不会把已上传完成的集重复推送（如斩神11集重复下载问题）。
        if (status == "done" and _CLIENT_TASKS[tid].get("type") == "chase-download"):
            _cid = _CLIENT_TASKS[tid].get("chase_id", "")
            _epn = _CLIENT_TASKS[tid].get("ep_num", 0)
            if _cid and isinstance(_epn, int):
                try:
                    with _CHASE_FILE_LOCK:
                        _cts = _load_chase()
                        for _t in _cts:
                            if _t.get("id") == _cid:
                                # 电影任务（负序号占位）：按文件名记入 downloaded_movies，
                                # 与 chase_done 行为对齐，防止电影被重复派发下载
                                if _epn < 0:
                                    _fn = _CLIENT_TASKS[tid].get("file_name", "")
                                    if _fn:
                                        _dm = set(_t.get("downloaded_movies", []) or [])
                                        if _fn not in _dm:
                                            _dm.add(_fn)
                                            _t["downloaded_movies"] = list(_dm)
                                _de = _t.setdefault("done_eps", [])
                                if int(_epn) not in _de:
                                    _de.append(int(_epn))
                                    _de.sort()
                                break
                        _save_chase(_cts)
                except Exception:
                    pass
        # ── 追更检查结果上报失败：自动重新排队（限次）──
        # 客户端在 chase_result 上报失败时会以 msg 含 "结果上报失败" 标记错误状态；
        # 服务端收到后重新排队该 chase-check，避免整批新集因一次网络抖动/超时被静默丢弃。
        if (status == "error" and msg and "结果上报失败" in msg
                and _CLIENT_TASKS[tid].get("type") == "chase-check"
                and _CLIENT_TASKS[tid].get("chase_retries", 0) < 3):
            # 先释放槽位与队列，再按 pending 重新排队
            if tid in _CLIENT_QUEUE:
                _CLIENT_QUEUE.remove(tid)
            if client_name and _CLIENT_ACTIVE.get(client_name, 0) > 0:
                _CLIENT_ACTIVE[client_name] -= 1
            _CLIENT_TASKS[tid]["chase_retries"] = _CLIENT_TASKS[tid].get("chase_retries", 0) + 1
            _CLIENT_TASKS[tid]["status"] = "pending"
            _CLIENT_TASKS[tid]["polled_by"] = ""
            _CLIENT_TASKS[tid]["retried"] = True
            _CLIENT_QUEUE.append(tid)
            return jsonify({"success": True, "retried": True})
        # 从队列移除
        if tid in _CLIENT_QUEUE:
            _CLIENT_QUEUE.remove(tid)
        # 递减客户端活跃计数（skipped 不扣减，因为没占过槽位）
        if status != "skipped" and client_name and _CLIENT_ACTIVE.get(client_name, 0) > 0:
            _CLIENT_ACTIVE[client_name] -= 1
        return jsonify({"success": True})
    return jsonify({"success": False, "msg": "任务不存在"}), 404

@bp.route("/api/client/chase_result", methods=["POST"])
def api_client_chase_result():
    """
    客户端上报追更检查结果。
    data: { chase_id, episodes: [ep_num, ...], siblings: {ep_num: filename}, error: str }
    如果是预览 chase（id 以 "preview-" 开头），则存入 _PREVIEW_RESULTS。
    """
    data = request.get_json(silent=True) or {}
    chase_id = data.get("chase_id", "")
    episodes = data.get("episodes", [])
    error = data.get("error", "")
    siblings = data.get("siblings", {})

    # ─── 预览模式 ───
    if chase_id.startswith("preview-") and chase_id in _PREVIEW_RESULTS:
        preview = _PREVIEW_RESULTS[chase_id]
        if error:
            preview["status"] = "error"
            preview["error"] = error
        else:
            ep_pattern = preview.get("ep_pattern", "")
            rename_template = preview.get("rename_template", "")
            season = preview.get("season", 0)
            base_ep = preview.get("base_ep", 0)
            tmdb_id = preview.get("tmdb_id", "")
            # 筛选 > base_ep 的集数
            matched = []
            for ep_str, fname in sorted(siblings.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                try:
                    ep_num = int(ep_str)
                except ValueError:
                    ep_num = ep_str
                # 只显示起始集数之后的新集
                if isinstance(ep_num, int) and ep_num <= base_ep:
                    continue
                renamed = _apply_rename_template(ep_num, rename_template, "Show", season, tmdb_id, fname)
                matched.append({
                    "filename": fname,
                    "ep_num": ep_num,
                    "matched_text": str(ep_num),
                    "renamed": renamed,
                })
            preview["status"] = "done"
            preview["files"] = list(siblings.values())
            preview["matched"] = matched
            preview["error"] = ""
        # 清理客户端任务
        for tid in list(_CLIENT_TASKS.keys()):
            t = _CLIENT_TASKS.get(tid, {})
            if t.get("chase_id") == chase_id:
                _CLIENT_TASKS.pop(tid, None)
        return jsonify({"success": True})

    # ─── 正常追更模式 ───
    with _CHASE_FILE_LOCK:
        result = _process_chase_result_locked(data, chase_id, episodes, error, siblings)
        if isinstance(result, tuple):
            return jsonify(result[0]), result[1]
        return jsonify(result)


def _process_chase_result_locked(data, chase_id, episodes, error, siblings):
    """chase_result 正常模式处理（调用方必须持有 _CHASE_FILE_LOCK）。

    锁内完成 读-判漏集-建任务-保存 全流程，避免与并发到达的 chase_done /
    管理端写请求互相覆盖，导致 done_eps 丢失、已上传集被漏集补追重复推送。
    """
    tasks = _load_chase()
    for t in tasks:
        if t.get("id") == chase_id:
            t["last_check"] = _beijing_now().strftime("%Y-%m-%d %H:%M:%S")
            # ── 追更已停止：不再创建任何下载/续扫任务，仅记录检查时间 ──
            if t.get("stopped"):
                _save_chase(tasks)
                return {"success": True, "stopped": True}
            # ── WPS 实际文件清单校准（漏集自愈核心）──
            # 客户端检查时列出 WPS 目标目录下重命名后的文件名，随结果上报 wps_files。
            # 服务端反解析集号持久化到 wps_eps：漏集判定把 wps_eps 视为"已存在"，
            # 即使 chase_done 上报丢失，只要文件真实落在 WPS 目录就不会再补漏。
            _wps_files = data.get("wps_files", []) or []
            if _wps_files and not (data.get("movie_files") or []):
                _ep_pat = t.get("ep_pattern", "")
                _wps_new = set()
                for _fn in _wps_files:
                    # 文件格式过滤：只反解析真实视频文件，忽略 jpg/png/txt 等附件
                    if not str(_fn).lower().endswith(_global_video_exts()):
                        continue
                    _epn, _ = _server_extract_episode(_fn, _ep_pat)
                    if _epn is None:
                        # 重命名后文件名可能与源文件名正则不匹配，回退自动识别
                        _epn, _ = _server_extract_episode(_fn, "")
                    if _epn is not None:
                        _wps_new.add(int(_epn))
                if _wps_new:
                    _wps_old = set(t.get("wps_eps", []) or [])
                    t["wps_eps"] = sorted(_wps_old | _wps_new)
                    _save_chase(tasks)
            # ── 剧集模式进度自动校准 ──
            # 分享内实际最大集数（排除年份等误报）。若 progress_ep 曾被误报推高
            # （如某次把 "xxx - 41.mp4" 这类文件误识别成第41集并下载成功），
            # 自动回落到真实水平，否则 check_from=max(base,prog) 会一直从虚高
            # 的进度开始查，后续真正的新集永远不被发现，造成永久漏追。
            if not (data.get("movie_files") or []):
                _base = t.get("base_ep", 0)
                _prog = t.get("progress_ep", 0)
                _share_max = 0
                for _k in siblings:
                    _s = str(_k).lstrip('-')
                    if _s.isdigit():
                        _v = int(_s)
                        if 0 < _v < 10000 and not (1900 <= _v <= 2100):
                            _share_max = max(_share_max, _v)
                if _share_max and _prog > max(_base, _share_max):
                    _old = _prog
                    # 回退到起始集与分享内最大集数中较小的一个，而不是 share_max。
                    # 否则若 16~23 实际从未下载过(只是进度被误报推高)，check_from
                    # 仍会跳过它们，造成中间集永久漏追。
                    t["progress_ep"] = min(_base, _share_max)
                    t["last_result"] = (f"进度自动校准: 分享内最大 {_share_max} 集，"
                                        f"已追至 {_old} 回退为 {t['progress_ep']}")
                    _append_chase_history(t, "calibrate", "warn", t["last_result"], eps=[t["progress_ep"]])
                    _save_chase(tasks)
                    return {"success": True, "calibrated": True}
            # ── 漏集补追：分享内存在但未成功上传的集数 ──
            # 与单集上报 done_eps 配合：客户端每成功上传一集，服务端把该集号
            # 持久化到 done_eps。每次检查拿分享内全部集数与 done_eps 对比，
            # 不在其中的即视为漏集，自动重新下发下载任务。这样即使某集失败
            # 重试耗尽、或被乱序成功集越过 progress_ep，下一次检查也会自愈，
            # 不会像过去那样永久漏追。
            # 老任务没有 done_eps 字段：以当前 progress_ep 为上限初始化
            # （校准已先行回退虚高进度，故这里初始化不会把 16~23 误判为已追）。
            if not (data.get("movie_files") or []) and not isinstance(t.get("done_eps"), list):
                _prog0 = t.get("progress_ep", 0)
                _base0 = t.get("base_ep", 0)
                t["done_eps"] = (list(range(_base0 + 1, _prog0 + 1))
                                 if _prog0 > _base0 else [])
            # 已存在下载任务（任意状态 pending/running）的集：漏集判定与派发时
            # 都要跳过，否则两个在线客户端各自上报一次检查结果时，同一集会被
            # 重复创建下载任务，导致多台客户端同时下载同一集。
            _existing_eps = set()
            for _tid0 in list(_CLIENT_QUEUE):
                _t0 = _CLIENT_TASKS.get(_tid0)
                if (_t0 and _t0.get("chase_id") == chase_id
                        and _t0.get("type") == "chase-download"):
                    if _t0.get("batch"):
                        # 批量任务：其包含的所有集都视为已派发，避免重复下发
                        for _f0 in (_t0.get("files") or []):
                            _e0 = _f0.get("ep_num")
                            if isinstance(_e0, int) and _e0 > 0:
                                _existing_eps.add(_e0)
                    else:
                        _ep0 = _t0.get("ep_num")
                        if isinstance(_ep0, int) and _ep0 > 0:
                            _existing_eps.add(_ep0)
            _missed = []
            if not (data.get("movie_files") or []):
                _done_eps = set(t.get("done_eps", []) or [])
                _wps_eps = set(t.get("wps_eps", []) or [])
                _abandoned = set(t.get("abandoned_eps", []) or [])
                _base = t.get("base_ep", 0)
                _manual_eps = t.get("manual_episodes", {}) or {}
                _known = set(int(x) for x in episodes)
                for _k, _fname in siblings.items():
                    try:
                        _ep = int(_k)
                    except (ValueError, TypeError):
                        continue
                    if (_ep <= _base or _ep in _done_eps or _ep in _wps_eps
                            or _ep in _known or _ep in _existing_eps):
                        continue
                    # 失败重试已耗尽的集不再自动补漏，避免每次检查重复下发
                    if _ep in _abandoned:
                        continue
                    # 最近被取消（用户停止）的集 60 秒内不补建：防止停止后重新
                    # 运行时"取消上报→漏集补建→再取消"刷屏
                    _cets0 = _CANCELED_EPS.get(chase_id, {})
                    _cct0 = _cets0.get(_ep, 0)
                    if _cct0 and time.time() - _cct0 < 60:
                        continue
                    if 1900 <= _ep <= 2100:
                        continue
                    _m = _manual_eps.get(_fname, {}) if _manual_eps else {}
                    if _m.get("skip"):
                        continue
                    _missed.append(_ep)
                _missed = sorted(set(_missed))
            # ── 指定集数强制推送（最高优先级）──
            # 用户明确指定了要推送的集数（episodes_filter）时，这是手动干预：
            # 无论该集是否已上传到 WPS（done_eps/wps_eps）、无论追更进度
            # （base_ep/progress_ep）如何、无论是否在补漏列表中，都无条件创建
            # 下载任务。只跳过已经在队列中待执行的任务（避免重复创建）。
            _ep_filter = (t.get("episodes_filter") or "").strip()
            if _ep_filter and not (data.get("movie_files") or []):
                _wanted_map = parse_episodes_filter(_ep_filter)  # {season: set(ep)}
                # 指定了具体季（key>0）时，需同时匹配文件名的季别
                _has_season_spec = any(s > 0 for s in _wanted_map)
                _forced = []
                for _k, _fname in siblings.items():
                    try:
                        _ep = int(_k)
                    except (ValueError, TypeError):
                        continue
                    if not _fname:
                        continue
                    # 从文件名提取季别（SxxExx 或 Sxx）；无季别标记视为 0（不分季）
                    _mfs = re.search(r'[Ss](\d{1,2})[Ee]\d+', _fname)
                    if not _mfs:
                        _mfs = re.search(r'[Ss](\d{1,2})', _fname)
                    _fseason = int(_mfs.group(1)) if _mfs else 0
                    _match = False
                    for _s, _eps in _wanted_map.items():
                        if _s == 0:
                            # 任意季（不分季）：只要集号命中即可
                            if _ep in _eps:
                                _match = True
                                break
                        else:
                            # 指定季：集号命中 且 文件季别一致
                            if _fseason == _s and _ep in _eps:
                                _match = True
                                break
                    if _match:
                        _forced.append(_ep)
                _forced = sorted(set(_forced))
                # 跳过已在队列中的集（任务已存在，无需重复创建）
                _forced = [ep for ep in _forced if ep not in _existing_eps]
                # ── 指定集数视为一次性补档（★14）：已成功上传的集不再重复强制推送 ──
                # 之前只要 episodes_filter 未清除，每次检查都会无条件重复推送该范围，
                # 导致 1-8 补完后又重下 1-8。现改为：
                #   1) 只推送"尚未成功"的集（排除 done_eps/wps_eps）；
                #   2) 指定集全部成功后自动清除 episodes_filter，恢复常规追更
                #      （本次检查即顺带做一次缺失/新集校验，从已追进度继续）。
                _done_eps = set(t.get("done_eps", []) or [])
                _wps_eps = set(t.get("wps_eps", []) or [])
                _wanted_all = set()
                for _s, _eps in _wanted_map.items():
                    _wanted_all |= _eps
                _need = [ep for ep in _forced
                         if ep not in _done_eps and ep not in _wps_eps]
                _all_done = bool(_wanted_all) and _wanted_all <= (_done_eps | _wps_eps)
                if _all_done:
                    # 指定集已全部完成：覆盖掉指定集数，恢复追更模式
                    _old_filter = t.get("episodes_filter", "")
                    t["episodes_filter"] = ""
                    _cf = max(t.get("base_ep", 0), t.get("progress_ep", 0))
                    t["last_result"] = (f"指定集数 {_old_filter} 已全部完成，恢复常规追更"
                                        f"(从第 {_cf} 集起)")
                    _append_chase_history(t, "filter", "ok", t["last_result"],
                                          eps=sorted(_done_eps | _wps_eps))
                    _save_chase(tasks)
                    # 不 return：继续走下方常规流程，本次检查即查缺失/新集
                elif _need:
                    with _chase_batch_lock:
                        _chase_batch_pending[chase_id] = set()
                    for _ep in _need:
                        _create_chase_download_task(t, _ep, siblings)
                    _skip_n = len(_forced) - len(_need)
                    t["last_result"] = (f"指定集数 {_ep_filter} 补档 {len(_need)} 集"
                                        f": {_need[0]}-{_need[-1]}"
                                        + (f"（{_skip_n} 集已完成跳过）" if _skip_n > 0 else ""))
                    _append_chase_history(t, "forced", "info", t["last_result"], eps=_need)
                    _save_chase(tasks)
                    return {"success": True, "forced": True}
                # 其余情况（无匹配集/无待补集）：继续走下方常规流程
            if error:
                t["last_result"] = f"检查失败: {error}"
                _append_chase_history(t, "check", "error", t["last_result"])
                t["last_error_type"] = _classify_chase_error(error)
                t["last_error"] = str(error)
                t["consecutive_failures"] = t.get("consecutive_failures", 0) + 1
                t["last_fail_ts"] = _beijing_now().strftime("%Y-%m-%d %H:%M:%S")
                send_notify(f"⚠️ 追更检查失败: {t.get('name', '')}",
                            f"原因: {error}", is_fail=True)
            elif not episodes and not _missed:
                _cf = max(t.get("base_ep", 0), t.get("progress_ep", 0))
                if _cf > t.get("base_ep", 0):
                    t["last_result"] = (f"无新集数 (检查起点 {_cf}，高于起始 {t.get('base_ep', 0)})")
                else:
                    t["last_result"] = f"无新集数 (当前 {_cf})"
                _append_chase_history(t, "check", "info", t["last_result"])
            else:
                eps_sorted = sorted(set(episodes) | set(_missed))
                # 防御：合并后仍剔除已成功上传的集与已放弃重试的集，
                # 防止校准回退/失败耗尽后重复下发
                if not (data.get("movie_files") or []):
                    _done_eps = set(t.get("done_eps", []) or [])
                    _wps_eps = set(t.get("wps_eps", []) or [])
                    _abandoned = set(t.get("abandoned_eps", []) or [])
                    eps_sorted = [ep for ep in eps_sorted
                                  if (ep not in _done_eps and ep not in _wps_eps
                                      and ep not in _abandoned and ep not in _existing_eps)]
                # ── 电影模式：按文件名去重，逐个视频文件派发下载 ──
                movie_files = data.get("movie_files", []) or []
                if movie_files:
                    downloaded_movies = set(t.get("downloaded_movies", []) or [])
                    siblings = data.get("siblings", {})
                    new_movies = [fn for fn in movie_files if fn not in downloaded_movies]
                    if not new_movies:
                        t["last_result"] = f"电影已全部下载({len(movie_files)}个)，无新文件"
                        _append_chase_history(t, "check", "info", t["last_result"])
                        _save_chase(tasks)
                        return {"success": True}
                    # 重置批次计数器
                    with _chase_batch_lock:
                        _chase_batch_pending[chase_id] = set()
                    valid = 0
                    for fn in new_movies:
                        # 用负序号作为 ep_num 占位，siblings 中以负序号为 key 存文件名
                        neg = -(valid + 1)
                        sib = {str(neg): fn}
                        _create_chase_download_task(t, neg, sib)
                        valid += 1
                    t["last_result"] = f"发现 {len(new_movies)} 个电影文件，已下发下载"
                    _append_chase_history(t, "check", "ok", t["last_result"])
                    _save_chase(tasks)
                    send_notify(f"🎬 追更到新资源: {t.get('name', '')}",
                                f"{t['last_result']}\n已下发 {len(new_movies)} 个下载任务。")
                    return {"success": True}

                # 剧集模式：应用指定集数筛选（如 "1,2,3" 或 "S02E01-S02E08"）
                ep_filter_str = t.get("episodes_filter", "").strip()
                if ep_filter_str:
                    try:
                        wanted_map = parse_episodes_filter(ep_filter_str)
                        if wanted_map:
                            # 带季范围（key>0）在正常检查中同样生效：需季别与集号都匹配
                            has_spec = any(s > 0 for s in wanted_map)
                            siblings = data.get("siblings", {}) or {}
                            before = len(eps_sorted)
                            filtered = []
                            for ep in eps_sorted:
                                ok = False
                                for s, epset in wanted_map.items():
                                    if ep not in epset:
                                        continue
                                    if s == 0 or not has_spec:
                                        ok = True
                                        break
                                    # 指定季：需要从分享内文件名反查该集所属季
                                    fn = (siblings.get(str(ep), "") if siblings else "") or ""
                                    mfs = re.search(r'[Ss](\d{1,2})[Ee]\d+', fn)
                                    if not mfs:
                                        mfs = re.search(r'[Ss](\d{1,2})', fn)
                                    fseason = int(mfs.group(1)) if mfs else 0
                                    if fseason == s:
                                        ok = True
                                        break
                                if ok:
                                    filtered.append(ep)
                            eps_sorted = filtered
                            t["last_result"] = (f"发现 {before} 集，按筛选仅推送 {len(eps_sorted)} 集"
                                                if eps_sorted else
                                                f"筛选 {ep_filter_str} 无匹配集 (共 {before} 集)")
                            _append_chase_history(t, "filter", "info", t["last_result"], eps=eps_sorted)
                    except Exception:
                        pass
                if not eps_sorted:
                    _save_chase(tasks)
                    return {"success": True}
                if _missed:
                    t["last_result"] = (f"发现 {len(eps_sorted)} 集(含补漏 {len(_missed)} 集 "
                                        f"{_missed[0]}-{_missed[-1]}): {eps_sorted[0]}-{eps_sorted[-1]}")
                else:
                    t["last_result"] = f"发现 {len(eps_sorted)} 集: {eps_sorted[0]}-{eps_sorted[-1]}"
                _append_chase_history(t, "check", "ok", t["last_result"], eps=eps_sorted)
                # 为新剧集创建下载任务（重置批次计数器，避免上次遗留数据污染）
                with _chase_batch_lock:
                    _chase_batch_pending[chase_id] = set()
                ep_pattern = t.get("ep_pattern", "")
                siblings = data.get("siblings", {})
                manual_eps = t.get("manual_episodes", {}) or {}
                valid_count = 0
                skipped_count = 0
                items = []  # (ep_num, siblings, manual) 按下载顺序
                for ep_num in eps_sorted:
                    # 最终防线：跳过已存在下载任务的集，避免并发检查重复下发
                    if ep_num in _existing_eps:
                        continue
                    # 服务端二次校验：确保文件名匹配 ep_pattern，防止客户端误报
                    if ep_pattern:
                        fname = siblings.get(str(ep_num), "")
                        # 文件格式过滤：jpg/png/txt 等附件即使命中正则也不算剧集
                        if not str(fname).lower().endswith(_global_video_exts()):
                            continue
                        sv_ep, _ = _server_extract_episode(fname, ep_pattern)
                        if sv_ep is None:
                            continue
                    # 手动设置：skip → 跳过本集不下载；ep 覆盖 → 用人工集数
                    fname = siblings.get(str(ep_num), "")
                    m = manual_eps.get(fname, {}) if manual_eps else {}
                    if m.get("skip"):
                        skipped_count += 1
                        continue
                    real_ep = ep_num
                    if m.get("ep") is not None:
                        real_ep = int(m["ep"])
                    items.append((real_ep, siblings, m))
                    valid_count += 1
                # 打包为批量任务下发：同路径多集由客户端流水线处理（上传本集时并行下载下一集），
                # 单集或跨路径时自动回退为单集任务，行为不变。
                _create_chase_download_batch_tasks(t, items)
                if valid_count < len(eps_sorted):
                    if valid_count == 0:
                        t["last_result"] = (f"发现 {len(eps_sorted)} 集但均不匹配 ep_pattern ({ep_pattern})，未下发下载"
                                            if ep_pattern else
                                            f"发现 {len(eps_sorted)} 集，已下发 0 集")
                    else:
                        t["last_result"] = f"发现 {len(eps_sorted)} 集，已下发 {valid_count} 集 ({len(eps_sorted)-valid_count} 集不匹配ep_pattern)"
                    _append_chase_history(t, "check", "warn", t["last_result"], eps=eps_sorted)
                if valid_count > 0:
                    send_notify(f"📺 追更到新集: {t.get('name', '')}",
                                f"{t['last_result']}\n已下发 {valid_count} 个下载任务。")
            _save_chase(tasks)
            return {"success": True}
    return {"success": False, "message": "追更任务未找到"}, 404

@bp.route("/api/client/chase_done", methods=["POST"])
def api_client_chase_done():
    """
    客户端上报某一集下载完成，更新追更进度。
    data: { chase_id, ep_num, ok: bool, error: str }
    """
    data = request.get_json(silent=True) or {}
    chase_id = data.get("chase_id", "")
    ep_num = data.get("ep_num", 0)
    ok = data.get("ok", False)

    if ok and ep_num:
        with _CHASE_FILE_LOCK:
            tasks = _load_chase()
            for t in tasks:
                if t.get("id") == chase_id:
                    # 电影模式：下载完成后把文件名记入 downloaded_movies，避免重复下载
                    fn = data.get("file_name", "")
                    if fn:
                        dm = set(t.get("downloaded_movies", []) or [])
                        if fn not in dm:
                            dm.add(fn)
                            t["downloaded_movies"] = list(dm)
                    # 兼容旧数据：base_ep 可能同时被当做起始集数和进度，优先取 progress_ep
                    prev_progress = max(t.get("base_ep", 0), t.get("progress_ep", 0))
                    if ep_num > prev_progress:
                        t["progress_ep"] = ep_num
                        t["last_result"] = f"已追到第 {ep_num} 集"
                        _append_chase_history(t, "download", "ok", t["last_result"], eps=[ep_num])
                        prev_progress = ep_num
                    # 成功后清除该集的失败计数与"已放弃"标记
                    t.get("failed_eps", {}).pop(str(ep_num), None)
                    abandoned = t.get("abandoned_eps", []) or []
                    if int(ep_num) in abandoned:
                        abandoned.remove(int(ep_num))
                    # 记录最后成功时间并清零连续失败计数（用于失败预警）
                    t["last_success_ts"] = _beijing_now().strftime("%Y-%m-%d %H:%M:%S")
                    t["consecutive_failures"] = 0
                    t["last_error_type"] = ""
                    send_notify(f"✅ 下载完成: {t.get('name', '')}",
                                f"第 {ep_num} 集已下载并上传 WPS。",
                                event="on_download")
                    # 记录已成功上传的集数(用于漏集检测与展示)
                    done_eps = t.setdefault("done_eps", [])
                    if int(ep_num) not in done_eps:
                        done_eps.append(int(ep_num))
                        done_eps.sort()
                    _save_chase(tasks)

                    # 追更批次计数器：移除此集
                    with _chase_batch_lock:
                        if chase_id in _chase_batch_pending:
                            _chase_batch_pending[chase_id].discard(ep_num)
                            # 如果该批次所有下载任务都完成了，触发对应 STRM
                            if not _chase_batch_pending[chase_id]:
                                del _chase_batch_pending[chase_id]
                                _trigger_strm_for_chase_idempotent(t)

                    # ── 自动续扫：下载完一集后立即重新检查，追到最新为止 ──
                    # 仅当任务仍启用、未手动停止、且距上次自动续扫超过冷却时间，
                    # 避免分享未更新时无限空扫 / 抖动。
                    if t.get("enabled", True) and not t.get("stopped", False):
                        _maybe_rescan_chase(t)

                    return jsonify({"success": True, "base_ep": prev_progress, "progress_ep": prev_progress})

    # ── 失败重试：下载失败后自动重新下发该集下载任务（限次）──
    # 否则失败集会一直被后续成功集的 progress_ep 越过，check_from 变高后
    # 永远不会再次被判定为"新集"，导致永久漏追。
    if not ok and chase_id:
        with _CHASE_FILE_LOCK:
            tasks = _load_chase()
            for t in tasks:
                if t.get("id") != chase_id:
                    continue
                retried = False
                err_text = data.get("error", "") or ""
                # ── 主动取消（用户停止/取消指令）不算失败：不记 failed_eps、
                # 不自动重试下发。否则停止后重新运行时，客户端取消状态残留导致
                # 新任务被取消 → 上报"已取消"(带file_name) → 误入失败重试 →
                # 5秒后重新下发同一集 → 再取消 → 无限刷屏。 ──
                if "取消" in err_text:
                    _eid = int(ep_num) if isinstance(ep_num, int) else 0
                    _CANCELED_EPS.setdefault(chase_id, {})[_eid] = time.time()
                    t["last_result"] = (f"第{ep_num}集已取消(用户停止)，"
                                        f"重新运行后可重新下载")
                    if _eid > 0:
                        _append_chase_history(t, "download", "warn", t["last_result"], eps=[_eid])
                    # 从批次计数中移除，避免阻塞批次其余集的 STRM 触发
                    with _chase_batch_lock:
                        if chase_id in _chase_batch_pending:
                            _chase_batch_pending[chase_id].discard(ep_num)
                    _save_chase(tasks)
                    return jsonify({"success": True, "canceled": True, "message": "已取消"})
                if t.get("enabled", True) and not t.get("stopped", False):
                    fn = data.get("file_name", "")
                    if fn:
                        failed = t.setdefault("failed_eps", {})
                        cnt = failed.get(str(ep_num), 0)
                        MAX_RETRY = 3
                        # 错误归类 + 连续失败计数（用于卡片预警）
                        err_type = _classify_chase_error(err_text)
                        t["last_error_type"] = err_type
                        t["last_error"] = err_text
                        t["consecutive_failures"] = t.get("consecutive_failures", 0) + 1
                        t["last_fail_ts"] = _beijing_now().strftime("%Y-%m-%d %H:%M:%S")
                        if cnt < MAX_RETRY:
                            failed[str(ep_num)] = cnt + 1
                            t["last_result"] = (f"第{ep_num}集下载失败(第{cnt+1}/{MAX_RETRY}次)，"
                                                f"自动重试中: {err_text}")
                            _append_chase_history(t, "download", "warn", t["last_result"], eps=[ep_num])
                            _save_chase(tasks)
                            # 指数退避：第 N 次重试延迟 5*2^(N-1) 秒（5s/10s/20s…），
                            # 给限流/Cookie 刷新留出时间，避免风暴式重试
                            backoff = 5 * (2 ** cnt)
                            _create_chase_download_task(t, int(ep_num), {str(ep_num): fn},
                                                        run_after=time.time() + backoff)
                            retried = True
                        else:
                            t["last_result"] = (f"第{ep_num}集下载失败{MAX_RETRY}次，已放弃重试: "
                                                f"{err_text}")
                            _append_chase_history(t, "download", "error", t["last_result"], eps=[ep_num])
                            send_notify(f"❌ 下载失败已放弃: {t.get('name', '')}",
                                        f"第 {ep_num} 集重试 {MAX_RETRY} 次仍失败。\n"
                                        f"错误类型: {_CHASE_ERROR_HINTS.get(err_type, err_type)}\n"
                                        f"详情: {err_text}",
                                        is_fail=True)
                            # 从批次计数中移除，避免永久阻塞该批次其余集的 STRM 触发
                            with _chase_batch_lock:
                                if chase_id in _chase_batch_pending:
                                    _chase_batch_pending[chase_id].discard(ep_num)
                            # 持久化"已放弃"标记，防止漏集补追每次检查都重新下发该集
                            # 造成无限循环（失败重试耗尽≠分享内无此集，需人工确认）
                            abandoned = t.setdefault("abandoned_eps", [])
                            if int(ep_num) not in abandoned:
                                abandoned.append(int(ep_num))
                                abandoned.sort()
                            _save_chase(tasks)
                return jsonify({"success": False, "retry": retried,
                                "message": data.get("error", "")})
    return jsonify({"success": ok, "message": data.get("error", "")})
