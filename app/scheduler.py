# app/scheduler.py
import os
import threading
import time
from datetime import datetime, timezone

from app.memfire_rest import get_memfire
from app.models.tournament import TABLE_MATCHES, TABLE_TOURNAMENTS
from app.services.tournament_service import TournamentService


_started = False
_start_lock = threading.Lock()


def _utc_now_text():
    """返回带 UTC 时区的 ISO 时间，供 PostgREST 时间过滤使用。"""
    return datetime.now(timezone.utc).isoformat()


def _activate_due_matches():
    """
    将已到开始时间的 pending 比赛切换为 running。
    例如北京时间 2026-10-22 00:00 对应数据库 UTC 2026-10-21 16:00。
    """
    db = get_memfire()
    now = _utc_now_text()

    due_matches = db.select(
        TABLE_MATCHES,
        filters={
            "status": "eq.pending",
            "start_time": f"lte.{now}",
        },
    )

    if not due_matches:
        return 0

    tournament_rounds = {}

    for match in due_matches:
        match_id = str(match["match_id"])

        # 再读一次当前记录，避免重复处理已被其他调度周期启动的比赛。
        current = db.select(
            TABLE_MATCHES,
            filters={
                "match_id": f"eq.{match_id}",
                "status": "eq.pending",
            },
            limit=1,
        )

        if not current:
            continue

        db.update(
            TABLE_MATCHES,
            {
                "match_id": f"eq.{match_id}",
                "status": "eq.pending",
            },
            {"status": "running"},
        )

        tournament_id = int(match["tournament_id"])
        round_number = int(match["round_number"])
        tournament_rounds[tournament_id] = max(
            tournament_rounds.get(tournament_id, 0),
            round_number,
        )

    for tournament_id, round_number in tournament_rounds.items():
        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{tournament_id}"},
            {
                "status": "running",
                "current_round": round_number,
            },
        )

    return len(tournament_rounds)


def _scheduler_loop():
    interval = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))
    revenue_interval = int(
        os.environ.get("REVENUE_INTERVAL_SECONDS", "300")
    )
    last_revenue_update = 0.0

    print(
        f"⏰ 比赛调度器已启动：检查间隔 {interval} 秒，"
        f"营收刷新间隔 {revenue_interval} 秒"
    )

    while True:
        cycle_started = time.time()

        try:
            activated_tournaments = _activate_due_matches()
            if activated_tournaments:
                print(
                    f"✅ 已启动到时比赛，涉及 "
                    f"{activated_tournaments} 个赛事"
                )
        except Exception as exc:
            print(f"⚠️ 启动待开始比赛失败：{exc}")

        update_revenue = (
            cycle_started - last_revenue_update >= revenue_interval
        )

        try:
            if update_revenue:
                updated = TournamentService.update_all_running_revenues()
                last_revenue_update = cycle_started
                if updated:
                    print(f"✅ 已更新 {updated} 场比赛营收")
        except Exception as exc:
            print(f"⚠️ 批量更新营收失败：{exc}")

        try:
            finished = TournamentService.check_and_finish_matches()
            if finished:
                print(f"✅ 本轮自动结束比赛：{finished} 场")
        except Exception as exc:
            print(f"⚠️ 检查和推进比赛失败：{exc}")

        time.sleep(interval)


def start_scheduler():
    """单个 Gunicorn worker 进程只启动一个调度线程。"""
    global _started

    with _start_lock:
        if _started:
            return

        thread = threading.Thread(
            target=_scheduler_loop,
            name="tournament-scheduler",
            daemon=True,
        )
        thread.start()
        _started = True