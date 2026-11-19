"""飞书长连接（多应用）：每个启用的应用各起一条长连接，收群消息 → 该应用的 AI+工具 → 回群。

每条连接绑定一个 app_id；收到消息时按 app_id 实时从 DB 取最新配置后再跑（后台改人设/工具/AI 即时生效）。
飞书连接只出站。守护线程刷新应用状态，连接异常退避重试，凭据变化后停旧连接再建立新连接。
"""
import asyncio
import hashlib
import json
import re
import threading
import time

import requests

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 PatchMessageRequest, PatchMessageRequestBody)

from core import store, taskstore
from feishu import members
from harness import approval, tasks, access
from llm.engine import run as llm_run

# lark 的 ws 客户端用了一个【模块级全局事件循环】，所有 Client.start() 共用它，
# 同进程跑多个长连接时第二个会撞 "This event loop is already running"。
# 用线程本地代理替换该全局 loop：每个应用线程各拿一个独立事件循环，互不干扰。
import lark_oapi.ws.client as _wsc

_loop_local = threading.local()

class _ThreadLoopProxy:
    def __getattr__(self, name):
        loop = getattr(_loop_local, "loop", None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _loop_local.loop = loop
        return getattr(loop, name)

_wsc.loop = _ThreadLoopProxy()

_send_clients = {}          # app_id -> (凭据指纹, lark.Client)
_running = {}               # app_id -> 已观察到连接建立
_connections = {}           # 内部控制记录，health 只返回允许字段
_connection_lock = threading.RLock()
_lifecycle_lock = threading.RLock()
_supervisor_stop = threading.Event()
_supervisor_thread = None
_SUPERVISOR_INTERVAL = 1.0
_RETRY_BASE = 2.0
_RETRY_MAX = 60.0
_STABLE_CONNECTION = 60.0
_task_executor_hook = None
_command_handler_hook = None


def register_task_executor(handler):
    """注册确定性任务入口：返回 None 交给模型，返回 str 表示已完成处理。"""
    global _task_executor_hook
    if handler is not None and not callable(handler):
        raise TypeError("任务执行钩子必须可调用")
    _task_executor_hook = handler


def register_command_handler(handler):
    """注册已点名消息的扩展命令；None 表示未匹配。"""
    global _command_handler_hook
    if handler is not None and not callable(handler):
        raise TypeError("命令钩子必须可调用")
    _command_handler_hook = handler


def _credential_key(app):
    return hashlib.sha256(json.dumps([app.get("feishu_app_id"), app.get("feishu_app_secret")]).encode()).hexdigest()


def _send_client(app):
    key = _credential_key(app)
    with _connection_lock:
        cached = _send_clients.get(app["id"])
        if cached is None or cached[0] != key:
            c = lark.Client.builder().app_id(app["feishu_app_id"]).app_secret(app["feishu_app_secret"]).build()
            _send_clients[app["id"]] = (key, c)
        return _send_clients[app["id"]][1]


def _tables_to_text(text):
    """飞书卡片 markdown 不支持表格：把 | a | b | 表格转成「- **首列**：列名 值 · 列名 值」的分行列表。
    未识别为表格的内容原样返回；流式中途的半张表也能容错处理。"""
    lines = text.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        # 表格头：本行是 |...| 且下一行是分隔行 |---|---|
        if ln.strip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:|\-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                i += 1
                if not any(cells):
                    continue
                first = cells[0]
                rest = [f"{h} {v}".strip() for h, v in zip(header[1:], cells[1:]) if v]
                out.append(f"- **{first}**" + ("：" + " · ".join(rest) if rest else ""))
            continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def _strip_headings(md):
    """`# 标题` → 粗体行；标题里已有 **加粗** 时只去掉 # 前缀，不二次包裹（会配对错乱漏出星号）。"""
    def heading(m):
        t = m.group(1).strip()
        return t if "**" in t else f"**{t}**"
    return re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", heading, md, flags=re.M)


def _md_card(text):
    """流式/中途用：单个 markdown 元素，md 表格降级为分行列表（飞书 markdown 不渲染表格）。"""
    md = _strip_headings(_tables_to_text(text))
    return {"config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": md}]}


