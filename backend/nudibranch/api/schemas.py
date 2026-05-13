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


class LibraryTreeTrack(BaseModel):
    id: str
    title: str
    track_number: int | None = None
    format: str | None = None
    is_lossless: bool = False
    path: str | None = None


class LibraryTreeAlbum(BaseModel):
    id: str
    title: str
    path: str | None = None
    cover_path: str | None = None
    tracks: list[LibraryTreeTrack] = Field(default_factory=list)


class LibraryTreeArtist(BaseModel):
    id: str
    name: str
    albums: list[LibraryTreeAlbum] = Field(default_factory=list)


class WishlistCreate(BaseModel):
    kind: str = Field(pattern="^(artist|album|track)$")
    artist: str
    album: str | None = None
    track: str | None = None


class WishlistOut(WishlistCreate):
    id: str
    user_id: str
    status: str
    created_at: datetime


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
    suppress_for: str = Field("week", pattern="^(day|week|forever|none)$")


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


class ImportScanRequest(BaseModel):
    path: str | None = None
    files: list[dict[str, Any]] | None = None


class ImportAcousticLookupRequest(BaseModel):
    file: dict[str, Any]


class AlbumLookupRequest(BaseModel):
    artist: str
    album: str
    release_id: str | None = None
