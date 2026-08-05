from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any, Generator

from fastapi import HTTPException
from sqlalchemy import Engine, URL, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from backend.config import Settings, settings
from backend.time_utils import utcnow
from backend.db.models import Base

logger = logging.getLogger("database")


@dataclass
class DatabaseRuntime:
    engine: Engine | None = None
    session_factory: sessionmaker | None = None
    active_url: URL | None = None
    state: str = "not_initialized"
    last_connected_at: datetime | None = None
    last_successful_write_at: datetime | None = None
    last_write_snapshot_id: str | None = None
    last_error: str | None = None
    lock: RLock = field(default_factory=RLock)

    def initialize(self, app_settings: Settings = settings) -> None:
        candidate_url = app_settings.database_url()
        candidate = create_database_engine(candidate_url)
        self.state = "connecting"
        try:
            with candidate.connect() as connection:
                connection.execute(text("SELECT 1"))
            Base.metadata.create_all(candidate)
            apply_legacy_column_upgrades(candidate)
            if candidate_url.drivername.startswith("sqlite") and candidate_url.database:
                os.chmod(candidate_url.database, 0o600)
        except Exception as exc:
            candidate.dispose()
            self.state = "connection_failed"
            self.last_error = safe_error(exc)
            logger.exception("Database initialization failed")
            raise

        with self.lock:
            if self.engine is not None:
                self.engine.dispose()
            self.engine = candidate
            self.session_factory = sessionmaker(
                bind=candidate,
                autoflush=False,
                expire_on_commit=False,
            )
            self.active_url = candidate_url
            self.state = "ready"
            self.last_connected_at = utcnow()
            self.last_error = None

    def session(self) -> Session:
        factory = self.session_factory
        if factory is None:
            raise RuntimeError("Database is not ready")
        return factory()

    def summary(self) -> dict[str, Any] | None:
        url = self.active_url
        if url is None:
            return None
        if url.drivername.startswith("sqlite"):
            from pathlib import Path

            return {"type": "sqlite", "filename": Path(url.database or "").name}
        return {
            "type": "postgresql",
            "host": url.host,
            "port": url.port or 5432,
            "database": url.database,
            "user": url.username,
            "sslmode": str(url.query.get("sslmode", "disable")),
        }

    def dispose(self) -> None:
        with self.lock:
            if self.engine is not None:
                self.engine.dispose()
            self.engine = None
            self.session_factory = None
            self.active_url = None
            self.state = "stopped"


def create_database_engine(url: URL) -> Engine:
    if url.drivername.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 15},
        )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": 5},
    )


def safe_error(exc: Exception) -> str:
    text_value = str(exc).replace(settings.db_password, "***") if settings.db_password else str(exc)
    return text_value[:300]


