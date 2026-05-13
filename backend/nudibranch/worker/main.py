import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nudibranch.db.init import init_db
from nudibranch.db.models import Album, Artist, Playlist, PlaylistTrack, ProposalBatch, ProposalItem, ProposalKind, ProposalStatus, Track
from nudibranch.db.session import SessionLocal
from nudibranch.services.imports import discover_import_files
from nudibranch.services.notifications import create_notification, deliver_apns_notifications
from nudibranch.services.settings_store import integration_settings
from nudibranch.services.tasks import claim_next_task, complete_task, fail_task, task_to_payload


def run_propose_import(session: Session, payload: dict) -> dict:
    files = payload.get("files") or discover_import_files(payload.get("path"), include_fingerprint=True)
    batch = ProposalBatch(title="Import folder review", kind=ProposalKind.import_files, tree_path="/app/import")
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
    create_notification(
        session,
        title="Import review ready",
        body=f"{len(files)} files were added to the task queue.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"batch_id": batch.id, "files": len(files)}


def run_execute_proposal_batch(session: Session, payload: dict) -> dict:
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

    if batch.kind == ProposalKind.import_files and not executable_items:
        errors.append("No approved import file operations were selected.")

    for item in executable_items:
        try:
            import_track_item(session, item)
            item.status = ProposalStatus.completed
            imported += 1
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")

    metadata_updated = 0
    for item in metadata_items:
        try:
            apply_metadata_item(session, item)
            item.status = ProposalStatus.completed
            metadata_updated += 1
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")

    file_actions = 0
    for item in file_action_items:
        try:
            apply_file_action_item(session, item)
            item.status = ProposalStatus.completed
            file_actions += 1
        except Exception as error:  # noqa: BLE001 - keep executing independent selected items.
            item.status = ProposalStatus.failed
            errors.append(f"{item.title}: {error}")

    for item in batch.items:
        if not errors and item.status == ProposalStatus.approved:
            item.status = ProposalStatus.completed
        elif not item.selected and item.status == ProposalStatus.pending:
            skipped += 1

    if errors:
        batch.status = ProposalStatus.failed
    elif all(item.status in {ProposalStatus.completed, ProposalStatus.rejected} or not item.selected for item in batch.items):
        batch.status = ProposalStatus.completed
    else:
        batch.status = ProposalStatus.pending
    session.commit()

    create_notification(
        session,
        title="Task queue item failed" if errors else "Task queue item completed",
        body=task_queue_notification_body(imported, metadata_updated, file_actions, skipped, errors),
        event_type="task_completed",
        target_url="/activity",
    )
    return {
        "batch_id": batch_id,
        "imported": imported,
        "metadata_updated": metadata_updated,
        "file_actions": file_actions,
        "skipped": skipped,
        "errors": errors,
    }


def task_queue_notification_body(imported: int, metadata_updated: int, file_actions: int, skipped: int, errors: list[str]) -> str:
    body = f"{imported} tracks imported. {metadata_updated} metadata changes applied. {file_actions} files handled. {skipped} items skipped."
    if errors:
        return f"{body} First failure: {errors[0]}"
    return body


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

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        raise FileExistsError(f"{target_path} already exists")

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
    target_path = unique_destination(Path(item.new_value or ""))
    track = session.get(Track, payload.get("track_id"))
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
    create_notification(
        session,
        title="Wishlist search finished",
        body="Download candidates are ready to review.",
        event_type="approval_needed",
        target_url="/task-queue",
    )
    return {"status": "stubbed", "message": "slskd ranking pipeline placeholder created"}


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

    create_notification(
        session,
        title="Favorites synced",
        body=f"{len(item_ids)} tracks were sent to the Jellyfin Favorites playlist.",
        event_type="task_completed",
        target_url="/playlists",
    )
    return {"synced": len(item_ids), "playlist": "Favorites"}


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
}


async def worker_loop() -> None:
    with SessionLocal() as session:
        init_db(session)

    while True:
        with SessionLocal() as session:
            task = claim_next_task(session)
            if not task:
                await deliver_apns_notifications(session)
                time.sleep(2)
                continue

            try:
                handler = TASK_HANDLERS.get(task.type)
                if not handler:
                    raise ValueError(f"No handler registered for task type {task.type}")
                result = handler(session, task_to_payload(task))
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