def _table_element(header, rows):
    """一张 markdown 表格 → 飞书原生 table 组件（真表格）。"""
    if not header:
        return None
    cols = [{"name": f"c{j}", "display_name": (h or f"列{j+1}"), "data_type": "text",
             "horizontal_align": "left"} for j, h in enumerate(header)]
    out_rows = [{f"c{j}": (cells[j] if j < len(cells) else "") for j in range(len(header))}
                for cells in rows]
    return {"tag": "table", "columns": cols, "rows": out_rows}


_MD_ELEMENT_LIMIT = 4000    # 飞书单 markdown 元素过大不渲染，超限按空行切段


def _rich_card(text):
    """最终定版用：普通文字走 markdown，markdown 表格渲染成飞书原生 table 组件（真表格）。"""
    elements, buf = [], []

    def flush():
        if not buf:
            return
        md = _strip_headings("\n".join(buf)).strip("\n")
        buf.clear()
        if not md.strip():
            return
        for chunk in _split_md(md):
            elements.append({"tag": "markdown", "content": chunk})

    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.strip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:|\-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                i += 1
                if any(cells):
                    rows.append(cells)
            tbl = _table_element(header, rows)
            if tbl:
                flush()
                elements.append(tbl)
            continue
        buf.append(ln)
        i += 1
    flush()
    if not elements:
        elements = [{"tag": "markdown", "content": text}]
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def _split_md(md, limit=_MD_ELEMENT_LIMIT):
    """超长 markdown 按空行切段，段仍超限时按行硬切；代码块/表格行不作为切分点优先级最高。"""
    if len(md) <= limit:
        return [md]
    paras = md.split("\n\n")
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + 2 + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    # 单段仍超限（如无空行的长列表）：按行兜底硬切
    final = []
    for chunk in out:
        if len(chunk) <= limit:
            final.append(chunk)
            continue
        line, rest = "", chunk
        while len(rest) > limit:
            cut = rest.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            final.append(rest[:cut])
            rest = rest[cut:].lstrip("\n")
        if rest:
            final.append(rest)
    return final


def _create_card(app, chat_id, card, message_uuid=None):
    body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(json.dumps(card))
    if message_uuid:
        body = body.uuid(message_uuid)
    req = (CreateMessageRequest.builder().receive_id_type("chat_id")
           .request_body(body.build()).build())
    return _send_client(app).im.v1.message.create(req)


def _reply(app, chat_id, text, rich=False):
    """发一条卡片消息。rich=True 用原生表格；被飞书拒了自动退回分行 markdown。"""
    resp = _create_card(app, chat_id, _rich_card(text) if rich else _md_card(text))
    if not resp.success() and rich:
        resp = _create_card(app, chat_id, _md_card(text))     # 真表格被拒 → 退回分行
    if not resp.success():
        print(f"[app {app['id']}] 回消息失败 code={resp.code}", flush=True)


def post(app, chat_id, text):
    """对外发消息（定时任务等主动推送用）。app 为 store.get_app() 字典。"""
    _reply(app, chat_id, text, rich=True)


def _send_card(app, chat_id, text):
    """发一张交互卡片，返回 message_id（供后续原地更新）；失败返回 None。"""
    resp = _create_card(app, chat_id, _md_card(text))
    if not resp.success():
        print(f"[app {app['id']}] 发卡片失败 code={resp.code}", flush=True)
        return None
    return getattr(resp.data, "message_id", None)


def _patch_card(app, message_id, card):
    req = (PatchMessageRequest.builder().message_id(message_id)
           .request_body(PatchMessageRequestBody.builder().content(json.dumps(card)).build()).build())
    return _send_client(app).im.v1.message.patch(req)


