import asyncio
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nudibranch.core.config import get_settings
from nudibranch.db.init import init_db
from nudibranch.db.models import Album, AppSetting, Artist, Playlist, PlaylistTrack, ProposalBatch, ProposalItem, ProposalKind, ProposalStatus, Task, TaskStatus, Track, User, WishlistItem
from nudibranch.db.session import SessionLocal
from nudibranch.services.imports import SUPPORTED_AUDIO_EXTENSIONS, discover_import_files, read_audio_metadata, safe_path_part, suggest_library_path, write_audio_metadata
from nudibranch.services.notifications import create_notification, deliver_apns_notifications
from nudibranch.services.metadata_lookup import clear_discover_art_cache, itunes_album_artwork, search_album_releases, lookup_album_tracks
from nudibranch.services.proposals import item_ids_with_descendants
from nudibranch.services.settings_store import integration_settings
from nudibranch.services.slskd import cancel_slskd_download, download_transfers, queue_slskd_download, search_slskd_detailed, transfer_state_category
from nudibranch.services.tasks import append_task_log, claim_next_task, complete_task, enqueue_task, fail_task, task_to_payload, update_task_progress


MAX_DOWNLOAD_AUTO_RETRIES = 5
ZERO_PROGRESS_RETRY_SECONDS = 90
STALLED_PROGRESS_RETRY_SECONDS = 150
MISSING_TRANSFER_RETRY_SECONDS = 45
RECENT_TRANSFER_DISAPPEARED_RETRY_SECONDS = 10
COMPLETED_MISSING_FILE_RETRY_SECONDS = 30
QUEUED_TRANSFER_RETRY_SECONDS = 45
REPLACEMENT_QUEUED_TRANSFER_RETRY_SECONDS = 30
DOWNLOAD_SCAN_INTERVAL_SECONDS = 3
REPLACEMENT_SEARCH_RETRY_SECONDS = 15
TRANSFER_COMPLETE_PERCENT = 99.5
DOWNLOAD_MANIFEST_ACTIVE_STATUSES = {"queued", "downloading", "retrying", "staged", "verifying", "verified", "failed"}
DOWNLOAD_MANIFEST_FINISHED_STATUSES = {"completed", "rejected", "rejected_removed"}
DOWNLOAD_MANIFEST_STAGING_STATUSES = {"staged", "verifying", "verified"}
DOWNLOAD_SLOT_STATUSES = {"queued", "downloading"}
DOWNLOAD_SLOT_STALE_SECONDS = 75
DOWNLOAD_SLOT_PENDING_RECORD_SECONDS = 30
MIN_SLSKD_TRACK_CONFIDENCE = 0.60
SLSKD_TRACK_SEARCH_WORKERS = 1
SLSKD_TRACK_QUERY_LIMIT = 6
SLSKD_ALBUM_SEARCH_TIMEOUT_SECONDS = 15
SLSKD_ALBUM_SEARCH_BUFFER_SECONDS = 8
SLSKD_ALBUM_SEARCH_POLL_INTERVAL = 1.0
LOSSLESS_AUDIO_EXTENSIONS = (".flac", ".wav", ".aiff", ".aif", ".alac")
DOWNLOAD_VERSION_WORDS = {
    "acapella",
    "acoustic",
    "clean",
    "demo",
    "edit",
    "instrumental",
    "karaoke",
    "live",
    "remaster",
    "remastered",
    "remix",
    "sped",
    "slowed",
}
JUNK_ARTIST_SEGMENTS = {"unknown", "unknown artist", "various artists", "various", "va", "soundtrack"}
TEXT_SEARCH_ALTERNATIVES: list[tuple[str, tuple[str, ...]]] = [
    ("&", ("and",)),
    ("@", ("at",)),
    ("#", ("number", "no")),
    ("%", ("percent",)),
    ("*", ("star",)),
    ("★", ("star",)),
    ("☆", ("star",)),
    ("♥", ("heart",)),
    ("+", ("plus",)),
    ("÷", ("divide", "division")),
    ("×", ("multiply", "times")),
    ("=", ("equals",)),
    ("°", ("degree",)),
    ("½", ("half",)),
    ("¼", ("quarter",)),
    ("¾", ("three quarters",)),
    ("$", ("dollar",)),
    ("€", ("euro",)),
    ("£", ("pound",)),
    ("¥", ("yen",)),
    ("∞", ("infinity",)),
    ("0", ("zero",)),
    ("1", ("one",)),
    ("2", ("two",)),
    ("3", ("three",)),
    ("4", ("four",)),
    ("5", ("five",)),
    ("6", ("six",)),
    ("7", ("seven",)),
    ("8", ("eight",)),
    ("9", ("nine",)),
    ("10", ("ten",)),
    ("11", ("eleven",)),
    ("12", ("twelve",)),
    ("13", ("thirteen",)),
    ("14", ("fourteen",)),
    ("15", ("fifteen",)),
    ("16", ("sixteen",)),
    ("17", ("seventeen",)),
    ("18", ("eighteen",)),
    ("19", ("nineteen",)),
    ("20", ("twenty",)),
    ("30", ("thirty",)),
    ("40", ("forty",)),
    ("50", ("fifty",)),
    ("60", ("sixty",)),
    ("70", ("seventy",)),
    ("80", ("eighty",)),
    ("90", ("ninety",)),
    ("100", ("one hundred", "hundred")),
]


def run_propose_import(session: Session, payload: dict, task: Task | None = None) -> dict:
    files = payload.get("files")
    if files is None:
        files = discover_import_files(payload.get("path"))
    download_requests = payload.get("download_requests") or []
    if not files and not download_requests:
        raise ValueError("No import files or downloads were selected")
    batch_title = "Import folder review"
    if download_requests and not files:
        batch_title = "Import/Add download review"
    elif download_requests:
        batch_title = "Import/Add review"
    elif files and all(str(file_info.get("path") or "").startswith(str(get_settings().downloads_path)) for file_info in files):
        batch_title = "Downloaded files review"
    batch_kind = ProposalKind.download if download_requests and not files else ProposalKind.import_files
    tree_path = "/task-queue" if batch_kind == ProposalKind.download else "/app/import"
    batch = ProposalBatch(title=batch_title, kind=batch_kind, tree_path=tree_path)
    session.add(batch)
    session.flush()

    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}

    for file_info in files:
        metadata = file_info["metadata"]
        artist = metadata.get("albumartist") or metadata.get("artist") or "Unknown Artist"
        album = metadata.get("album") or "Unknown Album"
        track_title = metadata.get("title") or file_info["relative_path"]
        artist_item = artist_items.get(artist)
        if not artist_item:
            artist_item = ProposalItem(
                batch_id=batch.id,
                title=artist,
                kind=ProposalKind.import_files,
                payload_json=json.dumps({"artist": artist}),
            )
            session.add(artist_item)
            session.flush()
            artist_items[artist] = artist_item

        album_key = (artist, album)
        album_item = album_items.get(album_key)
        if not album_item:
            album_item = ProposalItem(
                batch_id=batch.id,
                parent_id=artist_item.id,
                title=album,
                kind=ProposalKind.import_files,
                payload_json=json.dumps({"artist": artist, "album": album}),
            )
            session.add(album_item)
            session.flush()
            album_items[album_key] = album_item

        track_item = ProposalItem(
            batch_id=batch.id,
            parent_id=album_item.id,
            title=track_title,
            kind=ProposalKind.import_files,
            old_value=file_info["path"],
            new_value=file_info["suggested_library_path"],
            payload_json=json.dumps(file_info),
        )
        session.add(track_item)
        session.flush()
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=track_item.id,
                title=f"Write metadata for {track_title}",
                kind=ProposalKind.metadata,
                old_value=file_info["relative_path"],
                new_value=json.dumps(metadata),
                payload_json=json.dumps(
                    {
                        "source_path": file_info["path"],
                        "size_bytes": file_info["size_bytes"],
                        "mtime_ns": file_info["mtime_ns"],
                        "metadata": metadata,
                    }
                ),
            )
        )
    download_candidates = 0
    if download_requests:
        download_candidates = add_download_candidate_review_items(
            session,
            batch,
            download_requests,
            artist_items,
            album_items,
            task,
        )
    create_notification(
        session,
        title="Import review ready",
        body=f"{len(files)} files and {len(download_requests)} downloads with {download_candidates} candidates were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"batch_id": batch.id, "files": len(files), "downloads": len(download_requests), "download_candidates": download_candidates}


def add_download_candidate_review_items(
    session: Session,
    batch: ProposalBatch,
    download_requests: list[dict],
    artist_items: dict[str, ProposalItem],
    album_items: dict[tuple[str, str], ProposalItem],
    task: Task | None = None,
) -> int:
    parent_kind = ProposalKind.download if batch.kind == ProposalKind.download else ProposalKind.import_files
    grouped: dict[tuple[str, str], list[tuple[dict, ProposalItem]]] = {}
    for request in download_requests:
        artist = request.get("artist") or "Unknown Artist"
        album = request.get("album") or "Unknown Album"
        title = request.get("track") or request.get("title") or "Unknown Track"
        artist_item = artist_items.get(artist)
        if not artist_item:
            artist_item = ProposalItem(
                batch_id=batch.id,
                title=artist,
                kind=parent_kind,
                payload_json=json.dumps({"artist": artist, "status": "searching candidates"}),
            )
            session.add(artist_item)
            session.flush()
            artist_items[artist] = artist_item
        album_key = (artist, album)
        album_item = album_items.get(album_key)
        if not album_item:
            album_item = ProposalItem(
                batch_id=batch.id,
                parent_id=artist_item.id,
                title=album,
                kind=parent_kind,
                payload_json=json.dumps({"artist": artist, "album": album, "status": "searching candidates"}),
            )
            session.add(album_item)
            session.flush()
            album_items[album_key] = album_item
        track_item = ProposalItem(
            batch_id=batch.id,
            parent_id=album_item.id,
            title=title,
            kind=ProposalKind.download,
            old_value="download request",
            payload_json=json.dumps(
                {
                    "kind": "track",
                    "artist": artist,
                    "album": album,
                    "track": title,
                    "status": "searching candidates",
                }
            ),
        )
        session.add(track_item)
        session.flush()
        grouped.setdefault(album_key, []).append((normalize_download_request(request, artist, album, title), track_item.id, title))
    session.commit()

    total_tracks = max(1, len(download_requests))
    completed = 0
    candidates_added = 0
    for (artist, album), track_items in grouped.items():
        requests = [request for request, _track_item_id, _track_title in track_items]
        append_task_log(session, task, f"{artist} / {album}: searching album-level candidates for task queue review")
        folder_try_limit = slskd_album_folder_try_limit(integration_settings(session))
        pools = search_album_folder_pools(session, artist, album, requests, task, limit=folder_try_limit)
        if pools:
            append_task_log(session, task, f"{artist} / {album}: using {len(pools)} ranked album folder(s) for candidates")
        else:
            append_task_log(session, task, f"{artist} / {album}: no album folder candidates found; track searches skipped for album workflow", "warning")
        missing_track_jobs: list[tuple[dict, ProposalItem, str, int]] = []
        for request, track_item_id, track_title in track_items:
            track_item = session.get(ProposalItem, track_item_id)
            if not track_item:
                append_task_log(session, task, f"{track_title}: skipped candidate preparation because the review row was removed", "warning")
                completed += 1
                continue
            query = download_query(request)
            candidates = candidates_from_folder_pools(pools, request, limit=5, max_pools=folder_try_limit)
            if candidates:
                add_download_candidate_items(session, batch, track_item, request, query, candidates)
                candidates_added += len(candidates)
                set_item_payload_status(track_item, f"{len(candidates)} candidates ready")
                append_task_log(session, task, f"{track_title}: {len(candidates)} album-folder candidate(s) ready after trying up to {folder_try_limit} folder(s)")
            elif pools:
                set_item_payload_status(track_item, "no album-folder track match")
                add_ytdlp_fallback_item(session, batch, track_item, request, query, selected=False)
                append_task_log(session, task, f"{track_title}: no lossless track match in {min(len(pools), folder_try_limit)} album folder(s); YouTube fallback left unselected", "warning")
            elif should_use_track_search_fallback(album, requests):
                set_item_payload_status(track_item, "searching track candidates")
                missing_track_jobs.append((request, track_item, query, 5))
            else:
                set_item_payload_status(track_item, "no album folder candidates found")
                add_ytdlp_fallback_item(session, batch, track_item, request, query, selected=False)
                append_task_log(session, task, f"{track_title}: no matching lossless album folders found; YouTube fallback left unselected", "warning")
            completed += 1
            if task is not None:
                update_task_progress(session, task, completed, total_tracks, f"Prepared candidates for {track_title}")
        if missing_track_jobs:
            candidates_added += add_track_search_candidate_items(session, batch, missing_track_jobs, task)
        if (artist, album) in album_items:
            set_item_payload_status(album_items[(artist, album)], "candidate review ready")
        if artist in artist_items:
            set_item_payload_status(artist_items[artist], "candidate review ready")
        session.commit()
    session.flush()
    return candidates_added


def normalize_download_request(request: dict, artist: str, album: str, title: str) -> dict:
    return {
        **request,
        "artist": artist,
        "album": album,
        "track": title,
        "track_number": request.get("track_number"),
        "disc_number": request.get("disc_number"),
        "duration_ms": request.get("duration_ms") or request.get("length"),
        "musicbrainz_album_id": request.get("musicbrainz_album_id"),
        "musicbrainz_recording_id": request.get("musicbrainz_recording_id"),
        "replace_track_id": request.get("replace_track_id"),
        "replace_path": request.get("replace_path"),
        "require_lossless": request.get("require_lossless"),
        "workflow": request.get("workflow"),
    }


def add_track_search_candidate_items(
    session: Session,
    batch: ProposalBatch,
    jobs: list[tuple[dict, ProposalItem, str, int]],
    task: Task | None = None,
) -> int:
    if not jobs:
        return 0
    settings = integration_settings(session)
    slskd_url = settings.get("slskd_url", "")
    api_key = settings.get("slskd_api_key", "")
    added = 0
    prepared_jobs = []
    for request, track_item, query, limit in jobs:
        payload = json.loads(track_item.payload_json or "{}")
        track_title = payload.get("track") or track_item.__dict__.get("title") or download_query(request)
        prepared_jobs.append((request, track_item.id, track_title, query, limit))
    workers = min(SLSKD_TRACK_SEARCH_WORKERS, len(prepared_jobs))
    append_task_log(session, task, f"Searching {len(prepared_jobs)} track candidate set(s) with {workers} worker(s)")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(search_slskd_for_request_with_settings, slskd_url, api_key, {**request, "multiple_candidates": True}, limit): (request, track_item_id, track_title, query)
            for request, track_item_id, track_title, query, limit in prepared_jobs
        }
        for future in as_completed(futures):
            request, track_item_id, track_title, query = futures[future]
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001 - one failed search should leave the rest of the review usable.
                result = {"candidates": [], "diagnostics": {"query_logs": [f"slskd track search failed for {query}: {error}"]}}
            for line in result.get("diagnostics", {}).get("query_logs") or []:
                append_task_log(session, task, line)
            track_item = session.get(ProposalItem, track_item_id)
            if not track_item:
                append_task_log(session, task, f"{track_title}: skipped candidate results because the review row was removed", "warning")
                continue
            candidates = result.get("candidates") or []
            if candidates:
                add_download_candidate_items(session, batch, track_item, request, query, candidates[:5])
                added += len(candidates[:5])
                set_item_payload_status(track_item, f"{len(candidates[:5])} candidates ready")
                append_task_log(session, task, f"{track_title}: {len(candidates[:5])} track candidate(s) ready")
            else:
                rate_limited = bool(result.get("diagnostics", {}).get("rate_limited"))
                status = "slskd rate limited; no candidate yet" if rate_limited else "no slskd candidates found"
                set_item_payload_status(track_item, status)
                if rate_limited:
                    append_task_log(session, task, f"{track_title}: {status}", "warning")
                else:
                    add_ytdlp_fallback_item(session, batch, track_item, request, query, selected=False)
                    append_task_log(session, task, f"{track_title}: no slskd candidates found; YouTube fallback left unselected", "warning")
            session.commit()
    return added


def should_use_track_search_fallback(album: str, requests: list[dict]) -> bool:
    if any(request.get("workflow") in {"missing_tracks", "lossless_replacement"} for request in requests):
        return False
    if any(request.get("require_lossless") for request in requests) and len(requests) > 1:
        return False
    normalized_album = fuzzy_text(album)
    if normalized_album in {"", "singles", "unknown album", "unknown"}:
        return True
    return len(requests) <= 1


def run_execute_proposal_batch(session: Session, payload: dict, task: Task | None = None) -> dict:
    batch_id = payload["batch_id"]
    batch = session.get(ProposalBatch, batch_id)
    if not batch:
        raise ValueError("Proposal batch not found")
    batch.status = ProposalStatus.executing
    append_task_log(session, task, f"Executing proposal batch {batch.title} ({batch.id})")
    approved_roots = {item.id for item in batch.items if item.selected and item.status == ProposalStatus.approved}
    if approved_roots:
        approved_ids = item_ids_with_descendants(batch.items, approved_roots)
        for item in batch.items:
            if item.id in approved_ids and item.selected and item.status in {ProposalStatus.pending, ProposalStatus.failed}:
                item.status = ProposalStatus.approved
        append_task_log(session, task, f"Expanded {len(approved_roots)} approved selections to {len(approved_ids)} selected descendants")
    selected_items = [
        item
        for item in batch.items
        if item.selected and item.status == ProposalStatus.approved
    ]
    imported = 0
    skipped = 0
    errors: list[str] = []
    executable_items = [
        item
        for item in selected_items
        if item.kind == ProposalKind.import_files and item.old_value and item.new_value
    ]
    metadata_items = [
        item
        for item in selected_items
        if item.kind == ProposalKind.metadata and json.loads(item.payload_json or "{}").get("target_type")
    ]
    file_action_items = [
        item
        for item in selected_items
        if item.kind in {ProposalKind.delete, ProposalKind.file_move} and json.loads(item.payload_json or "{}").get("action")
    ]
    playlist_items = [
        item
        for item in selected_items
        if item.kind == ProposalKind.playlist and json.loads(item.payload_json or "{}").get("action")
    ]
    download_items = [
        item
        for item in selected_items
        if item.kind == ProposalKind.download and json.loads(item.payload_json or "{}").get("action")
    ]
    wishlist_download_items = [
        item for item in download_items if json.loads(item.payload_json or "{}").get("action") == "wishlist_request"
    ]
    direct_download_items = [
        item for item in download_items if json.loads(item.payload_json or "{}").get("action") != "wishlist_request"
    ]
    if direct_download_items:
        batch.kind = ProposalKind.download
        batch.tree_path = "/downloads"
        append_task_log(session, task, f"{batch.title}: selected candidates moved to Downloads for transfer and verification")
    lyrics_items = [
        item
        for item in selected_items
        if item.kind == ProposalKind.lyrics and json.loads(item.payload_json or "{}").get("action")
    ]
    progress_items = executable_items + metadata_items + file_action_items + playlist_items + wishlist_download_items[:1] + direct_download_items + lyrics_items
    progress_total = max(1, len(progress_items))
    progress_current = 0
    append_task_log(
        session,
        task,
        (
            f"Selected work: {len(executable_items)} imports, {len(metadata_items)} metadata changes, "
            f"{len(file_action_items)} file actions, {len(playlist_items)} playlist changes, "
            f"{len(wishlist_download_items)} wishlist download requests, {len(direct_download_items)} direct downloads, "
            f"{len(lyrics_items)} lyric actions"
        ),
    )

    def note_progress(message: str, item: ProposalItem | None = None) -> None:
        nonlocal progress_current
        if item is not None:
            item.status = ProposalStatus.executing
        if task is not None:
            update_task_progress(session, task, min(progress_current, progress_total), progress_total, message)
        else:
            session.commit()

    def finish_progress_step(message: str) -> None:
        nonlocal progress_current
        progress_current += 1
        if task is not None:
            update_task_progress(session, task, min(progress_current, progress_total), progress_total, message)
        else:
            session.commit()

    note_progress(f"Preparing {len(progress_items)} selected changes")

    if not progress_items:
        errors.append("No approved executable changes were selected.")
        append_task_log(session, task, "No executable selected changes were found in the approved batch", "warning")
    elif batch.kind == ProposalKind.import_files and not executable_items and not download_items:
        errors.append("No approved import file operations were selected.")

    # Import wizard files: import a whole album together and commit once per album so tracks are
    # not added to the library one at a time.
    executable_albums: dict[str, list[ProposalItem]] = {}
    for item in executable_items:
        executable_albums.setdefault(str(Path(item.new_value).parent), []).append(item)
    for album_path, album_items in executable_albums.items():
        album_label = Path(album_path).name or "album"
        note_progress(f"Importing {len(album_items)} track(s) from {album_label}")
        album_imported = 0
        for item in album_items:
            try:
                import_track_item(session, item)
                item.status = ProposalStatus.completed
                imported += 1
                album_imported += 1
            except Exception as error:  # noqa: BLE001 - keep importing the rest of the album.
                item.status = ProposalStatus.failed
                errors.append(f"{item.title}: {error}")
            progress_current += 1
        if task is not None:
            update_task_progress(session, task, min(progress_current, progress_total), progress_total, f"Imported {album_imported}/{len(album_items)} track(s) from {album_label}")
        else:
            session.commit()

    metadata_updated = 0
    for item in metadata_items:
        try:
            note_progress(f"Applying metadata for {item.title}", item)
            apply_metadata_item(session, item)
            item.status = ProposalStatus.completed
            metadata_updated += 1
            finish_progress_step(f"Updated metadata for {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")
            finish_progress_step(f"Metadata failed for {item.title}")

    file_actions = 0
    for item in file_action_items:
        try:
            note_progress(f"Handling file action for {item.title}", item)
            apply_file_action_item(session, item)
            item.status = ProposalStatus.completed
            file_actions += 1
            finish_progress_step(f"Handled file action for {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")
            finish_progress_step(f"File action failed for {item.title}")

    playlist_changes = 0
    for item in playlist_items:
        try:
            note_progress(f"Updating playlist item {item.title}", item)
            apply_playlist_item(session, item)
            item.status = ProposalStatus.completed
            playlist_changes += 1
            finish_progress_step(f"Updated playlist item {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")
            finish_progress_step(f"Playlist update failed for {item.title}")

    download_changes = 0
    download_errors: list[str] = []
    if wishlist_download_items:
        try:
            note_progress("Preparing download candidates", wishlist_download_items[0])
            process_wishlist_request_items(session, wishlist_download_items, task)
            for wishlist_item in wishlist_download_items:
                wishlist_item.status = ProposalStatus.completed
            download_changes += len(wishlist_download_items)
            finish_progress_step("Download candidates created")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            for wishlist_item in wishlist_download_items:
                wishlist_item.status = ProposalStatus.failed
            errors.append(f"Download search: {error}")
            finish_progress_step("Download candidate search failed")

    download_slot_tracker = {"available": download_slots_available(session, load_download_manifest())}
    for item in direct_download_items:
        try:
            is_slskd_download = keeps_download_batch_open(item)
            if is_slskd_download and not download_slot_available(download_slot_tracker):
                note_progress(f"Waiting for download slot for {item.title}", item)
                set_download_item_status(item, "waiting to download")
                item.status = ProposalStatus.executing
                download_changes += 1
                finish_progress_step(f"Waiting for download slot for {item.title}")
                continue
            note_progress(f"Queueing download for {item.title}", item)
            apply_download_item(session, item, task)
            item.status = ProposalStatus.executing if keeps_download_batch_open(item) else ProposalStatus.completed
            download_changes += 1
            if is_slskd_download:
                consume_download_slot(download_slot_tracker)
            finish_progress_step(f"Queued download for {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            failed_request = download_request_from_item(item)
            retry_started = False
            if failed_request:
                payload = json.loads(item.payload_json or "{}")
                failed_candidate = payload.get("candidate") or {}
                retry_started = retry_download_entry(
                    session,
                    batch,
                    {
                        "batch_id": item.batch_id,
                        "item_id": item.id,
                        "parent_id": item.parent_id,
                        "request": failed_request,
                        "candidate": failed_candidate,
                        "basename": remote_basename(failed_candidate.get("filename") or ""),
                        "queued_at": datetime.now(timezone.utc).isoformat(),
                    },
                    item,
                    f"candidate queue failed: {error}",
                    available_slots=download_slot_tracker,
                )
            if retry_started:
                download_changes += 1
            else:
                item.status = ProposalStatus.failed
                download_errors.append(f"{item.title}: {error}")
            finish_progress_step(f"Download needs attention for {item.title}")

    lyric_changes = 0
    for item in lyrics_items:
        try:
            note_progress(f"Downloading lyrics for {item.title}", item)
            apply_lyrics_item(session, item)
            item.status = ProposalStatus.completed
            lyric_changes += 1
            finish_progress_step(f"Downloaded lyrics for {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")
            finish_progress_step(f"Lyrics failed for {item.title}")

    for item in batch.items:
        if not errors and item.status == ProposalStatus.approved:
            item.status = ProposalStatus.completed
        elif not item.selected and item.status == ProposalStatus.pending and should_count_skipped_item(item):
            skipped += 1

    # Import-wizard files are imported directly above (not via the download manifest), so trigger
    # a Jellyfin rescan for them here. Download imports queue their own rescan as they complete.
    if imported:
        append_task_log(session, task, f"Imported {imported} file(s) into the library; queueing Jellyfin scan")
        enqueue_task(session, "jellyfin_scan", {})
    downloaded_import = import_completed_downloads(session)
    open_downloads = batch_has_open_downloads(batch)

    if not errors and not open_downloads:
        # Clear leftover grouping rows (artist/album containers) once the batch has executed, so a
        # spent candidate review doesn't linger in the task queue showing empty artist/album rows.
        for item in batch.items:
            if item.status in {ProposalStatus.pending, ProposalStatus.executing} and not json.loads(item.payload_json or "{}").get("action"):
                item.status = ProposalStatus.completed

    if errors:
        batch.status = ProposalStatus.failed
    elif open_downloads:
        batch.status = ProposalStatus.executing
    elif all(item.status in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.failed} or not item.selected for item in batch.items):
        batch.status = ProposalStatus.completed
    else:
        batch.status = ProposalStatus.pending
    session.commit()

    create_notification(
        session,
        title="Task queue item failed" if errors else "Downloads queued" if open_downloads else "Task queue item completed",
        body=task_queue_notification_body(
            imported,
            metadata_updated,
            file_actions,
            playlist_changes,
            download_changes,
            lyric_changes,
            skipped,
            errors,
            download_errors,
            downloaded_import,
            open_downloads,
        ),
        event_type="task_completed",
        target_url="/downloads" if open_downloads else "/activity",
    )
    return {
        "batch_id": batch_id,
        "imported": imported,
        "metadata_updated": metadata_updated,
        "file_actions": file_actions,
        "playlist_changes": playlist_changes,
        "download_changes": download_changes,
        "lyric_changes": lyric_changes,
        "skipped": skipped,
        "errors": errors,
        "download_errors": download_errors,
        "downloaded_import": downloaded_import,
        "open_downloads": open_downloads,
    }


def task_queue_notification_body(
    imported: int,
    metadata_updated: int,
    file_actions: int,
    playlist_changes: int,
    download_changes: int,
    lyric_changes: int,
    skipped: int,
    errors: list[str],
    download_errors: list[str] | None = None,
    downloaded_import: dict | None = None,
    open_downloads: bool = False,
) -> str:
    parts = [
        f"{imported} tracks imported",
        f"{metadata_updated} metadata changes applied",
        f"{file_actions} files handled",
        f"{playlist_changes} playlist changes applied",
        f"{download_changes} downloads handled",
        f"{lyric_changes} lyrics downloaded",
    ]
    if skipped:
        parts.append(f"{skipped} items skipped")
    if downloaded_import and downloaded_import.get("imported"):
        parts.append(f"{downloaded_import['imported']} downloaded files added to the library")
    if open_downloads:
        parts.append("downloads are running and will move to the library after verification")
    body = ". ".join(parts) + "."
    if errors:
        return f"{body} First failure: {errors[0]}"
    if download_errors:
        return f"{body} {len(download_errors)} downloads need another candidate in Downloads. First download issue: {trim_message(download_errors[0])}"
    return body


def trim_message(message: str, limit: int = 700) -> str:
    if len(message) <= limit:
        return message
    return f"{message[:limit].rstrip()}..."


def should_count_skipped_item(item: ProposalItem) -> bool:
    payload = json.loads(item.payload_json or "{}")
    action = payload.get("action")
    if item.kind == ProposalKind.download and action in {"queue_download", "queue_ytdlp_download"}:
        return False
    if item.kind == ProposalKind.download and not action:
        return False
    return True


def keeps_download_batch_open(item: ProposalItem) -> bool:
    payload = json.loads(item.payload_json or "{}")
    return item.kind == ProposalKind.download and payload.get("action") == "queue_download"


def batch_has_open_downloads(batch: ProposalBatch) -> bool:
    return any(keeps_download_batch_open(item) and item.status == ProposalStatus.executing for item in batch.items)


def import_track_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    replace_track_id = payload.get("replace_track_id")
    if replace_track_id:
        track = session.get(Track, replace_track_id)
        if track:
            replace_library_track_file(session, track, Path(item.old_value), Path(item.new_value), payload)
            return
    import_file_to_library(session, Path(item.old_value), Path(item.new_value), payload)


