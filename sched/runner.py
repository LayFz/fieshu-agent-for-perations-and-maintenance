"""定时任务调度线程：每 30s 检查到期任务 → 用对应应用跑指令 → 把结果发回原群。

任务持久化在 store.schedules；进程重启后自动重载，过期任务不补跑（reschedule_overdue）。
engine/bot 延迟导入，避免与 feishu.bot ←→ llm.engine 形成循环依赖。
"""
import threading
import time
import traceback

from core import store

_started = False


def _fire(sc):
    from llm.engine import run as llm_run
    from feishu import bot
    app = store.get_app(sc["app_id"])
    if not app or not app["enabled"]:
        return
    try:
        ans = llm_run(app, sc["chat_id"], sc["instruction"])
        bot.post(app, sc["chat_id"], ans)
        print(f"[定时任务 {sc['id']}] {sc.get('title')!r} 已执行并回群", flush=True)
    except Exception as e:
        print(f"[定时任务 {sc['id']}] 执行出错:", e, flush=True)
        traceback.print_exc()


def _loop():
    while True:
        try:
            now = time.time()
            for sc in store.due_schedules(now):
                store.mark_schedule_ran(sc["id"], now)   # 先标记下次时间，避免本轮重复触发
                threading.Thread(target=_fire, args=(sc,), daemon=True).start()
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def run_now(sid):
    """后台「立即执行一次」用。"""
    sc = store.get_schedule(sid)
    if not sc:
        return False
    threading.Thread(target=_fire, args=(sc,), daemon=True).start()
    return True


def start():
    global _started
    if _started:
        return
    _started = True
    store.reschedule_overdue()
    threading.Thread(target=_loop, name="scheduler", daemon=True).start()
    print("★ 调度线程已启动（每 30s 检查定时任务）", flush=True)
