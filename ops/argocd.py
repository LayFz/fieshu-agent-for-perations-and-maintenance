"""ArgoCD 只读集成：应用状态、比较/部署版本、镜像引用与资源。

为什么需要它：gitops 仓库里大量是浮动 tag（rabbitmq:3-management-alpine、redis:7-alpine…），
status.summary.images 只是资源引用的镜像字符串，不能据此确认运行时 imageID/digest。

连接存 kv：argocd_url / argocd_token（建议给只读账号生成：argocd account generate-token --account readonly）
/ argocd_insecure（内网自签证书时开）。只发 GET，不做任何同步/删除。
"""
from urllib.parse import quote, urlsplit, urlunsplit

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
                         timeout=_TIMEOUT, verify=not c["insecure"], allow_redirects=False)
    except requests.RequestException as e:
        # 异常/响应正文可能回显 URL、请求头或 token，不进入模型与审计。
        return None, {"error": f"连不上 ArgoCD：{type(e).__name__}"}
    if r.status_code == 401 or r.status_code == 403:
        return None, {"error": f"ArgoCD 拒绝访问（{r.status_code}），token 可能失效或无权限"}
    if r.status_code >= 300:
        return None, {"error": f"ArgoCD 返回 HTTP {r.status_code}"}
    try:
        return r.json(), None
    except ValueError:
        return None, {"error": "ArgoCD 返回的内容不是有效 JSON"}


def _safe_url(value):
    """只输出定位信息，移除 URL 内嵌凭据和查询参数。"""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        return value.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]
    except ValueError:
        return None


def _sources(obj):
    srcs = obj.get("sources") or ([obj["source"]] if obj.get("source") else [])
    return [{"repo": _safe_url(s.get("repoURL")), "path": s.get("path") or s.get("chart"),
             "revision": s.get("targetRevision"), "ref": s.get("ref"), "chart": s.get("chart")}
            for s in srcs]


def _brief(a):
    spec, st, md = a.get("spec") or {}, a.get("status") or {}, a.get("metadata") or {}
    dest = spec.get("destination") or {}
    sync = st.get("sync") or {}
    compared = sync.get("comparedTo") or {}
    compared_dest = compared.get("destination") or {}
    return {
        "name": md.get("name"), "project": spec.get("project"),
        "cluster": dest.get("name") or _safe_url(dest.get("server")), "namespace": dest.get("namespace"),
        "sources": _sources(spec),
        "sync": sync.get("status"), "health": (st.get("health") or {}).get("status"),
        # 比较版本并不等于最近成功部署版本；两者保留完整 revision 便于精确关联。
        "revision": sync.get("revision") or "", "revisions": sync.get("revisions") or [],
        "revision_source": "status.sync",
        "compared_to": {"sources": _sources(compared), "destination": {
            "name": compared_dest.get("name"), "server": _safe_url(compared_dest.get("server")),
            "namespace": compared_dest.get("namespace")}},
        "images": (st.get("summary") or {}).get("images") or [],
        "image_evidence": {"source": "status.summary.images", "kind": "references",
                           "runtime_digest_verified": False},
    }


def list_apps(name=None, project=None):
    data, err = _get("/api/v1/applications", {"projects": project} if project else None)
    if err:
        return err
    if not isinstance(data, dict):
        return {"error": "ArgoCD 应用列表格式不正确"}
    apps = [_brief(a) for a in (data.get("items") or [])]
    if name:
        apps = [a for a in apps if name.lower() in (a["name"] or "").lower()]
    images = sorted({i for a in apps for i in a["images"]})
    return {"count": len(apps), "apps": apps, "distinct_images": len(images)}


def get_app(name):
    if not isinstance(name, str) or not name.strip():
        return {"error": "缺少 ArgoCD 应用名称"}
    data, err = _get(f"/api/v1/applications/{quote(name, safe='')}")
    if err:
        return err
    if not isinstance(data, dict):
        return {"error": "ArgoCD 应用详情格式不正确"}
    out = _brief(data)
    st = data.get("status") or {}
    out["conditions"] = [{"type": c.get("type"), "message": (c.get("message") or "")[:300]}
                         for c in st.get("conditions") or []]
    op = st.get("operationState") or {}
    result = op.get("syncResult") or {}
    requested = (op.get("operation") or {}).get("sync") or {}
    out["last_operation"] = {"phase": op.get("phase"), "message": (op.get("message") or "")[:300],
                             "started": op.get("startedAt"), "finished": op.get("finishedAt"),
                             "revision": result.get("revision"), "revisions": result.get("revisions") or [],
                             "sources": _sources(result), "requested_revision": requested.get("revision"),
                             "requested_revisions": requested.get("revisions") or []}
    history = st.get("history") or []
    # ArgoCD history.id 单调增加；不依赖返回数组的排序。
    latest = max(history, key=lambda h: int(h.get("id", -1))) if history else None
    out["deployment_history_count"] = len(history)
    out["latest_deployment"] = ({"id": latest.get("id"), "revision": latest.get("revision"),
        "revisions": latest.get("revisions") or [], "deployed_at": latest.get("deployedAt"),
        "deploy_started_at": latest.get("deployStartedAt"), "sources": _sources(latest)} if latest else None)
    out["resources"] = [{"kind": r.get("kind"), "name": r.get("name"), "namespace": r.get("namespace"),
                         "sync": r.get("status"), "health": (r.get("health") or {}).get("status")}
                        for r in (st.get("resources") or [])][:200]
    out["resources_complete"] = len(st.get("resources") or []) <= 200
    return out


def test():
    data, err = _get("/api/version")
    if err:
        return {"ok": False, **err}
    apps = list_apps()
    return {"ok": "error" not in apps, "version": (data or {}).get("Version"),
            "apps": apps.get("count"), "error": apps.get("error")}
