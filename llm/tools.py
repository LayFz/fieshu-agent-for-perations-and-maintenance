"""工具池：全集 TOOLS（给大模型 function-calling 的 schema）+ 执行器。

每个应用在后台勾选自己要用的工具子集（store.apps.tools）。运行时：
  tools_for(names) -> 只把该应用勾选的工具暴露给模型（能力隔离，防越权）
  execute(name, args, ctx) -> 真正干活；ctx 携带 app_id（记忆/长期笔记按应用隔离）
"""
from datetime import datetime

from core import store
from feishu import docs as feishu_docs
from obs import openobserve, beszel, uptimekuma
from ops import ssh as ops_ssh
from ops import k8s as ops_k8s
from ops import argocd as ops_argocd
from ops import gitlab as ops_gitlab
from harness import workspace
from sec import feeds


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
    except Exception:
        return ""

# OpenAI 兼容的 tools schema —— 全集（工具池）
TOOLS = [
    {"type": "function", "function": {
        "name": "list_knowledge_bases",
        "description": "列出应用能访问的全部知识库（拿到 space_id）。不确定写到哪个库时先调它。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_nodes",
        "description": "列出某知识库下的文档节点（标题 + obj_token）。要改/读某篇前先用它定位。",
        "parameters": {"type": "object", "properties": {
            "space_id": {"type": "string"},
            "parent_node_token": {"type": "string", "description": "可选，钻取子节点"},
        }, "required": ["space_id"]},
    }},
    {"type": "function", "function": {
        "name": "read_doc",
        "description": "读取一篇文档的正文（doc_id = 节点的 obj_token）。",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"},
        }, "required": ["doc_id"]},
    }},
    {"type": "function", "function": {
        "name": "rewrite_doc",
        "description": "用 Markdown 覆盖重写一篇已有文档（doc_id = obj_token）。会清空原内容，请先 read_doc 确认。",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"},
            "markdown": {"type": "string", "description": "新正文，Markdown(支持 # ## ### / ``` / - / **粗** / `码` / > / | 表格 |)"},
        }, "required": ["doc_id", "markdown"]},
    }},
    {"type": "function", "function": {
        "name": "create_node",
        "description": "在某知识库下新建一篇文档并写入 Markdown 内容。",
        "parameters": {"type": "object", "properties": {
            "space_id": {"type": "string"},
            "title": {"type": "string"},
            "markdown": {"type": "string"},
            "parent_node_token": {"type": "string", "description": "可选，建在某节点下"},
        }, "required": ["space_id", "title", "markdown"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "记住一条长期信息（偏好/约定/结论），以后对话会自动带上。",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"},
        }, "required": ["note"]},
    }},
    {"type": "function", "function": {
        "name": "list_log_streams",
        "description": "列出 OpenObserve 各组织(org)下可查的日志流(stream)名称，按 org 分组返回。"
                       "OpenObserve 按 org 隔离数据(如 default 是应用/集群日志、rum 是前端埋点)，"
                       "不确定查哪个流、或者不确定在哪个 org 时，先调它。",
        "parameters": {"type": "object", "properties": {
            "org": {"type": "string", "description": "可选，只列某个组织，如 default / rum；不传列全部已配置组织"},
        }},
    }},
    {"type": "function", "function": {
        "name": "query_logs",
        "description": "查 OpenObserve 日志。给出日志流名、可选过滤条件、时间窗，返回最近的日志记录。"
                       "用于排查应用运行情况，例如某次发布是否失败、有没有报错。"
                       "OpenObserve 按组织(org)隔离数据，不同 org 的数据流不同(如 default / rum)，"
                       "用 org 参数指定；不传则查默认组织。不确定流名时先调 list_log_streams。",
        "parameters": {"type": "object", "properties": {
            "stream": {"type": "string", "description": "日志流名，例如 app_mp_publisher"},
            "org": {"type": "string", "description": "可选，组织名，如 default / rum；不传查默认组织"},
            "where": {"type": "string", "description": "可选 SQL 过滤条件（不含 WHERE），例如 level='error'"},
            "period": {"type": "string", "description": "时间窗，如 15m/1h/24h/7d，默认 15m"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 50，最多 500"},
        }, "required": ["stream"]},
    }},
    {"type": "function", "function": {
        "name": "server_stats",
        "description": "查服务器实时运行状况(来自 Beszel):列出服务器及其 CPU/内存/磁盘占用、负载、运行时长。"
                       "用于运维巡检/出服务器报告。不传 name 列全部,传 name 模糊筛某台。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "可选,服务器名关键字,如 agent1"},
        }},
    }},
    {"type": "function", "function": {
        "name": "docker_containers",
        "description": "查各服务器上正在运行的 Docker 容器(来自 Beszel):每个容器的 CPU/内存/网络占用,以及它在哪台机器上。"
                       "用于「某项目/某服务部署在哪台机」(传 name 容器名关键字)、「哪个容器占用最多」(sort=cpu/mem + top)、"
                       "「某台机上跑了哪些容器」(传 server)。",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string", "description": "可选,服务器名关键字,只看某台机,如 agent1"},
            "name": {"type": "string", "description": "可选,容器名关键字,找某项目部署在哪台机,如 gitlab"},
            "sort": {"type": "string", "enum": ["cpu", "mem", "name"], "description": "排序,默认 cpu(占用从高到低)"},
            "top": {"type": "integer", "description": "可选,只返回前 N 个,找占用最多时用"},
        }},
    }},
    {"type": "function", "function": {
        "name": "list_chat_members",
        "description": "列出当前群的成员(姓名 + open_id)。当你要 @ 某人处理任务、又不知道他 open_id 时先调它，"
                       "拿到 open_id 后在回复正文里写 <at id=该open_id></at> 即可 @ 到人。无参数。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "service_status",
        "description": "查各服务/站点的在线状态(来自 Uptime Kuma):是否在线、响应时间、HTTPS 证书剩余天数。"
                       "用于巡检哪些服务挂了、证书快到期没。无参数。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_ssh_hosts",
        "description": "列出可 SSH 操作的受管服务器：每台含 name、connect(实际连接地址)、aliases(别名/其它地址)。"
                       "一台机可能有多个叫法（如 default 的别名含 10.10.0.1、连接地址是内网 192.168.110.18），"
                       "它们都指同一台。要在某台机上跑命令前、或用户报的地址你不确定是哪台时，先调它。无参数。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "ssh_exec",
        "description": (
            "在受管服务器上执行一条 shell 命令并返回结果（stdout/stderr/退出码）。这是【高风险】能力，"
            "命令会真实作用于生产服务器。\n"
            "【执行前你必须自己判断安全性】：\n"
            "- 只读/诊断（如 systemctl status、docker ps、journalctl、df、ip a、wg show）——可直接执行。\n"
            "- 有变更但可逆、且用户明确要做的（如 sudo wg-quick up wg0 起网卡、systemctl restart 某服务）——"
            "先说清你要在哪台机跑什么、影响是什么，再执行。\n"
            "- 危险/不可逆/可能扩大故障（删数据、格式化、关机重启、改防火墙把自己锁死、动不了解的东西）——"
            "【拒绝执行】，说明理由，请用户确认或改由人工操作。\n"
            "- 拿不准就先查（用只读命令探明状态、结合你掌握的网络拓扑），别赌。宁可不做，绝不乱做。\n"
            "破坏性命令另有硬黑名单会直接拦截，但你不能依赖它——判断责任在你。\n"
            "【目标主机】target 填主机名或它的任意别名/地址都行（如 default、10.10.0.1、192.168.110.18 可能都指同一台）；"
            "系统会自动解析到对应主机、并用它登记的内网连接地址去连，你不用操心走哪个地址。"
            "若返回「不在受管列表」，说明这台机还没登记——如实告诉用户去后台「SSH 运维」页添加或补别名，别反复重试、别假装成功。"),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "主机名或其别名/地址（见 list_ssh_hosts，如 default 或 10.10.0.1）。只有一台时可省略。"},
            "command": {"type": "string", "description": "要执行的完整 shell 命令"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30，最多 300"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "list_k8s_clusters",
        "description": "列出可用 kubectl 操作的受管 k8s 集群：每个含 name、aliases(别名)、read_only(是否只读)。"
                       "一个集群可能有多个叫法。要对某集群跑 kubectl 前、或用户报的集群名你不确定是哪个时，先调它。无参数。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "kubectl",
        "description": (
            "对受管 k8s 集群执行一条 kubectl 命令并返回结果（stdout/stderr/退出码）。这是【高风险】能力，"
            "命令会真实作用于生产集群。\n"
            "【⚠️ GitOps 护栏，务必遵守】：这个集群的所有业务 app 都开了 ArgoCD 自愈(selfHeal+prune)——"
            "业务资源（deployment/service/configmap/secret/ingress 等）的正式变更【必须改 gitops 仓 git push】，"
            "别用 kubectl edit/apply/delete/rollout 直接改，几分钟内会被 ArgoCD 拉回或当孤儿删掉，白改还误导。"
            "kubectl 只用于：只读排查、运维态操作（rollout restart 让改过的密钥生效、CNPG 手动 failover、起临时诊断 pod）。\n"
            "【执行前你必须自己判断安全性】：\n"
            "- 只读/诊断（get、describe、logs、top、get events、get cluster、get applications）——可直接执行。\n"
            "- 有变更但可逆、且用户明确要做的（如 rollout restart deploy/xxx、delete 单个 pod 触发重建）——"
            "先说清你要在哪个集群、哪个命名空间做什么、影响是什么，再执行。\n"
            "- 危险/不可逆/可能扩大故障（delete cluster/pvc/pv/namespace/node、同时动多个 etcd 节点、delete --all、"
            "改 kube-system）——【拒绝执行】，说明理由，请用户确认或改由人工操作。\n"
            "- 拿不准就先用只读命令探明状态（结合 k8s-ops 技能里的集群拓扑与铁律），别赌。宁可不做，绝不乱做。\n"
            "破坏性命令另有硬黑名单会直接拦截，但你不能依赖它——判断责任在你。\n"
            "【目标集群】target 填集群名或它的任意别名（见 list_k8s_clusters）；只有一个集群时可省略。"
            "【命令】command 只填 kubectl 的子命令，不含前导 kubectl（如 'get pods -A'、'-n argocd get applications'）。"
            "若返回「不在受管列表」，说明这个集群还没登记——如实告诉用户去后台「K8s 运维」页添加，别反复重试、别假装成功。"),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "集群名或其别名（见 list_k8s_clusters）。只有一个集群时可省略。"},
            "command": {"type": "string", "description": "kubectl 子命令（不含前导 kubectl），如 'get pods -A'"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30，最多 300"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "schedule_task",
        "description": "创建一个定时任务：到点自动执行一段指令，并把结果发回当前群。"
                       "适合「每天下班前出运维报告」「每周一发周报」这类需求。"
                       "instruction 要写成将来可独立执行的完整指令（含要做什么、查什么、报告格式）。"
                       "时间含糊时（如“下班前”）先和用户确认具体钟点再调用。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "任务名，如：每日服务器运行报告"},
            "instruction": {"type": "string", "description": "到点要执行的完整指令"},
            "kind": {"type": "string", "enum": ["daily", "weekly", "interval"],
                     "description": "daily=每天；weekly=每周某几天；interval=每隔N分钟"},
            "spec": {"type": "string", "description": "daily: 'HH:MM'(24小时)；weekly: '1,2,3,4,5@HH:MM'(1=周一..7=周日)；interval: 分钟数"},
        }, "required": ["title", "instruction", "kind", "spec"]},
    }},
    {"type": "function", "function": {
        "name": "list_schedules",
        "description": "列出本应用已创建的定时任务。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "cancel_schedule",
        "description": "取消（删除）一个定时任务，按 list_schedules 给出的 id。",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "任务 id"},
        }, "required": ["id"]},
    }},
]

