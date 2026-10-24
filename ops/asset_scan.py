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


_INTERNAL_SUFFIXES = [".sslip.io", ".nip.io", ".local", ".internal", ".lan", ".svc", ".cluster.local", ".localhost"]
_RANK = {"public": 5, "indirect": 4, "lan": 3, "cluster": 2, "none": 1, "unknown": 0}
# 数据库、缓存、消息队列：只被同命名空间的应用访问，不会经前端转发到公网
DATASTORES = {"postgresql", "mysql", "mariadb", "redis", "valkey", "rabbitmq", "minio", "mongodb", "elasticsearch",
              "etcd", "kafka", "redpanda", "pgvector", "clickhouse", "memcached", "cloudnative-pg"}


def _internal_suffixes():
    try:
        value = json.loads(store.get_setting("asset_internal_host_suffixes") or "null")
        return value if isinstance(value, list) and value else _INTERNAL_SUFFIXES
    except ValueError:
        return _INTERNAL_SUFFIXES


def is_public_host(host, internal=None):
    """Ingress 域名是否公网可达：IP 或内网后缀（sslip.io 等指向内网 VIP 的域名）算内网，其余算公网。"""
    host = (host or "").lower().strip(".")
    if not host or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return False
    return not any(host.endswith(s) for s in (internal or _internal_suffixes()))


def service_exposure(services, ingresses, internal=None):
    """{(ns, service): (级别, 说明)}：被公网域名 Ingress 引用 → public；内网域名 Ingress / LoadBalancer / NodePort → lan；
    其余 → cluster（仅集群内）。"""
    out = {}

    def bump(key, level, note):
        cur = out.get(key)
        if not cur or _RANK[level] > _RANK[cur[0]]:
            out[key] = (level, note)
        elif cur[0] == level and note not in cur[1]:
            out[key] = (level, (cur[1] + "、" + note)[:200])
    for svc in services:
        md, spec = _d(svc.get("metadata")), _d(svc.get("spec"))
        key = (md.get("namespace"), md.get("name"))
        kind = spec.get("type") or "ClusterIP"
        if kind in ("LoadBalancer", "NodePort"):
            ip = (_l(_d(_d(svc.get("status")).get("loadBalancer")).get("ingress")) or [{}])[0].get("ip") or ""
            bump(key, "lan", f"{kind} 服务 {md.get('name')}" + (f"（{ip}）" if ip else ""))
        else:
            bump(key, "cluster", f"仅集群内 Service {md.get('name')}")
    for ing in ingresses:
        md, spec = _d(ing.get("metadata")), _d(ing.get("spec"))
        backends = []
        default = _d(_d(spec.get("defaultBackend")).get("service")).get("name")
        for rule in _l(spec.get("rules")):
            rule = _d(rule)
            for path in _l(_d(rule.get("http")).get("paths")):
                svc = _d(_d(_d(path).get("backend")).get("service")).get("name")
                if svc:
                    backends.append((rule.get("host") or "", svc))
            if default:
                backends.append((rule.get("host") or "", default))
        for host, svc in backends:
            key = (md.get("namespace"), svc)
            if is_public_host(host, internal):
                bump(key, "public", f"公网域名 {host}")
            else:
                bump(key, "lan", f"内网域名 {host or '（无域名）'}")
    return out


def pod_exposure(pod, services, svc_exposure):
    """Pod 被哪些 Service 选中，取其中暴露面最大的；没有 Service 选中 → none。"""
    md = _d(pod.get("metadata"))
    ns, labels = md.get("namespace"), _d(md.get("labels"))
    best = ("none", "没有 Service 指向它（后台任务或内部组件）")
    for svc in services:
        smd, sel = _d(svc.get("metadata")), _d(_d(svc.get("spec")).get("selector"))
        if smd.get("namespace") != ns or not sel or any(labels.get(k) != v for k, v in sel.items()):
            continue
        level = svc_exposure.get((ns, smd.get("name")), ("cluster", f"仅集群内 Service {smd.get('name')}"))
        if _RANK[level[0]] > _RANK[best[0]]:
            best = level
    return best