def _update_card(app, message_id, text, rich=False):
    """原地更新卡片（PATCH）。rich=True 用原生表格；被拒则退回分行 markdown。返回是否成功。"""
    if not message_id:
        return False
    resp = _patch_card(app, message_id, _rich_card(text) if rich else _md_card(text))
    if not resp.success() and rich:
        resp = _patch_card(app, message_id, _md_card(text))    # 真表格被拒 → 退回分行
    if not resp.success():
        print(f"[app {app['id']}] 更新卡片失败 code={resp.code}", flush=True)
        return False
    return True


# ---------- 审批卡片（harness.approval 的应答者） ----------
_APPROVAL_STATUS = {
    "pending": ("⏳ 等待审批", "orange"), "allowed-once": ("✅ 已批准", "green"),
    "rejected": ("⛔ 已拒绝", "red"), "expired": ("⌛ 已超时（未执行）", "grey"),
    "cancelled": ("🚫 已失效（服务重启，未执行）", "grey"), "unavailable": ("🚫 审批不可用（未执行）", "grey"),
}


def _approval_card(app, row, status="pending", decider_name=""):
    """审批卡片。row 至少含 id/chat_id/tool/args/risk/reason/requester_open_id。"""
    title, color = _APPROVAL_STATUS.get(status, (status, "grey"))
    args = row.get("args") or {}
    args_txt = json.dumps(args, ensure_ascii=False, indent=2) if isinstance(args, dict) else str(args)
    if len(args_txt) > 1500:
        args_txt = args_txt[:1500] + "\n…（已截断，完整参数见后台运行记录）"
    who = members.name_of(app, row.get("chat_id"), row.get("requester_open_id")) if row.get("requester_open_id") else "（未知）"
    lines = [f"**{app.get('name')}** 想执行一个需要审批的操作",
             f"**工具**：`{row.get('tool')}`（{ {'write': '写入', 'exec': '执行'}.get(row.get('risk'), row.get('risk')) }类）",
             f"**发起人**：{who}",
             f"**原因**：{row.get('reason') or ''}"]
    elements = [{"tag": "markdown", "content": "\n".join(lines)}]
    if row.get("preview"):                      # 工具给的审批预览（如提 MR 的 diff）——审批人真正要看的内容
        pv = str(row["preview"])
        if row.get("tool") == "code_propose_mr":
            # MR 审批必须展示全部 diff；预览过大已在生成阶段拒绝，不可在卡片里悄悄截断。
            heading, marker, diff = pv.partition("\n```diff\n")
            if not marker or not diff.endswith("\n```"):
                raise ValueError("MR 审批预览缺少完整 diff")
            elements.append({"tag": "markdown", "content": heading})
            diff = diff[:-4]
            for offset in range(0, len(diff), 2800):
                elements.append({"tag": "markdown", "content": "```diff\n" + diff[offset:offset + 2800] + "\n```"})
        else:
            elements.append({"tag": "markdown", "content": pv[:3500] + ("\n…（已截断）" if len(pv) > 3500 else "")})
    elements.append({"tag": "markdown", "content": f"**参数**\n```json\n{args_txt}\n```"})
    if status == "pending":
        value = {"kind": "approval", "approval_id": row["id"]}
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "批准一次"}, "type": "primary",
             "value": dict(value, choice="approve")},
            {"tag": "button", "text": {"tag": "plain_text", "content": "拒绝"}, "type": "danger",
             "value": dict(value, choice="reject")},
        ]})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                         "content": "只有审批人名单里的人能点；只对这一次调用生效。"}]})
    else:
        tail = f"{title}" + (f" · 审批人 {decider_name}" if decider_name else "")
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": tail}]})
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"template": color, "title": {"tag": "plain_text", "content": f"{title} · {row.get('tool')}"}},
            "elements": elements}


def _ask_approval(req):
    """harness.approval 调：在群里发审批卡片，返回 message_id。"""
    app = req["app"]
    row = {"id": req["approval_id"], "chat_id": req["chat_id"], "tool": req["tool"], "args": req["args"],
           "risk": req["risk"], "reason": req["reason"], "requester_open_id": req["requester_open_id"],
           "preview": req.get("preview")}
    resp = _create_card(app, req["chat_id"], _approval_card(app, row))
    if not resp.success():
        print(f"[app {app['id']}] 发审批卡片失败 code={resp.code}", flush=True)
        return None
    return getattr(resp.data, "message_id", None)


