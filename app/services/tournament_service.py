# app/services/tournament_service.py

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import random
import re

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
    """返回当前 UTC 无时区时间，兼容现有数据库时间字段。"""
    return datetime.now(UTC_TZ).replace(tzinfo=None)


def _parse_db_time(value):
    """
    解析数据库时间。

    当前数据库中的无时区比赛时间按 UTC 解释，
    再由调用方转换为北京时间进行业务日期筛选。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()

        if not text or text in {
            "-",
            "—",
            "N/A",
            "null",
            "None",
        }:
            return None

        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

    if parsed.tzinfo is not None:
        return parsed.astimezone(
            UTC_TZ
        ).replace(tzinfo=None)

    return parsed


def _parse_api_time(value):
    """
    解析上游直播接口时间。

    上游接口返回的无时区时间按北京时间解释。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()

        if not text or text in {
            "-",
            "—",
            "N/A",
            "null",
            "None",
        }:
            return None

        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=BEIJING_TZ
        )

    return parsed.astimezone(UTC_TZ)


def _to_decimal(
    value,
    field_name,
    room_id,
    date_text,
):
    """
    把接口数值安全转换成 Decimal，
    避免二进制浮点累计误差。
    """
    try:
        return Decimal(str(value or 0))
    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ) as exc:
        raise ValueError(
            f"直播间 {room_id} 在 {date_text} 的 "
            f"{field_name} 字段不是有效金额：{value!r}"
        ) from exc


def _month_strings(
    start_day,
    end_day_exclusive,
):
    """
    返回日期区间覆盖的月份，格式 YYYYMM。
    """
    months = []
    cursor = start_day.replace(day=1)

    while cursor < end_day_exclusive:
        months.append(
            cursor.strftime("%Y%m")
        )

        if cursor.month == 12:
            cursor = cursor.replace(
                year=cursor.year + 1,
                month=1,
            )
        else:
            cursor = cursor.replace(
                month=cursor.month + 1,
            )

    return months


def _month_strings_for_utc_window(
    start_utc,
    end_utc,
):
    """
    返回 UTC 时间窗口按北京时间转换后覆盖的月份。
    """
    start_bj = start_utc.astimezone(
        BEIJING_TZ
    )
    end_bj = end_utc.astimezone(
        BEIJING_TZ
    )

    start_month = start_bj.date().replace(
        day=1
    )
    end_month = end_bj.date().replace(
        day=1
    )

    months = []
    cursor = start_month

    while cursor <= end_month:
        months.append(
            cursor.strftime("%Y%m")
        )

        if cursor.month == 12:
            cursor = cursor.replace(
                year=cursor.year + 1,
                month=1,
            )
        else:
            cursor = cursor.replace(
                month=cursor.month + 1,
            )

    return months


def _series_info(match_id):
    """
    解析淘汰赛单局 ID，例如 qf1g2 -> (qf1, 2)。
    """
    found = re.match(
        r"^(qf[1-4]|sf[1-2]|final)g([1-3])$",
        str(match_id or ""),
    )

    if not found:
        return None, None

    return found.group(1), int(found.group(2))


