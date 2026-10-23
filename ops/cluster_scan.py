"""内置只读 Kubernetes 巡检：固定 API GET、内嵌 kubeconfig 凭据、按来源报告覆盖率。

不启动 kubectl、不执行 kubeconfig 插件、不读取 Secret/ConfigMap 或外部凭据文件。
原始对象只在采集器内解释；返回字段限定为资源标识、状态、时间和脱敏 Argo 来源。
"""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, urlsplit, urlunsplit

import requests
import yaml

from core import store

MAX_PAGES = 40
MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 10000
SCAN_SECONDS = 90
RECENT_SECONDS = 24 * 3600
_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")


class ScanError(Exception):
    """仅包含宿主生成的安全错误说明，不包含服务端正文或凭据。"""


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _identifier(value, limit=253):
    return value if isinstance(value, str) and len(value) <= limit and _NAME.fullmatch(value) else "unknown"


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,256}", value) else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else None
    except ValueError:
        return None


def _safe_time(value):
    return value if _timestamp(value) is not None else None


def _recent(value, now):
    ts = _timestamp(value)
    return ts is not None and -300 <= now - ts <= RECENT_SECONDS


def _number(value):
    return value if type(value) is int and 0 <= value <= 10 ** 9 else 0


def _resource(obj, kind):
    md = _dict(obj.get("metadata"))
    return f"{kind}/{_identifier(md.get('namespace'))}/{_identifier(md.get('name'))}"


def _url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        p = urlsplit(value)
        if p.scheme not in ("http", "https", "ssh", "git") or not p.hostname:
            return None
        host = p.hostname
        if ":" in host:
            host = "[" + host + "]"
        if p.port:
            host += ":" + str(p.port)
        return urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        return None


def _named(rows, name, field):
    matches = [r for r in _list(rows) if isinstance(r, dict) and r.get("name") == name]
    if len(matches) != 1 or not isinstance(matches[0].get(field), dict):
        raise ScanError("kubeconfig 当前上下文或引用不唯一/缺失")
    return matches[0][field]


def _credentials(cluster_name):
    try:
        configs = json.loads(store.get_setting("k8s_clusters", "[]") or "[]")
        candidates = [c for c in configs if isinstance(c, dict) and c.get("name") == cluster_name]
        if len(candidates) != 1:
            raise ScanError("未找到唯一的受管集群配置")
        raw = candidates[0].get("kubeconfig")
        if not isinstance(raw, str) or len(raw) > 2 * 1024 * 1024:
            raise ScanError("需要内嵌的 kubeconfig 文本")
        cfg = yaml.safe_load(raw)
        if not isinstance(cfg, dict):
            raise ScanError("kubeconfig 格式不正确")
        for row in _list(cfg.get("users")):
            u = _dict(_dict(row).get("user"))
            if any(k in u for k in ("exec", "auth-provider", "tokenFile", "token-file",
                                     "client-certificate", "client-key", "username", "password")):
                raise ScanError("拒绝 kubeconfig 插件、外部凭据文件及不支持的认证方式")
        for row in _list(cfg.get("clusters")):
            c = _dict(_dict(row).get("cluster"))
            if c.get("insecure-skip-tls-verify") or any(k in c for k in (
                    "certificate-authority", "proxy-url", "tls-server-name")):
                raise ScanError("需要 TLS 验证及内嵌 CA；拒绝外部文件、代理和服务器名覆盖")
        ctx = _named(cfg.get("contexts"), cfg.get("current-context"), "context")
        cluster = _named(cfg.get("clusters"), ctx.get("cluster"), "cluster")
        user = _named(cfg.get("users"), ctx.get("user"), "user") if ctx.get("user") else {}
        server = cluster.get("server")
        p = urlsplit(server) if isinstance(server, str) else None
        if (not p or p.scheme != "https" or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.path not in ("", "/")):
            raise ScanError("Kubernetes 地址必须为无凭据、无额外路径的 HTTPS API 地址")
        token = user.get("token")
        if token is not None and (not isinstance(token, str) or not token or
                                  len(token) > 65536 or any(c.isspace() for c in token)):
            raise ScanError("kubeconfig token 格式不正确")
        cert, key = user.get("client-certificate-data"), user.get("client-key-data")
        if bool(cert) != bool(key):
            raise ScanError("内嵌客户端证书与私钥必须成对配置")
        return {"server": server.rstrip("/"), "token": token,
                "ca": cluster.get("certificate-authority-data"), "cert": cert, "key": key}
    except ScanError:
        raise
    except Exception:
        raise ScanError("无法解析受管集群 kubeconfig") from None


