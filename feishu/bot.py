"""飞书长连接：收群消息 → 大模型+工具 → 回群。在后台线程里常驻，只出站、不开端口。"""
import asyncio
import json
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from core import store
from llm.engine import run as llm_run

RUNNING = False          # 给管理后台看连接状态
_client = None


def _api():
    global _client
    if _client is None:
        app_id, app_secret = store.feishu_creds()
        _client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    return _client


def _reply(chat_id, text):
    req = (CreateMessageRequest.builder().receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                         .msg_type("text").content(json.dumps({"text": text})).build()).build())
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


def on_message(data):
    try:
        msg = data.event.message
        chat_id = msg.chat_id
        text = _strip_mentions(_extract_text(msg), getattr(msg, "mentions", None))
        if not text:
            return
        _reply(chat_id, llm_run(chat_id, text))
    except Exception as e:
        print("处理消息出错:", e)


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
