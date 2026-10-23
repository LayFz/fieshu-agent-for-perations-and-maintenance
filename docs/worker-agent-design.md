# 云端 harness 控制面 + 工作机常驻编码 agent

状态：设计稿（2026-10-23），未实现。相关：[harness 设计](harness-design.md)、[服务端改造方案](server-harness-plan.md)、[浏览器实验](browser-spike.md)。

## 0. 一句话

harness（.51 常驻）继续做**控制面**：接任务、定计划、管权限和审批、持有全部凭据、负责提 MR。新增一台**工作机**，上面常驻一个 runner，按任务起一次性容器，在容器里跑现成的编码 agent（首选 Claude Agent SDK，dsh 作为第二个适配器）。工作机能力和本地一样（shell、git、装依赖、跑测试、浏览器），但**没有任何公司凭据，不能进内网，也不能直接产生对外副作用**；它唯一的产出是「补丁 + 证据」，由控制面核对后经审批提 MR。

**已定：工作机是一台独立机器，不进 k3s/VKE，不做集群节点。** agent 会执行仓库脚本、第三方依赖和自己写的代码，属于不可信代码；容器与宿主共用内核，内核或运行时漏洞、配置失误都可能逃逸。放在生产集群里，逃逸后能接触同节点 pod 的密钥、节点身份和内网；放在独立机器上，逃逸只拿到一台没有凭据、出不了内网的机器。

## 1. 目标与非目标

目标：

- 线上常驻、和本地开发体验一致的编码能力：读任意规模的仓库、改代码、装依赖、跑测试、查资料。
- 闭环：漏洞或需求 → 修复 → 测试 → 审批 → MR → CI → 评审意见 → 追加提交（补充 PR）→ 合并后复核。
- 编码 agent 可替换：Claude Code、dsh 或以后别的，不影响控制面。
- 沿用现有任务内核：持久任务、动作账本、非阻塞审批、资源授权、污染 guard、确定性提 MR。

非目标（本期）：

- 不让工作机直接 push、合并或部署；不给它 GitLab/ArgoCD/K8s/SSH 凭据。
- 不做多工作机调度与 microVM（接口留好，M3 再说）。
- 不替代 GitLab CI：CI 仍是最终的测试判定。

## 2. 架构与职责

```text
飞书 / 定时 / webhook（CI 失败、MR 评论、告警）
            │
            ▼
┌──────────────────────── harness 控制面（.51，单实例）────────────────────────┐
│ 任务内核：tasks / task_actions / approvals / outbox                           │
│ 计划：development / vulnerability workflow（现有 llm/engine.py）              │
│ 新增：jobs 队列 · runner API · 模型网关 · 源码打包 · 补丁核对                  │
│ 凭据：GitLab 读/写令牌、模型密钥、ArgoCD、K8s、SSH（全部只在这里）             │
│ 提 MR：现有 workspace.propose_mr（GitLab API 提交，不 git push）               │
└───────────────▲──────────────────────────────┬──────────────────────────────┘
                │ HTTPS：runner 主动拉任务、     │ 模型请求（短期 job 令牌）
                │ 回传事件与制品                  │
┌───────────────┴──────────────────────────────▼──────────────────────────────┐
│ 工作机（独立 VM）                                                            │
│ runner（systemd，可信代码）                                                   │
│   └─ 每个 job 一个容器：仓库副本 + 工具链 + 编码 agent + 可选浏览器           │
│ 出网：只经出境代理（白名单：包仓库、GitHub、文档站、漏洞源）                   │
│ 防火墙：拒绝全部内网网段，只放行 harness 的 runner API 与模型网关             │
└──────────────────────────────────────────────────────────────────────────────┘
```

| 职责 | 控制面 | 工作机 |
|---|---|---|
| 决定修什么、改哪个仓库、基于哪个提交 | ✓ | |
| 拉取私有仓库源码（读令牌） | ✓ 打包后下发 | 只收到源码包 |
| 改代码、装依赖、跑测试、查资料 | | ✓ |
| 模型密钥 | ✓ 模型网关代发 | 只有短期 job 令牌 |
| 核对补丁、路径策略、密钥扫描 | ✓ | |
| 审批、提 MR、追加提交、看 CI | ✓ | |
| 漏洞台账状态 | ✓ | |

