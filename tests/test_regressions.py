from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.analytics import journey_analytics, traffic_analytics
from backend.collector.models import DeviceObservation, NetworkNodeObservation, RouterSnapshot
from backend.collector.parsers import parse_clients, parse_rssi
from backend.config import settings
from backend.db import (
    ClientTrafficSample,
    ConnectionHistory,
    Device,
    DeviceAlias,
    EventLog,
    NetworkNode,
    RoamingSegment,
    db_runtime,
)
from backend.main import app, database_status_payload, serialize_favorite_activity
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
        if os.name != "nt":
            assert control_db.stat().st_mode & 0o777 == 0o600

        config = client.get("/api/config")
        assert config.status_code == 200
        payload = config.json()
        assert payload["database"]["type"] in {"sqlite", "postgresql"}
        assert "database_runtime" in payload
        assert payload["retention_days_normal"] == 1
        assert payload["retention_days_starred"] == 30

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
    if os.name != "nt":
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


def test_one_minute_absence_confirms_offline_and_favorite_activity_reports_move() -> None:
    reset_business_database()
    started_at = utcnow().replace(microsecond=0)
    first_node = NetworkNodeObservation(
        node_id="sn:AP-OFFICE",
        serial_number="AP-OFFICE",
        node_type="ap",
        name="AP-OFFICE",
        model="RG-EAP",
        management_ip=None,
        parent_node_id=None,
    )
    second_node = NetworkNodeObservation(
        node_id="sn:AP-WORKSHOP",
        serial_number="AP-WORKSHOP",
        node_type="ap",
        name="AP-WORKSHOP",
        model="RG-EAP",
        management_ip=None,
        parent_node_id=None,
    )
    first_device = DeviceObservation(
        mac="AA:BB:CC:DD:EE:09",
        ip="192.168.1.9",
        hostname="followed-client",
        ap_sn="AP-OFFICE",
        ap_name="3楼办公室",
        parent_node_id="sn:AP-OFFICE",
        ssid="Office",
        rssi=-55,
        rx_counter_bytes=0,
        tx_counter_bytes=0,
        rx_rate_kbps=0,
        tx_rate_kbps=0,
        usage_state="空闲",
    )
    process_snapshot(
        RouterSnapshot(
            snapshot_id="offline-grace-1",
            collected_at=started_at,
            source="test",
            complete=True,
            devices=(first_device,),
            nodes=(first_node, second_node),
        )
    )
    moved_device = DeviceObservation(
        **{
            **first_device.__dict__,
            "ap_sn": "AP-WORKSHOP",
            "ap_name": "车间办公室",
            "parent_node_id": "sn:AP-WORKSHOP",
        }
    )
    with db_runtime.session() as db:
        with db.begin():
            db.get(NetworkNode, first_node.node_id).alias = "3楼办公室"
            db.get(NetworkNode, second_node.node_id).alias = "车间办公室"

    process_snapshot(
        RouterSnapshot(
            snapshot_id="offline-grace-2",
            collected_at=started_at + timedelta(seconds=10),
            source="test",
            complete=True,
            devices=(moved_device,),
            nodes=(first_node, second_node),
        )
    )
    with db_runtime.session() as db:
        with db.begin():
            db.get(Device, first_device.mac).is_starred = True

    process_snapshot(
        RouterSnapshot(
            snapshot_id="offline-grace-3",
            collected_at=started_at + timedelta(seconds=40),
            source="test",
            complete=True,
            devices=(),
            nodes=(first_node, second_node),
        )
    )
    with db_runtime.session() as db:
        device = db.get(Device, first_device.mac)
        assert device is not None and device.is_online is True
        activity = serialize_favorite_activity(db)
        assert activity[0]["status"] == "moved"
        assert "从 3楼办公室 切换到 车间办公室" in activity[0]["change_message"]

    process_snapshot(
        RouterSnapshot(
            snapshot_id="offline-grace-4",
            collected_at=started_at + timedelta(seconds=71),
            source="test",
            complete=True,
            devices=(),
            nodes=(first_node, second_node),
        )
    )
    with db_runtime.session() as db:
        device = db.get(Device, first_device.mac)
        assert device is not None and device.is_online is False
        assert device.last_offline_at == started_at + timedelta(seconds=70)
        session = db.scalars(
            select(ConnectionHistory).where(ConnectionHistory.mac == first_device.mac)
        ).one()
        assert session.session_end == started_at + timedelta(seconds=70)
        latest_event = db.scalars(
            select(EventLog)
            .where(EventLog.mac == first_device.mac, EventLog.event_type == "OFFLINE")
            .order_by(EventLog.id.desc())
        ).one()
        assert "超过 1 分钟" in latest_event.message


