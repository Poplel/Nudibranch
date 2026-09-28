"""Turn one WishlistItem into the download requests that serve it.

Extracted from `api/routes.py::propose_wishlist_items` so it can run on the **worker**.  That move
is what makes auto-search-on-wishlist possible at all: building these payloads calls MusicBrainz
(`lookup_album_tracks`), which is far too slow to sit on the HTTP path of `POST /wishlist`.

⚠️ An album-level request MUST expand into one request per track.  The Soulseek matcher scores each
candidate file against a specific track, so an unexpanded album request matches 0 files even when
slskd found the right folder -- and then wrongly falls through to the YouTube fallback.
"""

from __future__ import annotations

import re

from nudibranch.db.models import WishlistItem
from nudibranch.services.metadata_lookup import lookup_album_tracks


def normalized_music_name(value: str | None) -> str:
    """Kept byte-identical to `api/routes.py`'s version -- this module is an extraction, not a
    redesign, and quietly changing how a wishlist track is matched to a MusicBrainz tracklist is
    not something to slip into one.

    (It would arguably be better with NFKD folding, so an accented title matches its unaccented
    spelling -- see the NFD "e-acute" note in the import sanitizer. Change both together, deliberately,
    or neither.)
    """
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _album_tracks(artist: str, album: str, cache: dict[tuple[str, str], dict | None]) -> dict | None:
    key = (artist, album)
    if key not in cache:
        try:
            cache[key] = lookup_album_tracks(artist, album)
        except Exception:  # noqa: BLE001 - a lookup miss must never block the request
            cache[key] = None
    return cache.get(key)


def _track_payload(record: dict, track: dict, artist: str, album: str) -> dict:
    return {
        "action": "wishlist_request",
        "kind": "track",
        "artist": artist,
        "album": album,
        "track": track.get("title"),
        "track_number": track.get("track_number"),
        "disc_number": track.get("disc_number"),
        "duration_ms": track.get("length"),
        "musicbrainz_album_id": track.get("musicbrainz_album_id") or record.get("musicbrainz_album_id"),
        "musicbrainz_recording_id": track.get("musicbrainz_recording_id"),
    }


def build_request_payloads(
    item: WishlistItem,
    cache: dict[tuple[str, str], dict | None] | None = None,
) -> list[dict]:
    """Every download request this wishlist item implies, each already stamped with its owner.

    An album expands to one payload per MusicBrainz track; a track resolves its own metadata; both
    fall back to a bare artist/album/track request when MusicBrainz has nothing.
    """
    cache = {} if cache is None else cache
    base_owner = {"user_id": item.user_id, "wishlist_item_id": item.id}
    payloads: list[dict] = []

    if item.kind == "album" and item.album and not item.track:
        record = _album_tracks(item.artist, item.album, cache)
        for track in (record or {}).get("tracks", []) or []:
            if not track.get("title"):
                continue
            payloads.append(_track_payload(record or {}, track, item.artist, item.album))

    if not payloads:
        payload = {
            "action": "wishlist_request",
            "kind": item.kind,
            "artist": item.artist,
            "album": item.album,
            "track": item.track,
        }
        if item.track and item.album:
            record = _album_tracks(item.artist, item.album, cache)
            expected = normalized_music_name(item.track)
            for track in (record or {}).get("tracks", []) or []:
                if normalized_music_name(track.get("title")) != expected:
                    continue
                payload.update(
                    {
                        "track_number": track.get("track_number"),
                        "disc_number": track.get("disc_number"),
                        "duration_ms": track.get("length"),
                        "musicbrainz_album_id": track.get("musicbrainz_album_id")
                        or (record or {}).get("musicbrainz_album_id"),
                        "musicbrainz_recording_id": track.get("musicbrainz_recording_id"),
                    }
                )
                break
        payloads.append(payload)

    return [payload | base_owner for payload in payloads]
