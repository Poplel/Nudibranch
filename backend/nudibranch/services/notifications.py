"""
APNS notification delivery.

Two delivery modes:
  - Direct: Nudibranch holds Apple Developer credentials and calls APNS itself.
  - Proxy:  Nudibranch relays through a NudibranchProxy server which holds the Apple
            credentials, so the end-user needs no Apple Developer account.

Proxy mode uses the App Attest per-pairing grant model (see docs/apns-proxy-auth.md):
this server has an Ed25519 identity keypair; the iOS app authorises this server (by its
public key + instance_id) with the proxy via App Attest and hands back an opaque grant
token, stored per device in MobileDevice.proxy_grant.  To push, this server signs each
request with its private key; the proxy verifies the signature against the grant-bound
public key.  There is no shared secret — a stolen grant is useless without this key.
"""

import base64
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.exc import OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import AppSetting, MobileDevice, Notification, Permission, User


def _get_or_create_instance_id(session: Session) -> str:
    """Return this instance's stable ID, creating it on first call."""
    row = session.get(AppSetting, "proxy_instance_id")
    if row is None:
        row = AppSetting(key="proxy_instance_id", value=secrets.token_hex(16))
        session.add(row)
        session.flush()
    return row.value


def get_or_create_signing_key(session: Session) -> Ed25519PrivateKey:
    """Return this server's Ed25519 push-signing private key, creating it on first call."""
    row = session.get(AppSetting, "proxy_signing_private_key")
    if row is None:
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        row = AppSetting(key="proxy_signing_private_key", value=pem)
        session.add(row)
        session.flush()
        return key
    return serialization.load_pem_private_key(row.value.encode(), password=None)


def signing_public_key_pem(session: Session) -> str:
    """PEM of this server's push-signing public key (handed to the app at pairing)."""
    key = get_or_create_signing_key(session)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def push_identity(session: Session) -> dict:
    """Identity the iOS app needs to authorise this server with the proxy."""
    settings = get_settings()
    return {
        "instance_id": _get_or_create_instance_id(session),
        "public_key": signing_public_key_pem(session),
        "proxy_url": settings.apns_proxy_url,
    }


def _canonical_push_message(
    *,
    grant_token: str,
    timestamp: int,
    nonce: str,
    event_type: str,
    title: str,
    body: str,
    target_url: str | None,
    notification_id: str | None,
) -> bytes:
    """Must byte-for-byte match the proxy's grants.push_message()."""
    return json.dumps(
        {
            "grant_token": grant_token,
            "timestamp": timestamp,
            "nonce": nonce,
            "event_type": event_type,
            "title": title,
            "body": body,
            "target_url": target_url,
            "notification_id": notification_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


# Broadcast notifications route to the people who own the menu/flow the notification
# is about (decided by its target_url's first path segment), plus admins. A notification
# with no/unknown target goes to admins only.
_NOTIFICATION_AUDIENCE: dict[str, Permission] = {
    "activity": Permission.activity_read,
    "task-queue": Permission.approvals_manage,
    "downloads": Permission.approvals_manage,
    "tools": Permission.tools_manage,
    "library": Permission.library_view,
    "automations": Permission.automations_manage,
    "wishlist": Permission.discover,
}


def _audience_permission(target_url: str | None) -> Permission | None:
    if not target_url:
        return None
    segment = target_url.split("?", 1)[0].strip("/").split("/", 1)[0]
    return _NOTIFICATION_AUDIENCE.get(segment)


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
        audience = _audience_permission(target_url)
        users = list(session.scalars(select(User)))
        target_user_ids = [
            user.id
            for user in users
            if user.is_admin
            or (audience is not None and any(permission.permission == audience for permission in user.permissions))
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
        return None
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
    signing_key = get_or_create_signing_key(session)
    delivered = 0
    base = proxy_url.rstrip("/")
    rate_limited = False

    async with httpx.AsyncClient(timeout=15) as client:
        for notification in pending:
            if rate_limited:
                break
            # Proxy mode targets only paired (grant-bearing) devices.
            target_devices = [
                d for d in devices
                if d.enabled and d.proxy_grant and notification.user_id in (None, d.user_id)
            ]
            notification_delivered = False
            for device in target_devices:
                timestamp = int(time.time())
                nonce = secrets.token_hex(16)
                message = _canonical_push_message(
                    grant_token=device.proxy_grant,
                    timestamp=timestamp,
                    nonce=nonce,
                    event_type=notification.event_type,
                    title=notification.title,
                    body=notification.body,
                    target_url=notification.target_url,
                    notification_id=notification.id,
                )
                signature = base64.b64encode(signing_key.sign(message)).decode()
                try:
                    response = await client.post(
                        f"{base}/push",
                        json={
                            "grant_token": device.proxy_grant,
                            "timestamp": timestamp,
                            "nonce": nonce,
                            "event_type": notification.event_type,
                            "title": notification.title,
                            "body": notification.body,
                            "target_url": notification.target_url,
                            "notification_id": notification.id,
                            "signature": signature,
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
                        rate_limited = True
                        break
                    elif response.status_code == 401:
                        # Grant revoked/invalid (e.g. user unpaired) — stop using this device.
                        device.enabled = False
                except httpx.HTTPError:
                    continue
            all_devices_gone = bool(target_devices) and all(not d.enabled for d in target_devices)
            if notification_delivered or all_devices_gone:
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
            target_devices = [d for d in devices if d.enabled and notification.user_id in (None, d.user_id)]
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
                        continue
                    response.raise_for_status()
                    notification_delivered = True
                    delivered += 1
                except httpx.HTTPError:
                    continue
            all_devices_gone = bool(target_devices) and all(not d.enabled for d in target_devices)
            if notification_delivered or all_devices_gone:
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
