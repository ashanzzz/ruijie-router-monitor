import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, Float
from sqlalchemy.orm import declarative_base, sessionmaker
from config import settings

# Ensure data dir exists
db_dir = os.path.dirname(settings.DATABASE_URL.replace("sqlite:///", ""))
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    pool_pre_ping=True if "postgresql" in settings.DATABASE_URL else False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Device(Base):
    __tablename__ = "devices"

    mac = Column(String, primary_key=True, index=True)
    ip = Column(String, index=True)
    hostname = Column(String, nullable=True)
    ap_sn = Column(String, nullable=True, index=True)
    ap_name = Column(String, default="主路由器")
    ssid = Column(String, nullable=True)
    is_online = Column(Boolean, default=True)
    
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    last_online_at = Column(DateTime, default=datetime.utcnow)
    last_offline_at = Column(DateTime, nullable=True)
    
    # Live speed & status
    rx_rate = Column(Float, default=0.0)  # KB/s
    tx_rate = Column(Float, default=0.0)  # KB/s
    usage_state = Column(String, default="空闲")  # 空闲, 视频/下载, 频繁活动
    is_starred = Column(Boolean, default=False, index=True)  # 重点关注标记


class DeviceAlias(Base):
    __tablename__ = "device_aliases"

    mac = Column(String, primary_key=True, index=True)
    alias = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)

class ConnectionHistory(Base):
    __tablename__ = "connection_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mac = Column(String, index=True, nullable=False)
    hostname = Column(String, nullable=True)
    
    session_start = Column(DateTime, default=datetime.utcnow, index=True)
    session_end = Column(DateTime, nullable=True, index=True)
    
    ap_sn = Column(String, nullable=True)
    ap_name = Column(String, nullable=True)
    
    total_rx_bytes = Column(Float, default=0.0)
    total_tx_bytes = Column(Float, default=0.0)

class EventLog(Base):
    __tablename__ = "event_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mac = Column(String, index=True)
    device_name = Column(String)  # alias or hostname or mac
    ip = Column(String)
    event_type = Column(String)   # ONLINE, OFFLINE, HIGH_TRAFFIC
    message = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

def reinit_engine(new_url: str):
    global engine, SessionLocal
    # Dispose old connections
    engine.dispose()
    
    # Create new engine
    engine = create_engine(
        new_url,
        connect_args={"check_same_thread": False} if "sqlite" in new_url else {},
        pool_pre_ping=True if "postgresql" in new_url else False
    )
    
    # Reconfigure session factory
    SessionLocal.configure(bind=engine)
    
    # Ensure tables are created in the new DB
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
