"""在 macOS 登录会话中运行真正的 feishu-agent 服务。

安装的 plist 留在项目 data/service 下；launchd KeepAlive 负责进程退出后拉起。
Linux/服务器请使用 deploy/compose.yml 的 restart: unless-stopped。
"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.upmore.feishu-agent"
DOMAIN = f"gui/{os.getuid()}"
SERVICE_DIR = ROOT / "data" / "service"
PLIST = SERVICE_DIR / "feishu-agent.plist"


def _run(*args):
    return subprocess.run(["launchctl", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def status():
    result = _run("print", f"{DOMAIN}/{LABEL}")
    if result.returncode:
        print("本机服务：未注册")
        return False
    line = next((line.strip() for line in result.stdout.splitlines() if "state =" in line), "已注册")
    pid = next((line.strip() for line in result.stdout.splitlines() if "pid =" in line), "")
    print("本机服务：", line, pid)
    return True


def install():
    if sys.platform != "darwin":
        raise SystemExit("本地 launchd 服务仅支持 macOS；Linux 使用 deploy/compose.yml")
    python = ROOT / ".venv-runtime" / "bin" / "python"
    if not python.exists() or not (ROOT / "config.yaml").is_file():
        raise SystemExit("需要仓库内 .venv-runtime Python 及已有 config.yaml")
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "app.main"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "EnvironmentVariables": {"WEB_HOST": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1",
                                 "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(SERVICE_DIR / "stdout.log"),
        "StandardErrorPath": str(SERVICE_DIR / "stderr.log"),
    }
    PLIST.write_bytes(plistlib.dumps(data))
    PLIST.chmod(0o600)
    # 仅替换本项目的 label，不影响用户其他服务。
    _run("bootout", f"{DOMAIN}/{LABEL}")
    # bootout 异步释放旧服务；立刻 bootstrap 偶发 I/O error 5。
    for attempt in range(4):
        result = _run("bootstrap", DOMAIN, str(PLIST))
        if not result.returncode:
            break
        if attempt < 3:
            time.sleep(0.5 * (attempt + 1))
    if result.returncode:
        raise SystemExit("注册本机服务失败：" + result.stderr.strip()[:300])
    print("已注册本机服务；退出后由 launchd 自动重启。")
    status()


def uninstall():
    result = _run("bootout", f"{DOMAIN}/{LABEL}")
    if result.returncode:
        raise SystemExit("本机服务未运行或卸载失败：" + result.stderr.strip()[:300])
    print("本机服务已停止并取消注册；SQLite 巡检记录保留。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    command = parser.parse_args().action
    {"install": install, "status": status, "uninstall": uninstall}[command]()
