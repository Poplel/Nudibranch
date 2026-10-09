"""Wishlist rows reconciled against their downloads.

These used to run on every `GET /wishlist` (and wrote on that GET), loading every download batch
with all its items to answer questions about a few dozen requests -- about a second on castiel.
They now ask SQL, scoped to the requests in question, through `ProposalItem.wishlist_item_id`
(indexed, and stamped on every row a request produces). The writes run on the worker's recovery
tick, not on a read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.db.models import (
    ItemStage,
    ProposalBatch,
    ProposalFlow,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    WishlistItem,
)
from nudibranch.services import queue_state

# `payload_json` is written with json.dumps defaults everywhere, so this matches exactly the two
# download actions (slskd and the YouTube fallback) without parsing a single payload.
_DOWNLOAD_LEAF = ProposalItem.payload_json.like('%"action": "queue_%download"%')
_RETRY_EXHAUSTED = ProposalItem.payload_json.like('%"auto_retry_exhausted": true%')


def _wishlist_ids_with(session: Session, wishlist_ids: set[str], *conditions) -> set[str]:
    if not wishlist_ids:
        return set()
    return set(
        session.scalars(
            select(ProposalItem.wishlist_item_id)
            .where(ProposalItem.wishlist_item_id.in_(wishlist_ids))
            .where(ProposalItem.kind == ProposalKind.download)
            .where(_DOWNLOAD_LEAF)
            .where(*conditions)
            .distinct()
        )
    )


def completed_wishlist_download_ids(session: Session, wishlist_ids: set[str]) -> set[str]:
    """Of `wishlist_ids`, those whose linked download leaf has completed -- i.e. downloaded and
    verified into staging. ⚠️ NOT "in the library": that needs gate (b) too, so callers must land
    this on "staged", never "completed" (see `reconcile_stale_approved_wishlist_items`).

    Only ACTUAL download leaves count (queue_download / queue_ytdlp_download): a completed container
    only means the candidate search ran.
    """
    return _wishlist_ids_with(session, wishlist_ids, ProposalItem.status == ProposalStatus.completed)


def active_wishlist_download_ids(session: Session, wishlist_ids: set[str]) -> set[str]:
    """Of `wishlist_ids`, those with a live download: keeps a request from being demoted to
    "wanted" while its download (slskd, or the YouTube fallback retry) is in flight."""
    live_batch = (
        select(ProposalBatch.id)
        .where(ProposalBatch.kind == ProposalKind.download)
        .where(ProposalBatch.flow == ProposalFlow.download_review)
        .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
    )
    unsettled = [status for status in ProposalStatus if status not in (queue_state.SETTLED_ITEM_STATUSES | {ProposalStatus.failed})]
    return _wishlist_ids_with(
        session,
        wishlist_ids,
        ProposalItem.batch_id.in_(live_batch),
        ProposalItem.status.in_(unsettled),
        ~_RETRY_EXHAUSTED,
    )


def downloading_wishlist_ids(session: Session, wishlist_ids: set[str]) -> set[str]:
    """Of `wishlist_ids`, those whose Soulseek download is executing right now."""
    if not wishlist_ids:
        return set()
    return set(
        session.scalars(
            select(ProposalItem.wishlist_item_id)
            .join(ProposalBatch, ProposalBatch.id == ProposalItem.batch_id)
            .where(ProposalItem.wishlist_item_id.in_(wishlist_ids))
            .where(ProposalItem.kind == ProposalKind.download)
            .where(ProposalItem.status == ProposalStatus.executing)
            .where(ProposalBatch.kind == ProposalKind.download)
            .where(ProposalBatch.status.in_([ProposalStatus.approved, ProposalStatus.executing]))
            .distinct()
        )
    )


def reconcile_stale_approved_wishlist_items(session: Session, user_id: str | None = None) -> None:
    """Complete any request whose download finished, and demote stale "approved" ones (abandoned
    downloads) back to "wanted". `user_id` scopes it to one requester; None covers everyone."""
    query = select(WishlistItem).where(WishlistItem.status.in_(["approved", "wanted", "review"]))
    if user_id is not None:
        query = query.where(WishlistItem.user_id == user_id)
    items = list(session.scalars(query))
    if not items:
        return
    ids = {item.id for item in items}
    active_ids = active_wishlist_download_ids(session, ids)
    # A download that finished is no longer "active", so without this branch the demotion below
    # would read it as an abandoned download and knock it back to "wanted". But a completed
    # DOWNLOAD only means the file reached staging (gate a) -- it is NOT in the library yet, so
    # this must land on "staged", never "completed": `complete_linked_wishlist_item` is the one
    # place "completed" is written, and only at the real library import. Keyed on the exact
    # wishlist_item_id carried by the download item, so it works even when
    # mark_matching_wishlist_completed missed it on fuzzy metadata (deluxe titles, feat., quotes).
    completed_ids = completed_wishlist_download_ids(session, ids)
    changed = False
    now = datetime.now(timezone.utc)
    for item in items:
        if item.id in completed_ids:
            if item.status not in {"staged", "completed"}:
                item.status = "staged"
                item.stage = ItemStage.staged.value
                item.status_changed_at = now
                changed = True
            continue
        # Only "approved" (a download was queued) demotes when its download is gone; genuine
        # "wanted"/"review" items are left untouched.
        if item.status == "approved" and item.id not in active_ids:
            item.status = "wanted"
            item.status_changed_at = now
            changed = True
    if changed:
        session.commit()


def terminal_wishlist_expired(item: WishlistItem) -> bool:
    # "rejected" deliberately never expires: a declined request stays on the requester's list until
    # they remove it or request it again (which replaces it -- see create_wishlist_item).
    if item.status not in {"completed", "removed"}:
        return False
    changed_at = item.status_changed_at or item.created_at
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    return changed_at < datetime.now(timezone.utc) - timedelta(hours=48)


def expire_old_terminal_wishlist_items(session: Session) -> int:
    expired = [
        item
        for item in session.scalars(select(WishlistItem).where(WishlistItem.status.in_(["completed", "removed"])))
        if terminal_wishlist_expired(item)
    ]
    for item in expired:
        session.delete(item)
    if expired:
        session.commit()
    return len(expired)
