"""工具执行管道：所有工具调用（原生 / MCP）都走 run_tool()，固定五段：

  1. 记录   写 tool/call；参数从此冻结，后面任何一段都改不了（执行器拿到的是副本）
  2. 策略   按应用规则表决定 allow / deny / ask（harness.policy.decide）
  3. guard  只拒不放；任何一个给出理由就拒绝，策略的 allow 也推翻不了
  4. 执行   allow 才执行；统一超时；抛异常归一为 {"error": ...}，不让整轮对话崩
  5. 后处理 写 tool/result，返回给模型的字符串

被拒绝不会让对话失败：给模型一个错误结果，写明被谁、为什么拦下，让它换路或如实告诉用户。
ask 在审批上线（P2）前按拒绝处理（失败即拒绝）。
"""
import contextvars
import copy
import json
import threading
import time
from dataclasses import dataclass, field

from . import policy

RESULT_PREVIEW = 4000        # 事件里保留的工具结果预览长度（完整结果外存留待 P3）
DEFAULT_TIMEOUT = 120


@dataclass
class ToolCall:
    """一次工具调用。guard 读它做判断；args 已冻结（深拷贝），guard 不应修改。"""
    id: str
    tool: str
    args: dict
    risk: str = "exec"             # read / write / exec（未知按最高风险）
    external: bool = False         # 结果是否含外部不可信文本（污染标记 P4 用）
    timeout: int = DEFAULT_TIMEOUT
    exposed: bool = True           # 是否在本应用暴露给模型的工具清单里
    raw_args: str = None           # 参数 JSON 解析失败时的原文
    trigger: str = ""
    app_id: int = None
    extra: dict = field(default_factory=dict)


def _run_with_timeout(fn, args, timeout):
    """在独立线程里执行，超时就放弃等待（同进程代码杀不掉，线程会自己跑完后丢弃结果）。
    用 copy_context 让执行线程继承 contextvar（飞书文档工具靠它拿本应用凭证）。"""
    box = {}
    ctx = contextvars.copy_context()

    def target():
        try:
            box["result"] = ctx.run(fn, args)
        except Exception as e:
            box["result"] = {"error": f"工具执行出错：{type(e).__name__}: {e}"}

    th = threading.Thread(target=target, name="tool-exec", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return {"error": f"工具执行超时（超过 {timeout} 秒），已放弃等待它的结果。"}
    return box.get("result")


def _denied_result(by, name, reason):
    who = "安全检查" if by == "guard" else "应用的工具策略"
    return {"error": f"调用被{who}拒绝，没有执行。原因：{reason}", "denied_by": f"{by}:{name}"}


def run_tool(rec, call, app_policy, executor):
    """执行一次工具调用，返回给模型的结果字符串。executor(args) -> 任意可 JSON 化的结果。"""
    # 1. 记录（先于执行）
    payload = {"args": call.args, "risk": call.risk}
    if call.raw_args is not None:
        payload.update(raw_args=call.raw_args, parse_error=True)
    if call.external:
        payload["external"] = True
    rec.emit("tool/call", payload, call_id=call.id, tool=call.tool)
    call.args = copy.deepcopy(call.args)        # 冻结：执行器和 guard 拿到的是副本

    # 2. 策略
    decision, rule_no, preset = policy.decide(app_policy, call.tool, call.risk)
    verdict = {"decision": decision, "by": "policy", "preset": preset, "rule": rule_no}
    if decision == "deny":
        verdict["reason"] = (f"预设「{policy.PRESETS.get(preset, {}).get('name', preset)}」"
                             + (f"第 {rule_no} 条规则" if rule_no else "没有规则放行") + f"拒绝 {call.risk} 类工具")
    elif decision == "ask":
        verdict.update(decision="deny", asked=True,
                       reason="这个调用按策略需要人工审批，但审批功能还没上线，已按拒绝处理。")

    # 3. guard（策略已拒绝的也跑一遍，审计里能看到所有命中的拦截原因；只取第一个）
    hit = policy.check_guards(call)
    if hit:
        verdict = {"decision": "deny", "by": "guard", "guard": hit[0], "reason": hit[1],
                   "policy_decision": decision, "preset": preset, "rule": rule_no}
    rec.emit("tool/decision", verdict, call_id=call.id, tool=call.tool)

    # 4. 执行
    t0 = time.time()
    if verdict["decision"] == "allow":
        result = _run_with_timeout(executor, call.args, call.timeout)
        status = "error" if isinstance(result, dict) and "error" in result else "ok"
    else:
        name = verdict.get("guard") or (f"rule{rule_no}" if rule_no else preset)
        result = _denied_result(verdict["by"], name, verdict["reason"])
        status = "denied"

    # 5. 后处理
    content = json.dumps(result, ensure_ascii=False, default=str)
    rec.emit("tool/result", {
        "status": status,
        "duration_ms": int((time.time() - t0) * 1000),
        "chars": len(content),
        "result": content[:RESULT_PREVIEW],
        "truncated": len(content) > RESULT_PREVIEW,
    }, call_id=call.id, tool=call.tool)
    return content
