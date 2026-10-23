"""飞书长连接（多应用）：每个启用的应用各起一条长连接，收群消息 → 该应用的 AI+工具 → 回群。

每条连接绑定一个 app_id；收到消息时按 app_id 实时从 DB 取最新配置后再跑（后台改人设/工具/AI 即时生效）。
只出站、不开入站端口。新增/改飞书凭证的应用需重启进程才会建立新连接。
"""
import asyncio
import json
import re
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 PatchMessageRequest, PatchMessageRequestBody)

from core import store
from feishu import members
from harness import approval
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

_send_clients = {}          # app_id -> lark.Client（回消息用）
_running = {}               # app_id -> bool（连接状态，给后台看）
_started = set()            # 已启动过 _serve 的 app_id，避免重复起线程
_seen = set()              # (app_id, message_id) 去重：飞书事件可能重投
_seen_lock = threading.Lock()


def _send_client(app):
    c = _send_clients.get(app["id"])
    if c is None:
        c = lark.Client.builder().app_id(app["feishu_app_id"]).app_secret(app["feishu_app_secret"]).build()
        _send_clients[app["id"]] = c
    return c


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


def _md_card(text):
    """流式/中途用：单个 markdown 元素，md 表格降级为分行列表（飞书 markdown 不渲染表格）。"""
    md = _tables_to_text(text)
    md = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"**\1**", md, flags=re.M)
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


def _rich_card(text):
    """最终定版用：普通文字走 markdown，markdown 表格渲染成飞书原生 table 组件（真表格）。"""
    elements, buf = [], []
    def flush():
        if buf:
            md = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"**\1**", "\n".join(buf), flags=re.M).strip("\n")
            if md.strip():
                elements.append({"tag": "markdown", "content": md})
            buf.clear()
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


def _create_card(app, chat_id, card):
    req = (CreateMessageRequest.builder().receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                         .msg_type("interactive").content(json.dumps(card)).build()).build())
    return _send_client(app).im.v1.message.create(req)


def _reply(app, chat_id, text, rich=False):
    """发一条卡片消息。rich=True 用原生表格；被飞书拒了自动退回分行 markdown。"""
    resp = _create_card(app, chat_id, _rich_card(text) if rich else _md_card(text))
    if not resp.success() and rich:
        resp = _create_card(app, chat_id, _md_card(text))     # 真表格被拒 → 退回分行
    if not resp.success():
        print(f"[app {app['id']}] 回消息失败:", resp.code, resp.msg, flush=True)


def post(app, chat_id, text):
    """对外发消息（定时任务等主动推送用）。app 为 store.get_app() 字典。"""
    _reply(app, chat_id, text, rich=True)


def _send_card(app, chat_id, text):
    """发一张交互卡片，返回 message_id（供后续原地更新）；失败返回 None。"""
    resp = _create_card(app, chat_id, _md_card(text))
    if not resp.success():
        print(f"[app {app['id']}] 发卡片失败:", resp.code, resp.msg, flush=True)
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
        print(f"[app {app['id']}] 更新卡片失败:", resp.code, resp.msg, flush=True)
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
    elements = [{"tag": "markdown", "content": "\n".join(lines)},
                {"tag": "markdown", "content": f"**参数**\n```json\n{args_txt}\n```"}]
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
           "risk": req["risk"], "reason": req["reason"], "requester_open_id": req["requester_open_id"]}
    resp = _create_card(app, req["chat_id"], _approval_card(app, row))
    if not resp.success():
        print(f"[app {app['id']}] 发审批卡片失败:", resp.code, resp.msg, flush=True)
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
        ok, msg, row = approval.decide(value.get("approval_id"), value.get("choice") == "approve", operator)
        print(f"[app {app_id}] 审批 {str(value.get('approval_id'))[:8]} by {operator[-6:]} "
              f"{value.get('choice')} -> {msg}", flush=True)
        body = {"toast": {"type": "success" if ok else "error", "content": msg}}
        if ok and row:
            app = store.get_app(row["app_id"]) or {"name": "", "id": app_id}
            decider = members.name_of(app, row.get("chat_id"), operator)
            body["card"] = {"type": "raw", "data": _approval_card(app, row, row["status"], decider)}
        return P2CardActionTriggerResponse(body)
    except Exception as e:
        print(f"[app {app_id}] 处理卡片回调出错:", e, flush=True)
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
_POST_COOLDOWN = 30        # 同一个群两次主动发言至少间隔（秒），防刷屏
_last_post = {}            # chat_id -> ts
_inflight = set()          # 正在主动研判中的 chat_id，避免并发风暴
_pro_lock = threading.Lock()
# 「刚交互过」窗口：某人点名（@/喊名/单聊）之后，这段时间内 ta 的追问也算「对机器人说话」，
# 不必每句都 @——连续对话更自然。按 (chat_id, 发言人) 隔离，只惠及正在跟它聊的那个人。
_FOLLOWUP_WINDOW = 180     # 秒
_last_engaged = {}         # (chat_id, sender_open_id) -> ts


