"""原生 GitLab 只读工具的授权、分派与分页契约；不使用外部连接或真实配置。"""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-readonly-routing-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store
from llm import authorization, tools


class ReadonlyToolRoutingTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch("requests.sessions.Session.request",
                                           side_effect=AssertionError("测试禁止外部 HTTP")))
        self.app = store.create_app("只读分派测试", tools=["gitlab_project", "gitlab_pipelines", "gitlab_tree", "gitlab_projects"])
        store.update_app(self.app, resources={"gitlab_projects": ["team/repo"]})
        self.ctx = {"app_id": self.app, "chat_id": "fixture-chat", "speaker_open_id": "fixture-user"}

    def call(self, name, args):
        prepared = authorization.prepare(name, args, self.ctx)
        authorization.validate(name, prepared, self.ctx)
        return tools.execute(name, prepared, self.ctx)

    def test_project_details_dispatch_uses_exact_authorized_project(self):
        response = {"path": "team/repo", "default_branch_commit": {"id": "a" * 40}}
        with mock.patch.object(tools.ops_gitlab, "project_details", return_value=response) as call:
            self.assertEqual(self.call("gitlab_project", {"project": "team/repo"}), response)
            call.assert_called_once_with("team/repo")

    def test_new_tools_reject_unlisted_project_before_connector(self):
        for name, function in (("gitlab_project", "project_details"), ("gitlab_pipelines", "pipelines")):
            with self.subTest(tool=name), mock.patch.object(tools.ops_gitlab, function) as connector:
                for project in ("other/repo", "team/repo-child", "10", "team%2Frepo", None):
                    with self.subTest(project=project), self.assertRaises(PermissionError):
                        self.call(name, {"project": project})
                connector.assert_not_called()

    def test_exact_project_permission_does_not_grant_listing(self):
        with mock.patch.object(tools.ops_gitlab, "projects") as connector:
            with self.assertRaises(PermissionError):
                self.call("gitlab_projects", {"search": "team"})
            connector.assert_not_called()

    def test_revoked_project_is_rechecked_before_execution(self):
        args = authorization.prepare("gitlab_pipelines", {"project": "team/repo"}, self.ctx)
        store.update_app(self.app, resources={"gitlab_projects": ["other/repo"]})
        with self.assertRaises(PermissionError):
            authorization.validate("gitlab_pipelines", args, self.ctx)

    def test_pipeline_filters_and_default_limit_are_forwarded(self):
        response = {"pipelines": [], "pagination": {"has_more": False}, "complete": True}
        with mock.patch.object(tools.ops_gitlab, "pipelines", return_value=response) as call:
            self.assertEqual(self.call("gitlab_pipelines", {"project": "team/repo", "ref": "release", "limit": 7}), response)
            call.assert_called_once_with("team/repo", "release", 7)
            call.reset_mock()
            self.call("gitlab_pipelines", {"project": "team/repo"})
            call.assert_called_once_with("team/repo", None, 20)

    def test_tree_pagination_and_legacy_defaults_are_forwarded(self):
        response = {"entries": [], "pagination": {"page": 3, "has_more": True}, "complete": False}
        with mock.patch.object(tools.ops_gitlab, "tree", return_value=response) as call:
            self.assertEqual(self.call("gitlab_tree", {"project": "team/repo", "path": "apps",
                                                       "ref": "a" * 40, "page": 3, "per_page": 25}), response)
            call.assert_called_once_with("team/repo", "apps", "a" * 40, page=3, per_page=25)
            call.reset_mock()
            self.call("gitlab_tree", {"project": "team/repo"})
            call.assert_called_once_with("team/repo", "", None, page=1, per_page=100)

    def test_invalid_paging_is_not_silently_replaced_with_defaults(self):
        with mock.patch.object(tools.ops_gitlab, "pipelines", return_value={"error": "invalid limit"}) as call:
            self.call("gitlab_pipelines", {"project": "team/repo", "limit": 0})
            call.assert_called_once_with("team/repo", None, 0)
        with mock.patch.object(tools.ops_gitlab, "tree", return_value={"error": "invalid page"}) as call:
            self.call("gitlab_tree", {"project": "team/repo", "page": 0})
            call.assert_called_once_with("team/repo", "", None, page=0, per_page=100)

    def test_new_tool_metadata_and_schema_keep_readonly_boundary(self):
        schema = {item["function"]["name"]: item["function"] for item in tools.tools_for(
            ["gitlab_project", "gitlab_pipelines", "gitlab_tree", "argocd_apps"])}
        for name in ("gitlab_project", "gitlab_pipelines"):
            self.assertEqual(tools.meta_for(name, {})["risk"], "read")
            self.assertTrue(tools.meta_for(name, {})["external"])
            self.assertIn("project", schema[name]["parameters"]["required"])
        tree = schema["gitlab_tree"]["parameters"]["properties"]
        self.assertEqual(tree["page"]["minimum"], 1)
        self.assertEqual(tree["per_page"]["maximum"], 100)
        self.assertIn("镜像引用", schema["argocd_apps"]["description"])
        self.assertNotIn("【实际运行】", schema["argocd_apps"]["description"])


if __name__ == "__main__":
    unittest.main()
