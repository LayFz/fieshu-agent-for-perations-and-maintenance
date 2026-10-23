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
from harness import policy
from harness import skills as skills_sync
from harness import workspace
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
    return {"tools": all_tools(), "policy_presets": policy.presets_public()}


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

def _manual_skill_or_400(sid):
    sk = store.get_skill(sid)
    if sk and sk.get("source_id"):
        raise HTTPException(status_code=400, detail="这个技能从 Git 同步而来，后台只读；要改请到对应能力仓提 MR，删除请在技能源里取消同步")
    return sk

@app.get("/api/skills/{sid}")
def skill_detail(sid: int, request: Request):
    _auth(request)
    sk = store.get_skill(sid)
    if not sk:
        raise HTTPException(status_code=404, detail="技能不存在")
    return sk

@app.put("/api/skills/{sid}")
async def edit_skill(sid: int, request: Request):
    _auth(request)
    _manual_skill_or_400(sid)
    b = await _body(request)
    store.update_skill(sid, name=b.get("name"), description=b.get("description"), content=b.get("content"))
    return {"ok": True}

@app.delete("/api/skills/{sid}")
def remove_skill(sid: int, request: Request):
    _auth(request)
    _manual_skill_or_400(sid)
    store.delete_skill(sid)
    return {"ok": True}


# ---------- 代码仓库与工作区（harness.workspace） ----------
def _repo_view(r):
    v = {k: r.get(k) for k in ("id", "name", "url", "branch", "username", "test_cmd", "enabled")}
    v.update(token_set=bool(r.get("token")), token_masked=_mask(r.get("token")),
             write_token_set=bool(r.get("write_token")), write_token_masked=_mask(r.get("write_token")))
    return v

