import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import (
    Album,
    Artist,
    ItemStage,
    Permission,
    TaskStatus,
    ProposalBatch,
    ProposalFlow,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    Task,
    Track,
    User,
    WishlistItem,
)
from nudibranch.services import queue_state
from nudibranch.services.download_manifest import load_download_manifest, update_download_manifest_entry
from nudibranch.services.notifications import create_notification, retire_queue_notifications
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
    if selected and items:
        # Round 4 #4a: this is the one place in the codebase that flips `.selected` without also
        # enforcing "at most one selected candidate per track" -- `approve_batch` and `retry_items`
        # both already keep that invariant themselves. Left unreconciled, picking an alternate
        # candidate here (without the caller separately deselecting the old one) left a track with
        # two independently "expected" selected candidates, each running its own download/retry
        # lifecycle in parallel (confirmed live: batch 83a8df73, track "Only").
        batch = session.get(ProposalBatch, batch_id)
        if batch:
            normalize_download_candidate_selection(batch.items, set(item_ids))
    session.commit()
    return len(items)


class ApprovalNotPermitted(PermissionError):
    """Raised when the actor may not approve this particular batch."""


def assert_may_approve(batch: ProposalBatch, actor: User | None) -> None:
    """The authoritative approval rule, deliberately in the service rather than the route.

    Approving is the act that starts bytes moving, and `approve_batch` is the ONLY path to it --
    so the check lives here, where a future route cannot forget it.  Two ways in:

    * `approvals:manage` (or admin) -- may approve anything, including their own requests.  An
      admin approving music they asked for is the normal single-user flow, not an escalation.
    * `wishlist:approve_all` -- may approve OTHER people's music requests, on the download gate
      only, and explicitly NOT a batch whose requests are all their own.  That last clause is the
      anti-self-approval rule: a `discover`-only user must never be able to make their own download
      proceed, and someone who can approve for others must not use it to wave through their own.
    """
    if actor is None:
        return  # internal/worker call: no actor to check against
    if actor.is_admin:
        return
    held = {user_permission.permission for user_permission in actor.permissions}
    if Permission.approvals_manage in held:
        return
    if Permission.wishlist_approve_all not in held:
        raise ApprovalNotPermitted("Requires approvals:manage")
    flow = batch.flow if isinstance(batch.flow, ProposalFlow) else ProposalFlow.library_change
    if flow is not ProposalFlow.download_review:
        raise ApprovalNotPermitted("wishlist:approve_all only covers download requests")
    requesters = {item.requester_id for item in batch.items if item.requester_id}
    if requesters and requesters == {actor.id}:
        raise ApprovalNotPermitted("You cannot approve your own request")


class NothingToApprove(ValueError):
    """Raised when an approve call resolves to no approvable work.

    Its own class because the honest answer is a 409, not a 404: the batch exists, the ids exist,
    and nothing about them can be approved. Approve used to accept that silently -- it marked the
    batch approved and enqueued an execute that did nothing -- which is precisely what "the Approve
    button did nothing" looks like from the outside.
    """


def approve_batch(
    session: Session,
    batch_id: str,
    item_ids: list[str] | None = None,
    actor: User | None = None,
) -> Task:
    """Approve a batch, or exactly the named items within it.

    Naming an id SELECTS it: a client that hands over a candidate the user picked no longer has to
    call `/selection` first, and picking an alternate candidate wins its sibling picker here rather
    than being silently skipped for not being the pre-selected one. Naming a container means its
    whole subtree; naming leaves means exactly those leaves.

    ⚠️ A failed download in the selection is RETRIED, not re-approved (the user's call,
    2026-09-28). Re-approving re-queued the candidate that had just failed, never moved on to an
    untried one, and -- because naming an id selects it -- could hand a track a second live row
    beside one still downloading. `retry_items` owns all of that, including one download per track.
    """
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    assert_may_approve(batch, actor)
    known_ids = {item.id for item in batch.items}
    preferred_ids = set(item_ids or [])
    if item_ids is not None:
        if not preferred_ids & known_ids:
            raise ValueError("None of those items are in this batch")
        preferred_ids &= known_ids
    approved_ids = item_ids_with_descendants(batch.items, preferred_ids) if item_ids is not None else None
    failed_download_ids = {
        item.id
        for item in batch.items
        if (approved_ids is None or item.id in approved_ids)
        and item.status is ProposalStatus.failed
        and json.loads(item.payload_json or "{}").get("action") in {"queue_download", "queue_ytdlp_download"}
    }
    retried, retry_tasks = _retry_items(session, batch_id, sorted(failed_download_ids)) if failed_download_ids else ([], [])
    preferred_ids -= failed_download_ids
    if approved_ids is not None:
        approved_ids -= failed_download_ids
    if approved_ids is not None:
        # An explicitly named leaf is being asked for, so make it the selection rather than
        # requiring the caller to have set it beforehand. Containers are left alone: selecting a
        # whole subtree is what `item_ids_with_descendants` already expresses.
        for item in batch.items:
            if item.id in preferred_ids and not item.children:
                item.selected = True
    normalize_download_candidate_selection(batch.items, preferred_ids)
    approved = 0
    for item in batch.items:
        if (approved_ids is not None and item.id not in approved_ids) or item.id in failed_download_ids:
            continue
        # A row a research retry is still searching for is not a choice yet (see `_retry_items`).
        if item.status is ProposalStatus.pending and item.stage == ItemStage.searching.value:
            continue
        # ⚠️ `canceled` is deliberately NOT in this set. Approving a batch must not resurrect a
        # track the user explicitly stopped -- that is what makes a cancel stick.
        if item.selected and item.status in {ProposalStatus.pending, ProposalStatus.failed}:
            item.status = ProposalStatus.approved
            approved += 1
            # The requester's row leaves "Awaiting approval" the moment someone says yes. The
            # worker takes it from here as the download moves (`mirror_download_stage_to_wishlist`).
            if item.wishlist_item_id:
                wishlist_item = session.get(WishlistItem, item.wishlist_item_id)
                if wishlist_item and wishlist_item.status in {"review", "searching", "wanted"}:
                    wishlist_item.status = "approved"
                    wishlist_item.stage = ItemStage.approved.value
                    wishlist_item.status_changed_at = datetime.now(timezone.utc)
    if not approved:
        if retried:
            # `_retry_items` has already committed its own work; nothing else here changed.
            session.commit()
            return retry_tasks[-1]
        session.rollback()
        raise NothingToApprove("Nothing in this selection can be approved")
    batch.status = ProposalStatus.approved
    # The ask is answered; the "needs approval" row must not sit unread (or still be pushed).
    retire_queue_notifications(session, batch_id)
    session.commit()
    return enqueue_task(session, "execute_proposal_batch", {"batch_id": batch_id})


