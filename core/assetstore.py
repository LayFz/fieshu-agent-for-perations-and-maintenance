"""资产清单：线上在跑什么（镜像、组件、版本、在哪个集群/命名空间/工作负载）。

每个来源（如 cluster:upmore、gitlab、extra）一份快照：刷新成功时整体替换该来源的资产行，
失败时保留上一次成功的数据并记录错误，报告据此说明「清单是否新鲜、哪些来源没采到」。
"""
import json
import time

from . import store
from .taskstore import transaction

with store._lock:
    store._conn.executescript("""
    CREATE TABLE IF NOT EXISTS asset_sources(
        source TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
        refreshed REAL, ok_at REAL, count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS assets(
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, kind TEXT NOT NULL,
        component TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
        digest TEXT NOT NULL DEFAULT '', cluster TEXT NOT NULL DEFAULT '', namespace TEXT NOT NULL DEFAULT '',
        workload TEXT NOT NULL DEFAULT '', app TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
        first_seen REAL NOT NULL, last_seen REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_assets_component ON assets(component);
    CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source);
    """)
    store._conn.commit()

FIELDS = ("kind", "component", "version", "image", "digest", "cluster", "namespace", "workload", "app", "evidence")


def _key(row):
    return (row.get("kind"), row.get("component"), row.get("image"), row.get("cluster"),
            row.get("namespace"), row.get("workload"))


def replace(source, rows):
    """来源刷新成功：整体替换该来源的资产；同一资产保留首次发现时间。"""
    now = time.time()
    with transaction() as conn:
        first = {}
        for r in conn.execute("SELECT * FROM assets WHERE source=?", (source,)):
            first[_key(dict(r))] = r["first_seen"]
        conn.execute("DELETE FROM assets WHERE source=?", (source,))
        seen = set()
        for row in rows:
            row = {k: str(row.get(k) or "")[:500] for k in FIELDS}
            k = _key(row)
            if k in seen:
                continue
            seen.add(k)
            conn.execute(f"INSERT INTO assets(source,{','.join(FIELDS)},first_seen,last_seen) "
                         f"VALUES(?,{','.join('?' * len(FIELDS))},?,?)",
                         [source] + [row[f] for f in FIELDS] + [first.get(k, now), now])
        conn.execute("INSERT INTO asset_sources(source,status,error,refreshed,ok_at,count) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(source) DO UPDATE SET status=excluded.status, error='', refreshed=excluded.refreshed, "
                     "ok_at=excluded.ok_at, count=excluded.count", (source, "ok", "", now, now, len(seen)))
    return len(seen)


def mark_error(source, error):
    """来源刷新失败：不动已有资产，只记录错误（报告会提示这部分数据可能过期）。"""
    now = time.time()
    with transaction() as conn:
        conn.execute("INSERT INTO asset_sources(source,status,error,refreshed) VALUES(?,?,?,?) "
                     "ON CONFLICT(source) DO UPDATE SET status='error', error=excluded.error, refreshed=excluded.refreshed",
                     (source, "error", str(error)[:300], now))


def sources():
    return [dict(r) for r in store._conn.execute("SELECT * FROM asset_sources ORDER BY source")]


def drop_source(source):
    with transaction() as conn:
        conn.execute("DELETE FROM assets WHERE source=?", (source,))
        conn.execute("DELETE FROM asset_sources WHERE source=?", (source,))


def list_assets(q=None, kind=None, component=None, limit=2000):
    sql, args = "SELECT * FROM assets WHERE 1=1", []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if component:
        sql += " AND component=?"
        args.append(component)
    if q:
        like = f"%{q.lower()}%"
        sql += " AND (lower(component) LIKE ? OR lower(image) LIKE ? OR lower(workload) LIKE ? OR lower(app) LIKE ?" \
               " OR lower(namespace) LIKE ?)"
        args += [like] * 5
    sql += " ORDER BY component, cluster, namespace, workload LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in store._conn.execute(sql, args)]


def extra_assets():
    """后台手工登记的集群外资产（如 .44 上的 Harbor/GitLab），JSON 数组：[{component, version, where}]。"""
    try:
        value = json.loads(store.get_setting("asset_extra", "[]") or "[]")
        return value if isinstance(value, list) else []
    except ValueError:
        return []
