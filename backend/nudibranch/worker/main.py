import asyncio
import json
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
from nudibranch.services.imports import SUPPORTED_AUDIO_EXTENSIONS, discover_import_files, read_audio_metadata, suggest_library_path
from nudibranch.services.notifications import create_notification, deliver_apns_notifications
from nudibranch.services.metadata_lookup import search_album_releases, lookup_album_tracks
from nudibranch.services.settings_store import integration_settings
from nudibranch.services.slskd import queue_slskd_download, search_slskd_detailed
from nudibranch.services.tasks import claim_next_task, complete_task, fail_task, task_to_payload, update_task_progress


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

    if batch.kind == ProposalKind.import_files and not executable_items and not download_items:
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
    failed_download_requests: list[dict] = []
    for item in wishlist_download_items:
        try:
            note_progress("Searching wishlist downloads", item)
            process_wishlist_request_items(session, wishlist_download_items)
            for wishlist_item in wishlist_download_items:
                wishlist_item.status = ProposalStatus.completed
            download_changes += len(wishlist_download_items)
            finish_progress_step("Wishlist download candidates created")
            break
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            for wishlist_item in wishlist_download_items:
                wishlist_item.status = ProposalStatus.failed
            errors.append(f"Download search: {error}")
            finish_progress_step("Wishlist download search failed")

    for item in direct_download_items:
        try:
            note_progress(f"Queueing download for {item.title}", item)
            apply_download_item(session, item)
            item.status = ProposalStatus.completed
            download_changes += 1
            finish_progress_step(f"Queued download for {item.title}")
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            download_errors.append(f"{item.title}: {error}")
            failed_request = download_request_from_item(item)
            if failed_request:
                failed_download_requests.append(failed_request)
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

    if failed_download_requests:
        create_download_retry_import_batch(session, failed_download_requests)
    downloaded_review = scan_downloads_to_import_review(session)

    if errors:
        batch.status = ProposalStatus.failed
    elif all(item.status in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.failed} or not item.selected for item in batch.items):
        batch.status = ProposalStatus.completed
    else:
        batch.status = ProposalStatus.pending
    session.commit()

    create_notification(
        session,
        title="Task queue item failed" if errors else "Task queue item completed",
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
        ),
        event_type="task_completed",
        target_url="/activity",
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
        "downloaded_review": downloaded_review,
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
    body = ". ".join(parts) + "."
    if errors:
        return f"{body} First failure: {errors[0]}"
    if download_errors:
        return f"{body} {len(download_errors)} downloads need another candidate and were sent back to Import/Add. First download issue: {trim_message(download_errors[0])}"
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


def import_track_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    source_path = Path(item.old_value)
    target_path = Path(item.new_value)
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

    album = session.scalar(select(Album).where(Album.artist_id == artist.id, Album.title == album_title))
    if not album:
        album = Album(artist_id=artist.id, title=album_title, path=str(target_path.parent))
        session.add(album)
        session.flush()

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


def apply_download_item(session: Session, item: ProposalItem) -> None:
    payload = json.loads(item.payload_json or "{}")
    action = payload.get("action")
    if action == "wishlist_request":
        create_download_candidate_batch(session, payload)
        return
    if action == "queue_download":
        settings = integration_settings(session)
        queue_slskd_download_with_candidate_fallbacks(session, item, settings.get("slskd_url", ""), settings.get("slskd_api_key", ""))
        return
    if action == "queue_ytdlp_download":
        queue_ytdlp_download(session, payload.get("request") or {})
        return
    raise ValueError("Unsupported download action")


def queue_slskd_download_with_candidate_fallbacks(session: Session, item: ProposalItem, slskd_url: str, api_key: str) -> dict:
    attempts: list[str] = []
    for candidate_item in download_candidate_attempt_order(session, item):
        payload = json.loads(candidate_item.payload_json or "{}")
        candidate = payload.get("candidate") or {}
        label = candidate.get("filename") or candidate_item.title
        try:
            result = queue_slskd_download(slskd_url, api_key, candidate)
            if candidate_item.id != item.id:
                candidate_item.status = ProposalStatus.completed
                item.new_value = candidate_item.new_value
                item.payload_json = candidate_item.payload_json
            return result
        except Exception as error:  # noqa: BLE001 - try the next available candidate for this track.
            attempts.append(f"{label}: {error}")
            candidate_item.status = ProposalStatus.failed
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


def scan_downloads_to_import_review(session: Session, minimum_age_seconds: int = 20) -> dict:
    settings = get_settings()
    root = settings.downloads_path
    if not root.exists():
        return {"files": 0, "batch_id": None}
    now = time.time()
    pending_paths = existing_proposal_source_paths(session)
    files = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        if str(file_path) in pending_paths:
            continue
        stat = file_path.stat()
        if now - stat.st_mtime < minimum_age_seconds:
            continue
        metadata = read_audio_metadata(file_path)
        files.append(
            {
                "path": str(file_path),
                "relative_path": str(file_path.relative_to(root)),
                "extension": file_path.suffix.lower(),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "metadata": metadata,
                "fingerprint": None,
                "suggested_library_path": str(suggest_library_path(metadata, file_path)),
            }
        )
    if not files:
        return {"files": 0, "batch_id": None}
    result = run_propose_import(session, {"files": files, "download_requests": []})
    return {"files": len(files), "batch_id": result.get("batch_id")}


def existing_proposal_source_paths(session: Session) -> set[str]:
    return {
        str(path)
        for path in session.scalars(
            select(ProposalItem.old_value)
            .join(ProposalBatch, ProposalBatch.id == ProposalItem.batch_id)
            .where(ProposalItem.old_value.is_not(None))
            .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
        )
        if path
    }


