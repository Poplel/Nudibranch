import base64
import os
import secrets
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from mutagen import File as MutagenFile

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
import httpx
from sqlalchemy import case, delete, func, literal, or_, select, update
from sqlalchemy.orm import Session, selectinload

from nudibranch.api.deps import SESSION_TTL, get_current_auth_session, get_current_user, require_admin, require_permission, resolve_media_user
from nudibranch.api.schemas import (
    CoverFromURLRequest,
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
    PinPodcastIn,
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
    PlaybackHandoffOut,
    PlaybackHandoffRejection,
    PlaybackQueueUpload,
    PlaybackSnapshot,
    PlaybackTransferOut,
    PlaybackEnqueueRequest,
    PlaybackTransferRequest,
    PlayerSessionOut,
    PlayerStateUpdate,
    PlaylistTrackOut,
    PlaylistImportRequest,
    PlaylistImportResponse,
    PlaylistShareOut,
    PlaylistShareRequest,
    PlaylistShareTargetOut,
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
    AdminSessionOut,
    SessionOut,
    StaticKeyCreate,
    StaticKeyOut,
    StaticKeyCreated,
    TaskCreate,
    TaskOut,
    UserCreate,
    UserAppearanceUpdate,
    HomeLayoutUpdate,
    HomeLayoutWebUpdate,
    JellyfinUserLinkUpdate,
    UserOut,
    OwnPinUpdate,
    UserPinUpdate,
    UserSearchSettingsUpdate,
    UserUpdate,
    WishlistApprovalRequest,
    WishlistCreate,
    WishlistOut,
    PodcastSubscribeIn,
    PodcastUpdateIn,
    PodcastOut,
    MarkEpisodesPlayedIn,
    PodcastSearchResult,
    EpisodeOut,
    EpisodeProgressOut,
    EpisodeProgressIn,
    PaginatedEpisodes,
    PodcastNotificationIn,
)
from nudibranch.db.models import (
    Album,
    AppSetting,
    Artist,
    Automation,
    AuthSession,
    Episode,
    EpisodeProgress,
    LIBRARY_DELETION_RETENTION,
    LibraryDeletion,
    MobileDevice,
    Notification,
    PlaybackCommand,
    NotificationStatus,
    Permission,
    Podcast,
    PodcastNotificationPref,
    PinnedItem,
    PinnedPlaylist,
    PlayEvent,
    Playlist,
    PlaylistCover,
    PlaylistShare,
    PlaylistTrack,
    PlaybackHandoff,
    ProposalBatch,
    ProposalItem,
    ProposalKind,
    ProposalStatus,
    SessionPlayerState,
    StaticApiKey,
    Task,
    Track,
    User,
    UserPermission,
    WishlistItem,
)
from nudibranch.core.config import get_settings
from nudibranch.db.session import get_session
from nudibranch.services.cover_images import square_cover_bytes
from nudibranch.services.auth import generate_token, hash_password, hash_token, token_prefix, verify_password
from nudibranch.services.imports import discover_import_files, read_audio_metadata, safe_path_part, SUPPORTED_AUDIO_EXTENSIONS
from nudibranch.services import podcasts as podcast_service
from nudibranch.services.app_log import tail_app_log, write_app_log
from nudibranch.services.itunes import album_tracks as itunes_album_tracks
from nudibranch.services.itunes import discover_music
from nudibranch.services.metadata_lookup import album_cover_candidate_urls, artist_image_candidate_urls, lookup_album_tracks, lookup_recording_by_musicbrainz_metadata, search_album_releases
from nudibranch.services.notifications import create_notification, push_identity
from nudibranch.services.proposals import approve_batch, reject_items, set_selection
from nudibranch.services.acoustid import audio_matches_claim
from nudibranch.services.match_tuning import match_tuning, match_tuning_schema, update_match_tuning
from nudibranch.services.settings_store import integration_settings, integration_value, update_integration_settings
from nudibranch.services.tasks import cancel_task, enqueue_task, task_result, task_to_payload
from nudibranch.services.search import rebuild_search_index, search_library
from nudibranch.services.automations import ACTION_TYPES, NOTIFY_MODES, NOTIFY_PRIORITIES, TRIGGER_TYPES, compute_next_run, run_automation

router = APIRouter(prefix="/api/v1")


PRESENCE_WINDOW = timedelta(seconds=60)

# Cross-device playback runs on TWO clocks, and collapsing them into one is the source of every
# ambiguity in this feature. `AuthSession.last_used_at` answers "is this app reachable?".
# `SessionPlayerState.reported_at` answers "is what it told us about playback still true?". They
# fail in opposite directions: a PAUSED BROWSER TAB keeps polling for commands but stops reporting
# status entirely, so it is reachable while its report goes stale — and it is the likeliest handoff
# target of all ("I'm at my desk, tab open, paused"). An iPHONE PLAYING DOWNLOADED FILES OFFLINE
# freezes both clocks: genuinely playing, genuinely unreachable.
#
# So: reported_at decides whether "playing" is believable. last_used_at decides whether a handoff
# can be delivered. Never substitute one for the other.

# Three missed 15s heartbeats. Tolerates one dropped request plus jitter, and bounds how long the
# server can claim "playing" after it stopped being true (crash, force-quit or network loss — a
# clean stop posts status=stopped explicitly) to ~45s. The rule this replaces for handoff purposes
# gave up only after 10 minutes, which is long enough to hand a queue to a laptop closed nine
# minutes ago.
LIVE_WINDOW = timedelta(seconds=45)

# ...but that reasoning is about a MOVING claim. "Playing" drifts the moment reporting stops: the
# position is wrong within seconds and the whole claim is wrong if the app died. "Paused" drifts
# nowhere — a session paused at 1:14 is still paused at 1:14 thirty minutes later, and the only
# thing a stale paused report can be wrong about is the app having gone away since — which is what
# the `last_used_at` check right below this window now also guards against: a paused session that
# has gone genuinely unreachable (force-quit, no network) stops being `live` at
# HANDOFF_REACHABLE_WINDOW even if its last report is still within this window.
PAUSED_LIVE_WINDOW = timedelta(minutes=30)

# Deliberately NOT PRESENCE_WINDOW. A hidden browser tab has its `setInterval` throttled to roughly
# once a minute, and `last_used_at` adds a >=30s bump throttle on top, so ~90s stale describes a
# perfectly healthy tab. The costs here are asymmetric: a false "reachable" is a soft failure (the
# handoff simply expires and the user retries), while a false "unreachable" hard-refuses a
# legitimate action. Asymmetric costs, asymmetric window.
HANDOFF_REACHABLE_WINDOW = timedelta(seconds=150)


# How long a handed-off queue stays adoptable. Not indefinite: a queue is a *now* object, and
# adopting a six-hour-old snapshot with autoplay is a resurrection rather than a handoff — audio
# starting on a device out of nowhere long after the user forgot pressing the button is the worst
# thing this feature could do. Not 30 seconds either: the whole point of the APNS nudge is reaching
# a BACKGROUNDED app, and those pushes are best-effort and can be deferred by minutes under Low
# Power Mode. Five minutes covers a realistically delayed wake while the user still remembers asking.
HANDOFF_TTL = timedelta(minutes=5)
#: How long a transport verb stays deliverable. Long enough to survive a backgrounded app or a brief
#: network blip, short enough that a device returning from a real absence is not driven by
#: instructions given while it was away.
COMMAND_TTL = timedelta(seconds=45)

# Past this, a target adopts the queue and position but starts PAUSED whatever autoplay said. Cheap
# safety valve against unattended audio; the server decides it so two clients cannot disagree.
HANDOFF_AUTOPLAY_DECAY = timedelta(seconds=60)

# A queue can be enormous — the web's "play library" really does page an entire library into its
# queue — so the snapshot is capped rather than trusted. Clients window their queue before sending;
# these are the backstop.
HANDOFF_MAX_ITEMS = 500
HANDOFF_MAX_PAYLOAD_BYTES = 128 * 1024


def _is_online(last_used_at: datetime | None) -> bool:
    if not last_used_at:
        return False
    ts = last_used_at if last_used_at.tzinfo is not None else last_used_at.replace(tzinfo=timezone.utc)
    return ts >= datetime.now(timezone.utc) - PRESENCE_WINDOW


def _session_presence(state: "SessionPlayerState | None", last_used_at: datetime | None) -> str:
    """Classify one session: `live`, `reachable`, or `unreachable`.

    ⚠ Derived at read time, and NOTHING may write it. Removing the worker's presence sweep — which
    stamped status="stopped" into these rows every 30 seconds forever, taking SQLite's single
    cross-process write lock to do it — is only sound while presence stays a projection. Never
    write a row to record what this function can compute.
    """
    now = datetime.now(timezone.utc)
    if state is not None and state.reported_at is not None:
        reported = as_utc(state.reported_at)
        if reported >= now - LIVE_WINDOW:
            return "live"
        # A paused session is believable for far longer, because there is nothing in the report
        # that can have drifted. See PAUSED_LIVE_WINDOW. Deliberately "paused" only: a stopped
        # session has nothing to resume, so claiming it is live buys nobody anything. Also gated on
        # the same reachability window as the "reachable" tier below: thirty minutes is long enough
        # that a paused session sitting on a phone with no network, or force-quit outright, should
        # stop presenting live transport controls well before the report itself goes stale.
        if (
            (state.status or "") == "paused"
            and reported >= now - PAUSED_LIVE_WINDOW
            and last_used_at is not None
            and as_utc(last_used_at) >= now - HANDOFF_REACHABLE_WINDOW
        ):
            return "live"
    if last_used_at is not None and as_utc(last_used_at) >= now - HANDOFF_REACHABLE_WINDOW:
        return "reachable"
    return "unreachable"


PERMISSION_SECTIONS = {
    Permission.library_view: "Library",
    Permission.library_edit: "Library",
    Permission.discover: "Discover & Wishlist",
    Permission.wishlist_approve_all: "Discover & Wishlist",
    Permission.import_run: "Import",
    Permission.approvals_manage: "Task Queue",
    Permission.playlists_manage: "Playlists",
    Permission.activity_read: "Activity",
    Permission.tools_manage: "Tools",
    Permission.automations_manage: "Automations",
    Permission.users_manage: "Users",
    Permission.settings_manage: "Settings",
}


# Failed-login throttle.  In-process and deliberately simple: the goal is to make online password
# guessing impractical, not to survive a restart or coordinate across replicas (there is one API
# container).  bcrypt already makes each attempt slow; this caps how many an attacker gets.
_LOGIN_FAILURE_LIMIT = 10
_LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
_login_failures: dict[str, list[datetime]] = {}


def _login_throttle_key(request: Request, username: str) -> str:
    # Behind a reverse proxy the socket peer is the proxy, so prefer the forwarded client.
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client = forwarded or (request.client.host if request.client else "?")
    return f"{client}|{username}"


def _check_login_throttle(key: str) -> None:
    now = datetime.now(timezone.utc)
    recent = [ts for ts in _login_failures.get(key, []) if now - ts < _LOGIN_FAILURE_WINDOW]
    _login_failures[key] = recent
    if len(recent) >= _LOGIN_FAILURE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Try again later.")


