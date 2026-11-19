"""真实接入/内核/模型循环集成；外部模型、飞书和运维服务均使用本地替身。"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

_test_dir = tempfile.TemporaryDirectory(prefix="feishu-integration-test-")
os.environ.setdefault("FEISHU_AGENT_DATA_DIR", _test_dir.name)
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

from fastapi.testclient import TestClient
from dulwich import porcelain
from core import store, taskstore
from feishu import bot
from harness import approval, tasks, workspace
from llm import engine
from web import admin


class SimulatedProcessDeath(BaseException):
    """不被业务 Exception 捕获，模拟已提交 checkpoint 后进程突然退出。"""


def tool(name, args, call_id="call-1"):
    return {"id": call_id, "name": name, "args": json.dumps(args, ensure_ascii=False)}


class HarnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        tasks.stop(timeout=1)
        self.assertNotEqual(Path(store.DB_PATH).parent, Path(__file__).resolve().parents[1] / "data")
        with taskstore.transaction() as conn:
            for table in ("task_outbox", "task_actions", "tasks", "approvals", "events", "runs", "msgs",
                          "notes", "token_usage", "schedules", "apps", "ai_providers", "admin", "kv"):
                conn.execute("DELETE FROM " + table)
        provider = store.create_provider("本地模型替身", "https://model.invalid/v1", "fixture-model", "fixture-key")
        self.app = store.create_app("集成测试应用", ai_provider_id=provider,
                                    tools=["kubectl", "query_logs", "cve_detail", "remember"])
        store.update_app(self.app, policy={"preset": "readonly"}, approvers=["reviewer"],
                         resources={"k8s_clusters": ["test-cluster"], "namespaces": ["test-ns"],
                                    "log_orgs": ["default"], "log_streams": ["k8s_test_ns"]})
        store.set_setting("k8s_clusters", json.dumps([{"name": "test-cluster", "read_only": True,
                                                      "kubeconfig": "fixture-not-a-real-config"}]))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # 遗漏的模拟必须立即失败，不允许测试触达真实网络。
        self.stack.enter_context(mock.patch("requests.sessions.Session.request", side_effect=AssertionError("测试禁止外部 HTTP")))
        self.stack.enter_context(mock.patch("socket.create_connection", side_effect=AssertionError("测试禁止外部连接")))
        self.stack.enter_context(mock.patch.object(engine, "OpenAI", return_value=mock.Mock()))
        self.model = self.stack.enter_context(mock.patch.object(engine, "_call"))
        self.cards = self.stack.enter_context(mock.patch.object(bot, "_create_card", return_value=NS(success=lambda: True, code=0)))
        self.asks = []
        def ask(req):
            self.asks.append(req)
            return "approval-card-" + req["approval_id"]
        self.stack.enter_context(mock.patch.object(approval, "_ask", side_effect=ask))
        self.stack.enter_context(mock.patch.object(approval, "_finalize", return_value=None))
        self.cluster = self.stack.enter_context(mock.patch("ops.k8s.run", return_value={
            "exit_code": 0, "stdout": "api-0 Running Ready 1/1", "stderr": ""}))
        self.logs = self.stack.enter_context(mock.patch("obs.openobserve.search", return_value={
            "total": 1, "hits": [{"message": "fixture timeout", "_timestamp": 123}]}))
        self.cve = self.stack.enter_context(mock.patch("sec.feeds.detail", return_value={
            "id": "CVE-2026-12345", "affected": [{"package": "fixture", "version": "1.0"}]}))
        tasks.register_executor(bot.execute_task)
        tasks.register_sender(bot.deliver_task)

    def tearDown(self):
        tasks.stop(timeout=1)
        tasks.register_executor(None)
        tasks.register_sender(None)

    def submit(self, text, **kwargs):
        return tasks.submit(self.app, "chat-test", text, speaker_open_id="requester", **kwargs)[0]

    def respond(self, name, args, answer="已查询，证据见结果。", retries=0):
        # retries：宿主验收未通过时会再给模型两次补证据的机会；这里模拟模型仍只给文字答复
        self.model.side_effect = [("", [tool(name, args)], None)] + [(answer, [], None)] * (1 + retries)

    def nudged(self):
        last = self.model.call_args.args[2] if self.model.call_args.args else self.model.call_args.kwargs.get("messages", [])
        return any("宿主验收未通过" in (m.get("content") or "") for m in last if isinstance(m, dict))

    @staticmethod
    def message(text, message_id="feishu-message-1"):
        return NS(event=NS(message=NS(chat_id="chat-test", message_id=message_id, chat_type="p2p",
            mentions=[], content=json.dumps({"text": text})), sender=NS(sender_id=NS(open_id="requester"))))

    def test_feishu_duplicate_to_cluster_workflow_and_outbox(self):
        self.respond("kubectl", {"target": "test-cluster", "command": "get pods -n test-ns"})
        event = self.message("检查集群 Pod 状态")
        bot._on_message(self.app, event)
        bot._on_message(self.app, event)
        rows = tasks.list_tasks(app_id=self.app)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.cards.call_count, 0)  # 回调只入库，不等待飞书网络。
        tasks.tick()
        row = tasks.get(rows[0]["id"])
        self.assertEqual(row["status"], "completed", row["error"])
        self.assertEqual(row["workflow"], "cluster")
        self.assertEqual(self.cluster.call_count, 1)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.cards.call_count, 2)
        stable_ids = [call.args[3] for call in self.cards.call_args_list]
        self.assertEqual(len(set(stable_ids)), 2)
        self.assertTrue(all(stable_ids))
        self.assertEqual(row["checkpoint"]["evidence"][0]["tool"], "kubectl")
        self.assertEqual(len(store.chat_messages(self.app, "chat-test")), 2)
        self.assertEqual(taskstore.action_get(row["id"], "1:call-1")["state"], "completed")

    def test_log_workflow_runs_authorized_query(self):
        self.respond("query_logs", {"org": "default", "stream": "k8s_test_ns", "where": "level='error'"})
        task = self.submit("查一下服务的问题日志")
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["workflow"], "logs")
        self.assertEqual(row["status"], "completed", row["error"])
        self.logs.assert_called_once_with("k8s_test_ns", "level='error'", "15m", 50, "default")

    def test_log_workflow_requires_log_or_event_evidence(self):
        cases = (("get pods -n test-ns", "needs_attention"),
                 ("logs api-0 -n test-ns", "completed"),
                 ("get events -n test-ns", "completed"))
        for command, expected in cases:
            with self.subTest(command=command):
                self.respond("kubectl", {"target": "test-cluster", "command": command},
                             retries=2 if expected == "needs_attention" else 0)
                task = self.submit("排查服务问题日志", workflow="logs")
                tasks.tick()
                row = tasks.get(task["id"])
                self.assertEqual(row["status"], expected, row["error"])
                evidence = row["checkpoint"]["evidence"]
                self.assertEqual(len(evidence), 1)
                self.assertEqual(evidence[0]["tool"], "kubectl")
                self.assertEqual(evidence[0]["args"]["command"], command)
                if expected == "needs_attention":
                    self.assertIn("日志", row["error"])

    def test_budget_exhausted_delivers_report_from_evidence(self):
        # token 预算耗尽时不再空手标「待核对」：不带工具降级调用一次，用已有证据交付报告。
        usage = NS(prompt_tokens=10, completion_tokens=10, total_tokens=200000)
        self.model.side_effect = [
            ("", [tool("kubectl", {"target": "test-cluster", "command": "get pods -n test-ns"})], usage),
            ("基于已查到的证据：集群正常。", [], None),
        ]
        task = self.submit("生成集群运行报告", workflow="cluster")
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "completed", row["error"])
        self.assertIn("集群正常", row["result"])
        # 降级调用不带工具：最后一次模型调用的 messages 里没有 tools schema。
        last_call = self.model.call_args
        self.assertIsNone(last_call.args[3])
        fallback_msg = last_call.args[2][-1]
        self.assertIn("token 预算", fallback_msg["content"])

    def test_oversized_tool_result_is_truncated_in_context_but_kept_in_evidence(self):
        long_stdout = "x" * 20000
        self.cluster.return_value = {"exit_code": 0, "stdout": long_stdout, "stderr": ""}
        self.respond("kubectl", {"target": "test-cluster", "command": "get pods -n test-ns"})
        task = self.submit("检查集群 Pod 状态")
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "completed", row["error"])
        # evidence 保留完整结果；上下文里的 tool 消息被截断并注明。
        evidence = json.dumps(row["checkpoint"]["evidence"], ensure_ascii=False)
        self.assertEqual(evidence.count(long_stdout), 1)
        msgs = self.model.call_args.args[2]
        tool_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "tool"]
        self.assertTrue(tool_msgs)
        self.assertLess(max(len(m["content"]) for m in tool_msgs), 8000)
        self.assertIn("已截断", tool_msgs[-1]["content"])
        self.cluster.return_value = {"exit_code": 0, "stdout": "api-0 Running Ready 1/1", "stderr": ""}

    def test_history_and_notes_are_isolated_by_app_chat_and_context(self):
        other_app = store.create_app("另一个记忆隔离应用")
        store.save(self.app, "x", "user", "legacy history")
        store.save(self.app, "x", "user", "context a history", context_key="a")
        self.assertEqual(store.load_history(self.app, "x", 20, context_key="b"), [])
        self.assertEqual(store.load_history(self.app, "y", 20, context_key="a"), [])
        self.assertEqual(store.load_history(other_app, "x", 20, context_key="a"), [])
        self.assertEqual(store.load_history(self.app, "x", 20, context_key="a"),
                         [{"role": "user", "content": "context a history"}])
        store.save(self.app, "x", "assistant", "context b history", context_key="b")
        self.assertEqual(store.load_history(self.app, "x", 20, context_key="b"),
                         [{"role": "assistant", "content": "context b history"}])
        self.assertEqual([item["content"] for item in store.load_history(self.app, "x", 20)],
                         ["legacy history", "context a history", "context b history"])
        self.assertEqual(len(store.chat_messages(self.app, "x")), 3)  # 管理端仍可查询完整留痕。

        store.add_note(self.app, "legacy app note")
        store.add_note(self.app, "context a note", scope="chat:x:a")
        store.add_note(self.app, "context b note", scope="chat:x:b")
        store.add_note(self.app, "other chat note", scope="chat:y:a")
        store.add_note(other_app, "other app note", scope="chat:x:a")
        self.assertEqual(store.load_notes(self.app, scope="chat:x:a"), ["context a note"])
        self.assertEqual(store.load_notes(self.app, scope="chat:x:b"), ["context b note"])
        self.assertEqual(store.load_notes(self.app, scope="chat:y:a"), ["other chat note"])
        self.assertEqual(store.load_notes(self.app, scope="chat:z:a"), [])
        self.assertEqual(store.load_notes(other_app, scope="chat:x:a"), ["other app note"])
        self.assertEqual(store.load_notes(self.app, scope="app"), ["legacy app note"])
        self.assertCountEqual(store.load_notes(self.app),
                              ["legacy app note", "context a note", "context b note", "other chat note"])

    def test_vulnerability_analysis_has_evidence_but_repair_needs_test_and_mr(self):
        self.respond("cve_detail", {"id": "CVE-2026-12345"})
        analysis = self.submit("查询漏洞 CVE-2026-12345 的影响")
        tasks.tick()
        self.assertEqual(tasks.get(analysis["id"])["status"], "completed")
        self.assertTrue(tasks.get(analysis["id"])["checkpoint"]["tainted"])
        self.respond("cve_detail", {"id": "CVE-2026-12345"}, "漏洞已经修复。", retries=2)
        repair = self.submit("修复漏洞 CVE-2026-12345")
        tasks.tick()
        row = tasks.get(repair["id"])
        self.assertEqual(row["status"], "needs_attention")
        self.assertTrue(self.nudged())
        self.assertIn("隔离测试", row["error"])
        self.assertEqual(self.cve.call_count, 2)

    def test_vulnerability_repair_passes_two_approvals_without_repeating_work(self):
        # 真正的 Git 工作区、文件修改、快照、审批和恢复；仅隔离外部模型/沙箱/GitLab。
        temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="feishu-repair-")))
        source = temp / "source"
        source.mkdir()
        (source / "requirements.txt").write_text("fixture==1.0\n")
        (source / "test_version.py").write_text(
            "from pathlib import Path\n"
            "assert Path('requirements.txt').read_text() == 'fixture==1.1\\n'\n")
        with porcelain.init(str(source)) as repo:
            repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
            porcelain.add(repo, paths=["requirements.txt", "test_version.py"])
            porcelain.commit(repo, message=b"vulnerable fixture",
                             author=b"Fixture <fixture@example.invalid>",
                             committer=b"Fixture <fixture@example.invalid>")
            base_sha = repo.head().decode()
        repo_name = temp.name
        store.create_code_repo(name=repo_name, url="https://git.invalid/group/fixture", branch="main",
                               write_token="fixture-write-token", test_cmd="python test_version.py", enabled=1)
        store.update_app(self.app, policy={"preset": "approval"}, resources={"repos": [repo_name]},
                         tools=["cve_detail", "code_open", "code_read", "code_edit", "code_test", "code_propose_mr"])
        self.stack.enter_context(mock.patch.object(workspace, "_root", return_value=temp / "workspaces"))
        self.stack.enter_context(mock.patch.object(workspace.sandbox, "resolve_image", return_value="sha256:fixture"))

        def clone_fixture(url, branch, target, username, token):
            self.assertEqual(url, "https://git.invalid/group/fixture")
            self.assertEqual(branch, "main")
            shutil.copytree(source, target)
            return base_sha

        clone = self.stack.enter_context(mock.patch.object(workspace.gitutil, "clone", side_effect=clone_fixture))
        read = self.stack.enter_context(mock.patch.object(workspace, "read_file", wraps=workspace.read_file))
        edit = self.stack.enter_context(mock.patch.object(workspace, "edit_file", wraps=workspace.edit_file))
        test = self.stack.enter_context(mock.patch.object(workspace, "run_tests", wraps=workspace.run_tests))
        propose = self.stack.enter_context(mock.patch.object(workspace, "propose_mr", wraps=workspace.propose_mr))

        def sandbox_success(path, command, **kwargs):
            snapshot_dir = Path(path)
            self.assertFalse((snapshot_dir / ".git").exists())
            self.assertFalse((snapshot_dir / "agent.db").exists())
            self.assertEqual((snapshot_dir / "requirements.txt").read_text(), "fixture==1.1\n")
            self.assertEqual(command, "python test_version.py")
            self.assertNotEqual(snapshot_dir, Path(store.list_workspaces(app_id=self.app)[0]["path"]) / "repo")
            kwargs["check_active"]()
            return {"passed": True, "exit_code": 0, "stdout": "fixture test passed", "stderr": "",
                    "image": "sha256:fixture", "duration": 0.01}

        sandbox = self.stack.enter_context(mock.patch.object(workspace.sandbox, "run", side_effect=sandbox_success))
        mr_url = "https://git.invalid/group/fixture/-/merge_requests/1"
        def response(payload):
            return mock.Mock(status_code=200, json=lambda: payload)
        gitlab = self.stack.enter_context(mock.patch.object(workspace.requests, "request", side_effect=[
            response({"id": "fixture-fixed-commit"}), response([]), response({"web_url": mr_url, "iid": 1})]))
        self.model.side_effect = [
            ("", [tool("cve_detail", {"id": "CVE-2026-12345"}, "cve")], None),
            ("", [tool("code_open", {"repo": repo_name}, "open")], None),
            ("", [tool("code_read", {"path": "requirements.txt"}, "read")], None),
            ("", [tool("code_edit", {"path": "requirements.txt", "old": "fixture==1.0",
                                       "new": "fixture==1.1"}, "edit")], None),
            ("", [tool("code_test", {}, "test")], None),
            ("", [tool("code_propose_mr", {"title": "Fix CVE-2026-12345",
                                             "description": "Upgrade fixture from 1.0 to 1.1."}, "mr")], None),
            ("修复 MR 已创建：" + mr_url + "。隔离测试通过，等待人工评审、发布与复扫。", [], None),
        ]
        task = self.submit("修复漏洞 CVE-2026-12345，升级依赖并提 MR")
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "waiting_approval", row["error"])
        self.assertEqual(row["workflow"], "vulnerability")
        self.assertEqual(self.model.call_count, 5)
        self.assertEqual([req["tool"] for req in self.asks], ["code_test"])
        clone.assert_called_once()
        read.assert_called_once()
        edit.assert_called_once()
        sandbox.assert_not_called()
        gitlab.assert_not_called()
        ws = store.list_workspaces(app_id=self.app)[0]
        self.assertEqual(workspace._meta(ws["id"])["task_id"], task["id"])
        self.assertEqual((Path(ws["path"]) / "repo" / "requirements.txt").read_text(), "fixture==1.1\n")
        self.assertEqual((source / "requirements.txt").read_text(), "fixture==1.0\n")
        test_approval = self.asks[0]["approval_id"]
        snapshot = self.asks[0]["args"]["_snapshot"]
        self.assertIn("python test_version.py", self.asks[0]["preview"])

        self.assertTrue(approval.decide(test_approval, True, "reviewer", app_id=self.app)[0])
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "waiting_approval", row["error"])
        self.assertEqual(self.model.call_count, 6)  # 继续生成 MR 动作；不重发此前的测试计划。
        self.assertEqual([req["tool"] for req in self.asks], ["code_test", "code_propose_mr"])
        sandbox.assert_called_once()
        gitlab.assert_not_called()
        self.assertEqual(self.asks[1]["args"]["_snapshot"], snapshot)
        proof = json.loads(workspace._meta(ws["id"])["test_result"])
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["snapshot"], snapshot)
        mr_approval = self.asks[1]["approval_id"]

        # 首次审批已经完成，不能因此把等待第二次审批的任务反复领取/挂起。
        attempts = row["attempts"]
        run_count = store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        for _ in range(3):
            self.assertFalse(tasks.tick())
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "waiting_approval")
        self.assertEqual(row["attempts"], attempts)
        self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], run_count)
        self.assertEqual(self.model.call_count, 6)
        self.assertEqual(len(self.asks), 2)
        sandbox.assert_called_once()
        gitlab.assert_not_called()

        self.assertTrue(approval.decide(mr_approval, True, "reviewer", app_id=self.app)[0])
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "completed", row["error"])
        self.assertEqual(self.model.call_count, 7)
        self.assertIn(mr_url, row["result"])
        evidence = {entry["tool"]: entry["result"] for entry in row["checkpoint"]["evidence"]}
        self.assertEqual(evidence["code_test"]["snapshot"], snapshot)
        self.assertEqual(evidence["code_propose_mr"]["mr_url"], mr_url)
        self.assertEqual(gitlab.call_count, 3)
        commit_call, lookup_call, mr_call = gitlab.call_args_list
        self.assertEqual(commit_call.args, ("POST", "https://git.invalid/api/v4/projects/group%2Ffixture/repository/commits"))
        self.assertEqual(commit_call.kwargs["json"]["start_sha"], base_sha)
        self.assertEqual(commit_call.kwargs["json"]["actions"], [{"action": "update", "file_path": "requirements.txt",
                                                                 "content": "fixture==1.1\n", "encoding": "text"}])
        self.assertIn(snapshot, commit_call.kwargs["json"]["commit_message"])
        self.assertEqual(lookup_call.args[0], "GET")
        self.assertEqual(mr_call.kwargs["json"]["source_branch"], commit_call.kwargs["json"]["branch"])
        self.assertEqual(store.get_workspace(ws["id"])["mr_url"], mr_url)
        self.assertEqual(workspace._meta(ws["id"])["mr_state"], "completed")

        # 重复回调和再次轮询不能重复改文件、跑测试或创建远端提交。
        self.assertFalse(approval.decide(test_approval, True, "reviewer", app_id=self.app)[0])
        self.assertFalse(approval.decide(mr_approval, True, "reviewer", app_id=self.app)[0])
        tasks.tick()
        clone.assert_called_once()
        read.assert_called_once()
        edit.assert_called_once()
        test.assert_called_once()
        propose.assert_called_once()
        sandbox.assert_called_once()
        self.cve.assert_called_once()
        self.assertEqual(gitlab.call_count, 3)
        self.assertEqual(self.model.call_count, 7)
        self.assertEqual(len(self.asks), 2)
        self.assertEqual(len(store.chat_messages(self.app, "chat-test")), 2)

    def test_model_claim_without_cluster_evidence_is_incomplete(self):
        self.model.return_value = ("所有集群都正常。", [], None)
        task = self.submit("检查集群")
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "needs_attention")
        self.assertIn("查询证据", row["error"])
        delivered = json.dumps(self.cards.call_args.args[2], ensure_ascii=False)
        self.assertIn("所有集群都正常", delivered)
        self.assertIn("验收待完成", delivered)
        self.assertIn("查询证据", delivered)
        self.cluster.assert_not_called()

    def test_approval_resume_uses_saved_model_action_once(self):
        store.update_app(self.app, policy={"preset": "approval"})
        self.respond("remember", {"note": "fixture note"}, "已保存笔记。")
        task = self.submit("记住这个偏好", workflow="general")
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "waiting_approval")
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(store.load_notes(self.app), [])
        self.assertEqual(len(self.asks), 1)
        aid = self.asks[0]["approval_id"]
        self.assertTrue(approval.decide(aid, True, "reviewer", app_id=self.app)[0])
        tasks.tick()
        row = tasks.get(task["id"])
        self.assertEqual(row["status"], "completed", row["error"])
        self.assertEqual(self.model.call_count, 2)  # 第2次只生成动作完成后的答复。
        self.assertEqual(store.load_notes(self.app), ["fixture note"])
        context = row["checkpoint"]["context_key"]
        self.assertEqual(store.load_notes(self.app, scope=f"chat:chat-test:{context}"), ["fixture note"])
        self.assertEqual(store.load_notes(self.app, scope=f"chat:other-chat:{context}"), [])
        self.assertFalse(approval.decide(aid, True, "reviewer", app_id=self.app)[0])
        tasks.tick()
        self.assertEqual(store.load_notes(self.app), ["fixture note"])
        self.assertEqual(len(self.asks), 1)
        self.assertEqual(len(store.chat_messages(self.app, "chat-test")), 2)

    def test_restart_after_model_checkpoint_does_not_replan(self):
        self.respond("kubectl", {"target": "test-cluster", "command": "get pods -n test-ns"})
        task = self.submit("检查集群")
        real_checkpoint = tasks.checkpoint
        crashed = False
        def crash_after_save(data):
            nonlocal crashed
            real_checkpoint(data)
            if data.get("pending") and not crashed:
                crashed = True
                raise SimulatedProcessDeath()
        with mock.patch.object(tasks, "checkpoint", side_effect=crash_after_save):
            with self.assertRaises(SimulatedProcessDeath):
                tasks.tick()
        self.assertEqual(self.model.call_count, 1)
        self.cluster.assert_not_called()
        self.assertEqual(tasks.get(task["id"])["status"], "running")
        taskstore.recover_expired(now=time.time() + 120)
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "completed")
        self.assertEqual(self.model.call_count, 2)
        self.cluster.assert_called_once()

    def test_resume_rejects_changed_authorization_or_provider_configuration(self):
        original_app = store.get_app(self.app)
        provider_id = original_app["ai_provider_id"]
        original_provider = store.get_provider(provider_id)
        cases = (("resources", {"k8s_clusters": ["test-cluster"], "namespaces": ["test-ns", "new-ns"]}),
                 ("base_url", "https://changed-model.invalid/v1"),
                 ("model", "changed-fixture-model"),
                 ("api_key", "changed-fixture-key"))
        for field, value in cases:
            with self.subTest(field=field):
                self.model.reset_mock()
                self.cluster.reset_mock()
                self.respond("kubectl", {"target": "test-cluster", "command": "get pods -n test-ns"})
                task = self.submit("检查集群")
                real_checkpoint = tasks.checkpoint

                def crash_after_model_save(data):
                    real_checkpoint(data)
                    if data.get("pending"):
                        raise SimulatedProcessDeath()

                with mock.patch.object(tasks, "checkpoint", side_effect=crash_after_model_save):
                    with self.assertRaises(SimulatedProcessDeath):
                        tasks.tick()
                saved = tasks.get(task["id"])["checkpoint"]
                self.assertTrue(saved["pending"])
                self.assertNotIn(original_provider["api_key"], json.dumps(saved))
                self.assertEqual(self.model.call_count, 1)
                self.cluster.assert_not_called()
                try:
                    if field == "resources":
                        store.update_app(self.app, resources=value)
                    else:
                        store.update_provider(provider_id, **{field: value})
                    taskstore.recover_expired(now=time.time() + 120)
                    tasks.tick()
                    row = tasks.get(task["id"])
                    self.assertEqual(row["status"], "needs_attention", row["error"])
                    self.assertEqual(self.model.call_count, 1)
                    self.cluster.assert_not_called()
                    self.assertIsNone(taskstore.action_get(task["id"], "1:call-1"))
                    self.assertEqual(row["checkpoint"]["pending"], saved["pending"])
                finally:
                    if field == "resources":
                        store.update_app(self.app, resources=original_app["resources"])
                    else:
                        store.update_provider(provider_id, **{field: original_provider[field]})

    def test_restart_after_tool_ledger_does_not_execute_tool_twice(self):
        self.respond("kubectl", {"target": "test-cluster", "command": "get pods -n test-ns"})
        task = self.submit("检查集群")
        real_checkpoint = tasks.checkpoint
        def crash_before_context_save(data):
            if data.get("next_tool") == 1:
                raise SimulatedProcessDeath()
            real_checkpoint(data)
        with mock.patch.object(tasks, "checkpoint", side_effect=crash_before_context_save):
            with self.assertRaises(SimulatedProcessDeath):
                tasks.tick()
        self.assertEqual(taskstore.action_get(task["id"], "1:call-1")["state"], "completed")
        self.assertEqual(tasks.get(task["id"])["checkpoint"]["next_tool"], 0)
        taskstore.recover_expired(now=time.time() + 120)
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "completed")
        self.assertEqual(self.cluster.call_count, 1)
        self.assertEqual(self.model.call_count, 2)

    def test_task_api_requires_auth_and_preserves_uncertain_actions(self):
        store.ensure_admin("integration-admin", "test-password-only")
        with TestClient(admin.app) as client:
            self.assertEqual(client.get("/api/tasks").status_code, 401)
            self.assertEqual(client.post("/api/tasks", json={}).status_code, 401)
            self.assertEqual(client.post("/api/login", json={"username": "integration-admin", "password": "wrong"}).status_code, 401)
            self.assertEqual(client.post("/api/login", json={"username": "integration-admin", "password": "test-password-only"}).status_code, 200)
            body = {"app_id": self.app, "chat_id": "chat-api", "instruction": "查看状态",
                    "workflow": "general", "request_id": "api-event"}
            response = client.post("/api/tasks", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            tid = response.json()["task"]["id"]
            again = client.post("/api/tasks", json=body).json()
            self.assertFalse(again["created"])
            self.assertEqual(again["task"]["id"], tid)
            detail = client.get("/api/tasks/" + tid)
            self.assertEqual(detail.status_code, 200)
            self.assertNotIn("checkpoint", detail.json())
            self.assertIn("actions", detail.json())
            self.assertEqual(len(client.get("/api/tasks").json()["tasks"]), 1)
            self.assertTrue(client.post(f"/api/tasks/{tid}/cancel").json()["ok"])
            self.assertEqual(tasks.get(tid)["status"], "cancelled")
            self.assertEqual(client.get("/api/tasks/" + "0" * 32).status_code, 404)
            body["request_id"] = "uncertain-api-event"
            uncertain = client.post("/api/tasks", json=body).json()["task"]["id"]
            claimed = taskstore.claim("integration-crash")
            lease = {"lease_owner": claimed["lease_owner"], "lease_epoch": claimed["lease_epoch"]}
            taskstore.action_prepare(uncertain, "write", "fixture-write", {}, "write", **lease)
            taskstore.action_update(uncertain, "write", state="running", **lease)
            taskstore.recover_expired(now=time.time() + 120)
            self.assertEqual(client.post(f"/api/tasks/{uncertain}/resume", json={}).status_code, 409)
            self.assertEqual(tasks.get(uncertain)["status"], "needs_attention")

    def test_missing_provider_is_failure_not_completed(self):
        store.update_app(self.app, ai_provider_id=0)
        task = self.submit("普通咨询", workflow="general")
        tasks.tick()
        self.assertEqual(tasks.get(task["id"])["status"], "failed")
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
