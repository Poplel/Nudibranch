import fcntl
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import Base, Permission, Task, User, UserPermission
from nudibranch.services.auth import hash_password, is_bcrypt_hash, slugify_username, wrap_legacy_hash
from nudibranch.db.session import engine
from nudibranch.services.app_log import write_app_log


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# Startup schema work is serialized across processes, and retried if it still loses a race.
_INIT_LOCK_WAIT_SECONDS = 600.0
_INIT_ATTEMPTS = 5


@contextmanager
def _startup_schema_lock():
    """Hold an advisory file lock for the duration of startup schema work.

    ⚠️ The api and worker containers start simultaneously and BOTH run `init_db` against the one
    SQLite file. SQLite allows a single writer, so they were racing: whichever got there second
    blocked on the first's write transaction, waited out `busy_timeout`, and raised
    "database is locked" — which, in the API's FastAPI startup hook, meant *"Application startup
    failed. Exiting."* and a crash-looping container. It only ever showed up on a simultaneous cold
    start, because that is the only time both processes do schema work at once.

    A longer `busy_timeout` does not fix this and was already set to 30s: the first process's work
    can legitimately exceed any timeout (the FTS backfill in `ensure_populated` rewrites the whole
    trigram index in one transaction on a cold index). The fix is to stop them overlapping at all —
    the loser now waits for the winner and then finds every step already done, since each migration
    step is individually guarded.

    The lock file lives beside the database so both containers see the same one through the shared
    volume; `config_path` is not used because it can be configured away from the DB.
    """
    lock_path = get_settings().db_path.parent / ".nudibranch-schema.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    deadline = time.monotonic() + _INIT_LOCK_WAIT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    # Fall through unlocked rather than refusing to boot. The steps are idempotent
                    # and guarded, and the retry below still covers a genuine collision — a server
                    # that will not start at all is strictly worse than one that races.
                    write_app_log("Schema lock wait timed out; continuing without it", "warning")
                    yield
                    return
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def init_db(session: Session) -> None:
    """Create/migrate the schema and seed the first admin.

    Safe to call from every process at once — see `_startup_schema_lock`. A lock failure is not
    fatal on its own either: the whole thing retries on SQLite's "database is locked" rather than
    taking the process down with it.
    """
    for attempt in range(_INIT_ATTEMPTS):
        try:
            with _startup_schema_lock():
                _init_schema(session)
            return
        except OperationalError as error:
            message = str(error).lower()
            if "locked" not in message and "busy" not in message:
                raise
            # Leave no half-open transaction behind for the next attempt.
            session.rollback()
            if attempt == _INIT_ATTEMPTS - 1:
                raise
            delay = min(2 ** attempt, 15)
            write_app_log(
                f"Database busy during startup schema work; retrying in {delay}s "
                f"(attempt {attempt + 1}/{_INIT_ATTEMPTS})",
                "warning",
            )
            time.sleep(delay)


def _init_schema(session: Session) -> None:
    Base.metadata.create_all(bind=engine)
    ensure_lightweight_migrations(session)
    try:
        from nudibranch.services.search import ensure_populated

        ensure_populated(session)
    except Exception as exc:  # search index is non-critical; never block startup
        session.rollback()
        try:
            write_app_log(f"Search index init failed: {exc}", "warning")
        except Exception:
            pass
    existing_admin = session.scalar(select(User).where(User.is_admin.is_(True)))
    if existing_admin:
        return

    settings = get_settings()
    admin = User(
        display_name="Admin",
        username="admin",
        pin_hash=hash_password(settings.first_admin_pin),
        api_key_hash=hash_secret(settings.full_access_api_key),
        is_admin=True,
    )
    session.add(admin)
    session.flush()

    for permission in Permission:
        session.add(UserPermission(user_id=admin.id, permission=permission))

    session.commit()