def apply_legacy_column_upgrades(engine: Engine) -> None:
    """Small compatibility bridge for databases created by early releases.

    New installations get the full schema from metadata. Existing installations gain
    additive columns here. Destructive/type-changing migrations should still be done
    by a dedicated migration command.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    additions: dict[str, list[tuple[str, str]]] = {
        "devices": [
            ("parent_node_id", "VARCHAR(160)"),
            ("is_starred", "BOOLEAN DEFAULT false"),
            ("rssi", "INTEGER"),
            ("rx_counter_bytes", "BIGINT DEFAULT 0"),
            ("tx_counter_bytes", "BIGINT DEFAULT 0"),
            ("ssid", "VARCHAR(255)"),
            ("ap_sn", "VARCHAR(128)"),
            ("ap_name", "VARCHAR(255)"),
            ("last_online_at", "TIMESTAMP"),
            ("last_offline_at", "TIMESTAMP"),
            ("usage_state", "VARCHAR(64) DEFAULT '空闲'"),
        ],
        "network_nodes": [
            ("serial_number", "VARCHAR(160)"),
            ("alias", "VARCHAR(255)"),
            ("model", "VARCHAR(128)"),
            ("management_ip", "VARCHAR(64)"),
            ("parent_node_id", "VARCHAR(160)"),
            ("is_online", "BOOLEAN DEFAULT true"),
            ("is_starred", "BOOLEAN DEFAULT false"),
            ("last_offline_at", "TIMESTAMP"),
        ],
        "connection_history": [
            ("start_rx_counter", "BIGINT DEFAULT 0"),
            ("start_tx_counter", "BIGINT DEFAULT 0"),
            ("last_rx_counter", "BIGINT DEFAULT 0"),
            ("last_tx_counter", "BIGINT DEFAULT 0"),
            ("session_rx_bytes", "BIGINT DEFAULT 0"),
            ("session_tx_bytes", "BIGINT DEFAULT 0"),
            ("initial_parent_node_id", "VARCHAR(160)"),
            ("last_parent_node_id", "VARCHAR(160)"),
        ],
        "client_traffic_samples": [
            ("rssi", "INTEGER"),
            ("parent_node_id", "VARCHAR(160)"),
        ],
        "event_logs": [
            ("node_id", "VARCHAR(160)"),
        ],
        "processed_snapshots": [
            ("source", "VARCHAR(64) DEFAULT 'probe'"),
            ("client_count", "INTEGER DEFAULT 0"),
            ("node_count", "INTEGER DEFAULT 0"),
        ],
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            if table not in tables:
                continue
            existing = {item["name"] for item in inspector.get_columns(table)}
            for name, sql_type in columns:
                if name not in existing:
                    try:
                        connection.execute(
                            text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')
                        )
                    except Exception:
                        pass
        if "processed_snapshots" in tables:
            ps_cols = {item["name"]: item for item in inspector.get_columns("processed_snapshots")}
            for old_col in ("processed_at", "device_count"):
                if old_col in ps_cols and not ps_cols[old_col].get("nullable", True):
                    try:
                        if engine.dialect.name == "postgresql":
                            connection.execute(
                                text(f'ALTER TABLE "processed_snapshots" ALTER COLUMN "{old_col}" DROP NOT NULL')
                            )
                    except Exception:
                        pass


def verify_candidate(url: URL) -> dict[str, Any]:
    engine = create_database_engine(url)
    try:
        Base.metadata.create_all(engine)
        apply_legacy_column_upgrades(engine)
        with engine.begin() as connection:
            if url.drivername.startswith("postgresql"):
                row = connection.execute(
                    text("SELECT current_database(), current_user")
                ).one()
                connection.execute(
                    text(
                        "CREATE TEMP TABLE ruijie_monitor_probe "
                        "(id INTEGER PRIMARY KEY, value TEXT NOT NULL) ON COMMIT DROP"
                    )
                )
                connection.execute(
                    text("INSERT INTO ruijie_monitor_probe VALUES (1, 'ok')")
                )
                probe_value = connection.execute(
                    text("SELECT value FROM ruijie_monitor_probe WHERE id=1")
                ).scalar_one()
                if probe_value != "ok":
                    raise RuntimeError("Data integrity check failed")
                return {"database": row[0], "user": row[1]}
            else:
                row = connection.execute(text("PRAGMA compile_options")).all()
                connection.execute(
                    text(
                        "CREATE TEMP TABLE ruijie_monitor_probe "
                        "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                    )
                )
                connection.execute(
                    text("INSERT INTO ruijie_monitor_probe VALUES (1, 'ok')")
                )
                probe_value = connection.execute(
                    text("SELECT value FROM ruijie_monitor_probe WHERE id=1")
                ).scalar_one()
                if probe_value != "ok":
                    raise RuntimeError("Data integrity check failed")
                return {"sqlite_version": row[0][0] if row else "unknown"}
    finally:
        engine.dispose()


db_runtime = DatabaseRuntime()


def get_db() -> Generator[Session, None, None]:
    if db_runtime.session_factory is None:
        raise HTTPException(503, "数据库尚未就绪")
    session = db_runtime.session()
    try:
        yield session
    finally:
        session.close()
