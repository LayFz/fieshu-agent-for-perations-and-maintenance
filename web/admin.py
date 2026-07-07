"""管理后台：FastAPI（登录 + AI供应商 + 应用 + 记忆 + 工具池 + 用量 + 全局可观测配置）。
前端是同源单文件 web/static/index.html（无需构建）。"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from core import store
from feishu import auth, bot
from llm import mcp as mcp_mod
from llm.tools import all_tools, tool_list_for

app = FastAPI(title="数字员工平台 管理后台", docs_url=None, redoc_url=None)
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

async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


# ---------- 静态首页 / 健康检查 ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (_WEB / "index.html").read_text(encoding="utf-8")

@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- 鉴权 ----------
@app.post("/api/login")
async def login(request: Request, response: Response):
    b = await _body(request)
    username = (b.get("username") or "").strip()
    password = b.get("password") or ""
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
    b = await _body(request)
    if not store.verify_admin(store.admin_username(), b.get("old") or ""):
        raise HTTPException(status_code=400, detail="原密码错误")
    new = b.get("new") or ""
    if len(new) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    store.set_password(new)
    return {"ok": True}


# ---------- 概览 ----------
@app.get("/api/overview")
def overview(request: Request):
    _auth(request)
    provs = {p["id"]: p for p in store.list_providers()}
    apps = []
    for a in store.list_apps():
        prov = provs.get(a.get("ai_provider_id"))
        apps.append({
            "id": a["id"], "name": a["name"], "enabled": a["enabled"],
            "running": bot.is_running(a["id"]),
            "feishu_set": bool(a.get("feishu_app_id") and a.get("feishu_app_secret")),
            "provider": prov["name"] if prov else None,
            "provider_ready": bool(prov and prov.get("api_key")),
            "tools_count": len(a.get("tools") or []),
            "skills_count": len(a.get("skills") or []),
        })
    return {
        "apps": apps,
        "providers_count": store.providers_count(),
        "any_running": bot.any_running(),
        "obs_configured": bool(store.get_setting("oo_base_url") and store.get_setting("oo_user")),
        "counts": store.counts(),
        "usage_total": store.usage_total(),
        "usage_today": store.usage_since(24 * 3600),
    }


# ---------- AI 供应商（多个模型） ----------
@app.get("/api/providers")
def providers(request: Request):
    _auth(request)
    return {"providers": [{
        "id": p["id"], "name": p["name"], "base_url": p["base_url"], "model": p["model"],
        "api_key_masked": _mask(p["api_key"]), "api_key_set": bool(p["api_key"]),
    } for p in store.list_providers()]}

@app.post("/api/providers")
async def create_provider(request: Request):
    _auth(request)
    b = await _body(request)
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填供应商名称")
    pid = store.create_provider(name, (b.get("base_url") or "").strip(),
                                (b.get("model") or "").strip(), (b.get("api_key") or "").strip())
    return {"ok": True, "id": pid}

@app.put("/api/providers/{pid}")
async def edit_provider(pid: int, request: Request):
    _auth(request)
    b = await _body(request)
    store.update_provider(pid, name=b.get("name"), base_url=b.get("base_url"),
                          model=b.get("model"), api_key=(b.get("api_key") or "").strip())
    return {"ok": True}

@app.delete("/api/providers/{pid}")
def remove_provider(pid: int, request: Request):
    _auth(request)
    store.delete_provider(pid)
    return {"ok": True}


# ---------- 工具池 ----------
@app.get("/api/tools/pool")
def tools_pool(request: Request):
    _auth(request)
    return {"tools": all_tools()}


# ---------- MCP server（全局注册表；应用按 server 整体勾选后其工具进工具循环） ----------
def _parse_headers(text):
    """把 'Key: Value' 多行文本解析成鉴权头字典（放 Authorization 之类）。"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out

def _mcp_view(s):
    h = s.get("headers") or {}
    return {"id": s["id"], "name": s["name"], "url": s.get("url") or "",
            "enabled": s["enabled"],
            "header_keys": list(h.keys()), "headers_set": bool(h)}   # 只回键名，值(可能含密钥)不外泄

@app.get("/api/mcp")
def mcp_list(request: Request):
    _auth(request)
    return {"servers": [_mcp_view(s) for s in store.list_mcp_servers()]}

@app.post("/api/mcp")
async def mcp_create(request: Request):
    _auth(request)
    b = await _body(request)
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填 MCP server 名称")
    mid = store.create_mcp_server(name, (b.get("url") or "").strip(),
                                  _parse_headers(b.get("headers_text")),
                                  1 if b.get("enabled", True) else 0)
    return {"ok": True, "id": mid}

