"""工具调用策略：按应用的规则表决定 allow / deny / ask，再叠加只拒不放的 guard。

规则表（apps.policy 列，JSON）：
  {"preset": "compat"}                       用预设
  {"preset": "custom", "rules": [...]}       自定义规则，自上而下第一条命中的生效
单条规则：{"tool": "kubectl" | ["a","b"] | "*", "risk": "read" | ["write","exec"], "decision": "allow|deny|ask"}
  tool / risk 省略即匹配全部；一条都没命中按 deny（失败即拒绝）。

guard：只能拒绝或弃权，没有 allow，任何策略都推翻不了它的拒绝。
上层（ops 等）在导入时用 register_guard() 把自己的检查挂进来，harness 不 import 上层。
"""
import json

DECISIONS = ("allow", "deny", "ask")
RISKS = ("read", "write", "exec")
DEFAULT_PRESET = "compat"
CUSTOM = "custom"

PRESETS = {
    "compat": {
        "name": "兼容",
        "description": "和改造前一致：勾选的工具全部放行，只叠加 guard（黑名单、只读集群、旁听拦截）。",
        "rules": [{"decision": "allow"}],
    },
    "readonly": {
        "name": "只读",
        "description": "只放行只读工具；写入（改文档、记笔记、建定时任务）和执行（ssh、变更类 kubectl）一律拒绝。",
        "rules": [{"risk": "read", "decision": "allow"}, {"decision": "deny"}],
    },
    "approval": {
        "name": "审批",
        "description": "只读工具直接放行；写入和执行先在群里发审批卡片，审批人点「批准一次」才执行。"
                       "定时任务、旁听等没人在线的场景一律拒绝。需要在下方配置审批人。",
        "rules": [{"risk": "read", "decision": "allow"}, {"decision": "ask"}],
    },
}


# ---------- 规则表 ----------
def _as_list(v):
    return v if isinstance(v, list) else [v]


def validate_rules(rules):
    """校验自定义规则，不合法抛 ValueError（后台保存时用）。"""
    if not isinstance(rules, list) or not rules:
        raise ValueError("规则必须是非空数组")
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise ValueError(f"第 {i} 条规则必须是对象")
        unknown = set(r) - {"tool", "risk", "decision"}
        if unknown:
            raise ValueError(f"第 {i} 条规则有未知字段：{', '.join(sorted(unknown))}")
        if r.get("decision") not in DECISIONS:
            raise ValueError(f"第 {i} 条规则的 decision 必须是 {'/'.join(DECISIONS)}")
        if "risk" in r and any(x not in RISKS for x in _as_list(r["risk"])):
            raise ValueError(f"第 {i} 条规则的 risk 只能是 {'/'.join(RISKS)}")
        if "tool" in r and not all(isinstance(x, str) and x for x in _as_list(r["tool"])):
            raise ValueError(f"第 {i} 条规则的 tool 必须是工具名字符串或数组")
    return rules


def normalize(policy):
    """把库里的 policy（dict / JSON 字符串 / 空）规范成 {"preset", "rules"?}。坏数据退回默认预设。"""
    if isinstance(policy, str):
        try:
            policy = json.loads(policy or "{}")
        except Exception:
            policy = {}
    if not isinstance(policy, dict):
        policy = {}
    preset = policy.get("preset") or DEFAULT_PRESET
    if preset == CUSTOM:
        try:
            return {"preset": CUSTOM, "rules": validate_rules(policy.get("rules"))}
        except ValueError:
            return {"preset": DEFAULT_PRESET}
    return {"preset": preset if preset in PRESETS else DEFAULT_PRESET}


def rules_of(policy):
    p = normalize(policy)
    return p["rules"] if p["preset"] == CUSTOM else PRESETS[p["preset"]]["rules"]


def _matches(rule, tool, risk):
    if "tool" in rule and "*" not in _as_list(rule["tool"]) and tool not in _as_list(rule["tool"]):
        return False
    if "risk" in rule and risk not in _as_list(rule["risk"]):
        return False
    return True


def decide(policy, tool, risk):
    """返回 (decision, 规则序号(1 起，没命中为 None), 预设名)。"""
    p = normalize(policy)
    for i, rule in enumerate(rules_of(p), 1):
        if _matches(rule, tool, risk):
            return rule["decision"], i, p["preset"]
    return "deny", None, p["preset"]


def presets_public():
    """给后台下拉框用。"""
    return [{"key": k, "name": v["name"], "description": v["description"], "rules": v["rules"]}
            for k, v in PRESETS.items()]


# ---------- guard 注册表 ----------
_guards = {}          # name -> fn(call) -> 拒绝理由 | None


def register_guard(name, fn):
    """注册（或按名替换）一个 guard。fn 收到 pipeline.ToolCall，返回拒绝理由字符串或 None。"""
    _guards[name] = fn


def guard_names():
    return list(_guards)


def check_guards(call):
    """依次跑所有 guard，第一个给出理由的即拒绝。guard 自身抛异常也按拒绝处理（失败即拒绝）。
    返回 (guard 名, 理由) 或 None。"""
    for name, fn in list(_guards.items()):
        try:
            reason = fn(call)
        except Exception as e:
            return name, f"安全检查「{name}」执行出错，按拒绝处理：{e}"
        if reason:
            return name, reason
    return None


# ---------- harness 自带的 guard ----------
def _guard_not_exposed(call):
    if not call.exposed:
        return (f"本应用没有启用工具「{call.tool}」（后台没勾选），不能调用。"
                f"请只使用提供给你的工具；确实需要就请用户去后台给应用勾上。")


def _guard_proactive(call):
    if call.trigger == "proactive" and call.risk in ("write", "exec"):
        return ("当前是旁听模式（没人点名你），旁听时只允许只读操作，不能写入或执行。"
                "如果确实需要处理，请在回复里说明，等有人点名你再做。")


register_guard("not_exposed", _guard_not_exposed)
register_guard("proactive", _guard_proactive)
