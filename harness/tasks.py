"""服务端任务：有界 worker、持久 checkpoint、异步等待、取消与 outbox。

只依赖 core。上层注册模型执行器和飞书投递器；测试可注入纯内存实现。
"""
import contextvars
import copy
import logging
import os
import threading
import time
import uuid

from core import store, taskstore

log = logging.getLogger(__name__)
_current = contextvars.ContextVar("harness_task", default=None)
_executor = None
_sender = None
_stop = threading.Event()
_threads = []
_lifecycle_lock = threading.Lock()
_last_tick = 0.0
LEASE_SECONDS = 90


class TaskSuspended(Exception):
    def __init__(self, status="waiting_approval", reason=""):
        if status not in ("waiting_approval", "waiting_input"):
            raise ValueError("不支持的等待状态")
        self.status, self.reason = status, reason
        super().__init__(reason)


class TaskUncertain(Exception):
    """副作用可能已经发生，等待核对，不可自动重试。"""


class TaskCancelled(Exception):
    pass


class TaskIncomplete(Exception):
    """验收未通过或预算达到上限，保留阶段性答案等待显式续做。"""


def current():
    return _current.get()


def check_active():
    task = current()
    if task is None:
        return None
    row = taskstore.get(task["id"])
    if not row or row["cancel_requested"]:
        raise TaskCancelled("用户已请求取消任务。")
    app = store.get_app(task["app_id"])
    if not app or not app.get("enabled"):
        raise TaskCancelled("应用已停用，停止执行任务。")
    from . import access
    access.actor(app, task["chat_id"], task.get("speaker_open_id") or "",
                 system=task["trigger"] in ("schedule", "job", "webhook"))
    try:
        return taskstore.assert_owned(task["id"], task["lease_owner"], task["lease_epoch"])
    except taskstore.LeaseLost as exc:
        raise TaskCancelled(str(exc)) from exc


def checkpoint(data):
    task = current()
    if task is None:
        return
    check_active()
    taskstore.checkpoint(task["id"], task["lease_owner"], task["lease_epoch"], data)
    task["checkpoint"] = copy.deepcopy(data)


def load_checkpoint():
    task = current()
    return copy.deepcopy(task.get("checkpoint") or {}) if task else {}


def submit(app_id, chat_id, instruction, *, source="chat", event_id=None, speaker="",
           speaker_open_id="", workflow="auto", trigger="chat"):
    app = store.get_app(app_id)
    if not app or not app.get("enabled"):
        raise ValueError("应用不存在或已停用")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("任务内容不能为空")
    if len(instruction) > 100000:
        raise ValueError("任务内容过长")
    if not chat_id:
        raise ValueError("任务必须指定会话")
    if trigger not in ("chat", "proactive", "schedule", "job", "webhook", "delegate"):
        raise ValueError("未知任务触发方式")
    from . import access, workflows
    access.actor(app, chat_id, speaker_open_id, system=trigger in ("schedule", "job", "webhook"))
    workflow = workflows.resolve(workflow, instruction)
    return taskstore.submit(app_id, str(chat_id), instruction.strip(), source=source, event_id=event_id,
                           speaker=speaker, speaker_open_id=speaker_open_id, workflow=workflow, trigger=trigger)


def enqueue_schedule(sc, now=None):
    from . import access, workflows
    app = store.get_app(sc["app_id"])
    if not app or not app.get("enabled"):
        return None, False
    access.actor(app, sc["chat_id"], "", system=True)
    return taskstore.enqueue_schedule(sc, now, workflow=workflows.resolve("auto", sc["instruction"]))


get = taskstore.get
list_tasks = taskstore.list_tasks
cancel = taskstore.cancel
resume = taskstore.resume
wake = taskstore.wake


def register_executor(fn):
    global _executor
    _executor = fn


def register_sender(fn):
    global _sender
    _sender = fn


def _heartbeat(task, done):
    try:
        while not done.wait(max(0.1, LEASE_SECONDS / 3)):
            try:
                if not taskstore.heartbeat(task["id"], task["lease_owner"], task["lease_epoch"], LEASE_SECONDS):
                    return
            except Exception:
                log.exception("任务心跳失败 task=%s", task["id"])
                return
    finally:
        _close_connection()


