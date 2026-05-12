# Docker Compose Deployment

Nudibranch should require no host dependencies beyond Docker and Docker Compose.
The API image installs its own media tooling, including Chromaprint, ffmpeg, and
yt-dlp.

## First Run

```sh
cp .env.example .env
docker compose up --build
```

For a server that should pull published images instead of building locally, use:

```sh
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

See [ghcr-images.md](ghcr-images.md) for the GitHub Container Registry flow.

Change these before exposing the app:

```txt
NUDIBRANCH_FIRST_ADMIN_PIN
NUDIBRANCH_FULL_ACCESS_API_KEY
```

## Reverse Proxy

The app can run behind Nginx Proxy Manager, Caddy, Traefik, or a similar reverse
proxy. For Nginx Proxy Manager, point the host to the `web` service port exposed
on the Docker host, default `5173`.

Recommended proxy behavior:

- enable HTTPS
- forward WebSocket headers for future live task updates
- preserve `X-Forwarded-Proto`
- restrict direct API port exposure when possible

## APNs

For App Store distribution and server-triggered background notifications, set:

```txt
APNS_ENABLED=true
APNS_USE_SANDBOX=false
APNS_TEAM_ID=
APNS_KEY_ID=
APNS_BUNDLE_ID=
APNS_PRIVATE_KEY_PATH=/app/config/AuthKey.p8
```

Mount or place the Apple `AuthKey_*.p8` file in the config volume. The APNs
outbox is persisted in SQLite, so notification delivery can be retried.

## External Services

Nudibranch connects to one Jellyfin server and one slskd instance.

```txt
JELLYFIN_URL=
JELLYFIN_API_KEY=
SLSKD_URL=
SLSKD_API_KEY=
```

These services may run in the same Compose project or elsewhere on the network.
