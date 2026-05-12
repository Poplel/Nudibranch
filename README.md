# Nudibranch

Nudibranch is a Docker-first music library manager for Jellyfin and slskd.
It treats its own database as the source of truth, proposes every library change
as a dry run, and only executes approved work.

## Goals

- Import and repair a Jellyfin music library.
- Search slskd for missing or upgraded tracks, with yt-dlp as a fallback.
- Enrich albums with `cover.jpg` and tracks with synced `.lrc` lyrics.
- Preserve existing metadata while proposing improvements for approval.
- Expose every web UI action through a versioned REST API.
- Support a SwiftUI iOS companion app with APNs background notifications.
- Deploy with Docker Compose without host-level dependencies.

## Quick Start

```sh
cp .env.example .env
docker compose up --build
```

## Pulling Published Images

After pushing this project to GitHub, the included GitHub Actions workflow
publishes images to GitHub Container Registry:

```txt
ghcr.io/<owner>/<repo>-api:latest
ghcr.io/<owner>/<repo>-web:latest
```

Set `NUDIBRANCH_IMAGE_PREFIX` in `.env`, then deploy without building locally:

```sh
NUDIBRANCH_IMAGE_PREFIX=ghcr.io/<owner>/<repo>
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Services:

- Web UI: `http://localhost:5173`
- API: `http://localhost:8000`
- OpenAPI: `http://localhost:8000/api/v1/openapi.json`
- Swagger UI: `http://localhost:8000/docs`

## Mounted Storage

```txt
/app/import     files manually dropped in for the import wizard
/app/staging    verified files waiting to enter the library
/app/library    managed Jellyfin library
/app/downloads  slskd and yt-dlp downloads
/app/trash      reversible deletes, purged after 30 days
/app/config     SQLite database and runtime configuration
/app/backups    scheduled and manual backups
```

## Current State

This repository is the first implementation scaffold. It includes the Docker
stack, FastAPI server, SQLite models, worker lease pattern, API route skeleton,
OpenAPI generation, web UI shell, and design/deployment/API documentation.
