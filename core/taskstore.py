"""持久任务数据层：事务领取、租约 fencing、动作账本与通知 outbox。

复用 store 的每线程 SQLite 连接及进程写锁；事务内不调用其他写接口。
多进程竞争由 BEGIN IMMEDIATE 与条件更新保护，不能仅依赖 Python 锁。
"""
from contextlib import contextmanager
import json
import time
import uuid

from . import store

TERMINAL = frozenset(("completed", "failed", "cancelled"))
WAITING = frozenset(("waiting_approval", "waiting_input", "needs_attention"))


class LeaseLost(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def transaction():
    with store._lock:
        conn = store._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def init():
    with store._lock:
        store._conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, app_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
            instruction TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT,
            speaker TEXT NOT NULL DEFAULT '', speaker_open_id TEXT NOT NULL DEFAULT '',
            workflow TEXT NOT NULL DEFAULT 'auto', trigger TEXT NOT NULL DEFAULT 'chat',
            status TEXT NOT NULL DEFAULT 'queued', checkpoint TEXT NOT NULL DEFAULT '{}',
            result TEXT, error TEXT NOT NULL DEFAULT '',
            lease_owner TEXT, lease_until REAL, lease_epoch INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
            wake_requested INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL, ended REAL,
            UNIQUE(source,app_id,event_id));
        CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status,created);
        CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(app_id,chat_id,status);
        CREATE TABLE IF NOT EXISTS task_actions(
            task_id TEXT NOT NULL, action_key TEXT NOT NULL, tool TEXT NOT NULL,
            args TEXT NOT NULL, risk TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'prepared',
            result TEXT, approval_id TEXT, digest TEXT, payload TEXT NOT NULL DEFAULT '{}',
            created REAL NOT NULL, updated REAL NOT NULL,
            PRIMARY KEY(task_id,action_key));
        CREATE INDEX IF NOT EXISTS idx_action_approval ON task_actions(approval_id);
        CREATE TABLE IF NOT EXISTS task_outbox(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, dedupe_key TEXT UNIQUE NOT NULL,
            text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
            lease_owner TEXT, lease_until REAL, error TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL, sent REAL);
        CREATE INDEX IF NOT EXISTS idx_task_outbox ON task_outbox(status,available_at);
        """)
        store._conn.commit()


def _task(row):
    if row is None:
        return None
    value = dict(row)
    value["checkpoint"] = json.loads(value["checkpoint"] or "{}")
    return value


def _action(row):
    if row is None:
        return None
    value = dict(row)
    value["args"] = json.loads(value["args"])
    value["payload"] = json.loads(value["payload"] or "{}")
    return value


def get(task_id):
    return _task(store._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def list_tasks(app_id=None, chat_id=None, status=None, limit=50):
    clauses, args = [], []
    for name, value in (("app_id", app_id), ("chat_id", chat_id), ("status", status)):
        if value is not None:
            clauses.append(name + "=?")
            args.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    args.append(max(1, min(int(limit), 200)))
    return [_task(r) for r in store._conn.execute(
        "SELECT * FROM tasks" + where + " ORDER BY created DESC LIMIT ?", args).fetchall()]


def submit(app_id, chat_id, instruction, *, source="chat", event_id=None, speaker="",
           speaker_open_id="", workflow="auto", trigger="chat"):
    now, task_id = time.time(), uuid.uuid4().hex
    # None 代表主动新建；禁止空字符串使每次 API 提交意外去重。
    event_id = str(event_id) if event_id not in (None, "") else None
    with transaction() as conn:
        cursor = conn.execute("""INSERT OR IGNORE INTO tasks
            (id,app_id,chat_id,instruction,source,event_id,speaker,speaker_open_id,workflow,trigger,created,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id, app_id, chat_id, instruction, source, event_id,
            speaker, speaker_open_id, workflow, trigger, now, now))
        created = cursor.rowcount == 1
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if created else conn.execute(
            "SELECT * FROM tasks WHERE source=? AND app_id=? AND event_id=?", (source, app_id, event_id)).fetchone()
        # 定时任务直接出结果，不在群里先发「已接收」
        if created and trigger not in ("proactive", "schedule"):
            conn.execute("""INSERT INTO task_outbox(id,task_id,dedupe_key,text,available_at,created)
                VALUES(?,?,?,?,?,?)""", (uuid.uuid4().hex, task_id, f"{task_id}:received",
                f"已接收任务 {task_id[:12]}，正在排队处理。\n可发送“任务 {task_id[:12]}”查看进度。", now, now))
    return _task(row), created


