"""持久任务契约测试；只用临时数据库与假执行器，不访问外部服务。"""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-tasks-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store, taskstore
from harness import tasks


class TaskRuntimeTests(unittest.TestCase):
    def setUp(self):
        tasks.stop(timeout=1)
        # 明确只允许测试库；不清理项目 data/agent.db。
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for table in ("task_outbox", "task_actions", "tasks", "approvals", "schedules", "apps"):
                conn.execute("DELETE FROM " + table)
        self.app = store.create_app("测试应用")
        self.sent = []
        tasks.register_sender(lambda task, text: self.sent.append((task["id"], text)))
        tasks.register_executor(lambda task: "完成")
        self.expiry = mock.patch("harness.approval.expire_due", return_value=0, create=True)
        self.expiry.start()

    def tearDown(self):
        tasks.stop(timeout=1)
        self.expiry.stop()
        tasks.register_sender(None)
        tasks.register_executor(None)

    def submit(self, event="event-1", chat="chat-1"):
        return tasks.submit(self.app, chat, "测试目标", event_id=event)

    def claim(self, task):
        return taskstore.claim("test-worker", lease_seconds=30)

    @staticmethod
    def fence(task):
        return {"lease_owner": task["lease_owner"], "lease_epoch": task["lease_epoch"]}

    def test_submission_deduplicates_durably(self):
        first, created = self.submit()
        second, again = self.submit()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(tasks.list_tasks()), 1)

    def test_concurrent_claims_are_unique_and_session_serial(self):
        first, _ = self.submit("first")
        self.submit("second")
        self.submit("other", "other-chat")
        barrier = threading.Barrier(3)
        claimed = []
        def claim():
            try:
                barrier.wait()
                result = taskstore.claim(threading.current_thread().name)
                if result:
                    claimed.append(result)
            finally:
                tasks._close_connection()
        threads = [threading.Thread(target=claim) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({t["id"] for t in claimed}), 2)
        self.assertIn(first["id"], {t["id"] for t in claimed})
        self.assertEqual(len({t["chat_id"] for t in claimed}), 2)

    def test_read_interruption_resumes_checkpoint(self):
        task, _ = self.submit()
        claimed = self.claim(task)
        taskstore.checkpoint(task["id"], claimed["lease_owner"], claimed["lease_epoch"], {"step": 2})
        taskstore.action_prepare(task["id"], "read", "logs", {}, "read", **self.fence(claimed))
        taskstore.action_update(task["id"], "read", state="running", **self.fence(claimed))
        taskstore.recover_expired(now=time.time() + 60)
        resumed = taskstore.claim("another-worker")
        self.assertEqual(resumed["id"], task["id"])
        self.assertEqual(resumed["checkpoint"], {"step": 2})
        self.assertGreater(resumed["lease_epoch"], claimed["lease_epoch"])

    def test_interrupted_write_is_not_retried(self):
        task, _ = self.submit()
        claimed = self.claim(task)
        taskstore.action_prepare(task["id"], "write", "mr", {}, "write", **self.fence(claimed))
        taskstore.action_update(task["id"], "write", state="running", **self.fence(claimed))
        taskstore.recover_expired(now=time.time() + 60)
        self.assertEqual(tasks.get(task["id"])["status"], "needs_attention")
        self.assertFalse(tasks.resume(task["id"]))
        self.assertIsNone(taskstore.claim("new-worker"))
        self.assertEqual(taskstore.action_get(task["id"], "write")["state"], "uncertain")

    def test_old_epoch_cannot_write_checkpoint_or_action(self):
        task, _ = self.submit()
        old = self.claim(task)
        taskstore.recover_expired(now=time.time() + 60)
        new = taskstore.claim("new")
        with self.assertRaises(taskstore.LeaseLost):
            taskstore.checkpoint(task["id"], old["lease_owner"], old["lease_epoch"], {"stale": True})
        with self.assertRaises(taskstore.LeaseLost):
            taskstore.action_prepare(task["id"], "x", "tool", {}, "write", **self.fence(old))
        self.assertEqual(tasks.get(task["id"])["lease_owner"], new["lease_owner"])

    def test_action_key_cannot_change_args(self):
        task, _ = self.submit()
        claimed = self.claim(task)
        taskstore.action_prepare(task["id"], "x", "tool", {"target": "a"}, "write", **self.fence(claimed))
        with self.assertRaises(ValueError):
            taskstore.action_prepare(task["id"], "x", "tool", {"target": "b"}, "write", **self.fence(claimed))

    def test_notification_retry_does_not_repeat_execution(self):
        task, _ = self.submit()
        executed = []
        tasks.register_executor(lambda _: executed.append(1) or "执行成功")
        def unavailable(*args):
            raise OSError("模拟断网")
        tasks.register_sender(unavailable)
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "completed")
        with taskstore.transaction() as conn:
            conn.execute("UPDATE task_outbox SET available_at=0")
        tasks.register_sender(lambda task, text: self.sent.append(text))
        tasks.tick()
        self.assertEqual(executed, [1])
        self.assertEqual(len(self.sent), 2)  # 接收回执与完成通知均重试，不重跑任务。

    def test_cancel_scope_and_running_cancellation(self):
        task, _ = self.submit()
        self.assertFalse(tasks.cancel(task["id"], app_id=self.app, chat_id="other"))
        claimed = self.claim(task)
        self.assertTrue(tasks.cancel(task["id"], app_id=self.app, chat_id="chat-1"))
        self.assertEqual(tasks.get(task["id"])["status"], "cancelling")
        token = tasks._current.set(claimed)
        try:
            with self.assertRaises(tasks.TaskCancelled):
                tasks.check_active()
        finally:
            tasks._current.reset(token)
        taskstore.finish(task["id"], claimed["lease_owner"], claimed["lease_epoch"], "completed", result="不应成功")
        self.assertEqual(tasks.get(task["id"])["status"], "cancelled")

    def test_approval_callback_before_suspend_is_not_lost(self):
        task, _ = self.submit()
        claimed = self.claim(task)
        tasks.wake(task["id"])
        taskstore.finish(task["id"], claimed["lease_owner"], claimed["lease_epoch"], "waiting_approval")
        self.assertEqual(tasks.get(task["id"])["status"], "queued")

    def test_waiting_task_releases_worker_and_continues(self):
        task, _ = self.submit()
        def execute(task):
            state = tasks.load_checkpoint()
            if not state:
                tasks.checkpoint({"step": "prepared"})
                raise tasks.TaskSuspended(reason="等待审批")
            return "从检查点继续"
        tasks.register_executor(execute)
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "waiting_approval")
        other, _ = self.submit("followup")
        claimed = taskstore.claim("other-worker")
        self.assertEqual(claimed["id"], other["id"])
        taskstore.finish(other["id"], claimed["lease_owner"], claimed["lease_epoch"], "completed")
        tasks.wake(task["id"])
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "completed")
        self.assertEqual(tasks.get(task["id"])["attempts"], 2)

    def test_schedule_enqueue_and_cursor_commit_together(self):
        sid, _ = store.create_schedule(self.app, "chat-1", "巡检", "测试", "interval", "5")
        with taskstore.transaction() as conn:
            conn.execute("UPDATE schedules SET next_run_ts=1 WHERE id=?", (sid,))
        sc = store.get_schedule(sid)
        first, created = tasks.enqueue_schedule(sc, now=time.time())
        second, again = tasks.enqueue_schedule(sc, now=time.time())
        self.assertTrue(created)
        self.assertIsNone(second)
        self.assertFalse(again)
        self.assertGreater(store.get_schedule(sid)["next_run_ts"], 1)
        self.assertEqual(first["trigger"], "schedule")
        self.assertEqual(len(tasks.list_tasks()), 1)

    def test_disabled_app_cannot_submit_or_execute(self):
        task, _ = self.submit()
        store.update_app(self.app, enabled=0)
        with self.assertRaises(ValueError):
            self.submit("new")
        self.assertIsNone(taskstore.claim("worker"))
        self.assertEqual(tasks.get(task["id"])["status"], "queued")

    def test_incomplete_result_can_resume_without_repeating_known_actions(self):
        task, _ = self.submit()
        def incomplete(_):
            tasks.checkpoint({"done": True, "final_answer": "还差复测", "evidence": ["read1"]})
            raise tasks.TaskIncomplete("验收不足")
        tasks.register_executor(incomplete)
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "needs_attention")
        self.assertEqual(row["result"], "还差复测")
        self.assertTrue(tasks.resume(task["id"]))
        self.assertNotIn("done", tasks.get(task["id"])["checkpoint"])
        self.assertEqual(tasks.get(task["id"])["checkpoint"]["evidence"], ["read1"])

    def test_resume_appends_input_but_preserves_pending_actions(self):
        task, _ = self.submit()
        claimed = self.claim(task)
        cp = {"messages": [{"role": "user", "content": "原目标"}], "pending": []}
        taskstore.checkpoint(task["id"], claimed["lease_owner"], claimed["lease_epoch"], cp)
        taskstore.finish(task["id"], claimed["lease_owner"], claimed["lease_epoch"], "waiting_input")
        self.assertTrue(tasks.resume(task["id"], instruction="只检查 staging"))
        self.assertEqual(tasks.get(task["id"])["checkpoint"]["messages"][-1]["content"], "只检查 staging")
        claimed = taskstore.claim("next")
        cp["pending"] = [{"tool": "write"}]
        taskstore.checkpoint(task["id"], claimed["lease_owner"], claimed["lease_epoch"], cp)
        taskstore.finish(task["id"], claimed["lease_owner"], claimed["lease_epoch"], "failed")
        self.assertFalse(tasks.resume(task["id"], instruction="更改审批目标"))

    def test_repeated_crashes_stop_at_recovery_limit(self):
        task, _ = self.submit()
        for attempt in range(2):
            self.assertIsNotNone(taskstore.claim("crashing-worker"))
            taskstore.recover_expired(now=time.time() + 120, max_attempts=2)
        row = tasks.get(task["id"])
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["status"], "needs_attention")
        self.assertIsNone(taskstore.claim("should-not-run"))


if __name__ == "__main__":
    unittest.main()
