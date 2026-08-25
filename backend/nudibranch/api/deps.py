from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.db.init import hash_secret
from nudibranch.db.models import AuthSession, Permission, StaticApiKey, User
from nudibranch.db.session import get_session
from nudibranch.services.auth import hash_token

# Sliding session lifetime: every authenticated request extends it.
SESSION_TTL = timedelta(days=90)

# Presence/sliding-expiry bump throttle: every commit takes SQLite's single
# cross-process write lock, and a page load fires dozens of authed requests at
# once — bumping on each one serialized the whole burst behind the lock.
# last_used_at only feeds the ~60s presence window, so a bump every ≥30s keeps
# presence accurate while removing the per-request write.
SESSION_BUMP_INTERVAL = timedelta(seconds=30)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _needs_bump(last_used_at: datetime | None, now: datetime) -> bool:
    if last_used_at is None:
        return True
    return now - _aware(last_used_at) >= SESSION_BUMP_INTERVAL


# Declared as a security scheme rather than a raw Header so that the generated OpenAPI document
# advertises it. That is what gives Swagger UI its "Authorize" button, and it stops `authorization`
# from being rendered as an ordinary optional header parameter on every authenticated operation.
# auto_error is False because this function raises its own 401 with a more specific message.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="Bearer token",
    description=(
        "A session token from `POST /api/v1/auth/login`, or a static API key from "
        "`/api/v1/me/api-keys`. Send as `Authorization: Bearer <token>`."
    ),
)


def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    if credentials is None or (credentials.scheme or "").lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    token = (credentials.credentials or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")
    token_h = hash_token(token)
    now = datetime.now(timezone.utc)

    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_h))
    if auth_session:
        if _aware(auth_session.expires_at) < now:
            session.delete(auth_session)
            session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
        if _needs_bump(auth_session.last_used_at, now):
            auth_session.last_used_at = now
            auth_session.expires_at = now + SESSION_TTL
            session.commit()
        # Stashed rather than looked up again by get_current_auth_session: this function has
        # already paid for the token hash and the indexed lookup, and a second query would run on
        # every request that wants the session — against SQLite's single write lock (see
        # SESSION_BUMP_INTERVAL above for what request storms cost here).
        request.state.auth_session = auth_session
        return auth_session.user

    static_key = session.scalar(
        select(StaticApiKey).where(StaticApiKey.key_hash == token_h, StaticApiKey.revoked.is_(False))
    )
    if static_key:
        if _needs_bump(static_key.last_used_at, now):
            static_key.last_used_at = now
            session.commit()
        # A static key is not a device — there is no session behind it to attribute state to.
        request.state.auth_session = None
        return static_key.user

    # Legacy fallback: env full-access key + web clients still holding a pre-refactor api_key.
    user = session.scalar(select(User).where(User.api_key_hash == hash_secret(token)))
    if user:
        request.state.auth_session = None
        return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def get_current_auth_session(
    request: Request,
    _user: User = Depends(get_current_user),
) -> AuthSession | None:
    """The device session behind this request, or None for static API keys and legacy keys.

    ⚠ The `get_current_user` dependency is what fills `request.state`, so this declares it rather
    than trusting a route to list them in the right order. Without it, a route whose parameters
    happen to resolve this one first gets `None` and silently attributes nothing to any device —
    a failure with no error to notice. FastAPI caches dependencies per request, so naming it here
    costs nothing on a route that already depends on it.
    """
    return getattr(request.state, "auth_session", None)


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
        if _needs_bump(auth_session.last_used_at, now):
            auth_session.last_used_at = now
            auth_session.expires_at = now + SESSION_TTL
            session.commit()
        return auth_session.user

    static_key = session.scalar(
        select(StaticApiKey).where(StaticApiKey.key_hash == token_h, StaticApiKey.revoked.is_(False))
    )
    if static_key:
        if _needs_bump(static_key.last_used_at, now):
            static_key.last_used_at = now
            session.commit()
        return static_key.user

    return session.scalar(select(User).where(User.api_key_hash == hash_secret(token)))


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


# Marker read by the OpenAPI customisation in `nudibranch.main`, which walks each route's
# dependency tree to discover its access requirement and publishes it in the generated schema.
# Without this, the permission a call needs is invisible in Swagger and discoverable only by
# provoking a 403 at runtime.
require_admin.__nudibranch_admin_only__ = True


def require_permission(permission: Permission):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.is_admin:
            return user
        if any(user_permission.permission == permission for user_permission in user.permissions):
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires {permission.value}")

    dependency.__nudibranch_permission__ = permission
    return dependency
