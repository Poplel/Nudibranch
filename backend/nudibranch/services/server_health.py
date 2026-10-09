"""Settings → Server (`GET /server/health`): is the worker alive and what is each lane doing, how
big is the library, how much disk is left, and what happened in the last day and week.

The worker reports through a heartbeat file, not the database: a heartbeat row would take SQLite's
one write lock every few seconds for nothing, and the file also proves the process is alive when
every lane is stuck inside a long task. The heartbeat thread also measures folder sizes, so the API
never walks the library on a request.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nudibranch import __version__
from nudibranch.core.config import get_settings
from nudibranch.db.models import (
    Album,
    Artist,
    DownloadManifestEntry,
    Podcast,
    Episode,
    Task,
    TaskStatus,
    Track,
)
from nudibranch.services.tasks import SEARCH_LANE_TASK_TYPES

HEARTBEAT_INTERVAL_SECONDS = 10
#: Three missed beats. Past this the worker is reported as not running.
HEARTBEAT_STALE_SECONDS = 45
FOLDER_SIZE_INTERVAL_SECONDS = 15 * 60
LOG_KEEP = 3

API_STARTED_AT = datetime.now(timezone.utc)


def heartbeat_path() -> Path:
    return get_settings().config_path / ".nudibranch-worker-heartbeat.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _folders() -> dict[str, Path]:
    settings = get_settings()
    return {
        "library": settings.library_path,
        "downloads": settings.downloads_path,
        "staging": settings.staging_path,
        "import": settings.import_path,
        "podcasts": settings.podcasts_path,
        "backups": settings.backups_path,
    }


def _folder_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


class WorkerHeartbeat:
    """The worker's side. Lanes report what they are doing; one thread writes it all out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = _now_iso()
        self._lanes: dict[str, dict] = {}
        self._folder_sizes: dict[str, int] = {}
        self._folders_measured_at: str | None = None

    def lane_idle(self, lane: str) -> None:
        with self._lock:
            self._lanes[lane] = {"state": "idle", "since": _now_iso()}

    def lane_running(self, lane: str, task: Task, label: str) -> None:
        with self._lock:
            self._lanes[lane] = {
                "state": "running",
                "since": _now_iso(),
                "task_id": task.id,
                "task_type": task.type,
                "task_label": label,
            }

    def lane_scanned(self, lane: str) -> None:
        """The download lane has no tasks of its own; a finished scan is its sign of life."""
        with self._lock:
            self._lanes[lane] = {"state": "idle", "since": _now_iso(), "last_scan_at": _now_iso()}

    def start(self) -> None:
        threading.Thread(target=self._run, name="heartbeat", daemon=True).start()

    def _run(self) -> None:
        last_measure = 0.0
        while True:
            if time.monotonic() - last_measure > FOLDER_SIZE_INTERVAL_SECONDS or not last_measure:
                sizes = {name: _folder_size(path) for name, path in _folders().items()}
                with self._lock:
                    self._folder_sizes = sizes
                    self._folders_measured_at = _now_iso()
                last_measure = time.monotonic()
            self._write()
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)

    def _write(self) -> None:
        with self._lock:
            payload = {
                "version": __version__,
                "pid": os.getpid(),
                "started_at": self._started_at,
                "beat_at": _now_iso(),
                "lanes": dict(self._lanes),
                "folder_sizes": dict(self._folder_sizes),
                "folders_measured_at": self._folders_measured_at,
            }
        path = heartbeat_path()
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, path)
        except OSError:
            pass


HEARTBEAT = WorkerHeartbeat()


# --- The API's side ---------------------------------------------------------------------------


def _read_heartbeat() -> dict | None:
    try:
        return json.loads(heartbeat_path().read_text())
    except (OSError, ValueError):
        return None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite drops the zone; the server stores UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _worker(beat: dict | None) -> dict:
    if beat is None:
        return {"running": False, "version": None, "started_at": None, "beat_at": None, "lanes": []}
    beat_at = _parse(beat.get("beat_at"))
    running = beat_at is not None and datetime.now(timezone.utc) - beat_at < timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    order = {"main": 0, "download": 99}
    lanes = [
        {"name": name, **state}
        for name, state in sorted((beat.get("lanes") or {}).items(), key=lambda item: (order.get(item[0], 50), item[0]))
    ]
    return {
        "running": running,
        "version": beat.get("version"),
        "started_at": beat.get("started_at"),
        "beat_at": beat.get("beat_at"),
        "lanes": lanes,
    }