def find_library_track(session: Session, artist_name: str, album_title: str, title: str):
    """Return an existing library Track with the same artist + album + title, else None.

    Matching is case/whitespace-insensitive. A track is only a duplicate when all three match, so
    the same song on a different album is never treated as a duplicate.
    """
    artist_key = normalize_match_text(artist_name)
    album_key = normalize_match_text(album_title)
    title_key = normalize_match_text(title)
    if not (artist_key and album_key and title_key):
        return None
    candidates = session.scalars(
        select(Track)
        .join(Album, Album.id == Track.album_id)
        .join(Artist, Artist.id == Album.artist_id)
        .where(func.lower(Album.title) == album_title.lower())
        .options(selectinload(Track.album).selectinload(Album.artist))
    )
    for track in candidates:
        album = track.album
        if not album or not album.artist:
            continue
        if (
            normalize_match_text(album.artist.name) == artist_key
            and normalize_match_text(album.title) == album_key
            and normalize_match_text(track.title) == title_key
        ):
            return track
    return None


def import_file_to_library(session: Session, source_path: Path, target_path: Path, payload: dict) -> None:
    metadata = payload.get("metadata", {})
    if not source_path.exists():
        raise FileNotFoundError(f"{source_path} no longer exists")

    stat = source_path.stat()
    if payload.get("size_bytes") and stat.st_size != payload["size_bytes"]:
        raise ValueError("source file size changed after review")

    create_record_only = payload.get("action") == "create_library_record"
    artist_name = metadata.get("albumartist") or metadata.get("artist") or "Unknown Artist"
    album_title = metadata.get("album") or "Unknown Album"
    track_title = metadata.get("title") or target_path.stem

    # Never create a 100%-certain duplicate: same artist + album + title already in the library.
    # (The same title on a different album is fine — albums are matched too.) This guards every
    # import/download path. Leave the source file in place so nothing is silently lost.
    duplicate = find_library_track(session, artist_name, album_title, track_title)
    if duplicate and str(duplicate.path or "") != str(target_path):
        append_task_log(session, None, f"{track_title}: already in {artist_name} / {album_title}; skipping duplicate import")
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and not create_record_only:
        raise FileExistsError(f"{target_path} already exists")

    if not create_record_only:
        shutil.move(str(source_path), str(target_path))

    artist = session.scalar(select(Artist).where(Artist.name == artist_name))
    if not artist:
        artist = Artist(name=artist_name)
        session.add(artist)
        session.flush()
    if metadata.get("musicbrainz_artist_id"):
        artist.musicbrainz_id = metadata.get("musicbrainz_artist_id")

    album = session.scalar(select(Album).where(Album.artist_id == artist.id, Album.title == album_title))
    if not album:
        album = Album(
            artist_id=artist.id,
            title=album_title,
            path=str(target_path.parent),
            musicbrainz_release_id=metadata.get("musicbrainz_album_id"),
        )
        session.add(album)
        session.flush()
    elif metadata.get("musicbrainz_album_id"):
        album.musicbrainz_release_id = metadata.get("musicbrainz_album_id")

    existing_track = session.scalar(select(Track).where(Track.path == str(target_path)))
    if existing_track:
        return

    session.add(
        Track(
            album_id=album.id,
            title=track_title,
            track_number=metadata.get("track_number"),
            disc_number=metadata.get("disc_number"),
            duration_ms=metadata.get("duration_ms"),
            format=metadata.get("format"),
            bitrate=metadata.get("bitrate"),
            path=str(target_path),
            musicbrainz_recording_id=metadata.get("musicbrainz_recording_id"),
            is_lossless=metadata.get("is_lossless", False),
            musicbrainz_verified=bool(metadata.get("musicbrainz_verified")),
        )
    )


def apply_metadata_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    changes = payload.get("changes") or {}
    if target_type == "artist":
        target = session.get(Artist, target_id)
        if not target:
            raise ValueError("Artist no longer exists")
        apply_artist_changes(session, target, changes)
    elif target_type == "album":
        target = session.get(Album, target_id)
        if not target:
            raise ValueError("Album no longer exists")
        apply_album_changes(session, target, changes)
    elif target_type == "track":
        target = session.get(Track, target_id)
        if not target:
            raise ValueError("Track no longer exists")
        apply_scalar_changes(target, changes, editable_track_fields())
    else:
        raise ValueError("Unsupported metadata target")


def apply_playlist_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    action = payload.get("action")
    if action == "set_position":
        entry = session.get(PlaylistTrack, payload.get("playlist_track_id"))
        if not entry:
            raise ValueError("Playlist entry no longer exists")
        entry.position = int(payload.get("position"))
    elif action == "rename_playlist":
        playlist = session.get(Playlist, payload.get("playlist_id"))
        if not playlist:
            raise ValueError("Playlist no longer exists")
        if playlist.protected:
            raise ValueError("Favorites cannot be renamed")
        playlist.name = str(payload.get("name") or "").strip()
    elif action == "delete_playlist":
        playlist = session.get(Playlist, payload.get("playlist_id"))
        if not playlist:
            raise ValueError("Playlist no longer exists")
        if playlist.protected:
            raise ValueError("Favorites cannot be deleted")
        session.delete(playlist)
    else:
        raise ValueError("Unsupported playlist action")
    enqueue_task(session, "sync_favorites_jellyfin", {})


def apply_download_item(session: Session, item: ProposalItem, task: Task | None = None) -> None:
    payload = json.loads(item.payload_json or "{}")
    action = payload.get("action")
    if action == "wishlist_request":
        append_task_log(session, task, f"Creating download candidates for wishlist request {item.title}")
        create_download_candidate_batch(session, payload)
        return
    if action == "queue_download":
        settings = integration_settings(session)
        append_task_log(session, task, f"Queueing slskd download for {item.title}")
        queue_slskd_download_with_candidate_fallbacks(session, item, settings.get("slskd_url", ""), settings.get("slskd_api_key", ""), task)
        return
    if action == "queue_ytdlp_download":
        append_task_log(session, task, f"Queueing YouTube fallback for {item.title}", "warning")
        queue_ytdlp_download(session, payload.get("request") or {})
        return
    raise ValueError("Unsupported download action")


def queue_slskd_download_with_candidate_fallbacks(session: Session, item: ProposalItem, slskd_url: str, api_key: str, task: Task | None = None) -> dict:
    attempts: list[str] = []
    candidate_items = download_candidate_attempt_order(session, item)
    append_task_log(session, task, f"{item.title}: trying {len(candidate_items)} slskd candidate(s)")
    for candidate_item in candidate_items:
        payload = json.loads(candidate_item.payload_json or "{}")
        candidate = payload.get("candidate") or {}
        request = payload.get("request") or {}
        label = candidate.get("filename") or candidate_item.title
        try:
            append_task_log(session, task, f"{item.title}: queueing candidate {label}")
            result = queue_slskd_download(slskd_url, api_key, candidate)
            record_download_manifest_entry(request, candidate, item)
            set_download_item_status(item, "queued in slskd; waiting for transfer state")
            if candidate_item.id != item.id:
                candidate_item.status = ProposalStatus.executing
                set_download_item_status(candidate_item, "queued in slskd; waiting for transfer state")
                item.title = candidate_item.title
                item.new_value = candidate_item.new_value
                item.payload_json = candidate_item.payload_json
            append_task_log(session, task, f"{item.title}: slskd accepted candidate {label}; waiting for transfer progress")
            return result
        except Exception as error:  # noqa: BLE001 - try the next available candidate for this track.
            attempts.append(f"{label}: {error}")
            candidate_item.status = ProposalStatus.failed
            append_task_log(session, task, f"{item.title}: slskd candidate failed: {label}: {error}", "warning")
    raise RuntimeError("All slskd candidates failed. " + " | ".join(attempts))


def download_candidate_attempt_order(session: Session, item: ProposalItem) -> list[ProposalItem]:
    payload = json.loads(item.payload_json or "{}")
    if payload.get("action") != "queue_download":
        return [item]
    siblings = list(
        session.scalars(
            select(ProposalItem)
            .where(ProposalItem.batch_id == item.batch_id)
            .where(ProposalItem.parent_id == item.parent_id)
            .where(ProposalItem.kind == ProposalKind.download)
        )
    )
    candidates = [
        candidate
        for candidate in siblings
        if json.loads(candidate.payload_json or "{}").get("action") == "queue_download"
    ]
    if not candidates:
        return [item]

    def candidate_sort_key(candidate: ProposalItem) -> tuple[int, str]:
        if candidate.id == item.id:
            return (0, candidate.id)
        if candidate.selected:
            return (1, candidate.id)
        return (2, candidate.id)

    return sorted(candidates, key=candidate_sort_key)


def existing_retry_candidate_items(session: Session, item: ProposalItem, failed_candidates: list[dict]) -> list[ProposalItem]:
    if not item.parent_id:
        return []
    ignored = {candidate_identity(candidate) for candidate in failed_candidates if isinstance(candidate, dict)}
    candidates = []
    for candidate_item in session.scalars(
        select(ProposalItem)
        .where(ProposalItem.batch_id == item.batch_id)
        .where(ProposalItem.parent_id == item.parent_id)
        .where(ProposalItem.kind == ProposalKind.download)
    ):
        payload = json.loads(candidate_item.payload_json or "{}")
        if payload.get("action") != "queue_download":
            continue
        candidate = payload.get("candidate") or {}
        if candidate_identity(candidate) in ignored:
            continue
        candidates.append(candidate_item)

    def retry_sort_key(candidate_item: ProposalItem) -> tuple[int, int, str]:
        payload = json.loads(candidate_item.payload_json or "{}")
        try:
            rank = int(payload.get("candidate_index", 9999))
        except (TypeError, ValueError):
            rank = 9999
        selected_penalty = 0 if candidate_item.selected else 1
        return (selected_penalty, rank, candidate_item.id)

    return sorted(candidates, key=retry_sort_key)


def download_request_from_item(item: ProposalItem) -> dict | None:
    payload = json.loads(item.payload_json or "{}")
    request = payload.get("request")
    if isinstance(request, dict):
        return request
    candidate = payload.get("candidate") or {}
    if payload.get("action") == "queue_ytdlp_download":
        return None
    parent = item.parent
    if parent:
        parent_payload = json.loads(parent.payload_json or "{}")
        artist = parent_payload.get("artist")
        album = parent_payload.get("album")
        track = parent_payload.get("track") or parent.title
        if artist or album or track:
            return {"artist": artist, "album": album, "track": track}
    if candidate.get("query"):
        return {"track": candidate["query"]}
    return None


