import hashlib
import json

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import Base, Permission, Task, User, UserPermission
from nudibranch.db.session import engine
from nudibranch.services.app_log import write_app_log


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def init_db(session: Session) -> None:
    Base.metadata.create_all(bind=engine)
    ensure_lightweight_migrations(session)
    existing_admin = session.scalar(select(User).where(User.is_admin.is_(True)))
    if existing_admin:
        return

    settings = get_settings()
    admin = User(
        display_name="Admin",
        pin_hash=hash_secret(settings.first_admin_pin),
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
    track_columns = {row[1] for row in session.execute(text("PRAGMA table_info(tracks)"))}
    if "musicbrainz_verified" not in track_columns:
        session.execute(text("ALTER TABLE tracks ADD COLUMN musicbrainz_verified BOOLEAN NOT NULL DEFAULT 0"))
        session.commit()
    move_task_result_logs_to_app_log(session)


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
