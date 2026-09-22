from __future__ import annotations

from datetime import datetime, timedelta
import time

from sqlalchemy import and_, delete, or_, select

from backend.collector.models import RouterSnapshot
from backend.collector.parsers import traffic_state
from backend.config import settings
from backend.time_utils import utcnow
from backend.db import (
    ClientTrafficSample,
    ConnectionHistory,
    Device,
    DeviceAlias,
    EventLog,
    NetworkNode,
    ProcessedSnapshot,
    RoamingSegment,
    db_runtime,
)


def counter_delta(current: int, previous: int) -> int:
    if current < previous:
        return 0
    return current - previous


def active_session(db, mac: str) -> ConnectionHistory | None:
    return db.scalars(
        select(ConnectionHistory)
        .where(
            ConnectionHistory.mac == mac,
            ConnectionHistory.session_end.is_(None),
        )
        .order_by(ConnectionHistory.id.desc())
        .limit(1)
    ).first()


def display_name(db, device: Device) -> str:
    alias = db.get(DeviceAlias, device.mac)
    return (alias.alias if alias else None) or device.hostname or device.mac


def node_location_name(db, node_id: str | None, fallback: str | None) -> str:
    node = db.get(NetworkNode, node_id) if node_id else None
    return (node.alias if node else None) or (node.name if node else None) or fallback or "未知位置"


def event(db, *, mac: str | None, node_id: str | None, name: str | None, ip: str | None, kind: str, message: str) -> str:
    db.add(
        EventLog(
            mac=mac,
            node_id=node_id,
            device_name=name,
            ip=ip,
            event_type=kind,
            message=message,
        )
    )
    return message


_OFFLINE_CONFIRMATION_SECONDS = 60

_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
_last_cleanup_monotonic = 0.0


def cleanup_expired_history(db, now: datetime) -> None:
    """Delete old telemetry and purge clients absent for one year."""
    normal_cutoff = now - timedelta(days=settings.retention_days_normal)
    starred_cutoff = now - timedelta(days=settings.retention_days_starred)
    starred_macs = select(Device.mac).where(Device.is_starred.is_(True))

    traffic_expired = or_(
        and_(
            ClientTrafficSample.mac.in_(starred_macs),
            ClientTrafficSample.sampled_at < starred_cutoff,
        ),
        and_(
            ClientTrafficSample.mac.not_in(starred_macs),
            ClientTrafficSample.sampled_at < normal_cutoff,
        ),
    )
    db.execute(delete(ClientTrafficSample).where(traffic_expired))

    events_expired = or_(
        and_(EventLog.mac.in_(starred_macs), EventLog.created_at < starred_cutoff),
        and_(
            or_(EventLog.mac.is_(None), EventLog.mac.not_in(starred_macs)),
            EventLog.created_at < normal_cutoff,
        ),
    )
    db.execute(delete(EventLog).where(events_expired))

    old_session_ids = select(ConnectionHistory.id).where(
        ConnectionHistory.session_end.is_not(None),
        or_(
            and_(
                ConnectionHistory.mac.in_(starred_macs),
                ConnectionHistory.session_end < starred_cutoff,
            ),
            and_(
                ConnectionHistory.mac.not_in(starred_macs),
                ConnectionHistory.session_end < normal_cutoff,
            ),
        ),
    )
    db.execute(delete(RoamingSegment).where(RoamingSegment.session_id.in_(old_session_ids)))
    db.execute(delete(ConnectionHistory).where(ConnectionHistory.id.in_(old_session_ids)))
    db.execute(
        delete(ProcessedSnapshot).where(ProcessedSnapshot.collected_at < normal_cutoff)
    )

    purge_stale_clients(db, now)


_CLIENT_IDENTITY_RETENTION_DAYS = 365


def purge_stale_clients(db, now: datetime) -> int:
    """Remove offline clients with no observation for one year."""
    cutoff = now - timedelta(days=_CLIENT_IDENTITY_RETENTION_DAYS)
    stale_clients = list(
        db.scalars(
            select(Device).where(
                Device.is_online.is_(False),
                Device.last_seen < cutoff,
                or_(
                    Device.last_offline_at.is_(None),
                    Device.last_offline_at < cutoff,
                ),
            )
        )
    )
    for device in stale_clients:
        session_ids = select(ConnectionHistory.id).where(
            ConnectionHistory.mac == device.mac
        )
        db.execute(delete(RoamingSegment).where(RoamingSegment.session_id.in_(session_ids)))
        db.execute(delete(ConnectionHistory).where(ConnectionHistory.mac == device.mac))
        db.execute(delete(ClientTrafficSample).where(ClientTrafficSample.mac == device.mac))
        db.execute(delete(EventLog).where(EventLog.mac == device.mac))
        db.execute(delete(DeviceAlias).where(DeviceAlias.mac == device.mac))
        db.delete(device)
    return len(stale_clients)