# ---------- 代码工作区（harness.workspace）：在一次性工作区里改代码 → 跑测试 → 出 diff → 提 MR ----------
_WS_PARAM = {"workspace": {"type": "string", "description": "可选，工作区 id（code_open 返回）；不传用本群最近打开的那个"}}
TOOLS += [
    {"type": "function", "function": {
        "name": "code_repos",
        "description": "列出可以改代码的仓库（名称、默认分支、能不能跑测试、能不能提 MR）。要改代码前先调它。无参数。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "code_open",
        "description": "把一个仓库克隆到新的一次性工作区，返回工作区 id 和顶层目录。之后的读写都只作用于这个工作区，"
                       "不会影响远端仓库；改完用 code_diff 看改动、code_test 跑测试、code_propose_mr 提 MR 给人评审。",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "仓库名（见 code_repos）"},
            "branch": {"type": "string", "description": "可选，基于哪个分支，默认仓库的默认分支"},
        }, "required": ["repo"]},
    }},
    {"type": "function", "function": {
        "name": "code_list",
        "description": "列工作区里的目录（跳过 .git、node_modules 等）。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "path": {"type": "string", "description": "目录相对路径，默认仓库根"},
            "depth": {"type": "integer", "description": "展开层数 1-4，默认 1"},
        })},
    }},
    {"type": "function", "function": {
        "name": "code_read",
        "description": "读工作区里的文件，返回带行号的内容（一次最多 2000 行）。改文件前先读，确认原文。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "path": {"type": "string"},
            "start": {"type": "integer", "description": "起始行，默认 1"},
            "end": {"type": "integer", "description": "结束行（含）"},
        }), "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "code_search",
        "description": "在工作区里按正则搜索代码，返回「路径:行号: 内容」（最多 200 条）。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "pattern": {"type": "string", "description": "Python 正则"},
            "glob": {"type": "string", "description": "可选，文件过滤，如 *.py、src/**/*.ts"},
        }), "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "code_edit",
        "description": "在工作区文件里做精确替换：old 必须与原文逐字一致（含缩进、换行），且默认只能出现一次。"
                       "小改动优先用它，而不是整文件重写。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "path": {"type": "string"},
            "old": {"type": "string", "description": "要被替换的原文"},
            "new": {"type": "string", "description": "替换成的新内容"},
            "replace_all": {"type": "boolean", "description": "old 出现多次时全部替换，默认 false"},
        }), "required": ["path", "old", "new"]},
    }},
    {"type": "function", "function": {
        "name": "code_write",
        "description": "在工作区里新建文件或整文件覆盖（最大 1MB）。改已有文件的一小部分请用 code_edit。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "path": {"type": "string"},
            "content": {"type": "string"},
        }), "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "code_delete",
        "description": "删除工作区里的一个文件。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "path": {"type": "string"},
        }), "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "code_diff",
        "description": "看工作区相对原始提交的改动（统一 diff + 每个文件增删行数）。提 MR 前先看一遍。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM)},
    }},
    {"type": "function", "function": {
        "name": "code_test",
        "description": "在工作区里运行该仓库在后台配置好的测试命令，返回是否通过与输出末尾。命令不能自定义。"
                       "注意：这会执行仓库里的代码（包括你改过的部分）。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM)},
    }},
    {"type": "function", "function": {
        "name": "code_propose_mr",
        "description": "把工作区里的改动提交到新分支并创建 Merge Request，返回 MR 链接，交给人评审合并（不会直接合并）。"
                       "提交前请确认 code_diff 的改动正确、code_test 已通过（若可跑）。一个工作区只能提一次。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM, **{
            "title": {"type": "string", "description": "MR 标题，一句话说清改了什么"},
            "description": {"type": "string", "description": "MR 描述：为什么改、改了什么、怎么验证的"},
        }), "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "code_close",
        "description": "关闭并删除工作区（改动会丢弃；已提的 MR 不受影响）。",
        "parameters": {"type": "object", "properties": dict(_WS_PARAM)},
    }},
]

