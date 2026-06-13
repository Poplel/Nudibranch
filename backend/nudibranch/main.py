from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from nudibranch import __version__
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
    {"name": "system", "description": "Health-check endpoint."},
]

app = FastAPI(
    title="Nudibranch API",
    version=__version__,
    description="REST API for Nudibranch, a self-hosted Jellyfin music companion. Handles library management, music discovery, downloads via slskd/YouTube, playlist sync with Jellyfin, and multi-user access control.",
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
    openapi_tags=_TAGS_METADATA,
)

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
        settings.trash_path,
        settings.backups_path,
        settings.config_path,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        init_db(session)
    write_app_log(f"API started (version {__version__})")


@app.get("/healthz", tags=["system"])
def healthz() -> dict:
    return {"ok": True, "version": __version__}

