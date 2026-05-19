import asyncio
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nudibranch.core.config import get_settings
from nudibranch.db.init import init_db
from nudibranch.db.models import Album, Artist, Playlist, PlaylistTrack, ProposalBatch, ProposalItem, ProposalKind, ProposalStatus, Task, TaskStatus, Track, User, WishlistItem
from nudibranch.db.session import SessionLocal
from nudibranch.services.imports import SUPPORTED_AUDIO_EXTENSIONS, discover_import_files, read_audio_metadata, suggest_library_path, write_audio_metadata
from nudibranch.services.notifications import create_notification, deliver_apns_notifications
from nudibranch.services.metadata_lookup import search_album_releases, lookup_album_tracks, lookup_recording_by_fingerprint
from nudibranch.services.proposals import item_ids_with_descendants
from nudibranch.services.settings_store import integration_settings
from nudibranch.services.slskd import queue_slskd_download, search_slskd_detailed
from nudibranch.services.tasks import append_task_log, claim_next_task, complete_task, enqueue_task, fail_task, merge_task_logs, task_to_payload, update_task_progress


def run_propose_import(session: Session, payload: dict) -> dict:
    files = payload.get("files")
    if files is None:
        files = discover_import_files(payload.get("path"), include_fingerprint=True)
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
    batch = ProposalBatch(title=batch_title, kind=ProposalKind.import_files, tree_path="/app/import")
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
    for request in download_requests:
        artist = request.get("artist") or "Unknown Artist"
        album = request.get("album") or "Unknown Album"
        title = request.get("track") or request.get("title") or "Unknown Track"
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
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=album_item.id,
                title=title,
                kind=ProposalKind.download,
                old_value="missing import slot",
                payload_json=json.dumps(
                    {
                        "action": "wishlist_request",
                        "kind": "track",
                        "artist": artist,
                        "album": album,
                        "track": title,
                        "track_number": request.get("track_number"),
                        "disc_number": request.get("disc_number"),
                        "musicbrainz_album_id": request.get("musicbrainz_album_id"),
                        "musicbrainz_recording_id": request.get("musicbrainz_recording_id"),
                    }
                ),
            )
        )
    create_notification(
        session,
        title="Import review ready",
        body=f"{len(files)} files and {len(download_requests)} downloads were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"batch_id": batch.id, "files": len(files), "downloads": len(download_requests)}


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

    for item in executable_items:
        try:
            note_progress(f"Importing {item.title}", item)
            import_track_item(session, item)
            item.status = ProposalStatus.completed
            imported += 1
            finish_progress_step(f"Imported {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")
            finish_progress_step(f"Import failed for {item.title}")

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

    for item in direct_download_items:
        try:
            note_progress(f"Queueing download for {item.title}", item)
            apply_download_item(session, item, task)
            item.status = ProposalStatus.executing if keeps_download_batch_open(item) else ProposalStatus.completed
            download_changes += 1
            finish_progress_step(f"Queued download for {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            download_errors.append(f"{item.title}: {error}")
            failed_request = download_request_from_item(item)
            if failed_request:
                replacements = add_replacement_candidates_to_download_batch(session, batch, {"parent_id": item.parent_id}, {**failed_request, "multiple_candidates": True})
                if replacements:
                    item.selected = False
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

    downloaded_import = import_completed_downloads(session)
    open_downloads = batch_has_open_downloads(batch)

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
        parts.append("downloads are queued and will move to the library after the files finish and verify")
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
    import_file_to_library(session, Path(item.old_value), Path(item.new_value), payload)


def import_file_to_library(session: Session, source_path: Path, target_path: Path, payload: dict) -> None:
    metadata = payload.get("metadata", {})
    if not source_path.exists():
        raise FileNotFoundError(f"{source_path} no longer exists")

    stat = source_path.stat()
    if payload.get("size_bytes") and stat.st_size != payload["size_bytes"]:
        raise ValueError("source file size changed after review")

    create_record_only = payload.get("action") == "create_library_record"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and not create_record_only:
        raise FileExistsError(f"{target_path} already exists")

    if not create_record_only:
        shutil.move(str(source_path), str(target_path))

    artist_name = metadata.get("albumartist") or metadata.get("artist") or "Unknown Artist"
    album_title = metadata.get("album") or "Unknown Album"
    track_title = metadata.get("title") or target_path.stem

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
            acoustic_fingerprint=json.dumps(payload.get("fingerprint")) if payload.get("fingerprint") else None,
            musicbrainz_recording_id=metadata.get("musicbrainz_recording_id"),
            is_lossless=metadata.get("is_lossless", False),
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
            set_download_item_status(item, "queued in slskd; waiting for downloaded file")
            if candidate_item.id != item.id:
                candidate_item.status = ProposalStatus.executing
                set_download_item_status(candidate_item, "queued in slskd; waiting for downloaded file")
                item.title = candidate_item.title
                item.new_value = candidate_item.new_value
                item.payload_json = candidate_item.payload_json
            append_task_log(session, task, f"{item.title}: slskd accepted candidate {label}")
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
    return get_settings().downloads_path / ".nudibranch-downloads.json"


def load_download_manifest() -> list[dict]:
    path = download_manifest_path()
    if not path.exists():
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
            and entry.get("status") in {"queued", "verified"}
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
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "status": "queued",
        }
    )
    save_download_manifest(entries[-500:])


