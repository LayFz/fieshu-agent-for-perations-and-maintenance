"""安全日报：数据直出卡片、跨天快照比较、卡片原样投递（不加任务编号头）。全部替身数据。"""
import json
import os
import tempfile
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-daily-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store, taskstore
from sec import daily


def findings(p1=1):
    item = {"priority": "P1", "priority_reason": "有可修复的严重漏洞", "reachability": "公网可达",
            "reachability_note": "公网域名 crm.upmore.net", "image": "harbor.upmore.net/up_ailab/crm:609f7f79",
            "service": "crm", "fixable_cves": {"critical": 1, "high": 2, "app_critical": 1, "app_high": 0},
            "app_packages": [{"package": "phpoffice/phpspreadsheet", "version": "1.22.0", "fixed": "5.6.0, 1.30.3"},
                             {"package": "Django", "version": "4.1.13", "fixed": "5.2.8, 5.1.14, 4.2.26"},
                             {"package": "a-lib", "version": "1.0", "fixed": "1.1"}, {"package": "b-lib", "version": "1.0", "fixed": "1.1"}],
            "fix_packages": [{"package": "libcrypto3", "version": "3.3.5-r0", "fixed": "3.3.7-r0"},
                             {"package": "libssl3", "version": "3.3.5-r0", "fixed": "3.3.7-r0"},
                             {"package": "phpoffice/phpspreadsheet", "version": "1.22.0", "fixed": "5.6.0, 1.30.3"}]}
    return {"items": [item] * p1, "by_priority": {"P1": p1, "P2": 3}, "fetched_at": "2026-10-26 10:12",
            "coverage": {"not_in_harbor": ["dockerhub/library/mysql"], "not_scanned": [], "errors": []},
            "common_fixes": [{"package": "libcrypto3 / libssl3", "critical": 1, "high": 9, "images": 12, "images_public": 11,
                              "fix_to": ["3.3.6-r0", "3.3.7-r2", "3.3.7-r10"], "layer": "base"}],
            "rebuild": {"images": 12, "critical": 1, "high": 30, "public": ["crm"], "advice": "更新 FROM 后重建"},
            "marks_summary": {"applied": 2, "by_status": {"planned": 1, "accepted": 1},
                              "overdue": [{"service": "crm", "package": "guzzlehttp/guzzle", "until": "2026-10-01"}]}}


REPORT = {"intel": {"in_window": 65, "kev_in_window": 0}, "related": [], "related_total": 0}


def texts(card):
    out = []
    for e in card["elements"]:
        if e["tag"] == "div":
            out.append(e["text"]["content"])
        elif e["tag"] == "column_set":
            out += [c["elements"][0]["text"]["content"] for c in e["columns"]]
    return "\n".join(out)


class DailyCardTests(unittest.TestCase):
    def setUp(self):
        store.set_setting("vuln_daily_snapshots", "")

    def test_card_layout_and_wording(self):
        with mock.patch.object(daily, "_image_cves", return_value={}):
            card, snap = daily.build(REPORT, findings(), ["quay.io/argoproj/argocd:v3.4.12"], base_url="http://x")
        body = texts(card)
        self.assertEqual(card["header"]["template"], "orange")
        self.assertIn("今天没有影响我们的新漏洞", body)
        self.assertIn("phpoffice/phpspreadsheet 1.22.0 → 1.30.3", body)               # 取同线最近的修复版本
        self.assertIn("Django 4.1.13 → 4.2.26", body)
        self.assertNotIn("libcrypto3", body.split("批量重建")[0])                     # 基础层只在批量重建里出现
        self.assertIn("批量重建镜像", body)
        self.assertIn("已标记 2 项不再重复提醒（计划修复 1 · 接受风险 1）", body)
        self.assertIn("crm 的 guzzlehttp/guzzle 计划修复已逾期", body)
        self.assertIn("12 个镜像的基础镜像有可修复漏洞（1 严重、30 高危", body)
        self.assertIn("libcrypto3 / libssl3", body.split("批量重建")[1])
        self.assertIn("2 个镜像没有扫描结果", body)
        self.assertNotIn("任务", body)
        self.assertEqual(card["elements"][-1]["actions"][0]["url"], "http://x/#security")
        self.assertEqual(snap["by_priority"]["P1"], 1)

    def test_diff_against_previous_day(self):
        prev = {"date": "2026-10-25", "by_priority": {"P1": 2},
                "images": {"a": {"service": "crm", "priority": "P1", "cves": ["CVE-1", "CVE-2"]},
                           "b": {"service": "oa", "priority": "P1", "cves": ["CVE-3"]}}}
        snap = {"by_priority": {"P1": 1}, "images": {"a": {"service": "crm", "priority": "P1", "cves": ["CVE-2", "CVE-9"]},
                                                     "c": {"service": "lab", "priority": "P2", "cves": ["CVE-9"]}}}
        d = daily.diff(prev, snap)
        self.assertEqual((d["fixed_images"], d["new_images"], d["cleared_cves"], d["new_cves"]), (["oa"], ["lab"], 2, 1))
        self.assertEqual(d["by_priority"]["P1"], -1)
        with mock.patch.object(daily, "_image_cves", return_value=snap["images"]):
            card, _ = daily.build(REPORT, findings(), [], prev=prev)
        self.assertIn("已修好：oa", texts(card))
        self.assertIn("较 2026-10-25：清掉 2 个 CVE", texts(card))

    def test_run_keeps_one_snapshot_per_day_and_compares_with_earlier_day(self):
        store.set_setting("vuln_daily_snapshots", json.dumps({"2026-10-20": {"date": "2026-10-20", "by_priority": {}, "images": {}}}))
        with mock.patch.object(daily.match, "report", return_value=REPORT), \
                mock.patch.object(daily.match, "image_findings", return_value=findings()), \
                mock.patch.object(daily.match, "outside_harbor", return_value=[]), \
                mock.patch.object(daily, "_image_cves", return_value={}), \
                mock.patch.object(daily, "_today", return_value="2026-10-26"):
            daily.run()
            card = daily.run()
        self.assertEqual(sorted(json.loads(store.get_setting("vuln_daily_snapshots"))), ["2026-10-20", "2026-10-26"])
        self.assertIn("较 2026-10-20", texts(card))


class CardDeliveryTests(unittest.TestCase):
    def test_card_result_is_sent_without_task_header(self):
        taskstore.init()
        task, _ = taskstore.submit(1, "chat", daily.INSTRUCTION, source="manual_schedule", trigger="schedule")
        card = {"header": {"title": {"tag": "plain_text", "content": "x"}}, "elements": []}
        with taskstore.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            taskstore._notify(conn, row, "completed", result=taskstore.CARD_PREFIX + json.dumps(card))
        text = store._conn.execute("SELECT text FROM task_outbox WHERE task_id=?", (task["id"],)).fetchone()["text"]
        self.assertTrue(text.startswith(taskstore.CARD_PREFIX))
        from feishu import bot
        sent = mock.Mock(return_value=mock.Mock(success=lambda: True))
        with mock.patch.object(bot, "_create_card", sent), mock.patch.object(bot.store, "get_app", return_value={"id": 1}):
            bot.deliver_task({"app_id": 1, "chat_id": "chat"}, text)
        self.assertEqual(sent.call_args.args[2], card)


if __name__ == "__main__":
    unittest.main()
