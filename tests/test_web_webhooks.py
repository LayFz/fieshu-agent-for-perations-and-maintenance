"""联网搜索/读网页、研究沙箱与 webhook 触发的安全契约；DNS、HTTP 与 Docker 全部替身，不访问外部服务。"""
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-web-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from core import store, taskstore
from harness import policy, tasks, webhooks
from harness.pipeline import ToolCall
from llm import authorization, engine
from ops import research, web


def _dns(mapping):
    """替身 getaddrinfo：mapping[host] = IP 列表；None 表示解析失败。"""
    def fake(host, port, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return mock.patch("ops.web.socket.getaddrinfo", side_effect=fake)


def _settings(**kv):
    return mock.patch("ops.web.store.get_setting", side_effect=lambda k: kv.get(k, ""))


class CheckUrlTests(unittest.TestCase):
    def test_literal_and_named_internal_targets_are_denied(self):
        for url in ("http://localhost/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.5/", "http://[::1]/", "http://api.svc/", "http://db.cluster.local/",
                    "ftp://example.com/", "http://user:pw@example.com/", "http://example.com:22/"):
            with self.subTest(url=url), _settings(), _dns({}), self.assertRaises(web.FetchError):
                web.check_url(url)

    def test_domain_resolving_to_private_is_denied(self):
        with _settings(), _dns({"evil.example": ["93.184.216.34", "10.1.2.3"]}), self.assertRaises(web.FetchError):
            web.check_url("https://evil.example/")
        with _settings(), _dns({"ok.example": ["93.184.216.34"]}):
            self.assertEqual(web.check_url("https://ok.example/"), ("ok.example", ["93.184.216.34"]))

    def test_fake_ip_needs_explicit_opt_in(self):
        with _settings(), _dns({"x.example": ["198.18.0.7"]}), self.assertRaises(web.FetchError):
            web.check_url("https://x.example/")
        with _settings(web_allow_fake_ip="1"), _dns({"x.example": ["198.18.0.7"]}):
            web.check_url("https://x.example/")
        # 字面量 fake-IP 永远拒绝
        with _settings(web_allow_fake_ip="1"), _dns({}), self.assertRaises(web.FetchError):
            web.check_url("http://198.18.0.7/")

    def test_proxied_mode_trusts_remote_dns_but_still_blocks_local_private(self):
        with _settings(), _dns({}):
            self.assertEqual(web.check_url("https://blocked-in-cn.example/", proxied=True), ("blocked-in-cn.example", []))
        with _settings(), _dns({"x.example": ["198.18.0.9"]}):
            self.assertEqual(web.check_url("https://x.example/", proxied=True)[1], [])
        with _settings(), _dns({"evil.example": ["192.168.1.1"]}), self.assertRaises(web.FetchError):
            web.check_url("https://evil.example/", proxied=True)
        with _settings(), _dns({}), self.assertRaises(web.FetchError):
            web.check_url("http://10.0.0.1/", proxied=True)

    def test_proxy_is_opt_in_per_use(self):
        with _settings(web_proxy="http://proxy.internal:3128", web_proxy_search="1"):
            self.assertEqual(web.proxy_for("search"), "http://proxy.internal:3128")
            self.assertEqual(web.proxy_for("fetch"), "")
        with _settings(web_proxy_search="1", web_proxy_fetch="1"):
            self.assertEqual(web.proxy_for("search"), "")

    def test_domain_allowlist(self):
        self.assertTrue(web.domain_allowed(["*"], "anything.io"))
        self.assertTrue(web.domain_allowed(["github.com"], "api.github.com"))
        self.assertFalse(web.domain_allowed(["github.com"], "evilgithub.com"))
        self.assertFalse(web.domain_allowed([], "github.com"))


class FetchTests(unittest.TestCase):
    def _resp(self, status, headers, body=b""):
        return web._Resp(status, headers, iter([body]), lambda: None)

    def test_redirect_to_internal_or_unauthorized_host_is_stopped(self):
        hops = [self._resp(302, {"location": "http://127.0.0.1/admin"})]
        with _settings(), _dns({"a.example": ["93.184.216.34"]}), \
                mock.patch("ops.web._open_pinned", side_effect=lambda u, ip: hops.pop(0)):
            r = web.fetch("https://a.example/")
        self.assertIn("error", r)
        hops = [self._resp(302, {"location": "https://b.example/"})]
        with _settings(), _dns({"a.example": ["93.184.216.34"], "b.example": ["93.184.216.35"]}), \
                mock.patch("ops.web._open_pinned", side_effect=lambda u, ip: hops.pop(0)):
            r = web.fetch("https://a.example/", allow_host=lambda h: h == "a.example")
        self.assertIn("未授权", r["error"])

    def test_pinned_connection_uses_vetted_ip_and_extracts_text(self):
        seen = []
        page = b"<html><title>T</title><nav>menu</nav><main>" + b"<p>hello world</p>" * 30 + b"</main></html>"

        def opener(url, ip):
            seen.append(ip)
            return self._resp(200, {"content-type": "text/html; charset=utf-8"}, page)
        with _settings(), _dns({"a.example": ["93.184.216.34"]}), mock.patch("ops.web._open_pinned", side_effect=opener):
            r = web.fetch("https://a.example/")
        self.assertEqual(seen, ["93.184.216.34"])
        self.assertEqual(r["title"], "T")
        self.assertIn("hello world", r["content"])
        self.assertNotIn("menu", r["content"])

    def test_binary_rejected_for_fetch_but_allowed_for_bounded_download(self):
        opener = lambda u, ip: self._resp(200, {"content-type": "application/zip"}, b"PK" * 10)
        with _settings(), _dns({"a.example": ["93.184.216.34"]}), mock.patch("ops.web._open_pinned", side_effect=opener):
            self.assertIn("不支持", web.fetch("https://a.example/x.zip")["error"])
            self.assertEqual(web.download("https://a.example/x.zip", 100)[2], b"PK" * 10)
            with self.assertRaises(web.FetchError):
                web.download("https://a.example/x.zip", 5)


class ResearchSandboxTests(unittest.TestCase):
    def test_paths_are_confined(self):
        for bad in ("../x", "/etc/passwd", "a/../../b", "", "-rf", "a,b"):
            with self.subTest(path=bad), self.assertRaises(research.ResearchError):
                research._safe_path(bad)
        self.assertEqual(research._safe_path("data/cve.json"), "data/cve.json")

    def test_inputs_are_staged_readable_and_downloads_go_through_web_rules(self):
        captured = {}

        def fake_run(source, command, timeout, check_active):
            base = Path(source)
            captured["files"] = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}
            captured["modes"] = {str(p.relative_to(base)): p.stat().st_mode & 0o777 for p in base.rglob("*")}
            return {"command": command, "exit_code": 0, "passed": True, "stdout_tail": "ok", "stopped": True}
        allow = lambda h: h == "nvd.example"
        with mock.patch("ops.research.sandbox.resolve_image", return_value="sha256:x"), \
                mock.patch("ops.research.sandbox.run", side_effect=fake_run), \
                mock.patch("ops.research.web.download", return_value=("https://nvd.example/f", "application/json", b"{}")) as dl:
            r = research.run("python3 a.py", {"a.py": "print(1)"}, [{"url": "https://nvd.example/f", "path": "in/f.json"}],
                             allow_host=allow)
        self.assertEqual(captured["files"], {"a.py": b"print(1)", "in/f.json": b"{}"})
        self.assertEqual(captured["modes"]["in"], 0o755)
        self.assertEqual(captured["modes"]["in/f.json"], 0o644)
        self.assertIs(dl.call_args.kwargs["allow_host"], allow)
        self.assertEqual(r["inputs"][1]["source"], "https://nvd.example/f")

    def test_refuses_without_sandbox_image_before_downloading(self):
        with mock.patch.dict(os.environ, {"HARNESS_SANDBOX_IMAGE": ""}), \
                mock.patch("ops.research.web.download") as dl, self.assertRaises(research.ResearchError):
            research.run("true", urls=[{"url": "https://a.example/", "path": "a"}])
        dl.assert_not_called()

    def test_guard_allows_tainted_sandbox_but_not_other_local_tools(self):
        call = ToolCall(id="1", tool="sandbox_run", args={}, risk="local", extra={"tainted": True, "sandboxed": True})
        self.assertIsNone(policy._guard_untrusted(call))
        call = ToolCall(id="2", tool="save_note", args={}, risk="local", extra={"tainted": True})
        self.assertIsNotNone(policy._guard_untrusted(call))


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        with taskstore.transaction() as conn:
            conn.execute("DELETE FROM apps")
        self.app_id = store.create_app("调研员")

    def prepare(self, name, args, resources):
        store.update_app(self.app_id, resources=resources)
        return authorization.prepare(name, args, {"app_id": self.app_id, "chat_id": "c"})

    def test_web_tools_require_domain_grants(self):
        with self.assertRaises(PermissionError):
            self.prepare("web_search", {"query": "x"}, {"web_domains": ["github.com"]})
        self.prepare("web_search", {"query": "x"}, {"web_domains": ["*"]})
        self.prepare("web_fetch", {"url": "https://api.github.com/x"}, {"web_domains": ["github.com"]})
        with self.assertRaises(PermissionError):
            self.prepare("web_fetch", {"url": "https://example.com/"}, {"web_domains": ["github.com"]})

    def test_sandbox_requires_grant_and_url_domains(self):
        args = {"command": "true", "urls": [{"url": "https://github.com/a", "path": "a"}]}
        with self.assertRaises(PermissionError):
            self.prepare("sandbox_run", args, {"web_domains": ["*"]})
        with self.assertRaises(PermissionError):
            self.prepare("sandbox_run", args, {"sandbox": ["research"], "web_domains": ["nvd.nist.gov"]})
        self.prepare("sandbox_run", args, {"sandbox": ["research"], "web_domains": ["github.com"]})


