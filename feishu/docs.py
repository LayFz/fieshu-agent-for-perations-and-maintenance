"""飞书云文档/知识库高层操作（应用身份 token）。供 tools 调用。"""
from . import md2feishu
from .md2feishu import api


def list_kbs():
    """列出应用可见的知识库。"""
    r = api("GET", "/wiki/v2/spaces?page_size=50")
    items = (r.get("data") or {}).get("items") or []
    return {"code": r.get("code"), "knowledge_bases": [
        {"name": s.get("name"), "space_id": s.get("space_id")} for s in items
    ]}


def list_nodes(space_id, parent_node_token=None):
    """列出某知识库（可指定父节点）下的文档节点。"""
    q = f"/wiki/v2/spaces/{space_id}/nodes?page_size=50"
    if parent_node_token:
        q += f"&parent_node_token={parent_node_token}"
    r = api("GET", q)
    items = (r.get("data") or {}).get("items") or []
    return {"code": r.get("code"), "nodes": [
        {"title": n.get("title"), "obj_token": n.get("obj_token"),
         "node_token": n.get("node_token"), "obj_type": n.get("obj_type"),
         "has_child": n.get("has_child")} for n in items
    ]}


def read_doc(doc_id):
    """读取一篇文档(docx)的纯文本正文。doc_id = 节点的 obj_token。"""
    r = api("GET", f"/docx/v1/documents/{doc_id}/raw_content")
    return (r.get("data") or {}).get("content") or ""


def rewrite_doc(doc_id, markdown):
    """用 Markdown 覆盖重写一篇已有文档。"""
    wrote = md2feishu.rewrite(doc_id, markdown)
    return {"ok": True, "wrote_blocks": wrote,
            "url": f"https://feishu.cn/docx/{doc_id}"}


def create_node(space_id, title, markdown, parent_node_token=None):
    """在某知识库下新建一篇文档(docx 节点)，写入 Markdown 内容。"""
    body = {"obj_type": "docx", "node_type": "origin", "title": title}
    if parent_node_token:
        body["parent_node_token"] = parent_node_token
    r = api("POST", f"/wiki/v2/spaces/{space_id}/nodes", body)
    node = (r.get("data") or {}).get("node") or {}
    obj = node.get("obj_token")
    if not obj:
        return {"ok": False, "error": r}
    md2feishu.write_blocks(obj, md2feishu.md_to_blocks(markdown), 0)
    return {"ok": True, "obj_token": obj, "node_token": node.get("node_token"),
            "url": f"https://feishu.cn/wiki/{node.get('node_token')}"}
