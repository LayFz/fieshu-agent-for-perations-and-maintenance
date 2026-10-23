# 工作机桌面原型

把一台带桌面的机器以结构化工具暴露给 agent：命令行、文件、有头真实浏览器、桌面图形程序，并能用 noVNC 实时观看和接管。设计背景见 [worker-agent-design](../../docs/worker-agent-design.md)。**本目录只是本机验证用的原型**，还没有接入 harness 的任务协议。

## 组成

| 层 | 实现 | 给 agent 的接口 |
|---|---|---|
| 命令行与文件 | `bash -lc`，工作目录 `/work`，超时与输出上限 | `POST /shell`、`GET/PUT /files` |
| 浏览器 | Playwright 驱动有头 Chromium（固定用户配置，保留登录与 Cookie） | `POST /browser/open`、`/browser/act`、`/browser/network`、`GET /browser/screenshot` |
| 桌面 | Xvfb + openbox；X11 截屏；xdotool 输入；AT-SPI 无障碍树 | `GET /desktop/tree`、`POST /desktop/act`、`GET /desktop/screenshot`、`POST /desktop/launch` |
| 观看与接管 | x11vnc（仅容器内回环）+ noVNC | 浏览器打开 `http://127.0.0.1:16080/vnc.html` |

浏览器返回的是「精简视图」：标题、主体正文、可见可交互元素（带短 id，`act` 用 id 点击或输入）、页面调用过的 JSON 接口；遇到人机验证页时带 `needs_human`，**不尝试绕过**，由人在 noVNC 里处理。桌面工具优先读 AT-SPI 结构树（角色、名称、屏幕坐标），截图只作兜底。

## 运行

```bash
docker build -t feishu-worker-desktop:dev worker/desktop
docker run -d --name feishu-worker-desktop --shm-size=1g -e RUNNER_TOKEN=<随机令牌> \
  -p 127.0.0.1:16080:6080 -p 127.0.0.1:18790:8790 feishu-worker-desktop:dev
RUNNER_URL=http://127.0.0.1:18790 RUNNER_TOKEN=<同上> python worker/desktop/verify.py --keep-shots /tmp/shots
```

构建默认用清华 Ubuntu ports 镜像源（`--build-arg APT_MIRROR=...` 可改）；镜像约 3GB。

## 验证结果（2026-10-23，本机 arm64，经本机代理出网）

| 任务 | 结果 | 耗时 | 回传约 token |
|---|---|---:|---:|
| shell：克隆 markupsafe 并读版本号 | ✓ | 2.6s | 69 |
| 浏览器：GitHub Advisory 搜索框输入 → 回车 → 点开第一条 → 读修复版本 | ✓ | 8.2s（3 次调用） | 8,553 |
| 浏览器：cve.org SPA 正文（评分 9.8、受影响版本 1.11.4） | ✓ | 4.8s | 1,745 |
| 浏览器：取 cve.org 背后的 JSON 接口 | ✓ | 6ms | 1,525 |
| 反爬：npmjs 包页 | 前几次出现 Cloudflare 人机验证（已识别并标记转人工，**没有读到内容**）；之后同一浏览器配置直接放行 | 18s | 141 |
| 反爬：nowsecure.nl | ✓（但 curl 也是 200，这个站当前不区分，不能证明什么） | 5.9s | 65 |
| 桌面：用 AT-SPI 找到 Mousepad 编辑区 → xdotool 输入中文 → Ctrl+S → 文件内容核对 | ✓ | 1.7s | 355 |
| 桌面：按名字找到 File 菜单并点开，读出菜单项 | ✓ | — | 908 |
| 桌面：浏览器也出现在 AT-SPI 树（可读到网页标题、链接） | ✓ | — | 8 |
| 桌面：整屏截图 | ✓ | 0.2s | — |

同一 URL，curl 拿到 npmjs 的 403；真实浏览器要么直接拿到页面，要么停在人机验证并交给人。

## 发现

1. **结构化取法是准的**：浏览器按元素 id 操作、桌面按 AT-SPI 坐标操作，没有一次点偏；不需要看图猜坐标。
2. **人机验证无法也不应自动化**：Cloudflare Turnstile 复选框出现时，正确做法是标记 `needs_human`、发飞书卡片、人在 noVNC 里点一下。固定用户配置会保留通过后的状态，后续访问通常直接放行。能走官方 API 的（npm 有 registry API）优先走 API。
3. **AT-SPI 要两处开关**：会话总线上 `org.a11y.Status.IsEnabled=true`，以及 Chromium 进程的 `ACCESSIBILITY_ENABLED=1`，否则无障碍树里看不到浏览器。
4. **容器里的 Chromium 关闭了自身沙箱**：Docker 默认 seccomp 不允许非特权用户命名空间，Playwright 以 `--no-sandbox` 启动 Chromium，网页漏洞会直接落到容器里。正式工作机要么给容器配允许 userns 的 seccomp 配置让 Chromium 沙箱生效，要么用 gVisor，且容器本身仍是边界（无凭据、不能进内网）。
5. 浏览器顶部有「受自动化软件控制」提示条：这是 Playwright 自带 Chromium 的正常现象，不影响使用；我们不做隐藏自动化特征的处理。

## 下一步

1. runner 改为主动连控制面拉任务（设计稿 §4），harness 增加 `box_shell / box_browser / box_desktop` 工具。
2. 接一个真实模型，用同样的任务看实际步数和成功率（需要使用模型额度，单独确认）。
3. 人机验证 → 飞书卡片 → noVNC 接管 → 继续任务的完整链路。
4. 正式工作机：独立机器、Chromium 沙箱或 gVisor、出境代理白名单、noVNC 加鉴权经后台反代。