# ---------- CVE 情报源（sec.feeds，只读，结果是外部文本 → external） ----------
TOOLS += [
    {"type": "function", "function": {
        "name": "cve_kev",
        "description": "查 CISA KEV（已确认在野利用的漏洞目录）最近新增的条目，按收录日期倒序。"
                       "keyword 可按厂商/产品过滤（如 gitlab、kubernetes、postgres、nginx）。命中 KEV = 最高优先级。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "最近多少天，默认 30"},
            "keyword": {"type": "string", "description": "可选，厂商/产品关键字"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 50"},
        }},
    }},
    {"type": "function", "function": {
        "name": "cve_osv",
        "description": "查某个软件包的某个版本受哪些已知漏洞影响（OSV 库，含 GitHub Advisory/Go/PyPI/npm 等），"
                       "返回漏洞 id、CVE、严重度、修复版本、是否在 KEV。组件版本要按包生态填，例如 "
                       "ArgoCD → ecosystem=Go, package=github.com/argoproj/argo-cd/v3；Python 包 → PyPI；Node 包 → npm。",
        "parameters": {"type": "object", "properties": {
            "ecosystem": {"type": "string", "description": "Go / PyPI / npm / Maven / crates.io / Debian:12 / Alpine:v3.20 …"},
            "package": {"type": "string"},
            "version": {"type": "string", "description": "具体版本号，不带 v 前缀（Go 模块除外按模块习惯）"},
        }, "required": ["ecosystem", "package", "version"]},
    }},
    {"type": "function", "function": {
        "name": "cve_detail",
        "description": "查单个漏洞详情（CVE / GHSA / GO 编号）：描述、影响范围与修复版本、参考链接、EPSS 被利用概率、是否在 KEV。",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "如 CVE-2026-12345、GHSA-xxxx-xxxx-xxxx"},
        }, "required": ["id"]},
    }},
]

