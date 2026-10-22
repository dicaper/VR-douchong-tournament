# app/services/tournament_service.py
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import random

import requests

from app.memfire_rest import MemFireError, get_memfire
from app.models.tournament import (
    BEST_OF_THREE_WINS,
    KNOCKOUT_GAME_HOURS,
    ROUND_QF,
    ROUND_SF,
    ROUND_FINAL,
    SWISS_MAX_ROUNDS,
    TABLE_TOURNAMENTS,
    TABLE_PLAYERS,
    TABLE_MATCHES,
    TABLE_VOTES,
    TABLE_REVENUE,
)


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc

DATA_API_BASES = (
    "https://vr.qianqiuzy.cn",
    "https://psp.qianqiuzy.cn",
)

HTTP_TIMEOUT_SECONDS = 20


def _utc_now_naive():
    """返回数据库使用的 UTC 无时区时间。"""
    return datetime.now(UTC_TZ).replace(tzinfo=None)


def _parse_db_time(value):
    """解析数据库时间；无时区值按 UTC 解释。"""
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text in {"-", "—", "N/A", "null", "None"}:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC_TZ).replace(tzinfo=None)

    return parsed


def _parse_api_time(value):
    """解析 API 时间；无时区值按北京时间解释。"""
    if value is None:
        return None

    text = str(value).strip()
    if not text or text in {"-", "—", "N/A", "null", "None"}:
        return None

    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)

    return parsed.astimezone(UTC_TZ)


def _month_strings(start_utc_naive, end_utc_naive):
    """返回时间窗口覆盖月份，格式为 YYYYMM。"""
    start_bj = start_utc_naive.replace(
        tzinfo=UTC_TZ
    ).astimezone(BEIJING_TZ)
    end_bj = end_utc_naive.replace(
        tzinfo=UTC_TZ
    ).astimezone(BEIJING_TZ)

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


def _series_info(match_id):
    """解析淘汰赛单局 ID，例如 qf1g2 -> (qf1, 2)。"""
    import re

    found = re.match(
        r"^(qf[1-4]|sf[1-2]|final)g([1-3])$",
        str(match_id or ""),
    )

    if not found:
        return None, None

    return found.group(1), int(found.group(2))


