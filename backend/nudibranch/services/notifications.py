from datetime import datetime, timedelta, timezone
import time

import httpx
import jwt
from sqlalchemy.exc import OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import MobileDevice, Notification, Permission, User


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
    if not all([settings.apns_team_id, settings.apns_key_id, settings.apns_bundle_id]):
        return 0
    if not settings.apns_private_key_path.exists():
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

    query = select(MobileDevice).where(MobileDevice.enabled.is_(True))
    devices = list(session.scalars(query))
    if not devices:
        return 0

    token = _build_apns_jwt()
    host = "https://api.sandbox.push.apple.com" if settings.apns_use_sandbox else "https://api.push.apple.com"
    delivered = 0

    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        for notification in pending:
            target_devices = [device for device in devices if notification.user_id in (None, device.user_id)]
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
