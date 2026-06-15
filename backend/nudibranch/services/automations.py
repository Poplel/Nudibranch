"""Automation engine — Trigger > Action > Notify.

An automation runs an **action** (run a maintenance tool, play something, or send a
media control) when its **trigger** fires (a time/interval schedule, an inbound webhook,
or an in-app event), then **notifies** per its configured mode. Every run is ALWAYS
recorded to the Activity log regardless of notify mode; `notify_mode` only governs the
extra in-app/push notification.

`run_automation` is the single entry point used by all firing paths: the run-now route,
the webhook route, the worker scheduler tick, and (later) event hooks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nudibranch.db.models import Album, Artist, Automation, PlaybackCommand, Playlist, Track, User
from nudibranch.services.app_log import write_app_log
from nudibranch.services.notifications import create_notification
from nudibranch.services.search import search_library
from nudibranch.services.tasks import enqueue_task

# Maintenance-tool slug -> worker task type (every tool tasks with an empty payload).
TOOL_TASK_TYPES = {
    "jellyfin-scan": "jellyfin_scan",
    "remap-tracks": "sync_favorites_jellyfin",
    "check-files": "check_files",
    "check-duplicates": "check_duplicates",
    "check-lyrics": "check_lyrics",
    "check-musicbrainz-ids": "check_musicbrainz_ids",
    "check-audio-content": "check_audio_content",
    "check-album-covers": "check_album_covers",
    "check-missing-tracks": "check_missing_tracks",
    "check-non-lossless": "check_non_lossless",
    "normalize-volume": "normalize_volume",
    "consolidate-folders": "consolidate_folders",
    "clear-downloads": "clear_downloads",
    "backup": "backup_now",
}

MEDIA_CONTROLS = {"pause", "resume", "next", "previous", "stop"}
LOOP_MODES = {"off", "one", "all"}
TRIGGER_TYPES = {"time", "interval", "webhook", "event"}
ACTION_TYPES = {"tool", "play", "media_control"}
NOTIFY_MODES = {"log", "notification", "both"}
NOTIFY_PRIORITIES = {"low", "normal", "high"}
EVENT_TYPES = {"download_complete", "wishlist_match", "scan_complete"}
# WRatio can score unrelated titles ~0.56 (substring overlap); real matches land 0.72+,
# so require a confident 0.65 to resolve a play target from a free-text query.
COMMAND_MATCH_FLOOR = 0.65


# --- scheduling ----------------------------------------------------------

def compute_next_run(trigger_type: str, trigger_config: dict, after: datetime | None = None) -> datetime | None:
    """Next fire time for a schedule trigger. None for webhook/event (no schedule).

    Computed from `after` (now by default), so missed runs while the server was down are
    skipped rather than backfilled.
    """
    now = after or datetime.now(timezone.utc)
    if trigger_type == "interval":
        seconds = int(trigger_config.get("seconds") or 0)
        return now + timedelta(seconds=seconds) if seconds > 0 else None
    if trigger_type == "time":
        cron = trigger_config.get("cron")
        if not cron:
            return None
        try:
            return croniter(cron, now).get_next(datetime)
        except Exception:
            return None
    return None


# --- action execution ----------------------------------------------------

def _nudge_device(session: Session, owner_id: str, body: str) -> None:
    try:
        create_notification(
            session,
            title="Remote playback",
            body=body,
            event_type="remote_playback_command",
            target_url="/player",
            user_id=owner_id,
            deliver_apns=True,
        )
    except Exception:
        pass


def _resolve_label(session: Session, target_type: str | None, target_id: str | None) -> str | None:
    if not target_id:
        return None
    model = {"track": Track, "album": Album, "artist": Artist, "playlist": Playlist}.get(target_type or "")
    if not model:
        return None
    obj = session.get(model, target_id)
    if not obj:
        return None
    return getattr(obj, "title", None) or getattr(obj, "name", None)


def _run_play(session: Session, owner_id: str, cfg: dict) -> str:
    target_type = cfg.get("target_type")
    target_id = cfg.get("target_id")
    target_label = None
    query = (cfg.get("target_query") or "").strip()
    if not target_id and query:
        if target_type == "playlist":
            playlist = session.scalar(
                select(Playlist).where(Playlist.name.ilike(query), or_(Playlist.user_id == owner_id, Playlist.user_id.is_(None)))
            )
            if not playlist:
                raise ValueError(f"No playlist named '{query}'")
            target_id, target_label = playlist.id, playlist.name
        else:
            owner = session.get(User, owner_id)
            floor = max(COMMAND_MATCH_FLOOR, owner.search_min_confidence if owner and owner.search_min_confidence is not None else 0.0)
            matches = search_library(session, query, kinds=[target_type] if target_type else None, min_confidence=floor, limit=1)
            if not matches:
                raise ValueError(f"No library match for '{query}'")
            top = matches[0]
            target_type, target_id, target_label = top["kind"], top["id"], top["name"]
    if not target_id:
        raise ValueError("play action needs a target_id or target_query")
    if not target_label:
        target_label = _resolve_label(session, target_type, target_id)
    command = PlaybackCommand(
        user_id=owner_id,
        device_id=cfg.get("device_id"),
        action="play",
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        loop=cfg.get("loop") if cfg.get("loop") in LOOP_MODES else "off",
        shuffle=bool(cfg.get("shuffle")),
        status="pending",
    )
    session.add(command)
    session.commit()
    _nudge_device(session, owner_id, f"Play {target_label}" if target_label else "Play")
    return f"queued playback: {target_label or target_id}"


def _run_media_control(session: Session, owner_id: str, cfg: dict) -> str:
    control = (cfg.get("control") or "").strip().lower()
    if control not in MEDIA_CONTROLS:
        raise ValueError(f"Unknown media control '{control}'")
    command = PlaybackCommand(user_id=owner_id, device_id=cfg.get("device_id"), action=control, status="pending")
    session.add(command)
    session.commit()
    _nudge_device(session, owner_id, control.capitalize())
    return f"sent media control: {control}"


def _run_tool(session: Session, cfg: dict) -> str:
    slug = cfg.get("action")
    task_type = TOOL_TASK_TYPES.get(slug)
    if not task_type:
        raise ValueError(f"Unknown tool '{slug}'")
    enqueue_task(session, task_type, {})
    return f"queued tool: {slug}"


def execute_action(session: Session, automation: Automation) -> str:
    cfg = json.loads(automation.action_config or "{}")
    if automation.action_type == "tool":
        return _run_tool(session, cfg)
    if automation.action_type == "play":
        return _run_play(session, automation.owner_id, cfg)
    if automation.action_type == "media_control":
        return _run_media_control(session, automation.owner_id, cfg)
    raise ValueError(f"Unknown action type '{automation.action_type}'")


# --- the one firing entry point ------------------------------------------

def run_automation(session: Session, automation: Automation, trigger_source: str = "manual") -> tuple[str, str]:
    """Execute the action, ALWAYS record to Activity, optionally notify. -> (status, message)."""
    try:
        message = execute_action(session, automation)
        status, error = "ok", None
    except Exception as exc:
        status, error, message = "error", str(exc), f"failed: {exc}"

    automation.last_run_at = datetime.now(timezone.utc)
    automation.last_status = status
    automation.last_error = error
    session.commit()

    # Always log to Activity, independent of notify_mode.
    try:
        write_app_log(
            f"Automation '{automation.name}' [{trigger_source}]: {message}",
            "info" if status == "ok" else "error",
            automation_id=automation.id,
        )
    except Exception:
        pass

    if automation.notify_mode in ("notification", "both"):
        try:
            create_notification(
                session,
                title=f"Automation: {automation.name}",
                body=message,
                event_type="automation",
                target_url="/automations",
                user_id=None,  # broadcast automation results to all notification-enabled users
                deliver_apns=True,
            )
        except Exception:
            pass

    return status, message


def run_due_automations(session: Session) -> int:
    """Run every enabled time/interval automation whose next_run_at is due, then reschedule
    from *now* (so a backlog from downtime collapses to a single run — missed runs skipped).
    Called from the worker's idle tick. Returns the number fired.
    """
    now = datetime.now(timezone.utc)
    rows = list(session.scalars(
        select(Automation).where(
            Automation.enabled.is_(True),
            Automation.trigger_type.in_(("time", "interval")),
            Automation.next_run_at.is_not(None),
        )
    ))
    fired = 0
    for automation in rows:
        due = automation.next_run_at
        if due is None:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due > now:
            continue
        run_automation(session, automation, trigger_source="schedule")
        try:
            cfg = json.loads(automation.trigger_config or "{}")
        except json.JSONDecodeError:
            cfg = {}
        automation.next_run_at = compute_next_run(automation.trigger_type, cfg, after=datetime.now(timezone.utc))
        session.commit()
        fired += 1
    return fired


def fire_event_automations(session: Session, event: str) -> int:
    """Run all enabled event-triggered automations matching `event`. Returns count fired."""
    rows = session.scalars(
        select(Automation).where(Automation.enabled.is_(True), Automation.trigger_type == "event")
    )
    fired = 0
    for automation in rows:
        try:
            cfg = json.loads(automation.trigger_config or "{}")
        except json.JSONDecodeError:
            continue
        if cfg.get("event") == event:
            run_automation(session, automation, trigger_source=f"event:{event}")
            fired += 1
    return fired
