"""引导配置：config.yaml < 环境变量。
只负责"冷启动引导"——飞书凭证 + 各项初始值 + 管理员初始密码。
LLM/提示词等运行时设置在管理后台修改并持久化到 data/agent.db，运行时以 DB 为准。"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "agent.db"


class Config:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        p = ROOT / "config.yaml"
        c = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}) or {}
        for k in ("feishu", "llm", "agent", "admin", "web"):
            c.setdefault(k, {})
        # 环境变量覆盖（优先级最高，便于生产/容器注入、不落盘）
        e = os.getenv
        if e("FEISHU_APP_ID"):       c["feishu"]["app_id"] = e("FEISHU_APP_ID")
        if e("FEISHU_APP_SECRET"):   c["feishu"]["app_secret"] = e("FEISHU_APP_SECRET")
        if e("LLM_BASE_URL"):        c["llm"]["base_url"] = e("LLM_BASE_URL")
        if e("LLM_MODEL"):           c["llm"]["model"] = e("LLM_MODEL")
        if e("LLM_API_KEY"):         c["llm"]["api_key"] = e("LLM_API_KEY")
        if e("ADMIN_USERNAME"):      c["admin"]["username"] = e("ADMIN_USERNAME")
        if e("ADMIN_PASSWORD"):      c["admin"]["password"] = e("ADMIN_PASSWORD")
        if e("WEB_SESSION_SECRET"):  c["web"]["session_secret"] = e("WEB_SESSION_SECRET")
        if e("WEB_PORT"):            c["web"]["port"] = int(e("WEB_PORT"))
        self.feishu = c["feishu"]
        self.llm = c["llm"]
        self.agent = c["agent"]
        self.admin = c["admin"]
        self.web = c["web"]

    def has_feishu(self):
        return bool(self.feishu.get("app_id") and self.feishu.get("app_secret"))


cfg = Config()