def _close_connection():
    conn = getattr(store._local, "conn", None)
    if conn is not None:
        conn.close()
        del store._local.conn


def _execute(task):
    token = _current.set(task)
    done = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(task, done), name="task-heartbeat", daemon=True)
    heartbeat.start()
    status, result, error = "failed", None, ""
    try:
        check_active()
        result = _executor(task)
        check_active()
        status = "completed"
    except TaskSuspended as exc:
        status, error = exc.status, exc.reason
    except TaskUncertain as exc:
        status, error = "needs_attention", str(exc)
    except TaskIncomplete as exc:
        status, error = "needs_attention", str(exc)
        result = load_checkpoint().get("final_answer")
    except TaskCancelled as exc:
        status = "needs_attention" if taskstore.has_uncertain(task["id"]) else "cancelled"
        error = str(exc)
    except Exception as exc:
        status = "needs_attention" if taskstore.has_uncertain(task["id"]) else "failed"
        error = f"{type(exc).__name__}: {exc}"
        log.exception("任务执行失败 task=%s", task["id"])
    finally:
        done.set()
        heartbeat.join(timeout=1)
        _current.reset(token)
    try:
        taskstore.finish(task["id"], task["lease_owner"], task["lease_epoch"], status,
                         result=result, error=error)
    except taskstore.LeaseLost:
        # 已被恢复器接管时不能让旧执行器覆盖新状态。
        log.warning("任务结果被 fencing 拒绝 task=%s", task["id"])
    return taskstore.get(task["id"])


def dispatch_notification():
    if _sender is None:
        return False
    owner = "outbox-" + uuid.uuid4().hex
    item = taskstore.claim_notification(owner)
    if item is None:
        return False
    task = taskstore.get(item["task_id"])
    if task is None:
        taskstore.finish_notification(item["id"], owner)
        return True
    # 投递器可使用稳定 notification_id 作为下游幂等标识。
    task["notification_id"] = item["id"]
    try:
        _sender(task, item["text"])
    except Exception as exc:
        taskstore.finish_notification(item["id"], owner, error=f"{type(exc).__name__}: {exc}")
        log.warning("通知暂未投递，稍后重试 task=%s", task["id"])
    else:
        taskstore.finish_notification(item["id"], owner)
    return True


def tick():
    """领取并执行一个任务，再投递一个通知；本地测试无需启动后台线程。"""
    global _last_tick
    taskstore.recover_expired(max_attempts=int(os.getenv("HARNESS_MAX_ATTEMPTS", "8")))
    from . import approval
    approval.expire_due()
    taskstore.wake_decided_approvals()
    _last_tick = time.time()
    delivered = dispatch_notification()  # 回执先投递；调用者仍只需持久提交，不等待飞书网络。
    task = taskstore.claim("worker-" + uuid.uuid4().hex, LEASE_SECONDS) if _executor else None
    if task is not None:
        _execute(task)
    delivered = dispatch_notification() or delivered
    _last_tick = time.time()
    return bool(task or delivered)


def _loop():
    try:
        while not _stop.is_set():
            try:
                busy = tick()
            except Exception:
                log.exception("任务 worker 异常")
                busy = False
            if not busy:
                _stop.wait(0.5)
    finally:
        _close_connection()


def start(workers=2):
    with _lifecycle_lock:
        if any(t.is_alive() for t in _threads):
            return
        if _executor is None:
            raise RuntimeError("尚未注册任务执行器")
        _threads.clear()
        _stop.clear()
        for number in range(max(1, min(int(workers), 8))):
            th = threading.Thread(target=_loop, name=f"harness-worker-{number}", daemon=True)
            _threads.append(th)
            th.start()


def stop(timeout=5):
    """停止领取并有限等待；仍运行的任务保留租约，重启后恢复，不伪称已取消。"""
    _stop.set()
    deadline = time.monotonic() + max(0, timeout)
    for th in list(_threads):
        th.join(timeout=max(0, deadline - time.monotonic()))


def health():
    return {"workers": sum(t.is_alive() for t in _threads), "stopping": _stop.is_set(),
            "last_tick": _last_tick, "executor_registered": _executor is not None}
