"""One vocabulary for "what is happening to this proposal item, and where does it show".

Before this module every client answered those questions for itself by pattern-matching free text
out of `ProposalItem.payload_json` -- iOS had four disagreeing status->colour maps and the web had
another three label functions, several of which rendered the raw wire enum ("wanted", "pending")
straight into the UI.  Everything here is deliberately pure: it imports only the models and the
standard library, so both `api/routes.py` and `worker/main.py` can use it with no import cycle.

The three concepts are NOT interchangeable:

* `ProposalFlow`  -- stored on the batch, fixed for its lifetime, says which approval gate it is.
* `ItemStage`     -- the live fine-grained state; changes constantly.
* `QueueBucket`   -- derived from the two, never stored (see `bucket_for`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from nudibranch.db.models import (
    ItemStage,
    ProposalBatch,
    ProposalFlow,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    QueueBucket,
)

LOSSLESS_AUDIO_EXTENSIONS = (".flac", ".wav", ".aiff", ".aif", ".alac")

# Stage assigned when nothing more specific is known, per terminal ProposalStatus.
_STATUS_TO_STAGE: dict[ProposalStatus, ItemStage] = {
    ProposalStatus.rejected: ItemStage.rejected,
    ProposalStatus.canceled: ItemStage.canceled,
    ProposalStatus.completed: ItemStage.completed,
    ProposalStatus.failed: ItemStage.failed,
    ProposalStatus.approved: ItemStage.approved,
}

# The worker's own `download_progress_payload` stage vocabulary, mapped onto ItemStage.  Note
# "verified" -> staged: the worker means "content check passed, sitting in staging", which is
# exactly the thing awaiting gate (b).
_PAYLOAD_STAGE_TO_ITEM_STAGE: dict[str, ItemStage] = {
    "searching": ItemStage.searching,
    "queued": ItemStage.queued,
    "downloading": ItemStage.downloading,
    "retrying": ItemStage.retrying,
    "staging": ItemStage.staging,
    "verifying": ItemStage.verifying,
    "verified": ItemStage.staged,
    "staged": ItemStage.staged,
    "importing": ItemStage.importing,
    "completed": ItemStage.completed,
    "failed": ItemStage.failed,
    "canceled": ItemStage.canceled,
}

# Download-manifest statuses (.nudibranch-downloads.json) onto ItemStage.  The manifest itself is
# never read by the API -- the worker mirrors it into ProposalItem.stage -- but the mapping lives
# here so there is exactly one translation table.
MANIFEST_STATUS_TO_STAGE: dict[str, ItemStage] = {
    "queued": ItemStage.queued,
    "downloading": ItemStage.downloading,
    "retrying": ItemStage.retrying,
    "staged": ItemStage.staging,
    "verifying": ItemStage.verifying,
    "verified": ItemStage.staged,
    "failed": ItemStage.failed,
    "completed": ItemStage.completed,
    "rejected": ItemStage.canceled,
    "rejected_removed": ItemStage.canceled,
}

_STAGE_DEFAULT_LABEL: dict[ItemStage, str] = {
    ItemStage.waiting: "waiting",
    # Gate 1: a wishlist request waiting for a human to approve the search. Never appears in the
    # Task Queue (a ProposalItem is never created before gate 1 passes).
    ItemStage.requested: "request approval",
    ItemStage.searching: "searching",
    # Flow-dependent gate (a)/(b)/(c) label -- resolved properly in `status_label` below, which
    # needs the batch's `flow` to pick "download approval" / "import approval" / "change
    # approval". This entry is only the fallback for a caller that has no flow to give it.
    ItemStage.awaiting_approval: "awaiting approval",
    ItemStage.approved: "waiting to download",
    ItemStage.queued: "waiting to download",
    ItemStage.downloading: "downloading",
    ItemStage.retrying: "retrying",
    ItemStage.staging: "moving into place",
    ItemStage.verifying: "verifying",
    # A `library_review` leaf sitting in staging, waiting for gate (b).
    ItemStage.staged: "import approval",
    ItemStage.importing: "importing",
    ItemStage.completed: "done",
    ItemStage.failed: "needs attention",
    ItemStage.canceled: "canceled",
    ItemStage.rejected: "rejected",
}

# Rough completion for a stage with no numeric progress of its own, so a row is never a dead 0%
# bar while real work is happening.
_STAGE_DEFAULT_PROGRESS: dict[ItemStage, float] = {
    ItemStage.waiting: 0.0,
    ItemStage.requested: 0.0,
    ItemStage.searching: 0.0,
    ItemStage.awaiting_approval: 0.0,
    ItemStage.approved: 0.0,
    ItemStage.queued: 0.0,
    ItemStage.downloading: 0.0,
    ItemStage.retrying: 0.0,
    ItemStage.staging: 100.0,
    ItemStage.verifying: 100.0,
    ItemStage.staged: 100.0,
    ItemStage.importing: 100.0,
    ItemStage.completed: 100.0,
    ItemStage.failed: 0.0,
    ItemStage.canceled: 0.0,
    ItemStage.rejected: 0.0,
}

_INDETERMINATE_STAGES = frozenset(
    {ItemStage.searching, ItemStage.verifying, ItemStage.importing, ItemStage.staging, ItemStage.retrying}
)

# Stages that only make sense BEFORE a human has approved something. `resolve_stage` below must
# not trust a cached value from this set once `status` has moved past `pending` -- see its comment.
_PRE_APPROVAL_STAGES = frozenset(
    {ItemStage.waiting, ItemStage.requested, ItemStage.searching, ItemStage.awaiting_approval}
)

# Worst-wins ordering for rolling child stages up to a container.  "Worst" means "most in need of
# a human": a batch with one failed track is a failed batch even if ten others finished.
_STAGE_SEVERITY: list[ItemStage] = [
    ItemStage.failed,
    ItemStage.requested,
    ItemStage.awaiting_approval,
    ItemStage.retrying,
    ItemStage.searching,
    ItemStage.downloading,
    ItemStage.queued,
    ItemStage.verifying,
    ItemStage.staging,
    ItemStage.importing,
    ItemStage.staged,
    ItemStage.approved,
    ItemStage.waiting,
    ItemStage.canceled,
    ItemStage.rejected,
    ItemStage.completed,
]

_DOWNLOAD_ACTIONS = frozenset({"queue_download", "queue_ytdlp_download"})

# "This item is settled -- do not reconsider, re-queue or re-dispatch it."
#
# ⚠️ `canceled` MUST be in here. `queue_missing_manifest_download` re-queues any download item that
# is still live and has no manifest entry, which is exactly the shape a just-cancelled track has --
# so a cancel that only cleared the manifest would be undone within one ~3s scan tick.  Cancel also
# sets `selected = False`, and most call sites check that first; this set is the second line, so the
# behaviour survives a future change that stops consulting `selected`.
SETTLED_ITEM_STATUSES = frozenset(
    {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.canceled}
)

# Stages a human can still approve from.  Anything else is either already moving or finished, and
# the clients grey it out rather than letting it be re-approved.
APPROVABLE_STAGES = frozenset({ItemStage.awaiting_approval, ItemStage.waiting, ItemStage.failed})

# ⚠️ Gate (b) adds one. A `library_review` leaf IS a downloaded file waiting in staging, so its
# honest stage is `staged` -- and `staged` was not approvable, which made "Add to library" report
# `can_approve=false` on the one row in the batch a human is there to approve, while its own
# artist/album containers said `true`. It stays out of the download gate, where a staged leaf is
# already past its approval.
LIBRARY_REVIEW_APPROVABLE_STAGES = APPROVABLE_STAGES | {ItemStage.staged}

# Stages where stopping the work still means something. ⚠️ 2026-09-23: NARROWED on purpose.
# `waiting`, `awaiting_approval` and `staged` are gates waiting on a human DECISION, not work in
# flight -- there is nothing there for Cancel to stop, and the contract's `can_approve`/"Remove"
# actions are what apply instead. Only these five have a live search or transfer behind them.
CANCELABLE_STAGES = frozenset(
    {
        ItemStage.searching,
        ItemStage.approved,
        ItemStage.queued,
        ItemStage.downloading,
        ItemStage.retrying,
    }
)


def payload_of(item: ProposalItem) -> dict:
    """Parse an item's payload once.  Callers should reuse the result, never re-parse per field."""
    try:
        payload = json.loads(item.payload_json or "{}")
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def is_lossless_filename(filename: str) -> bool:
    return str(filename or "").lower().endswith(LOSSLESS_AUDIO_EXTENSIONS)


