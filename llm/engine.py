"""大模型编排（按应用）：对话历史 + 长期记忆 + 工具循环 + token 计量。

每条消息按 app 取：它绑定的 AI 供应商(模型) + 勾选的工具子集 + 人设。
记忆/用量按 app 隔离；飞书取数用该 app 的凭证（contextvar）。
每次调用都是一个 run（harness.runs），模型回复与工具调用逐条写进事件日志供审计。
工具调用一律走 harness.pipeline.run_tool()：记录 → 策略 → guard → 执行 → 后处理。
"""
import hashlib
import json
import os
import time

from openai import OpenAI

from core import store
from feishu import auth
from harness import pipeline, policy, runs, tasks, workflows
from . import authorization
from . import mcp as mcp_mod
from .tools import tools_for, execute, meta_for, approval_preview, explain_errors, MCP_META, SKILL_TOOL

_MAX_TOOL_LOOPS = 40


def _system(app, skills, chat_id="", context_key=""):
    prompt = app.get("system_prompt") or "你是一个企业数字员工助手。"
    notes = store.load_notes(app["id"], scope=f"chat:{chat_id}:{context_key}")
    if notes:
        prompt += "\n\n【长期记忆】\n" + "\n".join("- " + n for n in notes)
    if skills:
        lines = "\n".join(f"- {s['name']}：{s.get('description') or ''}" for s in skills)
        prompt += ("\n\n【可用技能】（需要详情时用 read_skill(name) 拉取全文）\n" + lines)
    return prompt


def _record(app_id, chat_id, model, usage):
    u = usage
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


