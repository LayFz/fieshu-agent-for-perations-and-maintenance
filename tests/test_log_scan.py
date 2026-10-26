"""日志巡检只使用聚合结果，故障正文不进入台账。"""
import os
import tempfile
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-log-tests-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from ops import log_scan


class LogScanTests(unittest.TestCase):
    def test_only_aggregate_count_is_requested_and_recorded(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"hits": [{"matched": 9}]}
        with mock.patch.object(log_scan.openobserve, "sources", return_value=[
                {"org": "default", "base": "http://logs.example:5080", "user": "reader", "pass": "secret"}]), \
             mock.patch.object(log_scan.requests, "post", return_value=response) as post:
            result = log_scan.scan("default", "k8s_api", 1800)
        self.assertEqual(result["sources"][0]["status"], "ok")
        self.assertEqual(result["summary"]["matched"], 9)
        self.assertEqual(result["findings"][0]["evidence"]["matched"], 9)
        sql = post.call_args.kwargs["json"]["query"]["sql"]
        self.assertIn("SELECT count(*)", sql)
        self.assertNotIn("SELECT *", sql)
        self.assertNotIn("secret", str(result))

    def test_http_body_is_not_persisted_as_error(self):
        response = mock.Mock(status_code=500, text="SECRET_FROM_LOG_SERVER")
        with mock.patch.object(log_scan.openobserve, "sources", return_value=[
                {"org": "default", "base": "http://logs.example:5080", "user": "reader", "pass": "secret"}]), \
             mock.patch.object(log_scan.requests, "post", return_value=response):
            result = log_scan.scan("default", "k8s_api")
        self.assertEqual(result["sources"][0]["status"], "error")
        self.assertNotIn("SECRET_FROM_LOG_SERVER", str(result))

    def test_stream_name_cannot_change_query(self):
        result = log_scan.scan("default", 'x";DROP TABLE')
        self.assertEqual(result["sources"][0]["status"], "error")
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
