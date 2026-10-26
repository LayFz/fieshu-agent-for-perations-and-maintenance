"""持续巡检内核；采集器和通知器由上层注入，内核只读 core/宿主授权。"""
import copy
from datetime import datetime, timezone
import logging
import re
import threading
import time
import uuid

from core import patrolstore, store
from . import access

log = logging.getLogger(__name__)
_scanner = None
_sender = None
_stop = threading.Event()
_threads = []
_lock = threading.Lock()
_last_tick = 0.0
LEASE_SECONDS = 120
_FIELDS = {"app_id", "name", "cluster", "git_projects", "namespaces", "log_targets", "interval_seconds", "chat_id", "enabled"}


def _fields(values):
    if set(values) != _FIELDS:
        raise ValueError("巡检配置字段不完整")
    result = dict(values)
    if isinstance(result["app_id"], bool):
        raise ValueError("应用编号无效")
    result["app_id"] = int(result["app_id"])
    if not isinstance(result["name"], str) or not result["name"].strip():
        raise ValueError("巡检名称不能为空")
    result["name"] = result["name"].strip()[:200]
    for field in ("cluster", "chat_id"):
        if result[field] is not None and not isinstance(result[field], str):
            raise ValueError(field + " 必须为字符串")
        result[field] = (result[field] or "").strip() or None
    for field in ("git_projects", "namespaces"):
        values = result[field] or []
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(field + " 必须是非空字符串数组")
        result[field] = list(dict.fromkeys(v.strip() for v in values))
    targets = result["log_targets"] or []
    if not isinstance(targets, list) or len(targets) > 20:
        raise ValueError("日志目标必须是最多 20 项的数组")
    normalized = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"org", "stream"}:
            raise ValueError("日志目标必须包含 org 和 stream")
        org, stream = target["org"], target["stream"]
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v) for v in (org, stream)):
            raise ValueError("日志组织或流名称无效")
        item = {"org": org, "stream": stream}
        if item not in normalized:
            normalized.append(item)
    result["log_targets"] = normalized
    interval = result["interval_seconds"]
    if isinstance(interval, bool) or not str(interval).isdigit() or int(interval) < 60:
        raise ValueError("巡检间隔必须是至少 60 秒的整数")
    result["interval_seconds"] = int(interval)
    if not isinstance(result["enabled"], (bool, int)) or result["enabled"] not in (0, 1):
        raise ValueError("enabled 必须为布尔值")
    result["enabled"] = bool(result["enabled"])
    if not result["cluster"] and not result["git_projects"] and not result["log_targets"]:
        raise ValueError("巡检必须指定集群、GitLab 项目或日志流")
    if "*" in result["git_projects"]:
        raise ValueError("请列出要巡检的具体 GitLab 项目")
    return result


def _authorized(monitor, app):
    if not app or not app.get("enabled"):
        raise PermissionError("应用已停用或不存在")
    if monitor.get("chat_id"):
        access.actor(app, monitor["chat_id"], system=True)
    resolved = copy.deepcopy(monitor)
    if monitor.get("cluster"):
        access.require(app, "k8s_clusters", monitor["cluster"])
        namespaces = monitor.get("namespaces") or (app.get("resources") or {}).get("namespaces") or []
        if not isinstance(namespaces, list) or not namespaces:
            raise PermissionError("集群巡检必须明确授权命名空间")
        for ns in namespaces:
            access.require(app, "namespaces", ns)
        resolved["namespaces"] = list(dict.fromkeys(namespaces))
    for project in monitor.get("git_projects") or []:
        access.require(app, "gitlab_projects", project)
    for target in monitor.get("log_targets") or []:
        access.require(app, "log_orgs", target["org"])
        access.require(app, "log_streams", target["stream"])
    return resolved


def configure(app_id, name, cluster=None, git_projects=None, log_targets=None, interval_seconds=300, chat_id=None, namespaces=None, enabled=True):
    values = _fields(dict(app_id=app_id, name=name, cluster=cluster, git_projects=git_projects,
                         log_targets=log_targets, interval_seconds=interval_seconds, chat_id=chat_id,
                         namespaces=namespaces, enabled=enabled))
    _authorized(values, store.get_app(values["app_id"]))
    return patrolstore.create(values)


