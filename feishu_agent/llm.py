"""大模型编排：对话历史 + 长期记忆 + 工具循环 + token 计量。
配置（base_url/model/api_key/提示词/历史轮数）实时取自 DB——管理后台一改即生效。"""
import json

from openai import OpenAI

from . import store
from .tools import TOOLS, execute

_MAX_TOOL_LOOPS = 6


def _settings():
    s = store.all_settings()
    return {
        "base_url": s.get("llm_base_url") or "https://api.openai.com/v1",
        "model": s.get("llm_model") or "gpt-4o-mini",
        "api_key": s.get("llm_api_key") or "",
        "system_prompt": s.get("system_prompt") or "你是飞书助手。",
        "history_turns": int(s.get("history_turns") or 12),
    }


def _system(prompt):
    notes = store.load_notes("global")
    if notes:
        prompt += "\n\n【长期记忆】\n" + "\n".join("- " + n for n in notes)
    return prompt


def _record(chat_id, model, resp):
    u = getattr(resp, "usage", None)
    if not u:
        return
    store.record_usage(chat_id, model,
                       getattr(u, "prompt_tokens", 0) or 0,
                       getattr(u, "completion_tokens", 0) or 0,
                       getattr(u, "total_tokens", 0) or 0)


def run(chat_id: str, user_text: str) -> str:
    st = _settings()
    if not st["api_key"]:
        return "（还没配置大模型 API Key —— 请先到管理后台「大模型配置」里填好再用我。）"
    client = OpenAI(base_url=st["base_url"], api_key=st["api_key"])

    msgs = [{"role": "system", "content": _system(st["system_prompt"])}]
    msgs += store.load_history(chat_id, st["history_turns"])
    msgs.append({"role": "user", "content": user_text})
    store.save(chat_id, "user", user_text)

    for _ in range(_MAX_TOOL_LOOPS):
        resp = client.chat.completions.create(model=st["model"], messages=msgs, tools=TOOLS)
        _record(chat_id, st["model"], resp)
        m = resp.choices[0].message
        if not m.tool_calls:
            answer = (m.content or "").strip() or "(无输出)"
            store.save(chat_id, "assistant", answer)
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
    store.save(chat_id, "assistant", answer)
    return answer
