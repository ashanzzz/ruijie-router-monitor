from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class CollectorStatus:
    state: str = "not_configured"
    profile_id: Optional[str] = None
    collection_source: Optional[str] = None
    last_login_at: Optional[datetime] = None
    last_snapshot_at: Optional[datetime] = None
    last_database_commit_at: Optional[datetime] = None
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    client_count: int = 0
    node_count: int = 0
    consecutive_failures: int = 0