def ensure_lightweight_migrations(session: Session) -> None:
    wishlist_columns = {row[1] for row in session.execute(text("PRAGMA table_info(wishlist_items)"))}
    if "status_changed_at" not in wishlist_columns:
        session.execute(text("ALTER TABLE wishlist_items ADD COLUMN status_changed_at DATETIME"))
        session.execute(text("UPDATE wishlist_items SET status_changed_at = created_at WHERE status_changed_at IS NULL"))
        session.commit()
    artist_columns = {row[1] for row in session.execute(text("PRAGMA table_info(artists)"))}
    if "cover_path" not in artist_columns:
        session.execute(text("ALTER TABLE artists ADD COLUMN cover_path TEXT"))
        session.commit()
    if "cover_locked" not in artist_columns:
        session.execute(text("ALTER TABLE artists ADD COLUMN cover_locked BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    album_columns = {row[1] for row in session.execute(text("PRAGMA table_info(albums)"))}
    if "sort_name" not in album_columns:
        session.execute(text("ALTER TABLE albums ADD COLUMN sort_name VARCHAR(255)"))
        session.commit()
    if "cover_locked" not in album_columns:
        session.execute(text("ALTER TABLE albums ADD COLUMN cover_locked BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    track_columns = {row[1] for row in session.execute(text("PRAGMA table_info(tracks)"))}
    if "musicbrainz_verified" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN musicbrainz_verified BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    if "jellyfin_item_id" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN jellyfin_item_id VARCHAR(128) NULL"))
        session.execute(text("CREATE INDEX IF NOT EXISTS ix_tracks_jellyfin_item_id ON tracks(jellyfin_item_id)"))
        session.commit()
    if "replaygain_track_gain" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN replaygain_track_gain FLOAT NULL"))
        session.commit()
    # metadata_locked/artwork_locked/filename_locked existed on the Track model without ever
    # being added here — any query touching them (e.g. GET /library/tree) would raise
    # "no such column" on a database that predates their introduction into models.py.
    if "metadata_locked" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN metadata_locked BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    if "artwork_locked" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN artwork_locked BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    if "filename_locked" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN filename_locked BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    playlist_columns = {row[1] for row in session.execute(text("PRAGMA table_info(playlists)"))}
    if "origin" not in playlist_columns:
        session.execute(text("ALTER TABLE playlists ADD COLUMN origin TEXT NULL"))
        session.commit()
    if "jellyfin_mirror_state" not in playlist_columns:
        # NULL = never mirrored. The first reconcile treats that as "no base", which merges both
        # sides instead of letting either clobber the other — the right behaviour for an existing
        # install whose playlists already exist on both sides.
        session.execute(text("ALTER TABLE playlists ADD COLUMN jellyfin_mirror_state TEXT NULL"))
        session.commit()
    user_columns = {row[1] for row in session.execute(text("PRAGMA table_info(users)"))}
    if "theme" not in user_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN theme VARCHAR(16) NOT NULL DEFAULT 'light'"))
        session.commit()
    if "accent_color" not in user_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN accent_color VARCHAR(16) NOT NULL DEFAULT '#356df3'"))
        session.commit()
    if "background_tint" not in user_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN background_tint VARCHAR(16) NOT NULL DEFAULT '#356df3'"))
        session.commit()
    if "jellyfin_user_id" not in user_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN jellyfin_user_id VARCHAR(255)"))
        session.commit()
    if "crossfade_duration" not in user_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN crossfade_duration FLOAT NOT NULL DEFAULT 1.0"))
    if "remote_playback_enabled" not in user_columns:
        # Default 1: cross-device playback is on unless a user turns it off, so an existing account
        # keeps working exactly as it did before the toggle existed.
        session.execute(
            text("ALTER TABLE users ADD COLUMN remote_playback_enabled BOOLEAN NOT NULL DEFAULT 1")
        )
        session.commit()
    user_cols2 = {row[1] for row in session.execute(text("PRAGMA table_info(users)"))}
    if "search_min_confidence" not in user_cols2:
        session.execute(text("ALTER TABLE users ADD COLUMN search_min_confidence FLOAT NOT NULL DEFAULT 0.4"))
        session.commit()
    if "library_page_size" not in user_cols2:
        session.execute(text("ALTER TABLE users ADD COLUMN library_page_size INTEGER NOT NULL DEFAULT 100"))
        session.commit()
    user_cols = {row[1] for row in session.execute(text("PRAGMA table_info(users)"))}
    if "username" not in user_cols:
        session.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(120)"))
        session.commit()
    if "home_layout" not in user_cols:
        # NULL means "no custom arrangement" — clients fall back to the server's own ordering.
        session.execute(text("ALTER TABLE users ADD COLUMN home_layout TEXT"))
        session.commit()
    if "home_layout_web" not in user_cols:
        # Web-only mirror of home_layout — independent so web and iOS don't clobber each other's
        # arrangement. Same semantics: NULL means "no custom arrangement".
        session.execute(text("ALTER TABLE users ADD COLUMN home_layout_web TEXT"))
        session.commit()
    device_cols = {row[1] for row in session.execute(text("PRAGMA table_info(mobile_devices)"))}
    if device_cols and "muted_event_types" not in device_cols:
        # Per-device push category opt-outs. Empty means "deliver everything", which is the
        # pre-upgrade behaviour for every already-registered device.
        session.execute(text("ALTER TABLE mobile_devices ADD COLUMN muted_event_types TEXT NOT NULL DEFAULT ''"))
        session.commit()
    _migrate_player_states_to_sessions(session)
    auth_cols = {row[1] for row in session.execute(text("PRAGMA table_info(auth_sessions)"))}
    if auth_cols and "client" not in auth_cols:
        # Set at login so a device that has never played still shows correctly in a device picker.
        session.execute(text("ALTER TABLE auth_sessions ADD COLUMN client VARCHAR(16) NULL"))
        session.commit()
    sps_cols = {row[1] for row in session.execute(text("PRAGMA table_info(session_player_states)"))}
    if sps_cols and "queue_json" not in sps_cols:
        # The session's queue, so playback can be moved between any two online sessions without
        # waking the source. See SessionPlayerState for why the hash matters.
        session.execute(text("ALTER TABLE session_player_states ADD COLUMN queue_json TEXT NULL"))
        session.execute(text("ALTER TABLE session_player_states ADD COLUMN queue_hash VARCHAR(64) NULL"))
        session.execute(text("ALTER TABLE session_player_states ADD COLUMN queue_updated_at DATETIME NULL"))
        session.commit()
    if sps_cols and "playback_started_at" not in sps_cols:
        # When this session last STARTED playing, which is what decides who wins when two sessions
        # both believe they are playing — an offline device cannot be told to stop, so the tie is
        # broken by who started most recently rather than by who reported most recently.
        session.execute(text("ALTER TABLE session_player_states ADD COLUMN playback_started_at DATETIME NULL"))
        session.commit()
    cmd_cols = {row[1] for row in session.execute(text("PRAGMA table_info(playback_commands)"))}
    if cmd_cols:
        if "position_seconds" not in cmd_cols:
            # Carries the landing position for action="seek", the remote scrubber's verb.
            session.execute(text("ALTER TABLE playback_commands ADD COLUMN position_seconds INTEGER NULL"))
        # Positions in the target's published queue, for jump/remove/move.
        # ⚠ Guarded by `cmd_cols` being non-empty: on a fresh database `create_all` has already made
        # the table WITH these columns, and ALTERing them in would fail.
        if "queue_index" not in cmd_cols:
            session.execute(text("ALTER TABLE playback_commands ADD COLUMN queue_index INTEGER NULL"))
        if "queue_to_index" not in cmd_cols:
            session.execute(text("ALTER TABLE playback_commands ADD COLUMN queue_to_index INTEGER NULL"))
        session.commit()
    _drop_server_side_podcast_downloads(session)
    notif_cols = {row[1] for row in session.execute(text("PRAGMA table_info(notifications)"))}
    if notif_cols and "device_id" not in notif_cols:
        # Device-scoped APNS delivery (NULL = all the user's devices).
        session.execute(text("ALTER TABLE notifications ADD COLUMN device_id VARCHAR(64) NULL"))
        session.commit()
    if notif_cols and "group_key" not in notif_cols:
        # One user-visible notification can follow a long-running workflow through queued,
        # downloading, review-ready, and completed states.
        session.execute(text("ALTER TABLE notifications ADD COLUMN group_key VARCHAR(255) NULL"))
        session.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_group_key ON notifications(group_key)"))
        session.commit()
    if notif_cols:
        # Retire legacy per-task progress noise without hiding the distinct restart-recovery notice.
        session.execute(
            text(
                "UPDATE notifications SET status = 'dismissed' "
                "WHERE event_type = 'task_started' AND body = 'Task is running.'"
            )
        )
        session.commit()
    device_cols = {row[1] for row in session.execute(text("PRAGMA table_info(mobile_devices)"))}
    if device_cols and "proxy_grant" not in device_cols:
        # Per-pairing APNS proxy grant token (App Attest model); NULL = direct/legacy device.
        session.execute(text("ALTER TABLE mobile_devices ADD COLUMN proxy_grant TEXT"))
        session.commit()
    _backfill_usernames(session)
    _migrate_password_hashes(session)
    _migrate_playlists_per_user(session)
    _migrate_library_timestamps(session)
    _migrate_permissions(session)
    _scrub_invalid_mbids(session)
    move_task_result_logs_to_app_log(session)


# Columns carried over from the old per-user player_states table, paired with what to substitute
# when the source column is absent. shuffle/repeat/episode_id were themselves late additions, so a
# database that has not been opened in a long while genuinely lacks them.
_PSTATE_CARRIED_COLUMNS = (
    ("track_id", "NULL"),
    ("episode_id", "NULL"),
    ("title", "NULL"),
    ("artist", "NULL"),
    ("album", "NULL"),
    ("status", "'stopped'"),
    ("queue_length", "0"),
    ("current_index", "0"),
    ("position_seconds", "NULL"),
    ("duration_seconds", "NULL"),
    ("shuffle", "0"),
    ("repeat", "'off'"),
)


def _migrate_player_states_to_sessions(session: Session) -> None:
    """Fold the per-user player_states table into the per-session one and drop it.

    Two signed-in sessions of one account shared a single row, so each overwrote the other's
    now-playing. create_all has already made session_player_states by the time this runs.

    What is dropped here is a short-lived presence cache — it is rewritten within seconds by any
    client that is actually playing — so the backfill onto each user's most-recently-used session
    exists only to avoid a visible blank in the users list right after an upgrade. There is nothing
    else in that table worth preserving.
    """
    pstate_cols = {row[1] for row in session.execute(text("PRAGMA table_info(player_states)"))}
    if not pstate_cols:
        return
    carried = [name for name, _ in _PSTATE_CARRIED_COLUMNS]
    sources = [
        f"p.\"{name}\"" if name in pstate_cols else default
        for name, default in _PSTATE_CARRIED_COLUMNS
    ]
    target_list = ", ".join(f'"{name}"' for name in carried)
    source_list = ", ".join(sources)
    session.execute(
        text(
            f"INSERT OR IGNORE INTO session_player_states "
            f"(session_id, user_id, {target_list}, reported_at, updated_at) "
            f"SELECT s.id, p.user_id, {source_list}, p.updated_at, p.updated_at "
            "FROM player_states p "
            "JOIN auth_sessions s ON s.id = ("
            "    SELECT id FROM auth_sessions WHERE user_id = p.user_id "
            "    ORDER BY last_used_at DESC LIMIT 1)"
        )
    )
    session.execute(text("DROP TABLE player_states"))
    session.commit()


# A valid MusicBrainz id is a 36-char UUID (hyphens at 9/14/19/24). Files tagged by other tools
# (iTunes/Apple) stored a NUMERIC id in the MB tags, which imported into these columns and made
# MusicBrainz reject lookups with 400 "Invalid mbid.". Null out anything not UUID-shaped. Idempotent:
# structurally-valid ids pass the pattern, so a second pass updates nothing.
_MBID_SHAPE = "________-____-____-____-____________"
_MBID_SCRUB_COLUMNS = (
    ("albums", "musicbrainz_release_id"),
    ("albums", "musicbrainz_release_group_id"),
    ("tracks", "musicbrainz_recording_id"),
    ("artists", "musicbrainz_id"),
)


def _scrub_invalid_mbids(session: Session) -> None:
    for table, column in _MBID_SCRUB_COLUMNS:
        columns = {row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))}
        if column not in columns:
            continue
        session.execute(
            text(
                f"UPDATE {table} SET {column} = NULL "
                f"WHERE {column} IS NOT NULL AND {column} NOT LIKE :shape"
            ),
            {"shape": _MBID_SHAPE},
        )
    session.commit()


# Old fine-grained permission -> new flow/menu permission(s). notifications:read is
# dropped (notifications now route by the flow they belong to). Unlisted values are
# kept as-is (identity), which also makes this migration idempotent: after it runs,
# no stored value is a key here, so a second pass is a no-op.
_PERMISSION_REMAP = {
    "library:read": ["library:view"],
    "library:write": ["library:edit"],
    "metadata:edit": ["library:edit"],
    "library:manage": ["tools:manage", "library:edit"],
    "wishlist:manage_own": ["discover"],
    "wishlist:manage_all": ["wishlist:approve_all"],
    "downloads:manage": ["discover"],
    "backups:manage": ["tools:manage"],
    "jellyfin:manage": ["tools:manage"],
    "notifications:read": [],
}


def _migrate_permissions(session: Session) -> None:
    """Normalize user_permissions rows to the current flow/menu permission set.

    Does two jobs, idempotently, in one pass:
      1. Collapses the old 18 fine-grained permissions into the new set (_PERMISSION_REMAP).
      2. Repairs rows stored in the wrong serialization form. SQLAlchemy's ``Enum(Permission)``
         column persists/reads the enum MEMBER NAME ("library_view"), but an earlier version of
         this migration inserted the enum VALUE ("library:view") via raw SQL — those rows raise
         LookupError on ORM load and 500 every endpoint that serializes a user's permissions.
         We resolve each stored string back to a real Permission and rewrite it as the name.

    Only runs when at least one row is not already a clean member-name value (so it is a no-op on
    an already-correct DB). Dedupes because several old permissions map onto the same new one.
    """
    rows = list(session.execute(text("SELECT user_id, permission FROM user_permissions")))
    if not rows:
        return
    by_name = {permission.name: permission for permission in Permission}
    by_value = {permission.value: permission for permission in Permission}
    # Old fine-grained permissions were stored by the ORM as the member NAME ("library_read").
    # _PERMISSION_REMAP is keyed by the old VALUE ("library:read"); accept the old name form too
    # (member name == value with ':' -> '_'), so rows the ORM wrote directly are remapped, not
    # dropped. This is why the original collapse never fired — it only matched the value form.
    remap: dict[str, list[Permission]] = {}
    for old_value, targets in _PERMISSION_REMAP.items():
        members = [by_value[value] for value in targets]
        remap[old_value] = members
        remap[old_value.replace(":", "_")] = members
    # Clean row = already a current member name and not an old remap key. Skip the whole pass
    # only when every row is clean.
    if all(perm in by_name and perm not in remap for _, perm in rows):
        return

    def resolve(perm: str) -> list[Permission]:
        if perm in remap:
            return remap[perm]
        if perm in by_name:   # already a current member name
            return [by_name[perm]]
        if perm in by_value:  # mis-stored current value form, e.g. "library:view"
            return [by_value[perm]]
        return []             # unknown / dropped permission

    new_by_user: dict[str, set[Permission]] = {}
    for user_id, perm in rows:
        new_by_user.setdefault(user_id, set()).update(resolve(perm))
    session.execute(text("DELETE FROM user_permissions"))
    for user_id, perms in new_by_user.items():
        for permission in sorted(perms, key=lambda item: item.name):
            session.execute(
                text("INSERT INTO user_permissions (id, user_id, permission) VALUES (:id, :uid, :perm)"),
                {"id": uuid.uuid4().hex, "uid": user_id, "perm": permission.name},
            )
    session.commit()


def _migrate_library_timestamps(session: Session) -> None:
    """Add created_at/updated_at to artists/albums/tracks for delta sync + recently-added.

    SQLite forbids ALTER TABLE ADD COLUMN with a non-constant DEFAULT (CURRENT_TIMESTAMP),
    so add the columns nullable, then backfill existing rows with a constant timestamp.
    """
    # Match SQLAlchemy's SQLite DATETIME storage format (space-separated, no offset) so
    # string comparisons in /library/changes (updated_at > :since) work against ORM-written rows.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    for table in ("artists", "albums", "tracks"):
        cols = {row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))}
        if not cols:
            continue
        added = False
        if "created_at" not in cols:
            session.execute(text(f"ALTER TABLE {table} ADD COLUMN created_at DATETIME"))
            added = True
        if "updated_at" not in cols:
            session.execute(text(f"ALTER TABLE {table} ADD COLUMN updated_at DATETIME"))
            session.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_updated_at ON {table}(updated_at)"))
            added = True
        if added:
            session.execute(
                text(f"UPDATE {table} SET created_at = COALESCE(created_at, :now), updated_at = COALESCE(updated_at, :now)"),
                {"now": now},
            )
    session.commit()


