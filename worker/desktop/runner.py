"""工作机 runner 原型：把一台带桌面的机器以结构化工具暴露给 agent。

  shell     在 /work 下执行命令（bash -lc），有超时与输出上限
  files     读写 /work 内的文件
  browser   有头 Chromium（固定用户配置）：open / act / network / screenshot / tabs
            页面视图 = 标题 + 主体正文 + 可见可交互元素（短 id）+ 页面调用过的 JSON 接口
  desktop   X11 截屏、xdotool 输入、AT-SPI 无障碍树（给非浏览器的图形程序用）

原型只在本机容器里跑：端口发布到宿主 127.0.0.1，Bearer 令牌鉴权。正式版由 runner 主动连控制面拉任务。
"""
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from playwright.async_api import async_playwright

WORK = Path("/work")
TOKEN = os.environ.get("RUNNER_TOKEN", "")
OUTPUT_LIMIT = 64 * 1024
app = FastAPI(title="worker-runner")


def auth(authorization: str = Header("")):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


def _tail(data: bytes):
    text = data.decode("utf-8", "replace")
    return text if len(text) <= OUTPUT_LIMIT else "…（前面已截断）\n" + text[-OUTPUT_LIMIT:]


def _inside_work(rel):
    p = (WORK / rel).resolve()
    if p != WORK and WORK not in p.parents:
        raise HTTPException(400, "路径必须在 /work 内")
    return p


async def _run(argv, timeout=60, cwd=WORK, stdin=None):
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        return proc.returncode, out, err, False
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return None, out, err, True


@app.get("/health")
async def health():
    return {"ok": True, "browser": BROWSER.ctx is not None}


# ---------- shell / files ----------
@app.post("/shell", dependencies=[Depends(auth)])
async def shell(body: dict):
    cmd = body.get("cmd") or ""
    timeout = max(1, min(int(body.get("timeout") or 60), 900))
    cwd = _inside_work(body.get("cwd") or ".")
    t = time.monotonic()
    code, out, err, timed_out = await _run(["bash", "-lc", cmd], timeout, cwd)
    return {"exit_code": code, "timed_out": timed_out, "ms": int((time.monotonic() - t) * 1000),
            "stdout": _tail(out), "stderr": _tail(err)}


@app.get("/files", dependencies=[Depends(auth)])
async def read_file(path: str, start: int = 1, end: int = 400):
    p = _inside_work(path)
    if p.is_dir():
        return {"path": path, "entries": sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())[:500]}
    lines = p.read_text("utf-8", "replace").splitlines()
    return {"path": path, "total_lines": len(lines), "start": start,
            "content": "\n".join(f"{i}\t{ln}" for i, ln in enumerate(lines[start - 1:end], start))}


