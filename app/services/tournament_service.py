# app/services/tournament_service.py
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import random
import requests

from sqlalchemy import func

from app.models.tournament import (
    SessionLocal,
    TournamentConfig,
    TournamentPlayer,
    TournamentMatch,
    TournamentVote,
    TournamentRevenue,
)

# 瑞士轮之后的淘汰赛阶段编号
ROUND_QF = 100
ROUND_SF = 101
ROUND_FINAL = 102

BEST_OF_THREE_WINS = 2
KNOCKOUT_GAME_HOURS = 72
SWISS_MAX_ROUNDS = 5

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc

DATA_API_BASES = (
    "https://vr.qianqiuzy.cn",
    "https://psp.qianqiuzy.cn",
)

HTTP_TIMEOUT_SECONDS = 20


def _as_utc(value):
    """数据库中的 naive datetime 按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC_TZ)
    return value.astimezone(UTC_TZ)


def _parse_api_time(value):
    """上游 API 时间无时区时按北京时间解析。"""
    if not value:
        return None

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)

    return parsed.astimezone(UTC_TZ)


def _month_strings(start_bj, end_bj):
    """生成时间窗口覆盖的月份，格式 YYYYMM。"""
    months = []
    cursor = start_bj.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    while cursor < end_bj:
        months.append(cursor.strftime("%Y%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return months


def _get_room_sessions(room_id, month):
    """从 VR/PSP 数据接口获取直播场次。"""
    errors = []
    room_id = str(room_id)

    for base_url in DATA_API_BASES:
        try:
            response = requests.get(
                f"{base_url}/gift/live_sessions",
                params={"room_id": room_id, "month": month},
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            if response.status_code in (400, 404):
                errors.append(f"{base_url}: 房间不在此数据源")
                continue

            response.raise_for_status()
            payload = response.json()

            returned_room_id = payload.get("room_id")
            if (
                returned_room_id is not None
                and str(returned_room_id) != room_id
            ):
                errors.append(f"{base_url}: 返回房间号不匹配")
                continue

            sessions = payload.get("sessions")
            if not isinstance(sessions, list):
                raise RuntimeError(
                    f"{base_url} 返回的数据没有 sessions 数组"
                )

            return sessions

        except Exception as exc:
            errors.append(f"{base_url}: {exc}")

    raise RuntimeError(
        f"查询直播间 {room_id} 的 {month} 数据失败："
        + " | ".join(errors)
    )


def _accumulate_bucket(result, bucket, start_utc, end_utc):
    """将15分钟统计桶按比赛时间窗口的重叠比例累计。"""
    bucket_start = _parse_api_time(bucket.get("start_time"))
    bucket_end = _parse_api_time(bucket.get("end_time"))

    if not bucket_start:
        return

    if not bucket_end:
        bucket_end = bucket_start + timedelta(minutes=15)

    if bucket_end <= bucket_start:
        return

    overlap_start = max(bucket_start, start_utc)
    overlap_end = min(bucket_end, end_utc)
    overlap_seconds = (overlap_end - overlap_start).total_seconds()

    if overlap_seconds <= 0:
        return

    bucket_seconds = (bucket_end - bucket_start).total_seconds()
    ratio = min(1.0, overlap_seconds / bucket_seconds)

    result["gift_amount"] += float(bucket.get("gift") or 0) * ratio
    result["guard_amount"] += float(bucket.get("guard") or 0) * ratio
    result["sc_amount"] += float(bucket.get("super_chat") or 0) * ratio
    result["danmaku_count"] += (
        float(bucket.get("danmaku_count") or 0) * ratio
    )

    result["peak_viewers"] = max(
        result["peak_viewers"],
        int(bucket.get("max_concurrency") or 0),
    )

    avg_concurrency = bucket.get("avg_concurrency")
    sample_count = float(bucket.get("sample_count") or 0)

    if avg_concurrency is not None and sample_count > 0:
        weighted_samples = sample_count * ratio
        result["concurrency_weighted_sum"] += (
            float(avg_concurrency) * weighted_samples
        )
        result["concurrency_weight"] += weighted_samples


def _series_id_and_game_number(match_id):
    """
    从淘汰赛单局 ID 中解析系列赛和局数。
    例如 qf1g2 -> ("qf1", 2)，finalg1 -> ("final", 1)。
    """
    if "g" not in match_id:
        return None, None

    series_id, game_text = match_id.rsplit("g", 1)

    if not game_text.isdigit():
        return None, None

    if not (
        series_id.startswith("qf")
        or series_id.startswith("sf")
        or series_id == "final"
    ):
        return None, None

    return series_id, int(game_text)


class TournamentService:

    @staticmethod
    def init_tournament(name: str, players: list):
        """创建比赛和参赛选手。"""
        session = SessionLocal()
        try:
            tournament = TournamentConfig(
                name=name,
                status="pending",
            )
            session.add(tournament)
            session.flush()

            for item in players:
                session.add(TournamentPlayer(
                    tournament_id=tournament.id,
                    player_id=item["id"],
                    player_name=item["name"],
                    room_id=str(item["room_id"]),
                    avatar_url=item.get("avatar_url"),
                    seed=item.get("seed", 999),
                    wins=0,
                    losses=0,
                    status="active",
                ))

            session.commit()
            return tournament.id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def auto_start_next_swiss_round(tournament_id: int):
        """按当前战绩分组，在同战绩组内随机配对并开始下一轮。"""
        session = SessionLocal()
        try:
            tournament = session.get(TournamentConfig, tournament_id)
            if not tournament:
                return {"success": False, "error": "比赛不存在"}

            players = session.query(TournamentPlayer).filter_by(
                tournament_id=tournament_id,
                status="active",
            ).all()

            if len(players) < 2:
                return {"success": False, "error": "可配对选手不足"}

            groups = {}
            for player in players:
                groups.setdefault(
                    (player.wins, player.losses), []
                ).append(player)

            next_round = tournament.current_round + 1
            start_time = datetime.utcnow()
            end_time = start_time + timedelta(
                days=tournament.match_duration_days
            )
            match_number = 0

            for record in sorted(groups.keys(), reverse=True):
                group = groups[record]
                random.shuffle(group)

                for index in range(0, len(group) - 1, 2):
                    match_number += 1
                    player1 = group[index]
                    player2 = group[index + 1]

                    session.add(TournamentMatch(
                        tournament_id=tournament_id,
                        match_id=f"r{next_round}m{match_number}",
                        round_number=next_round,
                        player1_id=player1.player_id,
                        player2_id=player2.player_id,
                        start_time=start_time,
                        end_time=end_time,
                        status="running",
                    ))

            tournament.current_round = next_round
            tournament.status = "running"
            session.commit()

            return {
                "success": True,
                "round": next_round,
                "matches": match_number,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def start_knockout_stage(tournament_id: int):
        """瑞士轮结束后创建八强 BO3 的第一局。"""
        session = SessionLocal()
        try:
            tournament = session.get(TournamentConfig, tournament_id)
            if not tournament:
                return {"success": False, "error": "比赛不存在"}

            existing = session.query(TournamentMatch).filter_by(
                tournament_id=tournament_id,
                round_number=ROUND_QF,
            ).first()

            if existing:
                return {"success": False, "error": "八强对局已存在"}

            advanced = session.query(TournamentPlayer).filter_by(
                tournament_id=tournament_id,
                status="advanced",
            ).all()

            if len(advanced) != 8:
                return {
                    "success": False,
                    "error": f"晋级人数为{len(advanced)}，需要8人",
                }

            random.shuffle(advanced)

            start_time = datetime.utcnow()
            end_time = start_time + timedelta(
                hours=KNOCKOUT_GAME_HOURS
            )

            for index in range(4):
                player1 = advanced[index * 2]
                player2 = advanced[index * 2 + 1]

                session.add(TournamentMatch(
                    tournament_id=tournament_id,
                    match_id=f"qf{index + 1}g1",
                    round_number=ROUND_QF,
                    player1_id=player1.player_id,
                    player2_id=player2.player_id,
                    start_time=start_time,
                    end_time=end_time,
                    status="running",
                ))

            tournament.current_round = ROUND_QF
            tournament.status = "running"
            session.commit()

            return {
                "success": True,
                "stage": "quarterfinals",
                "matches_created": 4,
                "format": "BO3",
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _completed_series_wins(session, tournament_id, series_id):
        """返回系列赛双方当前赢下的局数。"""
        game_matches = session.query(TournamentMatch).filter(
            TournamentMatch.tournament_id == tournament_id,
            TournamentMatch.match_id.like(f"{series_id}g%"),
            TournamentMatch.status == "completed",
        ).all()

        wins = {}
        for game in game_matches:
            if game.winner_id:
                wins[game.winner_id] = wins.get(game.winner_id, 0) + 1

        return game_matches, wins

    @staticmethod
    def _start_next_series_game(
        session,
        tournament,
        series_id,
        round_number,
        player1_id,
        player2_id,
        previous_games,
    ):
        """创建系列赛的下一局，比赛时长72小时。"""
        game_number = previous_games + 1
        now = datetime.utcnow()

        session.add(TournamentMatch(
            tournament_id=tournament.id,
            match_id=f"{series_id}g{game_number}",
            round_number=round_number,
            player1_id=player1_id,
            player2_id=player2_id,
            start_time=now,
            end_time=now + timedelta(hours=KNOCKOUT_GAME_HOURS),
            status="running",
        ))

    @staticmethod
    def _knockout_series_ids(session, tournament_id, round_number):
        """列出某一淘汰赛阶段已经出现过的系列赛编号。"""
        matches = session.query(TournamentMatch).filter_by(
            tournament_id=tournament_id,
            round_number=round_number,
        ).all()

        ids = set()
        for match in matches:
            series_id, _game_number = _series_id_and_game_number(
                match.match_id
            )
            if series_id:
                ids.add(series_id)

        return sorted(ids)

    @staticmethod
    def _advance_knockout(tournament_id):
        """
        检查每个 BO3 系列赛：
        - 未达到2胜，自动创建下一局；
        - 当前阶段全部系列赛结束，自动开始下一阶段。
        """
        session = SessionLocal()
        try:
            tournament = session.get(TournamentConfig, tournament_id)
            if not tournament:
                return {"success": False, "error": "比赛不存在"}

            if tournament.current_round == ROUND_QF:
                round_number = ROUND_QF
                series_ids = ["qf1", "qf2", "qf3", "qf4"]
                next_round = ROUND_SF
                next_prefix = "sf"
                expected_series_count = 4
            elif tournament.current_round == ROUND_SF:
                round_number = ROUND_SF
                series_ids = ["sf1", "sf2"]
                next_round = ROUND_FINAL
                next_prefix = "final"
                expected_series_count = 2
            elif tournament.current_round == ROUND_FINAL:
                round_number = ROUND_FINAL
                series_ids = ["final"]
                next_round = None
                next_prefix = None
                expected_series_count = 1
            else:
                return {"success": False, "error": "当前不是淘汰赛阶段"}

            series_winners = {}
            all_series_complete = True

            for series_id in series_ids:
                games, wins = TournamentService._completed_series_wins(
                    session,
                    tournament_id,
                    series_id,
                )

                if not games:
                    all_series_complete = False
                    continue

                player1_id = games[0].player1_id
                player2_id = games[0].player2_id
                player1_wins = wins.get(player1_id, 0)
                player2_wins = wins.get(player2_id, 0)

                if player1_wins >= BEST_OF_THREE_WINS:
                    series_winners[series_id] = player1_id
                    continue

                if player2_wins >= BEST_OF_THREE_WINS:
                    series_winners[series_id] = player2_id
                    continue

                all_series_complete = False
                played_games = len(games)

                if played_games < 3:
                    TournamentService._start_next_series_game(
                        session=session,
                        tournament=tournament,
                        series_id=series_id,
                        round_number=round_number,
                        player1_id=player1_id,
                        player2_id=player2_id,
                        previous_games=played_games,
                    )

            if not all_series_complete:
                session.commit()
                return {
                    "success": True,
                    "stage": "series_continues",
                    "waiting": True,
                }

            if len(series_winners) != expected_series_count:
                session.commit()
                return {
                    "success": True,
                    "stage": "waiting_for_series",
                    "waiting": True,
                }

            if round_number == ROUND_FINAL:
                tournament.status = "finished"
                champion_id = series_winners["final"]
                session.commit()
                return {
                    "success": True,
                    "stage": "finished",
                    "champion": champion_id,
                }

            if round_number == ROUND_QF:
                next_pairs = [
                    (series_winners["qf1"], series_winners["qf2"]),
                    (series_winners["qf3"], series_winners["qf4"]),
                ]
            else:
                next_pairs = [
                    (series_winners["sf1"], series_winners["sf2"]),
                ]

            start_time = datetime.utcnow()
            end_time = start_time + timedelta(
                hours=KNOCKOUT_GAME_HOURS
            )

            for index, (player1_id, player2_id) in enumerate(next_pairs, 1):
                series_id = (
                    f"{next_prefix}{index}"
                    if next_prefix != "final"
                    else "final"
                )

                session.add(TournamentMatch(
                    tournament_id=tournament_id,
                    match_id=f"{series_id}g1",
                    round_number=next_round,
                    player1_id=player1_id,
                    player2_id=player2_id,
                    start_time=start_time,
                    end_time=end_time,
                    status="running",
                ))

            tournament.current_round = next_round
            tournament.status = "running"
            session.commit()

            return {
                "success": True,
                "stage": (
                    "semifinals"
                    if next_round == ROUND_SF
                    else "final"
                ),
                "matches_created": len(next_pairs),
                "format": "BO3",
            }

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def auto_advance_knockout(tournament_id: int):
        """兼容原有调用名，执行 BO3 阶段推进。"""
        return TournamentService._advance_knockout(tournament_id)

    @staticmethod
    def get_player_revenue(
        room_id: str,
        start_time: datetime,
        end_time: datetime = None,
    ):
        """查询指定比赛时间窗口内的真实营收。"""
        if not room_id or not start_time:
            raise ValueError("查询营收需要 room_id 和 start_time")

        start_utc = _as_utc(start_time)
        end_utc = _as_utc(end_time or datetime.now(UTC_TZ))

        if end_utc <= start_utc:
            return {
                "sc_amount": 0.0,
                "gift_amount": 0.0,
                "guard_amount": 0.0,
                "total_revenue": 0.0,
                "danmaku_count": 0,
                "peak_viewers": 0,
                "avg_viewers": 0,
            }

        start_bj = start_utc.astimezone(BEIJING_TZ)
        end_bj = end_utc.astimezone(BEIJING_TZ)

        total = {
            "gift_amount": 0.0,
            "guard_amount": 0.0,
            "sc_amount": 0.0,
            "danmaku_count": 0.0,
            "peak_viewers": 0,
            "concurrency_weighted_sum": 0.0,
            "concurrency_weight": 0.0,
        }

        for month in _month_strings(start_bj, end_bj):
            sessions = _get_room_sessions(room_id, month)

            for live_session in sessions:
                buckets = live_session.get("stats_15m") or []

                if buckets:
                    for bucket in buckets:
                        _accumulate_bucket(
                            total,
                            bucket,
                            start_utc,
                            end_utc,
                        )
                    continue

                session_start = _parse_api_time(
                    live_session.get("start_time")
                )
                session_end = _parse_api_time(
                    live_session.get("end_time")
                )

                if (
                    session_start is None
                    or session_end is None
                    or session_start < start_utc
                    or session_end > end_utc
                ):
                    continue

                total["gift_amount"] += float(
                    live_session.get("gift") or 0
                )
                total["guard_amount"] += float(
                    live_session.get("guard") or 0
                )
                total["sc_amount"] += float(
                    live_session.get("super_chat") or 0
                )
                total["danmaku_count"] += float(
                    live_session.get("danmaku_count") or 0
                )
                total["peak_viewers"] = max(
                    total["peak_viewers"],
                    int(live_session.get("max_concurrency") or 0),
                )

                average = live_session.get("avg_concurrency")
                duration = max(
                    1.0,
                    (session_end - session_start).total_seconds(),
                )
                if average is not None:
                    total["concurrency_weighted_sum"] += (
                        float(average) * duration
                    )
                    total["concurrency_weight"] += duration

        gift = round(total["gift_amount"], 2)
        guard = round(total["guard_amount"], 2)
        sc = round(total["sc_amount"], 2)

        average_viewers = 0
        if total["concurrency_weight"] > 0:
            average_viewers = round(
                total["concurrency_weighted_sum"]
                / total["concurrency_weight"]
            )

        return {
            "sc_amount": sc,
            "gift_amount": gift,
            "guard_amount": guard,
            "total_revenue": round(gift + guard + sc, 2),
            "danmaku_count": round(total["danmaku_count"]),
            "peak_viewers": total["peak_viewers"],
            "avg_viewers": average_viewers,
        }

    @staticmethod
    def _save_revenue_snapshot(session, match, player, revenue, now):
        """保存营收快照。模型没有 guard 列，因此 gift_amount 存 gift+guard。"""
        session.add(TournamentRevenue(
            player_id=player.player_id,
            match_id=match.match_id,
            sc_amount=revenue["sc_amount"],
            gift_amount=(
                revenue["gift_amount"] + revenue["guard_amount"]
            ),
            total_revenue=revenue["total_revenue"],
            danmaku_count=revenue["danmaku_count"],
            peak_viewers=revenue["peak_viewers"],
            avg_viewers=revenue["avg_viewers"],
            snapshot_time=now,
        ))

    @staticmethod
    def update_match_revenue(match_id: str):
        """更新当前进行中的单局营收快照。"""
        session = SessionLocal()
        try:
            match = session.query(TournamentMatch).filter_by(
                match_id=match_id
            ).first()

            if not match or match.status != "running":
                return None

            player1 = session.query(TournamentPlayer).filter_by(
                player_id=match.player1_id
            ).first()
            player2 = session.query(TournamentPlayer).filter_by(
                player_id=match.player2_id
            ).first()

            if not player1 or not player2:
                return None

            now = datetime.utcnow()
            cutoff = min(now, match.end_time) if match.end_time else now

            revenue1 = TournamentService.get_player_revenue(
                player1.room_id,
                match.start_time,
                cutoff,
            )
            revenue2 = TournamentService.get_player_revenue(
                player2.room_id,
                match.start_time,
                cutoff,
            )

            TournamentService._save_revenue_snapshot(
                session, match, player1, revenue1, now
            )
            TournamentService._save_revenue_snapshot(
                session, match, player2, revenue2, now
            )

            session.commit()
            return {
                "match_id": match_id,
                "player1": revenue1,
                "player2": revenue2,
            }

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _final_revenue_for_player(session, match, player, now):
        """重新查询整局时间窗口，作为最终营收，不依赖旧快照。"""
        revenue = TournamentService.get_player_revenue(
            player.room_id,
            match.start_time,
            match.end_time or now,
        )

        TournamentService._save_revenue_snapshot(
            session, match, player, revenue, now
        )
        return revenue

    @staticmethod
    def _pick_winner(match, revenue1, revenue2):
        """总营收较高者胜；营收相同按弹幕数判定。"""
        if revenue1["total_revenue"] > revenue2["total_revenue"]:
            return match.player1_id

        if revenue2["total_revenue"] > revenue1["total_revenue"]:
            return match.player2_id

        if revenue1["danmaku_count"] >= revenue2["danmaku_count"]:
            return match.player1_id

        return match.player2_id

    @staticmethod
    def check_and_finish_matches():
        """结束到期单局，并自动推进瑞士轮或 BO3 淘汰赛。"""
        session = SessionLocal()
        try:
            now = datetime.utcnow()

            expired = session.query(TournamentMatch).filter(
                TournamentMatch.status == "running",
                TournamentMatch.end_time <= now,
            ).all()

            finished_count = 0
            tournament_ids = set()

            for match in expired:
                player1 = session.query(TournamentPlayer).filter_by(
                    player_id=match.player1_id
                ).first()
                player2 = session.query(TournamentPlayer).filter_by(
                    player_id=match.player2_id
                ).first()

                if not player1 or not player2:
                    continue

                try:
                    revenue1 = TournamentService._final_revenue_for_player(
                        session, match, player1, now
                    )
                    revenue2 = TournamentService._final_revenue_for_player(
                        session, match, player2, now
                    )
                except Exception as exc:
                    print(
                        f"最终营收查询失败，暂不结束 {match.match_id}: {exc}"
                    )
                    continue

                match.player1_revenue = revenue1["total_revenue"]
                match.player2_revenue = revenue2["total_revenue"]
                match.winner_id = TournamentService._pick_winner(
                    match, revenue1, revenue2
                )
                match.status = "completed"

                # 瑞士轮更新选手战绩；淘汰赛局数不改瑞士轮战绩。
                if match.round_number < ROUND_QF:
                    winner = session.query(TournamentPlayer).filter_by(
                        player_id=match.winner_id
                    ).first()

                    loser_id = (
                        match.player2_id
                        if match.winner_id == match.player1_id
                        else match.player1_id
                    )
                    loser = session.query(TournamentPlayer).filter_by(
                        player_id=loser_id
                    ).first()

                    if winner:
                        winner.wins += 1
                        if winner.wins >= 3:
                            winner.status = "advanced"

                    if loser:
                        loser.losses += 1
                        if loser.losses >= 3:
                            loser.status = "eliminated"

                finished_count += 1
                tournament_ids.add(match.tournament_id)

            session.commit()

            for tournament_id in tournament_ids:
                tournament = session.get(
                    TournamentConfig,
                    tournament_id
                )
                if not tournament:
                    continue

                if tournament.current_round < ROUND_QF:
                    current_matches = session.query(
                        TournamentMatch
                    ).filter_by(
                        tournament_id=tournament_id,
                        round_number=tournament.current_round,
                    ).all()

                    if (
                        not current_matches
                        or not all(m.status == "completed" for m in current_matches)
                    ):
                        continue

                    players = session.query(TournamentPlayer).filter_by(
                        tournament_id=tournament_id
                    ).all()

                    advanced = sum(
                        1 for p in players if p.status == "advanced"
                    )
                    eliminated = sum(
                        1 for p in players if p.status == "eliminated"
                    )

                    if advanced == 8 and eliminated == 8:
                        TournamentService.start_knockout_stage(
                            tournament_id
                        )
                    elif tournament.current_round < SWISS_MAX_ROUNDS:
                        TournamentService.auto_start_next_swiss_round(
                            tournament_id
                        )
                else:
                    TournamentService._advance_knockout(tournament_id)

            return finished_count

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def vote(match_id, player_id, voter_fingerprint, voter_ip):
        """同一访客指纹在同一单局只能投票一次。"""
        if not voter_fingerprint:
            return {"success": False, "error": "missing_fingerprint"}

        session = SessionLocal()
        try:
            match = session.query(TournamentMatch).filter_by(
                match_id=match_id
            ).first()

            if not match:
                return {"success": False, "error": "match_not_found"}

            if player_id not in (match.player1_id, match.player2_id):
                return {"success": False, "error": "player_not_in_match"}

            existing = session.query(TournamentVote).filter_by(
                match_id=match_id,
                voter_fingerprint=voter_fingerprint,
            ).first()

            if existing:
                return {"success": False, "error": "already_voted"}

            session.add(TournamentVote(
                match_id=match_id,
                player_id=player_id,
                voter_fingerprint=voter_fingerprint,
                voter_ip=voter_ip,
            ))
            session.commit()
            return {"success": True}

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def get_vote_counts(match_id):
        """获取单局双方票数。"""
        session = SessionLocal()
        try:
            rows = session.query(
                TournamentVote.player_id,
                func.count(TournamentVote.id),
            ).filter_by(
                match_id=match_id
            ).group_by(
                TournamentVote.player_id
            ).all()
            return {player_id: count for player_id, count in rows}
        finally:
            session.close()

    @staticmethod
    def get_current_state(tournament_id):
        """返回比赛、选手、单局、BO3系列赛比分、营收和投票。"""
        session = SessionLocal()
        try:
            tournament = session.get(
                TournamentConfig,
                tournament_id
            )
            if not tournament:
                return None

            players = session.query(TournamentPlayer).filter_by(
                tournament_id=tournament_id
            ).all()

            matches = session.query(TournamentMatch).filter_by(
                tournament_id=tournament_id
            ).order_by(
                TournamentMatch.round_number,
                TournamentMatch.match_id,
            ).all()

            players_data = [{
                "id": p.player_id,
                "name": p.player_name,
                "room_id": p.room_id,
                "avatar": p.avatar_url,
                "wins": p.wins,
                "losses": p.losses,
                "status": p.status,
            } for p in players]

            # 汇总淘汰赛各 BO3 系列赛的局分
            series_rows = {}
            for match in matches:
                series_id, game_number = _series_id_and_game_number(
                    match.match_id
                )
                if not series_id:
                    continue

                row = series_rows.setdefault(series_id, {
                    "series_id": series_id,
                    "player1_id": match.player1_id,
                    "player2_id": match.player2_id,
                    "player1_game_wins": 0,
                    "player2_game_wins": 0,
                    "series_winner": None,
                    "games_played": 0,
                })

                if match.status == "completed":
                    row["games_played"] += 1
                    if match.winner_id == row["player1_id"]:
                        row["player1_game_wins"] += 1
                    elif match.winner_id == row["player2_id"]:
                        row["player2_game_wins"] += 1

                if row["player1_game_wins"] >= BEST_OF_THREE_WINS:
                    row["series_winner"] = row["player1_id"]
                elif row["player2_game_wins"] >= BEST_OF_THREE_WINS:
                    row["series_winner"] = row["player2_id"]

            matches_data = []

            for match in matches:
                series_id, game_number = _series_id_and_game_number(
                    match.match_id
                )

                match_data = {
                    "id": match.match_id,
                    "round": match.round_number,
                    "p1": match.player1_id,
                    "p2": match.player2_id,
                    "winner": match.winner_id,
                    "status": match.status,
                    "start_time": (
                        match.start_time.isoformat()
                        if match.start_time else None
                    ),
                    "end_time": (
                        match.end_time.isoformat()
                        if match.end_time else None
                    ),
                }

                if series_id:
                    match_data["series_id"] = series_id
                    match_data["game_number"] = game_number
                    match_data["series"] = series_rows[series_id]

                revenue = {}
                for player_id in (match.player1_id, match.player2_id):
                    latest = session.query(TournamentRevenue).filter_by(
                        match_id=match.match_id,
                        player_id=player_id,
                    ).order_by(
                        TournamentRevenue.snapshot_time.desc()
                    ).first()

                    if latest:
                        revenue[player_id] = {
                            "sc": latest.sc_amount or 0,
                            "gift": latest.gift_amount or 0,
                            "total": latest.total_revenue or 0,
                            "danmaku": latest.danmaku_count or 0,
                            "peak_viewers": latest.peak_viewers or 0,
                            "avg_viewers": latest.avg_viewers or 0,
                        }
                    else:
                        total = 0
                        if match.status == "completed":
                            total = (
                                match.player1_revenue or 0
                                if player_id == match.player1_id
                                else match.player2_revenue or 0
                            )

                        revenue[player_id] = {
                            "sc": 0,
                            "gift": 0,
                            "total": total,
                            "danmaku": 0,
                            "peak_viewers": 0,
                            "avg_viewers": 0,
                        }

                match_data["revenue"] = revenue
                match_data["votes"] = TournamentService.get_vote_counts(
                    match.match_id
                )
                matches_data.append(match_data)

            return {
                "tournament": {
                    "id": tournament.id,
                    "name": tournament.name,
                    "status": tournament.status,
                    "start_time": (
                        tournament.start_time.isoformat()
                        if tournament.start_time else None
                    ),
                    "current_round": tournament.current_round,
                    "match_duration_days": tournament.match_duration_days,
                },
                "players": players_data,
                "matches": matches_data,
                "series": list(series_rows.values()),
            }

        finally:
            session.close()