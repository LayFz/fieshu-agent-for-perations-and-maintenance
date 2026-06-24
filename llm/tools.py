"""工具池：全集 TOOLS（给大模型 function-calling 的 schema）+ 执行器。

每个应用在后台勾选自己要用的工具子集（store.apps.tools）。运行时：
  tools_for(names) -> 只把该应用勾选的工具暴露给模型（能力隔离，防越权）
  execute(name, args, ctx) -> 真正干活；ctx 携带 app_id（记忆/长期笔记按应用隔离）
"""
from datetime import datetime

from core import store
from feishu import docs as feishu_docs
from obs import openobserve, beszel, uptimekuma


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
    except Exception:
        return ""

# OpenAI 兼容的 tools schema —— 全集（工具池）
TOOLS = [
    {"type": "function", "function": {
        "name": "list_knowledge_bases",
        "description": "列出应用能访问的全部知识库（拿到 space_id）。不确定写到哪个库时先调它。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_nodes",
        "description": "列出某知识库下的文档节点（标题 + obj_token）。要改/读某篇前先用它定位。",
        "parameters": {"type": "object", "properties": {
            "space_id": {"type": "string"},
            "parent_node_token": {"type": "string", "description": "可选，钻取子节点"},
        }, "required": ["space_id"]},
    }},
    {"type": "function", "function": {
        "name": "read_doc",
        "description": "读取一篇文档的正文（doc_id = 节点的 obj_token）。",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"},
        }, "required": ["doc_id"]},
    }},
    {"type": "function", "function": {
        "name": "rewrite_doc",
        "description": "用 Markdown 覆盖重写一篇已有文档（doc_id = obj_token）。会清空原内容，请先 read_doc 确认。",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"},
            "markdown": {"type": "string", "description": "新正文，Markdown(支持 # ## ### / ``` / - / **粗** / `码` / > / | 表格 |)"},
        }, "required": ["doc_id", "markdown"]},
    }},
    {"type": "function", "function": {
        "name": "create_node",
        "description": "在某知识库下新建一篇文档并写入 Markdown 内容。",
        "parameters": {"type": "object", "properties": {
            "space_id": {"type": "string"},
            "title": {"type": "string"},
            "markdown": {"type": "string"},
            "parent_node_token": {"type": "string", "description": "可选，建在某节点下"},
        }, "required": ["space_id", "title", "markdown"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "记住一条长期信息（偏好/约定/结论），以后对话会自动带上。",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"},
        }, "required": ["note"]},
    }},
    {"type": "function", "function": {
        "name": "list_log_streams",
        "description": "列出 OpenObserve 里可查的日志流(stream)名称。不确定查哪个流时先调它。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "query_logs",
        "description": "查 OpenObserve 日志。给出日志流名、可选过滤条件、时间窗，返回最近的日志记录。"
                       "用于排查应用运行情况，例如某次发布是否失败、有没有报错。",
        "parameters": {"type": "object", "properties": {
            "stream": {"type": "string", "description": "日志流名，例如 app_mp_publisher"},
            "where": {"type": "string", "description": "可选 SQL 过滤条件（不含 WHERE），例如 level='error'"},
            "period": {"type": "string", "description": "时间窗，如 15m/1h/24h/7d，默认 15m"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 50，最多 500"},
        }, "required": ["stream"]},
    }},
    {"type": "function", "function": {
        "name": "server_stats",
        "description": "查服务器实时运行状况(来自 Beszel):列出服务器及其 CPU/内存/磁盘占用、负载、运行时长。"
                       "用于运维巡检/出服务器报告。不传 name 列全部,传 name 模糊筛某台。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "可选,服务器名关键字,如 agent1"},
        }},
    }},
    {"type": "function", "function": {
        "name": "docker_containers",
        "description": "查各服务器上正在运行的 Docker 容器(来自 Beszel):每个容器的 CPU/内存/网络占用,以及它在哪台机器上。"
                       "用于「某项目/某服务部署在哪台机」(传 name 容器名关键字)、「哪个容器占用最多」(sort=cpu/mem + top)、"
                       "「某台机上跑了哪些容器」(传 server)。",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string", "description": "可选,服务器名关键字,只看某台机,如 agent1"},
            "name": {"type": "string", "description": "可选,容器名关键字,找某项目部署在哪台机,如 gitlab"},
            "sort": {"type": "string", "enum": ["cpu", "mem", "name"], "description": "排序,默认 cpu(占用从高到低)"},
            "top": {"type": "integer", "description": "可选,只返回前 N 个,找占用最多时用"},
        }},
    }},
    {"type": "function", "function": {
        "name": "service_status",
        "description": "查各服务/站点的在线状态(来自 Uptime Kuma):是否在线、响应时间、HTTPS 证书剩余天数。"
                       "用于巡检哪些服务挂了、证书快到期没。无参数。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "schedule_task",
        "description": "创建一个定时任务：到点自动执行一段指令，并把结果发回当前群。"
                       "适合「每天下班前出运维报告」「每周一发周报」这类需求。"
                       "instruction 要写成将来可独立执行的完整指令（含要做什么、查什么、报告格式）。"
                       "时间含糊时（如“下班前”）先和用户确认具体钟点再调用。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "任务名，如：每日服务器运行报告"},
            "instruction": {"type": "string", "description": "到点要执行的完整指令"},
            "kind": {"type": "string", "enum": ["daily", "weekly", "interval"],
                     "description": "daily=每天；weekly=每周某几天；interval=每隔N分钟"},
            "spec": {"type": "string", "description": "daily: 'HH:MM'(24小时)；weekly: '1,2,3,4,5@HH:MM'(1=周一..7=周日)；interval: 分钟数"},
        }, "required": ["title", "instruction", "kind", "spec"]},
    }},
    {"type": "function", "function": {
        "name": "list_schedules",
        "description": "列出本应用已创建的定时任务。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "cancel_schedule",
        "description": "取消（删除）一个定时任务，按 list_schedules 给出的 id。",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "任务 id"},
        }, "required": ["id"]},
    }},
]

