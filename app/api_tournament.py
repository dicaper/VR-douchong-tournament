# app/api_tournament.py

from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request

from app.models.tournament import (
    SessionLocal,
    TournamentConfig,
    TournamentMatch,
    TournamentPlayer,
)
from app.services.tournament_service import TournamentService


bp = Blueprint("tournament", __name__, url_prefix="/api/tournament")

# 必须和你现有的 scripts/setup_tournament.py 中 ADMIN_TOKEN 一致
ADMIN_TOKEN = "my_secret_password_123"


def admin_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Admin-Token")
        if token != ADMIN_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return function(*args, **kwargs)

    return decorated


@bp.route("/state/<int:tournament_id>", methods=["GET"])
def get_state(tournament_id):
    """读取比赛、选手、对阵、营收和投票。"""
    state = TournamentService.get_current_state(tournament_id)

    if not state:
        return jsonify({"error": "Tournament not found"}), 404

    return jsonify(state)


@bp.route("/vote", methods=["POST"])
def vote():
    """访客为一场比赛投票。"""
    data = request.get_json(silent=True) or {}

    match_id = data.get("match_id")
    player_id = data.get("player_id")
    fingerprint = data.get("fingerprint")

    if not match_id or not player_id or not fingerprint:
        return jsonify({"error": "Missing parameters"}), 400

    result = TournamentService.vote(
        match_id=match_id,
        player_id=player_id,
        voter_fingerprint=fingerprint,
        voter_ip=request.remote_addr,
    )

    if result.get("success"):
        return jsonify(result), 200

    if result.get("error") == "already_voted":
        return jsonify(result), 409

    return jsonify(result), 400


@bp.route("/admin/init", methods=["POST"])
@admin_required
def admin_init():
    """创建比赛和参赛选手。"""
    data = request.get_json(silent=True) or {}

    if not data.get("name") or not isinstance(data.get("players"), list):
        return jsonify({
            "success": False,
            "error": "需要提供 name 和 players",
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
    在线创建并立即开始第一轮。
    固定对阵按用户提供的名单。
    比赛持续72小时。
    只能成功执行一次。
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
        tournament = session.get(TournamentConfig, tournament_id)

        if not tournament:
            return jsonify({
                "success": False,
                "error": f"找不到比赛 ID {tournament_id}",
            }), 404

        existing_match = session.query(TournamentMatch).filter_by(
            tournament_id=tournament_id,
            round_number=1,
        ).first()

        if existing_match:
            return jsonify({
                "success": False,
                "error": "第一轮对阵已经存在；为避免重复创建，本次没有修改比赛",
            }), 409

        players = session.query(TournamentPlayer).filter_by(
            tournament_id=tournament_id
        ).all()

        players_by_name = {
            player.player_name.strip().casefold(): player
            for player in players
        }

        missing_names = []

        for player1_name, player2_name in pairings:
            if player1_name.casefold() not in players_by_name:
                missing_names.append(player1_name)

            if player2_name.casefold() not in players_by_name:
                missing_names.append(player2_name)

        if missing_names:
            return jsonify({
                "success": False,
                "error": "线上数据库缺少选手：" + ", ".join(missing_names),
            }), 400

        # 数据库时间按 UTC naive datetime 保存
        start_time = datetime.utcnow()
        end_time = start_time + timedelta(hours=72)

        for index, (player1_name, player2_name) in enumerate(pairings, start=1):
            player1 = players_by_name[player1_name.casefold()]
            player2 = players_by_name[player2_name.casefold()]

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

        tournament.status = "running"
        tournament.current_round = 1
        tournament.start_time = start_time
        tournament.match_duration_days = 3

        session.commit()

        return jsonify({
            "success": True,
            "tournament_id": tournament_id,
            "round": 1,
            "matches_created": 8,
            "duration_hours": 72,
            "start_time_utc": start_time.isoformat(),
            "end_time_utc": end_time.isoformat(),
            "message": "第一轮已在线开始，持续72小时",
        })

    except Exception as error:
        session.rollback()
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    finally:
        session.close()


@bp.route("/admin/update-revenue/<match_id>", methods=["POST"])
@admin_required
def admin_update_revenue(match_id):
    """管理员手动更新一场比赛营收。"""
    try:
        result = TournamentService.update_match_revenue(match_id)
    except Exception as error:
        return jsonify({
            "success": False,
            "error": "更新营收失败",
            "detail": str(error),
        }), 500

    if result is None:
        return jsonify({
            "success": False,
            "error": "比赛不存在或比赛尚未进行",
        }), 404

    return jsonify(result)


@bp.route("/admin/check-finish", methods=["POST"])
@admin_required
def admin_check_finish():
    """检查到期比赛并尝试推进赛事。"""
    try:
        count = TournamentService.check_and_finish_matches()
    except Exception as error:
        return jsonify({
            "success": False,
            "error": "检查比赛失败",
            "detail": str(error),
        }), 500

    return jsonify({
        "success": True,
        "finished_matches": count,
    })