def normalize_download_candidate_selection(items: list[ProposalItem], preferred_ids: set[str] | None = None) -> None:
    preferred_ids = preferred_ids or set()
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
        selected.sort(key=lambda item: (item.id not in preferred_ids, download_candidate_rank(item), item.status == ProposalStatus.executing, item.id))
        for item in selected[1:]:
            item.selected = False


def download_candidate_rank(item: ProposalItem) -> int:
    payload = json.loads(item.payload_json or "{}")
    try:
        return int(payload.get("candidate_index", 9999))
    except (TypeError, ValueError):
        return 9999


def reject_items(
    session: Session,
    batch_id: str,
    item_ids: list[str] | None,
    expand_descendants: bool = True,
    actor_id: str | None = None,
    decline_requests: bool = True,
) -> tuple[int, dict[str, list[str]]]:
    """Delete rows for good, and decline any request left with nothing. Returns (rows removed,
    per-owner requests declined) -- the caller decides whether to tell them."""
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")

    if item_ids:
        items = (
            rejected_items_with_descendants(batch.items, set(item_ids))
            if expand_descendants
            else [item for item in batch.items if item.id in set(item_ids)]
        )
    else:
        items = list(batch.items)
    rejected_ids = {item.id for item in items}
    # Prefer the real columns; fall back to the payload for rows predating them.
    # ⚠️ The old condition required BOTH ids from the payload, and the gate-(b) review items
    # carried neither — so rejecting a staged import silently left the wishlist row reading
    # "completed" for a file that was just thrown away.
    wishlist_item_ids: set[str] = set()
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        request_payload = payload.get("request") or {}
        wishlist_item_id = (
            item.wishlist_item_id or payload.get("wishlist_item_id") or request_payload.get("wishlist_item_id")
        )
        if wishlist_item_id:
            wishlist_item_ids.add(wishlist_item_id)
    removed_download_files = remove_rejected_download_files(items) + remove_rejected_manifest_downloads(session, items)
    for item in items:
        session.delete(item)
    session.flush()
    declined = _decline_requests_left_empty(session, wishlist_item_ids, set(), actor_id) if decline_requests else {}

    if batch:
        session.expire(batch, ["items"])
        cleanup_empty_container_items(session, batch)
        session.expire(batch, ["items"])
        if not batch.items:
            batch.status = ProposalStatus.rejected
        # Only once nothing here still waits on a decision: rejecting part of a batch leaves the
        # rest of the ask standing, and its row must stay unread.
        if not any(item.status == ProposalStatus.pending for item in batch.items):
            retire_queue_notifications(session, batch_id)
    session.commit()
    if removed_download_files:
        create_notification(
            session,
            title="Downloaded files removed",
            body=f"{removed_download_files} rejected files were removed from downloads.",
            event_type="tool_completed",
            target_url="/task-queue?bucket=issues",
        )
    return len(rejected_ids), declined


def _decline_requests_left_empty(
    session: Session, wishlist_item_ids: set[str], leaving_ids: set[str], actor_id: str | None
) -> dict[str, list[str]]:
    """Decline each request that this removal leaves with no work at all; returns, per owner, the
    requests to tell them about (never the actor's own).

    ⚠️ Removing a ROW is not declining the REQUEST. Removing one wrong candidate used to decline the
    whole request -- the requester was told "Request declined" and the row read Declined while four
    good candidates sat beside the removed one. A request is declined only when nothing of it is
    left: no row outside `leaving_ids` (rows this same removal is taking) that is not already
    rejected or canceled. A completed IMPORT row counts as left -- that part is in the library. A
    completed DOWNLOAD row does not: its file was handed to an "Add to library" row, and removing
    that one is what leaves the request with nothing (seen live: requests kept reading "staged"
    after their staged files were removed).
    """
    now = datetime.now(timezone.utc)
    declined: set[str] = set()
    names_by_owner: dict[str, list[str]] = {}
    for wishlist_item_id in wishlist_item_ids:
        wishlist_item = session.get(WishlistItem, wishlist_item_id)
        if not wishlist_item or wishlist_item.status in {"rejected", "removed", "completed"}:
            continue
        remaining = session.scalar(
            select(ProposalItem.id)
            .where(ProposalItem.wishlist_item_id == wishlist_item_id)
            .where(ProposalItem.id.not_in(leaving_ids) if leaving_ids else True)
            .where(ProposalItem.status.not_in([ProposalStatus.rejected, ProposalStatus.canceled]))
            .where(~((ProposalItem.kind == ProposalKind.download) & (ProposalItem.status == ProposalStatus.completed)))
            .limit(1)
        )
        if remaining:
            continue
        wishlist_item.status = "rejected"
        wishlist_item.stage = "rejected"
        wishlist_item.status_changed_at = now
        declined.add(wishlist_item.id)
        if wishlist_item.user_id and wishlist_item.user_id != actor_id:
            name = " – ".join(part for part in (wishlist_item.artist, wishlist_item.track or wishlist_item.album) if part)
            names = names_by_owner.setdefault(wishlist_item.user_id, [])
            if name not in names:
                names.append(name)
    # A declined request must not keep searching for itself behind the decision.
    stop_wishlist_search_tasks(session, declined)
    session.flush()
    return names_by_owner


