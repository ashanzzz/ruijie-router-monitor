from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.collector.models import DeviceObservation, NetworkNodeObservation, RouterSnapshot
from backend.collector.parsers import parse_clients, parse_rssi
from backend.config import Settings, settings
from backend.db import (
    ClientTrafficSample,
    ConnectionHistory,
    Device,
    DeviceAlias,
    EventLog,
    NetworkNode,
    db_runtime,
)
from backend.main import (
    DatabaseConfigPatch,
    DatabaseRequest,
    app,
    database_status_payload,
    save_database_config,
)
from backend.db.runtime import verify_candidate
from backend.service import cleanup_expired_history, process_snapshot
from backend.time_utils import utcnow


def reset_business_database() -> None:
    db_runtime.dispose()
    db_path = settings.data_dir / settings.sqlite_filename
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    settings.database_type = "sqlite"
    settings.sqlite_filename = "monitor.db"
    db_runtime.initialize(settings)


def test_rssi_parser_accepts_dbm_and_rejects_quality_percentage() -> None:
    assert parse_rssi({"rssi": -61}) == -61
    assert parse_rssi({"signal": "-72 dBm"}) == -72
    assert parse_rssi({"signal": 65}) is None
    assert parse_rssi({"rssi": -180}) is None

    clients = parse_clients(
        [{"mac": "AA-BB-CC-DD-EE-FF", "ip": "192.168.1.10", "rssi": -58}],
        {},
    )
    assert clients[0].mac == "AA:BB:CC:DD:EE:FF"
    assert clients[0].rssi == -58


def test_config_and_database_status_contract_survives_missing_active_database() -> None:
    with TestClient(app) as client:
        status = client.get("/api/auth/status").json()
        if status["setup_required"]:
            setup = client.post(
                "/api/auth/setup",
                json={"password": "test-admin-123", "confirm_password": "test-admin-123"},
            )
            assert setup.status_code == 200
        else:
            login = client.post("/api/auth/login", json={"password": "test-admin-123"})
            assert login.status_code == 200

        control_db = settings.data_dir / "control.db"
        assert control_db.exists()
        assert control_db.stat().st_mode & 0o777 == 0o600

        config = client.get("/api/config")
        assert config.status_code == 200
        payload = config.json()
        assert payload["database"]["type"] in {"sqlite", "postgresql"}
        assert "database_runtime" in payload
        assert payload["retention_days_normal"] == 30
        assert payload["retention_days_starred"] == 180

        # Simulate the exact state that previously crashed the settings modal.
        db_runtime.active_url = None
        db_runtime.state = "connection_failed"
        runtime = database_status_payload()
        assert runtime["active"] is None
        assert runtime["configured"]["type"] in {"sqlite", "postgresql"}

        config = client.get("/api/config")
        assert config.status_code == 200
        assert config.json()["database_runtime"]["active"] is None

        auth = client.get("/api/auth/status").json()
        restart = client.post(
            "/api/system/restart",
            json={},
            headers={"X-CSRF-Token": auth["csrf_token"]},
        )
        assert restart.status_code == 409


def test_sqlite_candidate_test_verifies_read_and_write() -> None:
    reset_business_database()
    assert (settings.data_dir / settings.sqlite_filename).stat().st_mode & 0o777 == 0o600
    assert verify_candidate(settings.database_url())["read_write"] is True


def test_snapshot_persists_rssi_and_incremental_session_traffic() -> None:
    reset_business_database()
    first_at = utcnow().replace(microsecond=0)
    node = NetworkNodeObservation(
        node_id="sn:AP001",
        serial_number="AP001",
        node_type="ap",
        name="客厅AP",
        model="RG-EAP",
        management_ip="192.168.1.2",
        parent_node_id=None,
    )
    first_device = DeviceObservation(
        mac="AA:BB:CC:DD:EE:01",
        ip="192.168.1.50",
        hostname="HOST001",
        ap_sn="AP001",
        ap_name="客厅AP",
        parent_node_id="sn:AP001",
        ssid="Home-5G",
        rssi=-60,
        rx_counter_bytes=1000,
        tx_counter_bytes=2000,
        rx_rate_kbps=20.0,
        tx_rate_kbps=10.0,
        usage_state="轻微活动",
    )
    process_snapshot(
        RouterSnapshot(
            snapshot_id="test-rssi-1",
            collected_at=first_at,
            source="test",
            complete=True,
            devices=(first_device,),
            nodes=(node,),
        )
    )

    second_device = DeviceObservation(
        **{
            **first_device.__dict__,
            "rssi": -55,
            "rx_counter_bytes": 1500,
            "tx_counter_bytes": 2400,
        }
    )
    process_snapshot(
        RouterSnapshot(
            snapshot_id="test-rssi-2",
            collected_at=first_at + timedelta(seconds=61),
            source="test",
            complete=True,
            devices=(second_device,),
            nodes=(node,),
        )
    )

    with db_runtime.session() as db:
        device = db.get(Device, first_device.mac)
        assert device is not None
        assert device.rssi == -55
        samples = list(
            db.scalars(
                select(ClientTrafficSample)
                .where(ClientTrafficSample.mac == first_device.mac)
                .order_by(ClientTrafficSample.sampled_at)
            )
        )
        assert [item.rssi for item in samples] == [-60, -55]
        session = db.scalars(
            select(ConnectionHistory).where(
                ConnectionHistory.mac == first_device.mac,
                ConnectionHistory.session_end.is_(None),
            )
        ).one()
        assert session.session_rx_bytes == 500
        assert session.session_tx_bytes == 400