def remote_basename(filename: str) -> str:
    return str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()


def find_download_manifest_entry(file_path: Path) -> dict | None:
    basename = file_path.name.casefold()
    candidates = [entry for entry in load_download_manifest() if entry.get("status") in {"queued", "verified", "rejected"}]
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


def same_manifest_entry(entry: dict, target: dict) -> bool:
    target_item_id = target.get("_original_item_id") or target.get("item_id")
    return bool(
        entry.get("batch_id") == target.get("batch_id")
        and entry.get("item_id") == target_item_id
        and entry.get("basename")
        and entry.get("basename") == target.get("basename")
    )


def import_completed_downloads(session: Session, minimum_age_seconds: int = 10) -> dict:
    settings = get_settings()
    root = settings.downloads_path
    if not root.exists():
        return {"imported": 0, "errors": []}
    imported = 0
    errors: list[str] = []
    manifest_result = import_manifest_download_batches(session, minimum_age_seconds)
    imported += manifest_result["imported"]
    errors.extend(manifest_result["errors"])
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
            "fingerprint": None,
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
        enqueue_task(session, "jellyfin_scan", {})
    return {"imported": imported, "errors": errors}


def import_manifest_download_batches(session: Session, minimum_age_seconds: int) -> dict:
    entries_by_batch: dict[str, list[dict]] = {}
    for entry in load_download_manifest():
        batch_id = entry.get("batch_id")
        if batch_id and entry.get("status") in {"queued", "verified"}:
            entries_by_batch.setdefault(batch_id, []).append(entry)
    imported = 0
    errors: list[str] = []
    for batch_id, entries in entries_by_batch.items():
        batch = session.get(ProposalBatch, batch_id)
        if not batch or batch.status not in {ProposalStatus.executing, ProposalStatus.failed, ProposalStatus.pending, ProposalStatus.approved}:
            continue
        result = process_download_manifest_batch(session, batch, entries, minimum_age_seconds)
        imported += result["imported"]
        errors.extend(result["errors"])
    return {"imported": imported, "errors": errors}


def process_download_manifest_batch(session: Session, batch: ProposalBatch, entries: list[dict], minimum_age_seconds: int) -> dict:
    blocking_items = selected_download_blockers(batch)
    if blocking_items:
        for item in blocking_items:
            set_download_item_status(item, "needs attention before this batch can finish")
        return {"imported": 0, "errors": []}
    expected_item_ids = selected_slskd_download_item_ids(batch)
    if not expected_item_ids:
        return {"imported": 0, "errors": []}
    entries = reconcile_manifest_entries_to_selected_items(session, batch, entries, expected_item_ids)
    entries = [entry for entry in entries if entry.get("item_id") in expected_item_ids]
    manifest_item_ids = {entry.get("item_id") for entry in entries}
    if not expected_item_ids.issubset(manifest_item_ids):
        for item_id in expected_item_ids - manifest_item_ids:
            set_download_item_status(session.get(ProposalItem, item_id), "waiting for slskd queue record")
        session.flush()
        return {"imported": 0, "errors": []}
    ready_entries = []
    errors: list[str] = []
    for entry in entries:
        file_path, wait_status = manifest_entry_file_path(entry, minimum_age_seconds)
        if not file_path:
            set_download_item_status(session.get(ProposalItem, entry.get("item_id")), wait_status)
            session.flush()
            return {"imported": 0, "errors": []}
        set_download_item_status(session.get(ProposalItem, entry.get("item_id")), "downloaded; waiting for batch verification")
        ready_entries.append((entry, file_path))

    verified_entries = []
    for entry, file_path in ready_entries:
        if entry.get("status") == "verified" and entry.get("path") and entry.get("metadata"):
            verified_entries.append((entry, Path(entry["path"]), entry["metadata"]))
            continue
        set_download_item_status(session.get(ProposalItem, entry.get("item_id")), "verifying with AcoustID")
        session.flush()
        verification = verify_downloaded_file(session, file_path, entry)
        if verification["status"] == "mismatch":
            handle_download_mismatch(session, batch, entry, file_path, verification)
            return {"imported": 0, "errors": [verification["message"]]}
        if verification["status"] != "verified":
            message = verification.get("message") or f"{file_path.name} could not be verified with AcoustID."
            handle_download_verification_issue(session, batch, entry, file_path, message, verification)
            return {"imported": 0, "errors": [message]}
        metadata = {**read_audio_metadata(file_path), **verification.get("metadata", {})}
        update_download_manifest_entry(entry, "verified", path=str(file_path), metadata=metadata)
        set_download_item_status(session.get(ProposalItem, entry.get("item_id")), "verified; waiting for the rest of the batch")
        verified_entries.append((entry, file_path, metadata))

    try:
        for entry, _file_path, _metadata in verified_entries:
            set_download_item_status(session.get(ProposalItem, entry.get("item_id")), "importing verified batch")
        imported = import_verified_download_batch(session, batch, verified_entries)
    except Exception as error:  # noqa: BLE001 - keep the batch visible in Downloads if finalization fails.
        batch.status = ProposalStatus.failed
        create_notification(
            session,
            title="Download import failed",
            body=str(error),
            event_type="task_failed",
            target_url="/downloads",
        )
        session.commit()
        return {"imported": 0, "errors": [str(error)]}
    return {"imported": imported, "errors": errors}


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


