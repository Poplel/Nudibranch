import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Permission(str, enum.Enum):
    library_read = "library:read"
    library_write = "library:write"
    import_run = "import:run"
    approvals_manage = "approvals:manage"
    wishlist_manage_own = "wishlist:manage_own"
    wishlist_manage_all = "wishlist:manage_all"
    downloads_manage = "downloads:manage"
    metadata_edit = "metadata:edit"
    playlists_manage = "playlists:manage"
    notifications_read = "notifications:read"
    settings_manage = "settings:manage"
    users_manage = "users:manage"
    backups_manage = "backups:manage"
    jellyfin_manage = "jellyfin:manage"


class ProposalKind(str, enum.Enum):
    import_files = "import_files"
    download = "download"
    metadata = "metadata"
    artwork = "artwork"
    lyrics = "lyrics"
    file_move = "file_move"
    delete = "delete"
    jellyfin_sync = "jellyfin_sync"
    playlist = "playlist"


class ProposalStatus(str, enum.Enum):
    draft = "draft"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    executing = "executing"
    completed = "completed"
    failed = "failed"


class TaskStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class NotificationStatus(str, enum.Enum):
    unread = "unread"
    read = "read"
    dismissed = "dismissed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    permissions: Mapped[list["UserPermission"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    wishlists: Mapped[list["WishlistItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserPermission(Base):
    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    permission: Mapped[Permission] = mapped_column(Enum(Permission), nullable=False)

    user: Mapped[User] = relationship(back_populates="permissions")


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sort_name: Mapped[str | None] = mapped_column(String(255))
    musicbrainz_id: Mapped[str | None] = mapped_column(String(64), index=True)

    albums: Mapped[list["Album"]] = relationship(back_populates="artist")


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    artist_id: Mapped[str] = mapped_column(ForeignKey("artists.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    release_title: Mapped[str | None] = mapped_column(String(255))
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(64), index=True)
    musicbrainz_release_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    path: Mapped[str | None] = mapped_column(Text)
    cover_path: Mapped[str | None] = mapped_column(Text)

    artist: Mapped[Artist] = relationship(back_populates="albums")
    tracks: Mapped[list["Track"]] = relationship(back_populates="album")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    album_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    track_number: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    format: Mapped[str | None] = mapped_column(String(32))
    bitrate: Mapped[int | None] = mapped_column(Integer)
    path: Mapped[str | None] = mapped_column(Text, index=True)
    acoustic_fingerprint: Mapped[str | None] = mapped_column(Text)
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(64), index=True)
    explicit: Mapped[bool | None] = mapped_column(Boolean)
    is_lossless: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    artwork_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    filename_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    album: Mapped[Album] = relationship(back_populates="tracks")


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    artist: Mapped[str] = mapped_column(String(255), nullable=False)
    album: Mapped[str | None] = mapped_column(String(255))
    track: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="wanted", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="wishlists")


class ProposalBatch(Base):
    __tablename__ = "proposal_batches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[ProposalKind] = mapped_column(Enum(ProposalKind), nullable=False)
    status: Mapped[ProposalStatus] = mapped_column(Enum(ProposalStatus), default=ProposalStatus.pending, nullable=False)
    tree_path: Mapped[str] = mapped_column(Text, default="/", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    items: Mapped[list["ProposalItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ProposalItem(Base):
    __tablename__ = "proposal_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    batch_id: Mapped[str] = mapped_column(ForeignKey("proposal_batches.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("proposal_items.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[ProposalKind] = mapped_column(Enum(ProposalKind), nullable=False)
    status: Mapped[ProposalStatus] = mapped_column(Enum(ProposalStatus), default=ProposalStatus.pending, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    suppress_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    batch: Mapped[ProposalBatch] = relationship(back_populates="items")
    parent: Mapped["ProposalItem | None"] = relationship(remote_side=[id])
    children: Mapped[list["ProposalItem"]] = relationship(cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.queued, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    @staticmethod
    def lease_expiry(seconds: int = 300) -> datetime:
        return utcnow() + timedelta(seconds=seconds)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(Enum(NotificationStatus), default=NotificationStatus.unread, nullable=False)
    deliver_web: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deliver_apns: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    apns_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MobileDevice(Base):
    __tablename__ = "mobile_devices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_name: Mapped[str] = mapped_column(String(255), nullable=False)
    apns_token: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
