from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import URL


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _quote_env(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


@dataclass
class Settings:
    data_dir: Path
    config_file: Path
    app_name: str = "Ruijie Router Monitor"
    router_host: str = "http://192.168.8.1"
    router_user: str = "admin"
    router_password: str = ""
    poll_interval: int = 10
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    database_type: str = "sqlite"
    sqlite_filename: str = "monitor.db"
    db_host: str = ""
    db_port: int = 5432
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    db_sslmode: str = "disable"
    cookie_secure: bool = False
    retention_days_normal: int = 30
    retention_days_starred: int = 180
    self_restart_enabled: bool = True

    @classmethod
    def load(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "/app/data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        config_file = Path(
            os.getenv("CONFIG_FILE", str(data_dir / "config.env"))
        ).resolve()
        if config_file.parent != data_dir:
            raise RuntimeError("CONFIG_FILE must be inside DATA_DIR")

        file_values = dotenv_values(config_file) if config_file.exists() else {}

        def value(key: str, default: str = "") -> str:
            env_value = os.getenv(key)
            if env_value is not None:
                return env_value
            file_value = file_values.get(key)
            return str(file_value) if file_value is not None else default

        # Legacy DATABASE_URL is read once for compatibility.
        database_type = value("DATABASE_TYPE", "")
        legacy_url = value("DATABASE_URL", "")
        parsed_legacy = None
        if not database_type and legacy_url:
            from sqlalchemy.engine import make_url

            try:
                parsed_legacy = make_url(legacy_url)
                database_type = (
                    "postgresql"
                    if parsed_legacy.drivername.startswith("postgresql")
                    else "sqlite"
                )
            except Exception:
                database_type = "sqlite"

        settings = cls(
            data_dir=data_dir,
            config_file=config_file,
            router_host=value("RUIJIE_HOST", "http://192.168.8.1").rstrip("/"),
            router_user=value("RUIJIE_USER", "admin"),
            router_password=value("RUIJIE_PASS", ""),
            poll_interval=max(3, min(300, int(value("POLL_INTERVAL", "10")))),
            telegram_token=value("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=value("TELEGRAM_CHAT_ID", ""),
            telegram_enabled=_as_bool(value("TELEGRAM_ENABLE", "false")),
            database_type=database_type or "sqlite",
            sqlite_filename=value("SQLITE_FILENAME", "monitor.db"),
            db_host=value("DB_HOST", ""),
            db_port=int(value("DB_PORT", "5432")),
            db_name=value("DB_NAME", ""),
            db_user=value("DB_USER", ""),
            db_password=value("DB_PASSWORD", ""),
            db_sslmode=value("DB_SSLMODE", "disable"),
            cookie_secure=_as_bool(value("COOKIE_SECURE", "false")),
            retention_days_normal=max(1, min(3650, int(value("RETENTION_DAYS_NORMAL", "30")))),
            retention_days_starred=max(1, min(3650, int(value("RETENTION_DAYS_STARRED", "180")))),
            self_restart_enabled=_as_bool(value("ALLOW_SELF_RESTART", "true"), True),
        )

        if parsed_legacy is not None:
            if settings.database_type == "postgresql":
                settings.db_host = parsed_legacy.host or ""
                settings.db_port = parsed_legacy.port or 5432
                settings.db_name = parsed_legacy.database or ""
                settings.db_user = parsed_legacy.username or ""
                settings.db_password = parsed_legacy.password or ""
            else:
                settings.sqlite_filename = Path(
                    parsed_legacy.database or "monitor.db"
                ).name
        return settings

    def database_url(self) -> URL:
        if self.database_type == "sqlite":
            filename = validate_sqlite_filename(self.sqlite_filename)
            path = (self.data_dir / filename).resolve()
            if path.parent != self.data_dir:
                raise ValueError("SQLite file must be inside DATA_DIR")
            return URL.create("sqlite", database=str(path))

        if not all([self.db_host, self.db_name, self.db_user, self.db_password]):
            raise ValueError("PostgreSQL configuration is incomplete")
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"sslmode": self.db_sslmode},
        )

    def database_summary(self) -> dict[str, Any]:
        if self.database_type == "sqlite":
            return {"type": "sqlite", "filename": self.sqlite_filename}
        return {
            "type": "postgresql",
            "host": self.db_host,
            "port": self.db_port,
            "database": self.db_name,
            "user": self.db_user,
            "sslmode": self.db_sslmode,
        }

    def save(self) -> None:
        values = {
            "RUIJIE_HOST": self.router_host,
            "RUIJIE_USER": self.router_user,
            "RUIJIE_PASS": self.router_password,
            "POLL_INTERVAL": self.poll_interval,
            "TELEGRAM_BOT_TOKEN": self.telegram_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "TELEGRAM_ENABLE": str(self.telegram_enabled).lower(),
            "DATABASE_TYPE": self.database_type,
            "SQLITE_FILENAME": self.sqlite_filename,
            "DB_HOST": self.db_host,
            "DB_PORT": self.db_port,
            "DB_NAME": self.db_name,
            "DB_USER": self.db_user,
            "DB_PASSWORD": self.db_password,
            "DB_SSLMODE": self.db_sslmode,
            "COOKIE_SECURE": str(self.cookie_secure).lower(),
            "RETENTION_DAYS_NORMAL": self.retention_days_normal,
            "RETENTION_DAYS_STARRED": self.retention_days_starred,
            "ALLOW_SELF_RESTART": str(self.self_restart_enabled).lower(),
        }
        fd, temp_path = tempfile.mkstemp(
            prefix="config.env.", dir=self.data_dir, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for key, item in values.items():
                    stream.write(f"{key}={_quote_env(item)}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.config_file)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def validate_sqlite_filename(value: str) -> str:
    value = (value or "monitor.db").strip()
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("SQLite only accepts a file name")
    if not value.endswith(".db"):
        value += ".db"
    return value


settings = Settings.load()
