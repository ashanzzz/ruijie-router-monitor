from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import ClientTrafficSample, Device
from backend.time_utils import utcnow


@dataclass(frozen=True)
class RangeSpec:
    duration: timedelta
    bucket_seconds: int


TRAFFIC_RANGES: dict[str, RangeSpec] = {
    "1m": RangeSpec(timedelta(minutes=1), 5),
    "10m": RangeSpec(timedelta(minutes=10), 10),
    "1h": RangeSpec(timedelta(hours=1), 30),
    "2h": RangeSpec(timedelta(hours=2), 60),
    "24h": RangeSpec(timedelta(hours=24), 300),
    "7d": RangeSpec(timedelta(days=7), 1800),
    "30d": RangeSpec(timedelta(days=30), 7200),
}


def traffic_analytics(
    db: Session,
    mac: str,
    range_key: str,
    *,
    normal_retention_days: int,
    starred_retention_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    spec = TRAFFIC_RANGES[range_key]
    mac = mac.upper()
    device = db.get(Device, mac)
    if device is None:
        raise LookupError("客户端不存在")

    rows = list(
        db.scalars(
            select(ClientTrafficSample)
            .where(
                ClientTrafficSample.mac == mac,
                ClientTrafficSample.sampled_at >= now - spec.duration,
            )
            .order_by(ClientTrafficSample.sampled_at)
        )
    )
    interval_seconds = 10 if device.is_starred else 60
    retention_days = starred_retention_days if device.is_starred else normal_retention_days
    return {
        "range": range_key,
        "bucket_seconds": spec.bucket_seconds,
        "sample_interval_seconds": interval_seconds,
        "retention_days": retention_days,
        "is_starred": bool(device.is_starred),
        "available_from": _iso(rows[0].sampled_at) if rows else None,
        "points": _aggregate_samples(rows, spec.bucket_seconds),
        "summary": _traffic_summary(rows, spec.duration, interval_seconds),
    }


def _aggregate_samples(rows: list[ClientTrafficSample], bucket_seconds: int) -> list[dict[str, Any]]:
    buckets: dict[int, list[ClientTrafficSample]] = defaultdict(list)
    for row in rows:
        stamp = int(row.sampled_at.replace(tzinfo=timezone.utc).timestamp())
        buckets[stamp - stamp % bucket_seconds].append(row)

    points: list[dict[str, Any]] = []
    for stamp, items in sorted(buckets.items()):
        rssi_values = [item.rssi for item in items if item.rssi is not None]
        points.append(
            {
                "sampled_at": _iso(
                    datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
                ),
                "rx_rate_kbps": round(sum(x.rx_rate_kbps for x in items) / len(items), 2),
                "tx_rate_kbps": round(sum(x.tx_rate_kbps for x in items) / len(items), 2),
                "peak_rx_kbps": round(max(x.rx_rate_kbps for x in items), 2),
                "peak_tx_kbps": round(max(x.tx_rate_kbps for x in items), 2),
                "rssi": round(sum(rssi_values) / len(rssi_values)) if rssi_values else None,
            }
        )
    return points


def _traffic_summary(
    rows: list[ClientTrafficSample],
    duration: timedelta,
    interval_seconds: int,
) -> dict[str, Any]:
    rssi_values = [item.rssi for item in rows if item.rssi is not None]
    rx_values = [item.rx_rate_kbps for item in rows]
    tx_values = [item.tx_rate_kbps for item in rows]
    if rows:
        observed_seconds = min(
            duration.total_seconds(),
            max(
                interval_seconds,
                (rows[-1].sampled_at - rows[0].sampled_at).total_seconds()
                + interval_seconds,
            ),
        )
    else:
        observed_seconds = duration.total_seconds()
    expected = max(1, int(observed_seconds / interval_seconds))
    return {
        "average_rx_kbps": round(sum(rx_values) / len(rx_values), 2) if rx_values else 0,
        "average_tx_kbps": round(sum(tx_values) / len(tx_values), 2) if tx_values else 0,
        "peak_rx_kbps": round(max(rx_values), 2) if rx_values else 0,
        "peak_tx_kbps": round(max(tx_values), 2) if tx_values else 0,
        "average_rssi": round(sum(rssi_values) / len(rssi_values)) if rssi_values else None,
        "weakest_rssi": min(rssi_values) if rssi_values else None,
        "coverage_percent": round(min(1, len(rows) / expected) * 100, 1),
        "sample_count": len(rows),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None
