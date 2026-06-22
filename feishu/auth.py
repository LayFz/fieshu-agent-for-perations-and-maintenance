"""应用身份 token（tenant_access_token）：按应用的 app_id+secret 换取并缓存。

多应用：当前线程用哪个应用的凭证，由 contextvar 决定（engine 跑工具前用 use_app 设定）。
缓存按 app_id 分桶，不同应用互不串。
"""
import contextvars
import threading
import time

import requests

from core import store

_lock = threading.Lock()
_cache = {}                                  # app_id -> {"token": str, "exp": float}
_current = contextvars.ContextVar("feishu_creds", default=None)   # (app_id, app_secret)


def use_app(app_id, app_secret):
    """设定当前上下文使用的飞书应用凭证；返回 token 供 reset_ctx 还原。"""
    return _current.set((app_id, app_secret))

def reset_ctx(token):
    try:
        _current.reset(token)
    except Exception:
        pass

def reset(app_id=None):
    """凭证变更后清缓存：不传清全部，传 app_id 只清那一个。"""
    with _lock:
        if app_id is None:
            _cache.clear()
        else:
            _cache.pop(app_id, None)


def tenant_token() -> str:
    cur = _current.get()
    if cur and cur[0]:
        app_id, app_secret = cur
    else:
        app_id, app_secret = store.legacy_feishu_creds()   # 兜底：迁移前的全局凭证
    now = time.time()
    c = _cache.get(app_id)
    if c and now < c["exp"] - 120:
        return c["token"]
    with _lock:
        c = _cache.get(app_id)
        if c and now < c["exp"] - 120:
            return c["token"]
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        ).json()
        if r.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {r}")
        _cache[app_id] = {"token": r["tenant_access_token"], "exp": time.time() + r.get("expire", 7200)}
        return _cache[app_id]["token"]
