# app/api_tournament.py
from flask import Blueprint, request, jsonify
from functools import wraps
from datetime import datetime, timedelta
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.tournament_service import TournamentService
from app.models.tournament import SessionLocal, TournamentMatch

bp = Blueprint('tournament', __name__, url_prefix='/api/tournament')

# 管理员密码（上线前必须改掉，并改用环境变量）
ADMIN_TOKEN = "my_secret_password_123"


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Admin-Token')
        if token != ADMIN_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


@bp.route('/state/<int:tournament_id>', methods=['GET'])
def get_state(tournament_id):
    """获取比赛当前状态（前端调用）"""
    state = TournamentService.get_current_state(tournament_id)
    if not state:
        return jsonify({'error': 'Tournament not found'}), 404
    return jsonify(state)


@bp.route('/vote', methods=['POST'])
def vote():
    """观众投票"""
    data = request.get_json(silent=True) or {}
    match_id = data.get('match_id')
    player_id = data.get('player_id')
    fingerprint = data.get('fingerprint')

    if not all([match_id, player_id, fingerprint]):
        return jsonify({'error': 'Missing parameters'}), 400

    result = TournamentService.vote(
        match_id=match_id,
        player_id=player_id,
        voter_fingerprint=fingerprint,
        voter_ip=request.remote_addr
    )
    return jsonify(result)


@bp.route('/admin/init', methods=['POST'])
@admin_required
def admin_init():
    """管理员：初始化比赛（只需选手名单）"""
    data = request.get_json(silent=True) or {}
    tournament_id = TournamentService.init_tournament(
        name=data['name'],
        players=data['players']
    )
    return jsonify({'tournament_id': tournament_id, 'success': True})


@bp.route('/admin/update-revenue/<match_id>', methods=['POST'])
@admin_required
def admin_update_revenue(match_id):
    """管理员：手动更新营收"""
    result = TournamentService.update_match_revenue(match_id)
    if result is None:
        return jsonify({'error': 'Match not found'}), 404
    return jsonify(result)


@bp.route('/admin/check-finish', methods=['POST'])
@admin_required
def admin_check_finish():
    """管理员：检查并结束超时比赛"""
    count = TournamentService.check_and_finish_matches()
    return jsonify({'finished_matches': count})


@bp.route('/admin/force-finish-round', methods=['POST'])
@admin_required
def admin_force_finish_round():
    """管理员：强制结束某轮所有进行中的比赛（仅测试用）"""
    data = request.get_json(silent=True) or {}
    round_number = data.get('round_number', 1)

    session = SessionLocal()
    try:
        matches = session.query(TournamentMatch).filter_by(
            round_number=round_number, status='running'
        ).all()
        now = datetime.utcnow()
        for m in matches:
            m.end_time = now - timedelta(seconds=1)
        session.commit()
        expired = len(matches)
    finally:
        session.close()

    finished = TournamentService.check_and_finish_matches()
    return jsonify({'expired': expired, 'finished': finished})