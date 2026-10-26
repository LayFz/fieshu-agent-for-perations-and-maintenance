"""持续巡检契约：全部用临时 SQLite 和采集/发送替身，不访问网络。"""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-patrol-tests-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import patrolstore, store, taskstore
from harness import patrols, tasks
from app import patrol_service


def snapshot(findings=None, sources=None):
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "summary": {"checked": 1},
            "sources": sources if sources is not None else [{"name": "cluster:fixture:pods:team", "status": "ok"}],
            "findings": findings or []}


def issue(key="pod/api", severity="high", source="cluster:fixture:pods:team", detail="Pod unavailable"):
    return {"key": key, "source": source, "severity": severity, "title": "Pod unhealthy", "detail": detail,
            "resource": {"kind": "Pod", "name": "api", "namespace": "team"}, "evidence": {"phase": "Pending"}}


class PatrolTests(unittest.TestCase):
    def setUp(self):
        tasks.stop(timeout=1)
        patrols.stop(timeout=1)
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with patrolstore.transaction() as conn:
            for table in ("patrol_outbox", "patrol_issues", "patrol_scans", "patrol_monitors"):
                conn.execute("DELETE FROM " + table)
        self.app = store.create_app("巡检应用")
        store.update_app(self.app, resources={"k8s_clusters": ["fixture"], "namespaces": ["team"],
                                             "gitlab_projects": ["team/repo"], "chats": ["chat", "new-chat"]})
        self.scanner = mock.Mock(return_value=snapshot([issue()]))
        self.sender = mock.Mock()
        patrols.register_scanner(self.scanner)
        patrols.register_sender(self.sender)

    def tearDown(self):
        patrols.stop(timeout=1)
        patrols.register_scanner(None)
        patrols.register_sender(None)

    def monitor(self, chat="chat", **kwargs):
        return patrols.configure(self.app, "Fixture巡检", cluster="fixture", chat_id=chat, **kwargs)

    def again(self, monitor):
        self.assertTrue(patrols.request_scan(monitor["id"]))
        patrols.tick()

    def test_unchanged_problem_is_silent_but_updates_observation(self):
        m = self.monitor()
        patrols.tick()
        first = patrols.list_issues(m["id"])[0]
        self.assertEqual(self.sender.call_count, 1)
        second = issue()
        second["evidence"]["observation_counter"] = 2
        self.scanner.return_value = snapshot([second])
        self.again(m)
        current = patrols.list_issues(m["id"])[0]
        self.assertEqual(current["first_seen"], first["first_seen"])
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(len(patrols.list_scans(m["id"])), 2)
        self.assertEqual(current["evidence"]["observation_counter"], 2)
        self.assertEqual(self.scanner.call_args.args[0]["namespaces"], ["team"])

    def test_severity_material_change_recovery_and_reopen_notify_once(self):
        m = self.monitor()
        patrols.tick()
        self.scanner.return_value = snapshot([issue(severity="critical")])
        self.again(m)
        self.scanner.return_value = snapshot([issue(severity="critical", detail="Two pods unavailable")])
        self.again(m)
        self.scanner.return_value = snapshot()
        self.again(m)
        row = patrols.list_issues(m["id"])[0]
        self.assertFalse(row["active"])
        self.assertTrue(row["resolved_at"])
        self.again(m)
        self.assertEqual(self.sender.call_count, 4)
        self.scanner.return_value = snapshot([issue()])
        self.again(m)
        self.assertEqual(self.sender.call_count, 5)
        self.assertEqual(patrols.list_issues(m["id"])[0]["generation"], 2)

    def test_partial_failure_never_resolves_old_issue_and_failure_recovers(self):
        m = self.monitor()
        patrols.tick()
        self.scanner.return_value = snapshot(sources=[{"name": "cluster:fixture:pods:team", "status": "partial", "error": "timeout"}])
        self.again(m)
        self.assertEqual(len(patrols.list_issues(m["id"], active=True)), 2)
        self.assertEqual(self.sender.call_count, 2)
        self.again(m)
        self.assertEqual(self.sender.call_count, 2)
        self.scanner.return_value = snapshot()
        self.again(m)
        self.assertEqual(patrols.list_issues(m["id"], active=True), [])
        self.assertEqual(self.sender.call_count, 3)

    def test_only_successful_source_resolves_and_missing_source_is_reported(self):
        source_a, source_b = "a", "b"
        self.scanner.return_value = snapshot([issue("a-issue", source=source_a), issue("b-issue", source=source_b)],
            [{"name": "a", "status": "ok"}, {"name": "b", "status": "ok"}])
        m = self.monitor()
        patrols.tick()
        self.scanner.return_value = snapshot(sources=[{"name": "a", "status": "ok"}])
        self.again(m)
        active = {i["key"] for i in patrols.list_issues(m["id"], active=True)}
        self.assertEqual(active, {"b-issue", "__source__:b"})
        self.assertEqual(patrols.get_scan(patrols.get(m["id"])["last_scan_id"])["snapshot"]["sources"][1]["status"], "partial")

    def test_scanner_exception_is_durable_and_repeated_failure_is_silent(self):
        m = self.monitor()
        self.scanner.side_effect = OSError("must not leak credential in exception")
        patrols.tick()
        self.again(m)
        self.assertEqual(self.sender.call_count, 1)
        data = patrols.list_scans(m["id"])[0]["snapshot"]
        self.assertNotIn("credential", patrolstore.encode(data))
        self.assertTrue(data["summary"]["collection_failed"])
        self.scanner.side_effect = None
        self.scanner.return_value = snapshot()
        self.again(m)
        self.assertEqual(patrols.list_issues(m["id"], active=True), [])
        self.assertEqual(self.sender.call_count, 2)

    def test_notification_names_object_and_evidence(self):
        self.monitor()
        patrols.tick()
        text = self.sender.call_args.args[2]
        self.assertIn("对象：", text)
        self.assertIn("api", text)
        self.assertIn("phase=Pending", text)

    def test_pod_rollover_merges_into_one_still_failing_line(self):
        def pod(name):
            return {"key": "pod-not-ready:Pod/team/" + name, "source": "cluster:fixture:pods:team", "severity": "high",
                    "title": "Pod 未就绪", "detail": "", "resource": "Pod/team/" + name,
                    "evidence": {"phase": "Running", "waiting_reasons": ["CrashLoopBackOff"]}}
        self.scanner.return_value = snapshot([pod("topic-market-bf9df859f-xmmfs")])
        m = self.monitor()
        patrols.tick()
        self.scanner.return_value = snapshot([pod("topic-market-b554454d4-htvsc")])
        self.again(m)
        text = self.sender.call_args.args[2]
        self.assertIn("换代 1", text)
        self.assertNotIn("恢复", text)
        self.assertIn("问题仍在", text)
        self.assertIn("waiting_reasons=CrashLoopBackOff", text)

    def test_source_error_type_flapping_is_silent(self):
        def failing(error):
            return snapshot(sources=[{"name": "cluster:fixture:pods:team", "status": "error", "error": error}])
        self.scanner.return_value = failing("Kubernetes 连接失败：ReadTimeout")
        m = self.monitor()
        patrols.tick()
        self.scanner.return_value = failing("Kubernetes 连接失败：SSLError")
        self.again(m)
        self.assertEqual(self.sender.call_count, 1)
        self.assertIn("SSLError", patrols.list_issues(m["id"], active=True)[0]["detail"])

    def test_no_chat_persists_snapshot_without_notifications(self):
        m = self.monitor(chat=None)
        patrols.tick()
        self.assertEqual(len(patrols.list_scans(m["id"])), 1)
        self.assertEqual(len(patrols.list_issues(m["id"], active=True)), 1)
        self.sender.assert_not_called()
        self.assertEqual(patrols.health()["notifications"]["pending"], 0)

    def test_feishu_issue_command_creates_one_bound_repair_task(self):
        taskstore.init()
        m = self.monitor()
        patrols.tick()
        ledger = patrol_service.command(self.app, "chat", "问题台账", "user")
        ref = patrolstore.issue_ref(m["id"], "pod/api")
        self.assertIn("#" + ref, ledger)
        answer = patrol_service.command(self.app, "chat", "修复问题 " + ref, "user", "message-1")
        again = patrol_service.command(self.app, "chat", "修复问题 " + ref, "user", "message-1")
        self.assertIn("已创建修复任务", answer)
        self.assertIn("已接收过修复任务", again)
        rows = tasks.list_tasks(app_id=self.app, chat_id="chat")
        self.assertEqual(len([r for r in rows if r["source"] == "patrol"]), 1)
        self.assertEqual(rows[0]["workflow"], "development")
        self.assertEqual(rows[0]["speaker_open_id"], "user")
        self.assertIn("外部数据", rows[0]["instruction"])
        self.assertIn("尚未绑定", patrol_service.command(self.app, "new-chat", "修复问题 " + ref, "user"))
        with taskstore.transaction() as conn:
            conn.execute("DELETE FROM task_outbox WHERE task_id=?", (rows[0]["id"],))
            conn.execute("DELETE FROM tasks WHERE id=?", (rows[0]["id"],))

    def test_binding_chat_sends_fresh_baseline_even_when_issues_are_unchanged(self):
        m = self.monitor(chat=None)
        patrols.tick()
        self.sender.assert_not_called()
        patrols.update(m["id"], chat_id="chat")
        patrols.tick()
        self.sender.assert_called_once()
        self.assertIn("首次真实扫描完成", self.sender.call_args.args[2])
        self.assertIn("当前活动问题 1 项", self.sender.call_args.args[2])
        self.again(m)
        self.sender.assert_called_once()

    def test_notification_failure_retry_does_not_rescan(self):
        m = self.monitor()
        self.sender.side_effect = OSError("offline")
        patrols.tick()
        self.assertEqual(self.scanner.call_count, 1)
        first_id = self.sender.call_args.args[3]
        self.assertEqual(patrols.health()["notifications"]["pending"], 1)
        with patrolstore.transaction() as conn:
            conn.execute("UPDATE patrol_outbox SET available_at=0")
        self.sender.side_effect = None
        patrols.tick()
        self.assertEqual(self.scanner.call_count, 1)
        self.assertEqual(self.sender.call_args.args[3], first_id)
        self.assertEqual(patrols.health()["notifications"]["sent"], 1)

    def test_destination_change_or_disable_suppresses_old_notification(self):
        for change in ({"chat_id": "new-chat"}, {"enabled": False}):
            with self.subTest(change=change):
                m = self.monitor()
                patrols.register_sender(None)
                patrols.tick()
                self.assertEqual(patrols.health()["notifications"]["pending"], 1)
                patrols.update(m["id"], **change)
                patrols.register_sender(self.sender)
                patrols.dispatch_notification()
                self.sender.assert_not_called()
                patrols.update(m["id"], enabled=False)

    def test_resource_revoke_prevents_scanner_and_old_scope_save(self):
        m = self.monitor()
        store.update_app(self.app, resources={"k8s_clusters": ["other"], "namespaces": ["team"]})
        patrols.tick()
        self.scanner.assert_not_called()
        self.assertTrue(patrols.list_scans(m["id"])[0]["snapshot"]["summary"]["collection_failed"])
        self.sender.assert_not_called()
        store.update_app(self.app, resources={"k8s_clusters": ["fixture"], "namespaces": ["team"]})
        def revoke_during_scan(monitor, app):
            store.update_app(self.app, resources={"k8s_clusters": ["other"], "namespaces": ["team"]})
            return snapshot([issue()])
        self.scanner.side_effect = revoke_during_scan
        self.again(m)
        self.assertEqual(len(patrols.list_scans(m["id"])), 1)  # 原授权失败报告保留，旧范围新报告被丢弃。

    def test_config_change_during_scan_cannot_publish_old_destination(self):
        m = self.monitor()
        def change(monitor, app):
            patrols.update(m["id"], chat_id="new-chat")
            return snapshot([issue()])
        self.scanner.side_effect = change
        patrols.tick()
        self.assertEqual(patrols.list_scans(m["id"]), [])
        self.sender.assert_not_called()
        self.assertIsNone(patrols.get(m["id"])["lease_owner"])

    def test_app_disabled_blocks_claim_and_pending_delivery(self):
        m = self.monitor()
        patrols.register_sender(None)
        patrols.tick()
        store.update_app(self.app, enabled=False)
        patrols.register_sender(self.sender)
        self.again(m)
        self.assertEqual(self.scanner.call_count, 1)
        self.sender.assert_not_called()
        self.assertEqual(patrols.health()["notifications"]["suppressed"], 1)

    def test_two_claimers_cannot_overlap_and_expired_lease_recovers(self):
        m = self.monitor()
        barrier, claims = threading.Barrier(2), []
        def claim():
            try:
                barrier.wait()
                claims.append(patrolstore.claim(threading.current_thread().name))
            finally:
                patrols._close_connection()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        old = next(c for c in claims if c)
        self.assertEqual(sum(c is not None for c in claims), 1)
        new = patrolstore.claim("restarted-process", now=time.time() + 121)
        self.assertEqual(new["id"], m["id"])
        self.assertGreater(new["lease_epoch"], old["lease_epoch"])
        with self.assertRaises(patrolstore.LeaseLost):
            patrolstore.save_scan(old, patrols._normalize(snapshot()), patrolstore.app_scope_key(store.get_app(self.app)))
        self.assertFalse(patrolstore.heartbeat(old))

    def test_restart_keeps_pending_outbox_and_scan_deduplication(self):
        m = self.monitor()
        patrols.register_sender(None)
        patrols.tick()
        scan_id = patrols.get(m["id"])["last_scan_id"]
        patrols._close_connection()  # 新 SQLite 连接模拟进程重开；状态全部来自数据库。
        self.assertEqual(patrols.get_scan(scan_id)["monitor_id"], m["id"])
        patrols.register_sender(self.sender)
        patrols.tick()
        self.assertEqual(self.scanner.call_count, 1)
        self.sender.assert_called_once()
        self.assertEqual(len(patrols.list_scans(m["id"])), 1)

    def test_config_and_scope_fail_closed(self):
        with self.assertRaises(ValueError):
            self.monitor(interval_seconds=59)
        with self.assertRaises(PermissionError):
            self.monitor(namespaces=["other"])
        with self.assertRaises(PermissionError):
            self.monitor(git_projects=["other/repo"])
        with self.assertRaises(ValueError):
            self.monitor(git_projects=["*"])
        with self.assertRaises(PermissionError):
            self.monitor(log_targets=[{"org": "default", "stream": "k8s_api"}])
        with self.assertRaises(ValueError):
            self.monitor(log_targets=[{"org": "default", "stream": "bad\"stream"}])
        store.update_app(self.app, resources={"log_orgs": ["default"], "log_streams": ["k8s_api"],
                                              "gitlab_projects": ["team/repo"]})
        log_monitor = patrols.configure(self.app, "Only logs", log_targets=[{"org": "default", "stream": "k8s_api"}])
        self.assertEqual(log_monitor["log_targets"], [{"org": "default", "stream": "k8s_api"}])
        m = patrols.configure(self.app, "GitLab only", git_projects=["team/repo"], chat_id=None)
        self.assertIsNone(m["cluster"])

    def test_manual_request_during_scan_is_retained(self):
        m = self.monitor()
        def request_again(monitor, app):
            patrols.request_scan(monitor["id"])
            return snapshot()
        self.scanner.side_effect = request_again
        patrols.tick()
        self.assertLessEqual(patrols.get(m["id"])["next_run"], time.time())


if __name__ == "__main__":
    unittest.main()
