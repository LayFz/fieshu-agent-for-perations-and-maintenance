"""大模型编排（按应用）：对话历史 + 长期记忆 + 工具循环 + token 计量。

每条消息按 app 取：它绑定的 AI 供应商(模型) + 勾选的工具子集 + 人设。
记忆/用量按 app 隔离；飞书取数用该 app 的凭证（contextvar）。
"""
import json

from openai import OpenAI

from core import store
from feishu import auth
from . import mcp as mcp_mod
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


def _group_brief(speaker, speaker_open_id):
    """群聊身份感知：告诉模型当前是谁在说话、怎么 @ 人。"""
    who = f"「{speaker}」" + (f"(open_id={speaker_open_id})" if speaker_open_id else "")
    return (
        "\n\n【群聊身份】你在一个多人飞书群里工作，像团队里的运维工程师一样对话。"
        f"当前这条消息的发言人是 {who}。历史里每条用户发言都以「姓名」前缀标注是谁说的，"
        "请据此分辨不同的人、记住各自交代的事。\n"
        "需要 @ 某人时，在回复正文里直接写 `<at id=对方open_id></at>`，飞书会@到并通知他；"
        "@当前发言人用上面的 open_id 即可，@别人先用 list_chat_members 工具查群成员拿 open_id。"
        "只在确有必要（指派/确认/提醒某人）时 @，别逢人就 @。"
    )


def _proactive_brief():
    """主动模式：没人点名时的旁观判断 + 沉默出口。"""
    return (
        "\n\n【主动模式】你正在群里旁听，没人点名 @ 你。只有当你确实能帮上忙时才开口，例如："
        "有人报故障/报错/服务异常、有人提出你能回答或能查证的问题、或你发现该提醒某人跟进的事。"
        "其余闲聊一律保持沉默。\n"
        "判断此刻不该由你说话时，只回复 `__SILENT__`（不要任何其它字符）。\n"
        "决定开口时：先用工具查清楚再说（查容器/日志/负载等），话要短、直接给结论，"
        "该谁跟进就 @ 谁（<at id=open_id></at>）。宁可少说，绝不刷屏。"
    )


def run(app: dict, chat_id: str, user_text: str, speaker: str = "",
        speaker_open_id: str = "", proactive: bool = False):
    """app 为 store.get_app() 返回的字典（含 tools 列表、ai_provider_id 等）。
    speaker/speaker_open_id：群里这条消息的发言人姓名与 open_id（用于身份识别与 @）。
    proactive=True：主动模式，模型可回 __SILENT__ 选择不说话，此时返回 None（不发消息）。"""
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
    mcp_schema, mcp_routes = mcp_mod.tools_for_app(app)  # 应用勾选的 MCP server 暴露的工具
    tools_schema = tools_schema + mcp_schema
    turns = int(app.get("history_turns") or 12)

    system = _system(app, skills)
    if speaker:
        system += _group_brief(speaker, speaker_open_id)
    if proactive:
        system += _proactive_brief()
    # 用户发言带上「姓名」前缀，群里多人发言才分得清谁是谁（也会随历史留痕）
    turn = f"「{speaker}」：{user_text}" if speaker else user_text

    msgs = [{"role": "system", "content": system}]
    msgs += store.load_history(aid, chat_id, turns)
    msgs.append({"role": "user", "content": turn})
    store.save(aid, chat_id, "user", turn)

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
                answer = (m.content or "").strip()
                if proactive and (not answer or "__SILENT__" in answer):
                    return None                      # 主动模式判定不该说话：不发、不存 assistant
                answer = answer or "(无输出)"
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
                if tc.function.name in mcp_routes:      # MCP 工具走 MCP 客户端
                    result = mcp_mod.call(mcp_routes[tc.function.name], args)
                else:
                    result = execute(tc.function.name, args, ctx)
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})
    finally:
        auth.reset_ctx(ctok)

    if proactive:
        return None                                  # 主动模式下查不动就别硬插话
    answer = "（工具调用过多，已中止；请把任务拆细一点再试）"
    store.save(aid, chat_id, "assistant", answer)
    return answer
