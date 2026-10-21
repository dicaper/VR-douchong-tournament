# app/services/tournament_service.py
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
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
    return datetime.now(UTC_TZ).replace(tzinfo=None)


def _parse_db_time(value):
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC_TZ).replace(tzinfo=None)

    return parsed


def _parse_api_time(value):
    if not value:
        return None

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)

    return parsed.astimezone(UTC_TZ)


def _month_strings(start_utc, end_utc):
    start_bj = start_utc.replace(tzinfo=UTC_TZ).astimezone(BEIJING_TZ)
    end_bj = end_utc.replace(tzinfo=UTC_TZ).astimezone(BEIJING_TZ)

    months = []
    cursor = start_bj.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    while cursor < end_bj:
        months.append(cursor.strftime("%Y%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return months


def _series_info(match_id):
    import re

    found = re.match(r"^(qf[1-4]|sf[1-2]|final)g([1-3])$", match_id or "")
    if not found:
        return None, None

    return found.group(1), int(found.group(2))


def _get_room_sessions(room_id, month):
    errors = []

    for base_url in DATA_API_BASES:
        try:
            response = requests.get(
                f"{base_url}/gift/live_sessions",
                params={"room_id": str(room_id), "month": month},
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            if response.status_code in (400, 404):
                errors.append(f"{base_url}: 房间/月数据不存在")
                continue

            response.raise_for_status()
            payload = response.json()

            sessions = payload.get("sessions")
            if not isinstance(sessions, list):
                raise RuntimeError("接口响应中没有 sessions 数组")

            return sessions

        except Exception as exc:
            errors.append(f"{base_url}: {exc}")

    raise RuntimeError(
        f"查询直播间 {room_id} 的 {month} 数据失败；"
        + " | ".join(errors)
    )


def _add_bucket(result, bucket, start_utc, end_utc):
    bucket_start = _parse_api_time(bucket.get("start_time"))
    bucket_end = _parse_api_time(bucket.get("end_time"))

    if not bucket_start:
        return

    if not bucket_end:
        bucket_end = bucket_start + timedelta(minutes=15)

    if bucket_end <= bucket_start:
        return

    overlap_start = max(bucket_start, start_utc.replace(tzinfo=UTC_TZ))
    overlap_end = min(bucket_end, end_utc.replace(tzinfo=UTC_TZ))
    overlap_seconds = (overlap_end - overlap_start).total_seconds()

    if overlap_seconds <= 0:
        return

    bucket_seconds = (bucket_end - bucket_start).total_seconds()
    ratio = min(1.0, overlap_seconds / bucket_seconds)

    result["gift"] += float(bucket.get("gift") or 0) * ratio
    result["guard"] += float(bucket.get("guard") or 0) * ratio
    result["sc"] += float(bucket.get("super_chat") or 0) * ratio
    result["danmaku"] += float(bucket.get("danmaku_count") or 0) * ratio

    result["peak_viewers"] = max(
        result["peak_viewers"],
        int(bucket.get("max_concurrency") or 0),
    )

    average = bucket.get("avg_concurrency")
    samples = float(bucket.get("sample_count") or 0)
    if average is not None and samples > 0:
        weighted = samples * ratio
        result["viewers_weighted"] += float(average) * weighted
        result["viewers_weight"] += weighted


def _json_match_id(match_id):
    return str(match_id)


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
            filters={"tournament_id": f"eq.{int(tournament_id)}"},
        )

    @staticmethod
    def _get_matches(tournament_id, round_number=None):
        filters = {"tournament_id": f"eq.{int(tournament_id)}"}
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

        created = db.insert(TABLE_TOURNAMENTS, {
            "name": name,
            "status": "pending",
            "current_round": 1,
            "match_duration_days": 3,
        })

        if not created:
            raise RuntimeError("MemFire 未返回新赛事记录")

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
        """
        创建第一轮固定对阵。
        start_utc 必须为 UTC naive datetime。
        """
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            raise ValueError("比赛不存在")

        existing = TournamentService._get_matches(tournament_id, 1)
        if existing:
            raise ValueError("第一轮已存在；为避免覆盖，不重复创建")

        names = [
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
            for pair in names
            for name in pair
            if name.casefold() not in by_name
        ]
        if missing:
            raise ValueError("缺少选手：" + ", ".join(missing))

        end_utc = start_utc + timedelta(hours=72)
        rows = []

        for index, (name1, name2) in enumerate(names, start=1):
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
        matches = TournamentService._get_matches(tournament_id)
        played = set()

        for match in matches:
            if int(match.get("round_number") or 0) >= ROUND_QF:
                continue
            p1, p2 = match.get("player1_id"), match.get("player2_id")
            if p1 and p2:
                played.add(frozenset((str(p1), str(p2))))

        return played

    @staticmethod
    def _pair_swiss_players(players, played):
        """
        同战绩组优先配对且不重复对手。
        某战绩组人数为奇数时，余下一人浮动至另一余数选手配对。
        """
        groups = defaultdict(list)
        for player in players:
            groups[(int(player.get("wins") or 0),
                    int(player.get("losses") or 0))].append(player)

        pairs = []
        floaters = []

        for record in sorted(groups.keys(), reverse=True):
            group = list(groups[record])
            random.shuffle(group)

            while len(group) >= 2:
                first = group.pop()
                partner_index = next(
                    (
                        index
                        for index, other in enumerate(group)
                        if frozenset((
                            str(first["player_id"]),
                            str(other["player_id"]),
                        )) not in played
                    ),
                    None,
                )

                if partner_index is None:
                    floaters.append(first)
                    continue

                second = group.pop(partner_index)
                pairs.append((first, second))

            floaters.extend(group)

        random.shuffle(floaters)

        while len(floaters) >= 2:
            first = floaters.pop()
            partner_index = next(
                (
                    index
                    for index, other in enumerate(floaters)
                    if frozenset((
                        str(first["player_id"]),
                        str(other["player_id"]),
                    )) not in played
                ),
                None,
            )

            if partner_index is None:
                raise RuntimeError(
                    f"无法避免重复对手，未生成下一轮。选手：{first['player_name']}"
                )

            second = floaters.pop(partner_index)
            pairs.append((first, second))

        if floaters:
            raise RuntimeError(
                "活动选手为奇数，当前没有定义轮空规则，未生成下一轮"
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
            return {"success": False, "error": "已到瑞士轮最大轮数"}

        players = TournamentService._get_players(tournament_id)
        active = [p for p in players if p.get("status") == "active"]

        if len(active) < 2:
            return {"success": False, "error": "活动选手不足"}

        pairs = TournamentService._pair_swiss_players(
            active,
            TournamentService._played_pairs(tournament_id),
        )

        start = _utc_now_naive()
        end = start + timedelta(days=int(
            tournament.get("match_duration_days") or 3
        ))

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
            {"current_round": next_round, "status": "running"},
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

        existing = TournamentService._get_matches(
            tournament_id,
            ROUND_QF,
        )
        if existing:
            return {"success": False, "error": "八强对局已存在"}

        players = TournamentService._get_players(tournament_id)
        qualified = [p for p in players if p.get("status") == "advanced"]

        if len(qualified) != 8:
            return {
                "success": False,
                "error": f"晋级人数为{len(qualified)}，需要8人",
            }

        random.shuffle(qualified)
        start = _utc_now_naive()
        end = start + timedelta(hours=KNOCKOUT_GAME_HOURS)

        rows = []
        for index in range(4):
            rows.append({
                "tournament_id": int(tournament_id),
                "match_id": f"qf{index + 1}g1",
                "round_number": ROUND_QF,
                "player1_id": qualified[index * 2]["player_id"],
                "player2_id": qualified[index * 2 + 1]["player_id"],
                "start_time": start.isoformat(sep=" "),
                "end_time": end.isoformat(sep=" "),
                "status": "running",
            })

        db.insert(TABLE_MATCHES, rows)
        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {"current_round": ROUND_QF, "status": "running"},
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

            winner = match.get("winner_id")
            p1 = match.get("player1_id")
            p2 = match.get("player2_id")

            if not winner or not p1 or not p2:
                continue

            loser = p2 if str(winner) == str(p1) else p1

            if str(winner) in records:
                records[str(winner)]["wins"] += 1
            if str(loser) in records:
                records[str(loser)]["losses"] += 1

        for player in players:
            record = records[str(player["player_id"])]
            wins, losses = record["wins"], record["losses"]

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
        matches = TournamentService._get_matches(
            tournament_id,
            round_number,
        )
        return sorted(
            [
                match for match in matches
                if str(match.get("match_id", "")).startswith(series_id + "g")
            ],
            key=lambda m: int(_series_info(m["match_id"])[1] or 0),
        )

    @staticmethod
    def _advance_knockout(tournament_id):
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(tournament_id)

        if not tournament:
            return

        round_number = int(tournament.get("current_round") or 0)

        if round_number == ROUND_QF:
            series_ids = ["qf1", "qf2", "qf3", "qf4"]
            next_round = ROUND_SF
            next_series_ids = ["sf1", "sf2"]
            next_pairs_source = [("qf1", "qf2"), ("qf3", "qf4")]
        elif round_number == ROUND_SF:
            series_ids = ["sf1", "sf2"]
            next_round = ROUND_FINAL
            next_series_ids = ["final"]
            next_pairs_source = [("sf1", "sf2")]
        elif round_number == ROUND_FINAL:
            series_ids = ["final"]
            next_round = None
            next_series_ids = []
            next_pairs_source = []
        else:
            return

        winners = {}

        for series_id in series_ids:
            games = TournamentService._series_games(
                tournament_id,
                series_id,
                round_number,
            )

            if not games:
                return

            completed = [g for g in games if g.get("status") == "completed"]
            if any(g.get("status") == "running" for g in games):
                return

            wins = defaultdict(int)
            for game in completed:
                if game.get("winner_id"):
                    wins[str(game["winner_id"])] += 1

            player1 = str(games[0].get("player1_id"))
            player2 = str(games[0].get("player2_id"))

            if wins[player1] >= BEST_OF_THREE_WINS:
                winners[series_id] = player1
                continue
            if wins[player2] >= BEST_OF_THREE_WINS:
                winners[series_id] = player2
                continue

            if len(games) >= 3:
                # 理论上 BO3 不应走到三局仍无胜者；留作数据异常保护。
                return

            next_game_number = len(games) + 1
            start = _utc_now_naive()
            end = start + timedelta(hours=KNOCKOUT_GAME_HOURS)
            next_match_id = f"{series_id}g{next_game_number}"

            db.insert(TABLE_MATCHES, {
                "tournament_id": int(tournament_id),
                "match_id": next_match_id,
                "round_number": round_number,
                "player1_id": games[0]["player1_id"],
                "player2_id": games[0]["player2_id"],
                "start_time": start.isoformat(sep=" "),
                "end_time": end.isoformat(sep=" "),
                "status": "running",
            })
            return

        if len(winners) != len(series_ids):
            return

        if round_number == ROUND_FINAL:
            db.update(
                TABLE_TOURNAMENTS,
                {"id": f"eq.{int(tournament_id)}"},
                {"status": "finished"},
            )
            return

        start = _utc_now_naive()
        end = start + timedelta(hours=KNOCKOUT_GAME_HOURS)
        new_rows = []

        for index, (source1, source2) in enumerate(next_pairs_source):
            player1_id = winners[source1]
            player2_id = winners[source2]
            series_id = next_series_ids[index]

            new_rows.append({
                "tournament_id": int(tournament_id),
                "match_id": f"{series_id}g1",
                "round_number": next_round,
                "player1_id": player1_id,
                "player2_id": player2_id,
                "start_time": start.isoformat(sep=" "),
                "end_time": end.isoformat(sep=" "),
                "status": "running",
            })

        db.insert(TABLE_MATCHES, new_rows)
        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {"current_round": next_round, "status": "running"},
        )

    @staticmethod
    def get_player_revenue(room_id, start_time, end_time=None):
        if not room_id or not start_time:
            raise ValueError("营收查询需要 room_id 和 start_time")

        start = _parse_db_time(start_time)
        end = _parse_db_time(end_time) if end_time else _utc_now_naive()

        if end <= start:
            return {
                "sc_amount": 0.0,
                "gift_amount": 0.0,
                "guard_amount": 0.0,
                "total_revenue": 0.0,
                "danmaku_count": 0,
                "peak_viewers": 0,
                "avg_viewers": 0,
            }

        totals = {
            "gift": 0.0,
            "guard": 0.0,
            "sc": 0.0,
            "danmaku": 0.0,
            "peak_viewers": 0,
            "viewers_weighted": 0.0,
            "viewers_weight": 0.0,
        }

        for month in _month_strings(start, end):
            sessions = _get_room_sessions(room_id, month)

            for live_session in sessions:
                buckets = live_session.get("stats_15m") or []

                if buckets:
                    for bucket in buckets:
                        _add_bucket(totals, bucket, start, end)
                    continue

                session_start = _parse_api_time(
                    live_session.get("start_time")
                )
                session_end = _parse_api_time(
                    live_session.get("end_time")
                )

                if not session_start or not session_end:
                    continue

                start_aware = start.replace(tzinfo=UTC_TZ)
                end_aware = end.replace(tzinfo=UTC_TZ)

                if session_start < start_aware or session_end > end_aware:
                    continue

                totals["gift"] += float(live_session.get("gift") or 0)
                totals["guard"] += float(live_session.get("guard") or 0)
                totals["sc"] += float(live_session.get("super_chat") or 0)
                totals["danmaku"] += float(
                    live_session.get("danmaku_count") or 0
                )
                totals["peak_viewers"] = max(
                    totals["peak_viewers"],
                    int(live_session.get("max_concurrency") or 0),
                )

        gift = round(totals["gift"], 2)
        guard = round(totals["guard"], 2)
        sc = round(totals["sc"], 2)

        return {
            "sc_amount": sc,
            "gift_amount": gift,
            "guard_amount": guard,
            "total_revenue": round(gift + guard + sc, 2),
            "danmaku_count": round(totals["danmaku"]),
            "peak_viewers": totals["peak_viewers"],
            "avg_viewers": 0,
        }

    @staticmethod
    def _save_snapshot(match, player_id, revenue, snapshot_time):
        db = TournamentService._db()

        # 表没有 guard_amount 列，因此 gift_amount 中保存 gift + guard。
        db.insert(TABLE_REVENUE, {
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
        })

    @staticmethod
    def update_match_revenue(match_id):
        db = TournamentService._db()
        rows = db.select(
            TABLE_MATCHES,
            filters={"match_id": f"eq.{_json_match_id(match_id)}"},
            limit=1,
        )

        if not rows or rows[0].get("status") != "running":
            return None

        match = rows[0]
        players = TournamentService._get_players(match["tournament_id"])
        by_id = {str(p["player_id"]): p for p in players}

        player1 = by_id.get(str(match.get("player1_id")))
        player2 = by_id.get(str(match.get("player2_id")))
        if not player1 or not player2:
            return None

        now = _utc_now_naive()
        match_start = _parse_db_time(match.get("start_time"))
        match_end = _parse_db_time(match.get("end_time"))
        cutoff = min(now, match_end) if match_end else now

        revenue1 = TournamentService.get_player_revenue(
            player1["room_id"], match_start, cutoff
        )
        revenue2 = TournamentService.get_player_revenue(
            player2["room_id"], match_start, cutoff
        )

        TournamentService._save_snapshot(
            match, player1["player_id"], revenue1, now
        )
        TournamentService._save_snapshot(
            match, player2["player_id"], revenue2, now
        )

        return {
            "match_id": match_id,
            "player1": revenue1,
            "player2": revenue2,
        }

    @staticmethod
    def _latest_snapshots(match_ids):
        if not match_ids:
            return {}

        joined = ",".join(str(item) for item in match_ids)
        rows = TournamentService._db().select(
            TABLE_REVENUE,
            filters={"match_id": f"in.({joined})"},
        )

        latest = {}
        for row in rows:
            key = (str(row["match_id"]), str(row["player_id"]))
            previous = latest.get(key)
            if not previous or str(row.get("snapshot_time", "")) > str(
                previous.get("snapshot_time", "")
            ):
                latest[key] = row

        return latest

    @staticmethod
    def get_vote_counts(match_ids):
        if isinstance(match_ids, str):
            match_ids = [match_ids]
        if not match_ids:
            return {}

        joined = ",".join(str(item) for item in match_ids)
        votes = TournamentService._db().select(
            TABLE_VOTES,
            filters={"match_id": f"in.({joined})"},
        )

        counts = defaultdict(lambda: defaultdict(int))
        for vote in votes:
            counts[str(vote["match_id"])][str(vote["player_id"])] += 1

        return {key: dict(value) for key, value in counts.items()}

    @staticmethod
    def vote(match_id, player_id, voter_fingerprint, voter_ip):
        db = TournamentService._db()

        matches = db.select(
            TABLE_MATCHES,
            filters={"match_id": f"eq.{match_id}"},
            limit=1,
        )
        if not matches:
            return {"success": False, "error": "match_not_found"}

        match = matches[0]
        if str(player_id) not in (
            str(match.get("player1_id")),
            str(match.get("player2_id")),
        ):
            return {"success": False, "error": "player_not_in_match"}

        try:
            db.insert(TABLE_VOTES, {
                "match_id": str(match_id),
                "player_id": str(player_id),
                "voter_fingerprint": str(voter_fingerprint),
                "voter_ip": voter_ip,
            })
            return {"success": True}
        except MemFireError as exc:
            if exc.status_code == 409:
                return {"success": False, "error": "already_voted"}
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
        else:
            winner_id = (
                match["player1_id"]
                if revenue1["danmaku_count"] >= revenue2["danmaku_count"]
                else match["player2_id"]
            )

        TournamentService._save_snapshot(
            match, match["player1_id"], revenue1, now
        )
        TournamentService._save_snapshot(
            match, match["player2_id"], revenue2, now
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
            players_by_id = {str(p["player_id"]): p for p in players}
            matches = TournamentService._get_matches(tournament_id)
            now = _utc_now_naive()

            for match in matches:
                if match.get("status") != "running":
                    continue

                end = _parse_db_time(match.get("end_time"))
                if not end or end > now:
                    continue

                try:
                    if TournamentService._finish_match(
                        match, players_by_id, now
                    ):
                        finished_count += 1
                except Exception as exc:
                    print(
                        f"最终营收查询失败，暂不结束 {match.get('match_id')}: {exc}"
                    )

            TournamentService._recompute_swiss_records(tournament_id)

            current_round = int(tournament.get("current_round") or 1)

            if current_round < ROUND_QF:
                current_matches = TournamentService._get_matches(
                    tournament_id,
                    current_round,
                )

                if current_matches and all(
                    item.get("status") == "completed"
                    for item in current_matches
                ):
                    current_players = TournamentService._get_players(
                        tournament_id
                    )
                    advanced = sum(
                        1 for p in current_players
                        if p.get("status") == "advanced"
                    )
                    eliminated = sum(
                        1 for p in current_players
                        if p.get("status") == "eliminated"
                    )

                    if advanced == 8 and eliminated == 8:
                        TournamentService.start_knockout_stage(tournament_id)
                    elif current_round < SWISS_MAX_ROUNDS:
                        TournamentService.auto_start_next_swiss_round(
                            tournament_id
                        )
            else:
                TournamentService._advance_knockout(tournament_id)

        return finished_count

    @staticmethod
    def update_all_running_revenues():
        db = TournamentService._db()
        matches = db.select(
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
        match_ids = [m["match_id"] for m in matches]

        snapshots = TournamentService._latest_snapshots(match_ids)
        votes = TournamentService.get_vote_counts(match_ids)

        players_by_id = {str(p["player_id"]): p for p in players}

        series_map = {}
        for match in matches:
            series_id, game_number = _series_info(match["match_id"])
            if not series_id:
                continue

            row = series_map.setdefault(series_id, {
                "series_id": series_id,
                "player1_id": match.get("player1_id"),
                "player2_id": match.get("player2_id"),
                "player1_game_wins": 0,
                "player2_game_wins": 0,
                "series_winner": None,
                "games_played": 0,
            })

            if match.get("status") == "completed":
                row["games_played"] += 1
                if str(match.get("winner_id")) == str(row["player1_id"]):
                    row["player1_game_wins"] += 1
                elif str(match.get("winner_id")) == str(row["player2_id"]):
                    row["player2_game_wins"] += 1

        for row in series_map.values():
            if row["player1_game_wins"] >= 2:
                row["series_winner"] = row["player1_id"]
            elif row["player2_game_wins"] >= 2:
                row["series_winner"] = row["player2_id"]

        matches_data = []

        for match in matches:
            revenue = {}
            for player_id in (
                match.get("player1_id"),
                match.get("player2_id"),
            ):
                if not player_id:
                    continue

                snapshot = snapshots.get(
                    (str(match["match_id"]), str(player_id))
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

            series_id, game_number = _series_info(match["match_id"])

            item = {
                "id": match["match_id"],
                "round": int(match["round_number"]),
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
            "players": [{
                "id": player["player_id"],
                "name": player["player_name"],
                "room_id": player["room_id"],
                "avatar": player.get("avatar_url"),
                "wins": int(player.get("wins") or 0),
                "losses": int(player.get("losses") or 0),
                "status": player.get("status") or "active",
            } for player in players],
            "matches": matches_data,
            "series": list(series_map.values()),
        }