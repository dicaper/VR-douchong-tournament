# app/api_tournament.py

from datetime import datetime, timedelta
from functools import wraps
import random

from flask import Blueprint, jsonify, request

from app.models.tournament import (
    SessionLocal,
    TournamentConfig,
    TournamentMatch,
    TournamentPlayer,
    TournamentRevenue,
    TournamentVote,
)

from app.services.tournament_service import (
    ROUND_QF,
    ROUND_SF,
    ROUND_FINAL,
    TournamentService,
)


bp = Blueprint(
    "tournament",
    __name__,
    url_prefix="/api/tournament",
)

ADMIN_TOKEN = "my_secret_password_123"


def admin_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Admin-Token")

        if token != ADMIN_TOKEN:
            return jsonify({
                "success": False,
                "error": "Unauthorized",
            }), 401

        return function(*args, **kwargs)

    return decorated


@bp.route("/state/<int:tournament_id>", methods=["GET"])
def get_state(tournament_id):
    """读取比赛完整状态。"""
    try:
        state = TournamentService.get_current_state(tournament_id)
    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    if not state:
        return jsonify({
            "success": False,
            "error": "Tournament not found",
        }), 404

    return jsonify(state)


@bp.route("/vote", methods=["POST"])
def vote():
    """访客投票。"""
    data = request.get_json(silent=True) or {}

    match_id = data.get("match_id")
    player_id = data.get("player_id")
    fingerprint = data.get("fingerprint")

    if not match_id or not player_id or not fingerprint:
        return jsonify({
            "success": False,
            "error": "Missing parameters",
        }), 400

    try:
        result = TournamentService.vote(
            match_id=match_id,
            player_id=player_id,
            voter_fingerprint=fingerprint,
            voter_ip=request.remote_addr,
        )
    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    if result.get("success"):
        return jsonify(result), 200

    if result.get("error") == "already_voted":
        return jsonify(result), 409

    return jsonify(result), 400


@bp.route("/admin/init", methods=["POST"])
@admin_required
def admin_init():
    """初始化比赛和选手。"""
    data = request.get_json(silent=True) or {}

    if not data.get("name"):
        return jsonify({
            "success": False,
            "error": "缺少比赛名称",
        }), 400

    if not isinstance(data.get("players"), list):
        return jsonify({
            "success": False,
            "error": "缺少 players 数组",
        }), 400

    try:
        tournament_id = TournamentService.init_tournament(
            name=data["name"],
            players=data["players"],
        )
    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    return jsonify({
        "success": True,
        "tournament_id": tournament_id,
    })


@bp.route("/admin/start-round1/<int:tournament_id>", methods=["POST"])
@admin_required
def admin_start_round1(tournament_id):
    """
    创建并开始第一轮固定对阵。
    仅供第一次初始化时使用。
    """
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

    session = SessionLocal()

    try:
        tournament = session.get(
            TournamentConfig,
            tournament_id,
        )

        if not tournament:
            return jsonify({
                "success": False,
                "error": "比赛不存在",
            }), 404

        old_match = session.query(TournamentMatch).filter_by(
            tournament_id=tournament_id,
            round_number=1,
        ).first()

        if old_match:
            return jsonify({
                "success": False,
                "error": "第一轮已经存在，不能重复创建",
            }), 409

        players = session.query(TournamentPlayer).filter_by(
            tournament_id=tournament_id,
        ).all()

        player_map = {
            player.player_name.strip().casefold(): player
            for player in players
        }

        missing = []

        for name1, name2 in pairings:
            if name1.casefold() not in player_map:
                missing.append(name1)

            if name2.casefold() not in player_map:
                missing.append(name2)

        if missing:
            return jsonify({
                "success": False,
                "error": "缺少选手：" + ", ".join(missing),
            }), 400

        start_time = datetime.utcnow()
        end_time = start_time + timedelta(hours=72)

        for index, (name1, name2) in enumerate(pairings, start=1):
            player1 = player_map[name1.casefold()]
            player2 = player_map[name2.casefold()]

            session.add(TournamentMatch(
                tournament_id=tournament_id,
                match_id=f"r1m{index}",
                round_number=1,
                player1_id=player1.player_id,
                player2_id=player2.player_id,
                start_time=start_time,
                end_time=end_time,
                status="running",
            ))

        tournament.start_time = start_time
        tournament.current_round = 1
        tournament.match_duration_days = 3
        tournament.status = "running"

        session.commit()

        return jsonify({
            "success": True,
            "tournament_id": tournament_id,
            "round": 1,
            "matches_created": 8,
            "duration_hours": 72,
            "start_time_utc": start_time.isoformat(),
            "end_time_utc": end_time.isoformat(),
        })

    except Exception as error:
        session.rollback()

        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    finally:
        session.close()


@bp.route(
    "/admin/reset-knockout/<int:tournament_id>",
    methods=["POST"],
)
@admin_required
def reset_knockout(tournament_id):
    """
    仅重置淘汰赛。
    不删除瑞士轮、不删除选手。
    仅在旧的单局淘汰赛记录存在时使用。
    """
    session = SessionLocal()

    try:
        tournament = session.get(
            TournamentConfig,
            tournament_id,
        )

        if not tournament:
            return jsonify({
                "success": False,
                "error": "比赛不存在",
            }), 404

        advanced = session.query(TournamentPlayer).filter_by(
            tournament_id=tournament_id,
            status="advanced",
        ).all()

        if len(advanced) != 8:
            return jsonify({
                "success": False,
                "error": f"当前晋级人数为{len(advanced)}，需要8人",
            }), 400

        old_matches = session.query(TournamentMatch).filter(
            TournamentMatch.tournament_id == tournament_id,
            TournamentMatch.round_number >= ROUND_QF,
        ).all()

        old_match_ids = [
            match.match_id
            for match in old_matches
        ]

        for match_id in old_match_ids:
            session.query(TournamentRevenue).filter_by(
                match_id=match_id,
            ).delete(
                synchronize_session=False,
            )

            session.query(TournamentVote).filter_by(
                match_id=match_id,
            ).delete(
                synchronize_session=False,
            )

        session.query(TournamentMatch).filter(
            TournamentMatch.tournament_id == tournament_id,
            TournamentMatch.round_number >= ROUND_QF,
        ).delete(
            synchronize_session=False,
        )

        random.shuffle(advanced)

        start_time = datetime.utcnow()
        end_time = start_time + timedelta(hours=72)

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

        return jsonify({
            "success": True,
            "stage": "quarterfinals",
            "format": "BO3",
            "matches_created": 4,
            "game_duration_hours": 72,
            "old_matches_deleted": len(old_match_ids),
        })

    except Exception as error:
        session.rollback()

        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    finally:
        session.close()


@bp.route(
    "/admin/update-revenue/<match_id>",
    methods=["POST"],
)
@admin_required
def admin_update_revenue(match_id):
    """手动更新某一局营收。"""
    try:
        result = TournamentService.update_match_revenue(match_id)
    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    if result is None:
        return jsonify({
            "success": False,
            "error": "比赛不存在或没有进行中",
        }), 404

    return jsonify(result)


@bp.route("/admin/check-finish", methods=["POST"])
@admin_required
def admin_check_finish():
    """检查到期单局并自动推进瑞士轮或淘汰赛。"""
    try:
        count = TournamentService.check_and_finish_matches()
    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    return jsonify({
        "success": True,
        "finished_matches": count,
    })