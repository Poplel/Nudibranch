import json
from datetime import datetime, timezone
from typing import Any

from nudibranch.core.config import get_settings


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
    with settings.log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


def tail_app_log(limit: int = 500) -> list[dict[str, Any]]:
    path = get_settings().log_path
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
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