def _finalize_approval(row):
    """harness.approval 调：超时 / 重启失效时把卡片改成终态。"""
    app = store.get_app(row["app_id"])
    if app and row.get("message_id"):
        _patch_card(app, row["message_id"], _approval_card(app, row, row["status"]))


def _on_card_action(app_id, data):
    """卡片按钮回调（长连接，飞书要求 3 秒内回应）。返回 toast + 更新后的卡片。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    try:
        ev = data.event
        value = (ev.action.value or {}) if ev and ev.action else {}
        operator = getattr(ev.operator, "open_id", "") if ev and ev.operator else ""
        if value.get("kind") != "approval":
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "这个按钮已失效"}})
        ok, msg, row = approval.decide(value.get("approval_id"), value.get("choice") == "approve", operator,
                                       app_id=app_id)
        print(f"[app {app_id}] 审批 {str(value.get('approval_id'))[:8]} by {operator[-6:]} "
              f"{value.get('choice')} -> {msg}", flush=True)
        body = {"toast": {"type": "success" if ok else "error", "content": msg}}
        if ok and row:
            app = store.get_app(row["app_id"]) or {"name": "", "id": app_id}
            decider = members.name_of(app, row.get("chat_id"), operator)
            body["card"] = {"type": "raw", "data": _approval_card(app, row, row["status"], decider)}
        return P2CardActionTriggerResponse(body)
    except Exception as e:
        print(f"[app {app_id}] 处理卡片回调出错: {type(e).__name__}", flush=True)
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "处理失败，请重试"}})


approval.register_answerer(_ask_approval, _finalize_approval)


def _extract_text(message):
    try:
        return json.loads(message.content).get("text", "")
    except Exception:
        return ""


def _strip_mentions(text, mentions):
    for mt in (mentions or []):
        key = getattr(mt, "key", None)
        if key:
            text = text.replace(key, "")
    return text.strip()


def _is_bot_mentioned(app, mentions):
    """这条群消息是否 @ 了本机器人。"""
    oid = members.bot_open_id(app)
    if not oid:
        return False
    for mt in (mentions or []):
        mid = getattr(getattr(mt, "id", None), "open_id", None)
        if mid and mid == oid:
            return True
    return False


# ── 主动模式（Jarvis 旁听 + 该出手时主动说话）─────────────────────────────
# 直接喊它名字 = 等同点名，必回；预筛命中 = 让它自己判断要不要插话。
_NAME_HINTS = ("贾维斯", "賈維斯", "jarvis")
# 廉价预筛：只有像"有事"的消息才值得花一次大模型判断，闲聊直接跳过（控成本）
_TRIGGER_KW = ("挂了", "宕机", "崩了", "报错", "异常", "失败", "故障", "重启了", "起不来",
               "连不上", "超时", "卡住", "卡死", "503", "502", "504", "oom", "爆了",
               "满了", "告警", "alert", "error", "down", "部署", "上线", "发布",
               "回滚", "求助", "帮忙看", "谁知道", "怎么回事", "什么原因")
# 「刚交互过」窗口：某人点名（@/喊名/单聊）之后，这段时间内 ta 的追问也算「对机器人说话」，
# 不必每句都 @——连续对话更自然。按 (chat_id, 发言人) 隔离，只惠及正在跟它聊的那个人。
_FOLLOWUP_WINDOW = 180     # 秒
_last_engaged = {}         # (app_id, chat_id, sender_open_id) -> ts


def _name_called(text):
    t = (text or "").lower()
    return any(h in text or h in t for h in _NAME_HINTS)


def _worth_considering(text):
    t = (text or "").lower()
    return any(k in t for k in _TRIGGER_KW)


def execute_task(task):
    """worker 注册入口；运行结果统一由 outbox 送达。"""
    app = store.get_app(task["app_id"])
    access.actor(app, task["chat_id"], task.get("speaker_open_id", ""),
                 system=task["trigger"] in ("schedule", "job", "webhook"))
    if _task_executor_hook is not None and task["trigger"] != "proactive":
        result = _task_executor_hook(task)
        if result is not None:
            if not isinstance(result, str):
                raise TypeError("任务执行钩子必须返回文本或 None")
            return result
    return llm_run(app, task["chat_id"], task["instruction"],
                   speaker=task.get("speaker") or task.get("speaker_open_id") or "",
                   speaker_open_id=task.get("speaker_open_id", ""),
                   proactive=task["trigger"] == "proactive", trigger=task["trigger"])


def deliver_task(task, text):
    app = store.get_app(task["app_id"])
    if not app:
        raise RuntimeError("任务所属应用不存在，无法投递")
    if text.startswith(taskstore.CARD_PREFIX):
        card = json.loads(text[len(taskstore.CARD_PREFIX):])
        resp = _create_card(app, task["chat_id"], card, task.get("notification_id"))
    else:
        # 任务结果与主动推送同一待遇：md 表格渲染成原生 table 组件；被飞书拒绝退回分行 markdown。
        resp = _create_card(app, task["chat_id"], _rich_card(text), task.get("notification_id"))
        if not resp.success():
            resp = _create_card(app, task["chat_id"], _md_card(text), task.get("notification_id"))
    if not resp.success():
        raise RuntimeError(f"飞书投递失败 code={resp.code}")


def _task_command(app_id, chat_id, text, sender_open_id, event_id=None):
    access.actor(store.get_app(app_id), chat_id, sender_open_id)
    if text.strip() in ("我的身份", "/whoami"):
        return f"当前飞书 open_id：{sender_open_id or '未提供'}\n当前 chat_id：{chat_id}"
    if text.strip() in ("任务", "任务列表", "/tasks"):
        rows = tasks.list_tasks(app_id=app_id, chat_id=chat_id, limit=10)
        return "最近任务：\n" + "\n".join(f"`{t['id']}` · {t['status']} · {t['instruction'][:60]}" for t in rows)
    match = re.fullmatch(r"(任务|查看任务|取消任务|继续任务|/task|/cancel|/resume)\s+([0-9a-f]{12,32})(?:\s+([\s\S]+))?", text.strip())
    if not match:
        result = (_command_handler_hook(app_id, chat_id, text, sender_open_id, event_id)
                  if _command_handler_hook is not None else None)
        if result is not None and not isinstance(result, str):
            raise TypeError("命令钩子必须返回文本或 None")
        return result
    command, tid, extra = match.groups()
    if len(tid) < 32:
        matches = store._conn.execute("SELECT id FROM tasks WHERE app_id=? AND chat_id=? AND id LIKE ? LIMIT 2",
                                      (app_id, chat_id, tid + "%")).fetchall()
        if len(matches) != 1:
            return "任务编号不唯一或不存在，请使用完整编号。"
        tid = matches[0]["id"]
    task = tasks.get(tid)
    if not task or task["app_id"] != app_id or task["chat_id"] != chat_id:
        return "找不到本会话的这个任务。"
    if command in ("取消任务", "/cancel", "继续任务", "/resume"):
        app = store.get_app(app_id) or {}
        if sender_open_id != task.get("speaker_open_id") and sender_open_id not in (app.get("approvers") or []):
            return "只有任务发起人或应用审批人能取消/继续这个任务。"
        if command in ("取消任务", "/cancel"):
            ok = tasks.cancel(tid, app_id=app_id, chat_id=chat_id)
            return "已请求取消，执行器确认停止前可能仍在收尾。" if ok else "任务已结束或无法取消。"
        ok = tasks.resume(tid, app_id=app_id, chat_id=chat_id, instruction=extra)
        return "任务已重新排队。" if ok else "任务不能直接恢复；待审批请使用审批卡，结果不明的变更需先核对。"
    checkpoint = task.get("checkpoint") or {}
    detail = "\n\n".join(x for x in (task.get("result"), task.get("error")) if x)
    return (f"任务 `{tid}`\n状态：{task['status']}\n目标：{task['instruction']}\n"
            f"进展：{checkpoint.get('progress') or '尚无工具结果'}\n"
            f"{detail}")


def _on_message(app_id, data):
    try:
        msg = data.event.message
        chat_id = msg.chat_id
        msg_id = getattr(msg, "message_id", None)
        mentions = getattr(msg, "mentions", None)
        text = _strip_mentions(_extract_text(msg), mentions)
        if not text or not msg_id:
            return
        sender = getattr(getattr(data.event.sender, "sender_id", None), "open_id", "") or ""
        app = store.get_app(app_id)
        access.actor(app, chat_id, sender)
        store.observe_feishu_chat(app_id, chat_id, sender, getattr(msg, "chat_type", ""))
        now = time.time()
        ekey = (app_id, chat_id, sender)
        called = getattr(msg, "chat_type", "") == "p2p" or _is_bot_mentioned(app, mentions) or _name_called(text)
        addressed = called or (bool(sender) and now - _last_engaged.get(ekey, 0) < _FOLLOWUP_WINDOW)
        if addressed:
            if sender:
                _last_engaged[ekey] = now
            command = _task_command(app_id, chat_id, text, sender, msg_id)
            if command is not None:
                _reply(app, chat_id, command)
                return
        elif not _worth_considering(text):
            return
        # 跨重启去重、同会话串行、接收回执都由持久任务服务负责。
        tasks.submit(app_id, chat_id, text, source="feishu", event_id=msg_id,
                     speaker_open_id=sender, trigger="chat" if addressed else "proactive")
    except PermissionError as e:
        print(f"[app {app_id}] 拒绝消息: {e}", flush=True)
    except Exception:
        # 不吞掉入队失败；让飞书重投消息，避免已应答但任务未落库。
        raise


class _SupervisedClient(lark.ws.Client):
    """保留 SDK 协议处理，重连与生命周期由每应用守护器负责。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ended = asyncio.Event()
        self.failure_type = "ConnectionEnded"

    def _fmt_log(self, fmt, *args):
        # SDK 默认会把签名 websocket URL、错误正文甚至事件 payload 写入日志。
        # 格式串来自 SDK；所有动态参数都去掉，不记录 URL/token/消息正文。
        return re.sub(r"\{[^}]*\}", "[redacted]", fmt)

    def _get_conn_url(self):
        # SDK 的同步 bootstrap POST 没有 timeout；显式限时，避免停用后旧线程永远卡住。
        response = requests.post(self._domain + _wsc.GEN_ENDPOINT_URI,
            headers={"locale": "zh", _wsc.USER_AGENT: getattr(self, "_user_agent", "feishu-agent")},
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
            timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            raise ConnectionError("EndpointHTTPError")
        endpoint = _wsc.JSON.unmarshal(response.content.decode("utf-8"), _wsc.EndpointResp)
        if endpoint.code != _wsc.OK or endpoint.data is None:
            raise ConnectionError("EndpointRejected")
        if endpoint.data.ClientConfig is not None:
            self._configure(endpoint.data.ClientConfig)
        return endpoint.data.URL

    async def _receive_message_loop(self):
        try:
            await super()._receive_message_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure_type = type(exc).__name__
        finally:
            self.ended.set()


def _connection_current(entry):
    if entry["stop"].is_set() or _supervisor_stop.is_set():
        return False
    app = store.get_app(entry["app_id"])
    return bool(app and app.get("enabled") and _credential_key(app) == entry["credential_key"])


def _mark_listening(entry, connected):
    with _connection_lock:
        if _connections.get(entry["app_id"]) is not entry or entry["stop"].is_set():
            return
        entry["state"] = "listening"
        entry["connected"] = bool(connected)
        entry["connected_at"] = time.time() if connected else None
        entry["last_error"] = ""
        _running[entry["app_id"]] = bool(connected)


def _serve(app, entry):
    app_id = app["id"]
    # 旧连接即使尚在关闭，也不能以新配置的应用身份接收命令或审批。
    def on_message(data):
        if _connection_current(entry):
            return _on_message(app_id, data)

    def on_card(data):
        if _connection_current(entry):
            return _on_card_action(app_id, data)
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "连接配置已变更，请重新操作"}})

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card).build())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop_local.loop = loop
    # asyncio 未处理异常也不能输出带签名 URL 的异常原文。
    loop.set_exception_handler(lambda _loop, context: print(
        f"[app {app_id}] websocket 后台异常: {type(context.get('exception')).__name__}", flush=True))
    try:
        client = _SupervisedClient(app["feishu_app_id"], app["feishu_app_secret"],
                                  event_handler=handler, log_level=lark.LogLevel.ERROR, auto_reconnect=False)
    except BaseException:
        loop.close()
        _loop_local.loop = None
        raise
    with _connection_lock:
        entry["loop"], entry["client"] = loop, client

    async def session():
        try:
            if not _connection_current(entry):
                return
            await asyncio.wait_for(client._connect(), timeout=25)
            if not _connection_current(entry):
                return
            # 确认 SDK 已建立 conn 才标 connected；只有线程存活不算连通。
            _mark_listening(entry, getattr(client, "_conn", None) is not None)
            loop.create_task(client._ping_loop())
            while not entry["stop"].is_set() and not _supervisor_stop.is_set():
                if client.ended.is_set():
                    raise ConnectionError(client.failure_type)
                await asyncio.sleep(0.2)
        finally:
            conn = getattr(client, "_conn", None)
            try:
                await asyncio.wait_for(client._disconnect(), timeout=3)
            except (Exception, asyncio.CancelledError):
                transport = getattr(conn, "transport", None)
                if transport is not None:
                    transport.abort()

    runner = loop.create_task(session())
    with _connection_lock:
        entry["runner"] = runner
    try:
        loop.run_until_complete(runner)
    except asyncio.CancelledError:
        if not entry["stop"].is_set() and not _supervisor_stop.is_set():
            raise RuntimeError("UnexpectedCancellation") from None
    finally:
        # 每次重试创建新 loop；清掉 SDK receive/ping 等任务，避免旧凭据继续活动。
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=3))
        loop.close()
        _loop_local.loop = None
        with _connection_lock:
            entry["loop"], entry["client"], entry["runner"] = None, None, None