def _repo_fields(b, creating):
    f = {}
    if creating or "name" in b:
        f["name"] = (b.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", f["name"] or ""):
            raise HTTPException(status_code=400, detail="仓库名只能用字母、数字、. _ -（模型按这个名字打开仓库）")
    if creating or "url" in b:
        f["url"] = (b.get("url") or "").strip()
        if not (f["url"].startswith(("https://", "http://")) or os.path.isabs(f["url"])):
            raise HTTPException(status_code=400, detail="仓库地址需为 https:// 开头")
    for k in ("branch", "username", "test_cmd"):
        if k in b:
            f[k] = (b.get(k) or "").strip()
    for k in ("token", "write_token"):
        if b.get(k):
            f[k] = b[k].strip()
    if "enabled" in b:
        f["enabled"] = 1 if b["enabled"] else 0
    return f

@app.get("/api/code-repos")
def code_repos(request: Request):
    _auth(request)
    names = {r["id"]: r["name"] for r in store.list_code_repos()}
    apps_ = {a["id"]: a["name"] for a in store.list_apps()}
    wss = [{"id": w["id"], "repo": names.get(w["repo_id"], "#%s" % w["repo_id"]), "app": apps_.get(w["app_id"]),
            "chat_id": w["chat_id"], "base_branch": w["base_branch"], "base_commit": (w["base_commit"] or "")[:12],
            "changed": len(w["changed"]), "mr_url": w.get("mr_url"), "created": w["created"], "last_used": w["last_used"]}
           for w in store.list_workspaces(status="open")]
    return {"repos": [_repo_view(r) for r in store.list_code_repos()], "workspaces": wss,
            "ttl_hours": workspace.TTL_HOURS}

@app.post("/api/code-repos")
async def create_code_repo(request: Request):
    _auth(request)
    f = _repo_fields(await _body(request), creating=True)
    if store.get_code_repo_by_name(f["name"]):
        raise HTTPException(status_code=400, detail="仓库名已存在")
    return {"ok": True, "id": store.create_code_repo(**f)}

@app.put("/api/code-repos/{rid}")
async def edit_code_repo(rid: int, request: Request):
    _auth(request)
    if not store.get_code_repo(rid):
        raise HTTPException(status_code=404, detail="仓库不存在")
    f = _repo_fields(await _body(request), creating=False)
    other = store.get_code_repo_by_name(f.get("name") or "")
    if other and other["id"] != rid:
        raise HTTPException(status_code=400, detail="仓库名已存在")
    store.update_code_repo(rid, **f)
    return {"ok": True}

@app.delete("/api/code-repos/{rid}")
def remove_code_repo(rid: int, request: Request):
    _auth(request)
    for w in store.list_workspaces(status="open"):
        if w["repo_id"] == rid:
            workspace.close_workspace(w["id"])
    store.delete_code_repo(rid)
    return {"ok": True}

@app.delete("/api/workspaces/{ws_id}")
def remove_workspace(ws_id: str, request: Request):
    _auth(request)
    return {"ok": workspace.close_workspace(ws_id)}


# ---------- 技能源（从 Git 同步技能，harness.skills） ----------
def _source_view(s):
    v = {k: s.get(k) for k in ("id", "name", "kind", "url", "branch", "subdir", "username", "rewrite", "plugins",
                               "enabled", "interval_min", "last_sync", "last_status", "last_error", "last_report")}
    v["token_masked"] = _mask(s.get("token"))
    v["token_set"] = bool(s.get("token"))
    v["skill_count"] = len(store.skills_by_source(s["id"]))
    return v

def _split_list(v):
    if isinstance(v, str):
        v = re.split(r"[\s,，]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]

def _source_fields(b, creating):
    f = {}
    if creating or "name" in b:
        f["name"] = (b.get("name") or "").strip()
        if not f["name"]:
            raise HTTPException(status_code=400, detail="请填技能源名称")
    if creating or "kind" in b:
        f["kind"] = b.get("kind") or "marketplace"
        if f["kind"] not in ("marketplace", "repo"):
            raise HTTPException(status_code=400, detail="类型只能是 marketplace（索引仓）或 repo（单个能力仓）")
    if creating or "url" in b:
        f["url"] = (b.get("url") or "").strip()
        if not (f["url"].startswith(("https://", "http://")) or os.path.isabs(f["url"])):
            raise HTTPException(status_code=400, detail="仓库地址需为 https:// 开头（ssh 地址请用改写规则转成 https）")
    for k in ("branch", "subdir", "username"):
        if k in b:
            f[k] = (b.get(k) or "").strip()
    if b.get("token"):
        f["token"] = b["token"].strip()
    if "plugins" in b:
        f["plugins"] = _split_list(b.get("plugins"))
    if "rewrite" in b:
        raw = b.get("rewrite") or []
        if isinstance(raw, str):                         # 每行「from => to」
            raw = [ln.split("=>", 1) for ln in raw.splitlines() if "=>" in ln]
        rules = []
        for r in raw:
            if not (isinstance(r, (list, tuple)) and len(r) == 2 and str(r[0]).strip() and str(r[1]).strip()):
                raise HTTPException(status_code=400, detail="改写规则格式：每行「原前缀 => 新前缀」")
            rules.append([str(r[0]).strip(), str(r[1]).strip()])
        f["rewrite"] = rules
    if "enabled" in b:
        f["enabled"] = 1 if b["enabled"] else 0
    if "interval_min" in b:
        try:
            f["interval_min"] = max(5, int(b.get("interval_min") or 60))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="同步间隔需为分钟数")
    return f

@app.get("/api/skill-sources")
def skill_sources(request: Request):
    _auth(request)
    return {"sources": [_source_view(s) for s in store.list_skill_sources()]}

@app.post("/api/skill-sources")
async def create_skill_source(request: Request):
    _auth(request)
    sid = store.create_skill_source(**_source_fields(await _body(request), creating=True))
    return {"ok": True, "id": sid}

@app.put("/api/skill-sources/{sid}")
async def edit_skill_source(sid: int, request: Request):
    _auth(request)
    if not store.get_skill_source(sid):
        raise HTTPException(status_code=404, detail="技能源不存在")
    store.update_skill_source(sid, **_source_fields(await _body(request), creating=False))
    return {"ok": True}

@app.delete("/api/skill-sources/{sid}")
def remove_skill_source(sid: int, request: Request):
    _auth(request)
    store.delete_skill_source(sid)
    return {"ok": True}

@app.post("/api/skill-sources/{sid}/sync")
def sync_skill_source(sid: int, request: Request):
    """立即同步（同步执行，可能要几十秒）。"""
    _auth(request)
    try:
        rep = skills_sync.sync_source(sid)
    except skills_sync.SyncError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "report": rep, "source": _source_view(store.get_skill_source(sid))}


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
        "thinking": bool(a.get("thinking", True)),
        "policy": policy.normalize(a.get("policy")),
        "approvers": a.get("approvers") or [],
        "enabled": a["enabled"],
        "running": bot.is_running(a["id"]),
    }