def decline_linked_wishlist_items(
    session: Session, items: list[ProposalItem], leaving_ids: set[str], actor_id: str | None = None
) -> dict[str, list[str]]:
    """Decline the requests behind these live rows that the removal leaves with nothing, before the
    rows themselves are cancelled (they are deleted later, by the worker).

    ⚠️ Load-bearing for "remove implies cancel": the worker's `reset_canceled_wishlist_items` sends
    an emptied request back to `requested` (gate 1), which is wrong for a removal -- a removed
    request should stay declined, not reappear waiting for re-approval. It skips rows that are
    already `rejected`/`removed`, so declining them BEFORE the removal-cancel step is what stops a
    removed request coming back to life a second later.
    """
    return _decline_requests_left_empty(
        session, {item.wishlist_item_id for item in items if item.wishlist_item_id}, leaving_ids, actor_id
    )


def notify_declined_requests(session: Session, names_by_owner: dict[str, list[str]]) -> None:
    """One "Request declined" row per requester -- the same row `reject_items` sends."""
    for user_id, names in names_by_owner.items():
        shown = ", ".join(names[:5])
        extra = "" if len(names) <= 5 else f" and {len(names) - 5} more"
        create_notification(
            session,
            title="Request declined",
            body=f"{shown}{extra}",
            event_type="wishlist_denied",
            target_url="/wishlist",
            user_id=user_id,
            group_key=f"wishlist-decision:wishlist_denied:{user_id}",
        )


def _is_live_download_leaf(item: ProposalItem) -> bool:
    """A row with real work behind it: a transfer to stop, or a staged file to delete.

    ⚠️ A `pending` candidate is not one: it was never approved, so there is no transfer and no file,
    and it is simply deleted. Treating it as live sent it down the cancel-then-delete path, whose
    "nothing live left" check then settled -- and the worker deleted -- the whole batch, taking
    every other candidate for the track with it.
    """
    if item.status in {ProposalStatus.pending, ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}:
        return False
    payload = json.loads(item.payload_json or "{}")
    if payload.get("action") in {"queue_download", "queue_ytdlp_download"}:
        return True
    return item.kind == ProposalKind.import_files and bool(item.old_value)


def remove_items(
    session: Session,
    item_ids: list[str],
    actor_id: str | None = None,
    notify_declined: bool = False,
    decline_requests: bool = True,
) -> tuple[int, int, set[str]]:
    """Remove an arbitrary set of rows, from any batches. Returns (canceled, removed, batch_ids).

    **Removal always cancels first** (the user's rule, 2026-09-22): a row with a live transfer
    behind it is stopped, its partial file deleted and only then is the row gone, so a client can
    never remove something that keeps running. Split in two because the two halves delete in
    different places -- the worker owns the cancelled rows (it needs them to find the transfer and
    the manifest entry), and `reject_items` owns the rest.

    Idempotent: ids that no longer exist, or that are already settled, cost nothing.
    """
    by_batch: dict[str, set[str]] = {}
    for item_id in dict.fromkeys(item_ids):
        item = session.get(ProposalItem, item_id)
        if item:
            by_batch.setdefault(item.batch_id, set()).add(item.id)
    canceled_total = 0
    removed_total = 0
    declined_by_owner: dict[str, list[str]] = {}
    # Everything this call takes, across batches -- a request is only declined if none of it is left.
    leaving: set[str] = set()
    for batch_id, ids in by_batch.items():
        batch = session.get(ProposalBatch, batch_id)
        if batch:
            leaving |= item_ids_with_descendants(batch.items, ids)
    for batch_id, ids in by_batch.items():
        batch = session.get(ProposalBatch, batch_id)
        if not batch:
            continue
        wanted = item_ids_with_descendants(batch.items, ids)
        targets = [item for item in batch.items if item.id in wanted]
        live = [item for item in targets if _is_live_download_leaf(item)]
        live_ids = {item.id for item in live}
        # A row already cancelled belongs to the worker until it has stopped the transfer and
        # deleted it — pressing Remove again (or a stale bulk selection) must not snatch it away
        # and leave the transfer running with nothing pointing at it.
        live_ids |= {item.id for item in targets if item.status is ProposalStatus.canceled}
        if live and decline_requests:
            for owner_id, names in decline_linked_wishlist_items(session, live, leaving, actor_id).items():
                declined_by_owner.setdefault(owner_id, []).extend(n for n in names if n not in declined_by_owner.get(owner_id, []))
            # ⚠️ NOT `cancel_items` (2026-09-23): that function now hands a stopped download BACK
            # to gate (a), keeping the row -- exactly wrong for a Remove, which must delete it for
            # good. `_mark_items_canceled_for_removal` is the old terminal-`canceled` marking that
            # `run_cancel_download_item(delete_rows=True)` deletes once the transfer is stopped.
            canceled_total += len(_mark_items_canceled_for_removal(session, batch_id, sorted(live_ids), actor_id))
        session.expire(batch, ["items"])
        # ⚠️ Everything the cancel is still working on is left alone -- the row itself AND its
        # ancestors. The worker finds a cancelled download by its `ProposalItem` (that is how it
        # reaches the transfer and the manifest entry), and deleting an ancestor takes the whole
        # subtree with it through the ORM cascade, so removing them here would leave the transfer
        # running with nothing left pointing at it. The worker deletes those rows itself, and the
        # emptied containers and batch go with them.
        by_id = {item.id: item for item in batch.items}
        keep: set[str] = set()
        for live_id in live_ids:
            parent_id = by_id[live_id].parent_id if live_id in by_id else None
            while parent_id and parent_id in by_id and parent_id not in keep:
                keep.add(parent_id)
                parent_id = by_id[parent_id].parent_id
        remaining = [
            item.id for item in batch.items if item.id in wanted and item.id not in live_ids and item.id not in keep
        ]
        if remaining:
            # Already expanded to descendants above, so `reject_items` must not expand again: the
            # set it is given is exactly the set that goes.
            removed, declined = reject_items(
                session, batch_id, remaining, expand_descendants=False, actor_id=actor_id,
                decline_requests=decline_requests,
            )
            removed_total += removed
            for owner_id, names in declined.items():
                declined_by_owner.setdefault(owner_id, []).extend(n for n in names if n not in declined_by_owner.get(owner_id, []))
        # An emptied batch is debris -- nothing can be done to it and no UI can clear it. (Rows the
        # cancel above left behind are still there until the worker has stopped their transfers; it
        # deletes the batch with them.)
        session.expire(batch, ["items"])
        if not batch.items:
            session.delete(batch)
            session.flush()
    if notify_declined and declined_by_owner:
        # `create_notification` commits, so this goes last, once every removal above is written.
        notify_declined_requests(session, declined_by_owner)
    return canceled_total, removed_total, set(by_batch)