# ---------- ArgoCD / GitLab 只读（ops.argocd / ops.gitlab，连接在后台「连接凭证」页配） ----------
TOOLS += [
    {"type": "function", "function": {
        "name": "argocd_apps",
        "description": "列 ArgoCD 管理的全部应用：项目、目标集群/命名空间、来源仓库与路径、同步/健康状态，"
                       "以及集群里【实际运行】的镜像（status.summary.images）。查组件真实版本时优先用它——"
                       "gitops 仓库里很多是浮动 tag，只看声明拿不到真实版本。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "可选，应用名关键字过滤"},
            "project": {"type": "string", "description": "可选，只看某个 ArgoCD project"},
        }},
    }},
    {"type": "function", "function": {
        "name": "argocd_app",
        "description": "看单个 ArgoCD 应用详情：镜像、同步/健康、告警条件、最近一次同步结果、资源清单。",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "gitlab_projects",
        "description": "在 GitLab 上搜项目（返回路径、默认分支、最近活动）。不确定项目叫什么时先调它。",
        "parameters": {"type": "object", "properties": {"search": {"type": "string", "description": "可选，名称关键字"}}},
    }},
    {"type": "function", "function": {
        "name": "gitlab_tree",
        "description": "列 GitLab 项目某个目录下的文件（只读，不克隆）。",
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string", "description": "项目路径如 group/repo，或项目 id"},
            "path": {"type": "string", "description": "目录，默认仓库根"},
            "ref": {"type": "string", "description": "分支/标签/提交，默认默认分支"},
        }, "required": ["project"]},
    }},
    {"type": "function", "function": {
        "name": "gitlab_read",
        "description": "读 GitLab 项目里的一个文件（只读，不克隆；最多 200KB）。",
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"}, "path": {"type": "string"},
            "ref": {"type": "string", "description": "默认 HEAD"},
        }, "required": ["project", "path"]},
    }},
]

