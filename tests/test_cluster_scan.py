"""固定只读 API、凭据边界、分页覆盖与真实状态归因；不访问任何集群。"""
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

import requests
import yaml

_db = tempfile.TemporaryDirectory(prefix="cluster-scan-tests-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _db.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from ops import cluster_scan as scanner


def kubeconfig():
    return {"apiVersion": "v1", "current-context": "test", "contexts": [
        {"name": "test", "context": {"cluster": "test", "user": "test"}}],
        "clusters": [{"name": "test", "cluster": {"server": "https://kubernetes.invalid:6443"}}],
        "users": [{"name": "test", "user": {"token": "TOP_SECRET_TOKEN"}}]}


def obj(name, ns="demo", spec=None, status=None):
    return {"metadata": {"name": name, "namespace": ns,
                         "creationTimestamp": "2020-01-01T00:00:00Z"},
            "spec": spec or {}, "status": status or {}}


def response(items=None, status=200, continuation=None):
    payload = {"items": items or [], "metadata": {"continue": continuation or ""}}
    res = mock.Mock(status_code=status)
    res.iter_content.return_value = [json.dumps(payload).encode()]
    return res


class ClusterScanTests(unittest.TestCase):
    def setUp(self):
        self.config = kubeconfig()
        self.setting = mock.patch.object(scanner.store, "get_setting", side_effect=lambda *args: json.dumps([
            {"name": "fixture", "kubeconfig": yaml.safe_dump(self.config)}]))
        self.setting.start()
        self.session = mock.Mock()
        self.session.get.side_effect = lambda *a, **kw: response()
        self.factory = mock.patch.object(scanner.requests, "Session", return_value=self.session)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.addCleanup(self.setting.stop)
        self.now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def routes(self, routes):
        def get(url, **kwargs):
            data = routes.get(urlsplit(url).path, [])
            if isinstance(data, Exception):
                raise data
            if isinstance(data, int):
                return response(status=data)
            return response(data)
        self.session.get.side_effect = get

    def test_fixed_gets_namespace_scope_and_no_secret_return(self):
        pod = obj("api", status={"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
                  spec={"containers": [{"env": [{"name": "PASSWORD", "value": "BUSINESS_SECRET"}]}]})
        self.routes({"/api/v1/namespaces/demo/pods": [pod]})
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "ok")
        paths = [urlsplit(c.args[0]).path for c in self.session.get.call_args_list]
        self.assertEqual(len(paths), 5)
        self.assertTrue(all("/namespaces/demo/" in p for p in paths))
        self.assertFalse(any(p.endswith(("/nodes", "/secrets", "/configmaps", "/applications")) for p in paths))
        for call in self.session.get.call_args_list:
            self.assertIs(call.kwargs["verify"], True)
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer TOP_SECRET_TOKEN")
            self.assertLessEqual(call.kwargs["timeout"][1], 10)
        self.assertIs(self.session.trust_env, False)
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        self.assertNotIn("BUSINESS_SECRET", json.dumps(result))
        self.session.close.assert_called_once()

    def test_rejects_executable_external_and_insecure_kubeconfigs(self):
        mutations = [("user", {"exec": {"command": "touch /tmp/never"}}),
                     ("user", {"auth-provider": {"name": "gcp"}}),
                     ("user", {"tokenFile": "/tmp/token"}),
                     ("user", {"client-key": "/tmp/key"}),
                     ("cluster", {"certificate-authority": "/tmp/ca"}),
                     ("cluster", {"insecure-skip-tls-verify": True}),
                     ("cluster", {"server": "https://user:secret@host.invalid"}),
                     ("cluster", {"server": "http://host.invalid"}),
                     ("cluster", {"proxy-url": "http://proxy.invalid"})]
        for kind, mutation in mutations:
            with self.subTest(mutation=mutation):
                self.config = kubeconfig()
                self.config["users" if kind == "user" else "clusters"][0][kind].update(mutation)
                result = scanner.scan("fixture")
                self.assertEqual(result["summary"]["status"], "error")
        self.session.get.assert_not_called()

    def test_inline_certificates_are_private_and_removed(self):
        def data(kind):
            return base64.b64encode(("-----BEGIN " + kind + "-----\nfixture\n").encode()).decode()
        self.config["clusters"][0]["cluster"]["certificate-authority-data"] = data("CERTIFICATE")
        self.config["users"][0]["user"].update({"client-certificate-data": data("CERTIFICATE"),
                                                "client-key-data": data("PRIVATE KEY")})
        paths = []
        def get(url, **kw):
            paths.extend([kw["verify"], *kw["cert"]])
            for p in paths:
                self.assertTrue(Path(p).is_file())
                self.assertEqual(Path(p).stat().st_mode & 0o777, 0o600)
            return response()
        self.session.get.side_effect = get
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "ok")
        self.assertTrue(paths)
        self.assertTrue(all(not Path(p).exists() for p in paths))

    def test_partial_cnpg_query_does_not_claim_coverage(self):
        self.routes({"/apis/postgresql.cnpg.io/v1/namespaces/demo/clusters": [obj("pg")],
                     "/apis/postgresql.cnpg.io/v1/namespaces/demo/backups": 403})
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "partial")
        source = next(s for s in result["sources"] if ":cnpg-backup:" in s["name"])
        self.assertEqual(source["status"], "error")
        self.assertFalse(any(f["key"].startswith("cnpg-") for f in result["findings"]))

    def test_error_does_not_echo_response_or_exception_credentials(self):
        self.routes({"/api/v1/namespaces/demo/pods": requests.ConnectionError("Bearer TOP_SECRET_TOKEN")})
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "partial")
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        self.assertIn("ConnectionError", json.dumps(result))

    def test_pagination_and_budget_mark_incomplete_source(self):
        count = 0
        def get(url, **kw):
            nonlocal count
            if url.endswith("/pods"):
                count += 1
                return response([obj("p" + str(count), status={"phase": "Succeeded"})],
                                continuation="next" if count == 1 else None)
            return response()
        self.session.get.side_effect = get
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["pod_counts"]["demo"], 2)
        self.assertEqual(self.session.get.call_args_list[1].kwargs["params"]["continue"], "next")
        self.session.get.side_effect = lambda *a, **kw: response(continuation="repeated")
        with mock.patch.object(scanner, "MAX_PAGES", 2):
            result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "error")
        self.assertTrue(all(s["status"] == "error" for s in result["sources"]))

    def test_argo_history_is_not_current_outage_and_urls_are_redacted(self):
        app = obj("api", "argocd", spec={"source": {"repoURL": "https://user:TOP_SECRET_TOKEN@git.invalid/r.git?token=BAD",
                                                      "path": "apps/api", "targetRevision": "main"},
                                                "helm": {"values": "PASSWORD=BUSINESS_SECRET"}},
                  status={"sync": {"status": "Synced", "revision": "abc"}, "health": {"status": "Healthy"},
                          "operationState": {"phase": "Failed", "message": "secret=TOP_SECRET_TOKEN",
                                             "syncResult": {"revision": "attempted"}},
                          "history": [{"revision": "deployed", "deployedAt": self.now}]})
        self.routes({"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications": [app]})
        result = scanner.scan("fixture", ["argocd"])
        finding = next(f for f in result["findings"] if "argo-" in f["key"])
        self.assertEqual(finding["severity"], "info")
        summary = result["applications"][0]
        self.assertEqual(summary["sources"][0]["repo_url"], "https://git.invalid/r.git")
        self.assertEqual(summary["last_operation_revision"], "attempted")
        self.assertEqual(summary["latest_history_revision"], "deployed")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("message", json.dumps(result))

    def test_oom_and_recent_warning_are_classified_without_raw_message(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        pod = obj("worker", status={"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                  "containerStatuses": [{"name": "worker", "ready": True, "restartCount": 4,
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": self.now}}}]})
        def event(name, when):
            return {"type": "Warning", "reason": "Unhealthy", "lastTimestamp": when, "count": 10,
                    "involvedObject": {"kind": "Pod", "namespace": "demo", "name": name},
                    "message": "Readiness deadline exceeded password=TOP_SECRET_TOKEN"}
        self.routes({"/api/v1/namespaces/demo/pods": [pod],
                     "/api/v1/namespaces/demo/events": [event("worker", self.now), event("old", old)]})
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(next(f for f in result["findings"] if f["key"].startswith("event-"))["evidence"]["category"], "probe_timeout")
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_cnpg_missing_coverage_and_working_five_field_schedule(self):
        pg = obj("covered", spec={"backup": {"barmanObjectStore": {"secret": "TOP_SECRET_TOKEN"}}})
        backup = obj("daily", spec={"cluster": {"name": "covered"}}, status={"phase": "completed"})
        schedule = obj("daily", spec={"cluster": {"name": "covered"}, "schedule": "0 10 3 * *"})
        prefix = "/apis/postgresql.cnpg.io/v1/namespaces/demo/"
        self.routes({prefix + "clusters": [pg, obj("uncovered")], prefix + "backups": [backup],
                     prefix + "scheduledbackups": [schedule]})
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        self.assertEqual(finding["resource"], "Cluster/demo/uncovered")
        self.assertEqual(finding["severity"], "high")
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_global_nodes_and_current_argo_failure(self):
        node = obj("worker", status={"conditions": [{"type": "Ready", "status": "False"},
                                                      {"type": "DiskPressure", "status": "True"}]})
        app = obj("broken", "argocd", status={"sync": {"status": "OutOfSync"},
                                               "health": {"status": "Degraded"},
                                               "conditions": [{"type": "ComparisonError", "message": "TOP_SECRET_TOKEN"}]})
        self.routes({"/api/v1/nodes": [node], "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications": [app]})
        result = scanner.scan("fixture")
        self.assertEqual(result["summary"]["status"], "ok")
        self.assertEqual(result["summary"]["node_count"], 1)
        self.assertEqual(len(result["findings"]), 2)
        self.assertTrue(all(f["severity"] == "high" for f in result["findings"]))
        self.assertEqual({s["name"] for s in result["sources"]}, {
            "cluster:fixture:nodes", "cluster:fixture:pods:*", "cluster:fixture:events:*",
            "cluster:fixture:cnpg-backup:*", "cluster:fixture:applications:argocd"})
        self.assertNotIn("TOP_SECRET", json.dumps(result))

    def test_oversize_redirect_and_invalid_namespace_fail_closed(self):
        with mock.patch.object(scanner, "MAX_BYTES", 1):
            result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "error")
        self.session.get.reset_mock()
        for namespaces in (["demo/../../secrets"], ["*", "demo"], "demo", []):
            self.assertEqual(scanner.scan("fixture", namespaces)["summary"]["status"], "error")
        self.session.get.assert_not_called()
        self.routes({"/api/v1/namespaces/demo/pods": 302})
        result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "partial")
        self.assertEqual(result["sources"][0]["status"], "error")

    def test_expired_scan_budget_never_claims_empty_healthy_result(self):
        with mock.patch.object(scanner, "SCAN_SECONDS", 0):
            result = scanner.scan("fixture", ["demo"])
        self.assertEqual(result["summary"]["status"], "error")
        self.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