def _get_room_sessions(room_id, month):
    """
    逐个检查 VR / PSP 数据源，按返回的 room_id 判断房间属于哪个源。
    一个源返回空场次时继续尝试另一个源，避免把错误主机的空结果当作真实零营收。
    """
    room_id = str(room_id)
    errors = []
    empty_responses = []

    for base_url in DATA_API_BASES:
        try:
            response = requests.get(
                f"{base_url}/gift/live_sessions",
                params={
                    "room_id": room_id,
                    "month": str(month),
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            if response.status_code in (400, 404):
                errors.append(f"{base_url}: 房间或月份不存在")
                continue

            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise RuntimeError("接口响应不是 JSON 对象")

            returned_room_id = payload.get("room_id")
            sessions = payload.get("sessions")

            if not isinstance(sessions, list):
                raise RuntimeError("接口响应中没有 sessions 数组")

            if (
                returned_room_id is not None
                and str(returned_room_id) != room_id
            ):
                errors.append(f"{base_url}: 返回的房间号不匹配")
                continue

            if sessions:
                return sessions

            # 返回 room_id 匹配但场次为空，先继续试另一个数据源。
            empty_responses.append((returned_room_id, sessions))

        except Exception as exc:
            errors.append(f"{base_url}: {exc}")

    if empty_responses:
        return []

    raise RuntimeError(
        f"查询直播间 {room_id} 的 {month} 数据失败；"
        + " | ".join(errors)
    )


def _new_revenue_accumulator():
    return {
        "gift": 0.0,
        "guard": 0.0,
        "sc": 0.0,
        "danmaku": 0.0,
        "peak_viewers": 0,
        "viewers_weighted": 0.0,
        "viewers_weight": 0.0,
    }


def _accumulate_bucket(result, bucket, window_start, window_end):
    """按15分钟桶与比赛窗口的重叠时长比例累计。"""
    bucket_start = _parse_api_time(bucket.get("start_time"))
    bucket_end = _parse_api_time(bucket.get("end_time"))

    if not bucket_start:
        return

    if not bucket_end:
        bucket_end = bucket_start + timedelta(minutes=15)

    if bucket_end <= bucket_start:
        return

    overlap_start = max(bucket_start, window_start)
    overlap_end = min(bucket_end, window_end)
    overlap_seconds = (overlap_end - overlap_start).total_seconds()

    if overlap_seconds <= 0:
        return

    bucket_seconds = (bucket_end - bucket_start).total_seconds()
    ratio = min(1.0, overlap_seconds / bucket_seconds)

    result["gift"] += float(bucket.get("gift") or 0) * ratio
    result["guard"] += float(bucket.get("guard") or 0) * ratio
    result["sc"] += float(bucket.get("super_chat") or 0) * ratio
    result["danmaku"] += (
        float(bucket.get("danmaku_count") or 0) * ratio
    )

    result["peak_viewers"] = max(
        result["peak_viewers"],
        int(bucket.get("max_concurrency") or 0),
    )

    average = bucket.get("avg_concurrency")
    sample_count = float(bucket.get("sample_count") or 0)

    if average is not None and sample_count > 0:
        weighted = sample_count * ratio
        result["viewers_weighted"] += float(average) * weighted
        result["viewers_weight"] += weighted


def _accumulate_session_summary(result, live_session, ratio=1.0):
    """累加场次汇总；部分重叠时按 ratio 估算。"""
    result["gift"] += float(live_session.get("gift") or 0) * ratio
    result["guard"] += float(live_session.get("guard") or 0) * ratio
    result["sc"] += float(live_session.get("super_chat") or 0) * ratio
    result["danmaku"] += (
        float(live_session.get("danmaku_count") or 0) * ratio
    )

    result["peak_viewers"] = max(
        result["peak_viewers"],
        int(live_session.get("max_concurrency") or 0),
    )

    average = live_session.get("avg_concurrency")
    session_start = _parse_api_time(live_session.get("start_time"))
    session_end = _parse_api_time(live_session.get("end_time"))

    if average is not None and session_start and session_end:
        duration = max(
            1.0,
            (session_end - session_start).total_seconds() * ratio,
        )
        result["viewers_weighted"] += float(average) * duration
        result["viewers_weight"] += duration


class TournamentService:

    @staticmethod
    def _db():
        return get_memfire()

    @staticmethod
    def _get_tournament(tournament_id):
        rows = TournamentService._db().select(
            TABLE_TOURNAMENTS,
            filters={"id": f"eq.{int(tournament_id)}"},
            limit=1,
        )
        return rows[0] if rows else None

    @staticmethod
    def _get_players(tournament_id):
        return TournamentService._db().select(
            TABLE_PLAYERS,
            filters={
                "tournament_id": f"eq.{int(tournament_id)}",
            },
        )

    @staticmethod
    def _get_matches(tournament_id, round_number=None):
        filters = {
            "tournament_id": f"eq.{int(tournament_id)}",
        }

        if round_number is not None:
            filters["round_number"] = f"eq.{int(round_number)}"

        rows = TournamentService._db().select(
            TABLE_MATCHES,
            filters=filters,
        )

        return sorted(
            rows,
            key=lambda row: (
                int(row.get("round_number") or 0),
                str(row.get("match_id") or ""),
            ),
        )

    @staticmethod
    def init_tournament(name, players):
        db = TournamentService._db()

        created = db.insert(
            TABLE_TOURNAMENTS,
            {
                "name": name,
                "status": "pending",
                "current_round": 1,
                "match_duration_days": 3,
            },
        )

        if not created:
            raise RuntimeError("MemFire 没有返回新赛事记录")

        tournament_id = created[0]["id"]

        try:
            player_rows = []

            for player in players:
                player_rows.append({
                    "tournament_id": tournament_id,
                    "player_id": str(player["id"]),
                    "player_name": player["name"],
                    "room_id": str(player["room_id"]),
                    "avatar_url": player.get("avatar_url"),
                    "seed": int(player.get("seed", 999)),
                    "wins": 0,
                    "losses": 0,
                    "status": "active",
                })

            db.insert(TABLE_PLAYERS, player_rows)
            return tournament_id

        except Exception:
            db.delete(
                TABLE_TOURNAMENTS,
                {"id": f"eq.{tournament_id}"},
            )
            raise

    @staticmethod
    def schedule_first_round(tournament_id, start_utc):
        """创建固定第一轮；start_utc 是 UTC naive datetime。"""
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            raise ValueError("比赛不存在")

        existing = TournamentService._get_matches(tournament_id, 1)
        if existing:
            raise ValueError("第一轮已存在，为避免覆盖未重复创建")

        pairings = [
            ("Ameki", "chu2u"),
            ("Nagisa", "Rhea"),
            ("Mizuki", "Hazel"),
            ("Liko", "Izayoi"),
            ("SUI", "Harei"),
            ("Viridis", "Sumire"),
            ("Kloa", "Melody"),
            ("Mit3uri", "Shiori"),
        ]

        players = TournamentService._get_players(tournament_id)
        by_name = {
            str(player["player_name"]).strip().casefold(): player
            for player in players
        }

        missing = [
            name
            for pair in pairings
            for name in pair
            if name.casefold() not in by_name
        ]

        if missing:
            raise ValueError("缺少选手：" + ", ".join(missing))

        end_utc = start_utc + timedelta(hours=72)
        rows = []

        for index, (name1, name2) in enumerate(pairings, start=1):
            rows.append({
                "tournament_id": int(tournament_id),
                "match_id": f"r1m{index}",
                "round_number": 1,
                "player1_id": by_name[name1.casefold()]["player_id"],
                "player2_id": by_name[name2.casefold()]["player_id"],
                "start_time": start_utc.isoformat(sep=" "),
                "end_time": end_utc.isoformat(sep=" "),
                "status": "pending",
            })

        db.insert(TABLE_MATCHES, rows)

        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {
                "start_time": start_utc.isoformat(sep=" "),
                "current_round": 1,
                "match_duration_days": 3,
                "status": "pending",
            },
        )

        return {
            "success": True,
            "matches_created": 8,
            "start_time_utc": start_utc.isoformat(),
            "end_time_utc": end_utc.isoformat(),
        }

    @staticmethod
    def _played_pairs(tournament_id):
        played = set()

        for match in TournamentService._get_matches(tournament_id):
            if int(match.get("round_number") or 0) >= ROUND_QF:
                continue

            player1 = match.get("player1_id")
            player2 = match.get("player2_id")

            if player1 and player2:
                played.add(frozenset((str(player1), str(player2))))

        return played

    @staticmethod
    def _pair_swiss_players(players, played):
        """优先同战绩配对，并尝试避免重复对手。"""
        groups = defaultdict(list)

        for player in players:
            record = (
                int(player.get("wins") or 0),
                int(player.get("losses") or 0),
            )
            groups[record].append(player)

        pairs = []
        floaters = []

        for record in sorted(groups.keys(), reverse=True):
            group = list(groups[record])
            random.shuffle(group)

            while len(group) >= 2:
                player1 = group.pop()

                partner_index = next(
                    (
                        index
                        for index, player2 in enumerate(group)
                        if frozenset((
                            str(player1["player_id"]),
                            str(player2["player_id"]),
                        )) not in played
                    ),
                    None,
                )

                if partner_index is None:
                    floaters.append(player1)
                    continue

                player2 = group.pop(partner_index)
                pairs.append((player1, player2))

            floaters.extend(group)

        random.shuffle(floaters)

        while len(floaters) >= 2:
            player1 = floaters.pop()

            partner_index = next(
                (
                    index
                    for index, player2 in enumerate(floaters)
                    if frozenset((
                        str(player1["player_id"]),
                        str(player2["player_id"]),
                    )) not in played
                ),
                None,
            )

            if partner_index is None:
                raise RuntimeError(
                    f"无法避免重复配对：{player1.get('player_name')}"
                )

            player2 = floaters.pop(partner_index)
            pairs.append((player1, player2))

        if floaters:
            raise RuntimeError(
                "活动选手人数为奇数，当前没有定义轮空规则"
            )

        return pairs

    @staticmethod
    def auto_start_next_swiss_round(tournament_id):
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            return {"success": False, "error": "比赛不存在"}

        current_round = int(tournament.get("current_round") or 1)
        next_round = current_round + 1

        if next_round > SWISS_MAX_ROUNDS:
            return {"success": False, "error": "已达到瑞士轮最大轮数"}

        active_players = [
            player
            for player in TournamentService._get_players(tournament_id)
            if player.get("status") == "active"
        ]

        if len(active_players) < 2:
            return {"success": False, "error": "活动选手不足"}

        pairs = TournamentService._pair_swiss_players(
            active_players,
            TournamentService._played_pairs(tournament_id),
        )

        start = _utc_now_naive()
        end = start + timedelta(
            days=int(tournament.get("match_duration_days") or 3)
        )

        rows = []

        for index, (player1, player2) in enumerate(pairs, start=1):
            rows.append({
                "tournament_id": int(tournament_id),
                "match_id": f"r{next_round}m{index}",
                "round_number": next_round,
                "player1_id": player1["player_id"],
                "player2_id": player2["player_id"],
                "start_time": start.isoformat(sep=" "),
                "end_time": end.isoformat(sep=" "),
                "status": "running",
            })

        db.insert(TABLE_MATCHES, rows)

        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {
                "current_round": next_round,
                "status": "running",
            },
        )

        return {
            "success": True,
            "round": next_round,
            "matches": len(rows),
        }

    @staticmethod
    def start_knockout_stage(tournament_id):
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            return {"success": False, "error": "比赛不存在"}

        if TournamentService._get_matches(tournament_id, ROUND_QF):
            return {"success": False, "error": "八强比赛已存在"}

        qualified = [
            player
            for player in TournamentService._get_players(tournament_id)
            if player.get("status") == "advanced"
        ]

        if len(qualified) != 8:
            return {
                "success": False,
                "error": f"晋级人数为{len(qualified)}，需要8人",
            }

        random.shuffle(qualified)

        start = _utc_now_naive()
        end = start + timedelta(hours=KNOCKOUT_GAME_HOURS)

        db.insert(
            TABLE_MATCHES,
            [
                {
                    "tournament_id": int(tournament_id),
                    "match_id": f"qf{index + 1}g1",
                    "round_number": ROUND_QF,
                    "player1_id": qualified[index * 2]["player_id"],
                    "player2_id": qualified[index * 2 + 1]["player_id"],
                    "start_time": start.isoformat(sep=" "),
                    "end_time": end.isoformat(sep=" "),
                    "status": "running",
                }
                for index in range(4)
            ],
        )

        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {
                "current_round": ROUND_QF,
                "status": "running",
            },
        )

        return {
            "success": True,
            "stage": "quarterfinals",
            "matches_created": 4,
            "format": "BO3",
        }

    @staticmethod
    def _recompute_swiss_records(tournament_id):
        db = TournamentService._db()
        players = TournamentService._get_players(tournament_id)
        matches = TournamentService._get_matches(tournament_id)

        records = {
            str(player["player_id"]): {"wins": 0, "losses": 0}
            for player in players
        }

        for match in matches:
            if int(match.get("round_number") or 0) >= ROUND_QF:
                continue

            if match.get("status") != "completed":
                continue

            winner_id = match.get("winner_id")
            player1_id = match.get("player1_id")
            player2_id = match.get("player2_id")

            if not winner_id or not player1_id or not player2_id:
                continue

            loser_id = (
                player2_id
                if str(winner_id) == str(player1_id)
                else player1_id
            )

            if str(winner_id) in records:
                records[str(winner_id)]["wins"] += 1

            if str(loser_id) in records:
                records[str(loser_id)]["losses"] += 1

        for player in players:
            record = records[str(player["player_id"])]
            wins = record["wins"]
            losses = record["losses"]

            if wins >= 3:
                status = "advanced"
            elif losses >= 3:
                status = "eliminated"
            else:
                status = "active"

            db.update(
                TABLE_PLAYERS,
                {"player_id": f"eq.{player['player_id']}"},
                {
                    "wins": wins,
                    "losses": losses,
                    "status": status,
                },
            )

    @staticmethod
    def _series_games(tournament_id, series_id, round_number):
        games = [
            match
            for match in TournamentService._get_matches(
                tournament_id,
                round_number,
            )
            if str(match.get("match_id", "")).startswith(series_id + "g")
        ]

        return sorted(
            games,
            key=lambda match: (
                _series_info(match.get("match_id"))[1] or 0
            ),
        )

    @staticmethod
    def _advance_knockout(tournament_id):
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            return

        current_round = int(tournament.get("current_round") or 0)

        if current_round == ROUND_QF:
            series_ids = ["qf1", "qf2", "qf3", "qf4"]
            next_round = ROUND_SF
            next_pairs = [("qf1", "qf2"), ("qf3", "qf4")]
            next_series_ids = ["sf1", "sf2"]
        elif current_round == ROUND_SF:
            series_ids = ["sf1", "sf2"]
            next_round = ROUND_FINAL
            next_pairs = [("sf1", "sf2")]
            next_series_ids = ["final"]
        elif current_round == ROUND_FINAL:
            series_ids = ["final"]
            next_round = None
            next_pairs = []
            next_series_ids = []
        else:
            return

        series_winners = {}

        for series_id in series_ids:
            games = TournamentService._series_games(
                tournament_id,
                series_id,
                current_round,
            )

            if not games:
                return

            if any(game.get("status") == "running" for game in games):
                return

            wins = defaultdict(int)

            for game in games:
                if game.get("status") == "completed" and game.get("winner_id"):
                    wins[str(game["winner_id"])] += 1

            player1_id = str(games[0].get("player1_id"))
            player2_id = str(games[0].get("player2_id"))

            if wins[player1_id] >= BEST_OF_THREE_WINS:
                series_winners[series_id] = player1_id
                continue

            if wins[player2_id] >= BEST_OF_THREE_WINS:
                series_winners[series_id] = player2_id
                continue

            if len(games) >= 3:
                print(
                    f"淘汰赛数据异常：{series_id} 已完成3局但无人达到2胜"
                )
                return

            next_game_number = len(games) + 1
            start = _utc_now_naive()
            end = start + timedelta(hours=KNOCKOUT_GAME_HOURS)

            db.insert(
                TABLE_MATCHES,
                {
                    "tournament_id": int(tournament_id),
                    "match_id": f"{series_id}g{next_game_number}",
                    "round_number": current_round,
                    "player1_id": games[0]["player1_id"],
                    "player2_id": games[0]["player2_id"],
                    "start_time": start.isoformat(sep=" "),
                    "end_time": end.isoformat(sep=" "),
                    "status": "running",
                },
            )
            return

        if len(series_winners) != len(series_ids):
            return

        if current_round == ROUND_FINAL:
            db.update(
                TABLE_TOURNAMENTS,
                {"id": f"eq.{int(tournament_id)}"},
                {"status": "finished"},
            )
            return

        start = _utc_now_naive()
        end = start + timedelta(hours=KNOCKOUT_GAME_HOURS)
        next_matches = []

        for index, (source1, source2) in enumerate(next_pairs):
            next_matches.append({
                "tournament_id": int(tournament_id),
                "match_id": f"{next_series_ids[index]}g1",
                "round_number": next_round,
                "player1_id": series_winners[source1],
                "player2_id": series_winners[source2],
                "start_time": start.isoformat(sep=" "),
                "end_time": end.isoformat(sep=" "),
                "status": "running",
            })

        db.insert(TABLE_MATCHES, next_matches)

        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {
                "current_round": next_round,
                "status": "running",
            },
        )

    @staticmethod
    def get_player_revenue(room_id, start_time, end_time=None):
        """
        统计本场比赛时间窗口里的全部直播营收。

        - 完整落在比赛窗口内的直播，使用该场直播的汇总值；
        - 跨过比赛开始或结束边界的直播，使用15分钟桶按重叠时间统计；
        - 正在直播、end_time为空或为 '-'，按查询时刻作为临时结束时间；
        - 营收总额 = 礼物 + 守护 + SC。
        """
        if not room_id or not start_time:
            raise ValueError("营收查询需要 room_id 和 start_time")

        window_start_naive = _parse_db_time(start_time)
        window_end_naive = (
            _parse_db_time(end_time)
            if end_time
            else _utc_now_naive()
        )

        if window_start_naive is None or window_end_naive is None:
            raise ValueError("比赛时间格式无效")

        if window_end_naive <= window_start_naive:
            return {
                "sc_amount": 0.0,
                "gift_amount": 0.0,
                "guard_amount": 0.0,
                "total_revenue": 0.0,
                "danmaku_count": 0,
                "peak_viewers": 0,
                "avg_viewers": 0,
            }

        window_start = window_start_naive.replace(tzinfo=UTC_TZ)
        window_end = window_end_naive.replace(tzinfo=UTC_TZ)
        now_utc = datetime.now(UTC_TZ)
        totals = _new_revenue_accumulator()

        for month in _month_strings(
            window_start_naive,
            window_end_naive,
        ):
            sessions = _get_room_sessions(room_id, month)

            for live_session in sessions:
                session_start = _parse_api_time(
                    live_session.get("start_time")
                )

                if session_start is None:
                    continue

                session_end = _parse_api_time(
                    live_session.get("end_time")
                )

                is_active = session_end is None

                if is_active:
                    effective_end = min(now_utc, window_end)
                else:
                    effective_end = session_end

                if (
                    effective_end <= window_start
                    or session_start >= window_end
                ):
                    continue

                overlap_start = max(session_start, window_start)
                overlap_end = min(effective_end, window_end)
                overlap_seconds = (
                    overlap_end - overlap_start
                ).total_seconds()

                if overlap_seconds <= 0:
                    continue

                # 直播完整处于比赛窗口内时，顶层场次汇总是该场直播当前总额。
                # 这包括从比赛开始后开播、目前仍在直播的场次。
                fully_inside_window = (
                    session_start >= window_start
                    and effective_end <= window_end
                )

                if fully_inside_window:
                    _accumulate_session_summary(
                        totals,
                        live_session,
                        1.0,
                    )
                    continue

                # 直播跨越比赛起止边界时，用分桶数据裁出窗口内部分。
                buckets = live_session.get("stats_15m") or []

                if buckets:
                    for bucket in buckets:
                        _accumulate_bucket(
                            totals,
                            bucket,
                            window_start,
                            window_end,
                        )
                    continue

                # 没有分桶时只能按直播时长占比估算边界直播。
                session_duration = max(
                    1.0,
                    (effective_end - session_start).total_seconds(),
                )
                ratio = min(1.0, overlap_seconds / session_duration)

                _accumulate_session_summary(
                    totals,
                    live_session,
                    ratio,
                )

        gift = round(totals["gift"], 2)
        guard = round(totals["guard"], 2)
        sc = round(totals["sc"], 2)

        avg_viewers = 0
        if totals["viewers_weight"] > 0:
            avg_viewers = round(
                totals["viewers_weighted"]
                / totals["viewers_weight"]
            )

        return {
            "sc_amount": sc,
            "gift_amount": gift,
            "guard_amount": guard,
            "total_revenue": round(gift + guard + sc, 2),
            "danmaku_count": round(totals["danmaku"]),
            "peak_viewers": totals["peak_viewers"],
            "avg_viewers": avg_viewers,
        }

    @staticmethod
    def _save_snapshot(match, player_id, revenue, snapshot_time):
        """保存快照；当前表没有 guard_amount 列，gift 列存 gift+guard。"""
        TournamentService._db().insert(
            TABLE_REVENUE,
            {
                "player_id": str(player_id),
                "match_id": str(match["match_id"]),
                "sc_amount": revenue["sc_amount"],
                "gift_amount": (
                    revenue["gift_amount"] + revenue["guard_amount"]
                ),
                "total_revenue": revenue["total_revenue"],
                "danmaku_count": revenue["danmaku_count"],
                "peak_viewers": revenue["peak_viewers"],
                "avg_viewers": revenue["avg_viewers"],
                "snapshot_time": snapshot_time.isoformat(sep=" "),
            },
        )

    @staticmethod
    def update_match_revenue(match_id):
        db = TournamentService._db()

        rows = db.select(
            TABLE_MATCHES,
            filters={"match_id": f"eq.{match_id}"},
            limit=1,
        )

        if not rows or rows[0].get("status") != "running":
            return None

        match = rows[0]
        players = TournamentService._get_players(
            match["tournament_id"]
        )
        player_map = {
            str(player["player_id"]): player
            for player in players
        }

        player1 = player_map.get(str(match.get("player1_id")))
        player2 = player_map.get(str(match.get("player2_id")))

        if not player1 or not player2:
            return None

        now = _utc_now_naive()
        start = _parse_db_time(match.get("start_time"))
        match_end = _parse_db_time(match.get("end_time"))
        cutoff = min(now, match_end) if match_end else now

        revenue1 = TournamentService.get_player_revenue(
            player1["room_id"],
            start,
            cutoff,
        )
        revenue2 = TournamentService.get_player_revenue(
            player2["room_id"],
            start,
            cutoff,
        )

        TournamentService._save_snapshot(
            match,
            player1["player_id"],
            revenue1,
            now,
        )
        TournamentService._save_snapshot(
            match,
            player2["player_id"],
            revenue2,
            now,
        )

        return {
            "match_id": match_id,
            "player1": revenue1,
            "player2": revenue2,
        }

    @staticmethod
    def _latest_snapshots(match_ids):
        """每场、每位选手取时间最新的快照；按 datetime 比较而非字符串比较。"""
        if not match_ids:
            return {}

        joined = ",".join(str(match_id) for match_id in match_ids)

        rows = TournamentService._db().select(
            TABLE_REVENUE,
            filters={"match_id": f"in.({joined})"},
        )

        latest = {}

        for row in rows:
            key = (
                str(row.get("match_id")),
                str(row.get("player_id")),
            )
            current_time = _parse_db_time(row.get("snapshot_time"))

            if current_time is None:
                continue

            previous = latest.get(key)
            previous_time = (
                _parse_db_time(previous.get("snapshot_time"))
                if previous
                else None
            )

            if previous_time is None or current_time > previous_time:
                latest[key] = row

        return latest

    @staticmethod
    def get_vote_counts(match_ids):
        if isinstance(match_ids, str):
            match_ids = [match_ids]

        if not match_ids:
            return {}

        joined = ",".join(str(match_id) for match_id in match_ids)

        rows = TournamentService._db().select(
            TABLE_VOTES,
            filters={"match_id": f"in.({joined})"},
        )

        counts = defaultdict(lambda: defaultdict(int))

        for vote in rows:
            counts[str(vote["match_id"])][str(vote["player_id"])] += 1

        return {
            match_id: dict(player_counts)
            for match_id, player_counts in counts.items()
        }

    @staticmethod
    def vote(match_id, player_id, voter_fingerprint, voter_ip):
        if not voter_fingerprint:
            return {
                "success": False,
                "error": "missing_fingerprint",
            }

        db = TournamentService._db()

        matches = db.select(
            TABLE_MATCHES,
            filters={"match_id": f"eq.{match_id}"},
            limit=1,
        )

        if not matches:
            return {
                "success": False,
                "error": "match_not_found",
            }

        match = matches[0]
        eligible = {
            str(match.get("player1_id")),
            str(match.get("player2_id")),
        }

        if str(player_id) not in eligible:
            return {
                "success": False,
                "error": "player_not_in_match",
            }

        try:
            db.insert(
                TABLE_VOTES,
                {
                    "match_id": str(match_id),
                    "player_id": str(player_id),
                    "voter_fingerprint": str(voter_fingerprint),
                    "voter_ip": voter_ip,
                },
            )
            return {"success": True}

        except MemFireError as exc:
            if exc.status_code == 409:
                return {
                    "success": False,
                    "error": "already_voted",
                }
            raise

    @staticmethod
    def _finish_match(match, players_by_id, now):
        player1 = players_by_id.get(str(match.get("player1_id")))
        player2 = players_by_id.get(str(match.get("player2_id")))

        if not player1 or not player2:
            return False

        revenue1 = TournamentService.get_player_revenue(
            player1["room_id"],
            match.get("start_time"),
            match.get("end_time"),
        )
        revenue2 = TournamentService.get_player_revenue(
            player2["room_id"],
            match.get("start_time"),
            match.get("end_time"),
        )

        if revenue1["total_revenue"] > revenue2["total_revenue"]:
            winner_id = match["player1_id"]
        elif revenue2["total_revenue"] > revenue1["total_revenue"]:
            winner_id = match["player2_id"]
        elif revenue1["danmaku_count"] >= revenue2["danmaku_count"]:
            winner_id = match["player1_id"]
        else:
            winner_id = match["player2_id"]

        TournamentService._save_snapshot(
            match,
            match["player1_id"],
            revenue1,
            now,
        )
        TournamentService._save_snapshot(
            match,
            match["player2_id"],
            revenue2,
            now,
        )

        TournamentService._db().update(
            TABLE_MATCHES,
            {"match_id": f"eq.{match['match_id']}"},
            {
                "winner_id": str(winner_id),
                "player1_revenue": revenue1["total_revenue"],
                "player2_revenue": revenue2["total_revenue"],
                "status": "completed",
            },
        )

        return True

    @staticmethod
    def check_and_finish_matches():
        db = TournamentService._db()
        tournaments = db.select(TABLE_TOURNAMENTS)
        finished_count = 0

        for tournament in tournaments:
            tournament_id = int(tournament["id"])
            players = TournamentService._get_players(tournament_id)
            players_by_id = {
                str(player["player_id"]): player
                for player in players
            }
            matches = TournamentService._get_matches(tournament_id)
            now = _utc_now_naive()

            for match in matches:
                if match.get("status") != "running":
                    continue

                end_time = _parse_db_time(match.get("end_time"))
                if end_time is None or end_time > now:
                    continue

                try:
                    if TournamentService._finish_match(
                        match,
                        players_by_id,
                        now,
                    ):
                        finished_count += 1
                except Exception as exc:
                    print(
                        f"最终营收查询失败，暂不结束 "
                        f"{match.get('match_id')}: {exc}"
                    )

            TournamentService._recompute_swiss_records(tournament_id)

            current_round = int(tournament.get("current_round") or 1)

            if current_round < ROUND_QF:
                current_matches = TournamentService._get_matches(
                    tournament_id,
                    current_round,
                )

                if (
                    current_matches
                    and all(
                        match.get("status") == "completed"
                        for match in current_matches
                    )
                ):
                    refreshed_players = TournamentService._get_players(
                        tournament_id
                    )

                    advanced = sum(
                        1
                        for player in refreshed_players
                        if player.get("status") == "advanced"
                    )
                    eliminated = sum(
                        1
                        for player in refreshed_players
                        if player.get("status") == "eliminated"
                    )

                    if advanced == 8 and eliminated == 8:
                        TournamentService.start_knockout_stage(
                            tournament_id
                        )
                    elif current_round < SWISS_MAX_ROUNDS:
                        TournamentService.auto_start_next_swiss_round(
                            tournament_id
                        )
            else:
                TournamentService._advance_knockout(tournament_id)

        return finished_count

    @staticmethod
    def update_all_running_revenues():
        matches = TournamentService._db().select(
            TABLE_MATCHES,
            filters={"status": "eq.running"},
        )

        updated = 0

        for match in matches:
            try:
                result = TournamentService.update_match_revenue(
                    match["match_id"]
                )
                if result:
                    updated += 1
            except Exception as exc:
                print(
                    f"更新比赛 {match.get('match_id')} 营收失败: {exc}"
                )

        return updated

    @staticmethod
    def run_scheduler_cycle(update_revenue=True):
        if update_revenue:
            TournamentService.update_all_running_revenues()

        return TournamentService.check_and_finish_matches()

    @staticmethod
    def get_current_state(tournament_id):
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            return None

        players = TournamentService._get_players(tournament_id)
        matches = TournamentService._get_matches(tournament_id)
        match_ids = [match["match_id"] for match in matches]
        snapshots = TournamentService._latest_snapshots(match_ids)
        votes = TournamentService.get_vote_counts(match_ids)

        series_map = {}

        for match in matches:
            series_id, game_number = _series_info(match.get("match_id"))

            if not series_id:
                continue

            series = series_map.setdefault(
                series_id,
                {
                    "series_id": series_id,
                    "player1_id": match.get("player1_id"),
                    "player2_id": match.get("player2_id"),
                    "player1_game_wins": 0,
                    "player2_game_wins": 0,
                    "series_winner": None,
                    "games_played": 0,
                },
            )

            if match.get("status") == "completed":
                series["games_played"] += 1

                if str(match.get("winner_id")) == str(series["player1_id"]):
                    series["player1_game_wins"] += 1
                elif str(match.get("winner_id")) == str(series["player2_id"]):
                    series["player2_game_wins"] += 1

        for series in series_map.values():
            if series["player1_game_wins"] >= BEST_OF_THREE_WINS:
                series["series_winner"] = series["player1_id"]
            elif series["player2_game_wins"] >= BEST_OF_THREE_WINS:
                series["series_winner"] = series["player2_id"]

        matches_data = []

        for match in matches:
            revenue = {}

            for player_id in (
                match.get("player1_id"),
                match.get("player2_id"),
            ):
                if player_id is None:
                    continue

                snapshot = snapshots.get(
                    (
                        str(match["match_id"]),
                        str(player_id),
                    )
                )

                if snapshot:
                    revenue[str(player_id)] = {
                        "sc": snapshot.get("sc_amount") or 0,
                        "gift": snapshot.get("gift_amount") or 0,
                        "total": snapshot.get("total_revenue") or 0,
                        "danmaku": snapshot.get("danmaku_count") or 0,
                        "peak_viewers": snapshot.get("peak_viewers") or 0,
                        "avg_viewers": snapshot.get("avg_viewers") or 0,
                    }
                else:
                    revenue[str(player_id)] = {
                        "sc": 0,
                        "gift": 0,
                        "total": 0,
                        "danmaku": 0,
                        "peak_viewers": 0,
                        "avg_viewers": 0,
                    }

            series_id, game_number = _series_info(match.get("match_id"))

            item = {
                "id": match["match_id"],
                "round": int(match.get("round_number") or 0),
                "p1": match.get("player1_id"),
                "p2": match.get("player2_id"),
                "winner": match.get("winner_id"),
                "status": match.get("status"),
                "start_time": match.get("start_time"),
                "end_time": match.get("end_time"),
                "revenue": revenue,
                "votes": votes.get(str(match["match_id"]), {}),
            }

            if series_id:
                item["series_id"] = series_id
                item["game_number"] = game_number
                item["series"] = series_map[series_id]

            matches_data.append(item)

        return {
            "tournament": {
                "id": tournament["id"],
                "name": tournament["name"],
                "status": tournament["status"],
                "start_time": tournament.get("start_time"),
                "current_round": int(tournament.get("current_round") or 1),
                "match_duration_days": int(
                    tournament.get("match_duration_days") or 3
                ),
            },
            "players": [
                {
                    "id": player["player_id"],
                    "name": player["player_name"],
                    "room_id": player["room_id"],
                    "avatar": player.get("avatar_url"),
                    "wins": int(player.get("wins") or 0),
                    "losses": int(player.get("losses") or 0),
                    "status": player.get("status") or "active",
                }
                for player in players
            ],
            "matches": matches_data,
            "series": list(series_map.values()),
        }