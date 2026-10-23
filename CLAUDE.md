# CLAUDE.md

本仓库 agent / 开发者的工程说明。修改 `harness/`、`llm/engine.py` 前先读 [设计与历史路线](docs/harness-design.md)，验证步骤见 [本地测试手册](docs/local-harness-testing.md)。

## 当前状态

飞书服务端 Harness：多个应用通过各自飞书机器人接任务，在服务端持久队列中推进集群排障、日志分析、漏洞分析与研发修复。应用分别配置模型、工具、技能、资源权限和审批人。

本轮已实现本地可测的任务恢复、非阻塞审批、动作账本、默认拒绝的资源边界、Docker 代码测试和应用自主持续巡检。**尚未云端部署**；本机已联调飞书长连接、Kubernetes/ArgoCD、两个 GitOps 仓库、GitLab 项目与流水线 API、三个 OpenObserve 日志流。群列表和巡检通知投递尚缺权限/目标会话，真实 MR 审批尚缺飞书审批人及端到端验收。完整漏洞资产匹配、自动复扫闭环、通用 webhook 与子 agent 委派仍不是已验收能力。`8801` 演示数据全部是替身；`8800` 才是当前真实本地服务。

飞书长连接主动出站；管理后台仍开放 HTTP 端口。当前以单服务实例 + 持久 SQLite 目录运行，未验证多实例或高可用。

## 架构与模块

```text
feishu/bot.py、sched/runner.py、web/admin.py
                    ↓ 提交（source + app_id + event_id 去重）
core/taskstore.py → harness/tasks.py（有界 worker、租约、取消、outbox）
                    ↓ 注册的 executor
llm/engine.py（授权上下文指纹、检查点、工具循环、最小证据验收）
                    ↓
harness/pipeline.py（准备/冻结 → 策略/guard → 持久审批 → 执行 → 结果）
                    ↓
原生工具 / MCP / 隔离代码工作区
```

包分层：`core` ← `harness` ← `feishu` / `obs` / `ops` / `sched` ← `llm` ← `web` ← `app`。`harness` 只依赖 `core`；上层能力通过注册 executor、sender、审批应答器和 guard 注入，不能反向 import 上层。

| 模块 | 职责 |
|---|---|
| `core/config.py` | 引导配置、独立数据目录；`FEISHU_AGENT_CONFIG=''` 禁用配置文件读取 |
| `core/store.py` / `core/taskstore.py` | SQLite 业务数据 / 任务、动作账本与通知 outbox |
| `harness/tasks.py` | worker、租约续期、检查点、挂起/唤醒、取消、恢复 |
| `core/patrolstore.py` / `harness/patrols.py` | 持续巡检的快照、问题差异、来源成功才恢复、持久通知和租约 |
| `app/patrol_service.py` / `ops/*_scan.py` | 应用自主调度的集群、GitOps 与日志固定只读采集；扫描结果不由 Codex 手工替代 |
| `harness/runs.py` | 每次尝试的 run 与追加事件审计；不等于跨重启任务本身 |
| `harness/policy.py` / `access.py` | 风险策略、单调 guard / 确定性资源权限与只读 kubectl 语法 |
| `harness/pipeline.py` / `approval.py` | 工具持久动作、结果不明处理 / 审批保存、校验、唤醒 |
| `harness/workspace.py` / `sandbox.py` | 按任务隔离工作区、快照、测试、GitLab MR / Docker 执行器 |
| `harness/workflows.py` | cluster/logs/vulnerability/development/general 提示与最低证据门槛 |
| `harness/skills.py` / `gitutil.py` | Git 技能同步 / dulwich 克隆 |
| `llm/engine.py` / `authorization.py` | 模型工具循环、上下文恢复 / 新鲜授权、目标固定、上下文指纹 |
| `llm/tools.py` / `mcp.py` | 原生工具及元数据 / 同步远程 HTTP MCP |
| `feishu/` / `obs/` / `ops/` | 飞书入口、文档 / 只读监控 / SSH 与 K8s 连接 |
| `sched/runner.py` | 到期计划原子入队并推进 next_run，避免同次定时重复入队 |
| `web/` / `app/main.py` | 管理后台与 API / 启动迁移、worker、飞书连接、调度与 HTTP |

