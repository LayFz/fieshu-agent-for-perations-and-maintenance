"""Beszel 客户端（只读）：PocketBase 用户登录 → 读 systems 集合的实时快照。

连接信息存全局设置(kv)：bz_url / bz_user / bz_pass。
systems.info 直接带实时快照：cpu(CPU%)、mp(内存%)、dp(磁盘%)、la(负载1/5/15)、u(运行秒)、v(版本)。
注意：Beszel 普通用户只能看到被分享给它的服务器。
"""
import requests

from core import store


def _cfg():
    return (
        (store.get_setting("bz_url", "") or "").rstrip("/"),
        store.get_setting("bz_user", "") or "",
        store.get_setting("bz_pass", "") or "",
    )

def configured():
    url, u, p = _cfg()
    return bool(url and u and p)

def _token(url, u, p):
    r = requests.post(f"{url}/api/collections/users/auth-with-password",
                      json={"identity": u, "password": p}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Beszel 登录失败 HTTP {r.status_code}: {r.text[:160]}")
    return r.json()["token"]


def servers(name=None):
    """列出可见服务器及其实时 CPU/内存/磁盘/负载。name 给了就模糊筛选。"""
    url, u, p = _cfg()
    if not (url and u and p):
        return {"error": "Beszel 未配置：请在后台「可观测」里填 URL / 账号 / 密码。"}
    try:
        tok = _token(url, u, p)
        r = requests.get(f"{url}/api/collections/systems/records?perPage=100",
                         headers={"Authorization": tok}, timeout=10)
        if r.status_code != 200:
            return {"error": f"读取服务器列表失败 HTTP {r.status_code}"}
        out = []
        for s in r.json().get("items", []):
            nm = s.get("name", "")
            if name and name.lower() not in nm.lower():
                continue
            info = s.get("info") or {}
            out.append({
                "name": nm, "status": s.get("status"), "host": s.get("host"),
                "cpu_pct": info.get("cpu"), "mem_pct": info.get("mp"), "disk_pct": info.get("dp"),
                "load_avg": info.get("la"), "uptime_sec": info.get("u"), "agent_ver": info.get("v"),
            })
        if name and not out:
            return {"error": f"没有匹配「{name}」的服务器（也可能未分享给当前账号）", "count": 0}
        return {"count": len(out), "servers": out}
    except Exception as e:
        return {"error": str(e)}
