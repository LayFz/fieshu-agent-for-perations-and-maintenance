# 本地 Harness 测试与验收

本文覆盖本地自动测试、交互演示和显式启用的真实 Docker 沙箱测试。目标是验证服务端任务内核能持续处理集群、日志和漏洞任务，而不是把模拟结果当成生产系统已接通。

**验收记录（2026-10-23）**：第一阶段使用 Python 3.12.13，66 项测试通过，其中 3 项使用本机真实 Docker 验证隔离、超时销毁和取消清理。持续巡检、GitOps 和日志采集加入后，当前普通测试模式执行 140 项，137 项通过、3 项真实 Docker 测试按设计跳过；前述 Docker 实测发生在新巡检代码加入之前。本轮没有部署云端或创建真实 GitLab MR。

**真实本地联调**：本机常驻服务已使用现有飞书应用建立长连接，扫描真实 Kubernetes/ArgoCD、两个 GitOps 仓库、两个 GitLab 项目的流水线 API，以及三个 OpenObserve 日志流。服务重启后扫描和连接恢复。飞书群列表缺少相应权限，目标会话及审批人尚未绑定，因此尚无飞书巡检报告投递和真实 MR 审批的端到端证据。这里的 24 小时是常驻与恢复设计，尚无连续运行 24 小时的实测记录。

## 1. 准备独立环境

在仓库根目录执行，需要 Python 3.12：

```bash
python3.12 -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements-dev.txt
```

如果已有 `.venv`，先确认版本，再决定是否创建另一环境。本轮已有 `.venv` 是 Python 3.9.6，没有覆盖；实际使用的是临时环境 `/private/tmp/feishu-harness-py312/bin/python`（3.12.13）。该临时路径仅为此次记录，不是仓库运行依赖；下文 `.venv/bin/python` 均指你已准备好的 Python 3.12 解释器。

`requirements-dev.txt` 引用运行时依赖并增加 `httpx`，仅供 FastAPI 测试客户端使用；生产只需 `requirements.txt`。普通测试使用临时 SQLite 和外部接口替身，不需要飞书、模型或生产凭据。

## 2. 自动测试

```bash
FEISHU_AGENT_CONFIG='' PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest discover -s tests -v
```

`FEISHU_AGENT_CONFIG=''` 禁用 `config.yaml` 读取；测试默认创建临时数据目录。不要把 `FEISHU_AGENT_DATA_DIR` 指到已有业务数据库。普通测试不启动真实 Docker，真实沙箱测试只有设置 `HARNESS_LIVE_SANDBOX=1` 才运行。

| 测试文件 | 主要验证 |
|---|---|
| `test_tasks.py` | 持久去重、同群串行、租约 fencing、只读恢复、未知写动作停止、取消、审批竞态、通知重试、定时原子入队 |
| `test_pipeline.py` | readonly/deny 默认、非法参数拒绝、动作结果复用、当前授权复查、外部内容污染、审批挂起/恢复/过期、快照、超时与审计失败 |
| `test_access.py` | 显式资源权限、用户/群限制、kubectl 只读语法、Secret 别名/组/组合资源、身份/原始路径/本地文件绕过 |
| `test_workspace.py` | 应用/群/任务工作区边界、快照和命令变化、独立测试输入、MR 测试证据、未知提交停止与已有 MR 复用 |
| `test_sandbox.py` | 默认禁用、Docker 隔离参数、镜像卷限制、输出上限、超时和清理不确定性 |
| `test_integration.py` | 模拟飞书入口到真实任务引擎、三类工作流、两次审批修复路径、检查点恢复、权限/供应商变化、后台鉴权 |
| `test_sandbox_live.py` | 显式启用的真实 Docker 隔离/超时/取消测试 |
| `test_patrols.py`、`test_cluster_scan.py`、`test_gitops_scan.py`、`test_log_scan.py`、`test_bot_supervision.py` | 持续巡检的租约/恢复/来源差异/通知、只读采集、Secret 脱敏、日志聚合、长连接重连 |

CI 在现有 `build` 标签 shell runner 上用 `python:3.12-slim` 容器运行普通测试；仓库只读挂载、数据库放容器 `/tmp`，不挂载 Docker socket，不启用 live sandbox。Runner 需要能获取镜像和 pip 依赖。MR 与 main 都测试；main 测试成功后才进入原有构建/部署流程。本次只编辑了配置，没有触发远程 CI 或部署。

## 3. 启动可交互演示

```bash
.venv/bin/python scripts/local_demo.py --seed --port 8801
```

