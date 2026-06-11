# feishu-agent

内网可跑的**飞书 AI 助手**:群里 @机器人下任务 → 大模型理解 → 调工具**读写飞书云文档/知识库** → 回群。带对话记忆。

- **收/回消息**:飞书官方 SDK 长连接(WebSocket),**只需出站联网,不用公网 IP/域名/回调**。
- **大模型**:OpenAI 兼容接口,改 `config.yaml` 三行即可换任意模型(DeepSeek / 通义 / Kimi / OpenAI…)。
- **文档能力**:`tools` 里定义,大模型用 function-calling 调用,执行体走飞书 docx/wiki REST(Markdown→飞书块,支持原生表格)。
- **鉴权**:用**应用身份 `tenant_access_token`**(app_id+secret 自动续,**永不过期、不绑个人**)。
- **记忆**:SQLite 存每个群的对话历史 + 长期笔记。

## 架构

```
群里 @机器人 ──长连接──▶ main.on_message
    └─▶ llm.run(对话历史 + 记忆 + 工具清单 → 大模型)
            └─▶ 大模型决定调工具 → tools.execute → feishu_docs(读/写/建节点)
            └─▶ 大模型给最终话术 ──▶ 回群
```

## 跑起来

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml      # 填 app_secret + 模型 base_url/model/api_key
python -m feishu_agent.main             # 常驻;之后群里 @机器人 试
```
密钥也可走环境变量:`FEISHU_APP_SECRET`、`LLM_API_KEY`(覆盖 config.yaml)。

## 前提(飞书侧,已发布版本后)

1. 应用开启「机器人」能力,**把机器人拉进目标群**。
2. 订阅事件 `im.message.receive_v1`,选「**使用长连接接收事件**」。
3. 申请权限:`im:message`、`im:message.group_at_msg:readonly`、`im:message:send_as_bot`,以及文档权限 `wiki:wiki`、`docx:document`、`docs:document.media:upload`。
4. 让应用能写知识库:把「**含机器人的群**」加为知识库成员/管理员(`member_type=openchat`),或用 API 直接加应用。
5. 创建版本并**发布**(敏感权限需管理员审批)。

## 现状(v1)

- ✅ 收群消息(text)→ 大模型 → 工具读写文档 → 回群;对话历史 + 长期笔记。
- 🔜 待加:定时任务、配置后台网页、更多消息类型(富文本/卡片)、更细的记忆(向量检索)。
- ⚠️ `@机器人` 占位剔除做了简化(按 mentions 精确剔除可后续优化)。

详见各模块注释。
