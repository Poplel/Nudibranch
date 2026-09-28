import fcntl
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import bindparam, text
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
        is_admin=True,
    )
    session.add(admin)
    session.flush()

    for permission in Permission:
        session.add(UserPermission(user_id=admin.id, permission=permission))

    # The env full-access key is an ordinary static API key now, so it can be listed and revoked
    # like any other instead of living in a per-user column nothing could manage.
    _ensure_env_full_access_key(session, admin)
    session.commit()


def _ensure_env_full_access_key(session: Session, admin: User) -> None:
    """Make `NUDIBRANCH_FULL_ACCESS_API_KEY` a `StaticApiKey` row owned by the admin.

    Idempotent, and hashed exactly the way `deps.get_current_user` hashes a bearer token
    (`hash_token` == this module's old `hash_secret`: both are sha256 hex), so a key already in a
    deploy's `.env` keeps working now that the `users.api_key_hash` lookup is gone.
    """
    from nudibranch.db.models import StaticApiKey

    raw = (get_settings().full_access_api_key or "").strip()
    if not raw:
        return
    key_hash = hash_secret(raw)
    if session.scalar(select(StaticApiKey).where(StaticApiKey.key_hash == key_hash)):
        return
    session.add(
        StaticApiKey(
            user_id=admin.id,
            name="Full-access key (from the environment)",
            key_hash=key_hash,
            prefix=raw[:8],
        )
    )
    session.flush()


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
    # The per-device queue copy and start-time tiebreak were replaced by the account playback session
    # (`account_playback_sessions`); the handoff autoplay flag went with the old transfer route.
    _drop_columns(session, "session_player_states", ["queue_json", "queue_hash", "queue_updated_at", "playback_started_at"])
    _drop_columns(session, "playback_handoffs", ["autoplay"])
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
    if "playback_claim_timeout_minutes" not in user_cols:
        # Minutes a playback claim survives without playing; 0 = never, and never is now the
        # default (2026-09-23 -- "Never" is the default for Hand Off After).
        session.execute(
            text(
                "ALTER TABLE users ADD COLUMN playback_claim_timeout_minutes "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )
        session.commit()
    _migrate_queue_state_columns(session)
    # ⚠️ ORDER: everything that still reads `tree_path` must run BEFORE it is dropped.
    # `_migrate_queue_state_columns` backfills `flow` from it, and `_retire_intent_batches`
    # identifies the old "/wishlist" batches by it.
    _retire_intent_batches(session)
    _drop_columns(session, "proposal_batches", ["tree_path"])
    _migrate_static_api_keys(session)
    _backfill_usernames(session)
    _migrate_password_hashes(session)
    _migrate_playlists_per_user(session)
    _migrate_library_timestamps(session)
    _migrate_permissions(session)
    _scrub_invalid_mbids(session)
    _drop_empty_rejected_batches(session)
    _drop_canceled_leftovers(session)
    _fail_completed_batches_with_failed_items(session)
    _delete_empty_proposal_batches(session)
    _heal_searching_parent_items(session)
    move_task_result_logs_to_app_log(session)


def _drop_columns(session: Session, table: str, columns: list[str]) -> None:
    """Drop columns an older install still has. SQLite >= 3.35 supports DROP COLUMN."""
    existing = {row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))}
    for column in columns:
        if column in existing:
            session.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    session.commit()


