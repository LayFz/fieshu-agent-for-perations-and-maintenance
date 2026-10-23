// 对比 Playwright MCP 与直连 CDP 原型：同一批真实任务，按「一个 agent 需要的工具调用」脚本化执行。
// 记录：调用次数、回传给模型的 token（cl100k 近似）、工具耗时、是否拿到目标事实。不调用任何模型。
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { getEncoding } from "js-tiktoken";
import { mkdirSync, writeFileSync } from "node:fs";
import { CdpBrowser } from "./cdp.mjs";

const enc = getEncoding("cl100k_base");
const ROUNDS = Number(process.env.ROUNDS || 3);
const NVD = "https://nvd.nist.gov/vuln/detail/CVE-2025-1974";
const GH = "https://github.com/advisories?query=ingress-nginx";
const CVE = "https://www.cve.org/CVERecord?id=CVE-2025-1974";

const CHECKS = {
  nvd: [/9\.8/, /CRITICAL/i],
  github: [/GHSA-\w{4}-\w{4}-\w{4}/, /(critical|high|moderate|low)/i, /patched/i],
  cve_dom: [/9\.8/, /1\.11\.4/, /ingress-nginx/],
  cve_api: [/9\.8/, /1\.11\.4/, /ingress-nginx/],
};

// 记录每次工具调用：模型实际看到的文本、耗时
class Meter {
  constructor() { this.calls = []; }
  async call(name, fn) {
    const t = performance.now();
    const out = await fn();
    const text = typeof out === "string" ? out : JSON.stringify(out);
    this.calls.push({ name, ms: performance.now() - t, tokens: enc.encode(text).length, text });
    return out;
  }
  summary(checks) {
    const all = this.calls.map((c) => c.text).join("\n");
    return {
      calls: this.calls.length,
      tokens: this.calls.reduce((s, c) => s + c.tokens, 0),
      ms: Math.round(this.calls.reduce((s, c) => s + c.ms, 0)),
      ok: checks.every((re) => re.test(all)),
      steps: this.calls.map((c) => `${c.name}:${Math.round(c.ms)}ms/${c.tokens}tok`),
    };
  }
}

// ---------- Playwright MCP ----------
async function pwStart() {
  const t = new StdioClientTransport({ command: "node", args: ["node_modules/@playwright/mcp/cli.js", "--headless",
    "--browser", "chrome", "--isolated", "--output-dir", "/tmp/pwmcp-spike"] });
  const c = new Client({ name: "spike", version: "0" });
  await c.connect(t);
  const call = async (name, args) => (await c.callTool({ name, arguments: args })).content.map((x) => x.text || "").join("\n");
  await call("browser_navigate", { url: "about:blank" });   // 预热：启动浏览器不计入任务
  return { c, call };
}

// 导航/动作的回包不一定内联快照（新版写文件）；agent 想「看」页面就得再调一次 browser_snapshot
async function pwSee(pw, m, name, args) {
  const out = await m.call(name, () => pw.call(name, args));
  if (!out.includes("```yaml")) return m.call("browser_snapshot", () => pw.call("browser_snapshot", {}));
  return out;
}

const PW_TASKS = {
  async nvd(pw, m) {
    await m.call("browser_navigate", () => pw.call("browser_navigate", { url: NVD }));
    // wait_for 默认 5s 超时；NVD 常要 10~20s 才渲染完，agent 只能重试（每次都是一轮模型往返）
    for (let i = 0; i < 5; i++) {
      const out = await m.call("browser_wait_for", () => pw.call("browser_wait_for", { text: "Base Score" }));
      if (!out.includes("TimeoutError")) break;
    }
    await m.call("browser_snapshot", () => pw.call("browser_snapshot", {}));
  },
  async github(pw, m) {
    const snap = await pwSee(pw, m, "browser_navigate", { url: GH });
    const ref = snap.match(/link "[^"]*" \[ref=([a-z0-9]+)\][^\n]*\n\s*- \/url: \/advisories\/GHSA-/)?.[1];
    if (!ref) throw new Error("快照里没找到 advisory 链接");
    await pwSee(pw, m, "browser_click", { element: "first advisory", target: ref });
  },
  async cve_dom(pw, m) {
    await m.call("browser_navigate", () => pw.call("browser_navigate", { url: CVE }));
    await m.call("browser_wait_for", () => pw.call("browser_wait_for", { text: "Product Status" }));
    await m.call("browser_snapshot", () => pw.call("browser_snapshot", {}));
  },
  async cve_api(pw, m) {
    await m.call("browser_navigate", () => pw.call("browser_navigate", { url: CVE }));
    await m.call("browser_wait_for", () => pw.call("browser_wait_for", { text: "Product Status" }));
    const list = await m.call("browser_network_requests", () => pw.call("browser_network_requests", { static: false, filter: "cveawg.*api/cve/" }));
    const index = Number(list.match(/(\d+)\. \[GET\] https:\/\/cveawg\.mitre\.org\/api\/cve\/CVE/)?.[1]);
    await m.call("browser_network_request", () => pw.call("browser_network_request", { index, part: "response-body" }));
  },
};

