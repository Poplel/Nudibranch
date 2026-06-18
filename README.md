# Nudibranch

Nudibranchs (/ˈnjuːdɪbræŋk/) are a group of soft-bodied marine gastropod molluscs, belonging to the order Nudibranchia, a type of colorful sea slug. I named this program after Glaucus atlanticus, a nudibranch that has its **jelly**-likecerata positioned like wings or **fins** on it's back. I though this was fitting since the program was made with Jellyfin in mind, and also because it's probably my favorite animal.


Nudibranch (The program) is a music library and download manager.
It can be used as a standalone music player and downloader, or in tandem with Jellyfin.

## Features

- Import and repair a Jellyfin music library.
- Search and download from soulseek with slskd, with yt-dlp as a fallback.
- Enrich albums with cover art and lyrics.
- Expose every web UI action through a REST API, all actions can be done by any client.
- **In progress** IOS app for managing and playing your library.
- Deploy with a simple Docker Compose and no additonal programs required (Jellyfin is supported but optional).

## Quick Start with prebuilt images

```txt
ghcr.io/poplel/nudibranch-api:latest
ghcr.io/poplel/nudibranch-web:latest
```

Copy the docker-compose.yml example and the example.env to the folder you want to deploy in, rename example.env to .env.
Fill out the .env with your information then run:
```sh
docker compose pull && docker compose up -d
```
And nudibranch should deploy!

Services:

- Web UI: `http://localhost:5173`
- API: `http://localhost:8000`
- OpenAPI: `http://localhost:8000/api/v1/openapi.json`
- Swagger UI: `http://localhost:8000/docs`

## Mounted Storage

```txt
/app/import     files manually dropped in for the import wizard
/app/staging    downloaded and verified files waiting to enter the library
/app/library    managed library
/app/downloads  slskd and yt-dlp downloads
/app/trash      deleted files, purged every 30 days
/app/config     SQLite database and runtime configuration
/app/backups    database backups
```