# ---------- 漏洞台账与情报（sec，采集器自动维护；台账内容含外部文本 → external） ----------
TOOLS += [
    {"type": "function", "function": {
        "name": "vuln_list",
        "description": "查漏洞台账（情报采集器自动从 KEV/GHSA/厂商公告/社区舆情汇总的 CVE）："
                       "每条含标题、严重度、是否在 KEV、EPSS、舆情热度、来源、研判状态。",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["new", "triaging", "affected", "not_affected", "fixed", "ignored"]},
            "kev": {"type": "boolean", "description": "只看在 KEV（在野利用）里的"},
            "q": {"type": "string", "description": "关键字：CVE 编号、产品名（如 gitlab、argo、postgres）"},
            "days": {"type": "integer", "description": "最近多少天出现过的"},
            "sort": {"type": "string", "enum": ["last_seen", "epss", "social", "kev", "first_seen"]},
            "limit": {"type": "integer", "description": "默认 30"},
        }},
    }},
    {"type": "function", "function": {
        "name": "vuln_get",
        "description": "看台账里一条漏洞的完整记录，以及提到它的全部情报（官方公告、社区讨论）。",
        "parameters": {"type": "object", "properties": {"cve": {"type": "string"}}, "required": ["cve"]},
    }},
    {"type": "function", "function": {
        "name": "intel_search",
        "description": "搜最近抓到的情报原文（公告/帖子标题与摘要），看某个产品或漏洞的最新动态与讨论热度。",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "关键字"},
            "days": {"type": "integer", "description": "最近多少天，默认 30"},
            "limit": {"type": "integer", "description": "默认 30"},
        }, "required": ["q"]},
    }},
    {"type": "function", "function": {
        "name": "vuln_update",
        "description": "研判后更新台账：状态（affected 受影响 / not_affected 不受影响 / fixed 已修复 / ignored 忽略 / triaging 研判中）"
                       "与备注（写清依据：哪个服务、什么版本、怎么查到的）。会写入共享台账，请确认后再调用。",
        "parameters": {"type": "object", "properties": {
            "cve": {"type": "string"},
            "status": {"type": "string", "enum": ["triaging", "affected", "not_affected", "fixed", "ignored"]},
            "note": {"type": "string"},
            "assets": {"type": "array", "items": {"type": "string"}, "description": "可选，受影响的服务/组件，如 argocd@3.4.5"},
        }, "required": ["cve", "status", "note"]},
    }},
]

_BY_NAME = {t["function"]["name"]: t for t in TOOLS}

