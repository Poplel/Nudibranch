from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.db.init import hash_secret
from nudibranch.db.models import AuthSession, Permission, StaticApiKey, User
from nudibranch.db.session import get_session
from nudibranch.services.auth import hash_token

# Sliding session lifetime: every authenticated request extends it.
SESSION_TTL = timedelta(days=90)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def get_current_user(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    token = authorization.split(" ", 1)[1].strip()
    token_h = hash_token(token)
    now = datetime.now(timezone.utc)

    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_h))
    if auth_session:
        if _aware(auth_session.expires_at) < now:
            session.delete(auth_session)
            session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
        auth_session.last_used_at = now
        auth_session.expires_at = now + SESSION_TTL
        session.commit()
        return auth_session.user

    static_key = session.scalar(
        select(StaticApiKey).where(StaticApiKey.key_hash == token_h, StaticApiKey.revoked.is_(False))
    )
    if static_key:
        static_key.last_used_at = now
        session.commit()
        return static_key.user

    # Legacy fallback: env full-access key + web clients still holding a pre-refactor api_key.
    user = session.scalar(select(User).where(User.api_key_hash == hash_secret(token)))
    if user:
        return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def resolve_media_user(session: Session, token: str) -> User | None:
    """Resolve a user from a media query-param token (``?api_key=...``).

    Audio/cover/lyrics are loaded by ``<audio>``/``<img>`` elements that cannot
    send an Authorization header, so they pass the token in the query string.
    This mirrors ``get_current_user``'s precedence — session token, static API
    key, then the legacy ``api_key_hash`` — so a logged-in session token works
    for media the same way it does for header-authed routes. Returns ``None`` if
    the token matches nothing (callers raise their own 401/permission error).
    """
    if not token:
        return None
    token_h = hash_token(token)
    now = datetime.now(timezone.utc)

    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_h))
    if auth_session:
        if _aware(auth_session.expires_at) < now:
            return None
        auth_session.last_used_at = now
        auth_session.expires_at = now + SESSION_TTL
        session.commit()
        return auth_session.user

    static_key = session.scalar(
        select(StaticApiKey).where(StaticApiKey.key_hash == token_h, StaticApiKey.revoked.is_(False))
    )
    if static_key:
        static_key.last_used_at = now
        session.commit()
        return static_key.user

    return session.scalar(select(User).where(User.api_key_hash == hash_secret(token)))


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def require_permission(permission: Permission):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.is_admin:
            return user
        if any(user_permission.permission == permission for user_permission in user.permissions):
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires {permission.value}")

    return dependency