def process_snapshot(snapshot: RouterSnapshot) -> list[str]:
    global _last_cleanup_monotonic
    notifications: list[str] = []
    now_monotonic = time.monotonic()
    should_cleanup = now_monotonic - _last_cleanup_monotonic >= _CLEANUP_INTERVAL_SECONDS
    with db_runtime.session() as db:
        with db.begin():
            if db.get(ProcessedSnapshot, snapshot.snapshot_id) is not None:
                return []

            observed_node_ids = {item.node_id for item in snapshot.nodes}
            for item in snapshot.nodes:
                node = db.get(NetworkNode, item.node_id)
                if node is None:
                    node = NetworkNode(
                        node_id=item.node_id,
                        first_seen=snapshot.collected_at,
                    )
                    db.add(node)
                node.serial_number = item.serial_number
                node.node_type = item.node_type
                node.name = item.name
                node.model = item.model
                node.management_ip = item.management_ip
                node.parent_node_id = item.parent_node_id
                node.is_online = True
                node.last_seen = snapshot.collected_at

            if snapshot.complete:
                for node in db.scalars(select(NetworkNode).where(NetworkNode.is_online.is_(True))):
                    if node.node_id not in observed_node_ids:
                        node.is_online = False
                        node.last_offline_at = snapshot.collected_at

            observed_macs = {item.mac for item in snapshot.devices}
            for item in snapshot.devices:
                device = db.get(Device, item.mac)
                is_new = device is None
                was_online = bool(device.is_online) if device is not None else False
                old_parent = device.parent_node_id if device is not None else None
                old_parent_name = node_location_name(
                    db,
                    old_parent,
                    device.ap_name if device is not None else None,
                )
                if device is None:
                    device = Device(
                        mac=item.mac,
                        first_seen=snapshot.collected_at,
                        last_online_at=snapshot.collected_at,
                        is_online=True,
                    )
                    db.add(device)
                old_seen = device.last_seen
                old_rx = device.rx_counter_bytes or 0
                old_tx = device.tx_counter_bytes or 0

                device.ip = item.ip
                device.hostname = item.hostname
                device.ap_sn = item.ap_sn
                device.ap_name = item.ap_name
                device.parent_node_id = item.parent_node_id
                device.ssid = item.ssid
                device.rssi = item.rssi
                device.is_online = True
                device.last_seen = snapshot.collected_at

                rx_rate_kbps = item.rx_rate_kbps
                tx_rate_kbps = item.tx_rate_kbps
                if (rx_rate_kbps == 0 and tx_rate_kbps == 0) and old_seen and snapshot.collected_at > old_seen:
                    dt = (snapshot.collected_at - old_seen).total_seconds()
                    if 0.5 <= dt <= 300:
                        rx_delta = counter_delta(item.rx_counter_bytes, old_rx)
                        tx_delta = counter_delta(item.tx_counter_bytes, old_tx)
                        rx_rate_kbps = round((rx_delta / dt) / 1024.0, 1)
                        tx_rate_kbps = round((tx_delta / dt) / 1024.0, 1)

                device.rx_rate = rx_rate_kbps
                device.tx_rate = tx_rate_kbps
                device.rx_counter_bytes = item.rx_counter_bytes
                device.tx_counter_bytes = item.tx_counter_bytes
                device.usage_state = traffic_state(rx_rate_kbps, tx_rate_kbps)
                current_location_name = node_location_name(
                    db,
                    item.parent_node_id,
                    item.ap_name,
                )

                session = active_session(db, item.mac)
                if session is None:
                    session = ConnectionHistory(
                        mac=item.mac,
                        hostname=item.hostname,
                        session_start=snapshot.collected_at,
                        ap_sn=item.ap_sn,
                        ap_name=item.ap_name,
                        total_rx_bytes=float(item.rx_counter_bytes),
                        total_tx_bytes=float(item.tx_counter_bytes),
                        start_rx_counter=item.rx_counter_bytes,
                        start_tx_counter=item.tx_counter_bytes,
                        last_rx_counter=item.rx_counter_bytes,
                        last_tx_counter=item.tx_counter_bytes,
                        session_rx_bytes=0,
                        session_tx_bytes=0,
                        initial_parent_node_id=item.parent_node_id,
                        last_parent_node_id=item.parent_node_id,
                    )
                    db.add(session)
                    db.flush()
                    db.add(
                        RoamingSegment(
                            session_id=session.id,
                            parent_node_id=item.parent_node_id,
                            parent_name_snapshot=current_location_name,
                            entered_at=snapshot.collected_at,
                        )
                    )
                else:
                    session.session_rx_bytes += counter_delta(
                        item.rx_counter_bytes, session.last_rx_counter
                    )
                    session.session_tx_bytes += counter_delta(
                        item.tx_counter_bytes, session.last_tx_counter
                    )
                    session.last_rx_counter = item.rx_counter_bytes
                    session.last_tx_counter = item.tx_counter_bytes
                    session.total_rx_bytes = float(item.rx_counter_bytes)
                    session.total_tx_bytes = float(item.tx_counter_bytes)
                    session.ap_sn = item.ap_sn
                    session.ap_name = item.ap_name
                    session.last_parent_node_id = item.parent_node_id

                name = display_name(db, device)
                if is_new:
                    notifications.append(
                        event(
                            db,
                            mac=item.mac,
                            node_id=item.parent_node_id,
                            name=name,
                            ip=item.ip,
                            kind="ONLINE",
                            message=f"新设备上线：{name}（{item.ip or '无IP'}）",
                        )
                    )
                elif not was_online:
                    device.last_online_at = snapshot.collected_at
                    notifications.append(
                        event(
                            db,
                            mac=item.mac,
                            node_id=item.parent_node_id,
                            name=name,
                            ip=item.ip,
                            kind="ONLINE",
                            message=f"设备重新上线：{name}",
                        )
                    )

                if was_online and old_parent != item.parent_node_id:
                    open_segment = db.scalars(
                        select(RoamingSegment)
                        .where(
                            RoamingSegment.session_id == session.id,
                            RoamingSegment.left_at.is_(None),
                        )
                        .order_by(RoamingSegment.id.desc())
                        .limit(1)
                    ).first()
                    if open_segment:
                        open_segment.left_at = snapshot.collected_at
                    db.add(
                        RoamingSegment(
                            session_id=session.id,
                            parent_node_id=item.parent_node_id,
                            parent_name_snapshot=item.ap_name,
                            entered_at=snapshot.collected_at,
                        )
                    )
                    notifications.append(
                        event(
                            db,
                            mac=item.mac,
                            node_id=item.parent_node_id,
                            name=name,
                            ip=item.ip,
                            kind="LOCATION_CHANGE",
                            message=(
                                f"{name} 从 {old_parent_name or '未知位置'} "
                                f"切换到 {current_location_name}"
                            ),
                        )
                    )

                latest_sample = db.scalars(
                    select(ClientTrafficSample)
                    .where(ClientTrafficSample.mac == item.mac)
                    .order_by(ClientTrafficSample.sampled_at.desc())
                    .limit(1)
                ).first()
                sample_interval = 10 if device.is_starred else 60
                if (
                    latest_sample is None
                    or snapshot.collected_at - latest_sample.sampled_at
                    >= timedelta(seconds=sample_interval)
                ):
                    db.add(
                        ClientTrafficSample(
                            mac=item.mac,
                            sampled_at=snapshot.collected_at,
                            rx_rate_kbps=rx_rate_kbps,
                            tx_rate_kbps=tx_rate_kbps,
                            rx_counter_bytes=item.rx_counter_bytes,
                            tx_counter_bytes=item.tx_counter_bytes,
                            parent_node_id=item.parent_node_id,
                            rssi=item.rssi,
                        )
                    )

            if snapshot.complete:
                for device in db.scalars(select(Device).where(Device.is_online.is_(True))):
                    if device.mac in observed_macs:
                        continue
                    offline_at = device.last_seen + timedelta(
                        seconds=_OFFLINE_CONFIRMATION_SECONDS
                    )
                    if snapshot.collected_at <= offline_at:
                        continue
                    device.is_online = False
                    device.last_offline_at = offline_at
                    device.rx_rate = 0
                    device.tx_rate = 0
                    device.usage_state = "离线"
                    session = active_session(db, device.mac)
                    if session:
                        session.session_end = offline_at
                        segment = db.scalars(
                            select(RoamingSegment)
                            .where(
                                RoamingSegment.session_id == session.id,
                                RoamingSegment.left_at.is_(None),
                            )
                            .order_by(RoamingSegment.id.desc())
                            .limit(1)
                        ).first()
                        if segment:
                            segment.left_at = offline_at
                    name = display_name(db, device)
                    notifications.append(
                        event(
                            db,
                            mac=device.mac,
                            node_id=device.parent_node_id,
                            name=name,
                            ip=device.ip,
                            kind="OFFLINE",
                            message=(
                                f"确认离线：{name}（超过 1 分钟未出现在采集结果中）"
                            ),
                        )
                    )

            if should_cleanup:
                cleanup_expired_history(db, snapshot.collected_at)

            db.add(
                ProcessedSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    collected_at=snapshot.collected_at,
                    source=snapshot.source,
                    client_count=len(snapshot.devices),
                    node_count=len(snapshot.nodes),
                )
            )

    if should_cleanup:
        _last_cleanup_monotonic = now_monotonic
    db_runtime.last_successful_write_at = utcnow()
    db_runtime.last_write_snapshot_id = snapshot.snapshot_id
    return notifications