# 工具元数据：只给宿主（harness 管道）看，绝不发给模型。
#   risk     read / write / exec，或按参数判断的函数（kubectl 看动词）
#   external 结果是否含外部不可信文本（污染标记 P4 用；当前内置工具都是内部数据）
#   timeout  管道统一超时（秒）；ssh/kubectl 自身上限 300s，这里留余量
# 加新工具必须在这里登记；漏登记的按 exec（最高风险）处理。
_META = {
    "list_knowledge_bases": {"risk": "read"},
    "list_nodes": {"risk": "read"},
    "read_doc": {"risk": "read"},
    "rewrite_doc": {"risk": "write"},
    "create_node": {"risk": "write"},
    "remember": {"risk": "write"},
    "list_log_streams": {"risk": "read"},
    "query_logs": {"risk": "read"},
    "server_stats": {"risk": "read"},
    "docker_containers": {"risk": "read"},
    "list_chat_members": {"risk": "read"},
    "service_status": {"risk": "read"},
    "list_ssh_hosts": {"risk": "read"},
    "ssh_exec": {"risk": "exec", "timeout": 330},
    "list_k8s_clusters": {"risk": "read"},
    "kubectl": {"risk": lambda a: ops_k8s.risk_of(a.get("command")), "timeout": 330},
    "schedule_task": {"risk": "write"},
    "list_schedules": {"risk": "read"},
    "cancel_schedule": {"risk": "write"},
    "read_skill": {"risk": "read"},
    "code_repos": {"risk": "read"},
    "code_open": {"risk": "local", "timeout": 330},
    "code_list": {"risk": "read"},
    "code_read": {"risk": "read"},
    "code_search": {"risk": "read"},
    "code_edit": {"risk": "local"},
    "code_write": {"risk": "local"},
    "code_delete": {"risk": "local"},
    "code_diff": {"risk": "read"},
    "code_test": {"risk": "exec", "timeout": 630},     # 跑仓库代码（可能被改过）= 执行任意代码
    "code_propose_mr": {"risk": "write", "timeout": 90},
    "code_close": {"risk": "local"},
    "cve_kev": {"risk": "read", "external": True, "timeout": 60},
    "cve_osv": {"risk": "read", "external": True, "timeout": 60},
    "cve_detail": {"risk": "read", "external": True, "timeout": 60},
    "argocd_apps": {"risk": "read"},
    "argocd_app": {"risk": "read"},
    "gitlab_projects": {"risk": "read"},
    "gitlab_tree": {"risk": "read"},
    "gitlab_read": {"risk": "read"},
    "vuln_list": {"risk": "read", "external": True},
    "vuln_get": {"risk": "read", "external": True},
    "intel_search": {"risk": "read", "external": True},
    "vuln_update": {"risk": "write"},
}
def _preview_mr(args, ctx):
    """审批卡片上给人看的改动摘要：要批的是这些改动，不是模型写的标题。"""
    ws = workspace.get_workspace(args.get("workspace"), ctx.get("app_id"), ctx.get("chat_id"))
    d = workspace.diff(ws, max_chars=2500)
    files = "\n".join(f"- {f['status']} `{f['path']}` (+{f['added']} -{f['removed']})" for f in d["files"])
    return f"**改动 {len(d['files'])} 个文件**（基于 {d['base_commit']}）\n{files}\n```diff\n{d['diff']}\n```" \
           + ("\n（diff 已截断，完整内容见 MR）" if d["truncated"] else "")


def _preview_test(args, ctx):
    ws = workspace.get_workspace(args.get("workspace"), ctx.get("app_id"), ctx.get("chat_id"))
    repo = store.get_code_repo(ws["repo_id"]) or {}
    n = len(ws.get("changed") or {})
    return f"将在工作区运行：`{repo.get('test_cmd') or '（未配置）'}`\n会执行仓库代码，其中 {n} 个文件被数字员工改过。"


_META["code_propose_mr"]["preview"] = _preview_mr
_META["code_test"]["preview"] = _preview_test

_UNKNOWN_META = {"risk": "exec", "external": False, "timeout": 120}
# MCP 工具来自外部 server，能力未知：按 write 处理，结果视为外部内容
MCP_META = {"risk": "write", "external": True, "timeout": 120}


def meta_for(name, args):
    """某次调用的元数据：{"risk", "external", "timeout"}（不含 preview）。"""
    m = dict(_UNKNOWN_META, **_META.get(name, {}))
    m.pop("preview", None)
    if callable(m["risk"]):
        try:
            m["risk"] = m["risk"](args or {})
        except Exception:
            m["risk"] = "exec"
    return m


def approval_preview(name, args, ctx):
    """需要审批时显示在卡片上的预览（如提 MR 的 diff）；没有或算不出来返回 None。"""
    fn = _META.get(name, {}).get("preview")
    if not fn:
        return None
    try:
        return fn(args or {}, ctx or {})
    except Exception as e:
        return f"（预览生成失败：{e}）"

# 技能工具：仅当应用勾了技能时由 engine 自动挂上（不在工具池里给用户单独勾）
SKILL_TOOL = {"type": "function", "function": {
    "name": "read_skill",
    "description": "读取一个技能(skill)的完整内容（知识/工作方法/背景资料）。系统提示词里列了你可用的技能名与简介，"
                   "需要某个技能的详细内容时调用它。返回里的 files 是该技能附带的参考文件（如 references/xx.md、脚本），"
                   "正文里提到它们、需要细节时，再带上 file 参数读取。",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "技能名称（见系统提示词中的技能清单）"},
        "file": {"type": "string", "description": "可选，附带文件的相对路径（取自上次返回的 files），不传读正文"},
    }, "required": ["name"]},
}}


