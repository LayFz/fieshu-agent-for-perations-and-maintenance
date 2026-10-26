# feishu-agent · 飞书服务端 Harness

通过飞书交付任务、查看进度、批准操作的常驻助手。一个 Python 服务承载多个应用，每个应用有独立飞书身份、模型、工具、技能、资源白名单与审批人。面向三类工作：**集群排障、问题日志分析、漏洞分析与代码修复**。

当前版本已接入持久任务队列、工具动作账本、审批挂起恢复、隔离测试执行器和独立于模型的持续巡检。2026-10-23 已在本机验证：飞书长连接、集群与 ArgoCD 只读采集、两个 GitOps 仓库克隆与本地静态校验、GitLab 项目和流水线 API、三个 OpenObserve 日志流，以及进程重启恢复。**尚未部署云端；飞书群列表因缺少权限不可用，巡检通知和 MR 审批尚未绑定目标会话及审批人，尚未创建真实 MR。**

## 当前能力与边界

| 能力 | 当前实现 |
|---|---|
| 持续任务 | 飞书消息、定时触发进入 SQLite 任务队列；同一应用/群串行，worker 租约与检查点支持重启恢复 |
| 工具动作账本 | 动作先落库再执行；已完成动作恢复时复用结果；中断的非只读动作结果不明时停到 `needs_attention`，不自动重做 |
| 飞书审批 | 审批持久化，等待期间释放 worker；点击后恢复任务，并重查权限、guard 与代码快照 |
| 授权 | 默认 `readonly`；配置非法则拒绝；勾选工具之外还须配置资源白名单，缺失资源授权即拒绝 |
| 集群与日志 | 授权范围内只读查询；集群正式变更走 GitOps 仓库修复与 MR；Secret、身份覆盖、原始 API 路径等受限制 |
| 常驻巡检 | 固定只读采集 Kubernetes、ArgoCD、GitOps、GitLab 流水线及指定 OpenObserve 日志流；按来源标记成功/部分/失败，SQLite 保存快照与问题状态，只在变化或首次绑定时通过飞书通知 |
| 漏洞与研发 | 查询证据、隔离工作区改代码、Docker 测试或 GitOps 静态校验、审批后提 MR；校验通过和 MR 证据是修复任务的最低完成条件 |
| 代码测试 | Docker 隔离执行，默认禁用；须显式配置可信本地镜像及运行器，不回退到宿主 shell |
| 管理与审计 | 任务列表/详情/取消/继续、运行事件、应用/模型/技能/MCP/代码仓库与连接配置 |

漏洞匹配、根因判断和生产恢复仍需业务验收；“已建 MR”不代表“已发布修复”。当前证据检查是最低门槛，不能代替发布后验证或复扫。

## 架构

```text
飞书长连接 / 定时任务 / 后台提交
                    ↓
       tasks 队列 → worker 租约 → 模型检查点
                    ↓
       当前授权 → 工具策略/guard → task_actions
                    ↓
       只读查询 / 隔离工作区 / 审批挂起恢复
                    ↓
       证据校验 → 持久通知 outbox → 飞书

本地常驻服务 → patrols 租约 → 固定只读采集器
                    ↓
         快照/问题台账/通知 outbox → 飞书

管理后台 :8800 ── SQLite（任务、动作、审批、运行记录、配置）
代码测试 ── 显式配置的 Docker 沙箱（无网络、无生产凭据）
```

飞书使用主动出站的长连接，不需要公开 webhook 入口；管理后台仍监听 HTTP 端口，应由部署网络限制访问。SQLite 数据目录需持久保存，当前按单服务实例设计；未验证多实例、高可用或大规模负载。

### 本机真实巡检

本机已有配置时，使用 Python 3.12 虚拟环境启动 macOS 登录会话的常驻服务：

```bash
python3.12 -m venv .venv-runtime
.venv-runtime/bin/python -m pip install -r requirements-dev.txt
python3 scripts/local_service.py install
python3 scripts/local_service.py status
```

