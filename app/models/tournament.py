# app/models/tournament.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class TournamentConfig(Base):
    __tablename__ = 'tournament_config'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    start_time = Column(DateTime, nullable=True)
    status = Column(String(20), default='pending')
    current_round = Column(Integer, default=1)
    match_duration_days = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TournamentPlayer(Base):
    __tablename__ = 'tournament_players'
    
    id = Column(Integer, primary_key=True)
    tournament_id = Column(Integer, ForeignKey('tournament_config.id'), nullable=False)
    player_id = Column(String(10), nullable=False, unique=True)
    player_name = Column(String(100), nullable=False)
    room_id = Column(String(50), nullable=False)
    avatar_url = Column(String(500))
    avatar_ext = Column(String(10), default='webp')
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    status = Column(String(20), default='active')
    seed = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

class TournamentMatch(Base):
    __tablename__ = 'tournament_matches'
    
    id = Column(Integer, primary_key=True)
    tournament_id = Column(Integer, ForeignKey('tournament_config.id'), nullable=False)
    match_id = Column(String(20), nullable=False, unique=True)
    round_number = Column(Integer, nullable=False)
    player1_id = Column(String(10), ForeignKey('tournament_players.player_id'))
    player2_id = Column(String(10), ForeignKey('tournament_players.player_id'))
    winner_id = Column(String(10), ForeignKey('tournament_players.player_id'), nullable=True)
    player1_revenue = Column(Float, default=0.0)
    player2_revenue = Column(Float, default=0.0)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TournamentVote(Base):
    __tablename__ = 'tournament_votes'
    
    id = Column(Integer, primary_key=True)
    match_id = Column(String(20), ForeignKey('tournament_matches.match_id'), nullable=False)
    player_id = Column(String(10), ForeignKey('tournament_players.player_id'), nullable=False)
    voter_ip = Column(String(50))
    voter_fingerprint = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)

class TournamentRevenue(Base):
    __tablename__ = 'tournament_revenue'
    
    id = Column(Integer, primary_key=True)
    player_id = Column(String(10), ForeignKey('tournament_players.player_id'), nullable=False)
    match_id = Column(String(20), ForeignKey('tournament_matches.match_id'), nullable=False)
    sc_amount = Column(Float, default=0.0)
    gift_amount = Column(Float, default=0.0)
    total_revenue = Column(Float, default=0.0)
    danmaku_count = Column(Integer, default=0)
    peak_viewers = Column(Integer, default=0)
    avg_viewers = Column(Integer, default=0)
    snapshot_time = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

# 数据库配置
DATABASE_URL = "sqlite:///tournament.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def init_db():
    """初始化数据库"""
    Base.metadata.create_all(engine)