def all_tools():
    """工具池全集（名称 + 说明），给后台勾选用。"""
    return [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in TOOLS]


def tools_for(names):
    """按应用勾选的名单返回 OpenAI tools 子集；名单为空则该应用没有任何工具。"""
    names = names or []
    return [_BY_NAME[n] for n in names if n in _BY_NAME]


def tool_list_for(names):
    """某应用已启用工具的清单（名称 + 说明），给后台展示。"""
    return [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in tools_for(names)]


def _code_tool(name, args, ctx):
    """代码工作区工具。工作区按 应用+群 归属，越权访问别的应用的工作区会被拒。"""
    app_id, chat_id = ctx.get("app_id"), ctx.get("chat_id")
    try:
        if name == "code_repos":
            return {"repos": workspace.list_repos()}
        if name == "code_open":
            return workspace.open_workspace(app_id, chat_id, args.get("repo"), args.get("branch"))
        ws = workspace.get_workspace(args.get("workspace"), app_id, chat_id)
        if name == "code_list":
            return workspace.list_dir(ws, args.get("path") or ".", args.get("depth") or 1)
        if name == "code_read":
            return workspace.read_file(ws, args.get("path"), args.get("start") or 1, args.get("end"))
        if name == "code_search":
            return workspace.search(ws, args.get("pattern") or "", args.get("glob"))
        if name == "code_edit":
            return workspace.edit_file(ws, args.get("path"), args.get("old"), args.get("new"),
                                       bool(args.get("replace_all")))
        if name == "code_write":
            return workspace.write_file(ws, args.get("path"), args.get("content"))
        if name == "code_delete":
            return workspace.delete_file(ws, args.get("path"))
        if name == "code_diff":
            return workspace.diff(ws)
        if name == "code_test":
            return workspace.run_tests(ws)
        if name == "code_propose_mr":
            return workspace.propose_mr(ws, args.get("title"), args.get("description") or "")
        if name == "code_close":
            workspace.close_workspace(ws["id"])
            return {"ok": True, "closed": ws["id"]}
        return {"error": f"unknown tool {name}"}
    except workspace.WorkspaceError as e:
        return {"error": str(e)}


