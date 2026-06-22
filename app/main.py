"""入口：首启播种/迁移 → 启动各应用飞书长连接(后台线程) + 管理后台(uvicorn 主线程)。"""
import json

import uvicorn

from core import store
from core.config import cfg


def _migrate_legacy():
    """把旧版(单应用)的 config.yaml/env 配置迁成「一个默认 AI 供应商 + 一个默认应用」。
    只在还没有任何应用时执行一次；之后全部以后台管理为准。"""
    if store.apps_count() > 0:
        return

    # 1) 旧 LLM 配置 → 默认 AI 供应商
    provider_id = None
    base_url = store.get_setting("llm_base_url") or cfg.llm.get("base_url")
    model = store.get_setting("llm_model") or cfg.llm.get("model")
    api_key = store.get_setting("llm_api_key") or cfg.llm.get("api_key")
    if store.providers_count() == 0 and (api_key or base_url or model):
        provider_id = store.create_provider("默认模型", base_url or "", model or "", api_key or "")
        print("★ 已迁移旧 LLM 配置 → AI 供应商「默认模型」", flush=True)
    elif store.providers_count() > 0:
        provider_id = store.list_providers()[0]["id"]

    # 2) 旧飞书 + 提示词 → 默认应用（默认开放全部工具，可在后台收敛）
    fid = store.get_setting("feishu_app_id") or cfg.feishu.get("app_id") or ""
    fsec = store.get_setting("feishu_app_secret") or cfg.feishu.get("app_secret") or ""
    prompt = store.get_setting("system_prompt") or cfg.agent.get("system_prompt") or "你是一个企业数字员工助手。"
    turns = int(store.get_setting("history_turns") or cfg.agent.get("history_turns", 12) or 12)
    if fid or provider_id:
        from llm.tools import all_tools
        store.create_app("默认应用", fid, fsec, provider_id, prompt, turns,
                         [t["name"] for t in all_tools()], enabled=1)
        print("★ 已迁移旧配置 → 应用「默认应用」", flush=True)


def _bootstrap():
    # 旧版引导值先落 kv（迁移函数会从这里读），保持与历史行为兼容
    store.seed_setting("llm_base_url", cfg.llm.get("base_url"))
    store.seed_setting("llm_model", cfg.llm.get("model"))
    store.seed_setting("llm_api_key", cfg.llm.get("api_key"))
    store.seed_setting("system_prompt", cfg.agent.get("system_prompt"))
    store.seed_setting("history_turns", str(cfg.agent.get("history_turns", 12)))
    store.seed_setting("feishu_app_id", cfg.feishu.get("app_id"))
    store.seed_setting("feishu_app_secret", cfg.feishu.get("app_secret"))
    if cfg.web.get("session_secret"):
        store.set_setting("server_secret", cfg.web["session_secret"])

    _migrate_legacy()

    username = cfg.admin.get("username") or "admin"
    password = cfg.admin.get("password") or "admin12345"
    if store.ensure_admin(username, password):
        print(f"★ 已初始化管理员账号：{username} / {password}  —— 登录后请到「账号」里改掉初始密码")


def main():
    _bootstrap()

    from feishu import bot
    started = bot.start_all()
    if started == 0:
        print("⚠ 暂无可启动的应用（需在后台建应用、填飞书凭证、绑 AI、并启用）", flush=True)

    from sched import runner as scheduler
    scheduler.start()

    from web import admin
    port = int(cfg.web.get("port", 8800))
    print(f"★ 管理后台：http://localhost:{port}")
    uvicorn.run(admin.app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
