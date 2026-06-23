# CLAUDE.md

给在本仓库里干活的 agent / 开发者看的工程说明：架构、约定、以及若干**非踩不可的坑**。

## 这是什么

**飞书数字员工平台**：一个进程里跑多个「数字员工(应用)」。每个应用 = 一个独立飞书机器人 + 绑定一个 AI 模型 + 勾选的工具子集 + 勾选的技能子集 + 自己的人设。群里 @ 机器人 → 大模型 + 工具循环 → 回群；带对话记忆、长期记忆、定时任务，外加一个管理后台。

单容器（Python + SQLite，前端内置），飞书走**长连接只出站**，不开入站端口。

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
`core` ← `feishu` / `obs` / `sched` ← `llm` ← `web` ← `app`
加新能力就平级再开个包（如 obs/、sched/ 就是这么加的）。

| 包 | 职责 |
|---|---|
| `core/` | `config.py` 冷启动引导配置；`store.py` 数据层(全部 SQLite) |
| `feishu/` | `bot.py` 多应用长连接；`auth.py` 按应用 tenant_token；`docs.py`/`md2feishu.py` 文档读写 |
| `obs/` | 只读监控集成：`openobserve`(日志)、`beszel`(服务器负载)、`uptimekuma`(服务状态) |
| `sched/` | `runner.py` 定时任务调度线程 |
| `llm/` | `engine.py` 工具循环编排；`tools.py` 工具池 + 执行器 |
| `web/` | `admin.py` 后台 API；`static/index.html` 单文件前端(无构建) |
| `app/` | `main.py` 入口：引导/迁移 → 起各应用长连接 + 调度线程 + uvicorn 后台 |

## 数据模型（core/store.py，单个 SQLite）

- `ai_providers` — 多个 AI 模型（name/base_url/model/api_key，任意 OpenAI 兼容接口）
- `apps` — 多个应用（数字员工）：飞书凭证 + `ai_provider_id` + `tools`(JSON 名单) + `skills`(JSON id 名单) + `system_prompt` + `history_turns` + `enabled`
- `skills` — 技能库（name/description/content），知识/工作方法包，应用按需勾选
- `schedules` — 定时任务（app_id/chat_id/title/instruction/kind/spec/next_run_ts）
- `msgs` / `notes` / `token_usage` — 对话记忆 / 长期笔记 / 用量，**都带 app_id 按应用隔离**
- `kv` — 引导配置 + 全局设置（如 OpenObserve/Beszel/UptimeKuma 连接 `oo_*`/`bz_*`/`uk_*`）；`admin` — 管理员

## 核心概念

- **应用 = 数字员工 = 席位**。四层隔离：身份(独立飞书应用) + AI(各绑各的) + 工具(勾选子集) + 技能(勾选子集)。防越权靠这套，不靠代码自觉。
- **工具(tool) vs 技能(skill)**：工具是「能做的动作」(查日志/发文档)；技能是「该懂的知识/方法」(如「公司架构」)。技能用**渐进披露**：系统提示词只注入名称+简介，模型需要详情时调 `read_skill(name)` 拉全文，且只能读本应用勾选的技能。
- **定时任务**：到点用对应应用的 AI+工具+技能跑 instruction，结果发回原群(REST，不依赖长连接)。两种创建途径同一张表：对话创建(给应用勾 `schedule_task` 工具) + 后台「定时任务」页。
- **监控集成是只读原生工具**(非 MCP)：`query_logs`/`server_stats`/`service_status`，连接信息走后台「可观测」页存 kv。MCP 客户端化是后续产品化时的方向，当前未实现。

## 约定

- **加一个工具**：在 `llm/tools.py` 的 `TOOLS` 池加 schema，`execute()` 加分支；要按应用上下文就用 `ctx`(含 `app_id`/`chat_id`/`skills`)。然后应用在后台勾选即可用。
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

## 运行 / 部署

- 本地：`pip install -r requirements.txt` → `cp config.example.yaml config.yaml`(填飞书凭证+管理员密码) → `python -m app.main` → 开 http://localhost:8800。
- 容器：`docker compose up -d --build`。
- 生产：推 `main` → CI 构镜像推 Harbor → SSH 部署。内网部署细节见 `deploy/DEPLOY.md`。
- 首启自动把旧版单应用配置迁成「默认模型 + 默认应用」（`app/main.py:_migrate_legacy`），无损。
- 密钥不入库：`config.yaml`(gitignore) 或环境变量；运行时设置存 `data/agent.db`。