def purge_wishlist_work(session: Session, wishlist_item: WishlistItem, actor_id: str | None = None) -> int:
    """Take a declined or removed request's work with it: searches, downloads, rows and batches.

    Without this a removed request left its candidate rows (and, before the intent batch went, a
    whole "Request: X" batch) sitting in Review with nothing able to act on them -- which is how
    sandalphon ended up with two pending batches of rows reading "finding candidates" for a request
    whose wishlist row already said Declined.
    """
    stop_wishlist_search_tasks(session, {wishlist_item.id})
    item_ids = [
        row_id
        for row_id in session.scalars(
            select(ProposalItem.id).where(ProposalItem.wishlist_item_id == wishlist_item.id)
        )
    ]
    if not item_ids:
        session.commit()
        return 0
    # Never declines: Delete takes the request away itself, and a search Cancel puts it back at gate 1.
    canceled, removed, _ = remove_items(session, item_ids, actor_id, decline_requests=False)
    session.commit()
    return canceled + removed


def stop_wishlist_search_tasks(session: Session, wishlist_item_ids: set[str]) -> int:
    """Cancel any queued/running candidate search still working for these requests."""
    if not wishlist_item_ids:
        return 0
    stopped = 0
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.type == "search_wishlist_item",
                Task.status.in_([TaskStatus.queued, TaskStatus.running]),
            )
        )
    )
    for task in tasks:
        try:
            payload = json.loads(task.payload_json or "{}")
        except (ValueError, TypeError):
            continue
        if payload.get("wishlist_item_id") not in wishlist_item_ids:
            continue
        task.status = TaskStatus.canceled
        task.lease_until = None
        stopped += 1
    if stopped:
        session.flush()
    return stopped


def rejected_items_with_descendants(items: list[ProposalItem], rejected_ids: set[str]) -> list[ProposalItem]:
    expanded_ids = item_ids_with_descendants(items, rejected_ids)
    return [item for item in items if item.id in expanded_ids]


def item_ids_with_descendants(items: list[ProposalItem], root_ids: set[str]) -> set[str]:
    children_by_parent: dict[str, list[ProposalItem]] = {}
    for item in items:
        if item.parent_id:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    expanded_ids = set(root_ids)
    stack = list(root_ids)
    while stack:
        current_id = stack.pop()
        for child in children_by_parent.get(current_id, []):
            if child.id in expanded_ids:
                continue
            expanded_ids.add(child.id)
            stack.append(child.id)
    return expanded_ids


def remove_rejected_download_files(items: list[ProposalItem]) -> int:
    settings = get_settings()
    # ⚠️ Finished downloads are staged under `staging/downloads/<batch>/`, NOT the downloads folder,
    # so checking only `downloads_path` meant a rejected or cancelled "Add to library" file was never
    # removed at all. Both roots are ours to clean; nothing outside them is ever touched.
    roots = [settings.downloads_path.resolve(), (settings.staging_path / "downloads").resolve()]
    removed = 0
    seen_paths: set[Path] = set()
    for item in items:
        if item.kind != ProposalKind.import_files or not item.old_value:
            continue
        file_path = Path(item.old_value).resolve()
        if file_path in seen_paths:
            continue
        root = next((root for root in roots if root in file_path.parents), None)
        if root is None:
            continue
        seen_paths.add(file_path)
        if not file_path.is_file():
            continue
        file_path.unlink()
        prune_empty_download_dirs(file_path.parent, root)
        removed += 1
    return removed


def remove_rejected_manifest_downloads(session: Session, items: list[ProposalItem]) -> int:
    rejected_item_ids = {item.id for item in items}
    if not rejected_item_ids:
        return 0
    downloads_root = get_settings().downloads_path.resolve()
    removed = 0
    for entry in load_download_manifest(session, item_ids=rejected_item_ids):
        if entry.get("status") == "rejected":
            continue
        update_download_manifest_entry(session, entry, "rejected")
        removed += remove_manifest_entry_file(entry, downloads_root)
    return removed


def remove_manifest_entry_file(entry: dict, downloads_root: Path) -> int:
    candidates: list[Path] = []
    if entry.get("path"):
        candidates.append(Path(entry["path"]))
    basename = entry.get("basename")
    if basename:
        candidates.extend(path for path in downloads_root.rglob("*") if path.is_file() and path.name.casefold() == basename)
    for file_path in candidates:
        try:
            resolved = file_path.resolve()
        except OSError:
            continue
        if downloads_root not in [resolved, *resolved.parents] or not resolved.is_file():
            continue
        resolved.unlink()
        prune_empty_download_dirs(resolved.parent, downloads_root)
        return 1
    return 0


def prune_empty_download_dirs(path: Path, stop_at: Path) -> None:
    current = path
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


_METADATA_TARGET_MODELS = {"artist": Artist, "album": Album, "track": Track}