def enqueue_schedule(sc, now=None, workflow="auto"):
    """一次事务完成调度投递及游标推进；过期多轮合并为一次，不补跑风暴。"""
    now = time.time() if now is None else now
    with transaction() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (sc["id"],)).fetchone()
        if (not row or not row["enabled"] or row["next_run_ts"] is None
                or row["next_run_ts"] > now or row["next_run_ts"] != sc["next_run_ts"]):
            return None, False
        app = conn.execute("SELECT enabled FROM apps WHERE id=?", (row["app_id"],)).fetchone()
        if not app or not app["enabled"]:
            return None, False
        task_id = uuid.uuid4().hex
        event_id = f"{row['id']}:{row['next_run_ts']}"
        cur = conn.execute("""INSERT OR IGNORE INTO tasks
            (id,app_id,chat_id,instruction,source,event_id,workflow,trigger,created,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (task_id, row["app_id"], row["chat_id"], row["instruction"],
            "schedule", event_id, workflow, "schedule", now, now))
        conn.execute("UPDATE schedules SET last_run_ts=?,next_run_ts=? WHERE id=?",
                     (now, store.next_run(row["kind"], row["spec"], now), row["id"]))
        task = conn.execute("SELECT * FROM tasks WHERE source='schedule' AND app_id=? AND event_id=?",
                            (row["app_id"], event_id)).fetchone()
        return _task(task), cur.rowcount == 1


def _owned(conn, task_id, owner, epoch, *, now=None, cancellation=True):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    now = time.time() if now is None else now
    if (not row or row["lease_owner"] != owner or row["lease_epoch"] != epoch
            or row["status"] not in ("running", "cancelling")
            or not row["lease_until"] or row["lease_until"] <= now):
        raise LeaseLost("任务租约已过期或被其他执行器接管")
    if cancellation and row["cancel_requested"]:
        raise LeaseLost("任务已请求取消")
    return row


def assert_owned(task_id, owner, epoch):
    with transaction() as conn:
        return _task(_owned(conn, task_id, owner, epoch))


# 确定性任务可直接产出整张飞书卡片：结果以此前缀 + 卡片 JSON 保存，投递时原样发送
CARD_PREFIX = "\x1ecard:"


def _notify(conn, row, status, result=None, error=""):
    if status not in TERMINAL | {"needs_attention"}:
        return
    label = {"completed": "已完成", "failed": "执行失败", "cancelled": "已取消",
             "needs_attention": "需要核对"}[status]
    parts = [str(result)] if result else []
    if error and str(error) not in parts:
        parts.append(str(error))
    body = "\n\n".join(parts)
    if status == "completed" and result is None and row["trigger"] == "proactive":
        return
    if status == "completed" and isinstance(result, str) and result.startswith(CARD_PREFIX):
        text = result
    else:
        text = f"任务 {row['id'][:12]} · {label}\n{body or ''}".strip()
    now = time.time()
    conn.execute("""INSERT OR IGNORE INTO task_outbox
        (id,task_id,dedupe_key,text,available_at,created) VALUES(?,?,?,?,?,?)""",
        (uuid.uuid4().hex, row["id"], f"{row['id']}:{row['lease_epoch']}:{status}", text, now, now))


def _unsafe(conn, task_id):
    return conn.execute("SELECT 1 FROM task_actions WHERE task_id=? AND risk!='read' "
                        "AND state IN ('running','uncertain') LIMIT 1", (task_id,)).fetchone() is not None


def has_uncertain(task_id):
    return _unsafe(store._conn, task_id)


def recover_expired(now=None, max_attempts=8):
    now = time.time() if now is None else now
    recovered = 0
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE status IN ('running','cancelling') "
                            "AND lease_until<=?", (now,)).fetchall()
        for row in rows:
            unsafe = _unsafe(conn, row["id"])
            exhausted = row["attempts"] >= max(1, int(max_attempts))
            status = "needs_attention" if unsafe else ("cancelled" if row["cancel_requested"] else
                                                       "needs_attention" if exhausted else "queued")
            error = ("执行中断，外部动作结果不确定；请核对后处理，不会自动重试。" if unsafe else
                     "执行器重复中断，已达到自动恢复次数上限，请检查服务后显式继续。" if exhausted else
                     "执行器中断，已从检查点重新排队。")
            if unsafe:
                conn.execute("UPDATE task_actions SET state='uncertain',updated=? WHERE task_id=? "
                             "AND risk!='read' AND state='running'", (now, row["id"]))
            conn.execute("UPDATE tasks SET status=?,error=?,lease_owner=NULL,lease_until=NULL,updated=? "
                         "WHERE id=?", (status, error, now, row["id"]))
            _notify(conn, row, status, error=error)
            recovered += 1
    return recovered


def wake_decided_approvals():
    """修复审批回调先于 worker 保存 waiting 状态的竞争，也处理重启后的已决定审批。"""
    with transaction() as conn:
        rows = conn.execute("""SELECT DISTINCT t.id FROM tasks t
            JOIN task_actions a ON a.task_id=t.id JOIN approvals p ON p.id=a.approval_id
            WHERE t.status='waiting_approval' AND a.state='waiting_approval'
              AND p.status!='pending' AND t.cancel_requested=0""").fetchall()
        for row in rows:
            conn.execute("UPDATE tasks SET status='queued',updated=?,wake_requested=0 WHERE id=?",
                         (time.time(), row["id"]))
    return len(rows)


def claim(owner, lease_seconds=90, now=None):
    now = time.time() if now is None else now
    with transaction() as conn:
        row = conn.execute("""SELECT t.* FROM tasks t JOIN apps a ON a.id=t.app_id
            WHERE t.status='queued' AND t.cancel_requested=0 AND a.enabled=1
              AND NOT EXISTS(SELECT 1 FROM tasks busy WHERE busy.app_id=t.app_id
                  AND busy.chat_id=t.chat_id AND busy.status IN ('running','cancelling'))
            ORDER BY t.created,t.id LIMIT 1""").fetchone()
        if not row:
            return None
        conn.execute("UPDATE tasks SET status='running',lease_owner=?,lease_until=?,lease_epoch=lease_epoch+1,"
                     "attempts=attempts+1,updated=?,error='' WHERE id=? AND status='queued'",
                     (owner, now + lease_seconds, now, row["id"]))
        return _task(conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())


def heartbeat(task_id, owner, epoch, lease_seconds=90):
    now = time.time()
    with transaction() as conn:
        cursor = conn.execute("UPDATE tasks SET lease_until=?,updated=? WHERE id=? AND lease_owner=? "
                              "AND lease_epoch=? AND status IN ('running','cancelling') AND lease_until>?",
                              (now + lease_seconds, now, task_id, owner, epoch, now))
        return cursor.rowcount == 1


def checkpoint(task_id, owner, epoch, data):
    encoded = _json(data)
    with transaction() as conn:
        _owned(conn, task_id, owner, epoch)
        conn.execute("UPDATE tasks SET checkpoint=?,updated=? WHERE id=?", (encoded, time.time(), task_id))


def finish(task_id, owner, epoch, status, result=None, error=""):
    if status not in TERMINAL | WAITING | {"queued"}:
        raise ValueError("不支持的任务终态")
    now = time.time()
    with transaction() as conn:
        row = _owned(conn, task_id, owner, epoch, cancellation=False)
        if row["cancel_requested"]:
            status = "needs_attention" if _unsafe(conn, task_id) else "cancelled"
            error = "取消时仍有结果未确认的动作，需要核对。" if status == "needs_attention" else "用户已取消任务。"
            result = None
        elif status == "waiting_approval" and row["wake_requested"]:
            status = "queued"
        conn.execute("UPDATE tasks SET status=?,result=?,error=?,lease_owner=NULL,lease_until=NULL,"
                     "wake_requested=0,updated=?,ended=? WHERE id=?",
                     (status, result, error, now, now if status in TERMINAL else None, task_id))
        _notify(conn, row, status, result=result, error=error)
        return status


def _scope(row, app_id=None, chat_id=None):
    return row is not None and (app_id is None or row["app_id"] == app_id) and (
        chat_id is None or row["chat_id"] == chat_id)


def cancel(task_id, app_id=None, chat_id=None):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not _scope(row, app_id, chat_id) or row["status"] in TERMINAL:
            return False
        if row["status"] == "needs_attention" and _unsafe(conn, task_id):
            return False
        status = "cancelling" if row["status"] in ("running", "cancelling") else "cancelled"
        conn.execute("UPDATE tasks SET cancel_requested=1,status=?,updated=?,ended=? WHERE id=?",
                     (status, time.time(), time.time() if status == "cancelled" else None, task_id))
        conn.execute("UPDATE approvals SET status='cancelled',decided=? WHERE status='pending' "
                     "AND id IN (SELECT approval_id FROM task_actions WHERE task_id=?)", (time.time(), task_id))
        _notify(conn, row, status, error="用户已取消任务。")
    return True


def resume(task_id, app_id=None, chat_id=None, instruction=None):
    if instruction is not None and (not isinstance(instruction, str) or len(instruction) > 100000):
        raise ValueError("补充内容必须为不超过 100000 字的文本")
    with transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (not _scope(row, app_id, chat_id) or row["status"] not in ("failed", "waiting_input", "needs_attention")
                or _unsafe(conn, task_id)):
            return False
        app = conn.execute("SELECT enabled FROM apps WHERE id=?", (row["app_id"],)).fetchone()
        if not app or not app["enabled"]:
            return False
        saved = json.loads(row["checkpoint"] or "{}")
        if instruction and saved.get("pending"):
            # 保持已准备/审批的工具调用和它的上下文稳定，不能插入用户消息改变既有动作。
            return False
        task_instruction = row["instruction"]
        if instruction and instruction.strip():
            if saved.get("messages"):
                saved["messages"].append({"role": "user", "content": instruction.strip()})
            else:
                task_instruction += "\n\n用户补充：" + instruction.strip()
        saved.pop("done", None)
        saved.pop("final_answer", None)
        conn.execute("UPDATE tasks SET status='queued',cancel_requested=0,error='',ended=NULL,checkpoint=?,"
                     "instruction=?,updated=? WHERE id=?", (_json(saved), task_instruction, time.time(), task_id))
    return True


def wake(task_id):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["cancel_requested"]:
            return False
        if row["status"] == "waiting_approval":
            conn.execute("UPDATE tasks SET status='queued',updated=? WHERE id=?", (time.time(), task_id))
        elif row["status"] == "running":
            conn.execute("UPDATE tasks SET wake_requested=1 WHERE id=?", (task_id,))
        else:
            return False
    return True


def action_get(task_id, key):
    return _action(store._conn.execute("SELECT * FROM task_actions WHERE task_id=? AND action_key=?",
                                      (task_id, key)).fetchone())


def action_by_approval(approval_id):
    return _action(store._conn.execute("SELECT * FROM task_actions WHERE approval_id=?", (approval_id,)).fetchone())


def action_prepare(task_id, key, tool, args, risk, *, lease_owner=None, lease_epoch=None, **fields):
    encoded, now = _json(args), time.time()
    with transaction() as conn:
        _owned(conn, task_id, lease_owner, lease_epoch)
        row = conn.execute("SELECT * FROM task_actions WHERE task_id=? AND action_key=?", (task_id, key)).fetchone()
        if row:
            if row["tool"] != tool or json.loads(row["args"]) != args or row["risk"] != risk:
                raise ValueError("同一动作键不能改变工具、参数或风险等级")
            return _action(row)
        conn.execute("""INSERT INTO task_actions
            (task_id,action_key,tool,args,risk,digest,payload,created,updated) VALUES(?,?,?,?,?,?,?,?,?)""",
            (task_id, key, tool, encoded, risk, fields.get("digest"), _json(fields.get("payload", {})), now, now))
        return _action(conn.execute("SELECT * FROM task_actions WHERE task_id=? AND action_key=?", (task_id, key)).fetchone())


def action_update(task_id, key, *, lease_owner=None, lease_epoch=None, **fields):
    allowed = {"state", "result", "approval_id", "digest", "payload"}
    if set(fields) - allowed:
        raise ValueError("不支持的动作字段")
    sets, args = [], []
    for name, value in fields.items():
        sets.append(name + "=?")
        args.append(_json(value) if name == "payload" else value)
    if not sets:
        return action_get(task_id, key)
    with transaction() as conn:
        _owned(conn, task_id, lease_owner, lease_epoch)
        conn.execute("UPDATE task_actions SET " + ",".join(sets) + ",updated=? WHERE task_id=? AND action_key=?",
                     args + [time.time(), task_id, key])
        return _action(conn.execute("SELECT * FROM task_actions WHERE task_id=? AND action_key=?", (task_id, key)).fetchone())


def claim_notification(owner, lease_seconds=60):
    now = time.time()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_outbox WHERE (status='pending' AND available_at<=?) OR "
                           "(status='sending' AND lease_until<=?) ORDER BY created LIMIT 1", (now, now)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE task_outbox SET status='sending',lease_owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                     (owner, now + lease_seconds, row["id"]))
        return dict(conn.execute("SELECT * FROM task_outbox WHERE id=?", (row["id"],)).fetchone())


def finish_notification(notification_id, owner, error=None):
    now = time.time()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_outbox WHERE id=? AND lease_owner=? AND status='sending'",
                           (notification_id, owner)).fetchone()
        if not row:
            return False
        if error:
            conn.execute("UPDATE task_outbox SET status='pending',error=?,available_at=?,lease_owner=NULL,lease_until=NULL "
                         "WHERE id=?", (str(error)[:1000], now + min(300, 2 ** min(row["attempts"], 8)), notification_id))
        else:
            conn.execute("UPDATE task_outbox SET status='sent',sent=?,lease_owner=NULL,lease_until=NULL,error='' WHERE id=?",
                         (now, notification_id))
    return True


init()
