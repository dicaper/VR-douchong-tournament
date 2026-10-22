# app/models/tournament.py

"""
赛事相关常量。

项目当前使用 MemFire REST API，
不使用本地 SQLite 或 SQLAlchemy。
"""


# 瑞士轮第一轮
ROUND_SWISS_FIRST = 1

# 淘汰赛阶段
ROUND_QF = 100
ROUND_SF = 101
ROUND_FINAL = 102


# BO3 规则
BEST_OF_THREE_WINS = 2
KNOCKOUT_GAME_HOURS = 72


# 瑞士轮最多5轮
SWISS_MAX_ROUNDS = 5


# MemFire 数据表
TABLE_TOURNAMENTS = "tournament_config"
TABLE_PLAYERS = "tournament_players"
TABLE_MATCHES = "tournament_matches"
TABLE_VOTES = "tournament_votes"
TABLE_REVENUE = "tournament_revenue"


# 网页浏览量统计表
TABLE_PAGE_VIEWS = "page_views"