def candidate_format_label(candidate: dict) -> str:
    """Terse format/quality label for one candidate (e.g. "FLAC", "MP3 320").

    Moved here from worker/main.py so the label the UI shows and the label the worker logs are
    computed by the same code.
    """
    name = str(candidate.get("filename") or "")
    ext = Path(name.replace("\\", "/")).suffix.lower().lstrip(".")
    label = ext.upper() if ext else str(candidate.get("quality") or "").upper()
    if not is_lossless_filename(name):
        try:
            bitrate = int(candidate.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        if bitrate:
            label = f"{label} {bitrate}".strip()
    return label


def resolve_stage(item: ProposalItem, payload: dict | None = None) -> ItemStage:
    """The item's live stage.  First match wins; see the module docstring for why this ordering.

    The denormalized `item.stage` column is trusted when present because the worker writes it at
    the moment it changes something -- it is fresher than anything derivable here.
    """
    # ⚠️ ORDER MATTERS. A TERMINAL `status` beats the cache, always.
    #
    # `stage` is a denormalized convenience written at creation and refreshed by the worker, so it
    # goes stale the moment a path forgets to update it — and it was masking real outcomes: a
    # candidate sitting at `status=failed` still reported `awaiting_approval` because that was its
    # creation-time cache value, so a failed download looked like it was waiting for a human.
    # `status` is written by the core engine and is authoritative when it is terminal; the cache
    # only refines a non-terminal status (queued vs downloading vs verifying).
    if item.status in _STATUS_TO_STAGE and item.status is not ProposalStatus.approved:
        return _STATUS_TO_STAGE[item.status]

    if item.stage:
        try:
            cached = ItemStage(item.stage)
        except ValueError:
            cached = None  # unknown cache value: fall through and recompute rather than trust it
        # ⚠️ 2026-09-23: a pre-approval cache (awaiting_approval/waiting/searching/requested) is
        # stale the instant `status` moves past `pending` -- `approve_batch` flips `status` straight
        # to `approved` (and the worker on to `executing`/`queued`) without touching `stage`, which
        # is only refreshed once real download progress is reported. A just-approved item sitting in
        # that gap kept reporting its creation-time "awaiting approval" cache, which is why queued/
        # executing rows were showing `can_approve=true` (APPROVABLE_STAGES includes
        # awaiting_approval). Only trust a pre-approval cache while the row itself still agrees it
        # is pending.
        if cached is not None and not (cached in _PRE_APPROVAL_STAGES and item.status is not ProposalStatus.pending):
            return cached

    data = payload if payload is not None else payload_of(item)
    progress = data.get("download_progress")
    if isinstance(progress, dict):
        mapped = _PAYLOAD_STAGE_TO_ITEM_STAGE.get(str(progress.get("stage") or "").casefold())
        if mapped is not None:
            return mapped

    if item.status is ProposalStatus.approved:
        return ItemStage.approved
    if item.status is ProposalStatus.executing:
        if item.kind is ProposalKind.download:
            return ItemStage.queued
        return ItemStage.importing
    if item.status is ProposalStatus.pending:
        action = str(data.get("action") or "")
        if item.kind is ProposalKind.download and action in _DOWNLOAD_ACTIONS:
            return ItemStage.awaiting_approval
        if item.kind is ProposalKind.download and not action:
            # A download container with no candidates attached yet is still being searched -- that
            # is the window that used to show nothing at all between "wishlisted" and "ready".
            return ItemStage.searching if not item.children else ItemStage.awaiting_approval
        return ItemStage.awaiting_approval
    return ItemStage.waiting


def bucket_for(flow: ProposalFlow | str | None, stage: ItemStage) -> QueueBucket:
    """The single derivation of which Task Queue tab something belongs in.

    Deliberately not stored: `issues` flips on every failure, retry and cancel, so a column would
    mean a write on each of those in the hot download loop and would drift the first time a code
    path forgot one.
    """
    if stage in (ItemStage.failed, ItemStage.canceled):
        return QueueBucket.issues
    resolved = flow if isinstance(flow, ProposalFlow) else _coerce_flow(flow)
    if resolved is ProposalFlow.download_review:
        return QueueBucket.review
    return QueueBucket.changes


def _coerce_flow(flow: str | None) -> ProposalFlow:
    try:
        return ProposalFlow(str(flow))
    except ValueError:
        return ProposalFlow.library_change


# The `awaiting_approval` stage means three different things depending which gate the batch is
# for -- resolved here, once, so no client has to know `ProposalFlow` exists.
_AWAITING_APPROVAL_LABEL_BY_FLOW: dict[ProposalFlow, str] = {
    ProposalFlow.download_review: "download approval",
    ProposalFlow.library_review: "import approval",
    ProposalFlow.library_change: "change approval",
}


def status_label(
    item: ProposalItem, stage: ItemStage, payload: dict | None = None, flow: ProposalFlow | str | None = None
) -> str:
    """Human-readable status.  The worker's own free text wins when it exists -- it is more
    specific ("87% match - FLAC") than any generic stage name.

    `flow` disambiguates `awaiting_approval` into "download approval" / "import approval" /
    "change approval" -- the same stage means a different gate depending which batch it is in.
    Omitting it (the caller has none to give, e.g. a wishlist row) falls back to the flow-neutral
    `_STAGE_DEFAULT_LABEL` entry ("awaiting approval").
    """
    data = payload if payload is not None else payload_of(item)
    existing = data.get("status")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    if stage is ItemStage.awaiting_approval and flow is not None:
        resolved_flow = flow if isinstance(flow, ProposalFlow) else _coerce_flow(flow)
        return _AWAITING_APPROVAL_LABEL_BY_FLOW.get(resolved_flow, _STAGE_DEFAULT_LABEL[stage])
    return _STAGE_DEFAULT_LABEL.get(stage, stage.value)


def item_progress(
    item: ProposalItem, stage: ItemStage, payload: dict | None = None, flow: ProposalFlow | str | None = None
) -> dict:
    """`{value, label, indeterminate, stage}` for any item of any kind.

    Non-download proposals had no progress at all before this -- they now get a synthesized floor
    from their stage, so a long metadata or import apply shows movement instead of a frozen row.
    """
    data = payload if payload is not None else payload_of(item)
    raw = data.get("download_progress")
    value: float | None = None
    indeterminate: bool | None = None
    label: str | None = None
    if isinstance(raw, dict):
        for key in ("value", "progress"):
            candidate = raw.get(key)
            if isinstance(candidate, (int, float)):
                value = max(0.0, min(100.0, float(candidate)))
                break
        if isinstance(raw.get("indeterminate"), bool):
            indeterminate = raw["indeterminate"]
        if isinstance(raw.get("label"), str) and raw["label"].strip():
            label = raw["label"].strip()
    if value is None:
        value = _STAGE_DEFAULT_PROGRESS.get(stage, 0.0)
    if indeterminate is None:
        indeterminate = stage in _INDETERMINATE_STAGES
    return {
        "value": value,
        "label": label or status_label(item, stage, data, flow),
        "indeterminate": bool(indeterminate),
        "stage": stage,
    }


def music_key(payload: dict) -> str:
    """`artist::album::track`, casefolded -- the identity of the MUSIC a row is about.

    Both clients derived this from the raw payload to dedupe rows; it is computed here now so the
    two cannot disagree about what counts as the same track.
    """
    request = payload.get("request")
    request = request if isinstance(request, dict) else {}
    parts = []
    for keys in (("artist",), ("album",), ("track", "title")):
        value = ""
        for key in keys:
            value = request.get(key) or payload.get(key) or ""
            if value:
                break
        parts.append(" ".join(str(value).casefold().split()))
    return "::".join(parts)


def candidate_identity(payload: dict) -> str | None:
    """Stable identity for one candidate row, for client-side de-duplication.

    `<filename>::<username>` for an slskd candidate, `youtube::<music key>` for a yt-dlp fallback
    (which has no file to name yet).  Returns None for anything that is not a candidate.
    """
    candidate = payload.get("candidate")
    action = str(payload.get("action") or "")
    if not isinstance(candidate, dict) or not candidate:
        if action == "queue_ytdlp_download":
            return f"youtube::{music_key(payload)}"
        return None
    filename = " ".join(str(candidate.get("filename") or "").casefold().split())
    username = " ".join(str(candidate.get("username") or "").casefold().split())
    return f"{filename}::{username}"


def is_actionable(item: ProposalItem, payload: dict | None = None) -> bool:
    """Whether this row is a real change the batch would apply, rather than a grouping container.

    The same rule `run_execute_proposal_batch` selects work by, and the rule both clients used to
    re-derive from `payload_json` (`isExecutable` on iOS, `isExecutableApprovalItem` on the web).
    It is on the wire as `actionable` so they no longer have to.
    """
    data = payload if payload is not None else payload_of(item)
    if item.kind is ProposalKind.import_files:
        return bool(item.old_value and item.new_value)
    if item.kind is ProposalKind.metadata:
        return bool(data.get("target_type"))
    if item.kind in {
        ProposalKind.delete,
        ProposalKind.file_move,
        ProposalKind.playlist,
        ProposalKind.download,
        ProposalKind.lyrics,
    }:
        return bool(data.get("action"))
    return False


def candidate_out(payload: dict) -> dict | None:
    """Project the stored candidate dict onto the wire shape.

    Everything here already exists on the candidate the matcher produced; size, duration,
    queue_length, free_upload_slots and upload_speed were simply dropped before reaching any UI,
    which is why a human could never sanity-check a match the ranker got wrong.
    """
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict) or not candidate:
        return None

    def _int(key: str) -> int | None:
        try:
            raw = candidate.get(key)
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _float(key: str) -> float | None:
        try:
            raw = candidate.get(key)
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "identity": candidate_identity(payload),
        "username": candidate.get("username"),
        "filename": candidate.get("filename"),
        "folder": candidate.get("folder"),
        "size_bytes": _int("size"),
        "duration_seconds": _float("duration"),
        "bitrate": _int("bitrate"),
        "format": candidate_format_label(candidate) or None,
        "quality": candidate.get("quality"),
        "confidence": _int("confidence"),
        "same_album_folder": bool(candidate.get("same_album_folder")),
        "album_folder_rank": _int("album_folder_rank"),
        "free_upload_slots": candidate.get("free_upload_slots"),
        "queue_length": _int("queue_length"),
        "upload_speed": _float("upload_speed"),
    }


