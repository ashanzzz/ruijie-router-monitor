import os
from pathlib import Path
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

data_dir = os.environ.get("DATA_DIR", "/app/data")
CONTROL_DB_PATH = Path(data_dir) / "control.db"
CONTROL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

control_engine = create_engine(
    f"sqlite:///{CONTROL_DB_PATH}",
    connect_args={"check_same_thread": False},
)
ControlSessionLocal = sessionmaker(
    bind=control_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)
ControlBase = declarative_base()

class AdminCredential(ControlBase):
    __tablename__ = "admin_credentials"

    id = Column(Integer, primary_key=True, default=1)
    password_hash = Column(String(512), nullable=False)
    password_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

class AdminSession(ControlBase):
    __tablename__ = "admin_sessions"

    token_hash = Column(String(64), primary_key=True)
    password_version = Column(Integer, nullable=False)
    csrf_token = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    last_seen_at = Column(DateTime, nullable=False)

def init_auth_db():
    ControlBase.metadata.create_all(bind=control_engine)
