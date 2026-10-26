"""OpenObserve 定量巡检：仅取聚合计数，不下载或持久化日志正文。"""
from datetime import datetime, timezone
import re
import time
from urllib.parse import urlsplit

import requests

from obs import openobserve


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_ERROR_PREDICATE = "(" + " OR ".join(
    "lower(message) LIKE '%" + word + "%'" for word in ("error", "exception", "fatal", "panic")) + ")"


class LogScanError(Exception):
    """只含应用自己生成的安全诊断，不包含 HTTP 正文或日志内容。"""


def _count(org, stream, seconds):
    if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (org, stream)):
        raise LogScanError("日志组织或流名称无效")
    matches = [src for src in openobserve.sources() if src["org"] == org]
    if len(matches) != 1 or not all(matches[0].get(key) for key in ("base", "user", "pass")):
        raise LogScanError("日志组织未配置完整的只读连接")
    src = matches[0]
    url = urlsplit(src["base"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise LogScanError("OpenObserve 地址配置无效")
    end = int(time.time() * 1_000_000)
    request = {"query": {"sql": f'SELECT count(*) AS matched FROM "{stream}" WHERE {_ERROR_PREDICATE}',
                         "start_time": end - seconds * 1_000_000, "end_time": end, "size": 1}}
    try:
        response = requests.post(f"{src['base']}/api/{org}/_search", json=request,
                                 auth=(src["user"], src["pass"]), timeout=20, allow_redirects=False)
        if response.status_code != 200:
            raise LogScanError(f"日志 API 返回 HTTP {response.status_code}")
        hits = response.json().get("hits")
        if not isinstance(hits, list) or len(hits) != 1 or not isinstance(hits[0], dict):
            raise LogScanError("日志聚合响应格式无效")
        matched = hits[0].get("matched")
        if type(matched) is not int or matched < 0:
            raise LogScanError("日志聚合计数无效")
        return matched
    except LogScanError:
        raise
    except Exception:
        raise LogScanError("日志 API 查询失败或响应格式无效") from None


def scan(org, stream, seconds=1800):
    """返回单一来源；关键词只是待核对线索，不宣称已确定故障根因。"""
    source = f"logs:{org}:{stream}"
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "sources": [],
              "findings": [], "summary": {"org": org, "stream": stream, "window_seconds": seconds}}
    try:
        count = _count(org, stream, seconds)
        result["sources"].append({"name": source, "status": "ok"})
        result["summary"]["matched"] = count
        if count:
            result["findings"].append({"key": source + ":error-keywords", "source": source,
                "severity": "medium", "title": "日志错误关键词命中",
                "detail": "最近窗口内出现 error/exception/fatal/panic 关键词；仅为日志线索，需结合服务状态和原始日志核对。",
                "resource": org + "/" + stream,
                "evidence": {"matched": count, "window_seconds": seconds}})
    except LogScanError as exc:
        result["sources"].append({"name": source, "status": "error", "error": str(exc)})
    except Exception:
        result["sources"].append({"name": source, "status": "error", "error": "日志巡检未完成"})
    return result
