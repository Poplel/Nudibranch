import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nudibranch.core.config import get_settings


# Past this size the log is rolled: nudibranch.log -> .1 -> .2 -> .3 (the oldest is dropped).
LOG_ROTATE_BYTES = 20 * 1024 * 1024
LOG_KEEP_ROLLED = 3


def _rotate_if_needed(path: Path) -> None:
    """Roll the log once it passes LOG_ROTATE_BYTES.

    The api, the worker and its threads all append here, so the size check and the renames happen
    under an flock on a sidecar file (flock is per open file, so it also excludes threads). The size
    is re-checked inside the lock: whoever loses the race finds the work already done.
    """
    try:
        if path.stat().st_size < LOG_ROTATE_BYTES:
            return
    except OSError:
        return
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.stat().st_size < LOG_ROTATE_BYTES:
                return
            for index in range(LOG_KEEP_ROLLED, 1, -1):
                older = path.with_name(f"{path.name}.{index - 1}")
                if older.exists():
                    older.replace(path.with_name(f"{path.name}.{index}"))
            path.replace(path.with_name(path.name + ".1"))
        except OSError:
            pass  # a failed roll must never lose the line being written
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def write_app_log(message: str, level: str = "info", **context: Any) -> None:
    settings = get_settings()
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    clean_context = {key: value for key, value in context.items() if value is not None}
    if clean_context:
        entry["context"] = clean_context
    line = json.dumps(entry, sort_keys=True)
    print(line, flush=True)
    _rotate_if_needed(settings.log_path)
    with settings.log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Read only the last `limit` lines, seeking backwards from EOF in blocks —
    read_text() loaded the entire log file on every /logs poll."""
    block_size = 65536
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        remaining = handle.tell()
        data = b""
        while remaining > 0 and data.count(b"\n") <= limit:
            read_size = min(block_size, remaining)
            remaining -= read_size
            handle.seek(remaining)
            data = handle.read(read_size) + data
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-limit:]]


def tail_app_log(limit: int = 500) -> list[dict[str, Any]]:
    path = get_settings().log_path
    lines = _tail_lines(path, limit) if path.exists() else []
    if len(lines) < limit:
        # Just rolled: the newest history is at the end of the previous file.
        rolled = path.with_name(path.name + ".1")
        if rolled.exists():
            lines = _tail_lines(rolled, limit - len(lines)) + lines
    if not lines:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "level": "info",
                "message": line,
            }
        if isinstance(payload, dict):
            entries.append(payload)
    return entries
