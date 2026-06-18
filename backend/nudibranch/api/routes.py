import os
import secrets
import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from mutagen import File as MutagenFile

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
import httpx
from sqlalchemy import case, delete, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from nudibranch.api.deps import SESSION_TTL, get_current_user, require_permission, resolve_media_user
from nudibranch.api.schemas import (
    AlbumLookupRequest,
    AutomationCreate,
    AutomationOut,
    AutomationUpdate,
    AudioVerifyDetected,
    AudioVerifyResult,
    BackupRestoreRequest,
    BucketCount,
    CheckFileFixRequest,
    PinAlbumIn,
    PinArtistIn,
    PinPlaylistIn,
    PlayEventOut,
    PlayRecordIn,
    SessionRenameRequest,
    DeviceRegistration,
    DiscoverTaskQueueRequest,
    FavoritesOut,
    PushIdentityResponse,
    ImportMusicBrainzLookupRequest,
    IntegrationSettings,
    ImportScanRequest,
    JellyfinUserOut,
    LibraryArtistRow,
    LibraryAlbumRow,
    LibraryMetadataProposalRequest,
    LibraryRemoveProposalRequest,
    LibraryTrackRow,
    LibraryTreeAlbum,
    LibraryTreeArtist,
    LibraryTreeTrack,
    LogEntryOut,
    LoginRequest,
    LoginResponse,
    NotificationOut,
    PaginatedAlbums,
    PaginatedArtists,
    PaginatedTracks,
    PlaylistAddTracks,
    PlaylistCreate,
    PlaylistPositionProposalRequest,
    PlayerCommandCreate,
    PlayerCommandOut,
    PlayerStateUpdate,
    PlaylistTrackOut,
    PlaylistImportRequest,
    PlaylistImportResponse,
    PlaylistSyncStatsOut,
    PlaylistUpdate,
    PermissionOut,
    ProposalBatchOut,
    ProposalApproveRequest,
    ProposalItemOut,
    ProposalRejectRequest,
    ProposalSelectionUpdate,
    SearchResponse,
    SearchResultItem,
    SessionOut,
    StaticKeyCreate,
    StaticKeyOut,
    StaticKeyCreated,
    TaskCreate,
    TaskOut,
    UserCreate,
    UserAppearanceUpdate,
    JellyfinUserLinkUpdate,
    UserOut,
    UserPinUpdate,
    UserSearchSettingsUpdate,
    UserUpdate,
    WishlistApprovalRequest,
    WishlistCreate,
    WishlistOut,
)
from nudibranch.db.models import (
    Album,
    AppSetting,
    Artist,
    Automation,
    AuthSession,
    MobileDevice,
    Notification,
    PlaybackCommand,
    NotificationStatus,
    Permission,
    PinnedItem,
    PinnedPlaylist,
    PlayEvent,
    Playlist,
    PlaylistTrack,
    PlayerState,
    ProposalBatch,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    StaticApiKey,
    Task,
    Track,
    User,
    UserPermission,
    WishlistItem,
)
from nudibranch.core.config import get_settings
from nudibranch.db.session import get_session
from nudibranch.services.auth import generate_token, hash_password, hash_token, token_prefix, verify_password
from nudibranch.services.imports import discover_import_files, read_audio_metadata, safe_path_part, SUPPORTED_AUDIO_EXTENSIONS
from nudibranch.services.app_log import tail_app_log, write_app_log
from nudibranch.services.itunes import album_tracks as itunes_album_tracks
from nudibranch.services.itunes import discover_music
from nudibranch.services.metadata_lookup import album_cover_candidate_urls, artist_image_candidate_urls, lookup_album_tracks, lookup_recording_by_musicbrainz_metadata, search_album_releases
from nudibranch.services.notifications import create_notification, push_identity
from nudibranch.services.proposals import approve_batch, reject_items, set_selection
from nudibranch.services.acoustid import audio_matches_claim
from nudibranch.services.settings_store import integration_settings, integration_value, update_integration_settings
from nudibranch.services.tasks import cancel_task, enqueue_task, task_result, task_to_payload
from nudibranch.services.search import rebuild_search_index, search_library
from nudibranch.services.automations import ACTION_TYPES, NOTIFY_MODES, NOTIFY_PRIORITIES, TRIGGER_TYPES, compute_next_run, run_automation

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


@router.post("/auth/login", response_model=LoginResponse, tags=["auth"], summary="Log in with username and password")
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    user = session.scalar(select(User).where(func.lower(User.username) == payload.username.strip().lower()))
    if not user or not verify_password(payload.password, user.pin_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = generate_token()
    now = datetime.now(timezone.utc)
    expires = now + SESSION_TTL
    label = payload.device_label or None
    # Prune expired sessions for this user.
    for old in session.scalars(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at < now)).all():
        session.delete(old)
    # Renew the existing session row for this device instead of spawning a new one
    # (re-keys the token + extends expiry; keeps created_at, label, and id stable).
    existing = None
    if label:
        same_label = list(session.scalars(
            select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.device_label == label).order_by(AuthSession.created_at.asc())
        ))
        if same_label:
            existing = same_label[0]
            # Collapse any pre-existing duplicate same-label rows so one device == one session.
            for dup in same_label[1:]:
                session.delete(dup)
    if existing:
        existing.token_hash = hash_token(token)
        existing.last_used_at = now
        existing.expires_at = expires
    else:
        session.add(AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            device_label=label,
            created_at=now,
            last_used_at=now,
            expires_at=expires,
        ))
    session.commit()
    return LoginResponse(
        user_id=user.id,
        display_name=user.display_name,
        username=user.username,
        api_key=token,
        is_admin=user.is_admin,
        expires_at=expires,
    )


@router.post("/auth/logout", tags=["auth"], summary="Log out the current session")
def logout(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        token_h = hash_token(authorization.split(" ", 1)[1].strip())
        existing = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_h, AuthSession.user_id == user.id))
        if existing:
            session.delete(existing)
            session.commit()
    return {"ok": True}


