from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from sqlalchemy.orm import Session

from nudibranch import __version__
from nudibranch.api.deps import get_current_user
from nudibranch.api.routes import router
from nudibranch.core.config import get_settings
from nudibranch.db.init import init_db
from nudibranch.db.session import SessionLocal
from nudibranch.services.app_log import write_app_log

settings = get_settings()

_TAGS_METADATA = [
    {"name": "auth", "description": "Username + password authentication issuing sliding-expiry session tokens, plus session and named static API-key management. A session token or static API key is sent as a Bearer token (or `api_key` query parameter on media endpoints)."},
    {"name": "users", "description": "User accounts, permissions, appearance and search preferences, real-time player state, and remote playback commands (play/pause/next with loop and shuffle)."},
    {"name": "library", "description": "Browse the music library tree, update metadata, propose removals, and stream audio files or album art."},
    {"name": "imports", "description": "Scan the staging directory for new audio files, look up album/track metadata from MusicBrainz, and enqueue import proposals."},
    {"name": "discover", "description": "Search for music via iTunes, retrieve album tracks, fetch cached album art, and add downloads to the task queue."},
    {"name": "wishlist", "description": "Manage the download wishlist. Users can add items; admins can approve or deny them, which creates download tasks."},
    {"name": "playlists", "description": "Create and manage playlists. The protected Favorites playlist syncs with Jellyfin's native IsFavorite flag; other playlists sync as Jellyfin playlists."},
    {"name": "approvals", "description": "Review proposal batches (metadata edits, file removals, playlist changes). Approve or reject individual items."},
    {"name": "tasks", "description": "Inspect the background task queue and application log."},
    {"name": "tools", "description": "Administrative one-shot tools: library/file health checks, volume normalisation, duplicate detection, backups and restore."},
    {"name": "settings", "description": "Read and update integration settings (Jellyfin, slskd, YouTube cookies)."},
    {"name": "notifications", "description": "In-app and APNS push notifications. Register devices, mark notifications read, dismiss all."},
    {"name": "automations", "description": "Trigger → Action → Notify automations: run a maintenance tool or play music on a schedule (cron/interval), an inbound webhook (IFTTT-style; the token is the credential), or an in-app event."},
    {"name": "podcasts", "description": "Podcast subscriptions: subscribe by directory search or direct RSS feed, scan feeds for new episodes, record per-user listening progress, and set per-podcast new-episode notification preferences. Episode audio is never stored here — clients play the publisher's enclosure_url directly, and the stream route relays it for browsers, which cannot use a cross-origin URL in the web player's audio graph."},
    {"name": "system", "description": "Health-check endpoint."},
]

app = FastAPI(
    title="Nudibranch API",
    version=__version__,
    description=(
        "REST API for Nudibranch, a self-hosted music library manager and player.\n\n"
        "Covers library browsing and metadata management, music discovery and downloading via "
        "slskd and yt-dlp, playlists, podcasts, background maintenance tools, trigger-action "
        "automations, push notifications, and remote playback control. Jellyfin integration is "
        "supported but optional; playlists and favourites fall back to a native database backend "
        "when no Jellyfin server is configured.\n\n"
        "**Authentication.** Every endpoint except `GET /healthz`, `POST /api/v1/auth/login`, and "
        "the public automation webhook requires a bearer credential, sent as "
        "`Authorization: Bearer <token>`. The token may be a session token obtained from "
        "`POST /api/v1/auth/login`, or a static API key created through `/api/v1/me/api-keys`. "
        "Media endpoints that cannot set headers additionally accept the credential as an "
        "`api_key` query parameter.\n\n"
        "**Authorization.** Most endpoints further require one of thirteen permissions, and return "
        "`403 Forbidden` when the caller lacks it. Accounts flagged `is_admin` bypass every "
        "permission check. `GET /api/v1/permissions` returns the full catalogue."
    ),
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
    openapi_tags=_TAGS_METADATA,
    contact={"name": "Nudibranch", "url": "https://github.com/Poplel/Nudibranchserver"},
    license_info={"name": "MIT"},
)