def failure_out(payload: dict, stage: ItemStage) -> dict | None:
    """Why this failed, in a form a client can render without reading the task log."""
    if stage not in (ItemStage.failed, ItemStage.retrying):
        return None
    tried = payload.get("failed_candidates")
    try:
        retry_count = int(payload.get("retry_count") or 0)
    except (TypeError, ValueError):
        retry_count = 0
    exhausted = bool(payload.get("auto_retry_exhausted"))
    return {
        "reason": payload.get("retry_reason") or payload.get("failure_reason"),
        "retry_count": retry_count,
        "auto_retry_exhausted": exhausted,
        "retryable": True,
        "tried_candidates": len(tried) if isinstance(tried, (list, tuple)) else 0,
    }


def request_out(item: ProposalItem, payload: dict, requester_name: str | None = None) -> dict | None:
    """Who asked for this and what they asked for.  Prefers the real columns over the payload."""
    request = payload.get("request")
    request = request if isinstance(request, dict) else {}
    requester_id = item.requester_id or request.get("user_id") or payload.get("user_id")
    wishlist_item_id = (
        item.wishlist_item_id or request.get("wishlist_item_id") or payload.get("wishlist_item_id")
    )
    artist = request.get("artist") or payload.get("artist")
    album = request.get("album") or payload.get("album")
    track = request.get("track") or payload.get("track")
    if not any((requester_id, wishlist_item_id, artist, album, track)):
        return None
    return {
        "artist": artist,
        "album": album,
        "track": track,
        "wishlist_item_id": wishlist_item_id,
        "requester_id": requester_id,
        "requester_name": requester_name,
    }


