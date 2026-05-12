from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.db.init import hash_secret
from nudibranch.db.models import Permission, User
from nudibranch.db.session import get_session


def get_current_user(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    api_key = authorization.split(" ", 1)[1].strip()
    user = session.scalar(select(User).where(User.api_key_hash == hash_secret(api_key)))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return user


def require_permission(permission: Permission):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.is_admin:
            return user
        if any(user_permission.permission == permission for user_permission in user.permissions):
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires {permission.value}")

    return dependency

