"""工具集成层：解析真实资源，再交给 harness.access 校验。"""
import hashlib
import json
import re

from core import store
from harness import access, workspace, tasks
from ops import k8s, ssh, web
from obs import openobserve


def context_key(app):
    """绑定上下文的数据使用边界；配置含凭证时仅持久化 SHA256，不保存原文。"""
    provider = store.get_provider(app.get("ai_provider_id")) if app.get("ai_provider_id") else None
    data = {"app": {k: app.get(k) for k in (
        "id", "enabled", "resources", "tools", "skills", "mcp", "policy", "approvers",
        "system_prompt", "thinking", "ai_provider_id")},
        "provider": {k: (provider or {}).get(k) for k in ("id", "base_url", "model", "api_key")},
        "skills": store.skills_for(app.get("skills")),
        "mcp": [store.get_mcp_server(sid) for sid in (app.get("mcp") or [])]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def app_for(ctx):
    app = store.get_app(ctx.get("app_id"))
    task = tasks.current() or {}
    access.actor(app, ctx.get("chat_id"), ctx.get("speaker_open_id", ""),
                 system=task.get("trigger") in ("schedule", "job", "webhook"))
    return app


def prepare(name, args, ctx):
    args = dict(args)
    app = app_for(ctx)
    if name == "kubectl":
        cluster, _ = k8s._find_cluster(args.get("target"))
        if not cluster:
            raise PermissionError("请指定已登记且已授权的集群")
        args["target"] = cluster["name"]
        access.require(app, "k8s_clusters", args["target"])
        args["command"] = access.kubectl_scope(app, args.get("command"))
    elif name == "ssh_exec":
        # SSH 要求显式使用后台登记的名称，避免默认目标或别名改变审批目标。
        hosts = [h for h in ssh.hosts() if h.get("name") == args.get("target")]
        if not hosts:
            raise PermissionError("SSH 必须显式指定已登记的主机名称")
        access.require(app, "ssh_hosts", args["target"])
        identity = {k: hosts[0].get(k) for k in ("host", "port", "username")}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if args.get("_target_fingerprint") and args["_target_fingerprint"] != fingerprint:
            raise PermissionError("审批后 SSH 主机地址或身份发生变化，原审批失效")
        args["_target_fingerprint"] = fingerprint
    elif name == "code_open":
        access.require(app, "repos", args.get("repo"))
    elif name.startswith("code_") and name != "code_repos":
        ws = workspace.get_workspace(args.get("workspace"), ctx["app_id"], ctx["chat_id"])
        repo = store.get_code_repo(ws["repo_id"]) or {}
        access.require(app, "repos", repo.get("name"))
        args["workspace"] = ws["id"]
        if name in ("code_test", "code_propose_mr"):
            args = workspace.prepare_action(name, args, ctx)
    elif name in ("query_logs", "list_log_streams"):
        source, error = openobserve._pick(args.get("org"))
        if error:
            raise PermissionError("日志组织未配置")
        args["org"] = source["org"]
        access.require(app, "log_orgs", args["org"])
        if name == "query_logs":
            access.require(app, "log_streams", args.get("stream"))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(args.get("stream") or "")):
                raise PermissionError("日志流名称只能包含字母、数字和下划线")
            # 原工具支持 SQL where；只允许无子查询的过滤条件。
            where = str(args.get("where") or "").lower()
            if any(x in where for x in ("select", "union", ";", "--", "/*")):
                raise PermissionError("日志过滤条件不允许子查询或 SQL 注释")
    elif name in ("gitlab_project", "gitlab_pipelines", "gitlab_tree", "gitlab_read"):
        access.require(app, "gitlab_projects", args.get("project"))
    elif name == "gitlab_projects":
        access.require(app, "gitlab_projects", "*")
    elif name == "argocd_app":
        access.require(app, "argocd_apps", args.get("name"))
    elif name == "argocd_apps":
        access.require(app, "argocd_apps", "*")
    elif name in ("read_doc", "rewrite_doc"):
        access.require(app, "docs", args.get("doc_id"))
    elif name in ("list_nodes", "create_node"):
        access.require(app, "doc_spaces", args.get("space_id"))
    elif name == "list_knowledge_bases":
        access.require(app, "doc_spaces", "*")
    elif name == "web_search":
        # 搜索结果可能来自任意网站，等同于读取任意公网内容：要求显式授予全部域名
        access.require(app, "web_domains", "*")
    elif name == "web_fetch":
        host = web.host_of(args.get("url") or "")
        if not host:
            raise PermissionError("web_fetch 需要合法的 http(s) 网址")
        allowed = (app.get("resources") or {}).get("web_domains") or []
        if not (isinstance(allowed, list) and web.domain_allowed(allowed, host)):
            raise PermissionError(f"应用未获授权访问网站 {host}，请在应用资源权限 web_domains 中配置")
    elif name in ("vuln_report", "asset_list", "asset_refresh"):
        # 资产清单来自集群：至少要有一个集群授权；具体条目在执行时按集群/命名空间过滤
        if not (app.get("resources") or {}).get("k8s_clusters"):
            raise PermissionError("资产清单与漏洞报告需要应用至少被授权一个集群（资源权限 k8s_clusters）")
    elif name.startswith("box_"):
        # 工作机按名称授权；固定名称后审批/恢复都指向同一台
        args["box"] = args.get("box") or "default"
        access.require(app, "box", args["box"])
    elif name == "sandbox_run":
        access.require(app, "sandbox", "research")
        allowed = (app.get("resources") or {}).get("web_domains") or []
        for item in args.get("urls") or []:
            host = web.host_of(item.get("url") or "") if isinstance(item, dict) else ""
            if not host:
                raise PermissionError("sandbox_run 的 urls 需要合法的 http(s) 网址")
            if not (isinstance(allowed, list) and web.domain_allowed(allowed, host)):
                raise PermissionError(f"应用未获授权访问网站 {host}，请在应用资源权限 web_domains 中配置")
    elif name.startswith("mcp__"):
        sid = name.split("__", 2)[1]
        access.require(app, "mcp_servers", sid)
    return args


def validate(name, args, ctx):
    app = app_for(ctx)
    if ctx.get("context_key") and context_key(app) != ctx["context_key"]:
        raise tasks.TaskIncomplete("任务期间授权、技能或模型配置已变化，请核对原任务后创建新任务。")
    if name == "read_skill":
        current = {s["name"] for s in store.skills_for(app.get("skills"))}
        if args.get("name") not in current:
            raise PermissionError("该技能已从应用移除")
    if name.startswith("mcp__"):
        if int(name.split("__", 2)[1]) not in (app.get("mcp") or []):
            raise PermissionError("MCP 工具已停用")
    elif name != "read_skill" and name not in (app.get("tools") or []):
        raise PermissionError("工具已从应用移除")
    if name in ("code_test", "code_propose_mr"):
        workspace.validate_action(name, args, ctx)
        ws = workspace.get_workspace(args.get("workspace"), ctx["app_id"], ctx["chat_id"])
        access.require(app, "repos", (store.get_code_repo(ws["repo_id"]) or {}).get("name"))
    else:
        prepare(name, args, ctx)