打开 [真实管理后台的持续巡检](http://127.0.0.1:8800/#patrols) 查看来源状态、扫描快照和问题台账；`8801` 是独立的模拟演示。后台可配置项目 SSH 仓库、日志目标、间隔、汇报群并触发立即扫描。应用需要明确的 `k8s_clusters`、`namespaces`、`gitlab_projects`、`log_orgs`、`log_streams` 资源授权。日志巡检只请求固定错误关键词的聚合计数，不保存日志正文；关键词命中只是待核对线索。

在目标群 @机器人发送“我的身份”或“巡检状态”，它会返回 `chat_id`；后台也会列出机器人实际收到消息的会话及发言人 `open_id`。管理员选定汇报会话并登记审批人后，应用重新扫描并发送首次汇报。若希望后台直接列出机器人当前加入的群，需要在飞书开放平台为该应用开通 `im:chat:readonly` 或 `im:chat.group_info:readonly` 并发布。GitLab 只读巡检可用每项目 Reporter + `read_api`；若同一项目还需经审批创建修复 MR，可用 Developer + `api` + `read_repository` 项目令牌，并将它分别配置为该仓库读/写令牌。令牌不会在后台回显。停止本机常驻服务：`python3 scripts/local_service.py uninstall`；SQLite 数据保留。详细验收见 [本地 Harness 测试手册](docs/local-harness-testing.md#真实本地常驻巡检)。

## 本地测试与演示

需要 Python **3.12**。若已有虚拟环境，先检查 `.venv/bin/python --version`。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
FEISHU_AGENT_CONFIG='' .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/local_demo.py --seed --port 8801
```

打开 [本地演示](http://127.0.0.1:8801/demo) 或 [管理后台](http://127.0.0.1:8801/)。演示只绑定 localhost，使用假模型、假集群、假日志与假 CVE，经过真实任务引擎；不证明外部系统已经接通。完整验收、审批恢复、真实 Docker 测试与清理步骤见 [本地 Harness 测试手册](docs/local-harness-testing.md)。

## 连接真实服务

```bash
.venv/bin/python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑引导配置后启动；模型与资源可在后台配置
WEB_HOST=127.0.0.1 .venv/bin/python -m app.main
```

也可通过既有 Compose 部署流程启动服务；本轮没有在云端执行部署。默认数据在 `./data`；`FEISHU_AGENT_DATA_DIR` 可指定独立目录，`FEISHU_AGENT_CONFIG` 可指定引导配置路径，设为空字符串可禁用配置文件读取。云端 GitOps SSH 采集需为服务账户提供只读密钥和已验证的 `known_hosts`；镜像无 git 二进制时会使用 Paramiko/Dulwich。

1. 在后台配置 AI 供应商、监控/集群/仓库连接和所需技能。
2. 创建应用，勾选工具与技能，配置资源权限。工具可见不等于能访问所有资源；`*` 必须显式授予。
3. 保持 `readonly` 做首次诊断；要改代码时选 `approval` 并配置审批人。`compat` 仅为显式兼容选项，不是默认。
4. 配置飞书长连接并启用应用，重启服务建立连接。在飞书订阅 `im.message.receive_v1` 和审批回调 `card.action.trigger`，按实际工具需要授予权限并发布应用版本。
5. 用测试群和测试资源先验收，再接生产范围。审批不会覆盖资源限制、命令限制或外部内容污染 guard。

资源权限例子（名称应替换成后台实际登记的资源）：

```json
{
  "k8s_clusters": ["test-cluster"],
  "namespaces": ["demo"],
  "log_orgs": ["demo-org"],
  "log_streams": ["app_logs"],
  "repos": ["demo-repo"]
}
```

飞书可发送 `我的身份`、`任务列表`、`任务 <ID>`、`取消任务 <ID>`、`继续任务 <ID>`；已绑定巡检的会话还可发送 `巡检`、`巡检状态`、`问题台账`、`修复问题 <编号>`。修复任务会核对问题、在授权仓库本地改动并静态校验，具体完整 diff 通过飞书卡片审批后才由 GitLab API 创建 MR。审批等待须通过审批卡片处理；结果不明的写操作须先人工核对，不能用“继续”盲重试。应用权限、工具、技能、MCP 或模型供应商变更后，旧任务检查点停止继续使用，需按新配置新建任务。

后台配置的应用密钥、模型密钥和连接凭据可能持久化到 SQLite；`config.yaml`、数据库及备份都应按敏感数据保护。凭据不会因此自动加密。

## 代码测试的额外配置

设置 `HARNESS_SANDBOX_IMAGE` 为已存在的可信 Docker 镜像才可启用 `code_test`。依赖须预装在镜像里，测试无网络；审批绑定代码快照、测试命令和实际镜像 ID。测试容器不挂载服务配置、数据库、生产凭据或 Docker socket。

**现有主 Dockerfile 不含 Docker CLI，Compose 不会自动启用代码测试。** 云端接入还需验证可信执行节点，或明确配置控制面到 Docker daemon 的访问和一致的宿主挂载路径；不能仅设置镜像变量就认为可用。详见 [沙箱验收](docs/local-harness-testing.md#真实-docker-沙箱验收)。

## 开发与部署参考

Python 3.12 · FastAPI · SQLite WAL · lark-oapi · OpenAI 兼容模型接口 · 原生 JS 管理后台。

- [工程约定](CLAUDE.md)：数据模型、恢复契约与并发注意事项。
- [设计与历史路线](docs/harness-design.md)：已实现范围及后续计划。
- [内网部署流程](deploy/DEPLOY.md)：已有部署机制；本轮仅本地真实验收，未触发云端发布。
- CI 新增 Python 3.12 自动测试，测试通过后才进入原有 main 构建/部署阶段；本轮未触发远程 CI 或部署。
