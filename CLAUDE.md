# CLAUDE.md

给在本仓库里干活的 agent / 开发者看的工程说明：架构、约定、以及若干**非踩不可的坑**。

## 这是什么

**飞书数字员工平台**：一个进程里跑多个「数字员工(应用)」。每个应用 = 一个独立飞书机器人 + 绑定一个 AI 模型 + 勾选的工具子集 + 勾选的技能子集 + 自己的人设。群里 @ 机器人 → 大模型 + 工具循环 → 回群；带对话记忆、长期记忆、定时任务，外加一个管理后台。

单容器（Python + SQLite，前端内置），飞书走**长连接只出站**，不开入站端口。

**方向**：整个服务正在改造成一个 harness（长期在线的个人助手：监听、定时、安全地动手、改代码、懂架构）。目标、进度、各期设计见 [`docs/harness-design.md`](docs/harness-design.md)，改 `harness/`、`llm/engine.py` 前先读它，做完一期顺手更新其中的进度表。

## 架构总览

```
飞书群 ──长连接(出站)──▶ feishu/bot.py(每应用一条长连接,各自线程)
                              │ 收消息 → 按 app_id 实时取最新配置
                              ▼
                        llm/engine.py(编排：历史+长期记忆+技能简介注入 → 工具循环)
                              │                         │
                              ▼                         ▼
                        llm/tools.py(工具池)      绑定的 AI 供应商(OpenAI 兼容)
                              │ 按应用勾选的子集
            ┌─────────────────┼──────────────────┬──────────────┐
        feishu/docs       obs/(只读监控)      sched(定时任务)   read_skill
        (文档读写)    openobserve/beszel/uptimekuma            (按应用隔离)

管理员浏览器 ──:8800──▶ web/admin.py(FastAPI) ──▶ 同一个 SQLite(core/store.py)
sched/runner.py(调度线程,每30s) ──到点──▶ engine.run → bot.post 回群
```

### 包分层（依赖单向）
`core` ← `harness` ← `feishu` / `obs` / `sched` ← `llm` ← `web` ← `app`
加新能力就平级再开个包（如 obs/、sched/ 就是这么加的）。`harness` 只依赖 `core`；需要上层能力时由上层反向注册进来，不要让 harness import 上层。

| 包 | 职责 |
|---|---|
| `core/` | `config.py` 冷启动引导配置；`store.py` 数据层(全部 SQLite) |
| `harness/` | agent 内核：`runs.py` run 生命周期 + 事件日志；`pipeline.py` 工具执行管道 `run_tool()`；`policy.py` 策略预设/规则 + guard 注册表（后续：审批、外存、jobs） |
| `feishu/` | `bot.py` 多应用长连接；`auth.py` 按应用 tenant_token；`docs.py`/`md2feishu.py` 文档读写 |
| `obs/` | 只读监控集成：`openobserve`(日志)、`beszel`(服务器负载)、`uptimekuma`(服务状态) |
| `sched/` | `runner.py` 定时任务调度线程 |
| `llm/` | `engine.py` 工具循环编排；`tools.py` 工具池 + 执行器 |
| `web/` | `admin.py` 后台 API；`static/index.html` 单文件前端(无构建) |
| `app/` | `main.py` 入口：引导/迁移 → 起各应用长连接 + 调度线程 + uvicorn 后台 |

## 数据模型（core/store.py，单个 SQLite）

- `ai_providers` — 多个 AI 模型（name/base_url/model/api_key，任意 OpenAI 兼容接口）
- `apps` — 多个应用（数字员工）：飞书凭证 + `ai_provider_id` + `tools`(JSON 名单) + `skills`(JSON id 名单) + `mcp`(JSON id 名单，勾选的 MCP server) + `system_prompt` + `history_turns` + `enabled`
- `mcp_servers` — 外部 MCP server 注册表（name/url/headers(JSON 鉴权头)/enabled）；应用按 server 整体勾选后，其暴露的工具进该应用的工具循环
- `skills` — 技能库（name/description/content），知识/工作方法包，应用按需勾选
- `schedules` — 定时任务（app_id/chat_id/title/instruction/kind/spec/next_run_ts）
- `msgs` / `notes` / `token_usage` — 对话记忆 / 长期笔记 / 用量，**都带 app_id 按应用隔离**
- `runs` / `events` — 运行记录：一次 `engine.run` 是一个 run（trigger=chat/proactive/schedule/…、状态、token），每次模型回复与工具调用按 `seq` 只追加写进 `events`（类型见 `harness.runs.EVENT_TYPES`）。`tool/call` 先于执行写入；启动时遗留的 `running` 标 `aborted`；调度线程清理 90 天前的记录。后台「运行记录」页只读回放。`msgs` 仍负责对话记忆，两者并存
- `apps.policy` — 工具调用策略 JSON：`{"preset":"compat"|"readonly"}` 或 `{"preset":"custom","rules":[...]}`，空=兼容（见下「工具执行管道」）
- `kv` — 引导配置 + 全局设置（OpenObserve/Beszel/UptimeKuma 连接 `oo_*`/`bz_*`/`uk_*`；SSH 受管主机+凭证 `ssh_hosts`、硬黑名单 `ssh_blacklist`）；`admin` — 管理员
  - OpenObserve 按 **org** 隔离数据：`oo_org/oo_user/oo_pass` 是主 org，`oo_sources`（JSON 数组）存其余 org 各自的 service account；`query_logs`/`list_log_streams` 用可选 `org` 参数切换。每个 org 要用它自己建的 service account，主 org 的账号访问不了别的 org