class _Client:
    def __init__(self, config, directory):
        self.server = config["server"]
        self.deadline = time.monotonic() + SCAN_SECONDS
        self.pages = 0
        self.session = requests.Session()
        self.session.trust_env = False  # 不继承代理、netrc 或宿主自定义 CA 路径。
        self.verify = True
        self.cert = None
        self.headers = {"Accept": "application/json"}
        if config.get("token"):
            self.headers["Authorization"] = "Bearer " + config["token"]
        try:
            for name in ("ca", "cert", "key"):
                data = config.get(name)
                if not data:
                    continue
                if not isinstance(data, str) or len(data) > 1024 * 1024:
                    raise ScanError("内嵌证书数据超出限制")
                try:
                    pem = base64.b64decode(data, validate=True)
                except (ValueError, TypeError):
                    raise ScanError("内嵌证书数据不是有效 base64") from None
                if not pem.startswith(b"-----BEGIN "):
                    raise ScanError("内嵌证书数据不是 PEM")
                path = Path(directory) / (name + ".pem")
                path.write_bytes(pem)
                path.chmod(0o600)
                if name == "ca":
                    self.verify = str(path)
            if config.get("cert"):
                self.cert = (str(Path(directory) / "cert.pem"), str(Path(directory) / "key.pem"))
        except Exception:
            self.session.close()
            raise

    def close(self):
        self.session.close()

    def objects(self, path):
        # 调用者只能提供下面的固定只读集合端点，不提供通用 API 透传。
        if not re.fullmatch(r"/(?:api/v1/(?:namespaces/[a-z0-9.-]+/)?(?:pods|nodes|events)|"
                            r"apis/argoproj\.io/v1alpha1/namespaces/argocd/applications|"
                            r"apis/postgresql\.cnpg\.io/v1/(?:namespaces/[a-z0-9.-]+/)?"
                            r"(?:clusters|backups|scheduledbackups))", path):
            raise ScanError("请求不在只读资源白名单内")
        out, continuation, seen = [], None, set()
        while True:
            remaining = self.deadline - time.monotonic()
            if self.pages >= MAX_PAGES or remaining <= 0:
                raise ScanError("扫描达到总页数或时间预算；该来源覆盖不完整")
            self.pages += 1
            params = {"limit": 200}
            if continuation:
                params["continue"] = continuation
            if path.endswith("/events"):
                params["fieldSelector"] = "type=Warning"
            response = None
            try:
                response = self.session.get(self.server + path, params=params, headers=self.headers,
                                            verify=self.verify, cert=self.cert, allow_redirects=False,
                                            stream=True, timeout=(min(5, remaining), min(10, remaining)))
                if response.status_code != 200:
                    raise ScanError(f"Kubernetes API 返回 HTTP {response.status_code}；该来源未完成")
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() >= self.deadline:
                        raise ScanError("API 响应超出大小或时间预算")
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            except ScanError:
                raise
            except requests.RequestException as exc:
                raise ScanError("Kubernetes 连接失败：" + type(exc).__name__) from None
            except Exception:
                raise ScanError("Kubernetes API 响应格式无效") from None
            finally:
                if response is not None:
                    response.close()
            if (not isinstance(payload, dict) or not isinstance(payload.get("items"), list)
                    or any(not isinstance(x, dict) for x in payload["items"])):
                raise ScanError("Kubernetes 集合响应格式无效")
            out.extend(payload["items"])
            if len(out) > MAX_ITEMS:
                raise ScanError("资源数量超出扫描预算；该来源覆盖不完整")
            continuation = _dict(payload.get("metadata")).get("continue")
            if not continuation:
                return out
            if not isinstance(continuation, str) or len(continuation) > 4096 or continuation in seen:
                raise ScanError("API 分页标记无效或重复")
            seen.add(continuation)


def _finding(source, code, resource, severity, title, detail, evidence):
    return {"key": code + ":" + resource, "source": source, "severity": severity,
            "title": title, "detail": detail, "resource": resource, "evidence": evidence}