def test_retention_uses_longer_window_for_starred_clients_without_deleting_identity() -> None:
    reset_business_database()
    settings.retention_days_normal = 1
    settings.retention_days_starred = 30
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
                        last_seen=now - timedelta(days=2),
                    ),
                    Device(
                        mac=starred_mac,
                        hostname="starred",
                        is_online=False,
                        is_starred=True,
                        first_seen=now - timedelta(days=400),
                        last_seen=now - timedelta(days=2),
                    ),
                    DeviceAlias(mac=normal_mac, alias="普通设备"),
                    DeviceAlias(mac=starred_mac, alias="关注设备"),
                    ClientTrafficSample(
                        mac=normal_mac,
                        sampled_at=now - timedelta(days=2),
                        rx_rate_kbps=1,
                        tx_rate_kbps=1,
                    ),
                    ClientTrafficSample(
                        mac=starred_mac,
                        sampled_at=now - timedelta(days=2),
                        rx_rate_kbps=1,
                        tx_rate_kbps=1,
                    ),
                    ClientTrafficSample(
                        mac=starred_mac,
                        sampled_at=now - timedelta(days=31),
                        rx_rate_kbps=1,
                        tx_rate_kbps=1,
                    ),
                    EventLog(
                        mac=normal_mac,
                        event_type="OFFLINE",
                        message="old normal",
                        created_at=now - timedelta(days=2),
                    ),
                    EventLog(
                        mac=starred_mac,
                        event_type="OFFLINE",
                        message="keep starred",
                        created_at=now - timedelta(days=2),
                    ),
                    ConnectionHistory(
                        mac=normal_mac,
                        session_start=now - timedelta(days=3),
                        session_end=now - timedelta(days=2),
                    ),
                    ConnectionHistory(
                        mac=starred_mac,
                        session_start=now - timedelta(days=3),
                        session_end=now - timedelta(days=2),
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
        assert starred_samples[0].sampled_at == now - timedelta(days=2)
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


def test_traffic_analytics_aggregates_curves_and_reports_tracking_policy() -> None:
    reset_business_database()
    now = utcnow().replace(microsecond=0)
    mac = "AA:BB:CC:DD:EE:30"
    with db_runtime.session() as db:
        with db.begin():
            db.add(Device(mac=mac, hostname="tracked", is_online=True, is_starred=True))
            for index in range(6):
                db.add(
                    ClientTrafficSample(
                        mac=mac,
                        sampled_at=now - timedelta(seconds=50 - index * 10),
                        rx_rate_kbps=100 + index * 10,
                        tx_rate_kbps=20 + index,
                        rssi=-70 + index,
                    )
                )
    with db_runtime.session() as db:
        result = traffic_analytics(
            db,
            mac,
            "1m",
            normal_retention_days=1,
            starred_retention_days=30,
            now=now,
        )
    assert result["retention_days"] == 30
    assert result["sample_interval_seconds"] == 10
    assert len(result["points"]) == 6
    assert result["summary"]["peak_rx_kbps"] == 150
    assert result["summary"]["average_rssi"] == -68


def test_journey_inference_marks_short_same_ap_gap_as_signal_blip() -> None:
    reset_business_database()
    now = utcnow().replace(microsecond=0)
    mac = "AA:BB:CC:DD:EE:40"
    node_id = "sn:AP-OFFICE"
    with db_runtime.session() as db:
        with db.begin():
            db.add(NetworkNode(node_id=node_id, node_type="ap", name="AP-1", alias="3楼办公室"))
            db.add(
                Device(
                    mac=mac,
                    hostname="phone",
                    is_online=True,
                    is_starred=True,
                    parent_node_id=node_id,
                    first_seen=now - timedelta(minutes=10),
                )
            )
            first = ConnectionHistory(
                mac=mac,
                session_start=now - timedelta(minutes=10),
                session_end=now - timedelta(minutes=5, seconds=20),
                initial_parent_node_id=node_id,
                last_parent_node_id=node_id,
            )
            second = ConnectionHistory(
                mac=mac,
                session_start=now - timedelta(minutes=5),
                session_end=None,
                initial_parent_node_id=node_id,
                last_parent_node_id=node_id,
            )
            db.add_all([first, second])
            db.flush()
            db.add_all([
                RoamingSegment(session_id=first.id, parent_node_id=node_id, parent_name_snapshot="AP-1", entered_at=first.session_start, left_at=first.session_end),
                RoamingSegment(session_id=second.id, parent_node_id=node_id, parent_name_snapshot="AP-1", entered_at=second.session_start, left_at=None),
            ])
    with db_runtime.session() as db:
        result = journey_analytics(db, mac, "24h", now=now)
    offline = [item for item in result["timeline"] if item["kind"] == "offline"]
    assert any(item["duration_seconds"] == 20 for item in offline)
    short_gap = next(item for item in offline if item["duration_seconds"] == 20)
    assert "信号抖动" in short_gap["inference"]
    assert short_gap["confidence"] == "较高"
    assert "3楼办公室" in result["summary"]["locations"]


def test_one_year_absence_purges_client_material_and_keeps_recent_client() -> None:
    reset_business_database()
    now = utcnow().replace(microsecond=0)
    stale_mac = "AA:BB:CC:DD:EE:50"
    recent_mac = "AA:BB:CC:DD:EE:51"
    stale_at = now - timedelta(days=366)
    with db_runtime.session() as db:
        with db.begin():
            db.add_all([
                Device(
                    mac=stale_mac,
                    hostname="old-client",
                    is_online=False,
                    is_starred=True,
                    first_seen=stale_at,
                    last_seen=stale_at,
                    last_offline_at=stale_at,
                ),
                Device(
                    mac=recent_mac,
                    hostname="recent-client",
                    is_online=False,
                    is_starred=False,
                    first_seen=now - timedelta(days=10),
                    last_seen=now - timedelta(days=10),
                    last_offline_at=now - timedelta(days=10),
                ),
                DeviceAlias(mac=stale_mac, alias="旧备注"),
                ClientTrafficSample(mac=stale_mac, sampled_at=stale_at),
                EventLog(mac=stale_mac, event_type="OFFLINE", message="old"),
            ])
            session = ConnectionHistory(
                mac=stale_mac,
                session_start=stale_at - timedelta(minutes=10),
                session_end=stale_at,
            )
            db.add(session)
            db.flush()
            db.add(
                RoamingSegment(
                    session_id=session.id,
                    parent_node_id="sn:OLD",
                    parent_name_snapshot="旧位置",
                    entered_at=session.session_start,
                    left_at=stale_at,
                )
            )

    with db_runtime.session() as db:
        with db.begin():
            cleanup_expired_history(db, now)

    with db_runtime.session() as db:
        assert db.get(Device, stale_mac) is None
        assert db.get(DeviceAlias, stale_mac) is None
        assert db.scalar(select(ClientTrafficSample).where(ClientTrafficSample.mac == stale_mac)) is None
        assert db.scalar(select(EventLog).where(EventLog.mac == stale_mac)) is None
        assert db.scalar(select(ConnectionHistory).where(ConnectionHistory.mac == stale_mac)) is None
        assert db.get(Device, recent_mac) is not None


def test_frontend_contains_null_safe_database_normalization_and_no_fixed_reset_password() -> None:
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    assert "normalizeDatabaseState" in source
    assert "const active=state.database.active" in source
    assert "state.database.active.type" not in source
    assert "routerTestRevision===v.revision" in source
    assert "dbTestRevision===v.revision" in source
    assert "validation.routerTestRevision=-1" in source
    assert "validation.dbTestRevision=-1" in source
    assert "docker exec -it ruijie-router-monitor python -m backend.cli auth reset-password" in source
    assert "123456" not in source


def test_collector_uses_direct_http_without_browser_runtime() -> None:
    source = Path("backend/collector/supervisor.py").read_text(encoding="utf-8")
    assert "EwebApiClient" in source
    assert "playwright" not in source.lower()
    assert 'source="direct_api"' in source


def test_eweb_password_encryption_matches_router_javascript() -> None:
    from backend.collector.crypto import encrypt_eweb_password

    encrypted = encrypt_eweb_password(
        "test-password",
        "19aa5462174bafd5088ee1292353546a",
        salt=bytes(range(8)),
    )
    assert encrypted == "U2FsdGVkX18AAQIDBAUGB1rpb5ExMtadoyNCZUHX/IE="


def test_eweb_api_client_uses_sid_and_signed_json_headers() -> None:
    import asyncio
    import json

    import httpx

    from backend.collector.crypto import signed_headers
    from backend.collector.eweb_api import EwebApiClient

    seen_modules: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<script>params.pwd = GibberishAES.enc(passwordEl.value, '
                    '"dynamic-login-key")</script>'
                ),
            )

        payload = json.loads(request.content)
        if request.url.path.endswith("/api/auth"):
            assert payload["method"] == "login"
            assert payload["params"]["pwd"] != "router-password"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"token": "page-token", "sid": "api-sid", "sn": "router-sn"},
                },
                headers={"set-cookie": "router-sn=api-sid; Path=/cgi-bin/luci"},
            )

        assert request.url.params["auth"] == "api-sid"
        expected = signed_headers(request.content)
        assert request.headers["Content-Accept"] == expected["Content-Accept"]
        assert request.headers["Contents-Accept"] == expected["Contents-Accept"]
        module = payload["params"]["module"]
        seen_modules.append(module)
        if module == "local_topology":
            data = {"topo": {"deviceType": "EGW", "deviceSn": "router-sn"}}
        else:
            data = {"list": [], "total": "0"}
        return httpx.Response(200, json={"code": 0, "data": data})

    async def run() -> None:
        client = EwebApiClient(
            "http://router.test",
            "router-password",
            transport=httpx.MockTransport(handler),
        )
        try:
            topology, clients = await client.fetch_snapshot_payloads()
        finally:
            await client.close()
        assert "topo" in topology
        assert clients["total"] == "0"

    asyncio.run(run())
    assert sorted(seen_modules) == ["local_topology", "user_list"]
