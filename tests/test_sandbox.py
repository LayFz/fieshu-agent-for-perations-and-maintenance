"""Docker 沙箱命令和失败路径测试；Docker 请求用假实现，输出流用本地无害子进程。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harness import sandbox


class SandboxTests(unittest.TestCase):
    def test_no_image_never_executes_host_code(self):
        with mock.patch.dict(os.environ, {"HARNESS_SANDBOX_IMAGE": ""}), \
             mock.patch.object(sandbox.subprocess, "run") as run:
            with self.assertRaises(sandbox.SandboxError):
                sandbox.resolve_image()
        run.assert_not_called()

    def test_create_uses_readonly_input_no_network_and_limits(self):
        command = "python -c 'print(42)'"
        args = sandbox._create_args("fixture", "/tmp/fixture", "sha256:test", command)
        for flag in ("--pull=never", "--network=none", "--read-only", "--user=65532:65532",
                     "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=512m", "--pids-limit=128"):
            self.assertIn(flag, args)
        self.assertEqual(args.count("--mount"), 1)
        self.assertEqual(args[args.index("--mount") + 1], "type=bind,src=/tmp/fixture,dst=/input,readonly")
        self.assertEqual(args[-1], command)
        self.assertNotIn("docker.sock", " ".join(map(str, args)))

    def test_image_with_unbounded_volumes_is_rejected(self):
        response = mock.Mock(returncode=0, stdout=json.dumps([{"Id": "sha256:test", "Config": {"Volumes": {"/data": {}}}}]))
        with mock.patch.dict(os.environ, {"HARNESS_SANDBOX_IMAGE": "fixture"}), \
             mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/docker"), \
             mock.patch.object(sandbox, "_docker", return_value=response):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.resolve_image()

    def _run_with_fake_docker(self, code, remove=True, timeout=5, check_active=None):
        original_popen = subprocess.Popen
        calls = []
        def docker(args, **kwargs):
            calls.append(args)
            if args[0] == "inspect":
                return mock.Mock(returncode=0, stdout=json.dumps({"Running": False, "Status": "exited", "ExitCode": 0}))
            return mock.Mock(returncode=0, stdout="container", stderr="")
        def popen(*args, **kwargs):
            return original_popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(sandbox, "resolve_image", return_value="sha256:test"), \
             mock.patch.object(sandbox, "_docker", side_effect=docker), \
             mock.patch.object(sandbox, "_remove", return_value=remove) as cleanup, \
             mock.patch.object(sandbox.subprocess, "Popen", side_effect=popen):
            result = sandbox.run(directory, "fixture command", timeout=timeout, check_active=check_active)
        cleanup.assert_called_once()
        return result, calls

    def test_output_is_bounded_and_real_exit_state_used(self):
        result, calls = self._run_with_fake_docker("print('x' * 200000)")
        self.assertTrue(result["passed"])
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(result["stdout_tail"]), sandbox.OUTPUT_LIMIT)
        self.assertIn("inspect", [c[0] for c in calls])

    def test_cleanup_failure_is_uncertain(self):
        with self.assertRaises(sandbox.SandboxUncertain):
            self._run_with_fake_docker("print('done')", remove=False)

    def test_timeout_does_not_claim_success(self):
        result, _ = self._run_with_fake_docker("import time; time.sleep(1.2)", timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["stopped"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