@app.put("/files", dependencies=[Depends(auth)])
async def write_file(body: dict):
    p = _inside_work(body["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.get("content") or "", "utf-8")
    return {"path": body["path"], "bytes": p.stat().st_size}


# ---------- browser ----------
VIEW_JS = r"""(maxText) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const root = document.querySelector('main, [role=main], article') || document.body;
  const text = (root.innerText || '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
  const sel = 'a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [contenteditable=true]';
  const els = [];
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e)) continue;
    const name = (e.getAttribute('aria-label') || e.innerText || e.placeholder || e.value || e.title || e.name || '')
      .replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!name && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName)) continue;
    if (!e.dataset.aid) e.dataset.aid = String(window.__aid = (window.__aid || 0) + 1);
    const inMain = root !== document.body && root.contains(e);
    let line = `[${e.dataset.aid}] ${e.tagName.toLowerCase()}${e.type && e.tagName === 'INPUT' ? ':' + e.type : ''} ${name}`;
    const href = e.getAttribute('href');
    if (href && !href.startsWith('javascript:')) line += ' -> ' + href.slice(0, 120);
    els.push({ line, inMain });
  }
  // 主体里的元素优先，导航/页脚排后
  els.sort((a, b) => (b.inMain - a.inMain));
  return { url: location.href, title: document.title,
           text: text.length > maxText ? text.slice(0, maxText) + `\n…（正文共 ${text.length} 字，已截断）` : text,
           elements: els.map(x => x.line) };
}"""

DOM_QUIET_JS = """(ms) => new Promise((done) => {
  let t = setTimeout(fin, ms); const cap = setTimeout(fin, 4000);
  const mo = new MutationObserver(() => { clearTimeout(t); t = setTimeout(fin, ms); });
  mo.observe(document, { subtree: true, childList: true, characterData: true });
  function fin() { mo.disconnect(); clearTimeout(t); clearTimeout(cap); done(true); }
})"""


class Browser:
    def __init__(self):
        self.pw = self.ctx = None
        self.lock = asyncio.Lock()
        self.inflight = set()
        self.apis = []

    async def ensure(self):
        if self.ctx:
            return
        self.pw = await async_playwright().start()
        # 有头 + 固定用户配置：登录状态可保留，人能在 noVNC 里看到并接管；开启渲染无障碍，AT-SPI 也能读到它
        self.ctx = await self.pw.chromium.launch_persistent_context(
            "/home/agent/.chrome-profile", headless=False, viewport=None, locale="zh-CN",
            env={**os.environ, "ACCESSIBILITY_ENABLED": "1"},
            args=["--window-position=0,0", "--window-size=1440,900", "--force-renderer-accessibility",
                  "--no-first-run", "--disable-dev-shm-usage"])
        self.ctx.on("request", self._on_request)
        self.ctx.on("requestfinished", self._on_done)
        self.ctx.on("requestfailed", self._on_done)
        self.ctx.on("response", self._on_response)

    @property
    def page(self):
        return self.ctx.pages[-1]

    def _on_request(self, req):
        if req.resource_type in ("xhr", "fetch", "document"):
            self.inflight.add(req)

    def _on_done(self, req):
        self.inflight.discard(req)

    def _on_response(self, resp):
        req = resp.request
        if req.resource_type in ("xhr", "fetch") and "json" in (resp.headers.get("content-type") or ""):
            self.apis.append(resp)
            del self.apis[:-50]

    async def settle(self, wait_for=None, max_ms=4000):
        start = time.monotonic()
        try:
            await self.page.wait_for_load_state("load", timeout=max_ms)
        except Exception:
            pass
        while (time.monotonic() - start) * 1000 < max_ms:
            try:
                await self.page.evaluate(DOM_QUIET_JS, 300)
            except Exception:
                await asyncio.sleep(0.2)
            if not self.inflight:
                break
            await asyncio.sleep(0.05)
        if wait_for:
            try:
                await self.page.get_by_text(wait_for, exact=False).first.wait_for(timeout=20000)
            except Exception:
                return False
        return True

    async def view(self, max_text=5000, max_elements=60):
        v = await self.page.evaluate(VIEW_JS, max_text)
        v["elements"] = v["elements"][:max_elements]
        v["apis"] = [f"{r.status} {r.url[:160]}" for r in self.apis[-15:]]
        v["tabs"] = len(self.ctx.pages)
        if CHALLENGE.search(v["title"]) or CHALLENGE.search(v["text"][:300]):
            # 验证码 / 人机验证：不尝试绕过，交给人在桌面画面里处理
            v["needs_human"] = "页面要求人机验证，请在工作机桌面（noVNC）中手动完成后再继续"
        return v


BROWSER = Browser()
CHALLENGE = re.compile(r"Just a moment|请稍候|正在进行安全验证|verify you are human|Attention Required|captcha", re.I)


async def _center(page, aid):
    box = await page.evaluate("""(id) => { const e = document.querySelector(`[data-aid="${id}"]`);
      if (!e) return null; e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }""", int(aid))
    if not box:
        raise HTTPException(400, f"元素 {aid} 不存在（页面可能已变化，请重新 open 或看最新视图）")
    return box


@app.post("/browser/open", dependencies=[Depends(auth)])
async def browser_open(body: dict):
    async with BROWSER.lock:
        await BROWSER.ensure()
        t = time.monotonic()
        BROWSER.apis.clear()
        BROWSER.inflight.clear()
        error = None
        try:
            await BROWSER.page.goto(body["url"], wait_until="commit", timeout=30000)
        except Exception as e:
            error = f"打开失败：{type(e).__name__}"
        ready = await BROWSER.settle(body.get("wait_for"))
        v = await BROWSER.view(body.get("max_text") or 5000)
        v.update(ms=int((time.monotonic() - t) * 1000), ready=ready, error=error)
        return v


@app.post("/browser/act", dependencies=[Depends(auth)])
async def browser_act(body: dict):
    """actions: [{click:id} | {type:[id,text]} | {press:"Enter"} | {scroll:像素} | {wait:毫秒} | {back:true}]"""
    async with BROWSER.lock:
        await BROWSER.ensure()
        page = BROWSER.page
        t = time.monotonic()
        pages_before = len(BROWSER.ctx.pages)
        for a in body.get("actions") or []:
            if "click" in a:
                p = await _center(page, a["click"])
                await page.mouse.click(p["x"], p["y"])
                await asyncio.sleep(0.15)
            elif "type" in a:
                aid, text = a["type"]
                p = await _center(page, aid)
                await page.mouse.click(p["x"], p["y"])
                await page.keyboard.insert_text(text)
            elif "press" in a:
                await page.keyboard.press(a["press"])
                await asyncio.sleep(0.15)
            elif "scroll" in a:
                await page.mouse.wheel(0, int(a["scroll"]))
            elif "wait" in a:
                await asyncio.sleep(min(int(a["wait"]), 10000) / 1000)
            elif a.get("back"):
                await page.go_back()
            if len(BROWSER.ctx.pages) != pages_before:   # 点击打开了新标签：切过去
                page = BROWSER.page
                pages_before = len(BROWSER.ctx.pages)
        ready = await BROWSER.settle(body.get("wait_for"))
        v = await BROWSER.view(body.get("max_text") or 5000)
        v.update(ms=int((time.monotonic() - t) * 1000), ready=ready)
        return v


@app.post("/browser/network", dependencies=[Depends(auth)])
async def browser_network(body: dict):
    async with BROWSER.lock:
        await BROWSER.ensure()
        rx = re.compile(body.get("filter") or ".")
        out = []
        for r in BROWSER.apis:
            if not rx.search(r.url):
                continue
            try:
                text = await r.text()
            except Exception as e:
                out.append({"url": r.url, "error": type(e).__name__})
                continue
            out.append({"url": r.url, "status": r.status, "chars": len(text), "body": text[:body.get("max_chars") or 6000]})
        return {"responses": out[-5:]}


@app.get("/browser/screenshot", dependencies=[Depends(auth)])
async def browser_screenshot():
    async with BROWSER.lock:
        await BROWSER.ensure()
        png = await BROWSER.page.screenshot(type="png")
        return {"png_base64": base64.b64encode(png).decode(), "url": BROWSER.page.url}


# ---------- desktop ----------
@app.get("/desktop/screenshot", dependencies=[Depends(auth)])
async def desktop_screenshot(scale: float = 1.0):
    argv = ["import", "-window", "root"]
    if scale < 1:
        argv += ["-resize", f"{int(scale * 100)}%"]
    code, out, err, _ = await _run(argv + ["png:-"], 20)
    if code:
        raise HTTPException(500, _tail(err))
    return {"png_base64": base64.b64encode(out).decode()}


_KEY = re.compile(r"[A-Za-z0-9_+\-]{1,40}")


@app.post("/desktop/act", dependencies=[Depends(auth)])
async def desktop_act(body: dict):
    """actions: [{click:[x,y]} | {double_click:[x,y]} | {move:[x,y]} | {type:"文本"} | {key:"ctrl+s"} | {wait:毫秒}]"""
    done = []
    for a in body.get("actions") or []:
        if "click" in a or "double_click" in a or "move" in a:
            x, y = (int(v) for v in (a.get("click") or a.get("double_click") or a.get("move")))
            argv = ["xdotool", "mousemove", str(x), str(y)]
            if "click" in a:
                argv += ["click", "1"]
            elif "double_click" in a:
                argv += ["click", "--repeat", "2", "1"]
        elif "type" in a:
            argv = ["xdotool", "type", "--delay", "15", "--", str(a["type"])]
        elif "key" in a:
            if not _KEY.fullmatch(str(a["key"])):
                raise HTTPException(400, "按键格式不合法")
            argv = ["xdotool", "key", str(a["key"])]
        elif "wait" in a:
            await asyncio.sleep(min(int(a["wait"]), 10000) / 1000)
            continue
        else:
            raise HTTPException(400, f"未知动作 {a}")
        code, _out, err, _ = await _run(argv, 15)
        if code:
            raise HTTPException(500, _tail(err))
        done.append(argv[1])
    await asyncio.sleep(0.3)
    return {"done": done}


@app.get("/desktop/tree", dependencies=[Depends(auth)])
async def desktop_tree(app_name: str = "", depth: int = 25):
    code, out, err, timed_out = await _run(["/opt/venv/bin/python", "/opt/runner/atspi_tree.py", app_name, str(depth)], 20)
    if code or timed_out:
        raise HTTPException(500, "读取无障碍树失败：" + _tail(err)[-500:])
    return json.loads(out)


@app.post("/desktop/launch", dependencies=[Depends(auth)])
async def desktop_launch(body: dict):
    """在桌面上启动一个图形程序（后台运行），等它出现在无障碍树里。"""
    cmd = body.get("cmd") or ""
    await asyncio.create_subprocess_exec("bash", "-lc", f"nohup {cmd} >/tmp/launch.log 2>&1 &", cwd=str(WORK))
    await asyncio.sleep(float(body.get("wait") or 2))
    return {"launched": cmd}