## 数据与恢复契约

- `tasks`：任务指令、身份、工作流、状态、租约、检查点。状态包括 `queued/running/waiting_approval/waiting_input/completed/cancelled/failed/needs_attention`。同一应用/群同时只执行一个任务；审批挂起释放 worker。
- `task_actions`：持久 `action_key`、固定工具参数、风险、审批 ID、结果与状态。模型回复和待执行工具先写检查点，再执行；已完成动作恢复时复用存储结果，但仍重查当前授权。未完成只读动作允许重试，执行过但结果不明的非只读动作停止等待人工核对。
- `task_outbox`：任务通知持久保存并重试，稳定通知 ID 传给飞书；不宣称所有外部系统提供严格 exactly-once。
- `runs/events`：一个任务可以有多次 run。遗留 running run 标 `aborted`，任务由租约/检查点恢复。执行前关键记录失败必须停止动作，不能降级成仅打日志后执行。`msgs` 仍负责对话历史。
- `approvals`：`pending → allowed-once/rejected/expired/cancelled/unavailable`。任务审批绑定 action；重启保留任务审批，仅取消无任务的 legacy pending。
- `apps`：飞书身份、`ai_provider_id/tools/skills/mcp/policy/approvers/resources` 与人设；`ai_providers/skills/mcp_servers` 保存模型、技能和外部工具配置。
- `code_repos/workspaces/workspace_safety`：仓库配置、应用/群/任务归属、代码快照、测试记录与 MR 状态。工作区不装生产凭据。
- `schedules`、`msgs/notes/token_usage`、`kv`、`admin`：计划、历史/笔记/用量、连接设置、管理员。历史和新笔记按应用、群和授权上下文指纹读取；旧全局笔记管理员仍可查。

任务写入须使用当前 `lease_owner/lease_epoch` fencing；不要绕过 `tasks.check_active()`。控制异常 `TaskSuspended/TaskCancelled/TaskUncertain/TaskIncomplete` 要透传到 runtime，不能吞成普通工具错误。恢复时旧 worker 失去租约必须停止提交结果。

`context_key` 绑定应用权限、工具、技能内容、MCP 配置、人设和模型供应商等。配置改变后旧检查点停下，**需核对旧任务后创建新任务**；不能用“继续”绕过，也不能把旧敏感证据自动重放给新供应商。代码快照/SSH 目标变更另有执行前校验。

## 工具、授权与审批

- 工具可见性和资源访问是两道检查。`apps.resources` 默认空对象，资源名单未配置即拒绝；`*` 只有管理员显式授予才生效。`users/chats` 是可选限制，一旦设置即执行。新工具必须实现目标资源授权，不能只登记 schema。
- 默认策略为 `readonly`；空策略按只读、非法策略按 deny。`approval` 放行 read/local，write/exec 需审批；`compat` 只能显式选择，仍受所有 guard 限制。自定义规则第一条匹配生效，未命中拒绝。
- `run_tool()` 顺序为：严格解析对象参数 → `prepare` 固定资源/工作区 → 保存动作与审计 → 当前策略/guard → 持久审批 → 再查当前权限、guard 与 `validate` → 标 running → 执行 → 存结果。未知副作用停止任务，不提示模型随意换参数重试。
- 读取外部文本使 run 和检查点 tainted；后续生产 write/exec 被 guard 拒绝。允许只读、隔离代码本地变更、显式沙箱 `code_test` 与受审批 `code_propose_mr`；任意 MCP 不能借此执行。
- durable 审批等待不占 worker；点击回调校验同一 app、有效审批人、状态及过期时间，落库后唤醒任务。无审批人、发送失败、过期、拒绝均不执行；exec 不允许发起人自批。无 task 的 legacy 调用保留阻塞兼容路径。
- 正常任务的 chat/schedule/job/webhook 可以审批；proactive/delegate 固定 never。不能用审批代替资源权限或 guard。
- 飞书开发者后台需长连接订阅 `card.action.trigger` 并发版，才可点击卡片批准。真实回调链路本轮未联调。
- `kubectl` 只开放有限只读语法；禁止读取 Secret、覆盖服务器/身份、原始 API 路径、本地模板等。缺省 namespace 固定成 `default`，未知/集群级资源要求全局 namespace 权限。底层 Kubernetes RBAC 仍必须收紧。
- `ssh_exec` 只允许显式登记且授权的主机名称；审批固定 host/port/username 指纹，硬黑名单保留。不要把模型判断当权限边界。
- MCP 仅支持远程 HTTP（Streamable HTTP，兼容 SSE），不支持 stdio；应用勾选 server 之外还须授权 `mcp_servers` 资源。