class WebhookTests(unittest.TestCase):
    def setUp(self):
        tasks.stop(timeout=1)
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for table in ("task_outbox", "task_actions", "tasks", "apps", "webhooks", "webhook_calls"):
                conn.execute("DELETE FROM " + table)
        self.app_id = store.create_app("运维助手")
        self.token = webhooks.new_token()
        store.create_webhook("hook1", name="告警", app_id=self.app_id, chat_id="oc_1", workflow="cluster",
                             template="告警 {{alert}} 在 {{labels.ns}}\n{{payload}}",
                             token_hash=webhooks.hash_token(self.token), enabled=1, max_per_hour=2)

    def post(self, body, token=None, **headers):
        h = {"X-Harness-Token": self.token if token is None else token, **headers}
        return webhooks.receive("hook1", h, body if isinstance(body, bytes) else json.dumps(body).encode())

    def test_auth_is_uniform_for_unknown_hook_and_bad_token(self):
        self.assertEqual(self.post({}, token="wrong")[0], 401)
        self.assertEqual(webhooks.receive("nope", {"X-Harness-Token": self.token}, b"{}")[0], 401)
        self.assertEqual(webhooks.receive("hook1", {"Authorization": "Bearer " + self.token}, b"{}")[0], 202)

    def test_creates_tainted_webhook_task_and_deduplicates(self):
        status, body = self.post({"alert": "CrashLoop", "labels": {"ns": "pay"}}, **{"Idempotency-Key": "e1"})
        self.assertEqual(status, 202)
        task = taskstore.get(body["task_id"])
        self.assertEqual(task["trigger"], "webhook")
        self.assertEqual(task["source"], "webhook:hook1")
        self.assertTrue(engine._untrusted_input(task))
        self.assertIn("告警 CrashLoop 在 pay", task["instruction"])
        again = self.post({"alert": "CrashLoop", "labels": {"ns": "pay"}}, **{"Idempotency-Key": "e1"})
        self.assertEqual(again[0], 200)
        self.assertEqual(again[1]["task_id"], body["task_id"])

    def test_rejects_bad_bodies_disabled_and_throttles(self):
        self.assertEqual(self.post(b"not json")[0], 400)
        self.assertEqual(self.post(b"{" + b" " * webhooks.MAX_BODY + b"}")[0], 413)
        self.assertEqual(self.post({"n": 1})[0], 202)
        self.assertEqual(self.post({"n": 2})[0], 202)
        self.assertEqual(self.post({"n": 3})[0], 429)
        store.update_webhook("hook1", enabled=0)
        self.assertEqual(self.post({"n": 4})[0], 403)

    def test_render_marks_payload_as_external_and_truncates(self):
        out = webhooks.render("{{a.b}}|{{missing}}|{{list[1]}}{{payload}}", {"a": {"b": "x" * 900}, "list": [1, 2]})
        first, missing, rest = out.split("|", 2)
        self.assertEqual(len(first), 500)
        self.assertEqual(missing, "（无）")
        self.assertTrue(rest.startswith("2"))
        self.assertIn("外部系统发来的原始数据", rest)
        self.assertIn("外部数据结束", rest)


if __name__ == "__main__":
    unittest.main()
