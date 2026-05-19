from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from nudibranch.db.models import NotificationStatus, ProposalKind, ProposalStatus, TaskStatus


class LoginRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=32)


class LoginResponse(BaseModel):
    user_id: str
    display_name: str
    api_key: str
    is_admin: bool


class UserOut(BaseModel):
    id: str
    display_name: str
    is_admin: bool
    permissions: list[str]


class PermissionOut(BaseModel):
    value: str
    label: str
    section: str


class UserCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    pin: str = Field(min_length=4, max_length=32)
    is_admin: bool = False
    permissions: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    is_admin: bool | None = None
    permissions: list[str] | None = None


class UserPinUpdate(BaseModel):
    pin: str = Field(min_length=4, max_length=32)


class LibraryTreeTrack(BaseModel):
    id: str
    title: str
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    format: str | None = None
    bitrate: int | None = None
    is_lossless: bool = False
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
    apns_token: str


class IntegrationSettings(BaseModel):
    acoustid_api_key: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    slskd_url: str = ""
    slskd_api_key: str = ""
    playlist_conflict_winner: str = "nudibranch"
    youtube_cookies_browser: str = ""
    youtube_cookies_path: str = ""


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


class ImportAcousticLookupRequest(BaseModel):
    file: dict[str, Any]


class AlbumLookupRequest(BaseModel):
    artist: str
    album: str
    release_id: str | None = None


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
