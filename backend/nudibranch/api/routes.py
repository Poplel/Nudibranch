import secrets
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nudibranch.api.deps import get_current_user, require_permission
from nudibranch.api.schemas import (
    AlbumLookupRequest,
    BackupRestoreRequest,
    CheckFileFixRequest,
    DeviceRegistration,
    FavoritesOut,
    ImportAcousticLookupRequest,
    IntegrationSettings,
    ImportScanRequest,
    LibraryMetadataProposalRequest,
    LibraryRemoveProposalRequest,
    LibraryTreeAlbum,
    LibraryTreeArtist,
    LibraryTreeTrack,
    LoginRequest,
    LoginResponse,
    NotificationOut,
    PlaylistAddTracks,
    PlaylistCreate,
    PlaylistPositionProposalRequest,
    PlaylistTrackOut,
    PlaylistUpdate,
    PermissionOut,
    ProposalBatchOut,
    ProposalApproveRequest,
    ProposalItemOut,
    ProposalRejectRequest,
    ProposalSelectionUpdate,
    TaskCreate,
    TaskOut,
    UserCreate,
    UserOut,
    UserPinUpdate,
    UserUpdate,
    WishlistApprovalRequest,
    WishlistCreate,
    WishlistOut,
)
from nudibranch.db.init import hash_secret
from nudibranch.db.models import (
    Album,
    Artist,
    MobileDevice,
    Notification,
    NotificationStatus,
    Permission,
    Playlist,
    PlaylistTrack,
    ProposalBatch,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    Task,
    Track,
    User,
    UserPermission,
    WishlistItem,
)
from nudibranch.core.config import get_settings
from nudibranch.db.session import get_session
from nudibranch.services.imports import discover_import_files, read_audio_metadata
from nudibranch.services.metadata_lookup import lookup_album_tracks, lookup_recording_by_fingerprint, search_album_releases
from nudibranch.services.notifications import create_notification
from nudibranch.services.proposals import approve_batch, reject_items, set_selection
from nudibranch.services.settings_store import integration_settings, integration_value, update_integration_settings
from nudibranch.services.tasks import cancel_task, enqueue_task, task_result, task_to_payload

router = APIRouter(prefix="/api/v1")


PERMISSION_SECTIONS = {
    Permission.library_read: "Library",
    Permission.library_write: "Library",
    Permission.library_manage: "Library",
    Permission.metadata_edit: "Metadata",
    Permission.import_run: "Import",
    Permission.approvals_manage: "Task Queue",
    Permission.wishlist_manage_own: "Wishlist",
    Permission.wishlist_manage_all: "Wishlist",
    Permission.downloads_manage: "Downloads",
    Permission.playlists_manage: "Playlists",
    Permission.activity_read: "Activity",
    Permission.notifications_read: "Notifications",
    Permission.settings_manage: "Settings",
    Permission.users_manage: "Users",
    Permission.backups_manage: "Tools",
    Permission.jellyfin_manage: "Tools",
}


@router.post("/auth/login", response_model=LoginResponse, tags=["auth"])
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    pin_hash = hash_secret(payload.pin)
    user = session.scalar(select(User).where(User.pin_hash == pin_hash))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid PIN")

    api_key = secrets.token_urlsafe(32)
    user.api_key_hash = hash_secret(api_key)
    session.commit()
    return LoginResponse(user_id=user.id, display_name=user.display_name, api_key=api_key, is_admin=user.is_admin)


@router.get("/me", response_model=UserOut, tags=["users"])
def me(user: User = Depends(get_current_user)) -> UserOut:
    return serialize_user(user)


@router.get("/permissions", response_model=list[PermissionOut], tags=["users"])
def permission_catalog(_: User = Depends(get_current_user)) -> list[PermissionOut]:
    return [
        PermissionOut(value=permission.value, label=permission_label(permission), section=PERMISSION_SECTIONS.get(permission, "System"))
        for permission in Permission
    ]


