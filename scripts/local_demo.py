#!/usr/bin/env python3
"""本地演示：真实任务/审批内核 + 明确标记的固定模拟数据，完全不连接外部服务。

    .venv/bin/python scripts/local_demo.py --seed

管理后台 http://127.0.0.1:8801/，飞书模拟台 /demo。
首次登录 local-demo / local-demo-2026；数据库默认独立位于 data/local-demo。
此脚本不是生产入口，也不是任意代码的安全沙箱。
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
import uuid
from unittest.mock import patch
from contextlib import ExitStack


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "本地模拟 · 综合助手"
CHAT = "demo-chat"
USER = "demo-user"
REVIEWER = "demo-reviewer"
DISCLAIMER = "[本地模拟] 固定演示数据；未连接飞书、模型、生产集群或漏洞服务。"


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "local-demo")
    parser.add_argument("--seed", action="store_true", help="幂等创建集群、日志、漏洞三个样例任务")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    directory = args.data_dir.expanduser().resolve()
    if directory in (ROOT, ROOT / "data"):
        parser.error("演示必须使用独立目录，不能复用项目或生产 data 目录")
    marker = directory / ".local-demo"
    if (directory / "agent.db").exists() and not marker.is_file():
        parser.error("该目录已有非演示数据库；请选择空目录，避免读取真实配置")
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text("feishu-agent-local-demo-v1\n", encoding="utf-8")
    args.data_dir = directory
    return args


def configure_environment(directory):
    # 先设置环境再导入 core，既不读 config.yaml，也不继承生产引导凭据。
    os.environ["FEISHU_AGENT_DATA_DIR"] = str(directory)
    os.environ["FEISHU_AGENT_CONFIG"] = ""
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
                 "ADMIN_USERNAME", "ADMIN_PASSWORD", "WEB_SESSION_SECRET", "HARNESS_SANDBOX_IMAGE"):
        os.environ.pop(name, None)
    sys.path.insert(0, str(ROOT))


def reject_external(*args, **kwargs):
    raise OSError("本地演示禁止外部连接和子进程；请使用真实部署入口进行集成联调")


def fixture_cluster(target, command, timeout=30):
    return {"simulation": True, "target": target, "command": command, "exit_code": 0,
            "stdout": "[模拟数据]\nNAME                 READY STATUS            RESTARTS\n"
                      "demo-api-healthy     1/1   Running           0\n"
                      "demo-api-unhealthy   0/1   CrashLoopBackOff  5\n", "stderr": ""}


def fixture_logs(stream, where=None, period="15m", limit=50, org=None):
    return {"simulation": True, "org": org or "default", "stream": stream, "period": period,
            "count": 2, "hits": [
                {"timestamp": "2026-01-01T00:00:00Z", "level": "error", "service": "demo-api",
                 "message": "[模拟] database connection timeout after 3000ms", "trace_id": "demo-trace-1"},
                {"timestamp": "2026-01-01T00:00:01Z", "level": "error", "service": "demo-api",
                 "message": "[模拟] request failed: upstream unavailable", "trace_id": "demo-trace-1"}]}


def fixture_cve(vuln_id):
    return {"simulation": True, "id": "DEMO-CVE-0001", "requested_id": vuln_id,
            "summary": "虚构演示漏洞：demo-package < 2.0.1 的输入校验缺陷",
            "package": "demo-package", "affected": "<2.0.1", "fixed_version": "2.0.1",
            "source": "本地 fixture，非真实漏洞公告", "asset_version_verified": False}


def fixture_model(client, model, msgs, tools, extra_body, on_delta):
    from harness import tasks
    task = tasks.current() or {}
    instruction = task.get("instruction") or ""
    workflow = task.get("workflow") or "general"
    if msgs[-1].get("role") == "tool":
        try:
            result = json.loads(msgs[-1].get("content") or "{}")
        except (ValueError, TypeError):
            result = {}
        tool = msgs[-1].get("tool_call_id", "").removeprefix("demo-")
        if result.get("error"):
            answer = "本次演示动作被内核拒绝或执行失败：" + str(result["error"])
        elif tool == "kubectl":
            answer = ("样例巡检查询已完成：demo-api-unhealthy 为 CrashLoopBackOff，重启 5 次。"
                      "证据来自本次 kubectl 模拟返回。下一步应核对真实事件、启动日志和依赖；未执行集群变更。")
        elif tool == "query_logs":
            answer = ("样例日志查询已完成：两条错误共享 demo-trace-1，数据库连接超时后出现上游不可用。"
                      "关联关系有日志证据，根因尚待验证；下一步检查连接池、数据库健康与超时配置。")
        elif tool == "cve_detail":
            answer = ("已查询虚构示例 DEMO-CVE-0001：demo-package < 2.0.1 的输入校验缺陷，"
                      "样例修复版本为 2.0.1。尚未核对任何真实资产版本。"
                      "真实修复需要授权仓库、隔离测试和审批后的 MR；没有完成漏洞修复或发布。")
        else:
            answer = "审批后的演示笔记已写入独立演示数据库。"
        return DISCLAIMER + "\n\n" + answer, [], None
    calls = {
        "cluster": ("kubectl", {"target": "demo-cluster", "command": "get pods -n demo-api"}),
        "logs": ("query_logs", {"org": "default", "stream": "k8s_demo_api", "period": "15m",
                                "where": "level = 'error'"}),
        "vulnerability": ("cve_detail", {"id": "DEMO-CVE-0001"}),
    }
    # 只写演示数据库的笔记，用来验证等待审批、拒绝/批准及恢复，无远端副作用。
    if instruction.startswith("记住"):
        action = ("remember", {"note": "[本地演示笔记] " + instruction[2:].lstrip("：: ")})
    else:
        action = calls.get(workflow)
    if action:
        name, args = action
        return DISCLAIMER, [{"id": "demo-" + name, "name": name,
                             "args": json.dumps(args, ensure_ascii=False)}], None
    return (DISCLAIMER + "\n\n这个离线演示只提供集群巡检、日志分析、漏洞查询三个固定样例。"
            "输入“记住：演示验收项”可体验写入前审批。其他任务只展示受理流程。"
            "研发与漏洞修复需要在正式环境配置授权仓库和隔离 runner，不会在这里伪造测试或 MR。"), [], None


def setup(store):
    providers = store.list_providers()
    provider = next((p for p in providers if p["name"] == "local-demo-fixture"), None)
    pid = provider["id"] if provider else store.create_provider(
        "local-demo-fixture", "https://model.invalid/v1", "offline-fixture", "demo-no-secret")
    app = next((a for a in store.list_apps() if a["name"] == APP_NAME), None)
    aid = app["id"] if app else store.create_app(APP_NAME, ai_provider_id=pid)
    store.update_app(aid, enabled=1, ai_provider_id=pid, system_prompt=DISCLAIMER,
                     tools=["kubectl", "query_logs", "cve_detail", "remember"], skills=[], mcp=[],
                     policy={"preset": "approval"}, approvers=[REVIEWER], resources={
                         "users": [USER, "admin:local-demo"], "chats": [CHAT],
                         "k8s_clusters": ["demo-cluster"], "namespaces": ["demo-api"],
                         "log_orgs": ["default"], "log_streams": ["k8s_demo_api"]})
    store.set_setting("k8s_clusters", json.dumps([
        {"name": "demo-cluster", "read_only": True, "kubeconfig": "simulation-only"}]))
    store.set_setting("oo_org", "default")
    store.ensure_admin("local-demo", "local-demo-2026")
    with store._lock:
        store._conn.execute("CREATE TABLE IF NOT EXISTS demo_notifications ("
                            "id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, text TEXT, created REAL)")
        store._conn.commit()
    return aid


DEMO_HTML = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>飞书模拟台 · 本地演示</title>
<style>body{font:16px/1.6 system-ui;max-width:1000px;margin:32px auto;padding:0 20px;background:#f5f7fa;color:#182538}
section,article{background:white;border:1px solid #dae1e9;border-radius:12px;padding:18px;margin:14px 0}
h1{margin-bottom:4px}h2{font-size:19px}textarea{box-sizing:border-box;width:100%;font:inherit;padding:12px}
button,input{font:inherit;padding:8px 12px;margin:4px}button{cursor:pointer}small{color:#576577}
pre{white-space:pre-wrap;word-break:break-word}#error{color:#a12121}.badge{color:#915000;background:#fff0cf;padding:4px 10px}</style>
<h1>飞书模拟台</h1><span class="badge">离线演示 · 固定模拟数据</span>
<p>真实任务队列、权限检查、审批与恢复；模型、集群、日志、漏洞及飞书通知均为本地替身。<a href="/">打开管理后台</a></p>
<section id="login"><h2>登录演示账号</h2><input id="username" value="local-demo" aria-label="用户名">
<input id="password" type="password" value="local-demo-2026" aria-label="密码"><button onclick="login()">登录</button></section>
<section><h2>交代任务</h2><small>模拟发起人 demo-user · 会话 demo-chat。审批按钮模拟独立审批人 demo-reviewer。</small>
<p><button onclick="sample('巡检集群 demo-cluster')">巡检集群</button><button onclick="sample('分析 demo-api 问题日志')">分析日志</button>
<button onclick="sample('查询 demo-package 漏洞')">查询漏洞</button><button onclick="sample('记住：演示验收项')">体验审批</button></p>
<textarea id="message" rows="3" placeholder="例如：巡检集群 demo-cluster"></textarea><button onclick="send()">提交模拟飞书消息</button>
<small>研发修复需真实仓库和隔离 runner；演示不会生成虚假的测试通过记录或 MR。</small><p id="error"></p></section>
<section><h2>待审批</h2><div id="approvals">登录后查看</div></section>
<section><h2>任务进展</h2><div id="tasks">登录后查看</div></section>
<section><h2>模拟飞书通知</h2><div id="notifications">登录后查看</div></section>
<script>
const el=id=>document.getElementById(id);let loggedIn=false;
async function api(path,body){let r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
let d=await r.json();if(!r.ok)throw Error(d.detail||'请求失败');return d}
function error(e){el('error').textContent=e.message}
function sample(s){el('message').value=s}
async function login(){try{await api('/api/login',{username:el('username').value,password:el('password').value});await refresh()}catch(e){error(e)}}
async function send(){try{await api('/api/local-demo/message',{text:el('message').value});el('message').value='';el('error').textContent='已提交模拟消息';await refresh()}catch(e){error(e)}}
async function decide(id,approve){try{await api('/api/local-demo/approvals/'+id,{approve});await refresh()}catch(e){error(e)}}
function block(container,title,body){let a=document.createElement('article'),h=document.createElement('strong'),p=document.createElement('pre');h.textContent=title;p.textContent=body;a.append(h,p);container.append(a);return a}
async function refresh(){try{let d=await api('/api/local-demo/state');loggedIn=true;el('login').hidden=true;
for(let id of ['tasks','approvals','notifications'])el(id).replaceChildren();
for(let a of d.approvals){let card=block(el('approvals'),a.tool+' · 等待 demo-reviewer',JSON.stringify(a.args,null,2));
for(let [label,ok] of [['批准一次',true],['拒绝',false]]){let b=document.createElement('button');b.textContent=label;b.onclick=()=>decide(a.id,ok);card.append(b)}}
if(!d.approvals.length)el('approvals').textContent='暂无待审批动作';
for(let t of d.tasks)block(el('tasks'),t.instruction+' · '+t.status,'任务 '+t.id+'\n'+(t.result||t.error||t.progress||'等待执行'));
for(let n of d.notifications)block(el('notifications'),n.kind+' · '+new Date(n.created*1000).toLocaleString(),n.text)
}catch(e){if(loggedIn)error(e)}}
refresh();setInterval(()=>{if(loggedIn)refresh()},1500);
</script></html>"""