关键性质：**job 本身没有外部副作用**，产出只是补丁和证据。所以 job 失败、工作机宕机、租约丢失都可以安全重试，不需要「结果不明」处理；所有有副作用的动作仍在控制面，沿用现有的动作账本和审批。

## 3. 工作机配置

| 项 | 配置 |
|---|---|
| 规格 | 8 核 / 16GB / 200GB SSD 起步；并发 job 数 = 2（每个 job 3 核 6GB 上限） |
| 位置 | 独立机器或独立 VM：不是 k3s/VKE 节点，不与 .51、.44（Harbor/GitLab/监控中枢）同机，不加入 NetBird；宿主若是虚拟化平台，与业务 VM 分开网段 |
| 系统 | Ubuntu 24.04 LTS；内核自动安装安全更新（unattended-upgrades + 每周重建时重启） |
| 运行用户 | `runner` 普通用户；容器用 rootless Docker 或 Podman；runner 不在 docker 组之外拿 root |
| job 容器 | 非 root、`--cap-drop=ALL`、`no-new-privileges`、seccomp 默认配置、CPU/内存/PID/磁盘配额；**不挂 Docker socket、不挂宿主目录**；工作目录是 job 专属卷，结束即删；处理外部来源代码（PoC、陌生仓库）的 job 使用 gVisor（`runsc`）运行时 |
| 镜像 | `worker-base`（git、curl、jq、ripgrep、python3.12、node22、编码 agent CLI、Chromium + Playwright）+ 按语言的工具链镜像（go、java/maven、rust）；镜像在控制面登记 digest，runner 只运行登记过的 digest |
| 依赖缓存 | 每个工具链一个只读共享缓存（pip/npm/go mod/maven），加内网制品库或代理镜像；缓存只能由 runner 的预热任务写 |
| 出网 | 只允许经出境代理；代理按域名白名单（PyPI、npm、Go proxy、Maven Central、GitHub、各语言文档站、NVD/OSV/GHSA）；代理拒绝 RFC1918、169.254/16、100.64/10 |
| 防火墙 | nftables：出站只放行 代理:端口、harness runner API、模型网关；入站只放行运维 SSH（跳板来源）与可选的 noVNC（仅后台反代来源） |
| 不存在的东西 | 公司 GitLab 令牌、SSH 私钥、kubeconfig、ArgoCD 令牌、模型主密钥、飞书凭据、生产数据库地址 |
| 观测 | 接 Beszel 主机监控与 OpenObserve 日志（按 fleet 接入 SOP）；runner 日志不含 job 令牌 |
| 重置 | 每周按快照重建；runner 启动时清理残留容器与卷；job 级隔离靠容器，不靠重置 |
| 可视化 | 可选：job 容器内 Xvfb + Chromium，有头模式经 noVNC 由后台反代展示，供人工接管验证码 |

## 4. 任务下发协议

### 4.1 为什么是 runner 主动拉

和 GitLab Runner 同样的模式：工作机不开入站端口；控制面保持唯一权威（状态、租约、取消都在控制面）；工作机可以在 NAT 后面；以后加工作机只是多一个 runner。

### 4.2 身份与令牌

- **runner 令牌**：后台登记 runner 时生成，只显示一次，库里存 SHA-256；只能调用 `/runner/v1/*`。
- **job 令牌**：领取 job 时签发，绑定 `job_id + epoch`，有效期到租约到期，续租时轮换；只能访问该 job 的源码包、事件、制品，以及模型网关。job 令牌进入 job 容器；runner 令牌永不进入容器。
- 所有调用走 HTTPS（内网 CA 或后台反代的证书）；控制面校验 `epoch`，旧 epoch 的写入一律 409。

