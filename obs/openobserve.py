"""OpenObserve 客户端：用其 _search REST API 查日志。

连接信息存全局设置(kv)：oo_base_url / oo_org / oo_user / oo_pass。
所有应用共用同一个 OpenObserve；要不要让某个应用能查，由该应用是否勾选 query_logs 工具决定。
"""
import time

import requests

from core import store

_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def creds():
    return (
        (store.get_setting("oo_base_url", "") or "").rstrip("/"),
        store.get_setting("oo_org", "default") or "default",
        store.get_setting("oo_user", "") or "",
        store.get_setting("oo_pass", "") or "",
    )

def configured():
    base, _org, user, pw = creds()
    return bool(base and user and pw)

def _seconds(period):
    period = (period or "15m").strip().lower()
    try:
        return int(period[:-1]) * _UNIT.get(period[-1], 60)
    except Exception:
        return 900


def search(stream, where=None, period="15m", limit=50):
    """查某个 stream 最近 period 内的日志。where 是可选 SQL 条件（不含 WHERE 关键字）。"""
    base, org, user, pw = creds()
    if not (base and user and pw):
        return {"error": "OpenObserve 未配置：请在后台「全局设置」里填 base_url / 组织 / 账号 / 密码。"}
    limit = max(1, min(int(limit or 50), 500))
    now_us = int(time.time() * 1_000_000)
    start_us = now_us - _seconds(period) * 1_000_000
    sql = f'SELECT * FROM "{stream}"'
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY _timestamp DESC"
    body = {"query": {"sql": sql, "start_time": start_us, "end_time": now_us, "size": limit}}
    try:
        r = requests.post(f"{base}/api/{org}/_search", json=body, auth=(user, pw), timeout=20)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:300]}"}
        hits = (r.json() or {}).get("hits", [])
        return {"sql": sql, "period": period, "count": len(hits), "hits": hits[:limit]}
    except Exception as e:
        return {"error": str(e)}


def list_streams():
    base, org, user, pw = creds()
    if not (base and user and pw):
        return {"error": "OpenObserve 未配置。"}
    try:
        r = requests.get(f"{base}/api/{org}/streams", auth=(user, pw), timeout=15)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json() or {}
        return {"streams": [s.get("name") for s in data.get("list", [])]}
    except Exception as e:
        return {"error": str(e)}
