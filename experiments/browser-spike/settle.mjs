// Playwright MCP 每次调用约 500ms 的来源核查：把 --timeout-settle 设为 0 再测
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
for (const settle of [null, "0"]) {
  const args = ["node_modules/@playwright/mcp/cli.js", "--headless", "--browser", "chrome", "--isolated", "--output-dir", "/tmp/pwmcp-spike"];
  if (settle !== null) args.push("--timeout-settle", settle);
  const c = new Client({ name: "s", version: "0" }); await c.connect(new StdioClientTransport({ command: "node", args }));
  await c.callTool({ name: "browser_navigate", arguments: { url: "https://example.com/" } });
  const ts = [];
  for (let i = 0; i < 20; i++) { const t = performance.now(); await c.callTool({ name: "browser_evaluate", arguments: { function: "() => document.title" } }); ts.push(performance.now() - t); }
  ts.sort((a, b) => a - b);
  console.log(`timeout-settle=${settle ?? "默认"}  p50=${ts[10].toFixed(1)}ms`);
  await c.close();
}
