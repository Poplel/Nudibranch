import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import Base, Permission, User, UserPermission
from nudibranch.db.session import engine


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def init_db(session: Session) -> None:
    Base.metadata.create_all(bind=engine)
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

