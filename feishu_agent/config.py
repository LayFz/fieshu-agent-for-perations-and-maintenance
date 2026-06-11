"""配置加载：config.yaml < 环境变量（密钥）。"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self):
        p = ROOT / "config.yaml"
        if not p.exists():
            raise SystemExit("缺 config.yaml：先 `cp config.example.yaml config.yaml` 并填值")
        c = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        c.setdefault("feishu", {})
        c.setdefault("llm", {})
        c.setdefault("agent", {})
        # 环境变量覆盖密钥（优先级最高，便于生产注入、不落盘）
        if os.getenv("FEISHU_APP_SECRET"):
            c["feishu"]["app_secret"] = os.environ["FEISHU_APP_SECRET"]
        if os.getenv("LLM_API_KEY"):
            c["llm"]["api_key"] = os.environ["LLM_API_KEY"]
        self.feishu = c["feishu"]
        self.llm = c["llm"]
        self.agent = c["agent"]

    def require(self):
        miss = []
        if not self.feishu.get("app_id"):
            miss.append("feishu.app_id")
        if not self.feishu.get("app_secret"):
            miss.append("feishu.app_secret(或 FEISHU_APP_SECRET)")
        if not self.llm.get("api_key"):
            miss.append("llm.api_key(或 LLM_API_KEY)")
        if miss:
            raise SystemExit("配置缺失：" + "、".join(miss))


cfg = Config()