def process_wishlist_request_items(session: Session, items: list[ProposalItem]) -> None:
    grouped: dict[tuple[str, str], list[dict]] = {}
    wishlist_item_ids = []
    for item in items:
        payload = json.loads(item.payload_json or "{}")
        artist = payload.get("artist") or "Unknown Artist"
        album = payload.get("album") or "Singles"
        if payload.get("wishlist_item_id"):
            wishlist_item_ids.append(payload["wishlist_item_id"])
        grouped.setdefault((artist, album), []).append(payload)
    for (artist, album), requests in grouped.items():
        create_album_download_candidate_batch(session, artist, album, requests)
    if wishlist_item_ids:
        for wishlist_item in session.scalars(select(WishlistItem).where(WishlistItem.id.in_(wishlist_item_ids))):
            wishlist_item.status = "approved"


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


def create_album_download_candidate_batch(session: Session, artist: str, album: str, requests: list[dict]) -> None:
    batch = ProposalBatch(title=f"Download candidates: {artist} / {album}", kind=ProposalKind.download, tree_path="/downloads")
    session.add(batch)
    session.flush()
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
    searching_item = ProposalItem(
        batch_id=batch.id,
        parent_id=album_item.id,
        title="Searching download candidates...",
        kind=ProposalKind.download,
        selected=False,
        payload_json=json.dumps({"status": "searching"}),
    )
    session.add(searching_item)
    session.commit()

    slskd_tracks = 0
    fallback_tracks = 0
    diagnostic_lines = []
    for request in requests:
        query = download_query(request)
        track_title = request.get("track") or request.get("title") or query
        searching_item.title = f"Searching {track_title}"
        session.commit()
        track_item = ProposalItem(
            batch_id=batch.id,
            parent_id=album_item.id,
            title=track_title,
            kind=ProposalKind.download,
            payload_json=json.dumps({"artist": artist, "album": album, "track": track_title}),
        )
        session.add(track_item)
        session.flush()
        search_result = search_slskd_for_request(session, request)
        candidates = search_result["candidates"]
        diagnostic_lines.append(slskd_diagnostic_body(query, search_result["diagnostics"]))
        if candidates:
            slskd_tracks += 1
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
    session.delete(searching_item)
    session.flush()
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
    searching_item = ProposalItem(
        batch_id=batch.id,
        parent_id=track_parent.id,
        title="Searching download candidates...",
        kind=ProposalKind.download,
        selected=False,
        payload_json=json.dumps({"status": "searching"}),
    )
    session.add(searching_item)
    session.commit()
    search_result = search_slskd_for_request(session, request)
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
    session.delete(searching_item)
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


def search_slskd_for_request(session: Session, request: dict) -> dict:
    settings = integration_settings(session)
    slskd_url = settings.get("slskd_url", "")
    api_key = settings.get("slskd_api_key", "")
    attempted = []
    last_result = {"candidates": [], "diagnostics": {"queries": []}}
    for index, query in enumerate(download_query_variants(request)):
        if not query or query in attempted:
            continue
        attempted.append(query)
        result = search_slskd_detailed(
            slskd_url,
            api_key,
            query,
            timeout_seconds=12 if index == 0 else 6,
            timeout_buffer_seconds=3,
        )
        result["diagnostics"]["query"] = query
        result["diagnostics"]["queries"] = attempted.copy()
        last_result = result
        if result["candidates"]:
            return result
    last_result["diagnostics"]["queries"] = attempted
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
    create_notification(
        session,
        title="Wishlist search finished",
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
        raise ValueError("Jellyfin URL and API key are required to sync Favorites")

    playlist = session.scalar(
        select(Playlist)
        .where(Playlist.name == "Favorites")
        .options(selectinload(Playlist.tracks).selectinload(PlaylistTrack.track))
    )
    if not playlist:
        return {"synced": 0, "message": "Favorites playlist has not been created yet"}

    headers = {"X-Emby-Token": jellyfin_api_key}
    with httpx.Client(base_url=jellyfin_url, headers=headers, timeout=25) as client:
        users = client.get("/Users")
        users.raise_for_status()
        user_id = (users.json() or [{}])[0].get("Id")
        if not user_id:
            raise ValueError("No Jellyfin user was found for playlist sync")

        jellyfin_playlist_id = playlist.jellyfin_playlist_id or find_jellyfin_playlist(client, user_id, "Favorites")
        if not jellyfin_playlist_id:
            created = client.post("/Playlists", params={"Name": "Favorites", "UserId": user_id})
            created.raise_for_status()
            jellyfin_playlist_id = created.json().get("Id")
            playlist.jellyfin_playlist_id = jellyfin_playlist_id
            session.commit()

        item_ids = [item_id for item_id in (find_jellyfin_audio_item(client, user_id, entry.track) for entry in playlist.tracks) if item_id]
        if item_ids:
            response = client.post(f"/Playlists/{jellyfin_playlist_id}/Items", params={"Ids": ",".join(item_ids), "UserId": user_id})
            response.raise_for_status()

    return {"synced": len(item_ids), "playlist": "Favorites"}


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
    audio_suffixes = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".alac"}
    tracks = list(
        session.scalars(
            select(Track)
            .where(Track.path.is_not(None))
            .options(selectinload(Track.album).selectinload(Album.artist))
        )
    )
    tracks_by_path = {str(track.path): track for track in tracks if track.path}
    db_paths = set(tracks_by_path)
    disk_paths = {
        str(path)
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
    create_notification(
        session,
        title="File check complete",
        body=f"{len(missing_files)} records missing files. {len(missing_records)} files missing records.",
        event_type="tool_completed",
        target_url="/tools",
    )
    return {"missing_files": missing_files, "missing_records": missing_records}


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
                if time.time() - last_download_scan > 45:
                    try:
                        scan_downloads_to_import_review(session)
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
