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
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import AppSetting, MobileDevice, Notification, NotificationStatus, Permission, User

logger = logging.getLogger("nudibranch.notifications")


# How long a notification with no eligible device keeps retrying before it is marked
# delivered (terminal). A push created seconds before the device finishes registering —
# enabling notifications races the first delivery cycle — would otherwise be silently and
# permanently dropped; a fully-muted or unregistered audience still goes terminal, just
# after this window instead of immediately.
_NO_DEVICE_GRACE = timedelta(minutes=15)


def _past_no_device_grace(notification: "Notification") -> bool:
    created = notification.created_at
    if created is None:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > _NO_DEVICE_GRACE


def _proxy_error_detail(response: "httpx.Response") -> str:
    """Best-effort extraction of the proxy's error reason (FastAPI `detail`, else body)."""
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"])
    except Exception:  # noqa: BLE001 - non-JSON body
        pass
    return (response.text or "").strip()[:200]


def _get_or_create_instance_id(session: Session) -> str:
    """Return this instance's stable ID, creating it on first call.

    ⚠ Must COMMIT on creation, not just flush: this is served (uncommitted) by the
    read-only GET /notifications/push-identity route, whose session is never committed —
    a flush-only row is rolled back, so the app would pair against an identity the server
    then forgets. See get_or_create_signing_key for why that breaks push signatures."""
    row = session.get(AppSetting, "proxy_instance_id")
    if row is not None:
        return row.value
    value = secrets.token_hex(16)
    session.add(AppSetting(key="proxy_instance_id", value=value))
    try:
        session.commit()
    except IntegrityError:  # concurrent creator won the race — reuse theirs.
        session.rollback()
        existing = session.get(AppSetting, "proxy_instance_id")
        return existing.value if existing else value
    return value


def get_or_create_signing_key(session: Session) -> Ed25519PrivateKey:
    """Return this server's Ed25519 push-signing private key, creating it on first call.

    ⚠ Must COMMIT on creation. This key is handed to the iOS app (as its public half) by
    the read-only GET /notifications/push-identity route, which never commits its session.
    A flush-only key is rolled back, so the app stores a public key in its proxy grant that
    this server no longer holds; the worker later creates+commits a DIFFERENT key and signs
    with it, and the proxy rejects every push as `signature invalid` (401). Committing here
    makes the served identity durable so the grant's key matches what the worker signs."""
    row = session.get(AppSetting, "proxy_signing_private_key")
    if row is not None:
        return serialization.load_pem_private_key(row.value.encode(), password=None)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    session.add(AppSetting(key="proxy_signing_private_key", value=pem))
    try:
        session.commit()
    except IntegrityError:  # concurrent creator won the race — reuse theirs.
        session.rollback()
        existing = session.get(AppSetting, "proxy_signing_private_key")
        if existing is not None:
            return serialization.load_pem_private_key(existing.value.encode(), password=None)
    return key


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
    "podcasts": Permission.podcasts_manage,
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
    deliver_web: bool = True,
    device_id: str | None = None,
    group_key: str | None = None,
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
                    deliver_web=deliver_web,
                    device_id=device_id,
                    group_key=group_key,
                )
            return created
        return None
    for attempt in range(3):
        notification = None
        if group_key:
            notification = session.scalar(
                select(Notification)
                .where(Notification.user_id == user_id, Notification.group_key == group_key)
                .order_by(Notification.created_at.desc())
                .limit(1)
            )
        if notification is None:
            notification = Notification(
                user_id=user_id,
                title=title,
                body=body,
                event_type=event_type,
                target_url=target_url,
                deliver_apns=deliver_apns,
                deliver_web=deliver_web,
                device_id=device_id,
                group_key=group_key,
            )
            session.add(notification)
        else:
            notification.title = title
            notification.body = body
            notification.event_type = event_type
            notification.target_url = target_url
            notification.deliver_apns = deliver_apns
            notification.deliver_web = deliver_web
            notification.device_id = device_id
            notification.status = NotificationStatus.unread
            notification.created_at = datetime.now(timezone.utc)
            if deliver_apns:
                notification.apns_delivered_at = None
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

    if not settings.apns_proxy_url:
        logger.warning("push: APNS_ENABLED is set but APNS_PROXY_URL is empty — nothing can be delivered")
        return 0
    return await _deliver_via_proxy(session, pending, devices, settings.apns_proxy_url)


# Proxy PushRequest field limits (NudibranchProxy/proxy/schemas.py). A push alert must be short;
# bodies over these caps are rejected 422 by the proxy's schema validation. We truncate to fit
# BEFORE signing (the signature covers the sent values) so long diagnostic bodies still deliver.
_PUSH_TITLE_MAX = 128
_PUSH_BODY_MAX = 256
_PUSH_TARGET_MAX = 256


