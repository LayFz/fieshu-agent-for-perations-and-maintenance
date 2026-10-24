"""Harbor 镜像扫描接入：报告解析、制品定位、采集、优先级、权限与后台配置。Harbor 全部替身。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-harbor-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from fastapi.testclient import TestClient

from core import assetstore, store, taskstore
from llm import tools
from ops import harbor, image_scan
from sec import match
from web import admin

REPORT = {"application/vnd.security.vulnerability.report; version=1.1": {
    "generated_at": "2026-10-24T10:00:00Z", "scanner": {"name": "Trivy", "version": "v0.60"},
    "vulnerabilities": [
        {"id": "CVE-2026-0001", "package": "openssl", "version": "3.0.13", "fix_version": "3.0.15", "severity": "Critical",
         "description": "openssl bug", "preferred_cvss": {"score_v3": 9.8, "vector_v3": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}},
        {"id": "CVE-2026-0002", "package": "golang.org/x/net", "version": "0.20.0", "fix_version": "0.23.0", "severity": "High",
         "preferred_cvss": {"score_v3": 7.5}},
        {"id": "CVE-2026-0003", "package": "zlib", "version": "1.2.13", "fix_version": "", "severity": "High"},
        {"id": "CVE-2026-0004", "package": "busybox", "version": "1.36.1", "fix_version": "1.36.1-r1", "severity": "Low"},
    ]}}


def art(status="Success", summary=None):
    so = {"scan_status": status, "scanner": {"name": "Trivy"},
          "summary": {"summary": summary or {"Critical": 1, "High": 2, "Low": 1}, "total": 4, "fixable": 3}}
    return {"digest": "sha256:aaa", "scan_overview": {"application/vnd.security.vulnerability.report; version=1.1": so}}


class HarborClientTests(unittest.TestCase):
    def setUp(self):
        for k in ("harbor_url", "harbor_username", "harbor_password", "harbor_trigger_scan"):
            store.set_setting(k, "")

    def test_parse_overview_and_report(self):
        self.assertEqual(harbor.scan_overview(art())["status"], "Success")
        self.assertEqual(harbor.scan_overview({"scan_overview": {}})["status"], "not_scanned")
        store.set_setting("harbor_username", "robot$r")
        store.set_setting("harbor_password", "s")
        resp = mock.Mock(status_code=200, content=b"x", json=lambda: REPORT)
        with mock.patch.object(harbor._http, "request", return_value=resp) as req:
            out = harbor.vulnerabilities("up-oa", "web/frontend", "sha256:aaa")
        self.assertIn("/repositories/web%252Ffrontend/artifacts/sha256:aaa/additions/vulnerabilities", req.call_args.args[1])
        self.assertEqual(out["scanner"], "Trivy")
        self.assertEqual(out["vulnerabilities"][0], {"cve": "CVE-2026-0001", "package": "openssl", "version": "3.0.13",
            "fixed": "3.0.15", "severity": "Critical", "cvss": 9.8,
            "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "title": "openssl bug"})

    def test_unconfigured_and_trigger_disabled(self):
        self.assertIn("还没配置", harbor.artifact("p", "r", "latest")["error"])
        store.set_setting("harbor_username", "robot$r")
        store.set_setting("harbor_password", "s")
        self.assertIn("未开启", harbor.trigger_scan("p", "r", "latest")["error"])
        resp = mock.Mock(status_code=401, content=b"")
        with mock.patch.object(harbor._http, "request", return_value=resp):
            self.assertIn("拒绝访问", harbor.artifact("p", "r", "latest")["error"])


class CollectAndReportTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for t in ("assets", "asset_sources", "image_scans", "image_vulns", "vulns", "apps"):
                conn.execute("DELETE FROM " + t)
        store.set_setting("harbor_username", "robot$r")
        store.set_setting("harbor_password", "s")
        store.set_setting("harbor_trigger_scan", "")
        assetstore.replace("cluster:upmore", [
            {"kind": "service", "component": "up-oa", "image": "harbor.upmore.net/up-oa/web:1a2b3c4d", "digest": "sha256:aaa",
             "cluster": "upmore", "namespace": "up-oa", "workload": "Deployment/web · 容器 web"},
            {"kind": "image", "component": "mysql", "version": "5.7", "image": "harbor.upmore.net/dockerhub/library/mysql:5.7",
             "cluster": "upmore", "namespace": "crm", "workload": "StatefulSet/mysql · 容器 mysql"},
            {"kind": "image", "component": "argocd", "image": "quay.io/argoproj/argocd:v3.4.12",
             "cluster": "upmore", "namespace": "argocd", "workload": "Deployment/argocd-server · 容器 s"},
        ])

    def collect(self):
        def artifact(project, repo, ref):
            return art() if repo == "web" else {"scan_overview": {}}
        with mock.patch.object(harbor, "artifact", side_effect=artifact) as a, \
                mock.patch.object(harbor, "vulnerabilities",
                                  return_value={"vulnerabilities": harbor_vulns()}), \
                mock.patch.object(image_scan.asset_scan, "own_registries", return_value=["harbor.upmore.net"]):
            out = image_scan.collect()
        return out, a

    def test_collect_uses_runtime_digest_and_skips_public_registries(self):
        out, a = self.collect()
        refs = sorted(c.args for c in a.call_args_list)
        self.assertEqual(refs, [("dockerhub", "library/mysql", "5.7"), ("up-oa", "web", "sha256:aaa")])
        self.assertEqual(out["images"], 2)
        self.assertEqual(out["scanned"], 1)
        statuses = {s["repo"]: s["status"] for s in assetstore.list_scans()}
        self.assertEqual(statuses, {"web": "Success", "library/mysql": "not_scanned"})
        self.assertEqual(len(assetstore.image_vulns("up-oa", "web", "sha256:aaa")), 4)

    def test_proxy_images_fall_back_to_tag_or_report_missing(self):
        assetstore.replace("cluster:upmore", [
            {"kind": "image", "component": "python", "version": "3.12", "image": "harbor.upmore.net/dockerhub/library/python:3.12-alpine",
             "digest": "sha256:old", "cluster": "upmore", "namespace": "ci", "workload": "Deployment/notify · 容器 c"},
            {"kind": "image", "component": "redis", "version": "7", "image": "harbor.upmore.net/dockerhub/library/redis:7-alpine",
             "cluster": "upmore", "namespace": "crm", "workload": "StatefulSet/redis · 容器 r"}])
        def artifact(project, repo, ref):
            if repo == "library/python" and ref == "3.12-alpine":
                return art()
            return {"error": "Harbor 上找不到该项目/仓库/制品", "not_found": True}
        with mock.patch.object(harbor, "artifact", side_effect=artifact), \
                mock.patch.object(harbor, "vulnerabilities", return_value={"vulnerabilities": harbor_vulns()}), \
                mock.patch.object(image_scan.asset_scan, "own_registries", return_value=["harbor.upmore.net"]):
            out = image_scan.collect()
        scans = {s["repo"]: s for s in assetstore.list_scans()}
        self.assertEqual(scans["library/python"]["reference"], "3.12-alpine")
        self.assertIn("按 tag 匹配", scans["library/python"]["error"])
        self.assertEqual(scans["library/redis"]["status"], "not_in_harbor")
        self.assertEqual(out["errors"], 0)
        cov = match.image_findings(assetstore.list_assets(), kev=set())["coverage"]
        self.assertEqual(cov["not_in_harbor"], ["dockerhub/library/redis"])

    def test_image_priority_kev_and_fixable(self):
        self.collect()
        assets = assetstore.list_assets()
        r = match.image_findings(assets, kev=set())
        item = r["items"][0]
        self.assertEqual((item["priority"], item["service"]), ("P1", "up-oa"))         # 可修复的严重
        self.assertEqual(item["fixable_high_critical"], 2)                               # zlib 无修复不计
        self.assertEqual([p["package"] for p in item["fix_packages"]], ["openssl", "golang.org/x/net"])
        self.assertEqual(item["workloads"], ["upmore/up-oa/Deployment/web"])
        self.assertEqual(r["coverage"]["scanned"], 1)
        self.assertEqual(len(r["coverage"]["not_scanned"]), 1)
        self.assertEqual(match.image_findings(assets, kev={"CVE-2026-0002"})["items"][0]["priority"], "P0")
        assetstore.replace("cluster:upmore", [dict(a, exposure="cluster", exposure_note="仅集群内 Service web")
                                              for a in assetstore.list_assets() if a["source"] == "cluster:upmore"])
        internal = match.image_findings(assetstore.list_assets(), kev=set())["items"][0]
        self.assertEqual((internal["priority"], internal["reachability"]), ("P2", "仅集群内部可达"))
        self.assertIn("降一级", internal["priority_reason"])
        common = {c["package"]: c for c in r["common_fixes"]}
        self.assertEqual((common["openssl"]["fix_to"], common["openssl"]["images"], common["openssl"]["critical"]), (["3.0.15"], 1, 1))

    def test_report_and_tool_respect_visibility(self):
        self.collect()
        app_id = store.create_app("镜像权限")
        store.update_app(app_id, resources={"k8s_clusters": ["upmore"], "namespaces": ["crm"]})
        self.assertIn("error", tools._asset_tool("image_vulns", {"q": "up-oa"}, app_id))   # 看不到 up-oa 命名空间
        store.update_app(app_id, resources={"k8s_clusters": ["upmore"], "namespaces": ["*"]})
        out = tools._asset_tool("image_vulns", {"q": "up-oa", "fixable_only": True, "severity": "Critical"}, app_id)
        self.assertEqual([v["cve"] for v in out["images"][0]["vulns"]], ["CVE-2026-0001"])
        with mock.patch.object(match, "_detail", return_value=None):
            rep = tools._asset_tool("vuln_report", {"days": 7}, app_id)
        self.assertEqual(rep["image_findings"]["items"][0]["service"], "up-oa")
        # 情报里的 pip 包若在镜像里出现，不算无关
        now = time.time()
        with taskstore.transaction() as conn:
            conn.execute("INSERT INTO vulns(cve,title,products,sources,first_seen,last_seen,status) VALUES(?,?,?,?,?,?,'new')",
                         ("CVE-2026-0002", "x/net bug", json.dumps(["go:golang.org/x/net"]), "[]", now, now))
            conn.execute("INSERT INTO vulns(cve,title,products,sources,first_seen,last_seen,status) VALUES(?,?,?,?,?,?,'new')",
                         ("CVE-2026-9999", "other", json.dumps(["npm:left-pad"]), "[]", now, now))
        with mock.patch.object(match, "_detail", return_value=None):
            rep = tools._asset_tool("vuln_report", {"days": 7}, app_id)
        self.assertEqual(rep["in_images"]["count"], 1)
        self.assertEqual(rep["unrelated"]["count"], 1)
        self.assertIn("image_findings", rep["self_built_images"]["note"])


class AdminTests(unittest.TestCase):
    def test_password_is_write_only(self):
        name = "harbor-admin-test"
        store.ensure_admin(name, "pw-for-test-only")
        c = TestClient(admin.app)
        c.cookies.set(admin._COOKIE, admin._make_session(store._conn.execute("SELECT username FROM admin WHERE id=1").fetchone()["username"]))
        r = c.put("/api/connections", json={"harbor": {"url": "https://harbor.example", "username": "robot$x", "password": "s3cret"}})
        self.assertEqual(r.status_code, 200)
        view = c.get("/api/connections").json()["harbor"]
        self.assertEqual(view, {"url": "https://harbor.example", "username": "robot$x", "password_set": True, "trigger_scan": False})
        self.assertNotIn("s3cret", json.dumps(c.get("/api/connections").json()))


def harbor_vulns():
    """REPORT 归一化后的明细（与 harbor.vulnerabilities 的输出格式一致）。"""
    return [{"cve": v["id"], "package": v["package"], "version": v["version"], "fixed": v.get("fix_version") or "",
             "severity": v["severity"], "cvss": (v.get("preferred_cvss") or {}).get("score_v3"), "vector": "", "title": ""}
            for v in REPORT["application/vnd.security.vulnerability.report; version=1.1"]["vulnerabilities"]]


if __name__ == "__main__":
    unittest.main()
