import secrets

from fastapi import APIRouter, Depends, HTTPException
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nudibranch.api.deps import get_current_user, require_permission
from nudibranch.api.schemas import (
    AlbumLookupRequest,
    DeviceRegistration,
    ImportAcousticLookupRequest,
    ImportScanRequest,
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
    Permission,
    ProposalBatch,
    ProposalStatus,
    Task,
    User,
    WishlistItem,
)
from nudibranch.db.session import get_session
from nudibranch.services.imports import discover_import_files
from nudibranch.services.metadata_lookup import lookup_album_tracks, lookup_recording_by_fingerprint
from nudibranch.services.proposals import approve_batch, reject_items, set_selection
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
                            format=track.format,
                            is_lossless=track.is_lossless,
                            path=track.path,
                        )
                        for track in sorted(album.tracks, key=lambda track: (track.disc_number or 1, track.track_number or 9999))
                    ],
                )
                for album in sorted(artist.albums, key=lambda album: album.title.lower())
            ],
        )
        for artist in artists
    ]


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
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        candidates = lookup_recording_by_fingerprint(payload.file)
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
        return lookup_album_tracks(payload.artist, payload.album)
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Album lookup failed: {error}") from error


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


@router.get("/notifications", response_model=list[NotificationOut], tags=["notifications"])
def list_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> list[NotificationOut]:
    query = select(Notification).where((Notification.user_id == user.id) | (Notification.user_id.is_(None)))
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