def pods_to_assets(items, cluster, own, services=None, ingresses=None):
    services = services or []
    svc_exposure = service_exposure(services, ingresses or [])
    # 有公网入口的命名空间：入口通常落在前端/网关，后端 API 经它转发，ClusterIP 不等于外部到不了
    public_ns = {}
    for (ns, _svc), (level, note) in svc_exposure.items():
        if level == "public":
            public_ns.setdefault(ns, note)
    rows = []
    for pod in items:
        st = _d(pod.get("status"))
        if st.get("phase") not in ("Running", "Pending"):
            continue
        md, spec = _d(pod.get("metadata")), _d(pod.get("spec"))
        exposure, exposure_note = pod_exposure(pod, services, svc_exposure)
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
            exp, exp_note = exposure, exposure_note
            if exp in ("cluster", "none") and md.get("namespace") in public_ns and comp not in DATASTORES:
                exp, exp_note = "indirect", f"本身无公网入口，但同命名空间有{public_ns[md.get('namespace')]}，可能经前端/网关转发到达"
            rows.append({"kind": "service" if mine else "image", "component": comp, "version": version,
                         "image": image, "digest": digests.get(c.get("name"), ""), "cluster": cluster,
                         "namespace": md.get("namespace") or "", "workload": f"{_workload(pod)} · {role}",
                         "app": _app_of(pod), "evidence": "pod 运行时镜像（含摘要）",
                         "exposure": exp, "exposure_note": exp_note})
    return rows


def nodes_to_assets(items, cluster):
    rows = []
    for node in items:
        info = _d(_d(node.get("status")).get("nodeInfo"))
        name = _d(node.get("metadata")).get("name") or ""
        kubelet = info.get("kubeletVersion") or ""
        base = {"cluster": cluster, "workload": f"Node/{name}", "evidence": "node.status.nodeInfo",
                "exposure": "lan", "exposure_note": "集群节点组件（内网）"}
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


def _override(app, namespace):
    """远端集群（只看到 ArgoCD 引用）读不到 Service/Ingress：用后台配置 kv asset_exposure_overrides
    {"应用名或命名空间": "public|lan|cluster"} 补充，否则为 unknown。"""
    try:
        overrides = json.loads(store.get_setting("asset_exposure_overrides") or "{}")
    except ValueError:
        overrides = {}
    for key in (app, namespace):
        if isinstance(overrides, dict) and overrides.get(key) in _RANK:
            return overrides[key], f"后台配置：{key} → {overrides[key]}"
    return "unknown", "远端集群，未读取 Service/Ingress，暴露面未知"


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
        exposure, note = _override(md.get("name"), dest.get("namespace"))
        for image in _l(_d(st.get("summary")).get("images")):
            comp, version, mine = components.component_of(image, own)
            rows.append({"kind": "service" if mine else "image", "component": comp, "version": version,
                         "image": image, "cluster": target, "namespace": dest.get("namespace") or "",
                         "workload": "", "app": md.get("name") or "",
                         "evidence": f"ArgoCD {md.get('name')} status.summary.images（资源引用，未核对运行时摘要）",
                         "exposure": exposure, "exposure_note": note})
    return rows


def collect_cluster(name):
    source = f"cluster:{name}"
    try:
        creds = cluster_scan._credentials(name)
        with tempfile.TemporaryDirectory(prefix="harness-asset-scan-") as directory:
            client = cluster_scan._Client(creds, directory)
            try:
                own = own_registries()
                try:
                    services = client.objects("/api/v1/services")
                    ingresses = client.objects("/apis/networking.k8s.io/v1/ingresses")
                except cluster_scan.ScanError:
                    services, ingresses = [], []        # 读不到入口信息：暴露面按 none 处理
                rows = pods_to_assets(client.objects("/api/v1/pods"), name, own, services, ingresses)
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
    host = re.sub(r"^https?://", "", gitlab.config()["url"]).split("/")[0].split(":")[0]
    exposure = "public" if is_public_host(host) else "lan"
    count = assetstore.replace("gitlab", [{"kind": "component", "component": "gitlab", "version": version,
                                           "evidence": "GitLab /api/v4/version", "workload": "GitLab",
                                           "exposure": exposure, "exposure_note": f"{'公网' if exposure == 'public' else '内网'}域名 {host}"}])
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
        from . import image_scan
        scan = image_scan.collect()
        if scan.get("status") != "skipped":
            results.append({"source": "harbor-scan", "status": "ok" if scan.get("status") == "ok" else "error",
                            "assets": scan.get("scanned"), "error": scan.get("error") or
                            (f"{scan.get('errors')} 个镜像读取失败" if scan.get("errors") else None)})
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
