"""显式启用的真实 Docker 验收，不访问生产或公网。

HARNESS_LIVE_SANDBOX=1 HARNESS_SANDBOX_IMAGE=python:3.12-slim \
  python -m unittest discover -s tests -p test_sandbox_live.py -v
镜像必须已存在，不会自动拉取。
"""
import json
import os
from pathlib import Path
import tempfile
import unittest

from harness import sandbox


@unittest.skipUnless(os.environ.get("HARNESS_LIVE_SANDBOX") == "1", "显式启用本地 Docker 验收")
class LiveSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-live-sandbox-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def test_control_plane_and_network_isolation(self):
        fixture = self.root / "fixture.py"
        fixture.write_text('''import json, os, socket
from pathlib import Path
assert os.getuid() == 65532
for p in ('/code/config.yaml', '/code/data/agent.db', '/var/run/docker.sock'):
    assert not Path(p).exists(), p
try:
    Path('/input/should-not-write').write_text('x')
    raise AssertionError('input writable')
except OSError:
    pass
Path('/workspace/output.txt').write_text('sandbox only')
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(('198.51.100.1', 443))
    raise AssertionError('network reachable')
except OSError:
    pass
finally:
    s.close()
print(json.dumps({'isolated': True, 'uid': os.getuid()}))
''')
        fixture.chmod(0o644)
        result = sandbox.run(self.root, "python fixture.py", timeout=30)
        self.assertTrue(result["passed"], result)
        self.assertEqual(json.loads(result["stdout_tail"]), {"isolated": True, "uid": 65532})
        self.assertFalse((self.root / "output.txt").exists())

    def test_timeout_destroys_entire_execution(self):
        result = sandbox.run(self.root, 'python -c "import time; time.sleep(60)"', timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["stopped"])
        self.assertFalse(result["passed"])

    def test_cancellation_confirms_container_cleanup(self):
        calls = []
        def cancelled():
            calls.append(1)
            if len(calls) >= 3:
                raise RuntimeError("fixture task cancelled")
        with self.assertRaisesRegex(RuntimeError, "fixture task cancelled"):
            sandbox.run(self.root, 'python -c "import time; time.sleep(60)"', timeout=30,
                        check_active=cancelled)


if __name__ == "__main__":
    unittest.main()
