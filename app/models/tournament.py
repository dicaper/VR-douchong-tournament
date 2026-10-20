# app/models/tournament.py
import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class TournamentConfig(Base):
    __tablename__ = "tournament_config"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    start_time = Column(DateTime, nullable=True)
    status = Column(String(20), default="pending", nullable=False)
    current_round = Column(Integer, default=1, nullable=False)
    match_duration_days = Column(Integer, default=3, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class TournamentPlayer(Base):
    __tablename__ = "tournament_players"

    id = Column(Integer, primary_key=True)
    tournament_id = Column(
        Integer,
        ForeignKey("tournament_config.id"),
        nullable=False,
    )
    player_id = Column(String(10), nullable=False, unique=True)
    player_name = Column(String(100), nullable=False)
    room_id = Column(String(50), nullable=False)
    avatar_url = Column(String(500))
    wins = Column(Integer, default=0, nullable=False)
    losses = Column(Integer, default=0, nullable=False)
    status = Column(String(20), default="active", nullable=False)
    seed = Column(Integer, default=999)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TournamentMatch(Base):
    __tablename__ = "tournament_matches"

    id = Column(Integer, primary_key=True)
    tournament_id = Column(
        Integer,
        ForeignKey("tournament_config.id"),
        nullable=False,
    )
    match_id = Column(String(20), nullable=False, unique=True)
    round_number = Column(Integer, nullable=False)
    player1_id = Column(
        String(10),
        ForeignKey("tournament_players.player_id"),
        nullable=True,
    )
    player2_id = Column(
        String(10),
        ForeignKey("tournament_players.player_id"),
        nullable=True,
    )
    winner_id = Column(
        String(10),
        ForeignKey("tournament_players.player_id"),
        nullable=True,
    )
    player1_revenue = Column(Float, default=0.0, nullable=False)
    player2_revenue = Column(Float, default=0.0, nullable=False)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    status = Column(String(20), default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class TournamentVote(Base):
    __tablename__ = "tournament_votes"

    id = Column(Integer, primary_key=True)
    match_id = Column(
        String(20),
        ForeignKey("tournament_matches.match_id"),
        nullable=False,
    )
    player_id = Column(
        String(10),
        ForeignKey("tournament_players.player_id"),
        nullable=False,
    )
    voter_ip = Column(String(50))
    voter_fingerprint = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TournamentRevenue(Base):
    __tablename__ = "tournament_revenue"

    id = Column(Integer, primary_key=True)
    player_id = Column(
        String(10),
        ForeignKey("tournament_players.player_id"),
        nullable=False,
    )
    match_id = Column(
        String(20),
        ForeignKey("tournament_matches.match_id"),
        nullable=False,
    )
    sc_amount = Column(Float, default=0.0, nullable=False)
    gift_amount = Column(Float, default=0.0, nullable=False)
    total_revenue = Column(Float, default=0.0, nullable=False)
    danmaku_count = Column(Integer, default=0, nullable=False)
    peak_viewers = Column(Integer, default=0, nullable=False)
    avg_viewers = Column(Integer, default=0, nullable=False)
    snapshot_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def _database_url():
    """
    Render 设置 DATABASE_URL 后使用 PostgreSQL。
    本机没有 DATABASE_URL 时使用项目根目录中的 tournament.db。
    """
    configured = os.environ.get("DATABASE_URL", "").strip()

    if configured:
        if configured.startswith("postgres://"):
            return configured.replace(
                "postgres://",
                "postgresql+psycopg2://",
                1,
            )

        if configured.startswith("postgresql://"):
            return configured.replace(
                "postgresql://",
                "postgresql+psycopg2://",
                1,
            )

        return configured

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    )
    sqlite_path = os.path.join(project_root, "tournament.db")
    return "sqlite:///" + sqlite_path.replace("\\", "/")


DATABASE_URL = _database_url()

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


def init_db():
    """创建比赛系统所需的数据库表。"""
    Base.metadata.create_all(engine)