from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a naive UTC datetime for the existing database schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
