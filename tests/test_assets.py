"""资产清单、漏洞匹配与报告：组件归一、采集解析、快照语义、版本判断、误报规避、权限与验收。不访问集群或外网。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-assets-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import assetstore, components, store, taskstore
from harness import access, workflows
from llm import authorization, tools
from ops import asset_scan
from sec import match

OWN = ("harbor.upmore.net", "10.10.0.1")


def pod(ns, name, image, owner=("ReplicaSet", "api-7c9f8d6b5d"), phase="Running", digest="sha256:abc", tracking=None):
    md = {"namespace": ns, "name": name, "ownerReferences": [{"kind": owner[0], "name": owner[1]}]}
    if tracking:
        md["annotations"] = {"argocd.argoproj.io/tracking-id": tracking}
    return {"metadata": md, "spec": {"containers": [{"name": "c", "image": image}]},
            "status": {"phase": phase, "containerStatuses": [{"name": "c", "imageID": f"docker.io/x@{digest}"}]}}


class ComponentTests(unittest.TestCase):
    def test_image_to_component_and_version(self):
        cases = {"quay.io/argoproj/argocd:v3.1.5": ("argocd", "3.1.5", False),
                 "registry.k8s.io/ingress-nginx/controller:v1.11.3@sha256:abc": ("ingress-nginx", "1.11.3", False),
                 "rancher/mirrored-coredns-coredns:1.12.0": ("coredns", "1.12.0", False),
                 "redis:7-alpine": ("redis", "7", False),
                 "harbor.upmore.net/upmore/upmore-api:1a2b3c4d": ("upmore-api", "", True)}
        for ref, expected in cases.items():
            self.assertEqual(components.component_of(ref, OWN), expected, ref)

    def test_specific_component_supersedes_generic(self):
        self.assertEqual(components.mentioned(components.CATALOG, "Kubernetes ingress-nginx"), {"ingress-nginx": "ingress-nginx"})
        self.assertNotIn("redis", components.mentioned(["redis"], "a redistributable library"))

    def test_package_matching_is_exact(self):
        self.assertEqual(components.package_component("go:github.com/argoproj/argo-cd/v3", ["argocd", "redis"]), "argocd")
        self.assertIsNone(components.package_component("pip:redis", ["argocd", "redis"]))   # Python 客户端不是 Redis 服务
        self.assertTrue(components.is_package("npm:next"))


class CollectorTests(unittest.TestCase):
    def test_pods_nodes_and_remote_apps(self):
        rows = asset_scan.pods_to_assets([
            pod("argocd", "argocd-server-1", "quay.io/argoproj/argocd:v3.1.5", tracking="argocd:apps/Deployment"),
            pod("upmore-api", "api-1", "harbor.upmore.net/upmore/upmore-api:1a2b3c4d"),
            pod("old", "done-1", "busybox:1.36", phase="Succeeded"),
        ], "upmore", OWN)
        self.assertEqual([(r["component"], r["kind"], r["workload"]) for r in rows],
                         [("argocd", "image", "Deployment/api · 容器 c"), ("upmore-api", "service", "Deployment/api · 容器 c")])
        init = asset_scan.pods_to_assets([{"metadata": {"namespace": "argocd", "name": "redis-1"},
            "spec": {"containers": [{"name": "redis", "image": "redis:7.2"}],
                     "initContainers": [{"name": "secret-init", "image": "quay.io/argoproj/argocd:v3.4.5"}]},
            "status": {"phase": "Running"}}], "upmore", OWN)
        self.assertIn("init 容器 secret-init", init[1]["workload"])
        self.assertEqual(rows[0]["digest"], "sha256:abc")
        self.assertEqual(rows[0]["app"], "argocd")
        nodes = asset_scan.nodes_to_assets([{"metadata": {"name": "n1"}, "status": {"nodeInfo": {
            "kubeletVersion": "v1.36.2+k3s1", "containerRuntimeVersion": "containerd://2.0.5-k3s1", "kernelVersion": "6.8.0-45-generic"}}}], "upmore")
        self.assertEqual({(r["component"], r["version"]) for r in nodes},
                         {("kubernetes", "1.36.2"), ("k3s", "1.36.2+k3s1"), ("containerd", "2.0.5"), ("linux-kernel", "6.8.0")})
        apps = asset_scan.applications_to_assets([
            {"metadata": {"name": "upmore-api-vke"}, "spec": {"destination": {"name": "vke-cn-shanghai", "namespace": "upmore-api"}},
             "status": {"summary": {"images": ["redis:7.2.4"]}}},
            {"metadata": {"name": "local"}, "spec": {"destination": {"server": "https://kubernetes.default.svc"}},
             "status": {"summary": {"images": ["redis:7.2.4"]}}}], "upmore", OWN)
        self.assertEqual([(r["cluster"], r["component"], r["version"]) for r in apps], [("vke-cn-shanghai", "redis", "7.2.4")])


class StoreAndMatchTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for table in ("assets", "asset_sources", "vulns", "apps"):
                conn.execute("DELETE FROM " + table)

    def add_vuln(self, cve, products, title="", description="", kev=0, epss=0.01, days_ago=1):
        now = time.time() - days_ago * 86400
        with taskstore.transaction() as conn:
            conn.execute("INSERT INTO vulns(cve,title,description,kev,epss,products,sources,first_seen,last_seen,status) "
                         "VALUES(?,?,?,?,?,?,?,?,?,'new')",
                         (cve, title, description, kev, epss, json.dumps(products), json.dumps(["https://example/" + cve]), now, now))

    def test_snapshot_replace_keeps_first_seen_and_error_keeps_rows(self):
        row = {"kind": "image", "component": "argocd", "version": "3.1.5", "image": "a", "cluster": "upmore",
               "namespace": "argocd", "workload": "Deployment/argocd-server"}
        assetstore.replace("cluster:upmore", [row])
        first = assetstore.list_assets()[0]["first_seen"]
        time.sleep(0.01)
        assetstore.replace("cluster:upmore", [row, dict(row, workload="Deployment/argocd-repo-server")])
        rows = assetstore.list_assets()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r["workload"] == "Deployment/argocd-server")["first_seen"], first)
        assetstore.mark_error("cluster:upmore", "Kubernetes 连接失败")
        self.assertEqual(len(assetstore.list_assets()), 2)
        self.assertEqual(assetstore.sources()[0]["status"], "error")

    def test_cvss_vector_score(self):
        self.assertEqual(match.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H"), 9.9)
        self.assertEqual(match.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), 9.8)
        self.assertEqual(match.cvss3_score("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N"), 1.8)
        self.assertIsNone(match.cvss3_score("HIGH"))

    def test_verdict(self):
        self.assertEqual(match.verdict("3.1.5", ["2.14.20", "3.0.19", "3.1.8"])[0], "affected")
        self.assertEqual(match.verdict("3.1.9", ["2.14.20", "3.1.8"])[0], "fixed")
        self.assertEqual(match.verdict("3.2.0", ["3.1.8"])[0], "fixed")
        self.assertEqual(match.verdict("1.10.0", ["1.11.5", "1.12.1"])[0], "affected")
        self.assertEqual(match.verdict("", ["1.0.0"])[0], "unknown")
        self.assertEqual(match.verdict("7", [])[0], "unknown")
        self.assertEqual(match.verdict("19.1.9", ["0141f228c05216d97eae940261c824e64c73255a"])[0], "unknown")  # 提交哈希不是版本
        groups = match._RELEASE.findall("GitLab Critical Patch Release: 19.2.4, 19.3.2, and 19.4.1")
        self.assertEqual(groups and __import__("re").findall(r"\d+\.\d+(?:\.\d+)*", groups[0]), ["19.2.4", "19.3.2", "19.4.1"])

    def test_report_matches_only_our_components_with_evidence(self):
        assetstore.replace("cluster:upmore", [
            {"kind": "image", "component": "argocd", "version": "3.1.5", "image": "quay.io/argoproj/argocd:v3.1.5",
             "cluster": "upmore", "namespace": "argocd", "workload": "Deployment/argocd-server", "evidence": "pod（运行时）"},
            {"kind": "image", "component": "ingress-nginx", "version": "1.11.3", "image": "ingress-nginx/controller:v1.11.3",
             "cluster": "upmore", "namespace": "ingress-nginx", "workload": "Deployment/ingress-nginx-controller", "evidence": "pod（运行时）"},
            {"kind": "image", "component": "redis", "version": "7", "image": "redis:7-alpine",
             "cluster": "upmore", "namespace": "flowlab", "workload": "StatefulSet/redis", "evidence": "pod（运行时）"},
            {"kind": "service", "component": "upmore-api", "image": "harbor.upmore.net/upmore/upmore-api:1a2b3c4d",
             "cluster": "upmore", "namespace": "upmore-api", "workload": "Deployment/api", "evidence": "pod（运行时）"},
        ])
        self.add_vuln("CVE-2026-1", ["go:github.com/argoproj/argo-cd/v3"], "Argo CD repo-server SSRF", epss=0.02)
        self.add_vuln("CVE-2026-2", ["Kubernetes ingress-nginx"], "ingress-nginx admission RCE", kev=1, epss=0.9)
        self.add_vuln("CVE-2026-3", ["pip:redis"], "redis-py leaks credentials")            # 不是 Redis 服务
        self.add_vuln("CVE-2026-4", ["Citrix NetScaler"], "NetScaler memory overread", kev=1)
        self.add_vuln("CVE-2026-5", ["go:github.com/argoproj/argo-cd/v3"], "old", days_ago=30)  # 窗口外
        details = {"CVE-2026-1": {"affected": [{"package": "github.com/argoproj/argo-cd/v3", "fixed_in": ["3.0.19", "3.1.8"]}],
                                  "severity": "HIGH"},
                   "CVE-2026-2": {"affected": [], "severity": None}}
        osv = {"3.1.5": {"CVE-2026-1": ["3.0.19", "3.1.8"]}}          # OSV 按包版本查询：3.1.5 受 CVE-2026-1 影响
        with mock.patch.object(match, "_detail", side_effect=lambda cve: details.get(cve)), \
                mock.patch.object(match, "_osv_package", side_effect=lambda product, ver: osv.get(ver, {})):
            r = match.report(days=7)
        by = {x["cve"]: x for x in r["related"]}
        self.assertEqual(set(by), {"CVE-2026-1", "CVE-2026-2"})
        self.assertEqual(by["CVE-2026-1"]["priority"], "P1")
        self.assertEqual(by["CVE-2026-1"]["version_checks"][0]["verdict"], "affected")
        self.assertIn("OSV 确认", by["CVE-2026-1"]["version_checks"][0]["why"])
        self.assertEqual(by["CVE-2026-1"]["assets_affected"], 1)
        self.assertIn("源码包", by["CVE-2026-1"]["matched_by"])
        self.assertEqual(by["CVE-2026-2"]["component"], "ingress-nginx")
        self.assertEqual(by["CVE-2026-2"]["priority"], "P0")                       # KEV + 在跑 + 无法确认版本
        self.assertEqual(by["CVE-2026-2"]["assets"][0]["workload"], "Deployment/ingress-nginx-controller")
        self.assertEqual(r["unrelated"]["count"], 2)
        self.assertEqual(r["unrelated"]["kev"], 1)
        self.assertEqual(r["intel"]["in_window"], 4)
        self.assertEqual(r["self_built_images"]["names"], ["upmore-api"])
        self.assertEqual(r["related"][0]["cve"], "CVE-2026-2")                     # P0 排最前

    def test_osv_version_query_separates_affected_and_fixed_workloads(self):
        rows = [{"kind": "image", "component": "argocd", "version": ver, "image": f"quay.io/argoproj/argocd:v{ver}",
                 "cluster": "upmore", "namespace": "argocd", "workload": f"Deployment/argocd-{ver}", "evidence": "pod（运行时）"}
                for ver in ("3.4.5", "3.4.12")]
        assetstore.replace("cluster:upmore", rows)
        self.add_vuln("CVE-2026-55797", ["go:github.com/argoproj/argo-cd/v3"], "Argo CD command injection")
        self.add_vuln("CVE-2026-113708", ["go:github.com/argoproj/argo-cd/v3"], "Argo CD kustomize RCE")   # OSV 未收录
        osv = {"3.4.5": {"CVE-2026-55797": ["3.3.15", "3.4.10"]}, "3.4.12": {}}
        with mock.patch.object(match, "_detail", return_value=None), \
                mock.patch.object(match, "_osv_package", side_effect=lambda product, ver: osv[ver]):
            r = match.report(days=7)
        by = {x["cve"]: x for x in r["related"]}
        checks = {c["version"]: c["verdict"] for c in by["CVE-2026-55797"]["version_checks"]}
        self.assertEqual(checks, {"3.4.12": "fixed", "3.4.5": "affected"})
        self.assertEqual([a["workload"] for a in by["CVE-2026-55797"]["assets"]], ["Deployment/argocd-3.4.5"])
        self.assertEqual({c["verdict"] for c in by["CVE-2026-113708"]["version_checks"]}, {"unknown"})

    def test_init_container_only_is_downgraded_with_exposure_note(self):
        assetstore.replace("cluster:upmore", [
            {"kind": "image", "component": "argocd", "version": "3.4.5", "image": "quay.io/argoproj/argocd:v3.4.5",
             "cluster": "upmore", "namespace": "argocd", "workload": "Deployment/argocd-redis · init 容器 secret-init（启动时短暂运行）",
             "evidence": "pod 运行时镜像（含摘要）"},
            {"kind": "image", "component": "argocd", "version": "3.4.12", "image": "quay.io/argoproj/argocd:v3.4.12",
             "cluster": "upmore", "namespace": "argocd", "workload": "Deployment/argocd-server · 容器 argocd-server",
             "evidence": "pod 运行时镜像（含摘要）"}])
        self.add_vuln("CVE-2026-55797", ["go:github.com/argoproj/argo-cd/v3"], "Argo CD command injection", epss=0.02)
        osv = {"3.4.5": {"CVE-2026-55797": ["3.4.10"]}, "3.4.12": {}}
        with mock.patch.object(match, "_detail", return_value={"severity": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H"}), \
                mock.patch.object(match, "_osv_package", side_effect=lambda product, ver: osv[ver]):
            item = match.report(days=7)["related"][0]
        self.assertEqual(item["priority"], "P2")                         # 本应 P1，仅 init 容器 → 降一级
        self.assertIn("init 容器", item["exposure"])
        self.assertIn("降一级", item["priority_reason"])

    def test_daily_mode_only_counts_newly_seen(self):
        assetstore.replace("gitlab", [{"kind": "component", "component": "gitlab", "version": "19.1.9"}])
        self.add_vuln("CVE-2026-7", [], "GitLab Critical Patch Release: 19.2.4", days_ago=0.2)
        with taskstore.transaction() as conn:                             # 旧漏洞今天又被提到：last_seen 新、first_seen 旧
            conn.execute("UPDATE vulns SET first_seen=? WHERE cve='CVE-2026-7'", (time.time() - 3 * 86400,))
        self.add_vuln("CVE-2026-8", [], "GitLab Security Release: 19.2.5", days_ago=0.1)
        with mock.patch.object(match, "_detail", return_value=None):
            daily = match.report(days=1, new_only=True)
            weekly = match.report(days=1)
        self.assertEqual([x["cve"] for x in daily["related"]], ["CVE-2026-8"])
        self.assertEqual(sorted(x["cve"] for x in weekly["related"]), ["CVE-2026-7", "CVE-2026-8"])

    def test_subproduct_advisory_goes_to_needs_confirmation(self):
        assetstore.replace("gitlab", [{"kind": "component", "component": "gitlab", "version": "19.1.9"}])
        self.add_vuln("CVE-2026-90970", [], "GitLab AI Gateway Critical Patch Release: 19.2.4, 19.3.2, and 19.4.1",
                      "We have released versions 19.2.4 of the GitLab AI Gateway.")
        self.add_vuln("CVE-2026-90971", [], "GitLab Critical Patch Release: 19.2.4, 19.3.2", "GitLab CE/EE fixes")
        with mock.patch.object(match, "_detail", return_value=None):
            r = match.report(days=7)
        self.assertEqual([x["cve"] for x in r["needs_confirmation"]], ["CVE-2026-90970"])
        self.assertEqual(r["needs_confirmation"][0]["subproduct"], "ai gateway")
        self.assertEqual([x["cve"] for x in r["related"]], ["CVE-2026-90971"])
        self.assertEqual(r["related"][0]["version_checks"][0]["verdict"], "affected")

    def test_visibility_and_authorization(self):
        assetstore.replace("cluster:upmore", [{"kind": "image", "component": "argocd", "namespace": "argocd", "cluster": "upmore"},
                                              {"kind": "image", "component": "redis", "namespace": "flowlab", "cluster": "upmore"}])
        app_id = store.create_app("资产权限")
        store.update_app(app_id, resources={"k8s_clusters": ["upmore"], "namespaces": ["argocd"]})
        out = tools._asset_tool("asset_list", {}, app_id)
        self.assertEqual([a["component"] for a in out["assets"]], ["argocd"])
        store.update_app(app_id, resources={"repos": ["x"]})
        with self.assertRaises(PermissionError):
            authorization.prepare("vuln_report", {}, {"app_id": app_id, "chat_id": "c"})
        self.assertFalse(access.asset_visible(store.get_app(app_id), {"source": "cluster:upmore", "namespace": "argocd"}))


class WorkflowAndHintTests(unittest.TestCase):
    def test_report_requests_need_vuln_report_evidence(self):
        ev = [{"tool": "vuln_list", "result": {"items": []}}]
        self.assertEqual(workflows.resolve("auto", "发送漏洞报告"), "vulnerability")
        self.assertIn("vuln_report", workflows.verify("auto", "发送漏洞报告", ev))
        self.assertIsNone(workflows.verify("auto", "发送漏洞报告", ev + [{"tool": "vuln_report", "result": {"related": []}}]))
        self.assertIsNone(workflows.verify("auto", "查一下漏洞 CVE-2026-1 详情", [{"tool": "cve_detail", "result": {"id": "x"}}]))

    def test_network_errors_get_actionable_hint(self):
        out = tools.explain_errors("server_stats", {"error": "('Connection aborted.', RemoteDisconnected('Remote end closed'))"})
        self.assertIn("NetBird", out["hint"])
        self.assertNotIn("hint", tools.explain_errors("vuln_list", {"error": "ConnectionError"}))
        self.assertNotIn("hint", tools.explain_errors("kubectl", {"error": "Forbidden"}))


if __name__ == "__main__":
    unittest.main()