def prune_orphaned_metadata_items(session: Session, batch_id: str | None = None) -> int:
    """Delete unapplied metadata rows whose artist/album/track has left the library.

    ⚠️ A proposal names its target by row id, and a delete, a replace or a re-import gives the
    "same" album or track a new id. Left in the queue, such a row could only ever fail with "Track
    no longer exists" -- while the user can see the track playing in the library under its new id
    (castiel, 2026-10-07: June "Fill MusicBrainz info" / "Apply ReplayGain" batches for a Lemonade
    and a Treehouse that had since been deleted and re-imported). Nothing can apply them, so they
    go, through `remove_items` so emptied containers and batches go too. Runs on an hourly worker
    tick and once more just before a batch executes. Returns how many changes were dropped.

    ⚠️ A stale row with rows under it (an album change over its track changes) is NOT removed --
    `remove_items` takes descendants, and those may still be valid. Its own change is cleared
    instead, leaving it a plain grouping row.
    """
    query = select(ProposalItem).where(
        ProposalItem.kind == ProposalKind.metadata,
        ProposalItem.status.in_((ProposalStatus.pending, ProposalStatus.approved)),
    )
    if batch_id:
        query = query.where(ProposalItem.batch_id == batch_id)
    wanted: dict[str, dict[str, list[str]]] = {}
    for item in session.scalars(query):
        payload = queue_state.payload_of(item)
        target_type, target_id = payload.get("target_type"), payload.get("target_id")
        if target_type in _METADATA_TARGET_MODELS and target_id:
            wanted.setdefault(target_type, {}).setdefault(str(target_id), []).append(item.id)
    orphaned: list[str] = []
    for target_type, by_target in wanted.items():
        model = _METADATA_TARGET_MODELS[target_type]
        ids = list(by_target)
        present: set[str] = set()
        for start in range(0, len(ids), 500):
            present.update(session.scalars(select(model.id).where(model.id.in_(ids[start:start + 500]))))
        for target_id, item_ids in by_target.items():
            if target_id not in present:
                orphaned.extend(item_ids)
    if not orphaned:
        return 0
    parent_ids = set(
        session.scalars(select(ProposalItem.parent_id).where(ProposalItem.parent_id.in_(orphaned)).distinct())
    )
    leaves = [item_id for item_id in orphaned if item_id not in parent_ids]
    for item_id in orphaned:
        if item_id in parent_ids:
            item = session.get(ProposalItem, item_id)
            payload = queue_state.payload_of(item)
            for key in ("target_type", "target_id", "changes"):
                payload.pop(key, None)
            item.payload_json = json.dumps(payload)
            item.old_value = None
            item.new_value = None
    session.flush()
    if leaves:
        remove_items(session, leaves, decline_requests=False)
    session.commit()
    return len(orphaned)


def _is_work_item(item: ProposalItem) -> bool:
    """A row that is itself a change, as opposed to an artist/album/track grouping row.

    ⚠️ Not just "has an `action`": a metadata change carries `target_type` and an import-folder
    file carries `old_value`/`new_value`, neither with an `action`. Testing only `action` made
    `cleanup_empty_container_items` delete every metadata change in a batch the moment any one row
    of it was removed (found in review, 2026-10-07).
    """
    if item.old_value or item.new_value:
        return True
    payload = queue_state.payload_of(item)
    return bool(payload.get("action") or payload.get("target_type"))


def cleanup_empty_container_items(session: Session, batch: ProposalBatch) -> None:
    changed = True
    while changed:
        changed = False
        items = list(batch.items)
        child_parent_ids = {item.parent_id for item in items if item.parent_id}
        for item in items:
            if item.id in child_parent_ids:
                continue
            if _is_work_item(item):
                continue
            session.delete(item)
            changed = True
        if changed:
            session.flush()
            session.expire(batch, ["items"])


# --- retry / cancel -----------------------------------------------------------------------------


def _leaf_download_items(
    batch: ProposalBatch, item_ids: list[str] | None, include_staged: bool = False
) -> list[ProposalItem]:
    """Download leaves under the given ids (or the whole batch), expanded to descendants.

    Expanding means "cancel this album" and "cancel this track" are the same call with a different
    id, rather than two code paths that can disagree.

    `include_staged` adds gate-(b) leaves: downloaded files staged for "Add to library"
    (`import_files` items carrying the file in `old_value`). Cancel wants them -- a request can be
    cancelled after it downloaded -- but retry must not, since there is no transfer to restart.
    """
    if item_ids:
        wanted = item_ids_with_descendants(batch.items, set(item_ids))
        scope = [item for item in batch.items if item.id in wanted]
    else:
        scope = list(batch.items)
    leaves = []
    for item in scope:
        payload = json.loads(item.payload_json or "{}")
        if payload.get("action") in {"queue_download", "queue_ytdlp_download"}:
            leaves.append(item)
        elif include_staged and item.kind == ProposalKind.import_files and item.old_value:
            leaves.append(item)
    return leaves




def _stop_feeding_tasks(session: Session, batch: ProposalBatch, wishlist_item_ids: set[str]) -> int:
    """Stop any queued/running task that is still ADDING items to this batch.

    ⚠️ Cancelling the rows is not enough on its own. A candidate search runs for a long time on a
    big album and appends candidates as it finds them, so a cancel issued mid-search leaves the
    search happily creating fresh `searching`/`awaiting_approval` rows behind it — observed live:
    a cancelled 154-item batch grew to 569 items, 68 of them still live.
    """
    stopped = 0
    feeders = ("search_wishlist_item", "search_candidates", "search_alternatives", "execute_proposal_batch")
    batch_item_ids = {item.id for item in batch.items}
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.type.in_(feeders),
                Task.status.in_([TaskStatus.queued, TaskStatus.running]),
            )
        )
    )
    for task in tasks:
        try:
            payload = json.loads(task.payload_json or "{}")
        except (ValueError, TypeError):
            continue
        targets_batch = payload.get("batch_id") == batch.id or payload.get("track_id") in batch_item_ids
        targets_wishlist = payload.get("wishlist_item_id") in wishlist_item_ids
        if not (targets_batch or targets_wishlist):
            continue
        task.status = TaskStatus.canceled
        task.lease_until = None
        stopped += 1
    return stopped