def _name_called(text):
    t = (text or "").lower()
    return any(h in text or h in t for h in _NAME_HINTS)


def _worth_considering(text):
    t = (text or "").lower()
    return any(k in t for k in _TRIGGER_KW)


def _observe_or_react(app_id, chat_id, text, sender_open_id):
    """群里没点名的消息：先记历史攒上下文；像"有事"的再让 Jarvis 自己判断要不要主动开口。"""
    app = store.get_app(app_id)
    if not app:
        return
    speaker = members.name_of(app, chat_id, sender_open_id) if sender_open_id else ""

    # 不值得花大模型判断的闲聊：只默默记历史
    if not _worth_considering(text):
        turn = f"「{speaker}」：{text}" if speaker else text
        store.save(app_id, chat_id, "user", turn)
        return

    # 冷却中、或本群已有一条在研判：也只旁听记录，不重复烧钱
    now = time.time()
    with _pro_lock:
        if chat_id in _inflight or now - _last_post.get(chat_id, 0) < _POST_COOLDOWN:
            turn = f"「{speaker}」：{text}" if speaker else text
            store.save(app_id, chat_id, "user", turn)
            return
        _inflight.add(chat_id)
    try:
        # 主动模式：engine 会存这条 user，并自行判断该不该说话（不该则返回 None）
        ans = llm_run(app, chat_id, text, speaker=speaker,
                      speaker_open_id=sender_open_id, proactive=True)
        if ans:
            _reply(app, chat_id, ans)
            with _pro_lock:
                _last_post[chat_id] = time.time()
            print(f"[app {app_id}] Jarvis 主动发言 {ans[:60]!r}", flush=True)
    finally:
        with _pro_lock:
            _inflight.discard(chat_id)


def _handle(app_id, chat_id, text, sender_open_id=""):
    # 实时取最新应用配置（后台改了人设/工具/AI 立即生效）
    app = store.get_app(app_id)
    if not app:
        return
    # 解析发言人姓名（best-effort，无权限时回退 open_id 短码）
    speaker = members.name_of(app, chat_id, sender_open_id) if sender_open_id else ""
    # 即时回执 + 实时进度：先发一张「处理中」卡片，边查边把「正在跑什么」刷进同一张卡，最后更新为结论
    mid = _send_card(app, chat_id, "🔍 收到，正在处理…")
    steps = []
    ans_buf = [""]
    last = [0.0]
    def _render():
        blocks = []
        if steps:
            shown = steps[-10:]
            more = f"…（前 {len(steps) - len(shown)} 步略）\n" if len(steps) > len(shown) else ""
            blocks.append("🔍 处理中…\n" + more + "\n".join(f"- {s}" for s in shown))
        if ans_buf[0]:
            blocks.append(ans_buf[0])
        return "\n\n".join(blocks) or "🔍 收到，正在处理…"
    def on_step(labels):                                 # 进入新一轮工具调用：追加步骤 + 清掉上一轮零碎输出
        if not mid:
            return
        steps.extend(labels or [])
        ans_buf[0] = ""
        _update_card(app, mid, _render())
    def on_delta(piece):                                 # 最终答案的流式增量：累积 + 节流刷卡片
        if not mid:
            return
        ans_buf[0] += piece
        now = time.time()
        if now - last[0] >= 0.5:                          # 最多每 0.5s 刷一次（在飞书更新限流内）
            last[0] = now
            _update_card(app, mid, _render())
    try:
        ans = llm_run(app, chat_id, text, speaker=speaker, speaker_open_id=sender_open_id,
                      on_step=on_step, on_delta=on_delta)
        print(f"[app {app_id}] 已回复 {ans[:80]!r}", flush=True)
        if not (mid and _update_card(app, mid, ans, rich=True)):   # 最终定版：原生真表格（被拒自动退分行）
            _reply(app, chat_id, ans, rich=True)
    except Exception as e:
        import traceback
        print(f"[app {app_id}] 处理消息出错:", e, flush=True)
        traceback.print_exc()
        if mid:
            _update_card(app, mid, "处理出错了，稍后再试～")


