"""定时任务调度线程：每 30s 检查到期任务 → 用对应应用跑指令 → 把结果发回原群。

任务持久化在 store.schedules；进程重启后自动重载，过期任务不补跑（reschedule_overdue）。
engine/bot 延迟导入，避免与 feishu.bot ←→ llm.engine 形成循环依赖。
"""
import threading
import time
import traceback

from core import store
from harness import runs, skills, workspace
from sec import collector

_started = False
_PURGE_EVERY = 6 * 3600     # 运行记录（runs/events）过期清理间隔


def _fire(sc):
    from llm.engine import run as llm_run
    from feishu import bot
    app = store.get_app(sc["app_id"])
    if not app or not app["enabled"]:
        return
    try:
        ans = llm_run(app, sc["chat_id"], sc["instruction"], trigger="schedule")
        bot.post(app, sc["chat_id"], ans)
        print(f"[定时任务 {sc['id']}] {sc.get('title')!r} 已执行并回群", flush=True)
    except Exception as e:
        print(f"[定时任务 {sc['id']}] 执行出错:", e, flush=True)
        traceback.print_exc()


def _loop():
    last_purge = 0.0
    last_skill_check = 0.0
    last_ws_cleanup = 0.0
    while True:
        try:
            now = time.time()
            for sc in store.due_schedules(now):
                store.mark_schedule_ran(sc["id"], now)   # 先标记下次时间，避免本轮重复触发
                threading.Thread(target=_fire, args=(sc,), daemon=True).start()
            if now - last_skill_check >= 60:              # 技能源 / 情报源到期就同步（另起线程，不挡定时任务）
                last_skill_check = now
                threading.Thread(target=skills.sync_due, name="skill-sync", daemon=True).start()
                threading.Thread(target=collector.sync_due, name="intel-sync", daemon=True).start()
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
