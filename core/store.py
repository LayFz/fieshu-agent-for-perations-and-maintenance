"""数据层：单个 SQLite(data/agent.db) 管全部状态。

多应用架构：
  ai_providers   多个 AI（模型供应商）——name/base_url/model/api_key
  apps           多个应用（=数字员工/席位）——飞书凭证 + 绑定的 AI + 勾选的工具 + 人设
  msgs/notes/token_usage 都带 app_id，按应用隔离记忆与用量
其余：kv(引导/全局设置) / admin(管理员)。
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from .config import DB_PATH

_lock = threading.Lock()

# 每线程独立连接 + WAL：bot 线程写、后台线程读互不阻塞，避免 "database is locked"。
# 用代理对象保持 `_conn.execute(...)` 等原有调用不变。
_local = threading.local()

def _get_conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        _local.conn = c
    return c

class _ConnProxy:
    def __getattr__(self, name):
        return getattr(_get_conn(), name)

_conn = _ConnProxy()
_conn.executescript("""
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT, pw_hash TEXT);
    CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_msgs_chat ON msgs(chat_id, id);
    CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, scope TEXT, content TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS token_usage(id INTEGER PRIMARY KEY, chat_id TEXT, model TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_usage_ts ON token_usage(ts);

    CREATE TABLE IF NOT EXISTS ai_providers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        base_url TEXT, model TEXT, api_key TEXT,
        ts REAL);
    CREATE TABLE IF NOT EXISTS apps(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        feishu_app_id TEXT, feishu_app_secret TEXT,
        ai_provider_id INTEGER,
        system_prompt TEXT,
        history_turns INTEGER DEFAULT 12,
        tools TEXT DEFAULT '[]',
        skills TEXT DEFAULT '[]',
        enabled INTEGER DEFAULT 1,
        ts REAL);
    CREATE TABLE IF NOT EXISTS skills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        content TEXT,
        ts REAL);
    CREATE TABLE IF NOT EXISTS mcp_servers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        url TEXT,
        headers TEXT DEFAULT '{}',
        enabled INTEGER DEFAULT 1,
        ts REAL);
    CREATE TABLE IF NOT EXISTS schedules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id INTEGER, chat_id TEXT,
        title TEXT, instruction TEXT,
        kind TEXT, spec TEXT,
        enabled INTEGER DEFAULT 1,
        last_run_ts REAL, next_run_ts REAL,
        ts REAL);
