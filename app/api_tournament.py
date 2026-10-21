# app/api_tournament.py
from datetime import datetime, timedelta
from functools import wraps
import os
import random

from flask import Blueprint, jsonify, request

from app.memfire_rest import get_memfire
from app.models.tournament import (
    ROUND_QF,
    ROUND_SF,
    ROUND_FINAL,
    TABLE_TOURNAMENTS,
    TABLE_PLAYERS,
    TABLE_MATCHES,
    TABLE_REVENUE,
    TABLE_VOTES,
)
from app.services.tournament_service import TournamentService


bp = Blueprint("tournament", __name__, url_prefix="/api/tournament")

ADMIN_TOKEN = os.environ.get(
    "ADMIN_TOKEN",
    "my_secret_password_123",
)


def admin_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return function(*args, **kwargs)
    return wrapped


@bp.route("/state/<int:tournament_id>", methods=["GET"])
def get_state(tournament_id):
    try:
        state = TournamentService.get_current_state(tournament_id)
        if not state:
            return jsonify({"error": "Tournament not found"}), 404
        return jsonify(state)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/vote", methods=["POST"])
def vote():
    data = request.get_json(silent=True) or {}

    if not all(data.get(key) for key in (
        "match_id", "player_id", "fingerprint"
    )):
        return jsonify({"success": False, "error": "Missing parameters"}), 400

    try:
        result = TournamentService.vote(
            match_id=data["match_id"],
            player_id=data["player_id"],
            voter_fingerprint=data["fingerprint"],
            voter_ip=request.remote_addr,
        )
        return jsonify(result), 200 if result.get("success") else 409
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route("/admin/init", methods=["POST"])
@admin_required
def admin_init():
    data = request.get_json(silent=True) or {}

    if not data.get("name") or not isinstance(data.get("players"), list):
        return jsonify({
            "success": False,
            "error": "需要提供 name 和 players 数组",
        }), 400

    try:
        tournament_id = TournamentService.init_tournament(
            data["name"],
            data["players"],
        )
        return jsonify({
            "success": True,
            "tournament_id": tournament_id,
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route(
    "/admin/schedule-first-round/<int:tournament_id>",
    methods=["POST"],
)
@admin_required
def schedule_first_round(tournament_id):
    """安排北京时间2026-10-22 00:00开始的固定第一轮。"""
    start_utc = datetime(2026, 10, 21, 16, 0, 0)

    try:
        result = TournamentService.schedule_first_round(
            tournament_id,
            start_utc,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@bp.route(
    "/admin/reset-first-round-time/<int:tournament_id>",
    methods=["POST"],
)
@admin_required
def reset_first_round_time(tournament_id):
    """重置已有第一轮为北京时间2026-10-22 00:00至10-25 00:00。"""
    db = get_memfire()

    start_utc = datetime(2026, 10, 21, 16, 0, 0)
    end_utc = datetime(2026, 10, 24, 16, 0, 0)

    try:
        matches = db.select(
            TABLE_MATCHES,
            filters={
                "tournament_id": f"eq.{int(tournament_id)}",
                "round_number": "eq.1",
            },
        )

        if len(matches) != 8:
            return jsonify({
                "success": False,
                "error": f"第一轮有{len(matches)}场，应为8场",
            }), 400

        for match in matches:
            db.delete(
                TABLE_REVENUE,
                {"match_id": f"eq.{match['match_id']}"},
            )
            db.delete(
                TABLE_VOTES,
                {"match_id": f"eq.{match['match_id']}"},
            )
            db.update(
                TABLE_MATCHES,
                {"match_id": f"eq.{match['match_id']}"},
                {
                    "start_time": start_utc.isoformat(sep=" "),
                    "end_time": end_utc.isoformat(sep=" "),
                    "status": "pending",
                    "winner_id": None,
                    "player1_revenue": 0,
                    "player2_revenue": 0,
                },
            )

        # 删除第二轮及之后的测试数据
        later_matches = db.select(
            TABLE_MATCHES,
            filters={
                "tournament_id": f"eq.{int(tournament_id)}",
                "round_number": "gt.1",
            },
        )

        for match in later_matches:
            db.delete(TABLE_REVENUE, {
                "match_id": f"eq.{match['match_id']}"
            })
            db.delete(TABLE_VOTES, {
                "match_id": f"eq.{match['match_id']}"
            })
            db.delete(TABLE_MATCHES, {
                "match_id": f"eq.{match['match_id']}"
            })

        for player in db.select(
            TABLE_PLAYERS,
            filters={"tournament_id": f"eq.{int(tournament_id)}"},
        ):
            db.update(
                TABLE_PLAYERS,
                {"player_id": f"eq.{player['player_id']}"},
                {"wins": 0, "losses": 0, "status": "active"},
            )

        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {
                "start_time": start_utc.isoformat(sep=" "),
                "current_round": 1,
                "status": "pending",
            },
        )

        return jsonify({
            "success": True,
            "start_time_beijing": "2026/10/22 00:00:00",
            "end_time_beijing": "2026/10/25 00:00:00",
        })

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route(
    "/admin/reset-knockout/<int:tournament_id>",
    methods=["POST"],
)
@admin_required
def reset_knockout(tournament_id):
    """仅清除旧淘汰赛并重建八强BO3，不删除瑞士轮。"""
    db = get_memfire()

    try:
        players = db.select(
            TABLE_PLAYERS,
            filters={
                "tournament_id": f"eq.{int(tournament_id)}",
                "status": "eq.advanced",
            },
        )

        if len(players) != 8:
            return jsonify({
                "success": False,
                "error": f"晋级人数为{len(players)}，需要8人",
            }), 400

        old_matches = db.select(
            TABLE_MATCHES,
            filters={
                "tournament_id": f"eq.{int(tournament_id)}",
                "round_number": f"gte.{ROUND_QF}",
            },
        )

        for match in old_matches:
            db.delete(TABLE_REVENUE, {
                "match_id": f"eq.{match['match_id']}"
            })
            db.delete(TABLE_VOTES, {
                "match_id": f"eq.{match['match_id']}"
            })
            db.delete(TABLE_MATCHES, {
                "match_id": f"eq.{match['match_id']}"
            })

        random.shuffle(players)
        start = datetime.utcnow()
        end = start + timedelta(hours=72)

        db.insert(TABLE_MATCHES, [{
            "tournament_id": int(tournament_id),
            "match_id": f"qf{index + 1}g1",
            "round_number": ROUND_QF,
            "player1_id": players[index * 2]["player_id"],
            "player2_id": players[index * 2 + 1]["player_id"],
            "start_time": start.isoformat(sep=" "),
            "end_time": end.isoformat(sep=" "),
            "status": "running",
        } for index in range(4)])

        db.update(
            TABLE_TOURNAMENTS,
            {"id": f"eq.{int(tournament_id)}"},
            {"current_round": ROUND_QF, "status": "running"},
        )

        return jsonify({
            "success": True,
            "stage": "quarterfinals",
            "format": "BO3",
            "matches_created": 4,
            "game_duration_hours": 72,
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route(
    "/admin/update-revenue/<match_id>",
    methods=["POST"],
)
@admin_required
def admin_update_revenue(match_id):
    try:
        result = TournamentService.update_match_revenue(match_id)
        if result is None:
            return jsonify({
                "success": False,
                "error": "比赛不存在或未进行",
            }), 404
        return jsonify(result)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route("/admin/check-finish", methods=["POST"])
@admin_required
def admin_check_finish():
    try:
        count = TournamentService.check_and_finish_matches()
        return jsonify({"success": True, "finished_matches": count})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500