"""群成员花名册（open_id ↔ 姓名），best-effort + 缓存。

让 agent 在群里「分辨谁是谁」「@得到人」靠这层：
- name_of()：把发言人 open_id 换成姓名，注入上下文（查不到回退 open_id 短码）。
- roster()：列出全群成员（姓名+open_id），给 agent 决定 @ 谁。

依赖应用的「读取群成员」权限(im:chat:readonly / im:chat.members:read)。
没权限或失败一律返回空/短码，绝不抛错阻断回消息。结果缓存 5 分钟。
"""
import threading
import time

import lark_oapi as lark
import requests
from lark_oapi.api.im.v1 import GetChatMembersRequest

_clients = {}      # feishu_app_id -> Client
_cache = {}        # (feishu_app_id, chat_id) -> (ts, [{name, open_id}])
_bot_oid = {}      # feishu_app_id -> 机器人自己的 open_id（判断是否被 @ 用）
_lock = threading.Lock()
_TTL = 300


def bot_open_id(app):
    """取机器人自己的 open_id（缓存）。用于判断群消息里是否 @ 了本机器人。"""
    k = app.get("feishu_app_id")
    if not k:
        return ""
    if k in _bot_oid:
        return _bot_oid[k]
    oid = ""
    try:
        t = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": k, "app_secret": app.get("feishu_app_secret")}, timeout=10).json()
        tok = t.get("tenant_access_token")
        if tok:
            r = requests.get("https://open.feishu.cn/open-apis/bot/v3/info",
                             headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()
            oid = ((r.get("bot") or {}).get("open_id") or "")
    except Exception as e:
        print(f"[members] 取 bot open_id 失败: {e}", flush=True)
    _bot_oid[k] = oid
    return oid


def _client(app):
    k = app.get("feishu_app_id")
    c = _clients.get(k)
    if c is None:
        c = lark.Client.builder().app_id(k).app_secret(app.get("feishu_app_secret")).build()
        _clients[k] = c
    return c


def roster(app, chat_id, force=False):
    """返回 [{'name':..., 'open_id':...}]；无权限/失败返回 []。缓存 5 分钟。"""
    if not (app.get("feishu_app_id") and chat_id):
        return []
    key = (app["feishu_app_id"], chat_id)
    now = time.time()
    if not force:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    out, token = [], None
    try:
        while True:
            b = (GetChatMembersRequest.builder().chat_id(chat_id)
                 .member_id_type("open_id").page_size(100))
            if token:
                b = b.page_token(token)
            resp = _client(app).im.v1.chat_members.get(b.build())
            if not resp.success() or not resp.data:
                break
            for m in (resp.data.items or []):
                oid = getattr(m, "member_id", "") or ""
                out.append({"name": (getattr(m, "name", "") or "").strip(), "open_id": oid})
            token = getattr(resp.data, "page_token", None)
            if not token or not getattr(resp.data, "has_more", False):
                break
    except Exception as e:
        print(f"[members] 读群成员失败 chat={chat_id}: {e}", flush=True)
    with _lock:
        _cache[key] = (now, out)
    return out


def name_of(app, chat_id, open_id):
    """open_id → 姓名；查不到回退 open_id 短码（仍可稳定区分不同人）。"""
    if not open_id:
        return ""
    for m in roster(app, chat_id):
        if m["open_id"] == open_id:
            return m["name"] or open_id[-6:]
    return open_id[-6:]