def main():
    args = arguments()
    configure_environment(args.data_dir)
    import requests
    import uvicorn
    from fastapi import HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from core import store, taskstore
    from feishu import bot
    from harness import approval, runs, tasks
    from llm import engine
    from obs import openobserve
    from ops import k8s
    from sec import feeds
    from web import admin

    aid = setup(store)
    taskstore.init()
    runs.abort_stale()
    approval.cancel_stale()

    def notify(kind, text, task_id="", notification_id=None):
        with store._lock:
            store._conn.execute("INSERT OR IGNORE INTO demo_notifications VALUES(?,?,?,?,?)",
                                (notification_id or uuid.uuid4().hex, task_id, kind, str(text), time.time()))
            store._conn.commit()

    def ask(req):
        notify("模拟审批卡", json.dumps(req, ensure_ascii=False), notification_id="approval-" + req["approval_id"])
        return "local-demo-approval-" + req["approval_id"]

    @admin.app.middleware("http")
    async def demo_boundary(request: Request, call_next):
        # 禁止后台重启到真实 app.main，也不允许在 demo 配置真实集成。
        path = request.url.path
        allowed_mutation = (path in ("/api/login", "/api/logout") or path == "/api/tasks"
                            or path.startswith("/api/local-demo/")
                            # webhook 只会在演示库里建模拟任务，允许在演示中走通配置与投递
                            or path.startswith(("/api/webhooks", "/hooks/"))
                            or (path.startswith("/api/tasks/") and path.endswith(("/cancel", "/resume"))))
        if request.method not in ("GET", "HEAD", "OPTIONS") and not allowed_mutation:
            return JSONResponse({"detail": "本地演示不允许修改集成配置、重启服务或启动外部操作"}, status_code=403)
        if path == "/":
            banner = ('<div style="padding:12px;background:#fff0cf;color:#573700;text-align:center">'
                      '本地离线演示 · 全部业务数据为模拟 · <a href="/demo">打开飞书模拟台</a></div>')
            return HTMLResponse(admin.index().replace("<body>", "<body>" + banner, 1))
        return await call_next(request)

    @admin.app.get("/demo", response_class=HTMLResponse)
    def demo_page():
        return DEMO_HTML

    @admin.app.get("/api/local-demo/state")
    def demo_state(request: Request):
        admin._auth(request)
        approvals = store._conn.execute("SELECT id FROM approvals WHERE app_id=? AND status='pending' ORDER BY created",
                                        (aid,)).fetchall()
        return {"simulation": True, "app_id": aid, "chat_id": CHAT,
                "tasks": [admin._task_view(t) for t in tasks.list_tasks(app_id=aid, limit=30)],
                "approvals": [store.get_approval(a["id"]) for a in approvals],
                "notifications": [dict(n) for n in store._conn.execute(
                    "SELECT * FROM demo_notifications ORDER BY created DESC LIMIT 30").fetchall()]}

    @admin.app.post("/api/local-demo/message")
    async def demo_message(request: Request):
        admin._auth(request)
        body = await admin._body(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise HTTPException(400, "请输入 1 到 10000 字的演示任务")
        event_id = "local-demo-" + uuid.uuid4().hex
        message = SimpleNamespace(chat_id=CHAT, message_id=event_id, chat_type="p2p", message_type="text",
                                  content=json.dumps({"text": text}, ensure_ascii=False), mentions=[])
        event = SimpleNamespace(message=message, sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=USER)))
        bot._on_message(aid, SimpleNamespace(event=event))
        return {"simulation": True, "accepted": True, "event_id": event_id}

    @admin.app.post("/api/local-demo/approvals/{approval_id}")
    async def demo_decide(approval_id: str, request: Request):
        admin._auth(request)
        body = await admin._body(request)
        if type(body.get("approve")) is not bool:
            raise HTTPException(400, "approve 必须为布尔值")
        ok, message, row = approval.decide(approval_id, body["approve"], REVIEWER, app_id=aid)
        return {"simulation": True, "ok": ok, "message": message, "status": (row or {}).get("status")}

    # 入口仅替换外部边界；engine、工具管道、审批、权限和 SQLite 任务状态照常执行。
    with ExitStack() as stack:
        for target, replacement in (
            ("llm.engine.OpenAI", lambda **kwargs: object()), ("llm.engine._call", fixture_model),
            ("ops.k8s.run", fixture_cluster), ("obs.openobserve.search", fixture_logs),
            ("sec.feeds.detail", fixture_cve), ("requests.sessions.Session.request", reject_external),
            ("socket.create_connection", reject_external), ("socket.socket.connect", reject_external),
            ("socket.socket.connect_ex", reject_external), ("subprocess.Popen", reject_external),
            ("feishu.bot._reply", lambda app, chat_id, text: notify("模拟命令回复", text))):
            stack.enter_context(patch(target, replacement))
        original_resolve = socket.getaddrinfo

        def local_resolve(host, *a, **kw):
            if host not in (None, "localhost", "127.0.0.1", "::1", b"127.0.0.1"):
                return reject_external()
            return original_resolve(host, *a, **kw)

        stack.enter_context(patch("socket.getaddrinfo", local_resolve))
        approval.register_answerer(ask, lambda row: notify("模拟审批结果", row["status"]))
        tasks.register_executor(bot.execute_task)
        tasks.register_sender(lambda task, text: notify("模拟飞书通知", text, task["id"],
                                                        task.get("notification_id")))
        if args.seed:
            for workflow, instruction in (("cluster", "巡检集群 demo-cluster"),
                                          ("logs", "分析 demo-api 问题日志"),
                                          ("vulnerability", "查询 demo-package 漏洞")):
                tasks.submit(aid, CHAT, instruction, source="local-demo", event_id="seed-v1-" + workflow,
                             speaker_open_id=USER, workflow=workflow)
        tasks.start(workers=2)
        print(f"\n{DISCLAIMER}\n后台 http://127.0.0.1:{args.port}/ · 模拟台 /demo\n"
              f"首次账号 local-demo / local-demo-2026 · 数据目录 {args.data_dir}\n", flush=True)
        try:
            uvicorn.run(admin.app, host="127.0.0.1", port=args.port, log_level="info")
        finally:
            tasks.stop()


if __name__ == "__main__":
    main()
