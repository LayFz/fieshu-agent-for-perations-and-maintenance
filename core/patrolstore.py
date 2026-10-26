"""持续巡检存储：租约领取、问题差异与通知在同一事务提交。"""
from contextlib import contextmanager
import hashlib
import json
import re
import time
import uuid

from . import store


class LeaseLost(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        CREATE TABLE IF NOT EXISTS patrol_monitors(
          id TEXT PRIMARY KEY,app_id INTEGER NOT NULL,name TEXT NOT NULL,cluster TEXT,
          git_projects TEXT NOT NULL,namespaces TEXT NOT NULL,log_targets TEXT NOT NULL DEFAULT '[]',interval_seconds INTEGER NOT NULL,
          chat_id TEXT,enabled INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
          baseline_pending INTEGER NOT NULL DEFAULT 0,
          next_run REAL NOT NULL,requested INTEGER NOT NULL DEFAULT 0,
          lease_owner TEXT,lease_until REAL,lease_epoch INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL,updated REAL NOT NULL,last_scan_id TEXT,last_error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_patrol_due ON patrol_monitors(enabled,next_run);
        CREATE TABLE IF NOT EXISTS patrol_scans(
          id TEXT PRIMARY KEY,monitor_id TEXT NOT NULL,monitor_revision INTEGER NOT NULL,
          lease_epoch INTEGER NOT NULL,observed_at TEXT NOT NULL,snapshot TEXT NOT NULL,created REAL NOT NULL,
          UNIQUE(monitor_id,lease_epoch));
        CREATE TABLE IF NOT EXISTS patrol_issues(
          monitor_id TEXT NOT NULL,key TEXT NOT NULL,source TEXT NOT NULL,severity TEXT NOT NULL,
          title TEXT NOT NULL,detail TEXT NOT NULL,resource TEXT NOT NULL,evidence TEXT NOT NULL,
          fingerprint TEXT NOT NULL,active INTEGER NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
          resolved_at TEXT,generation INTEGER NOT NULL DEFAULT 1,last_scan_id TEXT NOT NULL,
          PRIMARY KEY(monitor_id,key));
        CREATE TABLE IF NOT EXISTS patrol_outbox(
          id TEXT PRIMARY KEY,monitor_id TEXT NOT NULL,scan_id TEXT NOT NULL UNIQUE,
          monitor_revision INTEGER NOT NULL,app_scope_key TEXT NOT NULL,app_id INTEGER NOT NULL,
          chat_id TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL,lease_owner TEXT,lease_until REAL,
          error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,sent REAL);
        CREATE INDEX IF NOT EXISTS idx_patrol_outbox ON patrol_outbox(status,available_at);
        """)
        # 兼容已运行过巡检的本地数据库。
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(patrol_monitors)")}
        if "baseline_pending" not in columns:
            store._conn.execute("ALTER TABLE patrol_monitors ADD COLUMN baseline_pending INTEGER NOT NULL DEFAULT 0")
        if "log_targets" not in columns:
            store._conn.execute("ALTER TABLE patrol_monitors ADD COLUMN log_targets TEXT NOT NULL DEFAULT '[]'")
        store._conn.commit()


def _monitor(row):
    if row is None:
        return None
    out = dict(row)
    for field in ("git_projects", "namespaces", "log_targets"):
        out[field] = json.loads(out[field])
    out["enabled"] = bool(out["enabled"])
    out["baseline_pending"] = bool(out["baseline_pending"])
    return out


def get(monitor_id):
    return _monitor(store._conn.execute("SELECT * FROM patrol_monitors WHERE id=?", (monitor_id,)).fetchone())


def list_monitors(app_id=None):
    rows = store._conn.execute("SELECT * FROM patrol_monitors" + (" WHERE app_id=?" if app_id is not None else "") +
                               " ORDER BY created,id", (app_id,) if app_id is not None else ()).fetchall()
    return [_monitor(row) for row in rows]


def create(fields):
    now, mid = time.time(), uuid.uuid4().hex
    with transaction() as conn:
        conn.execute("""INSERT INTO patrol_monitors
          (id,app_id,name,cluster,git_projects,namespaces,log_targets,interval_seconds,chat_id,enabled,baseline_pending,next_run,created,updated)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (mid, fields["app_id"], fields["name"], fields["cluster"],
          encode(fields["git_projects"]), encode(fields["namespaces"]), encode(fields["log_targets"]), fields["interval_seconds"],
          fields["chat_id"], int(fields["enabled"]), int(bool(fields["chat_id"])), now, now, now))
    return get(mid)


def update(monitor_id, fields, expected_revision=None):
    allowed = {"app_id", "name", "cluster", "git_projects", "namespaces", "log_targets", "interval_seconds", "chat_id", "enabled"}
    if not fields or set(fields) - allowed:
        raise ValueError("未知或空巡检配置")
    with transaction() as conn:
        old = conn.execute("SELECT revision,chat_id FROM patrol_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not old:
            return None
        if expected_revision is not None and old["revision"] != expected_revision:
            raise ValueError("巡检配置已被其他操作修改，请重试")
        values = [encode(v) if k in ("git_projects", "namespaces", "log_targets") else int(v) if k == "enabled" else v
                  for k, v in fields.items()]
        now = time.time()
        newly_bound = fields.get("chat_id") and fields["chat_id"] != old["chat_id"]
        conn.execute("UPDATE patrol_monitors SET " + ",".join(k + "=?" for k in fields) +
                     ",revision=revision+1,baseline_pending=CASE WHEN ? THEN 1 ELSE baseline_pending END,"
                     "next_run=?,updated=?,requested=1 WHERE id=?",
                     [*values, int(bool(newly_bound)), now, now, monitor_id])
        # 配置/目的地改变后，旧快照通知不得发到旧群。
        conn.execute("UPDATE patrol_outbox SET status='suppressed',error='巡检配置已变化' "
                     "WHERE monitor_id=? AND status IN ('pending','sending')", (monitor_id,))
    return get(monitor_id)


def request_scan(monitor_id):
    with transaction() as conn:
        cur = conn.execute("UPDATE patrol_monitors SET next_run=?,requested=1,updated=? WHERE id=? AND enabled=1",
                           (time.time(), time.time(), monitor_id))
        return cur.rowcount == 1


def claim(owner, lease_seconds=120, now=None):
    now = time.time() if now is None else now
    with transaction() as conn:
        row = conn.execute("""SELECT m.* FROM patrol_monitors m JOIN apps a ON a.id=m.app_id
          WHERE m.enabled=1 AND a.enabled=1 AND m.next_run<=?
            AND (m.lease_owner IS NULL OR m.lease_until<=?) ORDER BY m.next_run,m.created LIMIT 1""", (now, now)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE patrol_monitors SET lease_owner=?,lease_until=?,lease_epoch=lease_epoch+1,"
                     "requested=0,updated=? WHERE id=?", (owner, now + lease_seconds, now, row["id"]))
        return _monitor(conn.execute("SELECT * FROM patrol_monitors WHERE id=?", (row["id"],)).fetchone())


def _owned(conn, monitor):
    row = conn.execute("SELECT * FROM patrol_monitors WHERE id=?", (monitor["id"],)).fetchone()
    if (not row or row["lease_owner"] != monitor["lease_owner"] or row["lease_epoch"] != monitor["lease_epoch"]
            or not row["lease_until"] or row["lease_until"] <= time.time()):
        raise LeaseLost("巡检租约已失效")
    return row


def heartbeat(monitor, lease_seconds=120):
    now = time.time()
    with transaction() as conn:
        cur = conn.execute("UPDATE patrol_monitors SET lease_until=? WHERE id=? AND lease_owner=? AND lease_epoch=? "
                           "AND lease_until>?", (now + lease_seconds, monitor["id"], monitor["lease_owner"], monitor["lease_epoch"], now))
        return cur.rowcount == 1


def abandon(monitor, reason="巡检期间授权或配置变化，旧快照已丢弃"):
    with transaction() as conn:
        row = _owned(conn, monitor)
        # 未改变配置但应用已撤权/停用时延后重试；新配置则立即重新扫描。
        next_run = time.time() if row["revision"] != monitor["revision"] or row["requested"] else time.time() + row["interval_seconds"]
        conn.execute("UPDATE patrol_monitors SET lease_owner=NULL,lease_until=NULL,next_run=?,last_error=?,updated=? WHERE id=?",
                     (next_run, reason, time.time(), monitor["id"]))


def app_scope_key(app):
    return hashlib.sha256(encode({"id": app.get("id"), "enabled": app.get("enabled"),
                                 "resources": app.get("resources") or {}}).encode()).hexdigest()


def _fingerprint(finding):
    # 证据中的时间、计数和链接可每次变化；问题语义由标题、严重度、解释和资源定义。
    return hashlib.sha256(encode({k: finding.get(k) for k in ("source", "severity", "title", "detail", "resource")}).encode()).hexdigest()


def issue_ref(monitor_id, key):
    return hashlib.sha256(f"{monitor_id}:{key}".encode()).hexdigest()[:12]


_POD_SUFFIX = re.compile(r"-(?:[a-z0-9]{8,10}-[a-z0-9]{5}|[0-9]{8,}-[a-z0-9]{5}|[a-z0-9]{5})$")
_EVIDENCE_KEYS = ("waiting_reasons", "reasons", "reason", "phase", "exit_code", "restarts", "count", "last_operation_phase")


def _loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _workload(resource):
    """Pod/命名空间/名称 → 命名空间/工作负载名（去掉 ReplicaSet 哈希与 Pod 随机后缀），换代前后的 Pod 归为同一个。"""
    parts = str(resource or "").split("/")
    if len(parts) >= 3 and parts[0] == "Pod":
        return f"{parts[1]}/{_POD_SUFFIX.sub('', parts[2])}"
    return None


def _describe(item):
    """通知里的一行对象 + 关键证据：看消息就知道是哪个 Pod/应用、因为什么。"""
    resource = _loads(item.get("resource"))
    evidence = _loads(item.get("evidence"))
    line = f"对象：{resource}" if resource else ""
    if isinstance(evidence, dict):
        bits = []
        for k in _EVIDENCE_KEYS:
            v = evidence.get(k)
            if v in (None, "", []):
                continue
            bits.append(f"{k}={'/'.join(map(str, v)) if isinstance(v, list) else v}")
        if bits:
            line += ("；" if line else "") + "，".join(bits)[:200]
    return line


def _notification(monitor, changes):
    labels = {"new": "新增", "changed": "变化", "resolved": "恢复", "rolled": "换代"}
    # 同一工作负载换代：旧 Pod「恢复」+ 新 Pod「新增」同一问题，合并成一条，说明问题仍在
    merged, used = [], set()
    for i, c in enumerate(changes):
        if i in used or c["change"] != "new" or not _workload(_loads(c.get("resource"))):
            continue
        for j, o in enumerate(changes):
            if (j not in used and o["change"] == "resolved" and o["title"] == c["title"]
                    and _workload(_loads(o.get("resource"))) == _workload(_loads(c.get("resource")))):
                used.update((i, j))
                merged.append(dict(c, change="rolled", previous=_loads(o.get("resource"))))
                break
    changes = merged + [c for i, c in enumerate(changes) if i not in used]
    counts = {kind: sum(c["change"] == kind for c in changes) for kind in labels}
    text = f"巡检 · {monitor['name']}\n" + "，".join(f"{labels[k]} {v}" for k, v in counts.items() if v)
    for change in changes[:30]:
        text += (f"\n\n#{issue_ref(monitor['id'], change['key'])} "
                 f"[{labels[change['change']]}][{change['severity']}] {change['title'][:250]}")
        described = _describe(change)
        if described:
            text += "\n" + described
        if change["change"] == "rolled":
            text += f"\n问题仍在：Pod 已换代（原 {change['previous']}），新 Pod 同样异常"
        if change.get("detail"):
            text += "\n" + change["detail"][:500]
    if len(changes) > 30:
        text += f"\n\n另有 {len(changes)-30} 项变化，请查看巡检报告。"
    return text[:24000]


def _baseline_notification(monitor, snapshot, issues):
    statuses = "，".join(f"{source['name']}={source['status']}" for source in snapshot["sources"]
                        if not source["name"].startswith("__"))
    text = f"巡检 · {monitor['name']}\n已绑定本会话，首次真实扫描完成。当前活动问题 {len(issues)} 项。"
    if statuses:
        text += "\n采集：" + statuses[:3000]
    for issue in issues[:30]:
        text += f"\n\n#{issue_ref(monitor['id'], issue['key'])} [{issue['severity']}] {issue['title'][:250]}"
        described = _describe(dict(issue))
        if described:
            text += "\n" + described
        if issue["detail"]:
            text += "\n" + issue["detail"][:500]
    if len(issues) > 30:
        text += f"\n\n另有 {len(issues) - 30} 项，请到后台查看问题台账。"
    return text[:24000]


def save_scan(monitor, snapshot, scope_key):
    now, sid, observed = time.time(), uuid.uuid4().hex, snapshot["observed_at"]
    with transaction() as conn:
        row = _owned(conn, monitor)
        app_row = conn.execute("SELECT * FROM apps WHERE id=?", (row["app_id"],)).fetchone()
        # 在与快照提交同一写事务内再次验证权限版本，堵住模型/采集线程保存旧范围。
        if not app_row:
            raise LeaseLost("巡检应用已删除")
        app = dict(app_row)
        app["resources"] = json.loads(app.get("resources") or "{}")
        app["enabled"] = bool(app["enabled"])
        if row["revision"] != monitor["revision"] or not row["enabled"] or not app["enabled"] or app_scope_key(app) != scope_key:
            raise LeaseLost("巡检期间授权或配置已变化")
        conn.execute("INSERT INTO patrol_scans(id,monitor_id,monitor_revision,lease_epoch,observed_at,snapshot,created) VALUES(?,?,?,?,?,?,?)",
                     (sid, row["id"], row["revision"], row["lease_epoch"], observed, encode(snapshot), now))
        successful = {s["name"] for s in snapshot["sources"] if s["status"] == "ok"}
        incoming, changes = set(), []
        for finding in snapshot["findings"]:
            key, fingerprint = finding["key"], _fingerprint(finding)
            incoming.add(key)
            old = conn.execute("SELECT * FROM patrol_issues WHERE monitor_id=? AND key=?", (row["id"], key)).fetchone()
            change = "new" if not old or not old["active"] else "changed" if old["fingerprint"] != fingerprint else None
            if change == "changed" and key.startswith("__source__:") and old["severity"] == finding["severity"]:
                change = None
            generation = (old["generation"] + (0 if old["active"] else 1)) if old else 1
            conn.execute("""INSERT INTO patrol_issues
              (monitor_id,key,source,severity,title,detail,resource,evidence,fingerprint,active,first_seen,last_seen,resolved_at,generation,last_scan_id)
              VALUES(?,?,?,?,?,?,?,?,?,1,?,?,NULL,?,?) ON CONFLICT(monitor_id,key) DO UPDATE SET
              source=excluded.source,severity=excluded.severity,title=excluded.title,detail=excluded.detail,
              resource=excluded.resource,evidence=excluded.evidence,fingerprint=excluded.fingerprint,active=1,
              last_seen=excluded.last_seen,resolved_at=NULL,generation=excluded.generation,last_scan_id=excluded.last_scan_id""",
              (row["id"], key, finding["source"], finding["severity"], finding["title"], finding.get("detail", ""),
               encode(finding.get("resource")), encode(finding.get("evidence")), fingerprint,
               old["first_seen"] if old else observed, observed, generation, sid))
            if change:
                changes.append(dict(finding, change=change))
        for old in conn.execute("SELECT * FROM patrol_issues WHERE monitor_id=? AND active=1", (row["id"],)).fetchall():
            if old["key"] not in incoming and old["source"] in successful:
                conn.execute("UPDATE patrol_issues SET active=0,resolved_at=?,last_scan_id=? WHERE monitor_id=? AND key=?",
                             (observed, sid, row["id"], old["key"]))
                changes.append(dict(old, change="resolved"))
        errors = [s["name"] + ":" + s["status"] for s in snapshot["sources"] if s["status"] != "ok"]
        conn.execute("UPDATE patrol_monitors SET lease_owner=NULL,lease_until=NULL,baseline_pending=0,next_run=?,updated=?,last_scan_id=?,last_error=? WHERE id=?",
                     (now if row["requested"] else now + row["interval_seconds"], now, sid, "; ".join(errors), row["id"]))
        if row["chat_id"] and (changes or row["baseline_pending"]):
            active = conn.execute("SELECT key,severity,title,detail,resource,evidence FROM patrol_issues WHERE monitor_id=? AND active=1 "
                                  "ORDER BY last_seen DESC,key", (row["id"],)).fetchall()
            message = (_baseline_notification(row, snapshot, active) if row["baseline_pending"]
                       else _notification(row, changes))
            conn.execute("""INSERT INTO patrol_outbox
              (id,monitor_id,scan_id,monitor_revision,app_scope_key,app_id,chat_id,text,available_at,created)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"], sid, row["revision"], scope_key,
              row["app_id"], row["chat_id"], message, now, now))
    return get_scan(sid)


def _scan(row):
    if row is None:
        return None
    out = dict(row)
    out["snapshot"] = json.loads(out["snapshot"])
    return out


def get_scan(scan_id):
    return _scan(store._conn.execute("SELECT * FROM patrol_scans WHERE id=?", (scan_id,)).fetchone())


def list_scans(monitor_id=None, limit=50):
    args = [monitor_id] if monitor_id is not None else []
    args.append(max(1, min(int(limit), 200)))
    return [_scan(row) for row in store._conn.execute("SELECT * FROM patrol_scans" +
        (" WHERE monitor_id=?" if monitor_id is not None else "") + " ORDER BY created DESC,id DESC LIMIT ?", args).fetchall()]


def list_issues(monitor_id=None, active=None, limit=200):
    clauses, args = [], []
    if monitor_id is not None:
        clauses.append("monitor_id=?"); args.append(monitor_id)
    if active is not None:
        clauses.append("active=?"); args.append(int(active))
    args.append(max(1, min(int(limit), 2000)))
    rows = store._conn.execute("SELECT * FROM patrol_issues" + (" WHERE " + " AND ".join(clauses) if clauses else "") +
                              " ORDER BY active DESC,last_seen DESC,key LIMIT ?", args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item.update(resource=json.loads(item["resource"]), evidence=json.loads(item["evidence"]), active=bool(item["active"]))
        out.append(item)
    return out


def claim_notification(owner, lease_seconds=60):
    now = time.time()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM patrol_outbox WHERE (status='pending' AND available_at<=?) OR "
                           "(status='sending' AND lease_until<=?) ORDER BY created,id LIMIT 1", (now, now)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE patrol_outbox SET status='sending',lease_owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                     (owner, now + lease_seconds, row["id"]))
        return dict(conn.execute("SELECT * FROM patrol_outbox WHERE id=?", (row["id"],)).fetchone())


def finish_notification(item, owner, error=None, suppressed=False):
    now = time.time()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM patrol_outbox WHERE id=? AND lease_owner=? AND status='sending'", (item["id"], owner)).fetchone()
        if not row:
            return False
        status = "suppressed" if suppressed else "pending" if error else "sent"
        conn.execute("UPDATE patrol_outbox SET status=?,error=?,available_at=?,lease_owner=NULL,lease_until=NULL,sent=? WHERE id=?",
            (status, str(error or "")[:1000], now + min(300, 2 ** min(row["attempts"], 8)) if error else now,
             now if status == "sent" else None, row["id"]))
    return True


def notification_counts():
    counts = {state: 0 for state in ("pending", "sending", "sent", "suppressed")}
    counts.update({r["status"]: r["n"] for r in store._conn.execute("SELECT status,COUNT(*) n FROM patrol_outbox GROUP BY status").fetchall()})
    return counts


init()
