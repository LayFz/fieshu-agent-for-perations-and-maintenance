"""GitOps 巡检读取真正的仓库树；无需远程仓库或凭据。"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

_dir = tempfile.TemporaryDirectory(prefix="feishu-gitops-tests-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from dulwich import porcelain
from dulwich.repo import Repo
from ops import gitops_scan


class GitOpsScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gitops-tree-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "source"
        self.root.mkdir()
        (self.root / "apps" / "api").mkdir(parents=True)
        (self.root / "apps" / "ignored").mkdir(parents=True)
        (self.root / "apps" / "api" / "kustomization.yaml").write_text("resources:\n  - secret.yaml\n  - postgres.yaml\n")
        (self.root / "apps" / "api" / "secret.yaml").write_text(
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: api-key\ndata:\n  token: TEST_SECRET_MUST_NOT_APPEAR\n")
        (self.root / "apps" / "api" / "postgres.yaml").write_text(
            "apiVersion: postgresql.cnpg.io/v1\nkind: Cluster\nmetadata:\n  name: api-pg\nspec:\n  instances: 1\n")
        (self.root / "apps" / "ignored" / "secret.yaml").write_text(
            "kind: Secret\nmetadata:\n  name: ignored\ndata:\n  token: OTHER_SECRET_MUST_NOT_APPEAR\n")
        with Repo.init(str(self.root)) as repo:
            porcelain.add(repo, [b"apps/api/kustomization.yaml", b"apps/api/secret.yaml",
                                 b"apps/api/postgres.yaml", b"apps/ignored/secret.yaml"])
            porcelain.commit(repo, b"fixture", author=b"Fixture <fixture@example.test>",
                             committer=b"Fixture <fixture@example.test>")
            self.sha = repo.head().decode()
        self.applications = [{"name": "api", "sync": "Synced", "compared_revision": self.sha,
            "sources": [{"repo_url": "http://192.168.110.44:8929/layfz/gitops.git",
                         "path": "apps/api", "target_revision": "main"}]}]

    def _scan(self, applications=None):
        def clone(_config, destination):
            shutil.copytree(self.root, destination, dirs_exist_ok=True)
        with mock.patch.object(gitops_scan, "configured", return_value=[{
                "project": "layfz/gitops", "url": "ssh://git@gitlab.example:10022/layfz/gitops.git", "branch": "main"}]), \
             mock.patch.object(gitops_scan, "_clone", side_effect=clone), \
             mock.patch.object(gitops_scan.gitlab, "config", return_value={"token": ""}):
            return gitops_scan.scan("layfz/gitops", applications if applications is not None else self.applications)

    def test_argo_source_mapping_only_scans_active_path_and_omits_secret_value(self):
        result = self._scan()
        self.assertEqual(result["sources"][0]["status"], "ok")
        self.assertEqual(result["summary"]["application_paths"], ["apps/api"])
        self.assertEqual(result["summary"]["revision"], self.sha)
        self.assertEqual(result["summary"]["native_secrets_with_data"], 1)
        self.assertEqual({f["title"] for f in result["findings"]},
                         {"GitOps 跟踪了原生 Secret 数据", "CNPG 配置未声明托管备份"})
        serialized = json.dumps(result)
        self.assertNotIn("TEST_SECRET_MUST_NOT_APPEAR", serialized)
        self.assertNotIn("OTHER_SECRET_MUST_NOT_APPEAR", serialized)
        self.assertNotIn("ignored", serialized)

    def test_mismatched_compared_revision_is_not_claimed_active(self):
        apps = [{**self.applications[0], "compared_revision": "0" * 40}]
        result = self._scan(apps)
        self.assertEqual(result["sources"][0]["status"], "partial")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["summary"]["unverified_paths"], ["apps/api"])

    def test_no_live_argo_paths_fails_closed(self):
        result = self._scan([])
        self.assertEqual(result["sources"][0]["status"], "error")
        self.assertFalse(result["summary"])

    def test_ssh_repository_config_rejects_embedded_password_and_wrong_path(self):
        good = {"project": "layfz/gitops", "url": "ssh://git@gitlab.example:10022/layfz/gitops.git", "branch": "main"}
        self.assertEqual(gitops_scan.validate_repository(good), good)
        with self.assertRaises(ValueError):
            gitops_scan.validate_repository({**good, "url": "ssh://git:password@gitlab.example/layfz/gitops.git"})
        with self.assertRaises(ValueError):
            gitops_scan.validate_repository({**good, "url": "ssh://git@gitlab.example/other/repo.git"})
        with self.assertRaises(ValueError):
            gitops_scan.validate_repository({**good, "branch": "--upload-pack=evil"})


if __name__ == "__main__":
    unittest.main()
