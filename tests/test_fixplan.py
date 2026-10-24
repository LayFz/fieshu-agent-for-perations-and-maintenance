"""修复方案：tag 推荐、停止维护提示、gitops 引用定位、项目权限过滤、自研镜像分流。外部 API 全部替身。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-fixplan-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import assetstore, store, taskstore
from ops import registry
from sec import fixplan


class RegistryTests(unittest.TestCase):
    def test_suggest(self):
        self.assertEqual(registry.suggest("7-alpine", ["8.2.1-alpine", "7.4.11-alpine", "7.4-alpine", "7.4.11-alpine3.21"])[0],
                         "7.4.11-alpine")
        self.assertEqual(registry.suggest("3-management-alpine", ["3.13.7-management-alpine", "3.13.7-alpine"])[0],
                         "3.13.7-management-alpine")
        self.assertEqual(registry.suggest("16.4", ["16.10", "17.2", "16.9-alpine"])[0], "16.10")
        self.assertIsNone(registry.suggest("latest", ["7.4.11"])[0])
        self.assertIsNone(registry.suggest("7.4.11", ["6.2.1"])[0])

    def test_hub_repo(self):
        self.assertEqual(registry.hub_repo("harbor.upmore.net/dockerhub/library/mysql:5.7", ["harbor.upmore.net"]), "library/mysql")
        self.assertEqual(registry.hub_repo("redis:7-alpine"), "library/redis")
        self.assertEqual(registry.hub_repo("bitnami/redis:7.2"), "bitnami/redis")
        self.assertIsNone(registry.hub_repo("quay.io/argoproj/argocd:v3.4.12"))
        self.assertIsNone(registry.hub_repo("harbor.upmore.net/up-oa/web:1a2b3c4d", ["harbor.upmore.net"]))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for t in ("assets", "asset_sources", "image_scans", "image_vulns", "vulns", "code_repos"):
                conn.execute("DELETE FROM " + t)
        store.create_code_repo(name="gitops", url="https://gitlab.upmore.net/layfz/gitops.git", enabled=1)
        assetstore.replace("cluster:upmore", [
            {"kind": "image", "component": "mysql", "version": "5.7", "image": "harbor.upmore.net/dockerhub/library/mysql:5.7",
             "cluster": "upmore", "namespace": "crm", "workload": "StatefulSet/crm-mysql · 容器 mysql",
             "exposure": "cluster", "exposure_note": "仅集群内 Service crm-mysql"},
            {"kind": "service", "component": "crm", "image": "harbor.upmore.net/crm/web:1a2b3c4d",
             "cluster": "upmore", "namespace": "crm", "workload": "Deployment/crm-web · 容器 web",
             "exposure": "public", "exposure_note": "公网域名 crm.upmore.net"}])
        vuln = {"cve": "CVE-2026-1", "package": "openssl", "version": "1.1", "fixed": "1.2", "severity": "Critical", "cvss": 9.8}
        assetstore.replace_scans([
            {"project": "dockerhub", "repo": "library/mysql", "reference": "5.7", "image": "harbor.upmore.net/dockerhub/library/mysql:5.7",
             "status": "Success", "summary": {"Critical": 1}, "vulns": [vuln]},
            {"project": "crm", "repo": "web", "reference": "sha256:a", "image": "harbor.upmore.net/crm/web:1a2b3c4d",
             "status": "Success", "summary": {"Critical": 1}, "vulns": [dict(vuln, package="pillow", fixed="13.0.0")]}])

    def search(self, path, params=None, project=None, **kw):
        if params["search"] == "harbor.upmore.net/dockerhub/library/mysql:5.7":
            return [{"path": "apps/crm/mysql.yaml", "startline": 36, "data": "image: harbor.upmore.net/dockerhub/library/mysql:5.7"},
                    {"path": "README.md", "startline": 1, "data": "harbor.upmore.net/dockerhub/library/mysql:5.7"}], None
        return [], None

    def test_plan_third_party_and_self_built(self):
        with mock.patch.object(fixplan.gitlab, "_get", side_effect=self.search), \
                mock.patch.object(fixplan.registry, "tags", return_value=["5.7.44", "8.4.3", "5.7"]), \
                mock.patch.object(fixplan.asset_scan, "own_registries", return_value=["harbor.upmore.net"]):
            out = fixplan.plan()
        tp = out["third_party"][0]
        self.assertEqual((tp["component"], tp["current_tag"], tp["suggested_tag"]), ("mysql", "5.7", "5.7.44"))
        self.assertIn("停止维护", tp["eol"])
        self.assertEqual([r["path"] for r in tp["gitops_refs"]], ["apps/crm/mysql.yaml"])   # README 不算
        self.assertIn("code_propose_mr", tp["apply"])
        self.assertEqual(tp["reachability"], "仅集群内部可达")
        self.assertEqual(out["self_built"][0]["service"], "crm")
        self.assertEqual(out["self_built"][0]["fix_packages"][0]["package"], "pillow")

    def test_project_permission_filter(self):
        with mock.patch.object(fixplan.gitlab, "_get", side_effect=self.search) as g, \
                mock.patch.object(fixplan.registry, "tags", return_value=["5.7.44"]), \
                mock.patch.object(fixplan.asset_scan, "own_registries", return_value=["harbor.upmore.net"]):
            out = fixplan.plan(allowed_project=lambda p: False)
        g.assert_not_called()
        self.assertEqual(out["third_party"][0]["gitops_refs"], [])
        self.assertIn("需人工定位", out["third_party"][0]["apply"])


if __name__ == "__main__":
    unittest.main()