def _retire_intent_batches(session: Session) -> None:
    """Delete the "Request: X" intent batches, and heal the wishlist rows they stranded.

    Until 2026-09-22 a wishlist search committed a *visible* pending batch of `wishlist_request`
    rows into Review before the search ran, and retired it only on the happy path. A worker killed
    mid-search (sandalphon recreates its containers on every nightly push) therefore left a batch of
    rows reading "finding candidates" in Review forever, with nothing able to finish or clear them —
    and removing them one at a time emptied the batch without ever retiring it. Searching no longer
    creates a batch at all, so these are pure debris.

    Idempotent and self-limiting: after `tree_path` is dropped there is nothing left to match, and
    the rows this deletes never existed on a database created after the change.
    """
    batch_cols = {row[1] for row in session.execute(text("PRAGMA table_info(proposal_batches)"))}
    if "tree_path" not in batch_cols:
        return
    stale_ids = [
        row[0]
        for row in session.execute(
            text("SELECT id FROM proposal_batches WHERE tree_path = '/wishlist'")
        )
    ]
    if not stale_ids:
        return
    # The wishlist rows these batches were serving: re-point them at reality. One that already has
    # real candidates elsewhere is awaiting approval; one with nothing is searching again, and the
    # worker's recovery sweep re-queues it on its next tick.
    wishlist_ids = [
        row[0]
        for row in session.execute(
            text(
                "SELECT DISTINCT wishlist_item_id FROM proposal_items "
                "WHERE wishlist_item_id IS NOT NULL AND batch_id IN :ids"
            ).bindparams(bindparam("ids", value=stale_ids, expanding=True))
        )
    ]
    session.execute(
        text("DELETE FROM proposal_items WHERE batch_id IN :ids").bindparams(
            bindparam("ids", value=stale_ids, expanding=True)
        )
    )
    session.execute(
        text("DELETE FROM proposal_batches WHERE id IN :ids").bindparams(
            bindparam("ids", value=stale_ids, expanding=True)
        )
    )
    for wishlist_id in wishlist_ids:
        has_live_work = session.execute(
            text(
                "SELECT 1 FROM proposal_items WHERE wishlist_item_id = :id "
                "AND status IN ('pending', 'approved', 'executing') LIMIT 1"
            ),
            {"id": wishlist_id},
        ).first()
        session.execute(
            text(
                "UPDATE wishlist_items SET status = :status, stage = :stage "
                "WHERE id = :id AND status NOT IN "
                "('completed', 'rejected', 'removed', 'canceled', 'failed')"
            ),
            {
                "id": wishlist_id,
                "status": "review" if has_live_work else "searching",
                "stage": "awaiting_approval" if has_live_work else "searching",
            },
        )
    # A wishlist row that is settled (the user declined or removed it) must not keep work of its
    # own: it was the "removing a request left its rows behind" bug, and this clears what it left.
    session.execute(
        text(
            "DELETE FROM proposal_items WHERE wishlist_item_id IN "
            "(SELECT id FROM wishlist_items WHERE status IN ('rejected', 'removed')) "
            "AND status IN ('pending', 'approved')"
        )
    )
    session.commit()
    write_app_log(
        f"Retired {len(stale_ids)} stale wishlist intent batch(es) and healed "
        f"{len(wishlist_ids)} request row(s)",
        "warning",
    )


def _delete_empty_proposal_batches(session: Session) -> None:
    """Delete batches with no items left.

    A batch is only ever a container for its rows, and an empty one is unactionable debris — but
    nothing deleted them unless a *rejection* emptied them, so removing rows one by one left a husk
    in the Task Queue that no UI could clear. Download batches younger than 10 minutes are spared:
    a candidate search commits its batch before attaching the first candidate.

    Childless artist/album/track containers go first, for the same reason and in the same shape as
    `proposals.cleanup_empty_container_items`: a batch holding nothing but grouping rows renders as
    an empty tree rather than as nothing at all. Three passes covers artist > album > track.
    """
    for _ in range(3):
        session.execute(
            text(
                "DELETE FROM proposal_items WHERE instr(coalesce(payload_json, ''), '\"action\"') = 0 "
                "AND NOT EXISTS (SELECT 1 FROM proposal_items child WHERE child.parent_id = proposal_items.id) "
                "AND (old_value IS NULL OR new_value IS NULL)"
            )
        )
    session.commit()
    session.execute(
        text(
            "DELETE FROM proposal_batches WHERE NOT EXISTS "
            "(SELECT 1 FROM proposal_items WHERE proposal_items.batch_id = proposal_batches.id) "
            "AND (kind != 'download' OR created_at < datetime('now', '-10 minutes'))"
        )
    )
    session.commit()


def _heal_searching_parent_items(session: Session) -> None:
    """Un-stick track rows cached as `searching` that already have candidates under them.

    The stage cache was written when the row was created and not re-stamped when its search ended,
    so a track with five ready candidates still reported "finding candidates" -- `can_approve=false`
    on a row a client then (rightly) refuses to approve around. The write-time fix is in the worker;
    this repairs the rows that already exist. Idempotent.
    """
    session.execute(text(
        "UPDATE proposal_items SET stage = 'awaiting_approval' "
        "WHERE stage = 'searching' AND status = 'pending' AND EXISTS ("
        "SELECT 1 FROM proposal_items child WHERE child.parent_id = proposal_items.id "
        "AND child.status IN ('pending', 'approved', 'executing'))"
    ))
    session.commit()


