"""入口：飞书长连接收群消息 -> 大模型+工具 -> 回群。常驻进程，只出站、不开端口。"""
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .config import cfg
from .llm import run as llm_run

cfg.require()  # 启动即校验必填配置（缺 key 直接报错，别静默）

# 普通 API 客户端：回消息 / 操作文档（自动用 tenant_access_token）
_client = lark.Client.builder().app_id(cfg.feishu["app_id"]).app_secret(cfg.feishu["app_secret"]).build()


def _reply(chat_id, text):
    req = (CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(chat_id).msg_type("text")
                         .content(json.dumps({"text": text})).build())
           .build())
    resp = _client.im.v1.message.create(req)
    if not resp.success():
        print("回消息失败:", resp.code, resp.msg)


def _extract_text(message):
    try:
        c = json.loads(message.content)
    except Exception:
        return ""
    return c.get("text", "")  # text 类型 {"text":"..."}；其它类型 v1 先兜底


def _strip_mentions(text, mentions):
    # 群里 @机器人 时 text 形如 "@_user_1 实际内容"；按 mentions 去掉 @key 占位
    for mt in (mentions or []):
        key = getattr(mt, "key", None)
        if key:
            text = text.replace(key, "")
    return text.strip()


def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        chat_id = msg.chat_id
        text = _strip_mentions(_extract_text(msg), getattr(msg, "mentions", None))
        if not text:
            return
        answer = llm_run(chat_id, text)
        _reply(chat_id, answer)
    except Exception as e:
        print("处理消息出错:", e)


def main():
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())
    print("feishu-agent 启动：长连接监听中（Ctrl+C 退出）")
    lark.ws.Client(cfg.feishu["app_id"], cfg.feishu["app_secret"],
                   event_handler=handler, log_level=lark.LogLevel.INFO).start()


if __name__ == "__main__":
    main()
