import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import Base, Permission, Task, User, UserPermission
from nudibranch.services.auth import hash_password, is_bcrypt_hash, slugify_username, wrap_legacy_hash
from nudibranch.db.session import engine
from nudibranch.services.app_log import write_app_log


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def init_db(session: Session) -> None:
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
    album_columns = {row[1] for row in session.execute(text("PRAGMA table_info(albums)"))}
    if "sort_name" not in album_columns:
        session.execute(text("ALTER TABLE albums ADD COLUMN sort_name VARCHAR(255)"))
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
    playlist_columns = {row[1] for row in session.execute(text("PRAGMA table_info(playlists)"))}
    if "origin" not in playlist_columns:
        session.execute(text("ALTER TABLE playlists ADD COLUMN origin TEXT NULL"))
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
    pstate_cols = {row[1] for row in session.execute(text("PRAGMA table_info(player_states)"))}
    if "shuffle" not in pstate_cols:
        session.execute(text("ALTER TABLE player_states ADD COLUMN shuffle BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    if "repeat" not in pstate_cols:
        session.execute(text("ALTER TABLE player_states ADD COLUMN repeat VARCHAR(8) NOT NULL DEFAULT 'off'"))
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
    move_task_result_logs_to_app_log(session)


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
    # Clean row = already a current member name and not an old remap key. Skip the whole pass
    # only when every row is clean.
    if all(perm in by_name and perm not in _PERMISSION_REMAP for _, perm in rows):
        return

    def resolve(perm: str) -> list[Permission]:
        if perm in _PERMISSION_REMAP:
            return [by_value[value] for value in _PERMISSION_REMAP[perm]]
        if perm in by_name:
            return [by_name[perm]]
        if perm in by_value:  # mis-stored value form, e.g. "library:view"
            return [by_value[perm]]
        return []  # unknown / dropped permission

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