## 代码工作区与沙箱

代码仓库在后台登记读令牌、写令牌、固定测试命令或 GitOps 静态校验类型。同一个 Developer + `api` + `read_repository` 项目令牌可用于该项目 HTTPS 克隆和审批后的 GitLab API 提 MR，无需 `write_repository`。模型通过 `code_*` 在按应用/群/任务归属的工作区改代码；不能访问其他任务工作区。工作区路径、符号链接与 `.git` 受限制；并发容量满时拒绝新建，不自动删掉在用工作区。

`code_test` 默认禁用，必须设置 `HARNESS_SANDBOX_IMAGE`，镜像须已在本地且不声明额外卷；不会自动拉镜像，也没有宿主 shell 回退。测试使用独立代码快照，容器无网络、只读根文件系统、非 root、去 capabilities，限定 CPU/内存/PID；不挂载数据库、配置、生产密钥或 Docker socket。依赖须预装。超时/取消需确认容器销毁，无法确认则标记结果不明。

审批绑定全仓快照和验证证据；Docker 测试还绑定测试命令与实际 image ID。恢复时和实际执行前都重新校验。`code_propose_mr` 要求相同快照的成功隔离测试，或仓库配置为 GitOps 时通过 YAML/JSON 静态校验；GitOps 校验仅验证语法与原生 Secret 载荷，不代替 CI、ArgoCD 与发布复扫。飞书卡片必须呈现完整 diff，超过预览上限则拒绝审批。由确定性 GitLab API 提交和建 MR，模型不持有写令牌，工作区不 git push。结果不明停止核对，MR 不自动合并。

当前主 Dockerfile 未安装 Docker CLI，现有 Compose 未接入沙箱运行器。云端启用前仍须设计并验证可信执行节点或控制面到 Docker daemon 的连接与同路径挂载；别把宿主 socket 挂进执行仓库代码的容器。

## 开发约定

- 新工具在 `llm/tools.py` 增 schema、execute 分支和 `_META`（risk=read/local/write/exec、timeout、external 等）；漏登记按 exec。还须在 `llm/authorization.py` 加当前资源授权，必要时提供 prepare/validate/preview；不能只靠模型提示词。
- 单元/集成测试使用临时数据目录和 `FEISHU_AGENT_CONFIG=''`，外部连接使用替身；不要读取本机真实凭据或操作生产。Python 3.12 为统一测试版本，`httpx` 只在 `requirements-dev.txt`。
- `core/taskstore.py` 只依赖 core；需要注册上层执行器。新动作的完成、等待、未知结果都须有可验证的恢复路径。
- 每应用配置放 `apps`，全局连接放 `kv`。配置、模型/应用密钥和连接凭据可能存入 SQLite，数据库/备份属于敏感数据；不是“密钥不入库”。
- 技能从 Git 同步时保持来源约束、同主机令牌限制、路径校验、失败保留旧版本等已有防护。Git 来源技能编辑走能力仓 MR。
- 前端无构建，改 `web/static/index.html`；中文注释与现有风格一致。

## ⚠️ 非踩不可的坑（都被这些坑过，改前务必看）

