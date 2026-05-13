# API Guide

The REST API is versioned under `/api/v1`. The generated OpenAPI document is
available at `/api/v1/openapi.json`, and Swagger UI is available at `/docs`.

## Authentication

The web UI logs in with a PIN:

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "pin": "123456"
}
```

The response includes an API key. The iOS app should store the key securely in
Keychain and send it as:

```http
Authorization: Bearer <api-key>
```

## Core Resources

```txt
GET  /api/v1/me
GET  /api/v1/library/tree

POST /api/v1/imports/scan
POST /api/v1/imports/propose
POST /api/v1/imports/acoustic-match
POST /api/v1/imports/album-search
POST /api/v1/imports/album-lookup

GET  /api/v1/wishlist
POST /api/v1/wishlist
POST /api/v1/wishlist/process

GET  /api/v1/approvals
POST /api/v1/approvals/{batch_id}/selection
POST /api/v1/approvals/{batch_id}/approve
POST /api/v1/approvals/{batch_id}/reject

GET  /api/v1/tasks
POST /api/v1/tasks

GET  /api/v1/notifications
POST /api/v1/notifications/devices
```

## Approval Selection

Bulk approval is performed by changing selected state, then approving the batch.
No individual pop-up is required.

```http
POST /api/v1/approvals/{batch_id}/selection

{
  "item_ids": ["item-1", "item-2"],
  "selected": false
}
```

```http
POST /api/v1/approvals/{batch_id}/approve
```

Only selected items should execute.

## Import Metadata Lookups

Fingerprint-based track matching uses AcoustID when `ACOUSTID_API_KEY` is set:

```http
POST /api/v1/imports/acoustic-match

{
  "file": {
    "path": "/app/import/Artist/Album/01 Track.flac",
    "fingerprint": {
      "duration": 180,
      "fingerprint": "..."
    }
  }
}
```

Album search returns MusicBrainz release candidates with cover-art URLs where
available:

```http
POST /api/v1/imports/album-search

{
  "artist": "Artist",
  "album": "Album"
}
```

Album track-list checks use a selected MusicBrainz release, or the best match
when `release_id` is omitted:

```http
POST /api/v1/imports/album-lookup

{
  "artist": "Artist",
  "album": "Album",
  "release_id": "optional-musicbrainz-release-id"
}
```

## Rejection Suppression

```http
POST /api/v1/approvals/{batch_id}/reject

{
  "item_ids": ["item-3"],
  "suppress_for": "week"
}
```

Allowed `suppress_for` values:

- `none`
- `day`
- `week`
- `forever`

## iOS Device Registration

```http
POST /api/v1/notifications/devices

{
  "device_name": "Evan's iPhone",
  "apns_token": "<token from UIApplicationDelegate>"
}
```

The server stores device tokens per user and uses APNs for background
notifications when configured.

## Future Endpoint Groups

The scaffold intentionally leaves room for these API groups:

```txt
/api/v1/metadata
/api/v1/downloads
/api/v1/lyrics
/api/v1/artwork
/api/v1/playlists
/api/v1/jellyfin
/api/v1/backups
/api/v1/settings
/api/v1/system
```
