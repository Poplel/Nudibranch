import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# Event types delivered as a silent content-available wake rather than a user-visible alert.
# They must never be mutable per device (see MobileDevice.mutes) — a mute would disable the
# behaviour, not the noise, and there is no noise to disable.
SILENT_WAKE_EVENT_TYPES = frozenset({"remote_playback_command", "podcast_episode_available"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Permission(str, enum.Enum):
    # Flow/menu-level permissions: one per menu, each carries the whole flow.
    library_view = "library:view"            # Library: browse + play
    library_edit = "library:edit"            # Library: edit metadata, covers, remove, MB match, verify, replace, reindex
    discover = "discover"                     # Discover + Wishlist menus: search, request, queue downloads
    wishlist_approve_all = "wishlist:approve_all"  # Approve other users' wishlist requests
    import_run = "import:run"                # Import/Add menu
    approvals_manage = "approvals:manage"    # Task Queue menu: see + approve/reject everything
    playlists_manage = "playlists:manage"    # Playlists menu
    activity_read = "activity:read"          # Activity menu + tasks/logs + activity notifications
    tools_manage = "tools:manage"            # Tools menu: maintenance, backups/restore, Jellyfin, clear-downloads
    automations_manage = "automations:manage"  # Automations menu
    users_manage = "users:manage"            # Users menu
    settings_manage = "settings:manage"      # Settings menu
    podcasts_manage = "podcasts:manage"      # Podcasts menu: subscribe, scan, download, play episodes


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
    username: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    theme: Mapped[str] = mapped_column(String(16), default="light", nullable=False)
    accent_color: Mapped[str] = mapped_column(String(16), default="#356df3", nullable=False)
    background_tint: Mapped[str] = mapped_column(String(16), default="#356df3", nullable=False)
    crossfade_duration: Mapped[float] = mapped_column(default=0.5, nullable=False)
    #: Whether this account takes part in cross-device playback at all (§31). Off means local-only:
    #: the user's sessions stop seeing and driving each other, and stop being offered as targets.
    #: ⚠ Deliberately NOT a kill switch for the command channel — automations and IFTTT drive a
    #: device through the same route (§3/§24), and silently stopping those would be a second,
    #: unasked-for change.
    remote_playback_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    search_min_confidence: Mapped[float] = mapped_column(default=0.4, nullable=False)
    library_page_size: Mapped[int] = mapped_column(default=100, nullable=False)
    jellyfin_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Opaque per-user home-screen arrangement: a JSON object of ordered id lists, stored as text and
    # never interpreted here (see HomeLayoutUpdate for why the server stays out of its semantics).
    home_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Web-only mirror of home_layout, independently editable so the iOS app (which owns home_layout)
    # and the web UI don't clobber each other's arrangement. Same opaque/never-interpreted contract.
    home_layout_web: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    permissions: Mapped[list["UserPermission"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    wishlists: Mapped[list["WishlistItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    auth_sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    session_player_states: Mapped[list["SessionPlayerState"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    api_keys: Mapped[list["StaticApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserPermission(Base):
    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    permission: Mapped[Permission] = mapped_column(Enum(Permission), nullable=False)

    user: Mapped[User] = relationship(back_populates="permissions")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    device_label: Mapped[str | None] = mapped_column(String(255))
    # "ios" | "mac" | "web". Recorded at LOGIN rather than only when playback reports, because a
    # device that has never played anything still has to appear correctly in a device picker — and
    # under Mac Catalyst UIKit reports the iPad idiom, so the client kind cannot be inferred later.
    client: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="auth_sessions")


class StaticApiKey(Base):
    __tablename__ = "static_api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped["User"] = relationship(back_populates="api_keys")


class SessionPlayerState(Base):
    """What one signed-in device session is playing.

    Keyed by session, not by user: the same account signed in on a phone and a laptop is two
    independent players, and a single per-user row meant whichever one reported last erased the
    other's now-playing. The primary key is the auth session, so revoking or expiring a session
    drops its state with it and there is no separate cleanup path to forget to run.

    ``reported_at`` means "this session last described its player"; ``AuthSession.last_used_at``
    means "this session is reachable". They are not interchangeable and must never be collapsed
    into one field, because they fail in opposite directions: a paused web tab keeps polling, so
    it is reachable but silent, while an iOS app playing downloaded files in a tunnel is playing
    but unreachable. Reading either one alone gets one of those two cases wrong.
    """

    __tablename__ = "session_player_states"

    session_id: Mapped[str] = mapped_column(ForeignKey("auth_sessions.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    # When a podcast episode is playing, track_id is NULL and episode_id points at the episode
    # (so cross-device now-playing resolves the episode's title/cover). Only one is set at a time.
    episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(String(255))
    artist: Mapped[str | None] = mapped_column(String(255))
    album: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="stopped", nullable=False)
    queue_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    position_seconds: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    shuffle: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    repeat: Mapped[str] = mapped_column(String(8), default="off", nullable=False)
    # Indexed because reads pick a user's newest report and sort on this column.
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    #: When this session last STARTED playing (transitioned into it), not when it last reported.
    #: ⚠ This is what breaks the tie when two sessions both believe they are playing. It cannot be
    #: "most recent report", because a device that has gone offline stops reporting while genuinely
    #: still playing — and it is exactly that device which must keep the session if nothing else has
    #: started since. Whoever started last owns playback.
    playback_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The session's queue, so playback can be moved from ANY online session to any other without the
    # source having to be woken to hand it over.
    #
    # ⚠ The client stays the authority on its own queue — it never reads this back to play from. The
    # copy exists only so a THIRD device can move that queue somewhere. `queue_hash` is what keeps
    # that cheap: status reports carry the client's hash, and the server only asks for a fresh upload
    # when the two disagree, so an unchanged queue is never re-sent however long it plays.
    queue_json: Mapped[str | None] = mapped_column(Text)
    queue_hash: Mapped[str | None] = mapped_column(String(64))
    queue_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "ios" | "mac" | "web" — which client shape reported, for labelling a session in a device list.
    client: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="session_player_states")
    track: Mapped["Track | None"] = relationship()
    episode: Mapped["Episode | None"] = relationship()


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sort_name: Mapped[str | None] = mapped_column(String(255))
    musicbrainz_id: Mapped[str | None] = mapped_column(String(64), index=True)
    cover_path: Mapped[str | None] = mapped_column(Text)
    cover_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True)

    albums: Mapped[list["Album"]] = relationship(back_populates="artist")


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    artist_id: Mapped[str] = mapped_column(ForeignKey("artists.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    release_title: Mapped[str | None] = mapped_column(String(255))
    sort_name: Mapped[str | None] = mapped_column(String(255))
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(64), index=True)
    musicbrainz_release_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    path: Mapped[str | None] = mapped_column(Text)
    cover_path: Mapped[str | None] = mapped_column(Text)
    cover_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True)

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
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(64), index=True)
    jellyfin_item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # ReplayGain track gain in dB (ReplayGain 2.0, -18 LUFS reference). NULL = not measured.
    # Non-destructive volume normalization: the player applies it at playback; the tool also
    # writes it to the file's REPLAYGAIN_TRACK_GAIN tag.
    replaygain_track_gain: Mapped[float | None] = mapped_column(Float)
    explicit: Mapped[bool | None] = mapped_column(Boolean)
    is_lossless: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    musicbrainz_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    artwork_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    filename_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True)

    album: Mapped[Album] = relationship(back_populates="tracks")


class LibraryDeletion(Base):
    """Tombstone for a removed library row, so `/library/changes` can report deletions.

    That endpoint is an ``updated_at`` high-water-mark feed: a deleted row simply stops appearing,
    which is indistinguishable from "unchanged".  Offline-first clients mirror the library locally,
    so without a tombstone they keep a phantom artist/album/track until their next full resync —
    visible in browse and search, and failing when tapped.

    Rows are written by the ``before_delete`` mapper events below rather than by each call site.
    Deletions happen in several worker paths (duplicate merges, folder consolidation, remaps,
    replacement swaps, approved removals) and a per-call-site approach would silently miss any new
    one.  Pruned by the worker past ``LIBRARY_DELETION_RETENTION``; a client whose cursor predates
    that window is told to full-resync instead.
    """

    __tablename__ = "library_deletions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    # "artist" | "album" | "track"
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# How long tombstones are kept.  A client offline longer than this cannot be brought up to date
# from the delta feed alone, so `/library/changes` tells it to full-resync (which prunes by
# re-seeding under a new sync generation).  Keep comfortably longer than the client's own periodic
# full-resync interval — the iOS app resyncs every 7 days.
LIBRARY_DELETION_RETENTION = timedelta(days=30)


def _record_library_deletion(connection, entity_type: str, entity_id: str) -> None:
    # Written on the same connection/transaction as the delete, so a rollback discards the
    # tombstone too and we can never advertise a deletion that did not happen.
    connection.execute(
        LibraryDeletion.__table__.insert().values(
            id=uuid_str(),
            entity_type=entity_type,
            entity_id=entity_id,
            deleted_at=utcnow(),
        )
    )


def _drop_pins(connection, kind: str, item_id: str) -> None:
    """Pins are keyed by a bare id string with no foreign key, because a "podcast" pin and an
    "album" pin live in one table across two id spaces.  Nothing therefore cascades, so a pinned
    album that gets deleted leaves its pin row behind forever: `/me/home` silently drops it while
    resolving (the id no longer resolves to a row), so it is invisible *and* permanent — the user
    cannot even unpin it, because it never appears to be pinned.  Cleared here, alongside the
    tombstone, for the same reason the tombstone lives here: deletions happen across several worker
    paths and a per-call-site fix would miss the next one."""
    connection.execute(
        PinnedItem.__table__.delete().where(
            PinnedItem.__table__.c.kind == kind,
            PinnedItem.__table__.c.item_id == item_id,
        )
    )


@event.listens_for(Artist, "before_delete")
def _tombstone_artist(mapper, connection, target) -> None:
    _record_library_deletion(connection, "artist", target.id)
    _drop_pins(connection, "artist", target.id)


@event.listens_for(Album, "before_delete")
def _tombstone_album(mapper, connection, target) -> None:
    _record_library_deletion(connection, "album", target.id)
    _drop_pins(connection, "album", target.id)


@event.listens_for(Track, "before_delete")
def _tombstone_track(mapper, connection, target) -> None:
    _record_library_deletion(connection, "track", target.id)


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
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="wishlists")


class Playlist(Base):
    __tablename__ = "playlists"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    jellyfin_playlist_id: Mapped[str | None] = mapped_column(String(128))
    # Last state both sides agreed on, as JSON {"items": [jellyfin item ids], "name": str}.
    # This is the BASE of a three-way merge, and it is what lets the mirror be symmetric: without
    # it "present in Jellyfin, absent here" and "absent in Jellyfin, present here" are
    # indistinguishable from each other's opposite, so one side has to be declared the winner and
    # the other side's edits get silently reverted. With it, an item missing from the side that
    # had it last is a deletion, and an item new to either side is an addition.
    jellyfin_mirror_state: Mapped[str | None] = mapped_column(Text)
    # Source the playlist was imported from (e.g. a Spotify/Apple Music URL) so re-importing
    # the same playlist updates this record instead of creating a duplicate.
    origin: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped["User | None"] = relationship()

    tracks: Mapped[list["PlaylistTrack"]] = relationship(back_populates="playlist", cascade="all, delete-orphan")


class PlaylistCover(Base):
    """Custom playlist cover art, keyed by the opaque playlist id string rather than a foreign
    key to `Playlist.id` — a Jellyfin-backed playlist has NO native `Playlist` row at all (its id
    IS the Jellyfin item id), so cover storage has to work independently of which backend a
    playlist's tracks live in."""

    __tablename__ = "playlist_covers"

    playlist_id: Mapped[str] = mapped_column(String, primary_key=True)
    cover_path: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PlaylistShare(Base):
    """One user offering a copy of a playlist to another user.

    A share is a **snapshot, not a link**: `track_ids` is captured at send time and the recipient
    gets an independent copy on accept, so neither side's later edits touch the other. That is why
    nothing here is a foreign key to `Playlist` — the sender may delete or rewrite the original
    while the offer is still pending, and the offer must survive it intact.

    Pending offers are per (recipient, playlist, sender): re-sharing the same playlist to the same
    person refreshes the existing row rather than stacking duplicate notifications.
    """

    __tablename__ = "playlist_shares"
    __table_args__ = (UniqueConstraint("to_user_id", "from_user_id", "source_playlist_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    to_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Opaque: a native Playlist.id, a Jellyfin item id, or "favorites". Never resolved after send.
    source_playlist_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # JSON array of Nudibranch track ids, in playlist order.
    track_ids: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # pending | accepted | declined
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    # The recipient's own playlist created on accept — lets the notification deep-link to it.
    accepted_playlist_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    from_user: Mapped["User"] = relationship(foreign_keys=[from_user_id])
    to_user: Mapped["User"] = relationship(foreign_keys=[to_user_id])


class PlayEvent(Base):
    __tablename__ = "play_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True)
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="nudibranch", nullable=False)
    reported_to_jellyfin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    track: Mapped["Track"] = relationship()


class PinnedPlaylist(Base):
    __tablename__ = "pinned_playlists"
    __table_args__ = (UniqueConstraint("user_id", "playlist_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    playlist_id: Mapped[str] = mapped_column(String(128), nullable=False)  # Jellyfin item id, or "favorites"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PinnedItem(Base):
    """Home-pinned albums, artists, and podcasts (playlists use PinnedPlaylist)."""

    __tablename__ = "pinned_items"
    __table_args__ = (UniqueConstraint("user_id", "kind", "item_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # album | artist | podcast
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"
    __table_args__ = (UniqueConstraint("playlist_id", "track_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    playlist_id: Mapped[str] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    playlist: Mapped[Playlist] = relationship(back_populates="tracks")
    track: Mapped[Track] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


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
    # Vestigial: rejection-suppression was removed from every client and from the API.  Nothing
    # has written this since, so it is always NULL.  Kept only because dropping a column needs a
    # migration that buys nothing; do not reintroduce reads of it.
    suppress_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    batch: Mapped[ProposalBatch] = relationship(back_populates="items")
    parent: Mapped["ProposalItem | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["ProposalItem"]] = relationship(back_populates="parent", cascade="all, delete-orphan")


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
    # Stable workflow identity used to update one tray row as a batch progresses instead of
    # emitting a separate notification for every internal stage.
    group_key: Mapped[str | None] = mapped_column(String(255), index=True)
    # When set, APNS delivery targets only the MobileDevice with this id (a device-scoped nudge,
    # e.g. a remote playback command aimed at one device); NULL = deliver to all the user's devices.
    device_id: Mapped[str | None] = mapped_column(String(64))
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
    # Vestigial: direct APNS delivery was removed, so this is always "". The server never sees a
    # raw device token — the proxy holds it, and this row's credential is proxy_grant. Kept only
    # because dropping a NOT NULL column in SQLite means rebuilding the table for no benefit.
    # Do not reintroduce writes or reads of it.
    apns_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    proxy_grant: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Comma-separated event_type values this device does not want delivered. Empty = everything.
    # Suppression has to happen on the sending side: iOS cannot decline a push that arrives while
    # the app is backgrounded, so a per-category preference is only real if the sender honours it.
    muted_event_types: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    def mutes(self, event_type: str | None) -> bool:
        # Silent background wakes are not alerts — muting one would break the behaviour it drives
        # rather than quiet anything the user can see. remote_playback_command drives scheduled and
        # webhook playback; podcast_episode_available drives automatic episode downloading.
        if not event_type or event_type in SILENT_WAKE_EVENT_TYPES:
            return False
        if not self.muted_event_types:
            return False
        muted = {value.strip() for value in self.muted_event_types.split(",") if value.strip()}
        return event_type in muted


class PlaybackCommand(Base):
    __tablename__ = "playback_commands"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    device_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="play")
    target_type: Mapped[str | None] = mapped_column(String(16))
    target_id: Mapped[str | None] = mapped_column(String(64))
    target_label: Mapped[str | None] = mapped_column(String(255))
    #: Positions in the target's published queue, for the jump/remove/move actions.
    queue_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_to_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loop: Mapped[str] = mapped_column(String(8), default="off", nullable=False)
    shuffle: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Only meaningful for action="seek"; the remote scrubber's landing position.
    position_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlaybackHandoff(Base):
    """One queue in flight between two of a user's sessions.

    The snapshot lives HERE rather than as a column on `playback_commands` because that table is
    `SELECT`ed every ~4 seconds by every open client of every user, forever. A 10-60 KB JSON blob in
    it would make SQLite pull overflow pages on each of those polls, for rows that are almost all
    plain transport actions. Keep the hot polling table row-narrow.

    ⚠ The payload is deliberately NOT deleted when the command is acked. The iOS listener acks
    immediately after executing, so a transient failure between fetching the payload and adopting it
    would otherwise lose the queue with no trace of what happened. It is cleared on an explicit
    `/adopted` or `/rejected` instead, and by expiry.
    """

    __tablename__ = "playback_handoffs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    from_session_id: Mapped[str | None] = mapped_column(String(64))
    to_session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    command_id: Mapped[str | None] = mapped_column(String(64))
    payload_json: Mapped[str | None] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    autoplay: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # pending | adopted | expired | rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(64))


class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger_config: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False)
    action_config: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    notify_mode: Mapped[str] = mapped_column(String(16), default="log", nullable=False)
    notify_priority: Mapped[str] = mapped_column(String(8), default="normal", nullable=False)
    webhook_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Podcast(Base):
    """A podcast subscription (RSS feed).

    ⚠️ **This server never stores episode audio.** Podcasts follow the traditional podcast-app
    model: the server owns the *subscription* — scanning the feed, episode metadata, per-user
    progress, new-episode notifications, cover art, pins — and every client streams (or downloads)
    the audio straight from the publisher's enclosure. There is therefore no download policy, keep
    count, or retention window on this row; the only per-podcast media setting left lives on each
    device. Don't reintroduce a server-side copy: it doubled every transfer, broke outright for the
    publishers that refuse a datacenter IP, and was the only reason an episode could be visible but
    unplayable.
    """

    __tablename__ = "podcasts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    author: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    image_url: Mapped[str | None] = mapped_column(Text)          # remote cover URL from the feed
    cover_path: Mapped[str | None] = mapped_column(Text)          # downloaded local cover file
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True)

    episodes: Mapped[list["Episode"]] = relationship(back_populates="podcast")


class Episode(Base):
    """One podcast episode: feed metadata only. The audio lives at `enclosure_url` and is never
    copied onto this server (see `Podcast`)."""

    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("podcast_id", "guid"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    podcast_id: Mapped[str] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), nullable=False, index=True)
    guid: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # RSS <guid>, else enclosure URL
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    enclosure_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Declared by the feed's <enclosure>, not measured from a local file: `format` is the media
    # subtype (mp3/m4a/…) clients derive a filename extension from, `file_size` its stated length.
    format: Mapped[str | None] = mapped_column(String(32))
    file_size: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(Text)         # episode-specific art (falls back to podcast)
    season: Mapped[int | None] = mapped_column(Integer)
    episode_number: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True)

    podcast: Mapped[Podcast] = relationship(back_populates="episodes")


class EpisodeProgress(Base):
    """Per-user resume position + played state for a podcast episode."""

    __tablename__ = "episode_progress"
    __table_args__ = (UniqueConstraint("user_id", "episode_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True)
    position_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    played: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PodcastNotificationPref(Base):
    """Per-user opt-in for new-episode notifications on a specific podcast. Absent row = opted out
    (the global default): a new-episode notification is only created for users with enabled=True."""

    __tablename__ = "podcast_notification_prefs"
    __table_args__ = (UniqueConstraint("user_id", "podcast_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    podcast_id: Mapped[str] = mapped_column(ForeignKey("podcasts.id", ondelete="CASCADE"), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
