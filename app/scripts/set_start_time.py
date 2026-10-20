# app/scripts/set_start_time.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime, timezone, timedelta
from app.models.tournament import SessionLocal, TournamentConfig, TournamentMatch

# 北京时间 = UTC+8
BEIJING_TZ = timezone(timedelta(hours=8))
START_TIME_BJ = datetime(2026, 10, 21, 0, 0, 0, tzinfo=BEIJING_TZ)
START_TIME_UTC = START_TIME_BJ.astimezone(timezone.utc).replace(tzinfo=None)

session = SessionLocal()

try:
    # 读取比赛ID
    with open('tournament_id.txt', 'r') as f:
        tournament_id = int(f.read().strip())
    
    tournament = session.query(TournamentConfig).get(tournament_id)
    if not tournament:
        print("❌ 比赛不存在")
        exit(1)
    
    # 设置比赛开始时间
    tournament.start_time = START_TIME_UTC
    tournament.status = 'pending'  # 设置为待开始状态
    
    # 更新第一轮比赛的时间
    matches = session.query(TournamentMatch).filter_by(
        tournament_id=tournament_id,
        round_number=1
    ).all()
    
    for match in matches:
        match.start_time = START_TIME_UTC
        match.end_time = START_TIME_UTC + timedelta(days=3)
        match.status = 'pending'
    
    session.commit()
    
    print(f"✅ 比赛开始时间已设置")
    print(f"   北京时间: {START_TIME_BJ.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   UTC时间: {START_TIME_UTC.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   第一轮结束时间: {(START_TIME_UTC + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"\n📌 比赛将在北京时间 2026-10-21 00:00 自动开始")
    
except Exception as e:
    print(f"❌ 错误: {e}")
    import traceback
    traceback.print_exc()
    session.rollback()
finally:
    session.close()