## 核心概念

- **应用 = 数字员工 = 席位**。四层隔离：身份(独立飞书应用) + AI(各绑各的) + 工具(勾选子集) + 技能(勾选子集)。防越权靠这套，不靠代码自觉。
- **工具(tool) vs 技能(skill)**：工具是「能做的动作」(查日志/发文档)；技能是「该懂的知识/方法」(如「公司架构」)。技能用**渐进披露**：系统提示词只注入名称+简介，模型需要详情时调 `read_skill(name)` 拉全文，且只能读本应用勾选的技能。
- **定时任务**：到点用对应应用的 AI+工具+技能跑 instruction，结果发回原群(REST，不依赖长连接)。两种创建途径同一张表：对话创建(给应用勾 `schedule_task` 工具) + 后台「定时任务」页。
- **监控集成是只读原生工具**(非 MCP)：`query_logs`/`server_stats`/`service_status`，连接信息走后台「可观测」页存 kv。这些是内置在 `llm/tools.py` 里的固定工具。
- **MCP 工具是「外挂」工具**：后台「MCP 工具」页注册远程 HTTP MCP server（endpoint URL + 鉴权头），应用按 server 整体勾选 → 运行时 `llm/mcp.py` 连上它、把它 `tools/list` 出来的工具转成 OpenAI schema 挂进工具循环，名字统一 `mcp__{server_id}__{tool}` 便于路由与防撞名。**只支持远程 HTTP（Streamable HTTP，兼容 SSE 响应），不支持 stdio 本地进程。** 隔离照旧靠「勾了才有」。
- **工具执行管道（harness/pipeline.py）**：engine 里每次工具调用（原生 + MCP）都走 `run_tool()`，固定五段：**记录**（写 `tool/call`，参数从此冻结）→ **策略**（`policy.decide` 按应用规则表给 allow/deny/ask，第一条命中生效，都不命中=拒）→ **guard**（只拒不放，策略的 allow 推翻不了）→ **执行**（独立线程 + 统一超时，`copy_context` 透传 contextvar；异常归一为 `{"error"}`）→ **后处理**（写 `tool/decision`、`tool/result`）。被拒不会让对话失败，模型拿到写明原因的错误结果。审批上线（P2）前 ask 按拒绝。
  - 预设：**兼容**（默认，全部放行只叠加 guard，等于改造前行为）、**只读**（只放行 risk=read）。
  - 现有 guard：`not_exposed`（模型说出本应用没勾的工具名——以前会被照样执行）、`proactive`（旁听时拒 write/exec）、`ssh_blacklist`、`k8s_precheck`（只读集群 + 黑名单）。后两个由 `ops/ssh.py`、`ops/k8s.py` 导入时 `policy.register_guard()` 注册；`run()` 内仍调同一个 `precheck()` 兜底，防绕过管道直接调用。guard 自身抛异常按拒绝。
- **SSH 运维是高风险原生工具**：`ops/ssh.py`（paramiko）提供 `ssh_exec`/`list_ssh_hosts`，让数字员工在受管服务器上跑真实命令（如网掉了 `sudo wg-quick up wg0`）。受管主机+凭证存 kv（`ssh_hosts`/`ssh_blacklist`），后台「SSH 运维」页配。三层闸门：**① 模型判断为主**（工具说明里写死了「危险/不可逆就拒绝」，再叠加应用系统提示词+网络拓扑技能）；**② 硬黑名单兜底**（`ops.ssh._DEFAULT_BLACKLIST`，只挡 rm -rf /、mkfs、dd 到裸盘等不可逆命令，连接前就拦，不挡正常运维）；**③ 隔离**（应用勾了才有）。**铁律：绝不给「主动旁听」类应用勾 `ssh_exec`**——那等于让没人点名时也能自己 root 执行。