def selected_download_blockers(batch: ProposalBatch) -> list[ProposalItem]:
    blockers = []
    for item in batch.items:
        if not item.selected or item.kind != ProposalKind.download:
            continue
        action = json.loads(item.payload_json or "{}").get("action")
        if action == "queue_ytdlp_download" and item.status not in {ProposalStatus.completed, ProposalStatus.rejected}:
            blockers.append(item)
        elif action == "queue_download" and item.status == ProposalStatus.failed:
            blockers.append(item)
    return blockers


def selected_slskd_download_item_ids(batch: ProposalBatch) -> set[str]:
    ids = set()
    for item in batch.items:
        if not item.selected or item.kind != ProposalKind.download:
            continue
        if json.loads(item.payload_json or "{}").get("action") == "queue_download":
            ids.add(item.id)
    return ids


def manifest_entry_file_path(entry: dict, minimum_age_seconds: int) -> tuple[Path | None, str]:
    root = get_settings().downloads_path
    known_path = entry.get("path")
    candidates = []
    if known_path:
        candidates.append(Path(known_path))
    candidates.extend(root.rglob("*"))
    now = time.time()
    for file_path in candidates:
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        if not manifest_entry_matches_path(entry, file_path):
            continue
        try:
            stat = file_path.stat()
        except OSError:
            continue
        if now - stat.st_mtime < minimum_age_seconds:
            return None, "downloaded; waiting for file to settle"
        return file_path
    return None, "waiting for downloaded file"


def manifest_entry_matches_path(entry: dict, file_path: Path) -> bool:
    basename = file_path.name.casefold()
    if entry.get("basename") == basename:
        return True
    filename = str((entry.get("candidate") or {}).get("filename") or "").replace("\\", "/").casefold()
    return bool(filename and str(file_path).replace("\\", "/").casefold().endswith(filename))


def handle_download_mismatch(session: Session, batch: ProposalBatch, entry: dict, file_path: Path, verification: dict) -> None:
    try:
        file_path.unlink()
    except OSError as error:
        verification["message"] = f"{verification['message']} Could not remove {file_path.name}: {error}"
    update_download_manifest_entry(entry, "mismatch", path=str(file_path), metadata=verification.get("metadata"))
    item = session.get(ProposalItem, entry.get("item_id"))
    if item:
        item.status = ProposalStatus.failed
        item.selected = False
        set_download_item_status(item, verification["message"])
    batch.status = ProposalStatus.failed
    request = entry.get("request") or verification.get("request") or {}
    if request:
        add_replacement_candidates_to_download_batch(session, batch, entry, request)
    create_notification(
        session,
        title="Downloaded track did not match",
        body=verification["message"],
        event_type="task_failed",
        target_url="/downloads",
    )
    session.commit()


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


def add_replacement_candidates_to_download_batch(session: Session, batch: ProposalBatch, entry: dict, request: dict) -> int:
    query = download_query(request)
    search_result = search_slskd_for_request(session, {**request, "multiple_candidates": True}, limit=4)
    parent_id = entry.get("parent_id")
    if not parent_id:
        parent = add_download_tree_parents(session, batch, request, query)
        parent_id = parent.id
    candidates = search_result["candidates"]
    for index, candidate in enumerate(candidates):
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=parent_id,
                title=f"Replacement: {candidate.get('filename') or query}",
                kind=ProposalKind.download,
                selected=index == 0,
                old_value=query,
                new_value=candidate.get("username"),
                payload_json=json.dumps(
                    {
                        "action": "queue_download",
                        "request": {**request, "multiple_candidates": True},
                        "candidate": candidate,
                    }
                ),
            )
        )
    return len(candidates)


