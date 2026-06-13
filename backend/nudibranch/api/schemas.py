from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from nudibranch.db.models import NotificationStatus, ProposalKind, ProposalStatus, TaskStatus


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=4, max_length=128)
    device_label: str | None = None


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
    search_min_confidence: float = 0.4
    jellyfin_user_id: str | None = None


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


class UserAppearanceUpdate(BaseModel):
    theme: str = Field(pattern="^(light|dark)$")
    accent_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    background_tint: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    crossfade_duration: float = Field(default=0.5, ge=0.0, le=15.0)


class JellyfinUserLinkUpdate(BaseModel):
    jellyfin_user_id: str | None = None


class PlayerStateUpdate(BaseModel):
    track_id: str | None = None
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


class LibraryTreeAlbum(BaseModel):
    id: str
    title: str
    release_title: str | None = None
    path: str | None = None
    cover_path: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_release_group_id: str | None = None
    tracks: list[LibraryTreeTrack] = Field(default_factory=list)


class LibraryTreeArtist(BaseModel):
    id: str
    name: str
    sort_name: str | None = None
    musicbrainz_id: str | None = None
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
    suppress_until: datetime | None = None


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
    suppress_for: str = Field("none", pattern="^(day|week|forever|none)$")


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
    # Direct mode supplies apns_token; App Attest proxy mode supplies proxy_grant
    # (the opaque per-pairing grant token the app obtained from the proxy).
    apns_token: str = ""
    proxy_grant: str | None = None


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
    acoustid_api_key: str = ""


class PlaylistTrackOut(BaseModel):
    id: str
    track_id: str
    position: int
    title: str
    artist: str
    album: str
    format: str | None = None


class FavoritesOut(BaseModel):
    id: str
    name: str
    track_ids: list[str]
    tracks: list[PlaylistTrackOut] = Field(default_factory=list)
    protected: bool = True
    track_count: int = 0


class PlaylistPositionProposalRequest(BaseModel):
    position: int = Field(ge=1)


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class PlaylistUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class PlaylistAddTracks(BaseModel):
    track_ids: list[str] = Field(min_length=1)


class ImportScanRequest(BaseModel):
    path: str | None = None
    files: list[dict[str, Any]] | None = None
    download_requests: list[dict[str, Any]] | None = None
    playlist_name: str | None = None
    playlist_original_tracks: list[dict[str, Any]] | None = None


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
    album_count: int = 0


class LibraryAlbumRow(BaseModel):
    id: str
    title: str
    artist_id: str
    artist_name: str
    cover_path: str | None = None
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
    min_confidence: float = Field(ge=0.0, le=1.0)


class SessionOut(BaseModel):
    id: str
    device_label: str | None = None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool = False


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


class PlayerCommandOut(BaseModel):
    id: str
    action: str
    target_type: str | None = None
    target_id: str | None = None
    target_label: str | None = None
    loop: str = "off"
    shuffle: bool = False
    status: str
    device_id: str | None = None


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