_BY_NAME = {t["function"]["name"]: t for t in TOOLS}

# 技能工具：仅当应用勾了技能时由 engine 自动挂上（不在工具池里给用户单独勾）
SKILL_TOOL = {"type": "function", "function": {
    "name": "read_skill",
    "description": "读取一个技能(skill)的完整内容（知识/工作方法/背景资料）。系统提示词里列了你可用的技能名与简介，"
                   "需要某个技能的详细内容时调用它。",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "技能名称（见系统提示词中的技能清单）"},
    }, "required": ["name"]},
}}


def all_tools():
    """工具池全集（名称 + 说明），给后台勾选用。"""
    return [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in TOOLS]


def tools_for(names):
    """按应用勾选的名单返回 OpenAI tools 子集；名单为空则该应用没有任何工具。"""
    names = names or []
    return [_BY_NAME[n] for n in names if n in _BY_NAME]


def tool_list_for(names):
    """某应用已启用工具的清单（名称 + 说明），给后台展示。"""
    return [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in tools_for(names)]


def execute(name, args, ctx=None):
    ctx = ctx or {}
    app_id = ctx.get("app_id")
    try:
        if name == "read_skill":
            skills = ctx.get("skills") or {}          # 只含本应用勾选的技能 -> 隔离
            sk = skills.get(args.get("name"))
            if sk is None:
                return {"error": "没有这个技能或本应用未启用它",
                        "available": list(skills.keys())}
            return {"name": args.get("name"), "content": sk}
        if name == "list_knowledge_bases":
            return feishu_docs.list_kbs()
        if name == "list_nodes":
            return feishu_docs.list_nodes(args["space_id"], args.get("parent_node_token"))
        if name == "read_doc":
            return {"content": feishu_docs.read_doc(args["doc_id"])[:8000]}
        if name == "rewrite_doc":
            return feishu_docs.rewrite_doc(args["doc_id"], args["markdown"])
        if name == "create_node":
            return feishu_docs.create_node(args["space_id"], args["title"], args["markdown"],
                                           args.get("parent_node_token"))
        if name == "remember":
            store.add_note(app_id, args["note"])
            return {"ok": True}
        if name == "list_log_streams":
            return openobserve.list_streams()
        if name == "query_logs":
            return openobserve.search(args["stream"], args.get("where"),
                                      args.get("period", "15m"), args.get("limit", 50))
        if name == "server_stats":
            return beszel.servers(args.get("name"))
        if name == "docker_containers":
            return beszel.containers(args.get("server"), args.get("name"),
                                     args.get("sort", "cpu"), args.get("top"))
        if name == "service_status":
            return uptimekuma.status()
        if name == "schedule_task":
            if not ctx.get("chat_id"):
                return {"error": "无法确定要发送的群（缺少会话上下文）"}
            sid, nxt = store.create_schedule(app_id, ctx["chat_id"], args.get("title"),
                                             args.get("instruction"), args.get("kind"), args.get("spec"))
            return {"ok": True, "id": sid, "next_run": _fmt_ts(nxt),
                    "note": "已创建定时任务，到点会自动在本群发结果"}
        if name == "list_schedules":
            return {"schedules": [{
                "id": sc["id"], "title": sc["title"], "kind": sc["kind"], "spec": sc["spec"],
                "enabled": bool(sc["enabled"]), "next_run": _fmt_ts(sc["next_run_ts"]),
            } for sc in store.list_schedules(app_id)]}
        if name == "cancel_schedule":
            sc = store.get_schedule(args.get("id"))
            if not sc or sc["app_id"] != app_id:          # 只能取消本应用的任务 -> 隔离
                return {"error": "没有这个任务或不属于本应用"}
            store.delete_schedule(sc["id"])
            return {"ok": True}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}
