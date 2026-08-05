from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="ruijie-monitor-tests-"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["CONFIG_FILE"] = str(_TEST_DATA_DIR / "config.env")
os.environ["RUIJIE_PASS"] = ""
os.environ["ALLOW_SELF_RESTART"] = "false"
