"""受控 Docker 测试执行器：只读输入快照 + 容器内临时工作区，绝不回退主机 shell。

管理员必须预先加载可信镜像并设置 HARNESS_SANDBOX_IMAGE。Docker 客户端仅在控制面使用；
测试容器没有 daemon socket、控制面配置或凭据，网络关闭，所有可写空间都有大小限制。
"""
import json
import os
import selectors
import shutil
import subprocess
import time
import uuid
from pathlib import Path

OUTPUT_LIMIT = 64 * 1024


class SandboxError(Exception):
    pass


class SandboxUncertain(SandboxError):
    """无法确认容器已经停止，不能声称取消成功或盲目重试。"""


def image_name():
    return os.environ.get("HARNESS_SANDBOX_IMAGE", "").strip()


def _docker(args, timeout=15):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxError(f"Docker 沙箱不可用：{type(e).__name__}") from e


def resolve_image():
    image = image_name()
    if not image:
        raise SandboxError("未配置 HARNESS_SANDBOX_IMAGE，代码测试已拒绝；请管理员准备本地沙箱镜像。")
    if image.startswith("-") or any(c.isspace() for c in image):
        raise SandboxError("沙箱镜像名称不合法。")
    if not shutil.which("docker"):
        raise SandboxError("控制面未安装 Docker 客户端，代码测试已拒绝。")
    result = _docker(["image", "inspect", image])
    if result.returncode:
        raise SandboxError("Docker 不可访问或指定沙箱镜像尚未本地加载；不会自动拉取镜像。")
    try:
        info = json.loads(result.stdout)[0]
        resolved = info["Id"]
        if not resolved.startswith("sha256:"):
            raise ValueError("image id")
        if info.get("Config", {}).get("Volumes"):
            raise SandboxError("沙箱镜像不能声明 VOLUME；可写空间必须使用有上限的 tmpfs。")
        return resolved
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise SandboxError("无法校验沙箱镜像。") from e


def _create_args(name, source, image, command):
    # 输入目录由宿主准备，容器永不写它；测试对副本的改动在容器销毁时消失。
    bootstrap = 'cp -R /input/. /workspace/ && exec /bin/sh -c "$1"'
    return ["create", "--pull=never", "--name", name,
            "--label", "feishu-agent.sandbox=true", "--network=none", "--read-only",
            "--user=65532:65532", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=128",
            "--init", "--ipc=private", "--log-driver=none", "--ulimit", "nofile=1024:1024",
            "--mount", f"type=bind,src={source},dst=/input,readonly",
            "--tmpfs", "/workspace:rw,nosuid,nodev,size=512m,mode=1777",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--workdir=/workspace", "--env", "HOME=/tmp", "--env", "CI=1",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint=/bin/sh",
            image, "-c", bootstrap, "harness-test", command]


def _remove(name):
    try:
        result = _docker(["rm", "--force", name])
        return result.returncode == 0 or "No such container" in (result.stderr or "")
    except SandboxError:
        return False


def run(source, command, *, timeout=600, check_active=None):
    """source 必须是无符号链接的独立快照目录。仅返回有界输出和实际退出状态。"""
    source = Path(source).resolve(strict=True)
    if not source.is_dir() or "," in str(source):
        raise SandboxError("沙箱输入目录不合法。")
    if not command or not isinstance(command, str):
        raise SandboxError("测试命令不能为空。")
    timeout = max(1, min(int(timeout), 600))
    image = resolve_image()
    name = "feishu-test-" + uuid.uuid4().hex
    started = time.monotonic()
    created = False
    proc = None
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total = {"stdout": 0, "stderr": 0}
    timed_out = False
    pending_error = None
    return_code = None
    try:
        if check_active:
            check_active()
        created = True  # 即使 CLI 超时，daemon 也可能已创建；始终按固定名字清理。
        result = _docker(_create_args(name, source, image, command))
        if result.returncode:
            raise SandboxError("无法创建隔离测试容器；请检查 Docker 权限和资源配置。")
        proc = subprocess.Popen(["docker", "start", "--attach", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            if check_active:
                check_active()
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
            for key, _mask in selector.select(timeout=0.2):
                block = os.read(key.fileobj.fileno(), 8192)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                label = key.data
                total[label] += len(block)
                output[label].extend(block)
                if len(output[label]) > OUTPUT_LIMIT:
                    del output[label][:-OUTPUT_LIMIT]
        if not timed_out:
            proc.wait(timeout=5)
            # docker start --attach 的退出码不保证等于容器退出码；读取 daemon 的实际状态。
            state = _docker(["inspect", "--format", "{{json .State}}", name])
            if state.returncode:
                raise SandboxUncertain("测试容器状态读取失败，执行结果待核对。")
            data = json.loads(state.stdout)
            if data.get("Running") or data.get("Status") in ("created", "restarting"):
                raise SandboxUncertain("测试容器未确认退出，执行结果待核对。")
            return_code = int(data["ExitCode"])
    except BaseException as e:
        pending_error = e
    finally:
        selector.close()
        if created and not _remove(name):
            pending_error = SandboxUncertain(f"无法确认测试容器 {name} 已销毁，请管理员核对；禁止自动重试。")
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
    if pending_error:
        raise pending_error
    return {"command": command, "image": image, "exit_code": return_code,
            "passed": return_code == 0 and not timed_out, "timed_out": timed_out,
            "stopped": True, "duration_s": round(time.monotonic() - started, 1),
            "stdout_tail": output["stdout"].decode("utf-8", "replace"),
            "stderr_tail": output["stderr"].decode("utf-8", "replace"),
            "output_truncated": any(total[k] > OUTPUT_LIMIT for k in total)}
