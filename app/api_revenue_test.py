# app/api_revenue_test.py
import os
from functools import wraps

from flask import Blueprint, jsonify

from app.services.tournament_service import TournamentService


revenue_test_bp = Blueprint(
    "revenue_test",
    __name__,
    url_prefix="/api/test",
)

ADMIN_TOKEN = os.environ.get(
    "ADMIN_TOKEN",
    "my_secret_password_123",
)


def test_token_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from flask import request

        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify({
                "success": False,
                "error": "Unauthorized",
            }), 401

        return function(*args, **kwargs)

    return wrapped


@revenue_test_bp.route("/revenue", methods=["POST"])
@test_token_required
def test_revenue_source():
    """
    从云托管容器请求上游接口，测试云端网络连通性。
    测试 Ameki 直播间 27628019 的已知历史窗口：
    北京时间 2026-10-01 15:15 至 17:55。
    """
    from datetime import datetime

    try:
        result = TournamentService.get_player_revenue(
            room_id="27628019",
            # 数据库/服务函数将无时区时间按 UTC 解释
            start_time=datetime(2026, 10, 1, 7, 15, 0),
            end_time=datetime(2026, 10, 1, 9, 55, 0),
        )

        return jsonify({
            "success": True,
            "room_id": "27628019",
            "window_beijing": "2026-10-01 15:15:00 至 17:55:00",
            "revenue": result,
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 502