- 飞书模拟台：[http://127.0.0.1:8801/demo](http://127.0.0.1:8801/demo)
- 管理后台：[http://127.0.0.1:8801/](http://127.0.0.1:8801/)
- 演示账号：`local-demo`；演示密码：`local-demo-2026`。只用于本地演示。
- 默认演示数据目录：`data/local-demo`，带 `.local-demo` 标记；可用 `--data-dir /tmp/feishu-harness-demo` 指定独立目录。脚本拒绝复用没有演示标记的已有数据库，也拒绝使用仓库根或正式 `data` 根目录。

脚本只监听 `127.0.0.1`，禁用配置文件和真实引导凭据，替换模型/集群/日志/CVE/飞书通知边界，阻止出站连接及子进程。任务队列、权限、工具管道、审批、检查点和 SQLite 均使用真实实现；这不是生产入口或通用代码沙箱。

`--seed` 幂等创建三个样例，重启不会重复播种。模拟台里“巡检集群”“分析日志”“查询漏洞”会填入样例指令，再点击提交。每个返回都明确标注模拟。后台“任务”查看详情、证据、动作状态；可以取消或在允许的状态下继续任务。演示后台禁止修改真实连接配置或重启到正式入口。

## 4. 三类场景的验收标准

| 场景 | 演示输入与预期 | 自动测试覆盖 | 真实联调还需验证 |
|---|---|---|---|
| 集群管理 | `巡检集群 demo-cluster`：成功读取模拟 pods；报告 CrashLoopBackOff 与重启次数，保留查询证据，不宣称已恢复 | 飞书重复投递只建一个任务、只读授权查询、缺少查询证据不能完成 | 测试集群 RBAC/namespace、事件与日志关联；GitOps 修复 MR、发布后健康确认 |
| 问题日志 | `分析 demo-api 问题日志`：两条模拟日志共享 trace，区分关联事实与根因假设 | org/stream 授权、实际日志/事件证据门槛；仅查资源清单不算日志分析完成 | 真实 OpenObserve 查询窗口/时区/数据量、告警与集群事件关联 |
| 漏洞与修复 | `查询 demo-package 漏洞`：返回虚构 `DEMO-CVE-0001` 与明确“未核对资产版本”；不伪造修复 | 漏洞查询、隔离工作区、测试审批→测试通过→MR 审批→MR；恢复不重复执行，缺测试/MR 不能完成修复 | 真实情报与依赖/镜像版本匹配、公司 GitLab 权限、评审合并、发布版本与复扫 |

演示仅提供固定查询，不执行真实代码修复；完整修复路径在集成测试中使用仓库和 GitLab 替身，真实代码隔离由下面的 Docker 测试独立验证。最小证据检查不等同于资产已匹配、根因已确认或漏洞已消除。

## 5. 审批挂起与重启恢复

在模拟台提交 `记住：演示验收项`，这是仅写演示数据库的动作：

1. 任务变为 `waiting_approval`，出现 `remember` 待审批项；此时其他查询任务仍可执行。
2. 可先按 Ctrl-C 停止服务，再用相同数据目录启动；原任务和待审批项仍在。
3. 点击“批准一次”，由模拟独立审批人 `demo-reviewer` 决策；任务恢复完成，动作只执行一次，通知出现在模拟台。拒绝则不写笔记。
4. 重复点击、通知重试、恢复模型工具回复不能产生重复写入。取消等待中的任务后，原审批不能再让动作执行。

生产飞书审批依赖 `card.action.trigger` 长连接订阅与发布；本地模拟不验证飞书的身份事件签名、网络或卡片投递。没有审批人、审批过期、跨 app 回调或审批人已撤销时均不应执行。

对持久动作的中断恢复，自动测试分别覆盖模型响应已保存、工具结果已落账、等待审批、旧 worker 租约失效和结果不明。**不要为了验收手动改生产数据库状态。** 只读动作可恢复重试；非只读动作若执行结果不明进入 `needs_attention`，必须核对外部状态，系统不会自动重新执行。取消不是已经发生的外部副作用的回滚。

应用资源、工具、技能、MCP 或模型供应商等配置变更后，旧任务上下文指纹失效；先核对旧任务，再按新配置新建任务。“继续任务”不能绕过此限制。历史与新笔记也按应用、群、授权上下文隔离，不能自动送给新供应商。

## 真实 Docker 沙箱验收

默认没有 `HARNESS_SANDBOX_IMAGE`，`code_test` 直接失败；不会把仓库代码交给宿主 shell。真实验收需要本机可用的 Docker CLI/daemon，以及**已经存在**的可信 `python:3.12-slim` 镜像；测试不会自动拉镜像。先自行准备镜像，再运行：

```bash
docker image inspect python:3.12-slim
HARNESS_LIVE_SANDBOX=1 HARNESS_SANDBOX_IMAGE=python:3.12-slim \
  FEISHU_AGENT_CONFIG='' PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest discover -s tests -p test_sandbox_live.py -v
```

三个测试验证：非 root 运行、输入只读且无生产配置/数据库/socket、无外网连接；真实超时后容器已销毁；取消后确认容器清理。全部测试也可合并运行：

```bash
HARNESS_LIVE_SANDBOX=1 HARNESS_SANDBOX_IMAGE=python:3.12-slim \
  FEISHU_AGENT_CONFIG='' PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest discover -s tests -v
```

实际代码测试只挂载独立快照 `/input`，复制到容器临时 `/workspace` 后运行固定命令；根文件系统只读、无网络、CPU/内存/PID 有限额，输出有上限。依赖需预装在可信镜像。审批和测试证据绑定全仓快照、测试命令及实际 image ID；任何变化使旧审批/测试证据失效。无法确认进程/容器停止时任务标为结果不明，不能宣称安全结束。

**主服务镜像与 Compose 并不提供开箱即用的代码测试。** 主 Dockerfile 不含 Docker CLI；本轮真实验收在本机 Docker 完成。云上要配置并验证可信执行节点，或控制面可用的 CLI/daemon 与一致的宿主路径。不要向运行仓库代码的测试容器挂 Docker socket 或生产目录。Docker 隔离参数已测不等于云端网络、权限和运维拓扑已验收。

## 6. 停止与后续接入

用 Ctrl-C 停止演示。需保留重启恢复记录时保留 `data/local-demo`；需从零演示时改用新的空 `--data-dir`，不必删除现有业务数据。

正式接入使用 `python -m app.main`，先配置测试应用/群、审批人、只读连接与明确资源白名单。随后按三类场景逐项验收真实飞书、模型、测试集群/日志、GitLab 和漏洞源；云部署、真实业务闭环、多实例、高可用及负载测试均是后续工作。

## 真实本地常驻巡检

在已有 `config.yaml`、授权的应用和 Python 3.12 环境上，macOS 登录会话内可安装本机常驻服务：

```bash
python3.12 -m venv .venv-runtime
.venv-runtime/bin/python -m pip install -r requirements-dev.txt
python3 scripts/local_service.py install
python3 scripts/local_service.py status
```

`launchd` 使用 `KeepAlive` 重启退出的进程；登录会话退出或机器关机不在本机 24 小时运行保证内。生产云端仍须按部署环境设置单实例进程守护和持久化数据卷。后台 [持续巡检](http://127.0.0.1:8800/#patrols) 与模拟端口 `8801` 是两个独立服务。

本机测试应用已明确授权 `upmore` 集群、全命名空间、`layfz/gitops` 与 `layfz/gitops-cloud` 项目、`default` 组织下三个日志流。巡检以 900 秒为间隔，将快照和问题状态保存到 `data/agent.db`。2026-10-23 的应用自主扫描中，5 个集群来源、2 个 GitLab 流水线来源和 3 个日志来源为 `ok`；GitOps 两仓库能读取，但部分 ArgoCD 比较版本与仓库 HEAD 不同或部分路径离线解析不完整，来源保持 `partial`。当轮问题台账有 24 项活动问题；GitLab API 查询成功、最近流水线返回 0 项。日志关键词计数不能替代查看原始日志或判定根因。

应用自身的 patrol worker 负责定时扫描、写入、问题差异和通知重试；测试过程没有用 Codex 的手动扫描代替服务行为。重启前后服务 PID 改变，飞书长连接重新进入 `listening/connected`，SQLite 中旧扫描和问题保留。首次绑定已核实的飞书群后，下一轮扫描会主动发送基线报告；群未绑定时不发送环境详情。

后台群列表当前收到飞书 `99991672`：需为应用开通 `im:chat:readonly` 或 `im:chat.group_info:readonly` 并发布，才能列出当前加入的群。无需群列表权限的路径是在目标群 @机器人发送“我的身份”，后台会记录实际收到消息的会话及发言人 `open_id`；在“持续巡检”中选择该会话作为汇报目标，并在应用中配置审批人。机器人不能从未与它交互的群中推断目的地。GitLab 已为两个项目各接入 Developer、`api` + `read_repository` 的项目令牌（2027-01-20 到期）；应用已验证只读 API、HTTPS 克隆和本地 GitOps YAML 静态校验。写入仅通过飞书审批具体 diff 后的 `code_propose_mr`；尚未实测真实 MR 创建。云端 GitOps 采集还需把只读 SSH 密钥、已确认的 `known_hosts` 提供给服务账户。
