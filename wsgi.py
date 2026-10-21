# wsgi.py
from run import app
from app.scheduler import start_scheduler

start_scheduler()