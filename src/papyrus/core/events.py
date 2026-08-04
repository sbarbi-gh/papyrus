import json
import time
from pathlib import Path
from typing import Any, Iterator

from papyrus.core.constants import SCHEMA_VERSION


def write_event(kb_root: Path, event: dict[str, Any]) -> None:
    event.setdefault("schema_version", SCHEMA_VERSION)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    events_path = kb_root / "events.jsonl"
    with open(events_path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(events_path: Path) -> Iterator[dict[str, Any]]:
    if not events_path.exists():
        return
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
