# app/scheduler.py
import os
import threading
import time
from datetime import datetime

from app.services.tournament_service import TournamentService


_started = False
_lock = threading.Lock()


def _scheduler_loop():
    interval = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))
    revenue_interval = int(
        os.environ.get("REVENUE_INTERVAL_SECONDS", "300")
    )
    last_revenue_update = 0.0

    print(f"定时调度器已启动，检查间隔：{interval}秒")

    while True:
        now = time.time()
        update_revenue = now - last_revenue_update >= revenue_interval

        try:
            finished = TournamentService.run_scheduler_cycle(
                update_revenue=update_revenue
            )
            if update_revenue:
                last_revenue_update = now
            if finished:
                print(f"本轮自动结束比赛：{finished}场")
        except Exception as exc:
            print(f"调度器检查失败：{exc}")

        time.sleep(interval)


def start_scheduler():
    """单个容器进程只启动一个后台调度线程。"""
    global _started

    with _lock:
        if _started:
            return

        thread = threading.Thread(
            target=_scheduler_loop,
            name="tournament-scheduler",
            daemon=True,
        )
        thread.start()
        _started = True