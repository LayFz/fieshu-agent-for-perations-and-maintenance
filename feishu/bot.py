"""飞书长连接（多应用）：每个启用的应用各起一条长连接，收群消息 → 该应用的 AI+工具 → 回群。

每条连接绑定一个 app_id；收到消息时按 app_id 实时从 DB 取最新配置后再跑（后台改人设/工具/AI 即时生效）。
只出站、不开入站端口。新增/改飞书凭证的应用需重启进程才会建立新连接。
"""
import asyncio
import json
import re
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from core import store
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


def _handle(app_id, chat_id, text):
    # 实时取最新应用配置（后台改了人设/工具/AI 立即生效）
    app = store.get_app(app_id)
    if not app:
        return
    try:
        ans = llm_run(app, chat_id, text)
        print(f"[app {app_id}] 已回复 {ans[:80]!r}", flush=True)
        _reply(app, chat_id, ans)
    except Exception as e:
        import traceback
        print(f"[app {app_id}] 处理消息出错:", e, flush=True)
        traceback.print_exc()


def _on_message(app_id, data):
    try:
        msg = data.event.message
        msg_id = getattr(msg, "message_id", None)
        chat_id = msg.chat_id
        text = _strip_mentions(_extract_text(msg), getattr(msg, "mentions", None))
        print(f"[app {app_id}] 收到 id={msg_id} chat={chat_id} -> {text!r}", flush=True)
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
        threading.Thread(target=_handle, args=(app_id, chat_id, text), daemon=True).start()
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