## 约定

- **加一个工具**：在 `llm/tools.py` 的 `TOOLS` 池加 schema，`execute()` 加分支，**并在 `_META` 登记 `risk`（read/write/exec，可按参数判断）和需要时的 `timeout`**——漏登记按 exec（最高风险）处理，只读预设下会被拒；有安全检查就写成 guard 注册进 `harness.policy`，别只写在工具函数里。要按应用上下文就用 `ctx`(含 `app_id`/`chat_id`/`skills`)。然后应用在后台勾选即可用。**若能力来自外部 MCP server，别写死进 tools.py——去后台「MCP 工具」注册 server，应用勾选即可，不用改代码。**
- **配置存哪**：每应用的配置 → `apps` 表；全局/连接类 → `kv`(`store.get_setting`/`set_setting`)。
- **记忆/用量一律按 `app_id` 隔离**。
- 前端无构建，直接改 `web/static/index.html`（`admin.py` 每次请求从磁盘读它）。
- 中文注释为主，跟现有风格走。

## ⚠️ 非踩不可的坑（都被这些坑过，改前务必看）

1. **SQLite 不能跨线程共用一个连接**：bot 线程写、后台/调度线程读，共用一个 connection 会 `database is locked`。`store.py` 用**每线程独立连接 + WAL + busy_timeout**（`_ThreadLoopProxy` 同款 `_ConnProxy`）。别退回单连接。
2. **lark ws 客户端用模块级全局事件循环**：`lark_oapi/ws/client.py` 的 `loop` 是全局的，多个 `Client.start()` 同进程跑，第二个报 `This event loop is already running`。`feishu/bot.py` 用 `_ThreadLoopProxy` 替换 `lark_oapi.ws.client.loop`，每应用线程各拿独立 loop。**这是多应用长连接成立的前提。**
3. **Dockerfile 指令行尾不能写内联注释**：`VOLUME ["/code/data"]  # 注释` 会被 Docker 把注释当成额外卷参数，建出 `#`/垃圾匿名卷；首次 create 能成，之后 `docker compose up` recreate 按 `#` 非法路径重挂 → **部署必失败**。注释必须独占一行。
4. **容器时区默认 UTC**：python:slim 容器是 UTC，调度器 `datetime.now()` 取 UTC，"每天18:00"会差 8 小时。`deploy/compose.yml` 挂 `/etc/localtime`+`/etc/timezone` 用宿主时区。
5. **新增顶层包要在 Dockerfile 加 `COPY`**：`COPY obs ./obs` / `COPY sched ./sched`，漏了容器 import 即崩。
6. **新应用要重启服务才建立长连接**：`bot.start_all()` 只在启动时为已启用应用起连接；后台新建/改飞书凭证/改启用状态后需重启进程（后台有「重启服务」按钮，或 `docker restart`）。人设/工具/AI/技能/定时任务是即时生效的。
7. **前端时间显示强制 `Asia/Shanghai`**：`dt()` 写死 `timeZone:'Asia/Shanghai'`，否则按访问者浏览器时区渲染会误导。
8. **MCP 客户端是手写同步的，别换成官方异步 SDK**：`llm/mcp.py` 用同步 `requests` 手写 Streamable HTTP 传输，正是为了不碰坑#2——官方 `mcp` SDK 是 asyncio 的，塞进多线程 bot 会重演「全局事件循环已在运行」。每次工具调用**独立连接一次**（initialize→initialized→tools/call，用完关），无跨线程 loop、无持久连接，最贴合本仓库同步风格。工具清单按 server 缓存 `_CACHE_TTL`(默认 300s) 避免每条消息都 `tools/list`；server 配置改动/删除时 `mcp.invalidate(id)` 清缓存（admin 里已接）。某个 server 连不上时 `tools_for_app` 只跳过它、不让整轮对话崩。

## 运行 / 部署

- 本地：`pip install -r requirements.txt` → `cp config.example.yaml config.yaml`(填飞书凭证+管理员密码) → `python -m app.main` → 开 http://localhost:8800。
- 容器：`docker compose up -d --build`。
- 生产：推 `main` → CI 构镜像推 Harbor → SSH 部署。内网部署细节见 `deploy/DEPLOY.md`。
- 首启自动把旧版单应用配置迁成「默认模型 + 默认应用」（`app/main.py:_migrate_legacy`），无损。
- 密钥不入库：`config.yaml`(gitignore) 或环境变量；运行时设置存 `data/agent.db`。
