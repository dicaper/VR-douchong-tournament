# app/models/tournament.py
"""
比赛表常量。

比赛数据存储在 MemFire REST API 后面；
这里不再创建 SQLAlchemy engine 或本地数据库。
"""

ROUND_SWISS_FIRST = 1
ROUND_QF = 100
ROUND_SF = 101
ROUND_FINAL = 102

BEST_OF_THREE_WINS = 2
KNOCKOUT_GAME_HOURS = 72
SWISS_MAX_ROUNDS = 5

TABLE_TOURNAMENTS = "tournament_config"
TABLE_PLAYERS = "tournament_players"
TABLE_MATCHES = "tournament_matches"
TABLE_VOTES = "tournament_votes"
TABLE_REVENUE = "tournament_revenue"