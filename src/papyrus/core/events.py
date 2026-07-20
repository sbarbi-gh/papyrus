import json
import time
from pathlib import Path
from typing import Any

from papyrus.core.constants import SCHEMA_VERSION


def write_event(kb_root: Path, event: dict[str, Any]) -> None:
    event.setdefault("schema_version", SCHEMA_VERSION)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    events_path = kb_root / "events.jsonl"
    with open(events_path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