def _get_room_attention(room_id, month):
    """
    从 /gift/attention 获取某房间某月的日汇总。

    该接口只负责营收：
        gift
        guard
        super_chat
    """
    room_id = str(room_id)
    errors = []
    matching_empty_response = None

    for base_url in DATA_API_BASES:
        try:
            response = requests.get(
                f"{base_url}/gift/attention",
                params={
                    "room_id": room_id,
                    "month": str(month),
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            if response.status_code in (
                400,
                404,
            ):
                errors.append(
                    f"{base_url}: 房间或月份数据不存在"
                )
                continue

            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise RuntimeError(
                    "接口响应不是 JSON 对象"
                )

            returned_room_id = payload.get(
                "room_id"
            )

            if (
                returned_room_id is not None
                and str(returned_room_id) != room_id
            ):
                errors.append(
                    f"{base_url}: 返回的 room_id 不匹配"
                )
                continue

            rows = payload.get("attention")

            if not isinstance(rows, list):
                raise RuntimeError(
                    "接口响应中没有 attention 数组"
                )

            if rows:
                return rows

            # 房间号匹配但当月为空，
            # 继续检查另一个数据源。
            matching_empty_response = []

        except Exception as exc:
            errors.append(
                f"{base_url}: {exc}"
            )

    if matching_empty_response is not None:
        return matching_empty_response

    raise RuntimeError(
        f"查询直播间 {room_id} 的 {month} 日营收失败；"
        + " | ".join(errors)
    )


def _get_room_live_sessions(room_id, month):
    """
    从 /gift/live_sessions 获取某房间某月的直播场次。

    该接口只负责：
        danmaku_count
        max_concurrency
        avg_concurrency
    """
    room_id = str(room_id)
    errors = []
    matching_empty_response = None

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

            if response.status_code in (
                400,
                404,
            ):
                errors.append(
                    f"{base_url}: 房间或月份数据不存在"
                )
                continue

            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise RuntimeError(
                    "接口响应不是 JSON 对象"
                )

            returned_room_id = payload.get(
                "room_id"
            )

            if (
                returned_room_id is not None
                and str(returned_room_id) != room_id
            ):
                errors.append(
                    f"{base_url}: 返回的 room_id 不匹配"
                )
                continue

            rows = payload.get("sessions")

            if not isinstance(rows, list):
                raise RuntimeError(
                    "接口响应中没有 sessions 数组"
                )

            if rows:
                return rows

            matching_empty_response = []

        except Exception as exc:
            errors.append(
                f"{base_url}: {exc}"
            )

    if matching_empty_response is not None:
        return matching_empty_response

    raise RuntimeError(
        f"查询直播间 {room_id} 的 {month} 直播场次失败；"
        + " | ".join(errors)
    )


def _get_live_status_map():
    """
    从 /gift 获取当前所有直播间状态。

    返回：

    {
        "27628019": {
            "is_live": True,
            "current_concurrency": 1234
        }
    }

    VR 和 PSP 两个数据源会合并，
    通过 room_id 精确对应。
    """
    status_map = {}
    errors = []
    success_count = 0

    for base_url in DATA_API_BASES:
        try:
            response = requests.get(
                f"{base_url}/gift",
                timeout=HTTP_TIMEOUT_SECONDS,
            )

            if response.status_code in (
                400,
                404,
            ):
                errors.append(
                    f"{base_url}: HTTP {response.status_code}"
                )
                continue

            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, list):
                rows = payload

            elif isinstance(payload, dict):
                rows = payload.get("data")

                if rows is None:
                    rows = payload.get("rooms")

                if rows is None:
                    rows = []

            else:
                raise RuntimeError(
                    "接口响应不是房间数组"
                )

            if not isinstance(rows, list):
                raise RuntimeError(
                    "接口响应中的房间数据不是数组"
                )

            success_count += 1

            for row in rows:
                if not isinstance(row, dict):
                    continue

                room_id = row.get("room_id")

                if room_id is None:
                    continue

                status = row.get("status")

                try:
                    is_live = int(status or 0) == 1
                except (
                    TypeError,
                    ValueError,
                ):
                    is_live = str(
                        status
                    ).strip().lower() in {
                        "1",
                        "true",
                        "live",
                        "running",
                    }

                current_concurrency = row.get(
                    "current_concurrency"
                )

                if current_concurrency is not None:
                    try:
                        current_concurrency = int(
                            current_concurrency
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        current_concurrency = None

                status_map[str(room_id)] = {
                    "is_live": is_live,
                    "current_concurrency": (
                        current_concurrency
                        if is_live
                        else None
                    ),
                }

        except Exception as exc:
            errors.append(
                f"{base_url}: {exc}"
            )

    if success_count == 0:
        raise RuntimeError(
            "查询当前直播状态失败；"
            + " | ".join(errors)
        )

    return status_map


def _session_number(
    session,
    *names,
):
    """
    从直播场次对象读取整数指标。
    """
    for name in names:
        value = session.get(name)

        if value is None:
            continue

        try:
            return int(value)
        except (
            TypeError,
            ValueError,
        ):
            continue

    return 0


def _session_float(
    session,
    *names,
):
    """
    从直播场次对象读取浮点指标。
    """
    for name in names:
        value = session.get(name)

        if value is None:
            continue

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            continue

    return 0.0


def _get_live_metrics(
    room_id,
    start_time,
    end_time,
):
    """
    根据比赛时间窗口读取直播指标。

    比赛数据库时间先按现有数据库约定解析为 UTC，
    再转换到北京时间确定月份；
    上游直播场次无时区时间按北京时间解析。

    返回：

    {
        "danmaku_count": 123,
        "peak_viewers": 456,
        "avg_viewers": 234
    }
    """
    if not room_id or not start_time:
        return {
            "danmaku_count": 0,
            "peak_viewers": 0,
            "avg_viewers": 0,
        }

    start_naive = _parse_db_time(
        start_time
    )

    if start_naive is None:
        return {
            "danmaku_count": 0,
            "peak_viewers": 0,
            "avg_viewers": 0,
        }

    if end_time is None:
        end_naive = _utc_now_naive()
    else:
        end_naive = _parse_db_time(
            end_time
        )

    if end_naive is None:
        end_naive = _utc_now_naive()

    start_utc = start_naive.replace(
        tzinfo=UTC_TZ
    )
    end_utc = end_naive.replace(
        tzinfo=UTC_TZ
    )

    if end_utc <= start_utc:
        return {
            "danmaku_count": 0,
            "peak_viewers": 0,
            "avg_viewers": 0,
        }

    total_danmaku = 0
    peak_viewers = 0

    weighted_avg_sum = 0.0
    weighted_seconds = 0.0

    months = _month_strings_for_utc_window(
        start_utc,
        end_utc,
    )

    for month in months:
        sessions = _get_room_live_sessions(
            room_id,
            month,
        )

        for session in sessions:
            if not isinstance(session, dict):
                continue

            session_start = _parse_api_time(
                session.get("start_time")
            )

            if session_start is None:
                continue

            session_end = _parse_api_time(
                session.get("end_time")
            )

            # 未结束直播场次按当前比赛窗口结束时间处理。
            if session_end is None:
                session_end = end_utc

            overlap_start = max(
                start_utc,
                session_start,
            )

            overlap_end = min(
                end_utc,
                session_end,
            )

            if overlap_end <= overlap_start:
                continue

            overlap_seconds = (
                overlap_end - overlap_start
            ).total_seconds()

            danmaku_count = _session_number(
                session,
                "danmaku_count",
                "danmaku",
            )

            max_concurrency = _session_number(
                session,
                "max_concurrency",
                "peak_viewers",
                "peak_concurrency",
            )

            avg_concurrency = _session_float(
                session,
                "avg_concurrency",
                "avg_viewers",
                "average_concurrency",
            )

            total_danmaku += max(
                danmaku_count,
                0,
            )

            peak_viewers = max(
                peak_viewers,
                max_concurrency,
            )

            if avg_concurrency > 0:
                weighted_avg_sum += (
                    avg_concurrency
                    * overlap_seconds
                )
                weighted_seconds += (
                    overlap_seconds
                )

    if weighted_seconds > 0:
        avg_viewers = round(
            weighted_avg_sum
            / weighted_seconds
        )
    else:
        avg_viewers = 0

    return {
        "danmaku_count": total_danmaku,
        "peak_viewers": peak_viewers,
        "avg_viewers": avg_viewers,
    }


class TournamentService:

    @staticmethod
    def _db():
        return get_memfire()

    @staticmethod
    def _get_tournament(tournament_id):
        rows = TournamentService._db().select(
            TABLE_TOURNAMENTS,
            filters={
                "id": f"eq.{int(tournament_id)}",
            },
            limit=1,
        )

        return rows[0] if rows else None

    @staticmethod
    def _get_players(tournament_id):
        return TournamentService._db().select(
            TABLE_PLAYERS,
            filters={
                "tournament_id": (
                    f"eq.{int(tournament_id)}"
                ),
            },
        )

    @staticmethod
    def _get_matches(
        tournament_id,
        round_number=None,
    ):
        filters = {
            "tournament_id": (
                f"eq.{int(tournament_id)}"
            ),
        }

        if round_number is not None:
            filters["round_number"] = (
                f"eq.{int(round_number)}"
            )

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
        """
        通过 MemFire REST 创建赛事和参赛选手。
        """
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
            raise RuntimeError(
                "MemFire 没有返回新赛事记录"
            )

        tournament_id = created[0]["id"]

        try:
            player_rows = []

            for player in players:
                player_rows.append({
                    "tournament_id": tournament_id,
                    "player_id": str(
                        player["id"]
                    ),
                    "player_name": player["name"],
                    "room_id": str(
                        player["room_id"]
                    ),
                    "avatar_url": player.get(
                        "avatar_url"
                    ),
                    "seed": int(
                        player.get("seed", 999)
                    ),
                    "wins": 0,
                    "losses": 0,
                    "status": "active",
                })

            db.insert(
                TABLE_PLAYERS,
                player_rows,
            )

            return tournament_id

        except Exception:
            db.delete(
                TABLE_TOURNAMENTS,
                {
                    "id": f"eq.{tournament_id}",
                },
            )
            raise

    @staticmethod
    def schedule_first_round(
        tournament_id,
        start_utc,
    ):
        """
        创建固定第一轮。

        start_utc 为现有数据库使用的 UTC 无时区时间。
        """
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(
            tournament_id
        )

        if not tournament:
            raise ValueError("比赛不存在")

        existing = TournamentService._get_matches(
            tournament_id,
            1,
        )

        if existing:
            raise ValueError(
                "第一轮已存在，为避免覆盖，本次没有创建"
            )

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

        players = TournamentService._get_players(
            tournament_id
        )

        by_name = {
            str(player["player_name"])
            .strip()
            .casefold(): player
            for player in players
        }

        missing = [
            name
            for pair in pairings
            for name in pair
            if name.casefold() not in by_name
        ]

        if missing:
            raise ValueError(
                "缺少选手：" + ", ".join(missing)
            )

        end_utc = start_utc + timedelta(
            hours=72
        )

        rows = []

        for index, (name1, name2) in enumerate(
            pairings,
            start=1,
        ):
            rows.append({
                "tournament_id": int(
                    tournament_id
                ),
                "match_id": f"r1m{index}",
                "round_number": 1,
                "player1_id": by_name[
                    name1.casefold()
                ]["player_id"],
                "player2_id": by_name[
                    name2.casefold()
                ]["player_id"],
                "start_time": start_utc.isoformat(
                    sep=" "
                ),
                "end_time": end_utc.isoformat(
                    sep=" "
                ),
                "status": "pending",
            })

        db.insert(
            TABLE_MATCHES,
            rows,
        )

        db.update(
            TABLE_TOURNAMENTS,
            {
                "id": f"eq.{int(tournament_id)}",
            },
            {
                "start_time": start_utc.isoformat(
                    sep=" "
                ),
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

        for match in TournamentService._get_matches(
            tournament_id
        ):
            if int(
                match.get("round_number") or 0
            ) >= ROUND_QF:
                continue

            player1_id = match.get(
                "player1_id"
            )
            player2_id = match.get(
                "player2_id"
            )

            if player1_id and player2_id:
                played.add(
                    frozenset((
                        str(player1_id),
                        str(player2_id),
                    ))
                )

        return played

    @staticmethod
    def _pair_swiss_players(players, played):
        """
        ??????????????????????

        ???????????????????????
        ??????????????????

        ????
        1. ?????????
        2. ???????????????
        3. ?????????????????????

        ???????????????????????
        ???????????????
        """
        from functools import lru_cache

        pool = list(players)

        if not pool:
            return []

        if len(pool) % 2:
            raise RuntimeError(
                f"??????????{len(pool)}??"
                "??????????"
            )

        random.shuffle(pool)

        player_count = len(pool)
        full_mask = (1 << player_count) - 1

        records = [
            (
                int(player.get("wins") or 0),
                int(player.get("losses") or 0),
            )
            for player in pool
        ]

        played_pairs = {
            frozenset(str(player_id) for player_id in pair)
            for pair in played
        }

        # ?????????????
        # ??????????????????????
        rematch_penalty = 1_000_000_000

        @lru_cache(maxsize=None)
        def solve(mask):
            if mask == 0:
                return 0, ()

            # ??????????????????????
            first_bit = mask & -mask
            first_index = first_bit.bit_length() - 1
            remaining = mask ^ first_bit

            candidates = []
            bits = remaining

            while bits:
                bit = bits & -bits
                candidate_index = bit.bit_length() - 1
                candidates.append(candidate_index)
                bits ^= bit

            # ?????????????????????
            random.shuffle(candidates)

            best_cost = None
            best_pairs = None

            for partner_index in candidates:
                partner_bit = 1 << partner_index
                next_mask = remaining ^ partner_bit

                later_cost, later_pairs = solve(next_mask)

                first_player = pool[first_index]
                second_player = pool[partner_index]

                pair_key = frozenset((
                    str(first_player["player_id"]),
                    str(second_player["player_id"]),
                ))

                is_rematch = pair_key in played_pairs

                first_record = records[first_index]
                second_record = records[partner_index]

                record_distance = (
                    abs(first_record[0] - second_record[0])
                    + abs(first_record[1] - second_record[1])
                )

                pair_cost = (
                    (rematch_penalty if is_rematch else 0)
                    + record_distance * 1000
                )

                total_cost = pair_cost + later_cost

                if best_cost is None or total_cost < best_cost:
                    best_cost = total_cost
                    best_pairs = (
                        ((first_index, partner_index),)
                        + later_pairs
                    )

            return best_cost, best_pairs

        _, index_pairs = solve(full_mask)

        if not index_pairs or len(index_pairs) * 2 != player_count:
            raise RuntimeError("????????????????")

        return [
            (pool[first_index], pool[second_index])
            for first_index, second_index in index_pairs
        ]

    @staticmethod
    def auto_start_next_swiss_round(
        tournament_id,
    ):
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(
            tournament_id
        )

        if not tournament:
            return {
                "success": False,
                "error": "比赛不存在",
            }

        current_round = int(
            tournament.get("current_round") or 1
        )

        next_round = current_round + 1

        if next_round > SWISS_MAX_ROUNDS:
            return {
                "success": False,
                "error": "已达到瑞士轮最大轮数",
            }

        active_players = [
            player
            for player in TournamentService._get_players(
                tournament_id
            )
            if player.get("status") == "active"
        ]

        if len(active_players) < 2:
            return {
                "success": False,
                "error": "活动选手不足",
            }

        pairs = TournamentService._pair_swiss_players(
            active_players,
            TournamentService._played_pairs(
                tournament_id
            ),
        )

        start = _utc_now_naive()

        end = start + timedelta(
            days=int(
                tournament.get(
                    "match_duration_days"
                ) or 3
            )
        )

        rows = []

        for index, (player1, player2) in enumerate(
            pairs,
            start=1,
        ):
            rows.append({
                "tournament_id": int(
                    tournament_id
                ),
                "match_id": (
                    f"r{next_round}m{index}"
                ),
                "round_number": next_round,
                "player1_id": player1[
                    "player_id"
                ],
                "player2_id": player2[
                    "player_id"
                ],
                "start_time": start.isoformat(
                    sep=" "
                ),
                "end_time": end.isoformat(
                    sep=" "
                ),
                "status": "running",
            })

        db.insert(
            TABLE_MATCHES,
            rows,
        )

        db.update(
            TABLE_TOURNAMENTS,
            {
                "id": f"eq.{int(tournament_id)}",
            },
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
    def start_knockout_stage(
        tournament_id,
    ):
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(
            tournament_id
        )

        if not tournament:
            return {
                "success": False,
                "error": "比赛不存在",
            }

        if TournamentService._get_matches(
            tournament_id,
            ROUND_QF,
        ):
            return {
                "success": False,
                "error": "八强比赛已存在",
            }

        qualified = [
            player
            for player in TournamentService._get_players(
                tournament_id
            )
            if player.get("status") == "advanced"
        ]

        if len(qualified) != 8:
            return {
                "success": False,
                "error": (
                    f"晋级人数为{len(qualified)}，需要8人"
                ),
            }

        random.shuffle(qualified)

        start = _utc_now_naive()
        end = start + timedelta(
            hours=KNOCKOUT_GAME_HOURS
        )

        db.insert(
            TABLE_MATCHES,
            [
                {
                    "tournament_id": int(
                        tournament_id
                    ),
                    "match_id": (
                        f"qf{index + 1}g1"
                    ),
                    "round_number": ROUND_QF,
                    "player1_id": qualified[
                        index * 2
                    ]["player_id"],
                    "player2_id": qualified[
                        index * 2 + 1
                    ]["player_id"],
                    "start_time": start.isoformat(
                        sep=" "
                    ),
                    "end_time": end.isoformat(
                        sep=" "
                    ),
                    "status": "running",
                }
                for index in range(4)
            ],
        )

        db.update(
            TABLE_TOURNAMENTS,
            {
                "id": f"eq.{int(tournament_id)}",
            },
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
    def _recompute_swiss_records(
        tournament_id,
    ):
        db = TournamentService._db()
        players = TournamentService._get_players(
            tournament_id
        )
        matches = TournamentService._get_matches(
            tournament_id
        )

        records = {
            str(player["player_id"]): {
                "wins": 0,
                "losses": 0,
            }
            for player in players
        }

        for match in matches:
            if int(
                match.get("round_number") or 0
            ) >= ROUND_QF:
                continue

            if match.get("status") != "completed":
                continue

            winner_id = match.get("winner_id")
            player1_id = match.get(
                "player1_id"
            )
            player2_id = match.get(
                "player2_id"
            )

            if (
                not winner_id
                or not player1_id
                or not player2_id
            ):
                continue

            loser_id = (
                player2_id
                if str(winner_id) == str(player1_id)
                else player1_id
            )

            if str(winner_id) in records:
                records[str(winner_id)][
                    "wins"
                ] += 1

            if str(loser_id) in records:
                records[str(loser_id)][
                    "losses"
                ] += 1

        for player in players:
            record = records[
                str(player["player_id"])
            ]

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
                {
                    "player_id": (
                        f"eq.{player['player_id']}"
                    ),
                },
                {
                    "wins": wins,
                    "losses": losses,
                    "status": status,
                },
            )

    @staticmethod
    def _series_games(
        tournament_id,
        series_id,
        round_number,
    ):
        games = [
            match
            for match in TournamentService._get_matches(
                tournament_id,
                round_number,
            )
            if str(
                match.get("match_id", "")
            ).startswith(series_id + "g")
        ]

        return sorted(
            games,
            key=lambda match: (
                _series_info(
                    match.get("match_id")
                )[1] or 0
            ),
        )

    @staticmethod
    def _advance_knockout(tournament_id):
        """
        ??????????? BO3 ????

        ??????????????????????
        ??????????????????????
        2 ????????????????
        """
        db = TournamentService._db()
        tournament = TournamentService._get_tournament(
            tournament_id
        )

        if not tournament:
            return

        current_round = int(
            tournament.get("current_round") or 0
        )

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
                # ???????????????????????
                continue

            player1_id = str(games[0].get("player1_id"))
            player2_id = str(games[0].get("player2_id"))

            wins = defaultdict(int)

            for game in games:
                if (
                    game.get("status") == "completed"
                    and game.get("winner_id")
                ):
                    wins[str(game["winner_id"])] += 1

            if wins[player1_id] >= BEST_OF_THREE_WINS:
                series_winners[series_id] = player1_id
                continue

            if wins[player2_id] >= BEST_OF_THREE_WINS:
                series_winners[series_id] = player2_id
                continue

            # ????????????????????????
            if any(
                game.get("status") in {"running", "pending"}
                for game in games
            ):
                continue

            if len(games) >= 3:
                print(
                    f"????????{series_id} ???3??"
                    "?????2?"
                )
                continue

            # ?????????????????
            # ?????????? game_number ???
            next_game_number = len(games) + 1

            start = _utc_now_naive()
            end = start + timedelta(
                hours=KNOCKOUT_GAME_HOURS
            )

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

        # ????????????????????????
        if len(series_winners) != len(series_ids):
            return

        if current_round == ROUND_FINAL:
            db.update(
                TABLE_TOURNAMENTS,
                {"id": f"eq.{int(tournament_id)}"},
                {"status": "finished"},
            )
            return

        # ?????????????????????????
        start = _utc_now_naive()
        end = start + timedelta(
            hours=KNOCKOUT_GAME_HOURS
        )

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
    def get_player_revenue(
        room_id,
        start_time,
        end_time=None,
    ):
        """
        获取比赛窗口内的完整数据。

        营收：
            /gift/attention

        直播指标：
            /gift/live_sessions

        /gift/attention 仍然是唯一营收来源。
        """
        if not room_id or not start_time:
            raise ValueError(
                "营收查询需要 room_id 和 start_time"
            )

        start_naive = _parse_db_time(
            start_time
        )

        end_naive = (
            _parse_db_time(end_time)
            if end_time is not None
            else _utc_now_naive()
        )

        if (
            start_naive is None
            or end_naive is None
        ):
            raise ValueError(
                "比赛开始或结束时间无效"
            )

        if end_naive <= start_naive:
            return {
                "sc_amount": 0.0,
                "gift_amount": 0.0,
                "guard_amount": 0.0,
                "total_revenue": 0.0,
                "danmaku_count": 0,
                "peak_viewers": 0,
                "avg_viewers": 0,
            }

        start_bj = start_naive.replace(
            tzinfo=UTC_TZ
        ).astimezone(BEIJING_TZ)

        end_bj = end_naive.replace(
            tzinfo=UTC_TZ
        ).astimezone(BEIJING_TZ)

        first_day = start_bj.date()
        end_day_exclusive = end_bj.date()

        # /gift/attention 是按北京时间日期汇总。
        # 如果结束时间不是北京时间当天零点，
        # 结束日期也属于比赛窗口。
        if end_bj.time() != datetime.min.time():
            end_day_exclusive += timedelta(
                days=1
            )

        if end_day_exclusive <= first_day:
            end_day_exclusive = (
                first_day + timedelta(days=1)
            )

        total_gift = Decimal("0")
        total_guard = Decimal("0")
        total_sc = Decimal("0")

        cursor = first_day.replace(day=1)

        while cursor < end_day_exclusive:
            month = cursor.strftime("%Y%m")

            daily_rows = _get_room_attention(
                room_id,
                month,
            )

            for row in daily_rows:
                date_text = str(
                    row.get("date", "")
                ).strip()

                try:
                    row_day = datetime.strptime(
                        date_text,
                        "%Y%m%d",
                    ).date()
                except ValueError:
                    continue

                if row_day < first_day:
                    continue

                if row_day >= end_day_exclusive:
                    continue

                total_gift += _to_decimal(
                    row.get("gift"),
                    "gift",
                    room_id,
                    date_text,
                )

                total_guard += _to_decimal(
                    row.get("guard"),
                    "guard",
                    room_id,
                    date_text,
                )

                total_sc += _to_decimal(
                    row.get("super_chat"),
                    "super_chat",
                    room_id,
                    date_text,
                )

            cursor = (
                cursor.replace(
                    year=cursor.year + 1,
                    month=1,
                )
                if cursor.month == 12
                else cursor.replace(
                    month=cursor.month + 1
                )
            )

        gift_value = total_gift.quantize(
            Decimal("0.01")
        )
        guard_value = total_guard.quantize(
            Decimal("0.01")
        )
        sc_value = total_sc.quantize(
            Decimal("0.01")
        )
        total_value = (
            total_gift
            + total_guard
            + total_sc
        ).quantize(Decimal("0.01"))

        # 直播指标单独读取。
        # 如果上游直播指标接口临时失败，
        # 营收仍然保留，指标暂时使用0。
        try:
            metrics = _get_live_metrics(
                room_id,
                start_time,
                end_time,
            )
        except Exception as exc:
            print(
                f"读取直播指标失败，"
                f"room_id={room_id}: {exc}"
            )
            metrics = {
                "danmaku_count": 0,
                "peak_viewers": 0,
                "avg_viewers": 0,
            }

        return {
            "sc_amount": float(sc_value),
            "gift_amount": float(gift_value),
            "guard_amount": float(guard_value),
            "total_revenue": float(total_value),
            "danmaku_count": int(
                metrics["danmaku_count"]
            ),
            "peak_viewers": int(
                metrics["peak_viewers"]
            ),
            "avg_viewers": int(
                metrics["avg_viewers"]
            ),
        }

    @staticmethod
    def _save_snapshot(
        match,
        player_id,
        revenue,
        snapshot_time,
    ):
        """
        当前表没有 guard_amount 列，
        因此 gift_amount 保存 gift+guard；
        total_revenue 保存三类收入总和。
        """
        TournamentService._db().insert(
            TABLE_REVENUE,
            {
                "player_id": str(player_id),
                "match_id": str(
                    match["match_id"]
                ),
                "sc_amount": revenue[
                    "sc_amount"
                ],
                "gift_amount": (
                    revenue["gift_amount"]
                    + revenue["guard_amount"]
                ),
                "total_revenue": revenue[
                    "total_revenue"
                ],
                "danmaku_count": revenue[
                    "danmaku_count"
                ],
                "peak_viewers": revenue[
                    "peak_viewers"
                ],
                "avg_viewers": revenue[
                    "avg_viewers"
                ],
                "snapshot_time": snapshot_time.isoformat(
                    sep=" "
                ),
            },
        )

    @staticmethod
    def update_match_revenue(match_id):
        db = TournamentService._db()

        rows = db.select(
            TABLE_MATCHES,
            filters={
                "match_id": f"eq.{match_id}",
            },
            limit=1,
        )

        if (
            not rows
            or rows[0].get("status") != "running"
        ):
            return None

        match = rows[0]

        players = TournamentService._get_players(
            match["tournament_id"]
        )

        player_map = {
            str(player["player_id"]): player
            for player in players
        }

        player1 = player_map.get(
            str(match.get("player1_id"))
        )
        player2 = player_map.get(
            str(match.get("player2_id"))
        )

        if not player1 or not player2:
            return None

        now = _utc_now_naive()

        start = _parse_db_time(
            match.get("start_time")
        )

        match_end = _parse_db_time(
            match.get("end_time")
        )

        cutoff = (
            min(now, match_end)
            if match_end
            else now
        )

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
        """
        对每场比赛、每位选手直接查询最新快照。
        """
        if not match_ids:
            return {}

        db = TournamentService._db()

        joined_ids = ",".join(
            str(match_id)
            for match_id in match_ids
        )

        matches = db.select(
            TABLE_MATCHES,
            filters={
                "match_id": f"in.({joined_ids})",
            },
            select=(
                "match_id,"
                "player1_id,"
                "player2_id"
            ),
        )

        latest = {}

        for match in matches:
            match_id = str(
                match.get("match_id") or ""
            )

            if not match_id:
                continue

            player_ids = {
                str(match.get("player1_id")),
                str(match.get("player2_id")),
            }

            player_ids.discard("None")

            for player_id in player_ids:
                rows = db.select(
                    TABLE_REVENUE,
                    filters={
                        "match_id": (
                            f"eq.{match_id}"
                        ),
                        "player_id": (
                            f"eq.{player_id}"
                        ),
                    },
                    order="snapshot_time.desc",
                    limit=1,
                )

                if rows:
                    latest[
                        (match_id, player_id)
                    ] = rows[0]

        return latest

    @staticmethod
    def get_vote_counts(match_ids):
        if isinstance(match_ids, str):
            match_ids = [match_ids]

        if not match_ids:
            return {}

        joined = ",".join(
            str(match_id)
            for match_id in match_ids
        )

        rows = TournamentService._db().select(
            TABLE_VOTES,
            filters={
                "match_id": f"in.({joined})",
            },
        )

        counts = defaultdict(
            lambda: defaultdict(int)
        )

        for vote in rows:
            counts[
                str(vote["match_id"])
            ][
                str(vote["player_id"])
            ] += 1

        return {
            match_id: dict(player_counts)
            for match_id, player_counts
            in counts.items()
        }

    @staticmethod
    def vote(
        match_id,
        player_id,
        voter_fingerprint,
        voter_ip,
    ):
        if not voter_fingerprint:
            return {
                "success": False,
                "error": "missing_fingerprint",
            }

        db = TournamentService._db()

        matches = db.select(
            TABLE_MATCHES,
            filters={
                "match_id": f"eq.{match_id}",
            },
            limit=1,
        )

        if not matches:
            return {
                "success": False,
                "error": "match_not_found",
            }

        match = matches[0]

        eligible_ids = {
            str(match.get("player1_id")),
            str(match.get("player2_id")),
        }

        if str(player_id) not in eligible_ids:
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
                    "voter_fingerprint": str(
                        voter_fingerprint
                    ),
                    "voter_ip": voter_ip,
                },
            )

            return {
                "success": True,
            }

        except MemFireError as exc:
            if exc.status_code == 409:
                return {
                    "success": False,
                    "error": "already_voted",
                }

            raise

    @staticmethod
    def _finish_match(
        match,
        players_by_id,
        now,
    ):
        player1 = players_by_id.get(
            str(match.get("player1_id"))
        )

        player2 = players_by_id.get(
            str(match.get("player2_id"))
        )

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

        if (
            revenue1["total_revenue"]
            > revenue2["total_revenue"]
        ):
            winner_id = match["player1_id"]

        elif (
            revenue2["total_revenue"]
            > revenue1["total_revenue"]
        ):
            winner_id = match["player2_id"]

        elif (
            revenue1["danmaku_count"]
            >= revenue2["danmaku_count"]
        ):
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
            {
                "match_id": (
                    f"eq.{match['match_id']}"
                ),
            },
            {
                "winner_id": str(winner_id),
                "player1_revenue": revenue1[
                    "total_revenue"
                ],
                "player2_revenue": revenue2[
                    "total_revenue"
                ],
                "status": "completed",
            },
        )

        return True

    @staticmethod
    def check_and_finish_matches():
        db = TournamentService._db()
        tournaments = db.select(
            TABLE_TOURNAMENTS
        )

        finished_count = 0

        for tournament in tournaments:
            tournament_id = int(
                tournament["id"]
            )

            players = TournamentService._get_players(
                tournament_id
            )

            players_by_id = {
                str(player["player_id"]): player
                for player in players
            }

            matches = TournamentService._get_matches(
                tournament_id
            )

            now = _utc_now_naive()

            for match in matches:
                if match.get("status") != "running":
                    continue

                end_time = _parse_db_time(
                    match.get("end_time")
                )

                if (
                    end_time is None
                    or end_time > now
                ):
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
                        "最终营收查询失败，暂不结束 "
                        f"{match.get('match_id')}: {exc}"
                    )

            TournamentService._recompute_swiss_records(
                tournament_id
            )

            current_round = int(
                tournament.get("current_round") or 1
            )

            if current_round < ROUND_QF:
                current_matches = (
                    TournamentService._get_matches(
                        tournament_id,
                        current_round,
                    )
                )

                if (
                    current_matches
                    and all(
                        match.get("status")
                        == "completed"
                        for match in current_matches
                    )
                ):
                    refreshed_players = (
                        TournamentService._get_players(
                            tournament_id
                        )
                    )

                    advanced = sum(
                        1
                        for player
                        in refreshed_players
                        if player.get("status")
                        == "advanced"
                    )

                    eliminated = sum(
                        1
                        for player
                        in refreshed_players
                        if player.get("status")
                        == "eliminated"
                    )

                    if (
                        advanced == 8
                        and eliminated == 8
                    ):
                        TournamentService.start_knockout_stage(
                            tournament_id
                        )

                    elif current_round < SWISS_MAX_ROUNDS:
                        TournamentService.auto_start_next_swiss_round(
                            tournament_id
                        )

            else:
                TournamentService._advance_knockout(
                    tournament_id
                )

        return finished_count

    @staticmethod
    def update_all_running_revenues():
        matches = TournamentService._db().select(
            TABLE_MATCHES,
            filters={
                "status": "eq.running",
            },
        )

        updated = 0

        for match in matches:
            try:
                result = (
                    TournamentService.update_match_revenue(
                        match["match_id"]
                    )
                )

                if result:
                    updated += 1

            except Exception as exc:
                print(
                    f"更新比赛 "
                    f"{match.get('match_id')} 营收失败: {exc}"
                )

        return updated

    @staticmethod
    def run_scheduler_cycle(
        update_revenue=True,
    ):
        if update_revenue:
            TournamentService.update_all_running_revenues()

        return TournamentService.check_and_finish_matches()

    @staticmethod
    def get_current_state(tournament_id):
        tournament = TournamentService._get_tournament(
            tournament_id
        )

        if not tournament:
            return None

        players = TournamentService._get_players(
            tournament_id
        )

        matches = TournamentService._get_matches(
            tournament_id
        )

        match_ids = [
            match["match_id"]
            for match in matches
        ]

        snapshots = TournamentService._latest_snapshots(
            match_ids
        )

        votes = TournamentService.get_vote_counts(
            match_ids
        )

        try:
            live_status_map = _get_live_status_map()
        except Exception as exc:
            print(
                f"读取当前直播状态失败：{exc}"
            )
            live_status_map = {}

        series_map = {}

        for match in matches:
            series_id, game_number = _series_info(
                match.get("match_id")
            )

            if not series_id:
                continue

            series = series_map.setdefault(
                series_id,
                {
                    "series_id": series_id,
                    "player1_id": match.get(
                        "player1_id"
                    ),
                    "player2_id": match.get(
                        "player2_id"
                    ),
                    "player1_game_wins": 0,
                    "player2_game_wins": 0,
                    "series_winner": None,
                    "games_played": 0,
                },
            )

            if match.get("status") == "completed":
                series["games_played"] += 1

                if str(
                    match.get("winner_id")
                ) == str(
                    series["player1_id"]
                ):
                    series[
                        "player1_game_wins"
                    ] += 1

                elif str(
                    match.get("winner_id")
                ) == str(
                    series["player2_id"]
                ):
                    series[
                        "player2_game_wins"
                    ] += 1

        for series in series_map.values():
            if (
                series["player1_game_wins"]
                >= BEST_OF_THREE_WINS
            ):
                series["series_winner"] = (
                    series["player1_id"]
                )

            elif (
                series["player2_game_wins"]
                >= BEST_OF_THREE_WINS
            ):
                series["series_winner"] = (
                    series["player2_id"]
                )

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
                    revenue[
                        str(player_id)
                    ] = {
                        "sc": snapshot.get(
                            "sc_amount"
                        ) or 0,
                        "gift": snapshot.get(
                            "gift_amount"
                        ) or 0,
                        "total": snapshot.get(
                            "total_revenue"
                        ) or 0,
                        "danmaku": snapshot.get(
                            "danmaku_count"
                        ) or 0,
                        "peak_viewers": snapshot.get(
                            "peak_viewers"
                        ) or 0,
                        "avg_viewers": snapshot.get(
                            "avg_viewers"
                        ) or 0,
                    }

                else:
                    revenue[
                        str(player_id)
                    ] = {
                        "sc": 0,
                        "gift": 0,
                        "total": 0,
                        "danmaku": 0,
                        "peak_viewers": 0,
                        "avg_viewers": 0,
                    }

            series_id, game_number = _series_info(
                match.get("match_id")
            )

            item = {
                "id": match["match_id"],
                "round": int(
                    match.get("round_number") or 0
                ),
                "p1": match.get("player1_id"),
                "p2": match.get("player2_id"),
                "winner": match.get("winner_id"),
                "status": match.get("status"),
                "start_time": match.get(
                    "start_time"
                ),
                "end_time": match.get(
                    "end_time"
                ),
                "revenue": revenue,
                "votes": votes.get(
                    str(match["match_id"]),
                    {},
                ),
            }

            if series_id:
                item["series_id"] = series_id
                item["game_number"] = game_number
                item["series"] = series_map[
                    series_id
                ]

            matches_data.append(item)

        players_data = []

        for player in players:
            room_id = str(
                player.get("room_id") or ""
            )

            live_info = live_status_map.get(
                room_id,
                {
                    "is_live": False,
                    "current_concurrency": None,
                },
            )

            players_data.append({
                "id": player["player_id"],
                "name": player["player_name"],
                "room_id": player["room_id"],
                "avatar": player.get(
                    "avatar_url"
                ),
                "wins": int(
                    player.get("wins") or 0
                ),
                "losses": int(
                    player.get("losses") or 0
                ),
                "status": player.get(
                    "status"
                ) or "active",
                "is_live": bool(
                    live_info.get("is_live")
                ),
                "current_concurrency": (
                    live_info.get(
                        "current_concurrency"
                    )
                    if live_info.get("is_live")
                    else None
                ),
                "live_url": (
                    f"https://live.bilibili.com/"
                    f"{room_id}"
                    if room_id
                    else None
                ),
            })

        return {
            "tournament": {
                "id": tournament["id"],
                "name": tournament["name"],
                "status": tournament["status"],
                "start_time": tournament.get(
                    "start_time"
                ),
                "current_round": int(
                    tournament.get(
                        "current_round"
                    ) or 1
                ),
                "match_duration_days": int(
                    tournament.get(
                        "match_duration_days"
                    ) or 3
                ),
            },
            "players": players_data,
            "matches": matches_data,
            "series": list(
                series_map.values()
            ),
        }