1. **SQLite 不能跨线程共用一个连接**：bot 线程写、后台/调度线程读，共用一个 connection 会 `database is locked`。`store.py` 用**每线程独立连接 + WAL + busy_timeout**（`_ThreadLoopProxy` 同款 `_ConnProxy`）。别退回单连接。
2. **lark ws 客户端用模块级全局事件循环**：`lark_oapi/ws/client.py` 的 `loop` 是全局的，多个 `Client.start()` 同进程跑，第二个报 `This event loop is already running`。`feishu/bot.py` 用 `_ThreadLoopProxy` 替换 `lark_oapi.ws.client.loop`，每应用线程各拿独立 loop。**这是多应用长连接成立的前提。**
3. **Dockerfile 指令行尾不能写内联注释**：`VOLUME ["/code/data"]  # 注释` 会被 Docker 把注释当成额外卷参数，建出 `#`/垃圾匿名卷；首次 create 能成，之后 `docker compose up` recreate 按 `#` 非法路径重挂 → **部署必失败**。注释必须独占一行。
4. **容器时区默认 UTC**：python:slim 容器是 UTC，调度器 `datetime.now()` 取 UTC，"每天18:00"会差 8 小时。`deploy/compose.yml` 挂 `/etc/localtime`+`/etc/timezone` 用宿主时区。
5. **新增顶层包要在 Dockerfile 加 `COPY`**：`COPY obs ./obs` / `COPY sched ./sched`，漏了容器 import 即崩。
6. **新应用要重启服务才建立长连接**：`bot.start_all()` 只在启动时为已启用应用起连接；后台新建/改飞书凭证/改启用状态后需重启进程（后台有「重启服务」按钮，或 `docker restart`）。人设/工具/AI/技能/定时任务是即时生效的。
7. **前端时间显示强制 `Asia/Shanghai`**：`dt()` 写死 `timeZone:'Asia/Shanghai'`，否则按访问者浏览器时区渲染会误导。
8. **MCP 客户端是手写同步的，别换成官方异步 SDK**：`llm/mcp.py` 用同步 `requests` 手写 Streamable HTTP 传输，正是为了不碰坑#2——官方 `mcp` SDK 是 asyncio 的，塞进多线程 bot 会重演「全局事件循环已在运行」。每次工具调用**独立连接一次**（initialize→initialized→tools/call，用完关），无跨线程 loop、无持久连接，最贴合本仓库同步风格。工具清单按 server 缓存 `_CACHE_TTL`(默认 300s) 避免每条消息都 `tools/list`；server 配置改动/删除时 `mcp.invalidate(id)` 清缓存（admin 里已接）。某个 server 连不上时 `tools_for_app` 只跳过它、不让整轮对话崩。

## 运行与验收

- 创建/使用 Python 3.12 虚拟环境，安装 `requirements-dev.txt`，运行 `FEISHU_AGENT_CONFIG='' python -m unittest discover -s tests -v`。默认跳过显式启用的真实 Docker 测试。
- 本地模拟：`.venv/bin/python scripts/local_demo.py --seed --port 8801`；先确认该 `.venv` 是 Python 3.12。演示不读取生产配置，不连接真实飞书、模型、集群或漏洞源。
- 本机真实常驻：`.venv-runtime/bin/python` 安装依赖后用 `python3 scripts/local_service.py install/status/uninstall` 管理当前用户的 launchd 服务；后台 `http://127.0.0.1:8800/#patrols`。启动时保存旧数据库备份，运行中不要把测试数据目录指向正式 `data`。云端仍须单实例守护、持久卷和 SSH 身份配置。
- 正式入口：配置 `config.yaml` 后 `python -m app.main`；本机可用 `WEB_HOST=127.0.0.1` 仅监听本地。
- 容器：现有 `docker compose up -d --build`。部署资料见 [deploy/DEPLOY.md](deploy/DEPLOY.md)，本轮未部署。
- CI 新增 MR/main 的 Python 3.12 Docker 测试，运行通过后原有 main 构建/发布才继续；本次未触发远程流水线。
- 首启旧配置迁移逻辑保留。数据路径默认为 `data/agent.db`，用 `FEISHU_AGENT_DATA_DIR` 可隔离测试目录；改路径会切换数据库，而不是迁移数据。
