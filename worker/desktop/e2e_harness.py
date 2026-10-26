"""端到端：临时 harness（临时数据目录、不连飞书）+ 工作机容器经拉取协议连上来，agent 工具通过队列操作工作机。

用法（仓库根目录）：
  FEISHU_AGENT_CONFIG='' .venv-runtime/bin/python worker/desktop/e2e_harness.py
会重建并启动名为 feishu-worker-e2e 的容器（不发布 runner 端口，只发布 noVNC 到 127.0.0.1:16081），结束后保留容器供观看。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ["FEISHU_AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="feishu-box-e2e-")
os.environ.setdefault("FEISHU_AGENT_CONFIG", "")

import uvicorn  # noqa: E402

from core import boxstore  # noqa: E402
from llm import tools  # noqa: E402
from web import admin  # noqa: E402

PORT = 18800
NAME = "feishu-worker-e2e"


def serve():
    uvicorn.run(admin.app, host="127.0.0.1", port=PORT, log_level="warning")


def show(title, out, keys=("title", "url", "exit_code", "error", "needs_human")):
    brief = {k: out.get(k) for k in keys if isinstance(out, dict) and out.get(k) is not None}
    print(f"\n== {title}\n{json.dumps(brief, ensure_ascii=False)[:600]}")
    return out


def main():
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(1.5)
    token = boxstore.create_runner("default")
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", NAME, "--shm-size=1g", "-e", "RUNNER_TOKEN=unused-local-api",
                    "-e", f"HARNESS_URL=http://host.docker.internal:{PORT}", "-e", f"HARNESS_RUNNER_TOKEN={token}",
                    "-p", "127.0.0.1:16081:6080", "feishu-worker-desktop:dev"], check=True, capture_output=True)
    for _ in range(60):
        r = boxstore.get_runner("default")
        if r and r["online"]:
            break
        time.sleep(1)
    else:
        raise SystemExit("工作机 60s 内没有连上 harness：docker logs " + NAME)
    print("工作机已连上：", boxstore.get_runner("default")["info"])
    t0 = time.monotonic()

    out = show("box_shell 克隆仓库", tools._box_tool("box_shell", {
        "cmd": "rm -rf markupsafe && git clone -q --depth 1 https://github.com/pallets/markupsafe && grep -n '^version' markupsafe/pyproject.toml"}))
    print("stdout:", out.get("stdout", "").strip())
    out = show("box_files 读文件", tools._box_tool("box_files", {"action": "read", "path": "markupsafe/README.md", "end": 5}))
    print(out.get("content", "")[:200])

    v = show("box_browser open GitHub Advisory", tools._box_tool("box_browser", {"action": "open", "url": "https://github.com/advisories"}))
    box_id = next(int(e[1:e.index("]")]) for e in v["elements"] if "input:" in e and "Search" in e)
    v = show("box_browser act 输入并回车", tools._box_tool("box_browser", {"action": "act", "actions": [
        {"type": [box_id, "ingress-nginx"]}, {"press": "Enter"}]}))
    first = next(int(e[1:e.index("]")]) for e in v["elements"] if "-> /advisories/GHSA-" in e)
    v = show("box_browser act 点开第一条", tools._box_tool("box_browser", {"action": "act", "actions": [{"click": first}]}))
    print("正文片段：", v["text"][:200].replace("\n", " | "))

    v = show("box_browser open cve.org", tools._box_tool("box_browser", {"action": "open", "url": "https://www.cve.org/CVERecord?id=CVE-2025-1974",
                                                                          "wait_for": "Product Status"}))
    n = tools._box_tool("box_browser", {"action": "network", "filter": "cveawg.*api/cve/CVE"})
    print("背后接口：", [r["url"] for r in n.get("responses", [])])

    show("box_desktop launch", tools._box_tool("box_desktop", {"action": "launch", "cmd": "mousepad /work/e2e.txt"}))
    tree = tools._box_tool("box_desktop", {"action": "tree", "app": "mousepad"})
    editor = next(x for x in tree["nodes"] if x["role"] in ("text", "document text") and x.get("box"))
    x, y, w, h = editor["box"]
    show("box_desktop act 输入并保存", tools._box_tool("box_desktop", {"action": "act", "actions": [
        {"click": [x + w // 2, y + h // 2]}, {"type": "经 harness 队列写入"}, {"key": "ctrl+s"}, {"wait": 800}]}), keys=("done", "error"))
    out = tools._box_tool("box_files", {"action": "read", "path": "e2e.txt"})
    print("文件内容：", out.get("content", "").strip())

    print(f"\n总耗时 {time.monotonic() - t0:.1f}s；操作记录：")
    for r in admin.store._conn.execute("SELECT op, status, round(finished-created,2) s FROM box_ops ORDER BY created"):
        print(" ", r["op"], r["status"], f"{r['s']}s")
    print(f"\n工作机画面：http://127.0.0.1:16081/vnc.html?autoconnect=1&resize=scale（容器 {NAME} 保留）")


if __name__ == "__main__":
    main()