def _run_connection(app, entry):
    failure = "ConnectionEnded"
    try:
        _serve(app, entry)
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        now = time.time()
        with _connection_lock:
            entry["connected"] = False
            _running[app["id"]] = False
            entry["last_exit_at"] = now
            if entry["stop"].is_set() or _supervisor_stop.is_set():
                entry["state"] = "stopped"
            else:
                stable = entry.get("connected_at") and now - entry["connected_at"] >= _STABLE_CONNECTION
                entry["failures"] = 1 if stable else entry["failures"] + 1
                delay = min(_RETRY_MAX, _RETRY_BASE * 2 ** min(entry["failures"] - 1, 16))
                entry["next_retry_at"] = now + delay
                entry["state"], entry["last_error"] = "retrying", failure
                print(f"[app {app['id']}] 飞书连接退出: {failure}，{delay:g}s 后重试", flush=True)


def _request_stop(entry):
    entry["stop"].set()
    entry["connected"] = False
    _running[entry["app_id"]] = False
    thread = entry.get("thread")
    entry["state"] = "stopping" if thread and thread.is_alive() else "stopped"
    loop, runner = entry.get("loop"), entry.get("runner")
    if loop is not None and runner is not None and not loop.is_closed():
        try:
            loop.call_soon_threadsafe(runner.cancel)
        except RuntimeError:
            pass