def _migrate_static_api_keys(session: Session) -> None:
    """Move `users.api_key_hash` into `static_api_keys`, then drop the column.

    Same stored form on both sides (sha256 of the token), so every key that worked before still
    works — as a row that can be listed and revoked, which the column never could be.
    """
    user_cols = {row[1] for row in session.execute(text("PRAGMA table_info(users)"))}
    if "api_key_hash" in user_cols:
        rows = list(
            session.execute(
                text(
                    "SELECT id, api_key_hash FROM users "
                    "WHERE api_key_hash IS NOT NULL AND api_key_hash != ''"
                )
            )
        )
        for user_id, key_hash in rows:
            existing = session.execute(
                text("SELECT 1 FROM static_api_keys WHERE key_hash = :hash"), {"hash": key_hash}
            ).first()
            if existing:
                continue
            session.execute(
                text(
                    "INSERT INTO static_api_keys (id, user_id, name, key_hash, prefix, created_at, revoked) "
                    "VALUES (:id, :user_id, :name, :hash, :prefix, :created_at, 0)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "name": "Full-access key (migrated)",
                    "hash": key_hash,
                    "prefix": str(key_hash)[:8],
                    "created_at": datetime.now(timezone.utc).isoformat(sep=" "),
                },
            )
        session.commit()
        _drop_columns(session, "users", ["api_key_hash"])
        if rows:
            write_app_log(f"Migrated {len(rows)} legacy API key(s) to static_api_keys", "warning")
    # A deploy whose `.env` key was only ever in that column (or in no column at all) still needs a
    # row to authenticate against.
    admin = session.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.created_at.asc()))
    if admin is not None:
        _ensure_env_full_access_key(session, admin)
        session.commit()


def _drop_empty_rejected_batches(session: Session) -> None:
    """Delete fully rejected batches left behind before rejection started removing them.

    Rejecting deletes a batch's items, and since 2026-09-21 the reject route deletes the emptied
    batch as well. Rows written before that remain as empty `rejected` husks in the Task Queue
    history. Idempotent: once they are gone this matches nothing.
    """
    session.execute(text(
        "DELETE FROM proposal_batches WHERE status = 'rejected' "
        "AND NOT EXISTS (SELECT 1 FROM proposal_items WHERE proposal_items.batch_id = proposal_batches.id)"
    ))
    session.commit()


def _fail_completed_batches_with_failed_items(session: Session) -> None:
    """Re-mark batches that were stored `completed` although a selected item failed.

    Before 2026-09-22 the worker wrote `completed` once every item result had settled, failures
    included. Reads already project those as failed, but list filters work on the stored status, so
    the failure never showed in Issues. Idempotent: once fixed, nothing matches.
    """
    session.execute(text(
        "UPDATE proposal_batches SET status = 'failed' WHERE status = 'completed' AND EXISTS ("
        "SELECT 1 FROM proposal_items WHERE proposal_items.batch_id = proposal_batches.id "
        "AND proposal_items.selected = 1 AND proposal_items.status = 'failed')"
    ))
    session.commit()


def _drop_canceled_leftovers(session: Session) -> None:
    """One-time cleanup for the pre-2026-09-21 cancel behaviour.

    Before that date, `cancel_items` left cancelled items sitting in the DB forever (instead of
    deleting them once the worker had stopped their transfers) and marked a batch `rejected` -- not
    `canceled` -- once everything in it had been cancelled, overloading "rejected" to mean two
    different things. This deletes the leftover `canceled` item rows outright (their files are not
    touched here -- a *live* cancel already removed those on disk; this migration only fixes rows a
    completed cancel would already have cleaned up), then removes every `canceled` batch and every
    `rejected` one, items and all. Idempotent: once nothing matches, both statements are no-ops.
    """
    # A `rejected` batch that still has items can only be one of those mislabeled cancels -- a real
    # rejection deletes every item first -- so it goes whole, like a `canceled` one. Items are deleted
    # explicitly because raw SQL here does not rely on SQLite enforcing the ON DELETE CASCADE.
    session.execute(text(
        "DELETE FROM proposal_items WHERE status = 'canceled' "
        "OR batch_id IN (SELECT id FROM proposal_batches WHERE status IN ('canceled', 'rejected'))"
    ))
    session.execute(text("DELETE FROM proposal_batches WHERE status IN ('canceled', 'rejected')"))
    session.commit()


