"""飞书长连接：收群消息 → 大模型+工具 → 回群。在后台线程里常驻，只出站、不开端口。"""
import asyncio
import json
import re
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from core import store
from llm.engine import run as llm_run

RUNNING = False          # 给管理后台看连接状态
_client = None
_seen = set()            # 已处理的 message_id：飞书事件可能重投，去重避免重复回答
_seen_lock = threading.Lock()


def _api():
    global _client
    if _client is None:
        app_id, app_secret = store.feishu_creds()
        _client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    return _client


def _md_card(text):
    # 飞书纯文本不渲染 Markdown；用交互卡片的 markdown 组件渲染 **粗体**/列表/链接/代码。
    # 标题(# ~ ######)降级成粗体行，避免飞书 markdown 不吃 ATX 标题时露出原始 # 号。
    md = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"**\1**", text, flags=re.M)
    return {"config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": md}]}


def _reply(chat_id, text):
    req = (CreateMessageRequest.builder().receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                         .msg_type("interactive")
                         .content(json.dumps(_md_card(text))).build()).build())
    resp = _api().im.v1.message.create(req)
    if not resp.success():
        print("回消息失败:", resp.code, resp.msg)


def _extract_text(message):
    try:
        return json.loads(message.content).get("text", "")
    except Exception:
        return ""


def _strip_mentions(text, mentions):
    # 群里 @机器人 时 text 形如 "@_user_1 实际内容"；按 mentions 去掉 @key 占位
    for mt in (mentions or []):
        key = getattr(mt, "key", None)
        if key:
            text = text.replace(key, "")
    return text.strip()


def _handle(chat_id, text):
    # 真正干活：调模型 + 工具，再回群。放后台线程，避免阻塞事件 ACK
    try:
        ans = llm_run(chat_id, text)
        print(f"[已回复] {ans[:80]!r}", flush=True)
        _reply(chat_id, ans)
    except Exception as e:
        import traceback
        print("处理消息出错:", e, flush=True)
        traceback.print_exc()


def on_message(data):
    # 必须尽快返回让 SDK 及时 ACK；否则飞书超时会重投 → 重复回答
    try:
        msg = data.event.message
        msg_id = getattr(msg, "message_id", None)
        chat_id = msg.chat_id
        text = _strip_mentions(_extract_text(msg), getattr(msg, "mentions", None))
        print(f"[收到消息] id={msg_id} chat={chat_id} -> {text!r}", flush=True)
        if not text:
            return
        with _seen_lock:                      # 去重：同一条消息(飞书重投)只处理一次
            if msg_id in _seen:
                print(f"[跳过重复] id={msg_id}", flush=True)
                return
            _seen.add(msg_id)
            if len(_seen) > 2000:             # 防集合无限增长
                _seen.clear()
                _seen.add(msg_id)
        threading.Thread(target=_handle, args=(chat_id, text), daemon=True).start()
    except Exception as e:
        print("处理消息出错:", e, flush=True)


def _serve():
    global RUNNING
    asyncio.set_event_loop(asyncio.new_event_loop())  # 子线程需要自己的事件循环
    app_id, app_secret = store.feishu_creds()
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    RUNNING = True
    print("feishu-agent bot：长连接监听中")
    try:
        lark.ws.Client(app_id, app_secret,
                       event_handler=handler, log_level=lark.LogLevel.INFO).start()
    finally:
        RUNNING = False


def start_in_background():
    threading.Thread(target=_serve, name="feishu-bot", daemon=True).start()