def rollup_items(items: Iterable[ProposalItem]) -> list[ProposalItem]:
    """The items that actually represent work, for rollup purposes.

    ⚠️ Unselected alternate candidates are excluded. A download batch keeps roughly five candidates
    per track and selects one, so four out of five sit at `awaiting_approval` forever — and since
    that outranks `downloading` in the severity order, a batch that was actively transferring
    reported "awaiting approval" the whole time. They are options, not outstanding work.
    """
    materialized = list(items)
    working = [item for item in materialized if item.selected]
    return working or materialized


def library_review_title(items: Iterable[ProposalItem]) -> str:
    """The "Add to library: <artists>" title for a `library_review` batch, derived live.

    `ProposalBatch.title` is written once, at staging time (`present_staged_downloads_for_library_
    review`), and nothing rewrites it afterwards -- so it goes stale the moment an item leaves the
    batch (cancel, reject, partial approve, cleanup). This recomputes it from whatever items are
    passed in, the same way the bucket and stage are never stored either. Callers pass the batch's
    full item list for the batch-level title, or a requester's pruned subset (see
    `prune_batch_to_requester`) so a shared batch names only the artists in THAT view.

    Distinct artists come from the batch's artist-level container items -- root `import_files` rows
    with no parent, carrying `{"artist": ...}` in their payload -- the same shape
    `present_staged_downloads_for_library_review` builds them in.
    """
    artists: set[str] = set()
    for item in items:
        if item.parent_id is not None:
            continue
        artist = payload_of(item).get("artist")
        if isinstance(artist, str) and artist:
            artists.add(artist)
    distinct = sorted(a for a in artists if a != "Unknown Artist") or ["Unknown Artist"]
    label = ", ".join(distinct[:3]) + (" & more" if len(distinct) > 3 else "")
    return f"Add to library: {label}"


