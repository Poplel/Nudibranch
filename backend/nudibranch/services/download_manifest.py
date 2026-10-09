"""The worker's download manifest, kept in `download_manifest_entries`.

Entries are exchanged as plain dicts, each carrying its row `id`. Every function takes the CALLER's
session and issues Core statements on it, so a write joins the caller's transaction and a read is
never served from a stale identity map.

⚠️ Never open a second session from a caller that may hold uncommitted writes: SQLite has one write
lock, and the second connection would wait out `busy_timeout` against its own thread.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import case, delete, insert, select, update
from sqlalchemy.orm import Session

from nudibranch.db.models import DownloadManifestEntry as M

FINISHED_STATUSES = {"completed", "rejected", "rejected_removed"}
FINISHED_RETENTION = timedelta(days=7)
_COLUMN_KEYS = ("batch_id", "item_id", "parent_id", "status", "basename")
_TRANSIENT_KEYS = {"id", "_original_item_id"}
# Column selects, not the entity: a Core row is always read fresh, never from the identity map.
_COLS = (M.id, M.batch_id, M.item_id, M.parent_id, M.status, M.basename, M.data)


def remote_basename(filename: str) -> str:
    return str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _to_entry(row) -> dict:
    try:
        data = json.loads(row.data or "{}")
    except ValueError:
        data = {}
    entry = data if isinstance(data, dict) else {}
    entry.update({key: getattr(row, key) for key in _COLUMN_KEYS})
    entry["id"] = row.id
    return entry


def _split(entry: dict) -> tuple[dict, dict]:
    columns = {key: entry.get(key) for key in _COLUMN_KEYS if key in entry}
    rest = {key: value for key, value in entry.items() if key not in _COLUMN_KEYS and key not in _TRANSIENT_KEYS}
    return columns, rest


def load_download_manifest(
    session: Session,
    *,
    statuses: Iterable[str] | None = None,
    item_ids: Iterable[str] | None = None,
    batch_id: str | None = None,
) -> list[dict]:
    """Entries oldest first, optionally narrowed in SQL."""
    query = select(*_COLS)
    if statuses is not None:
        query = query.where(M.status.in_(list(statuses)))
    if item_ids is not None:
        query = query.where(M.item_id.in_(list(item_ids)))
    if batch_id is not None:
        query = query.where(M.batch_id == batch_id)
    rows = session.execute(query.order_by(M.created_at.asc(), M.id.asc())).all()
    return [_to_entry(row) for row in rows]


def manifest_entries_for_items(session: Session, item_ids: set[str]) -> list[dict]:
    return load_download_manifest(session, item_ids=item_ids) if item_ids else []


def same_manifest_entry(entry: dict, target: dict) -> bool:
    # Match on either item_id: after reconciliation the manifest holds the NEW id, but callers
    # that carry _original_item_id still need to find that entry.
    target_item_ids = {v for v in [target.get("item_id"), target.get("_original_item_id")] if v}
    return bool(
        target_item_ids
        and entry.get("batch_id") == target.get("batch_id")
        and entry.get("item_id") in target_item_ids
        and entry.get("basename")
        and entry.get("basename") == target.get("basename")
    )


def _match_row(session: Session, target: dict):
    """The stored row `target` stands for: by `id` when it has one, else batch + item + basename.

    Several rows can share the latter (a finished download and its retry), so a live row wins
    over a finished one."""
    if target.get("id"):
        return session.execute(select(*_COLS).where(M.id == target["id"])).first()
    item_ids = [v for v in [target.get("item_id"), target.get("_original_item_id")] if v]
    if not item_ids or not target.get("basename"):
        return None
    finished_last = case((M.status.in_(list(FINISHED_STATUSES)), 1), else_=0)
    return session.execute(
        select(*_COLS)
        .where(M.batch_id == target.get("batch_id"))
        .where(M.item_id.in_(item_ids))
        .where(M.basename == target["basename"])
        .order_by(finished_last, M.created_at.asc())
        .limit(1)
    ).first()


def record_download_manifest_entry(session: Session, request: dict, candidate: dict, item) -> None:
    if not request or not candidate:
        return
    filename = str(candidate.get("filename") or "")
    session.execute(
        delete(M)
        .where(M.batch_id == item.batch_id)
        .where(M.item_id == item.id)
        .where(M.status.not_in(list(FINISHED_STATUSES)))
    )
    now = datetime.now(timezone.utc).isoformat()
    session.execute(
        insert(M).values(
            id=_new_id(),
            batch_id=item.batch_id,
            item_id=item.id,
            parent_id=item.parent_id,
            status="queued",
            basename=remote_basename(filename),
            data=json.dumps(
                {
                    "request": request,
                    "candidate": {"username": candidate.get("username"), "filename": filename, "folder": candidate.get("folder")},
                    "initialized_at": now,
                    "queued_at": now,
                }
            ),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )


def _new_id() -> str:
    from nudibranch.db.models import uuid_str

    return uuid_str()


def find_download_manifest_entry(session: Session, file_path: Path, active_statuses: set[str]) -> dict | None:
    basename = file_path.name.casefold()
    rows = load_download_manifest(session, statuses=active_statuses | {"rejected"})
    for entry in rows:
        if entry.get("basename") == basename:
            return entry
    normalized_path = str(file_path).replace("\\", "/").casefold()
    for entry in rows:
        filename = str((entry.get("candidate") or {}).get("filename") or "").replace("\\", "/").casefold()
        if filename and normalized_path.endswith(filename):
            return entry
    return None


def update_download_manifest_entry(session: Session, target: dict, status: str, **fields: object) -> None:
    row = _match_row(session, target)
    if row is None:
        return
    try:
        data = json.loads(row.data or "{}")
    except ValueError:
        data = {}
    columns: dict = {"status": status}
    for key in ("item_id", "parent_id"):
        if target.get(key):
            columns[key] = target[key]
    for key in ("batch_id", "item_id", "parent_id", "basename"):
        if key in fields:
            columns[key] = fields.pop(key)
    data["status_changed_at"] = datetime.now(timezone.utc).isoformat()
    data.update(fields)
    session.execute(update(M).where(M.id == row.id).values(**columns, data=json.dumps(data)))


def remove_download_manifest_entry(session: Session, target: dict) -> None:
    if target.get("id"):
        session.execute(delete(M).where(M.id == target["id"]))
        return
    row = _match_row(session, target)
    if row is not None:
        session.execute(delete(M).where(M.id == row.id))


def remove_manifest_entries_for_items(session: Session, item_ids: set[str]) -> int:
    if not item_ids:
        return 0
    return session.execute(delete(M).where(M.item_id.in_(list(item_ids)))).rowcount or 0


def prune_finished_manifest_entries(session: Session) -> int:
    cutoff = datetime.now(timezone.utc) - FINISHED_RETENTION
    return session.execute(delete(M).where(M.status.in_(list(FINISHED_STATUSES))).where(M.updated_at < cutoff)).rowcount or 0