def _consume_stream(stream, on_delta):
    """消费一次流式响应。把最终答案的 content 增量喂给 on_delta（reasoning 思考内容丢弃、不展示）。
    返回 (content, tool_calls, usage)。tool_calls 为 [{'id','name','args'}]，按 index 重组。"""
    parts = []
    tool_map = {}          # index -> {'id','name','args'}
    usage = None
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not getattr(chunk, "choices", None):
            continue
        d = chunk.choices[0].delta
        piece = getattr(d, "content", None)
        if piece:
            parts.append(piece)
            if on_delta:
                try:
                    on_delta(piece)
                except Exception:
                    pass
        # reasoning_content（思考）：不累积、不转发
        for tc in (getattr(d, "tool_calls", None) or []):
            slot = tool_map.setdefault(tc.index, {"id": None, "name": "", "args": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["args"] += tc.function.arguments
    tool_calls = [tool_map[i] for i in sorted(tool_map)] if tool_map else []
    return "".join(parts), tool_calls, usage


def _call(client, model, msgs, tools, extra_body, on_delta):
    """跑一次模型调用（默认流式）。流式失败则退回非流式一次性调用，保证有结果。
    返回 (content, tool_calls[{'id','name','args'}], usage)。"""
    kwargs = {"model": model, "messages": msgs, "extra_body": extra_body}
    if tools:
        kwargs["tools"] = tools
    try:
        stream = client.chat.completions.create(
            stream=True, stream_options={"include_usage": True}, **kwargs)
        return _consume_stream(stream, on_delta)
    except Exception:
        resp = client.chat.completions.create(**kwargs)   # 回退：非流式一次性
        m = resp.choices[0].message
        content = m.content or ""
        if content and on_delta:
            try:
                on_delta(content)
            except Exception:
                pass
        tcs = [{"id": tc.id, "name": tc.function.name, "args": tc.function.arguments or ""}
               for tc in (m.tool_calls or [])]
        return content, tcs, getattr(resp, "usage", None)


def _tool_label(name, args_json):
    """把一次工具调用压成一句给人看的短进度，如 'kubectl get pods -A' / '查日志 k8s_upmore_api'。"""
    try:
        a = json.loads(args_json or "{}")
    except Exception:
        a = {}
    if name == "kubectl":
        return "kubectl " + (a.get("command") or "")
    if name == "ssh_exec":
        return "ssh " + (a.get("command") or "")[:50]
    if name == "query_logs":
        return "查日志 " + (a.get("stream") or "") + (f"（{a.get('where')}）" if a.get("where") else "")
    if name == "read_skill":
        return "读技能 " + (a.get("name") or "") + (f" / {a['file']}" if a.get("file") else "")
    if name.startswith("code_"):
        verb = {"code_repos": "列仓库", "code_open": "打开仓库", "code_list": "列目录", "code_read": "读",
                "code_search": "搜索", "code_edit": "改", "code_write": "写", "code_delete": "删",
                "code_diff": "看 diff", "code_test": "跑测试", "code_validate": "校验清单", "code_propose_mr": "提 MR",
                "code_close": "关闭工作区"}.get(name, name)
        obj = a.get("repo") or a.get("path") or a.get("pattern") or a.get("title") or ""
        return f"{verb} {obj}".strip()
    if name in ("server_stats", "docker_containers", "service_status", "list_log_streams",
                "list_ssh_hosts", "list_k8s_clusters"):
        return {"server_stats": "查服务器负载", "docker_containers": "查容器",
                "service_status": "查服务状态", "list_log_streams": "列日志流",
                "list_ssh_hosts": "列受管主机", "list_k8s_clusters": "列受管集群"}[name]
    return name


def _usage_dict(u):
    if not u:
        return None
    return {"prompt": getattr(u, "prompt_tokens", 0) or 0,
            "completion": getattr(u, "completion_tokens", 0) or 0,
            "total": getattr(u, "total_tokens", 0) or 0}


def _note_call(rec, content, tool_calls, usage, **extra):
    """一次模型调用落事件 + 累计 run 的 token。"""
    rec.add_tokens(getattr(usage, "total_tokens", 0) if usage else 0)
    rec.emit("assistant/message", {"content": content or "",
                                   "tool_calls": [{"id": t["id"], "name": t["name"], "args": t["args"]}
                                                  for t in tool_calls],
                                   "usage": _usage_dict(usage), **extra})


_TOOL_RESULT_LIMIT = int(os.getenv("HARNESS_TOOL_RESULT_CHARS", "6000"))


def _fit_tool_result(content_str):
    """超大工具结果截断后再进模型上下文；完整结果仍留在 evidence 供验收与续跑。
    JSON 结果截掉尾部并标注原大小，防止一次 get events / describe 把预算吃光。"""
    if len(content_str) <= _TOOL_RESULT_LIMIT:
        return content_str
    head = content_str[:_TOOL_RESULT_LIMIT]
    cut = head.rfind("\n")
    if cut > _TOOL_RESULT_LIMIT // 2:
        head = head[:cut]
    return (f"{head}\n…[结果共 {len(content_str)} 字符，已截断；如需其余部分，"
            f"换更精确的查询参数缩小范围后重新调用]")


def _untrusted_input(task):
    """任务指令本身含外部内容（巡检扫描结果、webhook 请求体）时，从第一步就按污染处理。"""
    return task.get("source") == "patrol" or task.get("trigger") == "webhook"


def run(app: dict, chat_id: str, user_text: str, speaker: str = "",
        speaker_open_id: str = "", proactive: bool = False, on_step=None, on_delta=None,
        trigger: str = None):
    """app 为 store.get_app() 返回的字典（含 tools 列表、ai_provider_id 等）。
    speaker/speaker_open_id：群里这条消息的发言人姓名与 open_id（用于身份识别与 @）。
    proactive=True：主动模式，模型可回 __SILENT__ 选择不说话，此时返回 None（不发消息）。
    trigger：写进 runs.trigger 的触发方式；不传则按 proactive 取 chat / proactive。"""
    trigger = trigger or ("proactive" if proactive else "chat")
    rec = runs.start(app["id"], chat_id, trigger, speaker_open_id)
    status, reason = "error", ""
    try:
        answer, status = _run(rec, app, chat_id, user_text, speaker, speaker_open_id,
                              proactive, on_step, on_delta)
        if tasks.current() and status == "error":
            raise RuntimeError(answer)
        return answer
    except tasks.TaskSuspended as e:
        status, reason = e.status, str(e)
        raise
    except tasks.TaskCancelled as e:
        status, reason = "cancelled", str(e)
        raise
    except (tasks.TaskUncertain, tasks.TaskIncomplete) as e:
        status, reason = "needs_attention", str(e)
        raise
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        raise
    finally:
        runs.end(rec, status, reason)


def _run(rec, app, chat_id, user_text, speaker, speaker_open_id, proactive, on_step, on_delta):
    """run() 的本体。返回 (answer, status)，status 见 harness.runs.STATUSES。"""
    aid = app["id"]
    prov = store.get_provider(app["ai_provider_id"]) if app.get("ai_provider_id") else None
    if not prov or not prov.get("api_key"):
        return ("（这个应用还没绑定可用的 AI —— 请到后台「应用」里给它绑定一个已配好 API Key 的 AI 供应商。）",
                "error")

    context_key = authorization.context_key(app)
    state = tasks.load_checkpoint()
    if state and state.get("context_key") != context_key:
        raise tasks.TaskIncomplete("授权、技能或模型配置已变化，旧上下文已停止使用；请核对原任务后创建新任务。")

    client = OpenAI(base_url=prov.get("base_url") or "https://api.openai.com/v1", api_key=prov["api_key"],
                    timeout=60, max_retries=1)
    model = prov.get("model") or "gpt-4o-mini"
    skills = store.skills_for(app.get("skills"))
    tools_schema = tools_for(app.get("tools"))
    hidden = workflows.hidden_tools((tasks.current() or {}).get("workflow", "auto"), user_text)
    tools_schema = [t for t in tools_schema if t["function"]["name"] not in hidden]
    if skills:                                   # 有技能才挂 read_skill（保持隔离）
        tools_schema = tools_schema + [SKILL_TOOL]
    mcp_schema, mcp_routes = mcp_mod.tools_for_app(app)  # 应用勾选的 MCP server 暴露的工具
    tools_schema = tools_schema + mcp_schema
    exposed = {t["function"]["name"] for t in tools_schema}   # 模型说出清单外的工具名会被 guard 拦下
    turns = int(app.get("history_turns") or 12)

    system = _system(app, skills, chat_id, context_key)
    task = tasks.current() or {}
    system += workflows.prompt(task.get("workflow", "auto"), user_text)
    if speaker:
        system += _group_brief(speaker, speaker_open_id)
    if proactive:
        system += _proactive_brief()
    # 用户发言带上「姓名」前缀，群里多人发言才分得清谁是谁（也会随历史留痕）
    turn = f"「{speaker}」：{user_text}" if speaker else user_text

    if not state:
        msgs = [{"role": "system", "content": system}] + store.load_history(aid, chat_id, turns, context_key=context_key)
        msgs.append({"role": "user", "content": turn})
        state = {"version": 1, "context_key": context_key, "messages": msgs, "step": 0, "tokens": 0,
                 "tainted": _untrusted_input(task),
                 "pending": [], "next_tool": 0, "evidence": [], "started": time.time()}
        tasks.checkpoint(state)
    msgs = state["messages"]
    if _untrusted_input(task):
        state["tainted"] = True
    rec.tainted = bool(state.get("tainted"))
    store.save(aid, chat_id, "user", turn, task_id=task.get("id"), context_key=context_key)
    rec.emit("run/start", {"provider": prov.get("name"), "model": model,
                           "tools": [t["function"]["name"] for t in tools_schema],
                           "skills": [s["name"] for s in skills],
                           "history_msgs": len(msgs) - 2, "proactive": bool(proactive),
                           "policy": policy.normalize(app.get("policy")),
                           "system_sha": hashlib.sha256(system.encode()).hexdigest()[:12]})
    rec.emit("user/message", {"content": turn})

    ctx = {"app_id": aid, "chat_id": chat_id, "speaker_open_id": speaker_open_id, "context_key": context_key,
           "skills": {s["name"]: {"content": s.get("content") or "", "files": s.get("files") or {}}
                      for s in skills}}
    ctok = auth.use_app(app.get("feishu_app_id"), app.get("feishu_app_secret"))  # 文档工具用本应用凭证
    thinking_on = bool(app.get("thinking", True))
    extra_body = {"thinking": {"type": "enabled" if thinking_on else "disabled"}}  # 豆包思考开关（默认开）
    deliver = on_delta if not proactive else None        # 主动模式不流式（不显卡片）
    try:
        while state["step"] < _MAX_TOOL_LOOPS or state.get("pending") or state.get("done"):
            tasks.check_active()
            if authorization.context_key(store.get_app(aid) or {}) != context_key:
                raise tasks.TaskIncomplete("任务期间授权、技能或模型配置已变化，请核对原任务后创建新任务。")
            if state.get("done"):
                answer = state.get("final_answer") or "(无输出)"
                if proactive and (answer == "(无输出)" or "__SILENT__" in answer):
                    return None, "silent"
                reason = workflows.verify(task.get("workflow", "auto"), user_text, state.get("evidence", []))
                if task and reason and state.get("verify_retries", 0) < 2:
                    # 先让模型补证据（最多两次），仍不满足再判定未完成；提示写进检查点，重启后不重复
                    state["verify_retries"] = state.get("verify_retries", 0) + 1
                    msgs.append({"role": "assistant", "content": answer})
                    msgs.append({"role": "user", "content": f"【宿主验收未通过】{reason}。请补充所需的工具调用后，再给出最终答复。"})
                    state.update(done=False, final_answer=None)
                    tasks.checkpoint(state)
                    continue
                if task and reason:
                    raise tasks.TaskIncomplete(f"验收待完成：{reason}")
                store.save(aid, chat_id, "assistant", answer, task_id=task.get("id"), context_key=context_key)
                return answer, "completed"
            if (state.get("tokens", 0) >= int(os.getenv("HARNESS_MAX_TOKENS", "120000"))
                    and not state.get("pending")):
                # 预算耗尽不再直接判「待核对」：证据已经够时，不带工具再调一次模型，用现有证据交付报告。
                # （pending 批次先在上面的分支里执行完，下一轮才进这里。）
                try:
                    content, _tc, usage = _call(client, model, msgs + [
                        {"role": "user",
                         "content": "（已达到 token 预算，禁止再调用任何工具）请基于以上已查到的证据直接产出最终答复："
                                    "写清结论、执行过的操作与证据、未完成项；没有采集到的数据不要编造。"}],
                        None, extra_body, deliver)
                    _record(aid, chat_id, model, usage)
                    _note_call(rec, content, [], usage, fallback=True)
                    answer = (content or "").strip()
                except Exception:
                    answer = ""
                if answer:
                    reason = workflows.verify(task.get("workflow", "auto"), user_text, state.get("evidence", []))
                    if not (task and reason):
                        state.update(done=True, final_answer=answer)
                        tasks.checkpoint(state)
                        if proactive and (answer == "(无输出)" or "__SILENT__" in answer):
                            return None, "silent"
                        store.save(aid, chat_id, "assistant", answer, task_id=task.get("id"), context_key=context_key)
                        return answer, "completed"
                raise tasks.TaskIncomplete("任务已达到 token 预算，请查看现有证据后调整任务范围。")
            if not state.get("pending"):
                content, tool_calls, usage = _call(client, model, msgs, tools_schema, extra_body, deliver)
                tasks.check_active()
                _record(aid, chat_id, model, usage)
                _note_call(rec, content, tool_calls, usage)
                state["tokens"] += (getattr(usage, "total_tokens", 0) or 0) if usage else 0
                state["step"] += 1
                if not tool_calls:
                    state.update(done=True, final_answer=(content or "").strip())
                    tasks.checkpoint(state)
                    continue
                for i, call in enumerate(tool_calls):
                    call["id"] = call["id"] or f"step{state['step']}-call{i}"
                msgs.append({"role": "assistant", "content": content or "",
                             "tool_calls": [{"id": t["id"], "type": "function",
                                "function": {"name": t["name"], "arguments": t["args"]}} for t in tool_calls]})
                state.update(pending=tool_calls, next_tool=0)
                tasks.checkpoint(state)  # 模型动作列表先持久化，再执行；恢复不重新生成已批准的动作。
            tool_calls = state["pending"]
            if on_step:                                  # 进度回调：让 bot 把「这轮在跑什么」刷到卡片上
                try:
                    on_step([_tool_label(t["name"], t["args"]) for t in tool_calls])
                except Exception:
                    pass
            for index in range(state.get("next_tool", 0), len(tool_calls)):
                t = tool_calls[index]
                name = t["name"]
                raw = None
                try:
                    args = json.loads(t["args"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("参数不是对象")
                except Exception:
                    args, raw = {}, t["args"]
                if name in mcp_routes:                  # MCP 工具走 MCP 客户端
                    meta = dict(MCP_META)
                    executor = (lambda a, _r=mcp_routes[name]: mcp_mod.call(_r, a))
                else:
                    meta = meta_for(name, args)
                    executor = (lambda a, _n=name: explain_errors(_n, execute(_n, a, ctx)))
                call = pipeline.ToolCall(id=t["id"], tool=name, args=args, raw_args=raw,
                                         exposed=name in exposed, trigger=rec.trigger, app_id=aid, **meta)
                # 需要审批时才算的卡片预览（如提 MR 的 diff），让审批人看到真正要批的内容
                call.extra["action_key"] = f"{state['step']}:{t['id']}"
                call.extra["prepare"] = lambda a, _n=name: authorization.prepare(_n, a, ctx)
                call.extra["validate"] = lambda a, _n=name: authorization.validate(_n, a, ctx)
                call.extra["sandboxed"] = name in ("code_test", "sandbox_run") or name.startswith("box_")
                call.extra["preview"] = (lambda a, _n=name: approval_preview(_n, a, ctx))
                content_str = pipeline.run_tool(rec, call, app.get("policy"), executor, app=app)
                msgs.append({"role": "tool", "tool_call_id": t["id"],
                             "content": _fit_tool_result(content_str)})
                state["evidence"].append({"tool": name, "args": call.args, "result": json.loads(content_str)})
                state.update(next_tool=index + 1, tainted=bool(rec.tainted),
                             progress=f"已执行 {name}")
                tasks.checkpoint(state)
            state.update(pending=[], next_tool=0)
            tasks.checkpoint(state)
    finally:
        auth.reset_ctx(ctok)

    if proactive:
        return None, "max-steps"                     # 主动模式下查不动就别硬插话
    if task:
        raise tasks.TaskIncomplete("任务达到工具轮数上限，已保存检查点与证据，请缩小范围后继续。")
    # 轮数用尽：不直接放弃，强制「不带工具」再问一次（同样流式），用已查到的信息给出当前最佳结论（优雅降级）
    try:
        content, _tc, usage = _call(client, model, msgs + [
            {"role": "user",
             "content": "（已达工具调用上限，别再调用任何工具）请基于以上已经查到的信息，直接给出你目前"
                        "能得出的结论；若还不完整，就说清已确认了什么、还差什么，给用户一个有用的阶段性答复。"}],
            None, extra_body, deliver)
        _record(aid, chat_id, model, usage)
        _note_call(rec, content, [], usage, fallback=True)
        answer = (content or "").strip() or "（暂未得出明确结论，请把问题说得更具体点再试）"
    except Exception:
        answer = "（这次排查比较复杂、暂未收敛；把问题缩小一点范围再试试）"
    store.save(aid, chat_id, "assistant", answer, context_key=context_key)
    return answer, "max-steps"
