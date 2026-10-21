# wsgi.py
from pathlib import Path

from flask import Flask, abort, send_from_directory

from app.api_tournament import bp as tournament_blueprint
from app.scheduler import start_scheduler


# 项目根目录：wsgi.py 所在目录
PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)

# 比赛 API
app.register_blueprint(tournament_blueprint)

# 页面路由：保留原有页面文件和静态资源
@app.route("/")
def home():
    page = STATIC_DIR / "swiss.html"
    if not page.is_file():
        abort(404)
    return send_from_directory(str(STATIC_DIR), "swiss.html")


@app.route("/<path:page_name>")
def serve_page(page_name):
    """
    提供 static 目录中的 HTML 页面。
    例如 /swiss.html、/playoffs.html、/swiss_bracket.html。
    """
    page = (STATIC_DIR / page_name).resolve()

    # 限制只能访问 static 目录中的文件，防止路径越界。
    try:
        page.relative_to(STATIC_DIR.resolve())
    except ValueError:
        abort(404)

    if not page.is_file():
        abort(404)

    return send_from_directory(str(STATIC_DIR), page_name)


# CloudBase 使用单个 Gunicorn worker。
# 该进程启动时启动一次赛事后台调度器。
start_scheduler()