def _pods(items, source, now):
    findings = []
    for obj in items:
        st, md = _dict(obj.get("status")), _dict(obj.get("metadata"))
        resource = _resource(obj, "Pod")
        phase = _label(st.get("phase"))
        statuses = [_dict(c) for c in _list(st.get("containerStatuses"))]
        if phase == "Failed":
            findings.append(_finding(source, "pod-failed", resource, "medium", "发现失败 Pod",
                "该 Pod 保留 Failed 状态；需要核对所属任务和后续成功记录，不能据此断言服务持续不可用。",
                {"phase": phase, "reasons": sorted({r for c in statuses if (r := _label(
                    _dict(_dict(c.get("state")).get("terminated")).get("reason")))})}))
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in _list(st.get("conditions")) if isinstance(c, dict))
        created = _timestamp(md.get("creationTimestamp"))
        if phase in ("Running", "Pending", "Unknown") and not ready and (created is None or now - created >= 300):
            findings.append(_finding(source, "pod-not-ready", resource, "high", "Pod 未就绪",
                "Pod 超过启动宽限期仍未 Ready；需要核对调度、探针和依赖。",
                {"phase": phase, "waiting_reasons": sorted({r for c in statuses if (r := _label(
                    _dict(_dict(c.get("state")).get("waiting")).get("reason")))})}))
        for c in statuses:
            last = _dict(_dict(c.get("lastState")).get("terminated"))
            if last.get("reason") == "OOMKilled" and _recent(last.get("finishedAt"), now):
                name = _identifier(c.get("name"))
                findings.append(_finding(source, "pod-oom", resource + "/" + name, "medium", "容器近期因内存不足退出",
                    "检测到最近 24 小时内 OOMKilled；当前是否恢复与内存泄漏/容量原因需分别核对。",
                    {"last_finished": _safe_time(last.get("finishedAt")), "restarts": _number(c.get("restartCount")),
                     "exit_code": _number(last.get("exitCode")), "currently_ready": c.get("ready") is True}))
    return findings


