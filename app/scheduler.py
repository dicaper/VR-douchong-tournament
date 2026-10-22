# app/scheduler.py
import threading
import time
from datetime import datetime, timezone

from app.memfire_rest import get_memfire
from app.models.tournament import TABLE_MATCHES, TABLE_TOURNAMENTS
from app.services.tournament_service import TournamentService


# 营收抓取、待开始比赛检查、到期比赛结算统一每30分钟执行一次。
SCHEDULER_INTERVAL_SECONDS = 30 * 60

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

        # 再检查一次当前状态，避免重复激活。
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
    interval = SCHEDULER_INTERVAL_SECONDS

    print(
        "⏰ 比赛调度器已启动："
        f"待开始检查、营收抓取和结束结算统一每 {interval // 60} 分钟执行一次"
    )

    # 服务启动时先运行一次，之后每30分钟运行一次。
    while True:
        try:
            activated = _activate_due_matches()
            if activated:
                print(f"✅ 已激活比赛，涉及 {activated} 个赛事轮次")

        except Exception as exc:
            print(f"⚠️ 检查并启动待开始比赛失败：{exc}")

        try:
            updated = TournamentService.update_all_running_revenues()
            print(f"📊 本轮营收抓取完成，更新 {updated} 场比赛")
        except Exception as exc:
            print(f"⚠️ 本轮营收抓取失败：{exc}")

        try:
            finished = TournamentService.check_and_finish_matches()
            if finished:
                print(f"✅ 本轮结束比赛：{finished} 场")
        except Exception as exc:
            print(f"⚠️ 检查并推进比赛失败：{exc}")

        time.sleep(interval)


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