### 4.3 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/runner/v1/claim` | 长轮询（≤25s）。请求带 runner 能力（镜像、并发余量）；有 job 返回 job spec 和 job 令牌，否则 204 |
| POST | `/runner/v1/jobs/{id}/heartbeat` | 续租（默认 60s 租约、20s 心跳），返回 `{cancel, new_token}` |
| GET | `/runner/v1/jobs/{id}/source` | 源码包（`git bundle`，含基线提交；不含 `.git/config` 里的凭据） |
| POST | `/runner/v1/jobs/{id}/events` | 批量事件：阶段、agent 工具调用摘要、token 用量；写入现有 runs/events |
| PUT | `/runner/v1/jobs/{id}/artifacts/{name}` | 制品：`patch.diff`、`test.log`、`transcript.jsonl`、`summary.md`、截图；单个 ≤20MB |
| POST | `/runner/v1/jobs/{id}/complete` | 结束：`succeeded / failed / cancelled`，附结果摘要与制品哈希 |
| POST | `/llm/v1/messages` | 模型网关（Anthropic Messages 格式），按 job 令牌鉴权、计费、限额 |
| POST | `/llm/v1/chat/completions` | 模型网关（OpenAI 格式），给 dsh 等适配器用 |

### 4.4 job spec（控制面 → runner）

```json
{
  "job_id": "j_7f3c…", "epoch": 3, "task_id": "t_…", "kind": "code_fix",
  "repo": {"name": "upmore-api", "base_sha": "9c1e…", "base_branch": "main",
           "mr": null},
  "goal": "修复 CVE-2025-XXXX：将 golang.org/x/net 升级到 ≥0.38.0，并确保构建与单测通过。",
  "context": ["漏洞台账条目摘要", "相关技能/架构说明", "仓库 AGENTS.md 由仓库自带"],
  "constraints": {
    "allowed_paths": ["**"], "denied_paths": [".gitlab-ci.yml", "deploy/**", "**/*secret*"],
    "max_changed_files": 40, "max_patch_bytes": 300000
  },
  "agent": {"adapter": "claude-sdk", "model": "gateway:deepseek-v3", "max_turns": 80,
            "max_budget_usd": 3, "timeout_s": 2400},
  "image": "registry.internal/worker-go@sha256:…",
  "test": {"command": "go test ./...", "timeout_s": 900},
  "network": "proxy"
}
```

### 4.5 runner 内一个 job 的执行

1. 校验镜像 digest 在登记清单里；创建 job 卷，解开源码包，检出 `base_sha`（工作区内 `git` 无远端）。
2. 起 job 容器：注入 job 令牌、模型网关地址（`ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` 指向网关）、出境代理；仓库挂在 `/work/repo`。
3. 适配器启动编码 agent，传入目标、约束和系统提示（只改仓库、不 push、结束时写 `summary.md`）。agent 的每个工具调用由适配器的权限回调按约束放行或拒绝（见 §5），并转成事件上报。
4. agent 结束后，**runner 自己**在一个全新容器里，用仓库登记的测试命令对最终代码树跑测试（不采信 agent 自述的测试结果）。
5. runner 生成 `git diff --binary base_sha`，计算补丁与测试日志的 SHA-256，上传制品，调用 `complete`。
6. 心跳返回 `cancel=true` 时：适配器先请求 agent 中断，10s 后强制删除容器，然后 `complete(cancelled)`。

### 4.6 状态机与恢复

```text
queued → claimed → running → succeeded / failed / cancelled
               ↘ 租约过期 → queued（epoch+1，最多重试 2 次）→ 超过则 failed
```

- job 没有外部副作用，租约过期可直接重排；旧 runner 的迟到写入因 epoch 不符被拒。
- 控制面任务在 job 运行期间**挂起**（与审批同一机制），不占 worker；job 结束时唤醒任务，任务从检查点继续。
- 同一 `action_key` 只建一个 job：任务恢复时若发现 job 已完成，直接复用结果（与现有动作账本一致）。

## 5. 编码 agent 选型

### 5.1 现状核实（2026-10-23）

