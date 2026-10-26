// 直连 CDP 的浏览器工具原型：不经过 Playwright，按「任务」粒度给模型返回精简视图。
//   open(url)        打开页面，等网络静默，返回 标题 + 正文摘要 + 可交互元素 + 页面调用过的 JSON 接口
//   act(actions)     一次执行多个动作（click / type / press / wait），用 Input.* 原生事件，返回新视图
//   network(filter)  取页面背后 JSON 接口的响应体（SPA 往往比读 DOM 快且准）
//   evaluate(expr)   微基准用：一次 Runtime.evaluate 往返
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class CdpBrowser {
  static async launch({ headless = true } = {}) {
    const dir = mkdtempSync(join(tmpdir(), "cdp-spike-"));
    const args = ["--remote-debugging-port=0", `--user-data-dir=${dir}`, "--no-first-run",
      "--no-default-browser-check", "--disable-extensions", "--window-size=1280,900", "about:blank"];
    if (headless) args.unshift("--headless=new");
    const proc = spawn(CHROME, args, { stdio: "ignore" });
    const portFile = join(dir, "DevToolsActivePort");
    for (let i = 0; i < 100 && !existsSync(portFile); i++) await sleep(50);
    const port = readFileSync(portFile, "utf8").split("\n")[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = targets.find((t) => t.type === "page");
    const b = new CdpBrowser(proc, dir);
    await b._connect(page.webSocketDebuggerUrl);
    return b;
  }

  constructor(proc, dir) {
    this.proc = proc; this.dir = dir; this.id = 0; this.pending = new Map();
    this.inflight = new Set(); this.lastNet = Date.now(); this.apis = new Map(); this.loaded = false;
  }

  async _connect(url) {
    this.ws = new WebSocket(url);
    await new Promise((ok, bad) => { this.ws.onopen = ok; this.ws.onerror = bad; });
    this.ws.onmessage = (ev) => this._onMessage(JSON.parse(ev.data));
    await Promise.all([this.send("Page.enable"), this.send("Network.enable"), this.send("Runtime.enable")]);
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok, bad) => this.pending.set(id, { ok, bad, method }));
  }

  _onMessage(m) {
    if (m.id) {
      const p = this.pending.get(m.id); this.pending.delete(m.id);
      return m.error ? p.bad(new Error(`${p.method}: ${m.error.message}`)) : p.ok(m.result);
    }
    const { method, params } = m;
    if (method === "Network.requestWillBeSent") {
      this.lastNet = Date.now();
      // 只跟踪数据请求；图片/埋点/字体不影响「内容是否到齐」
      if (["XHR", "Fetch", "Document"].includes(params.type)) this.inflight.add(params.requestId);
    }
    if (method === "Network.loadingFinished" || method === "Network.loadingFailed") {
      this.inflight.delete(params.requestId); this.lastNet = Date.now();
    }
    if (method === "Network.responseReceived") {
      const r = params.response;
      if (/json/i.test(r.mimeType) && ["XHR", "Fetch"].includes(params.type)) {
        this.apis.set(params.requestId, { url: r.url, status: r.status });
      }
    }
    if (method === "Page.loadEventFired") this.loaded = true;
  }

  // 等页面 load 且网络静默 quietMs（长连接/轮询不会让它卡死：最多 maxMs）
  // 先等 load，再等 DOM 连续 quietMs 没有变化（比「网络静默」可靠：埋点/轮询请求不会拖住它），总上限 maxMs
  async settle({ quietMs = 300, maxMs = 4000 } = {}) {
    const start = Date.now();
    while (!this.loaded && Date.now() - start < maxMs) await sleep(30);
    while (Date.now() - start < maxMs) {
      await this._domQuiet(quietMs, Math.max(0, maxMs - (Date.now() - start)));
      if (this.inflight.size === 0) return;
      await sleep(50);
    }
  }

  async _domQuiet(quietMs, left) {
    try {
      await this.evaluate(`new Promise((done) => {
        let t = setTimeout(fin, ${quietMs}); const cap = setTimeout(fin, ${left});
        const mo = new MutationObserver(() => { clearTimeout(t); t = setTimeout(fin, ${quietMs}); });
        mo.observe(document, { subtree: true, childList: true, characterData: true });
        function fin() { mo.disconnect(); clearTimeout(t); clearTimeout(cap); done(true); }
      })`);
    } catch { /* 导航中执行上下文被销毁：再等一次 load */ await sleep(200); }
  }

  async evaluate(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    return r.result.value;
  }

  // waitFor：慢 SPA（如 NVD 先渲染「Loading…」）可指定等某段文字出现，最多 20s
  async open(url, { waitFor } = {}) {
    this.loaded = false; this.apis.clear(); this.inflight.clear(); this.lastNet = Date.now();
    await this.send("Page.navigate", { url });
    await this.settle();
    if (waitFor) await this.waitText(waitFor);
    return this.view();
  }

  async waitText(text, maxMs = 20000) {
    const start = Date.now();
    while (Date.now() - start < maxMs) {
      if (await this.evaluate(`document.body && document.body.innerText.includes(${JSON.stringify(text)})`)) return true;
      await sleep(100);
    }
    return false;
  }

  // 精简视图：正文优先 main/article，元素只要可见的可交互元素，给稳定短 id
  async view({ maxText = 5000, maxElements = 60 } = {}) {
    const v = await this.evaluate(`(() => {
      const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const root = document.querySelector('main, [role=main], article') || document.body;
      const text = (root.innerText || '').replace(/[ \\t]+/g, ' ').replace(/\\n\\s*\\n+/g, '\\n').trim();
      const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=tab], [contenteditable=true]';
      const all = [...root.querySelectorAll(sel), ...(root === document.body ? [] : document.querySelectorAll('header ' + sel.split(',').join(', header ')))];
      const seen = new Set(), els = [];
      for (const e of all) {
        if (seen.has(e) || !vis(e)) continue; seen.add(e);
        const name = (e.getAttribute('aria-label') || e.innerText || e.placeholder || e.value || e.title || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
        if (!name && e.tagName !== 'INPUT') continue;
        if (!e.dataset.aid) e.dataset.aid = String(window.__aid = (window.__aid || 0) + 1);
        const item = { id: +e.dataset.aid, tag: e.tagName.toLowerCase(), name };
        if (e.href) item.href = e.getAttribute('href');
        if (e.type && e.tagName === 'INPUT') item.type = e.type;
        els.push(item);
      }
      return { url: location.href, title: document.title, text, elements: els };
    })()`);
    const more = v.text.length > maxText;
    return {
      url: v.url, title: v.title,
      text: v.text.slice(0, maxText) + (more ? `\n…（正文共 ${v.text.length} 字，已截断）` : ""),
      elements: v.elements.slice(0, maxElements).map((e) => `[${e.id}] ${e.tag}${e.type ? ":" + e.type : ""} ${e.name}${e.href ? " -> " + e.href : ""}`),
      apis: [...this.apis.values()].map((a) => `${a.status} ${a.url}`).slice(0, 20),
    };
  }

  async _center(id) {
    return this.evaluate(`(() => { const e = document.querySelector('[data-aid="${Number(id)}"]');
      if (!e) return null; e.scrollIntoView({ block: 'center' }); const r = e.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
  }

  async act(actions) {
    this.loaded = true;          // 动作不一定导航；导航时 frameNavigated→load 会重新置位
    const onNav = () => { this.loaded = false; this.apis.clear(); };
    for (const a of actions) {
      if (a.click != null) {
        const p = await this._center(a.click);
        if (!p) throw new Error(`元素 ${a.click} 不存在`);
        const urlBefore = await this.evaluate("location.href");
        for (const type of ["mousePressed", "mouseReleased"]) {
          await this.send("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, button: "left", clickCount: 1 });
        }
        await sleep(150);
        if ((await this.evaluate("document.readyState")) !== "complete" || (await this.evaluate("location.href")) !== urlBefore) onNav();
      } else if (a.type) {
        const [id, text] = a.type;
        const p = await this._center(id);
        await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: p.x, y: p.y, button: "left", clickCount: 1 });
        await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: p.x, y: p.y, button: "left", clickCount: 1 });
        await this.send("Input.insertText", { text });
      } else if (a.press) {
        const key = a.press;
        const code = key === "Enter" ? 13 : 0;
        await this.send("Input.dispatchKeyEvent", { type: "keyDown", key, code: key, windowsVirtualKeyCode: code, text: key === "Enter" ? "\r" : undefined });
        await this.send("Input.dispatchKeyEvent", { type: "keyUp", key, code: key, windowsVirtualKeyCode: code });
        await sleep(150);
        if ((await this.evaluate("document.readyState")) !== "complete") onNav();
      } else if (a.wait) {
        await sleep(a.wait);
      }
    }
    await this.settle();
    return this.view();
  }

  async network(filter, maxChars = 6000) {
    const re = new RegExp(filter || ".");
    const out = [];
    for (const [requestId, a] of this.apis) {
      if (!re.test(a.url)) continue;
      try {
        const { body, base64Encoded } = await this.send("Network.getResponseBody", { requestId });
        const text = base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body;
        out.push({ url: a.url, status: a.status, chars: text.length, body: text.slice(0, maxChars) });
      } catch (e) { out.push({ url: a.url, error: String(e.message) }); }
    }
    return out;
  }

  async close() {
    try { this.ws.close(); } catch {}
    this.proc.kill();
    await sleep(200);
    rmSync(this.dir, { recursive: true, force: true });
  }
}
