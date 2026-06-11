"""大模型编排：对话历史 + 长期记忆 + 工具循环。任意 OpenAI 兼容模型。"""
import json

from openai import OpenAI

from . import memory
from .config import cfg
from .tools import TOOLS, execute

_client = OpenAI(base_url=cfg.llm["base_url"], api_key=cfg.llm["api_key"])
_MAX_TOOL_LOOPS = 6


def _system():
    sys = cfg.agent.get("system_prompt", "你是飞书助手。")
    notes = memory.load_notes("global")
    if notes:
        sys += "\n\n【长期记忆】\n" + "\n".join("- " + n for n in notes)
    return sys


def run(chat_id: str, user_text: str) -> str:
    msgs = [{"role": "system", "content": _system()}]
    msgs += memory.load_history(chat_id, cfg.agent.get("history_turns", 12))
    msgs.append({"role": "user", "content": user_text})
    memory.save(chat_id, "user", user_text)

    for _ in range(_MAX_TOOL_LOOPS):
        resp = _client.chat.completions.create(model=cfg.llm["model"], messages=msgs, tools=TOOLS)
        m = resp.choices[0].message
        if not m.tool_calls:
            answer = (m.content or "").strip() or "(无输出)"
            memory.save(chat_id, "assistant", answer)
            return answer
        # 把带 tool_calls 的 assistant 轮接回去
        msgs.append({
            "role": "assistant",
            "content": m.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in m.tool_calls],
        })
        # 逐个执行工具，结果接回去
        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            result = execute(tc.function.name, args)
            msgs.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(result, ensure_ascii=False)})

    answer = "（工具调用过多，已中止；请把任务拆细一点再试）"
    memory.save(chat_id, "assistant", answer)
    return answer
