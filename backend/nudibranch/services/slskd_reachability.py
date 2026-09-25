"""Soulseek listen-port reachability check ("self-probe + identity").

Confirms slskd's own NAT/port-forward path is working, end to end, using only the configured
`slskd_url` + API key -- never docker networking or host assumptions (slskd need not run in the
same stack as Nudibranch). See CLAUDE-wip-slskd-and-sessions.md section 1 for the design.

Every step below is a call slskd makes to ITSELF, so a pass proves slskd's own listen port is
reachable from the outside -- not merely that Nudibranch can reach slskd's HTTP API.

1. `GET /api/v0/application` -- the Soulseek server connection must be "Connected"+"LoggedIn".
2. `GET /api/v0/options` -- the configured `soulseek.listenPort`.
3. `GET /api/v0/users/{self}/endpoint` -- the address:port the Soulseek server sees for us. The
   port must equal the configured listen port, or the forward points at the wrong port.
4. `GET /api/v0/users/{self}/info` -- makes slskd dial its OWN public address:port and complete a
   peer handshake. A non-200 here means nothing is answering the Soulseek protocol behind the
   forward (or the router has no NAT loopback -- see the note below).
5. `GET /api/v0/users/{self}/browse`, compared against slskd's own share summary
   (`application.shares`) -- confirms the forward lands on THIS slskd and not some other Soulseek
   client that happens to sit behind the same public address (verified live: a stock user-info
   response is identical for every slskd, so step 4 alone cannot tell them apart).

Hairpin note: steps 4 and 5 need the router to support NAT loopback (reaching your own public IP
from inside the network). If step 4 fails but step 3 succeeded, the plain-language detail says the
port may simply not be reachable, or the router may not support loopback -- either the listen port
truly isn't reachable, or it's fine and the router just can't test it from behind itself.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from nudibranch.db.models import AppSetting

# Fast steps (1-4) get a short, bounded timeout so a dead slskd can't hang the caller.
_FAST_TIMEOUT_SECONDS = 10.0
# Step 4 makes slskd perform a live peer handshake over the Soulseek network, which is slower
# than an ordinary API round trip; verified live at ~1s, but give it real headroom.
_REACHABILITY_TIMEOUT_SECONDS = 30.0
# Step 5 asks slskd to serialize its whole share tree. Verified live in well under a second for a
# small library, but a large one can be slow -- bound it generously rather than fail the whole
# check over a slow browse. This (not the fast steps) is why the check runs on the worker instead
# of inline on the HTTP request path: it alone can take up to this long.
_BROWSE_TIMEOUT_SECONDS = 90.0

_RESULT_SETTING_KEY = "slskd_port_check_result"
_MIN_AUTO_CHECK_INTERVAL = timedelta(minutes=30)


def _step(key: str, label: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "ok": ok, "detail": detail}


def _finalize(steps: list[dict[str, Any]], public_address: str | None, port: int | None) -> dict[str, Any]:
    ok_values = [step["ok"] for step in steps]
    if any(value is False for value in ok_values):
        status = "failed"
    elif any(value is None for value in ok_values):
        status = "warning"
    else:
        status = "ok"
    return {
        "ok": status == "ok",
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "public_address": public_address,
        "port": port,
        "steps": steps,
    }


def run_slskd_reachability_check(slskd_url: str, api_key: str) -> dict[str, Any]:
    """Run the 5-step self-probe against a live slskd. Never raises -- every failure mode is
    reported as a `False`/`None` step so the caller always gets a displayable result."""
    if not slskd_url or not api_key:
        return _finalize(
            [_step("config", "Soulseek configured", False, "slskd URL and API key are not configured in Settings.")],
            None,
            None,
        )

    steps: list[dict[str, Any]] = []
    public_address: str | None = None
    endpoint_port: int | None = None
    listen_port: int | None = None

    with httpx.Client(base_url=slskd_url.rstrip("/"), headers={"X-API-Key": api_key}, timeout=_FAST_TIMEOUT_SECONDS) as client:
        # Step 1: connected + logged in, and who we are.
        try:
            response = client.get("/api/v0/application")
            response.raise_for_status()
            application = response.json()
            server = application.get("server") or {}
            state = str(server.get("state") or "")
            username = ((application.get("user") or {}).get("username")) or None
            shares = application.get("shares") or {}
            ok = "Connected" in state and "LoggedIn" in state and bool(username)
            steps.append(_step(
                "login",
                "Connected to the Soulseek network",
                ok,
                f"slskd is connected and logged in as {username}." if ok
                else f"slskd is not connected/logged in to Soulseek (state: '{state or 'unknown'}').",
            ))
            if not ok:
                return _finalize(steps, public_address, endpoint_port)
        except Exception as error:  # noqa: BLE001 - report every failure mode as a step, never raise.
            steps.append(_step("login", "Connected to the Soulseek network", False, f"Could not reach slskd: {error}"))
            return _finalize(steps, public_address, endpoint_port)

        # Step 2: the configured listen port.
        try:
            response = client.get("/api/v0/options")
            response.raise_for_status()
            options = response.json()
            listen_port = (options.get("soulseek") or {}).get("listenPort")
            ok = bool(listen_port)
            steps.append(_step(
                "listen_port",
                "Listen port configured",
                ok,
                f"slskd is configured to listen on port {listen_port}." if ok
                else "slskd has no Soulseek listen port configured.",
            ))
            if not ok:
                return _finalize(steps, public_address, endpoint_port)
        except Exception as error:  # noqa: BLE001
            steps.append(_step("listen_port", "Listen port configured", False, f"Could not read slskd's options: {error}"))
            return _finalize(steps, public_address, endpoint_port)

        # Step 3: the port the Soulseek server itself sees for us.
        try:
            response = client.get(f"/api/v0/users/{quote(username, safe='')}/endpoint")
            response.raise_for_status()
            endpoint = response.json()
            public_address = endpoint.get("address")
            endpoint_port = endpoint.get("port")
            ok = endpoint_port is not None and endpoint_port == listen_port
            steps.append(_step(
                "advertised_port",
                "Advertised port matches",
                ok,
                f"The Soulseek server sees {public_address}:{endpoint_port}, matching the configured listen port." if ok
                else f"The Soulseek server sees port {endpoint_port}, but slskd is configured to listen on {listen_port} -- the port forward may point at the wrong port.",
            ))
            if not ok:
                return _finalize(steps, public_address, endpoint_port)
        except Exception as error:  # noqa: BLE001
            steps.append(_step("advertised_port", "Advertised port matches", False, f"Could not read slskd's advertised endpoint: {error}"))
            return _finalize(steps, public_address, endpoint_port)

        # Step 4: reachability -- slskd dials its own public address:port.
        reachable = False
        try:
            response = client.get(f"/api/v0/users/{quote(username, safe='')}/info", timeout=_REACHABILITY_TIMEOUT_SECONDS)
            reachable = response.status_code == 200
            steps.append(_step(
                "reachable",
                "Listen port reachable",
                reachable,
                "slskd could dial its own public address and complete a Soulseek peer handshake." if reachable
                else f"slskd could not reach its own public address on port {listen_port} (HTTP {response.status_code}). Check the port forward, or whether the router supports NAT loopback.",
            ))
        except Exception as error:  # noqa: BLE001
            steps.append(_step(
                "reachable",
                "Listen port reachable",
                False,
                f"slskd could not reach its own public address on port {listen_port}: {error}. Check the port forward, or whether the router supports NAT loopback.",
            ))
        if not reachable:
            return _finalize(steps, public_address, endpoint_port)

        # Step 5: identity -- does the forward land on THIS slskd?
        try:
            response = client.get(f"/api/v0/users/{quote(username, safe='')}/browse", timeout=_BROWSE_TIMEOUT_SECONDS)
            response.raise_for_status()
            browse = response.json()
            directories = browse.get("directories") or []
            browse_files = sum(len(directory.get("files") or []) for directory in directories)
            browse_dirs = browse.get("directoryCount")
            if browse_dirs is None:
                browse_dirs = len(directories)
            share_files = shares.get("files")
            share_dirs = shares.get("directories")
            files_match = share_files is not None and browse_files == share_files
            # Directory counts can be off by a folder or two even for the correct slskd -- e.g. a
            # share root with no files of its own directly inside it shows up in a browse listing
            # but not in slskd's own share summary (verified live on sandalphon: 16 vs 15 for the
            # SAME slskd). File counts are the reliable signal; directories are corroborating,
            # not decisive on their own.
            dirs_match = share_dirs is None or abs(browse_dirs - share_dirs) <= 1
            identity_ok = files_match and dirs_match
            detail = (
                f"Browsing our own share reports {browse_files} file(s) in {browse_dirs} folder(s); "
                f"slskd's own share summary reports {share_files} file(s) in {share_dirs} folder(s)."
            )
            detail += " These match -- the port forward reaches this slskd." if identity_ok \
                else " These don't match -- the port forward may reach a different Soulseek client."
            steps.append(_step("identity", "Port forward reaches this slskd", identity_ok, detail))
        except httpx.TimeoutException:
            steps.append(_step(
                "identity",
                "Port forward reaches this slskd",
                None,
                "Reachable, but browsing our own share timed out -- identity could not be confirmed.",
            ))
        except Exception as error:  # noqa: BLE001
            steps.append(_step(
                "identity",
                "Port forward reaches this slskd",
                None,
                f"Reachable, but identity could not be confirmed: {error}",
            ))

    return _finalize(steps, public_address, endpoint_port)


def load_last_slskd_check(session: Session) -> dict[str, Any] | None:
    row = session.get(AppSetting, _RESULT_SETTING_KEY)
    if not row or not row.value:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return None


def store_slskd_check_result(session: Session, result: dict[str, Any]) -> None:
    row = session.get(AppSetting, _RESULT_SETTING_KEY)
    value = json.dumps(result)
    if row is None:
        session.add(AppSetting(key=_RESULT_SETTING_KEY, value=value))
    else:
        row.value = value
    session.commit()


def should_run_download_failure_check(session: Session) -> bool:
    """Rate-limit the download-failure auto-trigger to once per 30 min (manual "Check now" from
    Settings is never throttled -- only this automatic path)."""
    last = load_last_slskd_check(session)
    checked_at_raw = last.get("checked_at") if last else None
    if not checked_at_raw:
        return True
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return True
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - checked_at) >= _MIN_AUTO_CHECK_INTERVAL