# Batch statuses that end the batch. Once one is set it beats anything its rows say — the same rule
# as `resolve_stage`, one level up.
_TERMINAL_BATCH_STAGE: dict[ProposalStatus, ItemStage] = {
    ProposalStatus.rejected: ItemStage.rejected,
    ProposalStatus.canceled: ItemStage.canceled,
    ProposalStatus.completed: ItemStage.completed,
}


def resolve_batch_stage(items: Iterable[ProposalItem], batch_status: ProposalStatus | None = None) -> ItemStage:
    """Worst-wins rollup over the items that represent real work.

    ⚠️ A terminal `batch_status` wins outright -- EXCEPT `completed`, which must still lose to a
    failed selected leaf. `run_execute_proposal_batch` used to write `completed` once every item
    result was "settled", even when settling meant failed, so a stored `completed` here is not
    trustworthy on its own: the same "a denormalized cache must never outrank the column it
    caches" rule applies one level up, with `status` playing the cache's role against the rows it
    summarizes. Rows can otherwise be left behind a settled batch (containers rejected before
    `_roll_up_container_status` existed still sit at `pending`), and rolling those up made a
    rejected batch report "awaiting approval" in Review.
    """
    stages = [resolve_stage(item) for item in rollup_items(items)]
    if batch_status is ProposalStatus.completed and ItemStage.failed in stages:
        return ItemStage.failed
    if batch_status in _TERMINAL_BATCH_STAGE:
        return _TERMINAL_BATCH_STAGE[batch_status]
    if not stages:
        return ItemStage.waiting
    present = set(stages)
    for stage in _STAGE_SEVERITY:
        if stage in present:
            return stage
    return ItemStage.waiting


