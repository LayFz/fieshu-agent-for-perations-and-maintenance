# 工作机桌面原型：虚拟桌面 + 有头 Chromium + AT-SPI 无障碍总线 + noVNC + runner。
# 只用于本机验证工具的准确性；正式工作机按 docs/worker-agent-design.md 部署。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 DISPLAY=:1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers NO_AT_BRIDGE=0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq tmux xvfb openbox x11vnc novnc websockify \
      xdotool imagemagick x11-utils dbus-x11 at-spi2-core python3-pyatspi gir1.2-atspi-2.0 \
      python3 python3-venv python3-pip mousepad xterm fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 构建期换国内镜像源（ports.ubuntu.com 在国内很慢）；正式工作机按所在网络选源
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports
RUN sed -i "s#http://ports.ubuntu.com/ubuntu-ports#${APT_MIRROR}#g" /etc/apt/sources.list.d/ubuntu.sources \
    && python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 playwright==1.55.0 \
    && /opt/venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行全部桌面进程
RUN useradd -m -s /bin/bash agent && mkdir -p /work && chown agent:agent /work
COPY --chown=agent:agent runner.py atspi_tree.py start.sh /opt/runner/
RUN chmod +x /opt/runner/start.sh

USER agent
WORKDIR /work
EXPOSE 6080 8790
CMD ["/opt/runner/start.sh"]
