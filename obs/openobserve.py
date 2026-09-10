"""OpenObserve 客户端：用其 _search REST API 查日志。

OpenObserve 用「组织(org)」做数据隔离：每个 org 一份数据、一个 service account，
互相看不见。所以连接信息按 org 存多份凭据：

  oo_base_url             OO 地址（各 org 共用）
  oo_org/oo_user/oo_pass  主组织（默认 default）的 org / 账号 / 密码(token)
  oo_sources              其余组织，JSON 数组：[{"name","org","user","pass"}]

所有应用共用同一份配置；要不要让某个应用能查，由该应用是否勾选 query_logs 工具决定。
"""
import json
import time

import requests

from core import store

_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def sources():
    """全部已配置的组织数据源；第一个(主源)对应 oo_org/oo_user/oo_pass。"""
    base = (store.get_setting("oo_base_url", "") or "").rstrip("/")
    main_org = (store.get_setting("oo_org", "default") or "default").strip()
    out = [{
        "name": main_org,
        "org": main_org,
        "user": (store.get_setting("oo_user", "") or "").strip(),
        "pass": store.get_setting("oo_pass", "") or "",
        "base": base,
    }]
    try:
        extra = json.loads(store.get_setting("oo_sources", "") or "[]")
    except Exception:
        extra = []
    for s in extra if isinstance(extra, list) else []:
        if not isinstance(s, dict):
            continue
        org = (s.get("org") or "").strip()
        if not org or org.lower() == main_org.lower():
            continue
        out.append({
            "name": (s.get("name") or org).strip(),
            "org": org,
            "user": (s.get("user") or "").strip(),
            "pass": s.get("pass") or "",
            "base": (s.get("base_url") or base).rstrip("/"),
        })
    return out


def _pick(org=None):
    """选数据源：不传 org 用主源；传了按 org 名（或别名）匹配。"""
    srcs = sources()
    if not org:
        return srcs[0], None
    key = str(org).strip().lower()
    for s in srcs:
        if key in (s["org"].lower(), s["name"].lower()):
            return s, None
    return None, {"error": f"未配置组织「{org}」。已配置：{', '.join(s['org'] for s in srcs)}"}


def _incomplete(src):
    if not (src["base"] and src["user"] and src["pass"]):
        return {"error": f"组织「{src['org']}」未配置完整：需要 base_url / 账号 / 密码。"}
    return None


def configured():
    return any(s["base"] and s["user"] and s["pass"] for s in sources())


def _seconds(period):
    period = (period or "15m").strip().lower()
    try:
        return int(period[:-1]) * _UNIT.get(period[-1], 60)
    except Exception:
        return 900


def search(stream, where=None, period="15m", limit=50, org=None):
    """查某 org 下 stream 最近 period 内的日志。where 是可选 SQL 条件（不含 WHERE 关键字）。"""
    src, err = _pick(org)
    if err:
        return err
    if (err := _incomplete(src)):
        return err
    limit = max(1, min(int(limit or 50), 500))
    now_us = int(time.time() * 1_000_000)
    start_us = now_us - _seconds(period) * 1_000_000
    sql = f'SELECT * FROM "{stream}"'
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY _timestamp DESC"
    body = {"query": {"sql": sql, "start_time": start_us, "end_time": now_us, "size": limit}}
    try:
        r = requests.post(f"{src['base']}/api/{src['org']}/_search", json=body,
                          auth=(src["user"], src["pass"]), timeout=20)
        if r.status_code != 200:
            return {"error": f"[{src['org']}] HTTP {r.status_code}: {r.text[:300]}"}
        hits = (r.json() or {}).get("hits", [])
        return {"org": src["org"], "sql": sql, "period": period, "count": len(hits), "hits": hits[:limit]}
    except Exception as e:
        return {"error": f"[{src['org']}] {e}"}


def list_streams(org=None):
    """列日志流：传 org 只列该组织；不传则列全部已配置组织。"""
    if org:
        src, err = _pick(org)
        if err:
            return err
        return {"org": src["org"], **_streams_of(src)}
    return {"orgs": [{"org": s["org"], **_streams_of(s)} for s in sources()]}


def _streams_of(src):
    err = _incomplete(src)
    if err:
        return err
    try:
        r = requests.get(f"{src['base']}/api/{src['org']}/streams",
                         params={"type": "logs"}, auth=(src["user"], src["pass"]), timeout=15)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json() or {}
        return {"streams": sorted(s["name"] for s in data.get("list", []) if s.get("name"))}
    except Exception as e:
        return {"error": str(e)}
