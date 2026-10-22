# wsgi.py
import os
from pathlib import Path

from flask import Flask, abort, send_from_directory

from app.analytics import analytics_bp
from app.api_tournament import bp as tournament_blueprint
from app.scheduler import start_scheduler


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)

# 注册比赛 API
app.register_blueprint(tournament_blueprint)

# 注册浏览量统计 API
app.register_blueprint(analytics_bp)


@app.route("/")
def home():
    """根路径默认打开瑞士轮比赛详情页。"""
    page = STATIC_DIR / "swiss.html"

    if not page.is_file():
        abort(404)

    return send_from_directory(
        str(STATIC_DIR),
        "swiss.html",
    )


@app.route("/<path:page_name>")
def serve_page(page_name):
    """
    提供 static 目录中的页面，并防止路径越界。

    示例：
        /swiss.html
        /swiss_bracket.html
        /playoffs.html
        /admin-stats.html
    """
    page = (STATIC_DIR / page_name).resolve()

    try:
        page.relative_to(STATIC_DIR.resolve())
    except ValueError:
        abort(404)

    if not page.is_file():
        abort(404)

    return send_from_directory(
        str(STATIC_DIR),
        page_name,
    )


# CloudBase 正常运行时保持调度器开启。
# 本地测试时设置 DISABLE_SCHEDULER=1，
# 这样本地启动不会自动结算或推进数据库中的比赛。
if os.environ.get("DISABLE_SCHEDULER", "0") != "1":
    start_scheduler()
else:
    print("⏸️ 本地测试模式：赛事自动调度器已关闭")