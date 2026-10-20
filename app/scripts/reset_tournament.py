# app/scripts/reset_tournament.py
from datetime import datetime
import sys
import os

# 项目根目录
ROOT_DIR = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__)
        )
    )
)

sys.path.insert(0, ROOT_DIR)

from app.models.tournament import (
    SessionLocal,
    TournamentConfig,
    TournamentPlayer,
    TournamentMatch,
    TournamentVote,
    TournamentRevenue,
)

TOURNAMENT_ID = 1

# 北京时间 2026/10/21 00:00
# 数据库使用 UTC，因此对应 2026/10/20 16:00
START_TIME_UTC = datetime(2026, 10, 20, 16, 0, 0)
END_TIME_UTC = datetime(2026, 10, 23, 16, 0, 0)

# 第一轮固定对阵
PAIRINGS = [
    ("Ameki", "chu2u"),
    ("Nagisa", "Rhea"),
    ("Mizuki", "Hazel"),
    ("Liko", "Izayoi"),
    ("SUI", "Harei"),
    ("Viridis", "Sumire"),
    ("Kloa", "Melody"),
    ("Mit3uri", "Shiori"),
]

session = SessionLocal()

try:
    tournament = session.get(TournamentConfig, TOURNAMENT_ID)

    if not tournament:
        print("❌ 找不到比赛 ID:", TOURNAMENT_ID)
        sys.exit(1)

    # 先删除依赖数据
    session.query(TournamentVote).delete(
        synchronize_session=False
    )

    session.query(TournamentRevenue).delete(
        synchronize_session=False
    )

    session.query(TournamentMatch).delete(
        synchronize_session=False
    )

    # 重置所有选手
    players = session.query(TournamentPlayer).filter_by(
        tournament_id=TOURNAMENT_ID
    ).all()

    for player in players:
        player.wins = 0
        player.losses = 0
        player.status = "active"

    # 重置比赛状态
    tournament.status = "pending"
    tournament.current_round = 1
    tournament.start_time = START_TIME_UTC

    # 建立第一轮固定对阵
    player_map = {
        player.player_name: player
        for player in players
    }

    for index, (name1, name2) in enumerate(PAIRINGS, start=1):
        p1 = player_map.get(name1)
        p2 = player_map.get(name2)

        if not p1 or not p2:
            print(f"❌ 找不到选手: {name1} 或 {name2}")
            continue

        match = TournamentMatch(
            tournament_id=TOURNAMENT_ID,
            match_id=f"r1m{index}",
            round_number=1,
            player1_id=p1.player_id,
            player2_id=p2.player_id,
            start_time=START_TIME_UTC,
            end_time=END_TIME_UTC,
            status="pending",
        )

        session.add(match)

    session.commit()

    print("✅ 比赛已重置为未开始状态")
    print("✅ 所有选手战绩已恢复为 0-0")
    print("✅ 第一轮固定对阵已恢复")
    print("✅ 比赛状态: pending")
    print("✅ 北京时间开始: 2026/10/21 00:00:00")
    print("✅ 北京时间结束: 2026/10/24 00:00:00")

except Exception as error:
    session.rollback()
    print("❌ 重置失败:", error)
    raise

finally:
    session.close()