def _drop_server_side_podcast_downloads(session: Session) -> None:
    """Retire every column that only existed because this server used to download podcast audio.

    Podcasts are now purely a subscription: clients stream and download from the publisher's
    enclosure themselves. The download policy (`download_limit`, `auto_download`, `purge_after_days`,
    `download_target`) and the per-episode file bookkeeping (`path`, `downloaded_at`,
    `download_state`, `bitrate`) describe a copy that no longer exists.

    ⚠️ These have to be *dropped*, not merely left unmapped: `auto_download`, `download_target` and
    `download_state` are NOT NULL with Python-side defaults, so an unmapped column would fail every
    INSERT. Any already-downloaded files are removed first — nothing reads them after this, so
    leaving them would be dead bytes nobody would ever think to look for.

    SQLite refuses to drop an indexed column, hence the DROP INDEX pass. Each statement is
    individually guarded so an unexpected schema (or an SQLite older than 3.35) degrades to
    "migration skipped" rather than taking the API and worker down at boot.
    """
    episode_cols = {row[1] for row in session.execute(text("PRAGMA table_info(episodes)"))}
    if "path" in episode_cols:
        removed = 0
        for (path,) in session.execute(text("SELECT path FROM episodes WHERE path IS NOT NULL")):
            try:
                Path(path).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        if removed:
            write_app_log(
                f"Removed {removed} server-side podcast episode file(s): episodes now stream from the publisher",
                feature="podcasts",
            )
    for index in (
        "ix_episodes_path",
        "ix_episodes_downloaded_at",
        "ix_episodes_download_state",
    ):
        _try_execute(session, f"DROP INDEX IF EXISTS {index}")
    for column in ("path", "downloaded_at", "download_state", "bitrate"):
        if column in episode_cols:
            _try_execute(session, f"ALTER TABLE episodes DROP COLUMN {column}")
    podcast_cols = {row[1] for row in session.execute(text("PRAGMA table_info(podcasts)"))}
    for column in ("download_limit", "purge_after_days", "auto_download", "download_target"):
        if column in podcast_cols:
            _try_execute(session, f"ALTER TABLE podcasts DROP COLUMN {column}")


