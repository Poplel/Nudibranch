import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import (
    Permission,
    TaskStatus,
    ProposalBatch,
    ProposalFlow,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    Task,
    User,
    WishlistItem,
)
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


def approve_batch(
    session: Session,
    batch_id: str,
    item_ids: list[str] | None = None,
    actor: User | None = None,
) -> Task:
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    assert_may_approve(batch, actor)
    batch.status = ProposalStatus.approved
    preferred_ids = set(item_ids or [])
    approved_ids = item_ids_with_descendants(batch.items, preferred_ids) if item_ids is not None else None
    normalize_download_candidate_selection(batch.items, preferred_ids)
    for item in batch.items:
        if approved_ids is not None and item.id not in approved_ids:
            continue
        # ⚠️ `canceled` is deliberately NOT in this set. Approving a batch must not resurrect a
        # track the user explicitly stopped -- that is what makes a cancel stick.
        if item.selected and item.status in {ProposalStatus.pending, ProposalStatus.failed}:
            item.status = ProposalStatus.approved
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


def reject_items(session: Session, batch_id: str, item_ids: list[str] | None) -> int:
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")

    if item_ids:
        items = rejected_items_with_descendants(batch.items, set(item_ids))
    else:
        items = list(batch.items)
    rejected_ids = {item.id for item in items}
    rejected_wishlist_items: dict[str, list[str]] = {}
    removed_download_files = remove_rejected_download_files(items) + remove_rejected_manifest_downloads(items)
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        request_payload = payload.get("request") or {}
        # Prefer the real columns; fall back to the payload for rows predating them.
        # ⚠️ The old condition required BOTH ids from the payload, and the gate-(b) review items
        # carried neither — so rejecting a staged import silently left the wishlist row reading
        # "completed" for a file that was just thrown away. Requiring only the wishlist id (and
        # resolving the owner from the row itself) is what closes that.
        wishlist_item_id = (
            item.wishlist_item_id
            or payload.get("wishlist_item_id")
            or request_payload.get("wishlist_item_id")
        )
        if wishlist_item_id:
            wishlist_item = session.get(WishlistItem, wishlist_item_id)
            if wishlist_item:
                owner_id = item.requester_id or payload.get("user_id") or wishlist_item.user_id
                if owner_id:
                    rejected_wishlist_items.setdefault(owner_id, []).append(str(item.title))
                wishlist_item.status = "rejected"
                wishlist_item.stage = "rejected"
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
            target_url="/task-queue?bucket=issues",
        )
    for user_id, titles in rejected_wishlist_items.items():
        shown = ", ".join(titles[:5])
        extra = "" if len(titles) <= 5 else f" and {len(titles) - 5} more"
        create_notification(
            session,
            title="Request declined",
            body=f"{shown}{extra}",
            event_type="wishlist_denied",
            target_url="/wishlist",
            user_id=user_id,
            group_key=f"wishlist-decision:wishlist_denied:{user_id}",
        )
    return len(rejected_ids)


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


def remove_rejected_manifest_downloads(items: list[ProposalItem]) -> int:
    rejected_item_ids = {item.id for item in items}
    if not rejected_item_ids:
        return 0
    settings = get_settings()
    downloads_root = settings.downloads_path.resolve()
    # Use the config-volume path (current location after migration); fall back to the legacy
    # downloads-folder path so rejections still work if the manifest hasn't been migrated yet.
    manifest_path = settings.config_path / ".nudibranch-downloads.json"
    if not manifest_path.exists():
        manifest_path = settings.downloads_path / ".nudibranch-downloads.json"
    if not manifest_path.exists():
        return 0
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(entries, list):
        return 0
    removed = 0
    changed = False
    for entry in entries:
        if entry.get("item_id") not in rejected_item_ids or entry.get("status") == "rejected":
            continue
        entry["status"] = "rejected"
        entry["status_changed_at"] = datetime.now(timezone.utc).isoformat()
        changed = True
        removed += remove_manifest_entry_file(entry, downloads_root)
    if changed:
        manifest_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
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


# --- retry / cancel -----------------------------------------------------------------------------