def _roll_up_container_status(batch: ProposalBatch) -> None:
    """Settle container rows whose children are all settled.

    Containers (artist/album/track) carry no `action` of their own, so the leaf-targeting used by
    cancel/retry never touches them -- which left a fully cancelled batch still showing three rows
    as "awaiting approval", still selected, still lighting up the Approve button. Their children
    *are* their content, so their state has to follow.
    """
    children: dict[str, list[ProposalItem]] = {}
    for item in batch.items:
        if item.parent_id:
            children.setdefault(item.parent_id, []).append(item)
    settled = {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}

    by_id = {item.id: item for item in batch.items}

    def depth(item: ProposalItem) -> int:
        n, parent_id, seen = 0, item.parent_id, set()
        while parent_id and parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            n += 1
            parent_id = by_id[parent_id].parent_id
        return n

    # ⚠️ Deepest FIRST, by real distance from the root — not by child count, which looks like a
    # depth proxy and is not: an artist row with one album child sorts before that album's sixty
    # tracks, so it is evaluated before its own subtree has settled and never rolls up.
    for item in sorted(batch.items, key=depth, reverse=True):
        kids = children.get(item.id)
        if not kids:
            continue
        if all(kid.status in settled for kid in kids):
            if all(kid.status is ProposalStatus.canceled for kid in kids):
                item.status = ProposalStatus.canceled
                item.stage = "canceled"
            item.selected = False


def _mark_items_canceled_for_removal(
    session: Session, batch_id: str, item_ids: list[str] | None, actor_id: str | None = None
) -> list[str]:
    """Stop live work and mark it `canceled`, for `remove_items`'s cancel-then-delete two-step.

    ⚠️ NOT the Cancel button -- see `cancel_items` below, which never deletes anything any more.
    `remove_items` needs the OLD terminal behaviour: a row marked `canceled` here is deleted for
    good by the worker's `run_cancel_download_item(delete_rows=True)`, AFTER it has told
    slskd/yt-dlp to stop and removed the partial file -- deleting the row here, before the worker
    has looked up its transfer/manifest entry by it, would hand the worker nothing to clean up
    with.

    ⚠️ Sets `selected = False` as well as the status. `queue_missing_manifest_download` re-queues
    any *selected* download item that has no manifest entry -- which is precisely the state a
    cancelled track is left in -- so without this the cancel is undone on the next ~3s scan tick.
    `SETTLED_ITEM_STATUSES` (queue_state.py) includes `canceled` for the same reason, so nothing
    re-queues it in the window between this call and the worker's delete either.
    """
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    targets = _leaf_download_items(batch, item_ids, include_staged=True)
    now = datetime.now(timezone.utc)
    cancelled: list[str] = []
    for item in targets:
        if item.status in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}:
            continue
        payload = json.loads(item.payload_json or "{}")
        payload["status"] = "canceled"
        payload["canceled_at"] = now.isoformat()
        if actor_id:
            payload["canceled_by"] = actor_id
        item.payload_json = json.dumps(payload)
        item.status = ProposalStatus.canceled
        item.stage = "canceled"
        item.selected = False
        cancelled.append(item.id)
    if cancelled:
        # Stop the search still feeding this batch BEFORE rolling up, or it re-populates behind us.
        wishlist_ids = {item.wishlist_item_id for item in batch.items if item.wishlist_item_id}
        _stop_feeding_tasks(session, batch, wishlist_ids)
        # Anything the search added while we were cancelling is still live; sweep it too -- but
        # ONLY for a whole-batch cancel. ⚠️ A partial cancel must never widen: a batch can hold
        # other tracks of the same album, or (in "Add to library") other people's requests, and
        # sweeping here cancelled all of them and deleted their staged files.
        for item in ([] if item_ids else _leaf_download_items(batch, None)):
            if item.status not in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}:
                item.status = ProposalStatus.canceled
                item.stage = "canceled"
                item.selected = False
                if item.id not in cancelled:
                    cancelled.append(item.id)
        _roll_up_container_status(batch)
    # A batch with nothing live left is finished, not pending forever. `canceled`, not `rejected` --
    # rejected means an approver declined the request, which is not what happened here, and this
    # value is transient anyway: the worker deletes the batch outright once it has emptied it.
    # ⚠️ ...and only then: a track whose removed row leaves alternates behind still has an open
    # decision (`queue_state.open_decision_rows`), and a batch marked canceled here is deleted by
    # the worker with everything still in it.
    if cancelled and not queue_state.open_decision_rows(batch.items) and all(
        item.status in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}
        or not item.selected
        for item in _leaf_download_items(batch, None, include_staged=True)
    ):
        batch.status = ProposalStatus.canceled
    if cancelled:
        retire_queue_notifications(session, batch_id)
    session.commit()
    if cancelled:
        enqueue_task(session, "cancel_download_item", {"item_ids": cancelled, "delete_rows": True})
    return cancelled


def cancel_items(session: Session, batch_id: str, item_ids: list[str] | None, actor_id: str | None = None) -> list[str]:
    """Stop an in-flight download and hand it back to gate (a) -- Download approval.

    2026-09-23 rewrite ("today a cancel re-searches, and that is a bug"). This is the Cancel
    button, and it is no longer a destructive act: it stops the real transfer and deletes the
    partial file (the worker's `run_cancel_download_item`, enqueued below, with `delete_rows`
    False), then puts the candidate `ProposalItem` straight back to `pending` + `selected` --
    WITHOUT deleting it or any sibling candidate, and WITHOUT starting a new search. Distinct from
    `remove_items`/`reject_items`, which delete for good and go through
    `_mark_items_canceled_for_removal` instead.

    Only stages genuinely doing something right now (`queue_state.CANCELABLE_STAGES`) are ever
    touched; anything else (awaiting approval, staged, already terminal) is a silent no-op and is
    never counted in the returned list -- so a stale bulk selection or a double-tap on a row that
    already finished costs nothing and reports nothing.
    """
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    # `include_staged=False`: a `staged` (gate-b, Import approval) leaf is no longer cancelable --
    # there is no transfer left to stop, and "Remove" is the right action there instead.
    targets = _leaf_download_items(batch, item_ids, include_staged=False)
    now = datetime.now(timezone.utc)
    cancelled: list[str] = []
    wishlist_item_ids: set[str] = set()
    for item in targets:
        if queue_state.resolve_stage(item) not in queue_state.CANCELABLE_STAGES:
            continue
        payload = json.loads(item.payload_json or "{}")
        # No free-text status: `status_label` prefers it over the stage, and this row's honest
        # label is the stage's own -- "download approval".
        payload.pop("status", None)
        payload.pop("download_progress", None)
        payload["canceled_at"] = now.isoformat()
        if actor_id:
            payload["canceled_by"] = actor_id
        item.payload_json = json.dumps(payload)
        item.status = ProposalStatus.pending
        item.stage = ItemStage.awaiting_approval.value
        # `selected` deliberately STAYS True: it is still the chosen candidate for its track, just
        # back at gate (a) instead of mid-transfer. `selected_slskd_download_item_ids` already
        # gates re-queueing on `status` being approved/executing/failed, not on `selected` alone,
        # so a `pending` row here is safely inert against `queue_missing_manifest_download`.
        cancelled.append(item.id)
        if item.wishlist_item_id:
            wishlist_item_ids.add(item.wishlist_item_id)
    if not cancelled:
        return []
    # Stop any search still feeding this batch -- a live search must not repopulate what was just
    # handed back to gate (a) behind us.
    wishlist_ids_feeding = {item.wishlist_item_id for item in batch.items if item.wishlist_item_id}
    _stop_feeding_tasks(session, batch, wishlist_ids_feeding)
    for wishlist_item_id in wishlist_item_ids:
        wishlist_item = session.get(WishlistItem, wishlist_item_id)
        if wishlist_item and wishlist_item.status not in {"rejected", "removed", "completed"}:
            # Back to Download approval, honestly -- not "searching", and nothing is re-enqueued.
            wishlist_item.status = "review"
            wishlist_item.stage = ItemStage.awaiting_approval.value
            wishlist_item.status_changed_at = now
    retire_queue_notifications(session, batch_id)
    session.commit()
    # The slow half: stop the real slskd/yt-dlp transfer and delete the partial file. This needs
    # the row to still exist -- it looks up the manifest/transfer entry by item id -- which is
    # exactly why this no longer deletes it.
    enqueue_task(session, "cancel_download_item", {"item_ids": cancelled, "delete_rows": False})
    return cancelled


