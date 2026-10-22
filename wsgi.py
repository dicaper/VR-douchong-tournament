# wsgi.py
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

# 比赛 API
app.register_blueprint(tournament_blueprint)

# 浏览量记录与管理员统计 API
app.register_blueprint(analytics_bp)


@app.route("/")
def home():
    """首页默认显示瑞士轮比赛详情页。"""
    if not (STATIC_DIR / "swiss.html").is_file():
        abort(404)

    return send_from_directory(
        str(STATIC_DIR),
        "swiss.html",
    )


@app.route("/<path:page_name>")
def serve_page(page_name):
    """提供 static 目录下的页面，并限制路径不能越出该目录。"""
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


# CloudBase 使用单个 Gunicorn worker。
# 应用进程启动时启动一次赛事后台调度器。
start_scheduler()
