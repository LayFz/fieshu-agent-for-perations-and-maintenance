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

# 内网监控服务：不继承 HTTP(S)_PROXY / 系统代理（否则 TUN/代理会把内网请求带走）
_http = requests.Session()
_http.trust_env = False

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
        r = _http.post(f"{src['base']}/api/{src['org']}/_search", json=body,
                          auth=(src["user"], src["pass"]), timeout=20)
        if r.status_code != 200:
            return {"error": f"[{src['org']}] HTTP {r.status_code}: {r.text[:300]}"}
        hits = (r.json() or {}).get("hits", [])
        return {"org": src["org"], "sql": sql, "period": period, "count": len(hits), "hits": hits[:limit]}
    except Exception as e:
        return {"error": f"[{src['org']}] {e}"}


def list_streams(org=None, stream_type="logs"):
    """列数据流：传 org 只列该组织；不传则列全部已配置组织。stream_type 可为 logs / metrics / traces。"""
    if org:
        src, err = _pick(org)
        if err:
            return err
        return {"org": src["org"], **_streams_of(src, stream_type)}
    return {"orgs": [{"org": s["org"], **_streams_of(s, stream_type)} for s in sources()]}


def _streams_of(src, stream_type="logs"):
    err = _incomplete(src)
    if err:
        return err
    try:
        r = _http.get(f"{src['base']}/api/{src['org']}/streams",
                         params={"type": stream_type}, auth=(src["user"], src["pass"]), timeout=15)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json() or {}
        return {"streams": sorted(s["name"] for s in data.get("list", []) if s.get("name"))}
    except Exception as e:
        return {"error": str(e)}


def _auto_step(secs):
    if secs <= 1800:
        return 60
    if secs <= 7200:
        return 120
    if secs <= 86400:
        return 600
    return 3600


def query_metrics(promql, period="15m", step=None, org=None, limit=50):
    """用 PromQL 查 OpenObserve 指标趋势（/prometheus/api/v1/query_range），返回各时间序列的最新值与统计摘要。"""
    src, err = _pick(org)
    if err:
        return err
    if (err := _incomplete(src)):
        return err
    q = (promql or "").strip()
    if not q:
        return {"error": "promql 不能为空"}
    secs = max(60, min(_seconds(period), 30 * 86400))
    now = int(time.time())
    start = now - secs
    step_s = max(15, int(step)) if step else _auto_step(secs)
    limit = max(1, min(int(limit or 50), 200))
    try:
        r = _http.get(
            f"{src['base']}/api/{src['org']}/prometheus/api/v1/query_range",
            params={"query": q, "start": start, "end": now, "step": step_s},
            auth=(src["user"], src["pass"]),
            timeout=20,
        )
        if r.status_code != 200:
            return {"error": f"[{src['org']}] HTTP {r.status_code}: {r.text[:300]}"}
        body = r.json() or {}
        if body.get("status") != "success":
            return {"error": f"[{src['org']}] PromQL 查询失败: {body.get('error') or str(body)[:200]}"}
        results = (body.get("data") or {}).get("result") or []
        series = []
        for item in results[:limit]:
            vals = item.get("values") or []
            nums = []
            for _, v in vals:
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    pass
            latest = vals[-1] if vals else None
            entry = {"metric": item.get("metric") or {}, "samples": len(vals)}
            if latest is not None:
                entry["latest_ts"] = latest[0]
                entry["latest"] = round(float(latest[1]), 4) if nums else latest[1]
            if nums:
                entry["min"] = round(min(nums), 4)
                entry["max"] = round(max(nums), 4)
                entry["avg"] = round(sum(nums) / len(nums), 4)
            series.append(entry)
        return {
            "org": src["org"],
            "promql": q,
            "period": period,
            "step_sec": step_s,
            "total_series": len(results),
            "returned": len(series),
            "series": series,
        }
    except Exception as e:
        return {"error": f"[{src['org']}] {e}"}


def query_traces(service=None, where=None, period="15m", min_duration_ms=None, limit=30, org=None):
    """查 OpenObserve OTel 链路追踪（type=traces，默认 default 流）。duration 字段单位为微秒。"""
    src, err = _pick(org)
    if err:
        return err
    if (err := _incomplete(src)):
        return err
    limit = max(1, min(int(limit or 30), 200))
    now_us = int(time.time() * 1_000_000)
    start_us = now_us - _seconds(period) * 1_000_000
    clauses = []
    if service:
        svc = str(service).strip().replace("'", "")
        clauses.append(f"service_name='{svc}'")
    if min_duration_ms is not None:
        try:
            min_us = max(0, int(float(min_duration_ms) * 1000))
            clauses.append(f"duration >= {min_us}")
        except (TypeError, ValueError):
            pass
    if where:
        clauses.append(f"({where})")
    sql = 'SELECT * FROM "default"'
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY start_time DESC"
    body = {"query": {"sql": sql, "start_time": start_us, "end_time": now_us, "size": limit}}
    try:
        r = _http.post(
            f"{src['base']}/api/{src['org']}/_search",
            params={"type": "traces"},
            json=body,
            auth=(src["user"], src["pass"]),
            timeout=20,
        )
        if r.status_code != 200:
            return {"error": f"[{src['org']}] HTTP {r.status_code}: {r.text[:300]}"}
        hits = (r.json() or {}).get("hits") or []
        keep = (
            "trace_id", "span_id", "service_name", "operation_name", "span_kind",
            "span_status", "status_message", "http_request_method", "http_route",
            "http_response_status_code", "url_path", "upmore_request_id", "start_time",
        )
        spans = []
        for h in hits[:limit]:
            row = {k: h[k] for k in keep if k in h and h[k] not in (None, "")}
            if "duration" in h and h["duration"] is not None:
                try:
                    row["duration_ms"] = round(float(h["duration"]) / 1000.0, 2)
                except (TypeError, ValueError):
                    row["duration"] = h["duration"]
            spans.append(row)
        return {"org": src["org"], "sql": sql, "period": period, "count": len(spans), "spans": spans}
    except Exception as e:
        return {"error": f"[{src['org']}] {e}"}