def _storage(beat: dict | None) -> dict:
    settings = get_settings()
    db = settings.db_path
    sizes = (beat or {}).get("folder_sizes") or {}
    folders = []
    for name, path in _folders().items():
        try:
            usage = shutil.disk_usage(path)
            free, total = usage.free, usage.total
        except OSError:
            free = total = None
        folders.append({"name": name, "path": str(path), "size_bytes": sizes.get(name), "free_bytes": free, "total_bytes": total})
    log = settings.log_path
    backups = sorted(
        (p for p in settings.backups_path.glob("*.sqlite") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if settings.backups_path.exists() else []
    last_backup = None
    if backups:
        newest = backups[0]
        last_backup = {
            "name": newest.name,
            "created_at": datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc).isoformat(),
            "size_bytes": _file_size(newest),
        }
    return {
        "database_bytes": sum(_file_size(Path(f"{db}{suffix}")) for suffix in ("", "-wal", "-shm")),
        "log_bytes": _file_size(log) + sum(_file_size(Path(f"{log}.{n}")) for n in range(1, LOG_KEEP + 1)),
        "folders": folders,
        "folders_measured_at": (beat or {}).get("folders_measured_at"),
        "backup_count": len(backups),
        "last_backup": last_backup,
    }


def _library(session: Session) -> dict:
    return {
        "artists": session.scalar(select(func.count(Artist.id))) or 0,
        "albums": session.scalar(select(func.count(Album.id))) or 0,
        "tracks": session.scalar(select(func.count(Track.id))) or 0,
        "lossless_tracks": session.scalar(select(func.count(Track.id)).where(Track.is_lossless.is_(True))) or 0,
        "duration_seconds": int((session.scalar(select(func.sum(Track.duration_ms))) or 0) / 1000),
        "podcasts": session.scalar(select(func.count(Podcast.id))) or 0,
        "podcast_episodes": session.scalar(select(func.count(Episode.id))) or 0,
    }


def _queue(session: Session) -> dict:
    counts = dict(session.execute(
        select(Task.status, func.count(Task.id))
        .where(Task.status.in_([TaskStatus.queued, TaskStatus.running]))
        .group_by(Task.status)
    ).all())
    oldest = _as_utc(session.scalar(select(func.min(Task.created_at)).where(Task.status == TaskStatus.queued)))
    return {
        "queued": counts.get(TaskStatus.queued, 0),
        "running": counts.get(TaskStatus.running, 0),
        "oldest_queued_at": oldest.isoformat() if oldest else None,
    }


def _activity(session: Session, hours: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    def tasks(status: TaskStatus, types: frozenset[str] | None = None) -> int:
        query = select(func.count(Task.id)).where(Task.status == status, Task.updated_at >= since)
        if types is not None:
            query = query.where(Task.type.in_(types))
        return session.scalar(query) or 0

    def downloads(status: str) -> int:
        return session.scalar(
            select(func.count(DownloadManifestEntry.id))
            .where(DownloadManifestEntry.status == status, DownloadManifestEntry.status_changed_at >= since)
        ) or 0

    return {
        "hours": hours,
        "tasks_completed": tasks(TaskStatus.completed),
        "tasks_failed": tasks(TaskStatus.failed),
        "searches": tasks(TaskStatus.completed, SEARCH_LANE_TASK_TYPES),
        "downloads_completed": downloads("completed"),
        "downloads_failed": downloads("failed"),
        "tracks_added": session.scalar(select(func.count(Track.id)).where(Track.created_at >= since)) or 0,
    }


def server_health(session: Session) -> dict:
    beat = _read_heartbeat()
    return {
        "version": __version__,
        "api_started_at": API_STARTED_AT.isoformat(),
        "worker": _worker(beat),
        "queue": _queue(session),
        "library": _library(session),
        "storage": _storage(beat),
        "activity": [_activity(session, 24), _activity(session, 24 * 7)],
    }