def _policy_from(b):
    """从请求体取工具调用策略并校验；没传返回 None（不改）。"""
    if "policy" not in b:
        return None
    p = b.get("policy") or {}
    if not isinstance(p, dict):
        raise HTTPException(status_code=400, detail="策略格式不对")
    preset = p.get("preset") or policy.DEFAULT_PRESET
    if preset == policy.CUSTOM:
        try:
            return {"preset": policy.CUSTOM, "rules": policy.validate_rules(p.get("rules"))}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"自定义规则有误：{e}")
    if preset not in policy.PRESETS:
        raise HTTPException(status_code=400, detail=f"未知的策略预设：{preset}")
    return {"preset": preset}

def _approvers_from(b):
    """审批人 open_id 名单：接受数组或换行/逗号分隔的字符串；没传返回 None（不改）。"""
    if "approvers" not in b:
        return None
    raw = b.get("approvers") or []
    if isinstance(raw, str):
        raw = re.split(r"[\s,，]+", raw)
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="审批人格式不对")
    out = []
    for x in raw:
        x = str(x).strip()
        if not x:
            continue
        if not re.fullmatch(r"ou_[0-9a-zA-Z]{8,64}", x):
            raise HTTPException(status_code=400, detail=f"审批人需填飞书 open_id（ou_ 开头）：{x}")
        if x not in out:
            out.append(x)
    return out

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
    pol = _policy_from(b)
    appr = _approvers_from(b)
    aid = store.create_app(
        name, (b.get("feishu_app_id") or "").strip(), (b.get("feishu_app_secret") or "").strip(),
        b.get("ai_provider_id"), b.get("system_prompt") or "",
        int(b.get("history_turns") or 12), b.get("tools") or [],
        1 if b.get("enabled", True) else 0, skills=b.get("skills") or [],
        mcp=b.get("mcp") or [])
    if "thinking" in b:
        store.update_app(aid, thinking=1 if b["thinking"] else 0)
    if pol is not None:
        store.update_app(aid, policy=pol)
    if appr is not None:
        store.update_app(aid, approvers=appr)
    return {"ok": True, "id": aid, "note": "新应用的飞书长连接需重启服务后建立"}

@app.put("/api/apps/{aid}")
async def edit_app(aid: int, request: Request):
    _auth(request)
    b = await _body(request)
    fields = {k: b[k] for k in ("name", "feishu_app_id", "ai_provider_id", "system_prompt",
                                "history_turns", "tools", "skills", "mcp", "enabled", "thinking") if k in b}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if "thinking" in fields:
        fields["thinking"] = 1 if fields["thinking"] else 0
    if b.get("feishu_app_secret"):
        fields["feishu_app_secret"] = b["feishu_app_secret"].strip()
    pol = _policy_from(b)
    if pol is not None:
        fields["policy"] = pol
    appr = _approvers_from(b)
    if appr is not None:
        fields["approvers"] = appr
    store.update_app(aid, **fields)
    auth.reset(aid)   # 该应用飞书凭证若变更，取数立即用新值；长连接仍需重启
    return {"ok": True, "note": "人设/工具/AI 即时生效；飞书凭证或启用状态变更需重启服务重连"}

@app.delete("/api/apps/{aid}")
def remove_app(aid: int, request: Request):
    _auth(request)
    store.delete_app(aid)
    return {"ok": True}


# ---------- 全局可观测配置（OpenObserve，供 query_logs 工具用） ----------
def _oo_sources_raw():
    """读 oo_sources（额外组织）：OpenObserve 按 org 隔离，每个 org 一套 service account。"""
    try:
        v = json.loads(store.get_setting("oo_sources", "") or "[]")
    except Exception:
        v = []
    return v if isinstance(v, list) else []


