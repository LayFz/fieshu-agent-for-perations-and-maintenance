"""工作机操作队列与 runner 接口：鉴权、往返、离线、错 runner 回传、授权与污染 guard。不连接真实工作机。"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-box-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from fastapi.testclient import TestClient

from core import boxstore, store, taskstore
from harness import box, policy
from harness.pipeline import ToolCall
from llm import authorization, tools
from web import admin


class BoxTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for table in ("box_ops", "box_runners", "apps"):
                conn.execute("DELETE FROM " + table)
        self.token = boxstore.create_runner("default")
        self.client = TestClient(admin.app)
        self.h = {"Authorization": f"Bearer {self.token}"}

    def fake_runner(self, handle, rounds=1, token=None):
        """模拟工作机：领取 rounds 个操作，按 handle(op) 回传。"""
        h = {"Authorization": f"Bearer {token or self.token}"}
        seen = []

        def run():
            c = TestClient(admin.app)
            for _ in range(rounds):
                r = c.post("/runner/v1/claim", headers=h, json={"info": {"version": "test"}})
                if r.status_code != 200:
                    continue
                op = r.json()
                seen.append(op)
                ok, result = handle(op)
                c.post(f"/runner/v1/ops/{op['id']}", headers=h, json={"ok": ok, "result": result})
        t = threading.Thread(target=run, daemon=True)
        return t, seen

    def test_runner_api_requires_valid_token(self):
        self.assertEqual(self.client.post("/runner/v1/claim").status_code, 401)
        self.assertEqual(self.client.post("/runner/v1/claim", headers={"Authorization": "Bearer box_wrong"}).status_code, 401)
        self.assertEqual(boxstore.authenticate(self.token), "default")
        new = boxstore.create_runner("default")          # 轮换后旧令牌失效
        self.assertIsNone(boxstore.authenticate(self.token))
        self.assertEqual(boxstore.authenticate(new), "default")

    def test_round_trip_through_long_poll(self):
        boxstore.touch("default")
        t, seen = self.fake_runner(lambda op: (True, {"stdout": "hi " + op["args"]["cmd"], "exit_code": 0}))
        t.start()
        result = box.call("shell", {"cmd": "echo"}, timeout=20)
        t.join(5)
        self.assertEqual(result, {"stdout": "hi echo", "exit_code": 0})
        self.assertEqual(seen[0]["op"], "shell")
        self.assertTrue(boxstore.get_runner("default")["online"])
        self.assertEqual(boxstore.get_runner("default")["info"]["version"], "test")

    def test_offline_runner_fails_fast(self):
        start = time.monotonic()
        with self.assertRaises(box.BoxError):
            box.call("shell", {"cmd": "true"}, timeout=30)
        self.assertLess(time.monotonic() - start, 2)
        with self.assertRaises(box.BoxError):
            box.call("shell", {"cmd": "true"}, runner="missing")

    def test_result_from_other_runner_is_rejected(self):
        other = boxstore.create_runner("other")
        op_id = boxstore.enqueue("default", "shell", {"cmd": "id"}, 30)
        self.assertEqual(boxstore.claim("default")["id"], op_id)
        r = self.client.post(f"/runner/v1/ops/{op_id}", headers={"Authorization": f"Bearer {other}"},
                             json={"ok": True, "result": {"stdout": "forged"}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(boxstore.get(op_id)["status"], "claimed")

    def test_runner_error_and_removal(self):
        boxstore.touch("default")
        t, _ = self.fake_runner(lambda op: (False, {"error": "boom"}))
        t.start()
        self.assertEqual(box.call("browser.open", {"url": "https://example.com"}, timeout=20), {"error": "boom"})
        t.join(5)
        op_id = boxstore.enqueue("default", "shell", {"cmd": "sleep 1"}, 30)
        boxstore.delete_runner("default")
        self.assertEqual(boxstore.get(op_id)["status"], "failed")

    def test_needs_human_hint_and_tool_mapping(self):
        boxstore.touch("default")
        store.set_setting("box_novnc_url", "https://box.example/vnc.html")
        t, seen = self.fake_runner(lambda op: (True, {"title": "请稍候…", "needs_human": "页面要求人机验证"}))
        t.start()
        out = tools._box_tool("box_browser", {"action": "open", "url": "https://www.npmjs.com/"})
        t.join(5)
        self.assertEqual(seen[0]["op"], "browser.open")
        self.assertIn("https://box.example/vnc.html", out["needs_human"])
        self.assertIn("error", tools._box_tool("box_browser", {"action": "delete"}))

    def test_authorization_and_guard(self):
        app_id = store.create_app("工作机测试")
        ctx = {"app_id": app_id, "chat_id": "c"}
        with self.assertRaises(PermissionError):
            authorization.prepare("box_shell", {"cmd": "id"}, ctx)
        store.update_app(app_id, resources={"box": ["default"]})
        self.assertEqual(authorization.prepare("box_shell", {"cmd": "id"}, ctx)["box"], "default")
        with self.assertRaises(PermissionError):
            authorization.prepare("box_shell", {"cmd": "id", "box": "other"}, ctx)
        tainted = ToolCall(id="1", tool="box_shell", args={}, risk="local", extra={"tainted": True, "sandboxed": True})
        self.assertIsNone(policy._guard_untrusted(tainted))
        self.assertEqual(tools.meta_for("box_shell", {})["risk"], "local")
        self.assertTrue(tools.meta_for("box_browser", {})["external"])


if __name__ == "__main__":
    unittest.main()
