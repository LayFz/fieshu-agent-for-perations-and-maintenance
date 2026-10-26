"""工具权限、动作去重、审批恢复和不确定副作用的契约测试；不调用外部服务。"""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_TEST_DIR = tempfile.TemporaryDirectory(prefix="feishu-pipeline-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _TEST_DIR.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store, taskstore
from harness import approval, pipeline, policy, runs, tasks


class PolicyTests(unittest.TestCase):
    def test_unconfigured_readonly_invalid_deny(self):
        for value in (None, {}, "", "{}"):
            self.assertEqual(policy.decide(value, "read", "read")[0], "allow")
            self.assertEqual(policy.decide(value, "exec", "exec")[0], "deny")
        for value in ("{", [], False, {"preset": []}, {"preset": "missing"},
                      {"preset": "custom", "rules": []}, {"preset": "compat", "rules": []}):
            self.assertEqual(policy.decide(value, "read", "read")[0], "deny")
        self.assertEqual(policy.decide({"preset": "compat"}, "exec", "exec")[0], "allow")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        for table in ("task_outbox", "task_actions", "tasks", "approvals", "events", "runs"):
            store._conn.execute("DELETE FROM " + table)
        store._conn.commit()
        self.app_id = store.create_app("test-safety")
        store.update_app(self.app_id, policy={"preset": "compat"}, approvers=["reviewer"])
        self.app = store.get_app(self.app_id)
        self.task, _ = tasks.submit(self.app_id, "chat", "test", speaker_open_id="requester")
        self.scope = taskstore.claim("test-owner", lease_seconds=90)
        self.token = tasks._current.set(self.scope)
        self.rec = runs.start(self.app_id, "chat", "chat", "requester")
        self.rec.emit("run/start", {})
        self.sent = []
        approval.register_answerer(lambda req: self.sent.append(req) or "card-1")

    def tearDown(self):
        runs.end(self.rec, "completed")
        tasks._current.reset(self.token)
        approval.register_answerer(None)

    def call(self, name="mock_read", risk="read", **kwargs):
        return pipeline.ToolCall(id=name + "-1", tool=name, args={}, risk=risk,
                                 app_id=self.app_id, trigger="chat", **kwargs)

    def execute(self, call, fn):
        return pipeline.run_tool(self.rec, call, self.app["policy"], fn, app=self.app)

    def test_malformed_args_never_execute(self):
        called = []
        result = self.execute(self.call(raw_args="not json"), lambda args: called.append(args))
        self.assertIn("denied_by", json.loads(result))
        self.assertFalse(called)

    def test_durable_result_replay_does_not_repeat_side_effect(self):
        called = []
        c = self.call("mock_write", "write")
        first = self.execute(c, lambda args: called.append(args) or {"ok": True})
        replay = self.execute(c, lambda args: self.fail("replay executed write"))
        self.assertEqual(first, replay)
        self.assertEqual(len(called), 1)

    def test_replay_rechecks_access_without_rewriting_original_evidence(self):
        c = self.call()
        original = self.execute(c, lambda _: {"sensitive": "fixture-value"})
        c.extra["validate"] = lambda _: False
        result = self.execute(c, lambda _: self.fail("reexecuted after revoke"))
        self.assertIn("replay_access", result)
        self.assertNotIn("fixture-value", result)
        action = taskstore.action_get(self.task["id"], c.id)
        self.assertEqual(action["state"], "completed")
        self.assertEqual(action["result"], original)

    def test_untrusted_result_blocks_production_exec(self):
        self.execute(self.call(external=True), lambda _: {"text": "external fixture"})
        result = self.execute(self.call("mock_exec", "exec"), lambda _: self.fail("executed"))
        self.assertIn("untrusted_content", result)

    def test_untrusted_run_allows_only_marked_sandbox_test(self):
        self.rec.tainted = True
        blocked = self.execute(self.call("code_test", "exec"), lambda _: self.fail("executed"))
        self.assertIn("untrusted_content", blocked)
        c = self.call("code_test", "exec")
        c.id = "sandbox-second"
        c.extra["sandboxed"] = True
        result = self.execute(c, lambda _: {"passed": False})
        self.assertFalse(json.loads(result)["passed"])

    def test_timeout_is_uncertain_and_cannot_repeat(self):
        release, finished = threading.Event(), threading.Event()
        def delayed(_):
            release.wait(2)
            finished.set()
            return {"ok": True}
        c = self.call("mock_write", "write", timeout=0.01)
        try:
            with self.assertRaises(tasks.TaskUncertain):
                self.execute(c, delayed)
            action = taskstore.action_get(self.task["id"], c.id)
            self.assertEqual(action["state"], "uncertain")
            with self.assertRaises(tasks.TaskUncertain):
                self.execute(c, lambda _: self.fail("repeated uncertain write"))
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_approval_suspend_resume_no_duplicate_card_or_write(self):
        store.update_app(self.app_id, policy={"preset": "approval"})
        c = self.call("mock_write", "write")
        with self.assertRaises(tasks.TaskSuspended):
            self.execute(c, lambda _: self.fail("unapproved"))
        self.assertEqual(len(self.sent), 1)
        aid = self.sent[0]["approval_id"]
        # 模拟重启清理：持久审批不得取消。
        approval.cancel_stale()
        self.assertEqual(store.get_approval(aid)["status"], "pending")
        with self.assertRaises(tasks.TaskSuspended):
            self.execute(c, lambda _: self.fail("still pending"))
        self.assertEqual(len(self.sent), 1)
        wrong = approval.decide(aid, True, "reviewer", app_id=self.app_id + 100)
        self.assertFalse(wrong[0])
        self.assertTrue(approval.decide(aid, True, "reviewer", app_id=self.app_id)[0])
        self.assertFalse(approval.decide(aid, True, "reviewer", app_id=self.app_id)[0])
        self.assertTrue(json.loads(self.execute(c, lambda _: {"ok": True}))["ok"])
        self.execute(c, lambda _: self.fail("approved action repeated"))

    def test_prepared_args_survive_resume_and_snapshot_rechecked(self):
        store.update_app(self.app_id, policy={"preset": "approval"})
        c = self.call("mock_write", "write")
        c.extra["prepare"] = lambda a: {"workspace": "fixed", "snapshot": "version1"}
        c.extra["preview"] = lambda a: json.dumps(a)
        with self.assertRaises(tasks.TaskSuspended):
            self.execute(c, lambda _: self.fail("unapproved"))
        self.assertIn("version1", self.sent[0]["preview"])
        approval.decide(self.sent[0]["approval_id"], True, "reviewer", app_id=self.app_id)
        c.extra["prepare"] = lambda _: self.fail("prepared again")
        c.extra["validate"] = lambda a: False
        result = self.execute(c, lambda _: self.fail("changed snapshot executed"))
        self.assertIn("snapshot", result)
        self.assertEqual(taskstore.action_get(self.task["id"], c.id)["args"]["workspace"], "fixed")

    def test_approval_expiry_without_waiting_thread(self):
        store.update_app(self.app_id, policy={"preset": "approval"})
        c = self.call("mock_write", "write")
        with self.assertRaises(tasks.TaskSuspended):
            self.execute(c, lambda _: None)
        aid = self.sent[0]["approval_id"]
        taskstore.action_update(self.task["id"], c.id, payload={"expires_at": time.time() - 1},
                                lease_owner=self.scope["lease_owner"], lease_epoch=self.scope["lease_epoch"])
        self.assertEqual(approval.expire_due(), 1)
        self.assertEqual(store.get_approval(aid)["status"], "expired")
        self.assertFalse(approval.decide(aid, True, "reviewer", app_id=self.app_id)[0])

    def test_rejected_approval_cannot_be_skipped_after_policy_change(self):
        store.update_app(self.app_id, policy={"preset": "approval"})
        c = self.call("mock_write", "write")
        with self.assertRaises(tasks.TaskSuspended):
            self.execute(c, lambda _: self.fail("unapproved"))
        approval.decide(self.sent[0]["approval_id"], False, "reviewer", app_id=self.app_id)
        store.update_app(self.app_id, policy={"preset": "compat"})
        result = self.execute(c, lambda _: self.fail("ignored rejection"))
        self.assertIn("rejected", result)

    def test_external_error_body_also_taints_run(self):
        self.execute(self.call(external=True), lambda _: {"error": "external fixture"})
        self.assertTrue(self.rec.tainted)
        result = self.execute(self.call("mock_exec", "exec"), lambda _: self.fail("executed"))
        self.assertIn("untrusted_content", result)

    def test_intent_audit_failure_prevents_execution(self):
        with patch.object(store, "append_event", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.execute(self.call("mock_write", "write"), lambda _: self.fail("not audited"))


if __name__ == "__main__":
    unittest.main()
