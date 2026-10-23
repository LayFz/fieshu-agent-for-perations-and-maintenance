"""kubectl 授权语法回归；仅解析命令，不启动 kubectl 或连接集群。"""
import unittest

from harness import access


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.scoped = {"enabled": True, "resources": {"namespaces": ["team"]}}
        self.global_app = {"enabled": True, "resources": {"namespaces": ["*"]}}

    def test_explicit_resource_scope_is_required(self):
        self.assertFalse(access.allowed({}, "repos", "example"))
        self.assertFalse(access.allowed({"resources": {"repos": "*"}}, "repos", "example"))
        self.assertTrue(access.allowed({"resources": {"repos": ["example"]}}, "repos", "example"))
        with self.assertRaises(PermissionError):
            access.require({"resources": {"repos": ["a"]}}, "repos", "b")

    def test_actor_scope_and_disabled_app(self):
        app = {"enabled": True, "resources": {"users": ["alice"], "chats": ["team-chat"]}}
        access.actor(app, "team-chat", "alice")
        for chat, user in (("other-chat", "alice"), ("team-chat", "bob")):
            with self.subTest(chat=chat, user=user), self.assertRaises(PermissionError):
                access.actor(app, chat, user)
        with self.assertRaises(PermissionError):
            access.actor(dict(app, enabled=False), "team-chat", "alice")

    def test_common_readonly_queries(self):
        for command in ("get pods -n team", "-n team get pods -o json",
                        "get deployments.apps -nteam -oyaml", "describe pod/api -n team",
                        "logs deployment/api -n team --tail=100 --previous",
                        "get pods -n team -l app=api --field-selector status.phase=Pending"):
            with self.subTest(command=command):
                access.kubectl_scope(self.scoped, command)

    def test_secret_aliases_groups_and_multiple_resource_forms_are_denied(self):
        for command in ("get secret -n team", "get secrets.v1 -n team", "get pods,secrets -n team",
                        "get se -n team", "get pod/a secrets/b -n team",
                        "describe pod/a secret/b -n team", "get pods,secret.v1 -n team"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                access.kubectl_scope(self.global_app, command)

    def test_identity_raw_path_and_file_flags_are_denied(self):
        for tail in ("--kubeconfig=/etc/other", "--server=https://example.invalid", "-shttps://example.invalid",
                     "--token=fixture", "--as=other", "--context=other", "--raw=/api/v1/secrets",
                     "-f/etc/passwd", "--filename=/etc/passwd", "-k/tmp", "--template-file=/etc/passwd",
                     "--output=go-template-file=/etc/passwd", "-ojsonpath-file=/etc/passwd",
                     "--proxy-url=http://example.invalid", "--insecure-skip-tls-verify"):
            command = "get pods -n team " + tail
            with self.subTest(command=command), self.assertRaises(PermissionError):
                access.kubectl_scope(self.scoped, command)

    def test_cluster_scope_cannot_hide_in_group_or_second_resource(self):
        for command in ("get nodes -n team", "get clusterroles.rbac.authorization.k8s.io -n team",
                        "get pod/a nodes/b -n team", "get pods.custom.example -n team", "get pods -A"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                access.kubectl_scope(self.scoped, command)
        access.kubectl_scope(self.global_app, "get nodes -A")

    def test_mutation_unknown_flags_and_ambiguous_namespace_denied(self):
        for command in ("delete pod/api -n team", "exec pod/api -n team -- id",
                        "get pods --unknown=value -n team", "get pods -n team --namespace=other",
                        "get pods -n other", "get pods -n"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                access.kubectl_scope(self.scoped, command)


if __name__ == "__main__":
    unittest.main()