def batch_progress(items: Iterable[ProposalItem], batch_status: ProposalStatus | None = None) -> dict:
    """Mean leaf progress plus the rolled-up stage.

    Read-only by contract: this runs at serialize time on a GET, and a GET must not write.
    """
    materialized = rollup_items(items)
    leaves = [item for item in materialized if not item.children] or materialized
    stage = resolve_batch_stage(materialized, batch_status)
    if not leaves:
        return {"value": 0.0, "label": _STAGE_DEFAULT_LABEL[stage], "indeterminate": False, "stage": stage}
    total = 0.0
    for item in leaves:
        data = payload_of(item)
        total += float(item_progress(item, resolve_stage(item, data), data)["value"])
    done = sum(1 for item in leaves if resolve_stage(item) is ItemStage.completed)
    return {
        "value": total / len(leaves),
        "label": f"{done} of {len(leaves)}",
        "indeterminate": stage in _INDETERMINATE_STAGES,
        "stage": stage,
    }


def batch_counts(items: Iterable[ProposalItem]) -> dict[str, int]:
    """Per-stage tallies, so a client can render "3 need you, 5 downloading" without walking the
    tree itself."""
    counts: dict[str, int] = {"total": 0}
    for item in items:
        counts["total"] += 1
        key = resolve_stage(item).value
        counts[key] = counts.get(key, 0) + 1
    return counts


