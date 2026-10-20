# run.py
import sys
import os

# 添加当前目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, send_from_directory
from flask_cors import CORS

from app.api_tournament import bp as tournament_bp
from app.models.tournament import init_db
from app.scheduler import start_scheduler

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static')
CORS(app)

app.register_blueprint(tournament_bp)

@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'swiss.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)

if __name__ == '__main__':
    print("正在初始化数据库...")
    init_db()
    print("✅ 数据库初始化完成")
    
    print("正在启动定时调度器...")
    start_scheduler()
    print("✅ 定时调度器已启动")
    
    print("🚀 服务器启动中...")
    print(f"📁 静态文件目录: {STATIC_DIR}")
    print("=" * 60)
    print("📌 服务器功能:")
    print("   - 每60秒自动检查比赛状态")
    print("   - 自动结束超时比赛并判断胜负")
    print("   - 自动更新选手营收数据")
    print("   - 自动开始下一轮瑞士轮（同战绩配对）")
    print("   - 瑞士轮结束后自动开始淘汰赛")
    print("=" * 60)
    
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)