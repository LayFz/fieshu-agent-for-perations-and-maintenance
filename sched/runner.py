"""每 30s 检查到期计划，事务入队并推进游标；任务内核执行和投递结果。

重启后到期计划合并补跑一次，避免重复执行所有错过的周期。
"""
import threading
import time
import traceback

from core import store
from harness import runs, skills, workspace, tasks
from sec import collector

_started = False
_stop = threading.Event()
_thread = None
_jobs = {}
_last_tick = 0.0
_PURGE_EVERY = 6 * 3600     # 运行记录（runs/events）过期清理间隔


def _fire(sc):
    app = store.get_app(sc["app_id"])
    if not app or not app["enabled"]:
        return
    try:
        tasks.submit(app["id"], sc["chat_id"], sc["instruction"], source="manual_schedule", trigger="schedule")
        print(f"[定时任务 {sc['id']}] {sc.get('title')!r} 已持久入队", flush=True)
    except Exception as e:
        print(f"[定时任务 {sc['id']}] 执行出错:", e, flush=True)
        traceback.print_exc()


def _loop():
    global _last_tick
    last_purge = 0.0
    last_skill_check = 0.0
    last_ws_cleanup = 0.0
    while not _stop.is_set():
        try:
            now = time.time()
            _last_tick = now
            for sc in store.due_schedules(now):
                tasks.enqueue_schedule(sc, now)
            if now - last_skill_check >= 60:              # 技能源 / 情报源到期就同步（另起线程，不挡定时任务）
                last_skill_check = now
                from ops import asset_scan
                for name, fn in (("skill-sync", skills.sync_due), ("intel-sync", collector.sync_due),
                                 ("asset-sync", asset_scan.sync_due)):
                    if name not in _jobs or not _jobs[name].is_alive():
                        _jobs[name] = threading.Thread(target=fn, name=name, daemon=True)
                        _jobs[name].start()
            if now - last_purge >= _PURGE_EVERY:          # 顺手清理过期运行记录
                last_purge = now
                n = runs.purge_old()
                if n:
                    print(f"[调度] 已清理 {n} 条过期运行记录", flush=True)
            if now - last_ws_cleanup >= 600:              # 闲置超过 TTL 的代码工作区删目录
                last_ws_cleanup = now
                n = workspace.cleanup_stale(now)
                if n:
                    print(f"[调度] 已清理 {n} 个闲置代码工作区", flush=True)
        except Exception:
            traceback.print_exc()
        _stop.wait(30)


def run_now(sid):
    """后台「立即执行一次」用。"""
    sc = store.get_schedule(sid)
    if not sc:
        return False
    _fire(sc)
    return True


def start():
    global _started, _thread
    if _thread and _thread.is_alive():
        return
    _started = True
    _stop.clear()
    # 启动时保留到期项，入队事务会合并补跑一次并推进 next_run。
    _thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    _thread.start()
    print("★ 调度线程已启动（每 30s 检查定时任务）", flush=True)


def stop(timeout=5):
    _stop.set()
    if _thread:
        _thread.join(timeout=timeout)


def health():
    return {"running": bool(_thread and _thread.is_alive()), "last_tick": _last_tick,
            "jobs": {name: thread.is_alive() for name, thread in _jobs.items()}}