def _try_execute(session: Session, statement: str) -> None:
    """Run one migration statement, rolling back (not raising) if the schema doesn't allow it."""
    try:
        session.execute(text(statement))
        session.commit()
    except Exception as error:  # noqa: BLE001 - a migration must never stop the process booting.
        session.rollback()
        write_app_log(f"Schema migration skipped: {statement} ({error})", "warning")


def _backfill_usernames(session: Session) -> None:
    rows = list(session.execute(text("SELECT id, display_name, is_admin, username FROM users")))
    taken = {str(r[3]).lower() for r in rows if r[3]}
    for user_id, display_name, is_admin, username in rows:
        if username:
            continue
        base = "admin" if is_admin else slugify_username(display_name or "user")
        candidate = base
        n = 1
        while candidate.lower() in taken:
            n += 1
            candidate = f"{base}{n}"
        taken.add(candidate.lower())
        session.execute(text("UPDATE users SET username = :u WHERE id = :id"), {"u": candidate, "id": user_id})
    session.commit()


def _migrate_password_hashes(session: Session) -> None:
    rows = list(session.execute(text("SELECT id, pin_hash FROM users")))
    for user_id, pin_hash in rows:
        if pin_hash and not is_bcrypt_hash(pin_hash):
            session.execute(text("UPDATE users SET pin_hash = :h WHERE id = :id"), {"h": wrap_legacy_hash(pin_hash), "id": user_id})
    session.commit()


