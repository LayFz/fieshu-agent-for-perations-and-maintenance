"""入口：首启播种(设置 + 管理员) → 启动飞书 bot(后台线程) + 管理后台(uvicorn 主线程)。"""
import uvicorn

from core import store
from core.config import cfg


def _bootstrap():
    # 首启把 config.yaml/env 的初始值播种进 DB（之后以管理后台为准）
    store.seed_setting("llm_base_url", cfg.llm.get("base_url"))
    store.seed_setting("llm_model", cfg.llm.get("model"))
    store.seed_setting("llm_api_key", cfg.llm.get("api_key"))
    store.seed_setting("system_prompt", cfg.agent.get("system_prompt"))
    store.seed_setting("history_turns", str(cfg.agent.get("history_turns", 12)))
    store.seed_setting("feishu_app_id", cfg.feishu.get("app_id"))
    store.seed_setting("feishu_app_secret", cfg.feishu.get("app_secret"))
    if cfg.web.get("session_secret"):
        store.set_setting("server_secret", cfg.web["session_secret"])

    # 初始化管理员（仅首启建号；密码来自 config.admin.password / 环境变量 ADMIN_PASSWORD）
    username = cfg.admin.get("username") or "admin"
    password = cfg.admin.get("password") or "admin12345"
    if store.ensure_admin(username, password):
        print(f"★ 已初始化管理员账号：{username} / {password}  —— 登录后请到「账号」里改掉初始密码")


def main():
    _bootstrap()
    from feishu import bot
    if store.has_feishu():
        bot.start_in_background()
    else:
        print("⚠ 未配置飞书 app_id/secret，bot 未启动（管理后台仍可用，可先配模型）")

    from web import admin
    port = int(cfg.web.get("port", 8800))
    print(f"★ 管理后台：http://localhost:{port}")
    uvicorn.run(admin.app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
