# app/analytics.py
"""网页浏览量记录及管理员统计接口。"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import os

from flask import Blueprint, jsonify, request

from app.memfire_rest import get_memfire


analytics_bp = Blueprint(
    "analytics",
    __name__,
    url_prefix="/api/analytics",
)


def _admin_token():
    # 不设置 ADMIN_TOKEN 时，管理员接口默认拒绝访问。
    return os.environ.get("ADMIN_TOKEN", "").strip()


def _hash_visitor(visitor_id):
    secret = os.environ.get(
        "ANALYTICS_HASH_SECRET",
        _admin_token(),
    )
    value = f"{secret}:{visitor_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _parse_utc(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _authorized():
    configured_token = _admin_token()
    supplied_token = request.headers.get("X-Admin-Token", "")

    return bool(configured_token) and supplied_token == configured_token


@analytics_bp.post("/visit")
def record_visit():
    """记录一次页面访问，不返回任何统计信息。"""
    data = request.get_json(silent=True) or {}

    page = str(data.get("page") or "/").strip()
    visitor_id = str(data.get("visitor_id") or "").strip()

    if (
        not page.startswith("/")
        or "://" in page
        or len(page) > 200
    ):
        return jsonify({
            "success": False,
            "error": "invalid_page",
        }), 400

    if not visitor_id or len(visitor_id) > 200:
        return jsonify({
            "success": False,
            "error": "invalid_visitor_id",
        }), 400

    try:
        get_memfire().insert(
            "page_views",
            {
                "page": page,
                "visitor_hash": _hash_visitor(visitor_id),
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            },
        )
        return jsonify({"success": True})

    except Exception as exc:
        # 统计故障不应阻断公开页面。
        print(f"记录页面浏览量失败：{exc}")
        return jsonify({"success": False}), 503


@analytics_bp.get("/admin/overview")
def admin_overview():
    """只有带正确 X-Admin-Token 的请求可以读取统计结果。"""
    if not _authorized():
        return jsonify({
            "success": False,
            "error": "Unauthorized",
        }), 401

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)

    try:
        rows = get_memfire().select(
            "page_views",
            filters={
                "created_at": f"gte.{since.isoformat()}",
            },
            select="page,visitor_hash,created_at",
            order="created_at.desc",
            limit=10000,
        )

        today = now.date()
        yesterday = today - timedelta(days=1)
        seven_days_ago = now - timedelta(days=7)

        today_rows = []
        yesterday_rows = []
        seven_day_rows = []
        page_counts = defaultdict(int)
        page_visitors = defaultdict(set)

        for row in rows:
            created = _parse_utc(row.get("created_at"))
            if created is None:
                continue

            page = str(row.get("page") or "/")
            visitor_hash = row.get("visitor_hash")

            page_counts[page] += 1
            if visitor_hash:
                page_visitors[page].add(visitor_hash)

            if created.date() == today:
                today_rows.append(row)
            if created.date() == yesterday:
                yesterday_rows.append(row)
            if created >= seven_days_ago:
                seven_day_rows.append(row)

        def unique_count(items):
            return len({
                row.get("visitor_hash")
                for row in items
                if row.get("visitor_hash")
            })

        return jsonify({
            "success": True,
            "today": {
                "views": len(today_rows),
                "unique_visitors": unique_count(today_rows),
            },
            "yesterday": {
                "views": len(yesterday_rows),
                "unique_visitors": unique_count(yesterday_rows),
            },
            "last_7_days": {
                "views": len(seven_day_rows),
                "unique_visitors": unique_count(seven_day_rows),
            },
            "last_30_days": {
                "views": len(rows),
                "unique_visitors": unique_count(rows),
            },
            "views_by_page": {
                page: {
                    "views": count,
                    "unique_visitors": len(page_visitors[page]),
                }
                for page, count in page_counts.items()
            },
            "latest_visit": (
                rows[0].get("created_at")
                if rows else None
            ),
            "truncated": len(rows) == 10000,
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500