def create_download_retry_import_batch(session: Session, requests: list[dict]) -> None:
    unique_requests: list[dict] = []
    seen = set()
    for request in requests:
        key = (
            str(request.get("artist") or "Unknown Artist").casefold(),
            str(request.get("album") or "Unknown Album").casefold(),
            str(request.get("track") or request.get("title") or "Unknown Track").casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_requests.append(request)
    if unique_requests:
        run_propose_import(session, {"files": [], "download_requests": unique_requests})


def download_manifest_path() -> Path:
    # Internal worker state — keep it on the local config volume, not the (NAS) downloads share,
    # so frequent rewrites never hit share permission/latency issues.
    return get_settings().config_path / ".nudibranch-downloads.json"


def legacy_download_manifest_path() -> Path:
    return get_settings().downloads_path / ".nudibranch-downloads.json"


def load_download_manifest() -> list[dict]:
    path = download_manifest_path()
    if not path.exists():
        # One-time migration from the old location in the downloads folder.
        legacy = legacy_download_manifest_path()
        if legacy.exists():
            try:
                payload = json.loads(legacy.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            if isinstance(payload, list):
                save_download_manifest(payload)
                return payload
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def save_download_manifest(entries: list[dict]) -> None:
    path = download_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def record_download_manifest_entry(request: dict, candidate: dict, item: ProposalItem) -> None:
    if not request or not candidate:
        return
    filename = str(candidate.get("filename") or "")
    entries = load_download_manifest()
    entries = [
        entry
        for entry in entries
        if not (
            entry.get("batch_id") == item.batch_id
            and entry.get("item_id") == item.id
            and entry.get("status") not in DOWNLOAD_MANIFEST_FINISHED_STATUSES
        )
    ]
    entries.append(
        {
            "batch_id": item.batch_id,
            "item_id": item.id,
            "parent_id": item.parent_id,
            "request": request,
            "candidate": {
                "username": candidate.get("username"),
                "filename": filename,
                "folder": candidate.get("folder"),
            },
            "basename": remote_basename(filename),
            "initialized_at": datetime.now(timezone.utc).isoformat(),
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "status": "queued",
        }
    )
    save_download_manifest(entries[-500:])


def remote_basename(filename: str) -> str:
    return str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()


def find_download_manifest_entry(file_path: Path) -> dict | None:
    basename = file_path.name.casefold()
    candidates = [
        entry
        for entry in load_download_manifest()
        if entry.get("status") in (DOWNLOAD_MANIFEST_ACTIVE_STATUSES | {"rejected"})
    ]
    for entry in candidates:
        if entry.get("basename") == basename:
            return entry
    normalized_path = str(file_path).replace("\\", "/").casefold()
    for entry in candidates:
        filename = str((entry.get("candidate") or {}).get("filename") or "").replace("\\", "/").casefold()
        if filename and normalized_path.endswith(filename):
            return entry
    return None


def update_download_manifest_entry(target: dict, status: str, **fields: object) -> None:
    entries = load_download_manifest()
    for entry in entries:
        if entry == target or same_manifest_entry(entry, target):
            if target.get("item_id"):
                entry["item_id"] = target["item_id"]
            if target.get("parent_id"):
                entry["parent_id"] = target["parent_id"]
            entry["status"] = status
            entry["status_changed_at"] = datetime.now(timezone.utc).isoformat()
            entry.update(fields)
            break
    save_download_manifest(entries)


def remove_download_manifest_entry(target: dict) -> None:
    save_download_manifest([entry for entry in load_download_manifest() if entry != target and not same_manifest_entry(entry, target)])


def same_manifest_entry(entry: dict, target: dict) -> bool:
    target_item_id = target.get("_original_item_id") or target.get("item_id")
    return bool(
        entry.get("batch_id") == target.get("batch_id")
        and entry.get("item_id") == target_item_id
        and entry.get("basename")
        and entry.get("basename") == target.get("basename")
    )


def download_staging_root(batch_id: str | None = None) -> Path:
    root = get_settings().staging_path / "downloads"
    return root / batch_id if batch_id else root


def stage_downloaded_file(session: Session, batch: ProposalBatch, entry: dict, file_path: Path) -> Path:
    if entry.get("status") in DOWNLOAD_MANIFEST_STAGING_STATUSES and entry.get("path"):
        staged = Path(str(entry["path"]))
        if staged.exists():
            return staged
    staging_root = download_staging_root(batch.id)
    staging_root.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(staging_root / file_path.name)
    append_task_log(session, None, f"{entry_download_label(entry)}: moving completed download to staging: {destination.name}")
    shutil.move(str(file_path), str(destination))
    update_download_manifest_entry(
        entry,
        "staged",
        path=str(destination),
        download_path=str(file_path),
        staged_at=datetime.now(timezone.utc).isoformat(),
    )
    entry.update({"status": "staged", "path": str(destination), "download_path": str(file_path)})
    item = session.get(ProposalItem, entry.get("item_id"))
    set_download_item_status(item, "downloaded; ready to add to library", stage="staging", progress=100)
    append_task_log(session, None, f"{entry_download_label(entry)}: moved to staging")
    return destination


def entry_download_label(entry: dict) -> str:
    request = entry.get("request") or {}
    candidate = entry.get("candidate") or {}
    return str(request.get("track") or request.get("title") or candidate.get("filename") or entry.get("basename") or "download")


def import_completed_downloads(session: Session, minimum_age_seconds: int = 5) -> dict:
    settings = get_settings()
    root = settings.downloads_path
    if not root.exists():
        return {"imported": 0, "errors": []}
    errors: list[str] = []
    manifest_result = import_manifest_download_batches(session, minimum_age_seconds)
    manifest_imported = manifest_result["imported"]
    errors.extend(manifest_result["errors"])
    manifest_waiting = manifest_result.get("waiting", 0)
    manifest_ready = manifest_result.get("ready", 0)
    manifest_failed = manifest_result.get("failed", 0)
    if manifest_imported:
        session.flush()
        create_notification(
            session,
            title="Downloaded album imported",
            body=f"{manifest_imported} tracks were added to the library.",
            event_type="tool_completed",
            target_url="/library",
        )
        append_task_log(session, None, f"Downloaded album import completed for {manifest_imported} track(s); queueing Jellyfin scan")
        enqueue_task(session, "jellyfin_scan", {})
        return {"imported": manifest_imported, "errors": errors, "waiting": manifest_waiting, "ready": manifest_ready, "failed": manifest_failed}
    if manifest_waiting or manifest_ready or manifest_failed:
        return {"imported": 0, "errors": errors, "waiting": manifest_waiting, "ready": manifest_ready, "failed": manifest_failed}
    imported = 0
    now = time.time()
    known_paths = existing_library_and_proposal_paths(session)
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        if str(file_path) in known_paths:
            continue
        stat = file_path.stat()
        if now - stat.st_mtime < minimum_age_seconds:
            continue
        metadata = read_audio_metadata(file_path)
        manifest_entry = find_download_manifest_entry(file_path)
        if manifest_entry:
            if manifest_entry.get("status") == "rejected":
                try:
                    file_path.unlink()
                    update_download_manifest_entry(manifest_entry, "rejected_removed")
                except OSError as error:
                    errors.append(f"{file_path.name}: failed to remove rejected download: {error}")
            continue
        payload = {
            "path": str(file_path),
            "relative_path": str(file_path.relative_to(root)),
            "extension": file_path.suffix.lower(),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "metadata": metadata,
            "suggested_library_path": str(suggest_library_path(metadata, file_path)),
        }
        target_path = Path(payload["suggested_library_path"])
        if str(target_path) in known_paths or target_path.exists():
            continue
        try:
            import_file_to_library(session, file_path, target_path, payload)
            mark_matching_wishlist_completed(session, metadata)
            imported += 1
        except Exception as error:  # noqa: BLE001 - keep sweeping independent finished downloads.
            errors.append(f"{file_path.name}: {error}")
    if imported:
        session.flush()
        create_notification(
            session,
            title="Downloaded files imported",
            body=f"{imported} files were added to the library.",
            event_type="tool_completed",
            target_url="/library",
        )
        append_task_log(session, None, f"Downloaded import completed for {imported} file(s); queueing Jellyfin scan")
        enqueue_task(session, "jellyfin_scan", {})
    return {"imported": imported, "errors": errors, "waiting": manifest_waiting, "ready": manifest_ready, "failed": manifest_failed}


def import_manifest_download_batches(session: Session, minimum_age_seconds: int) -> dict:
    entries_by_batch: dict[str, list[dict]] = {}
    for entry in load_download_manifest():
        batch_id = entry.get("batch_id")
        if batch_id and entry.get("status") in DOWNLOAD_MANIFEST_ACTIVE_STATUSES:
            entries_by_batch.setdefault(batch_id, []).append(entry)
    for batch in session.scalars(
        select(ProposalBatch)
        .where(ProposalBatch.status == ProposalStatus.executing)
        .where(ProposalBatch.tree_path == "/downloads")
    ):
        if batch.id not in entries_by_batch and selected_slskd_download_item_ids(batch):
            entries_by_batch[batch.id] = []
    imported = 0
    errors: list[str] = []
    waiting = 0
    ready = 0
    failed = 0
    for batch_id, entries in entries_by_batch.items():
        batch = session.get(ProposalBatch, batch_id)
        if not batch or batch.status not in {ProposalStatus.executing, ProposalStatus.failed, ProposalStatus.pending, ProposalStatus.approved}:
            continue
        result = process_download_manifest_batch(session, batch, entries, minimum_age_seconds)
        imported += result["imported"]
        errors.extend(result["errors"])
        waiting += result.get("waiting", 0)
        ready += result.get("ready", 0)
        failed += result.get("failed", 0)
    return {"imported": imported, "errors": errors, "waiting": waiting, "ready": ready, "failed": failed}


# Tracks the last import-failure message we notified per batch, so a persistent problem
# (e.g. a read-only library mount) doesn't raise a notification on every 3-second scan.
_reported_download_import_failures: dict[str, str] = {}


def describe_import_error(error: Exception) -> str:
    text = str(error)
    if isinstance(error, PermissionError) or "Errno 13" in text or "Permission denied" in text:
        return (
            f"permission denied writing to the library ({text}). "
            "The worker can read the library but cannot write into it — fix write permissions on "
            "the library mount (file ownership / the container's PUID:PGID, or the NAS share ACL)."
        )
    return text


def process_download_manifest_batch(session: Session, batch: ProposalBatch, entries: list[dict], minimum_age_seconds: int) -> dict:
    # Items that need manual attention (an exhausted download, a pending YouTube fallback) keep
    # the batch from being finalized, but they no longer short-circuit the whole batch: tracks
    # that already downloaded should still stage and import.
    blocking_items = selected_download_blockers(batch)
    for blocker in blocking_items:
        set_download_item_status(blocker, "needs attention before this batch can finish")
    all_download_item_ids = selected_slskd_download_item_ids(batch)
    if not all_download_item_ids:
        return {"imported": 0, "errors": []}
    # Tracks imported in an earlier incremental pass are done; don't reconsider or re-queue them.
    expected_item_ids = {
        item_id
        for item_id in all_download_item_ids
        if (existing_item := session.get(ProposalItem, item_id)) and existing_item.status not in {ProposalStatus.completed, ProposalStatus.rejected}
    }
    if not expected_item_ids:
        if not blocking_items:
            finalize_completed_download_batch(session, batch)
        return {"imported": 0, "errors": []}
    entries = reconcile_manifest_entries_to_selected_items(session, batch, entries, expected_item_ids)
    entries = [entry for entry in entries if entry.get("item_id") in expected_item_ids]
    entries = defer_excess_download_slot_entries(session, batch, entries)
    download_slot_tracker = {"available": download_slots_available(session, load_download_manifest())}
    manifest_item_ids = {entry.get("item_id") for entry in entries}

    staged_entries: list[tuple[dict, Path]] = []
    errors: list[str] = []
    waiting_count = 0
    ready_count = 0
    failed_count = 0

    # Re-queue selected items that have no manifest entry yet (deferred for a download slot,
    # missing a queue record, etc). This intentionally does NOT short-circuit staging and import
    # of the entries we already have, so a finished download still reaches the library while its
    # siblings are still waiting for a slot.
    for item_id in sorted(expected_item_ids - manifest_item_ids):
        item = session.get(ProposalItem, item_id)
        if download_item_retry_exhausted(item):
            set_download_item_status(item, "needs attention; could not be downloaded automatically")
            failed_count += 1
            continue
        if item and reconnect_existing_slskd_transfer(session, item):
            waiting_count += 1
            continue
        if not download_slot_available(download_slot_tracker):
            set_download_item_status(item, "waiting to download")
            waiting_count += 1
            continue
        if item and queue_missing_manifest_download(session, batch, item, available_slots=download_slot_tracker):
            waiting_count += 1
            continue
        set_download_item_status(item, "searching for slskd queue record")
        waiting_count += 1

    transfer_lookup, transfer_error_message = slskd_transfer_lookup(session, entries)
    if transfer_error_message:
        for entry in entries:
            set_download_item_status(session.get(ProposalItem, entry.get("item_id")), f"{transfer_error_message}; searching transfer state")
    for entry in entries:
        item = session.get(ProposalItem, entry.get("item_id"))
        if download_item_retry_exhausted(item):
            set_download_item_status(item, "needs attention; could not be downloaded automatically")
            failed_count += 1
            continue

        transfer = transfer_lookup.get(manifest_entry_key(entry))
        if entry.get("status") not in DOWNLOAD_MANIFEST_STAGING_STATUSES:
            apply_transfer_path_to_manifest(entry, transfer)

        file_path, wait_status = manifest_entry_file_path(entry, minimum_age_seconds)

        # Move a freshly-downloaded file into staging (no MusicBrainz verification — that now
        # happens via the manual add-to-library review below). Already-staged entries return
        # their staged path here and skip the move.
        if file_path and entry.get("status") not in DOWNLOAD_MANIFEST_STAGING_STATUSES:
            try:
                file_path = stage_downloaded_file(session, batch, entry, file_path)
            except Exception as error:  # noqa: BLE001 - keep the batch visible and retryable.
                failed_count += 1
                if item:
                    item.status = ProposalStatus.failed
                set_download_item_status(item, f"failed to move download to staging: {error}")
                update_download_manifest_entry(entry, "failed", retry_reason=f"staging failed: {error}")
                append_task_log(session, None, f"{entry_download_label(entry)}: failed to move download to staging: {error}", "error")
                continue

        if file_path:
            staged_entries.append((entry, file_path))
            ready_count += 1
            set_download_item_status(item, "downloaded; ready to add to library", stage="staging", progress=100)
            continue

        # Not downloaded yet — keep waiting on / retrying the transfer.
        retry_reason = None if wait_status.startswith("downloaded; settling") else download_retry_reason(entry, transfer)
        if item and retry_reason and not transfer_error_message:
            if retry_download_entry(session, batch, entry, item, retry_reason, transfer, available_slots=download_slot_tracker):
                waiting_count += 1
                continue
            failed_count += 1
            continue
        if transfer_error_message and not transfer:
            wait_status = f"{transfer_error_message}; {wait_status}"
        update_manifest_transfer_tracking(entry, transfer)
        progress_state = transfer_progress_state(entry, transfer, wait_status)
        set_download_item_status(
            item,
            progress_state["label"],
            stage=progress_state.get("stage"),
            progress=progress_state.get("progress"),
            indeterminate=progress_state.get("indeterminate"),
        )
        if transfer_is_failed(transfer) and item:
            item.status = ProposalStatus.failed
            failed_count += 1
        else:
            waiting_count += 1
        continue

    update_download_container_statuses(batch)
    session.flush()

    # Add the album to the library as a unit: wait until the whole batch has settled (nothing
    # still downloading or retrying) before presenting it, so tracks aren't reviewed piecemeal.
    if waiting_count > 0:
        for entry, _staged_path in staged_entries:
            set_download_item_status(session.get(ProposalItem, entry.get("item_id")), "downloaded; waiting for the rest of the album", stage="staging", progress=100)
        return {"imported": 0, "errors": errors, "waiting": waiting_count + ready_count, "ready": 0, "failed": failed_count}

    if not staged_entries:
        return {"imported": 0, "errors": errors, "waiting": waiting_count, "ready": ready_count, "failed": failed_count}

    # Downloads have settled. Instead of MusicBrainz auto-verification + auto-import, present a
    # review task the user approves to add the staged files to the library.
    try:
        present_staged_downloads_for_library_review(session, batch, staged_entries, finalize=(failed_count == 0 and not blocking_items))
    except Exception as error:  # noqa: BLE001 - keep the batch visible if the review can't be built.
        batch.status = ProposalStatus.failed
        message = describe_import_error(error)
        append_task_log(session, None, f"{batch.title}: could not prepare the add-to-library review: {message}", "error")
        if _reported_download_import_failures.get(batch.id) != message:
            _reported_download_import_failures[batch.id] = message
            create_notification(session, title="Download import failed", body=message, event_type="task_failed", target_url="/downloads")
        session.commit()
        return {"imported": 0, "errors": [message], "waiting": 0, "ready": ready_count, "failed": 1}
    _reported_download_import_failures.pop(batch.id, None)
    return {"imported": 0, "errors": errors, "waiting": 0, "ready": 0, "failed": failed_count}


def reconcile_manifest_entries_to_selected_items(session: Session, batch: ProposalBatch, entries: list[dict], expected_item_ids: set[str]) -> list[dict]:
    selected_by_parent = {
        item.parent_id: item
        for item in batch.items
        if item.parent_id and item.id in expected_item_ids
    }
    reconciled = []
    changed = False
    for entry in entries:
        if entry.get("item_id") in expected_item_ids:
            reconciled.append(entry)
            continue
        manifest_item = session.get(ProposalItem, entry.get("item_id"))
        selected_item = selected_by_parent.get(manifest_item.parent_id) if manifest_item and manifest_item.parent_id else None
        if not selected_item:
            reconciled.append(entry)
            continue
        patched = {
            **entry,
            "_original_item_id": entry.get("item_id"),
            "item_id": selected_item.id,
            "parent_id": selected_item.parent_id,
        }
        selected_item.title = manifest_item.title
        selected_item.new_value = manifest_item.new_value
        selected_item.payload_json = manifest_item.payload_json
        update_download_manifest_entry(patched, entry.get("status") or "queued")
        reconciled.append(patched)
        changed = True
    if changed:
        session.flush()
    return reconciled


def defer_excess_download_slot_entries(session: Session, batch: ProposalBatch, entries: list[dict]) -> list[dict]:
    limit = slskd_concurrent_download_limit(session)
    transfer_lookup, transfer_error_message = slskd_transfer_lookup(session, entries)
    if transfer_error_message:
        return entries
    active_pairs = [
        (entry, transfer)
        for entry in entries
        if entry.get("status") in DOWNLOAD_SLOT_STATUSES
        for transfer in [transfer_lookup.get(manifest_entry_key(entry))]
        if transfer_holds_download_slot(transfer)
    ]
    if len(active_pairs) <= limit:
        return entries
    started_pairs = [(entry, transfer) for entry, transfer in active_pairs if transfer_has_started(transfer)]
    waiting_pairs = sorted(
        [(entry, transfer) for entry, transfer in active_pairs if not transfer_has_started(transfer)],
        key=lambda pair: str(pair[0].get("queued_at") or pair[0].get("initialized_at") or ""),
    )
    keep_ids = {id(entry) for entry, _transfer in started_pairs}
    remaining_capacity = max(0, limit - len(started_pairs))
    keep_ids.update(id(entry) for entry, _transfer in waiting_pairs[:remaining_capacity])
    deferred: set[int] = set()
    for entry, transfer in waiting_pairs[remaining_capacity:]:
        item = session.get(ProposalItem, entry.get("item_id"))
        reason = f"download slot limit {limit} reached"
        if not item or not cancel_existing_slskd_transfer(session, transfer, item, reason):
            continue
        remove_download_manifest_entry(entry)
        set_download_item_status(item, "waiting to download")
        append_task_log(session, None, f"{entry_download_label(entry)}: deferred queued slskd transfer because {reason}")
        deferred.add(id(entry))
    if deferred:
        batch.status = ProposalStatus.executing
        session.flush()
    return [entry for entry in entries if id(entry) not in deferred or id(entry) in keep_ids]


def selected_download_blockers(batch: ProposalBatch) -> list[ProposalItem]:
    blockers = []
    for item in batch.items:
        if not item.selected or item.kind != ProposalKind.download:
            continue
        action = json.loads(item.payload_json or "{}").get("action")
        if action == "queue_ytdlp_download" and item.status not in {ProposalStatus.completed, ProposalStatus.rejected}:
            blockers.append(item)
        elif action == "queue_download" and item.status == ProposalStatus.failed and json.loads(item.payload_json or "{}").get("auto_retry_exhausted"):
            blockers.append(item)
    return blockers


def download_item_retry_exhausted(item: ProposalItem | None) -> bool:
    return bool(item and item.status == ProposalStatus.failed and json.loads(item.payload_json or "{}").get("auto_retry_exhausted"))


def download_entries_import_per_album(entries: list[dict]) -> bool:
    """Whether a batch's downloads should import a whole album at a time.

    Wishlist and plain album downloads import the album as a unit; missing-track and
    lossless-replacement downloads fill individual gaps and import each track as it lands.
    """
    for entry in entries:
        if (entry.get("request") or {}).get("workflow") in {"missing_tracks", "lossless_replacement"}:
            return False
    return True


def selected_slskd_download_item_ids(batch: ProposalBatch) -> set[str]:
    ids = set()
    for item in batch.items:
        if not item.selected or item.kind != ProposalKind.download:
            continue
        if json.loads(item.payload_json or "{}").get("action") == "queue_download":
            ids.add(item.id)
    return ids


def queue_missing_manifest_download(session: Session, batch: ProposalBatch, item: ProposalItem, available_slots: dict[str, int] | None = None) -> bool:
    payload = json.loads(item.payload_json or "{}")
    if payload.get("action") != "queue_download" or payload.get("auto_retry_exhausted"):
        return False
    if not download_slot_available(available_slots):
        set_download_item_status(item, "waiting to download")
        item.status = ProposalStatus.executing
        return True
    try:
        set_download_item_status(item, "queue record missing; queueing download automatically")
        apply_download_item(session, item)
        consume_download_slot(available_slots)
        item.status = ProposalStatus.executing
        batch.status = ProposalStatus.executing
        append_task_log(session, None, f"{item.title}: missing queue record was recreated automatically")
        return True
    except Exception as error:  # noqa: BLE001 - try a different candidate without creating another row.
        request = download_request_from_item(item)
        if not request:
            set_download_item_status(item, f"queue record missing and could not be recreated: {error}")
            return False
        candidate = payload.get("candidate") or {}
        return retry_download_entry(
            session,
            batch,
            {
                "batch_id": item.batch_id,
                "item_id": item.id,
                "parent_id": item.parent_id,
                "request": request,
                "candidate": candidate,
                "basename": remote_basename(candidate.get("filename") or ""),
                "queued_at": datetime.now(timezone.utc).isoformat(),
            },
            item,
            f"queue record missing and candidate failed: {error}",
            available_slots=available_slots,
        )


def reconnect_existing_slskd_transfer(session: Session, item: ProposalItem) -> bool:
    payload = json.loads(item.payload_json or "{}")
    if payload.get("action") != "queue_download":
        return False
    request = payload.get("request") or {}
    candidate = payload.get("candidate") or {}
    if not request or not candidate:
        return False
    transfers, transfer_error_message = slskd_download_transfer_list(session)
    if transfer_error_message:
        append_task_log(session, None, f"{item.title}: could not inspect slskd before recreating queue record: {transfer_error_message}", "warning")
        return False
    entry = {
        "batch_id": item.batch_id,
        "item_id": item.id,
        "parent_id": item.parent_id,
        "request": request,
        "candidate": {
            "username": candidate.get("username"),
            "filename": candidate.get("filename"),
            "folder": candidate.get("folder"),
        },
        "basename": remote_basename(candidate.get("filename") or ""),
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
    }
    transfer = matching_download_transfer(entry, transfers)
    # Only reconnect to a transfer that is actively downloading/queued. Reconnecting to a stale
    # "completed" transfer (e.g. left over from a previous run or a different downloads path)
    # whose file isn't present just loops on "reported complete but the file was not found"; let
    # those fall through to a fresh download instead.
    if not transfer or transfer_is_failed(transfer) or transfer_is_complete_or_finishing(transfer):
        return False
    record_download_manifest_entry(request, candidate, item)
    update_manifest_transfer_tracking(entry, transfer)
    set_download_item_status(item, transfer_wait_status(entry, transfer, "reconnected to existing slskd transfer"))
    item.status = ProposalStatus.executing
    append_task_log(session, None, f"{item.title}: existing slskd transfer found; queue record recreated without requeueing")
    return True


def slskd_download_transfer_list(session: Session) -> tuple[list[dict], str | None]:
    settings = integration_settings(session)
    slskd_url = settings.get("slskd_url", "")
    api_key = settings.get("slskd_api_key", "")
    if not slskd_url or not api_key:
        return [], "slskd settings are missing"
    try:
        return download_transfers(slskd_url, api_key), None
    except Exception as error:  # noqa: BLE001 - folder scans can still catch completed files.
        return [], f"slskd transfer status unavailable: {error}"


def slskd_transfer_lookup(session: Session, entries: list[dict]) -> tuple[dict[tuple[str, str, str], dict], str | None]:
    transfers, transfer_error_message = slskd_download_transfer_list(session)
    if transfer_error_message:
        return {}, transfer_error_message
    lookup: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        transfer = matching_download_transfer(entry, transfers)
        if transfer:
            lookup[manifest_entry_key(entry)] = transfer
    return lookup, None


def manifest_entry_key(entry: dict) -> tuple[str, str, str]:
    candidate = entry.get("candidate") or {}
    return (
        str(entry.get("batch_id") or ""),
        str(entry.get("item_id") or entry.get("_original_item_id") or ""),
        f"{candidate.get('username') or ''}:{candidate.get('filename') or entry.get('basename') or ''}",
    )


def matching_download_transfer(entry: dict, transfers: list[dict]) -> dict | None:
    candidate = entry.get("candidate") or {}
    expected_user = str(candidate.get("username") or "").casefold()
    expected_filename = normalize_remote_path(candidate.get("filename"))
    expected_basename = str(entry.get("basename") or remote_basename(expected_filename)).casefold()
    matches: list[dict] = []
    for transfer in transfers:
        transfer_user = str(transfer.get("username") or "").casefold()
        if expected_user and transfer_user and transfer_user != expected_user:
            continue
        paths = [
            normalize_remote_path(transfer.get("filename")),
            normalize_remote_path(transfer.get("local_path")),
        ]
        for path in paths:
            if not path:
                continue
            transfer_basename = path.rsplit("/", 1)[-1].casefold()
            if expected_basename and transfer_basename == expected_basename:
                matches.append(transfer)
                break
            if expected_filename and (path.endswith(expected_filename) or expected_filename.endswith(path)):
                matches.append(transfer)
                break
    if not matches:
        return None
    return max(matches, key=download_transfer_preference)


def download_transfer_preference(transfer: dict) -> tuple[int, float]:
    if transfer_is_failed(transfer):
        state_rank = 0
    elif transfer_is_complete_or_finishing(transfer):
        state_rank = 1
    elif transfer_holds_download_slot(transfer):
        state_rank = 3
    else:
        state_rank = 2
    return (state_rank, transfer_event_timestamp(transfer))


def transfer_event_timestamp(transfer: dict) -> float:
    for key in ("updatedAt", "UpdatedAt", "endedAt", "EndedAt", "startedAt", "StartedAt", "enqueuedAt", "EnqueuedAt", "requestedAt", "RequestedAt"):
        raw = transfer.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return 0.0


def normalize_remote_path(value: object) -> str:
    return str(value or "").replace("\\", "/").casefold()


def apply_transfer_path_to_manifest(entry: dict, transfer: dict | None) -> None:
    if not transfer or entry.get("path"):
        return
    local_path = transfer.get("local_path")
    if not isinstance(local_path, str) or not local_path:
        return
    path = Path(local_path)
    if not path.is_absolute():
        path = get_settings().downloads_path / path
    update_download_manifest_entry(entry, entry.get("status") or "queued", path=str(path))
    entry["path"] = str(path)


def transfer_wait_status(entry: dict, transfer: dict | None, fallback: str) -> str:
    if not transfer:
        return manifest_wait_status(entry, fallback)
    status = str(transfer.get("status") or "").strip()
    error = transfer.get("error")
    if transfer_is_failed(transfer):
        detail = f": {error}" if error else f": {status}" if status else ""
        return f"slskd transfer failed{detail}"
    if transfer_is_complete_or_finishing(transfer):
        return manifest_wait_status(entry, "moving completed file to staging")
    percent = transfer_percent(transfer)
    if percent is not None:
        if percent <= 0 and not transfer_has_started(transfer):
            return manifest_wait_status(entry, f"download queued in slskd: {friendly_transfer_status(status)}")
        speed = transfer_speed_label(transfer)
        return f"downloading {percent:.0f}%{speed}"
    if status:
        if transfer_is_queued_or_waiting(transfer):
            return manifest_wait_status(entry, f"download queued in slskd: {status}")
        return manifest_wait_status(entry, f"download status from slskd: {status}")
    return manifest_wait_status(entry, fallback)


def transfer_progress_state(entry: dict, transfer: dict | None, fallback: str) -> dict:
    status = transfer_wait_status(entry, transfer, fallback)
    if not transfer:
        return {"stage": "queued", "progress": 0, "label": status}
    if transfer_is_failed(transfer):
        return {"stage": "failed", "progress": transfer_percent(transfer) or 0, "label": status}
    if transfer_is_complete_or_finishing(transfer):
        return {"stage": "transferring", "progress": 100, "label": status, "indeterminate": True}
    percent = transfer_percent(transfer)
    if percent is not None:
        stage = "downloading" if percent > 0 or transfer_has_started(transfer) else "queued"
        return {"stage": stage, "progress": percent, "label": status}
    if transfer_is_queued_or_waiting(transfer):
        return {"stage": "queued", "progress": 0, "label": status}
    return {"stage": "transferring", "progress": 0, "label": status, "indeterminate": True}


def transfer_percent(transfer: dict | None) -> float | None:
    if not transfer:
        return None
    percent = transfer.get("percent")
    if not isinstance(percent, (int, float)):
        return None
    return max(0, min(100, float(percent)))


def transfer_has_started(transfer: dict | None) -> bool:
    if not transfer:
        return False
    bytes_transferred = transfer.get("bytes_transferred")
    speed = transfer.get("average_speed")
    try:
        if bytes_transferred is not None and int(bytes_transferred) > 0:
            return True
        if speed is not None and float(speed) > 0:
            return True
    except (TypeError, ValueError):
        return False
    return transfer_state_category(transfer) in {"in_progress", "succeeded", "completed"}


def transfer_is_queued_or_waiting(transfer: dict | None) -> bool:
    if not transfer:
        return False
    return transfer_state_category(transfer) in {"queued", "initializing"}


def friendly_transfer_status(status: str) -> str:
    cleaned = str(status or "").strip()
    return cleaned or "queued"


def transfer_speed_label(transfer: dict | None) -> str:
    if not transfer:
        return ""
    speed = transfer.get("average_speed")
    try:
        speed_value = float(speed)
    except (TypeError, ValueError):
        return ""
    if speed_value <= 0:
        return ""
    if speed_value >= 1024 * 1024:
        return f" · {speed_value / (1024 * 1024):.1f} MB/s"
    if speed_value >= 1024:
        return f" · {speed_value / 1024:.0f} KB/s"
    return f" · {speed_value:.0f} B/s"


def transfer_is_complete_or_finishing(transfer: dict | None) -> bool:
    if not transfer:
        return False
    category = transfer_state_category(transfer)
    if category in {"succeeded", "completed"}:
        return True
    if category in {"failed", "queued", "initializing"}:
        return False
    percent = transfer_percent(transfer)
    return percent is not None and percent >= TRANSFER_COMPLETE_PERCENT


def transfer_is_failed(transfer: dict | None) -> bool:
    if not transfer:
        return False
    if transfer.get("error"):
        return True
    return transfer_state_category(transfer) == "failed"


def transfer_status_is_failed(status: object) -> bool:
    return transfer_state_category({"status": status}) == "failed"


def download_retry_reason(entry: dict, transfer: dict | None) -> str | None:
    if transfer_is_failed(transfer):
        error = transfer.get("error") if transfer else None
        status = transfer.get("status") if transfer else None
        return f"transfer failed: {error or status or 'unknown error'}"
    age = manifest_entry_age_seconds(entry)
    if entry.get("status") == "retrying" and age >= REPLACEMENT_SEARCH_RETRY_SECONDS:
        return str(entry.get("retry_reason") or "continuing automatic replacement search")
    if transfer_is_complete_or_finishing(transfer):
        if age >= COMPLETED_MISSING_FILE_RETRY_SECONDS:
            downloads_root = get_settings().downloads_path
            slskd_path = (transfer or {}).get("local_path")
            where = f"; slskd wrote it to {slskd_path}" if slskd_path else ""
            return f"slskd reported complete but the file was not found under {downloads_root}{where} (check that slskd's downloads dir is the same shared folder)"
        return None
    if transfer_is_queued_or_waiting(transfer) and age >= queued_transfer_retry_seconds(entry):
        return "transfer stayed queued"
    if not transfer:
        last_error = entry.get("last_transfer_error")
        last_status = entry.get("last_transfer_status")
        if last_error or transfer_status_is_failed(last_status):
            return f"transfer failed: {last_error or last_status or 'unknown error'}"
        last_seen_age = manifest_seconds_since(entry.get("last_transfer_seen_at"))
        if last_seen_age is not None and last_seen_age >= RECENT_TRANSFER_DISAPPEARED_RETRY_SECONDS:
            return "slskd transfer disappeared before the file arrived"
        if age >= MISSING_TRANSFER_RETRY_SECONDS:
            return "slskd transfer did not appear after queueing"
        return None
    percent = transfer_percent(transfer)
    if percent is None:
        return None
    last_percent = manifest_float(entry.get("last_transfer_percent"))
    last_progress_age = manifest_seconds_since(entry.get("last_transfer_progress_at"))
    if percent <= 0 and age >= ZERO_PROGRESS_RETRY_SECONDS:
        queued_retry_seconds = queued_transfer_retry_seconds(entry)
        if transfer_is_queued_or_waiting(transfer) and age < queued_retry_seconds:
            return None
        if transfer_is_queued_or_waiting(transfer):
            return "transfer stayed queued"
        return "transfer stayed at 0%"
    if last_percent is not None and abs(last_percent - percent) >= 0.5:
        return None
    if last_progress_age is not None and last_progress_age >= STALLED_PROGRESS_RETRY_SECONDS:
        return f"transfer stalled at {percent:.0f}%"
    if last_percent is None and age >= STALLED_PROGRESS_RETRY_SECONDS * 2:
        return f"transfer stalled at {percent:.0f}%"
    return None


def queued_transfer_retry_seconds(entry: dict) -> int:
    retry_count = int(entry.get("retry_count") or 0)
    request = entry.get("request") or {}
    if retry_count > 0 or request.get("replace_track_id") or request.get("require_lossless"):
        return REPLACEMENT_QUEUED_TRANSFER_RETRY_SECONDS
    return QUEUED_TRANSFER_RETRY_SECONDS


def slskd_concurrent_download_limit(session: Session) -> int:
    # Downloads are handled strictly one track at a time. The old configurable concurrency caused
    # slot-accounting deadlocks (queued-remotely transfers counting against their own retries), so
    # it is fixed at 1 and the slskd_concurrent_downloads setting is ignored.
    return 1


def active_download_slot_count(session: Session, entries: list[dict]) -> int:
    slot_entries = [entry for entry in entries if entry.get("status") in DOWNLOAD_SLOT_STATUSES]
    transfers, transfer_error_message = slskd_download_transfer_list(session)
    if transfer_error_message:
        append_task_log(session, None, f"{transfer_error_message}; preserving current download slots", "warning")
        return sum(1 for entry in slot_entries if manifest_entry_uses_recent_download_slot(entry))
    transfer_lookup: dict[tuple[str, str, str], dict] = {}
    for entry in slot_entries:
        transfer = matching_download_transfer(entry, transfers)
        if transfer:
            transfer_lookup[manifest_entry_key(entry)] = transfer
    global_active = sum(1 for transfer in transfers if transfer_holds_download_slot(transfer))
    active = 0
    for entry in slot_entries:
        transfer = transfer_lookup.get(manifest_entry_key(entry))
        if transfer:
            update_manifest_transfer_tracking(entry, transfer)
            if transfer_holds_download_slot(transfer):
                active += 1
            continue
        if manifest_entry_waiting_for_slskd_record(entry):
            active += 1
    return max(active, global_active)


def manifest_entry_uses_recent_download_slot(entry: dict) -> bool:
    if entry.get("status") not in DOWNLOAD_SLOT_STATUSES:
        return False
    last_seen_age = manifest_seconds_since(entry.get("last_transfer_seen_at"))
    if last_seen_age is not None:
        return last_seen_age <= DOWNLOAD_SLOT_STALE_SECONDS
    return manifest_entry_age_seconds(entry) <= DOWNLOAD_SLOT_STALE_SECONDS


def manifest_entry_waiting_for_slskd_record(entry: dict) -> bool:
    initialized_age = manifest_seconds_since(entry.get("initialized_at"))
    if initialized_age is not None:
        return initialized_age <= DOWNLOAD_SLOT_PENDING_RECORD_SECONDS
    return manifest_entry_age_seconds(entry) <= DOWNLOAD_SLOT_PENDING_RECORD_SECONDS


def transfer_holds_download_slot(transfer: dict | None) -> bool:
    if not transfer or transfer_is_failed(transfer) or transfer_is_complete_or_finishing(transfer):
        return False
    if transfer_has_started(transfer) or transfer_is_queued_or_waiting(transfer):
        return True
    # An unrecognized but non-terminal state still occupies a slskd slot; count it so
    # we never over-queue past the concurrent-download limit.
    return transfer_state_category(transfer) == "unknown" and bool(transfer.get("status"))


def download_slots_available(session: Session, entries: list[dict]) -> int:
    return max(0, slskd_concurrent_download_limit(session) - active_download_slot_count(session, entries))


def download_slot_available(slot_tracker: dict[str, int] | None) -> bool:
    return slot_tracker is None or int(slot_tracker.get("available") or 0) > 0


def consume_download_slot(slot_tracker: dict[str, int] | None) -> None:
    if slot_tracker is not None:
        slot_tracker["available"] = max(0, int(slot_tracker.get("available") or 0) - 1)


def defer_download_for_slot(
    session: Session,
    item: ProposalItem,
    entry: dict,
    reason: str,
    failed_candidates: list[dict],
    retry_count: int,
) -> bool:
    update_download_manifest_entry(
        entry,
        "retrying",
        retry_count=retry_count,
        failed_candidates=failed_candidates[-25:],
        retry_reason=reason,
    )
    item.status = ProposalStatus.executing
    set_download_item_status(item, "waiting to download")
    return True


def queue_existing_retry_candidate(
    session: Session,
    item: ProposalItem,
    entry: dict,
    retry_count: int,
    failed_candidates: list[dict],
    reason: str,
    available_slots: dict[str, int] | None = None,
) -> bool:
    settings = integration_settings(session)
    for candidate_item in existing_retry_candidate_items(session, item, failed_candidates):
        candidate_payload = json.loads(candidate_item.payload_json or "{}")
        candidate = candidate_payload.get("candidate") or {}
        request = entry.get("request") or candidate_payload.get("request") or {}
        label = candidate.get("filename") or candidate_item.title
        try:
            if not download_slot_available(available_slots):
                set_download_item_status(item, "waiting to download")
                return False
            append_task_log(session, None, f"{item.title}: trying existing alternate candidate {label}")
            queue_slskd_download(settings.get("slskd_url", ""), settings.get("slskd_api_key", ""), candidate)
            consume_download_slot(available_slots)
            payload = json.loads(item.payload_json or "{}")
            payload.update(
                {
                    "action": "queue_download",
                    "request": request,
                    "candidate": candidate,
                    "failed_candidates": failed_candidates[-25:],
                    "status": f"existing alternate candidate queued ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})",
                    "auto_retry_exhausted": False,
                }
            )
            item.payload_json = json.dumps(payload)
            item.title = candidate_item.title
            item.new_value = candidate_item.new_value
            item.status = ProposalStatus.executing
            candidate_item.status = ProposalStatus.executing
            record_download_manifest_entry(request, candidate, item)
            update_download_manifest_entry(
                {
                    "batch_id": item.batch_id,
                    "item_id": item.id,
                    "basename": remote_basename(candidate.get("filename") or ""),
                },
                "queued",
                retry_count=retry_count,
                failed_candidates=failed_candidates[-25:],
                queued_at=datetime.now(timezone.utc).isoformat(),
                initialized_at=datetime.now(timezone.utc).isoformat(),
            )
            set_download_item_status(item, f"queued in slskd: existing alternate ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})")
            append_task_log(session, None, f"{item.title}: existing alternate candidate queued after {reason}")
            return True
        except Exception as error:  # noqa: BLE001 - keep walking the already reviewed alternates.
            failed_candidates.append(
                {
                    "username": candidate.get("username"),
                    "filename": candidate.get("filename"),
                    "reason": str(error),
                }
            )
            candidate_item.status = ProposalStatus.failed
            append_task_log(session, None, f"{item.title}: existing alternate candidate failed: {label}: {error}", "warning")
    return False


def retry_download_entry(
    session: Session,
    batch: ProposalBatch,
    entry: dict,
    item: ProposalItem,
    reason: str,
    transfer: dict | None = None,
    available_slots: dict[str, int] | None = None,
) -> bool:
    retry_count = max(0, min(int(entry.get("retry_count") or 0), MAX_DOWNLOAD_AUTO_RETRIES))
    current_candidate = entry.get("candidate") or {}
    failed_candidates = manifest_failed_candidates(entry)
    if current_candidate:
        failed_candidates.append(
            {
                "username": current_candidate.get("username"),
                "filename": current_candidate.get("filename"),
                "reason": reason,
            }
        )
    holds_slot = transfer is not None and transfer_holds_download_slot(transfer)
    # If we can't act yet (no free slot) and the current transfer isn't occupying a slot we could
    # reclaim, just wait — do NOT cancel/re-queue every scan. Re-queuing a failed transfer each
    # scan churned the track, spammed the log, and stopped retry_count from ever advancing.
    if not holds_slot and retry_count < MAX_DOWNLOAD_AUTO_RETRIES and not download_slot_available(available_slots):
        return defer_download_for_slot(session, item, entry, reason, failed_candidates, retry_count)
    # We're proceeding (or exhausting): abandon the current transfer now. Cancelling a slot-holding
    # transfer frees its slot for the 1:1 replacement swap (this was the "waiting for download slot"
    # deadlock); a failed transfer holds no slot, so there is nothing to give back.
    if transfer is not None:
        cancel_existing_slskd_transfer(session, transfer, item, reason)
        transfer = None
        if holds_slot and available_slots is not None:
            available_slots["available"] = int(available_slots.get("available") or 0) + 1
    if retry_count >= MAX_DOWNLOAD_AUTO_RETRIES:
        return exhaust_download_retries(session, item, entry, reason, failed_candidates, retry_count)
    if not download_slot_available(available_slots):
        return defer_download_for_slot(session, item, entry, reason, failed_candidates, retry_count)
    retry_count += 1
    request = {**(entry.get("request") or {}), "ignored_candidates": failed_candidates, "multiple_candidates": True}
    set_download_item_status(item, f"{reason}; trying another candidate ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})")
    payload = json.loads(item.payload_json or "{}")
    payload["auto_retry_exhausted"] = False
    item.payload_json = json.dumps(payload)
    item.status = ProposalStatus.executing
    update_download_manifest_entry(
        entry,
        "retrying",
        retry_count=retry_count,
        failed_candidates=failed_candidates[-25:],
        retry_reason=reason,
    )
    append_task_log(
        session,
        None,
        f"{item.title}: {reason}; trying replacement candidate {retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES}",
        "warning",
    )
    if queue_existing_retry_candidate(session, item, entry, retry_count, failed_candidates, reason, available_slots=available_slots):
        return True
    try:
        search_result = search_slskd_for_request(session, request, limit=8)
        candidates = filter_ignored_candidates(search_result.get("candidates") or [], failed_candidates)
    except Exception as error:  # noqa: BLE001 - keep the failed row visible with a useful reason.
        if retry_count >= MAX_DOWNLOAD_AUTO_RETRIES:
            return exhaust_download_retries(session, item, entry, f"replacement search failed: {error}", failed_candidates, retry_count)
        update_download_manifest_entry(
            entry,
            "retrying",
            retry_count=retry_count,
            failed_candidates=failed_candidates[-25:],
            retry_reason=f"replacement search failed: {error}",
            queued_at=datetime.now(timezone.utc).isoformat(),
        )
        item.status = ProposalStatus.executing
        set_download_item_status(item, f"replacement search failed; retrying automatically ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})")
        append_task_log(session, None, f"{item.title}: replacement search failed and will retry: {error}", "warning")
        return True
    if not candidates:
        if retry_count >= MAX_DOWNLOAD_AUTO_RETRIES:
            return exhaust_download_retries(session, item, entry, "no replacement candidates were found", failed_candidates, retry_count)
        update_download_manifest_entry(
            entry,
            "retrying",
            retry_count=retry_count,
            failed_candidates=failed_candidates[-25:],
            retry_reason="no replacement candidates were found",
            queued_at=datetime.now(timezone.utc).isoformat(),
        )
        item.status = ProposalStatus.executing
        set_download_item_status(item, f"no replacement candidate yet; retrying automatically ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})")
        return True
    candidate = candidates[0]
    payload = json.loads(item.payload_json or "{}")
    payload.update(
        {
            "action": "queue_download",
            "request": entry.get("request") or payload.get("request") or {},
            "candidate": candidate,
            "failed_candidates": failed_candidates[-25:],
            "status": f"replacement candidate found; queueing ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})",
            "auto_retry_exhausted": False,
        }
    )
    item.payload_json = json.dumps(payload)
    item.title = f"slskd: {candidate.get('filename') or download_query(payload['request'])}"
    item.new_value = candidate.get("username")
    item.status = ProposalStatus.executing
    if not download_slot_available(available_slots):
        return defer_download_for_slot(session, item, entry, reason, failed_candidates, retry_count)
    try:
        settings = integration_settings(session)
        queue_slskd_download(settings.get("slskd_url", ""), settings.get("slskd_api_key", ""), candidate)
        consume_download_slot(available_slots)
        record_download_manifest_entry(entry.get("request") or {}, candidate, item)
        update_download_manifest_entry(
            {
                "batch_id": item.batch_id,
                "item_id": item.id,
                "basename": remote_basename(candidate.get("filename") or ""),
            },
            "queued",
            retry_count=retry_count,
            failed_candidates=failed_candidates[-25:],
            queued_at=datetime.now(timezone.utc).isoformat(),
            initialized_at=datetime.now(timezone.utc).isoformat(),
        )
        set_download_item_status(item, f"queued in slskd: replacement candidate ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})")
        append_task_log(session, None, f"{item.title}: replacement candidate queued after {reason}")
        return True
    except Exception as error:  # noqa: BLE001 - immediately try again on the next scan without duplicating rows.
        failed_candidates.append(
            {
                "username": candidate.get("username"),
                "filename": candidate.get("filename"),
                "reason": str(error),
            }
        )
        if retry_count >= MAX_DOWNLOAD_AUTO_RETRIES:
            return exhaust_download_retries(session, item, entry, f"replacement queue failed: {error}", failed_candidates, retry_count)
        update_download_manifest_entry(
            entry,
            "retrying",
            retry_count=retry_count,
            failed_candidates=failed_candidates[-25:],
            retry_reason=f"replacement queue failed: {error}",
            queued_at=datetime.now(timezone.utc).isoformat(),
        )
        item.status = ProposalStatus.executing
        set_download_item_status(item, f"replacement queue failed; retrying automatically ({retry_count}/{MAX_DOWNLOAD_AUTO_RETRIES})")
        append_task_log(session, None, f"{item.title}: replacement candidate failed to queue: {error}", "warning")
        return True


def cancel_existing_slskd_transfer(session: Session, transfer: dict | None, item: ProposalItem, reason: str) -> bool:
    if not transfer:
        return False
    transfer_id = str(transfer.get("id") or "")
    username = str(transfer.get("username") or "")
    if not transfer_id or not username:
        return False
    settings = integration_settings(session)
    try:
        if cancel_slskd_download(settings.get("slskd_url", ""), settings.get("slskd_api_key", ""), username, transfer_id, remove=True):
            append_task_log(session, None, f"{item.title}: cancelled previous slskd transfer before retrying: {reason}", "warning")
            return True
    except Exception as error:  # noqa: BLE001 - retry can still proceed if slskd already removed the record.
        append_task_log(session, None, f"{item.title}: could not cancel previous slskd transfer before retrying: {error}", "warning")
    return False


def exhaust_download_retries(session: Session, item: ProposalItem, entry: dict, reason: str, failed_candidates: list[dict], retry_count: int) -> bool:
    payload = json.loads(item.payload_json or "{}")
    payload["status"] = f"needs attention after {retry_count} automatic retries: {reason}"
    payload["auto_retry_exhausted"] = True
    payload["failed_candidates"] = failed_candidates[-25:]
    item.payload_json = json.dumps(payload)
    item.status = ProposalStatus.failed
    update_download_manifest_entry(
        entry,
        "failed",
        retry_count=retry_count,
        failed_candidates=failed_candidates[-25:],
        retry_reason=reason,
    )
    label = entry_download_label(entry)
    append_task_log(session, None, f"{label}: could not download from Soulseek after {retry_count} candidate(s): {reason}", "error")
    create_notification(
        session,
        title="Download needs attention",
        body=f"Couldn't download {label} from Soulseek after {retry_count} candidate(s). Use the YouTube fallback for this track to finish the album.",
        event_type="task_failed",
        target_url="/downloads",
    )
    return False


def manifest_failed_candidates(entry: dict) -> list[dict]:
    failed = entry.get("failed_candidates")
    return list(failed) if isinstance(failed, list) else []


def filter_ignored_candidates(candidates: list[dict], ignored_candidates: list[dict]) -> list[dict]:
    ignored = {candidate_identity(candidate) for candidate in ignored_candidates}
    return [candidate for candidate in candidates if candidate_identity(candidate) not in ignored]


def candidate_identity(candidate: dict) -> tuple[str, str]:
    return (
        str(candidate.get("username") or "").casefold(),
        str(candidate.get("filename") or "").replace("\\", "/").casefold(),
    )


def update_manifest_transfer_tracking(entry: dict, transfer: dict | None) -> None:
    if not transfer:
        return
    percent = transfer.get("percent")
    now = datetime.now(timezone.utc).isoformat()
    manifest_status = entry.get("status") or "queued"
    if transfer_is_failed(transfer):
        manifest_status = "queued"
    elif transfer_is_complete_or_finishing(transfer):
        manifest_status = "downloading"
    elif transfer_has_started(transfer):
        manifest_status = "downloading"
    elif transfer_is_queued_or_waiting(transfer):
        manifest_status = "queued"
    fields: dict[str, object] = {
        "last_transfer_seen_at": now,
        "last_transfer_status": transfer.get("status"),
        "last_transfer_id": transfer.get("id"),
        "last_transfer_error": transfer.get("error"),
        "last_transfer_bytes_transferred": transfer.get("bytes_transferred"),
        "last_transfer_bytes_remaining": transfer.get("bytes_remaining"),
        "last_transfer_average_speed": transfer.get("average_speed"),
    }
    if isinstance(percent, (int, float)):
        bounded = max(0, min(100, float(percent)))
        last_percent = manifest_float(entry.get("last_transfer_percent"))
        fields["last_transfer_percent"] = bounded
        if last_percent is None or abs(last_percent - bounded) >= 0.5:
            fields["last_transfer_progress_at"] = now
    update_download_manifest_entry(entry, manifest_status, **fields)
    entry.update({"status": manifest_status, **fields})


def manifest_entry_age_seconds(entry: dict) -> int:
    return manifest_seconds_since(entry.get("queued_at")) or 0


def manifest_seconds_since(value: object) -> int | None:
    if not value:
        return None
    try:
        since = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - since).total_seconds()))


