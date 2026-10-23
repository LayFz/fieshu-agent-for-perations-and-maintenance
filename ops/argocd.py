"""ArgoCD 只读集成：列应用、看单个应用（同步/健康状态、实际运行的镜像、资源）。

为什么需要它：gitops 仓库里大量是浮动 tag（rabbitmq:3-management-alpine、redis:7-alpine…），
只看声明拿不到真实版本；ArgoCD 的 status.summary.images 是集群里实际在跑的镜像。

连接存 kv：argocd_url / argocd_token（建议给只读账号生成：argocd account generate-token --account readonly）
/ argocd_insecure（内网自签证书时开）。只发 GET，不做任何同步/删除。
"""
import requests

from core import store

_TIMEOUT = 20


def config():
    return {"url": (store.get_setting("argocd_url") or "").rstrip("/"),
            "token": store.get_setting("argocd_token") or "",
            "insecure": (store.get_setting("argocd_insecure") or "0") == "1"}


def _get(path, params=None):
    c = config()
    if not (c["url"] and c["token"]):
        return None, {"error": "ArgoCD 还没配置（后台「连接凭证」页填地址和只读 token）"}
    try:
        r = requests.get(c["url"] + path, params=params, headers={"Authorization": f"Bearer {c['token']}"},
                         timeout=_TIMEOUT, verify=not c["insecure"])
    except requests.RequestException as e:
        return None, {"error": f"连不上 ArgoCD：{type(e).__name__}: {str(e)[:200]}"}
    if r.status_code == 401 or r.status_code == 403:
        return None, {"error": f"ArgoCD 拒绝访问（{r.status_code}），token 可能失效或无权限"}
    if r.status_code >= 300:
        return None, {"error": f"ArgoCD 返回 {r.status_code}：{r.text[:200]}"}
    return r.json(), None


def _brief(a):
    spec, st, md = a.get("spec") or {}, a.get("status") or {}, a.get("metadata") or {}
    srcs = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    dest = spec.get("destination") or {}
    return {
        "name": md.get("name"), "project": spec.get("project"),
        "cluster": dest.get("name") or dest.get("server"), "namespace": dest.get("namespace"),
        "sources": [{"repo": s.get("repoURL"), "path": s.get("path") or s.get("chart"),
                     "revision": s.get("targetRevision")} for s in srcs],
        "sync": (st.get("sync") or {}).get("status"), "health": (st.get("health") or {}).get("status"),
        "revision": ((st.get("sync") or {}).get("revision") or "")[:12],
        "images": (st.get("summary") or {}).get("images") or [],
    }


def list_apps(name=None, project=None):
    data, err = _get("/api/v1/applications", {"projects": project} if project else None)
    if err:
        return err
    apps = [_brief(a) for a in (data.get("items") or [])]
    if name:
        apps = [a for a in apps if name.lower() in (a["name"] or "").lower()]
    images = sorted({i for a in apps for i in a["images"]})
    return {"count": len(apps), "apps": apps, "distinct_images": len(images)}


def get_app(name):
    data, err = _get(f"/api/v1/applications/{name}")
    if err:
        return err
    out = _brief(data)
    st = data.get("status") or {}
    out["conditions"] = [{"type": c.get("type"), "message": (c.get("message") or "")[:300]}
                         for c in st.get("conditions") or []]
    op = st.get("operationState") or {}
    out["last_operation"] = {"phase": op.get("phase"), "message": (op.get("message") or "")[:300],
                             "finished": op.get("finishedAt")}
    out["resources"] = [{"kind": r.get("kind"), "name": r.get("name"), "namespace": r.get("namespace"),
                         "sync": r.get("status"), "health": (r.get("health") or {}).get("status")}
                        for r in (st.get("resources") or [])][:200]
    return out


def test():
    data, err = _get("/api/version")
    if err:
        return {"ok": False, **err}
    apps = list_apps()
    return {"ok": "error" not in apps, "version": (data or {}).get("Version"),
            "apps": apps.get("count"), "error": apps.get("error")}
