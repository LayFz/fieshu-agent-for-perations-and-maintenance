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
        self.assertEqual(registry.suggest("3.12-alpine", ["3.13.1-alpine", "3.12.8-alpine"], keep=2)[0], "3.12.8-alpine")
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
        # 未授权的项目：可以按名称列候选，但不能搜索或读取任何仓库内容
        self.assertFalse([c for c in g.call_args_list if "/search" in c.args[0] or "/repository" in c.args[0]])
        self.assertEqual(out["third_party"][0]["gitops_refs"], [])
        self.assertIn("需人工定位", out["third_party"][0]["apply"])


if __name__ == "__main__":
    unittest.main()


from ops import source_repo


class SourceRepoTests(unittest.TestCase):
    OWN = ["harbor.upmore.net"]

    def setUp(self):
        for k in ("image_repo_map", "image_repo_cache"):
            store.set_setting(k, "")
        self.calls = []

    def fake_get(self, path, params=None, raw=False, with_headers=False, project=None):
        self.calls.append(path)
        if path == "/projects":
            return [{"id": 1, "path_with_namespace": "upmore/up-spider"}, {"id": 2, "path_with_namespace": "upmore/spider-web"}], None
        if path.endswith("/repository/commits/1a2b3c4d"):
            return ({"id": "1a2b3c4d" + "0" * 32}, None) if "up-spider" in path else (None, {"error": "404"})
        return None, {"error": "unexpected"}

    def test_resolve_by_commit_and_cache(self):
        with mock.patch.object(source_repo.gitlab, "_get", side_effect=self.fake_get):
            r = source_repo.resolve("harbor.upmore.net/up-spider/web:1a2b3c4d", own_registries=self.OWN)
        self.assertEqual(r["project"], "upmore/up-spider")
        self.assertIn("包含提交 1a2b3c4d", r["how"])
        self.assertEqual(json.loads(store.get_setting("image_repo_cache"))["up-spider/web"], "upmore/up-spider")
        with mock.patch.object(source_repo.gitlab, "_get", side_effect=self.fake_get):
            denied = source_repo.resolve("harbor.upmore.net/up-spider/web:1a2b3c4d", allowed=lambda p: False,
                                         own_registries=self.OWN)
        self.assertIn("授权", denied["error"])
        self.assertIn("不是自家 Harbor", source_repo.resolve("redis:7", own_registries=self.OWN)["error"])
        with mock.patch.object(source_repo.gitlab, "_get", return_value=(None, {"error": "GitLab 还没配置"})):
            r = source_repo.resolve("harbor.upmore.net/crm/web:609f7f79", own_registries=self.OWN)
        self.assertIn("只读令牌", r["error"])

    def test_dependency_file_filter(self):
        tree = [{"type": "blob", "path": p} for p in (
            "Dockerfile", "backend/requirements.txt", "web/package.json", "web/node_modules/x/package.json",
            "src/app.py", "docs/requirements.txt", "deploy/prod.Dockerfile", "go.mod", ".env", "a/b/c/d/e/go.mod")]
        with mock.patch.object(source_repo.gitlab, "_get", return_value=(tree, None)):
            files = source_repo.dependency_files("p", "sha")
        self.assertEqual(files, ["Dockerfile", "backend/requirements.txt", "web/package.json", "deploy/prod.Dockerfile", "go.mod"])

    def test_redact_and_locate(self):
        text = ("Pillow==11.3.0\nDjango>=4.1,<4.2\n--index-url https://user:s3cret@pypi.internal/simple\n"
                "API_KEY=abcdef123\ndjango-redis==5.0\n")
        files = {"requirements.txt": source_repo.redact(text), "go.mod": "require golang.org/x/net v0.20.0\n",
                 "Dockerfile": "FROM python:3.12-alpine AS build\nRUN pip install pillow\nFROM scratch\n"}
        self.assertNotIn("s3cret", files["requirements.txt"])
        self.assertNotIn("abcdef123", files["requirements.txt"])
        self.assertEqual([h["line"] for h in source_repo.locate("pillow", files)], [1])
        self.assertEqual([h["line"] for h in source_repo.locate("Django", files)], [2])          # 不命中 django-redis
        self.assertEqual(source_repo.locate("golang.org/x/net", files)[0]["path"], "go.mod")
        self.assertEqual(source_repo.base_images(files), [{"path": "Dockerfile", "line": 1, "image": "python:3.12-alpine"}])