def import_verified_download_batch(session: Session, batch: ProposalBatch, verified_entries: list[tuple[dict, Path, dict]]) -> int:
    known_paths = existing_library_and_proposal_paths(session)
    imported = 0
    for entry, file_path, metadata in sorted(verified_entries, key=lambda item: ((item[2].get("disc_number") or 0), (item[2].get("track_number") or 9999), item[2].get("title") or "")):
        normalized_metadata = normalize_download_metadata(metadata, entry.get("request") or {})
        write_audio_metadata(file_path, normalized_metadata)
        target_path = unique_destination(suggest_library_path(normalized_metadata, file_path))
        if str(target_path) in known_paths:
            update_download_manifest_entry(entry, "completed", path=str(file_path), metadata=normalized_metadata)
            continue
        payload = {
            "path": str(file_path),
            "relative_path": str(file_path.relative_to(get_settings().downloads_path)),
            "extension": file_path.suffix.lower(),
            "size_bytes": file_path.stat().st_size,
            "mtime_ns": file_path.stat().st_mtime_ns,
            "metadata": normalized_metadata,
            "fingerprint": None,
            "suggested_library_path": str(target_path),
        }
        import_file_to_library(session, file_path, target_path, payload)
        mark_matching_wishlist_completed(session, normalized_metadata)
        update_download_manifest_entry(entry, "completed", path=str(target_path), metadata=normalized_metadata)
        imported += 1
        known_paths.add(str(target_path))
    for item in batch.items:
        if item.status in {ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.pending}:
            item.status = ProposalStatus.completed
    batch.status = ProposalStatus.completed
    session.flush()
    return imported


def verify_downloaded_file(session: Session, file_path: Path, manifest_entry: dict | None) -> dict:
    if not manifest_entry:
        return {"status": "unknown"}
    request = manifest_entry.get("request") or {}
    api_key = integration_settings(session).get("acoustid_api_key", "")
    if not api_key:
        return {"status": "unknown", "request": request, "message": "AcoustID API key is required before downloaded batches can be verified."}
    try:
        candidates = lookup_recording_by_fingerprint({"path": str(file_path)}, api_key)
    except Exception as error:  # noqa: BLE001 - verification should not block an otherwise usable download when AcoustID is unavailable.
        return {"status": "unknown", "request": request, "error": str(error), "message": f"AcoustID lookup failed for {file_path.name}: {error}"}
    if not candidates:
        return {"status": "unknown", "request": request, "message": f"AcoustID did not return a match for {file_path.name}."}
    best = candidates[0]
    candidate_metadata = best.get("metadata") or {}
    if metadata_matches_request(candidate_metadata, request):
        return {"status": "verified", "request": request, "metadata": verified_download_metadata(candidate_metadata, request)}
    expected = download_query(request)
    found = " ".join(
        str(part)
        for part in [candidate_metadata.get("artist"), candidate_metadata.get("album"), candidate_metadata.get("title")]
        if part
    ).strip()
    return {
        "status": "mismatch",
        "request": request,
        "metadata": candidate_metadata,
        "message": f"{file_path.name} matched {found or 'a different recording'} instead of {expected or 'the requested track'}.",
    }


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
    grouped: dict[tuple[str, str], list[dict]] = {}
    wishlist_item_ids = []
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        artist = payload.get("artist") or "Unknown Artist"
        album = payload.get("album") or "Singles"
        if payload.get("wishlist_item_id"):
            wishlist_item_ids.append(payload["wishlist_item_id"])
        grouped.setdefault((artist, album), []).append(payload)
    append_task_log(session, task, f"Preparing download candidate searches for {len(items)} tracks across {len(grouped)} album batch(es)")
    for (artist, album), requests in grouped.items():
        create_album_download_candidate_batch(session, artist, album, requests, task)
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


