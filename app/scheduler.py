# app/scheduler.py
import threading
import time
from datetime import datetime, timezone

from app.memfire_rest import get_memfire
from app.models.tournament import TABLE_MATCHES, TABLE_TOURNAMENTS
from app.services.tournament_service import TournamentService


# 到期开赛检查：每5分钟。
ACTIVATE_INTERVAL_SECONDS = 5 * 60

# 结束结算检查：每30分钟，保持原来的结算频率。
FINISH_INTERVAL_SECONDS = 30 * 60

# 营收快照：每60分钟，原来是每30分钟。
REVENUE_INTERVAL_SECONDS = 60 * 60

_started = False
_start_lock = threading.Lock()


def _utc_now_text():
    """返回带 UTC 时区的时间，供 MemFire REST 时间过滤使用。"""
    return datetime.now(timezone.utc).isoformat()


def _activate_due_matches():
    """将开始时间已到的 pending 比赛改为 running。"""
    db = get_memfire()
    now = _utc_now_text()

    due_matches = db.select(
        TABLE_MATCHES,
        filters={
            "status": "eq.pending",
            "start_time": f"lte.{now}",
        },
    )

    activated_tournaments = set()

    for match in due_matches:
        match_id = str(match["match_id"])

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

        activated_tournaments.add(
            (
                int(match["tournament_id"]),
                int(match["round_number"]),
            )
        )

    for tournament_id, round_number in activated_tournaments:
        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{tournament_id}"},
            {
                "status": "running",
                "current_round": round_number,
            },
        )

    return len(activated_tournaments)


def _scheduler_loop():
    print(
        "比赛调度器已启动："
        "开赛检查每5分钟，结算检查每30分钟，"
        "营收快照每60分钟。"
    )

    last_activate = 0.0
    last_finish = 0.0
    last_revenue = 0.0

    while True:
        now = time.monotonic()

        if now - last_activate >= ACTIVATE_INTERVAL_SECONDS:
            try:
                activated = _activate_due_matches()
                if activated:
                    print(
                        f"已激活比赛，涉及 {activated} 个赛事轮次"
                    )
            except Exception as exc:
                print(f"检查待开始比赛失败：{exc}")
            finally:
                last_activate = time.monotonic()

        if now - last_revenue >= REVENUE_INTERVAL_SECONDS:
            try:
                updated = (
                    TournamentService
                    .update_all_running_revenues()
                )
                print(f"营收抓取完成，更新 {updated} 场比赛")
            except Exception as exc:
                print(f"营收抓取失败：{exc}")
            finally:
                last_revenue = time.monotonic()

        if now - last_finish >= FINISH_INTERVAL_SECONDS:
            try:
                finished = (
                    TournamentService.check_and_finish_matches()
                )
                if finished:
                    print(f"结束比赛：{finished} 场")
            except Exception as exc:
                print(f"检查并推进比赛失败：{exc}")
            finally:
                last_finish = time.monotonic()

        time.sleep(60)


def start_scheduler():
    """每个容器进程只启动一个调度线程。"""
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