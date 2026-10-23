# 浏览器控制方式实验：Playwright MCP 与直连 CDP

日期：2026-10-23　代码：[experiments/browser-spike](../experiments/browser-spike)

## 结论

1. **协议层不是瓶颈。** 读一次 `document.title`：直连 CDP 中位数 0.3ms；Playwright MCP 默认 508ms，但把 `--timeout-settle` 设为 0 后只有 2ms。那 500ms 是 Playwright MCP 每次动作后的默认等待，不是 Playwright 不够原生。
2. **真正决定快慢和效果的是工具设计。** 按任务粒度设计的工具（一次返回精简正文、可交互元素和页面调用过的接口；一次执行多个动作）把调用次数减半，DOM 类任务的回传 token 减少 60%～80%。每次调用都是一轮模型往返，这比工具本身的毫秒级差别大两个数量级。
3. **最难的是「等页面就绪」。** 自己用 CDP 写等待逻辑，在 cve.org 上反而比 Playwright 慢（3.9s 对 1.9s）；NVD 两种方式都经常超过 20s 或渲染不出来。等待策略要按信号组合、可配置，最终还需要转人工。
4. **建议**：工作机守护进程**用 Playwright 库作为驱动**，处理 iframe、下载、弹窗、自动等待这些坑；需要底层能力时用它的 `CDPSession` 直接发 CDP 命令（网络截获、无障碍树、原生输入）；**不用 Playwright MCP 的通用工具集**，给模型的工具按任务自己设计。

## 方法

- 同一台 Mac（Chrome 无头模式，经本机代理出网），两种实现各自一个浏览器实例，跑 3 轮取中位数。
- 不调用模型。每个任务按「一个称职 agent 至少要做的工具调用」写成脚本，记录：调用次数、回传给模型的文本 token（cl100k 近似）、工具耗时、结果里是否包含目标事实。
- Playwright MCP 0.0.83：`browser_navigate` 的回包不内联页面快照（写到文件），agent 要看页面必须再调 `browser_snapshot`。
- 直连 CDP 原型（[cdp.mjs](../experiments/browser-spike/cdp.mjs)）：`open(url, {waitFor})`、`act([...])`、`network(filter)`。等待策略：load → DOM 连续 300ms 无变化且没有进行中的 XHR/Fetch，最多 4s。

| 任务 | 目标事实 |
|---|---|
| nvd | NVD 页面上 CVE-2025-1974 的 CVSS 9.8 CRITICAL（慢 SPA） |
| github | GitHub Advisory 搜 ingress-nginx，点开第一条，读严重级别和修复版本 |
| cve_dom | cve.org 记录页（Vue SPA）读评分、受影响版本、产品 |
| cve_api | 同上，但直接取页面背后的 JSON 接口 |

## 结果（3 轮中位数）

| 任务 | 实现 | 调用次数 | 回传 token | 工具耗时 | 成功 |
|---|---|---:|---:|---:|---:|
| github | Playwright MCP | 4 | 11,642 | 3.3s | 3/3 |
| github | 直连 CDP | 2 | 4,661 | 3.6s | 3/3 |
| cve_dom | Playwright MCP | 3 | 5,067 | 1.9s | 3/3 |
| cve_dom | 直连 CDP | 1 | 1,006 | 3.9s | 3/3 |
| cve_api | Playwright MCP | 4 | 1,442 | 1.6s | 3/3 |
| cve_api | 直连 CDP | 2 | 2,251 | 1.7s | 3/3 |
| nvd | Playwright MCP | 7 | 2,103 | 25.4s | 1/3 |
| nvd | 直连 CDP | 1 | 106 | 24.1s | 0/3 |

协议往返微基准（同一页面 20 次读取标题，中位数）：

| 方式 | 耗时 |
|---|---:|
| 直连 CDP `Runtime.evaluate` | 0.3ms |
| Playwright MCP `browser_evaluate`（默认） | 508ms |
| Playwright MCP `browser_evaluate`（`--timeout-settle 0`） | 2.0ms |

浏览器冷启动：Playwright MCP 1.3s，直连 CDP 0.6s。

### 估算的端到端耗时

按每轮模型往返 4s 估算（**假设值**，实际随模型、上下文长度在 2～8s 之间，token 越多越慢）：

| 任务 | Playwright MCP | 直连 CDP 任务级工具 |
|---|---:|---:|
| github | 4×4 + 3.3 ≈ 19s | 2×4 + 3.6 ≈ 12s |
| cve_dom | 3×4 + 1.9 ≈ 14s | 1×4 + 3.9 ≈ 8s |
| cve_api | 4×4 + 1.6 ≈ 18s | 2×4 + 1.7 ≈ 10s |

## 观察

- **快照体积**：Playwright 的无障碍树快照包含整页导航、页脚和每个元素的 ref，GitHub 列表页一次就有 8,000 token；任务级视图只保留主体正文和可见的可交互元素。
- **调用次数**：Playwright MCP 一个动作一次调用，导航后还要单独拿快照；SPA 还要多一次 `wait_for`。任务级工具把「打开 + 等就绪 + 看页面」合成一次。
- **接口截获**：两边都能拿到页面背后的 JSON；直连 CDP 把接口列表随页面一起返回，少一次「列请求」的调用。`cve_api` 上 CDP 回传更多 token，是因为原型返回了 6,000 字的原始 JSON，可调。
- **等待**：Playwright MCP 的 `wait_for` 默认 5s 超时，NVD 需要 agent 连续重试 5 次（每次一轮模型往返）。自研的等待在有持续后台请求的站点会拖到上限。没有通用正确的「页面就绪」信号。
- **慢站点**：NVD 在两种方式下都不稳定（经常超过 20s 仍停在「Loading CVE Information」）。这类站点应优先走 API（NVD 有官方 JSON API），浏览器只作兜底。

## 局限

- 没有接入真实模型：调用次数是「理想 agent」的下限，真实模型会多走弯路；端到端耗时是估算。
- 只有 4 个只读任务，未覆盖表单、登录态、多标签、文件下载、跨站 iframe；这些正是自研 CDP 的主要成本。
- token 用 cl100k 近似，不同模型的分词不同；单机单网络环境。

## 下一步

1. 工作机守护进程原型：Playwright 库驱动 + `CDPSession`，暴露 `open / act / network / screenshot`，按任务保持浏览器上下文。
2. 等待策略做成多信号组合（load、DOM 稳定、数据请求、可选的等待文字），并把超时转成「需要人工 / 换 API」的明确结果。
3. 接入一个真实模型，用同样的任务对比实际步数、token 和端到端耗时（需要使用模型额度，单独确认后再跑）。
4. 增加表单、登录态、ArgoCD 这类后台 SPA 的任务。

## 复现

```bash
cd experiments/browser-spike && npm install && ROUNDS=3 node bench.mjs
```

结果写入 `experiments/browser-spike/results/results.json`；`node settle.mjs` 复现 `--timeout-settle` 的对比。
