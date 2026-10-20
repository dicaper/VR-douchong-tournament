# app/scripts/start_round1.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime, timezone, timedelta
from app.models.tournament import SessionLocal, TournamentConfig, TournamentMatch, TournamentPlayer

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
    
    # 删除旧的第一轮比赛（如果存在）
    old_matches = session.query(TournamentMatch).filter_by(
        tournament_id=tournament_id,
        round_number=1
    ).all()
    for m in old_matches:
        session.delete(m)
    
    # 设置比赛开始时间和状态
    tournament.start_time = START_TIME_UTC
    tournament.status = 'pending'  # 设为pending，比赛还没开始
    tournament.current_round = 1
    
    # 获取所有选手
    players = session.query(TournamentPlayer).filter_by(
        tournament_id=tournament_id
    ).all()
    
    if len(players) != 16:
        print(f"❌ 选手数量不对: {len(players)}")
        exit(1)
    
    # 固定配对规则（你给的对阵）
    pairings = [
        ("Ameki", "chu2u"),      # 197 vs 287
        ("Nagisa", "Rhea"),      # 217 vs 147
        ("Mizuki", "Hazel"),     # 207 vs 277
        ("Liko", "Izayoi"),      # 277 vs 277
        ("SUI", "Harei"),        # 177 vs 237
        ("Viridis", "Sumire"),   # 287 vs 287
        ("Kloa", "Melody"),      # 277 vs 247
        ("Mit3uri", "Shiori"),   # 257 vs 187
    ]
    
    # 创建选手名字到对象的映射
    player_map = {p.player_name: p for p in players}
    
    match_num = 1
    for p1_name, p2_name in pairings:
        p1 = player_map.get(p1_name)
        p2 = player_map.get(p2_name)
        
        if not p1 or not p2:
            print(f"❌ 找不到选手: {p1_name} 或 {p2_name}")
            continue
        
        match = TournamentMatch(
            tournament_id=tournament_id,
            match_id=f"r1m{match_num}",
            round_number=1,
            player1_id=p1.player_id,
            player2_id=p2.player_id,
            start_time=START_TIME_UTC,
            end_time=START_TIME_UTC + timedelta(days=tournament.match_duration_days),
            status='pending'  # 设为pending，比赛还没开始
        )
        session.add(match)
        print(f"✅ 对阵 {match_num}: {p1_name} ({p1.seed}) vs {p2_name} ({p2.seed})")
        match_num += 1
    
    session.commit()
    
    print(f"\n✅ 第一轮对阵已设置！")
    print(f"   比赛开始: {START_TIME_BJ.strftime('%Y/%m/%d %H:%M:%S')} (北京时间)")
    print(f"   比赛结束: {(START_TIME_BJ + timedelta(days=3)).strftime('%Y/%m/%d %H:%M:%S')} (北京时间)")
    print(f"\n⏰ 比赛将在开始时间自动启动")
    print(f"🌐 访问网页查看对阵: http://localhost:5000/swiss.html")
    
except Exception as e:
    print(f"\n❌ 发生错误: {e}")
    import traceback
    traceback.print_exc()
    session.rollback()
finally:
    session.close()