def execute(name, args, ctx=None):
    ctx = ctx or {}
    app_id = ctx.get("app_id")
    try:
        if name == "read_skill":
            skills = ctx.get("skills") or {}          # 只含本应用勾选的技能 -> 隔离
            sk = skills.get(args.get("name"))
            if sk is None:
                return {"error": "没有这个技能或本应用未启用它",
                        "available": list(skills.keys())}
            if isinstance(sk, str):                   # 兼容旧调用方：只有正文
                sk = {"content": sk, "files": {}}
            files = sk.get("files") or {}
            if args.get("file"):
                f = str(args["file"]).strip()
                f = f[2:] if f.startswith("./") else f
                if f not in files:
                    return {"error": f"技能「{args.get('name')}」没有附带文件 {args['file']}",
                            "files": sorted(files)}
                return {"name": args.get("name"), "file": f, "content": files[f]}
            out = {"name": args.get("name"), "content": sk.get("content") or ""}
            if files:
                out["files"] = sorted(files)
            return out
        if name.startswith("code_"):
            return _code_tool(name, args, ctx)
        if name == "cve_kev":
            return feeds.kev_recent(args.get("days") or 30, args.get("keyword"), args.get("limit") or 50)
        if name == "cve_osv":
            return feeds.osv_query(args.get("ecosystem"), args.get("package"), args.get("version"))
        if name == "cve_detail":
            return feeds.detail(args.get("id"))
        if name == "argocd_apps":
            return ops_argocd.list_apps(args.get("name"), args.get("project"))
        if name == "argocd_app":
            return ops_argocd.get_app(args.get("name"))
        if name == "gitlab_projects":
            return ops_gitlab.projects(args.get("search"))
        if name == "gitlab_tree":
            return ops_gitlab.tree(args.get("project"), args.get("path") or "", args.get("ref"))
        if name == "gitlab_read":
            return ops_gitlab.read(args.get("project"), args.get("path"), args.get("ref"))
        if name == "vuln_list":
            r = store.list_vulns(args.get("status"), args.get("kev"), args.get("q"), args.get("days"),
                                 args.get("sort") or "last_seen", min(int(args.get("limit") or 30), 100))
            keep = ("cve", "title", "severity", "cvss", "kev", "kev_added", "epss", "products", "sources",
                    "mentions", "social", "status", "note", "assets")
            return {"total": r["total"], "items": [{k: v.get(k) for k in keep} for v in r["items"]]}
        if name == "vuln_get":
            cve = (args.get("cve") or "").strip().upper()
            v = store.get_vuln(cve)
            items = store.intel_items(cve=cve, limit=30)
            return {"vuln": v, "intel": [{k: i.get(k) for k in ("source", "category", "title", "url", "published", "summary")}
                                         for i in items]} if v or items else {"error": f"台账里没有 {cve}"}
        if name == "intel_search":
            items = store.intel_items(q=args.get("q"), days=args.get("days") or 30, limit=min(int(args.get("limit") or 30), 100))
            return {"count": len(items), "items": [{k: i.get(k) for k in ("source", "category", "title", "url", "published", "cves")}
                                                   for i in items]}
        if name == "vuln_update":
            cve = (args.get("cve") or "").strip().upper()
            if not store.get_vuln(cve):
                return {"error": f"台账里没有 {cve}"}
            if args.get("status") not in store.VULN_STATUSES:
                return {"error": "状态不对"}
            store.upsert_vuln(cve, status=args["status"], note=args.get("note"), assets=args.get("assets") or [])
            return {"ok": True, "cve": cve, "status": args["status"]}
        if name == "list_knowledge_bases":
            return feishu_docs.list_kbs()
        if name == "list_nodes":
            return feishu_docs.list_nodes(args["space_id"], args.get("parent_node_token"))
        if name == "read_doc":
            return {"content": feishu_docs.read_doc(args["doc_id"])[:8000]}
        if name == "rewrite_doc":
            return feishu_docs.rewrite_doc(args["doc_id"], args["markdown"])
        if name == "create_node":
            return feishu_docs.create_node(args["space_id"], args["title"], args["markdown"],
                                           args.get("parent_node_token"))
        if name == "remember":
            store.add_note(app_id, args["note"])
            return {"ok": True}
        if name == "list_log_streams":
            return openobserve.list_streams(args.get("org"))
        if name == "query_logs":
            return openobserve.search(args["stream"], args.get("where"),
                                      args.get("period", "15m"), args.get("limit", 50),
                                      args.get("org"))
        if name == "server_stats":
            return beszel.servers(args.get("name"))
        if name == "docker_containers":
            return beszel.containers(args.get("server"), args.get("name"),
                                     args.get("sort", "cpu"), args.get("top"))
        if name == "list_chat_members":
            from feishu import members
            app = store.get_app(app_id)
            if not app or not ctx.get("chat_id"):
                return {"error": "缺少群上下文，无法读取群成员"}
            r = members.roster(app, ctx["chat_id"])
            if not r:
                return {"count": 0, "members": [],
                        "note": "读不到群成员（应用可能缺少「读取群成员」权限，或机器人不在该群）"}
            return {"count": len(r), "members": r}
        if name == "service_status":
            return uptimekuma.status()
        if name == "list_ssh_hosts":
            return ops_ssh.list_hosts()
        if name == "ssh_exec":
            return ops_ssh.run(args.get("target"), args.get("command"), args.get("timeout", 30))
        if name == "list_k8s_clusters":
            return ops_k8s.list_clusters()
        if name == "kubectl":
            return ops_k8s.run(args.get("target"), args.get("command"), args.get("timeout", 30))
        if name == "schedule_task":
            if not ctx.get("chat_id"):
                return {"error": "无法确定要发送的群（缺少会话上下文）"}
            sid, nxt = store.create_schedule(app_id, ctx["chat_id"], args.get("title"),
                                             args.get("instruction"), args.get("kind"), args.get("spec"))
            return {"ok": True, "id": sid, "next_run": _fmt_ts(nxt),
                    "note": "已创建定时任务，到点会自动在本群发结果"}
        if name == "list_schedules":
            return {"schedules": [{
                "id": sc["id"], "title": sc["title"], "kind": sc["kind"], "spec": sc["spec"],
                "enabled": bool(sc["enabled"]), "next_run": _fmt_ts(sc["next_run_ts"]),
            } for sc in store.list_schedules(app_id)]}
        if name == "cancel_schedule":
            sc = store.get_schedule(args.get("id"))
            if not sc or sc["app_id"] != app_id:          # 只能取消本应用的任务 -> 隔离
                return {"error": "没有这个任务或不属于本应用"}
            store.delete_schedule(sc["id"])
            return {"ok": True}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}
