"""MCP 客户端（同步）：给数字员工挂远程 MCP server，把它暴露的工具接进工具循环。

为什么手写而不是用官方 mcp SDK：官方 SDK 是 asyncio 的，本仓库是刻意的「同步 + 多线程」
架构（engine 工具循环、obs 都走同步 requests），异步 SDK 塞进来会撞上 CLAUDE.md 坑#2
（多线程里共用全局事件循环）。这里用同步 requests 手写 Streamable HTTP 传输，零新依赖、
和现有风格一致。

只支持【远程 HTTP】MCP server（Streamable HTTP，兼容旧版 SSE 响应）：
  POST 一个 JSON-RPC 到 server 的 endpoint URL，响应体是 application/json 或 text/event-stream。
握手：initialize -> notifications/initialized -> tools/list / tools/call。

隔离沿用平台范式：全局 mcp_servers 注册表（store），应用按 server 整体勾选（apps.mcp）。
应用运行时只看得到它勾选的 server 暴露的工具。
"""
import json
import re
import sys
import time

import requests

from core import store

_PROTO = "2025-06-18"                 # 我方声明的 MCP 协议版本（server 会协商回它支持的）
_TIMEOUT = (10, 60)                    # (连接, 读取) 秒
_CACHE_TTL = 300                       # 工具清单缓存秒数：避免每条消息都去 server 拉一次 tools/list
_clock = time.time

_CACHE = {}                            # server_id -> (ts, tools[])  原始 MCP 工具定义
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class MCPError(Exception):
    pass


# ---------- 传输：一条到某个 server 的连接（含握手后的 session 与协议版本） ----------
class _Conn:
    def __init__(self, url, headers):
        if not url:
            raise MCPError("未配置 MCP server URL")
        self.url = url
        self.extra = headers or {}          # 用户配的鉴权头（如 Authorization: Bearer xxx）
        self.http = requests.Session()
        self.sid = None                     # Mcp-Session-Id（server 若返回则后续带上）
        self.proto = _PROTO
        self._id = 0

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": self.proto}
        h.update(self.extra)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return h

    @staticmethod
    def _read(r):
        """从响应里取出 JSON-RPC 响应对象。兼容 application/json 与 text/event-stream。"""
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ct:
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    return obj          # 拿到我们要的响应即返回，不把 SSE 流读干（可能长挂）
            return None
        try:
            return r.json()
        except Exception:
            return None

    def request(self, method, params=None):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        r = self.http.post(self.url, json=payload, headers=self._headers(),
                           timeout=_TIMEOUT, stream=True)
        try:
            sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
            if sid:
                self.sid = sid
            if r.status_code >= 400:
                raise MCPError(f"HTTP {r.status_code}: {(r.text or '')[:300]}")
            msg = self._read(r)
        finally:
            r.close()
        if not isinstance(msg, dict):
            raise MCPError("响应为空或无法解析")
        if msg.get("error"):
            e = msg["error"]
            raise MCPError(f"{e.get('code')}: {e.get('message')}")
        return msg.get("result") or {}

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self.http.post(self.url, json=payload, headers=self._headers(), timeout=_TIMEOUT)
        except Exception:
            pass                              # 通知无响应，失败也不致命

    def initialize(self):
        res = self.request("initialize", {
            "protocolVersion": _PROTO, "capabilities": {},
            "clientInfo": {"name": "feishu-agent", "version": "1.0"}})
        pv = res.get("protocolVersion")
        if pv:
            self.proto = pv                   # 用 server 协商回的版本发后续请求
        self.notify("notifications/initialized")
        return res

    def close(self):
        try:
            if self.sid:
                self.http.delete(self.url, headers=self._headers(), timeout=5)
        except Exception:
            pass
        try:
            self.http.close()
        except Exception:
            pass


def _headers_of(server):
    h = server.get("headers")
    return h if isinstance(h, dict) else {}


def _connect(server):
    c = _Conn((server.get("url") or "").strip(), _headers_of(server))
    c.initialize()
    return c


# ---------- 对外：拉工具 / 调工具 ----------
def fetch_tools(server):
    """连 server 拉它暴露的工具清单（原始 MCP 定义）。失败抛 MCPError。给后台「测试连接」用。"""
    c = _connect(server)
    try:
        return c.request("tools/list", {}).get("tools", []) or []
    finally:
        c.close()


def call_tool(server, tool_name, arguments):
    """调用 server 的一个工具，返回给大模型看的结果。"""
    c = _connect(server)
    try:
        res = c.request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        return _flatten(res)
    finally:
        c.close()


def _flatten(res):
    """MCP tools/call 结果 -> 精简给模型。content 块拼成文本，保留 isError / 结构化输出。"""
    parts = []
    for b in res.get("content") or []:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "resource":
            r = b.get("resource") or {}
            parts.append(r.get("text") or r.get("uri") or json.dumps(r, ensure_ascii=False))
        else:
            parts.append(json.dumps(b, ensure_ascii=False))
    out = {"text": "\n".join(p for p in parts if p)}
    if res.get("isError"):
        out["isError"] = True
    if res.get("structuredContent") is not None:
        out["structured"] = res["structuredContent"]
    return out


# ---------- 工具清单缓存 ----------
def _cached_tools(server):
    sid = server["id"]
    now = _clock()
    ent = _CACHE.get(sid)
    if ent and now - ent[0] < _CACHE_TTL:
        return ent[1]
    tools = fetch_tools(server)
    _CACHE[sid] = (now, tools)
    return tools


def invalidate(server_id=None):
    """server 配置变更/删除时清缓存，下次运行按最新拉。"""
    if server_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(server_id, None)


def _safe(name):
    return _NAME_RE.sub("_", name or "")


# ---------- 给 engine 用：按应用勾选的 server 建 OpenAI schema + 路由表 ----------
def tools_for_app(app):
    """返回 (openai_tools_schema, routing)。
    routing: 暴露给模型的工具名 -> (server 字典, 原始工具名)。
    工具名统一命名成 mcp__{server_id}__{tool}，既避免和内置工具/别的 server 撞名，也便于路由。
    某个 server 连不上就跳过它——不影响其它工具，也不让整轮对话崩。"""
    ids = app.get("mcp") or []
    schema, routing = [], {}
    for sid in ids:
        server = store.get_mcp_server(sid)
        if not server or not server.get("enabled"):
            continue
        try:
            tools = _cached_tools(server)
        except Exception as e:
            print(f"[mcp] server#{sid}({server.get('name')}) 拉工具失败，跳过：{e}", file=sys.stderr)
            continue
        for t in tools:
            tname = t.get("name")
            if not tname:
                continue
            emitted = f"mcp__{sid}__{_safe(tname)}"[:64]
            while emitted in routing and routing[emitted][1] != tname:
                emitted = (emitted[:58] + "_" + str(len(routing)))[:64]
            routing[emitted] = (server, tname)
            schema.append({"type": "function", "function": {
                "name": emitted,
                "description": (t.get("description") or tname)[:1024],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            }})
    return schema, routing


def call(route, args):
    """engine 路由到这里执行 MCP 工具。失败以 error 形式回给模型，不抛。"""
    server, tname = route
    try:
        return call_tool(server, tname, args)
    except Exception as e:
        return {"error": f"MCP 调用失败：{e}"}