def _fit(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


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
            # A device-scoped notification (device_id set) pushes only to that one device.
            # `mutes` applies the device's own per-category opt-outs: iOS cannot decline a push
            # that arrives while the app is backgrounded, so the filter has to live here.
            target_devices = [
                d for d in devices
                if d.enabled and d.proxy_grant and notification.user_id in (None, d.user_id)
                and (notification.device_id is None or d.id == notification.device_id)
                and not d.mutes(notification.event_type)
            ]
            if notification.device_id is not None and not target_devices:
                # A target can be a web session rather than an APNS registration, or it may have
                # been revoked. Either way this scoped nudge must not remain pending forever.
                notification.apns_delivered_at = datetime.now(timezone.utc)
                continue
            # Truncate to the proxy's field limits once per notification (same values signed + sent).
            push_title = _fit(notification.title, _PUSH_TITLE_MAX) or " "
            push_body = _fit(notification.body, _PUSH_BODY_MAX) or " "
            push_target = _fit(notification.target_url, _PUSH_TARGET_MAX)
            notification_delivered = False
            notification_terminal = False
            for device in target_devices:
                timestamp = int(time.time())
                nonce = secrets.token_hex(16)
                message = _canonical_push_message(
                    grant_token=device.proxy_grant,
                    timestamp=timestamp,
                    nonce=nonce,
                    event_type=notification.event_type,
                    title=push_title,
                    body=push_body,
                    target_url=push_target,
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
                            "title": push_title,
                            "body": push_body,
                            "target_url": push_target,
                            "notification_id": notification.id,
                            "signature": signature,
                        },
                    )
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("message") == "device_token_gone":
                            logger.warning(
                                "proxy push: device token gone (event=%s device=%s) — disabling",
                                notification.event_type, device.id,
                            )
                            device.enabled = False
                        else:
                            notification_delivered = True
                            delivered += 1
                    elif response.status_code == 429:
                        logger.warning("proxy push: rate limited (event=%s)", notification.event_type)
                        rate_limited = True
                        break
                    elif response.status_code == 401:
                        # Grant revoked/invalid (e.g. user unpaired, or a stale signing key) —
                        # stop using this device. Detail carries the proxy's reason.
                        logger.warning(
                            "proxy push rejected 401 (event=%s device=%s): %s — disabling device",
                            notification.event_type, device.id, _proxy_error_detail(response),
                        )
                        device.enabled = False
                    elif response.status_code in (400, 422):
                        # Validation rejection (bad event_type, field too long, …) — permanent for
                        # THIS notification. Previously swallowed → the notification stayed pending
                        # and re-hit the proxy every worker loop forever (a retry storm that also
                        # burned the proxy rate limit). Mark it terminal so it's given up, not retried.
                        logger.warning(
                            "proxy push rejected %s (event=%s device=%s) — giving up: %s",
                            response.status_code, notification.event_type, device.id,
                            _proxy_error_detail(response),
                        )
                        notification_terminal = True
                    else:
                        # Transient/server-side (502 apns failure, 503, …): log and let it retry.
                        logger.warning(
                            "proxy push failed %s (event=%s device=%s): %s",
                            response.status_code, notification.event_type, device.id,
                            _proxy_error_detail(response),
                        )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "proxy push transport error (event=%s device=%s): %s",
                        notification.event_type, device.id, exc,
                    )
                    continue
            all_devices_gone = bool(target_devices) and all(not d.enabled for d in target_devices)
            # No eligible device is terminal, not "retry later": with per-device category mutes,
            # every registered device can legitimately decline a notification, and leaving it
            # pending would re-evaluate it on every delivery cycle forever. But only after a
            # grace window — a device mid-registration when the notification was created should
            # get it on a later cycle, not lose it silently.
            if notification_delivered or all_devices_gone or notification_terminal:
                notification.apns_delivered_at = datetime.now(timezone.utc)
            elif not target_devices and _past_no_device_grace(notification):
                logger.info(
                    "push: no eligible device for notification %s (event=%s) — giving up",
                    notification.id, notification.event_type,
                )
                notification.apns_delivered_at = datetime.now(timezone.utc)

    session.commit()
    return delivered


# The direct APNS (`.p8`) delivery path was removed. Push goes through the App Attest proxy only.
#
# Rationale, so it isn't reintroduced: the proxy IS the design (the app proves it is the genuine
# published build via App Attest, and self-hosters relay through the hosted proxy without needing
# an Apple account of their own). The direct path existed as a fallback and only ever worked for
# someone holding this app's own signing credentials — which is nobody but its author, since the
# bundle id and the `.p8` are inseparable. It also drifted: it never applied
# `_BACKGROUND_EVENT_TYPES`, so `remote_playback_command` went out as a plain alert with no
# `content-available` and could not wake a closed app at all, making scheduled and webhook playback
# silently proxy-only. Two payload builders for one contract is what let that happen.
#
# `_BACKGROUND_EVENT_TYPES` and the wake set now live in exactly one place: the proxy
# (`NudibranchProxy/proxy/`), which owns every payload decision. Keep the server's
# `ALLOWED_EVENT_TYPES` contribution in step with it, per §20.