# --- OpenAPI enrichment -------------------------------------------------------------------
# The access requirement of an endpoint is enforced by a FastAPI dependency, which by default
# leaves no trace in the generated schema: a consumer could only discover the permission a call
# needs by provoking a 403. The routes are also uniform enough that declaring 401/403/404 on each
# of them by hand would be ~350 near-identical edits that would then drift. Both are therefore
# derived here, once, by walking each route's dependency tree.

_MEDIA_API_KEY_DESCRIPTION = (
    "Credential for clients that cannot set request headers, such as an audio element or an image "
    "view. Accepts the same session token or static API key as the `Authorization` header. Treat "
    "URLs containing it as secrets: they appear in proxy logs, browser history, and referrer "
    "headers."
)


def _iter_api_routes():
    """Yield every route that carries a dependency tree.

    The API router is iterated directly rather than through ``app.routes``, because what
    ``include_router`` leaves in ``app.routes`` differs by FastAPI version: some flatten the
    included routes in, others keep them behind a private wrapper. Reading the router itself is
    stable across both, and a wrong choice here fails silently — the schema still builds, merely
    without any of the enrichment below.
    """
    seen: set[int] = set()
    for collection in (router.routes, app.routes):
        for route in collection:
            if getattr(route, "dependant", None) is not None and id(route) not in seen:
                seen.add(id(route))
                yield route


def _access_requirement(dependant) -> str | None:
    """Return the access requirement a route enforces: a permission value, 'admin', or None."""
    call = getattr(dependant, "call", None)
    permission = getattr(call, "__nudibranch_permission__", None)
    if permission is not None:
        return getattr(permission, "value", str(permission))
    if getattr(call, "__nudibranch_admin_only__", False):
        return "admin"
    for sub_dependant in getattr(dependant, "dependencies", []):
        found = _access_requirement(sub_dependant)
        if found is not None:
            return found
    return None


def _requires_authentication(dependant) -> bool:
    if getattr(dependant, "call", None) is get_current_user:
        return True
    return any(_requires_authentication(sub) for sub in getattr(dependant, "dependencies", []))


def _describe_access(operation: dict, requirement: str | None) -> None:
    if requirement == "admin":
        note = "**Requires administrator access.**"
    elif requirement:
        note = (
            f"**Requires the `{requirement}` permission.** "
            "Administrators bypass every permission check."
        )
    else:
        return
    existing = (operation.get("description") or "").rstrip()
    operation["description"] = f"{existing}\n\n{note}".strip() if existing else note


def custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=_TAGS_METADATA,
        contact=app.contact,
        license_info=app.license_info,
    )

    for route in _iter_api_routes():
        path = getattr(route, "path", None)
        dependant = getattr(route, "dependant", None)
        if not path or dependant is None or path not in schema.get("paths", {}):
            continue

        requirement = _access_requirement(dependant)
        authenticated = _requires_authentication(dependant)

        for method in getattr(route, "methods", set()) or set():
            operation = schema["paths"][path].get(method.lower())
            if not operation:
                continue

            responses = operation.setdefault("responses", {})
            if authenticated:
                responses.setdefault(
                    "401",
                    {"description": "The credential is missing, expired, or invalid."},
                )
            if requirement == "admin":
                responses.setdefault("403", {"description": "Administrator access is required."})
            elif requirement:
                responses.setdefault(
                    "403",
                    {"description": f"The `{requirement}` permission is required."},
                )
            if "{" in path:
                responses.setdefault("404", {"description": "The resource does not exist."})

            _describe_access(operation, requirement)

            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "api_key" and parameter.get("in") == "query":
                    parameter.setdefault("description", _MEDIA_API_KEY_DESCRIPTION)

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    for path in [
        settings.import_path,
        settings.staging_path,
        settings.library_path,
        settings.downloads_path,
        settings.podcasts_path,
        settings.backups_path,
        settings.config_path,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        init_db(session)
    write_app_log(f"API started (version {__version__})")


@app.get(
    "/healthz",
    tags=["system"],
    summary="Health check",
    description=(
        "Liveness probe. Requires no authentication and is served at the server root, outside the "
        "`/api/v1` prefix. Returns `{\"ok\": true, \"version\": \"<version>\"}`."
    ),
    response_model=dict,
)
def healthz() -> dict:
    return {"ok": True, "version": __version__}