@app.put("/api/mcp/{mid}")
async def mcp_edit(mid: int, request: Request):
    _auth(request)
    b = await _body(request)
    f = {}
    if "name" in b:    f["name"] = (b["name"] or "").strip()
    if "url" in b:     f["url"] = (b["url"] or "").strip()
    if "enabled" in b: f["enabled"] = 1 if b["enabled"] else 0
    ht = b.get("headers_text")                     # 留空=不改（保留旧鉴权头，同密码范式）
    if ht is not None and ht.strip():
        f["headers"] = _parse_headers(ht)
    store.update_mcp_server(mid, **f)
    mcp_mod.invalidate(mid)                         # 配置变了，清工具缓存
    return {"ok": True}

@app.delete("/api/mcp/{mid}")
def mcp_remove(mid: int, request: Request):
    _auth(request)
    store.delete_mcp_server(mid)
    mcp_mod.invalidate(mid)
    return {"ok": True}

@app.post("/api/mcp/{mid}/test")
def mcp_test(mid: int, request: Request):
    _auth(request)
    s = store.get_mcp_server(mid)
    if not s:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    try:
        tools = mcp_mod.fetch_tools(s)
        mcp_mod.invalidate(mid)                     # 下次运行按最新工具清单拉
        return {"ok": True, "count": len(tools),
                "tools": [{"name": t.get("name"), "description": t.get("description") or ""}
                          for t in tools]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- 技能（知识/工作方法包） ----------
@app.get("/api/skills")
def skills(request: Request):
    _auth(request)
    return {"skills": store.list_skills()}

@app.post("/api/skills")
async def create_skill(request: Request):
    _auth(request)
    b = await _body(request)
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填技能名称")
    try:
        sid = store.create_skill(name, b.get("description") or "", b.get("content") or "")
    except Exception:
        raise HTTPException(status_code=400, detail="技能名称已存在")
    return {"ok": True, "id": sid}

@app.put("/api/skills/{sid}")
async def edit_skill(sid: int, request: Request):
    _auth(request)
    b = await _body(request)
    store.update_skill(sid, name=b.get("name"), description=b.get("description"), content=b.get("content"))
    return {"ok": True}

@app.delete("/api/skills/{sid}")
def remove_skill(sid: int, request: Request):
    _auth(request)
    store.delete_skill(sid)
    return {"ok": True}


# ---------- 定时任务 ----------
@app.get("/api/schedules")
def schedules(request: Request):
    _auth(request)
    names = {a["id"]: a["name"] for a in store.list_apps()}
    out = []
    for sc in store.list_schedules(_app_id(request)):
        d = dict(sc)
        d["app_name"] = names.get(sc["app_id"])
        out.append(d)
    return {"schedules": out}

@app.post("/api/schedules")
async def create_schedule(request: Request):
    _auth(request)
    b = await _body(request)
    if not b.get("app_id") or not b.get("chat_id"):
        raise HTTPException(status_code=400, detail="需指定 app_id 与 chat_id（发哪个群）")
    if b.get("kind") not in ("daily", "weekly", "interval"):
        raise HTTPException(status_code=400, detail="kind 必须是 daily/weekly/interval")
    sid, nxt = store.create_schedule(int(b["app_id"]), b["chat_id"], b.get("title") or "",
                                     b.get("instruction") or "", b["kind"], b.get("spec") or "",
                                     1 if b.get("enabled", True) else 0)
    return {"ok": True, "id": sid, "next_run_ts": nxt}

@app.put("/api/schedules/{sid}")
async def edit_schedule(sid: int, request: Request):
    _auth(request)
    b = await _body(request)
    fields = {k: b[k] for k in ("title", "instruction", "kind", "spec", "chat_id", "enabled") if k in b}
    store.update_schedule(sid, **fields)
    return {"ok": True}

@app.delete("/api/schedules/{sid}")
def remove_schedule(sid: int, request: Request):
    _auth(request)
    store.delete_schedule(sid)
    return {"ok": True}

@app.post("/api/schedules/{sid}/run")
def run_schedule(sid: int, request: Request):
    _auth(request)
    from sched import runner
    ok = runner.run_now(sid)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


# ---------- 应用（数字员工/席位） ----------
def _app_view(a):
    return {
        "id": a["id"], "name": a["name"],
        "feishu_app_id": a.get("feishu_app_id") or "",
        "feishu_secret_masked": _mask(a.get("feishu_app_secret")),
        "feishu_secret_set": bool(a.get("feishu_app_secret")),
        "ai_provider_id": a.get("ai_provider_id"),
        "system_prompt": a.get("system_prompt") or "",
        "history_turns": int(a.get("history_turns") or 12),
        "tools": a.get("tools") or [],
        "skills": a.get("skills") or [],
        "mcp": a.get("mcp") or [],
        "enabled": a["enabled"],
        "running": bot.is_running(a["id"]),
    }

@app.get("/api/apps")
def apps(request: Request):
    _auth(request)
    return {"apps": [_app_view(a) for a in store.list_apps()]}

@app.get("/api/apps/{aid}")
def app_detail(aid: int, request: Request):
    _auth(request)
    a = store.get_app(aid)
    if not a:
        raise HTTPException(status_code=404, detail="应用不存在")
    v = _app_view(a)
    v["tool_list"] = tool_list_for(a.get("tools"))
    v["skill_list"] = [{"id": s["id"], "name": s["name"], "description": s.get("description")}
                       for s in store.skills_for(a.get("skills"))]
    v["mcp_list"] = [{"id": s["id"], "name": s["name"]}
                     for s in (store.get_mcp_server(i) for i in a.get("mcp") or []) if s]
    return v

@app.post("/api/apps")
async def create_app(request: Request):
    _auth(request)
    b = await _body(request)
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填应用名称")
    aid = store.create_app(
        name, (b.get("feishu_app_id") or "").strip(), (b.get("feishu_app_secret") or "").strip(),
        b.get("ai_provider_id"), b.get("system_prompt") or "",
        int(b.get("history_turns") or 12), b.get("tools") or [],
        1 if b.get("enabled", True) else 0, skills=b.get("skills") or [],
        mcp=b.get("mcp") or [])
    return {"ok": True, "id": aid, "note": "新应用的飞书长连接需重启服务后建立"}

@app.put("/api/apps/{aid}")
async def edit_app(aid: int, request: Request):
    _auth(request)
    b = await _body(request)
    fields = {k: b[k] for k in ("name", "feishu_app_id", "ai_provider_id",
                                "system_prompt", "history_turns", "tools", "skills", "mcp", "enabled") if k in b}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if b.get("feishu_app_secret"):
        fields["feishu_app_secret"] = b["feishu_app_secret"].strip()
    store.update_app(aid, **fields)
    auth.reset(aid)   # 该应用飞书凭证若变更，取数立即用新值；长连接仍需重启
    return {"ok": True, "note": "人设/工具/AI 即时生效；飞书凭证或启用状态变更需重启服务重连"}

@app.delete("/api/apps/{aid}")
def remove_app(aid: int, request: Request):
    _auth(request)
    store.delete_app(aid)
    return {"ok": True}


# ---------- 全局可观测配置（OpenObserve，供 query_logs 工具用） ----------
@app.get("/api/obs")
def obs_get(request: Request):
    _auth(request)
    oo_pw = store.get_setting("oo_pass", "")
    bz_pw = store.get_setting("bz_pass", "")
    uk_k = store.get_setting("uk_api_key", "")
    return {
        # OpenObserve（日志）
        "base_url": store.get_setting("oo_base_url", ""),
        "org": store.get_setting("oo_org", "default"),
        "user": store.get_setting("oo_user", ""),
        "pass_masked": _mask(oo_pw), "pass_set": bool(oo_pw),
        # Beszel（服务器负载）
        "bz_url": store.get_setting("bz_url", ""),
        "bz_user": store.get_setting("bz_user", ""),
        "bz_pass_masked": _mask(bz_pw), "bz_pass_set": bool(bz_pw),
        # Uptime Kuma（服务在线状态）
        "uk_url": store.get_setting("uk_url", ""),
        "uk_key_masked": _mask(uk_k), "uk_key_set": bool(uk_k),
    }

@app.post("/api/obs")
async def obs_set(request: Request):
    _auth(request)
    b = await _body(request)
    if "base_url" in b: store.set_setting("oo_base_url", (b["base_url"] or "").strip())
    if "org" in b:      store.set_setting("oo_org", (b["org"] or "default").strip())
    if "user" in b:     store.set_setting("oo_user", (b["user"] or "").strip())
    if b.get("pass"):   store.set_setting("oo_pass", b["pass"].strip())
    if "bz_url" in b:   store.set_setting("bz_url", (b["bz_url"] or "").strip())
    if "bz_user" in b:  store.set_setting("bz_user", (b["bz_user"] or "").strip())
    if b.get("bz_pass"): store.set_setting("bz_pass", b["bz_pass"].strip())
    if "uk_url" in b:   store.set_setting("uk_url", (b["uk_url"] or "").strip())
    if b.get("uk_key"): store.set_setting("uk_api_key", b["uk_key"].strip())
    return {"ok": True}


# ---------- SSH 运维（受管主机 + 硬黑名单，供 ssh_exec 工具用） ----------
@app.get("/api/ssh")
def ssh_get(request: Request):
    _auth(request)
    from ops import ssh as ops_ssh
    hosts = []
    for h in ops_ssh.hosts():                     # 不回密码/私钥明文，只回是否已设
        al = h.get("aliases")
        if isinstance(al, list):
            al = ", ".join(al)
        hosts.append({"name": h.get("name"), "host": h.get("host"),
                      "aliases": al or "",
                      "port": int(h.get("port", 22)), "username": h.get("username", "root"),
                      "password_set": bool(h.get("password")), "key_set": bool(h.get("key")),
                      "sudo_password_set": bool(h.get("sudo_password"))})
    bl = store.get_setting("ssh_blacklist")
    return {"hosts": hosts,
            "blacklist": json.loads(bl) if bl else ops_ssh._DEFAULT_BLACKLIST,
            "blacklist_is_default": not bl}

@app.post("/api/ssh")
async def ssh_set(request: Request):
    _auth(request)
    from ops import ssh as ops_ssh
    b = await _body(request)
    old = {h.get("name"): h for h in ops_ssh.hosts()}
    merged = []
    for h in b.get("hosts") or []:
        name = (h.get("name") or "").strip()
        if not name or not (h.get("host") or "").strip():
            continue                              # 名字/地址缺一不可
        prev = old.get(name, {})
        rec = {"name": name, "host": h["host"].strip(),
               "port": int(h.get("port") or 22),
               "username": (h.get("username") or "root").strip()}
        aliases = h.get("aliases")                # 别名：字符串(逗号/空格分隔)或数组 -> 存成去重列表
        if isinstance(aliases, str):
            aliases = re.split(r"[,\s]+", aliases)
        rec["aliases"] = [a.strip() for a in (aliases or []) if a and a.strip()]
        # 密码/私钥/sudo密码：传了才更新，留空则沿用旧值（同密码「留空=不改」范式）
        rec["password"] = h["password"] if h.get("password") else prev.get("password", "")
        rec["key"] = h["key"] if h.get("key") else prev.get("key", "")
        rec["sudo_password"] = h["sudo_password"] if h.get("sudo_password") else prev.get("sudo_password", "")
        merged.append(rec)
    store.set_setting("ssh_hosts", json.dumps(merged, ensure_ascii=False))
    if "blacklist" in b:                          # 空列表 = 用户显式清空；不传 = 不改
        bl = b["blacklist"]
        if isinstance(bl, list):
            store.set_setting("ssh_blacklist", json.dumps(bl, ensure_ascii=False))
    return {"ok": True, "count": len(merged)}

@app.post("/api/ssh/test")
async def ssh_test(request: Request):
    _auth(request)
    from ops import ssh as ops_ssh
    b = await _body(request)
    r = ops_ssh.run(b.get("target"), "whoami && hostname && uptime", timeout=15)
    return r


# ---------- 服务重启（建立新应用的长连接） ----------
@app.post("/api/restart")
def restart(request: Request):
    _auth(request)
    def _go():
        time.sleep(1.0)
        os.execv(sys.executable, [sys.executable, "-m", "app.main"])
    threading.Thread(target=_go, daemon=True).start()
    return {"ok": True}


# ---------- 记忆（按应用过滤） ----------
def _app_id(request):
    v = request.query_params.get("app_id")
    return int(v) if v not in (None, "", "all") else None

@app.get("/api/memory/notes")
def notes(request: Request):
    _auth(request)
    return {"notes": store.list_notes(_app_id(request))}

@app.delete("/api/memory/notes/{note_id}")
def del_note(note_id: int, request: Request):
    _auth(request)
    store.delete_note(note_id)
    return {"ok": True}

@app.get("/api/memory/chats")
def chats(request: Request):
    _auth(request)
    return {"chats": store.list_chats(_app_id(request))}

@app.get("/api/memory/chat")
def chat_detail(request: Request):
    _auth(request)
    aid = request.query_params.get("app_id")
    chat_id = request.query_params.get("chat_id", "")
    if aid in (None, "", "all"):
        raise HTTPException(status_code=400, detail="缺少 app_id")
    return {"messages": store.chat_messages(int(aid), chat_id)}


# ---------- token 用量（按应用过滤） ----------
@app.get("/api/usage")
def usage(request: Request):
    _auth(request)
    aid = _app_id(request)
    return {
        "total": store.usage_total(aid),
        "by_day": store.usage_by_day(app_id=aid),
        "by_model": store.usage_by_model(aid),
        "recent": store.usage_recent(50, aid),
    }
