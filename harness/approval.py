"""审批：策略判为 ask 的工具调用，问人「这一次能不能做」。

结果是闭集（OUTCOMES），只有 allowed-once 放行且只管这一次调用；其余一律拒绝（失败即拒绝）：
  没有应答者 / 发卡片失败 / 没配审批人 / 超时 / run 策略为 never —— 都不放行。

持久任务：落库 pending → 发卡片 → 保存 waiting_approval 并释放 worker；
回调校验应用/审批人/期限 → 原子 pending→终态 → 唤醒任务，恢复后重新验证动作。
旧的无任务调用保留短时 threading.Event 等待；只有这些审批在启动时取消。

harness 不 import 飞书：由 feishu/bot.py 启动时 register_answerer(ask, finalize) 把发卡片 / 改卡片挂进来。
"""
import threading
import time
import uuid

from core import store

OUTCOMES = ("allowed-once", "rejected", "cancelled", "unavailable", "expired")
DEFAULT_TIMEOUT = 600          # 秒；更长的等待走业务审批，别占线程

_ask = None                    # fn(req: dict) -> message_id | None：发审批卡片
_finalize = None               # fn(row: dict) -> None：把卡片改成终态（超时/重启失效时用）
_waiters = {}                  # approval_id -> threading.Event
_waiters_lock = threading.Lock()


def register_answerer(ask, finalize=None):
    global _ask, _finalize
    _ask, _finalize = ask, finalize


def _finish(rec, row, outcome, note=""):
    rec.emit("approval/decided", {"approval_id": row["id"] if row else None, "outcome": outcome,
                                  "decider_open_id": (row or {}).get("decider_open_id") or "",
                                  "note": note},
             call_id=(row or {}).get("call_id"), tool=(row or {}).get("tool"))
    return outcome


def request(rec, call, app, reason, timeout=None):
    """任务审批持久等待并释放 worker；非任务调用保留短时阻塞兼容。"""
    from . import tasks
    if tasks.current() is not None:
        return _request_durable(rec, call, app, reason, timeout)
    return _request_legacy(rec, call, app, reason, timeout)


def _outcome(rec, row):
    outcome = row["status"] if row and row["status"] in OUTCOMES else "unavailable"
    msgs = {"allowed-once": "已获批准。", "rejected": "审批人拒绝了这个操作。",
            "cancelled": "审批已取消。", "expired": "审批已超时。", "unavailable": "审批不可用。"}
    return _finish(rec, row, outcome), msgs[outcome]


def _request_durable(rec, call, app, reason, timeout=None):
    from core import taskstore
    from . import tasks, pipeline
    scope = tasks.current()
    tasks.check_active()
    action = taskstore.action_get(scope["id"], call.extra["action_key"])
    if not action:
        raise RuntimeError("审批前必须保存动作")
    if getattr(rec, "approval_policy", "never") != "ask":
        return "rejected", f"本次触发方式 {rec.trigger} 不允许请求审批。"
    if not app.get("approvers"):
        return "unavailable", "应用未配置审批人，已拒绝。"
    lease = {"lease_owner": scope["lease_owner"], "lease_epoch": scope["lease_epoch"]}
    if action.get("approval_id"):
        row = store.get_approval(action["approval_id"])
        if not row or row["app_id"] != scope["app_id"] or row["args"] != call.args:
            return "unavailable", "持久审批与动作不匹配，已拒绝。"
        expires = (action.get("payload") or {}).get("expires_at", 0)
        if time.time() >= expires:
            store.decide_approval(row["id"], "expired")
            _safe_finalize(store.get_approval(row["id"]))
            return "expired", "动作审批已超过有效期，请重新准备动作。"
        if row["status"] == "pending":
            if not row.get("message_id"):
                # 进程可能在发送卡片与保存回执之间中断。不要假设未发送而重复发卡。
                store.decide_approval(row["id"], "unavailable")
                return "unavailable", "上次审批卡片投递结果未确认，请重新准备动作。"
            raise tasks.TaskSuspended("waiting_approval", f"等待审批 {row['id']}")
        if row["status"] == "allowed-once" and row.get("decider_open_id") not in app["approvers"]:
            return "rejected", "批准人的权限已撤销，原审批不再有效。"
        return _outcome(rec, row)

    timeout = max(1, int(timeout or DEFAULT_TIMEOUT))
    aid = uuid.uuid4().hex
    requester = getattr(rec, "speaker_open_id", "") or ""
    try:
        shown = pipeline.preview(call)
    except Exception:
        return "unavailable", "无法生成完整动作预览，已拒绝审批。"
    store.create_approval(aid, "tool", rec.id, call.id, app["id"], rec.chat_id, call.tool,
                          call.risk, call.args, reason, requester)
    taskstore.action_update(scope["id"], action["action_key"], state="waiting_approval",
                            approval_id=aid, payload=dict(action.get("payload") or {},
                            expires_at=time.time() + timeout), **lease)
    rec.emit("approval/asked", {"approval_id": aid, "reason": reason,
                                "approvers": app["approvers"], "timeout_s": timeout,
                                "digest": action.get("digest")}, call_id=call.id, tool=call.tool)
    req = {"approval_id": aid, "app": app, "chat_id": rec.chat_id, "tool": call.tool,
           "args": call.args, "risk": call.risk, "reason": reason, "preview": shown,
           "requester_open_id": requester, "approvers": app["approvers"], "timeout_s": timeout}
    try:
        mid = _ask(req) if _ask else None
    except Exception:
        mid = None
    if not mid:
        store.decide_approval(aid, "unavailable")
        return _outcome(rec, store.get_approval(aid))
    store.set_approval_message(aid, mid)
    raise tasks.TaskSuspended("waiting_approval", f"等待审批 {aid}")