""")
_conn.commit()


def _ensure_col(table, col, decl):
    cols = [r["name"] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        _conn.commit()

# 老库迁移：给记忆/用量表补 app_id（已存在则跳过）
_ensure_col("msgs", "app_id", "INTEGER")
_ensure_col("notes", "app_id", "INTEGER")
_ensure_col("token_usage", "app_id", "INTEGER")
_ensure_col("apps", "skills", "TEXT DEFAULT '[]'")
_ensure_col("apps", "mcp", "TEXT DEFAULT '[]'")     # 应用勾选的 MCP server id 名单
_conn.execute("CREATE INDEX IF NOT EXISTS idx_msgs_app ON msgs(app_id, chat_id, id)")
_conn.commit()


# ---------- 运行时设置(kv：引导 + 全局，如 OpenObserve 连接) ----------
def get_setting(k, default=None):
    r = _conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default

def set_setting(k, v):
    with _lock:
        _conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        _conn.commit()

def all_settings():
    return {r["k"]: r["v"] for r in _conn.execute("SELECT k,v FROM kv").fetchall()}

def seed_setting(k, v):
    """仅当 DB 里还没有时写入（首启播种）。"""
    if v in (None, ""):
        return
    if _conn.execute("SELECT 1 FROM kv WHERE k=?", (k,)).fetchone() is None:
        set_setting(k, v)


# ---------- 服务端签名密钥（cookie 用） ----------
def server_secret():
    s = get_setting("server_secret")
    if not s:
        s = os.urandom(32).hex()
        set_setting("server_secret", s)
    return s


# ---------- AI 供应商（多个模型） ----------
def list_providers():
    rows = _conn.execute("SELECT id,name,base_url,model,api_key,ts FROM ai_providers ORDER BY id").fetchall()
    return [dict(r) for r in rows]

def get_provider(pid):
    r = _conn.execute("SELECT id,name,base_url,model,api_key,ts FROM ai_providers WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None

def create_provider(name, base_url, model, api_key):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO ai_providers(name,base_url,model,api_key,ts) VALUES(?,?,?,?,?)",
            (name, base_url or "", model or "", api_key or "", time.time()))
        _conn.commit()
        return cur.lastrowid

def update_provider(pid, **f):
    sets, vals = [], []
    for k in ("name", "base_url", "model"):
        if k in f and f[k] is not None:
            sets.append(f"{k}=?"); vals.append(f[k])
    if f.get("api_key"):                       # 仅非空才更新 key，避免掩码覆盖真值
        sets.append("api_key=?"); vals.append(f["api_key"])
    if not sets:
        return
    vals.append(pid)
    with _lock:
        _conn.execute(f"UPDATE ai_providers SET {','.join(sets)} WHERE id=?", vals)
        _conn.commit()

def delete_provider(pid):
    with _lock:
        _conn.execute("DELETE FROM ai_providers WHERE id=?", (pid,))
        _conn.commit()


# ---------- 应用（数字员工/席位） ----------
def _app_row(r):
    d = dict(r)
    for col in ("tools", "skills", "mcp"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except Exception:
            d[col] = []
    d["enabled"] = bool(d.get("enabled"))
    return d

def list_apps():
    rows = _conn.execute("SELECT * FROM apps ORDER BY id").fetchall()
    return [_app_row(r) for r in rows]

def list_enabled_apps():
    rows = _conn.execute("SELECT * FROM apps WHERE enabled=1 ORDER BY id").fetchall()
    return [_app_row(r) for r in rows]

def get_app(aid):
    r = _conn.execute("SELECT * FROM apps WHERE id=?", (aid,)).fetchone()
    return _app_row(r) if r else None

def create_app(name, feishu_app_id="", feishu_app_secret="", ai_provider_id=None,
               system_prompt="", history_turns=12, tools=None, enabled=1, skills=None, mcp=None):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO apps(name,feishu_app_id,feishu_app_secret,ai_provider_id,system_prompt,"
            "history_turns,tools,skills,mcp,enabled,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (name, feishu_app_id or "", feishu_app_secret or "", ai_provider_id,
             system_prompt or "", int(history_turns or 12),
             json.dumps(tools or [], ensure_ascii=False),
             json.dumps(skills or [], ensure_ascii=False),
             json.dumps(mcp or [], ensure_ascii=False), 1 if enabled else 0, time.time()))
        _conn.commit()
        return cur.lastrowid

def update_app(aid, **f):
    sets, vals = [], []
    for k in ("name", "feishu_app_id", "ai_provider_id", "system_prompt", "history_turns", "enabled"):
        if k in f and f[k] is not None:
            sets.append(f"{k}=?")
            vals.append(int(f[k]) if k in ("history_turns", "enabled", "ai_provider_id") else f[k])
    if "tools" in f and f["tools"] is not None:
        sets.append("tools=?"); vals.append(json.dumps(f["tools"], ensure_ascii=False))
    if "skills" in f and f["skills"] is not None:
        sets.append("skills=?"); vals.append(json.dumps(f["skills"], ensure_ascii=False))
    if "mcp" in f and f["mcp"] is not None:
        sets.append("mcp=?"); vals.append(json.dumps(f["mcp"], ensure_ascii=False))
    if f.get("feishu_app_secret"):              # 仅非空才更新 secret
        sets.append("feishu_app_secret=?"); vals.append(f["feishu_app_secret"])
    if not sets:
        return
    vals.append(aid)
    with _lock:
        _conn.execute(f"UPDATE apps SET {','.join(sets)} WHERE id=?", vals)
        _conn.commit()

def delete_app(aid):
    with _lock:
        _conn.execute("DELETE FROM apps WHERE id=?", (aid,))
        _conn.commit()

def apps_count():
    return _conn.execute("SELECT COUNT(*) n FROM apps").fetchone()["n"]


# ---------- MCP server（全局注册表，应用按 server 整体勾选） ----------
def _mcp_row(r):
    d = dict(r)
    try:
        d["headers"] = json.loads(d.get("headers") or "{}")
    except Exception:
        d["headers"] = {}
    d["enabled"] = bool(d.get("enabled"))
    return d

def list_mcp_servers():
    rows = _conn.execute("SELECT * FROM mcp_servers ORDER BY id").fetchall()
    return [_mcp_row(r) for r in rows]

def get_mcp_server(mid):
    r = _conn.execute("SELECT * FROM mcp_servers WHERE id=?", (mid,)).fetchone()
    return _mcp_row(r) if r else None

def create_mcp_server(name, url="", headers=None, enabled=1):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO mcp_servers(name,url,headers,enabled,ts) VALUES(?,?,?,?,?)",
            (name, url or "", json.dumps(headers or {}, ensure_ascii=False),
             1 if enabled else 0, time.time()))
        _conn.commit()
        return cur.lastrowid

def update_mcp_server(mid, **f):
    sets, vals = [], []
    if f.get("name"):
        sets.append("name=?"); vals.append(f["name"])
    if "url" in f and f["url"] is not None:
        sets.append("url=?"); vals.append(f["url"])
    if "headers" in f and f["headers"] is not None:   # 只在传了新 headers 时才覆盖（否则保留旧密钥）
        sets.append("headers=?"); vals.append(json.dumps(f["headers"], ensure_ascii=False))
    if "enabled" in f and f["enabled"] is not None:
        sets.append("enabled=?"); vals.append(1 if f["enabled"] else 0)
    if not sets:
        return
    vals.append(mid)
    with _lock:
        _conn.execute(f"UPDATE mcp_servers SET {','.join(sets)} WHERE id=?", vals)
        _conn.commit()

def delete_mcp_server(mid):
    with _lock:
        _conn.execute("DELETE FROM mcp_servers WHERE id=?", (mid,))
        _conn.commit()

def providers_count():
    return _conn.execute("SELECT COUNT(*) n FROM ai_providers").fetchone()["n"]


# ---------- 技能（知识/工作方法包，按应用勾选） ----------
def list_skills():
    rows = _conn.execute("SELECT id,name,description,content,ts FROM skills ORDER BY id").fetchall()
    return [dict(r) for r in rows]

def get_skill(sid):
    r = _conn.execute("SELECT id,name,description,content,ts FROM skills WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None

def create_skill(name, description, content):
    with _lock:
        cur = _conn.execute("INSERT INTO skills(name,description,content,ts) VALUES(?,?,?,?)",
                            (name, description or "", content or "", time.time()))
        _conn.commit()
        return cur.lastrowid

def update_skill(sid, **f):
    sets, vals = [], []
    for k in ("name", "description", "content"):
        if k in f and f[k] is not None:
            sets.append(f"{k}=?"); vals.append(f[k])
    if not sets:
        return
    vals.append(sid)
    with _lock:
        _conn.execute(f"UPDATE skills SET {','.join(sets)} WHERE id=?", vals)
        _conn.commit()

def delete_skill(sid):
    with _lock:
        _conn.execute("DELETE FROM skills WHERE id=?", (sid,))
        _conn.commit()

def skills_count():
    return _conn.execute("SELECT COUNT(*) n FROM skills").fetchone()["n"]

def skills_for(ids):
    """按 id 列表取技能（保持顺序、跳过已删除的）。"""
    out = []
    for sid in (ids or []):
        sk = get_skill(sid)
        if sk:
            out.append(sk)
    return out


# ---------- 定时任务（按应用） ----------
def next_run(kind, spec, from_ts=None):
    """根据周期算下次运行的 epoch 秒。kind: daily|weekly|interval。
    daily spec='HH:MM'；weekly spec='1,2,3,4,5@HH:MM'(1=周一..7=周日)；interval spec=分钟数。"""
    now = datetime.now() if from_ts is None else datetime.fromtimestamp(from_ts)
    try:
        if kind == "interval":
            return (now + timedelta(minutes=max(1, int(spec)))).timestamp()
        if kind == "daily":
            hh, mm = (int(x) for x in str(spec).split(":"))
            cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= now:
                cand += timedelta(days=1)
            return cand.timestamp()
        if kind == "weekly":
            days_part, time_part = str(spec).split("@")
            hh, mm = (int(x) for x in time_part.split(":"))
            days = {int(d) for d in days_part.split(",")}
            for add in range(0, 8):
                cand = (now + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if (cand.weekday() + 1) in days and cand > now:
                    return cand.timestamp()
    except Exception:
        pass
    return (now + timedelta(days=1)).timestamp()

def _sched_row(r):
    return dict(r)

def list_schedules(app_id=None):
    if app_id is None:
        rows = _conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    else:
        rows = _conn.execute("SELECT * FROM schedules WHERE app_id=? ORDER BY id", (app_id,)).fetchall()
    return [_sched_row(r) for r in rows]

def get_schedule(sid):
    r = _conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    return _sched_row(r) if r else None

def create_schedule(app_id, chat_id, title, instruction, kind, spec, enabled=1):
    nxt = next_run(kind, spec)
    with _lock:
        cur = _conn.execute(
            "INSERT INTO schedules(app_id,chat_id,title,instruction,kind,spec,enabled,next_run_ts,ts)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (app_id, chat_id, title or "", instruction or "", kind, str(spec),
             1 if enabled else 0, nxt, time.time()))
        _conn.commit()
        return cur.lastrowid, nxt

def update_schedule(sid, **f):
    cur = get_schedule(sid)
    if not cur:
        return
    sets, vals = [], []
    for k in ("title", "instruction", "kind", "spec", "chat_id"):
        if k in f and f[k] is not None:
            sets.append(f"{k}=?"); vals.append(str(f[k]) if k in ("spec",) else f[k])
    if "enabled" in f and f["enabled"] is not None:
        sets.append("enabled=?"); vals.append(1 if f["enabled"] else 0)
    # 周期/启用变了就重算下次运行
    kind = f.get("kind", cur["kind"])
    spec = f.get("spec", cur["spec"])
    if ("kind" in f) or ("spec" in f) or (f.get("enabled")):
        sets.append("next_run_ts=?"); vals.append(next_run(kind, spec))
    if not sets:
        return
    vals.append(sid)
    with _lock:
        _conn.execute(f"UPDATE schedules SET {','.join(sets)} WHERE id=?", vals)
        _conn.commit()

def delete_schedule(sid):
    with _lock:
        _conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
        _conn.commit()

def due_schedules(now_ts):
    rows = _conn.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_ts IS NOT NULL AND next_run_ts<=?",
                         (now_ts,)).fetchall()
    return [_sched_row(r) for r in rows]

def mark_schedule_ran(sid, ran_ts):
    sc = get_schedule(sid)
    if not sc:
        return
    nxt = next_run(sc["kind"], sc["spec"], ran_ts)
    with _lock:
        _conn.execute("UPDATE schedules SET last_run_ts=?, next_run_ts=? WHERE id=?", (ran_ts, nxt, sid))
        _conn.commit()

def reschedule_overdue():
    """启动时：把已启用但 next_run 落在过去的任务推到下一次（避免重启后补跑爆发）。"""
    now = time.time()
    for sc in list_schedules():
        if sc["enabled"] and (sc["next_run_ts"] is None or sc["next_run_ts"] <= now):
            with _lock:
                _conn.execute("UPDATE schedules SET next_run_ts=? WHERE id=?",
                              (next_run(sc["kind"], sc["spec"], now), sc["id"]))
                _conn.commit()

def schedules_count():
    return _conn.execute("SELECT COUNT(*) n FROM schedules").fetchone()["n"]


# ---------- 旧版（单应用）凭证读取，仅供首启迁移用 ----------
def legacy_feishu_creds():
    return (get_setting("feishu_app_id", "") or "", get_setting("feishu_app_secret", "") or "")


# ---------- 管理员 ----------
def _hash_pw(pw, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
    return salt.hex() + "$" + dk.hex()

def _verify_pw(pw, stored):
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

def ensure_admin(username, password):
    if _conn.execute("SELECT 1 FROM admin WHERE id=1").fetchone() is None:
        with _lock:
            _conn.execute("INSERT INTO admin(id,username,pw_hash) VALUES(1,?,?)",
                          (username, _hash_pw(password)))
            _conn.commit()
        return True
    return False

def admin_username():
    r = _conn.execute("SELECT username FROM admin WHERE id=1").fetchone()
    return r["username"] if r else None

def verify_admin(username, password):
    r = _conn.execute("SELECT username, pw_hash FROM admin WHERE id=1").fetchone()
    return bool(r and r["username"] == username and _verify_pw(password, r["pw_hash"]))

def set_password(new_password):
    with _lock:
        _conn.execute("UPDATE admin SET pw_hash=? WHERE id=1", (_hash_pw(new_password),))
        _conn.commit()


# ---------- 对话记忆（按应用隔离） ----------
def save(app_id, chat_id, role, content):
    with _lock:
        _conn.execute("INSERT INTO msgs(app_id,chat_id,role,content,ts) VALUES(?,?,?,?,?)",
                      (app_id, chat_id, role, content, time.time()))
        _conn.commit()

def load_history(app_id, chat_id, turns):
    rows = _conn.execute("SELECT role,content FROM msgs WHERE app_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
                         (app_id, chat_id, turns * 2)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def list_chats(app_id=None):
    if app_id is None:
        rows = _conn.execute(
            "SELECT app_id, chat_id, COUNT(*) n, MAX(ts) last FROM msgs GROUP BY app_id, chat_id "
            "ORDER BY last DESC").fetchall()
    else:
        rows = _conn.execute(
            "SELECT app_id, chat_id, COUNT(*) n, MAX(ts) last FROM msgs WHERE app_id=? "
            "GROUP BY chat_id ORDER BY last DESC", (app_id,)).fetchall()
    return [{"app_id": r["app_id"], "chat_id": r["chat_id"], "count": r["n"], "last_ts": r["last"]} for r in rows]

def chat_messages(app_id, chat_id, limit=200):
    rows = _conn.execute("SELECT role,content,ts FROM msgs WHERE app_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
                         (app_id, chat_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in reversed(rows)]


# ---------- 长期笔记（按应用隔离） ----------
def add_note(app_id, content):
    with _lock:
        _conn.execute("INSERT INTO notes(app_id,scope,content,ts) VALUES(?,?,?,?)",
                      (app_id, "app", content, time.time()))
        _conn.commit()

def load_notes(app_id, limit=30):
    rows = _conn.execute("SELECT content FROM notes WHERE app_id=? ORDER BY id DESC LIMIT ?",
                         (app_id, limit)).fetchall()
    return [r["content"] for r in rows]

def list_notes(app_id=None, limit=300):
    if app_id is None:
        rows = _conn.execute("SELECT id,app_id,content,ts FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = _conn.execute("SELECT id,app_id,content,ts FROM notes WHERE app_id=? ORDER BY id DESC LIMIT ?",
                             (app_id, limit)).fetchall()
    return [{"id": r["id"], "app_id": r["app_id"], "content": r["content"], "ts": r["ts"]} for r in rows]

def delete_note(note_id):
    with _lock:
        _conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        _conn.commit()


# ---------- token 用量（按应用隔离） ----------
def record_usage(app_id, chat_id, model, prompt_tokens, completion_tokens, total_tokens):
    with _lock:
        _conn.execute("INSERT INTO token_usage(app_id,chat_id,model,prompt_tokens,completion_tokens,total_tokens,ts)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (app_id, chat_id, model, prompt_tokens, completion_tokens, total_tokens, time.time()))
        _conn.commit()

def _app_filter(app_id):
    return ("", ()) if app_id is None else (" AND app_id=?", (app_id,))

def usage_total(app_id=None):
    w, p = _app_filter(app_id)
    r = _conn.execute("SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c,"
                      f" COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM token_usage WHERE 1=1{w}", p).fetchone()
    return {"prompt": r["p"], "completion": r["c"], "total": r["t"], "calls": r["n"]}

def usage_since(seconds, app_id=None):
    w, p = _app_filter(app_id)
    r = _conn.execute(f"SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM token_usage WHERE ts>=?{w}",
                      (time.time() - seconds, *p)).fetchone()
    return {"total": r["t"], "calls": r["n"]}

def usage_by_day(days=14, app_id=None):
    w, p = _app_filter(app_id)
    rows = _conn.execute("SELECT date(ts,'unixepoch','localtime') d, SUM(total_tokens) t, COUNT(*) n"
                         f" FROM token_usage WHERE 1=1{w} GROUP BY d ORDER BY d DESC LIMIT ?",
                         (*p, days)).fetchall()
    return [{"day": r["d"], "total": r["t"], "calls": r["n"]} for r in rows]

def usage_by_model(app_id=None):
    w, p = _app_filter(app_id)
    rows = _conn.execute(f"SELECT model, SUM(total_tokens) t, COUNT(*) n FROM token_usage WHERE 1=1{w}"
                         " GROUP BY model ORDER BY t DESC", p).fetchall()
    return [{"model": r["model"], "total": r["t"], "calls": r["n"]} for r in rows]

def usage_recent(n=50, app_id=None):
    w, p = _app_filter(app_id)
    rows = _conn.execute("SELECT app_id,chat_id,model,prompt_tokens,completion_tokens,total_tokens,ts"
                         f" FROM token_usage WHERE 1=1{w} ORDER BY id DESC LIMIT ?", (*p, n)).fetchall()
    return [dict(r) for r in rows]


# ---------- 计数（概览用） ----------
def counts(app_id=None):
    w, p = _app_filter(app_id)
    return {
        "messages": _conn.execute(f"SELECT COUNT(*) n FROM msgs WHERE 1=1{w}", p).fetchone()["n"],
        "notes": _conn.execute(f"SELECT COUNT(*) n FROM notes WHERE 1=1{w}", p).fetchone()["n"],
        "chats": _conn.execute(f"SELECT COUNT(DISTINCT chat_id) n FROM msgs WHERE 1=1{w}", p).fetchone()["n"],
        "apps": apps_count(),
        "providers": providers_count(),
        "skills": skills_count(),
        "schedules": schedules_count(),
    }