def _leaf_download_items(batch: ProposalBatch, item_ids: list[str] | None) -> list[ProposalItem]:
    """Download leaves under the given ids (or the whole batch), expanded to descendants.

    Expanding means "cancel this album" and "cancel this track" are the same call with a different
    id, rather than two code paths that can disagree.
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
    return leaves




def _stop_feeding_tasks(session: Session, batch: ProposalBatch, wishlist_item_ids: set[str]) -> int:
    """Stop any queued/running task that is still ADDING items to this batch.

    ⚠️ Cancelling the rows is not enough on its own. A candidate search runs for a long time on a
    big album and appends candidates as it finds them, so a cancel issued mid-search leaves the
    search happily creating fresh `searching`/`awaiting_approval` rows behind it — observed live:
    a cancelled 154-item batch grew to 569 items, 68 of them still live.
    """
    stopped = 0
    feeders = ("search_wishlist_item", "search_candidates", "execute_proposal_batch")
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
        targets_batch = payload.get("batch_id") == batch.id
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


def cancel_items(session: Session, batch_id: str, item_ids: list[str] | None, actor_id: str | None = None) -> list[str]:
    """Stop this work. Returns the ids marked cancelled.

    Distinct from `reject_items`, which is an approver's decline and tells the requester their
    request was denied. A cancel is "not now" -- from whoever's item it is, requester or approver --
    and per the 2026-09-21 product rule it is never a resting state: a cancelled item is deleted for
    good, an emptied batch goes with it, and a cancelled wishlist request goes back to searching
    rather than sitting there looking declined. This function only marks the state; the row/file
    deletion, the empty-batch cleanup and the wishlist reset all happen in the worker's
    `run_cancel_download_item`, AFTER it has told slskd/yt-dlp to stop and removed the partial file
    -- deleting the row here, before the worker has looked up its transfer/manifest entry by it,
    would hand the worker nothing to clean up with.

    ⚠️ Sets `selected = False` as well as the status. `queue_missing_manifest_download` re-queues
    any *selected* download item that has no manifest entry -- which is precisely the state a
    cancelled track is left in -- so without this the cancel is undone on the next ~3s scan tick.
    `SETTLED_ITEM_STATUSES` (queue_state.py) includes `canceled` for the same reason, so nothing
    re-queues it in the window between this call and the worker's delete either.
    """
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    targets = _leaf_download_items(batch, item_ids)
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
        if item.wishlist_item_id:
            wishlist_item = session.get(WishlistItem, item.wishlist_item_id)
            # ⚠️ "rejected" must be excluded here too, not just "completed"/"removed": the worker's
            # reset (run_cancel_download_item) skips reviving a rejected/removed row, but it can
            # only tell by reading THIS status -- overwriting a declined row to "canceled" here
            # would erase the fact that it was ever declined and let the worker bring it back.
            if wishlist_item and wishlist_item.status not in {"completed", "removed", "rejected"}:
                wishlist_item.status = "canceled"
                wishlist_item.stage = "canceled"
                wishlist_item.status_changed_at = now
    if cancelled:
        # Stop the search still feeding this batch BEFORE rolling up, or it re-populates behind us.
        wishlist_ids = {item.wishlist_item_id for item in batch.items if item.wishlist_item_id}
        _stop_feeding_tasks(session, batch, wishlist_ids)
        # Anything the search added while we were cancelling is still live; sweep it too.
        for item in _leaf_download_items(batch, None):
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
    if cancelled and all(
        item.status in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}
        or not item.selected
        for item in _leaf_download_items(batch, None)
    ):
        batch.status = ProposalStatus.canceled
    session.commit()
    if cancelled:
        enqueue_task(session, "cancel_download_item", {"item_ids": cancelled})
    return cancelled


def retry_items(session: Session, batch_id: str, item_ids: list[str] | None, mode: str = "next_candidate") -> list[str]:
    """Put failed downloads back in flight. Returns the ids actually retried.

    ⚠️ Retry starts a download, so it is approval-equivalent and is gated like one -- a requester
    may cancel their own request but never retry it. The route enforces that; this function assumes
    the caller already checked.

    ⚠️ `canceled` is deliberately NOT a retry target (it was, before 2026-09-21). A cancelled item
    is now deleted outright by the worker's `run_cancel_download_item`, so by the time a retry could
    reach it there is nothing here to put back in flight -- the wishlist row gets a fresh search
    instead, which is retry's `research` mode in everything but name.
    """
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    if mode not in {"next_candidate", "same_candidate", "research"}:
        raise ValueError(f"Unknown retry mode: {mode}")
    targets = _leaf_download_items(batch, item_ids)
    retried: list[str] = []
    for item in targets:
        if item.status is not ProposalStatus.failed:
            continue
        payload = json.loads(item.payload_json or "{}")
        # Keep the history -- Issues should be able to say what was already tried -- but clear the
        # flags that make the worker treat this as finished.
        if payload.get("failed_candidates"):
            payload["previous_failures"] = payload.get("failed_candidates")
        payload.pop("failed_candidates", None)
        payload.pop("auto_retry_exhausted", None)
        payload.pop("retry_reason", None)
        payload["status"] = "retrying"
        item.payload_json = json.dumps(payload)
        # `research` re-enters gate (a): it throws away the candidates and searches again, so a
        # human picks from the new ones. It must NOT auto-approve.
        item.status = ProposalStatus.pending if mode == "research" else ProposalStatus.approved
        item.stage = "awaiting_approval" if mode == "research" else "retrying"
        item.selected = True
        retried.append(item.id)
        if item.wishlist_item_id:
            wishlist_item = session.get(WishlistItem, item.wishlist_item_id)
            if wishlist_item:
                wishlist_item.status = "review" if mode == "research" else "downloading"
                wishlist_item.stage = "awaiting_approval" if mode == "research" else "retrying"
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
            batch.status = ProposalStatus.pending if mode == "research" else ProposalStatus.approved
        session.commit()
        enqueue_task(session, "retry_download_item", {"item_ids": retried, "mode": mode})
    else:
        session.commit()
    return retried
