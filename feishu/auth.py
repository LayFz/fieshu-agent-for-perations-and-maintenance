"""应用身份 token（tenant_access_token）：app_id+secret 自动换取并缓存，永不过期、不绑个人。"""
import threading
import time

import requests

from core import store

_lock = threading.Lock()
_cache = {"token": None, "exp": 0.0, "key": None}


def reset():
    """飞书凭证变更后清缓存，下次取数用新 token。"""
    with _lock:
        _cache["token"] = None


def tenant_token() -> str:
    app_id, app_secret = store.feishu_creds()  # 实时取自 DB（后台可改）
    now = time.time()
    if _cache["token"] and _cache["key"] == app_id and now < _cache["exp"] - 120:
        return _cache["token"]
    with _lock:
        if _cache["token"] and _cache["key"] == app_id and now < _cache["exp"] - 120:
            return _cache["token"]
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        ).json()
        if r.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {r}")
        _cache["token"] = r["tenant_access_token"]
        _cache["exp"] = time.time() + r.get("expire", 7200)
        _cache["key"] = app_id
        return _cache["token"]