def manifest_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def manifest_entry_file_path(entry: dict, minimum_age_seconds: int) -> tuple[Path | None, str]:
    root = get_settings().downloads_path
    if entry.get("status") in DOWNLOAD_MANIFEST_STAGING_STATUSES and entry.get("path"):
        staged = Path(str(entry["path"]))
        if staged.exists():
            return staged, "staged; ready for verification"
        return None, "staged file is missing"
    known_path = entry.get("path")
    candidates = []
    known_candidates: set[Path] = set()
    if known_path:
        known = Path(known_path)
        known_candidate = known if known.is_absolute() else root / known
        known_candidates.add(known_candidate)
        candidates.append(known_candidate)
    candidates.extend(root.rglob("*"))
    now = time.time()
    for file_path in candidates:
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        if file_path in known_candidates:
            matches_entry = True
        else:
            matches_entry = manifest_entry_matches_path(entry, file_path)
        if not matches_entry:
            continue
        try:
            stat = file_path.stat()
        except OSError:
            continue
        if now - stat.st_mtime < minimum_age_seconds:
            return None, "downloaded; settling before verification"
        return file_path, "downloaded; ready for verification"
    return None, "queued in slskd; checking transfer state"


def manifest_wait_status(entry: dict, wait_status: str) -> str:
    queued_at = entry.get("queued_at")
    if not queued_at:
        return wait_status
    try:
        queued_at_dt = datetime.fromisoformat(str(queued_at))
    except ValueError:
        return wait_status
    seconds = max(0, int((datetime.now(timezone.utc) - queued_at_dt).total_seconds()))
    if seconds < 90:
        elapsed = f"{seconds}s"
    else:
        elapsed = f"{seconds // 60}m"
    return f"{wait_status} ({elapsed})"


def update_download_container_statuses(batch: ProposalBatch) -> None:
    children_by_parent: dict[str, list[ProposalItem]] = {}
    for item in batch.items:
        if item.parent_id:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    def selected_action_descendants(item: ProposalItem) -> list[ProposalItem]:
        children = children_by_parent.get(item.id, [])
        if not children:
            payload = json.loads(item.payload_json or "{}")
            if item.selected and item.kind == ProposalKind.download and payload.get("action") == "queue_download":
                return [item]
            return []
        descendants: list[ProposalItem] = []
        for child in children:
            descendants.extend(selected_action_descendants(child))
        return descendants

    for item in batch.items:
        if item.id not in children_by_parent:
            continue
        leaves = selected_action_descendants(item)
        if not leaves:
            continue
        statuses = [
            json.loads(leaf.payload_json or "{}").get("status")
            or (leaf.status.value if hasattr(leaf.status, "value") else str(leaf.status))
            for leaf in leaves
        ]
        progress_payloads = [json.loads(leaf.payload_json or "{}").get("download_progress") or {} for leaf in leaves]
        downloaded = sum(1 for status in statuses if download_status_is_downloaded(status))
        verified = sum(1 for status in statuses if download_status_is_verified(status))
        failed = sum(1 for status in statuses if "need attention" in status or "failed" in status or "mismatch" in status or "could not be verified" in status)
        total = len(leaves)
        progress_values = [download_status_progress_value(status, payload) for status, payload in zip(statuses, progress_payloads)]
        average_progress = sum(progress_values) / max(1, total)
        if failed:
            status = f"{failed} of {total} need attention"
        elif verified == total:
            status = "verified 100% · ready to import"
        elif downloaded == total:
            verify_percent = (verified / max(1, total)) * 100
            status = f"verifying {verify_percent:.0f}% · {verified} of {total} verified"
        elif downloaded:
            status = f"downloading {average_progress:.0f}% · {downloaded} of {total} downloaded"
        elif average_progress > 0:
            status = f"downloading {average_progress:.0f}% · 0 of {total} downloaded"
        elif any("waiting to download" in status for status in statuses):
            status = f"waiting to download · {downloaded} of {total} downloaded"
        elif any("queued in slskd" in status or "download queued in slskd" in status for status in statuses):
            status = f"queued in slskd · {downloaded} of {total} downloaded"
        else:
            status = f"waiting for transfer progress · {downloaded} of {total} downloaded"
        stage = "failed" if failed else "verified" if verified == total else "verifying" if downloaded == total else "downloading" if average_progress > 0 else "queued"
        set_item_payload_status(item, status, download_progress_payload(status, stage=stage, progress=average_progress, indeterminate=stage in {"verifying"}))


def download_status_is_downloaded(status: str) -> bool:
    lowered = str(status or "").casefold()
    return any(token in lowered for token in ("downloaded", "staged", "verifying", "verified", "importing"))


def download_status_is_verified(status: str) -> bool:
    lowered = str(status or "").casefold()
    return any(token in lowered for token in ("verified", "importing"))


def download_status_progress_value(status: str, progress_payload: dict | None = None) -> float:
    if isinstance(progress_payload, dict) and isinstance(progress_payload.get("value"), (int, float)):
        return max(0.0, min(100.0, float(progress_payload["value"])))
    lowered = str(status or "").casefold()
    if download_status_is_downloaded(status):
        return 100.0
    match = re.search(r"downloading\s+(\d+(?:\.\d+)?)%", lowered)
    if match:
        return max(0.0, min(100.0, float(match.group(1))))
    return 0.0


def manifest_entry_matches_path(entry: dict, file_path: Path) -> bool:
    basename = file_path.name.casefold()
    if entry.get("basename") == basename:
        return True
    filename = str((entry.get("candidate") or {}).get("filename") or "").replace("\\", "/").casefold()
    return bool(filename and str(file_path).replace("\\", "/").casefold().endswith(filename))


def handle_download_mismatch(session: Session, batch: ProposalBatch, entry: dict, file_path: Path, verification: dict) -> bool:
    try:
        file_path.unlink()
    except OSError as error:
        verification["message"] = f"{verification['message']} Could not remove {file_path.name}: {error}"
    item = session.get(ProposalItem, entry.get("item_id"))
    if item:
        set_download_item_status(item, f"{verification['message']}; retrying another candidate")
        retry_started = retry_download_entry(
            session,
            batch,
            entry,
            item,
            verification["message"],
            available_slots={"available": download_slots_available(session, load_download_manifest())},
        )
        if retry_started:
            batch.status = ProposalStatus.executing
            create_notification(
                session,
                title="Download candidate rejected",
                body=f"{file_path.name} did not match and another candidate is being tried.",
                event_type="task_warning",
                target_url="/downloads",
            )
            session.commit()
            return True
    update_download_manifest_entry(entry, "failed", path=str(file_path), metadata=verification.get("metadata"), retry_reason=verification["message"])
    if item:
        item.status = ProposalStatus.failed
        set_download_item_status(item, f"needs attention: {verification['message']}")
    batch.status = ProposalStatus.failed
    create_notification(session, title="Downloaded track did not match", body=verification["message"], event_type="task_failed", target_url="/downloads")
    session.commit()
    return False


def handle_download_verification_issue(session: Session, batch: ProposalBatch, entry: dict, file_path: Path, message: str, verification: dict) -> None:
    update_download_manifest_entry(entry, "queued", path=str(file_path), verification_error=message, metadata=verification.get("metadata"))
    item = session.get(ProposalItem, entry.get("item_id"))
    if item:
        item.status = ProposalStatus.failed
        set_download_item_status(item, message)
    batch.status = ProposalStatus.failed
    create_notification(
        session,
        title="Download verification failed",
        body=message,
        event_type="task_failed",
        target_url="/downloads",
    )
    session.commit()


def import_verified_download_batch(session: Session, batch: ProposalBatch, verified_entries: list[tuple[dict, Path, dict]]) -> int:
    return import_verified_download_entries(session, batch, verified_entries, finalize=True)


def finalize_completed_download_batch(session: Session, batch: ProposalBatch) -> None:
    cleanup_download_staging_batch(batch.id)
    for item in batch.items:
        if item.status in {ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.pending}:
            item.status = ProposalStatus.completed
    batch.status = ProposalStatus.completed
    session.flush()


def present_staged_downloads_for_library_review(session: Session, batch: ProposalBatch, staged_entries: list[tuple[dict, Path]], finalize: bool) -> None:
    """Turn a fully-downloaded, staged batch into a manual "add to library" review task.

    No MusicBrainz verification: the files are staged and an import_files proposal is created in
    the task queue. The user approves it to move the files into the library (replacing the
    existing file for lossless-replacement requests). The download batch itself is marked done.
    """
    review_batch: ProposalBatch | None = None
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    first_artist: str | None = None
    first_album: str | None = None
    created = 0

    def ensure_review_tree(artist: str, album: str) -> str:
        nonlocal review_batch
        if review_batch is None:
            review_batch = ProposalBatch(title="Add downloaded music to library", kind=ProposalKind.import_files, tree_path="/task-queue")
            session.add(review_batch)
            session.flush()
        if artist not in artist_items:
            artist_item = ProposalItem(batch_id=review_batch.id, title=artist, kind=ProposalKind.import_files, payload_json=json.dumps({"artist": artist}))
            session.add(artist_item)
            session.flush()
            artist_items[artist] = artist_item
        album_key = (artist, album)
        if album_key not in album_items:
            album_item = ProposalItem(batch_id=review_batch.id, parent_id=artist_items[artist].id, title=album, kind=ProposalKind.import_files, payload_json=json.dumps({"artist": artist, "album": album}))
            session.add(album_item)
            session.flush()
            album_items[album_key] = album_item
        return album_items[album_key].id

    for entry, staged_path in staged_entries:
        # Per-item idempotency: never add the same downloaded track to a review twice (handles a
        # track that fails, retries, and succeeds after the album was already presented).
        item_id = str(entry.get("item_id") or "")
        already_in_review = item_id and session.scalar(
            select(ProposalItem.id)
            .where(ProposalItem.kind == ProposalKind.import_files)
            .where(ProposalItem.payload_json.like(f'%"source_download_item_id": "{item_id}"%'))
            .limit(1)
        )
        if not already_in_review:
            request = entry.get("request") or {}
            metadata = normalize_download_metadata(read_audio_metadata(staged_path), request)
            try:
                write_audio_metadata(staged_path, metadata)
            except Exception as error:  # noqa: BLE001 - tagging is best-effort; import still works.
                append_task_log(session, None, f"{entry_download_label(entry)}: could not write tags before review: {error}", "warning")
            replace_track_id = request.get("replace_track_id")
            replace_track = session.get(Track, replace_track_id) if replace_track_id else None
            target_path = replacement_target_path(replace_track, metadata, staged_path) if replace_track else unique_destination(suggest_library_path(metadata, staged_path))
            artist = metadata.get("albumartist") or metadata.get("artist") or "Unknown Artist"
            album = metadata.get("album") or "Unknown Album"
            first_artist = first_artist or artist
            first_album = first_album or album
            album_item_id = ensure_review_tree(artist, album)
            session.add(
                ProposalItem(
                    batch_id=review_batch.id,
                    parent_id=album_item_id,
                    title=metadata.get("title") or staged_path.stem,
                    kind=ProposalKind.import_files,
                    old_value=str(staged_path),
                    new_value=str(target_path),
                    payload_json=json.dumps(
                        {
                            "action": "replace_library_track" if replace_track else "import_download",
                            "source_download_batch_id": batch.id,
                            "source_download_item_id": item_id,
                            "replace_track_id": replace_track.id if replace_track else None,
                            "metadata": metadata,
                        }
                    ),
                )
            )
            created += 1
        # The download is done; the review task now owns the staged file.
        update_download_manifest_entry(entry, "completed", path=str(staged_path))
        download_item = session.get(ProposalItem, entry.get("item_id"))
        if download_item:
            download_item.status = ProposalStatus.completed
            set_download_item_status(download_item, "downloaded; review to add to library", stage="staging", progress=100)
    if review_batch is not None:
        label = " – ".join(part for part in [first_artist, first_album] if part)
        review_batch.title = f"Add to library: {label}" if label else "Add downloaded music to library"
        session.flush()
        append_task_log(session, None, f"{batch.title}: {created} downloaded track(s) staged; review to add them to the library")
        create_notification(
            session,
            title="Downloaded music ready to add",
            body=f"{created} track(s) downloaded for {label or 'your request'}. Review and approve to add them to your library.",
            event_type="approval_needed",
            target_url="/task-queue",
        )
    if finalize:
        # Mark the download batch complete WITHOUT cleaning staging — the review task still needs
        # the staged files; they leave staging when it is approved and imported.
        for item in batch.items:
            if item.status in {ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.pending}:
                item.status = ProposalStatus.completed
        batch.status = ProposalStatus.completed
    session.flush()


def import_verified_download_entries(
    session: Session,
    batch: ProposalBatch,
    verified_entries: list[tuple[dict, Path, dict]],
    finalize: bool = True,
) -> int:
    known_paths = existing_library_and_proposal_paths(session)
    imported = 0
    imported_albums: set[tuple[str, str]] = set()
    for entry, file_path, metadata in sorted(verified_entries, key=lambda item: ((item[2].get("disc_number") or 0), (item[2].get("track_number") or 9999), item[2].get("title") or "")):
        request = entry.get("request") or {}
        normalized_metadata = normalize_download_metadata(metadata, request)
        normalized_metadata["musicbrainz_verified"] = True
        append_task_log(session, None, f"{entry_download_label(entry)}: writing normalized metadata before library import")
        write_audio_metadata(file_path, normalized_metadata)
        replacement_track = session.get(Track, request.get("replace_track_id")) if request.get("replace_track_id") else None
        target_path = replacement_target_path(replacement_track, normalized_metadata, file_path)
        if not replacement_track and str(target_path) in known_paths:
            if file_path.exists() and file_path.resolve() != target_path.resolve():
                file_path.unlink()
                append_task_log(session, None, f"{entry_download_label(entry)}: removed duplicate staged file because {target_path.name} already exists")
            update_download_manifest_entry(entry, "completed", path=str(target_path), metadata=normalized_metadata)
            duplicate_item = session.get(ProposalItem, entry.get("item_id"))
            if duplicate_item:
                duplicate_item.status = ProposalStatus.completed
            continue
        payload = {
            "path": str(file_path),
            "relative_path": relative_media_path(file_path),
            "extension": file_path.suffix.lower(),
            "size_bytes": file_path.stat().st_size,
            "mtime_ns": file_path.stat().st_mtime_ns,
            "metadata": normalized_metadata,
            "suggested_library_path": str(target_path),
        }
        if replacement_track:
            replace_library_track_file(session, replacement_track, file_path, target_path, payload)
            append_task_log(session, None, f"{entry_download_label(entry)}: replaced library file at {target_path}")
        else:
            import_file_to_library(session, file_path, target_path, payload)
            append_task_log(session, None, f"{entry_download_label(entry)}: moved staged file into library at {target_path}")
        mark_matching_wishlist_completed(session, normalized_metadata)
        update_download_manifest_entry(entry, "completed", path=str(target_path), metadata=normalized_metadata)
        imported_item = session.get(ProposalItem, entry.get("item_id"))
        if imported_item:
            imported_item.status = ProposalStatus.completed
        imported += 1
        known_paths.add(str(target_path))
        imported_albums.add(
            (
                str(normalized_metadata.get("albumartist") or normalized_metadata.get("artist") or "Unknown Artist"),
                str(normalized_metadata.get("album") or "Unknown Album"),
            )
        )
    for artist_name, album_title in sorted(imported_albums):
        ensure_album_cover_for_import(session, artist_name, album_title)
    if finalize:
        finalize_completed_download_batch(session, batch)
        append_task_log(session, None, f"{batch.title}: library import finished for {imported} track(s)")
    else:
        session.flush()
        append_task_log(session, None, f"{batch.title}: imported {imported} verified track(s); other tracks still in progress")
    return imported


def relative_media_path(file_path: Path) -> str:
    settings = get_settings()
    roots = [
        download_staging_root(),
        settings.staging_path,
        settings.downloads_path,
        settings.import_path,
        settings.library_path,
    ]
    for root in roots:
        try:
            return str(file_path.relative_to(root))
        except ValueError:
            continue
    return file_path.name


def album_folder_path(album: Album) -> Path:
    if album.path:
        return Path(album.path)
    track_dirs = [Path(track.path).parent for track in album.tracks if track.path]
    if track_dirs:
        return track_dirs[0]
    return get_settings().library_path / safe_path_part(album.artist.name, "Unknown Artist") / safe_path_part(album.title, "Unknown Album")


def album_cover_candidate_urls(artist: str, album: str, results: list[dict]) -> list[str]:
    urls = [str(result.get("cover_art_url")) for result in results if result.get("cover_art_url")]
    itunes_url = itunes_album_artwork(artist, album)
    if itunes_url:
        urls.append(itunes_url)
    seen = set()
    unique_urls = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    return unique_urls


