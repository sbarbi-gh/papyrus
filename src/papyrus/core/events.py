import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from papyrus.core.constants import SCHEMA_VERSION


def write_event(kb_root: Path, event: dict[str, Any]) -> None:
    """Append one event to events.jsonl under an exclusive flock.

    Each writer holds the lock for a single os.write of the whole line, so
    concurrent processes (parallel embed/ingest/update) cannot interleave
    partial lines into malformed JSON (issue #3-E).
    """
    event.setdefault("schema_version", SCHEMA_VERSION)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    events_path = kb_root / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_events(
    events_path: Path,
    on_error: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield events from events.jsonl in file order.

    Malformed lines are never silently dropped: when on_error is provided it
    is called with the offending line so callers can surface a warning
    (issue #3-E).
    """
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
                if on_error is not None:
                    on_error(line)