def _migrate_playlists_per_user(session: Session) -> None:
    """Recreate playlists table with per-user ownership and updated unique constraint."""
    playlist_columns = {row[1] for row in session.execute(text("PRAGMA table_info(playlists)"))}
    if "user_id" in playlist_columns:
        return
    # Recreate with user_id column; SQLite doesn't support DROP CONSTRAINT
    session.execute(text("""
        CREATE TABLE playlists_new (
            id VARCHAR NOT NULL PRIMARY KEY,
            user_id VARCHAR REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            protected BOOLEAN NOT NULL DEFAULT 0,
            jellyfin_playlist_id VARCHAR(128),
            created_at DATETIME,
            UNIQUE (user_id, name)
        )
    """))
    # Copy existing playlists and assign all to the admin user
    session.execute(text("""
        INSERT INTO playlists_new (id, user_id, name, protected, jellyfin_playlist_id, created_at)
        SELECT p.id,
               (SELECT id FROM users WHERE is_admin = 1 ORDER BY created_at ASC LIMIT 1),
               p.name, p.protected, p.jellyfin_playlist_id, p.created_at
        FROM playlists p
    """))
    session.execute(text("DROP TABLE playlists"))
    session.execute(text("ALTER TABLE playlists_new RENAME TO playlists"))
    session.commit()


def move_task_result_logs_to_app_log(session: Session) -> None:
    changed = False
    for task in session.scalars(select(Task).where(Task.result_json.like('%"logs"%'))):
        try:
            result = json.loads(task.result_json or "{}")
        except json.JSONDecodeError:
            continue
        logs = result.pop("logs", None)
        if not isinstance(logs, list):
            continue
        for entry in logs:
            if not isinstance(entry, dict):
                continue
            write_app_log(
                str(entry.get("message") or ""),
                level=str(entry.get("level") or "info"),
                task_id=task.id,
                task_type=task.type,
                migrated_from="task_result",
            )
        task.result_json = json.dumps(result)
        changed = True
    if changed:
        session.commit()