def cover_extension(content_type: str, url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().casefold()
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def download_album_cover_to_library(session: Session, album: Album, urls: list[str]) -> str | None:
    album_dir = album_folder_path(album)
    album_dir.mkdir(parents=True, exist_ok=True)
    last_error = None
    for url in urls:
        try:
            response = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Nudibranch/0.1"})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not content_type.casefold().startswith("image/"):
                raise ValueError(f"unexpected content type {content_type or 'unknown'}")
            cover_path = album_dir / f"cover{cover_extension(content_type, url)}"
            cover_path.write_bytes(response.content)
            append_task_log(session, None, f"{album.artist.name} / {album.title}: downloaded album art to {cover_path}")
            return str(cover_path)
        except Exception as error:  # noqa: BLE001 - try the next artwork source.
            last_error = error
            append_task_log(session, None, f"{album.artist.name} / {album.title}: cover download failed from {url}: {error}", "warning")
    if last_error:
        append_task_log(session, None, f"{album.artist.name} / {album.title}: no cover source could be downloaded: {last_error}", "warning")
    return None


def ensure_album_cover_for_import(session: Session, artist_name: str, album_title: str) -> None:
    album = session.scalar(
        select(Album)
        .join(Artist, Album.artist_id == Artist.id)
        .where(Artist.name == artist_name, Album.title == album_title)
    )
    if not album or album.cover_path:
        return
    try:
        results = search_album_releases(artist_name, album_title)
    except Exception as error:  # noqa: BLE001 - cover art should not block completed imports.
        append_task_log(session, None, f"{artist_name} / {album_title}: album art lookup failed: {error}", "warning")
        return
    cover_path = download_album_cover_to_library(session, album, album_cover_candidate_urls(artist_name, album_title, results))
    if not cover_path:
        append_task_log(session, None, f"{artist_name} / {album_title}: no album art found", "warning")
        return
    album.cover_path = cover_path
    append_task_log(session, None, f"{artist_name} / {album_title}: album art set")


def cleanup_download_staging_batch(batch_id: str) -> None:
    staging_root = download_staging_root(batch_id)
    try:
        if staging_root.exists() and not any(staging_root.rglob("*")):
            staging_root.rmdir()
    except OSError:
        return


def replacement_target_path(track: Track | None, metadata: dict, file_path: Path) -> Path:
    if track and track.path:
        old_path = Path(track.path)
        return old_path.with_suffix(file_path.suffix.lower())
    return unique_destination(suggest_library_path(metadata, file_path))


def replace_library_track_file(session: Session, track: Track, source_path: Path, target_path: Path, payload: dict) -> None:
    settings = get_settings()
    old_path = Path(track.path) if track.path else None
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if old_path and old_path.exists():
        settings.trash_path.mkdir(parents=True, exist_ok=True)
        trash_path = unique_destination(settings.trash_path / old_path.name)
        append_task_log(session, None, f"{track.title}: moving lossy source to trash at {trash_path}")
        shutil.move(str(old_path), str(trash_path))
    elif target_path.exists():
        append_task_log(session, None, f"{track.title}: removing existing replacement target at {target_path}")
        target_path.unlink()
    moved_old_to_trash = old_path and old_path.exists() is False and "trash_path" in locals() and trash_path.exists()
    try:
        append_task_log(session, None, f"{track.title}: moving verified lossless replacement to {target_path}")
        shutil.move(str(source_path), str(target_path))
    except Exception:
        if moved_old_to_trash:
            old_path.parent.mkdir(parents=True, exist_ok=True)
            append_task_log(session, None, f"{track.title}: replacement move failed; restoring original file from {trash_path}", "warning")
            shutil.move(str(trash_path), str(old_path))
        raise
    metadata = payload.get("metadata", {})
    track.title = metadata.get("title") or track.title
    track.track_number = metadata.get("track_number")
    track.disc_number = metadata.get("disc_number")
    track.duration_ms = metadata.get("duration_ms")
    track.format = metadata.get("format")
    track.bitrate = metadata.get("bitrate")
    track.path = str(target_path)
    track.musicbrainz_recording_id = metadata.get("musicbrainz_recording_id")
    track.is_lossless = metadata.get("is_lossless", False)
    track.musicbrainz_verified = bool(metadata.get("musicbrainz_verified"))


def verify_downloaded_file(session: Session, file_path: Path, manifest_entry: dict | None) -> dict:
    if not manifest_entry:
        return {"status": "unknown", "message": "Download manifest entry is missing."}
    request = manifest_entry.get("request") or {}
    file_metadata = read_audio_metadata(file_path)
    expected_metadata = expected_download_musicbrainz_metadata(session, request)
    file_issue = downloaded_file_mismatch_reason(file_metadata, expected_metadata)
    if file_issue:
        return {"status": "mismatch", "request": request, "metadata": expected_metadata, "message": f"{file_path.name}: {file_issue}"}
    match = musicbrainz_download_file_match(file_metadata, expected_metadata, file_path)
    if match["matched"]:
        return {
            "status": "verified",
            "request": request,
            "metadata": verified_download_metadata({**file_metadata, **expected_metadata}, expected_metadata),
            "score": match.get("score"),
            "message": match.get("message"),
        }
    expected = download_query(expected_metadata)
    found = " ".join(
        str(part)
        for part in [file_metadata.get("albumartist") or file_metadata.get("artist"), file_metadata.get("album"), file_metadata.get("title") or file_path.stem]
        if part
    ).strip()
    return {
        "status": "mismatch",
        "request": request,
        "metadata": expected_metadata,
        "message": f"{file_path.name} matched {found or 'a different recording'} instead of {expected or 'the requested track'}.",
    }


def expected_download_musicbrainz_metadata(session: Session, request: dict) -> dict:
    expected = dict(request)
    expected["title"] = request.get("track") or request.get("title")
    artist = request.get("artist")
    album = request.get("album")
    if not artist or not album:
        return expected
    if expected.get("duration_ms") and (expected.get("musicbrainz_recording_id") or expected.get("title") or expected.get("track")):
        return expected
    try:
        album_record = lookup_album_tracks(str(artist), str(album), request.get("musicbrainz_album_id"))
    except Exception as error:  # noqa: BLE001 - keep downloads moving with request metadata if MusicBrainz is unavailable.
        append_task_log(session, None, f"MusicBrainz metadata lookup failed for {artist} / {album}: {error}", "warning")
        return expected
    best_track = best_musicbrainz_track_for_request(album_record, request)
    if best_track:
        expected.update(
            {
                "artist": album_record.get("artist") or artist,
                "albumartist": album_record.get("artist") or artist,
                "album": album_record.get("album") or album,
                "title": best_track.get("title") or expected.get("title"),
                "track": best_track.get("title") or expected.get("track"),
                "track_number": best_track.get("track_number") or expected.get("track_number"),
                "disc_number": best_track.get("disc_number") or expected.get("disc_number"),
                "duration_ms": best_track.get("length") or expected.get("duration_ms"),
                "musicbrainz_recording_id": best_track.get("musicbrainz_recording_id") or expected.get("musicbrainz_recording_id"),
                "musicbrainz_album_id": album_record.get("musicbrainz_album_id") or expected.get("musicbrainz_album_id"),
            }
        )
    return expected


def best_musicbrainz_track_for_request(album_record: dict, request: dict) -> dict | None:
    tracks = album_record.get("tracks") or []
    if not tracks:
        return None
    request_recording_id = normalize_match_text(request.get("musicbrainz_recording_id"))
    if request_recording_id:
        for track in tracks:
            if normalize_match_text(track.get("musicbrainz_recording_id")) == request_recording_id:
                return track
    scored = sorted(
        ((musicbrainz_request_track_score(track, request), track) for track in tracks),
        key=lambda item: item[0],
        reverse=True,
    )
    return scored[0][1] if scored and scored[0][0] >= 0.55 else None


def musicbrainz_request_track_score(track: dict, request: dict) -> float:
    title = request.get("track") or request.get("title")
    title_score = fuzzy_similarity(fuzzy_text(title), fuzzy_text(track.get("title"))) if title and track.get("title") else 0.0
    try:
        expected_number = int(request.get("track_number") or 0)
        track_number = int(track.get("track_number") or 0)
    except (TypeError, ValueError):
        expected_number = 0
        track_number = 0
    number_score = 1.0 if expected_number and track_number and expected_number == track_number else 0.0
    duration_score = duration_match_score(request_duration_ms(request), track.get("length"))
    return (title_score * 0.62) + (number_score * 0.28) + (duration_score * 0.10)


def musicbrainz_download_file_match(file_metadata: dict, expected: dict, file_path: Path) -> dict:
    expected_recording_id = normalize_match_text(expected.get("musicbrainz_recording_id"))
    file_recording_id = normalize_match_text(file_metadata.get("musicbrainz_recording_id"))
    if expected_recording_id and file_recording_id:
        matched = expected_recording_id == file_recording_id
        return {
            "matched": matched,
            "score": 1.0 if matched else 0.0,
            "message": "MusicBrainz recording id matched." if matched else "MusicBrainz recording id did not match.",
        }
    expected_title = fuzzy_text(expected.get("track") or expected.get("title"))
    file_title = fuzzy_text(file_metadata.get("title") or file_path.stem)
    title_score = fuzzy_similarity(expected_title, file_title) if expected_title and file_title else 0.0
    expected_artist = fuzzy_text(expected.get("albumartist") or expected.get("artist"))
    file_artist = fuzzy_text(file_metadata.get("albumartist") or file_metadata.get("artist"))
    artist_score = fuzzy_similarity(expected_artist, file_artist) if expected_artist and file_artist else 0.75
    expected_album = fuzzy_text(expected.get("album"))
    file_album = fuzzy_text(file_metadata.get("album"))
    album_score = fuzzy_similarity(expected_album, file_album) if expected_album and file_album else 0.75
    duration_score = duration_match_score(request_duration_ms(expected), file_metadata.get("duration_ms"))
    score = (title_score * 0.52) + (artist_score * 0.18) + (album_score * 0.12) + (duration_score * 0.18)
    if title_score < 0.76:
        return {"matched": False, "score": score, "message": f"title confidence {title_score:.0%} was too low"}
    if duration_score < 0.45:
        return {"matched": False, "score": score, "message": f"duration confidence {duration_score:.0%} was too low"}
    return {"matched": score >= 0.72, "score": score, "message": f"MusicBrainz metadata confidence {score:.0%}"}


def downloaded_file_mismatch_reason(metadata: dict, request: dict) -> str | None:
    expected_duration = request_duration_ms(request)
    actual_duration = metadata.get("duration_ms")
    if expected_duration and actual_duration:
        delta = abs(int(actual_duration) - int(expected_duration))
        tolerance = max(5000, int(expected_duration * 0.08))
        if delta > tolerance:
            return f"duration {round(int(actual_duration) / 1000)}s does not match expected {round(int(expected_duration) / 1000)}s"
    if request.get("require_lossless") and lossy_or_suspicious_audio(metadata):
        return "file is not a reliable lossless replacement"
    return None


def request_duration_ms(request: dict) -> int | None:
    raw = request.get("duration_ms") or request.get("length")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def lossy_or_suspicious_audio(metadata: dict) -> bool:
    fmt = str(metadata.get("format") or "").casefold()
    bitrate = int(metadata.get("bitrate") or 0)
    if fmt in {"mp3", "m4a", "aac", "ogg", "opus", "wma"}:
        return True
    if fmt in {"flac", "alac", "wav", "aiff", "aif"} and bitrate and bitrate < 650_000:
        return True
    return not metadata.get("is_lossless")


def metadata_matches_request(metadata: dict, request: dict) -> bool:
    request_recording_id = normalize_match_text(request.get("musicbrainz_recording_id"))
    metadata_recording_id = normalize_match_text(metadata.get("musicbrainz_recording_id"))
    if request_recording_id and metadata_recording_id:
        return request_recording_id == metadata_recording_id
    request_artist = normalize_match_text(request.get("artist"))
    request_track = normalize_match_text(request.get("track") or request.get("title"))
    metadata_artist = normalize_match_text(metadata.get("albumartist") or metadata.get("artist"))
    metadata_track = normalize_match_text(metadata.get("title"))
    if request_track and metadata_track and not loose_text_match(request_track, metadata_track):
        return False
    if request_artist and metadata_artist and not loose_text_match(request_artist, metadata_artist):
        return False
    return bool((request_track and metadata_track) or (request_artist and metadata_artist))


def verified_download_metadata(metadata: dict, request: dict) -> dict:
    artist = request.get("artist") or metadata.get("artist")
    return {
        **metadata,
        "artist": artist,
        "albumartist": artist,
        "album": request.get("album") or metadata.get("album"),
        "title": request.get("track") or request.get("title") or metadata.get("title"),
    }


def normalize_download_metadata(metadata: dict, request: dict) -> dict:
    artist = request.get("artist") or metadata.get("albumartist") or metadata.get("artist") or "Unknown Artist"
    album = request.get("album") or metadata.get("album") or "Unknown Album"
    title = request.get("track") or request.get("title") or metadata.get("title") or "Unknown Title"
    normalized = {
        **metadata,
        "artist": artist,
        "albumartist": artist,
        "album": album,
        "title": title,
        "track_number": request.get("track_number") or metadata.get("track_number"),
        "disc_number": request.get("disc_number") or metadata.get("disc_number"),
        "musicbrainz_album_id": request.get("musicbrainz_album_id") or metadata.get("musicbrainz_album_id"),
        "musicbrainz_recording_id": request.get("musicbrainz_recording_id") or metadata.get("musicbrainz_recording_id"),
    }
    normalized["format"] = metadata.get("format")
    normalized["is_lossless"] = metadata.get("is_lossless", False)
    return normalized


def loose_text_match(left: str, right: str) -> bool:
    if left == right:
        return True
    return left in right or right in left


def mark_matching_wishlist_completed(session: Session, metadata: dict) -> None:
    artist = normalize_match_text(metadata.get("albumartist") or metadata.get("artist"))
    album = normalize_match_text(metadata.get("album"))
    title = normalize_match_text(metadata.get("title"))
    if not artist or not title:
        return
    candidates = list(
        session.scalars(
            select(WishlistItem).where(WishlistItem.status.in_(["wanted", "review", "approved"]))
        )
    )
    for item in candidates:
        same_artist = normalize_match_text(item.artist) == artist
        same_album = not item.album or not album or normalize_match_text(item.album) == album
        same_track = normalize_match_text(item.track or item.album or item.artist) == title
        if same_artist and same_album and same_track:
            item.status = "completed"
            item.status_changed_at = datetime.now(timezone.utc)


def normalize_match_text(value: str | None) -> str:
    return " ".join(str(value or "").casefold().split())


def existing_library_and_proposal_paths(session: Session) -> set[str]:
    paths = {
        str(path)
        for path in session.scalars(select(Track.path).where(Track.path.is_not(None)))
        if path
    }
    paths.update(
        str(path)
        for path in session.scalars(
            select(ProposalItem.old_value)
            .join(ProposalBatch, ProposalBatch.id == ProposalItem.batch_id)
            .where(ProposalItem.old_value.is_not(None))
            .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
        )
        if path
    )
    return paths


def process_wishlist_request_items(session: Session, items: list[ProposalItem], task: Task | None = None) -> None:
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    wishlist_item_ids = []
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        artist = payload.get("artist") or "Unknown Artist"
        album = payload.get("album") or "Singles"
        workflow = payload.get("workflow") or "wishlist"
        if payload.get("wishlist_item_id"):
            wishlist_item_ids.append(payload["wishlist_item_id"])
        grouped.setdefault((workflow, artist, album), []).append(payload)
    append_task_log(session, task, f"Preparing download candidate searches for {len(items)} tracks across {len(grouped)} album batch(es)")
    for (workflow, artist, album), requests in grouped.items():
        create_album_download_candidate_batch(session, artist, album, requests, task, workflow=workflow)
    if wishlist_item_ids:
        for wishlist_item in session.scalars(select(WishlistItem).where(WishlistItem.id.in_(wishlist_item_ids))):
            wishlist_item.status = "approved"
            wishlist_item.status_changed_at = datetime.now(timezone.utc)


def apply_lyrics_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    if payload.get("action") != "download_lyrics":
        raise ValueError("Unsupported lyrics action")
    track = session.scalar(
        select(Track)
        .where(Track.id == payload.get("track_id"))
        .options(selectinload(Track.album).selectinload(Album.artist))
    )
    if not track or not track.path:
        raise ValueError("Track file no longer exists in the library")
    audio_path = Path(track.path)
    if not audio_path.exists():
        raise FileNotFoundError(f"{audio_path} is missing")
    lyric_text = fetch_lrclib_lyrics(track)
    if not lyric_text:
        raise ValueError("No synced or unsynced lyrics were found")
    lrc_path = audio_path.with_suffix(".lrc")
    lrc_path.write_text(lyric_text, encoding="utf-8")


def create_album_download_candidate_batch(session: Session, artist: str, album: str, requests: list[dict], task: Task | None = None, tree_path: str = "/task-queue", workflow: str = "wishlist") -> None:
    title_prefix = {
        "lossless_replacement": "Lossless replacement candidates",
        "missing_tracks": "Missing track candidates",
    }.get(workflow, "Download candidates")
    batch = ProposalBatch(title=f"{title_prefix}: {artist} / {album}", kind=ProposalKind.download, tree_path=tree_path)
    session.add(batch)
    session.flush()
    append_task_log(session, task, f"Created download candidate batch for {artist} / {album} with {len(requests)} requested track(s)")
    artist_item = ProposalItem(
        batch_id=batch.id,
        title=artist,
        kind=ProposalKind.download,
        payload_json=json.dumps({"artist": artist}),
    )
    session.add(artist_item)
    session.flush()
    album_item = ProposalItem(
        batch_id=batch.id,
        parent_id=artist_item.id,
        title=album,
        kind=ProposalKind.download,
        payload_json=json.dumps({"artist": artist, "album": album}),
    )
    session.add(album_item)
    session.flush()
    track_items: list[tuple[dict, str, str]] = []
    for request in requests:
        query = download_query(request)
        track_title = request.get("track") or request.get("title") or query
        track_item = ProposalItem(
            batch_id=batch.id,
            parent_id=album_item.id,
            title=track_title,
            kind=ProposalKind.download,
            payload_json=json.dumps({"artist": artist, "album": album, "track": track_title, "status": "queued"}),
        )
        session.add(track_item)
        session.flush()
        track_items.append((request, track_item.id, track_title))
    session.commit()

    slskd_tracks = 0
    retry_tracks = 0
    diagnostic_lines = []
    append_task_log(session, task, f"{artist} / {album}: searching slskd for lossless album folders")
    folder_try_limit = slskd_album_folder_try_limit(integration_settings(session))
    album_queries = album_search_query_variants(artist, album, requests)
    # Report progress across the whole prepare phase: one step per album-search query, then one
    # per track, so the "x of n" counter keeps climbing during the (slow) album search too.
    total_tracks = max(1, len(album_queries) + len(track_items))
    search_progress = {"done": 0}

    def album_search_progress(message: str) -> None:
        search_progress["done"] = min(search_progress["done"] + 1, len(album_queries))
        if task is not None:
            update_task_progress(session, task, search_progress["done"], total_tracks, message)

    folder_pools = search_album_folder_pools(session, artist, album, requests, task, limit=folder_try_limit, progress_callback=album_search_progress)
    # Credit any queries skipped by an early exit so the counter flows into the per-track phase.
    search_progress["done"] = len(album_queries)
    folder_pool = folder_pools[0] if folder_pools else None
    if folder_pool:
        diagnostic_lines.append(
            f"{artist} {album}: using {folder_pool.get('folder') or 'matched folder'} from {folder_pool.get('username')} for {folder_pool.get('matched_tracks', 0)} tracks."
        )
        append_task_log(
            session,
            task,
            f"{artist} / {album}: matched {len(folder_pools)} lossless album folder(s); trying up to {folder_try_limit} folder(s), best is from {folder_pool.get('username')} with {folder_pool.get('matched_tracks', 0)} track(s) already matched",
        )
    else:
        append_task_log(session, task, f"{artist} / {album}: no reusable album folder was found", "warning")
    search_jobs = []
    completed_tracks = search_progress["done"]
    integration = integration_settings(session)
    slskd_url = integration.get("slskd_url", "")
    api_key = integration.get("slskd_api_key", "")
    match_threshold = slskd_album_match_threshold(integration)
    if folder_pool and not folder_pool.get("match_threshold"):
        folder_pool["match_threshold"] = match_threshold
    for request, track_item_id, track_title in track_items:
        track_item = session.get(ProposalItem, track_item_id)
        if not track_item:
            append_task_log(session, task, f"{track_title}: skipped candidate preparation because the review row was removed", "warning")
            continue
        query = download_query(request)
        set_item_payload_status(track_item, f"searching {track_title}")
        folder_candidates = candidates_from_folder_pools(folder_pools, request, limit=5, max_pools=folder_try_limit) if folder_pools else []
        if folder_candidates:
            confidence = folder_candidates[0].get("confidence")
            diagnostic_lines.append(f"{query}: reused {folder_candidates[0].get('folder') or 'the same folder'} from {folder_candidates[0].get('username')} at {confidence}% confidence.")
            append_task_log(session, task, f"{track_title}: {len(folder_candidates)} album-folder candidate(s) ready after trying up to {folder_try_limit} folder(s); best {folder_candidates[0].get('filename')} at {confidence}% confidence")
            add_download_candidate_items(session, batch, track_item, request, query, folder_candidates)
            slskd_tracks += 1
            set_item_payload_status(track_item, f"{len(folder_candidates)} candidates ready")
            completed_tracks += 1
            if task is not None:
                update_task_progress(session, task, completed_tracks, total_tracks, f"Prepared download candidate for {track_title}")
        elif folder_pools:
            retry_tracks += 1
            set_item_payload_status(track_item, "no album-folder track match")
            add_ytdlp_fallback_item(session, batch, track_item, request, query, selected=False)
            append_task_log(session, task, f"{track_title}: no lossless track match in {min(len(folder_pools), folder_try_limit)} album folder(s); YouTube fallback left unselected", "warning")
            completed_tracks += 1
            if task is not None:
                update_task_progress(session, task, completed_tracks, total_tracks, f"Prepared fallback for {track_title}")
        elif should_use_track_search_fallback(album, requests):
            candidate_limit = 5
            search_jobs.append((request, track_item.id, track_title, query, candidate_limit))
        else:
            retry_tracks += 1
            set_item_payload_status(track_item, "no album folder candidates found")
            add_ytdlp_fallback_item(session, batch, track_item, request, query, selected=False)
            append_task_log(session, task, f"{track_title}: no matching lossless album folders found; YouTube fallback left unselected", "warning")
            completed_tracks += 1
            if task is not None:
                update_task_progress(session, task, completed_tracks, total_tracks, f"Prepared fallback for {track_title}")
        session.commit()

    if search_jobs:
        workers = min(SLSKD_TRACK_SEARCH_WORKERS, len(search_jobs))
        append_task_log(session, task, f"{artist} / {album}: searching {len(search_jobs)} track(s) with {workers} concurrent worker(s)")
        if task is not None:
            update_task_progress(session, task, completed_tracks, total_tracks, f"Searching {len(search_jobs)} download candidates")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(search_slskd_for_request_with_settings, slskd_url, api_key, request, candidate_limit): (request, track_item_id, track_title, query)
                for request, track_item_id, track_title, query, candidate_limit in search_jobs
            }
            for future in as_completed(futures):
                request, track_item_id, track_title, query = futures[future]
                try:
                    search_result = future.result()
                except Exception as error:  # noqa: BLE001 - keep creating candidates for the rest of the album.
                    search_result = {
                        "candidates": [],
                        "diagnostics": {"queries": download_query_variants(request), "query_logs": [f"slskd search failed for {query}: {error}"]},
                    }
                for line in search_result.get("diagnostics", {}).get("query_logs") or []:
                    append_task_log(session, task, line)
                track_item = session.get(ProposalItem, track_item_id)
                if not track_item:
                    append_task_log(session, task, f"{track_title}: skipped candidate results because the review row was removed", "warning")
                    completed_tracks += 1
                    continue
                candidates = search_result["candidates"]
                diagnostic_lines.append(slskd_diagnostic_body(query, search_result["diagnostics"]))
                if candidates and not request.get("multiple_candidates") and not folder_pool:
                    folder_pool = download_folder_pool(candidates[0])
                    if folder_pool:
                        folder_pool["match_threshold"] = match_threshold
                if candidates:
                    add_download_candidate_items(session, batch, track_item, request, query, candidates)
                    slskd_tracks += 1
                    set_item_payload_status(track_item, "candidate ready")
                    append_task_log(session, task, f"{track_title}: {len(candidates)} slskd candidate(s) ready")
                else:
                    retry_tracks += 1
                    rate_limited = bool(search_result.get("diagnostics", {}).get("rate_limited"))
                    status = "slskd rate limited; no candidate yet" if rate_limited else "no slskd candidate found"
                    set_item_payload_status(track_item, status)
                    if rate_limited:
                        append_task_log(session, task, f"{track_title}: {status}", "warning")
                    else:
                        add_ytdlp_fallback_item(session, batch, track_item, request, query, selected=False)
                        append_task_log(session, task, f"{track_title}: no slskd candidates found; YouTube fallback left unselected", "warning")
                completed_tracks += 1
                session.commit()
                if task is not None:
                    update_task_progress(session, task, completed_tracks, total_tracks, f"Prepared download candidate for {track_title}")
    session.flush()
    append_task_log(session, task, f"{artist} / {album}: candidate search finished with {slskd_tracks} slskd track(s) and {retry_tracks} track(s) needing fallback or attention")
    create_notification(
        session,
        title="Download candidates ready",
        body=f"{album}: {slskd_tracks} tracks with slskd candidates. {retry_tracks} tracks need fallback or attention. {' '.join(diagnostic_lines[:3])}",
        event_type="approval_needed",
        target_url="/task-queue" if tree_path != "/downloads" else "/downloads",
    )


def add_download_candidate_items(session: Session, batch: ProposalBatch, track_item: ProposalItem, request: dict, query: str, candidates: list[dict]) -> None:
    for index, candidate in enumerate(candidates):
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=track_item.id,
                title=f"slskd: {candidate.get('filename') or query}",
                kind=ProposalKind.download,
                selected=index == 0,
                old_value=query,
                new_value=candidate.get("username"),
                payload_json=json.dumps(
                    {
                        "action": "queue_download",
                        "request": request,
                        "candidate": candidate,
                        "candidate_index": index,
                        "status": candidate_status_label(candidate),
                    }
                ),
            )
        )


def candidate_status_label(candidate: dict) -> str:
    confidence = candidate.get("confidence")
    parts = []
    if confidence is not None:
        parts.append(f"{confidence}% match")
    if candidate.get("same_album_folder"):
        parts.append("same album folder")
    quality = candidate.get("quality")
    if quality:
        parts.append(str(quality))
    return " · ".join(parts) if parts else "candidate"


def add_ytdlp_fallback_item(session: Session, batch: ProposalBatch, track_item: ProposalItem, request: dict, query: str, selected: bool = True) -> None:
    session.add(
        ProposalItem(
            batch_id=batch.id,
            parent_id=track_item.id,
            title=f"YouTube fallback: {query}",
            kind=ProposalKind.download,
            selected=selected,
            old_value=query,
            new_value="yt-dlp",
            payload_json=json.dumps(
                {
                    "action": "queue_ytdlp_download",
                    "request": request,
                }
            ),
        )
    )


def create_download_candidate_batch(session: Session, request: dict) -> None:
    query = download_query(request)
    batch = ProposalBatch(title=f"Download candidates: {query}", kind=ProposalKind.download, tree_path="/downloads")
    session.add(batch)
    session.flush()
    track_parent = add_download_tree_parents(session, batch, request, query)
    set_item_payload_status(track_parent, f"searching {query}")
    session.commit()
    search_result = search_slskd_for_request(session, request, limit=4 if request.get("multiple_candidates") else 1)
    candidates = search_result["candidates"]
    diagnostics = search_result["diagnostics"]
    if not candidates:
        session.delete(batch)
        create_notification(
            session,
            title="slskd search found no candidates",
            body=slskd_diagnostic_body(query, diagnostics),
            event_type="download_empty",
            target_url="/activity",
        )
        create_ytdlp_fallback_batch(session, request, query)
        return
    for index, candidate in enumerate(candidates):
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=track_parent.id,
                title=candidate.get("filename") or query,
                kind=ProposalKind.download,
                selected=index == 0,
                old_value=query,
                new_value=candidate.get("username"),
                payload_json=json.dumps(
                    {
                        "action": "queue_download",
                        "request": request,
                        "candidate": candidate,
                    }
                ),
            )
        )
        session.commit()
    set_item_payload_status(track_parent, "candidate ready")
    session.flush()
    create_notification(
        session,
        title="Download candidates ready",
        body=f"{len(candidates)} slskd candidates found for {query}. {slskd_diagnostic_body(query, diagnostics)}",
        event_type="approval_needed",
        target_url="/downloads",
    )


def create_ytdlp_fallback_batch(session: Session, request: dict, query: str) -> None:
    batch = ProposalBatch(title=f"YouTube fallback: {query}", kind=ProposalKind.download, tree_path="/downloads")
    session.add(batch)
    session.flush()
    track_parent = add_download_tree_parents(session, batch, request, query)
    session.add(
        ProposalItem(
            batch_id=batch.id,
            parent_id=track_parent.id,
            title=f"YouTube fallback: {query}",
            kind=ProposalKind.download,
            old_value=query,
            new_value="yt-dlp",
            payload_json=json.dumps(
                {
                    "action": "queue_ytdlp_download",
                    "request": request,
                }
            ),
        )
    )
    create_notification(
        session,
        title="Download fallback ready",
        body=query,
        event_type="approval_needed",
        target_url="/downloads",
    )


def set_item_payload_status(item: ProposalItem, status: str, download_progress: dict | None = None) -> None:
    payload = json.loads(item.payload_json or "{}")
    payload["status"] = status
    if download_progress is not None:
        payload["download_progress"] = download_progress
    item.payload_json = json.dumps(payload)


def set_download_item_status(
    item: ProposalItem | None,
    status: str,
    *,
    stage: str | None = None,
    progress: float | None = None,
    indeterminate: bool | None = None,
) -> None:
    if not item:
        return
    progress_payload = download_progress_payload(status, stage=stage, progress=progress, indeterminate=indeterminate)
    set_item_payload_status(item, status, progress_payload)
    if item.parent and item.parent.kind == ProposalKind.download:
        set_item_payload_status(item.parent, status, progress_payload)
        item.parent.status = ProposalStatus.executing


def download_progress_payload(status: str, *, stage: str | None = None, progress: float | None = None, indeterminate: bool | None = None) -> dict:
    lowered = str(status or "").casefold()
    value = max(0.0, min(100.0, float(progress))) if isinstance(progress, (int, float)) else None
    resolved_stage = stage
    resolved_indeterminate = bool(indeterminate)
    if resolved_stage is None:
        if any(token in lowered for token in ("need attention", "failed", "mismatch", "could not be verified")):
            resolved_stage = "failed"
            value = 0 if value is None else value
        elif "verified" in lowered:
            resolved_stage = "verified"
            value = 100
        elif "importing" in lowered:
            resolved_stage = "importing"
            value = 100
            resolved_indeterminate = True
        elif "verifying" in lowered:
            resolved_stage = "verifying"
            value = 100 if value is None else value
            resolved_indeterminate = True
        elif any(token in lowered for token in ("downloaded", "staged", "moving completed file")):
            resolved_stage = "staging"
            value = 100
            resolved_indeterminate = "moving" in lowered
        elif match := re.search(r"downloading\s+(\d+(?:\.\d+)?)%", lowered):
            resolved_stage = "downloading"
            value = max(0.0, min(100.0, float(match.group(1))))
        elif any(token in lowered for token in ("waiting to download", "queued", "requested", "initializing", "checking transfer state")):
            resolved_stage = "queued"
            value = 0 if value is None else value
        elif any(token in lowered for token in ("searching", "retrying", "replacement")):
            resolved_stage = "queued"
            value = 0 if value is None else value
            resolved_indeterminate = True
        else:
            resolved_stage = "queued"
            value = 0 if value is None else value
    return {
        "stage": resolved_stage,
        "value": 0 if value is None else value,
        "label": status,
        "indeterminate": resolved_indeterminate,
    }


def download_folder_pool(candidate: dict) -> dict | None:
    folder_files = candidate.get("folder_files") or []
    if not folder_files:
        return None
    return {
        "username": candidate.get("username"),
        "folder": candidate.get("folder"),
        "files": folder_files,
        "query": candidate.get("query"),
        "free_upload_slots": candidate.get("free_upload_slots"),
        "upload_speed": candidate.get("upload_speed"),
        "queue_length": candidate.get("queue_length"),
    }


def search_album_folder_pool(session: Session, artist: str, album: str, requests: list[dict], task: Task | None = None) -> dict | None:
    pools = search_album_folder_pools(session, artist, album, requests, task, limit=1)
    return pools[0] if pools else None


def search_album_folder_pools(session: Session, artist: str, album: str, requests: list[dict], task: Task | None = None, limit: int = 5, progress_callback=None) -> list[dict]:
    if not requests:
        return []
    settings = integration_settings(session)
    queries = album_search_query_variants(artist, album, requests)
    if not queries:
        return []
    match_threshold = slskd_album_match_threshold(settings)
    folder_try_limit = min(max(1, limit), slskd_album_folder_try_limit(settings))
    requested_track_count = len(requests)
    pools: dict[tuple[str, str], dict] = {}
    for query_index, query in enumerate(queries, start=1):
        if progress_callback is not None:
            progress_callback(f"Searching album folders ({query_index}/{len(queries)})")
        try:
            append_task_log(session, task, f"slskd album search started: {query}")
            result = search_slskd_detailed(
                settings.get("slskd_url", ""),
                settings.get("slskd_api_key", ""),
                query,
                limit=120,
                poll_interval=SLSKD_ALBUM_SEARCH_POLL_INTERVAL,
                timeout_seconds=SLSKD_ALBUM_SEARCH_TIMEOUT_SECONDS,
                timeout_buffer_seconds=SLSKD_ALBUM_SEARCH_BUFFER_SECONDS,
                wait_for_settled_results=True,
            )
            diagnostics = result.get("diagnostics") or {}
            append_task_log(
                session,
                task,
                (
                    f"slskd album search finished: {query}: "
                    f"{diagnostics.get('responses', 0)} responses, {diagnostics.get('files', 0)} files, "
                    f"{len(result.get('folder_candidates') or [])} folder candidates "
                    f"(slskd reported {diagnostics.get('response_count', 0)} responses/{diagnostics.get('file_count', 0)} files, "
                    f"complete={diagnostics.get('is_complete')})"
                ),
            )
            if not diagnostics.get("responses"):
                append_task_log(session, task, f"slskd album search response shape for {query}: {diagnostics.get('payload_shape') or 'unknown'}", "warning")
        except Exception as error:
            append_task_log(session, task, f"slskd album search failed: {query}: {error}", "warning")
            continue
        added_from_query = 0
        for candidate in result.get("folder_candidates") or result.get("candidates", []):
            pool = download_folder_pool(candidate)
            if not pool:
                continue
            pool = lossless_folder_pool(pool)
            if not pool:
                continue
            key = (str(pool.get("username") or ""), str(pool.get("folder") or ""))
            current = pools.setdefault(key, {**pool, "files": [], "queries": []})
            current["queries"].append(query)
            known_filenames = {str(file_info.get("filename") or "") for file_info in current["files"]}
            for file_info in pool.get("files") or []:
                filename = str(file_info.get("filename") or "")
                if filename and filename not in known_filenames:
                    current["files"].append(file_info)
                    known_filenames.add(filename)
                    added_from_query += 1
        ranked_after_query = ranked_album_folder_pools(pools, requests, match_threshold)
        accepted_after_query = accepted_album_folder_pools(ranked_after_query, match_threshold)
        append_task_log(
            session,
            task,
            f"{artist} / {album}: {query} added {added_from_query} lossless file(s); {len(accepted_after_query)} folder(s) now meet album confidence",
        )
        best_matched = max((score[0] for score, _pool in accepted_after_query), default=0)
        # Stop as soon as a single folder already covers the whole album — more query variants
        # won't improve on a complete, confident match and just cost time.
        if best_matched >= requested_track_count:
            append_task_log(session, task, f"{artist} / {album}: found a complete album folder ({best_matched}/{requested_track_count} tracks); stopping album search early")
            break
        if len(accepted_after_query) >= folder_try_limit:
            append_task_log(session, task, f"{artist} / {album}: found {len(accepted_after_query)} matching lossless folder(s); stopping album search at configured try limit {folder_try_limit}")
            break
    ranked = ranked_album_folder_pools(pools, requests, match_threshold)
    accepted = accepted_album_folder_pools(ranked, match_threshold)
    if not accepted:
        append_task_log(session, task, f"{artist} / {album}: album queries did not locate enough confident folders; skipping individual track seed searches", "warning")
    if not pools:
        return []
    if not accepted:
        if ranked:
            best_score, best_pool = ranked[0]
            append_task_log(
                session,
                task,
                (
                    f"{artist} / {album}: best folder rejected: {best_pool.get('folder') or 'unknown folder'} "
                    f"matched {best_score[0]} tracks, artist score {best_score[2]:.2f}, album folder score {best_score[3]:.2f}, "
                    f"{best_score[4]} lossless file(s)"
                ),
                "warning",
            )
        return []
    result_pools = []
    for score, pool in accepted[:folder_try_limit]:
        prepared = {**pool}
        prepared["matched_tracks"] = score[0]
        prepared["match_threshold"] = match_threshold
        prepared["album_query"] = ", ".join(prepared.get("queries") or queries)
        prepared["album_folder_score"] = {
            "matched": score[0],
            "confidence_total": score[1],
            "artist": score[2],
            "album": score[3],
            "lossless": score[4],
            "files": score[5],
        }
        result_pools.append(prepared)
    best_score, best_pool = accepted[0]
    append_task_log(
        session,
        task,
        (
            f"{artist} / {album}: best album folder {best_pool.get('folder') or 'unknown folder'} scored "
            f"artist {best_score[2]:.2f}, album folder {best_score[3]:.2f}, matched {best_score[0]} requested track(s), "
            f"{best_score[4]} lossless file(s)"
        ),
    )
    return result_pools


# Cap how many folders get the expensive per-track file matching. A popular album can return
# hundreds of valid folders from slskd; fully scoring every one against every requested track is
# what made candidate preparation take minutes. We only need a handful of folders to try.
ALBUM_FOLDER_FULL_SCORE_LIMIT = 16