// ---------- 直连 CDP ----------
const CDP_TASKS = {
  async nvd(b, m) { await m.call("open", () => b.open(NVD, { waitFor: "Base Score" })); },
  async github(b, m) {
    const v = await m.call("open", () => b.open(GH));
    const id = v.elements.find((e) => / -> \/advisories\/GHSA-/.test(e))?.match(/^\[(\d+)\]/)[1];
    if (!id) throw new Error("视图里没找到 advisory 链接");
    await m.call("act", () => b.act([{ click: id }]));
  },
  async cve_dom(b, m) { await m.call("open", () => b.open(CVE)); },
  async cve_api(b, m) {
    await m.call("open", () => b.open(CVE));
    await m.call("network", () => b.network("cveawg.*api/cve/CVE"));
  },
};

async function micro(name, fn, n = 20) {
  const ts = [];
  for (let i = 0; i < n; i++) { const t = performance.now(); await fn(); ts.push(performance.now() - t); }
  ts.sort((a, b) => a - b);
  return { name, n, p50: +ts[n >> 1].toFixed(2), p90: +ts[Math.floor(n * 0.9)].toFixed(2) };
}

const median = (xs) => [...xs].sort((a, b) => a - b)[xs.length >> 1];

async function main() {
  const results = { when: new Date().toISOString(), rounds: ROUNDS, runs: [], micro: [] };
  let t = performance.now();
  const pw = await pwStart();
  results.startup = { playwright_mcp_ms: Math.round(performance.now() - t) };
  t = performance.now();
  const cdp = await CdpBrowser.launch();
  results.startup.cdp_ms = Math.round(performance.now() - t);

  for (let r = 1; r <= ROUNDS; r++) {
    for (const task of Object.keys(CHECKS)) {
      for (const [impl, tasks, ctx] of [["playwright-mcp", PW_TASKS, pw], ["cdp", CDP_TASKS, cdp]]) {
        const m = new Meter();
        let error = null;
        try { await tasks[task](ctx, m); } catch (e) { error = String(e.message || e); }
        const s = m.summary(CHECKS[task]);
        results.runs.push({ round: r, task, impl, ...s, ok: s.ok && !error, error });
        console.log(r, task.padEnd(8), impl.padEnd(15), `${s.calls} calls`, `${s.tokens} tok`, `${s.ms} ms`, s.ok && !error ? "OK" : "FAIL " + (error || ""));
      }
    }
  }

  // 协议往返微基准：同一页面上 20 次读取 document.title
  await pw.call("browser_navigate", { url: "https://example.com/" });
  await cdp.open("https://example.com/");
  results.micro.push(await micro("playwright-mcp browser_evaluate", () => pw.call("browser_evaluate", { function: "() => document.title" })));
  results.micro.push(await micro("cdp Runtime.evaluate", () => cdp.evaluate("document.title")));
  console.log(results.micro);

  // 汇总：每个 任务×实现 取中位数
  const table = [];
  for (const task of Object.keys(CHECKS)) {
    for (const impl of ["playwright-mcp", "cdp"]) {
      const rs = results.runs.filter((x) => x.task === task && x.impl === impl);
      table.push({ task, impl, calls: median(rs.map((x) => x.calls)), tokens: median(rs.map((x) => x.tokens)),
        ms: median(rs.map((x) => x.ms)), success: `${rs.filter((x) => x.ok).length}/${rs.length}` });
    }
  }
  results.table = table;
  console.table(table);
  mkdirSync("results", { recursive: true });
  writeFileSync("results/results.json", JSON.stringify(results, null, 2));
  await pw.c.close();
  await cdp.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
