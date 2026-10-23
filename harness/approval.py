"""审批：策略判为 ask 的工具调用，问人「这一次能不能做」。

结果是闭集（OUTCOMES），只有 allowed-once 放行且只管这一次调用；其余一律拒绝（失败即拒绝）：
  没有应答者 / 应答者发卡片失败 / 没配审批人 / 超时 / run 的策略是 never / 服务重启 —— 都不放行。

流程（全同步线程，不引入 asyncio）：
  工具线程 request() → 落库 pending + 写 approval/asked → 应答者发卡片 → 在 threading.Event 上等
  飞书回调线程 decide() → 校验审批人 → 原子 pending→终态 → set() 唤醒工具线程
  工具线程醒来读终态 → 写 approval/decided → 返回结果

harness 不 import 飞书：由 feishu/bot.py 启动时 register_answerer(ask, finalize) 把发卡片 / 改卡片挂进来。
"""
import threading
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
        preview = None
        fn = (getattr(call, "extra", None) or {}).get("preview")
        if callable(fn):
            try:
                preview = fn()
            except Exception as e:
                preview = f"（预览生成失败：{e}）"
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


def decide(approval_id, approve, operator_open_id):
    """审批人点了按钮（飞书回调线程调）。返回 (是否生效, 给点击人的提示, 最新行)。必须快（3 秒内回飞书）。"""
    row = store.get_approval(approval_id)
    if not row:
        return False, "找不到这个审批（可能已过期清理）。", None
    if row["status"] != "pending":
        return False, f"这个审批已经处理过了（{row['status']}）。", row
    app = store.get_app(row["app_id"]) or {}
    if operator_open_id not in (app.get("approvers") or []):
        return False, "你不在这个应用的审批人名单里，不能审批。", row
    if approve and row.get("risk") == "exec" and operator_open_id == row.get("requester_open_id"):
        return False, "执行类操作不能由发起人自己批准，请让其他审批人处理。", row
    status = "allowed-once" if approve else "rejected"
    if not store.decide_approval(approval_id, status, operator_open_id):
        row = store.get_approval(approval_id)
        return False, f"这个审批已经处理过了（{row['status'] if row else '未知'}）。", row
    with _waiters_lock:
        ev = _waiters.get(approval_id)
    if ev:
        ev.set()
    return True, ("已批准，本次操作将执行。" if approve else "已拒绝。"), store.get_approval(approval_id)


def cancel_stale():
    """进程启动时调：上次遗留的 pending 改为 cancelled，并尽力把卡片改成「已失效」。"""
    rows = store.cancel_pending_approvals()
    for r in rows:
        r["status"] = "cancelled"
        _safe_finalize(r)
    if rows:
        print(f"★ 已把 {len(rows)} 个遗留审批标为 cancelled", flush=True)
    return rows
