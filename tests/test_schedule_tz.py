"""定时任务钟点按北京时间解释，不随宿主机时区。"""
import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-schedule-tz-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store


class ScheduleTimezoneTests(unittest.TestCase):
    def test_daily_uses_shanghai_wall_clock(self):
        base = datetime(2026, 10, 26, 3, 0, tzinfo=ZoneInfo("UTC")).timestamp()       # 北京 11:00
        nxt = datetime.fromtimestamp(store.next_run("daily", "09:30", base), ZoneInfo("Asia/Shanghai"))
        self.assertEqual((nxt.day, nxt.hour, nxt.minute), (27, 9, 30))
        early = datetime(2026, 10, 25, 23, 0, tzinfo=ZoneInfo("UTC")).timestamp()     # 北京 07:00
        nxt = datetime.fromtimestamp(store.next_run("daily", "09:30", early), ZoneInfo("Asia/Shanghai"))
        self.assertEqual((nxt.day, nxt.hour), (26, 9))

    def test_weekly_weekday_in_shanghai(self):
        base = datetime(2026, 10, 25, 20, 0, tzinfo=ZoneInfo("UTC")).timestamp()      # 北京周一 04:00
        nxt = datetime.fromtimestamp(store.next_run("weekly", "1@09:00", base), ZoneInfo("Asia/Shanghai"))
        self.assertEqual((nxt.isoweekday(), nxt.day, nxt.hour), (1, 26, 9))


class ReportToolScopeTests(unittest.TestCase):
    def test_daily_report_hides_code_and_fix_plan(self):
        from harness import workflows
        daily = workflows.hidden_tools("auto", "生成今日安全漏洞日报：用 vuln_report(days=1, new_only=true)")
        self.assertIn("code_open", daily)
        self.assertIn("vuln_fix_plan", daily)
        self.assertNotIn("vuln_report", daily)
        self.assertEqual(workflows.hidden_tools("auto", "把 redis 漏洞修复并提 MR"), set())
        self.assertEqual(workflows.hidden_tools("auto", "看下集群 pod 状态"), set())
        text = "生成今日安全漏洞日报：vuln_report，写清修复版本，再概括通用修复（common_fixes）"
        self.assertIn("code_open", workflows.hidden_tools("auto", text))
        self.assertFalse(workflows.wants_repair(text))
        self.assertIsNone(workflows.verify("auto", text, [{"tool": "vuln_report", "result": {"related": []}}]))

    def test_scheduled_task_has_no_received_ack(self):
        from core import taskstore
        taskstore.init()
        task, _ = taskstore.submit(1, "chat", "日报", source="manual_schedule", trigger="schedule")
        rows = store._conn.execute("SELECT dedupe_key FROM task_outbox WHERE task_id=?", (task["id"],)).fetchall()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
