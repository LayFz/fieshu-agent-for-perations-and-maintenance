"""run 生命周期 + 事件日志。

一次 engine.run = 一个 run（写 runs 表）；它经历的每一步作为事件只追加写入 events 表，
供后台「运行记录」页审计回放。事件类型是闭集（EVENT_TYPES），payload 为 JSON。

约定：
  - 写事件是 best-effort：落库失败只打日志，绝不让对话本身失败。
  - tool/call 必须先于工具执行写入（进程中途崩了也能看到它当时打算做什么）。
  - 进程启动时 abort_stale() 把上次遗留的 running 标为 aborted。
当前 run 用 contextvar 传递（后续工具管道/审批从这里取，不用层层传参）。
"""
import contextvars
import threading
import time
import uuid

from core import store

TRIGGERS = ("chat", "proactive", "schedule", "job", "webhook", "delegate")
STATUSES = ("running", "completed", "silent", "max-steps", "error", "aborted")
EVENT_TYPES = (
    "run/start", "user/message", "assistant/message",
    "tool/call", "tool/result", "run/end",
)

_RETENTION_DAYS = 90
_current = contextvars.ContextVar("harness_run", default=None)


class Run:
    """一个进行中的 run。emit() 线程安全，seq 在 run 内单调递增。"""

    def __init__(self, run_id, app_id, chat_id, trigger, depth=0):
        self.id = run_id
        self.app_id = app_id
        self.chat_id = chat_id
        self.trigger = trigger
        self.depth = depth
        self.total_tokens = 0
        self.started = time.time()
        self._seq = 0
        self._lock = threading.Lock()
        self._token = None           # contextvar 复位用

    def emit(self, etype, payload=None, call_id=None, tool=None):
        if etype not in EVENT_TYPES:
            raise ValueError(f"unknown event type {etype}")
        with self._lock:
            self._seq += 1
            seq = self._seq
        try:
            store.append_event(self.id, seq, etype, payload, call_id=call_id, tool=tool)
        except Exception as e:
            print(f"[run {self.id[:8]}] 写事件 {etype} 失败:", e, flush=True)

    def add_tokens(self, n):
        self.total_tokens += int(n or 0)


def start(app_id, chat_id, trigger, speaker_open_id="", parent=None):
    """开一个 run 并设为当前 run。trigger 取 TRIGGERS 之一。"""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger}")
    depth = (parent.depth + 1) if parent else 0
    run = Run(uuid.uuid4().hex, app_id, chat_id, trigger, depth)
    try:
        store.create_run(run.id, app_id, chat_id, trigger, speaker_open_id,
                         parent_run_id=parent.id if parent else None, depth=depth,
                         approval_policy="ask" if trigger == "chat" else "never")
    except Exception as e:
        print(f"[run {run.id[:8]}] 建 run 失败:", e, flush=True)
    run._token = _current.set(run)
    return run


def end(run, status, reason=""):
    """结束 run：写 run/end 事件 + 更新 runs 行 + 复位当前 run。"""
    if status not in STATUSES:
        status = "error"
    run.emit("run/end", {"status": status, "reason": reason,
                         "duration_ms": int((time.time() - run.started) * 1000),
                         "total_tokens": run.total_tokens})
    try:
        store.end_run(run.id, status, run.total_tokens)
    except Exception as e:
        print(f"[run {run.id[:8]}] 结束 run 失败:", e, flush=True)
    if run._token is not None:
        try:
            _current.reset(run._token)
        except ValueError:           # 跨上下文结束（不应发生），退化为清空
            _current.set(None)
        run._token = None


def current():
    return _current.get()


def abort_stale():
    """进程启动时调用：上次遗留的 running 一律 aborted。"""
    n = store.abort_running_runs()
    if n:
        print(f"★ 已把 {n} 个中断的 run 标为 aborted", flush=True)
    return n


def purge_old(days=_RETENTION_DAYS):
    return store.purge_runs(time.time() - days * 86400)
