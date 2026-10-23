"""宿主确定性资源边界。未配置资源拒绝；星号必须由管理员显式授予。"""
import shlex

KINDS = {"repos", "ssh_hosts", "k8s_clusters", "namespaces", "log_orgs", "log_streams",
         "gitlab_projects", "argocd_apps", "docs", "doc_spaces", "mcp_servers",
         "web_domains", "sandbox", "box", "users", "chats"}


def validate(resources):
    if not isinstance(resources, dict) or set(resources) - KINDS:
        raise ValueError("资源权限必须为 JSON 对象，且只能使用支持的资源类别")
    for key, values in resources.items():
        if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
            raise ValueError(f"{key} 必须为非空字符串组成的数组")
    return resources


def allowed(app, kind, value):
    resources = app.get("resources") or {}
    if not isinstance(resources, dict):
        return False
    values = resources.get(kind, [])
    return isinstance(values, list) and ("*" in values or str(value) in values)


def require(app, kind, value):
    if not allowed(app, kind, value):
        raise PermissionError(f"应用未获授权访问 {kind}：{value}，请在应用资源权限中配置")


def actor(app, chat_id, user_id="", system=False):
    if not app or not app.get("enabled"):
        raise PermissionError("应用已停用或不存在")
    resources = app.get("resources") or {}
    # 用户/群限制可选；资源访问始终默认拒绝。
    if "chats" in resources:
        require(app, "chats", chat_id)
    if not system and "users" in resources:
        require(app, "users", user_id)


def kubectl_scope(app, command):
    """有限只读命令语法；未知旗标/资源拒绝或要求全局范围，下游仍须 RBAC。"""
    argv = shlex.split(command or "")
    if argv and argv[0] == "kubectl":
        argv.pop(0)
    value_flags = {"-n", "--namespace", "-l", "--selector", "--field-selector", "-o", "--output",
                   "--tail", "--since", "--since-time", "-c", "--container", "--limit-bytes",
                   "--chunk-size", "--request-timeout", "--sort-by"}
    bool_flags = {"-A", "--all-namespaces", "--previous", "-p", "--timestamps", "--prefix",
                  "--show-labels", "--ignore-not-found", "--all-containers"}
    tokens, namespaces = [], []
    all_namespaces = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("-"):
            tokens.append(arg); i += 1; continue
        flag, separator, value = arg.partition("=")
        if not separator and arg[:2] in ("-n", "-l", "-o", "-c") and len(arg) > 2 and not arg.startswith("--"):
            flag, value, separator = arg[:2], arg[2:], "="
        if flag in value_flags:
            if not separator:
                i += 1
                if i >= len(argv) or argv[i].startswith("-"):
                    raise PermissionError("kubectl 旗标缺少取值")
                value = argv[i]
            if flag in ("-n", "--namespace"):
                namespaces.append(value)
            if flag in ("-o", "--output") and not (value in ("json", "yaml", "wide", "name") or
                    value.startswith(("jsonpath=", "jsonpath-as-json=", "custom-columns="))):
                raise PermissionError("不允许从本地文件加载输出模板")
        elif flag in bool_flags:
            if separator and value not in ("true", "false"):
                raise PermissionError("布尔旗标值不合法")
            if flag in ("-A", "--all-namespaces") and value != "false":
                all_namespaces = True
        else:
            raise PermissionError(f"kubectl 不允许旗标 {flag}；身份覆盖、原始路径和本地文件读取均禁止")
        i += 1
    verbs = {"get", "describe", "logs", "top", "events", "version", "api-resources", "api-versions", "explain"}
    if not tokens or tokens[0] not in verbs:
        raise PermissionError("集群仅开放只读排查；正式变更通过代码工作区修改 GitOps 仓库并提 MR")
    namespace = namespaces[-1] if namespaces else "default"
    if len(set(namespaces)) > 1:
        raise PermissionError("不允许多个不同的 namespace 参数")
    require(app, "namespaces", "*" if all_namespaces else namespace)
    if tokens[0] in ("get", "describe", "top", "explain"):
        if len(tokens) < 2:
            raise PermissionError("需要明确资源类型")
        raw_resources = tokens[1].split(",") + [x for x in tokens[2:] if "/" in x]
        resources = [x.split("/", 1)[0].split(".", 1)[0].lower() for x in raw_resources]
        if any(x in {"secret", "secrets", "se"} for x in resources):
            raise PermissionError("不允许通过助手读取集群 Secret")
        namespaced = {"pods", "pod", "po", "deployments", "deployment", "deploy", "replicasets", "rs",
                      "statefulsets", "sts", "daemonsets", "ds", "services", "service", "svc", "endpoints", "ep",
                      "endpointslices", "events", "event", "ev", "ingresses", "ingress", "ing", "jobs", "job",
                      "cronjobs", "cj", "configmaps", "cm", "horizontalpodautoscalers", "hpa", "all",
                      "persistentvolumeclaims", "pvc", "networkpolicies", "netpol", "applications",
                      "clusters", "backups", "scheduledbackups"}
        if any(x not in namespaced for x in resources):
            require(app, "namespaces", "*")
        groups = {"apps", "batch", "autoscaling", "networking.k8s.io", "discovery.k8s.io",
                  "events.k8s.io", "argoproj.io", "postgresql.cnpg.io"}
        for raw in raw_resources:
            resource = raw.split("/", 1)[0]
            if "." in resource and resource.split(".", 1)[1] not in groups:
                require(app, "namespaces", "*")
    # 不依赖 kubeconfig context.namespace，默认命名空间必须与校验一致。
    if not namespaces and not all_namespaces:
        argv.append("--namespace=default")
    return shlex.join(argv)
