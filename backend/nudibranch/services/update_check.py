"""Tell admins when a newer Nudibranch release is out on GitHub.

The worker calls `check_for_update` on boot and then every `UPDATE_CHECK_TICK_SECONDS`. It reads the
latest published release of `UPDATE_CHECK_REPO` and, when that release is newer than this server's
`__version__`, sends one notification whose `target_url` is the release page. Both clients open an
http(s) `target_url` in the browser, so tapping it lands on the release notes.

Notified once per release: the tag already announced is kept in `AppSetting`, so a server left on an
old version is not reminded every tick. A nightly build that is ahead of the latest release (1.3.0
nightly against a 1.2.1 release) is never told to "update" backwards.
"""

from __future__ import annotations

import re

import httpx
from sqlalchemy.orm import Session

from nudibranch import __version__
from nudibranch.core.config import get_settings
from nudibranch.db.models import AppSetting
from nudibranch.services.notifications import create_notification

UPDATE_NOTIFIED_SETTING = "update_notified_version"


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """`v1.2.1` / `1.3.0` / `1.3.0-nightly` -> (1, 2, 1). None when there is no leading number."""
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(value or ""))
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def latest_release(repo: str) -> dict | None:
    """`{"version", "url"}` for the newest published (non-draft, non-prerelease) release."""
    response = httpx.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"Nudibranch/{__version__}"},
        timeout=10,
        follow_redirects=True,
    )
    if response.status_code == 404:
        return None  # no release published yet
    response.raise_for_status()
    data = response.json()
    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or "").strip() or f"https://github.com/{repo}/releases/latest"
    if not tag:
        return None
    return {"version": tag.lstrip("vV"), "url": url}


def fetch_latest_release() -> dict | None:
    """The configured repo's latest release, or None when the check is off. Network only -- the
    worker runs this in a thread so a slow GitHub never stalls its event loop."""
    settings = get_settings()
    if not settings.update_check_enabled or not settings.update_check_repo.strip():
        return None
    return latest_release(settings.update_check_repo.strip())


def check_for_update(session: Session, release: dict | None) -> str | None:
    """Notify admins when `release` is newer. Returns the version announced, else None."""
    if not release:
        return None
    latest, current = parse_version(release["version"]), parse_version(__version__)
    if latest is None or current is None or latest <= current:
        return None
    notified = session.get(AppSetting, UPDATE_NOTIFIED_SETTING)
    if notified and notified.value == release["version"]:
        return None
    # `target_url` is the release page: an external URL resolves to admins only
    # (`_audience_permissions`), and admins are the people who can update the server.
    create_notification(
        session,
        title=f"Nudibranch {release['version']} is available",
        body=f"This server is running {__version__}. Open the release to see what changed and update.",
        event_type="update_available",
        target_url=release["url"],
        group_key="system:update",
    )
    if notified:
        notified.value = release["version"]
    else:
        session.add(AppSetting(key=UPDATE_NOTIFIED_SETTING, value=release["version"]))
    session.commit()
    return release["version"]