def update(monitor_id, **fields):
    if not fields or set(fields) - _FIELDS:
        raise ValueError("未知或空巡检配置")
    old = get(monitor_id)
    if not old:
        return None
    values = _fields({**{k: old[k] for k in _FIELDS}, **fields})
    # 停用操作即使权限已经撤销也必须能成功。
    if values["enabled"]:
        _authorized(values, store.get_app(values["app_id"]))
    return patrolstore.update(monitor_id, values, expected_revision=old["revision"])


get = patrolstore.get
list_monitors = patrolstore.list_monitors
request_scan = patrolstore.request_scan
get_scan = patrolstore.get_scan
list_scans = patrolstore.list_scans
list_issues = patrolstore.list_issues


def register_scanner(fn):
    global _scanner
    _scanner = fn


def register_sender(fn):
    global _sender
    _sender = fn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _normalize(snapshot, previous=None):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("summary"), dict):
        raise ValueError("采集器必须返回 summary 对象")
    result = copy.deepcopy(snapshot)
    observed = result.get("observed_at")
    parsed = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("observed_at 必须为 UTC ISO 时间")
    result["observed_at"] = parsed.isoformat()
    sources, findings = result.get("sources"), result.get("findings")
    if not isinstance(sources, list) or not sources or not isinstance(findings, list):
        raise ValueError("采集器必须返回非空 sources 和 findings 数组")
    names = set()
    for source in sources:
        if (not isinstance(source, dict) or not isinstance(source.get("name"), str) or not source["name"]
                or source["name"].startswith("__") or source["name"] in names or source.get("status") not in ("ok", "error", "partial")):
            raise ValueError("采集来源格式无效")
        names.add(source["name"])
        if source.get("error") is not None and not isinstance(source["error"], str):
            raise ValueError("采集错误必须为文本")
    # 同配置下遗漏上次来源不能被当成成功；保留该来源旧问题并发出采集异常。
    if previous:
        for old in previous.get("sources", []):
            if not old["name"].startswith("__") and old["name"] not in names:
                sources.append({"name": old["name"], "status": "partial", "error": "本次采集缺少此前的数据来源"})
                names.add(old["name"])
    keys = set()
    for finding in findings:
        if (not isinstance(finding, dict) or not isinstance(finding.get("key"), str) or not finding["key"]
                or finding["key"].startswith("__") or finding["key"] in keys or finding.get("source") not in names
                or finding.get("severity") not in ("info", "low", "medium", "high", "critical")
                or not isinstance(finding.get("title"), str) or not finding["title"]
                or not isinstance(finding.get("detail", ""), str)):
            raise ValueError("巡检问题格式无效")
        keys.add(finding["key"])
        finding.setdefault("detail", "")
        finding.setdefault("resource", None)
        finding.setdefault("evidence", None)
    sources.extend([{"name": "__collector__", "status": "ok"}, {"name": "__authorization__", "status": "ok"}])
    _source_findings(result)
    if len(patrolstore.encode(result).encode()) > 8 * 1024 * 1024:
        raise ValueError("巡检快照超过 8 MiB")
    return result


def _source_findings(snapshot):
    for source in snapshot["sources"]:
        if source["status"] != "ok":
            snapshot["findings"].append({"key": "__source__:" + source["name"], "source": source["name"],
                "severity": "high" if source["status"] == "error" else "medium",
                "title": "采集失败" if source["status"] == "error" else "采集不完整",
                "detail": source.get("error") or source["status"], "resource": source["name"], "evidence": None})


def _failure(source, reason):
    snapshot = {"observed_at": _now(), "summary": {"collection_failed": True},
                "sources": [{"name": source, "status": "error", "error": reason}], "findings": []}
    _source_findings(snapshot)
    return snapshot


def _close_connection():
    conn = getattr(store._local, "conn", None)
    if conn is not None:
        conn.close()
        del store._local.conn


def _heartbeat(monitor, done):
    try:
        while not done.wait(max(0.1, LEASE_SECONDS / 3)):
            if not patrolstore.heartbeat(monitor, LEASE_SECONDS):
                break
    except Exception:
        log.warning("巡检心跳失败 monitor=%s", monitor["id"])
    finally:
        _close_connection()