| 能力 | Claude Agent SDK（Python 0.2.173 / CLI 2.1.295） | dsh Python SDK（0.2.1-rc.2） |
|---|---|---|
| 运行形态 | 库内启动 Claude Code 子进程，流式消息 | `dsh --profile sdk` 子进程，stdio JSON-RPC |
| 中途取消 | ✓ `interrupt()` | ✗ 无取消接口，只能结束进程 |
| 逐个工具审批 | ✓ `can_use_tool` 回调；CLI 有 `--permission-mode`、`--allowedTools/--disallowedTools` | 低层可应答服务端请求（`next_request/respond`），sdk profile 是否发审批请求未见文档 |
| 预算与轮数 | ✓ `max_budget_usd`、`max_turns` | `max_tokens`（单次输出）；无总预算 |
| 会话恢复 | ✓ resume | ✓ `resume_session` |
| 模型 | Claude 系列；可经 `ANTHROPIC_BASE_URL` 指向兼容 Anthropic 接口的网关（DeepSeek 等提供此类端点，非官方支持，效果需实测） | 默认 DeepSeek；挂 `llm-pi-ai` 后可接 OpenAI 兼容网关（豆包等） |
| 工具与扩展 | 内置文件编辑、bash、搜索、子 agent；MCP；hooks | 内置文件编辑、shell、工作区、Web；插件需 TypeScript |
| 成熟度与许可 | 广泛使用；使用受 Anthropic 商业条款约束 | 开发者预览、无兼容承诺；MIT |

### 5.2 结论

- **runner 内定义适配器接口**，控制面只看到统一的 job 语义：

  ```text
  start(spec, workdir, emit) → handle
  handle.permission(tool, input) → allow / deny（由 runner 按约束实现）
  handle.interrupt()
  handle.wait() → {final_text, finish_reason, usage}
  ```

- **M0 先做 Claude Agent SDK 适配器**：取消、逐工具权限回调、预算三项是安全运行的前提，它都有；模型先用 Claude，经模型网关计费。
- **M2 再做 dsh 适配器**：用 DeepSeek/豆包更便宜；取消用「结束进程 + 删除容器」实现（job 无外部副作用，可以接受）；权限约束改由容器边界与补丁核对兜底。
- **在 Claude Code 里跑非 Claude 模型**：可以作为成本选项实测，但不作为默认，效果以同一批修复任务的通过率对比为准（复用 §10 的验收集）。
- 不在控制面里自研编码 agent 循环：现有 `code_*` 工具保留给小改动和 GitOps 改 YAML；大改动交给工作机。

### 5.3 适配器的权限回调（runner 实现，agent 无法绕过）

- 文件写入：路径必须在 `/work/repo` 内且不命中 `denied_paths`。
- shell：允许（容器就是边界）；但拒绝 `git push`、`git remote add`、修改 `~/.gitconfig` 凭据助手（避免误以为能推送）。
- 网络：由代理白名单决定，不在回调里判断。
- MCP：只开放 runner 自带的浏览器工具（Playwright 库 + CDPSession，按 [浏览器实验](browser-spike.md) 的任务级工具设计）。

## 6. 代码修复到 MR 的闭环

### 6.1 首次修复

```text
1 触发   漏洞台账新增可修条目 / 飞书「修一下 X」/ CI 失败 webhook
2 计划   控制面 development 或 vulnerability workflow：确定仓库、基线、目标、约束（授权检查：repos 资源）
3 下发   动作 code_delegate（新工具）→ 建 job → 任务挂起
4 执行   工作机：agent 改代码 → runner 重新跑测试 → 回传补丁与证据
5 核对   控制面（确定性代码）：
         · 补丁能在 base_sha 上干净应用；改动文件数与大小在上限内
         · 路径策略：不碰 denied_paths；不新增二进制；不改 CI/部署文件（除非任务显式允许）
         · 密钥扫描：补丁里不得出现令牌/私钥模式
         · 把补丁应用到控制面工作区（现有 workspace），得到快照哈希
         · 记录 runner 测试证据，绑定 快照哈希 + 镜像 digest + 测试命令
6 审批   飞书卡片：完整 diff（超过预览上限拒绝审批）、agent 摘要、测试结果、风险说明
7 提 MR  现有 code_propose_mr：GitLab API 在新分支一次性提交，建 MR；写令牌不离开控制面
8 CI     监听 MR 流水线（GitLab webhook 或轮询），结果回写任务与飞书
9 台账   漏洞条目 → mr_open；合并后 → merged；发布并复扫通过后 → fixed（现有规则：MR 不等于修复）
```

### 6.2 补充 PR（评审意见 / CI 失败后追加提交）

