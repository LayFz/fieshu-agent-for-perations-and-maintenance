"""修复意图应进入需要校验和 MR 证据的工作流。"""
import unittest

from harness import workflows


class WorkflowRoutingTests(unittest.TestCase):
    def test_cluster_or_log_repair_requires_development_delivery(self):
        for instruction in ("修复集群中的 Pod 故障", "修复服务日志里的配置错误"):
            with self.subTest(instruction=instruction):
                self.assertEqual(workflows.resolve("auto", instruction), "development")
                self.assertIn("隔离测试或 GitOps 静态校验", workflows.verify("auto", instruction, []))

    def test_vulnerability_repair_keeps_vulnerability_evidence_gate(self):
        instruction = "修复 CVE-2026-1234 漏洞"
        self.assertEqual(workflows.resolve("auto", instruction), "vulnerability")
        self.assertIn("漏洞或受影响版本", workflows.verify("auto", instruction, []))


if __name__ == "__main__":
    unittest.main()