def _request_legacy(rec, call, app, reason, timeout=None):
    """为一次工具调用请求审批，阻塞到有结果。返回 (outcome, 说明)。"""
    timeout = int(timeout or DEFAULT_TIMEOUT)
    if getattr(rec, "approval_policy", "never") != "ask":
        rec.emit("approval/decided", {"approval_id": None, "outcome": "rejected",
                                      "note": f"本次触发方式（{rec.trigger}）不发审批，按拒绝处理"},
                 call_id=call.id, tool=call.tool)
        return "rejected", f"这次是「{rec.trigger}」触发的，没人在线审批，需要审批的操作一律不做。"
    approvers = [x for x in (app.get("approvers") or []) if x]
    if not approvers:
        rec.emit("approval/decided", {"approval_id": None, "outcome": "unavailable",
                                      "note": "应用没配审批人"}, call_id=call.id, tool=call.tool)
        return "unavailable", "这个应用还没配置审批人，需要审批的操作没人能批，已拒绝。请管理员在后台给应用加审批人。"

    aid = uuid.uuid4().hex
    requester = getattr(rec, "speaker_open_id", "") or ""
    store.create_approval(aid, "tool", rec.id, call.id, app["id"], rec.chat_id, call.tool, call.risk,
                          call.args, reason, requester)
    rec.emit("approval/asked", {"approval_id": aid, "reason": reason, "approvers": approvers,
                                "timeout_s": timeout}, call_id=call.id, tool=call.tool)
    ev = threading.Event()
    with _waiters_lock:
        _waiters[aid] = ev
    try:
        if _ask is None:
            store.decide_approval(aid, "unavailable")
            return _finish(rec, store.get_approval(aid), "unavailable", "没有注册审批应答者"), \
                "审批通道不可用，已拒绝。"
        from . import pipeline
        try:
            preview = pipeline.preview(call)
        except Exception:
            store.decide_approval(aid, "unavailable")
            return _finish(rec, store.get_approval(aid), "unavailable"), "无法生成完整动作预览，已拒绝审批。"
        req = {"approval_id": aid, "app": app, "chat_id": rec.chat_id, "tool": call.tool,
               "args": call.args, "risk": call.risk, "reason": reason, "preview": preview,
               "requester_open_id": requester, "approvers": approvers, "timeout_s": timeout}
        try:
            mid = _ask(req)
        except Exception as e:
            mid = None
            print(f"[审批 {aid[:8]}] 发卡片异常:", e, flush=True)
        if not mid:
            store.decide_approval(aid, "unavailable")
            return _finish(rec, store.get_approval(aid), "unavailable", "审批卡片发送失败"), \
                "审批卡片没发出去，已拒绝。"
        store.set_approval_message(aid, mid)

        if not ev.wait(timeout) and store.decide_approval(aid, "expired"):
            row = store.get_approval(aid)
            _safe_finalize(row)
            waited = f"{timeout // 60} 分钟" if timeout >= 60 else f"{timeout} 秒"
            return _finish(rec, row, "expired"), f"等了 {waited}没人审批，已按拒绝处理。"
        row = store.get_approval(aid)
        outcome = row["status"] if row and row["status"] in OUTCOMES else "unavailable"
        msgs = {"allowed-once": "已获批准。", "rejected": "审批人拒绝了这个操作。",
                "cancelled": "审批已取消。", "expired": "审批已超时。", "unavailable": "审批不可用。"}
        return _finish(rec, row, outcome), msgs[outcome]
    finally:
        with _waiters_lock:
            _waiters.pop(aid, None)