@router.get("/users", response_model=list[UserOut], tags=["users"])
def list_users(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> list[UserOut]:
    users = list(session.scalars(select(User).options(selectinload(User.permissions)).order_by(User.created_at.asc())))
    return [serialize_user(user) for user in users]


@router.post("/users", response_model=UserOut, tags=["users"])
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    user = User(
        display_name=payload.display_name.strip(),
        pin_hash=hash_secret(payload.pin),
        is_admin=payload.is_admin,
    )
    session.add(user)
    session.flush()
    set_user_permissions(session, user, payload.permissions)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.patch("/users/{user_id}", response_model=UserOut, tags=["users"])
def update_user(
    user_id: str,
    payload: UserUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.is_admin is not None:
        if user.is_admin and not payload.is_admin and count_admins(session) <= 1:
            raise HTTPException(status_code=400, detail="At least one admin user is required")
        user.is_admin = payload.is_admin
    if payload.permissions is not None:
        set_user_permissions(session, user, payload.permissions)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.post("/users/{user_id}/pin", response_model=UserOut, tags=["users"])
def update_user_pin(
    user_id: str,
    payload: UserPinUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.pin_hash = hash_secret(payload.pin)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.post("/me/pin", response_model=UserOut, tags=["users"])
def update_own_pin(
    payload: UserPinUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    user.pin_hash = hash_secret(payload.pin)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.get("/library/tree", response_model=list[LibraryTreeArtist], tags=["library"])
def library_tree(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_read)),
) -> list[LibraryTreeArtist]:
    artists = list(
        session.scalars(
            select(Artist).options(selectinload(Artist.albums).selectinload(Album.tracks)).order_by(Artist.name)
        )
    )
    return [
        LibraryTreeArtist(
            id=artist.id,
            name=artist.name,
            albums=[
                LibraryTreeAlbum(
                    id=album.id,
                    title=album.title,
                    path=album.path,
                    cover_path=album.cover_path,
                    tracks=[
                        LibraryTreeTrack(
                            id=track.id,
                            title=track.title,
                            track_number=track.track_number,
                            disc_number=track.disc_number,
                            duration_ms=track.duration_ms,
                            format=track.format,
                            bitrate=track.bitrate,
                            is_lossless=track.is_lossless,
                            path=track.path,
                            musicbrainz_recording_id=track.musicbrainz_recording_id,
                            explicit=track.explicit,
                            metadata_locked=track.metadata_locked,
                            artwork_locked=track.artwork_locked,
                            filename_locked=track.filename_locked,
                        )
                        for track in sorted(album.tracks, key=lambda track: (track.disc_number or 1, track.track_number or 9999))
                    ],
                    release_title=album.release_title,
                    musicbrainz_release_id=album.musicbrainz_release_id,
                    musicbrainz_release_group_id=album.musicbrainz_release_group_id,
                )
                for album in sorted(artist.albums, key=lambda album: album.title.lower())
            ],
            sort_name=artist.sort_name,
            musicbrainz_id=artist.musicbrainz_id,
        )
        for artist in artists
    ]


@router.post("/library/metadata", response_model=ProposalBatchOut, tags=["library"])
def propose_library_metadata(
    payload: LibraryMetadataProposalRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> ProposalBatchOut:
    changes = {key: value for key, value in payload.changes.items() if key in editable_fields(payload.target_type)}
    if not changes:
        raise HTTPException(status_code=400, detail="No editable metadata fields were supplied")

    target = metadata_target(session, payload.target_type, payload.target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Library item not found")

    old_values = {key: getattr(target, key, None) for key in changes}
    batch = ProposalBatch(
        title=f"Update {payload.target_type} metadata",
        kind=ProposalKind.metadata,
        tree_path="/library",
    )
    session.add(batch)
    session.flush()
    session.add(
        ProposalItem(
            batch_id=batch.id,
            title=metadata_target_title(payload.target_type, target),
            kind=ProposalKind.metadata,
            old_value=json.dumps(old_values),
            new_value=json.dumps(changes),
            payload_json=json.dumps(
                {
                    "target_type": payload.target_type,
                    "target_id": payload.target_id,
                    "changes": changes,
                }
            ),
        )
    )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/library/remove", response_model=ProposalBatchOut, tags=["library"])
def propose_library_remove(
    payload: LibraryRemoveProposalRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_write)),
) -> ProposalBatchOut:
    target = metadata_target(session, payload.target_type, payload.target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Library item not found")

    tracks = library_target_tracks(target)
    tracks = [track for track in tracks if track.path]
    if not tracks:
        raise HTTPException(status_code=400, detail="No files were found for this library item")

    batch_kind = ProposalKind.delete if payload.action == "delete" else ProposalKind.file_move
    batch = ProposalBatch(
        title=f"{remove_action_title(payload.action)} {payload.target_type}",
        kind=batch_kind,
        tree_path="/library",
    )
    session.add(batch)
    session.flush()
    settings = get_settings()
    destination_root = settings.trash_path if payload.action == "delete" else settings.import_path
    for track in tracks:
        old_path = Path(track.path)
        new_path = destination_root / old_path.name
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=track.title,
                kind=batch_kind,
                old_value=str(old_path),
                new_value=str(new_path),
                payload_json=json.dumps({"action": payload.action, "track_id": track.id}),
            )
        )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/library/albums/{album_id}/acoustic-match", tags=["library"])
def acoustic_match_library_album(
    album_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    api_key = integration_value(session, "acoustid_api_key")
    results = []
    for track in sorted(album.tracks, key=lambda track: (track.disc_number or 1, track.track_number or 9999, track.title.lower())):
        result = {
            "track_id": track.id,
            "title": track.title,
            "track_number": track.track_number,
            "status": "unmatched",
            "score": None,
            "candidate": None,
            "error": None,
        }
        if not track.path or not Path(track.path).exists():
            result["status"] = "missing_file"
            result["error"] = "Track file is missing"
            results.append(result)
            continue
        try:
            candidates = lookup_recording_by_fingerprint({"path": track.path}, api_key)
        except (ValueError, httpx.HTTPError) as error:
            result["status"] = "error"
            result["error"] = str(error)
            results.append(result)
            continue
        candidate = candidates[0] if candidates else None
        if not candidate:
            results.append(result)
            continue
        metadata = candidate.get("metadata") or {}
        candidate_title = metadata.get("title") or ""
        same_title = normalized_music_name(track.title) == normalized_music_name(candidate_title)
        same_recording = track.musicbrainz_recording_id and track.musicbrainz_recording_id == metadata.get("musicbrainz_recording_id")
        result.update(
            {
                "status": "matched" if same_recording or same_title else "changed",
                "score": round((candidate.get("score") or 0) * 100),
                "candidate": metadata,
            }
        )
        results.append(result)

    return {"album_id": album.id, "album": album.title, "tracks": results}


@router.get("/library/tracks/{track_id}/stream", tags=["library"])
def stream_track(
    track_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    user = session.scalar(select(User).where(User.api_key_hash == hash_secret(api_key)))
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.library_read not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    track = session.get(Track, track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    path = Path(track.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Track file is missing")
    return FileResponse(path)


@router.post("/imports/scan", tags=["imports"])
def scan_imports(
    payload: ImportScanRequest,
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        files = discover_import_files(payload.path, include_fingerprint=True)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"files": files, "count": len(files)}


@router.post("/imports/propose", response_model=TaskOut, tags=["imports"])
def propose_import(
    payload: ImportScanRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.import_run)),
) -> TaskOut:
    task = enqueue_task(
        session,
        "propose_import",
        {
            "path": payload.path,
            "files": payload.files,
            "download_requests": payload.download_requests or [],
        },
    )
    return serialize_task(task)


@router.post("/imports/acoustic-match", tags=["imports"])
def acoustic_match(
    payload: ImportAcousticLookupRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        candidates = lookup_recording_by_fingerprint(payload.file, integration_value(session, "acoustid_api_key"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=lookup_error_detail("AcoustID", error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="AcoustID could not be reached from the server") from error
    return {"candidates": candidates}


@router.post("/imports/album-lookup", tags=["imports"])
def album_lookup(
    payload: AlbumLookupRequest,
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        return lookup_album_tracks(payload.artist, payload.album, payload.release_id)
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=lookup_error_detail("MusicBrainz", error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="MusicBrainz could not be reached from the server") from error


@router.post("/imports/album-search", tags=["imports"])
def album_search(
    payload: AlbumLookupRequest,
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        return {"results": search_album_releases(payload.artist, payload.album)}
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=lookup_error_detail("MusicBrainz", error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="MusicBrainz could not be reached from the server") from error


@router.get("/wishlist", response_model=list[WishlistOut], tags=["wishlist"])
def list_wishlist(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> list[WishlistOut]:
    query = select(WishlistItem).options(selectinload(WishlistItem.user))
    if not user_has_permission(user, Permission.wishlist_manage_all):
        query = query.where(WishlistItem.user_id == user.id)
    items = list(session.scalars(query.order_by(WishlistItem.created_at.desc())))
    expire_old_terminal_wishlist_items(session, items)
    items = [item for item in items if item.status != "removed" and not terminal_wishlist_expired(item)]
    return [serialize_wishlist_item(item) for item in items]


@router.post("/wishlist", response_model=WishlistOut, tags=["wishlist"])
def create_wishlist_item(
    payload: WishlistCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> WishlistOut:
    item = WishlistItem(user_id=user.id, **payload.model_dump())
    item.status_changed_at = datetime.now(timezone.utc)
    session.add(item)
    session.commit()
    session.refresh(item)
    return serialize_wishlist_item(item)


@router.delete("/wishlist/{item_id}", response_model=WishlistOut, tags=["wishlist"])
def remove_wishlist_item(
    item_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> WishlistOut:
    item = session.get(WishlistItem, item_id)
    if not item or (not user_has_permission(user, Permission.wishlist_manage_all) and item.user_id != user.id):
        raise HTTPException(status_code=404, detail="Wishlist item not found")
    item.status = "removed"
    item.status_changed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(item)
    return serialize_wishlist_item(item)


@router.get("/wishlist/approvals", response_model=list[ProposalBatchOut], tags=["wishlist"])
def list_wishlist_approvals(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> list[ProposalBatchOut]:
    query = (
        select(ProposalBatch)
        .options(selectinload(ProposalBatch.items))
        .where(ProposalBatch.kind == ProposalKind.download)
        .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
        .order_by(ProposalBatch.created_at.desc())
    )
    batches = list(session.scalars(query))
    if user_has_permission(user, Permission.wishlist_manage_all):
        return [serialize_batch(batch) for batch in batches]
    visible_batches = []
    for batch in batches:
        if any((json.loads(item.payload_json or "{}").get("user_id") == user.id) for item in batch.items):
            visible_batches.append(batch)
    return [serialize_batch(batch) for batch in visible_batches]


@router.post("/wishlist/approvals", response_model=ProposalBatchOut, tags=["wishlist"])
def propose_wishlist_items(
    payload: WishlistApprovalRequest | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> ProposalBatchOut:
    query = select(WishlistItem).options(selectinload(WishlistItem.user)).where(WishlistItem.status == "wanted")
    if not user_has_permission(user, Permission.wishlist_manage_all):
        query = query.where(WishlistItem.user_id == user.id)
    all_wanted_items = list(session.scalars(query.order_by(WishlistItem.artist.asc(), WishlistItem.album.asc(), WishlistItem.track.asc())))
    denied_items: list[WishlistItem] = []
    if payload and payload.item_ids:
        selected_ids = set(payload.item_ids)
        items = [item for item in all_wanted_items if item.id in selected_ids]
        if payload.deny_unselected and user_has_permission(user, Permission.wishlist_manage_all):
            denied_items = [item for item in all_wanted_items if item.id not in selected_ids]
    else:
        items = all_wanted_items
    if not items:
        raise HTTPException(status_code=400, detail="No wishlist items are ready")

    batch = ProposalBatch(title="Wishlist download review", kind=ProposalKind.download, tree_path="/wishlist")
    session.add(batch)
    session.flush()
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    for wishlist_item in items:
        artist_name = wishlist_item.artist
        album_name = wishlist_item.album or "Singles"
        if artist_name not in artist_items:
            artist_item = ProposalItem(
                batch_id=batch.id,
                title=artist_name,
                kind=ProposalKind.download,
                payload_json=json.dumps({"user_id": wishlist_item.user_id, "kind": "artist", "artist": artist_name}),
            )
            session.add(artist_item)
            session.flush()
            artist_items[artist_name] = artist_item
        album_key = (artist_name, album_name)
        if album_key not in album_items:
            album_item = ProposalItem(
                batch_id=batch.id,
                parent_id=artist_items[artist_name].id,
                title=album_name,
                kind=ProposalKind.download,
                payload_json=json.dumps({"user_id": wishlist_item.user_id, "kind": "album", "artist": artist_name, "album": album_name}),
            )
            session.add(album_item)
            session.flush()
            album_items[album_key] = album_item
        session.add(
            ProposalItem(
                batch_id=batch.id,
                parent_id=album_items[album_key].id,
                title=wishlist_item.track or wishlist_item.album or wishlist_item.artist,
                kind=ProposalKind.download,
                payload_json=json.dumps(
                    {
                        "user_id": wishlist_item.user_id,
                        "action": "wishlist_request",
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
    notify_wishlist_decisions(session, items, "Wishlist request approved", "added to the task queue", "wishlist_approved", "/downloads")
    for denied_item in denied_items:
        denied_item.status = "rejected"
        denied_item.status_changed_at = datetime.now(timezone.utc)
    if denied_items:
        notify_wishlist_decisions(session, denied_items, "Wishlist request denied", "not selected for download", "wishlist_denied", "/wishlist")
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.get("/playlists/favorites", response_model=FavoritesOut, tags=["playlists"])
def favorites_playlist(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    playlist = get_or_create_favorites(session)
    return serialize_favorites(session, playlist)


@router.get("/playlists", response_model=list[FavoritesOut], tags=["playlists"])
def list_playlists(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> list[FavoritesOut]:
    playlists = list(session.scalars(select(Playlist).order_by(Playlist.name.asc())))
    if not any(playlist.name == "Favorites" for playlist in playlists):
        playlists.insert(0, get_or_create_favorites(session))
    return [serialize_favorites(session, playlist) for playlist in playlists]


@router.post("/playlists", response_model=FavoritesOut, tags=["playlists"])
def create_playlist(
    payload: PlaylistCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    existing = session.scalar(select(Playlist).where(Playlist.name == name))
    if existing:
        return serialize_favorites(session, existing)
    playlist = Playlist(name=name)
    session.add(playlist)
    session.commit()
    session.refresh(playlist)
    return serialize_favorites(session, playlist)


@router.patch("/playlists/{playlist_id}", response_model=ProposalBatchOut, tags=["playlists"])
def propose_playlist_rename(
    playlist_id: str,
    payload: PlaylistUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> ProposalBatchOut:
    playlist = session.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if playlist.protected:
        raise HTTPException(status_code=400, detail="Favorites cannot be renamed")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    if name == playlist.name:
        raise HTTPException(status_code=400, detail="Playlist name is already set")
    existing = session.scalar(select(Playlist).where(Playlist.name == name, Playlist.id != playlist.id))
    if existing:
        raise HTTPException(status_code=400, detail="A playlist with that name already exists")
    batch = ProposalBatch(title=f"Rename playlist {playlist.name}", kind=ProposalKind.playlist, tree_path=f"/playlists/{playlist.name}")
    session.add(batch)
    session.flush()
    session.add(
        ProposalItem(
            batch_id=batch.id,
            title=playlist.name,
            kind=ProposalKind.playlist,
            old_value=playlist.name,
            new_value=name,
            payload_json=json.dumps({"action": "rename_playlist", "playlist_id": playlist.id, "name": name}),
        )
    )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.delete("/playlists/{playlist_id}", response_model=ProposalBatchOut, tags=["playlists"])
def propose_playlist_delete(
    playlist_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> ProposalBatchOut:
    playlist = session.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if playlist.protected:
        raise HTTPException(status_code=400, detail="Favorites cannot be deleted")
    batch = ProposalBatch(title=f"Delete playlist {playlist.name}", kind=ProposalKind.playlist, tree_path=f"/playlists/{playlist.name}")
    session.add(batch)
    session.flush()
    session.add(
        ProposalItem(
            batch_id=batch.id,
            title=playlist.name,
            kind=ProposalKind.playlist,
            old_value=playlist.name,
            payload_json=json.dumps({"action": "delete_playlist", "playlist_id": playlist.id}),
        )
    )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/playlists/{playlist_id}/tracks", response_model=FavoritesOut, tags=["playlists"])
def add_playlist_tracks(
    playlist_id: str,
    payload: PlaylistAddTracks,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    playlist = session.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    existing_track_ids = {entry.track_id for entry in playlist.tracks}
    tracks = list(session.scalars(select(Track).where(Track.id.in_(payload.track_ids))))
    next_position = max([entry.position for entry in playlist.tracks] or [0]) + 1
    for track in tracks:
        if track.id in existing_track_ids:
            continue
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=next_position))
        next_position += 1
    session.commit()
    session.refresh(playlist)
    enqueue_task(session, "sync_favorites_jellyfin", {})
    return serialize_favorites(session, playlist)


@router.delete("/playlists/{playlist_id}/tracks/{track_id}", response_model=FavoritesOut, tags=["playlists"])
def remove_playlist_track(
    playlist_id: str,
    track_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    playlist = session.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    items = list(
        session.scalars(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id, PlaylistTrack.track_id == track_id)
        )
    )
    for item in items:
        session.delete(item)
    session.commit()
    session.refresh(playlist)
    enqueue_task(session, "sync_favorites_jellyfin", {})
    return serialize_favorites(session, playlist)


@router.post("/playlists/favorites/tracks/{track_id}", response_model=FavoritesOut, tags=["playlists"])
def add_favorite_track(
    track_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    track = session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    playlist = get_or_create_favorites(session)
    if not any(entry.track_id == track_id for entry in playlist.tracks):
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track_id, position=len(playlist.tracks) + 1))
        session.commit()
        session.refresh(playlist)
    enqueue_task(session, "sync_favorites_jellyfin", {})
    return serialize_favorites(session, playlist)


@router.delete("/playlists/favorites/tracks/{track_id}", response_model=FavoritesOut, tags=["playlists"])
def remove_favorite_track(
    track_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    playlist = get_or_create_favorites(session)
    items = list(
        session.scalars(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id, PlaylistTrack.track_id == track_id)
        )
    )
    for item in items:
        session.delete(item)
    session.commit()
    enqueue_task(session, "sync_favorites_jellyfin", {})
    return serialize_favorites(session, playlist)


@router.post("/playlists/sync", response_model=TaskOut, tags=["playlists"])
def sync_playlists(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "sync_favorites_jellyfin", {}))


@router.post("/playlists/favorites/entries/{entry_id}/position", response_model=ProposalBatchOut, tags=["playlists"])
def propose_favorite_position(
    entry_id: str,
    payload: PlaylistPositionProposalRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> ProposalBatchOut:
    playlist = get_or_create_favorites(session)
    entry = session.scalar(
        select(PlaylistTrack)
        .where(PlaylistTrack.id == entry_id, PlaylistTrack.playlist_id == playlist.id)
        .options(selectinload(PlaylistTrack.track))
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Playlist entry not found")
    if entry.position == payload.position:
        raise HTTPException(status_code=400, detail="Playlist order is already set to that value")

    batch = ProposalBatch(
        title="Update Favorites order",
        kind=ProposalKind.playlist,
        tree_path="/playlists/Favorites",
    )
    session.add(batch)
    session.flush()
    session.add(
        ProposalItem(
            batch_id=batch.id,
            title=entry.track.title,
            kind=ProposalKind.playlist,
            old_value=str(entry.position),
            new_value=str(payload.position),
            payload_json=json.dumps(
                {
                    "action": "set_position",
                    "playlist_track_id": entry.id,
                    "position": payload.position,
                }
            ),
        )
    )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/playlists/entries/{entry_id}/position", response_model=ProposalBatchOut, tags=["playlists"])
def propose_playlist_position(
    entry_id: str,
    payload: PlaylistPositionProposalRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> ProposalBatchOut:
    entry = session.scalar(
        select(PlaylistTrack)
        .where(PlaylistTrack.id == entry_id)
        .options(selectinload(PlaylistTrack.track), selectinload(PlaylistTrack.playlist))
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Playlist entry not found")
    if entry.position == payload.position:
        raise HTTPException(status_code=400, detail="Playlist order is already set to that value")

    batch = ProposalBatch(
        title=f"Update {entry.playlist.name} order",
        kind=ProposalKind.playlist,
        tree_path=f"/playlists/{entry.playlist.name}",
    )
    session.add(batch)
    session.flush()
    session.add(
        ProposalItem(
            batch_id=batch.id,
            title=entry.track.title,
            kind=ProposalKind.playlist,
            old_value=str(entry.position),
            new_value=str(payload.position),
            payload_json=json.dumps({"action": "set_position", "playlist_track_id": entry.id, "position": payload.position}),
        )
    )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/wishlist/process", response_model=TaskOut, tags=["wishlist"])
def process_wishlist(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.downloads_manage)),
) -> TaskOut:
    task = enqueue_task(session, "process_wishlist", {})
    return serialize_task(task)


@router.post("/tools/jellyfin-scan", response_model=TaskOut, tags=["tools"])
def tool_jellyfin_scan(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.jellyfin_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "jellyfin_scan", {}))


@router.post("/tools/check-files", response_model=TaskOut, tags=["tools"])
def tool_check_files(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_files", {}))


@router.post("/tools/check-lyrics", response_model=TaskOut, tags=["tools"])
def tool_check_lyrics(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_lyrics", {}))


@router.post("/tools/check-album-covers", response_model=TaskOut, tags=["tools"])
def tool_check_album_covers(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_album_covers", {}))


@router.post("/tools/check-files/fix", response_model=ProposalBatchOut, tags=["tools"])
def propose_check_file_fix(
    payload: CheckFileFixRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> ProposalBatchOut:
    if payload.action in {"remove_record", "download_record"}:
        if not payload.track_id:
            raise HTTPException(status_code=400, detail="track_id is required")
        track = session.scalar(
            select(Track)
            .where(Track.id == payload.track_id)
            .options(selectinload(Track.album).selectinload(Album.artist))
        )
        if not track:
            raise HTTPException(status_code=404, detail="Track record not found")
        if payload.action == "download_record":
            batch = ProposalBatch(title=f"Download missing file for {track.title}", kind=ProposalKind.download, tree_path="/library")
            session.add(batch)
            session.flush()
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    title=track.title,
                    kind=ProposalKind.download,
                    payload_json=json.dumps(
                        {
                            "action": "wishlist_request",
                            "kind": "track",
                            "artist": track.album.artist.name,
                            "album": track.album.title,
                            "track": track.title,
                        }
                    ),
                )
            )
        else:
            batch = ProposalBatch(title=f"Remove missing record for {track.title}", kind=ProposalKind.delete, tree_path="/library")
            session.add(batch)
            session.flush()
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    title=track.title,
                    kind=ProposalKind.delete,
                    old_value=track.path,
                    payload_json=json.dumps({"action": "remove_record", "track_id": track.id}),
                )
            )
    else:
        if not payload.path:
            raise HTTPException(status_code=400, detail="path is required")
        settings = get_settings()
        file_path = Path(payload.path).resolve()
        library_root = settings.library_path.resolve()
        if library_root not in [file_path, *file_path.parents] or not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=400, detail="File must be inside the library folder")
        if payload.action == "delete_file":
            batch = ProposalBatch(title=f"Delete untracked file {file_path.name}", kind=ProposalKind.delete, tree_path="/library")
            session.add(batch)
            session.flush()
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    title=file_path.name,
                    kind=ProposalKind.delete,
                    old_value=str(file_path),
                    payload_json=json.dumps({"action": "delete_file", "path": str(file_path)}),
                )
            )
            session.commit()
            session.refresh(batch)
            return serialize_batch(batch)
        metadata = read_audio_metadata(file_path)
        batch = ProposalBatch(title=f"Create record for {file_path.name}", kind=ProposalKind.import_files, tree_path="/library")
        session.add(batch)
        session.flush()
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=metadata.get("title") or file_path.stem,
                kind=ProposalKind.import_files,
                old_value=str(file_path),
                new_value=str(file_path),
                payload_json=json.dumps(
                    {
                        "action": "create_library_record",
                        "path": str(file_path),
                        "metadata": metadata,
                        "size_bytes": file_path.stat().st_size,
                    }
                ),
            )
        )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/tools/check-missing-tracks", response_model=TaskOut, tags=["tools"])
def tool_check_missing_tracks(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.downloads_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_missing_tracks", {}))


@router.post("/tools/backup", response_model=TaskOut, tags=["tools"])
def tool_backup(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "backup_now", {}))


@router.get("/tools/backups", tags=["tools"])
def list_backups(
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> dict:
    settings = get_settings()
    settings.backups_path.mkdir(parents=True, exist_ok=True)
    backups = sorted(settings.backups_path.glob("nudibranch-*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)
    return {"backups": [{"path": str(path), "name": path.name, "size_bytes": path.stat().st_size} for path in backups]}


@router.post("/tools/restore-default", response_model=TaskOut, tags=["tools"])
def tool_restore_default(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "restore_default", {}))


@router.post("/tools/restore-backup", response_model=TaskOut, tags=["tools"])
def tool_restore_backup(
    payload: BackupRestoreRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> TaskOut:
    settings = get_settings()
    backup_path = Path(payload.backup_path).resolve()
    backup_root = settings.backups_path.resolve()
    if backup_root not in [backup_path, *backup_path.parents] or not backup_path.exists():
        raise HTTPException(status_code=400, detail="Backup must be inside the backups folder")
    return serialize_task(enqueue_task(session, "restore_backup", {"backup_path": str(backup_path)}))


@router.post("/settings/youtube-cookies", response_model=IntegrationSettings, tags=["settings"])
async def upload_youtube_cookies(
    browser: str = Query(""),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> IntegrationSettings:
    settings = get_settings()
    settings.config_path.mkdir(parents=True, exist_ok=True)
    destination = settings.config_path / "youtube-cookies.txt"
    content = await file.read()
    destination.write_bytes(content)
    values = integration_settings(session)
    values["youtube_cookies_browser"] = browser.strip()
    values["youtube_cookies_path"] = str(destination)
    return IntegrationSettings(**update_integration_settings(session, values))


@router.get("/approvals", response_model=list[ProposalBatchOut], tags=["approvals"])
def list_approvals(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.approvals_manage)),
) -> list[ProposalBatchOut]:
    batches = list(
        session.scalars(
            select(ProposalBatch)
            .options(selectinload(ProposalBatch.items))
            .where(
                ProposalBatch.status.in_(
                    [ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]
                )
            )
            .order_by(ProposalBatch.created_at.desc())
        )
    )
    return [serialize_batch(batch) for batch in batches]


@router.post("/approvals/{batch_id}/selection", tags=["approvals"])
def update_selection(
    batch_id: str,
    payload: ProposalSelectionUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.approvals_manage)),
) -> dict:
    count = set_selection(session, batch_id, payload.item_ids, payload.selected)
    return {"batch_id": batch_id, "updated": count}


@router.post("/approvals/{batch_id}/approve", response_model=TaskOut, tags=["approvals"])
def approve(
    batch_id: str,
    payload: ProposalApproveRequest | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.approvals_manage)),
) -> TaskOut:
    try:
        task = approve_batch(session, batch_id, payload.item_ids if payload else None)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return serialize_task(task)


@router.post("/approvals/{batch_id}/reject", tags=["approvals"])
def reject(
    batch_id: str,
    payload: ProposalRejectRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.approvals_manage)),
) -> dict:
    count = reject_items(session, batch_id, payload.item_ids, payload.suppress_for)
    return {"batch_id": batch_id, "removed": count}


@router.get("/tasks", response_model=list[TaskOut], tags=["tasks"])
def list_tasks(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.activity_read)),
) -> list[TaskOut]:
    tasks = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
    return [serialize_task(task) for task in tasks]


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut, tags=["tasks"])
def cancel_existing_task(
    task_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.activity_read)),
) -> TaskOut:
    try:
        task = cancel_task(session, task_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_task(task)


@router.post("/tasks", response_model=TaskOut, tags=["tasks"])
def create_task(
    payload: TaskCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, payload.type, payload.payload))


@router.get("/settings/integrations", response_model=IntegrationSettings, tags=["settings"])
def get_integrations(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> IntegrationSettings:
    return IntegrationSettings(**integration_settings(session))


@router.put("/settings/integrations", response_model=IntegrationSettings, tags=["settings"])
def update_integrations(
    payload: IntegrationSettings,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> IntegrationSettings:
    return IntegrationSettings(**update_integration_settings(session, payload.model_dump()))


@router.get("/notifications", response_model=list[NotificationOut], tags=["notifications"])
def list_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> list[NotificationOut]:
    query = select(Notification).where(
        ((Notification.user_id == user.id) | (Notification.user_id.is_(None)))
        & (Notification.status != NotificationStatus.dismissed)
    )
    notifications = list(session.scalars(query.order_by(Notification.created_at.desc()).limit(100)))
    return [NotificationOut.model_validate(notification, from_attributes=True) for notification in notifications]


@router.post("/notifications/devices", tags=["notifications"])
def register_device(
    payload: DeviceRegistration,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    device = MobileDevice(user_id=user.id, device_name=payload.device_name, apns_token=payload.apns_token)
    session.add(device)
    session.commit()
    return {"device_id": device.id, "enabled": device.enabled}


@router.post("/notifications/read", tags=["notifications"])
def mark_notifications_read(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> dict:
    notifications = list(session.scalars(select(Notification).where((Notification.user_id == user.id) | (Notification.user_id.is_(None)))))
    for notification in notifications:
        if notification.status == NotificationStatus.unread:
            notification.status = NotificationStatus.read
    session.commit()
    return {"updated": len(notifications)}


@router.delete("/notifications", tags=["notifications"])
def clear_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> dict:
    notifications = list(session.scalars(select(Notification).where((Notification.user_id == user.id) | (Notification.user_id.is_(None)))))
    for notification in notifications:
        notification.status = NotificationStatus.dismissed
    session.commit()
    return {"cleared": len(notifications)}


def editable_fields(target_type: str) -> set[str]:
    if target_type == "artist":
        return {"name", "sort_name", "musicbrainz_id"}
    if target_type == "album":
        return {"title", "release_title", "path", "cover_path", "musicbrainz_release_id", "musicbrainz_release_group_id"}
    if target_type == "track":
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
    return set()


def metadata_target(session: Session, target_type: str, target_id: str):
    if target_type == "artist":
        return session.get(Artist, target_id)
    if target_type == "album":
        return session.get(Album, target_id)
    if target_type == "track":
        return session.get(Track, target_id)
    return None


def metadata_target_title(target_type: str, target) -> str:
    if target_type == "artist":
        return f"Artist: {target.name}"
    if target_type == "album":
        return f"Album: {target.title}"
    return f"Track: {target.title}"


def normalized_music_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def library_target_tracks(target) -> list[Track]:
    if isinstance(target, Artist):
        return [track for album in target.albums for track in album.tracks]
    if isinstance(target, Album):
        return list(target.tracks)
    if isinstance(target, Track):
        return [target]
    return []


def remove_action_title(action: str) -> str:
    return "Delete" if action == "delete" else "Move to import"


def get_or_create_favorites(session: Session) -> Playlist:
    playlist = session.scalar(select(Playlist).where(Playlist.name == "Favorites"))
    if not playlist:
        playlist = Playlist(name="Favorites", protected=True)
        session.add(playlist)
        session.commit()
        session.refresh(playlist)
    elif not playlist.protected:
        playlist.protected = True
        session.commit()
        session.refresh(playlist)
    return playlist


def serialize_favorites(session: Session, playlist: Playlist) -> FavoritesOut:
    entries = list(
        session.scalars(
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .options(selectinload(PlaylistTrack.track).selectinload(Track.album).selectinload(Album.artist))
            .order_by(PlaylistTrack.position, PlaylistTrack.created_at)
        )
    )
    tracks = [
        PlaylistTrackOut(
            id=entry.id,
            track_id=entry.track_id,
            position=entry.position,
            title=entry.track.title,
            artist=entry.track.album.artist.name,
            album=entry.track.album.title,
            format=entry.track.format,
        )
        for entry in entries
    ]
    track_ids = [entry.track_id for entry in entries]
    return FavoritesOut(
        id=playlist.id,
        name=playlist.name,
        protected=playlist.protected,
        track_ids=track_ids,
        tracks=tracks,
        track_count=len(track_ids),
    )


def lookup_error_detail(service: str, error: httpx.HTTPStatusError) -> str:
    response = error.response
    detail = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("error") or payload.get("message")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
    except ValueError:
        detail = response.text[:160] if response.text else None
    if response.status_code in {401, 403}:
        return f"{service} rejected the configured API key"
    if detail:
        return f"{service} lookup failed: {detail}"
    return f"{service} lookup failed with HTTP {response.status_code}"


def serialize_user(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        display_name=user.display_name,
        is_admin=user.is_admin,
        permissions=effective_permission_values(user),
    )


def serialize_wishlist_item(item: WishlistItem) -> WishlistOut:
    return WishlistOut(
        id=item.id,
        user_id=item.user_id,
        owner_name=item.user.display_name if item.user else None,
        kind=item.kind,
        artist=item.artist,
        album=item.album,
        track=item.track,
        status=item.status,
        created_at=item.created_at,
        status_changed_at=item.status_changed_at or item.created_at,
    )


def terminal_wishlist_expired(item: WishlistItem) -> bool:
    if item.status not in {"rejected", "completed", "removed"}:
        return False
    changed_at = item.status_changed_at or item.created_at
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    return changed_at < datetime.now(timezone.utc) - timedelta(hours=48)


def expire_old_terminal_wishlist_items(session: Session, items: list[WishlistItem]) -> None:
    expired = [item for item in items if terminal_wishlist_expired(item)]
    for item in expired:
        session.delete(item)
    if expired:
        session.commit()


def notify_wishlist_decisions(
    session: Session,
    items: list[WishlistItem],
    title: str,
    action_text: str,
    event_type: str,
    target_url: str,
) -> None:
    items_by_user: dict[str, list[WishlistItem]] = {}
    for item in items:
        items_by_user.setdefault(item.user_id, []).append(item)
    for user_id, user_items in items_by_user.items():
        names = [item.track or item.album or item.artist for item in user_items]
        shown = ", ".join(names[:5])
        extra = "" if len(names) <= 5 else f" and {len(names) - 5} more"
        create_notification(
            session,
            title=title,
            body=f"{shown}{extra} {action_text}.",
            event_type=event_type,
            target_url=target_url,
            user_id=user_id,
        )


def load_user(session: Session, user_id: str) -> User:
    user = session.scalar(select(User).options(selectinload(User.permissions)).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def effective_permission_values(user: User) -> list[str]:
    if user.is_admin:
        return [permission.value for permission in Permission]
    return sorted({permission.permission.value for permission in user.permissions})


def user_has_permission(user: User, permission: Permission) -> bool:
    return user.is_admin or any(user_permission.permission == permission for user_permission in user.permissions)


def permission_label(permission: Permission) -> str:
    return permission.value.replace(":", " ").replace("_", " ").title()


def parse_permissions(permission_values: list[str]) -> list[Permission]:
    permissions: list[Permission] = []
    for value in permission_values:
        try:
            permissions.append(Permission(value))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {value}") from error
    return sorted(set(permissions), key=lambda permission: permission.value)


def set_user_permissions(session: Session, user: User, permission_values: list[str]) -> None:
    for existing in list(user.permissions):
        session.delete(existing)
    session.flush()
    for permission in parse_permissions(permission_values):
        session.add(UserPermission(user_id=user.id, permission=permission))


def count_admins(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) or 0


def serialize_batch(batch: ProposalBatch) -> ProposalBatchOut:
    return ProposalBatchOut(
        id=batch.id,
        title=batch.title,
        kind=batch.kind,
        status=batch.status,
        tree_path=batch.tree_path,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        items=[
            ProposalItemOut(
                id=item.id,
                batch_id=item.batch_id,
                parent_id=item.parent_id,
                title=item.title,
                kind=item.kind,
                status=item.status,
                selected=item.selected,
                old_value=item.old_value,
                new_value=item.new_value,
                suppress_until=item.suppress_until,
            )
            for item in batch.items
        ],
    )


def serialize_task(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        type=task.type,
        status=task.status,
        payload=task_to_payload(task),
        result=task_result(task),
        error=task.error,
        attempts=task.attempts,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