def _nodes(items, source):
    findings = []
    for obj in items:
        st = _dict(obj.get("status"))
        conditions = {_label(c.get("type")): c.get("status") for c in _list(st.get("conditions")) if isinstance(c, dict)}
        name = _identifier(_dict(obj.get("metadata")).get("name"))
        pressures = [k for k in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable") if conditions.get(k) == "True"]
        if conditions.get("Ready") != "True" or pressures:
            findings.append(_finding(source, "node-health", "Node/" + name, "high", "节点状态异常",
                "节点未 Ready 或报告资源/网络压力；维护前需核对集群仲裁与影响范围。",
                {"ready": conditions.get("Ready") == "True", "pressures": pressures}))
    return findings


def _events(items, source, now):
    findings = {}
    allowed = {"Unhealthy", "BackOff", "FailedScheduling", "FailedMount", "FailedAttachVolume", "Failed", "FailedCreate", "InvalidSchedule"}
    for obj in items:
        reason = obj.get("reason")
        target = _dict(obj.get("involvedObject"))
        ts = _dict(obj.get("series")).get("lastObservedTime") or obj.get("lastTimestamp") or obj.get("eventTime")
        if (obj.get("type") != "Warning" or reason not in allowed or not _recent(ts, now)
                or target.get("kind") not in ("Pod", "Node", "Job", "Backup", "ScheduledBackup")):
            continue
        resource = target["kind"] + "/" + _identifier(target.get("namespace")) + "/" + _identifier(target.get("name"))
        category = "probe_failure" if reason == "Unhealthy" else "kubernetes_warning"
        # 原始 message 仅在内存分类，绝不回传自由文本（其中可能包含 URL/连接串/业务内容）。
        message = str(obj.get("message") or "").lower()[:8192]
        if reason == "Unhealthy" and ("timeout" in message or "deadline exceeded" in message):
            category = "probe_timeout"
        if target.get("kind") == "ScheduledBackup" and "schedule" in message and any(s in message for s in ("parse", "invalid", "expected")):
            category = "schedule_parse_error"
        item = _finding(source, "event-" + reason, resource, "medium", "近期 Kubernetes 警告事件",
            "最近 24 小时内有警告；探针可能间歇失败，当前 Ready 状态不能替代事件趋势核对。",
            {"reason": reason, "category": category, "last_seen": _safe_time(ts),
             "count": _number(_dict(obj.get("series")).get("count") or obj.get("count"))})
        old = findings.get(item["key"])
        if old is None or (_timestamp(ts) or 0) > (_timestamp(old["evidence"]["last_seen"]) or 0):
            findings[item["key"]] = item
    return list(findings.values())


def _applications(items, source):
    summaries, findings = [], []
    for obj in items:
        md, spec, st = _dict(obj.get("metadata")), _dict(obj.get("spec")), _dict(obj.get("status"))
        sync, health, op = _dict(st.get("sync")), _dict(st.get("health")), _dict(st.get("operationState"))
        history = [_dict(h) for h in _list(st.get("history"))]
        latest = max(history, key=lambda h: _timestamp(h.get("deployedAt")) or 0) if history else {}
        srcs = _list(spec.get("sources")) or ([spec["source"]] if isinstance(spec.get("source"), dict) else [])
        summary = {"name": _identifier(md.get("name")), "namespace": _identifier(md.get("namespace")),
                   "sync": _label(sync.get("status")), "health": _label(health.get("status")),
                   "compared_revision": _label(sync.get("revision")), "last_operation_phase": _label(op.get("phase")),
                   "last_operation_revision": _label(_dict(op.get("syncResult")).get("revision")),
                   "last_operation_finished": _safe_time(op.get("finishedAt")),
                   "latest_history_revision": _label(latest.get("revision")),
                   "latest_history_deployed_at": _safe_time(latest.get("deployedAt")),
                   "condition_types": [_label(c.get("type")) for c in _list(st.get("conditions")) if isinstance(c, dict) and _label(c.get("type"))],
                   "sources": [{"repo_url": _url(_dict(s).get("repoURL")), "path": _label(_dict(s).get("path")),
                                "target_revision": _label(_dict(s).get("targetRevision"))} for s in srcs]}
        summaries.append(summary)
        resource = _resource(obj, "Application")
        unhealthy = summary["sync"] != "Synced" or summary["health"] != "Healthy" or summary["condition_types"]
        if unhealthy:
            findings.append(_finding(source, "argo-state", resource, "high" if summary["health"] in ("Degraded", "Missing") else "medium",
                "ArgoCD 应用当前状态需要关注", "当前同步、健康或条件存在异常；先核对 GitOps 版本和资源状态。",
                {k: summary[k] for k in ("sync", "health", "condition_types", "compared_revision")}))
        elif op.get("phase") in ("Failed", "Error"):
            findings.append(_finding(source, "argo-historical-operation", resource, "info", "ArgoCD 保留历史同步失败记录",
                "应用当前 Synced 且 Healthy；旧失败操作不能直接视为当前故障，尝试版本也不等于成功部署版本。",
                {k: summary[k] for k in ("sync", "health", "last_operation_phase", "last_operation_revision",
                                          "last_operation_finished", "latest_history_revision")}))
    return summaries, findings


def _cnpg(clusters, backups, schedules, source):
    findings = []
    for obj in clusters:
        md, spec, st = _dict(obj.get("metadata")), _dict(obj.get("spec")), _dict(obj.get("status"))
        ns, name = md.get("namespace"), md.get("name")
        def matches(item):
            return (_dict(item.get("metadata")).get("namespace") == ns
                    and _dict(_dict(item.get("spec")).get("cluster")).get("name") == name)
        owned_backups = [b for b in backups if matches(b)]
        owned_schedules = [s for s in schedules if matches(s)]
        active = [s for s in owned_schedules if _dict(s.get("spec")).get("suspend") is not True]
        plugin = any(_dict(p).get("isWALArchiver") is True for p in _list(spec.get("plugins")))
        backup_config = _dict(spec.get("backup"))
        configured = bool(_dict(backup_config.get("barmanObjectStore")) or
                          _dict(backup_config.get("volumeSnapshot"))) or plugin
        resource = _resource(obj, "Cluster")
        completed = [b for b in owned_backups if _dict(b.get("status")).get("phase") == "completed"]
        evidence = {"backup_configured": configured, "active_schedules": len(active), "backup_objects": len(owned_backups),
                    "completed_backup_objects": len(completed), "last_successful_backup": _safe_time(st.get("lastSuccessfulBackup"))}
        if not configured or not active:
            findings.append(_finding(source, "cnpg-backup-coverage", resource, "high" if not configured and not completed else "medium",
                "CNPG 托管备份覆盖不完整", "缺少备份配置或启用的 ScheduledBackup；仅说明 CNPG 覆盖，外部备份和恢复能力未验证。", evidence))
        if owned_backups:
            latest = max(owned_backups, key=lambda b: _timestamp(_dict(b.get("metadata")).get("creationTimestamp")) or 0)
            state = _dict(latest.get("status"))
            if state.get("phase") in ("failed", "error"):
                findings.append(_finding(source, "cnpg-latest-backup-failed", resource, "medium", "最近一次 CNPG 备份失败",
                    "最新 Backup 对象失败；需核对导出、存储与后续补备份，未读取数据库或备份内容。",
                    {"backup": _identifier(_dict(latest.get("metadata")).get("name")), "phase": state["phase"],
                     "started": _safe_time(state.get("startedAt")), "stopped": _safe_time(state.get("stoppedAt"))}))
    return findings


def scan(cluster_name, namespaces=None):
    """按受管集群名扫描；具体 namespace 列表只请求对应命名空间，None/['*'] 为全局。"""
    now = datetime.now(timezone.utc)
    observed = now.isoformat().replace("+00:00", "Z")
    base = "cluster:" + str(cluster_name)
    result = {"observed_at": observed, "sources": [], "summary": {}, "findings": [], "applications": []}
    try:
        if not isinstance(cluster_name, str) or not cluster_name or len(cluster_name) > 100 or any(c in cluster_name for c in "\r\n"):
            raise ScanError("集群名称无效")
        if namespaces is None or namespaces == ["*"]:
            scopes = ["*"]
        elif (not isinstance(namespaces, list) or not namespaces or len(namespaces) > 32
              or any(not isinstance(n, str) or len(n) > 63 or not _NAME.fullmatch(n) or "." in n for n in namespaces)):
            raise ScanError("namespaces 必须是明确的命名空间列表或 ['*']")
        else:
            scopes = list(dict.fromkeys(namespaces))
        credentials = _credentials(cluster_name)
        with tempfile.TemporaryDirectory(prefix="harness-k8s-scan-") as directory:
            client = _Client(credentials, directory)
            try:
                def collect(name, operation):
                    source = base + ":" + name
                    try:
                        operation(source)
                    except ScanError as exc:
                        result["sources"].append({"name": source, "status": "error", "error": str(exc)})
                    except Exception:
                        result["sources"].append({"name": source, "status": "error", "error": "资源状态解析失败；该来源未完成"})
                    else:
                        result["sources"].append({"name": source, "status": "ok"})

                def nodes(source):
                    items = client.objects("/api/v1/nodes")
                    result["summary"]["node_count"] = len(items)
                    result["findings"].extend(_nodes(items, source))
                if scopes == ["*"]:
                    collect("nodes", nodes)
                for scope in scopes:
                    ns = "" if scope == "*" else "namespaces/" + quote(scope, safe="") + "/"
                    def pods(source):
                        items = client.objects("/api/v1/" + ns + "pods")
                        result["summary"].setdefault("pod_counts", {})[scope] = len(items)
                        result["findings"].extend(_pods(items, source, now.timestamp()))
                    collect("pods:" + scope, pods)
                    collect("events:" + scope, lambda source: result["findings"].extend(
                        _events(client.objects("/api/v1/" + ns + "events"), source, now.timestamp())))
                    def cnpg(source):
                        prefix = "/apis/postgresql.cnpg.io/v1/" + ns
                        clusters = client.objects(prefix + "clusters")
                        backups = client.objects(prefix + "backups")
                        schedules = client.objects(prefix + "scheduledbackups")
                        result["findings"].extend(_cnpg(clusters, backups, schedules, source))
                    collect("cnpg-backup:" + scope, cnpg)
                if scopes == ["*"] or "argocd" in scopes:
                    def applications(source):
                        apps, findings = _applications(client.objects("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"), source)
                        result["applications"] = apps
                        result["findings"].extend(findings)
                    collect("applications:argocd", applications)
            finally:
                client.close()
    except ScanError as exc:
        result["sources"].append({"name": base, "status": "error", "error": str(exc)})
    except Exception:
        result["sources"].append({"name": base, "status": "error", "error": "集群扫描初始化失败"})
    ok = sum(s["status"] == "ok" for s in result["sources"])
    total = len(result["sources"])
    result["summary"].update(status="ok" if ok == total else "partial" if ok else "error",
                              successful_sources=ok, total_sources=total, findings=len(result["findings"]),
                              applications=len(result["applications"]))
    return result
