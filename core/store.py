"""数据层：单个 SQLite(data/agent.db) 管全部状态——
运行时设置(kv) / 管理员 / 对话记忆(msgs) / 长期笔记(notes) / token 用量(token_usage)。"""
import hashlib
import hmac
import os
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT, pw_hash TEXT);
    CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_msgs_chat ON msgs(chat_id, id);
    CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, scope TEXT, content TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS token_usage(id INTEGER PRIMARY KEY, chat_id TEXT, model TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_usage_ts ON token_usage(ts);
""")
_conn.commit()


# ---------- 运行时设置(kv) ----------
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
    """仅当 DB 里还没有时写入（首启播种，之后以管理后台为准）。"""
    if v in (None, "") :
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
    """首启播种管理员；已存在则不动（改密走 set_password）。返回是否新建。"""
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


# ---------- 对话记忆 ----------
def save(chat_id, role, content):
    with _lock:
        _conn.execute("INSERT INTO msgs(chat_id,role,content,ts) VALUES(?,?,?,?)",
                      (chat_id, role, content, time.time()))
        _conn.commit()

def load_history(chat_id, turns):
    rows = _conn.execute("SELECT role,content FROM msgs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                         (chat_id, turns * 2)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def list_chats():
    rows = _conn.execute(
        "SELECT chat_id, COUNT(*) n, MAX(ts) last FROM msgs GROUP BY chat_id ORDER BY last DESC").fetchall()
    return [{"chat_id": r["chat_id"], "count": r["n"], "last_ts": r["last"]} for r in rows]

def chat_messages(chat_id, limit=200):
    rows = _conn.execute("SELECT role,content,ts FROM msgs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                         (chat_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in reversed(rows)]


# ---------- 长期笔记 ----------
def add_note(scope, content):
    with _lock:
        _conn.execute("INSERT INTO notes(scope,content,ts) VALUES(?,?,?)", (scope, content, time.time()))
        _conn.commit()

def load_notes(scope="global", limit=30):
    rows = _conn.execute("SELECT content FROM notes WHERE scope=? ORDER BY id DESC LIMIT ?",
                         (scope, limit)).fetchall()
    return [r["content"] for r in rows]

def list_notes(limit=300):
    rows = _conn.execute("SELECT id,scope,content,ts FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "scope": r["scope"], "content": r["content"], "ts": r["ts"]} for r in rows]

def delete_note(note_id):
    with _lock:
        _conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        _conn.commit()


# ---------- token 用量 ----------
def record_usage(chat_id, model, prompt_tokens, completion_tokens, total_tokens):
    with _lock:
        _conn.execute("INSERT INTO token_usage(chat_id,model,prompt_tokens,completion_tokens,total_tokens,ts)"
                      " VALUES(?,?,?,?,?,?)",
                      (chat_id, model, prompt_tokens, completion_tokens, total_tokens, time.time()))
        _conn.commit()

def usage_total():
    r = _conn.execute("SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c,"
                      " COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM token_usage").fetchone()
    return {"prompt": r["p"], "completion": r["c"], "total": r["t"], "calls": r["n"]}

def usage_since(seconds):
    r = _conn.execute("SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM token_usage WHERE ts>=?",
                      (time.time() - seconds,)).fetchone()
    return {"total": r["t"], "calls": r["n"]}

def usage_by_day(days=14):
    rows = _conn.execute("SELECT date(ts,'unixepoch','localtime') d, SUM(total_tokens) t, COUNT(*) n"
                         " FROM token_usage GROUP BY d ORDER BY d DESC LIMIT ?", (days,)).fetchall()
    return [{"day": r["d"], "total": r["t"], "calls": r["n"]} for r in rows]

def usage_by_model():
    rows = _conn.execute("SELECT model, SUM(total_tokens) t, COUNT(*) n FROM token_usage"
                         " GROUP BY model ORDER BY t DESC").fetchall()
    return [{"model": r["model"], "total": r["t"], "calls": r["n"]} for r in rows]

def usage_recent(n=50):
    rows = _conn.execute("SELECT chat_id,model,prompt_tokens,completion_tokens,total_tokens,ts"
                         " FROM token_usage ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 计数（概览用） ----------
def counts():
    return {
        "messages": _conn.execute("SELECT COUNT(*) n FROM msgs").fetchone()["n"],
        "notes": _conn.execute("SELECT COUNT(*) n FROM notes").fetchone()["n"],
        "chats": _conn.execute("SELECT COUNT(DISTINCT chat_id) n FROM msgs").fetchone()["n"],
    }
