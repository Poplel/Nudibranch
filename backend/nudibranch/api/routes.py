import secrets
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nudibranch.api.deps import get_current_user, require_permission
from nudibranch.api.schemas import (
    AlbumLookupRequest,
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
    ProposalBatchOut,
    ProposalApproveRequest,
    ProposalItemOut,
    ProposalRejectRequest,
    ProposalSelectionUpdate,
    TaskCreate,
    TaskOut,
    UserOut,
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
    WishlistItem,
)
from nudibranch.core.config import get_settings
from nudibranch.db.session import get_session
from nudibranch.services.imports import discover_import_files
from nudibranch.services.metadata_lookup import lookup_album_tracks, lookup_recording_by_fingerprint, search_album_releases
from nudibranch.services.proposals import approve_batch, reject_items, set_selection
from nudibranch.services.settings_store import integration_settings, integration_value, update_integration_settings
from nudibranch.services.tasks import enqueue_task, task_result, task_to_payload

router = APIRouter(prefix="/api/v1")


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
    return UserOut(
        id=user.id,
        display_name=user.display_name,
        is_admin=user.is_admin,
        permissions=[permission.permission.value for permission in user.permissions],
    )


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
        files = discover_import_files(payload.path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"files": files, "count": len(files)}


@router.post("/imports/propose", response_model=TaskOut, tags=["imports"])
def propose_import(
    payload: ImportScanRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.import_run)),
) -> TaskOut:
    task = enqueue_task(session, "propose_import", {"path": payload.path, "files": payload.files})
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
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Metadata lookup failed: {error}") from error
    return {"candidates": candidates}


@router.post("/imports/album-lookup", tags=["imports"])
def album_lookup(
    payload: AlbumLookupRequest,
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        return lookup_album_tracks(payload.artist, payload.album, payload.release_id)
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Album lookup failed: {error}") from error


@router.post("/imports/album-search", tags=["imports"])
def album_search(
    payload: AlbumLookupRequest,
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        return {"results": search_album_releases(payload.artist, payload.album)}
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Album search failed: {error}") from error


@router.get("/wishlist", response_model=list[WishlistOut], tags=["wishlist"])
def list_wishlist(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> list[WishlistOut]:
    query = select(WishlistItem)
    if not user.is_admin:
        query = query.where(WishlistItem.user_id == user.id)
    items = list(session.scalars(query.order_by(WishlistItem.created_at.desc())))
    return [WishlistOut.model_validate(item, from_attributes=True) for item in items]


@router.post("/wishlist", response_model=WishlistOut, tags=["wishlist"])
def create_wishlist_item(
    payload: WishlistCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> WishlistOut:
    item = WishlistItem(user_id=user.id, **payload.model_dump())
    session.add(item)
    session.commit()
    session.refresh(item)
    return WishlistOut.model_validate(item, from_attributes=True)


@router.get("/playlists/favorites", response_model=FavoritesOut, tags=["playlists"])
def favorites_playlist(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    playlist = get_or_create_favorites(session)
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


@router.post("/wishlist/process", response_model=TaskOut, tags=["wishlist"])
def process_wishlist(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.downloads_manage)),
) -> TaskOut:
    task = enqueue_task(session, "process_wishlist", {})
    return serialize_task(task)


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
    return {"batch_id": batch_id, "rejected": count, "suppress_for": payload.suppress_for}


@router.get("/tasks", response_model=list[TaskOut], tags=["tasks"])
def list_tasks(
    session: Session = Depends(get_session),
    _: User = Depends(get_current_user),
) -> list[TaskOut]:
    tasks = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
    return [serialize_task(task) for task in tasks]


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
    track_ids = list(session.scalars(select(PlaylistTrack.track_id).where(PlaylistTrack.playlist_id == playlist.id).order_by(PlaylistTrack.position)))
    return FavoritesOut(id=playlist.id, name=playlist.name, protected=playlist.protected, track_ids=track_ids)


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
