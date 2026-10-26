#!/bin/bash
# 启动顺序：会话总线 → 虚拟屏幕 → 窗口管理器 → 无障碍总线 → VNC/noVNC → runner
set -e
export DISPLAY=:1
eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS
Xvfb :1 -screen 0 1440x900x24 -nolisten tcp &
for i in $(seq 50); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
openbox &
/usr/libexec/at-spi-bus-launcher --launch-immediately &
# 打开全局无障碍开关：Chromium 启动时据此决定是否接入 AT-SPI（否则桌面无障碍树里看不到浏览器）
for i in $(seq 50); do
  dbus-send --session --dest=org.a11y.Bus /org/a11y/bus org.freedesktop.DBus.Properties.Set \
    string:org.a11y.Status string:IsEnabled variant:boolean:true 2>/dev/null && break; sleep 0.1
done
# VNC 只监听容器内回环，由 noVNC（websockify）对外；原型不设口令，端口只发布到宿主 127.0.0.1
x11vnc -display :1 -forever -shared -nopw -localhost -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
exec /opt/venv/bin/uvicorn --app-dir /opt/runner runner:app --host 0.0.0.0 --port 8790 --log-level warning
