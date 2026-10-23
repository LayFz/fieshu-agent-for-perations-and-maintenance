"""逐项验证工作机原型的工具准确性：shell、浏览器（含 SPA、表单、反爬对比）、桌面（AT-SPI + xdotool）。

用法：RUNNER_TOKEN=... python verify.py [--keep-shots 目录]
每项输出：是否拿到目标事实、耗时、回传给模型的大致 token（字符数/3）。不调用任何模型。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

import requests

BASE = os.environ.get("RUNNER_URL", "http://127.0.0.1:8790")
H = {"Authorization": f"Bearer {os.environ['RUNNER_TOKEN']}"}
SHOTS = sys.argv[sys.argv.index("--keep-shots") + 1] if "--keep-shots" in sys.argv else None
results = []


def call(method, path, **kw):
    t = time.monotonic()
    r = requests.request(method, BASE + path, headers=H, timeout=180, **kw)
    r.raise_for_status()
    return r.json(), int((time.monotonic() - t) * 1000)


def record(name, ok, ms, payload, note=""):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    results.append({"task": name, "ok": bool(ok), "ms": ms, "approx_tokens": len(text) // 3, "note": note})
    print(f"{'OK  ' if ok else 'FAIL'} {name:<34} {ms:>6} ms  ~{len(text) // 3:>5} tok  {note}")


def shot(name, data):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        with open(os.path.join(SHOTS, name + ".png"), "wb") as f:
            f.write(base64.b64decode(data["png_base64"]))


def find(elements, pattern):
    for line in elements:
        if re.search(pattern, line):
            return int(re.match(r"\[(\d+)\]", line).group(1))
    return None


# ---------- shell：克隆真实仓库并分析 ----------
out, ms = call("POST", "/shell", json={"cmd": "rm -rf markupsafe && git clone -q --depth 1 https://github.com/pallets/markupsafe "
                                            "&& cd markupsafe && git log -1 --format=%H && grep -n '^version' pyproject.toml "
                                            "&& ls src/markupsafe", "timeout": 120})
record("shell: clone + 读版本", out["exit_code"] == 0 and re.search(r'version = "', out["stdout"]), ms, out,
       out["stdout"].splitlines()[1] if out["exit_code"] == 0 else out["stderr"][-200:])

# ---------- browser：GitHub 搜索框输入 → 回车 → 点开第一条 advisory ----------
v, ms1 = call("POST", "/browser/open", json={"url": "https://github.com/advisories"})
box = find(v["elements"], r"input:(text|search).*(Search|搜索)")
v2, ms2 = call("POST", "/browser/act", json={"actions": [{"type": [box, "ingress-nginx"]}, {"press": "Enter"}]}) if box else ({}, 0)
first = find(v2.get("elements", []), r"-> /advisories/GHSA-")
v3, ms3 = call("POST", "/browser/act", json={"actions": [{"click": first}]}) if first else ({}, 0)
ok = bool(re.search(r"GHSA-\w{4}-\w{4}-\w{4}", v3.get("url", ""))) and re.search(r"(?i)patched", v3.get("text", ""))
record("browser: 表单输入 + 点击详情", ok, ms1 + ms2 + ms3, [v, v2, v3], v3.get("title", "")[:60])

# ---------- browser：SPA 正文与背后接口 ----------
v, ms = call("POST", "/browser/open", json={"url": "https://www.cve.org/CVERecord?id=CVE-2025-1974", "wait_for": "Product Status"})
record("browser: SPA 正文（cve.org）", "9.8" in v["text"] and "1.11.4" in v["text"], ms, v, f"apis={len(v['apis'])}")
n, ms = call("POST", "/browser/network", json={"filter": r"cveawg.*api/cve/CVE"})
body = n["responses"][0]["body"] if n["responses"] else ""
record("browser: 取页面背后 JSON 接口", '"ingress-nginx"' in body, ms, n)

# ---------- 反爬：同一 URL，curl 与真实浏览器对比 ----------
for name, url, marker in (("npmjs 包页", "https://www.npmjs.com/package/express", r"(?i)fast, unopinionated"),
                          ("Cloudflare 挑战测试页", "https://nowsecure.nl/", r"(?i)passed|you are not a bot|nowsecure")):
    c = subprocess.run(["curl", "-s", "-m", "20", "-A", "Mozilla/5.0", "-o", "/dev/null", "-w", "%{http_code}", url],
                       capture_output=True, text=True)
    v, ms = call("POST", "/browser/open", json={"url": url, "wait_for": None})
    if v.get("needs_human") or not re.search(marker, v["text"]):
        v, more = call("POST", "/browser/act", json={"actions": [{"wait": 8000}]})   # JS 挑战通常几秒内自动通过
        ms += more
    ok = bool(re.search(marker, v["text"])) and not v.get("needs_human")
    note = f"curl HTTP {c.stdout or '超时'} / 浏览器标题「{v['title'][:30]}」"
    if v.get("needs_human"):
        note += " → 人机验证，已标记转人工"
    record(f"反爬: {name}", ok or bool(v.get("needs_human")), ms, v, note)
    shot("antibot-" + name, call("GET", "/browser/screenshot")[0])

# ---------- desktop：图形程序（mousepad）靠 AT-SPI 定位、xdotool 操作 ----------
call("POST", "/desktop/launch", json={"cmd": "mousepad /work/note.txt", "wait": 3})
tree, ms_t = call("GET", "/desktop/tree", params={"app_name": "mousepad"})
editor = next((n for n in tree["nodes"] if n["role"] in ("text", "document text") and n.get("box")), None)
ms_a = 0
if editor:
    x, y, w, h = editor["box"]
    _, ms_a = call("POST", "/desktop/act", json={"actions": [{"click": [x + w // 2, y + h // 2]},
                                                             {"type": "harness 桌面原型：AT-SPI 定位 + xdotool 输入"},
                                                             {"key": "ctrl+s"}, {"wait": 800}]})
saved, _ = call("GET", "/files", params={"path": "note.txt"})
record("desktop: 无障碍树定位编辑区并输入保存", "AT-SPI" in saved.get("content", ""), ms_t + ms_a, tree,
       f"apps={tree['apps']} nodes={len(tree['nodes'])}")
menu = next((n for n in tree["nodes"] if n["role"] == "menu" and n.get("box") and re.search(r"(?i)^file|文件", n["name"])), None)
if menu:
    x, y, w, h = menu["box"]
    call("POST", "/desktop/act", json={"actions": [{"click": [x + w // 2, y + h // 2]}, {"wait": 500}]})
    after, _ = call("GET", "/desktop/tree", params={"app_name": "mousepad"})
    opened = [n["name"] for n in after["nodes"] if n["role"] == "menu item" and n.get("box")]
    record("desktop: 按名字点开菜单", len(opened) > 3, 0, after, f"菜单项 {opened[:5]}")
    call("POST", "/desktop/act", json={"actions": [{"key": "Escape"}]})
else:
    record("desktop: 按名字点开菜单", False, 0, tree, "树里没找到 File 菜单")
chrome = [a for a in tree["apps"] if re.search(r"(?i)chrom", a or "")]
record("desktop: 浏览器也出现在无障碍树", bool(chrome), 0, tree["apps"], str(chrome))
s, ms = call("GET", "/desktop/screenshot", params={"scale": 0.5})
shot("desktop", s)
record("desktop: 整屏截图", len(s["png_base64"]) > 1000, ms, "（图片不计 token）")

print(json.dumps({"passed": sum(r["ok"] for r in results), "total": len(results)}, ensure_ascii=False))
with open(os.environ.get("VERIFY_OUT", "verify-results.json"), "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
