# Nudibranch Architecture

## Principles

- Nudibranch is the source of truth.
- Jellyfin is managed as a downstream playback/display system.
- Every mutation starts as a dry-run proposal.
- Proposals are approved in bulk, with item-level selection and deselection.
- The REST API and web UI expose the same capabilities.
- Docker Compose owns all runtime dependencies.

## Services

```txt
web     Nginx-served React UI, proxies /api to api
api     FastAPI REST API, OpenAPI docs, auth, proposal/task creation
worker  SQLite-backed task runner, import scans, downloads, enrichment, APNs
```

SQLite is used with WAL mode, `busy_timeout`, and a database-backed task lease
pattern. The worker claims one queued task by atomic update before running it.

## Storage Flow

```txt
/app/import
  user drops files here
  import wizard scans and proposes organization

/app/downloads
  slskd and yt-dlp download targets
  downloaded files do not become library files directly

/app/staging
  candidate files after download/import processing
  metadata reads, MusicBrainz checks, and candidate naming happen here

/app/library
  managed Jellyfin music library
  default path: Artist/Album/#-Title.flac

/app/trash
  reversible deletes
  purged after 30 days
```

## Approval Model

Proposal batches contain tree-shaped proposal items. A user may select all,
deselect individual branches/items, approve selected work, or reject selected
work with a suppression period.

Suppression choices:

- none
- 1 day
- 1 week
- forever

Approval creates execution tasks. Execution tasks are visible in the queue and
are responsible for updating files, database state, Jellyfin, and notifications.

## Import Wizard

1. Scan `/app/import`.
2. Read tags with Mutagen.
3. Match tags against MusicBrainz release metadata when requested.
4. Group files into `Artist > Album > Track`.
5. Match against existing library.
6. Detect duplicates and deluxe/remaster/version conflicts.
7. Propose moves, renames, metadata writes, `cover.jpg`, and `.lrc`.
8. Approve selected changes.
9. Move files through `/app/staging` into `/app/library`.

## Download Flow

1. Wishlist items are created per user.
2. Admins can view all wishlists.
3. Scheduled processing defaults to every 6 hours.
4. slskd is searched first.
5. yt-dlp fallback is proposed if slskd has no acceptable match.
6. FLAC is preferred; ALAC/WAV/AIFF are acceptable; MP3 triggers a warning.
7. Exact album release is preferred.
8. Whole-album downloads for one track are not allowed.
9. Candidates become approval batches before download.
10. Approved downloads land in `/app/downloads`, move through staging, verify against MusicBrainz/request metadata, then import into the library as one batch.

## Notifications

Notifications are persisted first, then delivered to:

- top-left web tray
- web pop-up banners
- iOS APNs devices

The APNs outbox is part of the initial design so server-triggered notifications
can arrive when the iOS app is not open.