class SelfBuiltPlanTests(PlanTests):
    def test_self_built_plan_points_to_files_and_lines(self):
        assetstore.replace_scans([
            {"project": "crm", "repo": "web", "reference": "sha256:a", "image": "harbor.upmore.net/crm/web:1a2b3c4d",
             "status": "Success", "summary": {"Critical": 2},
             "vulns": [{"cve": "CVE-1", "package": "pillow", "version": "11.3.0", "fixed": "13.0.0", "severity": "Critical"},
                       {"cve": "CVE-2", "package": "libssl3", "version": "3.3.5-r0", "fixed": "3.3.7-r0", "severity": "Critical"}]}])
        files = {"requirements.txt": "Pillow==11.3.0\nDjango==4.2\n", "Dockerfile": "FROM python:3.12-alpine\nCOPY . .\n"}
        store.set_setting("source_repo_read", "1")
        self.addCleanup(store.set_setting, "source_repo_read", "")
        with mock.patch.object(fixplan.source_repo, "resolve",
                               return_value={"project": "upmore/crm", "commit": "1a2b3c4d" + "0" * 32, "how": "按名称"}), \
                mock.patch.object(fixplan.source_repo, "dependency_files", return_value=list(files)), \
                mock.patch.object(fixplan.source_repo, "read_files", return_value=files), \
                mock.patch.object(fixplan.registry, "tags", return_value=["3.12.8-alpine", "3.13.1-alpine"]), \
                mock.patch.object(fixplan.gitlab, "_get", return_value=([], None)), \
                mock.patch.object(fixplan.asset_scan, "own_registries", return_value=["harbor.upmore.net"]):
            item = fixplan.plan()["self_built"][0]
        self.assertEqual(item["source"]["project"], "upmore/crm")
        dep = item["dependency_changes"][0]
        self.assertEqual((dep["package"], dep["fix_to"], dep["locations"][0]["path"], dep["locations"][0]["line"]),
                         ("pillow", "13.0.0", "requirements.txt", 1))
        self.assertEqual(item["base_image"]["system_packages"], ["libssl3 3.3.5-r0 → 3.3.7-r0"])
        self.assertEqual(item["base_image"]["from_lines"][0]["suggested"], "3.12.8-alpine")


class LocateWithoutRepoTests(PlanTests):
    def test_default_does_not_touch_business_repo(self):
        assetstore.replace_scans([
            {"project": "crm", "repo": "web", "reference": "sha256:a", "image": "harbor.upmore.net/crm/web:1a2b3c4d",
             "status": "Success", "summary": {"Critical": 3},
             "vulns": [{"cve": "CVE-1", "package": "topthink/framework", "version": "6.0.1", "fixed": "6.0.14", "severity": "Critical"},
                       {"cve": "CVE-2", "package": "libssl3", "version": "3.3.5-r0", "fixed": "3.3.7-r0", "severity": "Critical"},
                       {"cve": "CVE-3", "package": "stdlib", "version": "v1.22.1", "fixed": "1.22.9", "severity": "Critical"}]}])
        with mock.patch.object(fixplan.source_repo, "resolve") as resolve, \
                mock.patch.object(fixplan.registry, "tags", return_value=[]), \
                mock.patch.object(fixplan.gitlab, "_get", return_value=([], None)), \
                mock.patch.object(fixplan.asset_scan, "own_registries", return_value=["harbor.upmore.net"]):
            item = fixplan.plan()["self_built"][0]
        resolve.assert_not_called()
        self.assertEqual(item["source"]["commit_hint"], "1a2b3c4d")
        self.assertEqual([d["package"] for d in item["dependency_changes"]], ["topthink/framework"])
        self.assertIn("composer.json", item["dependency_changes"][0]["where"])
        self.assertEqual(sorted(b["package"] for b in item["base_image"]["packages"]), ["libssl3", "stdlib"])

    def test_manifest_hint(self):
        kinds = {p: fixplan.manifest_hint(p, v)["kind"] for p, v in (
            ("golang.org/x/net", "v0.20.0"), ("github.com/jackc/pgx/v5", "v5.5.0"), ("stdlib", "v1.22.1"),
            ("guzzlehttp/guzzle", "7.4.0"), ("@babel/core", "7.1.0"), ("libc6", "2.36-9+deb12u7"),
            ("busybox", "1.36.1-r29"), ("pillow", "11.3.0"), ("org.yaml:snakeyaml", "1.33"))}
        self.assertEqual(kinds, {"golang.org/x/net": "go", "github.com/jackc/pgx/v5": "go", "stdlib": "runtime",
                                 "guzzlehttp/guzzle": "composer", "@babel/core": "npm", "libc6": "system",
                                 "busybox": "system", "pillow": "app", "org.yaml:snakeyaml": "maven"})