def _current_session_hash(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return hash_token(authorization.split(" ", 1)[1].strip())
    return None


@router.get("/me/sessions", response_model=list[SessionOut], tags=["auth"], summary="List my active sessions")
def list_sessions(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[SessionOut]:
    current_hash = _current_session_hash(authorization)
    rows = session.scalars(
        select(AuthSession).where(AuthSession.user_id == user.id).order_by(AuthSession.last_used_at.desc())
    )
    return [
        SessionOut(
            id=s.id,
            device_label=s.device_label,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
            current=(s.token_hash == current_hash),
        )
        for s in rows
    ]


@router.patch("/me/sessions/{session_id}", response_model=SessionOut, tags=["auth"], summary="Rename one of my sessions")
def rename_session(
    session_id: str,
    payload: SessionRenameRequest,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> SessionOut:
    existing = session.scalar(select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user.id))
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")
    label = (payload.device_label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Session name cannot be empty")
    existing.device_label = label[:255]
    session.commit()
    current_hash = _current_session_hash(authorization)
    return SessionOut(
        id=existing.id,
        device_label=existing.device_label,
        created_at=existing.created_at,
        last_used_at=existing.last_used_at,
        expires_at=existing.expires_at,
        current=(existing.token_hash == current_hash),
    )


@router.delete("/me/sessions/{session_id}", tags=["auth"], summary="Revoke one of my sessions")
def revoke_session(
    session_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    existing = session.scalar(select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user.id))
    if existing:
        session.delete(existing)
        session.commit()
    return {"ok": True}


@router.get("/me/api-keys", response_model=list[StaticKeyOut], tags=["auth"], summary="List my static API keys")
def list_api_keys(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[StaticKeyOut]:
    rows = session.scalars(
        select(StaticApiKey).where(StaticApiKey.user_id == user.id).order_by(StaticApiKey.created_at.desc())
    )
    return [
        StaticKeyOut(id=k.id, name=k.name, prefix=k.prefix, created_at=k.created_at, last_used_at=k.last_used_at, revoked=k.revoked)
        for k in rows
    ]


@router.post("/me/api-keys", response_model=StaticKeyCreated, tags=["auth"], summary="Create a static API key (shown once)")
def create_api_key(
    payload: StaticKeyCreate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> StaticKeyCreated:
    token = generate_token()
    key = StaticApiKey(user_id=user.id, name=payload.name.strip(), key_hash=hash_token(token), prefix=token_prefix(token))
    session.add(key)
    session.commit()
    return StaticKeyCreated(
        id=key.id, name=key.name, prefix=key.prefix, created_at=key.created_at, last_used_at=None, revoked=False, api_key=token
    )


@router.delete("/me/api-keys/{key_id}", tags=["auth"], summary="Revoke a static API key")
def revoke_api_key(
    key_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    key = session.scalar(select(StaticApiKey).where(StaticApiKey.id == key_id, StaticApiKey.user_id == user.id))
    if key:
        key.revoked = True
        session.commit()
    return {"ok": True}


@router.get("/me", response_model=UserOut, tags=["users"], summary="Get current user")
def me(user: User = Depends(get_current_user)) -> UserOut:
    return serialize_user(user)


@router.get("/permissions", response_model=list[PermissionOut], tags=["users"], summary="List available permissions")
def permission_catalog(_: User = Depends(get_current_user)) -> list[PermissionOut]:
    return [
        PermissionOut(value=permission.value, label=permission_label(permission), section=PERMISSION_SECTIONS.get(permission, "System"))
        for permission in Permission
    ]


@router.get("/users", response_model=list[UserOut], tags=["users"], summary="List users")
def list_users(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> list[UserOut]:
    users = list(session.scalars(select(User).options(selectinload(User.permissions)).order_by(User.created_at.asc())))
    return [serialize_user(user) for user in users]


@router.post("/users", response_model=UserOut, tags=["users"], summary="Create user")
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    username = payload.username.strip().lower()
    if session.scalar(select(User).where(func.lower(User.username) == username)):
        raise HTTPException(status_code=409, detail="Username already taken")
    user = User(
        display_name=payload.display_name.strip(),
        username=username,
        pin_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    session.add(user)
    session.flush()
    set_user_permissions(session, user, payload.permissions)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.patch("/users/{user_id}", response_model=UserOut, tags=["users"], summary="Update user")
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
    if payload.username is not None:
        new_username = payload.username.strip().lower()
        if session.scalar(select(User).where(func.lower(User.username) == new_username, User.id != user_id)):
            raise HTTPException(status_code=409, detail="Username already taken")
        user.username = new_username
    if payload.is_admin is not None:
        if user.is_admin and not payload.is_admin and count_admins(session) <= 1:
            raise HTTPException(status_code=400, detail="At least one admin user is required")
        user.is_admin = payload.is_admin
    if payload.permissions is not None:
        set_user_permissions(session, user, payload.permissions)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.post("/users/{user_id}/pin", response_model=UserOut, tags=["users"], summary="Update user PIN")
def update_user_pin(
    user_id: str,
    payload: UserPinUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.pin_hash = hash_password(payload.password)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.delete("/users/{user_id}", status_code=204, tags=["users"], summary="Delete a user and all their data")
def delete_user(
    user_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_permission(Permission.users_manage)),
) -> Response:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    # Delete the user's playlists (and their tracks + any pins referencing them) explicitly.
    playlist_ids = [row[0] for row in session.execute(select(Playlist.id).where(Playlist.user_id == user_id))]
    if playlist_ids:
        session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id.in_(playlist_ids)))
        session.execute(delete(PinnedPlaylist).where(PinnedPlaylist.playlist_id.in_(playlist_ids)))
        session.execute(delete(Playlist).where(Playlist.id.in_(playlist_ids)))
    # Delete remaining user-owned rows that lack ORM cascade.
    session.execute(delete(PlayEvent).where(PlayEvent.user_id == user_id))
    session.execute(delete(PinnedPlaylist).where(PinnedPlaylist.user_id == user_id))
    session.execute(delete(PinnedItem).where(PinnedItem.user_id == user_id))
    session.execute(delete(MobileDevice).where(MobileDevice.user_id == user_id))
    session.execute(delete(Notification).where(Notification.user_id == user_id))
    session.execute(delete(PlaybackCommand).where(PlaybackCommand.user_id == user_id))
    session.execute(delete(Automation).where(Automation.owner_id == user_id))
    # ORM cascade handles permissions, wishlists, player_state, auth_sessions, api_keys.
    session.delete(user)
    session.commit()
    return Response(status_code=204)


@router.post("/me/pin", response_model=UserOut, tags=["users"], summary="Update own PIN")
def update_own_pin(
    payload: UserPinUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    user.pin_hash = hash_password(payload.password)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.put("/me/jellyfin-user", response_model=UserOut, tags=["users"], summary="Link or unlink a Jellyfin user account for playlist sync")
def update_own_jellyfin_user(
    payload: JellyfinUserLinkUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    user.jellyfin_user_id = payload.jellyfin_user_id or None
    session.commit()
    session.refresh(user)
    return serialize_user(user)


@router.put("/users/{user_id}/jellyfin-user", response_model=UserOut, tags=["users"], summary="Link or unlink a Jellyfin user account for a given Nudibranch user")
def update_user_jellyfin_user(
    user_id: str,
    payload: JellyfinUserLinkUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.jellyfin_user_id = payload.jellyfin_user_id or None
    session.commit()
    session.refresh(target)
    return serialize_user(target)


@router.put("/me/appearance", response_model=UserOut, tags=["users"], summary="Update appearance settings")
def update_own_appearance(
    payload: UserAppearanceUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    user.theme = payload.theme
    user.accent_color = payload.accent_color
    user.background_tint = payload.background_tint
    user.crossfade_duration = payload.crossfade_duration
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.post("/player/status", tags=["users"], summary="Update player state", response_model=dict)
def update_player_status(
    payload: PlayerStateUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    state = session.get(PlayerState, user.id)
    if not state:
        state = PlayerState(user_id=user.id)
        session.add(state)
    track = session.get(Track, payload.track_id) if payload.track_id else None
    state.track_id = track.id if track else payload.track_id
    state.title = payload.title or (track.title if track else None)
    state.artist = payload.artist or (track.album.artist.name if track and track.album and track.album.artist else None)
    state.album = payload.album or (track.album.title if track and track.album else None)
    state.status = payload.status if payload.status in {"playing", "paused", "stopped"} else "stopped"
    state.queue_length = max(0, payload.queue_length)
    state.current_index = max(0, payload.current_index)
    state.position_seconds = payload.position_seconds
    state.duration_seconds = payload.duration_seconds
    state.shuffle = bool(payload.shuffle)
    state.repeat = payload.repeat if payload.repeat in {"off", "one", "all"} else "off"
    state.updated_at = datetime.now(timezone.utc)
    session.commit()
    return {"ok": True}


@router.post("/player/commands", response_model=PlayerCommandOut, tags=["users"], summary="Queue a remote playback command")
def create_player_command(
    payload: PlayerCommandCreate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlayerCommandOut:
    action = (payload.action or "play").strip().lower()
    target_type = payload.target_type
    target_id = payload.target_id
    target_label = None
    if action == "play" and not target_id and payload.target_query:
        q = payload.target_query.strip()
        if target_type == "playlist":
            playlist = session.scalar(
                select(Playlist).where(Playlist.name.ilike(q), or_(Playlist.user_id == user.id, Playlist.user_id.is_(None)))
            )
            if not playlist:
                raise HTTPException(status_code=404, detail="No playlist matches that name")
            target_id, target_label = playlist.id, playlist.name
        else:
            # Commands must resolve confidently (WRatio scores junk ~0.56; floor at 0.65,
            # or stricter per the user's threshold) so a nonsense query 404s.
            floor = max(0.65, user.search_min_confidence if user.search_min_confidence is not None else 0.0)
            matches = search_library(session, q, kinds=[target_type] if target_type else None, min_confidence=floor, limit=1)
            if not matches:
                raise HTTPException(status_code=404, detail="No library match for that query")
            top = matches[0]
            target_type, target_id, target_label = top["kind"], top["id"], top["name"]
    if target_id and not target_label:
        target_label = _resolve_target_label(session, target_type, target_id)
    command = PlaybackCommand(
        user_id=user.id,
        device_id=payload.device_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        loop=payload.loop if payload.loop in {"off", "one", "all"} else "off",
        shuffle=bool(payload.shuffle),
        status="pending",
    )
    session.add(command)
    session.commit()
    session.refresh(command)
    try:
        body = f"Play {target_label}" if action == "play" and target_label else action.capitalize()
        create_notification(
            session,
            title="Remote playback",
            body=body,
            event_type="remote_playback_command",
            target_url="/player",
            user_id=user.id,
            deliver_apns=True,
        )
    except Exception:
        pass
    return _serialize_command(command)


@router.get("/player/commands", response_model=list[PlayerCommandOut], tags=["users"], summary="Fetch my pending playback commands")
def list_player_commands(
    device_id: str | None = Query(None),
    include_consumed: bool = Query(False),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[PlayerCommandOut]:
    stmt = select(PlaybackCommand).where(PlaybackCommand.user_id == user.id)
    if not include_consumed:
        stmt = stmt.where(PlaybackCommand.status == "pending")
    if device_id:
        stmt = stmt.where(or_(PlaybackCommand.device_id.is_(None), PlaybackCommand.device_id == device_id))
    rows = session.scalars(stmt.order_by(PlaybackCommand.created_at.asc()))
    return [_serialize_command(c) for c in rows]


@router.post("/player/commands/{command_id}/ack", tags=["users"], summary="Mark a playback command consumed")
def ack_player_command(
    command_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    command = session.scalar(select(PlaybackCommand).where(PlaybackCommand.id == command_id, PlaybackCommand.user_id == user.id))
    if command:
        command.status = "consumed"
        command.consumed_at = datetime.now(timezone.utc)
        session.commit()
    return {"ok": True}


# ── Play history (local PlayEvent + best-effort Jellyfin report) ───────────────

def _report_play_to_jellyfin(session: Session, user: User, track: Track) -> bool:
    """Increment Jellyfin's play count for this user+track. Best-effort; never raises."""
    if not user.jellyfin_user_id or not track.jellyfin_item_id:
        return False
    client, jf_user_id = _jf_client(session, user)
    if not client:
        return False
    try:
        with client:
            resp = client.post(f"/Users/{jf_user_id}/PlayedItems/{track.jellyfin_item_id}")
            resp.raise_for_status()
        return True
    except Exception:
        return False


def _play_event_out(event: PlayEvent) -> PlayEventOut:
    track = event.track
    album = track.album if track else None
    artist = album.artist if album else None
    return PlayEventOut(
        track_id=event.track_id,
        title=track.title if track else None,
        artist=artist.name if artist else None,
        album=album.title if album else None,
        album_id=album.id if album else None,
        played_at=event.played_at,
    )


@router.post("/me/plays", response_model=PlayEventOut, tags=["users"], summary="Record a track play (local history + Jellyfin)")
def record_play(
    payload: PlayRecordIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlayEventOut:
    track = session.get(Track, payload.track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    reported = _report_play_to_jellyfin(session, user, track)
    event = PlayEvent(user_id=user.id, track_id=track.id, source="nudibranch", reported_to_jellyfin=reported)
    session.add(event)
    session.commit()
    session.refresh(event)
    return _play_event_out(event)


@router.get("/me/plays", response_model=list[PlayEventOut], tags=["users"], summary="My recent plays")
def list_my_plays(
    limit: int = 50,
    days: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[PlayEventOut]:
    limit = max(1, min(limit, 500))
    query = (
        select(PlayEvent)
        .where(PlayEvent.user_id == user.id)
        .options(selectinload(PlayEvent.track).selectinload(Track.album).selectinload(Album.artist))
        .order_by(PlayEvent.played_at.desc())
        .limit(limit)
    )
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.where(PlayEvent.played_at >= since)
    return [_play_event_out(e) for e in session.scalars(query)]


def _top_for_window(session: Session, user: User, days: int) -> dict:
    """Top artist / album / track for a user over the last `days` (by local play count)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = (
        select(PlayEvent.track_id, func.count(PlayEvent.id).label("plays"))
        .where(PlayEvent.user_id == user.id, PlayEvent.played_at >= since)
        .group_by(PlayEvent.track_id)
    ).subquery()
    rows = list(
        session.execute(
            select(Track, Album, Artist, base.c.plays)
            .join(base, base.c.track_id == Track.id)
            .join(Album, Track.album_id == Album.id)
            .join(Artist, Album.artist_id == Artist.id)
        )
    )
    if not rows:
        return {"days": days, "artist": None, "album": None, "track": None}
    artist_counts: dict[str, dict] = {}
    album_counts: dict[str, dict] = {}
    top_track = None
    top_track_plays = -1
    for track, album, artist, plays in rows:
        a = artist_counts.setdefault(artist.id, {"id": artist.id, "name": artist.name, "plays": 0})
        a["plays"] += plays
        al = album_counts.setdefault(album.id, {"id": album.id, "title": album.title, "artist": artist.name, "plays": 0})
        al["plays"] += plays
        if plays > top_track_plays:
            top_track_plays = plays
            top_track = {"id": track.id, "title": track.title, "artist": artist.name, "album": album.title, "plays": plays}
    top_artist = max(artist_counts.values(), key=lambda x: x["plays"])
    top_album = max(album_counts.values(), key=lambda x: x["plays"])
    return {"days": days, "artist": top_artist, "album": top_album, "track": top_track}


@router.get("/library/top", tags=["library"], summary="Top artist/album/track over a window", response_model=dict)
def library_top(
    days: int = 30,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    days = max(1, min(days, 365))
    return _top_for_window(session, user, days)


# ── Offline delta sync ────────────────────────────────────────────────────────

@router.get("/library/changes", tags=["library"], summary="Library rows changed since a timestamp (delta sync)", response_model=dict)
def library_changes(
    since: str | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_read)),
) -> dict:
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid 'since' timestamp (use ISO 8601)")

    def _filter(query, model):
        return query.where(model.updated_at > since_dt) if since_dt else query

    def _iso(dt):
        # SQLite stores these tz-naive; they are UTC, so emit a UTC-aware ISO string
        # consistent with server_time for client cursor math.
        if not dt:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()

    artists = [
        {"id": a.id, "name": a.name, "sort_name": a.sort_name, "musicbrainz_id": a.musicbrainz_id,
         "updated_at": _iso(a.updated_at)}
        for a in session.scalars(_filter(select(Artist), Artist))
    ]
    albums = [
        {"id": al.id, "artist_id": al.artist_id, "title": al.title, "release_title": al.release_title,
         "cover_path": al.cover_path, "musicbrainz_release_id": al.musicbrainz_release_id,
         "musicbrainz_release_group_id": al.musicbrainz_release_group_id,
         "updated_at": _iso(al.updated_at)}
        for al in session.scalars(_filter(select(Album), Album))
    ]
    tracks = [
        {"id": t.id, "album_id": t.album_id, "title": t.title, "track_number": t.track_number,
         "disc_number": t.disc_number, "duration_ms": t.duration_ms, "format": t.format,
         "bitrate": t.bitrate, "is_lossless": t.is_lossless, "explicit": t.explicit,
         "musicbrainz_recording_id": t.musicbrainz_recording_id, "jellyfin_item_id": t.jellyfin_item_id,
         "musicbrainz_verified": t.musicbrainz_verified,
         "updated_at": _iso(t.updated_at)}
        for t in session.scalars(_filter(select(Track), Track))
    ]
    return {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "since": since_dt.isoformat() if since_dt else None,
        "artists": artists,
        "albums": albums,
        "tracks": tracks,
        # NOTE: deletions are not delta-tracked; clients should occasionally full-resync (since omitted) to prune.
    }


# ── Pinned playlists + home dashboard ─────────────────────────────────────────

@router.get("/me/pinned-playlists", tags=["users"], summary="My pinned playlists", response_model=list[dict])
def list_pinned_playlists(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    rows = session.scalars(
        select(PinnedPlaylist).where(PinnedPlaylist.user_id == user.id).order_by(PinnedPlaylist.created_at.asc())
    )
    return [{"playlist_id": p.playlist_id, "name": p.name} for p in rows]


@router.post("/me/pinned-playlists", tags=["users"], summary="Pin a playlist", response_model=list[dict])
def pin_playlist(
    payload: PinPlaylistIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    existing = session.scalar(
        select(PinnedPlaylist).where(PinnedPlaylist.user_id == user.id, PinnedPlaylist.playlist_id == payload.playlist_id)
    )
    if not existing:
        session.add(PinnedPlaylist(user_id=user.id, playlist_id=payload.playlist_id, name=(payload.name or payload.playlist_id)[:255]))
        session.commit()
    elif payload.name and existing.name != payload.name:
        existing.name = payload.name[:255]
        session.commit()
    return list_pinned_playlists(session, user)


@router.delete("/me/pinned-playlists/{playlist_id}", tags=["users"], summary="Unpin a playlist", response_model=list[dict])
def unpin_playlist(
    playlist_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    existing = session.scalar(
        select(PinnedPlaylist).where(PinnedPlaylist.user_id == user.id, PinnedPlaylist.playlist_id == playlist_id)
    )
    if existing:
        session.delete(existing)
        session.commit()
    return list_pinned_playlists(session, user)


def _pinned_item_ids(session: Session, user: User, kind: str) -> list[str]:
    return [
        p.item_id
        for p in session.scalars(
            select(PinnedItem)
            .where(PinnedItem.user_id == user.id, PinnedItem.kind == kind)
            .order_by(PinnedItem.created_at.asc())
        )
    ]


def _toggle_pinned_item(session: Session, user: User, kind: str, item_id: str, pin: bool) -> None:
    existing = session.scalar(
        select(PinnedItem).where(PinnedItem.user_id == user.id, PinnedItem.kind == kind, PinnedItem.item_id == item_id)
    )
    if pin and not existing:
        session.add(PinnedItem(user_id=user.id, kind=kind, item_id=item_id))
        session.commit()
    elif not pin and existing:
        session.delete(existing)
        session.commit()


@router.get("/me/pinned-albums", tags=["users"], summary="My pinned albums", response_model=list[dict])
def list_pinned_albums(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    return [{"album_id": item_id} for item_id in _pinned_item_ids(session, user, "album")]


@router.post("/me/pinned-albums", tags=["users"], summary="Pin an album", response_model=list[dict])
def pin_album(
    payload: PinAlbumIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    _toggle_pinned_item(session, user, "album", payload.album_id, pin=True)
    return list_pinned_albums(session, user)


@router.delete("/me/pinned-albums/{album_id}", tags=["users"], summary="Unpin an album", response_model=list[dict])
def unpin_album(
    album_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    _toggle_pinned_item(session, user, "album", album_id, pin=False)
    return list_pinned_albums(session, user)


@router.get("/me/pinned-artists", tags=["users"], summary="My pinned artists", response_model=list[dict])
def list_pinned_artists(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    return [{"artist_id": item_id} for item_id in _pinned_item_ids(session, user, "artist")]


@router.post("/me/pinned-artists", tags=["users"], summary="Pin an artist", response_model=list[dict])
def pin_artist(
    payload: PinArtistIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    _toggle_pinned_item(session, user, "artist", payload.artist_id, pin=True)
    return list_pinned_artists(session, user)


@router.delete("/me/pinned-artists/{artist_id}", tags=["users"], summary="Unpin an artist", response_model=list[dict])
def unpin_artist(
    artist_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    _toggle_pinned_item(session, user, "artist", artist_id, pin=False)
    return list_pinned_artists(session, user)


@router.get("/me/home", tags=["users"], summary="Home dashboard aggregate", response_model=dict)
def me_home(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    # Recently added albums (by created_at)
    recent_albums = list(
        session.scalars(
            select(Album)
            .options(selectinload(Album.artist))
            .order_by(Album.created_at.desc())
            .limit(12)
        )
    )
    recently_added = [
        {"id": al.id, "title": al.title, "artist": al.artist.name if al.artist else None,
         "cover_path": al.cover_path, "created_at": al.created_at.isoformat() if al.created_at else None}
        for al in recent_albums
    ]

    # Recently approved from my wishlist (completed items)
    approved = list(
        session.scalars(
            select(WishlistItem)
            .where(WishlistItem.user_id == user.id, WishlistItem.status == "completed")
            .order_by(WishlistItem.status_changed_at.desc())
            .limit(12)
        )
    )
    recently_approved = [
        {"id": w.id, "artist": w.artist, "album": w.album, "track": w.track,
         "approved_at": w.status_changed_at.isoformat() if w.status_changed_at else None}
        for w in approved
    ]

    # Recent plays — dedupe by track_id, keeping the most-recent occurrence, cap at 12.
    _raw_plays = list_my_plays(limit=100, days=None, session=session, user=user)
    _seen_play_tracks: set[str] = set()
    recent_plays = []
    for _play in _raw_plays:
        if _play.track_id in _seen_play_tracks:
            continue
        _seen_play_tracks.add(_play.track_id)
        recent_plays.append(_play)
        if len(recent_plays) >= 12:
            break

    # Pinned playlists (names from our own table — no per-playlist Jellyfin fan-out).
    pinned = [
        {"playlist_id": p.playlist_id, "name": p.name, "track_count": None}
        for p in session.scalars(
            select(PinnedPlaylist).where(PinnedPlaylist.user_id == user.id).order_by(PinnedPlaylist.created_at.asc())
        )
    ]

    # Pinned albums/artists, resolved (in pin order) to title + cover for the Home grid.
    pinned_album_ids = _pinned_item_ids(session, user, "album")
    albums_by_id = {}
    if pinned_album_ids:
        albums_by_id = {
            al.id: al
            for al in session.scalars(
                select(Album).options(selectinload(Album.artist)).where(Album.id.in_(pinned_album_ids))
            )
        }
    pinned_albums = [
        {"id": al.id, "title": al.title, "artist": al.artist.name if al.artist else None, "cover_path": al.cover_path}
        for aid in pinned_album_ids
        if (al := albums_by_id.get(aid))
    ]
    pinned_artist_ids = _pinned_item_ids(session, user, "artist")
    artists_by_id = {}
    if pinned_artist_ids:
        artists_by_id = {a.id: a for a in session.scalars(select(Artist).where(Artist.id.in_(pinned_artist_ids)))}
    pinned_artists = [
        {"id": a.id, "name": a.name, "cover_path": a.cover_path}
        for aid in pinned_artist_ids
        if (a := artists_by_id.get(aid))
    ]

    # Favorites count is a single cheap call when Jellyfin is linked.
    favorites: dict | None = None
    client, jf_user_id = _jf_client(session, user)
    if client:
        with client:
            try:
                fav = _jf_favorites_out(session, client, jf_user_id)
                favorites = {"id": "favorites", "name": "Favorites", "track_count": fav.track_count}
            except Exception:
                favorites = None
            # Per-pinned-playlist track counts (same source as Favorites; pinned playlists are few).
            for entry in pinned:
                try:
                    if entry["playlist_id"] == "favorites":
                        entry["track_count"] = favorites["track_count"] if favorites else None
                    else:
                        entry["track_count"] = _jf_playlist_out(session, client, jf_user_id, entry["playlist_id"], entry["name"]).track_count
                except Exception:
                    entry["track_count"] = None

    return {
        "recently_added": recently_added,
        "recently_approved": recently_approved,
        "recent_plays": [e.model_dump(mode="json") for e in recent_plays],
        "favorites": favorites,
        "pinned_playlists": pinned,
        "pinned_albums": pinned_albums,
        "pinned_artists": pinned_artists,
    }


def _serialize_automation(automation: Automation) -> AutomationOut:
    def _loads(value: str | None) -> dict:
        try:
            return json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
    return AutomationOut(
        id=automation.id,
        name=automation.name,
        enabled=automation.enabled,
        trigger_type=automation.trigger_type,
        trigger_config=_loads(automation.trigger_config),
        action_type=automation.action_type,
        action_config=_loads(automation.action_config),
        notify_mode=automation.notify_mode,
        notify_priority=automation.notify_priority,
        webhook_token=automation.webhook_token,
        webhook_url=f"/api/v1/automations/hooks/{automation.webhook_token}" if automation.webhook_token else None,
        last_run_at=automation.last_run_at,
        last_status=automation.last_status,
        last_error=automation.last_error,
        next_run_at=automation.next_run_at,
        created_at=automation.created_at,
    )


@router.get("/automations", response_model=list[AutomationOut], tags=["automations"], summary="List my automations")
def list_automations(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.automations_manage)),
) -> list[AutomationOut]:
    rows = session.scalars(select(Automation).where(Automation.owner_id == user.id).order_by(Automation.created_at.desc()))
    return [_serialize_automation(a) for a in rows]


@router.post("/automations", response_model=AutomationOut, tags=["automations"], summary="Create an automation")
def create_automation(
    payload: AutomationCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.automations_manage)),
) -> AutomationOut:
    if payload.trigger_type not in TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail="Invalid trigger_type")
    if payload.action_type not in ACTION_TYPES:
        raise HTTPException(status_code=400, detail="Invalid action_type")
    automation = Automation(
        owner_id=user.id,
        name=payload.name.strip(),
        enabled=payload.enabled,
        trigger_type=payload.trigger_type,
        trigger_config=json.dumps(payload.trigger_config or {}),
        action_type=payload.action_type,
        action_config=json.dumps(payload.action_config or {}),
        notify_mode=payload.notify_mode if payload.notify_mode in NOTIFY_MODES else "log",
        notify_priority=payload.notify_priority if payload.notify_priority in NOTIFY_PRIORITIES else "normal",
        webhook_token=secrets.token_urlsafe(24),
    )
    automation.next_run_at = compute_next_run(automation.trigger_type, payload.trigger_config or {})
    session.add(automation)
    session.commit()
    session.refresh(automation)
    return _serialize_automation(automation)


@router.get("/automations/{automation_id}", response_model=AutomationOut, tags=["automations"], summary="Get an automation")
def get_automation(
    automation_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.automations_manage)),
) -> AutomationOut:
    automation = session.scalar(select(Automation).where(Automation.id == automation_id, Automation.owner_id == user.id))
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    return _serialize_automation(automation)


@router.patch("/automations/{automation_id}", response_model=AutomationOut, tags=["automations"], summary="Update an automation")
def update_automation(
    automation_id: str,
    payload: AutomationUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.automations_manage)),
) -> AutomationOut:
    automation = session.scalar(select(Automation).where(Automation.id == automation_id, Automation.owner_id == user.id))
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    if payload.name is not None:
        automation.name = payload.name.strip()
    if payload.enabled is not None:
        automation.enabled = payload.enabled
    if payload.trigger_type is not None:
        if payload.trigger_type not in TRIGGER_TYPES:
            raise HTTPException(status_code=400, detail="Invalid trigger_type")
        automation.trigger_type = payload.trigger_type
    if payload.trigger_config is not None:
        automation.trigger_config = json.dumps(payload.trigger_config)
    if payload.action_type is not None:
        if payload.action_type not in ACTION_TYPES:
            raise HTTPException(status_code=400, detail="Invalid action_type")
        automation.action_type = payload.action_type
    if payload.action_config is not None:
        automation.action_config = json.dumps(payload.action_config)
    if payload.notify_mode is not None and payload.notify_mode in NOTIFY_MODES:
        automation.notify_mode = payload.notify_mode
    if payload.notify_priority is not None and payload.notify_priority in NOTIFY_PRIORITIES:
        automation.notify_priority = payload.notify_priority
    try:
        automation.next_run_at = compute_next_run(automation.trigger_type, json.loads(automation.trigger_config or "{}"))
    except json.JSONDecodeError:
        automation.next_run_at = None
    session.commit()
    session.refresh(automation)
    return _serialize_automation(automation)


@router.delete("/automations/{automation_id}", tags=["automations"], summary="Delete an automation")
def delete_automation(
    automation_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.automations_manage)),
) -> dict:
    automation = session.scalar(select(Automation).where(Automation.id == automation_id, Automation.owner_id == user.id))
    if automation:
        session.delete(automation)
        session.commit()
    return {"ok": True}


@router.post("/automations/{automation_id}/run", tags=["automations"], summary="Run an automation now")
def run_automation_now(
    automation_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.automations_manage)),
) -> dict:
    automation = session.scalar(select(Automation).where(Automation.id == automation_id, Automation.owner_id == user.id))
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    status, message = run_automation(session, automation, trigger_source="manual")
    return {"status": status, "message": message}


@router.post("/automations/hooks/{token}", tags=["automations"], summary="Webhook trigger (IFTTT) — token is the auth")
def automation_webhook(token: str, session: Session = Depends(get_session)) -> dict:
    automation = session.scalar(select(Automation).where(Automation.webhook_token == token))
    if not automation or not automation.enabled:
        raise HTTPException(status_code=404, detail="Unknown or disabled automation")
    status, message = run_automation(session, automation, trigger_source="webhook")
    return {"status": status, "message": message}


@router.get("/users/playback", tags=["users"], summary="Get all users' playback state", response_model=dict)
def users_playback(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.activity_read)),
) -> dict:
    users = list(session.scalars(select(User).options(selectinload(User.player_state)).order_by(User.created_at.asc())))
    return {
        "app": [serialize_player_state(user) for user in users],
        "jellyfin": jellyfin_now_playing(session),
    }


def _bucket_first_char(sort_expr):
    return func.substr(func.trim(sort_expr), 1, 1)


def _bucket_condition(sort_expr, bucket: str):
    """SQL filter for a first-character bucket; None means no filter (all)."""
    if not bucket or bucket == "all":
        return None
    first = _bucket_first_char(sort_expr)
    # "#" is the unified non-letter bucket (digits + symbols).
    if bucket == "#":
        return first.op("NOT GLOB")("[A-Za-z]")
    if bucket == "0-9":
        return first.op("GLOB")("[0-9]")
    if bucket == "symbol":
        return first.op("NOT GLOB")("[A-Za-z0-9]")
    return func.upper(first) == bucket[:1].upper()


def _bucket_label(sort_expr):
    first = _bucket_first_char(sort_expr)
    return case(
        (func.upper(first).op("GLOB")("[A-Z]"), func.upper(first)),
        else_=literal("#"),
    )


def _bucket_sort_key(bucket: str):
    if bucket == "#":
        return (0, "")
    return (1, bucket)


def _artist_sort_expr():
    return func.coalesce(func.nullif(func.trim(Artist.sort_name), ""), Artist.name)


def _album_sort_expr():
    return func.coalesce(func.nullif(func.trim(Album.sort_name), ""), Album.title)


@router.get("/library/tree", response_model=list[LibraryTreeArtist], tags=["library"], summary="Get library tree")
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
                    sort_name=album.sort_name,
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
                            musicbrainz_verified=track.musicbrainz_verified,
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
                for album in sorted(artist.albums, key=lambda album: ((album.sort_name or album.title) or "").lower())
            ],
            sort_name=artist.sort_name,
            musicbrainz_id=artist.musicbrainz_id,
            cover_path=artist.cover_path,
        )
        for artist in artists
    ]


@router.get("/library/artists", response_model=PaginatedArtists, tags=["library"], summary="Paginated artists by bucket")
def library_artists(
    bucket: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    q: str | None = Query(None),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_read)),
) -> PaginatedArtists:
    sort_expr = _artist_sort_expr()
    stmt = select(Artist)
    cond = _bucket_condition(sort_expr, bucket)
    if cond is not None:
        stmt = stmt.where(cond)
    if q:
        stmt = stmt.where(Artist.name.ilike(f"%{q.strip()}%"))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(sort_expr, Artist.name).offset((page - 1) * page_size).limit(page_size).options(selectinload(Artist.albums))
    )
    items = [LibraryArtistRow(id=a.id, name=a.name, sort_name=a.sort_name, cover_path=a.cover_path, album_count=len(a.albums)) for a in rows]
    return PaginatedArtists(items=items, total=total, page=page, page_size=page_size)


@router.get("/library/albums", response_model=PaginatedAlbums, tags=["library"], summary="Paginated albums by bucket")
def library_albums(
    bucket: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    artist_id: str | None = Query(None),
    q: str | None = Query(None),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_read)),
) -> PaginatedAlbums:
    sort_expr = _album_sort_expr()
    stmt = select(Album)
    if artist_id:
        stmt = stmt.where(Album.artist_id == artist_id)
    cond = _bucket_condition(sort_expr, bucket)
    if cond is not None:
        stmt = stmt.where(cond)
    if q:
        stmt = stmt.where(Album.title.ilike(f"%{q.strip()}%"))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(sort_expr, Album.title).offset((page - 1) * page_size).limit(page_size)
        .options(selectinload(Album.artist), selectinload(Album.tracks))
    )
    items = [
        LibraryAlbumRow(
            id=al.id, title=al.title, sort_name=al.sort_name, artist_id=al.artist_id,
            artist_name=(al.artist.name if al.artist else ""),
            cover_path=al.cover_path, track_count=len(al.tracks),
        )
        for al in rows
    ]
    return PaginatedAlbums(items=items, total=total, page=page, page_size=page_size)


@router.get("/library/tracks", response_model=PaginatedTracks, tags=["library"], summary="Paginated tracks by bucket")
def library_tracks(
    bucket: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    album_id: str | None = Query(None),
    q: str | None = Query(None),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_read)),
) -> PaginatedTracks:
    stmt = select(Track)
    if album_id:
        stmt = stmt.where(Track.album_id == album_id)
    cond = _bucket_condition(Track.title, bucket)
    if cond is not None:
        stmt = stmt.where(cond)
    if q:
        stmt = stmt.where(Track.title.ilike(f"%{q.strip()}%"))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.options(selectinload(Track.album).selectinload(Album.artist))
    if album_id:
        ordered = stmt.order_by(Track.disc_number, Track.track_number)
    else:
        ordered = stmt.order_by(func.lower(Track.title))
    rows = session.scalars(ordered.offset((page - 1) * page_size).limit(page_size))
    items = [
        LibraryTrackRow(
            id=t.id, title=t.title, album_id=t.album_id,
            album_title=(t.album.title if t.album else ""),
            artist_id=(t.album.artist_id if t.album else ""),
            artist_name=(t.album.artist.name if t.album and t.album.artist else ""),
            track_number=t.track_number, disc_number=t.disc_number,
            duration_ms=t.duration_ms, format=t.format, is_lossless=t.is_lossless,
        )
        for t in rows
    ]
    return PaginatedTracks(items=items, total=total, page=page, page_size=page_size)


@router.get("/library/buckets", response_model=list[BucketCount], tags=["library"], summary="Non-empty buckets + counts")
def library_buckets(
    type: str = Query("artists"),
    artist_id: str | None = Query(None),
    album_id: str | None = Query(None),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_read)),
) -> list[BucketCount]:
    if type == "albums":
        sort_expr = Album.title
        base = select(_bucket_label(sort_expr).label("bucket"), func.count().label("n")).select_from(Album)
        if artist_id:
            base = base.where(Album.artist_id == artist_id)
    elif type == "tracks":
        sort_expr = Track.title
        base = select(_bucket_label(sort_expr).label("bucket"), func.count().label("n")).select_from(Track)
        if album_id:
            base = base.where(Track.album_id == album_id)
    else:
        sort_expr = _artist_sort_expr()
        base = select(_bucket_label(sort_expr).label("bucket"), func.count().label("n")).select_from(Artist)
    rows = session.execute(base.group_by("bucket")).all()
    counts = [BucketCount(bucket=r[0], count=r[1]) for r in rows]
    counts.sort(key=lambda c: _bucket_sort_key(c.bucket))
    return counts


@router.get("/library/search", response_model=SearchResponse, tags=["library"], summary="Fuzzy search artists/albums/tracks")
def library_search(
    q: str = Query(..., min_length=1),
    types: str | None = Query(None, description="Comma list of artist,album,track"),
    min_confidence: float | None = Query(None, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.library_read)),
) -> SearchResponse:
    kinds = [t.strip() for t in types.split(",") if t.strip()] if types else None
    threshold = min_confidence if min_confidence is not None else (
        user.search_min_confidence if user.search_min_confidence is not None else 0.4
    )
    results = search_library(session, q, kinds=kinds, min_confidence=threshold, limit=limit)
    return SearchResponse(query=q, min_confidence=threshold, results=[SearchResultItem(**r) for r in results])


@router.post("/library/search/reindex", tags=["library"], summary="Rebuild the search index")
def library_search_reindex(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> dict:
    return {"indexed": rebuild_search_index(session)}


@router.put("/me/search-settings", response_model=UserOut, tags=["users"], summary="Update my search confidence threshold")
def update_search_settings(
    payload: UserSearchSettingsUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    if payload.min_confidence is not None:
        user.search_min_confidence = payload.min_confidence
    if payload.page_size is not None:
        user.library_page_size = payload.page_size
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.post("/library/metadata", response_model=ProposalBatchOut, tags=["library"], summary="Propose metadata edit")
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


@router.post("/library/metadata/apply", tags=["library"], summary="Apply metadata edit directly")
def apply_library_metadata(
    payload: LibraryMetadataProposalRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    # Direct apply (no review queue): library metadata edits commit on field blur.
    # Reuses the exact apply path the approved-batch executor runs, so behavior
    # (cover-URL download, folder sync, duplicate merge) is identical.
    changes = {key: value for key, value in payload.changes.items() if key in editable_fields(payload.target_type)}
    if not changes:
        raise HTTPException(status_code=400, detail="No editable metadata fields were supplied")

    target = metadata_target(session, payload.target_type, payload.target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Library item not found")

    from nudibranch.worker.main import apply_metadata_item

    # Transient (never added to the session) — apply_metadata_item only reads payload_json.
    item = ProposalItem(
        title=metadata_target_title(payload.target_type, target),
        kind=ProposalKind.metadata,
        payload_json=json.dumps(
            {
                "target_type": payload.target_type,
                "target_id": payload.target_id,
                "changes": changes,
            }
        ),
    )
    try:
        apply_metadata_item(session, item)
        session.commit()
    except Exception as error:  # noqa: BLE001 — surface any apply failure to the client
        session.rollback()
        raise HTTPException(status_code=400, detail=f"Could not apply metadata change: {error}")
    return {"ok": True, "applied": changes}


@router.post("/library/remove", response_model=ProposalBatchOut, tags=["library"], summary="Propose library removal")
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
        # An album row with no track files is an orphan/duplicate; offer a record-only
        # removal (never deletes files — an empty duplicate may share its folder/cover
        # with the real album).
        if payload.target_type == "album" and not library_target_tracks(target):
            artist_name = target.artist.name if target.artist else "Unknown Artist"
            batch = ProposalBatch(title="Remove empty album", kind=ProposalKind.delete, tree_path="/library")
            session.add(batch)
            session.flush()
            artist_item = ProposalItem(batch_id=batch.id, title=artist_name, kind=ProposalKind.delete)
            session.add(artist_item)
            session.flush()
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    title=f"{target.title} (empty record — no tracks)",
                    kind=ProposalKind.delete,
                    old_value=target.title,
                    payload_json=json.dumps({"action": "remove_empty_album", "album_id": target.id}),
                    parent_id=artist_item.id,
                )
            )
            session.commit()
            session.refresh(batch)
            return serialize_batch(batch)
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
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    for track in tracks:
        artist_name = track.album.artist.name if track.album and track.album.artist else "Unknown Artist"
        album_title = track.album.title if track.album else "Unknown Album"
        album_key = (artist_name, album_title)
        if artist_name not in artist_items:
            artist_item = ProposalItem(batch_id=batch.id, title=artist_name, kind=batch_kind)
            session.add(artist_item)
            session.flush()
            artist_items[artist_name] = artist_item
        if album_key not in album_items:
            album_item = ProposalItem(
                batch_id=batch.id,
                title=album_title,
                kind=batch_kind,
                parent_id=artist_items[artist_name].id,
            )
            session.add(album_item)
            session.flush()
            album_items[album_key] = album_item
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
                parent_id=album_items[album_key].id,
            )
        )
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch)


@router.post("/library/albums/{album_id}/musicbrainz-match", tags=["library"], summary="Match album to MusicBrainz", response_model=dict)
def musicbrainz_match_library_album(
    album_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    album_record = None
    if album.artist:
        try:
            album_record = lookup_album_tracks(album.artist.name, album.title, album.musicbrainz_release_id)
        except (RuntimeError, httpx.HTTPError):
            album_record = None
    results = []
    for track in sorted(album.tracks, key=lambda track: (track.disc_number or 1, track.track_number or 9999, track.title.lower())):
        results.append(musicbrainz_match_track_result(session, track, force=False, album_record=album_record))

    metadata_batch = queue_musicbrainz_metadata_fixes(session, results)
    replacement_batch = queue_musicbrainz_replacement_downloads(session, results)
    session.commit()
    return {
        "album_id": album.id,
        "album": album.title,
        "tracks": results,
        "queued_changes": sum(1 for result in results if result.get("changes")),
        "queued_replacements": sum(1 for result in results if result.get("replacement_request")),
        "batch_id": metadata_batch.id if metadata_batch else replacement_batch.id if replacement_batch else None,
    }


@router.post("/library/tracks/{track_id}/musicbrainz-match", tags=["library"], summary="Match track to MusicBrainz", response_model=dict)
def musicbrainz_match_library_track(
    track_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    track = session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    result = musicbrainz_match_track_result(session, track, force=True)
    metadata_batch = queue_musicbrainz_metadata_fixes(session, [result])
    replacement_batch = queue_musicbrainz_replacement_downloads(session, [result])
    session.commit()
    result["queued_changes"] = 1 if result.get("changes") else 0
    result["queued_replacements"] = 1 if result.get("replacement_request") else 0
    result["batch_id"] = metadata_batch.id if metadata_batch else replacement_batch.id if replacement_batch else None
    return result


@router.post("/library/tracks/{track_id}/verify-audio", tags=["library"], summary="Verify track audio via AcoustID", response_model=AudioVerifyResult)
def verify_track_audio(
    track_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_permission(Permission.library_manage)),
) -> AudioVerifyResult:
    track = session.scalar(
        select(Track)
        .where(Track.id == track_id)
        .options(selectinload(Track.album).selectinload(Album.artist))
    )
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not track.path or not Path(track.path).exists():
        raise HTTPException(status_code=404, detail="Track file is missing")
    api_key = integration_value(session, "acoustid_api_key")
    claimed_title = track.title
    claimed_artist = track.album.artist.name if track.album and track.album.artist else None
    claimed_recording_id = track.musicbrainz_recording_id
    result = audio_matches_claim(
        Path(track.path),
        claimed_title=claimed_title,
        claimed_artist=claimed_artist,
        claimed_recording_id=claimed_recording_id,
        api_key=api_key,
    )
    # Surface the outcome in the Activity log + a notification (never inline in the tree).
    track_label = f"{claimed_artist} — {claimed_title}" if claimed_artist else (claimed_title or track_id)
    detected_summary = "; ".join(
        f"{d['title']} — {d['artist']} ({round((d.get('score') or 0) * 100)}%)" for d in result["detected"][:3]
    )
    log_message = f"Audio check: {track_label}: {result['message']}"
    if detected_summary:
        log_message = f"{log_message} (detected: {detected_summary})"
    write_app_log(log_message, level="warning" if result["matched"] is False else "info")
    create_notification(
        session,
        title="Audio check complete",
        body=f"{track_label}: {result['message']}",
        event_type="library_audio_check",
        target_url="/library",
        user_id=current_user.id,
    )
    session.commit()
    return AudioVerifyResult(
        matched=result["matched"],
        confidence=result["confidence"],
        message=result["message"],
        claimed={
            "title": claimed_title,
            "artist": claimed_artist,
            "recording_id": claimed_recording_id,
        },
        detected=[AudioVerifyDetected(**d) for d in result["detected"]],
        duration_seconds=result["duration"],
    )


@router.post("/library/tracks/{track_id}/replace", response_model=TaskOut, tags=["library"], summary="Queue a replacement download for a track")
def requeue_track_replacement(
    track_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    track = session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return serialize_task(enqueue_task(session, "requeue_replacement", {"track_ids": [track.id]}))


@router.post("/library/albums/{album_id}/replace", response_model=TaskOut, tags=["library"], summary="Queue replacement downloads for an album's tracks")
def requeue_album_replacement(
    album_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    track_ids = [t.id for t in session.scalars(select(Track.id).where(Track.album_id == album.id))]
    if not track_ids:
        raise HTTPException(status_code=400, detail="Album has no tracks")
    return serialize_task(enqueue_task(session, "requeue_replacement", {"track_ids": track_ids}))


def musicbrainz_match_track_result(session: Session, track: Track, force: bool = False, album_record: dict | None = None) -> dict:
    result = {
        "track_id": track.id,
        "title": track.title,
        "track_number": track.track_number,
        "status": "unmatched",
        "score": None,
        "candidate": None,
        "changes": {},
        "replacement_request": None,
        "error": None,
        "musicbrainz_verified": track.musicbrainz_verified,
    }
    if track.musicbrainz_verified and not force:
        result["status"] = "skipped_verified"
        return result
    if not track.path or not Path(track.path).exists():
        result["status"] = "missing_file"
        result["error"] = "Track file is missing"
        return result
    try:
        expected = expected_musicbrainz_metadata_for_track(track, album_record=album_record)
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        result["status"] = "error"
        result["error"] = str(error)
        return result
    file_metadata = read_audio_metadata(Path(track.path))
    match = musicbrainz_file_match(file_metadata, expected)
    matched = match["matched"]
    changes = musicbrainz_metadata_changes(track, expected) if matched else {}
    replacement_request = musicbrainz_replacement_request(track) if not matched else None
    result.update(
        {
            "status": "matched" if matched else "changed",
            "score": round(match["score"] * 100),
            "candidate": expected,
            "changes": changes,
            "replacement_request": replacement_request,
            "musicbrainz_verified": track.musicbrainz_verified,
            "message": match.get("message"),
        }
    )
    return result


def expected_musicbrainz_metadata_for_track(track: Track, album_record: dict | None = None) -> dict:
    fallback = {
        "artist": track.album.artist.name if track.album and track.album.artist else None,
        "albumartist": track.album.artist.name if track.album and track.album.artist else None,
        "album": track.album.title if track.album else None,
        "title": track.title,
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "duration_ms": track.duration_ms,
        "musicbrainz_recording_id": track.musicbrainz_recording_id,
        "musicbrainz_album_id": track.album.musicbrainz_release_id if track.album else None,
    }
    if not track.album or not track.album.artist:
        return fallback
    record = album_record or lookup_album_tracks(track.album.artist.name, track.album.title, track.album.musicbrainz_release_id)
    matches = record.get("tracks") or []
    selected = None
    if track.musicbrainz_recording_id:
        selected = next((candidate for candidate in matches if candidate.get("musicbrainz_recording_id") == track.musicbrainz_recording_id), None)
    if not selected and track.track_number is not None:
        selected = next((candidate for candidate in matches if candidate.get("track_number") == track.track_number), None)
    if not selected:
        selected = next((candidate for candidate in matches if normalized_music_name(candidate.get("title")) == normalized_music_name(track.title)), None)
    if not selected:
        return fallback
    return {
        **fallback,
        "artist": record.get("artist") or fallback["artist"],
        "albumartist": record.get("artist") or fallback["albumartist"],
        "album": record.get("album") or fallback["album"],
        "title": selected.get("title") or fallback["title"],
        "track_number": selected.get("track_number") or fallback["track_number"],
        "disc_number": selected.get("disc_number") or fallback["disc_number"],
        "duration_ms": selected.get("length") or fallback["duration_ms"],
        "musicbrainz_recording_id": selected.get("musicbrainz_recording_id") or fallback["musicbrainz_recording_id"],
        "musicbrainz_album_id": record.get("musicbrainz_album_id") or fallback["musicbrainz_album_id"],
    }


def musicbrainz_file_match(file_metadata: dict, expected: dict) -> dict:
    file_recording_id = normalized_music_name(file_metadata.get("musicbrainz_recording_id"))
    expected_recording_id = normalized_music_name(expected.get("musicbrainz_recording_id"))
    if file_recording_id and expected_recording_id:
        if file_recording_id == expected_recording_id:
            return {"matched": True, "score": 1.0}
        return {"matched": False, "score": 0.0, "message": "MusicBrainz recording ID does not match"}
    title_score = musicbrainz_text_score(file_metadata.get("title"), expected.get("title"))
    artist_score = musicbrainz_text_score(file_metadata.get("albumartist") or file_metadata.get("artist"), expected.get("albumartist") or expected.get("artist"))
    album_score = musicbrainz_text_score(file_metadata.get("album"), expected.get("album"))
    duration_score = musicbrainz_duration_score(file_metadata.get("duration_ms"), expected.get("duration_ms"))
    score = (title_score * 0.52) + (artist_score * 0.22) + (album_score * 0.10) + (duration_score * 0.16)
    if title_score < 0.78:
        return {"matched": False, "score": score, "message": "Title does not match MusicBrainz"}
    if duration_score < 0.45:
        return {"matched": False, "score": score, "message": "Duration does not match MusicBrainz"}
    if artist_score < 0.50 and album_score < 0.50:
        return {"matched": False, "score": score, "message": "Artist and album do not match MusicBrainz"}
    return {"matched": score >= 0.72, "score": score, "message": None if score >= 0.72 else "MusicBrainz confidence was too low"}


def musicbrainz_text_score(left: object, right: object) -> float:
    left_text = normalized_music_name(left)
    right_text = normalized_music_name(right)
    if not left_text or not right_text:
        return 0.5
    if left_text == right_text:
        return 1.0
    if left_text in right_text or right_text in left_text:
        return 0.94
    return SequenceMatcher(None, left_text, right_text).ratio()


def musicbrainz_duration_score(left: object, right: object) -> float:
    try:
        left_ms = int(left)
        right_ms = int(right)
    except (TypeError, ValueError):
        return 0.5
    if left_ms <= 0 or right_ms <= 0:
        return 0.5
    delta = abs(left_ms - right_ms)
    if delta <= 5000:
        return 1.0
    return max(0.0, 1.0 - (delta / max(left_ms, right_ms)) * 5)


def musicbrainz_metadata_changes(track: Track, metadata: dict) -> dict:
    changes = {}
    candidate_title = metadata.get("title")
    candidate_recording_id = metadata.get("musicbrainz_recording_id")
    if candidate_title and candidate_title != track.title:
        changes["title"] = candidate_title
    if candidate_recording_id and candidate_recording_id != track.musicbrainz_recording_id:
        changes["musicbrainz_recording_id"] = candidate_recording_id
    if not track.musicbrainz_verified:
        changes["musicbrainz_verified"] = True
    return changes


def musicbrainz_replacement_request(track: Track) -> dict:
    return {
        "action": "wishlist_request",
        "kind": "track",
        "artist": track.album.artist.name if track.album and track.album.artist else "Unknown Artist",
        "album": track.album.title if track.album else "Unknown Album",
        "track": track.title,
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "duration_ms": track.duration_ms,
        "musicbrainz_album_id": track.album.musicbrainz_release_id if track.album else None,
        "musicbrainz_recording_id": track.musicbrainz_recording_id,
        "replace_track_id": track.id,
        "replace_path": track.path,
        "require_lossless": True,
    }


def queue_musicbrainz_metadata_fixes(session: Session, results: list[dict]) -> ProposalBatch | None:
    fix_results = [result for result in results if result.get("changes")]
    if not fix_results:
        return None
    batch = ProposalBatch(
        title="MusicBrainz metadata fixes",
        kind=ProposalKind.metadata,
        tree_path="/library",
    )
    session.add(batch)
    session.flush()
    for result in fix_results:
        track = session.get(Track, result["track_id"])
        if not track:
            continue
        changes = {key: value for key, value in result["changes"].items() if key in editable_fields("track")}
        if not changes:
            continue
        old_values = {key: getattr(track, key, None) for key in changes}
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=track.title,
                kind=ProposalKind.metadata,
                old_value=json.dumps(old_values),
                new_value=json.dumps(changes),
                payload_json=json.dumps(
                    {
                        "target_type": "track",
                        "target_id": track.id,
                        "changes": changes,
                    }
                ),
            )
        )
    session.flush()
    return batch


def queue_musicbrainz_replacement_downloads(session: Session, results: list[dict]) -> ProposalBatch | None:
    replacement_results = [result for result in results if result.get("replacement_request")]
    if not replacement_results:
        return None
    batch = ProposalBatch(title="MusicBrainz replacement downloads", kind=ProposalKind.download, tree_path="/library")
    session.add(batch)
    session.flush()
    artist_items: dict[str, ProposalItem] = {}
    album_items: dict[tuple[str, str], ProposalItem] = {}
    for result in replacement_results:
        request = result["replacement_request"]
        artist = request.get("artist") or "Unknown Artist"
        album = request.get("album") or "Unknown Album"
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
                title=request.get("track") or result.get("title") or "Replacement download",
                kind=ProposalKind.download,
                old_value=request.get("replace_path"),
                payload_json=json.dumps(request),
            )
        )
    session.flush()
    return batch


_AUDIO_MEDIA_TYPES = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".wma": "audio/x-ms-wma",
}


def audio_media_type(path: Path) -> str:
    return _AUDIO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


@router.get("/library/tracks/{track_id}/stream", tags=["library"], summary="Stream track audio", response_class=FileResponse)
def stream_track(
    track_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.library_read not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    track = session.get(Track, track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    path = Path(track.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Track file is missing")
    # Python's mimetypes doesn't know several audio extensions (.flac, .m4a, .opus…),
    # so FileResponse would fall back to text/plain and some browsers refuse to play.
    return FileResponse(path, media_type=audio_media_type(path))


@router.get("/library/tracks/{track_id}/lyrics", tags=["library"], summary="Get track lyrics")
def get_track_lyrics(
    track_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> dict:
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.library_read not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    track = session.get(Track, track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    audio_path = Path(track.path)
    for ext in [".lrc", ".txt", ".lyrics"]:
        candidate = audio_path.with_suffix(ext)
        if candidate.exists():
            return {"lyrics": candidate.read_text(encoding="utf-8", errors="replace"), "format": ext.lstrip(".")}
    return {"lyrics": None, "format": None}


@router.get("/library/albums/{album_id}/cover", tags=["library"], summary="Get album cover art", response_class=FileResponse)
def album_cover(
    album_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.library_read not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    album = session.get(Album, album_id)
    if not album or not album.cover_path:
        raise HTTPException(status_code=404, detail="Album cover not found")
    path = Path(album.cover_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Album cover file is missing")
    library_root = get_settings().library_path.resolve()
    resolved = path.resolve()
    if library_root not in [resolved, *resolved.parents]:
        raise HTTPException(status_code=403, detail="Album cover is outside the library")
    return FileResponse(resolved)


@router.get("/library/albums/{album_id}/cover-candidates", tags=["library"], summary="Search album cover art sources", response_model=dict)
def album_cover_candidates(
    album_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    artist_name = album.artist.name if album.artist else ""
    try:
        results = search_album_releases(artist_name, album.title)
    except (RuntimeError, httpx.HTTPError):
        results = []
    urls = album_cover_candidate_urls(artist_name, album.title, results)
    return {"album_id": album.id, "urls": urls, "cover_path": urls[0] if urls else None}


@router.get("/library/artists/{artist_id}/cover", tags=["library"], summary="Get artist cover art", response_class=FileResponse)
def artist_cover(
    artist_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.library_read not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    artist = session.get(Artist, artist_id)
    if not artist or not artist.cover_path:
        raise HTTPException(status_code=404, detail="Artist cover not found")
    path = Path(artist.cover_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Artist cover file is missing")
    library_root = get_settings().library_path.resolve()
    resolved = path.resolve()
    if library_root not in [resolved, *resolved.parents]:
        raise HTTPException(status_code=403, detail="Artist cover is outside the library")
    return FileResponse(resolved)


@router.get("/library/artists/{artist_id}/cover-candidates", tags=["library"], summary="Search artist cover art sources", response_model=dict)
def artist_cover_candidates(
    artist_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    urls = artist_image_candidate_urls(artist.name)
    return {"artist_id": artist.id, "urls": urls, "cover_path": urls[0] if urls else None}


def _sniff_image_extension(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


@router.post("/library/artists/{artist_id}/cover", tags=["library"], summary="Upload artist cover art")
async def upload_artist_cover(
    artist_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    content = await file.read()
    ext = _sniff_image_extension(content)
    if not ext:
        raise HTTPException(status_code=400, detail="File is not a valid image (jpg, png, webp, gif, bmp)")
    folder = get_settings().library_path / safe_path_part(artist.name, "Unknown Artist")
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"cover{ext}"
    if artist.cover_path:
        old = Path(artist.cover_path)
        if old != destination and old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    artist.cover_path = str(destination)
    session.commit()
    write_app_log(f"Uploaded artist art for {artist.name}", source="upload", kind="artist_cover")
    return {"cover_path": str(destination)}


@router.post("/library/albums/{album_id}/cover", tags=["library"], summary="Upload album cover art")
async def upload_album_cover(
    album_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.metadata_edit)),
) -> dict:
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    content = await file.read()
    ext = _sniff_image_extension(content)
    if not ext:
        raise HTTPException(status_code=400, detail="File is not a valid image (jpg, png, webp, gif, bmp)")
    if album.path:
        folder = Path(album.path)
    else:
        folder = None
        for track in album.tracks:
            if track.path:
                folder = Path(track.path).parent
                break
        if folder is None:
            folder = get_settings().library_path / safe_path_part(album.artist.name, "Unknown Artist") / safe_path_part(album.title, "Unknown Album")
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"cover{ext}"
    if album.cover_path:
        old = Path(album.cover_path)
        if old != destination and old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    album.cover_path = str(destination)
    session.commit()
    write_app_log(f"Uploaded cover art for {album.artist.name} — {album.title}", source="upload", kind="album_cover")
    return {"cover_path": str(destination)}


@router.post("/imports/scan", tags=["imports"], summary="Scan staging directory for audio files", response_model=dict)
def scan_imports(
    payload: ImportScanRequest,
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        files = discover_import_files(payload.path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"files": files, "count": len(files)}


@router.get("/imports/existing", tags=["imports"], summary="List filenames already in the import folder", response_model=dict)
def list_import_existing(
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    import_root = get_settings().import_path
    names: list[str] = []
    if import_root.exists():
        names = [p.relative_to(import_root).as_posix() for p in import_root.rglob("*") if p.is_file()]
    return {"names": names, "count": len(names)}


@router.delete("/imports/files", tags=["imports"], summary="Delete all files in the import folder", response_model=dict)
def clear_import_files(
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    import_root = get_settings().import_path
    removed = 0
    if import_root.exists():
        # Deepest paths first so files go before the directories that hold them.
        for path in sorted(import_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                pass
    write_app_log(f"Import folder cleared: {removed} file(s) removed", source="upload", kind="import")
    return {"removed": removed}


@router.post("/imports/upload", tags=["imports"], summary="Upload audio files into the import folder")
async def upload_import_files(
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(default=[]),
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    import_root = get_settings().import_path
    import_root.mkdir(parents=True, exist_ok=True)
    root_resolved = import_root.resolve()
    write_app_log(f"Import upload started: {len(files)} file(s) received", source="upload", kind="import")
    saved: list[str] = []
    rejected: list[dict] = []
    for index, upload in enumerate(files):
        # Prefer the client-supplied relative path (folder uploads keep their structure),
        # falling back to the bare filename. Drop empty/./.. segments so a path can never
        # escape the import root.
        raw = paths[index] if index < len(paths) and paths[index] else (upload.filename or "")
        parts = [segment for segment in re.split(r"[\\/]+", raw) if segment not in ("", ".", "..")]
        name = parts[-1] if parts else ""
        rel_display = "/".join(parts) if parts else (upload.filename or "(unnamed)")
        if not name:
            rejected.append({"name": upload.filename or "(unnamed)", "reason": "missing filename"})
            write_app_log(f"Import upload rejected {upload.filename or '(unnamed)'}: missing filename", level="warning", source="upload", kind="import")
            continue
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_AUDIO_EXTENSIONS:
            rejected.append({"name": rel_display, "reason": f"unsupported type {ext or '(none)'}"})
            write_app_log(f"Import upload rejected {rel_display}: unsupported type {ext or '(none)'}", level="warning", source="upload", kind="import")
            continue
        target_dir = import_root.joinpath(*parts[:-1]) if len(parts) > 1 else import_root
        if root_resolved not in [target_dir.resolve(), *target_dir.resolve().parents]:
            rejected.append({"name": rel_display, "reason": "invalid path"})
            write_app_log(f"Import upload rejected {rel_display}: path escapes import root", level="warning", source="upload", kind="import")
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        content = await upload.read()
        destination = target_dir / name
        counter = 1
        while destination.exists():
            destination = target_dir / f"{Path(name).stem} ({counter}){ext}"
            counter += 1
        destination.write_bytes(content)
        try:
            parsed = MutagenFile(destination)
        except Exception:
            parsed = None
        if parsed is None:
            try:
                destination.unlink()
            except OSError:
                pass
            rejected.append({"name": name, "reason": "not a valid audio file"})
            write_app_log(f"Import upload rejected {name}: not a valid audio file", level="warning", source="upload", kind="import")
            continue
        saved.append(destination.name)
    write_app_log(f"Import upload complete: {len(saved)} saved, {len(rejected)} rejected", level="warning" if rejected else "info", source="upload", kind="import")
    return {"saved": saved, "rejected": rejected, "count": len(saved)}


@router.post("/imports/propose", response_model=TaskOut, tags=["imports"], summary="Enqueue import proposal")
def propose_import(
    payload: ImportScanRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_permission(Permission.import_run)),
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
    if payload.playlist_name and payload.playlist_original_tracks:
        import uuid as _uuid
        pending_key = f"pending_playlist:{_uuid.uuid4()}"
        setting = AppSetting(key=pending_key, value=json.dumps({
            "playlist_name": payload.playlist_name,
            "original_tracks": payload.playlist_original_tracks,
            "user_id": current_user.id,
            "retry_count": 0,
        }))
        session.add(setting)
        session.commit()
        write_app_log(f"Playlist import: stored pending playlist '{payload.playlist_name}' ({len(payload.playlist_original_tracks)} original tracks)")
    return serialize_task(task)


@router.post("/imports/musicbrainz-match", tags=["imports"], summary="Look up MusicBrainz recording by file metadata", response_model=dict)
def musicbrainz_match_import(
    payload: ImportMusicBrainzLookupRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.import_run)),
) -> dict:
    try:
        candidates = lookup_recording_by_musicbrainz_metadata(payload.file)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=lookup_error_detail("MusicBrainz", error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="MusicBrainz could not be reached from the server") from error
    return {"candidates": candidates}


@router.post("/imports/album-lookup", tags=["imports"], summary="Look up album tracks from MusicBrainz", response_model=dict)
def album_lookup(
    payload: AlbumLookupRequest,
    user: User = Depends(get_current_user),
) -> dict:
    require_album_lookup_access(user)
    try:
        return lookup_album_tracks(payload.artist, payload.album, payload.release_id)
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=lookup_error_detail("MusicBrainz", error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="MusicBrainz could not be reached from the server") from error


@router.post("/imports/album-search", tags=["imports"], summary="Search MusicBrainz for album releases", response_model=dict)
def album_search(
    payload: AlbumLookupRequest,
    user: User = Depends(get_current_user),
) -> dict:
    require_album_lookup_access(user)
    try:
        return {"results": search_album_releases(payload.artist, payload.album)}
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=lookup_error_detail("MusicBrainz", error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="MusicBrainz could not be reached from the server") from error


@router.post("/imports/playlist-url", response_model=PlaylistImportResponse, tags=["imports"], summary="Fetch track list from a public Spotify or Apple Music playlist URL")
def import_playlist_url(
    payload: PlaylistImportRequest,
    user: User = Depends(get_current_user),
) -> dict:
    require_album_lookup_access(user)
    return _scrape_playlist_url(payload.url)


def _spotify_get_token() -> str | None:
    """
    Returns a Spotify bearer token. Tries client credentials first (if configured),
    then falls back to the anonymous token the web player uses for public content.
    """
    import base64 as _base64

    settings = get_settings()
    if settings.spotify_client_id and settings.spotify_client_secret:
        creds = _base64.b64encode(
            f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
        ).decode()
        try:
            token_resp = httpx.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
                timeout=10,
            )
            token_resp.raise_for_status()
            return token_resp.json()["access_token"]
        except (httpx.HTTPError, KeyError) as exc:
            write_app_log(f"Spotify client-credentials token error: {exc}", level="warning")

    # Anonymous token — same endpoint the Spotify web player hits for public content
    try:
        anon_resp = httpx.get(
            "https://open.spotify.com/get_access_token",
            params={"reason": "transport", "productType": "web_player"},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://open.spotify.com/",
            },
            timeout=10,
            follow_redirects=True,
        )
        anon_resp.raise_for_status()
        return anon_resp.json().get("accessToken")
    except (httpx.HTTPError, KeyError) as exc:
        write_app_log(f"Spotify anonymous token error: {exc}", level="warning")
        return None


def _spotify_api_fetch(playlist_id: str) -> dict | None:
    """Fetch playlist via Spotify Web API. Returns None on failure."""
    access_token = _spotify_get_token()
    if not access_token:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = httpx.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}",
            headers=headers,
            params={"limit": 100},
            timeout=15,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        write_app_log(f"Spotify API error: {exc}", level="warning", playlist_id=playlist_id)
        return None

    data = resp.json()
    playlist_name: str | None = data.get("name")
    tracks: list[dict] = []

    tracks_page = data.get("tracks", {})
    while True:
        for item in tracks_page.get("items", []):
            track = item.get("track") if item else None
            if not track or track.get("type") != "track":
                continue
            title = track.get("name") or ""
            artists = track.get("artists") or []
            artist = artists[0].get("name", "") if artists else ""
            album = (track.get("album") or {}).get("name") or None
            if title:
                tracks.append({"title": title, "artist": artist, "album": album})
        next_url = tracks_page.get("next")
        if not next_url:
            break
        try:
            page_resp = httpx.get(next_url, headers=headers, timeout=15)
            page_resp.raise_for_status()
            tracks_page = page_resp.json()
        except httpx.HTTPError:
            break

    return {"name": playlist_name, "tracks": tracks}


def _scrape_playlist_url(url: str) -> dict:
    import json as _json
    import re as _re

    url_lower = url.lower()
    if "open.spotify.com" in url_lower or ("spotify.com" in url_lower and "/playlist/" in url_lower):
        source = "Spotify"
    elif "music.apple.com" in url_lower and "/playlist/" in url_lower:
        source = "Apple Music"
    else:
        raise HTTPException(status_code=400, detail="URL must be a Spotify (open.spotify.com/playlist/…) or Apple Music (music.apple.com/…/playlist/…) playlist link.")

    if source == "Spotify":
        m = _re.search(r"playlist/([A-Za-z0-9]+)", url)
        if m:
            # Try Spotify Web API first (when credentials configured)
            api_result = _spotify_api_fetch(m.group(1))
            if api_result is not None:
                if not api_result["tracks"]:
                    raise HTTPException(status_code=422, detail="Could not extract any tracks from this Spotify URL. Make sure the playlist is public and the link points directly to a playlist.")
                return {"source": source, "name": api_result["name"], "tracks": api_result["tracks"], "count": len(api_result["tracks"])}

            # Fall back to spotifyscraper (embed-page scraping, no credentials needed)
            try:
                from spotify_scraper import SpotifyClient as _SpotifyClient  # type: ignore[import]
                _sc = _SpotifyClient(log_level="WARNING")
                _pl = _sc.get_playlist_info(url)
                _raw = _pl.get("tracks") or []
                tracks: list[dict] = []
                playlist_name: str | None = _pl.get("name") or None
                for item in _raw:
                    t = item.get("track", item) if isinstance(item, dict) and "track" in item else item
                    if not t or not isinstance(t, dict):
                        continue
                    title = t.get("name") or ""
                    artists = t.get("artists") or []
                    artist = (artists[0].get("name") or "") if artists else ""
                    album = (t.get("album") or {}).get("name") or None
                    if title:
                        tracks.append({"title": title, "artist": artist, "album": album})
                if tracks:
                    return {"source": source, "name": playlist_name, "tracks": tracks, "count": len(tracks)}
                write_app_log("spotifyscraper returned no tracks", level="warning", url=url)
            except Exception as exc:
                write_app_log(f"spotifyscraper error: {exc}", level="warning", url=url)

        raise HTTPException(
            status_code=422,
            detail="Could not extract any tracks from this Spotify URL. Make sure the playlist is public and the link points directly to a playlist.",
        )

    # Apple Music: extract bearer token from page meta tag, then call catalog API
    _AM_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        page_resp = httpx.get(url, headers=_AM_HEADERS, timeout=20, follow_redirects=True)
        page_resp.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = f"Could not fetch Apple Music playlist page: HTTP {error.response.status_code}"
        write_app_log(detail, level="warning", url=url)
        raise HTTPException(status_code=502, detail=detail) from error
    except httpx.RequestError as error:
        detail = "Could not reach Apple Music: network error"
        write_app_log(detail, level="warning", url=url, error=str(error))
        raise HTTPException(status_code=502, detail=detail) from error

    # Extract the bearer token embedded in the page config meta tag
    from bs4 import BeautifulSoup as _BS
    from urllib.parse import unquote as _unquote
    soup = _BS(page_resp.text, "html.parser")
    meta = soup.find("meta", attrs={"name": "desktop-music-app/config/environment"})
    if not meta or not meta.get("content"):
        raise HTTPException(status_code=422, detail="Could not extract any tracks from this Apple Music URL. Make sure the playlist is public and the link points directly to a playlist.")
    try:
        config = _json.loads(_unquote(meta["content"]))
        bearer_token = config["MEDIA_API"]["token"]
    except (KeyError, _json.JSONDecodeError) as exc:
        write_app_log(f"Apple Music config parse error: {exc}", level="warning", url=url)
        raise HTTPException(status_code=422, detail="Could not extract any tracks from this Apple Music URL. Make sure the playlist is public and the link points directly to a playlist.") from exc

    # Parse country + playlist ID from URL: music.apple.com/{country}/playlist/{name}/{id}
    url_parts = url.rstrip("/").split("/")
    try:
        country = url_parts[3]
        playlist_id = url_parts[-1].split("?")[0]
    except IndexError:
        raise HTTPException(status_code=400, detail="Could not parse Apple Music playlist URL.")

    api_headers = {"Authorization": f"Bearer {bearer_token}", "Origin": "https://music.apple.com"}
    tracks: list[dict] = []
    playlist_name: str | None = None
    offset = 0
    limit = 100

    while True:
        try:
            api_resp = httpx.get(
                f"https://api.music.apple.com/v1/catalog/{country}/playlists/{playlist_id}/tracks",
                headers=api_headers,
                params={"limit": limit, "offset": offset},
                timeout=15,
            )
            api_resp.raise_for_status()
        except httpx.HTTPError as exc:
            write_app_log(f"Apple Music catalog API error: {exc}", level="warning", url=url)
            break
        page_data = api_resp.json()
        if playlist_name is None:
            # name lives on the playlist object, fetch it once
            try:
                pl_resp = httpx.get(
                    f"https://api.music.apple.com/v1/catalog/{country}/playlists/{playlist_id}",
                    headers=api_headers,
                    timeout=15,
                )
                pl_resp.raise_for_status()
                playlist_name = pl_resp.json()["data"][0]["attributes"].get("name")
            except Exception:
                pass
        for item in page_data.get("data", []):
            attrs = item.get("attributes") or {}
            title = attrs.get("name") or ""
            artist = attrs.get("artistName") or ""
            album = attrs.get("albumName") or None
            if title:
                tracks.append({"title": title, "artist": artist, "album": album})
        if page_data.get("next"):
            offset += limit
        else:
            break

    if not tracks:
        raise HTTPException(status_code=422, detail="Could not extract any tracks from this Apple Music URL. Make sure the playlist is public and the link points directly to a playlist.")

    return {"source": source, "name": playlist_name, "tracks": tracks, "count": len(tracks)}


@router.get("/discover/search", tags=["discover"], summary="Search music via iTunes")
def discover_search(
    q: str = Query(min_length=1, max_length=180),
    background_tasks: BackgroundTasks = None,
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> dict:
    write_app_log("Discover API search requested", feature="discover", query=q, user_id=user.id)
    try:
        payload = discover_music(q)
        write_app_log(
            "Discover API search returned",
            feature="discover",
            query=q,
            user_id=user.id,
            artists=len(payload.get("artists") or []),
            albums=len(payload.get("albums") or []),
            tracks=len(payload.get("tracks") or []),
        )
        return payload
    except httpx.HTTPStatusError as error:
        write_app_log("Discover API search failed: MusicBrainz status error", level="error", feature="discover", query=q, user_id=user.id, error=str(error))
        raise HTTPException(status_code=502, detail=lookup_error_detail("MusicBrainz", error)) from error
    except httpx.RequestError as error:
        write_app_log("Discover API search failed: MusicBrainz unreachable", level="error", feature="discover", query=q, user_id=user.id, error=str(error))
        raise HTTPException(status_code=503, detail="MusicBrainz could not be reached from the server") from error


@router.get("/discover/album-tracks/{album_id}", tags=["discover"], summary="Get tracks for an iTunes album", response_model=dict)
def discover_album_tracks(
    album_id: str,
    _: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> dict:
    tracks = itunes_album_tracks(album_id)
    return {"tracks": tracks}


@router.post("/discover/task-queue", response_model=TaskOut, tags=["discover"], summary="Add discovered tracks to download queue")
def discover_task_queue(
    payload: DiscoverTaskQueueRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.downloads_manage)),
) -> TaskOut:
    write_app_log("Discover task queue requested", feature="discover", user_id=user.id, downloads=len(payload.download_requests))
    task = enqueue_task(session, "propose_import", {"path": None, "files": [], "download_requests": payload.download_requests})
    write_app_log("Discover task queue created", feature="discover", user_id=user.id, task_id=task.id, downloads=len(payload.download_requests))
    return serialize_task(task)


@router.get("/wishlist", response_model=list[WishlistOut], tags=["wishlist"], summary="List wishlist")
def list_wishlist(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> list[WishlistOut]:
    reconcile_stale_approved_wishlist_items(session, user)
    query = select(WishlistItem).options(selectinload(WishlistItem.user))
    if not user_has_permission(user, Permission.wishlist_manage_all):
        query = query.where(WishlistItem.user_id == user.id)
    items = list(session.scalars(query.order_by(WishlistItem.created_at.desc())))
    expire_old_terminal_wishlist_items(session, items)
    items = [item for item in items if item.status != "removed" and not terminal_wishlist_expired(item)]
    downloading_ids = downloading_wishlist_ids(session)
    return [serialize_wishlist_item(item, downloading_ids) for item in items]


@router.post("/wishlist", response_model=WishlistOut, tags=["wishlist"], summary="Add to wishlist")
def create_wishlist_item(
    payload: WishlistCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> WishlistOut:
    write_app_log(
        "Wishlist add requested",
        feature=payload.source or "wishlist",
        user_id=user.id,
        kind=payload.kind,
        artist=payload.artist,
        album=payload.album,
        track=payload.track,
    )
    reconcile_stale_approved_wishlist_items(session, user)
    existing = session.scalar(
        select(WishlistItem)
        .where(WishlistItem.user_id == user.id)
        .where(WishlistItem.kind == payload.kind)
        .where(WishlistItem.artist == payload.artist)
        .where(WishlistItem.album == payload.album)
        .where(WishlistItem.track == payload.track)
        .where(WishlistItem.status.in_(["wanted", "review", "approved"]))
    )
    if existing:
        write_app_log(
            "Wishlist add reused existing item",
            feature=payload.source or "wishlist",
            user_id=user.id,
            item_id=existing.id,
            kind=payload.kind,
            artist=payload.artist,
            album=payload.album,
            track=payload.track,
        )
        return serialize_wishlist_item(existing)
    item = WishlistItem(user_id=user.id, **payload.model_dump(exclude={"source"}))
    item.status_changed_at = datetime.now(timezone.utc)
    session.add(item)
    session.commit()
    session.refresh(item)
    write_app_log(
        "Wishlist item created",
        feature=payload.source or "wishlist",
        user_id=user.id,
        item_id=item.id,
        kind=item.kind,
        artist=item.artist,
        album=item.album,
        track=item.track,
    )
    return serialize_wishlist_item(item)


@router.delete("/wishlist/{item_id}", response_model=WishlistOut, tags=["wishlist"], summary="Remove from wishlist")
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


@router.get("/wishlist/approvals", response_model=list[ProposalBatchOut], tags=["wishlist"], summary="Get wishlist items pending approval")
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


@router.post("/wishlist/approvals", response_model=ProposalBatchOut, tags=["wishlist"], summary="Approve or deny wishlist batch")
def propose_wishlist_items(
    payload: WishlistApprovalRequest | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.wishlist_manage_own)),
) -> ProposalBatchOut:
    reconcile_stale_approved_wishlist_items(session, user)
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
    album_lookup_cache: dict[tuple[str, str], dict | None] = {}
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
        # Expand an album-level wishlist entry into one download request per track so the
        # Soulseek per-track folder matcher can match each track against the found album
        # folder. Fall back to the single album-level request if MusicBrainz has no tracklist.
        track_payloads: list[dict] = []
        if wishlist_item.kind == "album" and not wishlist_item.track and wishlist_item.album:
            cache_key = (wishlist_item.artist, wishlist_item.album)
            if cache_key not in album_lookup_cache:
                try:
                    album_lookup_cache[cache_key] = lookup_album_tracks(wishlist_item.artist, wishlist_item.album)
                except Exception:
                    album_lookup_cache[cache_key] = None
            record = album_lookup_cache.get(cache_key)
            for track in (record or {}).get("tracks", []) or []:
                title = track.get("title")
                if not title:
                    continue
                track_payloads.append(
                    {
                        "action": "wishlist_request",
                        "kind": "track",
                        "artist": wishlist_item.artist,
                        "album": wishlist_item.album,
                        "track": title,
                        "track_number": track.get("track_number"),
                        "disc_number": track.get("disc_number"),
                        "duration_ms": track.get("length"),
                        "musicbrainz_album_id": track.get("musicbrainz_album_id") or (record or {}).get("musicbrainz_album_id"),
                        "musicbrainz_recording_id": track.get("musicbrainz_recording_id"),
                    }
                )
        if not track_payloads:
            track_payloads = [wishlist_download_payload(wishlist_item, album_lookup_cache)]
        for track_payload in track_payloads:
            session.add(
                ProposalItem(
                    batch_id=batch.id,
                    parent_id=album_items[album_key].id,
                    title=track_payload.get("track") or wishlist_item.album or wishlist_item.artist,
                    kind=ProposalKind.download,
                    payload_json=json.dumps(
                        track_payload | {"user_id": wishlist_item.user_id, "wishlist_item_id": wishlist_item.id}
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
    enqueue_task(session, "search_candidates", {"batch_id": batch.id})
    return serialize_batch(batch)


# ── Jellyfin-direct playlist helpers ──────────────────────────────────────────

def _jf_client(session: Session, user: User) -> "tuple[httpx.Client | None, str | None]":
    if not user.jellyfin_user_id:
        return None, None
    settings = integration_settings(session)
    url = settings.get("jellyfin_url", "").rstrip("/")
    key = settings.get("jellyfin_api_key", "")
    if not url or not key:
        return None, None
    return httpx.Client(base_url=url, headers={"X-Emby-Token": key}, timeout=10), user.jellyfin_user_id


def _build_playlist_out(pl_id: str, pl_name: str, items: list[dict], session: Session, *, protected: bool = False) -> FavoritesOut:
    jf_ids = [item["Id"] for item in items if item.get("Id")]
    tracks_by_jf_id: dict[str, Track] = {}
    if jf_ids:
        for track in session.scalars(
            select(Track).where(Track.jellyfin_item_id.in_(jf_ids)).options(selectinload(Track.album).selectinload(Album.artist))
        ):
            if track.jellyfin_item_id:
                tracks_by_jf_id[track.jellyfin_item_id] = track
    playlist_tracks: list[PlaylistTrackOut] = []
    track_ids: list[str] = []
    for i, item in enumerate(items):
        jf_id = item.get("Id", "")
        track = tracks_by_jf_id.get(jf_id)
        entry_id = item.get("PlaylistItemId") or jf_id
        if track:
            track_ids.append(track.id)
            artist_name = track.album.artist.name if track.album and track.album.artist else ""
            album_title = track.album.title if track.album else ""
            playlist_tracks.append(PlaylistTrackOut(
                id=entry_id,
                track_id=track.id,
                position=i + 1,
                title=item.get("Name") or track.title,
                artist=(item.get("Artists") or [artist_name])[0] if item.get("Artists") else artist_name,
                album=item.get("Album") or album_title,
                album_id=track.album_id,
                format=track.format,
            ))
    return FavoritesOut(id=pl_id, name=pl_name, protected=protected, track_ids=track_ids, tracks=playlist_tracks, track_count=len(items))


def _jf_favorites_out(session: Session, client: httpx.Client, jf_user_id: str) -> FavoritesOut:
    try:
        resp = client.get(f"/Users/{jf_user_id}/Items", params={"Filters": "IsFavorite", "IncludeItemTypes": "Audio", "Recursive": "true", "Limit": "500"})
        resp.raise_for_status()
        items = resp.json().get("Items", [])
    except Exception:
        items = []
    return _build_playlist_out("favorites", "Favorites", items, session, protected=True)


def _jf_playlist_out(session: Session, client: httpx.Client, jf_user_id: str, pl_id: str, pl_name: str) -> FavoritesOut:
    if not pl_name:
        try:
            nr = client.get(f"/Users/{jf_user_id}/Items/{pl_id}")
            if nr.is_success:
                pl_name = nr.json().get("Name", pl_id)
        except Exception:
            pl_name = pl_id
    try:
        resp = client.get(f"/Playlists/{pl_id}/Items", params={"userId": jf_user_id})
        resp.raise_for_status()
        items = resp.json().get("Items", [])
    except Exception:
        items = []
    return _build_playlist_out(pl_id, pl_name, items, session)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/playlists/favorites", response_model=FavoritesOut, tags=["playlists"], summary="Get Favorites")
def favorites_playlist(session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    client, jf_user_id = _jf_client(session, user)
    if not client:
        return FavoritesOut(id="favorites", name="Favorites", protected=True, track_ids=[], tracks=[], track_count=0)
    with client:
        return _jf_favorites_out(session, client, jf_user_id)


@router.get("/playlists", response_model=list[FavoritesOut], tags=["playlists"], summary="List all playlists")
def list_playlists(session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> list[FavoritesOut]:
    client, jf_user_id = _jf_client(session, user)
    if not client:
        return []
    with client:
        result: list[FavoritesOut] = [_jf_favorites_out(session, client, jf_user_id)]
        try:
            resp = client.get(f"/Users/{jf_user_id}/Items", params={"IncludeItemTypes": "Playlist", "Recursive": "true", "Limit": "1000"})
            resp.raise_for_status()
            for pl in resp.json().get("Items", []):
                pl_id, pl_name = pl.get("Id", ""), pl.get("Name", "")
                if pl_id and pl_name:
                    result.append(_jf_playlist_out(session, client, jf_user_id, pl_id, pl_name))
        except Exception:
            pass
        return result


@router.post("/playlists", response_model=FavoritesOut, tags=["playlists"], summary="Create playlist")
def create_playlist(payload: PlaylistCreate, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    client, jf_user_id = _jf_client(session, user)
    if not client:
        raise HTTPException(status_code=412, detail="Jellyfin not configured or no Jellyfin account linked")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    with client:
        try:
            resp = client.post("/Playlists", json={"Name": name, "UserId": jf_user_id, "MediaType": "Audio", "Ids": []})
            resp.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=error.response.status_code, detail=f"Jellyfin: {error.response.text}")
        pl_id = resp.json().get("Id") or resp.json().get("PlaylistId") or ""
        return _jf_playlist_out(session, client, jf_user_id, pl_id, name)


@router.patch("/playlists/{playlist_id}", response_model=FavoritesOut, tags=["playlists"], summary="Rename playlist")
def rename_playlist(playlist_id: str, payload: PlaylistUpdate, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    client, jf_user_id = _jf_client(session, user)
    if not client:
        raise HTTPException(status_code=412, detail="Jellyfin not configured or no Jellyfin account linked")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    with client:
        # Fetch the current item metadata, update Name, POST back in-place
        try:
            item_resp = client.get(f"/Users/{jf_user_id}/Items/{playlist_id}")
            item_resp.raise_for_status()
            item_data = item_resp.json()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=error.response.status_code, detail="Playlist not found in Jellyfin")
        item_data["Name"] = name
        try:
            update_resp = client.post(f"/Items/{playlist_id}", json=item_data)
            update_resp.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=error.response.status_code, detail=f"Jellyfin rename failed: {error.response.text}")
        return _jf_playlist_out(session, client, jf_user_id, playlist_id, name)


@router.delete("/playlists/{playlist_id}", tags=["playlists"], summary="Delete playlist")
def delete_playlist(playlist_id: str, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> dict:
    client, _ = _jf_client(session, user)
    if not client:
        return {}
    with client:
        client.delete(f"/Items/{playlist_id}")
    return {}


@router.post("/playlists/{playlist_id}/tracks", response_model=FavoritesOut, tags=["playlists"], summary="Add tracks to playlist")
def add_playlist_tracks(playlist_id: str, payload: PlaylistAddTracks, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    client, jf_user_id = _jf_client(session, user)
    if not client:
        raise HTTPException(status_code=412, detail="Jellyfin not configured or no Jellyfin account linked")
    tracks = list(session.scalars(select(Track).where(Track.id.in_(payload.track_ids))))
    with client:
        if playlist_id == "favorites":
            for track in tracks:
                if track.jellyfin_item_id:
                    try:
                        client.post(f"/Users/{jf_user_id}/FavoriteItems/{track.jellyfin_item_id}")
                    except Exception:
                        pass
            return _jf_favorites_out(session, client, jf_user_id)
        jf_ids = [t.jellyfin_item_id for t in tracks if t.jellyfin_item_id]
        if not jf_ids:
            raise HTTPException(status_code=400, detail="None of the selected tracks are in Jellyfin yet. Run a sync first.")
        try:
            client.post(f"/Playlists/{playlist_id}/Items", params={"ids": ",".join(jf_ids), "userId": jf_user_id}).raise_for_status()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=error.response.status_code, detail=f"Jellyfin: {error.response.text}")
        return _jf_playlist_out(session, client, jf_user_id, playlist_id, "")


@router.delete("/playlists/{playlist_id}/tracks/{track_id}", response_model=FavoritesOut, tags=["playlists"], summary="Remove track from playlist")
def remove_playlist_track(playlist_id: str, track_id: str, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    client, jf_user_id = _jf_client(session, user)
    if not client:
        raise HTTPException(status_code=412, detail="Jellyfin not configured or no Jellyfin account linked")
    track = session.get(Track, track_id)
    with client:
        if playlist_id == "favorites":
            if track and track.jellyfin_item_id:
                try:
                    client.delete(f"/Users/{jf_user_id}/FavoriteItems/{track.jellyfin_item_id}")
                except Exception:
                    pass
            return _jf_favorites_out(session, client, jf_user_id)
        if not track or not track.jellyfin_item_id:
            raise HTTPException(status_code=404, detail="Track not found in Jellyfin")
        try:
            ir = client.get(f"/Playlists/{playlist_id}/Items", params={"userId": jf_user_id})
            items = ir.json().get("Items", []) if ir.is_success else []
        except Exception:
            items = []
        entry_ids = [item["PlaylistItemId"] for item in items if item.get("Id") == track.jellyfin_item_id and item.get("PlaylistItemId")]
        if entry_ids:
            client.delete(f"/Playlists/{playlist_id}/Items", params={"EntryIds": ",".join(entry_ids)})
        return _jf_playlist_out(session, client, jf_user_id, playlist_id, "")


@router.post("/playlists/sync", response_model=TaskOut, tags=["playlists"], summary="Remap Nudibranch tracks to Jellyfin item IDs", description="Queues the track-mapping job, which is also triggered automatically after a Jellyfin library scan or track import. Only tracks not yet mapped are processed.")
def sync_playlists(session: Session = Depends(get_session), _: User = Depends(require_permission(Permission.playlists_manage))) -> TaskOut:
    return serialize_task(enqueue_task(session, "sync_favorites_jellyfin", {}))


@router.get("/playlists/sync/stats", response_model=PlaylistSyncStatsOut, tags=["playlists"], summary="Track remap job stats")
def playlist_sync_stats(session: Session = Depends(get_session), _: User = Depends(require_permission(Permission.playlists_manage))) -> dict:
    last_run_at = session.get(AppSetting, "mapping_last_run_at")
    run_count = session.get(AppSetting, "mapping_run_count")
    started_at = session.get(AppSetting, "mapping_started_at")
    return {
        "last_run_at": last_run_at.value if last_run_at else None,
        "run_count": int(run_count.value) if run_count else 0,
        "started_at": started_at.value if started_at else None,
    }


# ── (removed) proposal-based position reorder — position is order from Jellyfin ──

@router.post("/playlists/favorites/entries/{entry_id}/position", response_model=ProposalBatchOut, tags=["playlists"], summary="Reorder Favorites entry")
def propose_favorite_position(
    entry_id: str,
    payload: PlaylistPositionProposalRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> ProposalBatchOut:
    playlist = get_or_create_favorites(session, user.id)
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
        title=f"Update {playlist.name} order",
        kind=ProposalKind.playlist,
        tree_path=f"/playlists/{playlist.name}",
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


@router.post("/playlists/entries/{entry_id}/position", response_model=ProposalBatchOut, tags=["playlists"], summary="Reorder playlist entry")
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


@router.post("/tools/jellyfin-scan", response_model=TaskOut, tags=["tools"], summary="Trigger Jellyfin library scan")
def tool_jellyfin_scan(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.jellyfin_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "jellyfin_scan", {}))


@router.post("/tools/remap-tracks", response_model=TaskOut, tags=["tools"], summary="Remap Nudibranch tracks to Jellyfin item IDs")
def tool_remap_tracks(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.jellyfin_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "sync_favorites_jellyfin", {}))


@router.post("/tools/check-files", response_model=TaskOut, tags=["tools"], summary="Check library files for issues")
def tool_check_files(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_files", {}))


@router.post("/tools/check-duplicates", response_model=TaskOut, tags=["tools"], summary="Check for duplicate files")
def tool_check_duplicates(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_duplicates", {}))


@router.post("/tools/check-lyrics", response_model=TaskOut, tags=["tools"], summary="Check for missing lyrics")
def tool_check_lyrics(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_lyrics", {}))


@router.post("/tools/check-musicbrainz-ids", response_model=TaskOut, tags=["tools"], summary="Fill missing MusicBrainz IDs")
def tool_check_musicbrainz_ids(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_musicbrainz_ids", {}))


@router.post("/tools/check-audio-content", response_model=TaskOut, tags=["tools"], summary="Verify audio matches metadata")
def tool_check_audio_content(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_audio_content", {}))


@router.post("/tools/check-album-covers", response_model=TaskOut, tags=["tools"], summary="Check for missing album art")
def tool_check_album_covers(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_album_covers", {}))


@router.post("/tools/check-artist-covers", response_model=TaskOut, tags=["tools"], summary="Check for missing artist art")
def tool_check_artist_covers(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_artist_covers", {}))


@router.post("/tools/check-files/fix", response_model=ProposalBatchOut, tags=["tools"], summary="Apply file check fix")
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


@router.post("/tools/check-missing-tracks", response_model=TaskOut, tags=["tools"], summary="Check for missing tracks")
def tool_check_missing_tracks(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.downloads_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_missing_tracks", {}))


@router.post("/tools/check-non-lossless", response_model=TaskOut, tags=["tools"], summary="Check for non-lossless files")
def tool_check_non_lossless(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_non_lossless", {}))


@router.post("/tools/normalize-volume", response_model=TaskOut, tags=["tools"], summary="Normalize volume (ReplayGain)")
def tool_normalize_volume(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "normalize_volume", {}))


@router.post("/tools/consolidate-folders", response_model=TaskOut, tags=["tools"], summary="Consolidate album folders")
def tool_consolidate_folders(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "consolidate_folders", {}))


@router.post("/tools/clear-downloads", response_model=TaskOut, tags=["tools"], summary="Clear completed downloads")
def tool_clear_downloads(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.downloads_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "clear_downloads", {}))


@router.post("/tools/backup", response_model=TaskOut, tags=["tools"], summary="Create library backup")
def tool_backup(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "backup_now", {}))


@router.get("/tools/backups", tags=["tools"], summary="List available backups", response_model=dict)
def list_backups(
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> dict:
    settings = get_settings()
    settings.backups_path.mkdir(parents=True, exist_ok=True)
    backups = sorted(settings.backups_path.glob("nudibranch-*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)
    return {"backups": [{"path": str(path), "name": path.name, "size_bytes": path.stat().st_size} for path in backups]}


@router.post("/tools/restore-default", response_model=TaskOut, tags=["tools"], summary="Restore from latest backup")
def tool_restore_default(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.backups_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "restore_default", {}))


@router.post("/tools/restore-backup", response_model=TaskOut, tags=["tools"], summary="Restore from specific backup")
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


@router.post("/settings/youtube-cookies", response_model=IntegrationSettings, tags=["settings"], summary="Upload YouTube cookies file")
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
    result = update_integration_settings(session, values)
    session.commit()
    return _integration_settings_out(result)


@router.get("/approvals", response_model=list[ProposalBatchOut], tags=["approvals"], summary="List pending approval batches")
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


@router.post("/approvals/{batch_id}/selection", tags=["approvals"], summary="Update approval item selection", response_model=ProposalBatchOut)
def update_selection(
    batch_id: str,
    payload: ProposalSelectionUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.approvals_manage)),
) -> ProposalBatchOut:
    set_selection(session, batch_id, payload.item_ids, payload.selected)
    batch = session.scalar(select(ProposalBatch).options(selectinload(ProposalBatch.items)).where(ProposalBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return serialize_batch(batch)


@router.post("/approvals/{batch_id}/approve", response_model=TaskOut, tags=["approvals"], summary="Approve proposal batch")
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


@router.post("/approvals/{batch_id}/reject", tags=["approvals"], summary="Reject proposal items", response_model=ProposalBatchOut)
def reject(
    batch_id: str,
    payload: ProposalRejectRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.approvals_manage)),
) -> ProposalBatchOut:
    reject_items(session, batch_id, payload.item_ids, payload.suppress_for)
    batch = session.scalar(select(ProposalBatch).options(selectinload(ProposalBatch.items)).where(ProposalBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return serialize_batch(batch)


@router.get("/tasks", response_model=list[TaskOut], tags=["tasks"], summary="List background tasks")
def list_tasks(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.activity_read)),
) -> list[TaskOut]:
    tasks = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
    return [serialize_task(task) for task in tasks]


@router.get("/logs", response_model=list[LogEntryOut], tags=["tasks"], summary="Get application log")
def list_logs(
    limit: int = Query(500, ge=1, le=2000),
    _: User = Depends(require_permission(Permission.activity_read)),
) -> list[LogEntryOut]:
    return [serialize_log_entry(entry) for entry in tail_app_log(limit)]


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut, tags=["tasks"], summary="Cancel task")
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


@router.post("/tasks", response_model=TaskOut, tags=["tasks"], summary="Enqueue a task directly")
def create_task(
    payload: TaskCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, payload.type, payload.payload))


def _integration_settings_out(values: dict) -> IntegrationSettings:
    """Build the response model, flagging whether a cookies file is actually present."""
    cookies_path = values.get("youtube_cookies_path") or ""
    uploaded = bool(cookies_path) and Path(cookies_path).exists()
    return IntegrationSettings(**values, youtube_cookies_uploaded=uploaded)


@router.get("/settings/integrations", response_model=IntegrationSettings, tags=["settings"], summary="Get integration settings")
def get_integrations(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> IntegrationSettings:
    return _integration_settings_out(integration_settings(session))


@router.get("/settings/jellyfin-users", tags=["settings"], summary="List Jellyfin users available with the configured API key", response_model=list[JellyfinUserOut])
def list_jellyfin_users(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> list[JellyfinUserOut]:
    settings = integration_settings(session)
    url = settings.get("jellyfin_url", "").rstrip("/")
    key = settings.get("jellyfin_api_key", "")
    if not url or not key:
        return []
    try:
        response = httpx.get(f"{url}/Users", headers={"X-Emby-Token": key}, timeout=10)
        response.raise_for_status()
        return [{"id": u["Id"], "name": u["Name"]} for u in (response.json() or []) if u.get("Id") and u.get("Name")]
    except Exception:
        return []


@router.put("/settings/integrations", response_model=IntegrationSettings, tags=["settings"], summary="Update integration settings")
def update_integrations(
    payload: IntegrationSettings,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> IntegrationSettings:
    old_url = integration_settings(session).get("jellyfin_url", "")
    new_url = (payload.jellyfin_url or "").rstrip("/")
    update_integration_settings(session, payload.model_dump())
    if new_url and new_url != old_url.rstrip("/"):
        # Jellyfin URL changed — item IDs from the old server are invalid, clear them
        # so the next remap job rebuilds the mapping against the new server.
        session.query(Track).filter(Track.jellyfin_item_id.isnot(None)).update({"jellyfin_item_id": None})
        enqueue_task(session, "sync_favorites_jellyfin", {})
    session.commit()
    return _integration_settings_out(integration_settings(session))


@router.get("/notifications", response_model=list[NotificationOut], tags=["notifications"], summary="List notifications")
def list_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> list[NotificationOut]:
    query = select(Notification).where(
        (Notification.user_id == user.id)
        & (Notification.status != NotificationStatus.dismissed)
    )
    notifications = list(session.scalars(query.order_by(Notification.created_at.desc()).limit(100)))
    return [NotificationOut.model_validate(notification, from_attributes=True) for notification in notifications]


@router.get("/notifications/push-identity", tags=["notifications"], summary="This server's APNS push identity", response_model=PushIdentityResponse)
def get_push_identity(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PushIdentityResponse:
    """Identity the iOS app needs to authorise this server with the APNS proxy
    (App Attest grant flow): the server's instance_id, Ed25519 public key, and proxy URL."""
    return PushIdentityResponse(**push_identity(session))


@router.post("/notifications/devices", tags=["notifications"], summary="Register push notification device", response_model=dict)
def register_device(
    payload: DeviceRegistration,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    if not payload.proxy_grant and not payload.apns_token:
        raise HTTPException(status_code=400, detail="Provide apns_token (direct) or proxy_grant (proxy mode)")

    # Dedupe on whichever credential identifies the device for the active mode.
    if payload.proxy_grant:
        existing = session.scalar(
            select(MobileDevice).where(
                MobileDevice.user_id == user.id,
                MobileDevice.proxy_grant == payload.proxy_grant,
            )
        )
    else:
        existing = session.scalar(
            select(MobileDevice).where(
                MobileDevice.user_id == user.id,
                MobileDevice.apns_token == payload.apns_token,
            )
        )
    if existing:
        existing.device_name = payload.device_name
        existing.apns_token = payload.apns_token or existing.apns_token
        if payload.proxy_grant:
            existing.proxy_grant = payload.proxy_grant
        existing.enabled = True
        session.commit()
        return {"device_id": existing.id, "enabled": existing.enabled}
    device = MobileDevice(
        user_id=user.id,
        device_name=payload.device_name,
        apns_token=payload.apns_token,
        proxy_grant=payload.proxy_grant,
    )
    session.add(device)
    session.commit()
    return {"device_id": device.id, "enabled": device.enabled}


@router.delete("/notifications/devices/{device_id}", tags=["notifications"], summary="Deregister push notification device", response_model=dict)
def deregister_device(
    device_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    device = session.scalar(
        select(MobileDevice).where(MobileDevice.id == device_id, MobileDevice.user_id == user.id)
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    session.delete(device)
    session.commit()
    return {"ok": True}


@router.post("/notifications/read", tags=["notifications"], summary="Mark notifications as read", response_model=list[NotificationOut])
def mark_notifications_read(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> dict:
    notifications = list(session.scalars(select(Notification).where(Notification.user_id == user.id)))
    for notification in notifications:
        if notification.status == NotificationStatus.unread:
            notification.status = NotificationStatus.read
    session.commit()
    return {"updated": len(notifications)}


@router.delete("/notifications", tags=["notifications"], summary="Dismiss all notifications", response_model=dict)
def clear_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.notifications_read)),
) -> dict:
    notifications = list(session.scalars(select(Notification).where(Notification.user_id == user.id)))
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
            "musicbrainz_verified",
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


def wishlist_download_payload(item: WishlistItem, album_lookup_cache: dict[tuple[str, str], dict | None]) -> dict:
    payload = {
        "action": "wishlist_request",
        "kind": item.kind,
        "artist": item.artist,
        "album": item.album,
        "track": item.track,
    }
    if not item.track or not item.album:
        return payload
    cache_key = (item.artist, item.album)
    if cache_key not in album_lookup_cache:
        try:
            album_lookup_cache[cache_key] = lookup_album_tracks(item.artist, item.album)
        except Exception:
            album_lookup_cache[cache_key] = None
    record = album_lookup_cache.get(cache_key)
    if not record:
        return payload
    expected_title = normalized_music_name(item.track)
    for track in record.get("tracks", []):
        if normalized_music_name(track.get("title")) != expected_title:
            continue
        payload.update(
            {
                "track_number": track.get("track_number"),
                "disc_number": track.get("disc_number"),
                "duration_ms": track.get("length"),
                "musicbrainz_album_id": track.get("musicbrainz_album_id") or record.get("musicbrainz_album_id"),
                "musicbrainz_recording_id": track.get("musicbrainz_recording_id"),
            }
        )
        break
    return payload


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


def get_or_create_favorites(session: Session, user_id: str) -> Playlist:
    playlist = session.scalar(select(Playlist).where(Playlist.protected.is_(True), Playlist.user_id == user_id))
    if not playlist:
        playlist = session.scalar(select(Playlist).where(Playlist.name == "Favorites", Playlist.user_id == user_id))
    if not playlist:
        playlist = Playlist(name="Favorites", protected=True, user_id=user_id)
        session.add(playlist)
        session.flush()
    elif not playlist.protected:
        playlist.protected = True
        session.flush()
    return playlist


def set_app_setting(session: Session, key: str, value: str) -> None:
    setting = session.get(AppSetting, key)
    if not setting:
        setting = AppSetting(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
    session.flush()


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
            album_id=entry.track.album_id,
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
        username=user.username,
        is_admin=user.is_admin,
        permissions=effective_permission_values(user),
        theme=user.theme if user.theme in {"light", "dark"} else "light",
        accent_color=user.accent_color or "#356df3",
        background_tint=user.background_tint or "#356df3",
        crossfade_duration=user.crossfade_duration if user.crossfade_duration is not None else 1.0,
        search_min_confidence=user.search_min_confidence if user.search_min_confidence is not None else 0.4,
        library_page_size=user.library_page_size if user.library_page_size is not None else 100,
        jellyfin_user_id=user.jellyfin_user_id or None,
    )


def serialize_player_state(user: User) -> dict:
    state = user.player_state
    if not state:
        return {"user_id": user.id, "user_name": user.display_name, "status": "stopped", "source": "Nudibranch"}
    updated_at = state.updated_at
    if updated_at and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    stale = bool(updated_at and updated_at < datetime.now(timezone.utc) - timedelta(minutes=10))
    return {
        "user_id": user.id,
        "user_name": user.display_name,
        "track_id": state.track_id,
        "title": state.title,
        "artist": state.artist,
        "album": state.album,
        "status": "stopped" if stale else state.status,
        "source": "Nudibranch",
        "queue_length": state.queue_length,
        "current_index": state.current_index,
        "position_seconds": state.position_seconds,
        "duration_seconds": state.duration_seconds,
        "shuffle": state.shuffle,
        "repeat": state.repeat,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _resolve_target_label(session: Session, target_type: str | None, target_id: str | None) -> str | None:
    if not target_id:
        return None
    if target_type == "track":
        obj = session.get(Track, target_id)
        return obj.title if obj else None
    if target_type == "album":
        obj = session.get(Album, target_id)
        return obj.title if obj else None
    if target_type == "artist":
        obj = session.get(Artist, target_id)
        return obj.name if obj else None
    if target_type == "playlist":
        obj = session.get(Playlist, target_id)
        return obj.name if obj else None
    return None


def _serialize_command(cmd: PlaybackCommand) -> PlayerCommandOut:
    return PlayerCommandOut(
        id=cmd.id, action=cmd.action, target_type=cmd.target_type, target_id=cmd.target_id,
        target_label=cmd.target_label, loop=cmd.loop, shuffle=cmd.shuffle, status=cmd.status,
        device_id=cmd.device_id, created_at=cmd.created_at,
    )


def jellyfin_now_playing(session: Session) -> list[dict]:
    settings = integration_settings(session)
    jellyfin_url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    if not jellyfin_url or not api_key:
        return []
    try:
        response = httpx.get(f"{jellyfin_url}/Sessions", headers={"X-Emby-Token": api_key}, timeout=8)
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    sessions = []
    for item in response.json():
        now_playing = item.get("NowPlayingItem") or {}
        if not now_playing:
            continue
        sessions.append(
            {
                "user_name": item.get("UserName") or item.get("UserId") or "Jellyfin user",
                "client": item.get("Client"),
                "device_name": item.get("DeviceName"),
                "title": now_playing.get("Name"),
                "artist": ", ".join(now_playing.get("Artists") or []),
                "album": now_playing.get("Album"),
                "status": "playing" if not (item.get("PlayState") or {}).get("IsPaused") else "paused",
                "source": "Jellyfin",
            }
        )
    return sessions


def serialize_wishlist_item(item: WishlistItem, downloading_ids: set[str] | None = None) -> WishlistOut:
    status = item.status
    if status == "approved" and downloading_ids and item.id in downloading_ids:
        status = "downloading"
    return WishlistOut(
        id=item.id,
        user_id=item.user_id,
        owner_name=item.user.display_name if item.user else None,
        kind=item.kind,
        artist=item.artist,
        album=item.album,
        track=item.track,
        status=status,
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


def reconcile_stale_approved_wishlist_items(session: Session, user: User) -> None:
    query = select(WishlistItem).where(WishlistItem.status == "approved")
    if not user_has_permission(user, Permission.wishlist_manage_all):
        query = query.where(WishlistItem.user_id == user.id)
    approved_items = list(session.scalars(query))
    if not approved_items:
        return
    active_ids = active_wishlist_download_ids(session)
    changed = False
    now = datetime.now(timezone.utc)
    for item in approved_items:
        if item.id in active_ids:
            continue
        item.status = "wanted"
        item.status_changed_at = now
        changed = True
    if changed:
        session.commit()


def active_wishlist_download_ids(session: Session) -> set[str]:
    active_ids: set[str] = set()
    batches = list(
        session.scalars(
            select(ProposalBatch)
            .options(selectinload(ProposalBatch.items))
            .where(ProposalBatch.kind == ProposalKind.download)
            .where(ProposalBatch.tree_path.in_(["/task-queue", "/downloads"]))
            .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
        )
    )
    for batch in batches:
        for item in batch.items:
            if item.kind != ProposalKind.download or item.status in {ProposalStatus.completed, ProposalStatus.rejected, ProposalStatus.failed}:
                continue
            payload = json.loads(item.payload_json or "{}")
            if payload.get("action") != "queue_download" or payload.get("auto_retry_exhausted"):
                continue
            request = payload.get("request") or {}
            wishlist_item_id = request.get("wishlist_item_id") or payload.get("wishlist_item_id")
            if wishlist_item_id:
                active_ids.add(wishlist_item_id)
    return active_ids


def downloading_wishlist_ids(session: Session) -> set[str]:
    """Wishlist items whose linked Soulseek download is actively executing right now."""
    ids: set[str] = set()
    batches = list(
        session.scalars(
            select(ProposalBatch)
            .options(selectinload(ProposalBatch.items))
            .where(ProposalBatch.kind == ProposalKind.download)
            .where(ProposalBatch.status.in_([ProposalStatus.approved, ProposalStatus.executing]))
        )
    )
    for batch in batches:
        for item in batch.items:
            if item.kind != ProposalKind.download or item.status != ProposalStatus.executing:
                continue
            payload = json.loads(item.payload_json or "{}")
            request = payload.get("request") or {}
            wishlist_item_id = request.get("wishlist_item_id") or payload.get("wishlist_item_id")
            if wishlist_item_id:
                ids.add(wishlist_item_id)
    return ids


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


def require_album_lookup_access(user: User) -> None:
    allowed = {
        Permission.import_run,
        Permission.wishlist_manage_own,
        Permission.wishlist_manage_all,
        Permission.library_read,
        Permission.downloads_manage,
    }
    if user.is_admin or any(user_permission.permission in allowed for user_permission in user.permissions):
        return
    raise HTTPException(status_code=403, detail="Not enough permissions")


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
                payload_json=item.payload_json,
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
        created_at=as_utc(task.created_at),
        updated_at=as_utc(task.updated_at),
    )


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def serialize_log_entry(entry: dict) -> LogEntryOut:
    created_at = entry.get("created_at")
    if isinstance(created_at, str):
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except ValueError:
            parsed_created_at = datetime.now(timezone.utc)
    else:
        parsed_created_at = datetime.now(timezone.utc)
    return LogEntryOut(
        created_at=parsed_created_at,
        level=str(entry.get("level") or "info"),
        message=str(entry.get("message") or ""),
        context=entry.get("context") if isinstance(entry.get("context"), dict) else {},
    )