def ranked_album_folder_pools(pools: dict[tuple[str, str], dict], requests: list[dict], threshold: float) -> list[tuple[tuple[int, float, float, float, int, int], dict]]:
    expected_artist = fuzzy_text(requests[0].get("artist")) if requests else ""
    expected_album = str(requests[0].get("album") or "") if requests else ""
    # Phase 1: cheap pre-rank using only folder-name signals (no per-track file matching).
    prelim: list[tuple[float, float, int, dict]] = []
    for pool in pools.values():
        files = lossless_folder_files(pool.get("files") or [])
        folder_segments = album_folder_segments(pool)
        artist_score = best_segment_score(expected_artist, folder_segments)
        album_score = album_folder_name_score(expected_album, pool)
        prelim.append((album_score, artist_score, len(files), pool))
    prelim.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    # Phase 2: fully score only the most promising folders; the rest keep their cheap scores so
    # they remain available but rank below the fully-scored matches.
    scored: list[tuple[tuple[int, float, float, float, int, int], dict]] = []
    for index, (album_score, artist_score, lossless, pool) in enumerate(prelim):
        if index < ALBUM_FOLDER_FULL_SCORE_LIMIT and lossless and album_score >= threshold:
            scored.append((score_album_folder_pool(pool, requests, threshold), pool))
        else:
            scored.append(((0, 0.0, artist_score, album_score, lossless, lossless), pool))
    return sorted(scored, key=album_folder_pool_sort_key, reverse=True)


def add_seed_track_folder_pools(
    session: Session,
    task: Task | None,
    settings: dict[str, str],
    artist: str,
    album: str,
    requests: list[dict],
    pools: dict[tuple[str, str], dict],
    match_threshold: float,
    folder_try_limit: int,
) -> None:
    seed_requests = representative_album_seed_requests(requests, limit=min(3, folder_try_limit))
    for index, request in enumerate(seed_requests, start=1):
        track_title = request.get("track") or request.get("title") or f"track {index}"
        append_task_log(session, task, f"{artist} / {album}: seed search {index}/{len(seed_requests)} using {track_title}")
        result = search_slskd_for_request_with_settings(
            settings.get("slskd_url", ""),
            settings.get("slskd_api_key", ""),
            {**request, "artist": artist, "album": album, "require_lossless": True, "multiple_candidates": True},
            limit=8,
        )
        for line in result.get("diagnostics", {}).get("query_logs") or []:
            append_task_log(session, task, line)
        added = 0
        for candidate in result.get("candidates") or []:
            pool = lossless_folder_pool(download_folder_pool(candidate))
            if not pool:
                continue
            key = (str(pool.get("username") or ""), str(pool.get("folder") or ""))
            current = pools.setdefault(key, {**pool, "files": [], "queries": []})
            current["queries"].append(str(candidate.get("query") or download_query(request)))
            known_filenames = {str(file_info.get("filename") or "") for file_info in current["files"]}
            for file_info in pool.get("files") or []:
                filename = str(file_info.get("filename") or "")
                if filename and filename not in known_filenames:
                    current["files"].append(file_info)
                    known_filenames.add(filename)
                    added += 1
        ranked = ranked_album_folder_pools(pools, requests, match_threshold)
        accepted = accepted_album_folder_pools(ranked, match_threshold)
        append_task_log(session, task, f"{artist} / {album}: seed search from {track_title} added {added} lossless file(s); {len(accepted)} folder(s) now meet album confidence")
        if accepted:
            break


def representative_album_seed_requests(requests: list[dict], limit: int) -> list[dict]:
    if not requests or limit <= 0:
        return []
    if len(requests) <= limit:
        return requests
    indexes = [0, len(requests) // 2, len(requests) - 1]
    chosen = []
    seen = set()
    for index in indexes:
        if index in seen:
            continue
        seen.add(index)
        chosen.append(requests[index])
        if len(chosen) >= limit:
            break
    return chosen


def accepted_album_folder_pools(ranked: list[tuple[tuple[int, float, float, float, int, int], dict]], threshold: float) -> list[tuple[tuple[int, float, float, float, int, int], dict]]:
    return [
        (score, pool)
        for score, pool in ranked
        if score[4] > 0 and score[3] >= threshold and (score[2] >= 0.25 or score[3] >= 0.92)
    ]


def album_folder_pool_sort_key(item: tuple[tuple[int, float, float, float, int, int], dict]) -> tuple[float, float, int, int, float, int]:
    score, _pool = item
    matched, confidence_total, artist_score, album_score, lossless, files = score
    average_track_confidence = confidence_total / max(matched, 1)
    return (album_score, artist_score, lossless, matched, average_track_confidence, -files)


def slskd_album_match_threshold(settings: dict[str, str]) -> float:
    try:
        value = float(settings.get("slskd_album_match_threshold") or 72)
    except (TypeError, ValueError):
        value = 72
    return max(0.5, min(0.95, value / 100 if value > 1 else value))


def slskd_album_folder_try_limit(settings: dict[str, str]) -> int:
    try:
        value = int(settings.get("slskd_album_folder_tries") or 5)
    except (TypeError, ValueError):
        value = 5
    return max(1, min(12, value))


def score_album_folder_pool(pool: dict, requests: list[dict], threshold: float) -> tuple[int, float, float, float, int, int]:
    matched = 0
    confidence_total = 0.0
    artist_scores = []
    files = lossless_folder_files(pool.get("files") or [])
    lossless = len(files)
    folder_segments = album_folder_segments(pool)
    artist_score = best_segment_score(fuzzy_text(requests[0].get("artist") if requests else ""), folder_segments)
    album_score = album_folder_name_score(str(requests[0].get("album") or "") if requests else "", pool)
    prepared_pool = {**pool, "files": files, "match_threshold": threshold}
    for request in requests:
        candidate = candidate_from_folder_pool(prepared_pool, request, threshold)
        if not candidate:
            continue
        matched += 1
        confidence_total += float(candidate.get("confidence") or 0)
        artist_scores.append(float(candidate.get("artist_score") or 0))
    if artist_scores:
        artist_score = max(artist_score, sum(artist_scores) / max(1, len(artist_scores)))
    return (matched, confidence_total, artist_score, album_score, lossless, len(files))


def lossless_folder_pool(pool: dict | None) -> dict | None:
    if not pool:
        return None
    files = lossless_folder_files(pool.get("files") or [])
    if not files:
        return None
    return {**pool, "files": files}


def lossless_folder_files(files: list[dict]) -> list[dict]:
    return [file_info for file_info in files if is_lossless_filename(str(file_info.get("filename") or ""))]


def is_lossless_filename(filename: str) -> bool:
    return str(filename or "").lower().endswith(LOSSLESS_AUDIO_EXTENSIONS)


def album_folder_segments(pool: dict) -> list[str]:
    folder = str(pool.get("folder") or "")
    if not folder:
        filenames = [str(file_info.get("filename") or "") for file_info in pool.get("files") or []]
        folder = str(Path(filenames[0].replace("\\", "/")).parent) if filenames else ""
    return [segment for segment in re.split(r"[/\\]+", folder) if segment]


def album_folder_name_score(album: str, pool: dict) -> float:
    expected = fuzzy_text(album)
    if not expected:
        return 0.5
    segments = album_folder_segments(pool)
    if not segments:
        return 0.0
    scored_segments = [fuzzy_text(segment) for segment in segments]
    scored_segments.append(fuzzy_text(" ".join(segments[-2:])))
    scored_segments.append(fuzzy_text(" ".join(segments)))
    return max(fuzzy_similarity(expected, segment) for segment in scored_segments if segment)


def candidate_from_folder_pool(pool: dict | None, request: dict, threshold: float | None = None) -> dict | None:
    if not pool:
        return None
    threshold = threshold if threshold is not None else float(pool.get("match_threshold") or 0.72)
    track = request.get("track") or request.get("title")
    if not track:
        return None
    files = lossless_folder_files(pool.get("files") or [])
    ranked = sorted(
        (
            (download_file_match_score(file_info, request, username=pool.get("username"), folder=pool.get("folder")), file_info)
            for file_info in files
        ),
        key=lambda item: folder_file_score(item[1], item[0].get("confidence", 0.0)),
    )
    ranked = [(score, file_info) for score, file_info in ranked if score.get("confidence", 0.0) >= threshold and not score.get("rejected")]
    if not ranked:
        return None
    score, file_info = ranked[0]
    confidence = float(score.get("confidence") or 0.0)
    return {
        "username": pool.get("username"),
        "query": download_query(request),
        "filename": file_info.get("filename"),
        "folder": pool.get("folder"),
        "size": file_info.get("size"),
        "duration": file_info.get("duration"),
        "bitrate": file_info.get("bitrate"),
        "free_upload_slots": pool.get("free_upload_slots"),
        "upload_speed": pool.get("upload_speed"),
        "queue_length": pool.get("queue_length"),
        "quality": "lossless" if is_lossless_filename(str(file_info.get("filename") or "")) else "unknown",
        "confidence": round(confidence * 100),
        "artist_score": round(float(score.get("artist_score") or 0.0), 3),
        "album_score": round(float(score.get("album_score") or 0.0), 3),
        "title_score": round(float(score.get("title_score") or 0.0), 3),
        "duration_score": round(float(score.get("duration_score") or 0.0), 3),
        "files": [file_info],
        "folder_files": files,
    }


def candidates_from_folder_pools(pools: list[dict], request: dict, limit: int = 5, max_pools: int | None = None) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    pool_limit = max_pools if max_pools is not None else len(pools)
    for pool_index, pool in enumerate(pools[:pool_limit], start=1):
        candidate = candidate_from_folder_pool(pool, request)
        if not candidate:
            continue
        identity = candidate_identity(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        candidate["same_album_folder"] = True
        candidate["album_folder_rank"] = pool_index
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def folder_file_confidence(file_info: dict, request: dict) -> float:
    return float(download_file_match_score(file_info, request).get("confidence") or 0.0)


def download_file_match_score(file_info: dict, request: dict, username: object | None = None, folder: object | None = None) -> dict:
    title = fuzzy_text(request.get("track") or request.get("title"))
    artist = fuzzy_text(request.get("artist"))
    album = fuzzy_text(request.get("album"))
    if not title:
        return {"confidence": 0.0, "rejected": True, "reason": "missing expected title"}
    filename = str(file_info.get("filename") or "")
    stem = Path(filename.replace("\\", "/")).stem
    path_segments = [segment for segment in re.split(r"[/\\]+", filename) if segment]
    folder_segments = [segment for segment in re.split(r"[/\\]+", str(folder or "")) if segment]
    segments = [*folder_segments, *(path_segments[-1:] if folder_segments else path_segments)]
    variants = {
        fuzzy_text(stem),
        fuzzy_text(strip_leading_track_number(stem)),
        fuzzy_text(remove_known_album_terms(stem, request)),
        fuzzy_text(strip_leading_track_number(remove_known_album_terms(stem, request))),
    }
    variants.discard("")
    if not variants:
        return {"confidence": 0.0, "rejected": True, "reason": "missing candidate title"}
    title_score = max(fuzzy_similarity(title, variant) for variant in variants)
    artist_score = best_segment_score(artist, segments)
    album_score = best_segment_score(album, folder_segments or path_segments[:-1])
    if artist and artist_score < 0.25:
        return {
            "confidence": 0.0,
            "title_score": title_score,
            "artist_score": artist_score,
            "album_score": album_score,
            "duration_score": 0.0,
            "rejected": True,
            "reason": "artist not present in candidate path",
        }
    candidate_version_words = version_words_for_text(stem)
    expected_version_words = version_words_for_text(str(request.get("track") or request.get("title") or ""))
    extra_version_words = candidate_version_words - expected_version_words
    if extra_version_words:
        return {
            "confidence": 0.0,
            "title_score": title_score,
            "artist_score": artist_score,
            "album_score": album_score,
            "duration_score": 0.0,
            "rejected": True,
            "reason": f"wrong version marker: {', '.join(sorted(extra_version_words))}",
        }
    if title_score < 0.30:
        return {
            "confidence": 0.0,
            "title_score": title_score,
            "artist_score": artist_score,
            "album_score": album_score,
            "duration_score": 0.0,
            "rejected": True,
            "reason": "title match too weak",
        }
    if any(fuzzy_text(segment) in JUNK_ARTIST_SEGMENTS for segment in segments):
        artist_score *= 0.4
    duration_score = duration_match_score(request_duration_ms(request), candidate_duration_ms(file_info))
    number = leading_track_number(stem)
    request_number = request.get("track_number")
    try:
        request_number_int = int(request_number) if request_number is not None else None
    except (TypeError, ValueError):
        request_number_int = None
    track_number_bonus = 0.0
    if number is not None and request_number_int is not None:
        track_number_bonus = 0.08 if number == request_number_int else -0.18
    album_bonus = 0.08 if album and album_score >= 0.85 else 0.03 if album and album_score >= 0.60 else 0.0
    confidence = (title_score * 0.45) + (artist_score * 0.38) + (duration_score * 0.12) + album_bonus + track_number_bonus
    if title_score < 0.55 and artist_score < 0.75:
        confidence *= 0.7
    return {
        "confidence": max(0.0, min(1.0, confidence)),
        "title_score": title_score,
        "artist_score": artist_score,
        "album_score": album_score,
        "duration_score": duration_score,
        "rejected": False,
        "reason": "matched",
        "username": username,
    }


def candidate_duration_ms(file_info: dict) -> int | None:
    raw = file_info.get("duration") or file_info.get("Duration") or file_info.get("length") or file_info.get("Length")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(value if value > 10000 else value * 1000)


def duration_match_score(expected_ms: int | None, candidate_ms: int | None) -> float:
    if not expected_ms or not candidate_ms:
        return 0.5
    delta = abs(expected_ms - candidate_ms)
    if delta <= 5000:
        return 1.0
    return max(0.0, 1.0 - (delta / max(expected_ms, 1)) * 5)


def best_segment_score(expected: str, segments: list[str]) -> float:
    if not expected:
        return 0.5
    expected_tokens = set(expected.split())
    best = 0.0
    for segment in segments:
        normalized = fuzzy_text(segment)
        if not normalized:
            continue
        segment_tokens = set(normalized.split())
        if normalized in JUNK_ARTIST_SEGMENTS:
            best = max(best, 0.0)
            continue
        if expected_tokens and expected_tokens.issubset(segment_tokens):
            return 1.0
        if expected and (expected in normalized or normalized in expected):
            best = max(best, 0.95)
        best = max(best, SequenceMatcher(None, expected, normalized).ratio())
    return best


def version_words_for_text(value: object) -> set[str]:
    return set(fuzzy_text(value).split()) & DOWNLOAD_VERSION_WORDS


def fuzzy_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    sequence_score = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    overlap_score = len(left_tokens & right_tokens) / max(1, len(left_tokens))
    containment_score = 1.0 if left in right or right in left else 0.0
    return max(sequence_score, overlap_score * 0.92, containment_score)


def fuzzy_text(value: object) -> str:
    text = str(value or "").casefold().replace("’", "'")
    for token, alternatives in TEXT_SEARCH_ALTERNATIVES:
        replacement = alternatives[0]
        if token.isdigit():
            text = re.sub(rf"\b{re.escape(token)}\b", f" {token} {replacement} ", text)
        else:
            text = text.replace(token, f" {replacement} ")
    text = re.sub(r"\[[^\]]+\]|\([^\)]+\)", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def strip_leading_track_number(value: str) -> str:
    return re.sub(r"^\s*\d{1,3}\s*[-_. )]+", "", str(value or ""))


def leading_track_number(value: str) -> int | None:
    match = re.match(r"^\s*(\d{1,3})\s*[-_. )]+", str(value or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def remove_known_album_terms(value: str, request: dict) -> str:
    text = fuzzy_text(value)
    for field in ("artist", "album"):
        for token in fuzzy_text(request.get(field)).split():
            text = re.sub(rf"\b{re.escape(token)}\b", " ", text)
    return " ".join(text.split())


def has_extra_version_words(stem: str, normalized_title: str) -> bool:
    title_tokens = set(normalized_title.split())
    version_words = {"remix", "instrumental", "acapella", "karaoke", "live", "demo", "edit", "clean", "explicit"}
    stem_tokens = set(fuzzy_text(stem).split())
    return bool((stem_tokens - title_tokens) & version_words)


def folder_file_score(file_info: dict, confidence: float) -> tuple[int, float, str]:
    filename = str(file_info.get("filename") or "")
    quality_rank = 0 if is_lossless_filename(filename) else 1
    return (quality_rank, -confidence, filename)


def search_slskd_for_request(session: Session, request: dict, limit: int = 1, task: Task | None = None) -> dict:
    settings = integration_settings(session)
    result = search_slskd_for_request_with_settings(settings.get("slskd_url", ""), settings.get("slskd_api_key", ""), request, limit)
    for line in result.get("diagnostics", {}).get("query_logs") or []:
        append_task_log(session, task, line)
    return result


def rank_slskd_candidates_for_request(candidates: list[dict], request: dict, ignored_candidates: list[dict], limit: int) -> tuple[list[dict], list[str]]:
    accepted = []
    rejected_reasons: list[str] = []
    for candidate in filter_ignored_candidates(candidates, ignored_candidates):
        file_info = {
            "filename": candidate.get("filename"),
            "size": candidate.get("size"),
            "duration": candidate.get("duration"),
            "bitrate": candidate.get("bitrate"),
        }
        score = download_file_match_score(file_info, request, username=candidate.get("username"), folder=candidate.get("folder"))
        reason = str(score.get("reason") or "unknown")
        confidence = float(score.get("confidence") or 0.0)
        enriched = {
            **candidate,
            "confidence": round(confidence * 100),
            "title_score": round(float(score.get("title_score") or 0.0), 3),
            "artist_score": round(float(score.get("artist_score") or 0.0), 3),
            "album_score": round(float(score.get("album_score") or 0.0), 3),
            "duration_score": round(float(score.get("duration_score") or 0.0), 3),
            "match_reason": reason,
        }
        if request.get("require_lossless") and enriched.get("quality") != "lossless":
            if len(rejected_reasons) < 4:
                rejected_reasons.append(f"{candidate.get('filename') or 'unknown'} ({round(confidence * 100)}%): not lossless")
            continue
        if not score.get("rejected") and confidence >= MIN_SLSKD_TRACK_CONFIDENCE:
            accepted.append(enriched)
        elif len(rejected_reasons) < 4:
            rejected_reasons.append(f"{candidate.get('filename') or 'unknown'} ({round(confidence * 100)}%): {reason}")
    accepted.sort(key=slskd_candidate_sort_key, reverse=True)
    return accepted[:limit], rejected_reasons


def slskd_candidate_sort_key(candidate: dict) -> tuple[int, int, int, float, int, int]:
    quality = candidate.get("quality")
    quality_score = 2 if quality == "lossless" else 1 if quality == "lossy" else 0
    free_slots = 1 if candidate.get("free_upload_slots") else 0
    try:
        upload_speed = float(candidate.get("upload_speed") or 0)
    except (TypeError, ValueError):
        upload_speed = 0
    try:
        queue_length = int(candidate.get("queue_length") or 0)
    except (TypeError, ValueError):
        queue_length = 0
    try:
        size = int(candidate.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return (int(candidate.get("confidence") or 0), quality_score, free_slots, upload_speed, -queue_length, size)


def search_slskd_for_request_with_settings(slskd_url: str, api_key: str, request: dict, limit: int = 1) -> dict:
    attempted = []
    query_logs = []
    rate_limited = False
    raw_ignored_candidates = request.get("ignored_candidates") or []
    ignored_candidates = raw_ignored_candidates if isinstance(raw_ignored_candidates, list) else []
    ignored = {candidate_identity(candidate) for candidate in ignored_candidates if isinstance(candidate, dict)}
    last_result = {"candidates": [], "diagnostics": {"queries": [], "query_logs": query_logs, "rate_limited": False}}
    for index, query in enumerate(download_query_variants(request)[:SLSKD_TRACK_QUERY_LIMIT]):
        if not query or query in attempted:
            continue
        attempted.append(query)
        query_logs.append(f"slskd track search started: {query}")
        result = None
        for attempt in range(7):
            try:
                result = search_slskd_detailed(
                    slskd_url,
                    api_key,
                    query,
                    limit=max(12, len(ignored) + limit * 8),
                    poll_interval=0.75,
                    timeout_seconds=10 if index < 3 else 8,
                    timeout_buffer_seconds=2,
                )
                break
            except Exception as error:  # noqa: BLE001 - keep trying lower-confidence query variants.
                if is_rate_limit_error(error) and attempt < 6:
                    rate_limited = True
                    delay = min(10.0, 2.0 * (attempt + 1))
                    query_logs.append(f"slskd track search rate limited: {query}; retrying in {delay:g}s")
                    time.sleep(delay)
                    continue
                rate_limited = rate_limited or is_rate_limit_error(error)
                query_logs.append(f"slskd track search failed: {query}: {error}")
                last_result = {
                    "candidates": [],
                    "diagnostics": {
                        "queries": attempted.copy(),
                        "query_logs": query_logs,
                        "query": query,
                        "rate_limited": rate_limited,
                    },
                }
                break
        if result is None:
            if rate_limited:
                query_logs.append(f"slskd track search deferred after rate limits: {query}; trying the next query variant")
                time.sleep(3)
            continue
        result["diagnostics"]["query"] = query
        result["diagnostics"]["queries"] = attempted.copy()
        result["diagnostics"]["query_logs"] = query_logs
        result["diagnostics"]["rate_limited"] = rate_limited
        result["candidates"], rejected = rank_slskd_candidates_for_request(result.get("candidates") or [], request, ignored_candidates, limit)
        if rejected:
            result["diagnostics"]["rejected_candidates"] = rejected
        result["diagnostics"]["ignored_candidates"] = len(ignored_candidates)
        last_result = result
        diagnostics = result["diagnostics"]
        query_logs.append(
            (
                f"slskd track search finished: {query}: "
                f"{diagnostics.get('responses', 0)} responses, {diagnostics.get('files', 0)} files, "
                f"{len(result.get('candidates') or [])} usable candidates after {diagnostics.get('polls', 0)} polls "
                f"(slskd reported {diagnostics.get('response_count', 0)} responses/{diagnostics.get('file_count', 0)} files, "
                f"complete={diagnostics.get('is_complete')})"
            ),
        )
        if not diagnostics.get("responses"):
            query_logs.append(f"slskd track search response shape for {query}: {diagnostics.get('payload_shape') or 'unknown'}")
        for rejected_line in rejected:
            query_logs.append(f"slskd candidate rejected: {rejected_line}")
        if result["candidates"]:
            return result
    last_result["diagnostics"]["queries"] = attempted
    last_result["diagnostics"]["query_logs"] = query_logs
    last_result["diagnostics"]["rate_limited"] = rate_limited
    query_logs.append(f"slskd found no candidates after {len(attempted)} query variant(s): {download_query(request)}")
    return last_result


def is_rate_limit_error(error: Exception) -> bool:
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429


def add_download_tree_parents(session: Session, batch: ProposalBatch, request: dict, query: str) -> ProposalItem:
    artist = request.get("artist") or "Unknown Artist"
    album = request.get("album") or "Singles"
    track = request.get("track") or request.get("title") or query
    artist_item = ProposalItem(
        batch_id=batch.id,
        title=artist,
        kind=ProposalKind.download,
        selected=True,
        payload_json=json.dumps({"artist": artist}),
    )
    session.add(artist_item)
    session.flush()
    album_item = ProposalItem(
        batch_id=batch.id,
        parent_id=artist_item.id,
        title=album,
        kind=ProposalKind.download,
        selected=True,
        payload_json=json.dumps({"artist": artist, "album": album}),
    )
    session.add(album_item)
    session.flush()
    track_item = ProposalItem(
        batch_id=batch.id,
        parent_id=album_item.id,
        title=track,
        kind=ProposalKind.download,
        selected=True,
        payload_json=json.dumps({"artist": artist, "album": album, "track": track}),
    )
    session.add(track_item)
    session.flush()
    return track_item


def slskd_diagnostic_body(query: str, diagnostics: dict) -> str:
    queries = diagnostics.get("queries") or [diagnostics.get("query") or query]
    return (
        f"{query}: searched {', '.join(str(item) for item in queries if item)}; "
        f"last search {diagnostics.get('search_id', 'unknown')} returned "
        f"{diagnostics.get('responses', 0)} responses, {diagnostics.get('files', 0)} files, "
        f"{diagnostics.get('candidates', 0)} candidates after {diagnostics.get('polls', 0)} polls "
        f"(state: {diagnostics.get('state') or 'unknown'}, timeout: {diagnostics.get('timeout_seconds', 'unknown')}s)."
    )


def queue_ytdlp_download(session: Session, request: dict) -> None:
    settings = get_settings()
    integration = integration_settings(session)
    query = download_query(request)
    if not query:
        raise ValueError("Download query is empty")
    settings.downloads_path.mkdir(parents=True, exist_ok=True)
    command = [
        "yt-dlp",
        f"ytsearch1:{query}",
        "--no-playlist",
        "--paths",
        str(settings.downloads_path),
        "--extract-audio",
        "--audio-format",
        "mp3",
    ]
    cookies_path = integration.get("youtube_cookies_path") or ""
    if cookies_path and Path(cookies_path).exists():
        command.extend(["--cookies", cookies_path])
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or "").strip()
        if not details:
            details = f"exit code {error.returncode}"
        raise RuntimeError(f"yt-dlp failed for {query}: {details[-1200:]}") from error
    create_notification(
        session,
        title="YouTube fallback queued",
        body=f"{query}: {result.stdout[-600:].strip() if result.stdout else 'download command completed'}",
        event_type="download_queued",
        target_url="/downloads",
        deliver_apns=False,
    )


def fetch_lrclib_lyrics(track: Track) -> str | None:
    duration_seconds = round((track.duration_ms or 0) / 1000) if track.duration_ms else None
    params = {
        "track_name": track.title,
        "artist_name": track.album.artist.name,
        "album_name": track.album.title,
    }
    if duration_seconds:
        params["duration"] = str(duration_seconds)
    headers = {"User-Agent": "Nudibranch/0.1 (https://github.com/Poplel/Nudibranch)"}
    with httpx.Client(base_url="https://lrclib.net", headers=headers, timeout=20) as client:
        response = client.get("/api/get", params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    return payload.get("syncedLyrics") or payload.get("plainLyrics")


def download_query(request: dict) -> str:
    return " ".join(str(part) for part in [request.get("artist"), request.get("album"), request.get("track")] if part).strip()


def album_search_query_variants(artist: str, album: str, requests: list[dict]) -> list[str]:
    artist_values = text_search_values(artist, max_values=2)
    album_values = text_search_values(album, max_values=3)
    primary_artist = artist_values[0] if artist_values else artist
    primary_album = album_values[0] if album_values else album
    years = release_year_values(requests)
    require_lossless = any(request.get("require_lossless") for request in requests)
    flac_variants = [
        " ".join(part for part in [primary_artist, primary_album, "flac"] if part),
        " ".join(part for part in [primary_album, primary_artist, "flac"] if part),
    ]
    variants: list[str] = []
    # For lossless requests the FLAC-qualified queries narrow straight to the target folders, so
    # try them first; otherwise they go last as a fallback.
    if require_lossless:
        variants.extend(flac_variants)
    variants.append(" ".join(part for part in [primary_artist, primary_album] if part))
    variants.append(" ".join(part for part in [primary_album, primary_artist] if part))
    for year in years[:2]:
        variants.append(" ".join(part for part in [primary_album, year] if part))
        variants.append(" ".join(part for part in [primary_artist, primary_album, year] if part))
    for album_value in album_values[1:]:
        variants.append(" ".join(part for part in [primary_artist, album_value] if part))
        variants.append(" ".join(part for part in [album_value, primary_artist] if part))
    for artist_value in artist_values[1:]:
        variants.append(" ".join(part for part in [artist_value, primary_album] if part))
    if not require_lossless:
        variants.extend(flac_variants)
    return unique_nonempty(variants)


def release_year_values(requests: list[dict]) -> list[str]:
    years: list[str] = []
    for request in requests:
        for key in ("year", "date", "release_date", "released", "album_year", "album_date"):
            match = re.search(r"\b(19\d{2}|20\d{2})\b", str(request.get(key) or ""))
            if match:
                years.append(match.group(1))
    return unique_nonempty(years)


def download_query_variants(request: dict) -> list[str]:
    artist = str(request.get("artist") or "").strip()
    album = str(request.get("album") or "").strip()
    track = str(request.get("track") or request.get("title") or "").strip()
    album_values = text_search_values(album, max_values=3)
    track_values = text_search_values(track, max_values=2)[:2]
    variants = []
    primary_album = album_values[0] if album_values else album
    primary_track = track_values[0] if track_values else track
    variants.append(" ".join(part for part in [artist, primary_album, primary_track] if part))
    variants.append(" ".join(part for part in [artist, primary_track] if part))
    variants.append(f"{artist} - {primary_track}".strip(" -"))
    variants.append(" ".join(part for part in [primary_album, primary_track] if part))
    variants.append(primary_track)
    for album_value in album_values[1:]:
        variants.append(" ".join(part for part in [artist, album_value, primary_track] if part))
    if request.get("require_lossless"):
        variants.append(" ".join(part for part in [artist, primary_album, primary_track, "flac"] if part))
    variants.extend(
        " ".join(part for part in [artist, primary_album, track_value] if part)
        for track_value in track_values[1:]
    )
    return unique_nonempty(variants)


def text_search_values(value: str, max_values: int = 8) -> list[str]:
    values = [value]
    for token, alternatives in TEXT_SEARCH_ALTERNATIVES:
        if token not in value:
            continue
        for alternative in alternatives:
            if token.isdigit():
                values.append(re.sub(rf"\b{re.escape(token)}\b", alternative, value, flags=re.IGNORECASE))
            else:
                values.append(value.replace(token, alternative))
                values.append(value.replace(token, alternative.title()))
    return unique_nonempty(values)[:max_values]


def unique_nonempty(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        normalized = " ".join(str(value or "").split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def apply_artist_changes(session: Session, artist: Artist, changes: dict) -> None:
    apply_scalar_changes(artist, changes, {"name", "sort_name", "musicbrainz_id"})
    matching_artist = session.scalar(select(Artist).where(Artist.name == artist.name, Artist.id != artist.id))
    if not matching_artist:
        return
    for album in list(artist.albums):
        matching_album = session.scalar(
            select(Album).where(Album.artist_id == matching_artist.id, Album.title == album.title, Album.id != album.id)
        )
        if matching_album:
            for track in album.tracks:
                track.album_id = matching_album.id
        else:
            album.artist_id = matching_artist.id


def apply_album_changes(session: Session, album: Album, changes: dict) -> None:
    apply_scalar_changes(
        album,
        changes,
        {"title", "release_title", "path", "cover_path", "musicbrainz_release_id", "musicbrainz_release_group_id"},
    )
    matching_album = session.scalar(
        select(Album).where(Album.artist_id == album.artist_id, Album.title == album.title, Album.id != album.id)
    )
    if not matching_album:
        return
    for track in album.tracks:
        track.album_id = matching_album.id


def apply_scalar_changes(target, changes: dict, allowed_fields: set[str]) -> None:
    for key, value in changes.items():
        if key in allowed_fields:
            setattr(target, key, value)


def editable_track_fields() -> set[str]:
    return {
        "title",
        "track_number",
        "disc_number",
        "duration_ms",
        "format",
        "bitrate",
        "path",
        "musicbrainz_recording_id",
        "explicit",
        "is_lossless",
        "musicbrainz_verified",
        "metadata_locked",
        "artwork_locked",
        "filename_locked",
    }


def apply_file_action_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    source_path = Path(item.old_value or "")
    track = session.get(Track, payload.get("track_id"))
    if payload.get("action") == "normalize_volume":
        normalize_audio_file_volume(source_path)
        return
    if payload.get("action") == "remove_record":
        if not track:
            raise ValueError("Track record no longer exists")
        album = track.album
        session.delete(track)
        session.flush()
        cleanup_empty_album_artist(session, album)
        return
    if payload.get("action") == "delete_file":
        settings = get_settings()
        library_root = settings.library_path.resolve()
        resolved = source_path.resolve()
        if library_root not in [resolved, *resolved.parents]:
            raise ValueError("File must be inside the library folder")
        if not resolved.is_file():
            raise FileNotFoundError(f"{resolved} no longer exists")
        resolved.unlink()
        return
    if payload.get("action") == "trash_duplicate":
        settings = get_settings()
        if source_path.exists():
            settings.trash_path.mkdir(parents=True, exist_ok=True)
            trash_dest = unique_destination(settings.trash_path / source_path.name)
            shutil.move(str(source_path), str(trash_dest))
            append_task_log(session, None, f"Moved duplicate to trash: {trash_dest.name}")
        if track:
            album = track.album
            session.delete(track)
            session.flush()
            cleanup_empty_album_artist(session, album)
        return
    target_path = unique_destination(Path(item.new_value or ""))
    if not source_path.exists():
        raise FileNotFoundError(f"{source_path} no longer exists")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_path), str(target_path))
    if track:
        album = track.album
        session.delete(track)
        session.flush()
        cleanup_empty_album_artist(session, album)


def normalize_audio_file_volume(source_path: Path) -> None:
    if not source_path.exists():
        raise FileNotFoundError(f"{source_path} no longer exists")
    temp_path = source_path.with_name(f".nudibranch-normalized-{source_path.name}")
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-map_metadata",
        "0",
        str(temp_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=3600)
    backup_path = unique_destination(get_settings().trash_path / source_path.name)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_path), str(backup_path))
    shutil.move(str(temp_path), str(source_path))


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No available destination for {path}")


def cleanup_empty_album_artist(session: Session, album: Album | None) -> None:
    if not album:
        return
    artist = album.artist
    track_count = session.scalar(select(func.count()).select_from(Track).where(Track.album_id == album.id))
    if track_count == 0:
        session.delete(album)
        session.flush()
    if artist:
        album_count = session.scalar(select(func.count()).select_from(Album).where(Album.artist_id == artist.id))
        if album_count == 0:
            session.delete(artist)


def run_process_wishlist(session: Session, _payload: dict) -> dict:
    items = list(session.scalars(select(WishlistItem).where(WishlistItem.status == "wanted").order_by(WishlistItem.artist, WishlistItem.album, WishlistItem.track)))
    if not items:
        return {"items": 0, "batch_id": None}

    batch = ProposalBatch(title="Wishlist download review", kind=ProposalKind.download, tree_path="/wishlist")
    session.add(batch)
    session.flush()
    for wishlist_item in items:
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=download_query(
                    {
                        "artist": wishlist_item.artist,
                        "album": wishlist_item.album,
                        "track": wishlist_item.track,
                    }
                ),
                kind=ProposalKind.download,
                payload_json=json.dumps(
                    {
                        "action": "wishlist_request",
                        "user_id": wishlist_item.user_id,
                        "wishlist_item_id": wishlist_item.id,
                        "kind": wishlist_item.kind,
                        "artist": wishlist_item.artist,
                        "album": wishlist_item.album,
                        "track": wishlist_item.track,
                    }
                ),
            )
        )
        wishlist_item.status = "review"
        wishlist_item.status_changed_at = datetime.now(timezone.utc)
    create_notification(
        session,
        title="Wishlist review ready",
        body=f"{len(items)} wishlist items are ready for approval.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"items": len(items), "batch_id": batch.id}


def run_sync_favorites_jellyfin(session: Session, _payload: dict) -> dict:
    settings = integration_settings(session)
    jellyfin_url = settings.get("jellyfin_url", "").rstrip("/")
    jellyfin_api_key = settings.get("jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_api_key:
        raise ValueError("Jellyfin URL and API key are required to sync playlists")

    conflict_winner = settings.get("playlist_conflict_winner") or "nudibranch"
    if conflict_winner not in {"nudibranch", "jellyfin"}:
        conflict_winner = "nudibranch"

    headers = {"X-Emby-Token": jellyfin_api_key}
    synced_playlists = 0
    pushed_tracks = 0
    pulled_tracks = 0
    with httpx.Client(base_url=jellyfin_url, headers=headers, timeout=25) as client:
        users = client.get("/Users")
        users.raise_for_status()
        user_id = (users.json() or [{}])[0].get("Id")
        if not user_id:
            raise ValueError("No Jellyfin user was found for playlist sync")

        jellyfin_playlists = list_jellyfin_playlists(client, user_id)
        playlists = list(session.scalars(select(Playlist).order_by(Playlist.name.asc())))
        local_by_name = {playlist.name: playlist for playlist in playlists}
        imported_playlist_names = set()
        for jellyfin_playlist in jellyfin_playlists.values():
            name = jellyfin_playlist.get("Name")
            if not name or name in local_by_name:
                continue
            playlist = Playlist(name=name, jellyfin_playlist_id=jellyfin_playlist.get("Id"))
            session.add(playlist)
            session.flush()
            local_by_name[name] = playlist
            playlists.append(playlist)
            imported_playlist_names.add(name)

        ensure_favorites_playlist(session)
        playlists = list(
            session.scalars(
                select(Playlist)
                .options(selectinload(Playlist.tracks).selectinload(PlaylistTrack.track).selectinload(Track.album).selectinload(Album.artist))
                .order_by(Playlist.name.asc())
            )
        )

        for playlist in playlists:
            jellyfin_playlist_id = playlist.jellyfin_playlist_id or (jellyfin_playlists.get(playlist.name) or {}).get("Id")
            jellyfin_items: list[dict] = []
            created_jellyfin_playlist = False
            if jellyfin_playlist_id:
                if playlist.jellyfin_playlist_id != jellyfin_playlist_id:
                    playlist.jellyfin_playlist_id = jellyfin_playlist_id
                try:
                    jellyfin_items = jellyfin_playlist_items(client, user_id, jellyfin_playlist_id)
                except JellyfinPlaylistMissing:
                    append_task_log(session, None, f"Jellyfin playlist id for {playlist.name} was stale; resolving it again", "warning")
                    if playlist.jellyfin_playlist_id == jellyfin_playlist_id:
                        playlist.jellyfin_playlist_id = None
                    refreshed_playlists = list_jellyfin_playlists(client, user_id)
                    refreshed_id = (refreshed_playlists.get(playlist.name) or {}).get("Id")
                    jellyfin_playlists.update(refreshed_playlists)
                    if refreshed_id and refreshed_id != jellyfin_playlist_id:
                        jellyfin_playlist_id = refreshed_id
                        playlist.jellyfin_playlist_id = refreshed_id
                        jellyfin_items = jellyfin_playlist_items(client, user_id, jellyfin_playlist_id)
                    else:
                        jellyfin_playlist_id = None
            if not jellyfin_playlist_id:
                created = create_jellyfin_playlist(client, user_id, playlist.name)
                jellyfin_playlist_id = jellyfin_playlist_id_from_response(created)
                playlist.jellyfin_playlist_id = jellyfin_playlist_id
                session.flush()
                jellyfin_items = []
                created_jellyfin_playlist = True
            local_item_ids: list[str] = []
            unmapped_local_tracks: list[str] = []
            for entry in sorted(playlist.tracks, key=lambda entry: (entry.position, entry.created_at)):
                item_id = find_jellyfin_audio_item(client, user_id, entry.track)
                if item_id:
                    local_item_ids.append(item_id)
                else:
                    unmapped_local_tracks.append(entry.track.title)
                    append_task_log(session, None, f"{playlist.name}: could not find Jellyfin audio item for {entry.track.title}", "warning")
            jellyfin_item_ids = [item.get("Id") for item in jellyfin_items if item.get("Id")]
            should_pull_from_jellyfin = (conflict_winner == "jellyfin" and not created_jellyfin_playlist) or playlist.name in imported_playlist_names
            if should_pull_from_jellyfin:
                pulled_tracks += sync_playlist_from_jellyfin(session, playlist, jellyfin_items)
            else:
                if unmapped_local_tracks:
                    append_task_log(session, None, f"{playlist.name}: skipped Jellyfin overwrite because {len(unmapped_local_tracks)} Nudibranch track(s) are not visible in Jellyfin", "warning")
                elif jellyfin_item_ids != local_item_ids:
                    try:
                        pushed_tracks += replace_jellyfin_playlist_items(client, user_id, jellyfin_playlist_id, jellyfin_items, local_item_ids)
                    except JellyfinPlaylistMissing:
                        append_task_log(session, None, f"Jellyfin playlist {playlist.name} disappeared during sync; recreating it", "warning")
                        created = create_jellyfin_playlist(client, user_id, playlist.name)
                        jellyfin_playlist_id = jellyfin_playlist_id_from_response(created)
                        playlist.jellyfin_playlist_id = jellyfin_playlist_id
                        session.flush()
                        pushed_tracks += replace_jellyfin_playlist_items(client, user_id, jellyfin_playlist_id, [], local_item_ids)
            synced_playlists += 1
    session.commit()
    create_notification(
        session,
        title="Playlists synced",
        body=f"{synced_playlists} playlists synced. {pushed_tracks} Jellyfin changes. {pulled_tracks} Nudibranch changes.",
        event_type="tool_completed",
        target_url="/playlists",
        deliver_apns=False,
    )
    return {"synced": synced_playlists, "pushed_tracks": pushed_tracks, "pulled_tracks": pulled_tracks}


def run_jellyfin_scan(session: Session, _payload: dict) -> dict:
    settings = integration_settings(session)
    jellyfin_url = settings.get("jellyfin_url", "").rstrip("/")
    jellyfin_api_key = settings.get("jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_api_key:
        raise ValueError("Jellyfin URL and API key are required")
    with httpx.Client(base_url=jellyfin_url, headers={"X-Emby-Token": jellyfin_api_key}, timeout=25) as client:
        response = client.post("/Library/Refresh")
        response.raise_for_status()
    create_notification(session, title="Jellyfin scan queued", body="Library refresh was requested.", event_type="tool_completed", target_url="/tools")
    return {"requested": True}


def run_clear_discover_cache(session: Session, _payload: dict) -> dict:
    removed = clear_discover_art_cache()
    create_notification(session, title="Discover cache cleared", body=f"{removed} cached file(s) removed.", event_type="tool_completed", target_url="/tools")
    return {"removed": removed}


def run_check_files(session: Session, _payload: dict) -> dict:
    settings = get_settings()
    library_root = settings.library_path.resolve()
    audio_suffixes = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".alac"}
    tracks = list(
        session.scalars(
            select(Track)
            .where(Track.path.is_not(None))
            .options(selectinload(Track.album).selectinload(Album.artist))
        )
    )
    tracks_by_path: dict[str, Track] = {}
    for track in tracks:
        if not track.path:
            continue
        try:
            track_path = Path(track.path).resolve()
            track_path.relative_to(library_root)
        except (OSError, ValueError):
            continue
        tracks_by_path[str(track_path)] = track
    db_paths = set(tracks_by_path)
    disk_paths = {
        str(path.resolve())
        for path in settings.library_path.rglob("*")
        if path.is_file() and path.suffix.lower() in audio_suffixes
    }
    missing_file_paths = sorted(path for path in db_paths if not Path(path).exists())
    missing_record_paths = sorted(path for path in disk_paths if path not in db_paths)
    # Read metadata once for every file on disk that no record claims; it is reused both to
    # relink moved files and to create records for genuinely new files.
    orphan_records = {path: file_info_for_existing_library_file(Path(path)) for path in missing_record_paths}
    # A migration (e.g. moving Nudibranch to a new VM) usually relocates files rather than
    # deleting them. Re-point records at the matching file on disk instead of marking every
    # track as needing a redownload.
    relinked, consumed_orphans, relinked_paths = relink_moved_library_files(session, tracks_by_path, missing_file_paths, orphan_records)
    if relinked:
        append_task_log(session, None, f"File check relinked {relinked} record(s) to files that moved on disk")
        session.flush()
    missing_file_paths = [path for path in missing_file_paths if path not in relinked_paths]
    missing_records = [info for path, info in orphan_records.items() if path not in consumed_orphans]
    missing_files = [
        {
            "track_id": tracks_by_path[path].id,
            "path": path,
            "title": tracks_by_path[path].title,
            "artist": tracks_by_path[path].album.artist.name,
            "album": tracks_by_path[path].album.title,
            "track_number": tracks_by_path[path].track_number,
        }
        for path in missing_file_paths
    ]
    queued_missing_files = queue_missing_file_downloads(session, missing_files)
    queued_missing_records = queue_missing_record_imports(session, missing_records)
    create_notification(
        session,
        title="File check complete",
        body=f"Relinked {relinked} moved file(s). {len(missing_files)} records still missing files. {len(missing_records)} files missing records. {queued_missing_files + queued_missing_records} fixes added to the task queue.",
        event_type="tool_completed",
        target_url="/task-queue",
    )
    return {
        "relinked": relinked,
        "missing_files": missing_files,
        "missing_records": [],
        "queued_missing_files": queued_missing_files,
        "queued_missing_records": queued_missing_records,
    }


def relink_moved_library_files(
    session: Session,
    tracks_by_path: dict[str, Track],
    missing_file_paths: list[str],
    orphan_records: dict[str, dict],
) -> tuple[int, set[str], set[str]]:
    """Re-point records whose file moved on disk (e.g. after a VM migration) at the matching
    orphan file instead of redownloading it.

    Returns (relinked_count, consumed_orphan_paths, relinked_record_paths). Only orphan files
    (present on disk but claimed by no record) are eligible targets, and each is used at most
    once, so relinking never creates duplicate or conflicting paths.
    """
    consumed: set[str] = set()
    relinked_paths: set[str] = set()
    if not missing_file_paths or not orphan_records:
        return 0, consumed, relinked_paths

    orphans_by_basename: dict[str, list[str]] = {}
    orphans_by_recording: dict[str, list[str]] = {}
    orphans_by_metakey: dict[tuple[str, str, str], list[str]] = {}
    for path, info in orphan_records.items():
        orphans_by_basename.setdefault(Path(path).name.casefold(), []).append(path)
        metadata = info.get("metadata") or {}
        recording_id = normalize_match_text(metadata.get("musicbrainz_recording_id"))
        if recording_id:
            orphans_by_recording.setdefault(recording_id, []).append(path)
        metakey = (
            normalize_match_text(metadata.get("albumartist") or metadata.get("artist")),
            normalize_match_text(metadata.get("album")),
            normalize_match_text(metadata.get("title")),
        )
        if any(metakey):
            orphans_by_metakey.setdefault(metakey, []).append(path)

    album_moves: dict[str, str] = {}
    relinked = 0
    for old_path in missing_file_paths:
        track = tracks_by_path.get(old_path)
        if not track:
            continue
        match = relink_candidate(track, old_path, orphans_by_basename, orphans_by_recording, orphans_by_metakey, consumed)
        if not match:
            continue
        track.path = match
        consumed.add(match)
        relinked_paths.add(old_path)
        relinked += 1
        if track.album_id:
            album_moves[track.album_id] = str(Path(match).parent)

    if relinked:
        for album_id, parent in album_moves.items():
            album = session.get(Album, album_id)
            if album:
                album.path = parent
        session.flush()
    return relinked, consumed, relinked_paths


def relink_candidate(
    track: Track,
    old_path: str,
    orphans_by_basename: dict[str, list[str]],
    orphans_by_recording: dict[str, list[str]],
    orphans_by_metakey: dict[tuple[str, str, str], list[str]],
    consumed: set[str],
) -> str | None:
    old_basename = Path(old_path).name.casefold()

    # 1. MusicBrainz recording id is the strongest signal and survives re-tagging/renames.
    recording_id = normalize_match_text(track.musicbrainz_recording_id)
    if recording_id:
        available = [path for path in orphans_by_recording.get(recording_id, []) if path not in consumed]
        if len(available) == 1:
            return available[0]
        if len(available) > 1:
            by_name = [path for path in available if Path(path).name.casefold() == old_basename]
            if len(by_name) == 1:
                return by_name[0]
            suffix = longest_path_suffix_match(old_path, available)
            if suffix:
                return suffix

    # 2. Same filename (the common case: a plain move/copy keeps filenames intact).
    available = [path for path in orphans_by_basename.get(old_basename, []) if path not in consumed]
    if len(available) == 1:
        return available[0]
    if len(available) > 1:
        suffix = longest_path_suffix_match(old_path, available)
        if suffix:
            return suffix

    # 3. Artist / album / title metadata (handles files renamed during the migration).
    album = track.album
    metakey = (
        normalize_match_text(album.artist.name if album and album.artist else None),
        normalize_match_text(album.title if album else None),
        normalize_match_text(track.title),
    )
    if any(metakey):
        available = [path for path in orphans_by_metakey.get(metakey, []) if path not in consumed]
        if len(available) == 1:
            return available[0]
        if len(available) > 1:
            return longest_path_suffix_match(old_path, available)
    return None


def longest_path_suffix_match(old_path: str, candidates: list[str]) -> str | None:
    """Pick the candidate sharing the longest trailing path-component run with old_path.

    Returns a match only when a single candidate wins outright, so ambiguous cases are left
    for manual review rather than guessed.
    """
    old_parts = [part.casefold() for part in Path(old_path).parts]
    best_len = 0
    best: list[str] = []
    for candidate in candidates:
        candidate_parts = [part.casefold() for part in Path(candidate).parts]
        shared = 0
        for left, right in zip(reversed(old_parts), reversed(candidate_parts)):
            if left != right:
                break
            shared += 1
        if shared > best_len:
            best_len = shared
            best = [candidate]
        elif shared == best_len:
            best.append(candidate)
    if best_len >= 1 and len(best) == 1:
        return best[0]
    return None


def queue_missing_file_downloads(session: Session, missing_files: list[dict]) -> int:
    if not missing_files:
        return 0
    existing = existing_missing_file_download_keys(session)
    rows = []
    for record in missing_files:
        key = missing_file_download_key(record.get("artist"), record.get("album"), record.get("title"))
        if key in existing:
            continue
        rows.append(record)
        existing.add(key)
    if not rows:
        return 0
    batch = ProposalBatch(title="Download missing library files", kind=ProposalKind.download, tree_path="/library")
    session.add(batch)
    session.flush()
    for record in rows:
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=record["title"],
                kind=ProposalKind.download,
                payload_json=json.dumps(
                    {
                        "action": "wishlist_request",
                        "kind": "track",
                        "track_id": record.get("track_id"),
                        "artist": record.get("artist"),
                        "album": record.get("album"),
                        "track": record.get("title"),
                        "track_number": record.get("track_number"),
                    }
                ),
            )
        )
    session.flush()
    return len(rows)


def run_check_duplicates(session: Session, _payload: dict, task: Task | None = None) -> dict:
    """Find tracks with the same artist + album + title appearing in multiple files and queue a
    review batch to move the duplicate copies to trash (keeping the best copy of each song)."""
    tracks = list(
        session.scalars(
            select(Track).options(selectinload(Track.album).selectinload(Album.artist))
        )
    )
    groups: dict[tuple[str, str, str], list[Track]] = {}
    for track in tracks:
        if not track.path or not track.album or not track.album.artist:
            continue
        key = (
            normalize_match_text(track.album.artist.name),
            normalize_match_text(track.album.title),
            normalize_match_text(track.title),
        )
        if not all(key):
            continue
        groups.setdefault(key, []).append(track)
    duplicate_groups = {key: items for key, items in groups.items() if len(items) > 1}
    if not duplicate_groups:
        create_notification(
            session,
            title="Duplicate check complete",
            body="No duplicate tracks were found.",
            event_type="tool_completed",
            target_url="/tools",
        )
        return {"songs_with_duplicates": 0, "files_queued": 0}

    batch = ProposalBatch(title="Remove duplicate library files", kind=ProposalKind.delete, tree_path="/task-queue")
    session.add(batch)
    session.flush()
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}

    def file_size(track: Track) -> int:
        if not track.path:
            return 0
        try:
            return Path(track.path).stat().st_size
        except OSError:
            return 0

    def ensure_tree(artist_name: str, album_title: str) -> str:
        if artist_name not in artist_items:
            ai = ProposalItem(
                batch_id=batch.id,
                title=artist_name,
                kind=ProposalKind.delete,
                payload_json=json.dumps({"artist": artist_name}),
            )
            session.add(ai)
            session.flush()
            artist_items[artist_name] = ai
        album_key = (artist_name, album_title)
        if album_key not in album_items:
            ali = ProposalItem(
                batch_id=batch.id,
                parent_id=artist_items[artist_name].id,
                title=album_title,
                kind=ProposalKind.delete,
                payload_json=json.dumps({"artist": artist_name, "album": album_title}),
            )
            session.add(ali)
            session.flush()
            album_items[album_key] = ali
        return album_items[album_key].id

    queued = 0
    for group in duplicate_groups.values():
        # Keep the best copy: lossless > larger file > lower id (stable). Trash the rest.
        sorted_group = sorted(group, key=lambda t: (0 if t.is_lossless else 1, -file_size(t), t.id or ""))
        keeper = sorted_group[0]
        for dup in sorted_group[1:]:
            album = dup.album
            artist_name = album.artist.name if album and album.artist else "Unknown Artist"
            album_title = album.title if album else "Unknown Album"
            album_item_id = ensure_tree(artist_name, album_title)
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    parent_id=album_item_id,
                    title=dup.title,
                    kind=ProposalKind.delete,
                    old_value=dup.path,
                    payload_json=json.dumps(
                        {
                            "action": "trash_duplicate",
                            "track_id": dup.id,
                            "keeping_track_id": keeper.id,
                            "keeping_path": keeper.path,
                        }
                    ),
                )
            )
            queued += 1
    session.flush()
    create_notification(
        session,
        title="Duplicate review ready",
        body=f"{queued} duplicate file(s) across {len(duplicate_groups)} song(s). Review and approve to move them to trash.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"songs_with_duplicates": len(duplicate_groups), "files_queued": queued}


def queue_missing_record_imports(session: Session, missing_records: list[dict]) -> int:
    if not missing_records:
        return 0
    known_paths = existing_library_and_proposal_paths(session)
    rows = [record for record in missing_records if record.get("path") and record["path"] not in known_paths]
    if not rows:
        return 0
    batch = ProposalBatch(title="Create records for library files", kind=ProposalKind.import_files, tree_path="/library")
    session.add(batch)
    session.flush()
    for record in rows:
        path = record["path"]
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=(record.get("metadata") or {}).get("title") or record.get("name") or Path(path).stem,
                kind=ProposalKind.import_files,
                old_value=path,
                new_value=path,
                payload_json=json.dumps(record),
            )
        )
    session.flush()
    return len(rows)


