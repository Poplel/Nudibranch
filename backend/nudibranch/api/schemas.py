from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from nudibranch.db.models import NotificationStatus, ProposalKind, ProposalStatus, TaskStatus


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=4, max_length=128)
    device_label: str | None = None
    # "ios" | "mac" | "web". Recorded on the session so a device that has never played still shows
    # with the right identity in a device picker.
    client: str | None = None


class LoginResponse(BaseModel):
    user_id: str
    display_name: str
    api_key: str
    is_admin: bool
    username: str | None = None
    expires_at: datetime


class UserOut(BaseModel):
    id: str
    display_name: str
    username: str | None = None
    is_admin: bool
    permissions: list[str]
    theme: str = "light"
    accent_color: str = "#356df3"
    background_tint: str = "#356df3"
    crossfade_duration: float = 0.5
    remote_playback_enabled: bool = True
    search_min_confidence: float = 0.4
    library_page_size: int = 100
    jellyfin_user_id: str | None = None
    home_layout: dict[str, list[str]] = Field(default_factory=dict)
    home_layout_web: dict[str, list[str]] = Field(default_factory=dict)
    online: bool = False


class PermissionOut(BaseModel):
    value: str
    label: str
    section: str


class UserCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=4, max_length=128)
    is_admin: bool = False
    permissions: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    username: str | None = None
    is_admin: bool | None = None
    permissions: list[str] | None = None


class UserPinUpdate(BaseModel):
    password: str = Field(min_length=4, max_length=128)


class OwnPinUpdate(BaseModel):
    """Self-service password change.  Separate from UserPinUpdate because changing your OWN
    password must prove you know the current one — otherwise a borrowed or stolen session token
    becomes permanent account takeover, with the real owner locked out."""

    current_password: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=4, max_length=128)