def _execute(monitor):
    done = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(monitor, done), daemon=True, name="patrol-heartbeat")
    heartbeat.start()
    app = store.get_app(monitor["app_id"])
    scope_key = patrolstore.app_scope_key(app or {})
    try:
        try:
            resolved = _authorized(monitor, app)
        except PermissionError as exc:
            snapshot = _failure("__authorization__", str(exc))
        else:
            try:
                previous = get_scan(monitor["last_scan_id"]) if monitor.get("last_scan_id") else None
                previous = previous["snapshot"] if previous and previous["monitor_revision"] == monitor["revision"] else None
                snapshot = _normalize(_scanner(resolved, app), previous)
            except Exception as exc:
                # 连接异常可含凭据或 URL；仅持久化异常类别，具体脱敏诊断由来源提供。
                snapshot = _failure("__collector__", "采集器未完成：" + type(exc).__name__)
        fresh_app, fresh_monitor = store.get_app(monitor["app_id"]), get(monitor["id"])
        if (not fresh_monitor or fresh_monitor["revision"] != monitor["revision"] or not fresh_monitor["enabled"]
                or not fresh_app or not fresh_app.get("enabled") or patrolstore.app_scope_key(fresh_app) != scope_key):
            patrolstore.abandon(monitor)
            return
        patrolstore.save_scan(monitor, snapshot, scope_key)
    except patrolstore.LeaseLost:
        try:
            patrolstore.abandon(monitor)
        except patrolstore.LeaseLost:
            pass
    finally:
        done.set()
        heartbeat.join(timeout=1)


def dispatch_notification():
    if _sender is None:
        return False
    owner = "patrol-outbox-" + uuid.uuid4().hex
    item = patrolstore.claim_notification(owner)
    if not item:
        return False
    monitor, app = get(item["monitor_id"]), store.get_app(item["app_id"])
    permitted = (monitor and app and monitor["enabled"] and app.get("enabled")
                 and monitor["revision"] == item["monitor_revision"] and monitor["chat_id"] == item["chat_id"]
                 and patrolstore.app_scope_key(app) == item["app_scope_key"])
    if permitted:
        try:
            _authorized(monitor, app)
        except PermissionError:
            permitted = False
    if not permitted:
        patrolstore.finish_notification(item, owner, error="巡检已停用或授权/目的地变化", suppressed=True)
        return True
    try:
        _sender(item["app_id"], item["chat_id"], item["text"], item["id"])
    except Exception as exc:
        patrolstore.finish_notification(item, owner, error=type(exc).__name__)
    else:
        patrolstore.finish_notification(item, owner)
    return True


def tick():
    global _last_tick
    delivered = dispatch_notification()
    monitor = patrolstore.claim("patrol-worker-" + uuid.uuid4().hex, LEASE_SECONDS) if _scanner else None
    if monitor:
        _execute(monitor)
    delivered = dispatch_notification() or delivered
    _last_tick = time.time()
    return bool(monitor or delivered)


def _loop():
    try:
        while not _stop.is_set():
            try:
                busy = tick()
            except Exception:
                log.exception("巡检 worker 异常")
                busy = False
            if not busy:
                _stop.wait(0.5)
    finally:
        _close_connection()


def start(workers=1):
    with _lock:
        if any(t.is_alive() for t in _threads):
            return
        if _scanner is None:
            raise RuntimeError("尚未注册巡检采集器")
        _threads.clear()
        _stop.clear()
        for number in range(max(1, min(int(workers), 4))):
            thread = threading.Thread(target=_loop, name=f"patrol-worker-{number}", daemon=True)
            _threads.append(thread)
            thread.start()


def stop(timeout=5):
    _stop.set()
    deadline = time.monotonic() + max(0, timeout)
    for thread in list(_threads):
        thread.join(timeout=max(0, deadline - time.monotonic()))


def health():
    return {"workers": sum(t.is_alive() for t in _threads), "stopping": _stop.is_set(), "last_tick": _last_tick,
            "scanner_registered": _scanner is not None, "notifications": patrolstore.notification_counts()}