def _ensure_connection(app, now):
    """调用者持锁；同一应用旧线程退出前绝不启动第二条连接。"""
    aid = app["id"]
    key = _credential_key(app)
    entry = _connections.get(aid)
    if entry and entry.get("thread") and entry["thread"].is_alive():
        if entry["credential_key"] != key:
            _request_stop(entry)
            _send_clients.pop(aid, None)
        return False
    if entry is None or entry["credential_key"] != key or entry["stop"].is_set():
        entry = {"app_id": aid, "credential_key": key, "stop": threading.Event(),
                 "thread": None, "loop": None, "runner": None, "client": None,
                 "state": "starting", "connected": False, "attempts": 0, "failures": 0,
                 "next_retry_at": None, "last_error": "", "started_at": None,
                 "connected_at": None, "last_exit_at": None}
        _connections[aid] = entry
        _send_clients.pop(aid, None)
    if entry["next_retry_at"] and now < entry["next_retry_at"]:
        return False
    entry.update(state="starting", connected=False, started_at=now, connected_at=None, next_retry_at=None)
    entry["attempts"] += 1
    thread = threading.Thread(target=_run_connection, args=(dict(app), entry), name=f"bot-{aid}", daemon=True)
    entry["thread"] = thread
    thread.start()
    return True


def _supervise_once(now=None):
    now = time.time() if now is None else now
    apps = {a["id"]: a for a in store.list_apps()}
    started = 0
    with _connection_lock:
        if _supervisor_stop.is_set():
            return 0
        for aid, entry in list(_connections.items()):
            app = apps.get(aid)
            if not app or not app.get("enabled") or not (app.get("feishu_app_id") and app.get("feishu_app_secret")):
                _request_stop(entry)
                _send_clients.pop(aid, None)
                if not entry.get("thread") or not entry["thread"].is_alive():
                    entry["state"] = "disabled" if not app or not app.get("enabled") else "unconfigured"
        for app in apps.values():
            if app.get("enabled") and app.get("feishu_app_id") and app.get("feishu_app_secret"):
                started += int(_ensure_connection(app, now))
    return started


