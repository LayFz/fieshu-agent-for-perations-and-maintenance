"""工作区归属、审批快照和 MR 副作用契约；全部使用临时仓库/数据库。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from dulwich import porcelain

_db = tempfile.TemporaryDirectory(prefix="feishu-workspace-tests-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _db.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store
from feishu import bot
from harness import workspace
from llm import tools as llm_tools


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="feishu-ws-")
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "app.py").write_text("print('before')\n")
        with porcelain.init(str(self.root)) as repo:
            porcelain.add(repo, paths=["app.py"])
            identity = b"Fixture <fixture@example.invalid>"
            porcelain.commit(repo, message=b"fixture", author=identity, committer=identity,
                             no_verify=True, sign=False)
            sha = repo.head().decode("ascii")
        self.app = store.create_app("workspace fixture")
        self.repo_id = store.create_code_repo(name=self.temp.name, url="https://git.invalid/group/repo",
                                             branch="main", write_token="fake-token", test_cmd="", enabled=1)
        self.ws_id = Path(self.temp.name).name
        store.create_workspace(self.ws_id, self.app, "chat-a", self.repo_id, self.temp.name, "main", sha)
        self.ws = store.get_workspace(self.ws_id)
        self.ctx = {"app_id": self.app, "chat_id": "chat-a"}
        self.active = mock.patch.object(workspace, "_active")
        self.active.start()
        self.task = mock.patch.object(workspace, "_task", return_value=None)
        self.task.start()
        self.image = mock.patch.object(workspace.sandbox, "resolve_image", return_value="sha256:fixture")
        self.image.start()

    def tearDown(self):
        self.image.stop()
        self.task.stop()
        self.active.stop()
        self.temp.cleanup()

    def test_explicit_workspace_enforces_chat(self):
        with self.assertRaises(workspace.WorkspaceError):
            workspace.get_workspace(self.ws_id, self.app, "chat-b")
        self.assertEqual(workspace.get_workspace(self.ws_id, self.app, "chat-a")["id"], self.ws_id)

    def test_task_bound_workspace_cannot_be_borrowed(self):
        workspace._save_meta(self.ws_id, task_id="task-a")
        with mock.patch.object(workspace, "_task", return_value={"id": "task-b"}):
            with self.assertRaises(workspace.WorkspaceError):
                workspace.get_workspace(self.ws_id, self.app, "chat-a")
            with self.assertRaises(workspace.WorkspaceError):
                workspace.get_workspace(None, self.app, "chat-a")

    def test_prepare_fixes_target_and_rejects_changed_contents(self):
        action = workspace.prepare_action("code_propose_mr", {"title": "fix"}, self.ctx)
        self.assertEqual(action["workspace"], self.ws_id)
        workspace.validate_action("code_propose_mr", action, self.ctx)
        workspace.write_file(self.ws, "app.py", "print('after')\n")
        with self.assertRaises(workspace.WorkspaceError):
            workspace.validate_action("code_propose_mr", action, self.ctx)

    def test_mr_approval_refuses_truncated_diff(self):
        workspace.write_file(self.ws, "app.py", "x" * 13000)
        with self.assertRaisesRegex(workspace.WorkspaceError, "完整展示"):
            llm_tools.approval_preview("code_propose_mr", {"workspace": self.ws_id}, self.ctx)

    def test_mr_card_contains_entire_approved_diff(self):
        workspace.write_file(self.ws, "app.py", "print('after')\n")
        preview = llm_tools.approval_preview("code_propose_mr", {"workspace": self.ws_id}, self.ctx)
        card = bot._approval_card({"name": "fixture"}, {"id": "approval", "tool": "code_propose_mr",
            "args": {"title": "fix"}, "risk": "write", "reason": "test", "preview": preview})
        rendered = "\n".join(x.get("content", "") for x in card["elements"] if x["tag"] == "markdown")
        self.assertIn("-print('before')", rendered)
        self.assertIn("+print('after')", rendered)
        self.assertNotIn("已截断", rendered)

    def test_gitops_validation_is_bound_to_snapshot_and_rejects_plain_secret(self):
        store.update_code_repo(self.repo_id, validation_kind="gitops")
        manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: example\n"
        workspace.write_file(self.ws, "app.yaml", manifest)
        result = workspace.validate_manifests(self.ws)
        self.assertTrue(result["passed"])
        workspace.write_file(self.ws, "app.yaml", manifest + "data:\n  hello: world\n")
        with self.assertRaisesRegex(workspace.WorkspaceError, "当前代码快照通过 GitOps 静态校验"):
            workspace.propose_mr(self.ws, "test")
        workspace.write_file(self.ws, "app.yaml", "apiVersion: v1\nkind: Secret\nmetadata:\n  name: unsafe\nstringData:\n  password: value\n")
        report = workspace.validate_manifests(self.ws)
        self.assertFalse(report["passed"])
        self.assertIn("原生 Secret", report["errors"][0])
        self.assertNotIn("password: value", json.dumps(report, ensure_ascii=False))

    def test_approval_rejects_changed_test_command(self):
        store.update_code_repo(self.repo_id, test_cmd="python app.py")
        action = workspace.prepare_action("code_test", {}, self.ctx)
        store.update_code_repo(self.repo_id, test_cmd="python other.py")
        with self.assertRaises(workspace.WorkspaceError):
            workspace.validate_action("code_test", action, self.ctx)

    def test_diff_does_not_follow_changed_symlink(self):
        workspace.write_file(self.ws, "app.py", "changed")
        (self.root / "app.py").unlink()
        (self.root / "app.py").symlink_to("/etc/passwd")
        with self.assertRaises(workspace.WorkspaceError):
            workspace.diff(self.ws)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.prepare_action("code_test", {}, self.ctx)

    def test_tests_use_independent_snapshot_without_git_or_database(self):
        store.update_code_repo(self.repo_id, test_cmd="python app.py")
        prepared = workspace.prepare_action("code_test", {}, self.ctx)
        def runner(path, command, **kwargs):
            p = Path(path)
            self.assertNotEqual(p, self.root)
            self.assertFalse((p / ".git").exists())
            self.assertFalse((p / "agent.db").exists())
            self.assertEqual((p / "app.py").read_text(), "print('before')\n")
            (p / "app.py").write_text("test mutated its copy")
            return {"passed": True, "exit_code": 0, "image": "sha256:fixture"}
        with mock.patch.object(workspace.sandbox, "run", side_effect=runner):
            result = workspace.run_tests(self.ws, expected_snapshot=prepared["_snapshot"])
        self.assertTrue(result["passed"])
        self.assertEqual((self.root / "app.py").read_text(), "print('before')\n")
        self.assertEqual(json.loads(workspace._meta(self.ws_id)["test_result"])["snapshot"], prepared["_snapshot"])

    def test_mr_requires_current_test_proof(self):
        store.update_code_repo(self.repo_id, test_cmd="python app.py")
        workspace.write_file(self.ws, "app.py", "fixed\n")
        first = workspace._snapshot(self.ws)
        workspace._save_meta(self.ws_id, test_result=json.dumps({"passed": True, "snapshot": first}))
        workspace.write_file(self.ws, "app.py", "changed after tests\n")
        with mock.patch.object(workspace.requests, "request") as request:
            with self.assertRaises(workspace.WorkspaceError):
                workspace.propose_mr(self.ws, "fix")
        request.assert_not_called()

    def test_unknown_commit_cannot_be_repeated_or_modified(self):
        workspace.write_file(self.ws, "app.py", "fixed\n")
        with mock.patch.object(workspace.requests, "request", side_effect=workspace.requests.Timeout) as request:
            first = workspace.propose_mr(self.ws, "fix")
            second = workspace.propose_mr(self.ws, "another title")
        self.assertTrue(first["outcome_unknown"])
        self.assertTrue(second["outcome_unknown"])
        self.assertEqual(request.call_count, 1)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.write_file(self.ws, "app.py", "new")

    def test_successful_mr_returns_existing_result_on_repeat(self):
        workspace.write_file(self.ws, "app.py", "fixed\n")
        def response(payload):
            return mock.Mock(status_code=200, json=lambda: payload)
        with mock.patch.object(workspace.requests, "request", side_effect=[
            response({"id": "commit-sha"}), response([]),
            response({"web_url": "https://git.invalid/mr/1", "iid": 1})]) as request:
            first = workspace.propose_mr(self.ws, "fix")
            second = workspace.propose_mr(self.ws, "fix")
        self.assertEqual(first["mr_url"], second["mr_url"])
        self.assertTrue(second["reused"])
        self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
