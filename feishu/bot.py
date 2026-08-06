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


def _md_card(text):
    md = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"**\1**", text, flags=re.M)
    return {"config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": md}]}


def _reply(app, chat_id, text):
    req = (CreateMessageRequest.builder().receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                         .msg_type("interactive")
                         .content(json.dumps(_md_card(text))).build()).build())
    resp = _send_client(app).im.v1.message.create(req)
    if not resp.success():
        print(f"[app {app['id']}] 回消息失败:", resp.code, resp.msg, flush=True)


def post(app, chat_id, text):
    """对外发消息（定时任务等主动推送用）。app 为 store.get_app() 字典。"""
    _reply(app, chat_id, text)


def _send_card(app, chat_id, text):
    """发一张交互卡片，返回 message_id（供后续原地更新）；失败返回 None。"""
    req = (CreateMessageRequest.builder().receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                         .msg_type("interactive")
                         .content(json.dumps(_md_card(text))).build()).build())
    resp = _send_client(app).im.v1.message.create(req)
    if not resp.success():
        print(f"[app {app['id']}] 发卡片失败:", resp.code, resp.msg, flush=True)
        return None
    return getattr(resp.data, "message_id", None)


def _update_card(app, message_id, text):
    """原地更新一张已发的卡片（PATCH）。message_id 为空则跳过。返回是否成功；失败只记日志、不抛。"""
    if not message_id:
        return False
    req = (PatchMessageRequest.builder().message_id(message_id)
           .request_body(PatchMessageRequestBody.builder()
                         .content(json.dumps(_md_card(text))).build()).build())
    resp = _send_client(app).im.v1.message.patch(req)
    if not resp.success():
        print(f"[app {app['id']}] 更新卡片失败:", resp.code, resp.msg, flush=True)
        return False
    return True


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
        if not (mid and _update_card(app, mid, ans)):    # 最终答案完整落一版（也兜底 patch 失败 / 没卡片）
            _reply(app, chat_id, ans)
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
