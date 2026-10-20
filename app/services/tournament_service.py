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

# 瑞士轮之后的淘汰赛轮次编号
ROUND_QF = 100
ROUND_SF = 101
ROUND_FINAL = 102

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc

# 数据接口的两个主机；函数会用房间号判断使用哪一个。
DATA_API_BASES = (
    "https://vr.qianqiuzy.cn",
    "https://psp.qianqiuzy.cn",
)

HTTP_TIMEOUT_SECONDS = 20


def _as_utc(value):
    """把数据库里的时间转换成带时区 UTC 时间。"""
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC_TZ)

    return value.astimezone(UTC_TZ)


def _parse_api_time(value):
    """
    解析数据 API 的时间。
    API 文档中的场次时间是北京时间格式的本地时间字符串。
    """
    if not value:
        return None

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)

    return parsed.astimezone(UTC_TZ)


def _month_strings(start_bj, end_bj):
    """返回时间范围覆盖的月份，格式为 YYYYMM。"""
    months = []
    cursor = start_bj.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    while cursor < end_bj:
        months.append(cursor.strftime("%Y%m"))

        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return months


def _get_room_sessions(room_id, month):
    """
    从 VR/PSP 数据接口查询某个直播间某月的直播场次。
    返回 (sessions, base_url)。
    """
    errors = []
    room_id = str(room_id)

    for base_url in DATA_API_BASES:
        try:
            response = requests.get(
                f"{base_url}/gift/live_sessions",
                params={
                    "room_id": room_id,
                    "month": month,
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            # 这个 host 不提供该房间时，尝试另一个 host。
            if response.status_code == 400 or response.status_code == 404:
                errors.append(f"{base_url}: 房间不在此数据源")
                continue

            response.raise_for_status()
            payload = response.json()

            # 用返回的 room_id 确认该主机确实匹配这个直播间。
            returned_room_id = payload.get("room_id")
            if returned_room_id is not None and str(returned_room_id) != room_id:
                errors.append(f"{base_url}: 返回的房间号不匹配")
                continue

            sessions = payload.get("sessions")
            if not isinstance(sessions, list):
                raise RuntimeError(
                    f"{base_url} 的 /gift/live_sessions 响应没有 sessions 数组"
                )

            return sessions, base_url

        except Exception as exc:
            errors.append(f"{base_url}: {exc}")

    raise RuntimeError(
        f"查询直播间 {room_id} 的 {month} 数据失败。"
        + " | ".join(errors)
    )


def _accumulate_bucket(result, bucket, window_start_utc, window_end_utc):
    """
    将一个15分钟数据桶按它与比赛窗口的重叠时长累加。
    比赛边界落在桶中间时按比例估算。
    """
    bucket_start = _parse_api_time(bucket.get("start_time"))
    bucket_end = _parse_api_time(bucket.get("end_time"))

    if not bucket_start:
        return

    # 若 API 没给 end_time，按15分钟桶处理。
    if not bucket_end:
        bucket_end = bucket_start + timedelta(minutes=15)

    if bucket_end <= bucket_start:
        return

    overlap_start = max(bucket_start, window_start_utc)
    overlap_end = min(bucket_end, window_end_utc)
    overlap_seconds = (overlap_end - overlap_start).total_seconds()

    if overlap_seconds <= 0:
        return

    bucket_seconds = (bucket_end - bucket_start).total_seconds()
    ratio = min(1.0, overlap_seconds / bucket_seconds)

    result["gift_amount"] += float(bucket.get("gift") or 0) * ratio
    result["guard_amount"] += float(bucket.get("guard") or 0) * ratio
    result["sc_amount"] += float(bucket.get("super_chat") or 0) * ratio
    result["danmaku_count"] += float(bucket.get("danmaku_count") or 0) * ratio

    max_concurrency = bucket.get("max_concurrency")
    if max_concurrency is not None:
        result["peak_viewers"] = max(
            result["peak_viewers"],
            int(max_concurrency or 0)
        )

    avg_concurrency = bucket.get("avg_concurrency")
    sample_count = float(bucket.get("sample_count") or 0)

    if avg_concurrency is not None and sample_count > 0:
        weighted_samples = sample_count * ratio
        result["concurrency_weighted_sum"] += (
            float(avg_concurrency) * weighted_samples
        )
        result["concurrency_weight"] += weighted_samples


class TournamentService:

    @staticmethod
    def init_tournament(name: str, players: list):
        """初始化比赛，只创建比赛和选手。"""
        session = SessionLocal()

        try:
            tournament = TournamentConfig(
                name=name,
                status="pending"
            )
            session.add(tournament)
            session.flush()

            for player_data in players:
                session.add(TournamentPlayer(
                    tournament_id=tournament.id,
                    player_id=player_data["id"],
                    player_name=player_data["name"],
                    room_id=str(player_data["room_id"]),
                    avatar_url=player_data.get("avatar_url"),
                    seed=player_data.get("seed", 999),
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
        """
        开始下一轮瑞士轮。
        按当前战绩分组，再在同一战绩组内随机配对。
        """
        session = SessionLocal()

        try:
            tournament = session.get(TournamentConfig, tournament_id)

            if not tournament:
                return {"success": False, "error": "比赛不存在"}

            players = session.query(TournamentPlayer).filter_by(
                tournament_id=tournament_id,
                status="active"
            ).all()

            if len(players) < 2:
                return {"success": False, "error": "可配对选手不足"}

            groups = {}

            for player in players:
                record = (player.wins, player.losses)
                groups.setdefault(record, []).append(player)

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
                    player1 = group[index]
                    player2 = group[index + 1]
                    match_number += 1

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
        """瑞士轮结束后创建八强，晋级选手随机配对。"""
        session = SessionLocal()

        try:
            tournament = session.get(TournamentConfig, tournament_id)

            if not tournament:
                return {"success": False, "error": "比赛不存在"}

            existing = session.query(TournamentMatch).filter_by(
                tournament_id=tournament_id,
                round_number=ROUND_QF
            ).first()

            if existing:
                return {"success": False, "error": "八强对阵已经创建"}

            advanced = session.query(TournamentPlayer).filter_by(
                tournament_id=tournament_id,
                status="advanced"
            ).all()

            if len(advanced) != 8:
                return {
                    "success": False,
                    "error": f"晋级人数为 {len(advanced)}，需要8人",
                }

            random.shuffle(advanced)

            start_time = datetime.utcnow()
            end_time = start_time + timedelta(
                days=tournament.match_duration_days
            )

            for index in range(4):
                player1 = advanced[index * 2]
                player2 = advanced[index * 2 + 1]

                session.add(TournamentMatch(
                    tournament_id=tournament_id,
                    match_id=f"qf{index + 1}",
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

            return {"success": True, "stage": "quarterfinals"}

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

    @staticmethod
    def auto_advance_knockout(tournament_id: int):
        """自动推进八强、半决赛、决赛。"""
        session = SessionLocal()

        try:
            tournament = session.get(TournamentConfig, tournament_id)

            if not tournament:
                return {"success": False, "error": "比赛不存在"}

            def get_round(round_number):
                return session.query(TournamentMatch).filter_by(
                    tournament_id=tournament_id,
                    round_number=round_number
                ).order_by(TournamentMatch.match_id).all()

            quarterfinals = get_round(ROUND_QF)
            semifinals = get_round(ROUND_SF)
            finals = get_round(ROUND_FINAL)

            start_time = datetime.utcnow()
            end_time = start_time + timedelta(
                days=tournament.match_duration_days
            )

            if finals:
                if finals[0].status == "completed":
                    tournament.status = "finished"
                    session.commit()
                    return {
                        "success": True,
                        "stage": "finished",
                        "champion": finals[0].winner_id,
                    }

                return {"success": False, "waiting": True}

            if semifinals:
                if (
                    len(semifinals) == 2
                    and all(match.status == "completed" for match in semifinals)
                ):
                    session.add(TournamentMatch(
                        tournament_id=tournament_id,
                        match_id="final",
                        round_number=ROUND_FINAL,
                        player1_id=semifinals[0].winner_id,
                        player2_id=semifinals[1].winner_id,
                        start_time=start_time,
                        end_time=end_time,
                        status="running",
                    ))

                    tournament.current_round = ROUND_FINAL
                    session.commit()
                    return {"success": True, "stage": "final"}

                return {"success": False, "waiting": True}

            if (
                len(quarterfinals) == 4
                and all(match.status == "completed" for match in quarterfinals)
            ):
                for index in range(2):
                    session.add(TournamentMatch(
                        tournament_id=tournament_id,
                        match_id=f"sf{index + 1}",
                        round_number=ROUND_SF,
                        player1_id=quarterfinals[index * 2].winner_id,
                        player2_id=quarterfinals[index * 2 + 1].winner_id,
                        start_time=start_time,
                        end_time=end_time,
                        status="running",
                    ))

                tournament.current_round = ROUND_SF
                session.commit()
                return {"success": True, "stage": "semifinals"}

            return {"success": False, "waiting": True}

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

    @staticmethod
    def get_player_revenue(
        room_id: str,
        start_time: datetime,
        end_time: datetime = None
    ):
        """
        读取 VR/PSP 项目的真实直播数据，只统计 start_time 到 end_time。

        API 数据按15分钟统计。比赛边界与统计桶不完全重合时，
        边界桶按重叠秒数比例估算。
        """
        if not room_id or not start_time:
            raise ValueError("查询真实营收必须提供 room_id 和 start_time")

        window_start_utc = _as_utc(start_time)
        window_end_utc = _as_utc(end_time or datetime.now(UTC_TZ))

        if window_end_utc <= window_start_utc:
            return {
                "sc_amount": 0.0,
                "gift_amount": 0.0,
                "guard_amount": 0.0,
                "total_revenue": 0.0,
                "danmaku_count": 0,
                "peak_viewers": 0,
                "avg_viewers": 0,
            }

        start_bj = window_start_utc.astimezone(BEIJING_TZ)
        end_bj = window_end_utc.astimezone(BEIJING_TZ)

        result = {
            "gift_amount": 0.0,
            "guard_amount": 0.0,
            "sc_amount": 0.0,
            "danmaku_count": 0.0,
            "peak_viewers": 0,
            "concurrency_weighted_sum": 0.0,
            "concurrency_weight": 0.0,
        }

        for month in _month_strings(start_bj, end_bj):
            sessions, _base_url = _get_room_sessions(room_id, month)

            for live_session in sessions:
                buckets = live_session.get("stats_15m") or []

                if buckets:
                    for bucket in buckets:
                        _accumulate_bucket(
                            result,
                            bucket,
                            window_start_utc,
                            window_end_utc
                        )
                    continue

                # 兼容没有15分钟明细的旧场次：
                # 只计入整场落在比赛窗口内的记录，避免把比赛外收入算进来。
                session_start = _parse_api_time(
                    live_session.get("start_time")
                )
                session_end = _parse_api_time(
                    live_session.get("end_time")
                )

                if (
                    session_start is None
                    or session_end is None
                    or session_start < window_start_utc
                    or session_end > window_end_utc
                ):
                    continue

                result["gift_amount"] += float(
                    live_session.get("gift") or 0
                )
                result["guard_amount"] += float(
                    live_session.get("guard") or 0
                )
                result["sc_amount"] += float(
                    live_session.get("super_chat") or 0
                )
                result["danmaku_count"] += float(
                    live_session.get("danmaku_count") or 0
                )

                result["peak_viewers"] = max(
                    result["peak_viewers"],
                    int(live_session.get("max_concurrency") or 0)
                )

                avg_concurrency = live_session.get("avg_concurrency")
                if avg_concurrency is not None:
                    duration = max(
                        1.0,
                        (session_end - session_start).total_seconds()
                    )
                    result["concurrency_weighted_sum"] += (
                        float(avg_concurrency) * duration
                    )
                    result["concurrency_weight"] += duration

        avg_viewers = 0
        if result["concurrency_weight"] > 0:
            avg_viewers = round(
                result["concurrency_weighted_sum"]
                / result["concurrency_weight"]
            )

        gift_amount = round(result["gift_amount"], 2)
        guard_amount = round(result["guard_amount"], 2)
        sc_amount = round(result["sc_amount"], 2)

        # 总营收定义为礼物 + 守护 + SC。
        total_revenue = round(
            gift_amount + guard_amount + sc_amount,
            2
        )

        return {
            "sc_amount": sc_amount,
            "gift_amount": gift_amount,
            "guard_amount": guard_amount,
            "total_revenue": total_revenue,
            "danmaku_count": round(result["danmaku_count"]),
            "peak_viewers": result["peak_viewers"],
            "avg_viewers": avg_viewers,
        }

    @staticmethod
    def _save_revenue_snapshot(session, match, player, revenue, snapshot_time):
        """
        现有 TournamentRevenue 模型没有 guard_amount 列。
        因此把 gift + guard 存入 gift_amount，确保 total_revenue 正确。
        """
        gift_and_guard = (
            revenue["gift_amount"] + revenue["guard_amount"]
        )

        session.add(TournamentRevenue(
            player_id=player.player_id,
            match_id=match.match_id,
            sc_amount=revenue["sc_amount"],
            gift_amount=gift_and_guard,
            total_revenue=revenue["total_revenue"],
            danmaku_count=revenue["danmaku_count"],
            peak_viewers=revenue["peak_viewers"],
            avg_viewers=revenue["avg_viewers"],
            snapshot_time=snapshot_time,
        ))

    @staticmethod
    def update_match_revenue(match_id: str):
        """读取当前比赛窗口营收并保存快照。"""
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

            rev1 = TournamentService.get_player_revenue(
                player1.room_id,
                match.start_time,
                cutoff
            )

            rev2 = TournamentService.get_player_revenue(
                player2.room_id,
                match.start_time,
                cutoff
            )

            TournamentService._save_revenue_snapshot(
                session, match, player1, rev1, now
            )
            TournamentService._save_revenue_snapshot(
                session, match, player2, rev2, now
            )

            session.commit()

            return {
                "match_id": match_id,
                "player1": rev1,
                "player2": rev2,
            }

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

    @staticmethod
    def _calculate_winner(match, revenue1, revenue2):
        """按总营收判胜；打平时按弹幕数判胜。"""
        if revenue1["total_revenue"] > revenue2["total_revenue"]:
            return match.player1_id

        if revenue2["total_revenue"] > revenue1["total_revenue"]:
            return match.player2_id

        if revenue1["danmaku_count"] >= revenue2["danmaku_count"]:
            return match.player1_id

        return match.player2_id

    @staticmethod
    def check_and_finish_matches():
        """
        结束已到时间的比赛。
        到期时重新读取整个比赛时间窗口，避免使用过期快照判定。
        """
        session = SessionLocal()

        try:
            now = datetime.utcnow()

            expired_matches = session.query(TournamentMatch).filter(
                TournamentMatch.status == "running",
                TournamentMatch.end_time <= now
            ).all()

            finished_count = 0
            tournament_ids = set()

            for match in expired_matches:
                player1 = session.query(TournamentPlayer).filter_by(
                    player_id=match.player1_id
                ).first()

                player2 = session.query(TournamentPlayer).filter_by(
                    player_id=match.player2_id
                ).first()

                if not player1 or not player2:
                    continue

                try:
                    # 结束时直接查询完整比赛窗口，作为最终比分来源。
                    final1 = TournamentService.get_player_revenue(
                        player1.room_id,
                        match.start_time,
                        match.end_time or now
                    )
                    final2 = TournamentService.get_player_revenue(
                        player2.room_id,
                        match.start_time,
                        match.end_time or now
                    )
                except Exception as exc:
                    # 上游数据接口暂时不可用时保留 running，
                    # 下一次调度会重试，不用随机数据判胜。
                    print(
                        f"⚠️ 最终营收查询失败，暂不结束 {match.match_id}: {exc}"
                    )
                    continue

                TournamentService._save_revenue_snapshot(
                    session, match, player1, final1, now
                )
                TournamentService._save_revenue_snapshot(
                    session, match, player2, final2, now
                )

                match.player1_revenue = final1["total_revenue"]
                match.player2_revenue = final2["total_revenue"]
                match.winner_id = TournamentService._calculate_winner(
                    match, final1, final2
                )
                match.status = "completed"

                # 瑞士轮更新战绩；淘汰赛不修改瑞士轮战绩。
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
                    round_matches = session.query(TournamentMatch).filter_by(
                        tournament_id=tournament_id,
                        round_number=tournament.current_round
                    ).all()

                    if not round_matches or not all(
                        item.status == "completed"
                        for item in round_matches
                    ):
                        continue

                    players = session.query(TournamentPlayer).filter_by(
                        tournament_id=tournament_id
                    ).all()

                    advanced_count = sum(
                        1 for player in players
                        if player.status == "advanced"
                    )
                    eliminated_count = sum(
                        1 for player in players
                        if player.status == "eliminated"
                    )

                    if advanced_count == 8 and eliminated_count == 8:
                        TournamentService.start_knockout_stage(
                            tournament_id
                        )
                    elif tournament.current_round < 5:
                        TournamentService.auto_start_next_swiss_round(
                            tournament_id
                        )

                else:
                    TournamentService.auto_advance_knockout(
                        tournament_id
                    )

            return finished_count

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

    @staticmethod
    def vote(match_id: str, player_id: str, voter_fingerprint: str, voter_ip: str):
        """投票：一个指纹在同一场比赛只能投一次。"""
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
                voter_fingerprint=voter_fingerprint
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
    def get_vote_counts(match_id: str):
        """获取单场比赛两边的投票数。"""
        session = SessionLocal()

        try:
            counts = session.query(
                TournamentVote.player_id,
                func.count(TournamentVote.id)
            ).filter_by(
                match_id=match_id
            ).group_by(
                TournamentVote.player_id
            ).all()

            return {
                player_id: count
                for player_id, count in counts
            }

        finally:
            session.close()

    @staticmethod
    def get_current_state(tournament_id: int):
        """返回比赛、选手、对阵、营收和投票数据。"""
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
                TournamentMatch.match_id
            ).all()

            players_data = [{
                "id": player.player_id,
                "name": player.player_name,
                "room_id": player.room_id,
                "avatar": player.avatar_url,
                "wins": player.wins,
                "losses": player.losses,
                "status": player.status,
            } for player in players]

            matches_data = []

            for match in matches:
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

                revenue = {}

                for player_id in (match.player1_id, match.player2_id):
                    latest = session.query(TournamentRevenue).filter_by(
                        match_id=match.match_id,
                        player_id=player_id
                    ).order_by(
                        TournamentRevenue.snapshot_time.desc()
                    ).first()

                    if latest:
                        revenue[player_id] = {
                            "sc": latest.sc_amount or 0,
                            # 当前数据库模型没有 guard_amount 字段；
                            # 此字段存储 gift + guard。
                            "gift": latest.gift_amount or 0,
                            "total": latest.total_revenue or 0,
                            "danmaku": latest.danmaku_count or 0,
                            "peak_viewers": latest.peak_viewers or 0,
                            "avg_viewers": latest.avg_viewers or 0,
                        }
                    elif match.status == "completed":
                        if player_id == match.player1_id:
                            total = match.player1_revenue or 0
                        else:
                            total = match.player2_revenue or 0

                        revenue[player_id] = {
                            "sc": 0,
                            "gift": 0,
                            "total": total,
                            "danmaku": 0,
                            "peak_viewers": 0,
                            "avg_viewers": 0,
                        }
                    else:
                        revenue[player_id] = {
                            "sc": 0,
                            "gift": 0,
                            "total": 0,
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
            }

        finally:
            session.close()