def _next_untried_candidate(item: ProposalItem) -> ProposalItem | None:
    """The best still-untried sibling candidate for a failed download leaf, or None.

    Every candidate for a track is its own sibling `ProposalItem` under the same parent, created
    `pending` and left there until something actually uses it: the one originally selected moves to
    `approved`/`executing`/`failed` as it runs, and one the worker's own in-flight replacement
    search swaps in later is settled to `executing`/`failed` too (see the worker's
    `settle_replaced_candidate_item`). A sibling still sitting at `pending` has therefore never been
    tried by anything -- no need to replay `failed_candidates` history to work that out.
    """
    if not item.parent:
        return None
    untried = [
        sibling
        for sibling in item.parent.children
        if sibling.id != item.id
        and sibling.kind == ProposalKind.download
        and sibling.status is ProposalStatus.pending
        and json.loads(sibling.payload_json or "{}").get("action") == "queue_download"
    ]
    if not untried:
        return None

    def _candidate_rank(sibling: ProposalItem) -> int:
        try:
            return int(json.loads(sibling.payload_json or "{}").get("candidate_index", 9999))
        except (TypeError, ValueError):
            return 9999

    return min(untried, key=_candidate_rank)


def _track_download_siblings(item: ProposalItem) -> list[ProposalItem]:
    """The other candidate rows for the same track as `item`."""
    if not item.parent:
        return []
    return [
        sibling
        for sibling in item.parent.children
        if sibling.id != item.id
        and sibling.kind == ProposalKind.download
        and json.loads(sibling.payload_json or "{}").get("action") in {"queue_download", "queue_ytdlp_download"}
    ]


def retry_items(session: Session, batch_id: str, item_ids: list[str] | None, mode: str = "next_candidate") -> list[str]:
    """Put failed downloads back in flight. Returns the ids actually retried.

    ⚠️ Retry starts a download, so it is approval-equivalent and is gated like one -- a requester
    may cancel their own request but never retry it. The route enforces that; this function assumes
    the caller already checked.

    ⚠️ `canceled` is deliberately NOT a retry target. A row the Cancel button touches is put back
    at `pending` (gate a, Download approval) by `cancel_items`, not left `canceled` -- so by the
    time a retry could reach a `canceled` row, it can only be one `remove_items` is in the middle
    of deleting, and there is nothing here to put back in flight. Re-entering the download from
    gate (a) is an ordinary Approve, not a retry.
    """
    return _retry_items(session, batch_id, item_ids, mode)[0]


def _find_alternatives(session: Session, batch: ProposalBatch, item_ids: list[str] | None) -> tuple[list[str], list[Task]]:
    """Start an alternatives search for each track the ids resolve to (track or candidate rows).

    The current candidates stay; the worker adds up to 5 new unselected ones. Nothing downloads.
    """
    flow = batch.flow if isinstance(batch.flow, ProposalFlow) else ProposalFlow.library_change
    eligible = queue_state.alternatives_track_ids(batch.items, flow, batch.status)
    by_id = {item.id: item for item in batch.items}
    track_ids: list[str] = []
    for leaf in _leaf_download_items(batch, item_ids):
        if leaf.parent_id in eligible and leaf.parent_id not in track_ids:
            track_ids.append(leaf.parent_id)
    if not track_ids:
        raise ValueError("Nothing here can look for other candidates")
    for track_id in track_ids:
        track = by_id[track_id]
        payload = json.loads(track.payload_json or "{}")
        payload["status"] = "finding candidates"
        payload["finding_alternatives"] = True
        payload.pop("no_alternatives", None)
        track.payload_json = json.dumps(payload)
        track.stage = ItemStage.searching.value
    session.commit()
    tasks = [enqueue_task(session, "search_alternatives", {"track_id": track_id}) for track_id in track_ids]
    return track_ids, tasks