- 触发：GitLab MR 评论（Note Hook）或流水线失败（Pipeline Hook）进入现有 webhook，按 MR 关联到原任务的后续任务。
- 下发：job spec 的 `repo.mr = {iid, source_branch, head_sha}`，基线改为 MR 分支头；context 附上评论原文或失败日志（标注为外部数据）。
- 核对同 6.1；另加「MR 分支头仍是 head_sha」检查，有人在期间推送过则停止并提示。
- 提交：新动作 `code_update_mr`（write，需审批），用 GitLab Commits API 在同一分支追加一个提交，不强推、不改历史。
- 循环上限：同一 MR 自动追加最多 3 轮，之后转人工。

### 6.3 测试证据的信任层级

| 证据 | 谁执行 | 用途 |
|---|---|---|
| agent 自述「测试通过」 | agent | 只作参考，不作为门槛 |
| runner 在新容器里跑登记的测试命令 | runner（可信代码） | 提 MR 的门槛（替代现有「控制面 Docker 沙箱测试」，因为工作机能装依赖） |
| GitOps 静态校验 | 控制面 | GitOps 仓库的门槛（现有） |
| GitLab CI | GitLab | 最终判定；不通过则触发 6.2 或转人工 |

### 6.4 与现有 CI/CD 的衔接（Harbor + ArgoCD）

现有发布链路（见 k8s-ops 手册）：push main → 集群内 buildkitd 构建、tag 为 8 位 sha 推 Harbor → Image Updater 每 2 分钟回写 gitops 仓 → ArgoCD 同步（内网应用自动；云上 4 个应用手动 Sync）。CI 只有推 Harbor 的权限。agent 不进入这条链路，只在入口提 MR、在出口做核对：

```text
Harbor 镜像扫描 ─┐
KEV/OSV/GHSA ────┼─→ 漏洞台账（镜像 → ArgoCD 应用 → 集群/命名空间 → 源码仓）
                 │
                 ▼
     修复类型判断（受信代码 + 模型计划）
       ├─ 依赖/基础镜像 → 源码仓 MR（工作机编码 agent）
       └─ 清单/chart/配置 → gitops 仓 MR（控制面 code_* + GitOps 静态校验）
                 ▼
     审批 → MR → 人合并 → CI 构建推 Harbor → Image Updater → ArgoCD
                 ▼
     复核（确定性）：Harbor 对新镜像的扫描不再含该 CVE
                   且 ArgoCD 显示目标应用运行的是新 tag 且 Synced/Healthy
                 → 台账 fixed；任一不满足 → 保持 mr_merged 并提醒
```

- **资产匹配**：Harbor 扫描给出「每个镜像有哪些 CVE」，ArgoCD 给出「镜像跑在哪个应用/集群」，补上当前漏洞台账缺的资产对应。前提是 Harbor 已启用扫描器（待核实）。
- **云上手动同步**：新增确定性动作 `argocd_sync`（write，需审批），只针对 `*-vke` 应用；使用只授予 `applications, sync` 的 ArgoCD 项目角色令牌，不复用管理员账号。
- **合并**：先由人合并。之后可按策略对低风险改动自动合并（如依赖补丁版本升级、CI 全绿、改动只在依赖文件），策略默认关闭。
- **agent 永远不做**：合并、push main、`kubectl edit`、持有 CI/Harbor/集群凭据。

## 7. 安全模型

| 威胁 | 对策 |
|---|---|
| 仓库或网页里的提示注入让 agent 写恶意代码 | 审批卡片展示完整 diff；路径策略与密钥扫描；CI；MR 不自动合并 |
| agent 外传源码或数据 | 工作机只能经白名单代理出网；源码包只含目标仓库；不下发其他仓库与内部文档 |
| agent 攻击内网 | 防火墙拒绝内网网段；代理拒绝内网目标；工作机无内网凭据 |
| 窃取模型密钥 | 工作机只有短期 job 令牌；网关按 job 限额、可随时吊销 |
| runner 被攻破冒充其他 job | job 令牌绑定 job_id+epoch；控制面校验 epoch；runner 令牌可吊销 |
| 补丁伪造测试结果 | 测试由 runner 在新容器执行；证据绑定补丁哈希与镜像 digest；CI 兜底 |
| 工作机残留数据 | job 卷用完即删；每周重建；不保存凭据 |
| 资源滥用（挖矿、刷流量） | 容器配额；代理白名单；job 超时与预算；监控告警 |

