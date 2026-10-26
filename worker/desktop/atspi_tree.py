"""打印桌面应用的 AT-SPI 无障碍树（JSON）。runner 以子进程调用，避免 GLib 主循环和 asyncio 混在一起。

用法：atspi_tree.py [应用名过滤] [最大深度]
每个节点：id（应用序号.路径）、role、name、坐标（屏幕绝对坐标，可直接给 xdotool 点击）、可用动作。
"""
import json
import sys

import pyatspi

MAX_NODES = 400
INTERESTING = {"push button", "toggle button", "check box", "radio button", "menu item", "menu", "text", "entry",
               "combo box", "list item", "page tab", "link", "label", "heading", "paragraph", "frame", "dialog",
               "tool bar", "menu bar", "document text", "terminal", "spin button", "slider", "table cell"}


def walk(node, path, depth, max_depth, out):
    if len(out) >= MAX_NODES or depth > max_depth:
        return
    try:
        role = node.getRoleName()
        name = (node.name or "").strip()
        states = node.getState()
        if not states.contains(pyatspi.STATE_SHOWING) and depth > 1:
            return
        item = {"id": path, "role": role, "name": name[:120]}
        try:
            ext = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
            if ext.width > 0 and ext.height > 0:
                item["box"] = [ext.x, ext.y, ext.width, ext.height]
        except NotImplementedError:
            pass
        try:
            action = node.queryAction()
            item["actions"] = [action.getName(i) for i in range(action.nActions)]
        except NotImplementedError:
            pass
        try:
            text = node.queryText()
            content = text.getText(0, min(text.characterCount, 300))
            if content and content != name:
                item["text"] = content
        except NotImplementedError:
            pass
        if role in INTERESTING or name or item.get("text"):
            out.append(item)
        for i in range(node.childCount):
            walk(node.getChildAtIndex(i), f"{path}.{i}", depth + 1, max_depth, out)
    except Exception:
        return   # 应用在遍历中退出或节点失效：跳过


def main():
    app_filter = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    max_depth = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    desktop = pyatspi.Registry.getDesktop(0)
    apps, nodes = [], []
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app is None:
            continue
        apps.append(app.name)
        if app_filter and app_filter not in (app.name or "").lower():
            continue
        walk(app, str(i), 0, max_depth, nodes)
    print(json.dumps({"apps": apps, "nodes": nodes, "truncated": len(nodes) >= MAX_NODES}, ensure_ascii=False))


if __name__ == "__main__":
    main()