def _safe_finalize(row):
    if _finalize and row:
        try:
            _finalize(row)
        except Exception as e:
            print(f"[审批 {row['id'][:8]}] 更新卡片失败:", e, flush=True)


def decide(approval_id, approve, operator_open_id, app_id=None):
    """审批人点了按钮（飞书回调线程调）。返回 (是否生效, 给点击人的提示, 最新行)。必须快（3 秒内回飞书）。"""
    row = store.get_approval(approval_id)
    if not row:
        return False, "找不到这个审批（可能已过期清理）。", None
    if app_id is not None and row["app_id"] != app_id:
        return False, "审批不属于当前应用。", None
    if row["status"] != "pending":
        return False, f"这个审批已经处理过了（{row['status']}）。", row
    app = store.get_app(row["app_id"]) or {}
    if not app.get("enabled"):
        return False, "应用已停用，不能批准操作。", row
    if operator_open_id not in (app.get("approvers") or []):
        return False, "你不在这个应用的审批人名单里，不能审批。", row
    if approve and row.get("risk") == "exec" and operator_open_id == row.get("requester_open_id"):
        return False, "执行类操作不能由发起人自己批准，请让其他审批人处理。", row
    from core import taskstore
    from . import tasks
    action = taskstore.action_by_approval(approval_id)
    if action:
        task = tasks.get(action["task_id"])
        if not task or task.get("cancel_requested") or task["status"] not in ("running", "waiting_approval", "queued"):
            return False, "任务已结束或取消，不能审批。", row
        if time.time() >= (action.get("payload") or {}).get("expires_at", 0):
            store.decide_approval(approval_id, "expired")
            tasks.wake(action["task_id"])
            return False, "审批已经过期。", store.get_approval(approval_id)
    status = "allowed-once" if approve else "rejected"
    if not store.decide_approval(approval_id, status, operator_open_id):
        row = store.get_approval(approval_id)
        return False, f"这个审批已经处理过了（{row['status'] if row else '未知'}）。", row
    with _waiters_lock:
        ev = _waiters.get(approval_id)
    if ev:
        ev.set()
    if action:
        tasks.wake(action["task_id"])
    return True, ("已批准，本次操作将执行。" if approve else "已拒绝。"), store.get_approval(approval_id)


def cancel_stale():
    """只取消没有持久任务归属的旧审批；任务审批在重启后继续等待。"""
    from core import taskstore
    taskstore.init()
    ids = store._conn.execute("SELECT id FROM approvals WHERE status='pending' AND id NOT IN "
                              "(SELECT approval_id FROM task_actions WHERE approval_id IS NOT NULL)").fetchall()
    rows = [store.get_approval(r["id"]) for r in ids]
    for r in rows:
        store.decide_approval(r["id"], "cancelled")
        r["status"] = "cancelled"
        _safe_finalize(r)
    if rows:
        print(f"★ 已把 {len(rows)} 个遗留审批标为 cancelled", flush=True)
    return rows


def expire_due():
    """定时维护持久审批期限，无需占用等待线程。"""
    from core import taskstore
    from . import tasks
    rows = store._conn.execute("SELECT p.id FROM approvals p JOIN task_actions a ON a.approval_id=p.id "
                               "WHERE p.status='pending'").fetchall()
    count = 0
    for r in rows:
        action = taskstore.action_by_approval(r["id"])
        if time.time() >= (action.get("payload") or {}).get("expires_at", 0):
            if store.decide_approval(r["id"], "expired"):
                count += 1
                tasks.wake(action["task_id"])
                _safe_finalize(store.get_approval(r["id"]))
    return count
