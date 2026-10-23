"""工作机（box）操作队列与 runner 登记。

工作机上的 runner 主动来拉操作（不需要入站端口），执行后回传结果；harness 侧的工具调用阻塞等待结果。
runner 令牌只存 SHA-256。操作本身只在工作机内产生影响（工作机没有公司凭据、不能进内网），
因此结果丢失时按失败处理即可，不需要「结果不明」的人工核对。
"""
import hashlib
import hmac
import json
import secrets
import time
import uuid

from . import store
from .taskstore import transaction

ONLINE_SECONDS = 60

with store._lock:
    store._conn.executescript("""
    CREATE TABLE IF NOT EXISTS box_runners(
        name TEXT PRIMARY KEY, token_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        last_seen REAL, info TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS box_ops(
        id TEXT PRIMARY KEY, runner TEXT NOT NULL, op TEXT NOT NULL, args TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', result TEXT, task_id TEXT,
        created REAL NOT NULL, claimed REAL, finished REAL, deadline REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_box_ops_queue ON box_ops(runner, status, created);
    """)
    store._conn.commit()


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- runner ----------
def create_runner(name):
    token = "box_" + secrets.token_urlsafe(32)
    with transaction() as conn:
        conn.execute("INSERT INTO box_runners(name,token_hash,created) VALUES(?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET token_hash=excluded.token_hash, enabled=1",
                     (name, _hash(token), time.time()))
    return token


def delete_runner(name):
    with transaction() as conn:
        conn.execute("DELETE FROM box_runners WHERE name=?", (name,))
        conn.execute("UPDATE box_ops SET status='failed', result=?, finished=? WHERE runner=? AND status IN "
                     "('queued','claimed')", (json.dumps({"error": "工作机已被移除"}), time.time(), name))


def authenticate(token):
    """返回 runner 名称；令牌不匹配或已停用返回 None。顺带记录心跳。"""
    if not token:
        return None
    digest = _hash(token)
    rows = store._conn.execute("SELECT name, token_hash, enabled FROM box_runners").fetchall()
    for r in rows:
        if r["enabled"] and hmac.compare_digest(r["token_hash"], digest):
            return r["name"]
    return None


def touch(name, info=None):
    with transaction() as conn:
        if info is None:
            conn.execute("UPDATE box_runners SET last_seen=? WHERE name=?", (time.time(), name))
        else:
            conn.execute("UPDATE box_runners SET last_seen=?, info=? WHERE name=?",
                         (time.time(), json.dumps(info, ensure_ascii=False)[:4000], name))


def list_runners():
    now = time.time()
    out = []
    for r in store._conn.execute("SELECT name, enabled, last_seen, info, created FROM box_runners ORDER BY name"):
        d = dict(r)
        d["info"] = json.loads(d["info"] or "{}")
        d["online"] = bool(d["enabled"] and d["last_seen"] and now - d["last_seen"] < ONLINE_SECONDS)
        out.append(d)
    return out


def get_runner(name):
    return next((r for r in list_runners() if r["name"] == name), None)


# ---------- 操作 ----------
def enqueue(runner, op, args, timeout, task_id=None):
    op_id = uuid.uuid4().hex
    now = time.time()
    with transaction() as conn:
        conn.execute("INSERT INTO box_ops(id,runner,op,args,task_id,created,deadline) VALUES(?,?,?,?,?,?,?)",
                     (op_id, runner, op, json.dumps(args, ensure_ascii=False), task_id, now, now + timeout))
    return op_id


def claim(runner):
    now = time.time()
    with transaction() as conn:
        conn.execute("UPDATE box_ops SET status='expired', finished=? WHERE runner=? AND status='queued' AND deadline<?",
                     (now, runner, now))
        row = conn.execute("SELECT * FROM box_ops WHERE runner=? AND status='queued' ORDER BY created LIMIT 1",
                           (runner,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE box_ops SET status='claimed', claimed=? WHERE id=? AND status='queued'", (now, row["id"]))
    return {"id": row["id"], "op": row["op"], "args": json.loads(row["args"]),
            "timeout": max(1, int(row["deadline"] - now))}


def complete(op_id, runner, ok, result):
    """只接受领取它的 runner、且仍处于 claimed 的结果；迟到或伪造的回传被丢弃。"""
    with transaction() as conn:
        cur = conn.execute("UPDATE box_ops SET status=?, result=?, finished=? WHERE id=? AND runner=? AND status='claimed'",
                           ("done" if ok else "failed", json.dumps(result, ensure_ascii=False), time.time(), op_id, runner))
        return cur.rowcount == 1


def get(op_id):
    row = store._conn.execute("SELECT * FROM box_ops WHERE id=?", (op_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def abandon(op_id, reason):
    with transaction() as conn:
        conn.execute("UPDATE box_ops SET status='expired', result=?, finished=? WHERE id=? AND status IN ('queued','claimed')",
                     (json.dumps({"error": reason}, ensure_ascii=False), time.time(), op_id))


def prune(max_age=7 * 86400):
    with transaction() as conn:
        conn.execute("DELETE FROM box_ops WHERE created<?", (time.time() - max_age,))
