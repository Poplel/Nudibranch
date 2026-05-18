import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import ProposalBatch, ProposalItem, ProposalKind, ProposalStatus, Task, WishlistItem
from nudibranch.services.notifications import create_notification
from nudibranch.services.tasks import enqueue_task


def list_batches(session: Session) -> list[ProposalBatch]:
    return list(session.scalars(select(ProposalBatch).order_by(ProposalBatch.created_at.desc())))


def set_selection(session: Session, batch_id: str, item_ids: list[str], selected: bool) -> int:
    items = list(
        session.scalars(
            select(ProposalItem).where(ProposalItem.batch_id == batch_id, ProposalItem.id.in_(item_ids))
        )
    )
    for item in items:
        item.selected = selected
    session.commit()
    return len(items)


def approve_batch(session: Session, batch_id: str, item_ids: list[str] | None = None) -> Task:
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    batch.status = ProposalStatus.approved
    normalize_download_candidate_selection(batch.items)
    for item in batch.items:
        if item_ids is not None and item.id not in item_ids:
            continue
        if item.selected and item.status in {ProposalStatus.pending, ProposalStatus.failed}:
            item.status = ProposalStatus.approved
    session.commit()
    return enqueue_task(session, "execute_proposal_batch", {"batch_id": batch_id})


def normalize_download_candidate_selection(items: list[ProposalItem]) -> None:
    candidates_by_parent: dict[str, list[ProposalItem]] = {}
    for item in items:
        if item.kind != "download" or not item.parent_id:
            continue
        payload = json.loads(item.payload_json or "{}")
        if payload.get("action") not in {"queue_download", "queue_ytdlp_download"}:
            continue
        candidates_by_parent.setdefault(item.parent_id, []).append(item)
    for candidates in candidates_by_parent.values():
        selected = [item for item in candidates if item.selected]
        if len(selected) <= 1:
            continue
        for item in selected[1:]:
            item.selected = False


def reject_items(session: Session, batch_id: str, item_ids: list[str] | None, suppress_for: str) -> int:
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")

    if item_ids:
        items = rejected_items_with_descendants(batch.items, set(item_ids))
    else:
        items = list(batch.items)
    rejected_ids = {item.id for item in items}
    rejected_wishlist_items: dict[str, list[str]] = {}
    removed_download_files = remove_rejected_download_files(items)
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        request_payload = payload.get("request") or {}
        wishlist_item_id = payload.get("wishlist_item_id") or request_payload.get("wishlist_item_id")
        user_id = payload.get("user_id")
        if wishlist_item_id and user_id:
            rejected_wishlist_items.setdefault(user_id, []).append(str(item.title))
            wishlist_item = session.get(WishlistItem, wishlist_item_id)
            if wishlist_item:
                wishlist_item.status = "rejected"
                wishlist_item.status_changed_at = datetime.now(timezone.utc)
        session.delete(item)
    session.flush()

    if batch:
        session.expire(batch, ["items"])
        cleanup_empty_container_items(session, batch)
        session.expire(batch, ["items"])
        if not batch.items:
            batch.status = ProposalStatus.rejected
    session.commit()
    if removed_download_files:
        create_notification(
            session,
            title="Downloaded files removed",
            body=f"{removed_download_files} rejected files were removed from downloads.",
            event_type="tool_completed",
            target_url="/downloads",
        )
    for user_id, titles in rejected_wishlist_items.items():
        shown = ", ".join(titles[:5])
        extra = "" if len(titles) <= 5 else f" and {len(titles) - 5} more"
        create_notification(
            session,
            title="Wishlist request denied",
            body=f"{shown}{extra}",
            event_type="wishlist_denied",
            target_url="/wishlist",
            user_id=user_id,
        )
    return len(rejected_ids)


def rejected_items_with_descendants(items: list[ProposalItem], rejected_ids: set[str]) -> list[ProposalItem]:
    children_by_parent: dict[str, list[ProposalItem]] = {}
    item_by_id = {item.id: item for item in items}
    for item in items:
        if item.parent_id:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    expanded_ids = set(rejected_ids)
    stack = list(rejected_ids)
    while stack:
        current_id = stack.pop()
        for child in children_by_parent.get(current_id, []):
            if child.id in expanded_ids:
                continue
            expanded_ids.add(child.id)
            stack.append(child.id)
    return [item for item_id, item in item_by_id.items() if item_id in expanded_ids]


def remove_rejected_download_files(items: list[ProposalItem]) -> int:
    settings = get_settings()
    downloads_root = settings.downloads_path.resolve()
    removed = 0
    seen_paths: set[Path] = set()
    for item in items:
        if item.kind != ProposalKind.import_files or not item.old_value:
            continue
        file_path = Path(item.old_value).resolve()
        if file_path in seen_paths:
            continue
        if downloads_root not in [file_path, *file_path.parents]:
            continue
        seen_paths.add(file_path)
        if not file_path.is_file():
            continue
        file_path.unlink()
        prune_empty_download_dirs(file_path.parent, downloads_root)
        removed += 1
    return removed


def prune_empty_download_dirs(path: Path, stop_at: Path) -> None:
    current = path
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def cleanup_empty_container_items(session: Session, batch: ProposalBatch) -> None:
    changed = True
    while changed:
        changed = False
        items = list(batch.items)
        child_parent_ids = {item.parent_id for item in items if item.parent_id}
        for item in items:
            if item.id in child_parent_ids:
                continue
            if item.payload_json and '"action"' in item.payload_json:
                continue
            session.delete(item)
            changed = True
        if changed:
            session.flush()
            session.expire(batch, ["items"])