class HomeLayoutUpdate(BaseModel):
    """A client's own arrangement of its home screen, stored verbatim and never interpreted here.

    Keys are row names the client chooses; values are ordered id lists. The server deliberately
    knows nothing about what an entry means — the iOS home row mixes pinned playlist ids with
    synthetic entries for its built-in Play/Shuffle/Favorites chips, which correspond to no row in
    any table. Bounds are the only validation: this is user-supplied data on a user record."""

    layout: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("layout")
    @classmethod
    def _bounded(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(value) > 16:
            raise ValueError("too many rows")
        for key, ids in value.items():
            if len(key) > 64:
                raise ValueError("row name too long")
            if len(ids) > 500:
                raise ValueError("too many entries")
            if any(len(item) > 128 for item in ids):
                raise ValueError("entry too long")
        return value


class HomeLayoutWebUpdate(BaseModel):
    """Web-only mirror of HomeLayoutUpdate — same opaque, never-interpreted, verbatim-stored
    contract, kept independent so the web UI's arrangement doesn't clobber the iOS app's
    (which owns HomeLayoutUpdate/home_layout). Bounds are the only validation."""

    layout: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("layout")
    @classmethod
    def _bounded(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(value) > 16:
            raise ValueError("too many rows")
        for key, ids in value.items():
            if len(key) > 64:
                raise ValueError("row name too long")
            if len(ids) > 500:
                raise ValueError("too many entries")
            if any(len(item) > 128 for item in ids):
                raise ValueError("entry too long")
        return value


class UserAppearanceUpdate(BaseModel):
    theme: str = Field(pattern="^(light|dark)$")
    accent_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    background_tint: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    crossfade_duration: float = Field(default=0.5, ge=0.0, le=15.0)
    #: ⚠ Optional, and None means LEAVE ALONE — not "restore the default". The web app sends this
    #: body without the field (it predates the setting), so a non-optional default of True would
    #: silently switch cross-device playback back on for anyone who turned it off on another client
    #: and then changed their theme in a browser. Same rule `UserUpdate` follows for its fields.
    remote_playback_enabled: bool | None = None


class JellyfinUserLinkUpdate(BaseModel):
    jellyfin_user_id: str | None = None


class PlayerStateUpdate(BaseModel):
    track_id: str | None = None
    episode_id: str | None = None  # set instead of track_id when a podcast episode is playing
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    status: str = "stopped"
    queue_length: int = 0
    current_index: int = 0
    position_seconds: int | None = None
    duration_seconds: int | None = None
    shuffle: bool = False
    repeat: str = "off"
    # Which client shape is reporting. Coerced rather than rejected: an unrecognised value is a
    # newer or older client, and a status report must never 422 over a cosmetic label.
    client: str | None = None

    @field_validator("client")
    @classmethod
    def _known_client(cls, value: str | None) -> str | None:
        return value if value in {"ios", "mac", "web"} else None
    # What the caller's queue currently hashes to. The server answers `queue_stale` when its
    # stored copy disagrees, which is the client's cue to publish the queue itself.
    queue_hash: str | None = None


class LibraryTreeTrack(BaseModel):
    id: str
    title: str
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    format: str | None = None
    bitrate: int | None = None
    is_lossless: bool = False
    musicbrainz_verified: bool = False
    path: str | None = None
    musicbrainz_recording_id: str | None = None
    explicit: bool | None = None
    metadata_locked: bool = False
    artwork_locked: bool = False
    filename_locked: bool = False
    replaygain_track_gain: float | None = None
    artist_name: str | None = None


class LibraryTreeAlbum(BaseModel):
    id: str
    title: str
    release_title: str | None = None
    sort_name: str | None = None
    path: str | None = None
    cover_path: str | None = None
    cover_locked: bool = False
    musicbrainz_release_id: str | None = None
    musicbrainz_release_group_id: str | None = None
    artist_name: str | None = None
    tracks: list[LibraryTreeTrack] = Field(default_factory=list)


class LibraryTreeArtist(BaseModel):
    id: str
    name: str
    sort_name: str | None = None
    musicbrainz_id: str | None = None
    cover_path: str | None = None
    cover_locked: bool = False
    albums: list[LibraryTreeAlbum] = Field(default_factory=list)


class WishlistCreate(BaseModel):
    kind: str = Field(pattern="^(artist|album|track)$")
    artist: str
    album: str | None = None
    track: str | None = None
    source: str | None = None


class WishlistOut(WishlistCreate):
    id: str
    user_id: str
    owner_name: str | None = None
    status: str
    created_at: datetime
    status_changed_at: datetime


class WishlistApprovalRequest(BaseModel):
    item_ids: list[str] | None = None
    deny_unselected: bool = False


class ProposalItemOut(BaseModel):
    id: str
    batch_id: str
    parent_id: str | None
    title: str
    kind: ProposalKind
    status: ProposalStatus
    selected: bool
    old_value: str | None = None
    new_value: str | None = None
    payload_json: str = "{}"


class ProposalBatchOut(BaseModel):
    id: str
    title: str
    kind: ProposalKind
    status: ProposalStatus
    tree_path: str
    created_at: datetime
    updated_at: datetime
    items: list[ProposalItemOut]


class ProposalSelectionUpdate(BaseModel):
    item_ids: list[str]
    selected: bool


class ProposalApproveRequest(BaseModel):
    item_ids: list[str] | None = None


class ProposalRejectRequest(BaseModel):
    item_ids: list[str] | None = None


class TaskCreate(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskOut(BaseModel):
    id: str
    type: str
    status: TaskStatus
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int
    created_at: datetime
    updated_at: datetime


class LogEntryOut(BaseModel):
    created_at: datetime
    level: str = "info"
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class NotificationOut(BaseModel):
    id: str
    user_id: str | None
    title: str
    body: str
    event_type: str
    target_url: str | None
    status: NotificationStatus
    deliver_web: bool
    deliver_apns: bool
    created_at: datetime


class DeviceRegistration(BaseModel):
    device_name: str
    # The MobileDevice.id a previous registration returned to this install, echoed back so
    # re-registration updates that row instead of inserting another. device_name is the only other
    # handle and it is user-editable (the app reuses its session label), so a rename used to strand
    # the old row — still enabled, still holding a valid grant, still receiving every push.
    device_id: str | None = None
    # The opaque per-pairing grant token the app obtained from the APNS proxy after proving, via
    # App Attest, that it is a genuine build. This is the only supported credential: direct APNS
    # delivery was removed, so a registration without a grant could never be pushed to.
    proxy_grant: str = Field(min_length=1)
    # event_type values this device does not want pushed. Omitted leaves an already-registered
    # device's preferences untouched; [] clears them.
    muted_event_types: list[str] | None = None


class PushIdentityResponse(BaseModel):
    instance_id: str
    public_key: str
    proxy_url: str


class IntegrationSettings(BaseModel):
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    slskd_url: str = ""
    slskd_api_key: str = ""
    slskd_album_match_threshold: str = "72"
    slskd_album_folder_tries: str = "5"
    slskd_concurrent_downloads: str = "1"
    youtube_cookies_browser: str = ""
    youtube_cookies_path: str = ""
    youtube_cookies_uploaded: bool = False
    acoustid_api_key: str = ""
    allow_m4a_downloads: str = "true"
    allow_ytdlp_fallback: str = "false"


class PlaylistTrackOut(BaseModel):
    id: str
    track_id: str
    position: int
    title: str
    artist: str
    album: str
    album_id: str | None = None
    format: str | None = None
    replaygain_track_gain: float | None = None


class FavoritesOut(BaseModel):
    id: str
    name: str
    track_ids: list[str]
    tracks: list[PlaylistTrackOut] = Field(default_factory=list)
    protected: bool = True
    track_count: int = 0
    has_cover: bool = False


class PlaylistPositionProposalRequest(BaseModel):
    position: int = Field(ge=1)


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class PlaylistUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class PlaylistAddTracks(BaseModel):
    track_ids: list[str] = Field(min_length=1)


class PlaylistShareRequest(BaseModel):
    to_user_id: str = Field(min_length=1)


class PlaylistShareTargetOut(BaseModel):
    """A user a playlist can be sent to. Deliberately narrower than `UserOut` — sharing must not
    require `users:manage`, so this exposes only what a recipient picker needs to render."""

    id: str
    display_name: str
    username: str | None = None


class PlaylistShareOut(BaseModel):
    id: str
    from_user_id: str
    from_user_name: str
    to_user_id: str
    name: str
    track_count: int
    # How many of the shared tracks still resolve in the library right now, so the recipient is
    # told up front when a share will arrive short rather than finding out after accepting.
    available_track_count: int = 0
    status: str = "pending"
    created_at: datetime | None = None
    accepted_playlist_id: str | None = None


class SessionRenameRequest(BaseModel):
    device_label: str = Field(min_length=1, max_length=255)


class PlayRecordIn(BaseModel):
    track_id: str


class PlayEventOut(BaseModel):
    track_id: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_id: str | None = None
    played_at: datetime


class PinPlaylistIn(BaseModel):
    playlist_id: str
    name: str | None = None


class PinAlbumIn(BaseModel):
    album_id: str


class PinArtistIn(BaseModel):
    artist_id: str


class PinPodcastIn(BaseModel):
    podcast_id: str


class ImportScanRequest(BaseModel):
    path: str | None = None
    files: list[dict[str, Any]] | None = None
    download_requests: list[dict[str, Any]] | None = None
    playlist_name: str | None = None
    playlist_original_tracks: list[dict[str, Any]] | None = None
    playlist_origin: str | None = None


class ImportMusicBrainzLookupRequest(BaseModel):
    file: dict[str, Any]


class AlbumLookupRequest(BaseModel):
    artist: str
    album: str
    release_id: str | None = None


class PlaylistTrackItem(BaseModel):
    title: str
    artist: str
    album: str | None = None


class PlaylistImportResponse(BaseModel):
    source: str
    name: str | None = None
    tracks: list[PlaylistTrackItem]
    count: int


class PlaylistSyncStatsOut(BaseModel):
    last_run_at: str | None = None
    run_count: int = 0
    started_at: str | None = None


class PlaylistImportRequest(BaseModel):
    url: str


class DiscoverTaskQueueRequest(BaseModel):
    download_requests: list[dict[str, Any]] = Field(min_length=1)


class LibraryMetadataProposalRequest(BaseModel):
    target_type: str = Field(pattern="^(artist|album|track)$")
    target_id: str
    changes: dict[str, Any]


class CoverFromURLRequest(BaseModel):
    """Body for `/library/{albums,artists}/{id}/cover-from-url`.

    The **server** downloads this and stores the bytes locally; `cover_path` is a filesystem path
    and must never be set to a URL by a client."""

    url: str = Field(min_length=1, max_length=2048)


class LibraryRemoveProposalRequest(BaseModel):
    target_type: str = Field(pattern="^(artist|album|track)$")
    target_id: str
    action: str = Field(pattern="^(delete|move_to_import)$")


class CheckFileFixRequest(BaseModel):
    action: str = Field(pattern="^(remove_record|download_record|create_record|delete_file)$")
    path: str | None = None
    track_id: str | None = None


class BackupRestoreRequest(BaseModel):
    backup_path: str


class JellyfinUserOut(BaseModel):
    id: str
    name: str


class AudioVerifyDetected(BaseModel):
    recording_id: str | None = None
    title: str | None = None
    artist: str | None = None
    score: float = 0.0


class AudioVerifyResult(BaseModel):
    matched: bool | None
    confidence: float
    message: str
    claimed: dict[str, Any]
    detected: list[AudioVerifyDetected]
    duration_seconds: int | None = None


class LibraryArtistRow(BaseModel):
    id: str
    name: str
    sort_name: str | None = None
    cover_path: str | None = None
    cover_locked: bool = False
    album_count: int = 0


class LibraryAlbumRow(BaseModel):
    id: str
    title: str
    sort_name: str | None = None
    artist_id: str
    artist_name: str
    cover_path: str | None = None
    cover_locked: bool = False
    track_count: int = 0


class LibraryTrackRow(BaseModel):
    id: str
    title: str
    album_id: str
    album_title: str
    artist_id: str
    artist_name: str
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    format: str | None = None
    is_lossless: bool = False
    replaygain_track_gain: float | None = None


class PaginatedArtists(BaseModel):
    items: list[LibraryArtistRow] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100


class PaginatedAlbums(BaseModel):
    items: list[LibraryAlbumRow] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100


class PaginatedTracks(BaseModel):
    items: list[LibraryTrackRow] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100


class BucketCount(BaseModel):
    bucket: str
    count: int


class UserSearchSettingsUpdate(BaseModel):
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    page_size: int | None = Field(default=None, ge=1, le=5000)


class SessionOut(BaseModel):
    id: str
    device_label: str | None = None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool = False


class PlaybackSnapshotItem(BaseModel):
    type: str  # "track" | "episode"
    id: str
    # Needed even though the server could look it up: the CLIENT uses it to pick the right podcast
    # bucket in its own store, and a PodcastEpisodePlayback cannot be rebuilt without it.
    podcast_id: str | None = None
    # Filled in ONLY by `GET /player/sessions/{id}/queue?resolve=true`, for the web client, which has
    # no local library mirror to turn ids into a readable list. Never sent by a client, and never
    # populated on the transfer path — a queue in flight stays ids only.
    title: str | None = None
    artist: str | None = None
    album_id: str | None = None


class PlaybackSnapshot(BaseModel):
    version: int = 1
    items: list[PlaybackSnapshotItem]
    current_index: int = 0
    position_seconds: float = 0.0
    playing: bool = True
    shuffle: bool = False
    repeat: str = "off"


class PlaybackQueueUpload(BaseModel):
    """A session publishing its own queue so a THIRD device can move it somewhere.

    The client remains the authority on its queue and never plays from this copy; it exists only so
    playback can be moved between two sessions without waking the source.
    """

    hash: str
    snapshot: PlaybackSnapshot


class PlaybackTransferRequest(BaseModel):
    to_session_id: str
    autoplay: bool = True
    # Omit to move the CALLER's own queue. Naming another session moves that session's stored queue
    # instead, which is what lets any device move playback between two others.
    from_session_id: str | None = None
    # Omit when moving another session's queue — the server uses the copy that session published.
    snapshot: PlaybackSnapshot | None = None


class PlaybackEnqueueRequest(BaseModel):
    to_session_id: str
    # "next" inserts after whatever is playing there; "end" appends.
    mode: str = "end"
    snapshot: PlaybackSnapshot


class PlaybackTransferOut(BaseModel):
    id: str
    status: str
    expires_at: datetime
    item_count: int
    to_device_label: str | None = None


class PlaybackHandoffOut(BaseModel):
    """The target gets the snapshot; the source gets status only, never the payload back."""

    id: str
    status: str
    item_count: int
    created_at: datetime
    expires_at: datetime
    from_device_label: str | None = None
    autoplay_effective: bool | None = None
    snapshot: PlaybackSnapshot | None = None


class PlaybackHandoffRejection(BaseModel):
    reason: str = "declined"


class PlayerSessionOut(BaseModel):
    """One of the caller's own sessions, with its playback state as the server can honestly describe it.

    `presence` is derived, never stored — see `_session_presence` in routes.py for the two clocks it
    reads. `status` is projected from it: as reported while `live`, "stopped" while `reachable`, and
    **"unknown"** while `unreachable`. That last value is the point: the row keeps its last-reported
    title, so a client can say "last seen playing X" instead of having to claim either that playback
    is still going or that it stopped, neither of which the server knows.

    ⚠ "unknown" is new vocabulary and exists ONLY here. `/users/playback` still projects "stopped"
    so the existing iOS Users screen and web Users page are untouched.
    """

    session_id: str
    device_label: str | None = None
    client: str | None = None
    current: bool = False
    presence: str
    status: str
    track_id: str | None = None
    episode_id: str | None = None
    podcast_id: str | None = None
    # ⚠ The album's ID, not its title. Cover art is fetched by id; passing the title as one is why
    # remote artwork silently never loaded.
    album_id: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    queue_length: int = 0
    current_index: int = 0
    position_seconds: int | None = None
    duration_seconds: int | None = None
    shuffle: bool = False
    repeat: str = "off"
    reported_at: datetime | None = None
    last_used_at: datetime | None = None


class AdminSessionOut(BaseModel):
    id: str
    user_id: str
    user_name: str
    username: str | None = None
    device_label: str | None = None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    online: bool = False


class StaticKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class StaticKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked: bool = False


class StaticKeyCreated(StaticKeyOut):
    api_key: str


class SearchResultItem(BaseModel):
    kind: str
    id: str
    name: str
    artist_id: str | None = None
    album_id: str | None = None
    confidence: float


class SearchResponse(BaseModel):
    query: str
    min_confidence: float
    results: list[SearchResultItem] = Field(default_factory=list)


class PlayerCommandCreate(BaseModel):
    action: str = "play"
    target_type: str | None = None
    target_id: str | None = None
    target_query: str | None = None
    loop: str = "off"
    shuffle: bool = False
    device_id: str | None = None
    # action="seek" only.
    position_seconds: int | None = None
    #: action="jump"/"remove"/"move" only — an index into the queue the target published.
    #: ⚠ An INDEX, not a track id: a queue may hold the same track twice, and only the position
    #: identifies which occurrence the user meant.
    queue_index: int | None = None
    #: action="move" only — where the item at `queue_index` should end up.
    queue_to_index: int | None = None


class PlayerCommandOut(BaseModel):
    id: str
    action: str
    target_type: str | None = None
    target_id: str | None = None
    target_label: str | None = None
    # ⚠ Null means "this command says nothing about playback mode", and that distinction is the
    # whole point of these being optional HERE while `PlayerCommandCreate` keeps concrete defaults.
    # A caller cannot omit them on the way in — Pydantic fills "off"/False — so a `pause` used to be
    # stored and re-served as a concrete "repeat off, shuffle off", and every client that applied
    # mode on each command it received had its repeat and shuffle silently cleared by a remote pause.
    # `_serialize_command` therefore emits them only for the two actions where they mean something.
    loop: str | None = None
    shuffle: bool | None = None
    status: str
    device_id: str | None = None
    position_seconds: int | None = None
    queue_index: int | None = None
    queue_to_index: int | None = None
    # `_serialize_command` has always passed this and the schema has always dropped it, so no client
    # could age a command. Declared now because the remote needs to know how old an instruction is.
    created_at: datetime | None = None


class AutomationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    trigger_type: str
    trigger_config: dict = Field(default_factory=dict)
    action_type: str
    action_config: dict = Field(default_factory=dict)
    notify_mode: str = "log"
    notify_priority: str = "normal"


class AutomationUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    action_type: str | None = None
    action_config: dict | None = None
    notify_mode: str | None = None
    notify_priority: str | None = None


class AutomationOut(BaseModel):
    id: str
    name: str
    enabled: bool
    trigger_type: str
    trigger_config: dict = Field(default_factory=dict)
    action_type: str
    action_config: dict = Field(default_factory=dict)
    notify_mode: str
    notify_priority: str
    webhook_token: str | None = None
    webhook_url: str | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    next_run_at: datetime | None = None
    created_at: datetime
    created_at: datetime


class PodcastSubscribeIn(BaseModel):
    """Subscribing takes the feed and nothing else.

    The server no longer stores episode audio — clients stream (and optionally download) straight
    from the publisher's enclosure — so there is no download policy, keep count, or retention
    window left to configure here.
    """

    feed_url: str


class PodcastUpdateIn(BaseModel):
    enabled: bool | None = None


class PodcastOut(BaseModel):
    id: str
    title: str
    author: str | None = None
    description: str | None = None
    feed_url: str
    has_cover: bool = False
    enabled: bool
    episode_count: int = 0
    unplayed_count: int = 0
    last_scanned_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    # Per-user (the caller's) opt-in for new-episode notifications; default False = opted out.
    notify_on_new_episodes: bool = False


class PodcastNotificationIn(BaseModel):
    enabled: bool


class PodcastSearchResult(BaseModel):
    id: str = ""
    title: str
    author: str = ""
    feed_url: str
    artwork_url: str | None = None
    store_url: str | None = None
    genre: str = ""
    episode_count: int | None = None


class EpisodeProgressOut(BaseModel):
    position_ms: int = 0
    duration_ms: int | None = None
    played: bool = False
    updated_at: datetime | None = None


class EpisodeOut(BaseModel):
    id: str
    podcast_id: str
    title: str
    description: str | None = None
    published_at: datetime | None = None
    duration_ms: int | None = None
    # Both come from the feed's own <enclosure> declaration (type / length), not from a file this
    # server holds — nothing here is ever downloaded.
    format: str | None = None
    file_size: int | None = None
    season: int | None = None
    episode_number: int | None = None
    # The publisher's media URL: what every client streams and downloads from. Public information
    # already present in the feed.
    enclosure_url: str | None = None
    has_cover: bool = False
    progress: EpisodeProgressOut | None = None


class PaginatedEpisodes(BaseModel):
    items: list[EpisodeOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100


class EpisodeProgressIn(BaseModel):
    position_ms: int | None = None
    duration_ms: int | None = None
    played: bool | None = None


class MarkEpisodesPlayedIn(BaseModel):
    played: bool
    # "all" marks/unmarks every episode; "before_oldest_played" only touches episodes published
    # before the caller's currently-oldest played episode (a backlog catch-up action) and is a
    # no-op when nothing is played yet.
    scope: Literal["all", "before_oldest_played"] = "all"
