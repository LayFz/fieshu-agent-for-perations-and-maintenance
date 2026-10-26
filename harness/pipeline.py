"""统一工具管道：策略/guard、持久动作、审批、执行与结果记录。

有副作用的动作必须先落库，超时或中断进入待核对，不能由模型盲目重试。
线程超时只停止等待；代码沙箱的实际终止由独立执行器负责。
"""
import contextvars
import copy
import hashlib
import inspect
import json
import threading
import time
from dataclasses import dataclass, field

from . import policy

RESULT_PREVIEW = 4000
DEFAULT_TIMEOUT = 120


@dataclass
class ToolCall:
    id: str
    tool: str
    args: dict
    risk: str = "exec"
    external: bool = False
    timeout: int = DEFAULT_TIMEOUT
    exposed: bool = True
    raw_args: str = None
    trigger: str = ""
    app_id: int = None
    extra: dict = field(default_factory=dict)


def _run_with_timeout(fn, args, timeout):
    """不把等待超时描述成取消；执行结果不明时由调用者停止有副作用任务。"""
    box = {}
    ctx = contextvars.copy_context()

    def target():
        try:
            box["result"] = ctx.run(fn, copy.deepcopy(args))
        except Exception as e:
            box["exception"] = e

    th = threading.Thread(target=target, name="tool-exec", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return {"error": f"工具等待超过 {timeout} 秒；执行可能仍在继续，需核对实际结果。",
                "outcome": "unknown", "timed_out": True, "retry_safe": False}
    if "exception" in box:
        from . import tasks
        exc = box["exception"]
        if isinstance(exc, (tasks.TaskCancelled, tasks.TaskSuspended, tasks.TaskUncertain)):
            raise exc
        return {"error": f"工具执行出错：{type(exc).__name__}: {exc}",
                "outcome": "unknown", "retry_safe": False}
    return box.get("result")


def _denied_result(by, name, reason):
    return {"error": f"调用被拒绝，没有执行。原因：{reason}", "denied_by": f"{by}:{name}"}


def _digest(tool, args):
    body = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def preview(call):
    """新预览函数接收规范后的参数；保留旧无参回调兼容。"""
    fn = call.extra.get("preview")
    if not callable(fn):
        return None
    params = inspect.signature(fn).parameters
    if params and next(iter(params.values())).default is inspect.Parameter.empty:
        return fn(copy.deepcopy(call.args))
    return fn()


def _result(rec, call, result, status, started):
    content = json.dumps(result, ensure_ascii=False, default=str)
    rec.emit("tool/result", {"status": status,
             "duration_ms": int((time.time() - started) * 1000), "chars": len(content),
             "result": content[:RESULT_PREVIEW], "truncated": len(content) > RESULT_PREVIEW},
             call_id=call.id, tool=call.tool)
    return content


def run_tool(rec, call, app_policy, executor, app=None):
    from core import taskstore, store
    from . import tasks

    started = time.time()
    tasks.check_active()
    scope = tasks.current()
    key = call.extra.get("action_key") or call.id
    lease = ({"lease_owner": scope["lease_owner"], "lease_epoch": scope["lease_epoch"]}
             if scope else {})
    action = taskstore.action_get(scope["id"], key) if scope else None
    call.args = copy.deepcopy(call.args)
    call.extra["tainted"] = bool(getattr(rec, "tainted", False))

    def update(**fields):
        if scope:
            return taskstore.action_update(scope["id"], key, **lease, **fields)

    def deny(reason, by="guard", name="validation", persist=True):
        content = _result(rec, call, _denied_result(by, name, reason), "denied", started)
        if action and persist:
            update(state="denied", result=content)
        return content

    if call.raw_args is not None or not isinstance(call.args, dict):
        rec.emit("tool/call", {"parse_error": True, "risk": call.risk}, call_id=call.id, tool=call.tool)
        return deny("工具参数不是有效的 JSON 对象，必须重新生成有效参数。")
    if action:
        if action["tool"] != call.tool or action["risk"] != call.risk:
            raise tasks.TaskUncertain("恢复动作的工具或风险等级发生变化，需要人工核对。")
        call.args = copy.deepcopy(action["args"])
        if action["state"] in ("completed", "denied"):
            # 已执行结果的重放也是一次读取；撤权后不再把历史敏感结果送给模型。
            fresh = store.get_app(scope["app_id"])
            if not call.exposed or policy.decide(fresh.get("policy"), call.tool, call.risk)[0] == "deny":
                return deny("当前应用权限不允许读取此动作的历史结果。", "guard", "replay_access", persist=False)
            validate = call.extra.get("validate")
            if callable(validate):
                try:
                    if validate(copy.deepcopy(call.args)) is False:
                        raise ValueError("当前授权或动作版本无效")
                except Exception as e:
                    return deny(f"历史动作结果已不可访问：{e}", "guard", "replay_access", persist=False)
            if call.external and action["state"] == "completed":
                rec.tainted = True
            return action["result"]
        if action["state"] in ("running", "uncertain") and call.risk != "read":
            raise tasks.TaskUncertain(f"动作 {call.tool} 的上次执行结果未确认，禁止自动重复执行。")
    else:
        prepare = call.extra.get("prepare")
        if callable(prepare):
            try:
                call.args = prepare(copy.deepcopy(call.args))
                if not isinstance(call.args, dict):
                    raise ValueError("动作准备必须返回参数对象")
            except Exception as e:
                return deny(f"动作准备失败：{e}")
        if scope:
            action = taskstore.action_prepare(scope["id"], key, call.tool, call.args, call.risk,
                                              digest=_digest(call.tool, call.args), **lease)
    call.extra["action"] = action
    call.extra["action_key"] = key
    rec.emit("tool/call", {"args": call.args, "risk": call.risk, "external": call.external},
             call_id=call.id, tool=call.tool)

    # 恢复后读取最新策略，不沿用审批前快照中的权限。
    if scope:
        fresh = store.get_app(scope["app_id"])
        if not fresh or not fresh["enabled"]:
            raise tasks.TaskCancelled("应用已停用或删除")
        app, app_policy = fresh, fresh.get("policy")
    decision, rule_no, preset = policy.decide(app_policy, call.tool, call.risk)
    reason = f"策略 {preset} 第 {rule_no} 条规则：{decision} {call.risk}"
    # 外部内容驱动的 MR 必须由人批准，不能通过 compat 自动对外提交。
    if decision == "allow" and call.extra["tainted"] and call.tool == "code_propose_mr":
        decision, reason = "ask", "外部内容参与的代码变更必须批准对应快照后再提 MR。"
    if decision == "allow" and action and action.get("approval_id"):
        decision, reason = "ask", "已有审批必须按其持久结果处理，不能跳过拒绝或过期决定。"
    hit = policy.check_guards(call)
    if hit:
        decision, reason = "deny", hit[1]
    rec.emit("tool/decision", {"decision": decision, "preset": preset, "rule": rule_no,
                              "reason": reason, "guard": hit[0] if hit else None},
             call_id=call.id, tool=call.tool)
    if decision == "deny":
        return deny(reason, "guard" if hit else "policy", hit[0] if hit else preset)
    if decision == "ask":
        from . import approval
        if app is None:
            return deny("没有应用上下文，无法发起审批。", "approval", "unavailable")
        outcome, note = approval.request(rec, call, app, reason)
        if outcome != "allowed-once":
            return deny(note, "approval", outcome)
        # 审批等待期间环境/权限可变化，批准不能绕过最新 guard。
        tasks.check_active()
        hit = policy.check_guards(call)
        if hit:
            return deny(hit[1], "guard", hit[0])
        if scope:
            fresh = store.get_app(scope["app_id"])
            current_decision, _, _ = policy.decide(fresh.get("policy"), call.tool, call.risk)
            if current_decision == "deny":
                return deny("审批后应用策略已拒绝此操作。", "policy", "changed")
    validate = call.extra.get("validate")
    if callable(validate):
        try:
            validated = validate(copy.deepcopy(call.args))
            if validated is False:
                raise ValueError("动作快照已变化")
        except Exception as e:
            return deny(f"执行前校验失败，原审批失效：{e}", "guard", "snapshot")
    tasks.check_active()
    if action and action.get("digest") != _digest(call.tool, call.args):
        return deny("动作参数在审批后发生变化，拒绝执行。", "guard", "digest")
    if action:
        update(state="running")
    result = _run_with_timeout(executor, call.args, call.timeout)
    if call.external:
        # 远端错误正文也属于外部文本，不能借“请求失败”绕过来源限制。
        rec.tainted = True
    is_error = isinstance(result, dict) and "error" in result
    uncertain = is_error and call.risk != "read"
    content = json.dumps(result, ensure_ascii=False, default=str)
    if action:
        update(state="uncertain" if uncertain else "completed", result=content)
    _result(rec, call, result, "uncertain" if uncertain else ("error" if is_error else "ok"), started)
    if uncertain:
        rec.uncertain = True
        raise tasks.TaskUncertain(f"{call.tool} 执行结果需要核对：{result['error']}")
    return content
