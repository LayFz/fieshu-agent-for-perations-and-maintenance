"""在工作机上执行一次操作并等待结果（harness 侧）。

工具调用 → 入队 → 工作机 runner 长轮询领取 → 执行 → 回传 → 唤醒等待者。
等待期间检查任务取消；工作机离线时立即失败，不白等。结果是工作机产出的外部内容，由调用方按不可信处理。
"""
import threading
import time

from core import boxstore

OPS = {"shell", "files.read", "files.write", "browser.open", "browser.act", "browser.network",
       "desktop.tree", "desktop.act", "desktop.launch"}
_cond = threading.Condition()


class BoxError(Exception):
    pass


def notify():
    """runner 回传结果或有新操作时调用，唤醒同进程里的等待者与长轮询。"""
    with _cond:
        _cond.notify_all()


def wait(timeout):
    with _cond:
        _cond.wait(timeout)


def call(op, args, runner="default", timeout=120, check_active=None):
    if op not in OPS:
        raise BoxError(f"未知工作机操作 {op}")
    info = boxstore.get_runner(runner)
    if not info or not info["enabled"]:
        raise BoxError(f"没有登记名为「{runner}」的工作机，请在后台「连接凭证 → 工作机」添加")
    if not info["online"]:
        raise BoxError(f"工作机「{runner}」不在线（超过 {boxstore.ONLINE_SECONDS}s 没有心跳），请检查 runner 进程")
    task_id = None
    if check_active:
        from . import tasks
        task_id = (tasks.current() or {}).get("id")
    op_id = boxstore.enqueue(runner, op, args, timeout, task_id)
    notify()
    deadline = time.monotonic() + timeout + 5
    try:
        while time.monotonic() < deadline:
            row = boxstore.get(op_id)
            if row["status"] in ("done", "failed", "expired"):
                result = row["result"] or {"error": "工作机没有返回结果"}
                if row["status"] == "expired" and "error" not in result:
                    result = {"error": "工作机没有及时领取或完成这次操作"}
                return result
            if check_active:
                check_active()
            wait(0.5)
        boxstore.abandon(op_id, "等待工作机结果超时")
        return {"error": f"等待工作机结果超时（{timeout}s）"}
    except BaseException:
        boxstore.abandon(op_id, "调用方已停止等待（任务取消或进程退出）")
        raise