def _supervise():
    while not _supervisor_stop.wait(_SUPERVISOR_INTERVAL):
        try:
            _supervise_once()
        except Exception as exc:
            # DB 暂时不可用也不杀掉守护线程，不输出可能带密钥的异常正文。
            print(f"[feishu supervisor] 刷新失败: {type(exc).__name__}", flush=True)


def start_app(app):
    """兼容单应用入口；守护线程会持续读取 DB 中当前启用状态和凭据。"""
    if not app or not app.get("enabled") or not (app.get("feishu_app_id") and app.get("feishu_app_secret")):
        return False
    before = {a["app_id"]: a["attempts"] for a in health()["apps"]}
    start_all()
    after = {a["app_id"]: a["attempts"] for a in health()["apps"]}
    return after.get(app["id"], 0) > before.get(app["id"], 0)


def start_all():
    """启动一次 supervisor；重复调用只做幂等刷新，不创建重复连接。"""
    global _supervisor_thread
    with _lifecycle_lock:
        _supervisor_stop.clear()
        count = _supervise_once()
        if _supervisor_thread is None or not _supervisor_thread.is_alive():
            _supervisor_thread = threading.Thread(target=_supervise, name="feishu-supervisor", daemon=True)
            _supervisor_thread.start()
        return count


def stop(timeout=5):
    """请求断开并在有界时间内等待退出；未退出的连接在 health 中仍显示 stopping。"""
    with _lifecycle_lock:
        _supervisor_stop.set()
        with _connection_lock:
            for entry in _connections.values():
                _request_stop(entry)
            threads = [e["thread"] for e in _connections.values() if e.get("thread")]
        if _supervisor_thread is not None:
            threads.append(_supervisor_thread)
        deadline = time.monotonic() + max(0, timeout)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in threads)


def health():
    """仅返回状态/时间/错误类别，不包含名称、凭据、指纹或 websocket URL。"""
    fields = ("app_id", "state", "connected", "attempts", "failures", "next_retry_at", "last_error",
              "started_at", "connected_at", "last_exit_at")
    with _connection_lock:
        return {"supervisor_running": bool(_supervisor_thread and _supervisor_thread.is_alive()),
                "stopping": _supervisor_stop.is_set(),
                "apps": [{key: entry.get(key) for key in fields} for _, entry in sorted(_connections.items())]}


def is_running(app_id):
    with _connection_lock:
        return bool(_running.get(app_id))


def any_running():
    with _connection_lock:
        return any(_running.values())
