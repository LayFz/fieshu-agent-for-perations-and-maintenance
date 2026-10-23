"""项目级 GitLab API 凭据由后台保存，读接口只显示已配置状态。"""
import json
import os
import tempfile
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-project-tokens-tests-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from fastapi.testclient import TestClient
from core import store
from ops import gitlab
from web import admin


class GitLabProjectTokenTests(unittest.TestCase):
    def test_project_token_is_scoped_and_never_returned(self):
        old_repos = store.get_setting("gitops_repositories", "[]")
        old_tokens = store.get_setting("gitlab_project_tokens", "{}")
        self.addCleanup(store.set_setting, "gitops_repositories", old_repos)
        self.addCleanup(store.set_setting, "gitlab_project_tokens", old_tokens)
        store.set_setting("gitops_repositories", json.dumps([{
            "project": "fixture/gitops", "url": "ssh://git@gitlab.example/fixture/gitops.git", "branch": "main"}]))
        store.set_setting("gitlab_project_tokens", "{}")
        with TestClient(admin.app) as client:
            self.assertEqual(client.get("/api/connections/gitlab-project-tokens").status_code, 401)
            with mock.patch.object(admin, "_auth", return_value="test-admin"):
                secret = "fixture-project-read-api-token"
                bad = client.put("/api/connections/gitlab-project-tokens", json={
                    "project": "other/project", "token": secret})
                self.assertEqual(bad.status_code, 400)
                saved = client.put("/api/connections/gitlab-project-tokens", json={
                    "project": "fixture/gitops", "token": secret})
                self.assertEqual(saved.status_code, 200, saved.text)
                listed = client.get("/api/connections/gitlab-project-tokens")
                self.assertEqual(listed.json()["projects"], [{"project": "fixture/gitops", "token_set": True}])
                self.assertNotIn(secret, saved.text + listed.text)
                self.assertEqual(gitlab.project_tokens()["fixture/gitops"], secret)


if __name__ == "__main__":
    unittest.main()