def _record_login_failure(key: str) -> None:
    _login_failures.setdefault(key, []).append(datetime.now(timezone.utc))
    # Bound the dict so a spray across many usernames can't grow it without limit.
    if len(_login_failures) > 5000:
        cutoff = datetime.now(timezone.utc) - _LOGIN_FAILURE_WINDOW
        for stale in [k for k, v in _login_failures.items() if not v or max(v) < cutoff]:
            _login_failures.pop(stale, None)


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    tags=["auth"],
    summary="Log in with username and password",
    responses={
        401: {"description": "Invalid username or password"},
        429: {"description": "Too many failed attempts for this client + username (10 per 15 minutes)"},
    },
)
def login(payload: LoginRequest, request: Request, session: Session = Depends(get_session)) -> LoginResponse:
    """Returns a session token in ``api_key``; send it as ``Authorization: Bearer <token>``.

    Send a stable ``device_label`` — sessions are deduped per label, so re-logging in from the same
    device renews one row instead of piling up.  Expiry slides 90 days on every authenticated
    request.  The response carries no permissions array; call ``GET /me`` for those.

    Any field in the body other than the three documented ones is ignored — in particular
    ``is_admin`` and ``permissions`` cannot be set here or anywhere else by the account itself.
    """
    username = payload.username.strip().lower()
    throttle_key = _login_throttle_key(request, username)
    _check_login_throttle(throttle_key)
    user = session.scalar(select(User).where(func.lower(User.username) == username))
    if not user or not verify_password(payload.password, user.pin_hash):
        _record_login_failure(throttle_key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _login_failures.pop(throttle_key, None)
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
    client_kind = payload.client if payload.client in {"ios", "mac", "web"} else None
    if existing:
        existing.token_hash = hash_token(token)
        existing.last_used_at = now
        existing.expires_at = expires
        # Only overwrite when the client says what it is: an older build omits the field, and
        # blanking a known identity would put that device back to an unlabelled row.
        if client_kind:
            existing.client = client_kind
    else:
        session.add(AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            device_label=label,
            client=client_kind,
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
        # ⚠ `session_player_states` CASCADEs, but `playback_commands.device_id` and
        # `playback_handoffs.to_session_id` are plain strings with no FK. Anything still addressed to
        # a revoked device can never be delivered, and a payload-carrying command is exempt from
        # COMMAND_TTL — so without this it stays `pending` forever and is served to any poll that
        # does not filter by `device_id`.
        session.execute(delete(PlaybackCommand).where(
            PlaybackCommand.user_id == user.id,
            PlaybackCommand.device_id == existing.id,
            PlaybackCommand.status == "pending",
        ))
        session.execute(delete(PlaybackHandoff).where(
            PlaybackHandoff.user_id == user.id,
            PlaybackHandoff.to_session_id == existing.id,
            PlaybackHandoff.status == "pending",
        ))
        session.delete(existing)
        session.commit()
    return {"ok": True}


@router.get("/sessions", response_model=list[AdminSessionOut], tags=["auth"], summary="List all users' sessions (admin/tools)")
def list_all_sessions(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> list[AdminSessionOut]:
    rows = session.scalars(
        select(AuthSession).options(selectinload(AuthSession.user)).order_by(AuthSession.last_used_at.desc())
    )
    return [
        AdminSessionOut(
            id=s.id,
            user_id=s.user_id,
            user_name=s.user.display_name,
            username=s.user.username,
            device_label=s.device_label,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
            online=_is_online(s.last_used_at),
        )
        for s in rows
    ]


@router.delete("/sessions/{session_id}", tags=["auth"], summary="Revoke any user's session (admin/tools)")
def revoke_any_session(
    session_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> dict:
    existing = session.scalar(select(AuthSession).where(AuthSession.id == session_id))
    if existing:
        session.delete(existing)
        session.commit()
    return {"ok": True}


@router.get("/me/api-keys", response_model=list[StaticKeyOut], tags=["auth"], summary="List my static API keys")
def list_api_keys(
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
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
    user: User = Depends(require_admin),
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
    user: User = Depends(require_admin),
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
    users = list(session.scalars(select(User).options(selectinload(User.permissions), selectinload(User.auth_sessions)).order_by(User.created_at.asc())))
    return [serialize_user(user) for user in users]


def _require_admin_grant(actor: User) -> None:
    """The admin flag is the one privilege users:manage may not hand out.

    Admins bypass every permission check, so letting a users:manage holder set is_admin made that
    permission silently equivalent to admin: they could promote themselves and gain settings:manage,
    tools:manage and the rest in one call.  Only an existing admin may create or confer admin.
    """
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can grant admin access")


def _require_admin_to_touch_admin(actor: User, target: User) -> None:
    """A users:manage holder may not modify an admin account.

    Without this they could simply reset the admin's password (or rename the account) and log in
    as them — the same escalation by a different door.
    """
    if target.is_admin and not actor.is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can modify an admin account")


@router.post(
    "/users",
    response_model=UserOut,
    tags=["users"],
    summary="Create user",
    responses={
        403: {"description": "Requires users:manage; creating an admin additionally requires the caller to be an admin"},
        409: {"description": "Username already taken"},
    },
)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    """Requires ``users:manage``.

    ``is_admin: true`` additionally requires the **caller** to be an admin. Admins bypass every
    permission check, so allowing a plain ``users:manage`` holder to confer it would make that
    permission equivalent to admin.
    """
    if payload.is_admin:
        _require_admin_grant(actor)
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


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    tags=["users"],
    summary="Update user",
    responses={
        400: {"description": "Would remove the last admin"},
        403: {"description": "Requires users:manage; see the description for the admin and self-edit rules"},
        404: {"description": "User not found"},
        409: {"description": "Username already taken"},
    },
)
def update_user(
    user_id: str,
    payload: UserUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    """Requires ``users:manage``, plus three rules that keep that permission from being a back door
    to admin:

    * Only an admin may change ``is_admin`` in either direction.
    * Only an admin may modify an account that is **already** an admin (otherwise a ``users:manage``
      holder could rename it or, via ``/users/{id}/pin``, take it over).
    * A non-admin may not change **their own** ``permissions``. They can still manage every other
      account, which is what the permission is for.

    A missing field means "leave alone", so clients that want to clear a user's permissions must
    send ``permissions: []`` explicitly rather than omitting the key.
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _require_admin_to_touch_admin(actor, user)
    if payload.is_admin is not None and payload.is_admin != user.is_admin:
        _require_admin_grant(actor)
    # Self-elevation is the whole attack: without this a users:manage holder could grant themselves
    # settings:manage/tools:manage/etc. one call at a time.  They may still manage other accounts.
    if user.id == actor.id and not actor.is_admin:
        if payload.permissions is not None and set(payload.permissions) != {
            up.permission.value for up in user.permissions
        }:
            raise HTTPException(status_code=403, detail="You cannot change your own permissions")
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


@router.post(
    "/users/{user_id}/pin",
    response_model=UserOut,
    tags=["users"],
    summary="Reset another user's password",
    responses={
        403: {"description": "Requires users:manage; refuses admin targets for non-admins, and refuses self"},
        404: {"description": "User not found"},
    },
)
def update_user_pin(
    user_id: str,
    payload: UserPinUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    """Administrative reset for **another** user — no current password needed.

    Refuses two cases: a non-admin resetting an admin's password (that was a direct path to taking
    over the admin account), and anyone resetting their **own** password here. Use ``POST /me/pin``
    for your own, which requires the current password.
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _require_admin_to_touch_admin(actor, user)
    # Changing your OWN password goes through /me/pin, which proves you know the current one.
    if user.id == actor.id:
        raise HTTPException(status_code=403, detail="Use /me/pin to change your own password")
    user.pin_hash = hash_password(payload.password)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.delete(
    "/users/{user_id}",
    status_code=204,
    tags=["users"],
    summary="Delete a user and all their data",
    responses={
        400: {"description": "Deleting yourself, or removing the last admin"},
        403: {"description": "Requires users:manage; only an admin may delete an admin"},
        404: {"description": "User not found"},
    },
)
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
    # Same admin boundary as PATCH /users/{id} and /users/{id}/pin: users:manage must not be able
    # to reach an admin account. Deleting one was the remaining unguarded path.
    _require_admin_to_touch_admin(current_user, user)
    if user.is_admin and count_admins(session) <= 1:
        raise HTTPException(status_code=400, detail="At least one admin user is required")
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
    # ORM cascade handles permissions, wishlists, auth_sessions (and their player state), api_keys.
    session.delete(user)
    session.commit()
    return Response(status_code=204)


@router.post(
    "/me/pin",
    response_model=UserOut,
    tags=["users"],
    summary="Change my own password (requires the current one)",
    responses={403: {"description": "Current password is incorrect"}},
)
def update_own_pin(
    payload: OwnPinUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    """Requires ``current_password`` alongside the new ``password``.

    A valid session token on its own is deliberately **not** enough: without re-authentication,
    anyone who obtained a token could change the password and lock the real owner out permanently.
    """
    # Re-authenticate: a session token alone must not be enough to change the password, or anyone
    # who gets hold of one can lock the real owner out permanently.
    if not verify_password(payload.current_password, user.pin_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    user.pin_hash = hash_password(payload.password)
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.put("/me/jellyfin-user", response_model=UserOut, tags=["users"], summary="Link or unlink a Jellyfin user account for playlist sync (requires users:manage, not self-service)")
def update_own_jellyfin_user(
    payload: JellyfinUserLinkUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.users_manage)),
) -> UserOut:
    previously_linked = bool(user.jellyfin_user_id)
    user.jellyfin_user_id = payload.jellyfin_user_id or None
    session.commit()
    session.refresh(user)
    # Newly linked (was unset, now set) — seed Jellyfin from the playlists this user already has,
    # which is what establishes the mirror. Unlinking needs no counterpart: the native rows stay
    # put and simply stop being mirrored.
    if not previously_linked and user.jellyfin_user_id:
        enqueue_task(session, "migrate_native_playlists_to_jellyfin", {"user_id": user.id})
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
    previously_linked = bool(target.jellyfin_user_id)
    target.jellyfin_user_id = payload.jellyfin_user_id or None
    session.commit()
    session.refresh(target)
    if not previously_linked and target.jellyfin_user_id:
        enqueue_task(session, "migrate_native_playlists_to_jellyfin", {"user_id": target.id})
    return serialize_user(target)


@router.put("/me/home-layout", response_model=UserOut, tags=["users"], summary="Update home screen arrangement")
def update_own_home_layout(
    payload: HomeLayoutUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    user.home_layout = json.dumps(payload.layout, sort_keys=True) if payload.layout else None
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.put("/me/home-layout-web", response_model=UserOut, tags=["users"], summary="Update web-only home screen arrangement")
def update_own_home_layout_web(
    payload: HomeLayoutWebUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserOut:
    user.home_layout_web = json.dumps(payload.layout, sort_keys=True) if payload.layout else None
    session.commit()
    return serialize_user(load_user(session, user.id))


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
    if payload.remote_playback_enabled is not None:
        user.remote_playback_enabled = payload.remote_playback_enabled
    session.commit()
    return serialize_user(load_user(session, user.id))


@router.post("/player/status", tags=["users"], summary="Update player state", response_model=dict)
def update_player_status(
    payload: PlayerStateUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> dict:
    # Player state is attributed to a device session, and a static API key is not a device — there
    # is nothing to attribute the report to. Accept it silently: a headless client posting status is
    # harmless, and failing the call would surface as a playback error on the client's side.
    if auth_session is None:
        return {"ok": True}
    state = session.get(SessionPlayerState, auth_session.id)
    if not state:
        state = SessionPlayerState(session_id=auth_session.id)
        session.add(state)
    state.user_id = user.id
    previous_status = state.status
    track = session.get(Track, payload.track_id) if payload.track_id else None
    episode = session.get(Episode, payload.episode_id) if (payload.episode_id and not track) else None
    state.track_id = track.id if track else (None if episode else payload.track_id)
    state.episode_id = episode.id if episode else None
    # ⚠ When the id RESOLVES, the library wins — the client's own strings are only a fallback for
    # something this server cannot look up.
    #
    # It used to be the other way round (`payload.title or track.title`), and that let a report be
    # internally inconsistent: rows were observed carrying one track's id and a different track's
    # title, because a client mid-crossfade has two elements loaded and can read its metadata from
    # one and its ids from the other. Every other session then displayed the wrong song. Deriving
    # from the id is what makes that unrepresentable rather than merely unlikely.
    if episode:
        podcast = episode.podcast
        state.title = episode.title or payload.title
        state.artist = (podcast.author or podcast.title if podcast else None) or payload.artist
        state.album = (podcast.title if podcast else None) or payload.album
    elif track:
        state.title = track.title
        state.artist = (track.album.artist.name if track.album and track.album.artist else None)
        state.album = (track.album.title if track.album else None)
    else:
        state.title = payload.title
        state.artist = payload.artist
        state.album = payload.album
    state.status = payload.status if payload.status in {"playing", "paused", "stopped"} else "stopped"
    state.queue_length = max(0, payload.queue_length)
    state.current_index = max(0, payload.current_index)
    state.position_seconds = payload.position_seconds
    # Same rule for duration, and this is the one users actually see go wrong. A client reports what
    # its media element says, and mid-crossfade that is the OUTGOING element — so a track would be
    # announced to every other session with a duration that belonged to a different song, or with a
    # partial one while the next element was still loading. The library knows how long the track is;
    # the element's figure is only used for something the server cannot resolve.
    library_ms = (track.duration_ms if track else None) or (episode.duration_ms if episode else None)
    state.duration_seconds = (
        round(library_ms / 1000) if library_ms else payload.duration_seconds
    )
    state.shuffle = bool(payload.shuffle)
    state.repeat = payload.repeat if payload.repeat in {"off", "one", "all"} else "off"
    if payload.client:
        state.client = payload.client
    now = datetime.now(timezone.utc)
    if state.status == "playing" and previous_status != "playing":
        state.playback_started_at = now
    state.reported_at = now
    state.updated_at = now
    session.commit()
    if state.status == "playing":
        _resolve_playback_ownership(session, user)
    # The hash handshake: the client is the authority on its own queue and never reads this copy back
    # to play from. It sends what its queue currently hashes to, and only uploads the queue itself
    # when the server says the stored copy disagrees — so a queue that plays for an hour unchanged is
    # never re-sent, while a reordered one is picked up on the next heartbeat.
    queue_stale = bool(payload.queue_hash) and payload.queue_hash != state.queue_hash
    return {"ok": True, "queue_stale": queue_stale}


def _resolve_playback_ownership(session: Session, user: User) -> None:
    """One account plays in one place: whoever STARTED most recently owns it, and the rest are stopped.

    ⚠ The tie is broken by `playback_started_at`, never by who reported most recently. A device that
    loses its network keeps playing and stops reporting, so "most recent report" would hand the
    session to whichever device merely stayed reachable — and then, the moment the real one came
    back, it would be stopped by a decision made while it was away. Ownership follows the audio.

    The consequences that follow, and that this is written to produce:
      • playing, goes offline, another starts → the other started later, so it takes over;
      • playing, goes offline, comes back with nothing else started → still the latest start, so it
        keeps the session and nothing interrupts it;
      • two sessions both playing → the later start wins, whichever of them is reporting right now.

    ⚠ Rows are not rewritten on a device's behalf; only a stop command is sent. A row records what a
    device said about itself, and an offline device is still playing whatever its row last claimed.
    """
    playing = session.scalars(
        select(SessionPlayerState).where(
            SessionPlayerState.user_id == user.id,
            SessionPlayerState.status == "playing",
        )
    ).all()
    if len(playing) < 2:
        return
    # A row with no recorded start predates this column; treat it as the oldest possible claim.
    def started(row: SessionPlayerState) -> datetime:
        return as_utc(row.playback_started_at) or datetime.min.replace(tzinfo=timezone.utc)

    owner = max(playing, key=started)
    losers = [row for row in playing if row.session_id != owner.session_id]
    # ⚠ Only tell a device once. This runs on every report from the owner — several times a minute
    # while it plays — and a loser that is OFFLINE never acts on the stop or updates its row, so
    # without this it would collect one stop command and one push every few seconds for as long as
    # it stayed away.
    already_told = set(session.scalars(
        select(PlaybackCommand.device_id).where(
            PlaybackCommand.user_id == user.id,
            PlaybackCommand.status == "pending",
            PlaybackCommand.action == "stop",
        )
    ).all())
    losers = [row for row in losers if row.session_id not in already_told]
    if not losers:
        return
    for other in losers:
        session.add(PlaybackCommand(
            user_id=user.id,
            device_id=other.session_id,
            action="stop",
            status="pending",
        ))
    session.commit()
    for other in losers:
        try:
            create_notification(
                session,
                title="Playback moved",
                body="Continuing on another device",
                event_type="remote_playback_command",
                target_url="/player",
                user_id=user.id,
                deliver_apns=True,
                deliver_web=False,
                device_id=apns_device_for_session(session, user.id, other.session_id),
            )
        except Exception:  # noqa: BLE001 - the stop stands even if the wake cannot be sent.
            pass


def apns_device_for_session(session: Session, user_id: str, session_id: str | None) -> str | None:
    """Translate a playback target (an `AuthSession` id) into an APNS target (a `MobileDevice` id).

    Playback is addressed by session, but push registrations have their own ids, so the two are
    paired through the stable per-install label that both login and push registration write.

    ⚠ When the session has no push registration this returns the SESSION id unchanged, on purpose:
    an intentionally unmatched scope means the nudge is recorded as handled rather than broadcast to
    a different phone. Returning None would widen it to every device the user owns.

    This is the one copy for the API. `services/automations.py:_nudge_device` is a second, older one
    for the scheduler; do not add a third.
    """
    if not session_id:
        return None
    target_session = session.scalar(
        select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user_id)
    )
    if target_session and target_session.device_label:
        mobile_target = session.scalar(
            select(MobileDevice)
            .where(
                MobileDevice.user_id == user_id,
                MobileDevice.device_name == target_session.device_label,
                MobileDevice.enabled.is_(True),
            )
            .order_by(MobileDevice.created_at.desc())
        )
        if mobile_target:
            return mobile_target.id
    return session_id


@router.get(
    "/player/sessions",
    response_model=list[PlayerSessionOut],
    tags=["users"],
    summary="List my sessions and what each one is playing",
)
def list_player_sessions(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> list[PlayerSessionOut]:
    """The caller's OWN sessions — what is playing where, and which can be handed a queue.

    Gated on authentication alone, deliberately: `activity:read` gates seeing *other people's*
    playback (`/users/playback`), which is a different question. Refusing a user their own devices
    would make cross-device playback an admin feature.

    One read, no writes: presence is computed from the rows rather than recorded into them.

    ⚠ With cross-device playback switched off (`user.remote_playback_enabled`) this returns ONLY the
    caller's own session. Gating it here rather than only in the UI is what makes "local only" true:
    hidden-but-listed devices would still be offered as handoff targets by any other client, and the
    user would still appear as a target on their own other devices.
    """
    query = (
        select(AuthSession, SessionPlayerState)
        .outerjoin(SessionPlayerState, SessionPlayerState.session_id == AuthSession.id)
        .where(AuthSession.user_id == user.id)
    )
    if not bool(getattr(user, "remote_playback_enabled", True)):
        query = query.where(AuthSession.id == (auth_session.id if auth_session else ""))
    rows = list(session.execute(query))

    out: list[PlayerSessionOut] = []
    for auth_row, state in rows:
        presence = _session_presence(state, auth_row.last_used_at)
        if state is None:
            status_value = "unknown" if presence == "unreachable" else "stopped"
        elif presence == "live":
            status_value = state.status
        elif presence == "reachable":
            status_value = "stopped"
        else:
            status_value = "unknown"
        # The podcast id is what a client needs to rehydrate an episode from its own store, and it
        # cannot be derived from the synthetic track record an episode plays as.
        podcast_id = state.episode.podcast_id if (state is not None and state.episode) else None
        # Cover art is addressed by album id; the title is not one.
        album_id = state.track.album_id if (state is not None and state.track) else None
        out.append(
            PlayerSessionOut(
                session_id=auth_row.id,
                device_label=auth_row.device_label,
                # The session's own record wins: it is set at login, so a device that has never
                # played still identifies itself correctly.
                client=auth_row.client or (state.client if state else None),
                current=(auth_session is not None and auth_row.id == auth_session.id),
                presence=presence,
                status=status_value,
                track_id=state.track_id if state else None,
                episode_id=state.episode_id if state else None,
                podcast_id=podcast_id,
                album_id=album_id,
                title=state.title if state else None,
                artist=state.artist if state else None,
                album=state.album if state else None,
                queue_length=state.queue_length if state else 0,
                current_index=state.current_index if state else 0,
                position_seconds=state.position_seconds if state else None,
                duration_seconds=state.duration_seconds if state else None,
                shuffle=state.shuffle if state else False,
                repeat=state.repeat if state else "off",
                # ⚠ Normalised to aware UTC. SQLite hands these back naive, and a naive timestamp
                # serialises without an offset — which a strict ISO8601 client decoder rejects,
                # taking the whole response with it (see the podcast timestamp bug).
                reported_at=as_utc(state.reported_at) if (state and state.reported_at) else None,
                last_used_at=as_utc(auth_row.last_used_at) if auth_row.last_used_at else None,
            )
        )
    # Most recently described first, so a client can take the head as "the other session".
    out.sort(key=lambda row: as_utc(row.reported_at) if row.reported_at else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


@router.post(
    "/player/commands",
    response_model=PlayerCommandOut,
    tags=["users"],
    summary="Queue a remote playback command",
    responses={403: {"description": "Requires library:view when the command names a target"}},
)
def create_player_command(
    payload: PlayerCommandCreate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlayerCommandOut:
    action = (payload.action or "play").strip().lower()
    # adopt_handoff carries a queue and is only ever minted inside /player/transfer, which validates
    # the snapshot and the target's reachability. Accepting it here would let a caller point a device
    # at an arbitrary handoff id.
    if action == "adopt_handoff":
        raise HTTPException(status_code=400, detail="Use POST /player/transfer to move playback")
    # Same reasoning: these name a handoff row holding a queue, and only /player/enqueue mints one
    # after validating the items and the target.
    if action in {"enqueue_next", "enqueue_end"}:
        raise HTTPException(status_code=400, detail="Use POST /player/enqueue to add to a queue")
    target_type = payload.target_type
    target_id = payload.target_id
    target_label = None
    # Transport-only commands (pause/next/…) need no library rights, but anything naming a target
    # resolves it against the library and echoes its title back — which made this a free library
    # search for an account with no library:view.
    if (target_id or payload.target_query) and not (
        user.is_admin or any(up.permission == Permission.library_view for up in user.permissions)
    ):
        raise HTTPException(status_code=403, detail="Requires library:view")
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
        position_seconds=payload.position_seconds if action == "seek" else None,
        queue_index=payload.queue_index if action in _QUEUE_ACTIONS else None,
        queue_to_index=payload.queue_to_index if action == "move" else None,
        status="pending",
    )
    session.add(command)
    session.commit()
    session.refresh(command)
    try:
        body = f"Play {target_label}" if action == "play" and target_label else action.capitalize()
        apns_target_id = apns_device_for_session(session, user.id, payload.device_id)
        # A remote playback command is a wake nudge for the device that should play, not tray
        # content: deliver_web=False (no tray entry), and when a specific device is targeted the
        # APNS push goes ONLY to that device. Untargeted commands
        # (device_id None) still nudge all the user's devices so one picks it up.
        create_notification(
            session,
            title="Remote playback",
            body=body,
            event_type="remote_playback_command",
            target_url="/player",
            user_id=user.id,
            deliver_apns=True,
            deliver_web=False,
            device_id=apns_target_id,
        )
    except Exception:
        pass
    return _serialize_command(command)


def _require_session(auth_session: AuthSession | None) -> AuthSession:
    if auth_session is None:
        raise HTTPException(status_code=400, detail="This endpoint requires a device session")
    return auth_session


def _handoff_or_404(session: Session, handoff_id: str, user_id: str) -> PlaybackHandoff:
    row = session.scalar(
        select(PlaybackHandoff).where(PlaybackHandoff.id == handoff_id, PlaybackHandoff.user_id == user_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Handoff not found")
    return row


@router.post(
    "/player/queue",
    tags=["users"],
    summary="Publish this session's queue so another device can move it",
)
def publish_player_queue(
    payload: PlaybackQueueUpload,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> dict:
    """Store the caller's queue so a THIRD device can move it somewhere.

    ⚠ This is a copy for other devices to act on, never a source of truth the owner reads back. The
    owning client stays local-first: it plays from its own queue and re-publishes when that queue
    changes, which the hash on `POST /player/status` is what detects.
    """
    origin = _require_session(auth_session)
    snapshot = payload.snapshot
    if len(snapshot.items) > HANDOFF_MAX_ITEMS:
        raise HTTPException(status_code=413, detail=f"Queue exceeds {HANDOFF_MAX_ITEMS} items")
    encoded = snapshot.model_dump_json()
    if len(encoded.encode("utf-8")) > HANDOFF_MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Queue is too large")
    state = session.get(SessionPlayerState, origin.id)
    if not state:
        state = SessionPlayerState(session_id=origin.id, user_id=user.id)
        session.add(state)
    now = datetime.now(timezone.utc)
    state.user_id = user.id
    state.queue_json = encoded
    state.queue_hash = payload.hash[:64]
    state.queue_updated_at = now
    session.commit()
    return {"ok": True}


@router.post(
    "/player/enqueue",
    response_model=PlaybackTransferOut,
    tags=["users"],
    summary="Add tracks to what another of my sessions is playing",
    responses={
        400: {"description": "Not a different session of yours"},
        409: {"description": "The target session cannot be reached"},
        413: {"description": "Too many items"},
    },
)
def enqueue_on_session(
    payload: PlaybackEnqueueRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackTransferOut:
    """Queue tracks onto the session that is actually playing, instead of starting a second player.

    ⚠ The payload rides on `playback_handoffs`, NOT on the command row. `playback_commands` is
    SELECTed every few seconds by every open client, and a queue of ids is large enough that putting
    it there would make SQLite pull overflow pages on every poll for rows that are almost all
    transport verbs. This reuses the side table a transfer already uses, and the same
    `GET /player/handoffs/{id}` collects it.

    The mode is carried by the ACTION (`enqueue_next` / `enqueue_end`) rather than by overloading
    `target_type`, which already means "what kind of thing is target_id".
    """
    if not bool(getattr(user, "remote_playback_enabled", True)):
        # "Local only" has to be refused server-side, or one client with the toggle off is still
        # reachable from every other one.
        raise HTTPException(status_code=409, detail="Cross-device playback is turned off")
    mode = (payload.mode or "end").strip().lower()
    if mode not in {"next", "end"}:
        raise HTTPException(status_code=400, detail="mode must be 'next' or 'end'")
    target = session.get(AuthSession, payload.to_session_id)
    if not target or target.user_id != user.id:
        raise HTTPException(status_code=400, detail="Unknown session")
    # ⚠ `_session_presence` takes (state, last_used_at) and returns a plain string. Calling it with
    # the DB session and unpacking a pair out of it is what made every enqueue a 500.
    target_state = session.get(SessionPlayerState, target.id)
    presence = _session_presence(target_state, target.last_used_at)
    if presence == "unreachable":
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "device_unreachable",
                "presence": presence,
                "device_label": target.device_label,
                "last_seen_at": as_utc(target.last_used_at).isoformat() if target.last_used_at else None,
                "last_seen_status": target_state.status if target_state else None,
            },
        )
    snapshot = payload.snapshot
    if not snapshot.items:
        raise HTTPException(status_code=400, detail="Nothing to queue")
    if len(snapshot.items) > HANDOFF_MAX_ITEMS:
        raise HTTPException(status_code=413, detail=f"Queue exceeds {HANDOFF_MAX_ITEMS} items")
    encoded = snapshot.model_dump_json()
    if len(encoded.encode("utf-8")) > HANDOFF_MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Queue is too large")

    now = datetime.now(timezone.utc)
    handoff = PlaybackHandoff(
        user_id=user.id,
        from_session_id=None,
        to_session_id=target.id,
        payload_json=encoded,
        item_count=len(snapshot.items),
        autoplay=False,
        status="pending",
        created_at=now,
        expires_at=now + HANDOFF_TTL,
    )
    session.add(handoff)
    session.flush()
    # ⚠ No loop/shuffle here. Both clients apply those two unconditionally on every command they
    # receive, so sending them would silently change the far end's playback mode as a side effect of
    # adding a song to its queue.
    command = PlaybackCommand(
        user_id=user.id,
        device_id=target.id,
        action=f"enqueue_{mode}",
        target_type="handoff",
        target_id=handoff.id,
        loop=(target_state.repeat if target_state else "off"),
        shuffle=(target_state.shuffle if target_state else False),
        status="pending",
    )
    session.add(command)
    session.flush()
    handoff.command_id = command.id
    session.commit()
    session.refresh(handoff)
    # Same silent wake the transfer uses, so a backgrounded target collects this without waiting for
    # its next foreground poll. The queue addition stands even if the nudge cannot be delivered.
    try:
        create_notification(
            session,
            title="Queue updated",
            body=f"Added to {target.device_label or 'this device'}",
            event_type="remote_playback_command",
            target_url="/player",
            user_id=user.id,
            deliver_apns=True,
            deliver_web=False,
            device_id=apns_device_for_session(session, user.id, target.id),
        )
    except Exception:  # noqa: BLE001
        pass
    return PlaybackTransferOut(
        id=handoff.id,
        status=handoff.status,
        expires_at=as_utc(handoff.expires_at),
        item_count=handoff.item_count,
        to_device_label=target.device_label,
    )


@router.get(
    "/player/sessions/{session_id}/queue",
    response_model=PlaybackSnapshot,
    tags=["users"],
    summary="Read what another of my sessions has queued",
)
def read_player_session_queue(
    session_id: str,
    resolve: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlaybackSnapshot:
    """The queue a sibling session published, so a remote player can list it like a local one.

    ⚠ Scoped to the caller's OWN sessions by `user_id`, not just by session id: session ids are
    opaque but guessable in principle, and a queue is a description of what someone is listening to.

    This is deliberately a read of the same copy `/player/transfer` moves, rather than a second
    store.

    `resolve=true` fills in the titles. It exists for the WEB client, which has no local library
    mirror and so cannot turn ids into a list anyone can read; the apps leave it off and resolve
    against their own mirror, which is both faster and works offline. One indexed query for the
    whole queue either way — never one per item.
    """
    state = session.get(SessionPlayerState, session_id)
    if not state or state.user_id != user.id or not state.queue_json:
        raise HTTPException(status_code=404, detail="No queue published for that session")
    snapshot = PlaybackSnapshot.model_validate_json(state.queue_json)
    if not resolve:
        return snapshot

    track_ids = [item.id for item in snapshot.items if item.type != "episode"]
    episode_ids = [item.id for item in snapshot.items if item.type == "episode"]
    tracks = {
        row.id: row
        for row in session.scalars(select(Track).where(Track.id.in_(track_ids)))
    } if track_ids else {}
    episodes = {
        row.id: row
        for row in session.scalars(select(Episode).where(Episode.id.in_(episode_ids)))
    } if episode_ids else {}
    for item in snapshot.items:
        if item.type == "episode":
            episode = episodes.get(item.id)
            if episode:
                item.title = episode.title
                item.artist = episode.podcast.title if episode.podcast else None
            continue
        track = tracks.get(item.id)
        if track:
            item.title = track.title
            # A track's artist hangs off its album, not off the track itself.
            item.artist = track.album.artist.name if track.album and track.album.artist else None
            item.album_id = track.album_id
    return snapshot


@router.post(
    "/player/transfer",
    response_model=PlaybackTransferOut,
    tags=["users"],
    summary="Hand this session's queue to another of my sessions",
    responses={
        400: {"description": "No device session, or the target is not a different session of yours"},
        409: {"description": "The target session cannot be reached"},
        413: {"description": "The queue snapshot is too large"},
    },
)
def transfer_playback(
    payload: PlaybackTransferRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> PlaybackTransferOut:
    """Push the caller's queue to one of their other sessions.

    Push only, always from the session that is playing. The pull direction — an idle device asking
    for someone else's queue — would need a round trip to wake the source and could never work
    against a force-quit app, so it does not exist.

    ⚠ **The invariant this route exists to protect: the source's playback is never disturbed until
    the server has accepted the handoff.** Every rejection below happens in the same request that
    would otherwise have carried the queue away, so a refused transfer leaves the source playing and
    nothing is lost. Do not move any of these checks after the commit, and do not have the server
    tell the source to stop — the source stops itself once it sees a 200.
    """
    if not bool(getattr(user, "remote_playback_enabled", True)):
        # "Local only" has to be refused server-side, or one client with the toggle off is still
        # reachable from every other one.
        raise HTTPException(status_code=409, detail="Cross-device playback is turned off")
    origin = _require_session(auth_session)
    target = session.scalar(
        select(AuthSession).where(
            AuthSession.id == payload.to_session_id, AuthSession.user_id == user.id
        )
    )
    if not target:
        raise HTTPException(status_code=404, detail="No such device session")

    # The queue being moved belongs to `from_session_id`, defaulting to the caller. Naming a third
    # session is what lets a Mac move playback from a phone to itself, or between two other devices,
    # without any of them being the one asking.
    source_id = payload.from_session_id or origin.id
    if source_id == payload.to_session_id:
        raise HTTPException(status_code=400, detail="Playback is already on that device")
    source = session.scalar(
        select(AuthSession).where(AuthSession.id == source_id, AuthSession.user_id == user.id)
    )
    if not source:
        raise HTTPException(status_code=404, detail="No such device session")

    snapshot = payload.snapshot
    if snapshot is None or source_id != origin.id:
        # Moving someone else's queue: use the copy that session published. This is why sessions
        # publish at all — the source never has to be woken to take part in its own handoff.
        source_state = session.get(SessionPlayerState, source_id)
        if not source_state or not source_state.queue_json:
            raise HTTPException(status_code=409, detail={
                "detail": "queue_unavailable",
                "device_label": source.device_label,
            })
        snapshot = PlaybackSnapshot.model_validate_json(source_state.queue_json)
        # Take the live position from that session's own last report rather than from the queue copy,
        # which only changes when the queue itself does.
        if source_state.position_seconds is not None:
            snapshot.position_seconds = float(source_state.position_seconds)
        if source_state.current_index is not None and source_state.current_index < len(snapshot.items):
            snapshot.current_index = source_state.current_index
    if not snapshot.items:
        raise HTTPException(status_code=400, detail="Nothing to transfer")
    if len(snapshot.items) > HANDOFF_MAX_ITEMS:
        raise HTTPException(status_code=413, detail=f"Queue snapshot exceeds {HANDOFF_MAX_ITEMS} items")
    for item in snapshot.items:
        if item.type not in {"track", "episode"}:
            raise HTTPException(status_code=400, detail="Queue items must be tracks or episodes")
        if not item.id or len(item.id) > 64:
            raise HTTPException(status_code=400, detail="Queue item ids are missing or too long")
    if not 0 <= snapshot.current_index < len(snapshot.items):
        raise HTTPException(status_code=400, detail="current_index is outside the queue")
    # Ids only, never titles — the target resolves display metadata from its own mirror, so the
    # server is not asked to be a second source of truth for what a track is called.
    encoded = snapshot.model_dump_json()
    if len(encoded.encode("utf-8")) > HANDOFF_MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Queue snapshot is too large")

    target_state = session.get(SessionPlayerState, target.id)
    presence = _session_presence(target_state, target.last_used_at)
    if presence == "unreachable":
        # Refuse rather than queue for a device that may be gone. A pending handoff nobody collects
        # is indistinguishable from a lost one, and the user would have stopped their music for it.
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "device_unreachable",
                "presence": presence,
                "device_label": target.device_label,
                "last_seen_at": as_utc(target.last_used_at).isoformat() if target.last_used_at else None,
                "last_seen_status": target_state.status if target_state else None,
            },
        )

    now = datetime.now(timezone.utc)
    handoff = PlaybackHandoff(
        user_id=user.id,
        from_session_id=source_id,
        to_session_id=target.id,
        payload_json=encoded,
        item_count=len(snapshot.items),
        autoplay=bool(payload.autoplay),
        status="pending",
        created_at=now,
        expires_at=now + HANDOFF_TTL,
    )
    session.add(handoff)
    session.flush()
    # loop/shuffle are set from the snapshot deliberately: a client too old to know adopt_handoff
    # still applies those two unconditionally before falling through, so this makes that harmless
    # rather than a surprise change of playback mode on the target.
    command = PlaybackCommand(
        user_id=user.id,
        device_id=target.id,
        action="adopt_handoff",
        target_type="handoff",
        target_id=handoff.id,
        loop=snapshot.repeat if snapshot.repeat in {"off", "one", "all"} else "off",
        shuffle=bool(snapshot.shuffle),
        status="pending",
    )
    session.add(command)
    session.flush()
    handoff.command_id = command.id
    session.commit()
    session.refresh(handoff)

    try:
        create_notification(
            session,
            title="Playback moved",
            body=f"Continue on {target.device_label or 'this device'}",
            event_type="remote_playback_command",
            target_url="/player",
            user_id=user.id,
            deliver_apns=True,
            deliver_web=False,
            device_id=apns_device_for_session(session, user.id, target.id),
        )
    except Exception:  # noqa: BLE001 - the handoff stands even if the wake nudge cannot be sent.
        pass

    # A third-party move has to stop the source, because that session is not the one calling and will
    # not stop itself. When the caller IS the source it stops locally on the 200 instead — no command
    # needed, and no window where the server has told it to stop before it knows the move succeeded.
    if source_id != origin.id:
        session.add(PlaybackCommand(
            user_id=user.id,
            device_id=source_id,
            action="stop",
            status="pending",
        ))
        session.commit()
        try:
            create_notification(
                session,
                title="Playback moved",
                body=f"Now on {target.device_label or 'another device'}",
                event_type="remote_playback_command",
                target_url="/player",
                user_id=user.id,
                deliver_apns=True,
                deliver_web=False,
                device_id=apns_device_for_session(session, user.id, source_id),
            )
        except Exception:  # noqa: BLE001 - the move stands even if the source cannot be nudged.
            pass

    return PlaybackTransferOut(
        id=handoff.id,
        status=handoff.status,
        expires_at=as_utc(handoff.expires_at),
        item_count=handoff.item_count,
        to_device_label=target.device_label,
    )


@router.get(
    "/player/handoffs/{handoff_id}",
    response_model=PlaybackHandoffOut,
    tags=["users"],
    summary="Fetch a handed-off queue, or check what became of one",
    responses={404: {"description": "Not a handoff of yours"}, 410: {"description": "Expired"}},
)
def get_playback_handoff(
    handoff_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> PlaybackHandoffOut:
    """The target collects the queue here; the source checks the outcome here.

    ⚠ Expiry is enforced at READ time, not by a sweeper, so a stale payload can never be adopted
    even if no worker is running. The sweeper only reclaims bytes.

    404 rather than 403 for someone else's handoff: there is no reason to confirm that an id exists.
    """
    origin = _require_session(auth_session)
    handoff = _handoff_or_404(session, handoff_id, user.id)
    if origin.id not in {handoff.to_session_id, handoff.from_session_id}:
        raise HTTPException(status_code=404, detail="Handoff not found")

    now = datetime.now(timezone.utc)
    if handoff.status == "pending" and as_utc(handoff.expires_at) < now:
        handoff.status = "expired"
        handoff.payload_json = None
        handoff.resolved_at = now
        session.commit()
        raise HTTPException(status_code=410, detail="Handoff expired")

    from_label = None
    if handoff.from_session_id:
        source = session.get(AuthSession, handoff.from_session_id)
        from_label = source.device_label if source else None

    out = PlaybackHandoffOut(
        id=handoff.id,
        status=handoff.status,
        item_count=handoff.item_count,
        created_at=as_utc(handoff.created_at),
        expires_at=as_utc(handoff.expires_at),
        from_device_label=from_label,
    )
    # Only the target gets the payload, and only while it is still pending. The source reads status
    # to learn the outcome; handing its own queue back would just be a way to get it wrong twice.
    if origin.id == handoff.to_session_id and handoff.status == "pending" and handoff.payload_json:
        out.snapshot = PlaybackSnapshot.model_validate_json(handoff.payload_json)
        # Decided here, not by each client, so the two cannot derive it differently.
        out.autoplay_effective = bool(
            handoff.autoplay and as_utc(handoff.created_at) >= now - HANDOFF_AUTOPLAY_DECAY
        )
    return out


def _resolve_handoff(
    session: Session,
    handoff_id: str,
    user: User,
    auth_session: AuthSession | None,
    status_value: str,
    reason: str | None = None,
) -> dict:
    origin = _require_session(auth_session)
    handoff = _handoff_or_404(session, handoff_id, user.id)
    if origin.id != handoff.to_session_id:
        raise HTTPException(status_code=404, detail="Handoff not found")
    # Idempotent: the listener may retry an ack, and re-reporting an outcome must not rewrite it.
    if handoff.status != "pending":
        return {"ok": True, "status": handoff.status}
    handoff.status = status_value
    handoff.resolved_at = datetime.now(timezone.utc)
    handoff.payload_json = None
    if reason:
        handoff.error = reason[:64]
    session.commit()
    return {"ok": True, "status": handoff.status}


@router.post("/player/handoffs/{handoff_id}/adopted", tags=["users"], summary="Report a handed-off queue adopted")
def adopt_playback_handoff(
    handoff_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> dict:
    return _resolve_handoff(session, handoff_id, user, auth_session, "adopted")


@router.post("/player/handoffs/{handoff_id}/rejected", tags=["users"], summary="Report a handed-off queue refused")
def reject_playback_handoff(
    handoff_id: str,
    payload: PlaybackHandoffRejection,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    auth_session: AuthSession | None = Depends(get_current_auth_session),
) -> dict:
    return _resolve_handoff(session, handoff_id, user, auth_session, "rejected", payload.reason)


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
    rows = list(session.scalars(stmt.order_by(PlaybackCommand.created_at.asc())))
    if include_consumed:
        return [_serialize_command(c) for c in rows]

    # ⚠ A transport verb is an instruction about NOW, and delivering a stale one is worse than
    # dropping it. A device that was offline while somebody worked the transport on another device
    # would otherwise come back and replay every skip at once — the reported "I turned my phone's
    # network off, skipped a few times on the Mac, and it changed the phone anyway". Past this window
    # the instruction no longer describes anything the user still wants.
    #
    # Commands that CARRY something are exempt: a handoff and a queue addition are not "now" verbs,
    # they are payloads, and the server's own handoff expiry (§31) is what bounds those instead.
    now = datetime.now(timezone.utc)
    carries_payload = {"adopt_handoff", "enqueue_next", "enqueue_end"}
    fresh: list[PlaybackCommand] = []
    expired = False
    for command in rows:
        created = as_utc(command.created_at) or now
        if command.action not in carries_payload and (now - created) > COMMAND_TTL:
            command.status = "expired"
            command.consumed_at = now
            expired = True
            continue
        fresh.append(command)
    if expired:
        session.commit()
    return [_serialize_command(c) for c in fresh]


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


@router.post(
    "/me/plays",
    response_model=PlayEventOut,
    tags=["users"],
    summary="Record a track play (local history + Jellyfin)",
    responses={403: {"description": "Requires library:view"}},
)
def record_play(
    payload: PlayRecordIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.library_view)),
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


@router.get(
    "/me/plays",
    response_model=list[PlayEventOut],
    tags=["users"],
    summary="My recent plays",
    responses={403: {"description": "Requires library:view — rows carry track/album/artist titles"}},
)
def list_my_plays(
    limit: int = 50,
    days: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.library_view)),
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


@router.get(
    "/library/top",
    tags=["library"],
    summary="Top artist/album/track over a window",
    response_model=dict,
    responses={403: {"description": "Requires library:view"}},
)
def library_top(
    days: int = 30,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.library_view)),
) -> dict:
    days = max(1, min(days, 365))
    return _top_for_window(session, user, days)


# ── Offline delta sync ────────────────────────────────────────────────────────

@router.get("/library/changes", tags=["library"], summary="Library rows changed since a timestamp (delta sync)", response_model=dict)
def library_changes(
    since: str | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_view)),
) -> dict:
    # Capture the response high-water mark before any entity query. If an import commits while
    # these queries are running, its updated_at is newer than this cursor and the next delta will
    # return it. Capturing server_time after the queries could advance the client beyond a row that
    # was committed between the final SELECT and response serialization, skipping it permanently.
    server_time = datetime.now(timezone.utc)
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid 'since' timestamp (use ISO 8601)")

    def _filter(query, model):
        query = query.where(model.updated_at <= server_time)
        return query.where(model.updated_at > since_dt) if since_dt else query

    def _iso(dt):
        # SQLite stores these tz-naive; they are UTC, so emit a UTC-aware ISO string
        # consistent with server_time for client cursor math.
        if not dt:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()

    artists = [
        {"id": a.id, "name": a.name, "sort_name": a.sort_name, "musicbrainz_id": a.musicbrainz_id,
         "cover_locked": a.cover_locked, "updated_at": _iso(a.updated_at)}
        for a in session.scalars(_filter(select(Artist), Artist))
    ]
    albums = [
        {"id": al.id, "artist_id": al.artist_id, "title": al.title, "release_title": al.release_title,
         "cover_path": al.cover_path, "cover_locked": al.cover_locked,
         "musicbrainz_release_id": al.musicbrainz_release_id,
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
    # Deletions come from tombstones (`LibraryDeletion`), written by mapper events on every delete
    # path.  Bounded by the same server_time high-water mark as the entity queries so a client's
    # cursor can never skip one.
    deleted = {"artists": [], "albums": [], "tracks": []}
    bucket = {"artist": "artists", "album": "albums", "track": "tracks"}
    if since_dt:
        rows = session.scalars(
            select(LibraryDeletion)
            .where(LibraryDeletion.deleted_at > since_dt, LibraryDeletion.deleted_at <= server_time)
        )
        for row in rows:
            key = bucket.get(row.entity_type)
            if key:
                deleted[key].append(row.entity_id)

    return {
        "server_time": server_time.isoformat(),
        "since": since_dt.isoformat() if since_dt else None,
        "artists": artists,
        "albums": albums,
        "tracks": tracks,
        "deleted": deleted,
        # A cursor older than the tombstone retention window cannot be reconciled from the delta
        # feed alone — deletions from before it have been pruned. The client must re-seed instead.
        # Always false when `since` is omitted, since that request already IS a full read.
        "full_resync_required": bool(
            since_dt and since_dt < server_time - LIBRARY_DELETION_RETENTION
        ),
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


@router.get("/me/pinned-podcasts", tags=["users"], summary="My pinned podcasts", response_model=list[dict])
def list_pinned_podcasts(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    return [{"podcast_id": item_id} for item_id in _pinned_item_ids(session, user, "podcast")]


@router.post("/me/pinned-podcasts", tags=["users"], summary="Pin a podcast", response_model=list[dict])
def pin_podcast(
    payload: PinPodcastIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    if not session.get(Podcast, payload.podcast_id):
        raise HTTPException(status_code=404, detail="Podcast not found")
    _toggle_pinned_item(session, user, "podcast", payload.podcast_id, pin=True)
    return list_pinned_podcasts(session, user)


@router.delete("/me/pinned-podcasts/{podcast_id}", tags=["users"], summary="Unpin a podcast", response_model=list[dict])
def unpin_podcast(
    podcast_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    _toggle_pinned_item(session, user, "podcast", podcast_id, pin=False)
    return list_pinned_podcasts(session, user)


@router.get("/me/home", tags=["users"], summary="Home dashboard aggregate", response_model=dict)
def me_home(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """One call backing the whole Home screen. Any authenticated user may call it.

    Because it aggregates several independently-permissioned things, sections the caller is not
    entitled to come back **empty rather than 403** — a podcast-only account keeps a working
    dashboard. Without ``library:view`` that means ``recently_added``, ``recent_plays``,
    ``pinned_albums`` and ``pinned_artists`` are all ``[]``.
    """
    # Home is an aggregate of several independently-permissioned things, so it stays reachable for
    # everyone and empties the sections the caller isn't entitled to — a blanket 403 would take the
    # whole dashboard away from a podcast-only user.  Without this gate the library sections leaked
    # album/artist/track titles to any authenticated account, even one with no library:view.
    can_view_library = user.is_admin or any(
        up.permission == Permission.library_view for up in user.permissions
    )

    # Recently added albums (by created_at)
    recent_albums = list(
        session.scalars(
            select(Album)
            .options(selectinload(Album.artist))
            .order_by(Album.created_at.desc())
            .limit(12)
        )
    ) if can_view_library else []
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
    _raw_plays = list_my_plays(limit=100, days=None, session=session, user=user) if can_view_library else []
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
    # `has_cover` rides along because Home has no other way to know: it never calls /playlists, so
    # without this its chips can only ever draw the generated placeholder, and an uploaded cover
    # would appear on the playlist screen but never on Home. One indexed lookup for all of them,
    # not one per playlist. Existence of the row is enough here — the cover endpoint revalidates
    # the file itself, and a client that gets a 404 falls back to the same placeholder anyway.
    _pinned_rows = list(
        session.scalars(
            select(PinnedPlaylist).where(PinnedPlaylist.user_id == user.id).order_by(PinnedPlaylist.created_at.asc())
        )
    )
    _covered_playlist_ids = set(
        session.scalars(
            select(PlaylistCover.playlist_id).where(
                PlaylistCover.playlist_id.in_([p.playlist_id for p in _pinned_rows])
            )
        )
    ) if _pinned_rows else set()
    pinned = [
        {
            "playlist_id": p.playlist_id,
            "name": p.name,
            "track_count": None,
            "has_cover": p.playlist_id in _covered_playlist_ids,
        }
        for p in _pinned_rows
    ]

    # Pinned albums/artists, resolved (in pin order) to title + cover for the Home grid.
    pinned_album_ids = _pinned_item_ids(session, user, "album") if can_view_library else []
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
    pinned_artist_ids = _pinned_item_ids(session, user, "artist") if can_view_library else []
    artists_by_id = {}
    if pinned_artist_ids:
        artists_by_id = {a.id: a for a in session.scalars(select(Artist).where(Artist.id.in_(pinned_artist_ids)))}
    pinned_artists = [
        {"id": a.id, "name": a.name, "cover_path": a.cover_path}
        for aid in pinned_artist_ids
        if (a := artists_by_id.get(aid))
    ]
    pinned_podcast_ids = _pinned_item_ids(session, user, "podcast")
    podcasts_by_id = {}
    if pinned_podcast_ids:
        podcasts_by_id = {
            podcast.id: podcast
            for podcast in session.scalars(select(Podcast).where(Podcast.id.in_(pinned_podcast_ids)))
        }
    pinned_podcasts = [
        _podcast_out(session, podcast, user.id).model_dump(mode="json")
        for podcast_id in pinned_podcast_ids
        if (podcast := podcasts_by_id.get(podcast_id))
    ]

    # Track counts come straight from the local rows now that those are the store, in one grouped
    # query. This used to be one blocking Jellyfin call for Favorites plus another per pinned
    # playlist — an N+1 HTTP fan-out on the single request every client makes at launch (§7) — and
    # a user without Jellyfin linked got no Favorites card at all, because the whole block was
    # inside `if client`.
    counts = dict(session.execute(
        select(PlaylistTrack.playlist_id, func.count(PlaylistTrack.id))
        .join(Playlist, Playlist.id == PlaylistTrack.playlist_id)
        .where(Playlist.user_id == user.id)
        .group_by(PlaylistTrack.playlist_id)
    ).all())
    favorites_row = get_or_create_favorites(session, user.id)
    session.commit()
    favorites = {
        "id": "favorites",
        "name": "Favorites",
        "track_count": counts.get(favorites_row.id, 0),
    }
    # Pins are recorded under the public id, which for a mirrored playlist is its Jellyfin id.
    native_by_public_id = {
        _public_playlist_id(pl): pl
        for pl in session.scalars(select(Playlist).where(Playlist.user_id == user.id))
    }
    for entry in pinned:
        row = native_by_public_id.get(entry["playlist_id"])
        entry["track_count"] = counts.get(row.id, 0) if row else None

    return {
        "recently_added": recently_added,
        "recently_approved": recently_approved,
        "recent_plays": [e.model_dump(mode="json") for e in recent_plays],
        "favorites": favorites,
        "pinned_playlists": pinned,
        "pinned_albums": pinned_albums,
        "pinned_artists": pinned_artists,
        "pinned_podcasts": pinned_podcasts,
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
    users = list(session.scalars(select(User).options(selectinload(User.session_player_states), selectinload(User.auth_sessions)).order_by(User.created_at.asc())))
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
    _: User = Depends(require_permission(Permission.library_view)),
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
                    cover_locked=album.cover_locked,
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
                            replaygain_track_gain=track.replaygain_track_gain,
                            artist_name=artist.name,
                        )
                        for track in sorted(album.tracks, key=lambda track: (track.disc_number or 1, track.track_number or 9999))
                    ],
                    release_title=album.release_title,
                    musicbrainz_release_id=album.musicbrainz_release_id,
                    musicbrainz_release_group_id=album.musicbrainz_release_group_id,
                    artist_name=artist.name,
                )
                for album in sorted(artist.albums, key=lambda album: ((album.sort_name or album.title) or "").lower())
            ],
            sort_name=artist.sort_name,
            musicbrainz_id=artist.musicbrainz_id,
            cover_path=artist.cover_path,
            cover_locked=artist.cover_locked,
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
    _: User = Depends(require_permission(Permission.library_view)),
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
    items = [
        LibraryArtistRow(
            id=a.id, name=a.name, sort_name=a.sort_name, cover_path=a.cover_path,
            cover_locked=a.cover_locked, album_count=len(a.albums),
        )
        for a in rows
    ]
    return PaginatedArtists(items=items, total=total, page=page, page_size=page_size)


@router.get("/library/albums", response_model=PaginatedAlbums, tags=["library"], summary="Paginated albums by bucket")
def library_albums(
    bucket: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    artist_id: str | None = Query(None),
    q: str | None = Query(None),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_view)),
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
            cover_path=al.cover_path, cover_locked=al.cover_locked, track_count=len(al.tracks),
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
    _: User = Depends(require_permission(Permission.library_view)),
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
        # Treat a missing disc as disc 1 (mixed NULL/1 disc_number otherwise splits the
        # album into two groups, since SQLite sorts NULLs first); untracked tracks last.
        ordered = stmt.order_by(func.coalesce(Track.disc_number, 1), func.coalesce(Track.track_number, 9999), func.lower(Track.title))
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
            replaygain_track_gain=t.replaygain_track_gain,
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
    _: User = Depends(require_permission(Permission.library_view)),
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
    user: User = Depends(require_permission(Permission.library_view)),
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    # Removals are permanent: a delete has no destination at all, and the worker unlinks the file
    # in place. Only move_to_import relocates, and it relocates into the import folder.
    destination_root = None if payload.action == "delete" else settings.import_path
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
        new_path = destination_root / old_path.name if destination_root is not None else None
        session.add(
            ProposalItem(
                batch_id=batch.id,
                title=track.title,
                kind=batch_kind,
                old_value=str(old_path),
                new_value=str(new_path) if new_path is not None else None,
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    current_user: User = Depends(require_permission(Permission.library_edit)),
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
    _: User = Depends(require_permission(Permission.library_edit)),
) -> TaskOut:
    """Queues a ``requeue_replacement`` worker task that searches for a better copy of this track.

    Nothing on disk changes here. The candidates it finds arrive in the Task Queue as a download
    proposal batch, and the file is only overwritten once a candidate is approved there — the same
    propose → review → approve flow every other mutating operation uses.
    """
    track = session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return serialize_task(enqueue_task(session, "requeue_replacement", {"track_ids": [track.id]}))


@router.post("/library/albums/{album_id}/replace", response_model=TaskOut, tags=["library"], summary="Queue replacement downloads for an album's tracks")
def requeue_album_replacement(
    album_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_edit)),
) -> TaskOut:
    """Same as the per-track replace, for every track on the album in one task.

    Candidates land in the Task Queue for approval; nothing is overwritten until they are approved.
    400s when the album has no tracks.
    """
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    track_ids = list(session.scalars(select(Track.id).where(Track.album_id == album.id)))
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
    if not user or (not user.is_admin and Permission.library_view not in permissions):
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
    if not user or (not user.is_admin and Permission.library_view not in permissions):
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
    if not user or (not user.is_admin and Permission.library_view not in permissions):
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
    _: User = Depends(require_permission(Permission.library_edit)),
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
    if not user or (not user.is_admin and Permission.library_view not in permissions):
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
    _: User = Depends(require_permission(Permission.library_edit)),
) -> dict:
    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    urls = artist_image_candidate_urls(artist.name)
    return {"artist_id": artist.id, "urls": urls, "cover_path": urls[0] if urls else None}


def _artist_cover_destination(artist: Artist, ext: str) -> Path:
    folder = get_settings().library_path / safe_path_part(artist.name, "Unknown Artist")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"cover{ext}"


def _album_cover_destination(album: Album, ext: str) -> Path:
    """Album folder if known, else the folder of any track it owns, else a derived library path.

    Shared by the multipart upload and the from-URL route so a cover lands in the same place
    regardless of how it was chosen.
    """
    folder = Path(album.path) if album.path else None
    if folder is None:
        for track in album.tracks:
            if track.path:
                folder = Path(track.path).parent
                break
    if folder is None:
        folder = (
            get_settings().library_path
            / safe_path_part(album.artist.name, "Unknown Artist")
            / safe_path_part(album.title, "Unknown Album")
        )
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"cover{ext}"


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
    _: User = Depends(require_permission(Permission.library_edit)),
) -> dict:
    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    content = await file.read()
    ext = _sniff_image_extension(content)
    if not ext:
        raise HTTPException(status_code=400, detail="File is not a valid image (jpg, png, webp, gif, bmp)")
    content, ext = square_cover_bytes(content, ext)
    destination = _artist_cover_destination(artist, ext)
    if artist.cover_path:
        old = Path(artist.cover_path)
        if old != destination and old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    artist.cover_path = str(destination)
    # A manually-chosen cover is what cover_locked exists to protect — lock it automatically so
    # refresh-covers-style tools don't silently replace what the user just picked.
    artist.cover_locked = True
    session.commit()
    write_app_log(f"Uploaded artist art for {artist.name}", source="upload", kind="artist_cover")
    return {"cover_path": str(destination), "cover_locked": True}


def _download_cover_bytes(url: str) -> tuple[bytes, str]:
    """Fetch a candidate cover URL and return (bytes, extension).

    Clients hand us a URL from `/cover-candidates`; the **server** downloads it so that
    `cover_path` is always a real file on disk. Storing a URL in that column would make
    `GET /library/{albums,artists}/{id}/cover` 404 — it opens the value with `Path(...)`.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Cover URL must be http(s)")
    try:
        response = httpx.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not download that image")
    content = response.content
    ext = _sniff_image_extension(content)
    if not ext:
        raise HTTPException(status_code=400, detail="That URL is not a valid image")
    # Candidate art is squared on the same terms as an upload — it lands in the same frames.
    return square_cover_bytes(content, ext)


@router.post(
    "/library/albums/{album_id}/cover-from-url",
    tags=["library"],
    summary="Download a cover URL and store it as the album cover",
    responses={
        400: {"description": "Not an http(s) URL, or the download wasn't a valid image"},
        404: {"description": "Album not found"},
        502: {"description": "The image could not be downloaded"},
    },
)
def set_album_cover_from_url(
    album_id: str,
    payload: CoverFromURLRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_edit)),
) -> dict:
    """Companion to the multipart upload, for picking one of `/cover-candidates`' URLs.

    ⚠️ Clients must use this rather than writing the URL into `cover_path` via
    `/library/metadata/apply`. `cover_path` is a filesystem path and must never hold a URL; the
    apply path only rescues that case by downloading behind the client's back, which leaves the
    contract lying about what the field contains.
    """
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    content, ext = _download_cover_bytes(payload.url)
    destination = _album_cover_destination(album, ext)
    if album.cover_path:
        old = Path(album.cover_path)
        if old != destination and old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    album.cover_path = str(destination)
    # Same reasoning as the upload route: a deliberately chosen cover is exactly what cover_locked
    # protects, so refresh-covers-style tools don't silently replace it.
    album.cover_locked = True
    session.commit()
    write_app_log(f"Set cover art from URL for {album.artist.name} — {album.title}", source="upload", kind="album_cover")
    return {"cover_path": str(destination), "cover_locked": True}


@router.post(
    "/library/artists/{artist_id}/cover-from-url",
    tags=["library"],
    summary="Download a cover URL and store it as the artist image",
    responses={
        400: {"description": "Not an http(s) URL, or the download wasn't a valid image"},
        404: {"description": "Artist not found"},
        502: {"description": "The image could not be downloaded"},
    },
)
def set_artist_cover_from_url(
    artist_id: str,
    payload: CoverFromURLRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_edit)),
) -> dict:
    artist = session.get(Artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    content, ext = _download_cover_bytes(payload.url)
    destination = _artist_cover_destination(artist, ext)
    if artist.cover_path:
        old = Path(artist.cover_path)
        if old != destination and old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    artist.cover_path = str(destination)
    artist.cover_locked = True
    session.commit()
    write_app_log(f"Set image from URL for {artist.name}", source="upload", kind="artist_cover")
    return {"cover_path": str(destination), "cover_locked": True}


@router.post("/library/albums/{album_id}/cover", tags=["library"], summary="Upload album cover art")
async def upload_album_cover(
    album_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.library_edit)),
) -> dict:
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    content = await file.read()
    ext = _sniff_image_extension(content)
    if not ext:
        raise HTTPException(status_code=400, detail="File is not a valid image (jpg, png, webp, gif, bmp)")
    content, ext = square_cover_bytes(content, ext)
    destination = _album_cover_destination(album, ext)
    if album.cover_path:
        old = Path(album.cover_path)
        if old != destination and old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    album.cover_path = str(destination)
    # A manually-chosen cover is what cover_locked exists to protect — lock it automatically so
    # refresh-covers-style tools don't silently replace what the user just picked.
    album.cover_locked = True
    session.commit()
    write_app_log(f"Uploaded cover art for {album.artist.name} — {album.title}", source="upload", kind="album_cover")
    return {"cover_path": str(destination), "cover_locked": True}


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
    has_playlist = bool(payload.playlist_name and payload.playlist_original_tracks)
    if has_playlist:
        import uuid as _uuid
        pending_key = f"pending_playlist:{_uuid.uuid4()}"
        setting = AppSetting(key=pending_key, value=json.dumps({
            "playlist_name": payload.playlist_name,
            "original_tracks": payload.playlist_original_tracks,
            "user_id": current_user.id,
            "origin": (payload.playlist_origin or "").strip() or None,
            "retry_count": 0,
        }))
        session.add(setting)
        session.commit()
        write_app_log(f"Playlist import: stored pending playlist '{payload.playlist_name}' ({len(payload.playlist_original_tracks)} original tracks)")

    has_import_work = bool(payload.files) or bool(payload.download_requests)
    if has_import_work:
        task = enqueue_task(
            session,
            "propose_import",
            {
                "path": payload.path,
                "files": payload.files,
                "download_requests": payload.download_requests or [],
            },
        )
    elif has_playlist:
        # Nothing to download/import (every song is already in the library) — don't fire an empty
        # propose_import (it raises "No import files or downloads were selected"); build the playlist
        # now from the owned tracks instead.
        task = enqueue_task(session, "create_pending_playlists", {})
    else:
        raise HTTPException(status_code=400, detail="No import files or downloads were selected")
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


def _spotify_embed_fetch(playlist_id: str) -> dict | None:
    """Fetch a public Spotify playlist's track list from the embed page's __NEXT_DATA__ JSON.

    Credential-free: Spotify's anonymous token endpoint now returns 403, so when no client
    credentials are configured this is how we get the FULL track list — the rendered embed and the
    spotifyscraper library only expose ~20-30 of the tracks, while the embed's embedded JSON carries
    them all. The embed caps very large playlists at ~100 tracks and carries no album name (title +
    artist only), which is fine here: 'songs' mode groups under Singles and 'albums' mode resolves
    albums via MusicBrainz. Returns None on any failure so the caller can fall through.
    """
    import json as _json
    import re as _re

    try:
        resp = httpx.get(
            f"https://open.spotify.com/embed/playlist/{playlist_id}",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        write_app_log(f"Spotify embed fetch error: {exc}", level="warning", playlist_id=playlist_id)
        return None

    match = _re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, _re.S)
    if not match:
        return None
    try:
        entity = _json.loads(match.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, _json.JSONDecodeError):
        return None

    tracks: list[dict] = []
    for item in entity.get("trackList", []):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        tracks.append({"title": title, "artist": (item.get("subtitle") or "").strip(), "album": None})
    if not tracks:
        return None
    return {"name": entity.get("name") or entity.get("title"), "tracks": tracks}


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
            # 1) Spotify Web API first (when credentials configured): full list + album info.
            api_result = _spotify_api_fetch(m.group(1))
            if api_result is not None and api_result["tracks"]:
                return {"source": source, "name": api_result["name"], "tracks": api_result["tracks"], "count": len(api_result["tracks"])}

            # 2) Credential-free: parse the embed page's __NEXT_DATA__ (full track list, up to ~100).
            #    Spotify's anonymous token endpoint is now blocked (403), so this is the no-creds path.
            embed_result = _spotify_embed_fetch(m.group(1))
            if embed_result is not None and embed_result["tracks"]:
                return {"source": source, "name": embed_result["name"], "tracks": embed_result["tracks"], "count": len(embed_result["tracks"])}

            # 3) Last resort: spotifyscraper (embed-page scraping; often truncates to ~20-30).
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
    user: User = Depends(require_permission(Permission.discover)),
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
    _: User = Depends(require_permission(Permission.discover)),
) -> dict:
    tracks = itunes_album_tracks(album_id)
    return {"tracks": tracks}


@router.post("/discover/task-queue", response_model=TaskOut, tags=["discover"], summary="Add discovered tracks to download queue")
def discover_task_queue(
    payload: DiscoverTaskQueueRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.discover)),
) -> TaskOut:
    write_app_log("Discover task queue requested", feature="discover", user_id=user.id, downloads=len(payload.download_requests))
    task = enqueue_task(session, "propose_import", {"path": None, "files": [], "download_requests": payload.download_requests})
    write_app_log("Discover task queue created", feature="discover", user_id=user.id, task_id=task.id, downloads=len(payload.download_requests))
    return serialize_task(task)


@router.get("/wishlist", response_model=list[WishlistOut], tags=["wishlist"], summary="List wishlist")
def list_wishlist(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.discover)),
) -> list[WishlistOut]:
    reconcile_stale_approved_wishlist_items(session, user)
    query = select(WishlistItem).options(selectinload(WishlistItem.user))
    if not user_has_permission(user, Permission.wishlist_approve_all):
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
    user: User = Depends(require_permission(Permission.discover)),
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
    user: User = Depends(require_permission(Permission.discover)),
) -> WishlistOut:
    item = session.get(WishlistItem, item_id)
    if not item or (not user_has_permission(user, Permission.wishlist_approve_all) and item.user_id != user.id):
        raise HTTPException(status_code=404, detail="Wishlist item not found")
    item.status = "removed"
    item.status_changed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(item)
    return serialize_wishlist_item(item)


@router.get("/wishlist/approvals", response_model=list[ProposalBatchOut], tags=["wishlist"], summary="Get wishlist items pending approval")
def list_wishlist_approvals(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.discover)),
) -> list[ProposalBatchOut]:
    query = (
        select(ProposalBatch)
        .options(selectinload(ProposalBatch.items))
        .where(ProposalBatch.kind == ProposalKind.download)
        .where(ProposalBatch.status.in_([ProposalStatus.pending, ProposalStatus.approved, ProposalStatus.executing, ProposalStatus.failed]))
        .order_by(ProposalBatch.created_at.desc())
    )
    batches = prune_settled_batches(session, list(session.scalars(query)))
    if user_has_permission(user, Permission.wishlist_approve_all):
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
    # Moving a "wanted" item into a real download batch (Task Queue) is a review action, not a
    # normal "discover" capability — a plain discover-only user must NOT be able to self-approve
    # their own wishlist request. Only wishlist_approve_all holders (or admins) may submit here,
    # for anyone's items. (The web UI already only shows the submit button to canApproveAll users;
    # this closes the matching server-side gap that let other clients call this directly.)
    user: User = Depends(require_permission(Permission.wishlist_approve_all)),
) -> ProposalBatchOut:
    reconcile_stale_approved_wishlist_items(session, user)
    query = select(WishlistItem).options(selectinload(WishlistItem.user)).where(WishlistItem.status == "wanted")
    all_wanted_items = list(session.scalars(query.order_by(WishlistItem.artist.asc(), WishlistItem.album.asc(), WishlistItem.track.asc())))
    denied_items: list[WishlistItem] = []
    if payload and payload.item_ids:
        selected_ids = set(payload.item_ids)
        items = [item for item in all_wanted_items if item.id in selected_ids]
        if payload.deny_unselected and user_has_permission(user, Permission.wishlist_approve_all):
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


def _jf_items(client: httpx.Client, path: str, params: dict | None = None) -> list[dict] | None:
    """Fetch an Items-shaped Jellyfin response.

    ⚠️ Returns **None on failure**, which is deliberately distinct from an empty list. The inbound
    mirror reads "this playlist is absent from Jellyfin" as "the user deleted it in Jellyfin" and
    deletes the local copy — so a timeout or a 500 that reported itself as `[]` would silently
    destroy every playlist the user has. Every caller must branch on None before applying removals.
    """
    try:
        resp = client.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json().get("Items", [])
    except Exception:
        return None


def _playlist_covers_root() -> Path:
    return get_settings().config_path / "playlist_covers"


def _playlist_local_cover_path(session: Session, playlist_id: str) -> Path | None:
    row = session.get(PlaylistCover, playlist_id)
    if not row:
        return None
    path = Path(row.cover_path)
    if not path.exists() or not path.is_file():
        return None
    resolved = path.resolve()
    root = _playlist_covers_root().resolve()
    if root not in [resolved, *resolved.parents]:
        return None
    return resolved


def _playlist_has_cover(session: Session, playlist_id: str) -> bool:
    return _playlist_local_cover_path(session, playlist_id) is not None


# ── Native (DB-backed) playlists — the SOURCE OF TRUTH for every user ─────────
# Playlist/PlaylistTrack rows back playlists and Favorites for EVERY user, linked to Jellyfin or
# not. When a user has a Jellyfin account linked, Jellyfin is a two-way **mirror** of these rows
# (see the mirror helpers below): writes here push out to it, and `_mirror_pull` folds changes made
# in Jellyfin back into these rows.
#
# ⚠️ This replaced an either/or design where a Jellyfin-linked user's playlists lived ONLY in
# Jellyfin and had no native rows at all. Two user-visible failures came from that, and both are
# structural — do not go back to it:
#   • Unlinking a Jellyfin account (or clearing the instance's Jellyfin config) made every playlist
#     vanish, because the native tables the routes fell back to had been emptied on link.
#   • A playlist edited in Jellyfin and one edited in Nudibranch were the same object with no
#     reconciliation, so whichever side you looked at last was "right".
# ⚠ Do NOT reintroduce an instance-level "Jellyfin configured" gate here either — that made a
# not-yet-linked user on a Jellyfin instance get empty responses and a 412 on create.


def _native_playlist_out(session: Session, playlist: Playlist) -> FavoritesOut:
    entries = sorted(playlist.tracks, key=lambda pt: (pt.position if pt.position is not None else 0))
    tracks_out: list[PlaylistTrackOut] = []
    track_ids: list[str] = []
    for i, entry in enumerate(entries):
        track = entry.track
        if not track:
            continue
        track_ids.append(track.id)
        artist_name = track.album.artist.name if track.album and track.album.artist else ""
        album_title = track.album.title if track.album else ""
        tracks_out.append(PlaylistTrackOut(
            id=entry.id,
            track_id=track.id,
            position=i + 1,
            title=track.title,
            artist=artist_name,
            album=album_title,
            album_id=track.album_id,
            format=track.format,
            replaygain_track_gain=track.replaygain_track_gain,
        ))
    pid = _public_playlist_id(playlist)
    return FavoritesOut(
        id=pid, name=playlist.name, protected=playlist.protected, track_ids=track_ids, tracks=tracks_out,
        track_count=len(track_ids), has_cover=_playlist_has_cover(session, pid),
    )


def _public_playlist_id(playlist: Playlist) -> str:
    """The id clients see.

    ⚠️ A mirrored playlist keeps exposing its **Jellyfin** id, even though the native row is now
    the store. Both `PinnedPlaylist.playlist_id` and `PlaylistCover.playlist_id` are opaque strings
    with no foreign key (they have to be — a playlist id used to be either kind), and every client
    has cached ids too. Switching the exposed id to the native row id would silently orphan every
    existing pin and every uploaded cover on any Jellyfin-linked instance. The native id is an
    internal detail; `_native_playlist_query` accepts either.
    """
    if playlist.protected:
        return "favorites"
    return playlist.jellyfin_playlist_id or playlist.id


def _native_playlist_query(session: Session, user_id: str, playlist_id: str) -> Playlist | None:
    """Resolve a public playlist id — native row id, mirrored Jellyfin item id, or "favorites"."""
    if playlist_id == "favorites":
        return get_or_create_favorites(session, user_id)
    loaded = selectinload(Playlist.tracks).selectinload(PlaylistTrack.track).selectinload(Track.album).selectinload(Album.artist)
    return session.scalar(
        select(Playlist)
        .where(
            Playlist.user_id == user_id,
            or_(Playlist.id == playlist_id, Playlist.jellyfin_playlist_id == playlist_id),
        )
        .options(loaded)
    )


def _add_tracks_to_native_playlist(session: Session, playlist: Playlist, track_ids: list[str]) -> None:
    existing_track_ids = {pt.track_id for pt in playlist.tracks}
    next_position = max((pt.position or 0) for pt in playlist.tracks) + 1 if playlist.tracks else 1
    # Preserve the requested order; skip tracks already present and unknown ids.
    valid_ids = {t.id for t in session.scalars(select(Track).where(Track.id.in_(track_ids)))}
    for track_id in track_ids:
        if track_id in existing_track_ids or track_id not in valid_ids:
            continue
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track_id, position=next_position))
        existing_track_ids.add(track_id)
        next_position += 1


def _set_native_tracks(session: Session, playlist: Playlist, track_ids: list[str]) -> bool:
    """Replace a playlist's membership wholesale, preserving the given order.

    Returns whether anything actually changed — the caller uses that to skip a commit. Every
    authed request already costs the one SQLite write lock (CLAUDE.md §7); a mirror pull that
    committed on every poll would serialize the whole instance behind a no-op.
    """
    current = [pt.track_id for pt in sorted(playlist.tracks, key=lambda pt: pt.position or 0)]
    if current == track_ids:
        return False
    # Bulk-delete rather than mutating `playlist.tracks`: the relationship is delete-orphan, so
    # clearing the collection AND session.add-ing replacements in the same flush races the unique
    # (playlist_id, track_id) constraint when a track survives the rewrite.
    session.execute(PlaylistTrack.__table__.delete().where(PlaylistTrack.__table__.c.playlist_id == playlist.id))
    session.expire(playlist, ["tracks"])
    for position, track_id in enumerate(track_ids, start=1):
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track_id, position=position))
    return True


# ── Jellyfin mirror ───────────────────────────────────────────────────────────
# Native rows are the store; these keep a linked Jellyfin account in step with them, in both
# directions. Every push is best-effort: Jellyfin being down, slow, or refusing an operation must
# never fail a Nudibranch write or lose local data, because the local row is the real one.


# Records a Jellyfin playlist this user deleted in Nudibranch that Jellyfin then refused to
# delete. The inbound mirror skips these, so a delete can't be undone by the next poll.
_JF_TOMBSTONE_PREFIX = "jf_playlist_tombstone:"


def _jf_tombstones(session: Session, user_id: str) -> set[str]:
    prefix = f"{_JF_TOMBSTONE_PREFIX}{user_id}:"
    keys = session.scalars(select(AppSetting.key).where(AppSetting.key.like(f"{prefix}%")))
    return {key[len(prefix):] for key in keys}


def _mirror_base(playlist: Playlist) -> tuple[set[str], str | None]:
    """The last state both sides agreed on: (jellyfin item ids, playlist name). Empty set + None
    means never mirrored, which the merge reads as "no base" and resolves by union."""
    try:
        state = json.loads(playlist.jellyfin_mirror_state or "{}")
    except ValueError:
        return set(), None
    return set(state.get("items") or []), state.get("name")


def _set_mirror_base(playlist: Playlist, items: list[str], name: str) -> None:
    playlist.jellyfin_mirror_state = json.dumps({"items": items, "name": name})


def _merge_membership(base: set[str], jf_now: list[str], local_now: list[str]) -> list[str]:
    """Three-way merge of one playlist's membership. **Neither side wins.**

    An item is kept unless the side that last had it dropped it, and any item new to either side
    is added. Because an addition is by definition absent from the base and a removal is by
    definition present in it, the two can never describe the same item — there is no conflict to
    arbitrate, which is what makes this symmetric rather than a policy choice.

    Ordering follows Jellyfin for anything it holds, then local additions, so a reorder made in
    either client survives instead of being reshuffled on the next pass.
    """
    jf_set, local_set = set(jf_now), set(local_now)
    removed = (base - jf_set) | (base - local_set)     # dropped by whichever side had it
    target = (base | jf_set | local_set) - removed
    ordered = [item for item in jf_now if item in target]
    ordered += [item for item in local_now if item in target and item not in jf_set]
    return ordered


def _jf_ids_for_tracks(session: Session, track_ids: list[str]) -> list[str]:
    by_id = {t.id: t.jellyfin_item_id for t in session.scalars(select(Track).where(Track.id.in_(track_ids)))}
    return [by_id[tid] for tid in track_ids if by_id.get(tid)]


def _mirror_create(session: Session, client: httpx.Client, jf_user_id: str, playlist: Playlist) -> None:
    """Push a native playlist Jellyfin has never seen, and record the id it came back with."""
    pushed = _jf_ids_for_tracks(session, [pt.track_id for pt in playlist.tracks])
    try:
        resp = client.post("/Playlists", json={
            "Name": playlist.name,
            "UserId": jf_user_id,
            "MediaType": "Audio",
            "Ids": pushed,
        })
        resp.raise_for_status()
        jf_id = resp.json().get("Id") or resp.json().get("PlaylistId") or ""
    except Exception:
        return
    if jf_id:
        playlist.jellyfin_playlist_id = jf_id
        # Seed the merge base with exactly what was pushed. Without a base the next reconcile
        # unions instead of merging, so a track removed in Jellyfin before that pass would come
        # straight back from the local side.
        _set_mirror_base(playlist, pushed, playlist.name)


def _mirror_rename(client: httpx.Client, jf_user_id: str, jf_playlist_id: str, name: str) -> None:
    try:
        item_resp = client.get(f"/Users/{jf_user_id}/Items/{jf_playlist_id}")
        item_resp.raise_for_status()
        item_data = item_resp.json()
        item_data["Name"] = name
        client.post(f"/Items/{jf_playlist_id}", json=item_data).raise_for_status()
    except Exception:
        pass


def _mirror_delete(client: httpx.Client, jf_playlist_id: str) -> bool:
    """Delete in Jellyfin. Returns whether Jellyfin agreed.

    ⚠️ The old code issued this and ignored the result, so a Jellyfin user without the
    `EnableContentDeletion` permission got a 401/403 that was thrown away and a `{}` success —
    the playlist came back on the next refresh and delete looked broken. The local row is deleted
    regardless now; the return value only decides whether to warn.
    """
    try:
        resp = client.delete(f"/Items/{jf_playlist_id}")
        return resp.is_success
    except Exception:
        return False


def _mirror_set_tracks(session: Session, client: httpx.Client, jf_user_id: str, playlist: Playlist) -> None:
    """Make a Jellyfin playlist's membership match the native row's, by diffing rather than
    clearing and refilling — a wholesale rewrite would churn Jellyfin's own PlaylistItemIds and
    lose its ordering for anything we didn't change."""
    jf_playlist_id = playlist.jellyfin_playlist_id
    if not jf_playlist_id:
        _mirror_create(session, client, jf_user_id, playlist)
        return
    items = _jf_items(client, f"/Playlists/{jf_playlist_id}/Items", {"userId": jf_user_id})
    if items is None:
        return
    want = _jf_ids_for_tracks(session, [pt.track_id for pt in sorted(playlist.tracks, key=lambda pt: pt.position or 0)])
    have = {item.get("Id"): item.get("PlaylistItemId") for item in items if item.get("Id")}
    missing = [jf_id for jf_id in want if jf_id not in have]
    stale = [entry_id for jf_id, entry_id in have.items() if jf_id not in want and entry_id]
    try:
        if missing:
            client.post(f"/Playlists/{jf_playlist_id}/Items", params={"ids": ",".join(missing), "userId": jf_user_id})
        if stale:
            client.delete(f"/Playlists/{jf_playlist_id}/Items", params={"EntryIds": ",".join(stale)})
    except Exception:
        pass


def _mirror_favorites(session: Session, client: httpx.Client, jf_user_id: str, favorites: Playlist) -> None:
    """Favorites has no Jellyfin playlist — it is the per-track `IsFavorite` flag."""
    items = _jf_items(client, f"/Users/{jf_user_id}/Items", {
        "Filters": "IsFavorite", "IncludeItemTypes": "Audio", "Recursive": "true", "Limit": "500",
    })
    if items is None:
        return
    have = {item.get("Id") for item in items if item.get("Id")}
    want = set(_jf_ids_for_tracks(session, [pt.track_id for pt in favorites.tracks]))
    for jf_id in want - have:
        try:
            client.post(f"/Users/{jf_user_id}/FavoriteItems/{jf_id}")
        except Exception:
            pass
    for jf_id in have - want:
        try:
            client.delete(f"/Users/{jf_user_id}/FavoriteItems/{jf_id}")
        except Exception:
            pass


def _reconcile_playlist(
    session: Session,
    client: httpx.Client,
    jf_user_id: str,
    playlist: Playlist,
    jf_items: list[dict],
    jf_name: str,
) -> bool:
    """Bring one playlist and its Jellyfin counterpart into agreement, in both directions.

    ⚠️ Tracks with no `jellyfin_item_id` are outside the mirror entirely and are never touched.
    Jellyfin cannot see them, so their absence from its listing says nothing — treating it as a
    removal would delete exactly the freshly-imported tracks on the next pass.
    """
    changed = False
    base_items, base_name = _mirror_base(playlist)

    # Local membership, split into what Jellyfin can see and what it can't.
    local_entries = sorted(playlist.tracks, key=lambda pt: pt.position or 0)
    local_track_ids = [pt.track_id for pt in local_entries]
    jf_id_by_track: dict[str, str] = {}
    if local_track_ids:
        for track in session.scalars(select(Track).where(Track.id.in_(local_track_ids))):
            if track.jellyfin_item_id:
                jf_id_by_track[track.id] = track.jellyfin_item_id
    local_now = [jf_id_by_track[tid] for tid in local_track_ids if tid in jf_id_by_track]
    invisible = [tid for tid in local_track_ids if tid not in jf_id_by_track]

    jf_now = [item["Id"] for item in jf_items if item.get("Id")]
    entry_ids = {item.get("Id"): item.get("PlaylistItemId") for item in jf_items if item.get("Id")}
    target = _merge_membership(base_items, jf_now, local_now)

    # Push the local side of the merge out.
    if playlist.jellyfin_playlist_id:
        missing = [jf_id for jf_id in target if jf_id not in set(jf_now)]
        stale = [entry_ids[jf_id] for jf_id in jf_now if jf_id not in set(target) and entry_ids.get(jf_id)]
        try:
            if missing:
                client.post(
                    f"/Playlists/{playlist.jellyfin_playlist_id}/Items",
                    params={"ids": ",".join(missing), "userId": jf_user_id},
                )
            if stale:
                client.delete(
                    f"/Playlists/{playlist.jellyfin_playlist_id}/Items",
                    params={"EntryIds": ",".join(stale)},
                )
        except Exception:
            pass

    # Apply the Jellyfin side locally, keeping the tracks Jellyfin can't see.
    track_by_jf_id = {jf_id: tid for tid, jf_id in jf_id_by_track.items()}
    unknown = [jf_id for jf_id in target if jf_id not in track_by_jf_id]
    if unknown:
        for track in session.scalars(select(Track).where(Track.jellyfin_item_id.in_(unknown))):
            if track.jellyfin_item_id:
                track_by_jf_id[track.jellyfin_item_id] = track.id
    merged_local = [track_by_jf_id[jf_id] for jf_id in target if jf_id in track_by_jf_id] + invisible
    if _set_native_tracks(session, playlist, merged_local):
        changed = True

    # Name: same three-way rule. A rename made here has already been pushed synchronously, so a
    # difference means Jellyfin was renamed — unless the local name also moved off the base, in
    # which case the local one is kept (documented tiebreak: the user is in Nudibranch).
    if jf_name and jf_name != playlist.name:
        local_renamed = base_name is not None and playlist.name != base_name
        if local_renamed:
            _mirror_rename(client, jf_user_id, playlist.jellyfin_playlist_id or "", playlist.name)
            jf_name = playlist.name
        else:
            playlist.name = jf_name
            changed = True

    new_state = json.dumps({"items": target, "name": jf_name or playlist.name})
    if playlist.jellyfin_mirror_state != new_state:
        playlist.jellyfin_mirror_state = new_state
        changed = True
    return changed


def _mirror_pull(session: Session, user: User, client: httpx.Client, jf_user_id: str) -> bool:
    """Jellyfin → native. Folds edits made in any Jellyfin client back into the local rows.

    Returns whether anything changed. Ordering rule: **Jellyfin wins for anything it knows about**,
    because a Nudibranch-side write has already been pushed out synchronously by the time this
    runs, so a difference here means someone edited Jellyfin directly.

    ⚠️ Every removal in here is gated on the corresponding fetch having actually succeeded. A
    Jellyfin that is down returns None (never `[]`), and this returns early rather than reading
    silence as "the user deleted everything".
    """
    listing = _jf_items(client, f"/Users/{jf_user_id}/Items", {
        "IncludeItemTypes": "Playlist", "Recursive": "true", "Limit": "1000",
    })
    if listing is None:
        return False
    jf_playlists = {pl["Id"]: pl.get("Name", "") for pl in listing if pl.get("Id")}

    changed_tombstones = False
    tombstones = _jf_tombstones(session, user.id)
    if tombstones:
        # Drop tombstones Jellyfin has caught up with, so the set can't grow forever.
        for jf_id in tombstones - set(jf_playlists):
            setting = session.get(AppSetting, f"{_JF_TOMBSTONE_PREFIX}{user.id}:{jf_id}")
            if setting:
                session.delete(setting)
                changed_tombstones = True
        for jf_id in tombstones & set(jf_playlists):
            jf_playlists.pop(jf_id, None)

    native = list(session.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id)
        .options(selectinload(Playlist.tracks))
    ))
    by_jf_id = {pl.jellyfin_playlist_id: pl for pl in native if pl.jellyfin_playlist_id}
    by_name = {pl.name.lower(): pl for pl in native if not pl.jellyfin_playlist_id and not pl.protected}
    changed = changed_tombstones

    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = dict(zip(jf_playlists, pool.map(
            lambda pl_id: _jf_items(client, f"/Playlists/{pl_id}/Items", {"userId": jf_user_id}),
            jf_playlists,
        )))

    for jf_id, jf_name in jf_playlists.items():
        playlist = by_jf_id.get(jf_id)
        discovered = playlist is None
        if not playlist:
            # Adopt a same-named local playlist that has never been mirrored before creating a
            # duplicate — this is the common case the first time a user links an account whose
            # Jellyfin already holds the same playlists by name.
            playlist = by_name.pop(jf_name.lower(), None)
            if playlist:
                playlist.jellyfin_playlist_id = jf_id
            else:
                playlist = Playlist(name=jf_name or jf_id, user_id=user.id, jellyfin_playlist_id=jf_id)
                session.add(playlist)
                session.flush()
            by_jf_id[jf_id] = playlist
            changed = True
        items = fetched.get(jf_id)
        # `items is None` means the fetch failed — skip rather than reconcile against silence.
        if items is not None and _reconcile_playlist(session, client, jf_user_id, playlist, items, jf_name):
            changed = True
        if discovered and _jf_pull_cover(session, client, playlist):
            changed = True

    # A playlist we hold a Jellyfin id for that Jellyfin no longer lists was deleted over there.
    # (Tombstoned ids were removed from `jf_playlists` above, but no local row can point at one —
    # the tombstone only exists because that row was already deleted here.)
    removed: set[str] = set()
    for playlist in native:
        if playlist.jellyfin_playlist_id and playlist.jellyfin_playlist_id not in jf_playlists:
            # Both ids: pins and covers are recorded under whichever was public at the time, and
            # for a mirrored playlist that is the Jellyfin id (_public_playlist_id).
            _forget_playlist(session, playlist.id)
            _forget_playlist(session, playlist.jellyfin_playlist_id)
            removed.add(playlist.id)
            session.delete(playlist)
            changed = True

    # Anything local that Jellyfin has never seen goes the other way.
    for playlist in native:
        if playlist.id in removed or playlist.protected or playlist.jellyfin_playlist_id:
            continue
        _mirror_create(session, client, jf_user_id, playlist)
        changed = changed or bool(playlist.jellyfin_playlist_id)

    # Favorites is the per-track IsFavorite flag rather than a playlist, so it can't go through
    # _reconcile_playlist's add/remove-items calls — but it gets the same three-way merge, so
    # un-favoriting in either client sticks instead of being undone by the other.
    favorites = get_or_create_favorites(session, user.id)
    fav_items = _jf_items(client, f"/Users/{jf_user_id}/Items", {
        "Filters": "IsFavorite", "IncludeItemTypes": "Audio", "Recursive": "true", "Limit": "500",
    })
    if fav_items is not None:
        base_items, _ = _mirror_base(favorites)
        local_ids = [pt.track_id for pt in sorted(favorites.tracks, key=lambda pt: pt.position or 0)]
        jf_id_by_track = {
            t.id: t.jellyfin_item_id
            for t in session.scalars(select(Track).where(Track.id.in_(local_ids)))
            if t.jellyfin_item_id
        } if local_ids else {}
        local_now = [jf_id_by_track[tid] for tid in local_ids if tid in jf_id_by_track]
        invisible = [tid for tid in local_ids if tid not in jf_id_by_track]
        jf_now = [item["Id"] for item in fav_items if item.get("Id")]
        target = _merge_membership(base_items, jf_now, local_now)

        for jf_id in set(target) - set(jf_now):
            try:
                client.post(f"/Users/{jf_user_id}/FavoriteItems/{jf_id}")
            except Exception:
                pass
        for jf_id in set(jf_now) - set(target):
            try:
                client.delete(f"/Users/{jf_user_id}/FavoriteItems/{jf_id}")
            except Exception:
                pass

        track_by_jf_id = {jf: tid for tid, jf in jf_id_by_track.items()}
        unknown = [jf_id for jf_id in target if jf_id not in track_by_jf_id]
        if unknown:
            for track in session.scalars(select(Track).where(Track.jellyfin_item_id.in_(unknown))):
                if track.jellyfin_item_id:
                    track_by_jf_id[track.jellyfin_item_id] = track.id
        merged_local = [track_by_jf_id[j] for j in target if j in track_by_jf_id] + invisible
        if _set_native_tracks(session, favorites, merged_local):
            changed = True
        new_state = json.dumps({"items": target, "name": favorites.name})
        if favorites.jellyfin_mirror_state != new_state:
            favorites.jellyfin_mirror_state = new_state
            changed = True
    return changed


def _sync_playlists(session: Session, user: User) -> None:
    """Run the inbound mirror for a user who has Jellyfin linked. No-op otherwise, which is what
    makes every read route below identical for both kinds of user."""
    client, jf_user_id = _jf_client(session, user)
    if not client:
        return
    with client:
        try:
            if _mirror_pull(session, user, client, jf_user_id):
                session.commit()
            else:
                session.rollback()
        except Exception:
            session.rollback()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/playlists/favorites", response_model=FavoritesOut, tags=["playlists"], summary="Get Favorites")
def favorites_playlist(session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    _sync_playlists(session, user)
    favorites = get_or_create_favorites(session, user.id)
    session.commit()
    return _native_playlist_out(session, favorites)


@router.get("/playlists", response_model=list[FavoritesOut], tags=["playlists"], summary="List all playlists")
def list_playlists(session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> list[FavoritesOut]:
    # Favorites first — every client relies on that ordering (the iOS Add-to-Playlist sheet renders
    # it as the row that duplicates the heart, CLAUDE.md §14).
    _sync_playlists(session, user)
    favorites = get_or_create_favorites(session, user.id)
    session.commit()
    result: list[FavoritesOut] = [_native_playlist_out(session, favorites)]
    others = session.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id, Playlist.protected.is_(False))
        .order_by(func.lower(Playlist.name).asc())
        .options(selectinload(Playlist.tracks).selectinload(PlaylistTrack.track).selectinload(Track.album).selectinload(Album.artist))
    )
    result.extend(_native_playlist_out(session, pl) for pl in others)
    return result


@router.post("/playlists", response_model=FavoritesOut, tags=["playlists"], summary="Create playlist")
def create_playlist(payload: PlaylistCreate, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    existing = session.scalar(select(Playlist).where(Playlist.user_id == user.id, func.lower(Playlist.name) == name.lower()))
    if existing:
        raise HTTPException(status_code=409, detail="A playlist with that name already exists")
    playlist = Playlist(name=name, user_id=user.id, protected=False)
    session.add(playlist)
    session.flush()
    client, jf_user_id = _jf_client(session, user)
    if client:
        with client:
            _mirror_create(session, client, jf_user_id, playlist)
    session.commit()
    return _native_playlist_out(session, playlist)


@router.patch("/playlists/{playlist_id}", response_model=FavoritesOut, tags=["playlists"], summary="Rename playlist")
def rename_playlist(playlist_id: str, payload: PlaylistUpdate, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist or playlist.protected:
        raise HTTPException(status_code=404, detail="Playlist not found")
    conflict = session.scalar(select(Playlist).where(Playlist.user_id == user.id, func.lower(Playlist.name) == name.lower(), Playlist.id != playlist.id))
    if conflict:
        raise HTTPException(status_code=409, detail="A playlist with that name already exists")
    playlist.name = name
    client, jf_user_id = _jf_client(session, user)
    if client and playlist.jellyfin_playlist_id:
        with client:
            _mirror_rename(client, jf_user_id, playlist.jellyfin_playlist_id, name)
    session.commit()
    return _native_playlist_out(session, playlist)


@router.delete("/playlists/{playlist_id}", tags=["playlists"], summary="Delete playlist")
def delete_playlist(playlist_id: str, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> dict:
    """Delete a playlist, everywhere it is known.

    ⚠️ This route used to answer `{}` — success — in three cases where it had deleted nothing:
    a playlist id that matched no row, a `protected` one, and (the common one) a Jellyfin
    `DELETE /Items/{id}` that came back 401/403 because the linked Jellyfin account lacks
    `EnableContentDeletion`. The playlist reappeared on the client's next refresh, which is what
    "I can't delete playlists" was. Silence is now an error, and the local row is the thing that
    decides the outcome — Jellyfin is only a mirror and cannot veto a local delete.
    """
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if playlist.protected:
        raise HTTPException(status_code=400, detail="Favorites cannot be deleted")

    jf_playlist_id = playlist.jellyfin_playlist_id
    jellyfin_deleted: bool | None = None
    client, _ = _jf_client(session, user)
    if client and jf_playlist_id:
        with client:
            jellyfin_deleted = _mirror_delete(client, jf_playlist_id)

    # Both ids are forgotten: pins and covers were recorded under whichever id was public at the
    # time, and a mirrored playlist has been exposed under its Jellyfin id (_public_playlist_id).
    _forget_playlist(session, playlist.id)
    if jf_playlist_id:
        _forget_playlist(session, jf_playlist_id)
    session.delete(playlist)
    session.commit()

    if jellyfin_deleted is False:
        # Deleted here, still in Jellyfin. Without a tombstone the next inbound mirror sees an
        # unmatched Jellyfin playlist and faithfully re-creates it, so the delete would appear to
        # undo itself a few seconds later. Not an HTTP error: the playlist IS gone from Nudibranch,
        # which is what was asked.
        set_app_setting(session, f"{_JF_TOMBSTONE_PREFIX}{user.id}:{jf_playlist_id}", playlist_id)
        session.commit()
        write_app_log(
            f"Deleted playlist {playlist_id} locally but Jellyfin refused to delete it "
            f"(the linked Jellyfin account likely lacks content-deletion permission)",
            "warning",
        )
    return {"deleted": True, "jellyfin_deleted": jellyfin_deleted}


def _forget_playlist(session: Session, playlist_id: str) -> None:
    """Drops what a deleted playlist leaves behind.

    ⚠️ A pin is the visible one: `/me/home` serves pinned playlists straight from `PinnedPlaylist`
    (name and all — it deliberately does no per-playlist Jellyfin fan-out to resolve them), so a
    deleted playlist kept appearing as a home chip that opened an empty screen, on every device,
    with no way to remove it. Neither table has a foreign key to the playlist — a playlist id is a
    native row id **or** an opaque Jellyfin item id — so nothing cascades on its own.
    """
    session.execute(
        PinnedPlaylist.__table__.delete().where(PinnedPlaylist.__table__.c.playlist_id == playlist_id)
    )
    cover = session.get(PlaylistCover, playlist_id)
    if cover:
        path = Path(cover.cover_path)
        if path.exists() and path.is_file() and _playlist_covers_root().resolve() in path.resolve().parents:
            path.unlink(missing_ok=True)
        session.delete(cover)


def _user_owns_playlist(session: Session, user: User, playlist_id: str) -> bool:
    """Ownership check for the mutating cover routes — a user may only replace/remove the cover of
    a playlist they can actually manage. One local lookup now that native rows exist for every
    user; it used to cost a Jellyfin round-trip per cover write. Skipped for GET (cover serving
    mirrors the album/artist cover routes, which don't re-validate ownership on every image load)."""
    return _native_playlist_query(session, user.id, playlist_id) is not None


def _jf_push_cover(session: Session, user: User, playlist_id: str, content: bytes) -> None:
    """Push playlist art to the mirrored Jellyfin playlist. Jellyfin takes a base64 body with the
    image's content type as the Content-Type header — not multipart, and not raw bytes."""
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist or not playlist.jellyfin_playlist_id:
        return
    client, _ = _jf_client(session, user)
    if not client:
        return
    content_type = {
        ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp",
    }.get(_sniff_image_extension(content) or "", "image/jpeg")
    with client:
        try:
            client.post(
                f"/Items/{playlist.jellyfin_playlist_id}/Images/Primary",
                content=base64.b64encode(content),
                headers={"Content-Type": content_type},
            )
        except Exception:
            pass


def _jf_delete_cover(session: Session, user: User, playlist_id: str) -> None:
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist or not playlist.jellyfin_playlist_id:
        return
    client, _ = _jf_client(session, user)
    if not client:
        return
    with client:
        try:
            client.delete(f"/Items/{playlist.jellyfin_playlist_id}/Images/Primary")
        except Exception:
            pass


def _jf_pull_cover(session: Session, client: httpx.Client, playlist: Playlist) -> bool:
    """Adopt Jellyfin's playlist art when Nudibranch has none of its own.

    ⚠️ Called only for a playlist the mirror has just discovered, never for every playlist on
    every pull — this is an image fetch, and `/playlists` is polled. A local cover always wins
    besides: it was uploaded here explicitly, and Jellyfin auto-generates collage art for
    playlists which would otherwise replace it.
    """
    if not playlist.jellyfin_playlist_id:
        return False
    public_id = _public_playlist_id(playlist)
    if _playlist_has_cover(session, public_id):
        return False
    try:
        resp = client.get(f"/Items/{playlist.jellyfin_playlist_id}/Images/Primary")
        if not resp.is_success or not resp.content:
            return False
        content = resp.content
    except Exception:
        return False
    ext = _sniff_image_extension(content)
    if not ext:
        return False
    folder = _playlist_covers_root()
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{public_id}{ext}"
    destination.write_bytes(content)
    existing = session.get(PlaylistCover, public_id)
    if existing:
        existing.cover_path = str(destination)
    else:
        session.add(PlaylistCover(playlist_id=public_id, cover_path=str(destination)))
    return True


@router.get("/playlists/{playlist_id}/cover", tags=["playlists"], summary="Get playlist cover art", response_class=FileResponse)
def playlist_cover(
    playlist_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.playlists_manage not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    resolved = _playlist_local_cover_path(session, playlist_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Playlist cover not found")
    return FileResponse(resolved)


@router.post("/playlists/{playlist_id}/cover", tags=["playlists"], summary="Upload playlist cover art")
async def upload_playlist_cover(
    playlist_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> dict:
    if not user.is_admin and not _user_owns_playlist(session, user, playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    content = await file.read()
    ext = _sniff_image_extension(content)
    if not ext:
        raise HTTPException(status_code=400, detail="File is not a valid image (jpg, png, webp, gif, bmp)")
    content, ext = square_cover_bytes(content, ext)
    folder = _playlist_covers_root()
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{playlist_id}{ext}"
    existing = session.get(PlaylistCover, playlist_id)
    if existing and Path(existing.cover_path) != destination:
        old = Path(existing.cover_path)
        if old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    destination.write_bytes(content)
    if existing:
        existing.cover_path = str(destination)
    else:
        session.add(PlaylistCover(playlist_id=playlist_id, cover_path=str(destination)))
    session.commit()
    _jf_push_cover(session, user, playlist_id, content)
    write_app_log(f"Uploaded playlist cover for {playlist_id}", source="upload", kind="playlist_cover")
    return {"has_cover": True}


@router.delete("/playlists/{playlist_id}/cover", tags=["playlists"], summary="Remove playlist cover art")
def delete_playlist_cover(
    playlist_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> dict:
    if not user.is_admin and not _user_owns_playlist(session, user, playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    existing = session.get(PlaylistCover, playlist_id)
    if existing:
        old = Path(existing.cover_path)
        if old.exists() and old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
        session.delete(existing)
        session.commit()
    _jf_delete_cover(session, user, playlist_id)
    return {"has_cover": False}


@router.post("/playlists/{playlist_id}/tracks", response_model=FavoritesOut, tags=["playlists"], summary="Add tracks to playlist")
def add_playlist_tracks(playlist_id: str, payload: PlaylistAddTracks, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    _add_tracks_to_native_playlist(session, playlist, payload.track_ids)
    session.flush()
    session.refresh(playlist)
    # A track that Jellyfin has never scanned is still added locally and simply isn't mirrored —
    # the old Jellyfin path rejected the whole request with "run a sync first", which made adding
    # a freshly imported song to a playlist fail outright for Jellyfin-linked users.
    client, jf_user_id = _jf_client(session, user)
    if client:
        with client:
            if playlist.protected:
                _mirror_favorites(session, client, jf_user_id, playlist)
            else:
                _mirror_set_tracks(session, client, jf_user_id, playlist)
    session.commit()
    session.refresh(playlist)
    return _native_playlist_out(session, playlist)


@router.delete("/playlists/{playlist_id}/tracks/{track_id}", response_model=FavoritesOut, tags=["playlists"], summary="Remove track from playlist")
def remove_playlist_track(playlist_id: str, track_id: str, session: Session = Depends(get_session), user: User = Depends(require_permission(Permission.playlists_manage))) -> FavoritesOut:
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    for entry in list(playlist.tracks):
        if entry.track_id == track_id:
            session.delete(entry)
    session.flush()
    session.refresh(playlist)
    client, jf_user_id = _jf_client(session, user)
    if client:
        with client:
            if playlist.protected:
                _mirror_favorites(session, client, jf_user_id, playlist)
            else:
                _mirror_set_tracks(session, client, jf_user_id, playlist)
    session.commit()
    session.refresh(playlist)
    return _native_playlist_out(session, playlist)


# ── Sharing ───────────────────────────────────────────────────────────────────
# A share is an offer, not a transfer: the recipient accepts, and gets an independent COPY
# materialized in whichever backend they use. Nothing about the sender's playlist is linked to it
# afterwards. Sharing is gated on `playlists:manage` on both sides — a user who can't manage
# playlists has nowhere for a copy to land.


def _share_out(session: Session, share: PlaylistShare) -> PlaylistShareOut:
    track_ids = json.loads(share.track_ids or "[]")
    available = session.scalar(
        select(func.count(Track.id)).where(Track.id.in_(track_ids))
    ) if track_ids else 0
    sender = session.get(User, share.from_user_id)
    return PlaylistShareOut(
        id=share.id,
        from_user_id=share.from_user_id,
        from_user_name=sender.display_name if sender else "Someone",
        to_user_id=share.to_user_id,
        name=share.name,
        track_count=len(track_ids),
        available_track_count=int(available or 0),
        status=share.status,
        created_at=share.created_at,
        accepted_playlist_id=share.accepted_playlist_id,
    )


def _can_manage_playlists(user: User) -> bool:
    return user.is_admin or any(up.permission == Permission.playlists_manage for up in user.permissions)


@router.get("/playlists/share-targets", response_model=list[PlaylistShareTargetOut], tags=["playlists"], summary="Users a playlist can be shared with")
def list_share_targets(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> list[PlaylistShareTargetOut]:
    """Everyone who could receive a playlist, excluding the caller.

    ⚠️ Deliberately NOT `GET /users`, which requires `users:manage`. Sharing is an ordinary
    playlist action, so requiring the admin-adjacent user directory to use it would put it out of
    reach of exactly the accounts most likely to want it. Only id and display name leave here.
    """
    return [
        PlaylistShareTargetOut(id=other.id, display_name=other.display_name, username=other.username)
        for other in session.scalars(select(User).order_by(func.lower(User.display_name)))
        if other.id != user.id and _can_manage_playlists(other)
    ]


@router.get("/playlists/shares", response_model=list[PlaylistShareOut], tags=["playlists"], summary="Playlists other users have shared with me")
def list_playlist_shares(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> list[PlaylistShareOut]:
    shares = session.scalars(
        select(PlaylistShare)
        .where(PlaylistShare.to_user_id == user.id, PlaylistShare.status == "pending")
        .order_by(PlaylistShare.created_at.desc())
    )
    return [_share_out(session, share) for share in shares]


@router.post("/playlists/{playlist_id}/share", response_model=PlaylistShareOut, tags=["playlists"], summary="Share a playlist with another user")
def share_playlist(
    playlist_id: str,
    payload: PlaylistShareRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> PlaylistShareOut:
    playlist = _native_playlist_query(session, user.id, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    recipient = session.get(User, payload.to_user_id)
    if not recipient or recipient.id == user.id:
        raise HTTPException(status_code=404, detail="That user isn't available to share with")
    if not _can_manage_playlists(recipient):
        raise HTTPException(status_code=400, detail=f"{recipient.display_name} can't receive playlists")

    track_ids = [pt.track_id for pt in sorted(playlist.tracks, key=lambda pt: pt.position or 0)]
    # Re-sharing the same playlist to the same person refreshes the pending offer instead of
    # stacking a second notification for what the recipient sees as one thing.
    share = session.scalar(
        select(PlaylistShare).where(
            PlaylistShare.to_user_id == recipient.id,
            PlaylistShare.from_user_id == user.id,
            PlaylistShare.source_playlist_id == playlist_id,
        )
    )
    if share:
        share.name, share.track_ids = playlist.name, json.dumps(track_ids)
        share.status, share.responded_at, share.accepted_playlist_id = "pending", None, None
        share.created_at = datetime.now(timezone.utc)
    else:
        share = PlaylistShare(
            from_user_id=user.id,
            to_user_id=recipient.id,
            source_playlist_id=playlist_id,
            name=playlist.name,
            track_ids=json.dumps(track_ids),
        )
        session.add(share)
    session.flush()

    create_notification(
        session,
        title="Playlist shared with you",
        body=f"{user.display_name} shared “{playlist.name}” ({len(track_ids)} tracks) with you.",
        event_type="playlist_shared",
        target_url="/playlists/shares",
        user_id=recipient.id,
        group_key=f"playlist-share:{share.id}",
    )
    session.commit()
    write_app_log(f"{user.username} shared playlist '{playlist.name}' with {recipient.username}", source="playlists", kind="share")
    return _share_out(session, share)


@router.post("/playlists/shares/{share_id}/accept", response_model=FavoritesOut, tags=["playlists"], summary="Accept a shared playlist")
def accept_playlist_share(
    share_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> FavoritesOut:
    """Materialize the shared copy in the recipient's OWN store.

    The copy is always created natively and then mirrored out by the normal push, which is what
    makes "copied to the type the receiving user has" fall out automatically: a recipient with
    Jellyfin linked gets a real Jellyfin playlist, one without gets native rows only, and neither
    side of the share needs to know which the other uses.
    """
    share = session.get(PlaylistShare, share_id)
    if not share or share.to_user_id != user.id:
        raise HTTPException(status_code=404, detail="Shared playlist not found")
    if share.status != "pending":
        raise HTTPException(status_code=409, detail="That share has already been answered")

    # Only tracks this library still has. A share captured ids at send time and the library can
    # change underneath it, so a missing track is expected, not an error.
    wanted = json.loads(share.track_ids or "[]")
    present = {t.id for t in session.scalars(select(Track).where(Track.id.in_(wanted)))} if wanted else set()
    track_ids = [tid for tid in wanted if tid in present]

    name = share.name
    if session.scalar(select(Playlist).where(Playlist.user_id == user.id, func.lower(Playlist.name) == name.lower())):
        # Names are unique per user, and the recipient may well already have one by this name.
        suffix = 2
        while session.scalar(select(Playlist).where(Playlist.user_id == user.id, func.lower(Playlist.name) == f"{name} ({suffix})".lower())):
            suffix += 1
        name = f"{name} ({suffix})"

    playlist = Playlist(name=name, user_id=user.id, protected=False)
    session.add(playlist)
    session.flush()
    _add_tracks_to_native_playlist(session, playlist, track_ids)
    session.flush()
    session.refresh(playlist)

    client, jf_user_id = _jf_client(session, user)
    if client:
        with client:
            _mirror_create(session, client, jf_user_id, playlist)

    # Carry the sender's cover over, so a shared playlist arrives looking like the one that was sent.
    source_cover = _playlist_local_cover_path(session, share.source_playlist_id)
    if source_cover:
        folder = _playlist_covers_root()
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{_public_playlist_id(playlist)}{source_cover.suffix}"
        try:
            destination.write_bytes(source_cover.read_bytes())
            session.add(PlaylistCover(playlist_id=_public_playlist_id(playlist), cover_path=str(destination)))
        except OSError:
            pass

    share.status = "accepted"
    share.responded_at = datetime.now(timezone.utc)
    share.accepted_playlist_id = _public_playlist_id(playlist)
    session.commit()
    session.refresh(playlist)
    return _native_playlist_out(session, playlist)


@router.post("/playlists/shares/{share_id}/decline", tags=["playlists"], summary="Decline a shared playlist")
def decline_playlist_share(
    share_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.playlists_manage)),
) -> dict:
    share = session.get(PlaylistShare, share_id)
    if not share or share.to_user_id != user.id:
        raise HTTPException(status_code=404, detail="Shared playlist not found")
    share.status = "declined"
    share.responded_at = datetime.now(timezone.utc)
    session.commit()
    return {"declined": True}


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
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "jellyfin_scan", {}))


@router.post("/tools/remap-tracks", response_model=TaskOut, tags=["tools"], summary="Remap Nudibranch tracks to Jellyfin item IDs")
def tool_remap_tracks(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "sync_favorites_jellyfin", {}))


@router.post("/tools/check-files", response_model=TaskOut, tags=["tools"], summary="Check library files for issues")
def tool_check_files(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_files", {}))


@router.post("/tools/check-duplicates", response_model=TaskOut, tags=["tools"], summary="Check for duplicate files")
def tool_check_duplicates(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_duplicates", {}))


@router.post("/tools/check-lyrics", response_model=TaskOut, tags=["tools"], summary="Check for missing lyrics")
def tool_check_lyrics(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_lyrics", {}))


@router.post("/tools/check-musicbrainz-ids", response_model=TaskOut, tags=["tools"], summary="Fill missing MusicBrainz IDs")
def tool_check_musicbrainz_ids(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_musicbrainz_ids", {}))


@router.post("/tools/check-audio-content", response_model=TaskOut, tags=["tools"], summary="Verify audio matches metadata")
def tool_check_audio_content(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_audio_content", {}))


@router.post("/tools/check-album-covers", response_model=TaskOut, tags=["tools"], summary="Check for missing album art")
def tool_check_album_covers(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_album_covers", {}))


@router.post("/tools/check-artist-covers", response_model=TaskOut, tags=["tools"], summary="Check for missing artist art")
def tool_check_artist_covers(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_artist_covers", {}))


@router.post("/tools/refresh-covers", response_model=TaskOut, tags=["tools"], summary="Re-fetch low-resolution album covers")
def tool_refresh_covers(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "refresh_covers", {}))


@router.post("/tools/check-files/fix", response_model=ProposalBatchOut, tags=["tools"], summary="Apply file check fix")
def propose_check_file_fix(
    payload: CheckFileFixRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
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
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_missing_tracks", {}))


@router.post("/tools/check-non-lossless", response_model=TaskOut, tags=["tools"], summary="Check for non-lossless files")
def tool_check_non_lossless(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "check_non_lossless", {}))


@router.post("/tools/apply-replaygain", response_model=TaskOut, tags=["tools"], summary="Measure + apply ReplayGain (review-gated)")
def tool_apply_replaygain(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "apply_replaygain", {}))


@router.post("/tools/consolidate-folders", response_model=TaskOut, tags=["tools"], summary="Consolidate album folders")
def tool_consolidate_folders(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "consolidate_folders", {}))


@router.post("/tools/clear-downloads", response_model=TaskOut, tags=["tools"], summary="Clear completed downloads")
def tool_clear_downloads(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "clear_downloads", {}))


@router.post("/tools/backup", response_model=TaskOut, tags=["tools"], summary="Create library backup")
def tool_backup(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "backup_now", {}))


@router.get("/tools/backups", tags=["tools"], summary="List available backups", response_model=dict)
def list_backups(
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> dict:
    settings = get_settings()
    settings.backups_path.mkdir(parents=True, exist_ok=True)
    backups = sorted(settings.backups_path.glob("nudibranch-*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)
    return {"backups": [{"path": str(path), "name": path.name, "size_bytes": path.stat().st_size} for path in backups]}


@router.post("/tools/restore-default", response_model=TaskOut, tags=["tools"], summary="Restore from latest backup")
def tool_restore_default(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "restore_default", {}))


@router.post("/tools/restore-backup", response_model=TaskOut, tags=["tools"], summary="Restore from specific backup")
def tool_restore_backup(
    payload: BackupRestoreRequest,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.tools_manage)),
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


# A batch whose items are all in terminal states (completed/rejected/failed) renders as
# NOTHING in the frontend (groupApprovalBatches keeps only items whose status is in this set),
# yet /approvals + /wishlist/approvals select on BATCH status — so a batch left
# "pending"/"approved"/"failed" after every item settled lingers in the API while invisible in
# the UI, with no way for the user to clear it (the stale-batch mismatch). Reconcile-on-read
# marks such settled batches completed so the API matches what the UI shows. A failed item here
# means an attempted item produced a recorded result (for example, lyrics were not found); it is
# not unfinished work and must not keep a completed mixed-result batch in the Task Queue.
_UI_ACTIVE_ITEM_STATUSES = {
    ProposalStatus.pending,
    ProposalStatus.approved,
    ProposalStatus.executing,
}
_UI_SETTLED_ITEM_STATUSES = {
    ProposalStatus.completed,
    ProposalStatus.rejected,
    ProposalStatus.failed,
}


def prune_settled_batches(session: Session, batches: list[ProposalBatch]) -> list[ProposalBatch]:
    settled: set[str] = set()
    for batch in batches:
        if batch.items:
            actionable_items = [
                item for item in batch.items
                if item.selected and approval_item_is_actionable(item)
            ]
            if actionable_items and all(item.status in _UI_SETTLED_ITEM_STATUSES for item in actionable_items):
                batch.status = ProposalStatus.completed
                settled.add(batch.id)
            elif not actionable_items and not any(item.selected and item.status in _UI_ACTIVE_ITEM_STATUSES for item in batch.items):
                batch.status = ProposalStatus.completed
                settled.add(batch.id)
        elif batch.kind != ProposalKind.download:
            # An empty non-download batch is an abandoned/failed tool run — safe to retire.
            batch.status = ProposalStatus.completed
            settled.add(batch.id)
        elif batch.created_at and as_utc(batch.created_at) < datetime.now(timezone.utc) - timedelta(minutes=10):
            # An empty DOWNLOAD batch is left alone while fresh — a candidate search commits its
            # batch before attaching items and must not be finalized mid-search — but one older
            # than any plausible in-flight search is a dead leftover and can be retired too.
            batch.status = ProposalStatus.completed
            settled.add(batch.id)
    if settled:
        session.commit()
    return [batch for batch in batches if batch.id not in settled]


def approval_item_is_actionable(item: ProposalItem) -> bool:
    """Ignore selected tree containers when deciding whether a batch still has work."""
    payload = json.loads(item.payload_json or "{}")
    if item.kind == ProposalKind.import_files:
        return bool(item.old_value and item.new_value)
    if item.kind == ProposalKind.metadata:
        return bool(payload.get("target_type"))
    if item.kind in {ProposalKind.delete, ProposalKind.file_move, ProposalKind.playlist, ProposalKind.download, ProposalKind.lyrics}:
        return bool(payload.get("action"))
    return False


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
    batches = prune_settled_batches(session, batches)
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
    reject_items(session, batch_id, payload.item_ids)
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


@router.get("/settings/connections", tags=["settings"], summary="Live connection status for slskd and Jellyfin")
def get_connection_status(
    session: Session = Depends(get_session),
    _: User = Depends(get_current_user),
) -> dict:
    """Probe the configured slskd/Jellyfin servers so the Settings → Status panel can show whether
    they're reachable. Short timeouts; any non-2xx/3xx or exception is reported as 'error'."""
    settings = integration_settings(session)

    def probe(url: str, path: str, headers: dict) -> str:
        try:
            response = httpx.get(f"{url.rstrip('/')}{path}", headers=headers, timeout=4)
            return "connected" if response.status_code < 400 else "error"
        except Exception:
            return "error"

    slskd_url = settings.get("slskd_url", "")
    slskd_key = settings.get("slskd_api_key", "")
    jellyfin_url = settings.get("jellyfin_url", "")
    jellyfin_key = settings.get("jellyfin_api_key", "")
    return {
        "slskd": probe(slskd_url, "/api/v0/application", {"X-API-Key": slskd_key} if slskd_key else {}) if slskd_url else "disabled",
        "jellyfin": probe(jellyfin_url, "/System/Info", {"X-Emby-Token": jellyfin_key}) if (jellyfin_url and jellyfin_key) else "disabled",
    }


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


@router.get("/settings/match-tuning", tags=["settings"], summary="Get download (slskd) match tuning")
def get_match_tuning(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> dict:
    return {"schema": match_tuning_schema(), "values": match_tuning(session)}


@router.put("/settings/match-tuning", tags=["settings"], summary="Update download (slskd) match tuning")
def put_match_tuning(
    payload: dict,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.settings_manage)),
) -> dict:
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    updated = update_match_tuning(session, values or {})
    session.commit()
    return {"schema": match_tuning_schema(), "values": updated}


@router.get("/notifications", response_model=list[NotificationOut], tags=["notifications"], summary="List notifications")
def list_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[NotificationOut]:
    query = select(Notification).where(
        (Notification.user_id == user.id)
        & (Notification.status != NotificationStatus.dismissed)
        & (Notification.deliver_web.is_(True))
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
    # ⚠️ Every handle this route has is unstable in some direction, so it tries them in order of
    # how much each one actually proves, and collapses whatever the earlier ones missed:
    #   - device_id  — the row we handed this install last time. Survives a re-minted grant AND a
    #                  rename; the app echoes it back. Missing only before the first registration
    #                  and after a reinstall.
    #   - proxy_grant — the pairing credential. The grant is re-minted
    #                  whenever pairing is repaired or App Attest re-registers, so on its own it
    #                  inserted a fresh row every time while the old row stayed enabled with a
    #                  grant the proxy still resolved to the same phone: one extra copy of every
    #                  push, per re-pair, forever.
    #   - device_name — the app's per-install label. It is user-editable (it doubles as the session
    #                  label), so a rename splits the device in two the same way.
    # Note the server cannot see the APNS token in proxy mode, so it can never be certain two rows
    # are one phone. The proxy can, and retires superseded registrations on that basis.
    candidates: list[MobileDevice] = []
    if payload.device_id:
        candidates += list(
            session.scalars(
                select(MobileDevice).where(
                    MobileDevice.id == payload.device_id,
                    MobileDevice.user_id == user.id,
                )
            )
        )
    credential_match = MobileDevice.proxy_grant == payload.proxy_grant
    candidates += list(
        session.scalars(
            select(MobileDevice)
            .where(MobileDevice.user_id == user.id, credential_match)
            .order_by(MobileDevice.created_at.asc())
        )
    )
    if payload.device_name:
        candidates += list(
            session.scalars(
                select(MobileDevice)
                .where(
                    MobileDevice.user_id == user.id,
                    MobileDevice.device_name == payload.device_name,
                )
                .order_by(MobileDevice.created_at.asc())
            )
        )
    existing = candidates[0] if candidates else None
    muted = (
        ",".join(sorted({value.strip() for value in payload.muted_event_types if value.strip()}))
        if payload.muted_event_types is not None
        else None
    )
    if existing:
        # Collapse every other row this registration matched, or each one keeps delivering its own
        # copy of every push. Deduped by id: the three lookups above overlap.
        seen = {existing.id}
        for stale in candidates:
            if stale.id in seen:
                continue
            seen.add(stale.id)
            session.delete(stale)
        existing.device_name = payload.device_name
        existing.proxy_grant = payload.proxy_grant
        if muted is not None:
            existing.muted_event_types = muted
        existing.enabled = True
        session.commit()
        return {
            "device_id": existing.id,
            "enabled": existing.enabled,
            "muted_event_types": _split_muted(existing.muted_event_types),
        }
    device = MobileDevice(
        user_id=user.id,
        device_name=payload.device_name,
        proxy_grant=payload.proxy_grant,
        muted_event_types=muted or "",
    )
    session.add(device)
    session.commit()
    return {
        "device_id": device.id,
        "enabled": device.enabled,
        "muted_event_types": _split_muted(device.muted_event_types),
    }


def _split_muted(value: str | None) -> list[str]:
    if not value:
        return []
    return sorted({item.strip() for item in value.split(",") if item.strip()})


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


@router.post("/notifications/read", tags=["notifications"], summary="Mark notifications as read", response_model=dict)
def mark_notifications_read(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    notifications = list(session.scalars(select(Notification).where(Notification.user_id == user.id)))
    for notification in notifications:
        if notification.status == NotificationStatus.unread:
            notification.status = NotificationStatus.read
    session.commit()
    return {"updated": len(notifications)}


@router.post("/notifications/test", tags=["notifications"], summary="Send myself a test notification", response_model=NotificationOut)
def send_test_notification(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> NotificationOut:
    """Create a durable notification for the current user through the real worker/APNS path so the
    end-to-end push pipeline (not just local authorization) can be tested from the app."""
    notification = create_notification(
        session,
        title="Test notification",
        body="If you can see this, notifications from your Nudibranch server are working.",
        event_type="notification_test",
        target_url="/notifications",
        user_id=user.id,
        deliver_web=True,
        deliver_apns=True,
    )
    if notification is None:
        raise HTTPException(status_code=500, detail="Notification could not be created")
    return NotificationOut.model_validate(notification, from_attributes=True)


@router.delete("/notifications", tags=["notifications"], summary="Dismiss all notifications", response_model=dict)
def clear_notifications(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    notifications = list(session.scalars(select(Notification).where(Notification.user_id == user.id)))
    for notification in notifications:
        notification.status = NotificationStatus.dismissed
    session.commit()
    return {"cleared": len(notifications)}


def editable_fields(target_type: str) -> set[str]:
    """Fields a client may set through `/library/metadata/apply`.

    ⚠️ `cover_path` is deliberately **absent**. It is a filesystem path, and clients were writing
    candidate URLs into it — `apply_album_changes` then quietly downloaded the URL to keep
    `GET .../cover` from 404ing, which meant the field's contract said one thing and the wire said
    another. Cover changes go through the dedicated routes instead: multipart `/cover` for an
    upload, `/cover-from-url` for a candidate pick. `cover_locked` stays editable — it's a flag.
    """
    # "artist" is a virtual field (not a column): on album/track it reassigns the owning
    # artist — see apply_album_changes / apply_metadata_item track branch in the worker.
    if target_type == "artist":
        return {"name", "sort_name", "musicbrainz_id", "cover_locked"}
    if target_type == "album":
        return {
            "title",
            "sort_name",
            "release_title",
            "path",
            "cover_locked",
            "musicbrainz_release_id",
            "musicbrainz_release_group_id",
            "artist",
        }
    if target_type == "track":
        # Mirror the worker's canonical apply-side set so the two never drift — a field
        # missing here is filtered to an empty changeset → spurious 400 (this is exactly
        # how the ReplayGain field silently 400'd before). Lazy import dodges the cycle.
        from nudibranch.worker.main import editable_track_fields

        return editable_track_fields() | {"artist"}
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
            replaygain_track_gain=entry.track.replaygain_track_gain,
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
        remote_playback_enabled=bool(getattr(user, "remote_playback_enabled", True)),
        search_min_confidence=user.search_min_confidence if user.search_min_confidence is not None else 0.4,
        library_page_size=user.library_page_size if user.library_page_size is not None else 100,
        jellyfin_user_id=user.jellyfin_user_id or None,
        home_layout=_home_layout(user),
        home_layout_web=_home_layout_web(user),
        online=any(_is_online(s.last_used_at) for s in user.auth_sessions),
    )


def _home_layout(user: User) -> dict[str, list[str]]:
    """Never let a malformed stored blob break /me — the whole session restore depends on it."""
    if not user.home_layout:
        return {}
    try:
        value = json.loads(user.home_layout)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): [str(item) for item in ids]
        for key, ids in value.items()
        if isinstance(ids, list)
    }


def _home_layout_web(user: User) -> dict[str, list[str]]:
    """Web-only mirror of _home_layout — same defensive parsing, same reason: /me must never
    raise on a malformed stored blob."""
    if not user.home_layout_web:
        return {}
    try:
        value = json.loads(user.home_layout_web)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): [str(item) for item in ids]
        for key, ids in value.items()
        if isinstance(ids, list)
    }


def serialize_player_state(user: User) -> dict:
    # State is per session now, but this route answers "what is this person listening to" — one
    # answer per user — so it reports whichever session described its player most recently.
    state = max(
        user.session_player_states,
        key=lambda s: as_utc(s.reported_at) if s.reported_at else datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )
    online = any(_is_online(s.last_used_at) for s in user.auth_sessions)
    if not state:
        return {"user_id": user.id, "user_name": user.display_name, "status": "stopped", "source": "Nudibranch", "online": online}
    updated_at = state.updated_at
    if updated_at and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    stale = bool(updated_at and updated_at < datetime.now(timezone.utc) - timedelta(minutes=10))
    return {
        "user_id": user.id,
        "user_name": user.display_name,
        "track_id": state.track_id,
        "episode_id": state.episode_id,
        "title": state.title,
        "artist": state.artist,
        "album": state.album,
        "status": "stopped" if (stale or not online) else state.status,
        "source": "Nudibranch",
        "queue_length": state.queue_length,
        "current_index": state.current_index,
        "position_seconds": state.position_seconds,
        "duration_seconds": state.duration_seconds,
        "shuffle": state.shuffle,
        "repeat": state.repeat,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "online": online,
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


#: Actions whose `loop`/`shuffle` are an instruction rather than leftover column defaults: `state`
#: exists purely to carry them, and a `play` uses them to decide how the new queue is built.
_MODE_BEARING_ACTIONS = {"play", "state"}

#: Actions that address a position in the target's published queue rather than the transport.
#: They carry a payload about the QUEUE, so like `adopt_handoff` they are exempt from COMMAND_TTL's
#: "an instruction about now" reasoning — but they are still small and idempotent enough to expire.
_QUEUE_ACTIONS = {"jump", "remove", "move"}


def _serialize_command(cmd: PlaybackCommand) -> PlayerCommandOut:
    # ⚠ Withhold mode from every other action. The columns are non-nullable with defaults, so a
    # transport verb always carries "off"/False in the database whether or not the caller meant it —
    # see the note on PlayerCommandOut. Deciding here rather than at the column keeps this a pure
    # serialization change: no migration, and every client is fixed at once by the field simply
    # being absent, which is what their existing null guards already test for.
    # `resume` WITH a target rebuilds a queue and so is a play; a bare `resume` is pure transport.
    # This predicate is mirrored exactly in the iOS client's `isPlay`, and the two must stay in step.
    carries_mode = cmd.action in _MODE_BEARING_ACTIONS or (
        cmd.action == "resume" and cmd.target_id is not None
    )
    return PlayerCommandOut(
        id=cmd.id, action=cmd.action, target_type=cmd.target_type, target_id=cmd.target_id,
        target_label=cmd.target_label,
        loop=cmd.loop if carries_mode else None,
        shuffle=cmd.shuffle if carries_mode else None,
        status=cmd.status,
        device_id=cmd.device_id,
        created_at=as_utc(cmd.created_at) if cmd.created_at else None,
        position_seconds=cmd.position_seconds,
        queue_index=cmd.queue_index,
        queue_to_index=cmd.queue_to_index,
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
    # Look at non-terminal items: complete any whose download finished, and demote stale
    # "approved" ones (abandoned downloads) back to "wanted".
    query = select(WishlistItem).where(WishlistItem.status.in_(["approved", "wanted", "review"]))
    if not user_has_permission(user, Permission.wishlist_approve_all):
        query = query.where(WishlistItem.user_id == user.id)
    items = list(session.scalars(query))
    if not items:
        return
    active_ids = active_wishlist_download_ids(session)
    # A download that finished is no longer "active", so an item whose download COMPLETED must
    # be marked completed — otherwise an item that downloaded + imported reverts to "Awaiting
    # Approval" (the queue_download item went terminal, reconcile then demoted approved→wanted).
    # Keyed on the exact wishlist_item_id carried by the download item, so it works even when
    # mark_matching_wishlist_completed missed it on fuzzy metadata (deluxe titles, feat., quotes).
    completed_ids = completed_wishlist_download_ids(session)
    changed = False
    now = datetime.now(timezone.utc)
    for item in items:
        if item.id in completed_ids:
            if item.status != "completed":
                item.status = "completed"
                item.status_changed_at = now
                changed = True
            continue
        # Only "approved" (a download was queued) demotes when its download is gone; genuine
        # "wanted"/"review" items are left untouched.
        if item.status == "approved" and item.id not in active_ids:
            item.status = "wanted"
            item.status_changed_at = now
            changed = True
    if changed:
        session.commit()


def completed_wishlist_download_ids(session: Session) -> set[str]:
    """Wishlist item ids whose linked download ProposalItem has completed (downloaded+imported).

    Only count ACTUAL download leaves (queue_download / queue_ytdlp_download). A completed
    `wishlist_request` item just means the candidate search ran — search_candidates marks the
    intent batch (and its per-track leaves, which carry wishlist_item_id) completed once the
    search finishes, NOT once anything is fetched. Counting those wrongly marked every album
    "completed" the moment its search ended, so albums slskd couldn't seed (whose yt-dlp
    fallback was never selected/downloaded) silently vanished from the wishlist with 0 tracks.
    """
    ids: set[str] = set()
    batches = list(
        session.scalars(
            select(ProposalBatch)
            .options(selectinload(ProposalBatch.items))
            .where(ProposalBatch.kind == ProposalKind.download)
        )
    )
    for batch in batches:
        for item in batch.items:
            if item.kind != ProposalKind.download or item.status != ProposalStatus.completed:
                continue
            payload = json.loads(item.payload_json or "{}")
            if payload.get("action") not in {"queue_download", "queue_ytdlp_download"}:
                continue
            request = payload.get("request") or {}
            wishlist_item_id = request.get("wishlist_item_id") or payload.get("wishlist_item_id")
            if wishlist_item_id:
                ids.add(wishlist_item_id)
    return ids


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
            # queue_download = slskd; queue_ytdlp_download = the YouTube fallback retry. Both
            # keep the wishlist item "active" so it isn't demoted to wanted while downloading.
            if payload.get("action") not in {"queue_download", "queue_ytdlp_download"} or payload.get("auto_retry_exhausted"):
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
        Permission.discover,
        Permission.wishlist_approve_all,
        Permission.library_view,
    }
    if user.is_admin or any(user_permission.permission in allowed for user_permission in user.permissions):
        return
    raise HTTPException(status_code=403, detail="Not enough permissions")


PERMISSION_LABELS = {
    Permission.library_view: "Browse & play",
    Permission.library_edit: "Edit & manage",
    Permission.discover: "Discover & Wishlist",
    Permission.wishlist_approve_all: "Approve others' requests",
    Permission.import_run: "Import & add",
    Permission.approvals_manage: "Task Queue (approve)",
    Permission.playlists_manage: "Playlists",
    Permission.activity_read: "Activity & logs",
    Permission.tools_manage: "Tools & maintenance",
    Permission.automations_manage: "Automations",
    Permission.users_manage: "Users",
    Permission.settings_manage: "Settings",
}


def permission_label(permission: Permission) -> str:
    return PERMISSION_LABELS.get(permission, permission.value.replace(":", " ").replace("_", " ").title())


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


# ---------------------------------------------------------------------------
# Podcasts (Podcast > Episode). Episodes play through the same web player as
# tracks but live in their own tab and download via the worker's podcast tasks.
# ---------------------------------------------------------------------------

# How many episodes `POST /podcasts` ingests before handing off to the worker — see the note in
# subscribe_podcast. Large enough that a new subscription is immediately usable, small enough that
# the response never approaches a client request timeout.
_SUBSCRIBE_INLINE_EPISODES = 50


def _podcast_local_cover(podcast: Podcast) -> Path | None:
    cover_path = podcast.cover_path
    if not cover_path or cover_path.startswith(("http://", "https://")):
        return None
    path = Path(cover_path)
    if not path.exists() or not path.is_file():
        return None
    resolved = path.resolve()
    podcasts_root = get_settings().podcasts_path.resolve()
    if podcasts_root not in [resolved, *resolved.parents]:
        return None
    return resolved


def _fetch_podcast_cover_now(podcast: Podcast) -> None:
    """Download the feed's cover image synchronously so a freshly-subscribed podcast has real
    art immediately instead of waiting for the next `podcast_scan` worker pass (which otherwise
    left the podcast showing a generated placeholder until the background task ran)."""
    if _podcast_local_cover(podcast) is not None or not podcast.image_url:
        return
    try:
        # Same escalation as the feed fetch: podcast art sits on the same edge as the feed, so a
        # host that refuses our client refuses both, and a subscribe rescued by the fallback would
        # otherwise land with no artwork.
        content, _content_type = podcast_service.fetch_media(podcast.image_url, timeout=15)
        ext = _sniff_image_extension(content)
        if not ext:
            return
        folder = get_settings().podcasts_path / safe_path_part(podcast.title, "Unknown Podcast")
        folder.mkdir(parents=True, exist_ok=True)
        cover_path = folder / f"cover{ext}"
        cover_path.write_bytes(content)
        podcast.cover_path = str(cover_path)
    except (podcast_service.FeedFetchError, httpx.HTTPError, OSError):
        # Cover art is decoration — a subscribe must never fail because the image didn't come
        # down. The next podcast_scan tries again.
        pass


def _podcast_out(session: Session, podcast: Podcast, user_id: str) -> PodcastOut:
    episode_count = session.scalar(select(func.count()).select_from(Episode).where(Episode.podcast_id == podcast.id)) or 0
    played_count = session.scalar(
        select(func.count())
        .select_from(EpisodeProgress)
        .join(Episode, Episode.id == EpisodeProgress.episode_id)
        .where(Episode.podcast_id == podcast.id, EpisodeProgress.user_id == user_id, EpisodeProgress.played.is_(True))
    ) or 0
    notify_pref = session.scalar(
        select(PodcastNotificationPref.enabled).where(
            PodcastNotificationPref.podcast_id == podcast.id,
            PodcastNotificationPref.user_id == user_id,
        )
    )
    return PodcastOut(
        id=podcast.id,
        title=podcast.title,
        author=podcast.author,
        description=podcast.description,
        feed_url=podcast.feed_url,
        has_cover=_podcast_local_cover(podcast) is not None,
        enabled=podcast.enabled,
        episode_count=episode_count,
        # Every episode is playable now that clients stream from the publisher, so "unplayed" is
        # measured against the whole feed rather than against whatever this server had fetched.
        unplayed_count=max(0, episode_count - played_count),
        last_scanned_at=podcast.last_scanned_at,
        last_error=podcast.last_error,
        created_at=podcast.created_at,
        notify_on_new_episodes=bool(notify_pref),
    )


def _episode_out(episode: Episode, progress: EpisodeProgress | None, has_cover: bool) -> EpisodeOut:
    return EpisodeOut(
        id=episode.id,
        podcast_id=episode.podcast_id,
        title=episode.title,
        description=episode.description,
        published_at=episode.published_at,
        duration_ms=episode.duration_ms,
        format=episode.format,
        file_size=episode.file_size,
        season=episode.season,
        episode_number=episode.episode_number,
        enclosure_url=episode.enclosure_url,
        has_cover=has_cover,
        progress=(
            EpisodeProgressOut(
                position_ms=progress.position_ms,
                duration_ms=progress.duration_ms,
                played=progress.played,
                updated_at=progress.updated_at,
            )
            if progress
            else None
        ),
    )


@router.get("/podcasts", response_model=list[PodcastOut], tags=["podcasts"], summary="List subscribed podcasts")
def list_podcasts(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> list[PodcastOut]:
    podcasts = session.scalars(select(Podcast).order_by(func.lower(Podcast.title)))
    return [_podcast_out(session, podcast, user.id) for podcast in podcasts]


@router.get("/podcasts/search", response_model=list[PodcastSearchResult], tags=["podcasts"], summary="Search the podcast directory (iTunes)")
def search_podcasts(
    q: str = Query("", min_length=0),
    limit: int = Query(25, ge=1, le=50),
    _: User = Depends(require_permission(Permission.podcasts_manage)),
) -> list[PodcastSearchResult]:
    term = q.strip()
    if not term:
        return []
    from nudibranch.services.itunes import podcast_search
    return [PodcastSearchResult(**row) for row in podcast_search(term, limit=limit)]


@router.post("/podcasts", response_model=PodcastOut, tags=["podcasts"], summary="Subscribe to a podcast RSS feed")
def subscribe_podcast(
    payload: PodcastSubscribeIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> PodcastOut:
    feed_url = (payload.feed_url or "").strip()
    if not feed_url:
        raise HTTPException(status_code=400, detail="A feed URL is required")
    # ⚠️ Every failure below is written to the app log before it becomes a 400. The client
    # deliberately shows terse copy (no protocol detail), so without this the ONLY record of why a
    # subscribe failed was an HTTP body the user never sees — which is exactly the position this
    # route left us in when a feed that worked everywhere else failed on one server. Settings →
    # Activity is now the place to look.
    try:
        feed = podcast_service.fetch_feed(feed_url)
    except podcast_service.FeedFetchError as error:
        write_app_log(f"Subscribe failed for {feed_url} — {error}", "error", source="podcasts", kind="subscribe_failed")
        raise HTTPException(status_code=400, detail=f"Could not fetch feed: {error}")
    except Exception as error:  # noqa: BLE001 - surface a clean 400 for a bad/unreachable feed.
        write_app_log(
            f"Subscribe failed for {feed_url} — unexpected {type(error).__name__}: {error}",
            "error", source="podcasts", kind="subscribe_failed",
        )
        raise HTTPException(status_code=400, detail=f"Could not fetch feed: {error}")
    entry_count = len(getattr(feed, "entries", None) or [])
    if not entry_count and not getattr(feed, "feed", None):
        # feedparser is lenient, so reaching here usually means an HTML error/consent page was
        # served with a 200 rather than the feed — worth recording what actually arrived.
        bozo = getattr(feed, "bozo_exception", None)
        write_app_log(
            f"Subscribe failed for {feed_url} — response was not a feed" + (f" ({bozo})" if bozo else ""),
            "error", source="podcasts", kind="subscribe_failed",
        )
        raise HTTPException(status_code=400, detail="That URL does not look like a podcast feed")
    podcast = podcast_service.upsert_podcast(session, feed_url, feed)
    # ⚠️ Only the newest slice is ingested on the request path. A long-running show's feed carries
    # its ENTIRE back catalogue — a Patreon feed of 1,142 episodes in ~2 MB is what exposed this —
    # and inserting every one inline pushed the response past the iOS client's 20 s request
    # timeout, so subscribing to a big show silently did nothing. The `podcast_scan` enqueued
    # below ingests the rest; this slice only exists so the podcast opens with episodes in it.
    episodes = podcast_service.parse_episodes(feed)
    # Undated entries sort oldest rather than raising on a None-vs-datetime comparison.
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=lambda item: item["published_at"] or oldest, reverse=True)
    podcast_service.upsert_episodes(session, podcast, episodes[:_SUBSCRIBE_INLINE_EPISODES])
    _fetch_podcast_cover_now(podcast)
    session.commit()
    enqueue_task(session, "podcast_scan", {"podcast_id": podcast.id})
    session.commit()
    write_app_log(f"Subscribed to podcast {podcast.title}", source="podcasts", kind="subscribe")
    return _podcast_out(session, podcast, user.id)


@router.get("/podcasts/{podcast_id}", response_model=PodcastOut, tags=["podcasts"], summary="Podcast detail")
def get_podcast(
    podcast_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> PodcastOut:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    return _podcast_out(session, podcast, user.id)


@router.patch("/podcasts/{podcast_id}", response_model=PodcastOut, tags=["podcasts"], summary="Update a podcast subscription")
def update_podcast(
    podcast_id: str,
    payload: PodcastUpdateIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> PodcastOut:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    # `enabled` is all that is left: whether the daily feed scan includes this subscription. Where
    # and whether episodes are downloaded is a per-device decision, held by each client.
    if payload.enabled is not None:
        podcast.enabled = payload.enabled
    session.commit()
    return _podcast_out(session, podcast, user.id)


@router.put("/podcasts/{podcast_id}/notifications", response_model=PodcastOut, tags=["podcasts"], summary="Set my new-episode notification preference for a podcast")
def set_podcast_notifications(
    podcast_id: str,
    payload: PodcastNotificationIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> PodcastOut:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    pref = session.scalar(
        select(PodcastNotificationPref).where(
            PodcastNotificationPref.podcast_id == podcast_id,
            PodcastNotificationPref.user_id == user.id,
        )
    )
    if pref is None:
        pref = PodcastNotificationPref(user_id=user.id, podcast_id=podcast_id, enabled=payload.enabled)
        session.add(pref)
    else:
        pref.enabled = payload.enabled
    session.commit()
    return _podcast_out(session, podcast, user.id)


@router.delete("/podcasts/{podcast_id}", tags=["podcasts"], summary="Unsubscribe from a podcast")
def delete_podcast(
    podcast_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.podcasts_manage)),
) -> dict:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    episode_ids = list(session.scalars(select(Episode.id).where(Episode.podcast_id == podcast.id)))
    # The only thing on disk is the cover art this server fetched; episode audio was never here.
    folder = get_settings().podcasts_path / safe_path_part(podcast.title, "Unknown Podcast")
    try:
        if folder.exists():
            import shutil as _shutil
            _shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass
    if episode_ids:
        session.execute(delete(EpisodeProgress).where(EpisodeProgress.episode_id.in_(episode_ids)))
        session.execute(update(SessionPlayerState).where(SessionPlayerState.episode_id.in_(episode_ids)).values(episode_id=None))
        session.execute(delete(Episode).where(Episode.podcast_id == podcast.id))
    session.execute(
        delete(PinnedItem).where(PinnedItem.kind == "podcast", PinnedItem.item_id == podcast.id)
    )
    session.delete(podcast)
    session.commit()
    return {"deleted": True}


@router.get("/podcasts/{podcast_id}/episodes", response_model=PaginatedEpisodes, tags=["podcasts"], summary="Paginated episodes of a podcast")
def list_podcast_episodes(
    podcast_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> PaginatedEpisodes:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    has_cover = _podcast_local_cover(podcast) is not None
    stmt = select(Episode).where(Episode.podcast_id == podcast_id)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    episodes = list(
        session.scalars(
            stmt.order_by(Episode.published_at.desc().nullslast()).offset((page - 1) * page_size).limit(page_size)
        )
    )
    progress_map = {
        row.episode_id: row
        for row in session.scalars(
            select(EpisodeProgress).where(
                EpisodeProgress.user_id == user.id,
                EpisodeProgress.episode_id.in_([episode.id for episode in episodes]),
            )
        )
    } if episodes else {}
    items = [_episode_out(episode, progress_map.get(episode.id), has_cover) for episode in episodes]
    return PaginatedEpisodes(items=items, total=total, page=page, page_size=page_size)


@router.post("/podcasts/{podcast_id}/mark-played", response_model=PodcastOut, tags=["podcasts"], summary="Bulk mark a podcast's episodes played/unplayed")
def mark_podcast_episodes_played(
    podcast_id: str,
    payload: MarkEpisodesPlayedIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> PodcastOut:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")

    episode_ids_query = select(Episode.id).where(Episode.podcast_id == podcast_id)
    if payload.scope == "before_oldest_played":
        oldest_played = session.scalar(
            select(func.min(Episode.published_at))
            .join(EpisodeProgress, EpisodeProgress.episode_id == Episode.id)
            .where(
                Episode.podcast_id == podcast_id,
                EpisodeProgress.user_id == user.id,
                EpisodeProgress.played.is_(True),
            )
        )
        # Nothing is played yet, so there is no "before" boundary to catch up to.
        if oldest_played is None:
            return _podcast_out(session, podcast, user.id)
        episode_ids_query = episode_ids_query.where(
            Episode.published_at.is_not(None),
            Episode.published_at < oldest_played,
        )

    episode_ids = list(session.scalars(episode_ids_query))
    if episode_ids:
        existing = {
            row.episode_id: row
            for row in session.scalars(
                select(EpisodeProgress).where(
                    EpisodeProgress.user_id == user.id,
                    EpisodeProgress.episode_id.in_(episode_ids),
                )
            )
        }
        now = datetime.now(timezone.utc)
        for episode_id in episode_ids:
            progress = existing.get(episode_id)
            if not progress:
                progress = EpisodeProgress(user_id=user.id, episode_id=episode_id)
                session.add(progress)
            progress.played = payload.played
            progress.position_ms = 0
            progress.updated_at = now
        session.commit()
    return _podcast_out(session, podcast, user.id)


@router.post("/podcasts/scan", response_model=TaskOut, tags=["podcasts"], summary="Scan all podcast feeds now")
def scan_all_podcasts(
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.podcasts_manage)),
) -> TaskOut:
    return serialize_task(enqueue_task(session, "podcast_scan", {}))


@router.post("/podcasts/{podcast_id}/scan", response_model=TaskOut, tags=["podcasts"], summary="Scan one podcast feed now")
def scan_podcast(
    podcast_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_permission(Permission.podcasts_manage)),
) -> TaskOut:
    podcast = session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    return serialize_task(enqueue_task(session, "podcast_scan", {"podcast_id": podcast.id}))


@router.get("/podcasts/episodes/{episode_id}/progress", response_model=EpisodeProgressOut, tags=["podcasts"], summary="Get episode resume progress")
def get_episode_progress(
    episode_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> EpisodeProgressOut:
    progress = session.scalar(
        select(EpisodeProgress).where(EpisodeProgress.user_id == user.id, EpisodeProgress.episode_id == episode_id)
    )
    if not progress:
        return EpisodeProgressOut()
    return EpisodeProgressOut(
        position_ms=progress.position_ms,
        duration_ms=progress.duration_ms,
        played=progress.played,
        updated_at=progress.updated_at,
    )


@router.put("/podcasts/episodes/{episode_id}/progress", response_model=EpisodeProgressOut, tags=["podcasts"], summary="Update episode resume progress")
def update_episode_progress(
    episode_id: str,
    payload: EpisodeProgressIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_permission(Permission.podcasts_manage)),
) -> EpisodeProgressOut:
    episode = session.get(Episode, episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    progress = session.scalar(
        select(EpisodeProgress).where(EpisodeProgress.user_id == user.id, EpisodeProgress.episode_id == episode_id)
    )
    if not progress:
        progress = EpisodeProgress(user_id=user.id, episode_id=episode_id)
        session.add(progress)
    if payload.position_ms is not None:
        progress.position_ms = max(0, payload.position_ms)
    if payload.duration_ms is not None:
        progress.duration_ms = payload.duration_ms
    if payload.played is not None:
        progress.played = payload.played
    progress.updated_at = datetime.now(timezone.utc)
    session.commit()
    return EpisodeProgressOut(
        position_ms=progress.position_ms,
        duration_ms=progress.duration_ms,
        played=progress.played,
        updated_at=progress.updated_at,
    )


@router.get("/podcasts/episodes/{episode_id}/stream", tags=["podcasts"], summary="Stream episode audio from the publisher")
def stream_episode(
    request: Request,
    episode_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Relay the publisher's enclosure. Nothing is stored, cached, or written to disk.

    ⚠️ **A redirect would be simpler and is wrong.** The web player routes both audio elements
    through `createMediaElementSource`, and a cross-origin resource with no CORS headers taints
    that graph — the page would play in perfect silence, which is exactly the failure nobody would
    connect back to this route. Relaying keeps the audio same-origin. Native clients don't come
    through here at all: they read `enclosure_url` and fetch it directly, so this costs bandwidth
    only for browser playback, and it is also the reason a publisher that refuses this server's
    network still works everywhere except the web.

    Range headers pass through in both directions or seeking a two-hour episode would mean
    downloading it first.
    """
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.podcasts_manage not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    episode = session.get(Episode, episode_id)
    if not episode or not episode.enclosure_url:
        raise HTTPException(status_code=404, detail="Episode not found")

    headers = {"User-Agent": "Nudibranch/1.0", "Accept": "*/*"}
    if range_header := request.headers.get("range"):
        headers["Range"] = range_header
    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
    try:
        upstream = client.send(
            client.build_request("GET", episode.enclosure_url, headers=headers), stream=True
        )
    except httpx.HTTPError as error:
        client.close()
        write_app_log(
            f"Episode stream relay failed for {episode.title}: {type(error).__name__}",
            "warning", source="podcasts",
        )
        raise HTTPException(status_code=502, detail="The publisher could not be reached")
    if upstream.status_code >= 400:
        status = upstream.status_code
        upstream.close()
        client.close()
        write_app_log(
            f"Episode stream relay refused for {episode.title}: host returned {status}",
            "warning", source="podcasts",
        )
        raise HTTPException(status_code=502, detail="The publisher refused the request")

    def relay():
        try:
            yield from upstream.iter_raw(chunk_size=65536)
        finally:
            upstream.close()
            client.close()

    passthrough = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in {"content-length", "content-range", "accept-ranges", "content-type"}
    }
    passthrough.setdefault("accept-ranges", "bytes")
    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get("content-type", "audio/mpeg"),
    )


def _serve_podcast_cover(session: Session, api_key: str, podcast: Podcast | None) -> FileResponse:
    user = resolve_media_user(session, api_key)
    permissions = {permission.permission for permission in user.permissions} if user else set()
    if not user or (not user.is_admin and Permission.podcasts_manage not in permissions):
        raise HTTPException(status_code=401, detail="Invalid API key")
    resolved = _podcast_local_cover(podcast) if podcast else None
    if not resolved:
        raise HTTPException(status_code=404, detail="Podcast cover not found")
    return FileResponse(resolved)


@router.get("/podcasts/{podcast_id}/cover", tags=["podcasts"], summary="Podcast cover art", response_class=FileResponse)
def podcast_cover(
    podcast_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    return _serve_podcast_cover(session, api_key, session.get(Podcast, podcast_id))


@router.get("/podcasts/episodes/{episode_id}/cover", tags=["podcasts"], summary="Episode cover art (falls back to podcast)", response_class=FileResponse)
def episode_cover(
    episode_id: str,
    api_key: str = Query(""),
    session: Session = Depends(get_session),
) -> FileResponse:
    episode = session.get(Episode, episode_id)
    return _serve_podcast_cover(session, api_key, episode.podcast if episode else None)
