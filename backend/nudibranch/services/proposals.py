import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.db.models import ProposalBatch, ProposalItem, ProposalStatus, Task, WishlistItem
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
    query = select(ProposalItem).where(ProposalItem.batch_id == batch_id)
    if item_ids:
        query = query.where(ProposalItem.id.in_(item_ids))

    items = list(session.scalars(query))
    rejected_ids = {item.id for item in items}
    rejected_wishlist_items: dict[str, list[str]] = {}
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        wishlist_item_id = payload.get("wishlist_item_id")
        user_id = payload.get("user_id")
        if wishlist_item_id and user_id:
            rejected_wishlist_items.setdefault(user_id, []).append(str(item.title))
            wishlist_item = session.get(WishlistItem, wishlist_item_id)
            if wishlist_item:
                wishlist_item.status = "removed"
        session.delete(item)
    session.flush()

    batch = session.get(ProposalBatch, batch_id)
    if batch:
        session.expire(batch, ["items"])
        cleanup_empty_container_items(session, batch)
        session.expire(batch, ["items"])
        if not batch.items:
            batch.status = ProposalStatus.rejected
    session.commit()
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
