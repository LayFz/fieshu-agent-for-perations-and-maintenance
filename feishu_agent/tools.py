"""工具定义（给大模型 function-calling）+ 执行器（真正干活）。"""
from . import feishu_docs, store

# OpenAI 兼容的 tools schema
TOOLS = [
    {"type": "function", "function": {
        "name": "list_knowledge_bases",
        "description": "列出应用能访问的全部知识库（拿到 space_id）。不确定写到哪个库时先调它。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_nodes",
        "description": "列出某知识库下的文档节点（标题 + obj_token）。要改/读某篇前先用它定位。",
        "parameters": {"type": "object", "properties": {
            "space_id": {"type": "string"},
            "parent_node_token": {"type": "string", "description": "可选，钻取子节点"},
        }, "required": ["space_id"]},
    }},
    {"type": "function", "function": {
        "name": "read_doc",
        "description": "读取一篇文档的正文（doc_id = 节点的 obj_token）。",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"},
        }, "required": ["doc_id"]},
    }},
    {"type": "function", "function": {
        "name": "rewrite_doc",
        "description": "用 Markdown 覆盖重写一篇已有文档（doc_id = obj_token）。会清空原内容，请先 read_doc 确认。",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string"},
            "markdown": {"type": "string", "description": "新正文，Markdown(支持 # ## ### / ``` / - / **粗** / `码` / > / | 表格 |)"},
        }, "required": ["doc_id", "markdown"]},
    }},
    {"type": "function", "function": {
        "name": "create_node",
        "description": "在某知识库下新建一篇文档并写入 Markdown 内容。",
        "parameters": {"type": "object", "properties": {
            "space_id": {"type": "string"},
            "title": {"type": "string"},
            "markdown": {"type": "string"},
            "parent_node_token": {"type": "string", "description": "可选，建在某节点下"},
        }, "required": ["space_id", "title", "markdown"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "记住一条长期信息（偏好/约定/结论），以后对话会自动带上。",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"},
        }, "required": ["note"]},
    }},
]


def tool_list():
    """给管理后台看的工具清单（名称 + 说明）。"""
    return [{"name": t["function"]["name"], "description": t["function"]["description"]} for t in TOOLS]


def execute(name, args):
    try:
        if name == "list_knowledge_bases":
            return feishu_docs.list_kbs()
        if name == "list_nodes":
            return feishu_docs.list_nodes(args["space_id"], args.get("parent_node_token"))
        if name == "read_doc":
            return {"content": feishu_docs.read_doc(args["doc_id"])[:8000]}
        if name == "rewrite_doc":
            return feishu_docs.rewrite_doc(args["doc_id"], args["markdown"])
        if name == "create_node":
            return feishu_docs.create_node(args["space_id"], args["title"], args["markdown"],
                                           args.get("parent_node_token"))
        if name == "remember":
            store.add_note("global", args["note"])
            return {"ok": True}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}
