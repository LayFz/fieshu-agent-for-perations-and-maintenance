"""飞书连接守护与扩展入口的离线回归，不连接真实飞书。"""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

_TEST_DIR = tempfile.TemporaryDirectory(prefix="feishu-bot-supervision-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _TEST_DIR.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from feishu import bot


class BotSupervisionTests(unittest.TestCase):
    def setUp(self):
        bot.stop(timeout=1)
        with bot._connection_lock:
            bot._connections.clear()
            bot._running.clear()
            bot._send_clients.clear()
        bot._supervisor_stop.clear()
        bot.register_task_executor(None)
        bot.register_command_handler(None)
        self.app = {"id": 1, "name": "fixture", "enabled": 1, "feishu_app_id": "fixture-app",
                    "feishu_app_secret": "fixture-secret", "resources": {"users": ["user"], "chats": ["chat"]}}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(bot.store, "list_apps", side_effect=lambda: [dict(self.app)]))
        self.stack.enter_context(mock.patch.object(bot.store, "get_app", side_effect=lambda aid: dict(self.app) if aid == 1 else None))
        self.stack.enter_context(mock.patch("requests.sessions.Session.request", side_effect=AssertionError("禁止真实HTTP")))
        self.stack.enter_context(mock.patch("socket.create_connection", side_effect=AssertionError("禁止真实连接")))

    def tearDown(self):
        bot.stop(timeout=1)
        bot.register_task_executor(None)
        bot.register_command_handler(None)
        self.stack.close()
        bot._last_engaged.clear()

    def join(self):
        bot._connections[1]["thread"].join(1)
        self.assertFalse(bot._connections[1]["thread"].is_alive())

    def test_addressed_group_message_records_chat_and_sender_for_binding(self):
        message = NS(chat_id="chat", message_id="observed-1", chat_type="group",
                     content=json.dumps({"text": "@_bot 我的身份"}),
                     mentions=[NS(key="@_bot", id=NS(open_id="bot"))])
        event = NS(event=NS(message=message, sender=NS(sender_id=NS(open_id="user"))))
        with mock.patch.object(bot.members, "bot_open_id", return_value="bot"), \
             mock.patch.object(bot, "_reply") as reply:
            bot._on_message(1, event)
        self.assertIn("当前 chat_id：chat", reply.call_args.args[2])
        observed = bot.store.list_observed_feishu_chats(1)
        self.assertEqual(observed[0]["chat_id"], "chat")
        self.assertEqual(observed[0]["chat_type"], "group")
        self.assertIn("user", observed[0]["sender_open_ids"])

    def test_crashed_thread_retries_with_bounded_backoff_and_safe_health(self):
        with mock.patch.object(bot, "_serve", side_effect=RuntimeError("wss://secret?token=fixture-secret")) as serve, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bot._supervise_once(), 1)
            self.join()
            first = bot.health()["apps"][0]
            self.assertEqual(first["state"], "retrying")
            self.assertFalse(first["connected"])
            self.assertEqual(first["failures"], 1)
            self.assertAlmostEqual(first["next_retry_at"] - first["last_exit_at"], 2)
            self.assertEqual(bot._supervise_once(now=first["next_retry_at"] - 0.01), 0)
            self.assertEqual(bot._supervise_once(now=first["next_retry_at"]), 1)
            self.join()
            second = bot.health()["apps"][0]
            self.assertEqual(second["failures"], 2)
            self.assertAlmostEqual(second["next_retry_at"] - second["last_exit_at"], 4)
            bot._connections[1].update(failures=100, next_retry_at=0)
            bot._supervise_once()
            self.join()
            bounded = bot.health()["apps"][0]
            self.assertAlmostEqual(bounded["next_retry_at"] - bounded["last_exit_at"], 60)
        self.assertEqual(serve.call_count, 3)
        self.assertNotIn("fixture-secret", json.dumps(bot.health()) + output.getvalue())
        self.assertNotIn("wss://", json.dumps(bot.health()) + output.getvalue())
        self.assertFalse(bot.is_running(1))

    def test_disabling_stops_existing_connection_without_retry(self):
        ready = threading.Event()
        def serve(app, entry):
            bot._mark_listening(entry, True)
            ready.set()
            entry["stop"].wait(2)
        with mock.patch.object(bot, "_serve", side_effect=serve) as mocked:
            bot._supervise_once()
            self.assertTrue(ready.wait(1))
            self.assertTrue(bot.is_running(1))
            self.app["enabled"] = 0
            bot._supervise_once()
            self.join()
            bot._supervise_once(now=time.time() + 1000)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(bot.health()["apps"][0]["state"], "disabled")
        self.assertFalse(bot.any_running())

    def test_changed_credentials_stop_old_thread_before_new_connection(self):
        seen = []
        ready = threading.Event()
        def serve(app, entry):
            seen.append(app["feishu_app_secret"])
            ready.set()
            entry["stop"].wait(2)
        with mock.patch.object(bot, "_serve", side_effect=serve):
            bot._supervise_once()
            self.assertTrue(ready.wait(1))
            old_entry = bot._connections[1]
            self.assertTrue(bot._connection_current(old_entry))
            self.app["feishu_app_secret"] = "rotated-secret"
            self.assertFalse(bot._connection_current(old_entry))
            bot._supervise_once()
            self.join()
            ready.clear()
            bot._supervise_once()
            self.assertTrue(ready.wait(1))
            self.assertIsNot(bot._connections[1], old_entry)
        self.assertEqual(seen, ["fixture-secret", "rotated-secret"])
        self.assertNotIn("rotated-secret", json.dumps(bot.health()))

    def test_start_all_is_idempotent_and_stop_joins_supervisor(self):
        ready = threading.Event()
        def serve(app, entry):
            ready.set()
            entry["stop"].wait(2)
        with mock.patch.object(bot, "_serve", side_effect=serve) as mocked:
            self.assertEqual(bot.start_all(), 1)
            self.assertTrue(ready.wait(1))
            self.assertEqual(bot.start_all(), 0)
            self.assertTrue(bot.health()["supervisor_running"])
            self.assertTrue(bot.stop(timeout=1))
        self.assertEqual(mocked.call_count, 1)
        self.assertFalse(bot.health()["supervisor_running"])
        self.assertTrue(bot.health()["stopping"])
        self.assertEqual(bot.health()["apps"][0]["state"], "stopped")

    def test_thread_liveness_alone_does_not_report_connected(self):
        ready = threading.Event()
        def serve(app, entry):
            ready.set()
            entry["stop"].wait(2)
        with mock.patch.object(bot, "_serve", side_effect=serve):
            bot._supervise_once()
            self.assertTrue(ready.wait(1))
            self.assertEqual(bot.health()["apps"][0]["state"], "starting")
            self.assertFalse(bot.is_running(1))
            bot._mark_listening(bot._connections[1], False)
            self.assertEqual(bot.health()["apps"][0]["state"], "listening")
            self.assertFalse(bot.health()["apps"][0]["connected"])

    def test_sdk_connection_is_observed_and_disconnected_on_stop(self):
        ready = threading.Event()
        disconnected = threading.Event()
        class Client:
            def __init__(self, *args, **kwargs):
                self.ended = asyncio.Event()
                self._conn = None
            async def _connect(self):
                self._conn = object()
            async def _ping_loop(self):
                ready.set()
                await asyncio.sleep(10)
            async def _disconnect(self):
                self._conn = None
                disconnected.set()
        with mock.patch.object(bot, "_SupervisedClient", Client):
            bot._supervise_once()
            self.assertTrue(ready.wait(1))
            self.assertTrue(bot.health()["apps"][0]["connected"])
            self.assertTrue(bot.stop(timeout=1))
            self.assertTrue(disconnected.is_set())
        self.assertFalse(bot.is_running(1))

    def test_sdk_bootstrap_is_bounded_and_log_arguments_are_redacted(self):
        client = bot._SupervisedClient.__new__(bot._SupervisedClient)
        client._domain = "https://feishu.example"
        client._app_id, client._app_secret, client._user_agent = "fixture-app", "fixture-secret", "fixture-agent"
        payload = {"code": 0, "data": {"URL": "wss://socket.example?token=fixture-secret"}}
        with mock.patch.object(bot.requests, "post", return_value=NS(status_code=200, content=json.dumps(payload).encode())) as post:
            self.assertEqual(client._get_conn_url(), payload["data"]["URL"])
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 15))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        log = client._fmt_log("connected {} err {}", payload["data"]["URL"], RuntimeError("fixture-secret"))
        self.assertNotIn("fixture-secret", log)
        self.assertNotIn("wss://", log)

    def test_real_sdk_connect_and_stop_use_only_mocked_transport(self):
        pinged = threading.Event()
        closed = threading.Event()
        class Socket:
            def __init__(self):
                self.done = asyncio.Event()
            async def recv(self):
                await self.done.wait()
                raise ConnectionError("fixture-secret")
            async def send(self, data):
                pinged.set()
            async def close(self):
                self.done.set()
                closed.set()
        async def connect(*args, **kwargs):
            return Socket()
        url = f"wss://socket.example/?{bot._wsc.DEVICE_ID}=device&{bot._wsc.SERVICE_ID}=1&token=fixture-secret"
        payload = {"code": 0, "data": {"URL": url}}
        with mock.patch.object(bot.requests, "post", return_value=NS(status_code=200, content=json.dumps(payload).encode())), \
             mock.patch.object(bot._wsc.websockets, "connect", side_effect=connect):
            bot._supervise_once()
            self.assertTrue(pinged.wait(1))
            self.assertTrue(bot.health()["apps"][0]["connected"])
            self.assertFalse(bot._connections[1]["client"]._auto_reconnect)
            self.assertTrue(bot.stop(timeout=1))
            self.assertTrue(closed.is_set())

    def task(self, trigger="chat"):
        return {"app_id": 1, "chat_id": "chat", "speaker_open_id": "user", "trigger": trigger, "instruction": "巡检"}

    def test_task_hook_runs_after_actor_and_falls_back_when_unhandled(self):
        hook = mock.Mock(return_value="巡检已完成")
        bot.register_task_executor(hook)
        with mock.patch.object(bot, "llm_run", return_value="模型结果") as llm:
            self.assertEqual(bot.execute_task(self.task()), "巡检已完成")
            llm.assert_not_called()
            hook.return_value = None
            self.assertEqual(bot.execute_task(self.task()), "模型结果")
            llm.assert_called_once()
            hook.reset_mock()
            self.app["enabled"] = 0
            with self.assertRaises(PermissionError):
                bot.execute_task(self.task())
            hook.assert_not_called()

    def test_proactive_cannot_invoke_task_hook_and_unregistered_path_works(self):
        hook = mock.Mock(return_value="不应发生")
        bot.register_task_executor(hook)
        with mock.patch.object(bot, "llm_run", return_value="模型结果") as llm:
            self.assertEqual(bot.execute_task(self.task("proactive")), "模型结果")
            hook.assert_not_called()
            self.assertTrue(llm.call_args.kwargs["proactive"])
            bot.register_task_executor(None)
            self.assertEqual(bot.execute_task(self.task()), "模型结果")

    def test_command_hook_is_after_authorization_and_default_commands_win(self):
        hook = mock.Mock(return_value="巡检已开启")
        bot.register_command_handler(hook)
        self.assertIn("当前飞书 open_id：user", bot._task_command(1, "chat", "我的身份", "user"))
        hook.assert_not_called()
        self.assertEqual(bot._task_command(1, "chat", "开启巡检", "user"), "巡检已开启")
        hook.assert_called_once_with(1, "chat", "开启巡检", "user", None)
        hook.reset_mock()
        with mock.patch.object(bot.tasks, "list_tasks", return_value=[]):
            self.assertIn("最近任务", bot._task_command(1, "chat", "任务列表", "user"))
        hook.assert_not_called()
        with self.assertRaises(PermissionError):
            bot._task_command(1, "chat", "开启巡检", "intruder")
        hook.assert_not_called()
        bot.register_command_handler(None)
        self.assertIsNone(bot._task_command(1, "chat", "普通消息", "user"))

    def test_unaddressed_group_message_does_not_run_command_hook(self):
        hook = mock.Mock(return_value="已开启")
        bot.register_command_handler(hook)
        def message(chat_type="group"):
            return NS(event=NS(message=NS(chat_id="chat", message_id="fixture-msg", chat_type=chat_type,
                mentions=[], content=json.dumps({"text": "开启巡检，异常告警"})), sender=NS(sender_id=NS(open_id="user"))))
        with mock.patch.object(bot, "_is_bot_mentioned", return_value=False), \
             mock.patch.object(bot.tasks, "submit") as submit, mock.patch.object(bot, "_reply") as reply:
            bot._on_message(1, message())
            hook.assert_not_called()
            self.assertEqual(submit.call_args.kwargs["trigger"], "proactive")
            bot._on_message(1, message("p2p"))
            hook.assert_called_once()
            reply.assert_called_once()


if __name__ == "__main__":
    unittest.main()