def _oo_sources_public():
    """回给前端的额外组织配置（密码只回是否已设，不回明文）。"""
    out = []
    for s in _oo_sources_raw():
        if not isinstance(s, dict) or not (s.get("org") or "").strip():
            continue
        out.append({
            "name": (s.get("name") or s["org"]).strip(),
            "org": s["org"].strip(),
            "user": (s.get("user") or "").strip(),
            "pass_set": bool(s.get("pass")),
        })
    return out


def _save_oo_sources(rows):
    """保存额外组织；某行没传新密码时沿用旧密码（前端留空=不改）。"""
    old = {(s.get("org") or "").strip().lower(): s
           for s in _oo_sources_raw() if isinstance(s, dict)}
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        org = (r.get("org") or "").strip()
        key = org.lower()
        if not org or key in seen:
            continue
        seen.add(key)
        pw = (r.get("pass") or "").strip() or (old.get(key) or {}).get("pass") or ""
        out.append({"name": (r.get("name") or org).strip(), "org": org,
                    "user": (r.get("user") or "").strip(), "pass": pw})
    store.set_setting("oo_sources", json.dumps(out, ensure_ascii=False))


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
        "org_sources": _oo_sources_public(),
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
    if "org_sources" in b: _save_oo_sources(b.get("org_sources"))
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


# ---------- K8s 运维（受管集群 kubeconfig + 硬黑名单，供 kubectl 工具用） ----------
@app.get("/api/k8s")
def k8s_get(request: Request):
    _auth(request)
    from ops import k8s as ops_k8s
    clusters = []
    for c in ops_k8s.clusters():                  # 不回 kubeconfig 明文，只回是否已设
        al = c.get("aliases")
        if isinstance(al, list):
            al = ", ".join(al)
        clusters.append({"name": c.get("name"), "aliases": al or "",
                         "read_only": bool(c.get("read_only")),
                         "kubeconfig_set": bool(c.get("kubeconfig"))})
    bl = store.get_setting("k8s_blacklist")
    return {"clusters": clusters,
            "blacklist": json.loads(bl) if bl else ops_k8s._DEFAULT_BLACKLIST,
            "blacklist_is_default": not bl}


@app.post("/api/k8s")
async def k8s_set(request: Request):
    _auth(request)
    from ops import k8s as ops_k8s
    b = await _body(request)
    old = {c.get("name"): c for c in ops_k8s.clusters()}
    merged = []
    for c in b.get("clusters") or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        prev = old.get(name, {})
        rec = {"name": name, "read_only": bool(c.get("read_only"))}
        aliases = c.get("aliases")                 # 别名：字符串(逗号/空格分隔)或数组 -> 去重列表
        if isinstance(aliases, str):
            aliases = re.split(r"[,\s]+", aliases)
        rec["aliases"] = [a.strip() for a in (aliases or []) if a and a.strip()]
        # kubeconfig：传了才更新，留空则沿用旧值（同 SSH 密码「留空=不改」范式）
        rec["kubeconfig"] = c["kubeconfig"] if c.get("kubeconfig") else prev.get("kubeconfig", "")
        merged.append(rec)
    store.set_setting("k8s_clusters", json.dumps(merged, ensure_ascii=False))
    if "blacklist" in b:                           # 空列表 = 用户显式清空；不传 = 不改
        bl = b["blacklist"]
        if isinstance(bl, list):
            store.set_setting("k8s_blacklist", json.dumps(bl, ensure_ascii=False))
    return {"ok": True, "count": len(merged)}


@app.post("/api/k8s/test")
async def k8s_test(request: Request):
    _auth(request)
    from ops import k8s as ops_k8s
    b = await _body(request)
    r = ops_k8s.run(b.get("target"), "get nodes -o wide", timeout=15)
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


# ---------- 运行记录（run + 事件时间线，只读） ----------
@app.get("/api/runs")
def runs_list(request: Request):
    _auth(request)
    q = request.query_params
    return {"runs": store.list_runs(_app_id(request), trigger=q.get("trigger") or None,
                                    status=q.get("status") or None, before=q.get("before") or None,
                                    limit=q.get("limit") or 50)}

@app.get("/api/runs/{rid}")
def run_detail(rid: str, request: Request):
    _auth(request)
    r = store.get_run(rid)
    if not r:
        raise HTTPException(status_code=404, detail="没有这个 run")
    return {"run": r, "events": store.run_events(rid)}


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
