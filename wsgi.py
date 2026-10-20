# wsgi.py
from run import app
from app.models.tournament import init_db
from app.scheduler import start_scheduler

# Gunicorn 导入本文件时初始化数据库并启动一次调度器。
init_db()
start_scheduler()