def create_album_download_candidate_batch(session: Session, artist: str, album: str, requests: list[dict], task: Task | None = None) -> None:
    batch = ProposalBatch(title=f"Download candidates: {artist} / {album}", kind=ProposalKind.download, tree_path="/downloads")
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
    track_items: list[tuple[dict, ProposalItem]] = []
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
        track_items.append((request, track_item))
    session.commit()

    slskd_tracks = 0
    fallback_tracks = 0
    diagnostic_lines = []
    append_task_log(session, task, f"{artist} / {album}: searching slskd for an album folder")
    folder_pool = search_album_folder_pool(session, artist, album, requests, task)
    if folder_pool:
        diagnostic_lines.append(
            f"{artist} {album}: using {folder_pool.get('folder') or 'matched folder'} from {folder_pool.get('username')} for {folder_pool.get('matched_tracks', 0)} tracks."
        )
        append_task_log(
            session,
            task,
            f"{artist} / {album}: matched album folder from {folder_pool.get('username')} with {folder_pool.get('matched_tracks', 0)} track(s)",
        )
    else:
        append_task_log(session, task, f"{artist} / {album}: no reusable album folder was found", "warning")
    total_tracks = max(1, len(track_items))
    for track_index, (request, track_item) in enumerate(track_items, start=1):
        query = download_query(request)
        track_title = request.get("track") or request.get("title") or query
        set_item_payload_status(track_item, f"searching {track_title}")
        if task is not None:
            update_task_progress(session, task, track_index - 1, total_tracks, f"Searching download candidates for {track_title}")
        session.commit()
        folder_candidate = candidate_from_folder_pool(folder_pool, request) if folder_pool else None
        if folder_candidate:
            candidates = [folder_candidate]
            diagnostic_lines.append(f"{query}: reused {folder_candidate.get('folder') or 'the same folder'} from {folder_candidate.get('username')}.")
            append_task_log(session, task, f"{track_title}: reused album-folder candidate {folder_candidate.get('filename')}")
        else:
            candidate_limit = 4 if request.get("multiple_candidates") else 1
            search_result = search_slskd_for_request(session, request, limit=candidate_limit, task=task)
            candidates = search_result["candidates"]
            diagnostic_lines.append(slskd_diagnostic_body(query, search_result["diagnostics"]))
            if candidates and not request.get("multiple_candidates") and not folder_pool:
                folder_pool = download_folder_pool(candidates[0])
        if candidates:
            slskd_tracks += 1
            set_item_payload_status(track_item, "candidate ready")
            append_task_log(session, task, f"{track_title}: {len(candidates)} slskd candidate(s) ready")
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
                            }
                        ),
                    )
                )
        else:
            fallback_tracks += 1
            set_item_payload_status(track_item, "fallback ready")
            append_task_log(session, task, f"{track_title}: no slskd candidates found; YouTube fallback prepared", "warning")
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    parent_id=track_item.id,
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
        session.commit()
        if task is not None:
            update_task_progress(session, task, track_index, total_tracks, f"Prepared download candidate for {track_title}")
    session.flush()
    append_task_log(session, task, f"{artist} / {album}: candidate search finished with {slskd_tracks} slskd track(s) and {fallback_tracks} fallback track(s)")
    create_notification(
        session,
        title="Download candidates ready",
        body=f"{album}: {slskd_tracks} tracks with slskd candidates. {fallback_tracks} tracks using YouTube fallback. {' '.join(diagnostic_lines[:3])}",
        event_type="approval_needed",
        target_url="/downloads",
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


def set_item_payload_status(item: ProposalItem, status: str) -> None:
    payload = json.loads(item.payload_json or "{}")
    payload["status"] = status
    item.payload_json = json.dumps(payload)


def set_download_item_status(item: ProposalItem | None, status: str) -> None:
    if not item:
        return
    set_item_payload_status(item, status)
    if item.parent and item.parent.kind == ProposalKind.download:
        set_item_payload_status(item.parent, status)
        item.parent.status = ProposalStatus.executing


def download_folder_pool(candidate: dict) -> dict | None:
    folder_files = candidate.get("folder_files") or []
    if not folder_files:
        return None
    return {
        "username": candidate.get("username"),
        "folder": candidate.get("folder"),
        "files": folder_files,
        "query": candidate.get("query"),
    }


def search_album_folder_pool(session: Session, artist: str, album: str, requests: list[dict], task: Task | None = None) -> dict | None:
    if not requests:
        return None
    settings = integration_settings(session)
    query = " ".join(part for part in [artist, album] if part).strip()
    if not query:
        return None
    try:
        append_task_log(session, task, f"slskd album search started: {query}")
        result = search_slskd_detailed(
            settings.get("slskd_url", ""),
            settings.get("slskd_api_key", ""),
            query,
            limit=40,
            timeout_seconds=12,
            timeout_buffer_seconds=2,
        )
        diagnostics = result.get("diagnostics") or {}
        append_task_log(
            session,
            task,
            (
                f"slskd album search finished: {query}: "
                f"{diagnostics.get('responses', 0)} responses, {diagnostics.get('files', 0)} files, "
                f"{len(result.get('folder_candidates') or [])} folder candidates"
            ),
        )
    except Exception as error:
        append_task_log(session, task, f"slskd album search failed: {query}: {error}", "warning")
        return None
    pools: dict[tuple[str, str], dict] = {}
    for candidate in result.get("folder_candidates") or result.get("candidates", []):
        pool = download_folder_pool(candidate)
        if not pool:
            continue
        key = (str(pool.get("username") or ""), str(pool.get("folder") or ""))
        current = pools.setdefault(key, {**pool, "files": []})
        known_filenames = {str(file_info.get("filename") or "") for file_info in current["files"]}
        for file_info in pool.get("files") or []:
            filename = str(file_info.get("filename") or "")
            if filename and filename not in known_filenames:
                current["files"].append(file_info)
                known_filenames.add(filename)
    ranked = sorted(
        ((score_album_folder_pool(pool, requests), pool) for pool in pools.values()),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0][0] == 0:
        return None
    best_score, best_pool = ranked[0]
    best_pool["matched_tracks"] = best_score[0]
    best_pool["album_query"] = query
    return best_pool


def score_album_folder_pool(pool: dict, requests: list[dict]) -> tuple[int, int, int]:
    matched = 0
    lossless = 0
    for request in requests:
        candidate = candidate_from_folder_pool(pool, request)
        if not candidate:
            continue
        matched += 1
        if candidate.get("quality") == "lossless":
            lossless += 1
    return (matched, lossless, len(pool.get("files") or []))


def candidate_from_folder_pool(pool: dict | None, request: dict) -> dict | None:
    if not pool:
        return None
    track = normalize_match_text(request.get("track") or request.get("title"))
    if not track:
        return None
    files = pool.get("files") or []
    ranked = sorted(
        (file_info for file_info in files if filename_matches_track(file_info.get("filename"), track)),
        key=lambda file_info: folder_file_score(file_info, track),
    )
    if not ranked:
        return None
    file_info = ranked[0]
    return {
        "username": pool.get("username"),
        "query": download_query(request),
        "filename": file_info.get("filename"),
        "folder": pool.get("folder"),
        "size": file_info.get("size"),
        "quality": "lossless" if str(file_info.get("filename") or "").lower().endswith((".flac", ".wav", ".aiff", ".alac")) else "unknown",
        "files": [file_info],
        "folder_files": files,
    }


def filename_matches_track(filename: str | None, normalized_track: str) -> bool:
    stem = Path(str(filename or "").replace("\\", "/")).stem
    normalized_stem = normalize_match_text(stem)
    if not normalized_stem:
        return False
    if normalized_track in normalized_stem or normalized_stem in normalized_track:
        return True
    stripped_stem = normalize_match_text(re.sub(r"^\d+\s*[-_. ]\s*", "", stem))
    return normalized_track in stripped_stem or stripped_stem in normalized_track


def folder_file_score(file_info: dict, normalized_track: str) -> tuple[int, int, str]:
    filename = str(file_info.get("filename") or "")
    stem = normalize_match_text(Path(filename.replace("\\", "/")).stem)
    quality_rank = 0 if filename.lower().endswith((".flac", ".wav", ".aiff", ".alac")) else 1
    return (quality_rank, abs(len(stem) - len(normalized_track)), filename)


def search_slskd_for_request(session: Session, request: dict, limit: int = 1, task: Task | None = None) -> dict:
    settings = integration_settings(session)
    slskd_url = settings.get("slskd_url", "")
    api_key = settings.get("slskd_api_key", "")
    attempted = []
    last_result = {"candidates": [], "diagnostics": {"queries": []}}
    for index, query in enumerate(download_query_variants(request)):
        if not query or query in attempted:
            continue
        attempted.append(query)
        append_task_log(session, task, f"slskd track search started: {query}")
        result = search_slskd_detailed(
            slskd_url,
            api_key,
            query,
            limit=limit,
            timeout_seconds=12 if index == 0 else 6,
            timeout_buffer_seconds=3,
        )
        result["diagnostics"]["query"] = query
        result["diagnostics"]["queries"] = attempted.copy()
        last_result = result
        diagnostics = result["diagnostics"]
        append_task_log(
            session,
            task,
            (
                f"slskd track search finished: {query}: "
                f"{diagnostics.get('responses', 0)} responses, {diagnostics.get('files', 0)} files, "
                f"{len(result.get('candidates') or [])} candidates after {diagnostics.get('polls', 0)} polls"
            ),
        )
        if result["candidates"]:
            return result
    last_result["diagnostics"]["queries"] = attempted
    append_task_log(session, task, f"slskd found no candidates after {len(attempted)} query variant(s): {download_query(request)}", "warning")
    return last_result


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


def download_query_variants(request: dict) -> list[str]:
    artist = str(request.get("artist") or "").strip()
    album = str(request.get("album") or "").strip()
    track = str(request.get("track") or request.get("title") or "").strip()
    return [
        " ".join(part for part in [artist, album, track] if part),
        " ".join(part for part in [artist, track] if part),
        f"{artist} - {track}".strip(" -"),
        " ".join(part for part in [album, track] if part),
        track,
    ]


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
        "metadata_locked",
        "artwork_locked",
        "filename_locked",
    }


