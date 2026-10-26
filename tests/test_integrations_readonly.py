"""只读扫描接口契约；所有 HTTP 和连接配置均为替身。"""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import requests

_TEST_DIR = tempfile.TemporaryDirectory(prefix="feishu-readonly-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _TEST_DIR.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from ops import argocd, gitlab


SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "abcdef0123456789abcdef0123456789abcdef01"
SECRET = "fixture-private-token-do-not-return"


def response(data=None, status=200, headers=None, invalid=False):
    def body():
        if invalid:
            raise ValueError(SECRET)
        return data
    return SimpleNamespace(status_code=status, json=body, headers=headers or {},
                           text=SECRET, content=SECRET.encode())


class ReadonlyIntegrationsTests(unittest.TestCase):
    def setUp(self):
        self.argocd_config = mock.patch.object(argocd, "config", return_value={
            "url": "https://argo.example", "token": SECRET, "insecure": False})
        self.gitlab_config = mock.patch.object(gitlab, "config", return_value={
            "url": "https://gitlab.example", "token": SECRET})
        self.argocd_config.start()
        self.gitlab_config.start()
        self.addCleanup(mock.patch.stopall)

    def test_permission_errors_do_not_expose_remote_body(self):
        for module in (argocd, gitlab):
            for status in (401, 403, 302, 500):
                with self.subTest(module=module.__name__, status=status), \
                     mock.patch("requests.get", return_value=response(status=status)) as get:
                    data, error = module._get("/fixture")
                    self.assertIsNone(data)
                    self.assertNotIn(SECRET, json.dumps(error))
                    self.assertIn(str(status), error["error"])
                    self.assertEqual(get.call_args.kwargs["timeout"], 20)
                    self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_transport_and_json_failures_never_echo_token(self):
        for module in (argocd, gitlab):
            with self.subTest(module=module.__name__), mock.patch(
                    "requests.get", side_effect=requests.Timeout("url?token=" + SECRET)):
                data, error = module._get("/fixture")
                self.assertIsNone(data)
                self.assertNotIn(SECRET, json.dumps(error))
            with mock.patch("requests.get", return_value=response(invalid=True)):
                data, error = module._get("/fixture")
                self.assertIsNone(data)
                self.assertNotIn(SECRET, json.dumps(error))

    def test_argocd_preserves_compared_operation_and_deployed_revisions(self):
        source = {"repoURL": "https://user:" + SECRET + "@gitlab.example/g/r.git?token=" + SECRET,
                  "path": "deploy", "targetRevision": "main", "helm": {"values": SECRET}}
        app = {"metadata": {"name": "app"}, "spec": {"source": source}, "status": {
            "sync": {"status": "Synced", "revision": SHA, "revisions": [SHA, OTHER_SHA],
                     "comparedTo": {"sources": [source, {"repoURL": "https://charts.example", "chart": "app", "targetRevision": "1.2.3"}],
                                    "destination": {"namespace": "demo"}}},
            "summary": {"images": ["registry.example/app:latest"]},
            "operationState": {"phase": "Succeeded", "startedAt": "2026-01-01T00:00:00Z",
                               "finishedAt": "2026-01-01T00:01:00Z", "operation": {"sync": {"revision": "main"}},
                               "syncResult": {"revision": OTHER_SHA, "revisions": [OTHER_SHA, SHA], "source": source}},
            # 故意乱序，最新历史必须按自增 ID 判断。
            "history": [{"id": 12, "revision": OTHER_SHA, "revisions": [OTHER_SHA, SHA],
                         "sources": [source], "deployedAt": "2026-01-01T00:01:00Z"},
                        {"id": 11, "revision": SHA}]}}
        with mock.patch("requests.get", return_value=response(app)) as get:
            result = argocd.get_app("app")
        self.assertEqual(get.call_args.args[0], "https://argo.example/api/v1/applications/app")
        self.assertEqual(result["revision"], SHA)
        self.assertEqual(result["revisions"], [SHA, OTHER_SHA])
        self.assertEqual(result["last_operation"]["revision"], OTHER_SHA)
        self.assertEqual(result["last_operation"]["revisions"], [OTHER_SHA, SHA])
        self.assertEqual(result["last_operation"]["requested_revision"], "main")
        self.assertEqual(result["latest_deployment"]["id"], 12)
        self.assertEqual(result["latest_deployment"]["revision"], OTHER_SHA)
        self.assertEqual(len(result["compared_to"]["sources"]), 2)
        self.assertEqual(result["images"], ["registry.example/app:latest"])
        self.assertEqual(result["image_evidence"]["kind"], "references")
        self.assertFalse(result["image_evidence"]["runtime_digest_verified"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_argocd_missing_history_is_not_inferred_from_comparison(self):
        with mock.patch("requests.get", return_value=response({"status": {"sync": {"revision": SHA}}})):
            result = argocd.get_app("app")
        self.assertEqual(result["revision"], SHA)
        self.assertIsNone(result["latest_deployment"])
        self.assertIsNone(result["last_operation"]["revision"])
        self.assertEqual(result["deployment_history_count"], 0)

    def test_gitlab_tree_exposes_next_page_and_complete_directory(self):
        entries = [{"path": "src", "type": "tree", "id": SHA, "mode": "040000"},
                   {"path": "readme", "type": "blob", "id": OTHER_SHA, "mode": "100644"}]
        with mock.patch("requests.get", side_effect=[
            response(entries, headers={"X-Next-Page": "2", "X-Total": "3"}),
            response(entries[:1], headers={"X-Next-Page": "", "X-Total": "3"}),
            response(entries, headers={"X-Next-Page": ""})]) as get:
            first = gitlab.tree("group/repo", ref=SHA, per_page=2)
            last = gitlab.tree("group/repo", ref=SHA, page=2, per_page=2)
            full = gitlab.tree("group/repo", ref=SHA)
        self.assertFalse(first["complete"])
        self.assertEqual(first["pagination"]["next_page"], 2)
        self.assertEqual(first["pagination"]["total"], 3)
        self.assertFalse(last["pagination"]["has_more"])
        self.assertFalse(last["complete"])  # 单独第二页不包含整个目录。
        self.assertTrue(full["complete"])
        self.assertEqual(first["entries"][0]["id"], SHA)
        self.assertEqual(get.call_args_list[0].args[0], "https://gitlab.example/api/v4/projects/group%2Frepo/repository/tree")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["ref"], SHA)

    def test_tree_missing_pagination_headers_cannot_claim_full_page_complete(self):
        with mock.patch("requests.get", return_value=response([{"path": "file", "type": "blob"}])):
            result = gitlab.tree("group/repo", per_page=1)
        self.assertFalse(result["complete"])
        self.assertTrue(result["pagination"]["has_more"])
        self.assertEqual(result["pagination"]["source"], "page_size")

    def test_invalid_pagination_never_requests_remote(self):
        with mock.patch("requests.get") as get:
            for kwargs in ({"page": 0}, {"page": -1}, {"page": True}, {"per_page": 101}, {"per_page": 1.5}):
                self.assertIn("error", gitlab.tree("group/repo", **kwargs))
            self.assertIn("error", gitlab.pipelines("group/repo", limit=101))
        get.assert_not_called()

    def test_project_details_selects_safe_metadata_and_full_commit_sha(self):
        project = {"id": 7, "name": "repo", "path_with_namespace": "group/repo", "default_branch": "release/1",
                   "web_url": "https://user:" + SECRET + "@gitlab.example/group/repo?private_token=" + SECRET,
                   "runners_token": SECRET, "import_url": SECRET, "permissions": {"token": SECRET}}
        commit = {"id": SHA, "short_id": SHA[:8], "parent_ids": [OTHER_SHA], "created_at": "2026-01-01T00:00:00Z",
                  "author_email": SECRET, "committer_email": SECRET, "message": SECRET, "trailers": {"secret": SECRET},
                  "web_url": "https://gitlab.example/group/repo/-/commit/" + SHA}
        with mock.patch("requests.get", side_effect=[response(project), response({"commit": commit})]) as get:
            result = gitlab.project_details("group/repo")
        self.assertEqual(result["default_branch"], "release/1")
        self.assertEqual(result["default_branch_commit"]["id"], SHA)
        self.assertEqual(result["default_branch_commit"]["parent_ids"], [OTHER_SHA])
        self.assertEqual(result["commit_status"], "available")
        self.assertEqual(get.call_args_list[1].args[0],
                         "https://gitlab.example/api/v4/projects/group%2Frepo/repository/branches/release%2F1")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("author_email", result["default_branch_commit"])

    def test_project_without_branch_or_branch_permission_is_explicit(self):
        with mock.patch("requests.get", return_value=response({"id": 1, "default_branch": None})) as get:
            result = gitlab.project_details(1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result["commit_status"], "no_default_branch")
        with mock.patch("requests.get", side_effect=[response({"default_branch": "main"}), response(status=403)]):
            result = gitlab.project_details(1)
        self.assertIsNone(result["default_branch_commit"])
        self.assertEqual(result["commit_status"], "unavailable")
        self.assertIn("error", result)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_pipelines_are_latest_readonly_safe_metadata_with_full_sha(self):
        data = [{"id": 91, "status": "success", "ref": "main", "sha": SHA,
                 "web_url": "https://gitlab.example/g/r/-/pipelines/91", "created_at": "2026-01-01T00:00:00Z",
                 "updated_at": "2026-01-01T00:05:00Z", "variables": [{"value": SECRET}], "user": {"email": SECRET}}]
        with mock.patch("requests.get", return_value=response(data, headers={"X-Next-Page": ""})) as get, \
             mock.patch("requests.post") as post, mock.patch("requests.put") as put, mock.patch("requests.delete") as delete:
            result = gitlab.pipelines("g/r", ref="main", limit=3)
        self.assertEqual(get.call_args.kwargs["params"],
                         {"ref": "main", "page": 1, "per_page": 3, "order_by": "id", "sort": "desc"})
        self.assertEqual(result["pipelines"][0]["sha"], SHA)
        self.assertTrue(result["complete"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(set(result["pipelines"][0]), {"id", "status", "ref", "sha", "web_url", "created_at", "updated_at"})
        post.assert_not_called()
        put.assert_not_called()
        delete.assert_not_called()

    def test_project_scoped_token_is_used_only_for_its_project(self):
        with mock.patch.object(gitlab, "project_tokens", return_value={"group/repo": "project-read-api"}), \
             mock.patch("requests.get", return_value=response([], headers={"X-Next-Page": ""})) as get:
            gitlab.pipelines("group/repo", limit=1)
            gitlab.pipelines("other/repo", limit=1)
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["PRIVATE-TOKEN"], "project-read-api")
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["PRIVATE-TOKEN"], SECRET)


if __name__ == "__main__":
    unittest.main()