def _on_message(app_id, data):
    try:
        msg = data.event.message
        msg_id = getattr(msg, "message_id", None)
        chat_id = msg.chat_id
        mentions = getattr(msg, "mentions", None)
        chat_type = getattr(msg, "chat_type", "")     # 'p2p' 单聊 / 'group' 群聊
        text = _strip_mentions(_extract_text(msg), mentions)
        sender_open_id = ""
        try:
            sender_open_id = data.event.sender.sender_id.open_id or ""
        except Exception:
            pass
        if not text:
            return
        seen_key = (app_id, msg_id)
        with _seen_lock:
            if seen_key in _seen:
                return
            _seen.add(seen_key)
            if len(_seen) > 4000:
                _seen.clear()
                _seen.add(seen_key)
        # 回应闸门（开了 group_msg 后群里所有消息都会来）：
        #   单聊 / 被@ / 直接喊「贾维斯」= 点名 → 必回；
        #   其余 → 旁听，像"有事"的再由 Jarvis 自己判断要不要主动开口。
        app = store.get_app(app_id)
        if not app:
            return
        now = time.time()
        ekey = (chat_id, sender_open_id)
        # 显式点名：@ / 喊名字 / 单聊
        called = (chat_type == "p2p") or _is_bot_mentioned(app, mentions) or _name_called(text)
        # 刚交互过：同一个人点名后 _FOLLOWUP_WINDOW 秒内的追问，也算对它说话（不必再 @）
        engaged = bool(sender_open_id) and (now - _last_engaged.get(ekey, 0) < _FOLLOWUP_WINDOW)
        addressed = called or engaged
        if addressed and sender_open_id:                 # 刷新窗口，让连续对话保持"在聊"
            if len(_last_engaged) > 2000:
                _last_engaged.clear()
            _last_engaged[ekey] = now
        tag = "回应" if called else ("回应(追问)" if engaged else "旁听")
        print(f"[app {app_id}] 收到 id={msg_id} chat={chat_id} from={sender_open_id[-6:]} "
              f"{tag} -> {text!r}", flush=True)
        if addressed:
            threading.Thread(target=_handle, args=(app_id, chat_id, text, sender_open_id), daemon=True).start()
        else:
            threading.Thread(target=_observe_or_react, args=(app_id, chat_id, text, sender_open_id), daemon=True).start()
    except Exception as e:
        print(f"[app {app_id}] 事件处理出错:", e, flush=True)


def _serve(app):
    app_id = app["id"]
    # 事件循环由上面的 _ThreadLoopProxy 按线程惰性创建（lark start() 首次访问 loop 时）
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(lambda data, _id=app_id: _on_message(_id, data))
               .register_p2_card_action_trigger(lambda data, _id=app_id: _on_card_action(_id, data))
               .build())
    _running[app_id] = True
    print(f"[app {app_id}] {app['name']} 长连接监听中", flush=True)
    try:
        lark.ws.Client(app["feishu_app_id"], app["feishu_app_secret"],
                       event_handler=handler, log_level=lark.LogLevel.INFO).start()
    finally:
        _running[app_id] = False


def start_app(app):
    """为单个应用起长连接（已起过或缺凭证则跳过）。"""
    if not (app.get("feishu_app_id") and app.get("feishu_app_secret")):
        return False
    if app["id"] in _started:
        return False
    _started.add(app["id"])
    threading.Thread(target=_serve, args=(app,), name=f"bot-{app['id']}", daemon=True).start()
    return True


def start_all():
    """启动所有「启用且配了飞书凭证」的应用长连接。"""
    n = 0
    for app in store.list_enabled_apps():
        if start_app(app):
            n += 1
    print(f"★ 已启动 {n} 个应用的飞书长连接", flush=True)
    return n


def is_running(app_id):
    return bool(_running.get(app_id))

def any_running():
    return any(_running.values())
