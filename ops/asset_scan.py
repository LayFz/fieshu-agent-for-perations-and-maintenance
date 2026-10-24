"""资产清单采集：线上在跑的镜像与组件版本。只读，复用 cluster_scan 的受限 API 客户端（固定只读端点、无代理、TLS 校验）。

来源与可信度：
  cluster:<名>   Pod 实际运行的镜像（含 imageID 摘要）、节点的 Kubernetes/k3s、containerd、内核版本 —— 运行时事实
  cluster:<名>   本集群 ArgoCD 管理的「部署到其他集群」的应用（如 VKE）：status.summary.images —— 资源引用，未核对运行时摘要
  gitlab         GitLab 版本（有令牌时）
  extra          后台手工登记的集群外资产（kv asset_extra）
"""
import json
import re
import tempfile
import time

from core import assetstore, components, store
from . import cluster_scan, k8s

_DEFAULT_OWN = ["harbor.upmore.net", "10.10.0.1", "192.168.110.44"]
REFRESH_HOURS = 6
RETRY_MINUTES = 15          # 有来源失败时（如 VPN 断开）更快重试
_running = False


def own_registries():
    try:
        value = json.loads(store.get_setting("asset_own_registries") or "null")
        return value if isinstance(value, list) and value else _DEFAULT_OWN
    except ValueError:
        return _DEFAULT_OWN


def _d(v):
    return v if isinstance(v, dict) else {}


def _l(v):
    return v if isinstance(v, list) else []


def _workload(pod):
    md = _d(pod.get("metadata"))
    owners = _l(md.get("ownerReferences"))
    if owners:
        o = _d(owners[0])
        name = o.get("name") or ""
        if o.get("kind") == "ReplicaSet":
            name = re.sub(r"-[a-z0-9]{6,10}$", "", name)       # Deployment 的 ReplicaSet 后缀
        return f"{'Deployment' if o.get('kind') == 'ReplicaSet' else o.get('kind')}/{name}"
    return f"Pod/{md.get('name')}"


def _app_of(pod):
    md = _d(pod.get("metadata"))
    labels, ann = _d(md.get("labels")), _d(md.get("annotations"))
    tracking = ann.get("argocd.argoproj.io/tracking-id") or ""
    return tracking.split(":", 1)[0] if tracking else labels.get("app.kubernetes.io/instance") or ""


def pods_to_assets(items, cluster, own):
    rows = []
    for pod in items:
        st = _d(pod.get("status"))
        if st.get("phase") not in ("Running", "Pending"):
            continue
        md, spec = _d(pod.get("metadata")), _d(pod.get("spec"))
        digests = {}
        for cs in _l(st.get("containerStatuses")) + _l(st.get("initContainerStatuses")):
            cs = _d(cs)
            image_id = cs.get("imageID") or ""
            digests[cs.get("name")] = image_id.rsplit("@", 1)[-1] if "@" in image_id else ""
        for init, c in [(False, x) for x in _l(spec.get("containers"))] + [(True, x) for x in _l(spec.get("initContainers"))]:
            c = _d(c)
            image = c.get("image") or ""
            if not image:
                continue
            comp, version, mine = components.component_of(image, own)
            role = f"init 容器 {c.get('name')}（启动时短暂运行）" if init else f"容器 {c.get('name')}"
            rows.append({"kind": "service" if mine else "image", "component": comp, "version": version,
                         "image": image, "digest": digests.get(c.get("name"), ""), "cluster": cluster,
                         "namespace": md.get("namespace") or "", "workload": f"{_workload(pod)} · {role}",
                         "app": _app_of(pod), "evidence": "pod 运行时镜像（含摘要）"})
    return rows


def nodes_to_assets(items, cluster):
    rows = []
    for node in items:
        info = _d(_d(node.get("status")).get("nodeInfo"))
        name = _d(node.get("metadata")).get("name") or ""
        kubelet = info.get("kubeletVersion") or ""
        base = {"cluster": cluster, "workload": f"Node/{name}", "evidence": "node.status.nodeInfo"}
        if kubelet:
            rows.append({**base, "kind": "component", "component": "kubernetes",
                         "version": components.version_of(kubelet.split("+")[0])})
            if "+k3s" in kubelet:
                rows.append({**base, "kind": "component", "component": "k3s", "version": kubelet.lstrip("v")})
        runtime = info.get("containerRuntimeVersion") or ""
        if runtime.startswith("containerd://"):
            rows.append({**base, "kind": "component", "component": "containerd",
                         "version": components.version_of(runtime.split("://", 1)[1])})
        if info.get("kernelVersion"):
            rows.append({**base, "kind": "component", "component": "linux-kernel",
                         "version": components.version_of(info["kernelVersion"])})
    return rows


def _cluster_label(target):
    """ArgoCD 目标集群：优先后台配置的友好名（kv asset_cluster_names：{server 或名称: 显示名}），否则用 API 主机名。"""
    try:
        names = json.loads(store.get_setting("asset_cluster_names") or "{}")
    except ValueError:
        names = {}
    if isinstance(names, dict) and names.get(target):
        return str(names[target])
    m = re.match(r"https?://([^/:]+)", target)
    return m.group(1) if m else target