def apply_file_action_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    source_path = Path(item.old_value or "")
    track = session.get(Track, payload.get("track_id"))
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

    ensure_favorites_playlist(session)
    playlists = list(
        session.scalars(
            select(Playlist)
            .options(selectinload(Playlist.tracks).selectinload(PlaylistTrack.track).selectinload(Track.album).selectinload(Album.artist))
            .order_by(Playlist.name.asc())
        )
    )

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
        local_by_name = {playlist.name: playlist for playlist in playlists}
        imported_playlist_names = set()
        for jellyfin_playlist in jellyfin_playlists.values():
            name = jellyfin_playlist.get("Name")
            if not name or name in local_by_name:
                continue
            playlist = Playlist(name=name, jellyfin_playlist_id=jellyfin_playlist.get("Id"), protected=name == "Favorites")
            session.add(playlist)
            session.flush()
            local_by_name[name] = playlist
            playlists.append(playlist)
            imported_playlist_names.add(name)

        for playlist in playlists:
            jellyfin_playlist_id = playlist.jellyfin_playlist_id or (jellyfin_playlists.get(playlist.name) or {}).get("Id")
            if not jellyfin_playlist_id:
                created = client.post("/Playlists", params={"Name": playlist.name, "UserId": user_id})
                created.raise_for_status()
                jellyfin_playlist_id = created.json().get("Id")
                playlist.jellyfin_playlist_id = jellyfin_playlist_id
                session.flush()
            jellyfin_items = jellyfin_playlist_items(client, user_id, jellyfin_playlist_id)
            local_item_ids = [item_id for item_id in (find_jellyfin_audio_item(client, user_id, entry.track) for entry in sorted(playlist.tracks, key=lambda entry: (entry.position, entry.created_at))) if item_id]
            jellyfin_item_ids = [item.get("Id") for item in jellyfin_items if item.get("Id")]
            if conflict_winner == "jellyfin" or playlist.name in imported_playlist_names:
                pulled_tracks += sync_playlist_from_jellyfin(session, playlist, jellyfin_items)
            else:
                removed = remove_jellyfin_playlist_items(client, user_id, jellyfin_playlist_id, jellyfin_items, set(local_item_ids))
                missing_ids = [item_id for item_id in local_item_ids if item_id not in set(jellyfin_item_ids)]
                if missing_ids:
                    add_jellyfin_playlist_items(client, user_id, jellyfin_playlist_id, missing_ids)
                pushed_tracks += len(missing_ids) + removed
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
    missing_records = [file_info_for_existing_library_file(Path(path)) for path in missing_record_paths]
    queued_missing_files = queue_missing_file_downloads(session, missing_files)
    queued_missing_records = queue_missing_record_imports(session, missing_records)
    create_notification(
        session,
        title="File check complete",
        body=f"{len(missing_files)} records missing files. {len(missing_records)} files missing records. {queued_missing_files + queued_missing_records} fixes added to the task queue.",
        event_type="tool_completed",
        target_url="/task-queue",
    )
    return {
        "missing_files": missing_files,
        "missing_records": [],
        "queued_missing_files": queued_missing_files,
        "queued_missing_records": queued_missing_records,
    }


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
        "fingerprint": None,
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
        results = search_album_releases(album.artist.name, album.title)
        cover_url = next((result.get("cover_art_url") for result in results if result.get("cover_art_url")), None)
        if not cover_url:
            continue
        found += 1
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=f"{album.artist.name} / {album.title}",
                kind=ProposalKind.metadata,
                old_value=json.dumps({"cover_path": album.cover_path}),
                new_value=json.dumps({"cover_path": cover_url}),
                payload_json=json.dumps(
                    {
                        "target_type": "album",
                        "target_id": album.id,
                        "changes": {"cover_path": cover_url},
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


def run_check_missing_tracks(session: Session, _payload: dict) -> dict:
    albums = list(
        session.scalars(
            select(Album)
            .where(Album.musicbrainz_release_id.is_not(None))
            .options(selectinload(Album.artist), selectinload(Album.tracks))
        )
    )
    created = 0
    checked = 0
    batch = ProposalBatch(title="Missing album tracks", kind=ProposalKind.download, tree_path="/library")
    session.add(batch)
    session.flush()
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    for album in albums:
        checked += 1
        record = lookup_album_tracks(album.artist.name, album.title, album.musicbrainz_release_id)
        existing_numbers = {track.track_number for track in album.tracks if track.track_number}
        for track in record.get("tracks", []):
            track_number = track.get("track_number")
            if not track_number or track_number in existing_numbers:
                continue
            add_download_request_item(session, batch, artist_items, album_items, album.artist.name, album.title, track.get("title"))
            created += 1
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


def add_download_request_item(
    session: Session,
    batch: ProposalBatch,
    artist_items: dict[str, ProposalItem],
    album_items: dict[tuple[str, str], ProposalItem],
    artist: str,
    album: str,
    track: str | None,
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
    playlist = session.scalar(select(Playlist).where(Playlist.name == "Favorites"))
    if not playlist:
        playlist = Playlist(name="Favorites", protected=True)
        session.add(playlist)
        session.flush()
    elif not playlist.protected:
        playlist.protected = True
    return playlist


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
        response = client.get(f"/Playlists/{playlist_id}/Items", params={"UserId": user_id})
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {404, 405}:
            return []
        raise
    return response.json().get("Items", [])


def add_jellyfin_playlist_items(client: httpx.Client, user_id: str, playlist_id: str, item_ids: list[str]) -> None:
    response = client.post(f"/Playlists/{playlist_id}/Items", params={"Ids": ",".join(item_ids), "UserId": user_id})
    if response.is_success:
        return
    if response.status_code < 500:
        response.raise_for_status()
    failures = []
    for item_id in item_ids:
        single = client.post(f"/Playlists/{playlist_id}/Items", params={"Ids": item_id, "UserId": user_id})
        if not single.is_success:
            failures.append(f"{item_id}: {single.status_code} {single.text[-300:]}")
    if failures:
        raise RuntimeError(f"Jellyfin playlist sync failed for {len(failures)} item(s): {'; '.join(failures[:3])}")


def remove_jellyfin_playlist_items(client: httpx.Client, user_id: str, playlist_id: str, items: list[dict], keep_item_ids: set[str]) -> int:
    entry_ids = [
        str(item.get("PlaylistItemId") or item.get("PlaylistItemID") or "")
        for item in items
        if item.get("Id") not in keep_item_ids and (item.get("PlaylistItemId") or item.get("PlaylistItemID"))
    ]
    if not entry_ids:
        return 0
    response = client.delete(f"/Playlists/{playlist_id}/Items", params={"EntryIds": ",".join(entry_ids), "UserId": user_id})
    if response.status_code in {404, 405, 501}:
        return 0
    response.raise_for_status()
    return len(entry_ids)


def sync_playlist_from_jellyfin(session: Session, playlist: Playlist, jellyfin_items: list[dict]) -> int:
    next_entries: list[tuple[Track, int]] = []
    for index, item in enumerate(jellyfin_items, start=1):
        track = find_local_track_for_jellyfin_item(session, item)
        if track:
            next_entries.append((track, index))
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
    name = item.get("Name")
    if not name:
        return None
    candidates = list(
        session.scalars(
            select(Track)
            .where(Track.title == name)
            .options(selectinload(Track.album).selectinload(Album.artist))
        )
    )
    artist_names = {
        normalize_match_text(value)
        for value in [item.get("AlbumArtist"), item.get("Artist"), *(item.get("Artists") or [])]
        if value
    }
    album_name = normalize_match_text(item.get("Album"))
    for track in candidates:
        artist_match = not artist_names or normalize_match_text(track.album.artist.name) in artist_names
        album_match = not album_name or normalize_match_text(track.album.title) == album_name
        if artist_match and album_match:
            return track
    return candidates[0] if candidates else None


def find_jellyfin_audio_item(client: httpx.Client, user_id: str, track: Track) -> str | None:
    response = client.get(
        f"/Users/{user_id}/Items",
        params={"Recursive": "true", "IncludeItemTypes": "Audio", "SearchTerm": track.title},
    )
    response.raise_for_status()
    normalized_path = str(track.path or "").lower()
    for item in response.json().get("Items", []):
        path = str(item.get("Path") or "").lower()
        if normalized_path and path == normalized_path:
            return item.get("Id")
    return (response.json().get("Items") or [{}])[0].get("Id")


TASK_HANDLERS = {
    "propose_import": run_propose_import,
    "execute_proposal_batch": run_execute_proposal_batch,
    "process_wishlist": run_process_wishlist,
    "sync_favorites_jellyfin": run_sync_favorites_jellyfin,
    "jellyfin_scan": run_jellyfin_scan,
    "check_files": run_check_files,
    "check_lyrics": run_check_lyrics,
    "check_album_covers": run_check_album_covers,
    "check_missing_tracks": run_check_missing_tracks,
    "backup_now": run_backup_now,
    "restore_default": run_restore_default,
    "restore_backup": run_restore_backup,
}


async def worker_loop() -> None:
    with SessionLocal() as session:
        init_db(session)

    last_download_scan = 0.0
    while True:
        with SessionLocal() as session:
            task = claim_next_task(session)
            if not task:
                if time.time() - last_download_scan > 15:
                    try:
                        import_completed_downloads(session)
                    except Exception as error:  # noqa: BLE001 - idle scans should never stop the worker.
                        create_notification(session, title="Download scan failed", body=str(error), event_type="task_failed", target_url="/activity")
                    last_download_scan = time.time()
                await deliver_apns_notifications(session)
                time.sleep(2)
                continue

            try:
                handler = TASK_HANDLERS.get(task.type)
                if not handler:
                    raise ValueError(f"No handler registered for task type {task.type}")
                if task.type == "execute_proposal_batch":
                    result = handler(session, task_to_payload(task), task)
                else:
                    result = handler(session, task_to_payload(task))
                session.refresh(task)
                if task.status == TaskStatus.canceled:
                    continue
                if result.get("errors"):
                    result = merge_task_logs(task, result)
                    task.status = TaskStatus.failed
                    task.result_json = json.dumps(result)
                    task.error = result["errors"][0]
                    task.lease_until = None
                    session.commit()
                else:
                    complete_task(session, task, result)
            except Exception as error:  # noqa: BLE001 - worker must persist task failures.
                create_notification(
                    session,
                    title=f"{task.type} failed",
                    body=str(error),
                    event_type="task_failed",
                    target_url="/activity",
                )
                fail_task(session, task, str(error))


if __name__ == "__main__":
    asyncio.run(worker_loop())
