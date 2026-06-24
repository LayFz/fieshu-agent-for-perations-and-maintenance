"""Beszel 客户端（只读）：PocketBase 用户登录 → 读 systems / container_stats 集合。

连接信息存全局设置(kv)：bz_url / bz_user / bz_pass。
systems.info 直接带实时快照：cpu(CPU%)、mp(内存%)、dp(磁盘%)、la(负载1/5/15)、u(运行秒)、v(版本)。
容器级数据在 container_stats：每台机每分钟一条记录，stats 是当时的容器数组
  [{n:容器名, c:CPU%, m:内存MB, b:[网络发,收]}]，取每台机最新一条即当前快照。
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


def containers(server=None, name=None, sort="cpu", top=None):
    """列出各服务器上正在运行的 Docker 容器及其 CPU/内存/网络占用。

    server: 服务器名关键字(模糊)，只看某台机；
    name:   容器名关键字(模糊)，找「某项目部署在哪台机/占用多少」；
    sort:   cpu | mem | name 排序(默认 cpu 从高到低)；
    top:    只返回前 N 个(找占用最多时用)。
    """
    url, u, p = _cfg()
    if not (url and u and p):
        return {"error": "Beszel 未配置：请在后台「可观测」里填 URL / 账号 / 密码。"}
    try:
        tok = _token(url, u, p)
        H = {"Authorization": tok}
        sys = requests.get(f"{url}/api/collections/systems/records?perPage=100",
                           headers=H, timeout=10)
        if sys.status_code != 200:
            return {"error": f"读取服务器列表失败 HTTP {sys.status_code}"}
        rows = []
        for s in sys.json().get("items", []):
            snm = s.get("name", "")
            if server and server.lower() not in snm.lower():
                continue
            # 取这台机最新一条容器快照（每分钟一条，最新即当前）
            r = requests.get(
                f"{url}/api/collections/container_stats/records",
                headers=H, timeout=10,
                params={"perPage": 1, "sort": "-created",
                        "filter": f'system="{s["id"]}" && type="1m"'},
            )
            items = r.json().get("items", []) if r.status_code == 200 else []
            if not items:
                continue
            rec = items[0]
            for c in rec.get("stats") or []:
                cn = c.get("n", "")
                if name and name.lower() not in cn.lower():
                    continue
                b = c.get("b") or [None, None]
                rows.append({
                    "server": snm, "name": cn,
                    "cpu_pct": round(c.get("c", 0) or 0, 2),
                    "mem_mb": round(c.get("m", 0) or 0, 1),
                    "net_sent": b[0], "net_recv": b[1],
                    "as_of": rec.get("created"),
                })
        if server and not rows:
            return {"error": f"没有匹配「{server}」的服务器，或它没有容器数据", "count": 0}
        if name and not rows:
            return {"error": f"在可见服务器上没找到名字含「{name}」的容器", "count": 0}
        key = {"cpu": lambda x: -(x["cpu_pct"] or 0),
               "mem": lambda x: -(x["mem_mb"] or 0),
               "name": lambda x: (x["server"], x["name"])}.get(sort, None)
        if key:
            rows.sort(key=key)
        total = len(rows)
        if top:
            rows = rows[:int(top)]
        return {"count": total, "returned": len(rows), "containers": rows}
    except Exception as e:
        return {"error": str(e)}