def applications_to_assets(items, cluster, own):
    """只取部署到「其他集群」的应用镜像：本集群的应用已经由 Pod 运行时事实覆盖。"""
    rows = []
    for app in items:
        md, spec, st = _d(app.get("metadata")), _d(app.get("spec")), _d(app.get("status"))
        dest = _d(spec.get("destination"))
        target = dest.get("name") or dest.get("server") or ""
        if target in ("in-cluster", "https://kubernetes.default.svc", ""):
            continue
        target = _cluster_label(target)
        for image in _l(_d(st.get("summary")).get("images")):
            comp, version, mine = components.component_of(image, own)
            rows.append({"kind": "service" if mine else "image", "component": comp, "version": version,
                         "image": image, "cluster": target, "namespace": dest.get("namespace") or "",
                         "workload": "", "app": md.get("name") or "",
                         "evidence": f"ArgoCD {md.get('name')} status.summary.images（资源引用，未核对运行时摘要）"})
    return rows


def collect_cluster(name):
    source = f"cluster:{name}"
    try:
        creds = cluster_scan._credentials(name)
        with tempfile.TemporaryDirectory(prefix="harness-asset-scan-") as directory:
            client = cluster_scan._Client(creds, directory)
            try:
                own = own_registries()
                rows = pods_to_assets(client.objects("/api/v1/pods"), name, own)
                rows += nodes_to_assets(client.objects("/api/v1/nodes"), name)
                try:
                    apps = client.objects("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications")
                    rows += applications_to_assets(apps, name, own)
                except cluster_scan.ScanError:
                    pass       # 该集群没有 ArgoCD 或无权读取：不影响运行时清单
            finally:
                client.close()
    except cluster_scan.ScanError as e:
        assetstore.mark_error(source, e)
        return {"source": source, "status": "error", "error": str(e)}
    except Exception as e:
        assetstore.mark_error(source, f"采集失败：{type(e).__name__}")
        return {"source": source, "status": "error", "error": f"采集失败：{type(e).__name__}"}
    count = assetstore.replace(source, rows)
    return {"source": source, "status": "ok", "assets": count}


def collect_gitlab():
    from . import gitlab
    if not gitlab.config().get("url"):
        return None
    # 只配了项目级令牌时，用任一项目令牌读版本（/version 对任何有效令牌可见）
    project = None if gitlab.config().get("token") else next(iter(gitlab.project_tokens() or {}), None)
    try:
        data, err = gitlab._get("/version", project=project)
        err = (err or {}).get("error")
    except Exception as e:
        data, err = None, type(e).__name__
    if err or not isinstance(data, dict) or not data.get("version"):
        assetstore.mark_error("gitlab", err or "未取到 GitLab 版本（令牌可能无权限）")
        return {"source": "gitlab", "status": "error", "error": str(err or "未取到版本")}
    version = components.version_of(data["version"])
    count = assetstore.replace("gitlab", [{"kind": "component", "component": "gitlab", "version": version,
                                           "evidence": "GitLab /api/v4/version", "workload": "GitLab"}])
    return {"source": "gitlab", "status": "ok", "assets": count}


def collect_extra():
    rows = [{"kind": "component", "component": str(x.get("component") or "").lower(), "version": str(x.get("version") or ""),
             "workload": str(x.get("where") or ""), "evidence": "后台手工登记"}
            for x in assetstore.extra_assets() if isinstance(x, dict) and x.get("component")]
    count = assetstore.replace("extra", rows)
    return {"source": "extra", "status": "ok", "assets": count}


def refresh():
    """采集全部来源；单个来源失败不影响其他来源。"""
    global _running
    if _running:
        return {"error": "资产清单正在刷新中"}
    _running = True
    try:
        results = [collect_cluster(c.get("name")) for c in k8s.clusters() if c.get("name") and c.get("kubeconfig")]
        g = collect_gitlab()
        if g:
            results.append(g)
        results.append(collect_extra())
        now = time.time()
        store.set_setting("asset_attempted_at", str(now))
        if all(r.get("status") == "ok" for r in results):
            store.set_setting("asset_refreshed_at", str(now))
        return {"results": results}
    finally:
        _running = False


def sync_due(now=None):
    """调度线程调：超过 REFRESH_HOURS 未刷新就刷新一次。"""
    now = now or time.time()
    last_ok = float(store.get_setting("asset_refreshed_at") or 0)
    last_try = float(store.get_setting("asset_attempted_at") or 0)
    if now - last_ok >= REFRESH_HOURS * 3600 and now - last_try >= RETRY_MINUTES * 60:
        result = refresh()
        ok = [r["source"] for r in result.get("results", []) if r.get("status") == "ok"]
        bad = [f"{r['source']}（{r.get('error')}）" for r in result.get("results", []) if r.get("status") != "ok"]
        print(f"[资产] 刷新完成：成功 {ok}" + (f"，失败 {bad}" if bad else ""), flush=True)
