"""
APNS notification delivery.

Two delivery modes:
  - Direct: Nudibranch holds Apple Developer credentials and calls APNS itself.
  - Proxy:  Nudibranch signs requests with _PROXY_CLIENT_SECRET and sends them
            to a NudibranchProxy server, which holds the Apple credentials.

Proxy mode requires no Apple Developer account from the end-user.  The secret
below is the shared credential that proves to the proxy that the caller is a
legitimate Nudibranch instance.  Keep it out of logs and UI output.
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from sqlalchemy.exc import OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import AppSetting, MobileDevice, Notification, Permission, User

# Shared secret with the NudibranchProxy server.
# Must match NUDIBRANCH_PROXY_CLIENT_SECRET in the proxy's environment.
_PROXY_CLIENT_SECRET = "nb-proxy-v1-placeholder-replace-before-deploying-proxy"


def _get_or_create_instance_id(session: Session) -> str:
    """Return this instance's stable ID, creating it on first call."""
    row = session.get(AppSetting, "proxy_instance_id")
    if row is None:
        row = AppSetting(key="proxy_instance_id", value=secrets.token_hex(16))
        session.add(row)
        session.commit()
    return row.value


def _proxy_signature(
    instance_id: str,
    timestamp: int,
    nonce: str,
    apns_token: str,
    title: str,
    body: str,
) -> str:
    message = f"{timestamp}:{nonce}:{instance_id}:{apns_token}:{title}:{body}"
    return hmac.new(_PROXY_CLIENT_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def create_notification(
    session: Session,
    title: str,
    body: str,
    event_type: str,
    target_url: str | None = None,
    user_id: str | None = None,
    deliver_apns: bool = True,
) -> Notification:
    if user_id is None:
        users = list(session.scalars(select(User)))
        target_user_ids = [
            user.id
            for user in users
            if user.is_admin or any(permission.permission == Permission.notifications_read for permission in user.permissions)
        ]
        if target_user_ids:
            created: Notification | None = None
            for target_user_id in target_user_ids:
                created = create_notification(
                    session,
                    title=title,
                    body=body,
                    event_type=event_type,
                    target_url=target_url,
                    user_id=target_user_id,
                    deliver_apns=deliver_apns,
                )
            return created
    for attempt in range(3):
        notification = Notification(
            user_id=user_id,
            title=title,
            body=body,
            event_type=event_type,
            target_url=target_url,
            deliver_apns=deliver_apns,
        )
        session.add(notification)
        try:
            session.commit()
            session.refresh(notification)
            return notification
        except OperationalError as error:
            if "database is locked" not in str(error).lower() or attempt == 2:
                raise
            session.rollback()
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError("Notification could not be created")


async def deliver_apns_notifications(session: Session) -> int:
    settings = get_settings()
    if not settings.apns_enabled:
        return 0

    pending = list(
        session.scalars(
            select(Notification).where(
                Notification.deliver_apns.is_(True),
                Notification.apns_delivered_at.is_(None),
            )
        )
    )
    if not pending:
        return 0

    devices = list(session.scalars(select(MobileDevice).where(MobileDevice.enabled.is_(True))))
    if not devices:
        return 0

    if settings.apns_proxy_url:
        return await _deliver_via_proxy(session, pending, devices, settings.apns_proxy_url)
    return await _deliver_direct(session, pending, devices)


async def _deliver_via_proxy(session: Session, pending, devices, proxy_url: str) -> int:
    instance_id = _get_or_create_instance_id(session)
    delivered = 0
    base = proxy_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15) as client:
        for notification in pending:
            target_devices = [d for d in devices if notification.user_id in (None, d.user_id)]
            notification_delivered = False
            for device in target_devices:
                timestamp = int(time.time())
                nonce = secrets.token_hex(16)
                sig = _proxy_signature(
                    instance_id=instance_id,
                    timestamp=timestamp,
                    nonce=nonce,
                    apns_token=device.apns_token,
                    title=notification.title,
                    body=notification.body,
                )
                try:
                    response = await client.post(
                        f"{base}/push",
                        json={
                            "instance_id": instance_id,
                            "timestamp": timestamp,
                            "nonce": nonce,
                            "apns_token": device.apns_token,
                            "title": notification.title,
                            "body": notification.body,
                            "event_type": notification.event_type,
                            "target_url": notification.target_url,
                            "notification_id": notification.id,
                            "signature": sig,
                        },
                    )
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("message") == "device_token_gone":
                            device.enabled = False
                        else:
                            notification_delivered = True
                            delivered += 1
                    elif response.status_code == 429:
                        break
                except httpx.HTTPError:
                    continue
            if notification_delivered:
                notification.apns_delivered_at = datetime.now(timezone.utc)

    session.commit()
    return delivered


async def _deliver_direct(session: Session, pending, devices) -> int:
    settings = get_settings()
    if not all([settings.apns_team_id, settings.apns_key_id, settings.apns_bundle_id]):
        return 0
    if not settings.apns_private_key_path.exists():
        return 0

    token = _build_apns_jwt()
    host = "https://api.sandbox.push.apple.com" if settings.apns_use_sandbox else "https://api.push.apple.com"
    delivered = 0

    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        for notification in pending:
            target_devices = [d for d in devices if notification.user_id in (None, d.user_id)]
            notification_delivered = False
            for device in target_devices:
                try:
                    response = await client.post(
                        f"{host}/3/device/{device.apns_token}",
                        headers={
                            "authorization": f"bearer {token}",
                            "apns-topic": settings.apns_bundle_id,
                            "apns-push-type": "alert",
                            "apns-priority": "10",
                        },
                        json={
                            "aps": {
                                "alert": {"title": notification.title, "body": notification.body},
                                "sound": "default",
                                "badge": 1,
                            },
                            "event_type": notification.event_type,
                            "target_url": notification.target_url,
                            "notification_id": notification.id,
                        },
                    )
                    if response.status_code == 410:
                        device.enabled = False
                    response.raise_for_status()
                    notification_delivered = True
                    delivered += 1
                except httpx.HTTPError:
                    continue
            if notification_delivered:
                notification.apns_delivered_at = datetime.now(timezone.utc)

    session.commit()
    return delivered


def _build_apns_jwt() -> str:
    settings = get_settings()
    private_key = settings.apns_private_key_path.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": settings.apns_team_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=45)).timestamp()),
        },
        private_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )
