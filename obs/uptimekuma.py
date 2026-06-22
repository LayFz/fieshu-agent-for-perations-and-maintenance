"""Uptime Kuma 客户端（只读）：用 API Key 读 /metrics(Prometheus) 并解析成每个监控的状态。

连接信息存全局设置(kv)：uk_url / uk_api_key。
认证：HTTP Basic，空用户名、API Key 作密码（curl -u :KEY）。
解析 monitor_status(1=在线/0=离线)、monitor_response_time、monitor_cert_days_remaining。
"""
import re

import requests

from core import store

_LINE = re.compile(r'^(\w+)\{([^}]*)\}\s+([0-9.eE+-]+)')
_NAME = re.compile(r'monitor_name="([^"]*)"')


def _cfg():
    return ((store.get_setting("uk_url", "") or "").rstrip("/"),
            store.get_setting("uk_api_key", "") or "")

def configured():
    url, key = _cfg()
    return bool(url and key)


def status():
    """返回所有监控的在线状态 / 响应时间 / 证书剩余天数。"""
    url, key = _cfg()
    if not (url and key):
        return {"error": "Uptime Kuma 未配置：请在后台「可观测」里填 URL 和 API Key。"}
    try:
        r = requests.get(f"{url}/metrics", auth=("", key), timeout=10)
        if r.status_code != 200:
            return {"error": f"读取 /metrics 失败 HTTP {r.status_code}（确认 API Key 正确）"}
        mon = {}
        for line in r.text.splitlines():
            m = _LINE.match(line)
            if not m:
                continue
            metric, labels, val = m.group(1), m.group(2), m.group(3)
            nm = _NAME.search(labels)
            if not nm:
                continue
            name = nm.group(1)
            d = mon.setdefault(name, {"name": name})
            try:
                num = float(val)
            except ValueError:
                continue
            if metric == "monitor_status":
                d["up"] = (num == 1)
            elif metric == "monitor_response_time":
                d["response_ms"] = num
            elif metric == "monitor_cert_days_remaining":
                d["cert_days"] = num
        monitors = list(mon.values())
        down = [m["name"] for m in monitors if m.get("up") is False]
        return {"count": len(monitors), "down": down, "monitors": monitors}
    except Exception as e:
        return {"error": str(e)}