def test_retention_uses_longer_window_for_starred_clients_without_deleting_identity() -> None:
    reset_business_database()
    settings.retention_days_normal = 30
    settings.retention_days_starred = 180
    now = utcnow().replace(microsecond=0)
    normal_mac = "AA:BB:CC:DD:EE:10"
    starred_mac = "AA:BB:CC:DD:EE:20"

    with db_runtime.session() as db:
        with db.begin():
            db.add_all(
                [
                    Device(
                        mac=normal_mac,
                        hostname="normal",
                        is_online=False,
                        is_starred=False,
                        first_seen=now - timedelta(days=400),
                        last_seen=now - timedelta(days=31),
                    ),
                    Device(
                        mac=starred_mac,
                        hostname="starred",
                        is_online=False,
                        is_starred=True,
                        first_seen=now - timedelta(days=400),
                        last_seen=now - timedelta(days=31),
                    ),
                    DeviceAlias(mac=normal_mac, alias="普通设备"),
                    DeviceAlias(mac=starred_mac, alias="关注设备"),
                    ClientTrafficSample(
                        mac=normal_mac,
                        sampled_at=now - timedelta(days=31),
                        rx_rate_kbps=1,
                        tx_rate_kbps=1,
                    ),
                    ClientTrafficSample(
                        mac=starred_mac,
                        sampled_at=now - timedelta(days=31),
                        rx_rate_kbps=1,
                        tx_rate_kbps=1,
                    ),
                    ClientTrafficSample(
                        mac=starred_mac,
                        sampled_at=now - timedelta(days=181),
                        rx_rate_kbps=1,
                        tx_rate_kbps=1,
                    ),
                    EventLog(
                        mac=normal_mac,
                        event_type="OFFLINE",
                        message="old normal",
                        created_at=now - timedelta(days=31),
                    ),
                    EventLog(
                        mac=starred_mac,
                        event_type="OFFLINE",
                        message="keep starred",
                        created_at=now - timedelta(days=31),
                    ),
                    ConnectionHistory(
                        mac=normal_mac,
                        session_start=now - timedelta(days=32),
                        session_end=now - timedelta(days=31),
                    ),
                    ConnectionHistory(
                        mac=starred_mac,
                        session_start=now - timedelta(days=32),
                        session_end=now - timedelta(days=31),
                    ),
                ]
            )

    with db_runtime.session() as db:
        with db.begin():
            cleanup_expired_history(db, now)

    with db_runtime.session() as db:
        assert db.get(Device, normal_mac) is not None
        assert db.get(Device, starred_mac) is not None
        assert db.get(DeviceAlias, normal_mac) is not None
        assert db.get(DeviceAlias, starred_mac) is not None
        normal_samples = list(
            db.scalars(select(ClientTrafficSample).where(ClientTrafficSample.mac == normal_mac))
        )
        starred_samples = list(
            db.scalars(select(ClientTrafficSample).where(ClientTrafficSample.mac == starred_mac))
        )
        assert normal_samples == []
        assert len(starred_samples) == 1
        assert starred_samples[0].sampled_at == now - timedelta(days=31)
        assert db.scalar(
            select(EventLog).where(EventLog.mac == normal_mac).limit(1)
        ) is None
        assert db.scalar(
            select(EventLog).where(EventLog.mac == starred_mac).limit(1)
        ) is not None
        assert db.scalar(
            select(ConnectionHistory).where(ConnectionHistory.mac == normal_mac).limit(1)
        ) is None
        assert db.scalar(
            select(ConnectionHistory).where(ConnectionHistory.mac == starred_mac).limit(1)
        ) is not None


def test_frontend_contains_configured_cards_and_no_fixed_reset_password() -> None:
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    assert "normalizeDatabaseState" in source
    assert "state.database.active.type" not in source
    assert "✓ 已配置" in source
    assert "重新配置" in source
    assert "/api/config/router" in source
    assert "/api/config/database" in source
    assert "credentialsChanged&&!v.routerProbeToken" in source
    assert "docker exec -it ruijie-router-monitor python -m backend.cli auth reset-password" in source
    assert "123456" not in source


def test_database_save_uses_username_and_reverifies(monkeypatch, tmp_path) -> None:
    from backend import main as main_module

    old_values = settings.__dict__.copy()
    settings.data_dir = tmp_path
    settings.config_file = tmp_path / "config.env"
    settings.database_type = "sqlite"
    settings.sqlite_filename = "monitor.db"

    verified = []

    def fake_verify(url):
        verified.append(url)
        return {"read_write": True, "database": "ruijie", "user": "monitor_user"}

    monkeypatch.setattr(main_module, "verify_candidate", fake_verify)
    monkeypatch.setattr(main_module.db_runtime, "active_url", settings.database_url())

    request = DatabaseConfigPatch(
        database=DatabaseRequest(
            type="postgresql",
            host="postgres",
            port=5432,
            database="ruijie",
            username="monitor_user",
            password="secret",
            sslmode="disable",
        )
    )
    import asyncio

    result = asyncio.run(save_database_config(request, None))
    assert result["status"] == "success"
    assert result["restart_required"] is True
    assert settings.db_user == "monitor_user"
    assert verified
    assert "DB_USER=\"monitor_user\"" in settings.config_file.read_text()

    for key, value in old_values.items():
        setattr(settings, key, value)


def test_main_source_has_no_database_request_user_typo() -> None:
    source = Path("backend/main.py").read_text(encoding="utf-8")
    assert "payload.database.user or" not in source
    assert "db_req.user or" not in source
    assert "payload.database.username" in source
    assert "verify_candidate(url)" in source


def test_password_only_collector_does_not_fill_empty_username() -> None:
    source = Path("backend/collector/supervisor.py").read_text(encoding="utf-8")
    assert "if self.username and await user_input.count()" in source
    assert "Password-only firmware" in source

