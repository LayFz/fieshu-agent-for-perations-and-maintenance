"""记忆：SQLite 存每个会话的对话历史 + 长期笔记。"""
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "memory.db"
_lock = threading.Lock()
_conn = sqlite3.connect(DB, check_same_thread=False)
_conn.execute("CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT, ts REAL)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_msgs_chat ON msgs(chat_id, id)")
_conn.execute("CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, scope TEXT, content TEXT, ts REAL)")
_conn.commit()


def save(chat_id, role, content):
    with _lock:
        _conn.execute("INSERT INTO msgs(chat_id, role, content, ts) VALUES(?,?,?,?)",
                      (chat_id, role, content, time.time()))
        _conn.commit()


def load_history(chat_id, turns):
    """取最近 turns 轮（user+assistant）历史，按时间正序返回。"""
    rows = _conn.execute(
        "SELECT role, content FROM msgs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, turns * 2),
    ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def add_note(scope, content):
    with _lock:
        _conn.execute("INSERT INTO notes(scope, content, ts) VALUES(?,?,?)",
                      (scope, content, time.time()))
        _conn.commit()


def load_notes(scope="global", limit=30):
    rows = _conn.execute(
        "SELECT content FROM notes WHERE scope=? ORDER BY id DESC LIMIT ?",
        (scope, limit),
    ).fetchall()
    return [c for (c,) in rows]