def can_approve(stage: ItemStage, flow: ProposalFlow | str | None = None) -> bool:
    """Whether a human pressing Approve on THIS leaf would do something.

    Flow-dependent by necessity: gate (b) approves `staged` files, gate (a) never does.
    Containers do not use this -- see `container_can_approve`.
    """
    resolved = flow if isinstance(flow, ProposalFlow) else _coerce_flow(flow)
    allowed = (
        LIBRARY_REVIEW_APPROVABLE_STAGES
        if resolved is ProposalFlow.library_review
        else APPROVABLE_STAGES
    )
    return stage in allowed


def approvable_item_ids(items: Iterable[ProposalItem], flow: ProposalFlow | str | None) -> set[str]:
    """Every id a human could approve, containers resolved from their descendants.

    ⚠️ A container's own stage says nothing about whether there is work under it to approve. They
    used to report `can_approve=true` unconditionally (their stage rolls up to `awaiting_approval`),
    so a batch whose only real row was already downloading still lit up Approve -- and, the other
    way round, an artist row over a track still searching invited an approve that would do nothing.
    A container is approvable iff some actionable descendant of it is.
    """
    materialized = list(items)
    children: dict[str, list[ProposalItem]] = {}
    for item in materialized:
        if item.parent_id:
            children.setdefault(item.parent_id, []).append(item)
    approvable: set[str] = set()

    def visit(item: ProposalItem, seen: frozenset[str]) -> bool:
        if item.id in seen:
            return False
        kids = children.get(item.id, [])
        # ⚠️ NOT `any(...)`: it short-circuits, and every child has to be visited for its own
        # answer. With `any`, the first approvable candidate under a track stopped the walk and its
        # four sibling candidates were reported unapprovable — so picking any alternate was refused.
        under = False
        for kid in kids:
            if visit(kid, seen | {item.id}):
                under = True
        payload = payload_of(item)
        mine = is_actionable(item, payload) and can_approve(resolve_stage(item, payload), flow)
        if under or mine:
            approvable.add(item.id)
        return under or mine

    # Anything whose parent is not in this list is a root here -- including in a requester's pruned
    # view, where the ancestor chain can be partial.
    by_id = {item.id: item for item in materialized}
    for item in materialized:
        if item.parent_id and item.parent_id in by_id:
            continue
        visit(item, frozenset())
    return approvable


def can_cancel(stage: ItemStage) -> bool:
    return stage in CANCELABLE_STAGES


def can_retry(stage: ItemStage, flow: ProposalFlow | str | None) -> bool:
    """Retry restarts a download, so it exists only on the download gate."""
    resolved = flow if isinstance(flow, ProposalFlow) else _coerce_flow(flow)
    return resolved is ProposalFlow.download_review and stage in (ItemStage.failed, ItemStage.canceled)
