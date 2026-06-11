"""Markdown -> 飞书 docx 块。用应用 token(tenant_access_token)。
支持: # ## ### 标题 / ``` 代码块 / - 列表 / **粗** / `行内码` / > 引用 / | 表格 |(原生表格，列宽自适应)。
"""
import json
import re
import time

import requests

from .auth import tenant_token

BASE = "https://open.feishu.cn/open-apis"


def api(method, path, body=None):
    r = requests.request(
        method, BASE + path,
        headers={"Authorization": "Bearer " + tenant_token(),
                 "Content-Type": "application/json; charset=utf-8"},
        json=body, timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"code": r.status_code, "msg": r.text[:300]}


def runs(text):
    out, pos = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+?)`", text):
        if m.start() > pos:
            out.append({"text_run": {"content": text[pos:m.start()]}})
        if m.group(1) is not None:
            out.append({"text_run": {"content": m.group(1), "text_element_style": {"bold": True}}})
        else:
            out.append({"text_run": {"content": m.group(2), "text_element_style": {"inline_code": True}}})
        pos = m.end()
    if pos < len(text):
        out.append({"text_run": {"content": text[pos:]}})
    return out or [{"text_run": {"content": text}}]


def _cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def md_to_blocks(md):
    blocks, lines, i = [], md.split("\n"), 0
    sep = re.compile(r"^\s*\|?[\s:|-]*-[-\s:|]*\|?\s*$")
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append({"block_type": 14, "code": {"elements": [{"text_run": {"content": "\n".join(buf)}}],
                                                      "style": {"language": 1, "wrap": True}}})
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and sep.match(lines[i + 1]) and "-" in lines[i + 1]:
            headers = _cells(ln)
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            blocks.append({"_table": {"headers": headers, "rows": rows}})
            continue
        if ln.startswith("### "):
            blocks.append({"block_type": 5, "heading3": {"elements": runs(ln[4:])}})
        elif ln.startswith("## "):
            blocks.append({"block_type": 4, "heading2": {"elements": runs(ln[3:])}})
        elif ln.startswith("# "):
            blocks.append({"block_type": 3, "heading1": {"elements": runs(ln[2:])}})
        elif ln.lstrip().startswith(("- ", "* ")):
            blocks.append({"block_type": 12, "bullet": {"elements": runs(ln.lstrip()[2:])}})
        elif ln.lstrip().startswith("> "):
            blocks.append({"block_type": 2, "text": {"elements": runs("💡 " + ln.lstrip()[2:])}})
        elif ln.strip() == "":
            pass
        else:
            blocks.append({"block_type": 2, "text": {"elements": runs(ln)}})
        i += 1
    return blocks


def _clen(s):
    return sum(1.8 if ord(ch) > 0x2e80 else 1 for ch in str(s))


def _col_widths(allrows, ncol, total=700, floor=72):
    w = []
    for c in range(ncol):
        m = max((_clen(r[c]) if c < len(r) else 0) for r in allrows)
        w.append(max(m, 2))
    tot = sum(w) or 1
    return [max(floor, int(total * x / tot)) for x in w]


def _table_payload(tbl, index):
    headers, rows = tbl["headers"], tbl["rows"]
    ncol = len(headers)
    allrows = [headers] + rows
    nrow = len(allrows)
    desc = [{"block_id": "tbl", "block_type": 31,
             "table": {"property": {"row_size": nrow, "column_size": ncol, "header_row": True,
                                    "column_width": _col_widths(allrows, ncol)}},
             "children": []}]
    children = []
    for r in range(nrow):
        for c in range(ncol):
            cid, tid = "c%d_%d" % (r, c), "t%d_%d" % (r, c)
            children.append(cid)
            content = allrows[r][c] if c < len(allrows[r]) else ""
            desc.append({"block_id": cid, "block_type": 32, "table_cell": {}, "children": [tid]})
            desc.append({"block_id": tid, "block_type": 2, "text": {"elements": runs(content)}})
    desc[0]["children"] = children
    return {"index": index, "children_id": ["tbl"], "descendants": desc}


def root_child_count(doc):
    r = api("GET", f"/docx/v1/documents/{doc}/blocks?page_size=500")
    for b in (r.get("data") or {}).get("items") or []:
        if b.get("block_id") == doc:
            return len(b.get("children") or [])
    return 0


def write_blocks(doc, blocks, start_index):
    idx = start_index
    batch = []

    def flush():
        nonlocal idx
        for k in range(0, len(batch), 50):
            chunk = batch[k:k + 50]
            r = api("POST", f"/docx/v1/documents/{doc}/blocks/{doc}/children", {"index": idx, "children": chunk})
            if r.get("code") != 0:
                raise RuntimeError("写入失败 @%d: %s" % (idx, json.dumps(r, ensure_ascii=False)))
            idx += len(chunk)
            time.sleep(0.4)
        batch.clear()

    for b in blocks:
        if "_table" in b:
            flush()
            r = api("POST", f"/docx/v1/documents/{doc}/blocks/{doc}/descendant", _table_payload(b["_table"], idx))
            if r.get("code") != 0:
                raise RuntimeError("写表格失败 @%d: %s" % (idx, json.dumps(r, ensure_ascii=False)))
            idx += 1
            time.sleep(0.4)
        else:
            batch.append(b)
    flush()
    return idx - start_index


def rewrite(doc_id, markdown):
    """清空并用 Markdown 重写一篇文档。"""
    n = root_child_count(doc_id)
    if n > 0:
        api("DELETE", f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete",
            {"start_index": 0, "end_index": n})
    return write_blocks(doc_id, md_to_blocks(markdown), 0)
