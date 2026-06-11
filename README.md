# feishu-agent

公司内部的飞书智能助手。**飞书群里 @机器人说话 → 大模型 + 工具读写飞书文档/知识库 → 回群**，
带**对话记忆**、**长期记忆**，并提供一个**管理后台**（登录后配置模型、看记忆、看工具、看 token 用量）。

- **只对内、不用公网**：飞书用**长连接**（机器人主动拨出），服务只出站、不开放入站端口；管理后台端口只在内网开。
- **可换任意模型**：任何 OpenAI 兼容接口（DeepSeek / 通义 / OpenAI / 自建网关…），后台改 3 项即生效，不动代码。
- **密钥不入库**：`config.yaml`（gitignore）或环境变量；运行时设置存 `data/agent.db`。
- **一条命令 Docker 起**。

## 架构

```
飞书群 ──长连接(出站)──▶  bot(后台线程)  ──▶  llm 编排(对话历史+长期记忆+工具循环)
                                                   │  每次调用记 token 用量
                                                   ▼
                                         tools ──▶ 飞书 docx/wiki REST（读/写/建文档）
管理员浏览器 ──:8800──▶  admin(FastAPI, 主线程)  ──▶  同一个 SQLite(data/agent.db)
                          登录 / 配模型 / 看记忆 / 看工具 / 看用量
```

bot 与 admin **同进程**：后台改了模型配置，bot 下一条消息就用新配置（都读同一个 DB）。

## 目录

```
feishu-agent/                仓库根（领域分包，互不搅）
├ app/      入口与装配
│  └ main.py        播种(设置+管理员) → 起 bot 线程 + uvicorn(admin)
├ core/     共享基座（谁都依赖）
│  ├ config.py      引导配置：config.yaml < 环境变量
│  └ store.py       数据层：设置/管理员/记忆/笔记/token 用量（单个 SQLite）
├ feishu/   飞书域
│  ├ bot.py         长连接：收群消息 → llm → 回群
│  ├ auth.py        应用身份 tenant_access_token（自动续，不绑个人）
│  ├ docs.py        飞书 docx/wiki 高层操作
│  └ md2feishu.py   Markdown → 飞书块（含原生表格）
├ llm/      大模型域
│  ├ engine.py      编排：历史 + 记忆 + 工具循环 + token 计量（配置实时取自 DB）
│  └ tools.py       工具定义(function calling) + 执行器
├ web/      管理后台域
│  ├ admin.py       FastAPI（登录 + 配置 + 记忆 + 工具 + 用量）
│  └ static/index.html   前端单页（无需构建）
└ Dockerfile / docker-compose.yml / config.example.yaml / requirements.txt
data/        运行后生成：agent.db（gitignore）

依赖单向、好扩展：core ← feishu ← llm ← web ← app（加新能力就平级再开个包，如 scheduler/）
```

## 本地跑

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml      # 填飞书凭证；模型可留空，登录后台再配
python -m app.main
```
首启会打印初始管理员账号（默认 `admin / admin12345`），浏览器开 **http://localhost:8800** 登录。

## Docker 跑

```bash
docker compose up -d --build
docker compose logs -f                  # 看首启打印的管理员账号
```
访问 **http://<内网IP>:8800**。数据持久化在 `./data`（删容器不丢记忆/配置）。
生产建议：在 compose 里用环境变量设强 `ADMIN_PASSWORD`，并固定 `WEB_SESSION_SECRET`（重启不掉登录态）。

## 管理后台能干嘛

| 页面 | 作用 |
|---|---|
| 概览 | 机器人在线/飞书/模型状态；累计消息、记忆、会话、token |
| 大模型配置 | 改 base_url / model / API Key / 提示词 / 历史轮数，**保存即生效** |
| 记忆系统 | 看长期记忆（可删）、按会话看对话历史 |
| 工具 | 当前机器人可调用的工具清单 |
| Token 用量 | 总量、按天、按模型、最近调用明细 |
| 账号 | 改管理员密码 |

## 飞书侧前提（一次性）

1. 自建应用启用**机器人**，把机器人**加进目标群**。
2. 事件订阅选**长连接**方式，订阅 `im.message.receive_v1`。
3. 开通权限：`im:message`、`im:message:send_as_bot`、`wiki:wiki`、`docx:document`（要写文档再加 `drive:drive`）。
4. 把**群**作为成员加进目标知识库（文档权限收敛到群，群里有机器人即可操作）。
5. 改完权限/事件要**重新发版**才生效。

> ⚠️ 关键：事件订阅必须走**长连接**（不是 webhook），这是内网免公网方案成立的根。
