"""大模型编排（按应用）：对话历史 + 长期记忆 + 工具循环 + token 计量。

每条消息按 app 取：它绑定的 AI 供应商(模型) + 勾选的工具子集 + 人设。
记忆/用量按 app 隔离；飞书取数用该 app 的凭证（contextvar）。
"""
import json

from openai import OpenAI

from core import store
from feishu import auth
from .tools import tools_for, execute, SKILL_TOOL

_MAX_TOOL_LOOPS = 6


def _system(app, skills):
    prompt = app.get("system_prompt") or "你是一个企业数字员工助手。"
    notes = store.load_notes(app["id"])
    if notes:
        prompt += "\n\n【长期记忆】\n" + "\n".join("- " + n for n in notes)
    if skills:
        lines = "\n".join(f"- {s['name']}：{s.get('description') or ''}" for s in skills)
        prompt += ("\n\n【可用技能】（需要详情时用 read_skill(name) 拉取全文）\n" + lines)
    return prompt


def _record(app_id, chat_id, model, resp):
    u = getattr(resp, "usage", None)
    if not u:
        return
    store.record_usage(app_id, chat_id, model,
                       getattr(u, "prompt_tokens", 0) or 0,
                       getattr(u, "completion_tokens", 0) or 0,
                       getattr(u, "total_tokens", 0) or 0)


def run(app: dict, chat_id: str, user_text: str) -> str:
    """app 为 store.get_app() 返回的字典（含 tools 列表、ai_provider_id 等）。"""
    aid = app["id"]
    prov = store.get_provider(app["ai_provider_id"]) if app.get("ai_provider_id") else None
    if not prov or not prov.get("api_key"):
        return "（这个应用还没绑定可用的 AI —— 请到后台「应用」里给它绑定一个已配好 API Key 的 AI 供应商。）"

    client = OpenAI(base_url=prov.get("base_url") or "https://api.openai.com/v1", api_key=prov["api_key"])
    model = prov.get("model") or "gpt-4o-mini"
    skills = store.skills_for(app.get("skills"))
    tools_schema = tools_for(app.get("tools"))
    if skills:                                   # 有技能才挂 read_skill（保持隔离）
        tools_schema = tools_schema + [SKILL_TOOL]
    turns = int(app.get("history_turns") or 12)

    msgs = [{"role": "system", "content": _system(app, skills)}]
    msgs += store.load_history(aid, chat_id, turns)
    msgs.append({"role": "user", "content": user_text})
    store.save(aid, chat_id, "user", user_text)

    ctx = {"app_id": aid, "chat_id": chat_id,
           "skills": {s["name"]: s.get("content") or "" for s in skills}}
    ctok = auth.use_app(app.get("feishu_app_id"), app.get("feishu_app_secret"))  # 文档工具用本应用凭证
    try:
        for _ in range(_MAX_TOOL_LOOPS):
            kwargs = {"model": model, "messages": msgs}
            if tools_schema:
                kwargs["tools"] = tools_schema
            resp = client.chat.completions.create(**kwargs)
            _record(aid, chat_id, model, resp)
            m = resp.choices[0].message
            if not m.tool_calls:
                answer = (m.content or "").strip() or "(无输出)"
                store.save(aid, chat_id, "assistant", answer)
                return answer
            msgs.append({
                "role": "assistant",
                "content": m.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in m.tool_calls],
            })
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = execute(tc.function.name, args, ctx)
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})
    finally:
        auth.reset_ctx(ctok)

    answer = "（工具调用过多，已中止；请把任务拆细一点再试）"
    store.save(aid, chat_id, "assistant", answer)
    return answer