控制面原有规则不变：job 来自外部触发（webhook、漏洞源）时任务为污染态；`code_delegate` 是 local 风险但必须有 `repos` 资源授权；`code_propose_mr` / `code_update_mr` 必须审批。

## 8. 数据模型

| 表 | 字段（要点） |
|---|---|
| `runners` | id、name、token_hash、labels（可用镜像）、max_jobs、enabled、last_seen |
| `jobs` | id、task_id、action_key、app_id、repo_id、kind、spec(json)、status、runner_id、lease_owner、lease_epoch、lease_until、attempts、cancel_requested、result(json)、created、updated |
| `job_artifacts` | job_id、name、sha256、bytes、path（数据目录下） |
| `llm_usage` | job_id、model、input/output tokens、cost；并入现有 token_usage 汇总 |
| `workspaces` 元数据 | 新增 `source=job:{id}`、`runner_test`（绑定快照）、`mr_iid`、`mr_head_sha` |

## 9. 现有代码的接入点

| 位置 | 改动 |
|---|---|
| `core/store.py` / 新 `core/jobstore.py` | 上述表与带 epoch fencing 的 claim/heartbeat/complete |
| `harness/jobs.py`（新） | job 生命周期、租约过期重排、唤醒挂起任务；只依赖 core |
| `web/runner_api.py`（新） | `/runner/v1/*` 与 `/llm/v1/*`，与管理后台分开鉴权 |
| `harness/workspace.py` | `apply_patch(ws, patch, base_sha)`；`propose_mr` 接受 runner 测试证据；新增 `update_mr` |
| `llm/tools.py` / `llm/authorization.py` | `code_delegate`（local）、`code_update_mr`（write）；授权检查 repos 资源与 MR 归属 |
| `harness/workflows.py` | development 的最低证据加入 job 结果；补充 PR 的上下文模板 |
| `harness/webhooks.py` | GitLab Note/Pipeline 事件到 MR 关联任务的路由（受信代码解析，不交给模型） |
| `web/static/index.html` | 「工作机」页：runner 状态、job 列表、实时事件、制品下载、取消 |
| `worker/`（新，独立部署） | runner、适配器（claude-sdk、dsh）、镜像定义、nftables 与代理配置样例 |

## 10. 分期与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 跑通 | 单 runner；Claude SDK 适配器；源码包下发；补丁回传；控制面应用补丁 + 现有提 MR；手动从飞书触发 | 3 个真实小修复（依赖升级、单测失败、配置错误）从飞书到 MR 全程无人工改代码；工作机上找不到任何公司凭据 |
| M1 可运维 | 模型网关与预算；取消；租约重排；事件进运行记录；后台工作机页；runner 自测 | 运行中拔掉工作机网络/重启 runner，job 自动重排且不产生重复 MR；取消 10s 内容器消失 |
| M2 闭环 | 补充 PR（评论/CI 失败触发追加提交）；CI 结果回写；Harbor 扫描入台账与发布后复核；云上 `argocd_sync`；dsh 适配器 | 一个漏洞从台账到 fixed 全程留痕；同一批任务上 Claude/DeepSeek 两个适配器的通过率、成本对比 |
| M3 扩展 | 多 runner；每 job 一台 microVM（需宿主 KVM）；有头浏览器 + noVNC 人工接管 | 并发 4 个 job 互不可见；验证码场景可人工接管后继续 |

## 11. 待确认

1. 工作机用哪台：新装一台物理机、虚拟化平台上的独立 VM，还是云上 ECS（天然不在内网，回连 harness 经 up-server2 转发）？宿主是否支持 KVM（决定 M3 能否做 microVM）？
2. Harbor 是否已启用 Trivy 扫描；harness 能否拿到一个只读的 Harbor robot 账号读取扫描结果。
3. ArgoCD 能否为 harness 建一个只有 sync 权限的项目角色（云上手动同步用）。
4. 默认模型：Claude（经网关计费）还是 DeepSeek/豆包？这决定 M0 用哪个适配器更划算。
5. 出境代理白名单的初始范围；依赖镜像能否复用 Harbor 的代理缓存项目。
6. GitLab 能否给 harness 配 Note Hook / Pipeline Hook（补充 PR 依赖它）。
7. 每月模型预算上限与单 job 预算默认值。
