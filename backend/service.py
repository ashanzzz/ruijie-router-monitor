from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select

from backend.collector.models import DeviceObservation, RouterSnapshot
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


import time
_last_cleanup = 0.0

def _cleanup_old_data_if_needed(db) -> None:
    from backend.config import settings
    from datetime import timezone
    global _last_cleanup
    now_ts = time.time()
    if now_ts - _last_cleanup < 3600:
        return
    _last_cleanup = now_ts

    from sqlalchemy import delete
    now = datetime.now(timezone.utc)
    cutoff_normal = now - timedelta(days=settings.retention_days_normal)
    cutoff_starred = now - timedelta(days=settings.retention_days_starred)
    
    starred_macs = list(db.scalars(select(Device.mac).where(Device.is_starred.is_(True))).all())
    
    for model, time_col in [
        (ClientTrafficSample, ClientTrafficSample.sampled_at),
        (ConnectionHistory, ConnectionHistory.session_start),
        (EventLog, EventLog.timestamp),
    ]:
        if starred_macs:
            db.execute(delete(model).where(model.mac.notin_(starred_macs), time_col < cutoff_normal))
            db.execute(delete(model).where(model.mac.in_(starred_macs), time_col < cutoff_starred))
        else:
            db.execute(delete(model).where(time_col < cutoff_normal))

    db.execute(delete(RoamingSegment).where(
        RoamingSegment.session_id.notin_(select(ConnectionHistory.id))
    ))


def process_snapshot(snapshot: RouterSnapshot) -> list[str]:
    notifications: list[str] = []
    with db_runtime.session() as db:
        with db.begin():
            _cleanup_old_data_if_needed(db)
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
                if device is None:
                    device = Device(
                        mac=item.mac,
                        first_seen=snapshot.collected_at,
                        last_online_at=snapshot.collected_at,
                        is_online=True,
                    )
                    db.add(device)
                device.ip = item.ip
                device.hostname = item.hostname
                device.ap_sn = item.ap_sn
                device.ap_name = item.ap_name
                device.parent_node_id = item.parent_node_id
                device.ssid = item.ssid
                device.is_online = True
                device.last_seen = snapshot.collected_at
                device.rx_rate = item.rx_rate_kbps
                device.tx_rate = item.tx_rate_kbps
                device.rx_counter_bytes = item.rx_counter_bytes
                device.tx_counter_bytes = item.tx_counter_bytes
                device.usage_state = item.usage_state

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
                            parent_name_snapshot=item.ap_name,
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
                            message=f"{name} 切换到 {item.ap_name or '未知上级设备'}",
                        )
                    )

                latest_sample = db.scalars(
                    select(ClientTrafficSample)
                    .where(ClientTrafficSample.mac == item.mac)
                    .order_by(ClientTrafficSample.sampled_at.desc())
                    .limit(1)
                ).first()
                if (
                    latest_sample is None
                    or snapshot.collected_at - latest_sample.sampled_at >= timedelta(seconds=60)
                ):
                    db.add(
                        ClientTrafficSample(
                            mac=item.mac,
                            sampled_at=snapshot.collected_at,
                            rx_rate_kbps=item.rx_rate_kbps,
                            tx_rate_kbps=item.tx_rate_kbps,
                            rx_counter_bytes=item.rx_counter_bytes,
                            tx_counter_bytes=item.tx_counter_bytes,
                            parent_node_id=item.parent_node_id,
                        )
                    )

            if snapshot.complete:
                for device in db.scalars(select(Device).where(Device.is_online.is_(True))):
                    if device.mac in observed_macs:
                        continue
                    device.is_online = False
                    device.last_offline_at = snapshot.collected_at
                    device.rx_rate = 0
                    device.tx_rate = 0
                    device.usage_state = "离线"
                    session = active_session(db, device.mac)
                    if session:
                        session.session_end = snapshot.collected_at
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
                            segment.left_at = snapshot.collected_at
                    name = display_name(db, device)
                    notifications.append(
                        event(
                            db,
                            mac=device.mac,
                            node_id=device.parent_node_id,
                            name=name,
                            ip=device.ip,
                            kind="OFFLINE",
                            message=f"设备下线：{name}",
                        )
                    )

            db.add(
                ProcessedSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    collected_at=snapshot.collected_at,
                    source=snapshot.source,
                    client_count=len(snapshot.devices),
                    node_count=len(snapshot.nodes),
                )
            )

    db_runtime.last_successful_write_at = datetime.utcnow()
    db_runtime.last_write_snapshot_id = snapshot.snapshot_id
    return notifications