def _migrate_queue_state_columns(session: Session) -> None:
    """Add the Review/Issues/Changes state columns and backfill them from what already exists.

    Idempotent, PRAGMA-guarded, and safe to re-run: each backfill only runs in the same branch
    that just created its column, so a second pass is a no-op rather than a re-write.

    NOTE on the raw-SQL backfills below: an `Enum()` column persists the enum MEMBER NAME, not its
    value.  `ProposalFlow`'s names and values are deliberately identical, which is what makes
    writing the literal strings here correct.  Do not add a member where they differ.
    """

    batch_cols = {row[1] for row in session.execute(text("PRAGMA table_info(proposal_batches)"))}
    if batch_cols and "flow" not in batch_cols:
        session.execute(
            text(
                "ALTER TABLE proposal_batches ADD COLUMN flow VARCHAR(32) "
                "NOT NULL DEFAULT 'library_change'"
            )
        )
        session.execute(
            text("CREATE INDEX IF NOT EXISTS ix_proposal_batches_flow ON proposal_batches(flow)")
        )
        # Gate (a): every download batch, whichever stage marker it happened to carry.
        session.execute(
            text(
                "UPDATE proposal_batches SET flow = 'download_review' "
                "WHERE kind = 'download' "
                "AND tree_path IN ('/wishlist', '/task-queue', '/downloads')"
            )
        )
        # Gate (b): the "add the staged files to the library" review, distinguished from an
        # ordinary disk import only by its title -- there was never a typed marker for it.
        session.execute(
            text(
                "UPDATE proposal_batches SET flow = 'library_review' "
                "WHERE kind = 'import_files' AND tree_path = '/task-queue' "
                "AND (title LIKE 'Add downloaded music to library%' "
                "     OR title LIKE 'Add to library:%')"
            )
        )
        session.commit()

    item_cols = {row[1] for row in session.execute(text("PRAGMA table_info(proposal_items)"))}
    if item_cols and "requester_id" not in item_cols:
        session.execute(text("ALTER TABLE proposal_items ADD COLUMN requester_id VARCHAR NULL"))
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_proposal_items_requester_id "
                "ON proposal_items(requester_id)"
            )
        )
        session.commit()
        _backfill_item_json_column(session, "requester_id", ("user_id",), ("request", "user_id"))
    if item_cols and "wishlist_item_id" not in item_cols:
        session.execute(text("ALTER TABLE proposal_items ADD COLUMN wishlist_item_id VARCHAR NULL"))
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_proposal_items_wishlist_item_id "
                "ON proposal_items(wishlist_item_id)"
            )
        )
        session.commit()
        _backfill_item_json_column(
            session, "wishlist_item_id", ("wishlist_item_id",), ("request", "wishlist_item_id")
        )
    if item_cols and "stage" not in item_cols:
        # Denormalized cache of resolve_stage(); left NULL so the resolver's status/payload
        # fallback answers for every pre-existing row until the worker next touches it.
        session.execute(text("ALTER TABLE proposal_items ADD COLUMN stage VARCHAR(24) NULL"))
        session.execute(
            text("CREATE INDEX IF NOT EXISTS ix_proposal_items_stage ON proposal_items(stage)")
        )
        session.commit()

    wishlist_cols = {row[1] for row in session.execute(text("PRAGMA table_info(wishlist_items)"))}
    if wishlist_cols and "batch_id" not in wishlist_cols:
        # No backfill is possible -- this linkage never existed, and reconstructing it from the
        # payload JSON would be guesswork.  NULL means "legacy row"; the read path falls back to
        # the old scan for exactly those, for one release.
        session.execute(text("ALTER TABLE wishlist_items ADD COLUMN batch_id VARCHAR NULL"))
        session.execute(
            text("CREATE INDEX IF NOT EXISTS ix_wishlist_items_batch_id ON wishlist_items(batch_id)")
        )
        session.commit()
    if wishlist_cols and "item_id" not in wishlist_cols:
        session.execute(text("ALTER TABLE wishlist_items ADD COLUMN item_id VARCHAR NULL"))
        session.commit()
    if wishlist_cols and "stage" not in wishlist_cols:
        session.execute(text("ALTER TABLE wishlist_items ADD COLUMN stage VARCHAR(24) NULL"))
        session.commit()


def _backfill_item_json_column(
    session: Session,
    column: str,
    top_key: tuple[str, ...],
    nested_key: tuple[str, ...],
) -> None:
    """Lift a value out of proposal_items.payload_json into a real column.

    Tries SQLite's json_extract first (one statement, no round trip).  JSON1 has been compiled in
    by default since SQLite 3.38, but the deployed build is not something this code can assume, so
    a plain Python loop is the fallback rather than a note in a plan.  The loop is bounded by the
    number of live proposal items, which prune_settled_batches keeps small.
    """

    top = "$." + ".".join(top_key)
    nested = "$." + ".".join(nested_key)
    try:
        session.execute(
            text(
                f"UPDATE proposal_items SET {column} = COALESCE("
                f"  json_extract(payload_json, :top), json_extract(payload_json, :nested)) "
                f"WHERE {column} IS NULL AND payload_json LIKE :needle"
            ),
            {"top": top, "nested": nested, "needle": f"%{top_key[-1]}%"},
        )
        session.commit()
        return
    except OperationalError:
        session.rollback()

    rows = session.execute(
        text(
            f"SELECT id, payload_json FROM proposal_items "
            f"WHERE {column} IS NULL AND payload_json LIKE :needle"
        ),
        {"needle": f"%{top_key[-1]}%"},
    ).fetchall()
    for item_id, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get(top_key[-1])
        if value is None:
            nested_obj = payload.get(nested_key[0])
            if isinstance(nested_obj, dict):
                value = nested_obj.get(nested_key[-1])
        if value is None:
            continue
        session.execute(
            text(f"UPDATE proposal_items SET {column} = :value WHERE id = :id"),
            {"value": str(value), "id": item_id},
        )
    session.commit()



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