def _retry_items(
    session: Session, batch_id: str, item_ids: list[str] | None, mode: str = "next_candidate"
) -> tuple[list[str], list[Task]]:
    """`retry_items`, also returning the worker tasks it enqueued (Approve answers with one)."""
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    if mode not in {"next_candidate", "same_candidate", "research", "alternatives"}:
        raise ValueError(f"Unknown retry mode: {mode}")
    if mode == "alternatives":
        return _find_alternatives(session, batch, item_ids)
    targets = _leaf_download_items(batch, item_ids)
    retried: list[str] = []
    tasks: list[Task] = []
    # Round 4 #2: Retry must always do something. `next_candidate` used to silently no-op when
    # every sibling candidate had already been tried -- tracked per item here so a batch that mixes
    # "switch candidate" and "search again" items still enqueues the right worker task for each.
    retried_mode: dict[str, str] = {}
    retried_tracks: set[str] = set()
    for item in targets:
        if item.status is not ProposalStatus.failed:
            continue
        # ⚠️ One download per track. Every tried candidate is left `failed` under the track, and
        # clients send them all when Retry is pressed on a track, album or batch. Each one used to
        # start its own alternate, next to the track's live row, which was still working through
        # its own automatic retries. That put two or three transfers on one track at once and
        # staged every copy that landed. So a track is retried once per call, and not at all while
        # another of its rows is downloading or has already downloaded.
        track_key = item.parent_id or item.id
        siblings = _track_download_siblings(item)
        if track_key in retried_tracks or any(sibling.status in {ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.completed} for sibling in siblings):
            continue
        retried_tracks.add(track_key)
        # `next_candidate` means switch to a different, never-tried sibling candidate -- it must
        # NOT just reset this same failed item and hope. Before this, both modes did exactly the
        # same thing: reset the failed item and let the worker re-queue its own already-failing
        # candidate, so "next candidate" only advanced if the worker's own in-flight replacement
        # search happened to find something (and exhausted it all over again if not).
        target = item
        effective_mode = mode
        if mode == "next_candidate":
            alternate = _next_untried_candidate(item)
            if alternate is not None:
                item.selected = False
                target = alternate
            else:
                # Nothing left to switch to -- fall back to `research` (a fresh search for this
                # track) instead of doing nothing. This is the same path the "research" button
                # takes, re-entering gate (a) / Download approval once new candidates are found.
                effective_mode = "research"
        payload = json.loads(target.payload_json or "{}")
        # Keep the history -- Issues should be able to say what was already tried -- but clear the
        # flags that make the worker treat this as finished.
        if payload.get("failed_candidates"):
            payload["previous_failures"] = payload.get("failed_candidates")
        payload.pop("failed_candidates", None)
        payload.pop("auto_retry_exhausted", None)
        payload.pop("retry_reason", None)
        if target.parent is not None:
            parent_payload = json.loads(target.parent.payload_json or "{}")
            if parent_payload.pop("no_alternatives", None):
                target.parent.payload_json = json.dumps(parent_payload)
        # `research` re-enters gate (a): it throws away the candidates and searches again, so a
        # human picks from the new ones. It must NOT auto-approve.
        #
        # ⚠️ Until the worker has run that search, this row IS the search -- `searching`, not
        # `awaiting_approval`. It used to read `awaiting_approval` (+ "retrying" text), which made
        # the candidate that had just failed approvable again: Approve re-downloaded it, and the
        # worker then deleted the row mid-transfer when its research landed.
        if effective_mode == "research":
            payload["status"] = "finding candidates"
            target.status = ProposalStatus.pending
            target.stage = ItemStage.searching.value
        else:
            payload["status"] = "retrying"
            target.status = ProposalStatus.approved
            target.stage = ItemStage.retrying.value
        target.payload_json = json.dumps(payload)
        # The retried row becomes the track's only selected row, so the track reads as this
        # attempt rather than the worst of the failed ones beside it.
        for sibling in siblings:
            sibling.selected = False
        target.selected = True
        retried.append(target.id)
        retried_mode[target.id] = effective_mode
        if target.wishlist_item_id:
            wishlist_item = session.get(WishlistItem, target.wishlist_item_id)
            if wishlist_item:
                wishlist_item.status = "searching" if effective_mode == "research" else "downloading"
                wishlist_item.stage = ItemStage.searching.value if effective_mode == "research" else ItemStage.retrying.value
                wishlist_item.status_changed_at = datetime.now(timezone.utc)
    if retried:
        # Revive the ancestor chain. `cancel_items` settles containers when everything beneath them
        # settles, and batch execution settles them as completed/failed, so a retried leaf would
        # otherwise hang under an artist row still reading "canceled" or "completed" — a tree that
        # contradicts itself. Any settled ancestor is live again once work beneath it is.
        by_id = {item.id: item for item in batch.items}
        for item_id in retried:
            parent_id = by_id[item_id].parent_id if item_id in by_id else None
            seen: set[str] = set()
            while parent_id and parent_id in by_id and parent_id not in seen:
                seen.add(parent_id)
                parent = by_id[parent_id]
                if parent.status in {ProposalStatus.canceled, ProposalStatus.completed, ProposalStatus.failed}:
                    parent.status = ProposalStatus.pending
                    parent.selected = True
                # Stamp the leaf's new stage on EVERY ancestor, live ones included. Clearing it
                # instead let the row fall back to its old progress payload ("1 of 1 need
                # attention"), so a retry read as failed until the worker's next tick.
                parent.stage = by_id[item_id].stage
                parent_id = parent.parent_id
        # ⚠️ `canceled` belongs in this set for the same reason `rejected` used to before
        # 2026-09-21: a batch can settle to `canceled` while still holding a `failed`-but-unselected
        # item (cancel's "nothing live left" check treats unselected as settled too), and reviving
        # that one item here must not leave a terminal batch status sitting on top of it.
        if batch.status in {ProposalStatus.failed, ProposalStatus.rejected, ProposalStatus.canceled, ProposalStatus.completed}:
            any_research = any(m == "research" for m in retried_mode.values())
            batch.status = ProposalStatus.pending if any_research else ProposalStatus.approved
        session.commit()
        # One task per effective mode -- a `next_candidate` call that fell back to `research` for
        # some items must not tell the worker to treat those as ordinary candidate switches.
        by_mode: dict[str, list[str]] = {}
        for item_id in retried:
            by_mode.setdefault(retried_mode.get(item_id, mode), []).append(item_id)
        for effective_mode, ids in by_mode.items():
            tasks.append(enqueue_task(session, "retry_download_item", {"item_ids": ids, "mode": effective_mode}))
    else:
        session.commit()
    return retried, tasks
