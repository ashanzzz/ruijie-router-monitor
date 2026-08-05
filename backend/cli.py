from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from sqlalchemy import MetaData, URL, create_engine, func, insert, select

from backend.auth_utils import init_control_db, reset_admin_password
from backend.config import settings
from backend.db.models import Base
from backend.db.runtime import create_database_engine, verify_candidate


def auth_reset(password_stdin: bool) -> None:
    init_control_db()
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New administrator password: ")
        confirm = getpass.getpass("Confirm administrator password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match")
    reset_admin_password(password)
    print("Administrator password reset; all sessions revoked.")


def database_inspect() -> None:
    url = settings.database_url()
    result = verify_candidate(url)
    print(json.dumps({"configured": settings.database_summary(), **result}, ensure_ascii=False, indent=2))


def migrate_sqlite(source: Path, dry_run: bool) -> None:
    if settings.database_type != "postgresql":
        raise SystemExit("Target configuration is not PostgreSQL")
    if not source.exists():
        raise SystemExit(f"Source SQLite file does not exist: {source}")

    source_engine = create_engine(
        URL.create("sqlite", database=str(source.resolve())),
        connect_args={"check_same_thread": False},
    )
    target_engine = create_database_engine(settings.database_url())
    Base.metadata.create_all(target_engine)

    source_meta = MetaData()
    source_meta.reflect(source_engine)
    target_meta = MetaData()
    target_meta.reflect(target_engine)
    table_names = [
        "network_nodes",
        "devices",
        "device_aliases",
        "connection_history",
        "roaming_segments",
        "client_traffic_samples",
        "event_logs",
        "processed_snapshots",
    ]
    plan = {}
    with source_engine.connect() as source_conn, target_engine.connect() as target_conn:
        for name in table_names:
            if name not in source_meta.tables or name not in target_meta.tables:
                continue
            source_count = source_conn.execute(
                select(func.count()).select_from(source_meta.tables[name])
            ).scalar_one()
            target_count = target_conn.execute(
                select(func.count()).select_from(target_meta.tables[name])
            ).scalar_one()
            plan[name] = {"source": source_count, "target": target_count}
            if target_count:
                raise SystemExit(f"Target table {name} is not empty; migration aborted")

    print(json.dumps({"dry_run": dry_run, "tables": plan}, ensure_ascii=False, indent=2))
    if dry_run:
        source_engine.dispose()
        target_engine.dispose()
        return

    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        for name in table_names:
            if name not in plan:
                continue
            source_table = source_meta.tables[name]
            target_table = target_meta.tables[name]
            common_columns = [
                column.name for column in target_table.columns if column.name in source_table.c
            ]
            rows = source_conn.execute(select(*(source_table.c[name] for name in common_columns))).mappings()
            batch = []
            for row in rows:
                batch.append(dict(row))
                if len(batch) >= 500:
                    target_conn.execute(insert(target_table), batch)
                    batch.clear()
            if batch:
                target_conn.execute(insert(target_table), batch)

    print("Migration completed. Verify counts before starting the main container.")
    source_engine.dispose()
    target_engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ruijie-monitor")
    group = parser.add_subparsers(dest="group", required=True)
    auth = group.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="command", required=True)
    reset = auth_commands.add_parser("reset-password")
    reset.add_argument("--password-stdin", action="store_true")

    database = group.add_parser("database")
    database_commands = database.add_subparsers(dest="command", required=True)
    database_commands.add_parser("inspect")
    migrate = database_commands.add_parser("migrate")
    migrate.add_argument("--source-sqlite", required=True)
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.group == "auth" and args.command == "reset-password":
        auth_reset(args.password_stdin)
    elif args.group == "database" and args.command == "inspect":
        database_inspect()
    elif args.group == "database" and args.command == "migrate":
        migrate_sqlite(Path(args.source_sqlite), args.dry_run)


if __name__ == "__main__":
    main()
