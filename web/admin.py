"""管理后台：FastAPI（登录 + 大模型配置 + 记忆浏览 + 工具清单 + token 用量）。
前端是同源单文件 web/static/index.html（无需构建）。"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from core import store
from core.config import cfg
from feishu import auth, bot
from llm.tools import tool_list

app = FastAPI(title="feishu-agent 管理后台", docs_url=None, redoc_url=None)
_WEB = Path(__file__).resolve().parent / "static"
_COOKIE = "fa_sid"
_TTL = 7 * 24 * 3600


# ---------- 会话（HMAC 签名 cookie） ----------
def _make_session(username):
    payload = json.dumps({"u": username, "exp": int(time.time()) + _TTL}).encode()
    sig = hmac.new(store.server_secret().encode(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode() + "." + sig

def _read_session(token):
    try:
        b64, sig = token.split(".")
        payload = base64.urlsafe_b64decode(b64)
        good = hmac.new(store.server_secret().encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        data = json.loads(payload)
        return data["u"] if data.get("exp", 0) >= time.time() else None
    except Exception:
        return None

def _auth(request: Request):
    u = _read_session(request.cookies.get(_COOKIE, ""))
    if not u:
        raise HTTPException(status_code=401, detail="未登录")
    return u

def _mask(s):
    if not s:
        return ""
    return (s[:3] + "***" + s[-2:]) if len(s) > 6 else "***"


# ---------- 静态首页 ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (_WEB / "index.html").read_text(encoding="utf-8")


# ---------- 鉴权 ----------
@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not store.verify_admin(username, password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(_COOKIE, _make_session(username), max_age=_TTL, httponly=True, samesite="lax")
    return {"ok": True, "username": username}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(_COOKIE)
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    return {"username": _auth(request)}

@app.post("/api/password")
async def change_password(request: Request):
    _auth(request)
    body = await request.json()
    if not store.verify_admin(store.admin_username(), body.get("old") or ""):
        raise HTTPException(status_code=400, detail="原密码错误")
    new = body.get("new") or ""
    if len(new) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    store.set_password(new)
    return {"ok": True}


# ---------- 概览 ----------
@app.get("/api/overview")
def overview(request: Request):
    _auth(request)
    return {
        "bot_running": bot.RUNNING,
        "feishu_configured": store.has_feishu(),
        "llm_configured": bool(store.get_setting("llm_api_key")),
        "model": store.get_setting("llm_model"),
        "counts": store.counts(),
        "usage_total": store.usage_total(),
        "usage_today": store.usage_since(24 * 3600),
    }


# ---------- 大模型配置 ----------
@app.get("/api/settings")
def get_settings(request: Request):
    _auth(request)
    return {
        "base_url": store.get_setting("llm_base_url", ""),
        "model": store.get_setting("llm_model", ""),
        "api_key_masked": _mask(store.get_setting("llm_api_key", "")),
        "api_key_set": bool(store.get_setting("llm_api_key")),
        "system_prompt": store.get_setting("system_prompt", ""),
        "history_turns": int(store.get_setting("history_turns", "12") or 12),
    }

@app.post("/api/settings")
async def save_settings(request: Request):
    _auth(request)
    b = await request.json()
    if "base_url" in b:       store.set_setting("llm_base_url", (b["base_url"] or "").strip())
    if "model" in b:          store.set_setting("llm_model", (b["model"] or "").strip())
    if b.get("api_key"):      store.set_setting("llm_api_key", b["api_key"].strip())  # 仅非空才更新，避免掩码覆盖真值
    if "system_prompt" in b:  store.set_setting("system_prompt", b["system_prompt"] or "")
    if "history_turns" in b:
        try:
            store.set_setting("history_turns", str(max(0, int(b["history_turns"]))))
        except (TypeError, ValueError):
            pass
    return {"ok": True}


# ---------- 飞书配置 ----------
@app.get("/api/feishu")
def feishu_get(request: Request):
    _auth(request)
    sec = store.get_setting("feishu_app_secret", "")
    return {"app_id": store.get_setting("feishu_app_id", ""),
            "app_secret_masked": _mask(sec), "app_secret_set": bool(sec),
            "bot_running": bot.RUNNING}

@app.post("/api/feishu")
async def feishu_set(request: Request):
    _auth(request)
    b = await request.json()
    if "app_id" in b:       store.set_setting("feishu_app_id", (b["app_id"] or "").strip())
    if b.get("app_secret"): store.set_setting("feishu_app_secret", b["app_secret"].strip())  # 仅非空才更新
    auth.reset()  # 取数(读写文档)立即用新凭证；长连接需「重启」重连
    return {"ok": True}

@app.post("/api/feishu/restart")
def feishu_restart(request: Request):
    _auth(request)
    # 进程自重执行：长连接是启动时建立的，换凭证后整进程重启才会用新凭证重连（本地/Docker 都管用）
    def _go():
        time.sleep(1.0)
        os.execv(sys.executable, [sys.executable, "-m", "app.main"])
    threading.Thread(target=_go, daemon=True).start()
    return {"ok": True}


# ---------- 工具清单 ----------
@app.get("/api/tools")
def tools(request: Request):
    _auth(request)
    return {"tools": tool_list()}


# ---------- 记忆 ----------
@app.get("/api/memory/notes")
def notes(request: Request):
    _auth(request)
    return {"notes": store.list_notes()}

@app.delete("/api/memory/notes/{note_id}")
def del_note(note_id: int, request: Request):
    _auth(request)
    store.delete_note(note_id)
    return {"ok": True}

@app.get("/api/memory/chats")
def chats(request: Request):
    _auth(request)
    return {"chats": store.list_chats()}

@app.get("/api/memory/chats/{chat_id}")
def chat_detail(chat_id: str, request: Request):
    _auth(request)
    return {"messages": store.chat_messages(chat_id)}


# ---------- token 用量 ----------
@app.get("/api/usage")
def usage(request: Request):
    _auth(request)
    return {
        "total": store.usage_total(),
        "by_day": store.usage_by_day(),
        "by_model": store.usage_by_model(),
        "recent": store.usage_recent(50),
    }