def existing_missing_file_download_keys(session: Session) -> set[tuple[str, str, str]]:
    keys = set()
    items = session.scalars(
        select(ProposalItem)
        .join(ProposalBatch, ProposalBatch.id == ProposalItem.batch_id)
        .where(ProposalItem.kind == ProposalKind.download)
        .where(ProposalItem.payload_json.is_not(None))
        .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
    )
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        if payload.get("action") != "wishlist_request":
            continue
        keys.add(missing_file_download_key(payload.get("artist"), payload.get("album"), payload.get("track") or item.title))
    return keys


def missing_file_download_key(artist: str | None, album: str | None, title: str | None) -> tuple[str, str, str]:
    return (normalize_match_text(artist), normalize_match_text(album), normalize_match_text(title))


def file_info_for_existing_library_file(file_path: Path) -> dict:
    settings = get_settings()
    stat = file_path.stat()
    metadata = read_audio_metadata(file_path)
    try:
        relative_path = str(file_path.relative_to(settings.library_path))
    except ValueError:
        relative_path = file_path.name
    return {
        "path": str(file_path),
        "relative_path": relative_path,
        "extension": file_path.suffix.lower(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "metadata": metadata,
        "suggested_library_path": str(file_path),
        "action": "create_library_record",
        "name": file_path.name,
    }


def run_check_lyrics(session: Session, _payload: dict) -> dict:
    tracks = list(
        session.scalars(
            select(Track)
            .where(Track.path.is_not(None))
            .options(selectinload(Track.album).selectinload(Album.artist))
            .order_by(Track.title.asc())
        )
    )
    missing = []
    existing = 0
    for track in tracks:
        if not track.path:
            continue
        audio_path = Path(track.path)
        if not audio_path.exists():
            continue
        if audio_path.with_suffix(".lrc").exists():
            existing += 1
            continue
        missing.append(track)

    if not missing:
        create_notification(
            session,
            title="Lyrics check complete",
            body=f"{existing} tracks already have lyrics. No missing lyrics found.",
            event_type="tool_completed",
            target_url="/tools",
        )
        return {"checked": len(tracks), "existing": existing, "missing": 0, "batch_id": None}

    batch = ProposalBatch(title="Download missing lyrics", kind=ProposalKind.lyrics, tree_path="/library")
    session.add(batch)
    session.flush()
    for track in missing:
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=track.title,
                kind=ProposalKind.lyrics,
                old_value=str(Path(track.path).with_suffix(".lrc")),
                new_value="LRCLIB",
                payload_json=json.dumps(
                    {
                        "action": "download_lyrics",
                        "track_id": track.id,
                        "artist": track.album.artist.name,
                        "album": track.album.title,
                        "track": track.title,
                    }
                ),
            )
        )
    create_notification(
        session,
        title="Lyrics review ready",
        body=f"{len(missing)} lyric downloads were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"checked": len(tracks), "existing": existing, "missing": len(missing), "batch_id": batch.id}


def run_check_album_covers(session: Session, _payload: dict) -> dict:
    albums = list(
        session.scalars(
            select(Album)
            .options(selectinload(Album.artist))
            .where((Album.cover_path.is_(None)) | (Album.cover_path == ""))
            .order_by(Album.title.asc())
        )
    )
    batch = ProposalBatch(title="Download missing album covers", kind=ProposalKind.artwork, tree_path="/library")
    session.add(batch)
    session.flush()
    found = 0
    for album in albums:
        try:
            results = search_album_releases(album.artist.name, album.title)
        except Exception as error:  # noqa: BLE001 - keep checking other albums.
            append_task_log(session, task=None, message=f"{album.artist.name} / {album.title}: album cover lookup failed: {error}", level="warning")
            continue
        cover_path = download_album_cover_to_library(session, album, album_cover_candidate_urls(album.artist.name, album.title, results))
        if not cover_path:
            continue
        found += 1
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=f"{album.artist.name} / {album.title}",
                kind=ProposalKind.metadata,
                old_value=json.dumps({"cover_path": album.cover_path}),
                new_value=json.dumps({"cover_path": cover_path}),
                payload_json=json.dumps(
                    {
                        "target_type": "album",
                        "target_id": album.id,
                        "changes": {"cover_path": cover_path},
                    }
                ),
            )
        )
    if found == 0:
        session.delete(batch)
        create_notification(
            session,
            title="Album cover check complete",
            body=f"{len(albums)} albums checked. No missing covers were found online.",
            event_type="tool_completed",
            target_url="/tools",
        )
        return {"albums_checked": len(albums), "cover_changes": 0, "batch_id": None}
    create_notification(
        session,
        title="Album cover review ready",
        body=f"{found} album cover changes were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"albums_checked": len(albums), "cover_changes": found, "batch_id": batch.id}


def run_check_missing_tracks(session: Session, _payload: dict, task: Task | None = None) -> dict:
    albums = list(
        session.scalars(
            select(Album).options(selectinload(Album.artist), selectinload(Album.tracks)).order_by(Album.title.asc())
        )
    )
    created = 0
    checked = 0
    batch = ProposalBatch(title="Missing album tracks", kind=ProposalKind.download, tree_path="/task-queue")
    session.add(batch)
    session.flush()
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    for album in albums:
        checked += 1
        if task is not None and checked % 5 == 0:
            update_task_progress(session, task, checked, max(1, len(albums)), f"Checking missing tracks for {album.artist.name} / {album.title}")
        lookup_title = album.release_title or album.title
        try:
            append_task_log(session, task, f"{album.artist.name} / {album.title}: checking MusicBrainz album track list")
            record = lookup_album_tracks(album.artist.name, lookup_title, album.musicbrainz_release_id)
        except Exception as error:  # noqa: BLE001 - one bad lookup should not stop the full scan.
            append_task_log(session, task, f"{album.artist.name} / {album.title}: missing-track lookup failed: {error}", "warning")
            continue
        if record.get("musicbrainz_album_id") and not album.musicbrainz_release_id:
            album.musicbrainz_release_id = record.get("musicbrainz_album_id")
        existing_positions = {
            (track.disc_number or 1, track.track_number)
            for track in album.tracks
            if track.track_number
        }
        existing_titles = {normalize_match_text(track.title) for track in album.tracks if track.title}
        for track in record.get("tracks", []):
            track_number = track.get("track_number")
            disc_number = track.get("disc_number") or 1
            title_key = normalize_match_text(track.get("title"))
            # Skip tracks already on the album — by position OR by title — so we never re-download
            # something the album already has (the cause of the duplicate imports).
            if not track_number:
                continue
            if (disc_number, track_number) in existing_positions or (title_key and title_key in existing_titles):
                continue
            add_download_request_item(
                session,
                batch,
                artist_items,
                album_items,
                album.artist.name,
                album.title,
                track.get("title"),
                track_number=track.get("track_number"),
                disc_number=disc_number,
                duration_ms=track.get("length"),
                musicbrainz_album_id=record.get("musicbrainz_album_id"),
                musicbrainz_recording_id=track.get("musicbrainz_recording_id"),
                require_lossless=True,
                workflow="missing_tracks",
            )
            created += 1
            append_task_log(session, task, f"{album.artist.name} / {album.title}: queued missing track review item for {track.get('title')} with lossless candidate matching")
    if created == 0:
        session.delete(batch)
        create_notification(
            session,
            title="Missing track check complete",
            body=f"{checked} albums checked. No missing tracks were found.",
            event_type="tool_completed",
            target_url="/tools",
        )
        return {"albums_checked": checked, "download_items_created": 0, "batch_id": None}
    create_notification(
        session,
        title="Missing track review ready",
        body=f"{created} missing tracks were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"albums_checked": checked, "download_items_created": created, "batch_id": batch.id}


def run_check_non_lossless(session: Session, _payload: dict, task: Task | None = None) -> dict:
    tracks = list(session.scalars(select(Track).options(selectinload(Track.album).selectinload(Album.artist)).order_by(Track.title.asc())))
    batch = ProposalBatch(title="Lossless replacement downloads", kind=ProposalKind.download, tree_path="/task-queue")
    session.add(batch)
    session.flush()
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    created = 0
    checked = 0
    for track in tracks:
        checked += 1
        if task is not None and checked % 25 == 0:
            update_task_progress(session, task, checked, max(1, len(tracks)), f"Checking lossless status for {track.title}")
        try:
            metadata = read_audio_metadata(Path(track.path)) if track.path and Path(track.path).exists() else {
                "format": track.format,
                "bitrate": track.bitrate,
                "is_lossless": track.is_lossless,
            }
        except Exception:
            metadata = {"format": track.format, "bitrate": track.bitrate, "is_lossless": False}
        if not lossy_or_suspicious_audio(metadata):
            continue
        album = track.album
        artist_name = album.artist.name if album and album.artist else "Unknown Artist"
        album_title = album.title if album else "Unknown Album"
        add_download_request_item(
            session,
            batch,
            artist_items,
            album_items,
            artist_name,
            album_title,
            track.title,
            track_number=track.track_number,
            disc_number=track.disc_number,
            duration_ms=track.duration_ms or metadata.get("duration_ms"),
            musicbrainz_album_id=album.musicbrainz_release_id if album else None,
            musicbrainz_recording_id=track.musicbrainz_recording_id,
            replace_track_id=track.id,
            replace_path=track.path,
            require_lossless=True,
            workflow="lossless_replacement",
        )
        created += 1
        append_task_log(session, task, f"{artist_name} / {album_title}: queued lossless replacement review item for {track.title}")
    if created == 0:
        session.delete(batch)
        create_notification(
            session,
            title="Lossless check complete",
            body=f"{checked} tracks checked. No lossy or suspicious files were found.",
            event_type="tool_completed",
            target_url="/tools",
        )
        return {"tracks_checked": checked, "download_items_created": 0, "batch_id": None}
    create_notification(
        session,
        title="Lossless replacement review ready",
        body=f"{created} replacement downloads were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"tracks_checked": checked, "download_items_created": created, "batch_id": batch.id}


def run_normalize_volume(session: Session, _payload: dict) -> dict:
    tracks = list(session.scalars(select(Track).where(Track.path.is_not(None)).order_by(Track.title.asc())))
    batch = ProposalBatch(title="Normalize library volume", kind=ProposalKind.file_move, tree_path="/library")
    session.add(batch)
    session.flush()
    created = 0
    for track in tracks:
        if not track.path or not Path(track.path).exists():
            continue
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=track.title,
                kind=ProposalKind.file_move,
                old_value=track.path,
                new_value=track.path,
                payload_json=json.dumps({"action": "normalize_volume", "track_id": track.id, "path": track.path}),
            )
        )
        created += 1
    if created == 0:
        session.delete(batch)
        create_notification(session, title="Volume check complete", body="No library files were found to normalize.", event_type="tool_completed", target_url="/tools")
        return {"tracks_checked": len(tracks), "items_created": 0, "batch_id": None}
    create_notification(
        session,
        title="Volume normalization review ready",
        body=f"{created} files were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"tracks_checked": len(tracks), "items_created": created, "batch_id": batch.id}


def add_download_request_item(
    session: Session,
    batch: ProposalBatch,
    artist_items: dict[str, ProposalItem],
    album_items: dict[tuple[str, str], ProposalItem],
    artist: str,
    album: str,
    track: str | None,
    track_number: int | None = None,
    disc_number: int | None = None,
    duration_ms: int | None = None,
    musicbrainz_album_id: str | None = None,
    musicbrainz_recording_id: str | None = None,
    replace_track_id: str | None = None,
    replace_path: str | None = None,
    require_lossless: bool | None = None,
    workflow: str | None = None,
) -> None:
    if artist not in artist_items:
        artist_item = ProposalItem(batch_id=batch.id, title=artist, kind=ProposalKind.download, payload_json=json.dumps({"artist": artist}))
        session.add(artist_item)
        session.flush()
        artist_items[artist] = artist_item
    album_key = (artist, album)
    if album_key not in album_items:
        album_item = ProposalItem(
            batch_id=batch.id,
            parent_id=artist_items[artist].id,
            title=album,
            kind=ProposalKind.download,
            payload_json=json.dumps({"artist": artist, "album": album}),
        )
        session.add(album_item)
        session.flush()
        album_items[album_key] = album_item
    session.add(
        ProposalItem(
            batch_id=batch.id,
            parent_id=album_items[album_key].id,
            title=track or album,
            kind=ProposalKind.download,
            payload_json=json.dumps(
                {
                    "action": "wishlist_request",
                    "kind": "track",
                    "artist": artist,
                    "album": album,
                    "track": track,
                    "track_number": track_number,
                    "disc_number": disc_number,
                    "duration_ms": duration_ms,
                    "musicbrainz_album_id": musicbrainz_album_id,
                    "musicbrainz_recording_id": musicbrainz_recording_id,
                    "replace_track_id": replace_track_id,
                    "replace_path": replace_path,
                    "require_lossless": require_lossless,
                    "workflow": workflow,
                }
            ),
        )
    )


def run_backup_now(session: Session, _payload: dict) -> dict:
    settings = get_settings()
    settings.backups_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = settings.backups_path / f"nudibranch-{timestamp}.sqlite"
    session.commit()
    shutil.copy2(settings.db_path, backup_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{settings.db_path}{suffix}")
        if sidecar.exists():
            shutil.copy2(sidecar, settings.backups_path / f"{backup_path.name}{suffix}")
    create_notification(session, title="Backup complete", body=str(backup_path), event_type="tool_completed", target_url="/tools")
    return {"backup_path": str(backup_path)}


def run_restore_default(session: Session, _payload: dict) -> dict:
    backup = run_backup_now(session, {})
    for model in (ProposalItem, ProposalBatch, PlaylistTrack, Playlist, WishlistItem, Track, Album, Artist):
        for item in list(session.scalars(select(model))):
            session.delete(item)
    session.commit()
    create_notification(session, title="Restore complete", body="Library data was restored to default.", event_type="tool_completed", target_url="/tools")
    return {"reset": True, "pre_restore_backup": backup.get("backup_path")}


def run_restore_backup(session: Session, payload: dict) -> dict:
    settings = get_settings()
    backup_path = Path(payload.get("backup_path", "")).resolve()
    backup_root = settings.backups_path.resolve()
    if backup_root not in [backup_path, *backup_path.parents] or not backup_path.exists():
        raise ValueError("Backup must be inside the backups folder")
    pre_restore = run_backup_now(session, {})
    session.commit()
    shutil.copy2(backup_path, settings.db_path)
    for suffix in ("-wal", "-shm"):
        source = Path(f"{backup_path}{suffix}")
        target = Path(f"{settings.db_path}{suffix}")
        if source.exists():
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()
    create_notification(session, title="Restore complete", body=backup_path.name, event_type="tool_completed", target_url="/tools")
    return {"restored_from": str(backup_path), "pre_restore_backup": pre_restore.get("backup_path")}


def run_clear_downloads(session: Session, _payload: dict, task: Task | None = None) -> dict:
    root = get_settings().downloads_path
    append_task_log(session, task, f"Clear downloads started for {root}")
    if task is not None:
        update_task_progress(session, task, 0, 1, "Scanning downloads folder")
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = download_manifest_path().resolve()
    append_task_log(session, task, f"Keeping download manifest at {manifest_path}")
    removed_files = 0
    removed_dirs = 0
    scanned = 0
    skipped = 0
    errors: list[str] = []
    try:
        paths = sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True)
    except OSError as error:
        append_task_log(session, task, f"Clear downloads scan failed: {error}", "error")
        return {"removed_files": 0, "removed_dirs": 0, "skipped": 0, "errors": [str(error)]}
    total = max(1, len(paths))
    append_task_log(session, task, f"Clear downloads found {len(paths)} path(s) to review")
    for index, path in enumerate(paths, start=1):
        scanned += 1
        try:
            if path.resolve() == manifest_path:
                skipped += 1
                append_task_log(session, task, f"Skipped download manifest {path}")
                continue
            if path.is_file():
                path.unlink()
                removed_files += 1
                append_task_log(session, task, f"Removed downloaded file {path}")
            elif path.is_dir():
                try:
                    path.rmdir()
                    removed_dirs += 1
                    append_task_log(session, task, f"Removed empty downloads directory {path}")
                except OSError:
                    skipped += 1
                    append_task_log(session, task, f"Skipped non-empty downloads directory {path}")
                    continue
        except OSError as error:
            message = f"{path}: {error}"
            errors.append(message)
            append_task_log(session, task, f"Clear downloads could not remove {message}", "error")
        if task is not None and (index == total or index % 25 == 0):
            task.lease_until = Task.lease_expiry()
            update_task_progress(
                session,
                task,
                index,
                total,
                f"Cleared {removed_files} file(s), {removed_dirs} folder(s), skipped {skipped}",
                removed_files=removed_files,
                removed_dirs=removed_dirs,
                skipped=skipped,
                errors=len(errors),
            )
    append_task_log(
        session,
        task,
        f"Clear downloads finished: scanned {scanned}, removed {removed_files} file(s), removed {removed_dirs} folder(s), skipped {skipped}, errors {len(errors)}",
        "warning" if errors else "info",
    )
    create_notification(
        session,
        title="Downloads folder cleared",
        body=f"{removed_files} files and {removed_dirs} folders removed. {skipped} skipped.",
        event_type="tool_completed" if not errors else "tool_warning",
        target_url="/tools",
    )
    return {"removed_files": removed_files, "removed_dirs": removed_dirs, "skipped": skipped, "scanned": scanned, "errors": errors}


def first_admin_id(session: Session) -> str:
    admin_id = session.scalar(select(User.id).where(User.is_admin.is_(True)).order_by(User.created_at.asc()))
    if not admin_id:
        raise ValueError("No admin user exists for generated wishlist items")
    return admin_id


def find_jellyfin_playlist(client: httpx.Client, user_id: str, name: str) -> str | None:
    response = client.get(f"/Users/{user_id}/Items", params={"Recursive": "true", "IncludeItemTypes": "Playlist", "SearchTerm": name})
    response.raise_for_status()
    for item in response.json().get("Items", []):
        if item.get("Name") == name:
            return item.get("Id")
    return None


def ensure_favorites_playlist(session: Session) -> Playlist:
    configured_setting = session.get(AppSetting, "favorite_playlist_id")
    configured_id = configured_setting.value if configured_setting else ""
    explicit_setting = session.get(AppSetting, "favorite_playlist_explicit")
    explicit_favorite = explicit_setting and explicit_setting.value == "1"
    playlist = session.get(Playlist, configured_id) if explicit_favorite and configured_id else None
    if not playlist:
        playlist = session.scalar(select(Playlist).where(Playlist.name == "Favorites"))
    if not playlist:
        playlist = Playlist(name="Favorites", protected=True)
        session.add(playlist)
        session.flush()
    for protected_playlist in session.scalars(select(Playlist).where(Playlist.protected.is_(True), Playlist.id != playlist.id)):
        protected_playlist.protected = False
    playlist.protected = True
    session.flush()
    return playlist


class JellyfinPlaylistMissing(RuntimeError):
    pass


def list_jellyfin_playlists(client: httpx.Client, user_id: str) -> dict[str, dict]:
    response = client.get(f"/Users/{user_id}/Items", params={"Recursive": "true", "IncludeItemTypes": "Playlist"})
    response.raise_for_status()
    playlists = {}
    for item in response.json().get("Items", []):
        name = item.get("Name")
        if name:
            playlists[name] = item
    return playlists


def jellyfin_playlist_item_ids(client: httpx.Client, user_id: str, playlist_id: str) -> set[str]:
    return {item.get("Id") for item in jellyfin_playlist_items(client, user_id, playlist_id) if item.get("Id")}


def jellyfin_playlist_items(client: httpx.Client, user_id: str, playlist_id: str) -> list[dict]:
    try:
        response = client.get(
            f"/Playlists/{playlist_id}/Items",
            params={"userId": user_id, "fields": "Path,ProviderIds,RunTimeTicks"},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise JellyfinPlaylistMissing(f"Jellyfin playlist {playlist_id} was not found") from error
        if error.response.status_code == 405:
            return []
        raise
    return response.json().get("Items", [])


def create_jellyfin_playlist(client: httpx.Client, user_id: str, name: str) -> httpx.Response:
    response = client.post(
        "/Playlists",
        json={"Name": name, "UserId": user_id, "MediaType": "Audio", "Ids": []},
    )
    if response.is_success:
        return response
    fallback = client.post("/Playlists", params={"name": name, "userId": user_id, "mediaType": "Audio"})
    fallback.raise_for_status()
    return fallback


def jellyfin_playlist_id_from_response(response: httpx.Response) -> str:
    payload = response.json()
    playlist_id = payload.get("Id") or payload.get("id")
    if not playlist_id:
        raise RuntimeError("Jellyfin created a playlist but did not return a playlist id")
    return str(playlist_id)


def add_jellyfin_playlist_items(client: httpx.Client, user_id: str, playlist_id: str, item_ids: list[str]) -> None:
    if not item_ids:
        return
    response = client.post(f"/Playlists/{playlist_id}/Items", params={"ids": ",".join(item_ids), "userId": user_id})
    if response.is_success:
        return
    if response.status_code == 404:
        raise JellyfinPlaylistMissing(f"Jellyfin playlist {playlist_id} was not found")
    if response.status_code < 500:
        response.raise_for_status()
    failures = []
    for item_id in item_ids:
        single = client.post(f"/Playlists/{playlist_id}/Items", params={"ids": item_id, "userId": user_id})
        if single.status_code == 404:
            raise JellyfinPlaylistMissing(f"Jellyfin playlist {playlist_id} was not found")
        if not single.is_success:
            failures.append(f"{item_id}: {single.status_code} {single.text[-300:]}")
    if failures:
        raise RuntimeError(f"Jellyfin playlist sync failed for {len(failures)} item(s): {'; '.join(failures[:3])}")


def jellyfin_playlist_entry_id(item: dict) -> str:
    return str(item.get("PlaylistItemId") or item.get("PlaylistItemID") or "")


def remove_jellyfin_playlist_entry_ids(client: httpx.Client, playlist_id: str, entry_ids: list[str]) -> int:
    if not entry_ids:
        return 0
    response = client.delete(f"/Playlists/{playlist_id}/Items", params={"entryIds": ",".join(entry_ids)})
    if response.status_code == 404:
        raise JellyfinPlaylistMissing(f"Jellyfin playlist {playlist_id} was not found")
    if response.status_code in {405, 501}:
        return 0
    response.raise_for_status()
    return len(entry_ids)


def remove_jellyfin_playlist_items(client: httpx.Client, _user_id: str, playlist_id: str, items: list[dict], keep_item_ids: set[str]) -> int:
    entry_ids = [
        jellyfin_playlist_entry_id(item)
        for item in items
        if item.get("Id") not in keep_item_ids and jellyfin_playlist_entry_id(item)
    ]
    return remove_jellyfin_playlist_entry_ids(client, playlist_id, entry_ids)


def replace_jellyfin_playlist_items(client: httpx.Client, user_id: str, playlist_id: str, current_items: list[dict], desired_item_ids: list[str]) -> int:
    entry_ids = [entry_id for entry_id in (jellyfin_playlist_entry_id(item) for item in current_items) if entry_id]
    removed = remove_jellyfin_playlist_entry_ids(client, playlist_id, entry_ids)
    add_jellyfin_playlist_items(client, user_id, playlist_id, desired_item_ids)
    return removed + len(desired_item_ids)


def sync_playlist_from_jellyfin(session: Session, playlist: Playlist, jellyfin_items: list[dict]) -> int:
    next_entries: list[tuple[Track, int]] = []
    for index, item in enumerate(jellyfin_items, start=1):
        track = find_local_track_for_jellyfin_item(session, item)
        if track:
            next_entries.append((track, index))
        else:
            append_task_log(session, None, f"{playlist.name}: Jellyfin item {item.get('Name') or item.get('Id')} is not in Nudibranch yet", "warning")
    current_entries = list(playlist.tracks)
    current_by_track_id = {entry.track_id: entry for entry in current_entries}
    next_track_ids = {track.id for track, _ in next_entries}
    changes = 0
    for entry in current_entries:
        if entry.track_id not in next_track_ids:
            session.delete(entry)
            changes += 1
    for track, position in next_entries:
        entry = current_by_track_id.get(track.id)
        if not entry:
            session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=position))
            changes += 1
        elif entry.position != position:
            entry.position = position
            changes += 1
    return changes


def find_local_track_for_jellyfin_item(session: Session, item: dict) -> Track | None:
    path = item.get("Path")
    if path:
        track = session.scalar(select(Track).where(Track.path == path))
        if track:
            return track
    provider_ids = jellyfin_provider_ids(item)
    recording_id = provider_ids.get("musicbrainztrack") or provider_ids.get("musicbrainzrecording")
    if recording_id:
        track = session.scalar(select(Track).where(func.lower(Track.musicbrainz_recording_id) == recording_id))
        if track:
            return track
    name = item.get("Name")
    if not name:
        return None
    candidates = list(
        session.scalars(
            select(Track)
            .where(func.lower(Track.title) == name.casefold())
            .options(selectinload(Track.album).selectinload(Album.artist))
        )
    )
    artist_names = jellyfin_artist_names(item)
    album_name = normalize_match_text(item.get("Album"))
    for track in candidates:
        album = track.album
        artist_match = not artist_names or normalize_match_text(album.artist.name if album and album.artist else None) in artist_names
        album_match = not album_name or normalize_match_text(album.title if album else None) == album_name
        duration_match = jellyfin_duration_matches(track, item)
        if artist_match and album_match and duration_match:
            return track
    return None


def find_jellyfin_audio_item(client: httpx.Client, user_id: str, track: Track) -> str | None:
    response = client.get(
        f"/Users/{user_id}/Items",
        params={
            "Recursive": "true",
            "IncludeItemTypes": "Audio",
            "SearchTerm": track.title,
            "Fields": "Path,ProviderIds,RunTimeTicks",
        },
    )
    response.raise_for_status()
    best_item: dict | None = None
    best_score = 0.0
    for item in response.json().get("Items", []):
        score = jellyfin_audio_match_score(track, item)
        if score > best_score:
            best_score = score
            best_item = item
    if best_item and best_score >= 0.65:
        return best_item.get("Id")
    return None


def jellyfin_provider_ids(item: dict) -> dict[str, str]:
    provider_ids = item.get("ProviderIds") or item.get("ProviderIDs") or {}
    if not isinstance(provider_ids, dict):
        return {}
    return {normalize_match_text(key): normalize_match_text(value) for key, value in provider_ids.items() if value}


def jellyfin_artist_names(item: dict) -> set[str]:
    artists = item.get("Artists") or []
    if isinstance(artists, str):
        artists = [artists]
    return {
        normalize_match_text(value)
        for value in [item.get("AlbumArtist"), item.get("Artist"), *artists]
        if value
    }


def jellyfin_duration_ms(item: dict) -> int | None:
    ticks = item.get("RunTimeTicks")
    try:
        return round(int(ticks) / 10000) if ticks is not None else None
    except (TypeError, ValueError):
        return None


def jellyfin_duration_matches(track: Track, item: dict) -> bool:
    item_duration = jellyfin_duration_ms(item)
    if not track.duration_ms or not item_duration:
        return True
    return abs(track.duration_ms - item_duration) <= 10000


def jellyfin_audio_match_score(track: Track, item: dict) -> float:
    if track.path and str(item.get("Path") or "").lower() == str(track.path).lower():
        return 1.0
    provider_ids = jellyfin_provider_ids(item)
    recording_id = normalize_match_text(track.musicbrainz_recording_id)
    if recording_id and recording_id in {
        provider_ids.get("musicbrainztrack"),
        provider_ids.get("musicbrainzrecording"),
    }:
        return 0.98

    title = normalize_match_text(track.title)
    item_title = normalize_match_text(item.get("Name"))
    if not title or title != item_title:
        return 0.0

    score = 0.45
    album = track.album
    artist_name = normalize_match_text(album.artist.name if album and album.artist else None)
    item_artists = jellyfin_artist_names(item)
    if artist_name and artist_name in item_artists:
        score += 0.25
    elif item_artists:
        return 0.0

    album_title = normalize_match_text(album.title if album else None)
    item_album = normalize_match_text(item.get("Album"))
    if album_title and item_album and album_title == item_album:
        score += 0.2
    elif item_album:
        score -= 0.2

    if jellyfin_duration_matches(track, item):
        score += 0.1
    else:
        score -= 0.2
    return score


TASK_HANDLERS = {
    "propose_import": run_propose_import,
    "execute_proposal_batch": run_execute_proposal_batch,
    "process_wishlist": run_process_wishlist,
    "sync_favorites_jellyfin": run_sync_favorites_jellyfin,
    "jellyfin_scan": run_jellyfin_scan,
    "clear_discover_cache": run_clear_discover_cache,
    "check_files": run_check_files,
    "check_duplicates": run_check_duplicates,
    "check_lyrics": run_check_lyrics,
    "check_album_covers": run_check_album_covers,
    "check_missing_tracks": run_check_missing_tracks,
    "check_non_lossless": run_check_non_lossless,
    "normalize_volume": run_normalize_volume,
    "backup_now": run_backup_now,
    "restore_default": run_restore_default,
    "restore_backup": run_restore_backup,
    "clear_downloads": run_clear_downloads,
}


def check_mount_writability(session: Session) -> None:
    """Probe the folders the download pipeline writes to and warn loudly if any is read-only.

    Surfaces mount/permission problems (e.g. an NFS share that squashes the container's user) at
    startup, instead of only after a full album has downloaded and fails to import.
    """
    settings = get_settings()
    mounts = [
        ("library", settings.library_path, True),
        ("downloads", settings.downloads_path, True),
        ("staging", settings.staging_path, False),
    ]
    for name, path, critical in mounts:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".nudibranch-writetest"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as error:  # noqa: BLE001 - report the problem, never crash the worker.
            append_task_log(
                session,
                None,
                f"Startup check: the {name} folder ({path}) is NOT writable: {error}. "
                "Downloads can stage but imports/writes there will fail until the mount's "
                "permissions are fixed (file ownership / the container's user, or the NFS share).",
                "error" if critical else "warning",
            )
            if name == "library":
                create_notification(
                    session,
                    title="Library folder is not writable",
                    body=(
                        f"The worker cannot write to the library folder ({path}). Downloaded "
                        "tracks will stage but cannot be imported. Fix the mount's write permissions."
                    ),
                    event_type="task_failed",
                    target_url="/downloads",
                )
        else:
            append_task_log(session, None, f"Startup check: the {name} folder ({path}) is writable")
    session.commit()


async def worker_loop() -> None:
    with SessionLocal() as session:
        init_db(session)
        check_mount_writability(session)

    last_download_scan = 0.0
    last_download_scan_summary = ""
    last_download_scan_log = 0.0
    while True:
        with SessionLocal() as session:
            task = claim_next_task(session)
            if not task:
                if time.time() - last_download_scan > DOWNLOAD_SCAN_INTERVAL_SECONDS:
                    try:
                        scan_result = import_completed_downloads(session)
                        if scan_result.get("waiting") or scan_result.get("ready") or scan_result.get("failed"):
                            summary = f"{scan_result.get('waiting', 0)}:{scan_result.get('ready', 0)}:{scan_result.get('failed', 0)}"
                            now = time.time()
                            if summary == last_download_scan_summary and now - last_download_scan_log < 120:
                                session.commit()
                                last_download_scan = now
                                await deliver_apns_notifications(session)
                                time.sleep(2)
                                continue
                            append_task_log(
                                session,
                                None,
                                f"Download scan checked {scan_result.get('waiting', 0)} active or queued download(s), {scan_result.get('ready', 0)} staged or verified, {scan_result.get('failed', 0)} needing attention",
                            )
                            last_download_scan_summary = summary
                            last_download_scan_log = now
                        session.commit()
                    except Exception as error:  # noqa: BLE001 - idle scans should never stop the worker.
                        session.rollback()
                        create_notification(session, title="Download scan failed", body=str(error), event_type="task_failed", target_url="/activity")
                    last_download_scan = time.time()
                await deliver_apns_notifications(session)
                time.sleep(2)
                continue

            try:
                handler = TASK_HANDLERS.get(task.type)
                if not handler:
                    raise ValueError(f"No handler registered for task type {task.type}")
                create_notification(
                    session,
                    title=f"{task_notification_title(task.type)} started",
                    body="Task is running.",
                    event_type="task_started",
                    target_url=task_target_url(task.type),
                    deliver_apns=False,
                )
                append_task_log(session, task, f"{task.type} started: {task.payload_json or '{}'}")
                if task.type in {"propose_import", "execute_proposal_batch", "clear_downloads", "check_missing_tracks", "check_non_lossless"}:
                    result = handler(session, task_to_payload(task), task)
                else:
                    result = handler(session, task_to_payload(task))
                session.refresh(task)
                if task.status == TaskStatus.canceled:
                    append_task_log(session, task, f"{task.type} canceled", "warning")
                    continue
                if result.get("errors"):
                    append_task_log(session, task, f"{task.type} failed with {len(result.get('errors') or [])} error(s): {result['errors'][0]}", "error")
                    task.status = TaskStatus.failed
                    task.result_json = json.dumps(result)
                    task.error = result["errors"][0]
                    task.lease_until = None
                    session.commit()
                else:
                    append_task_log(session, task, f"{task.type} completed: {json.dumps(result, sort_keys=True)[:1200]}")
                    complete_task(session, task, result)
            except Exception as error:  # noqa: BLE001 - worker must persist task failures.
                append_task_log(session, task, f"{task.type} failed unexpectedly: {error}", "error")
                create_notification(
                    session,
                    title=f"{task.type} failed",
                    body=str(error),
                    event_type="task_failed",
                    target_url="/activity",
                )
                fail_task(session, task, str(error))


def task_notification_title(task_type: str) -> str:
    return {
        "propose_import": "Import review",
        "execute_proposal_batch": "Task queue item",
        "process_wishlist": "Wishlist scan",
        "sync_favorites_jellyfin": "Playlist sync",
        "jellyfin_scan": "Jellyfin scan",
        "check_files": "File check",
        "check_duplicates": "Duplicate check",
        "check_lyrics": "Lyrics check",
        "check_album_covers": "Album cover check",
        "check_missing_tracks": "Missing tracks check",
        "check_non_lossless": "Lossless check",
        "normalize_volume": "Volume normalization",
        "backup_now": "Backup",
        "restore_default": "Restore",
        "restore_backup": "Restore",
        "clear_downloads": "Clear downloads",
        "clear_discover_cache": "Clear discover cache",
    }.get(task_type, task_type.replace("_", " ").title())


def task_target_url(task_type: str) -> str:
    if task_type in {"execute_proposal_batch"}:
        return "/task-queue"
    if task_type in {"propose_import"}:
        return "/import"
    if task_type in {"sync_favorites_jellyfin"}:
        return "/playlists"
    if task_type in {"check_files", "check_duplicates", "check_lyrics", "check_album_covers", "check_missing_tracks", "check_non_lossless", "normalize_volume", "jellyfin_scan", "backup_now", "restore_default", "restore_backup", "clear_downloads", "clear_discover_cache"}:
        return "/tools"
    return "/activity"


if __name__ == "__main__":
    asyncio.run(worker_loop())
