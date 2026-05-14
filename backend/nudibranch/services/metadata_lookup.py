import re
from pathlib import Path

import httpx

from nudibranch.core.config import get_settings
from nudibranch.services.imports import fingerprint_audio


USER_AGENT = "Nudibranch/0.1 (https://github.com/Poplel/Nudibranch)"


def lookup_recording_by_fingerprint(file_info: dict, acoustid_api_key: str | None = None) -> list[dict]:
    settings = get_settings()
    api_key = acoustid_api_key or settings.acoustid_api_key
    if not api_key:
        raise ValueError("ACOUSTID_API_KEY is required for acoustic metadata lookup")

    fingerprint = file_info.get("fingerprint")
    if not fingerprint:
        path = Path(file_info["path"])
        fingerprint = fingerprint_audio(path)
    if not fingerprint:
        raise ValueError("Unable to fingerprint this file")

    params = {
        "client": api_key,
        "duration": fingerprint.get("duration"),
        "fingerprint": fingerprint.get("fingerprint"),
        "meta": "recordings releasegroups releases tracks",
    }
    response = httpx.get("https://api.acoustid.org/v2/lookup", params=params, timeout=20, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") == "error":
        error = payload.get("error") or {}
        message = error.get("message") or "AcoustID rejected the lookup request"
        raise ValueError(f"AcoustID lookup failed: {message}")
    candidates = []
    for result in payload.get("results", []):
        for recording in result.get("recordings", []):
            release = first_release(recording)
            artist = artist_credit(recording.get("artists", []))
            candidates.append(
                {
                    "score": result.get("score", 0),
                    "metadata": {
                        "artist": artist,
                        "albumartist": artist,
                        "album": release.get("title"),
                        "title": recording.get("title"),
                        "track_number": release.get("track_number"),
                        "musicbrainz_recording_id": recording.get("id"),
                        "musicbrainz_album_id": release.get("id"),
                    },
                    "source": "acoustid",
                }
            )
    return sorted(candidates, key=lambda candidate: candidate.get("score") or 0, reverse=True)


def search_album_releases(artist: str, album: str) -> list[dict]:
    releases = find_releases(artist, album, limit=10)
    return [
        {
            "id": release.get("id"),
            "title": release.get("title"),
            "artist": artist_credit(release.get("artist-credit", [])) or artist,
            "date": release.get("date"),
            "country": release.get("country"),
            "score": release.get("score"),
            "track_count": release.get("track-count"),
            "cover_art_url": cover_art_url(release.get("id"), release),
        }
        for release in releases
        if release.get("id")
    ]


def lookup_album_tracks(artist: str, album: str, release_id: str | None = None) -> dict:
    release = {"id": release_id} if release_id else find_release(artist, album)
    if not release:
        return {"artist": artist, "album": album, "tracks": [], "source": "musicbrainz"}

    release_id = release["id"]
    response = httpx.get(
        f"https://musicbrainz.org/ws/2/release/{release_id}",
        params={"fmt": "json", "inc": "recordings+media+artist-credits"},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    detail = response.json()
    tracks = []
    for medium in detail.get("media", []):
        disc_number = medium.get("position")
        for track in medium.get("tracks", []):
            recording = track.get("recording") or {}
            tracks.append(
                {
                    "track_number": parse_track_number(track.get("number") or track.get("position")),
                    "disc_number": disc_number,
                    "title": track.get("title") or recording.get("title") or "Unknown Title",
                    "musicbrainz_recording_id": recording.get("id"),
                    "length": track.get("length") or recording.get("length"),
                }
            )
    return {
        "artist": artist_credit(detail.get("artist-credit", [])) or artist,
        "album": detail.get("title") or album,
        "musicbrainz_album_id": release_id,
        "tracks": tracks,
        "source": "musicbrainz",
    }


def find_releases(artist: str, album: str, limit: int = 5) -> list[dict]:
    query = f'artist:"{escape_query(artist)}" AND release:"{escape_query(album)}"'
    response = httpx.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"fmt": "json", "query": query, "limit": limit},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.json().get("releases", [])


def find_release(artist: str, album: str) -> dict | None:
    releases = find_releases(artist, album, limit=5)
    if not releases:
        return None
    normalized_album = normalize(album)
    return sorted(
        releases,
        key=lambda release: (
            normalize(release.get("title")) != normalized_album,
            -(release.get("score") or 0),
        ),
    )[0]


def first_release(recording: dict) -> dict:
    releases = recording.get("releases") or []
    if not releases:
        return {}
    release = releases[0]
    media = release.get("mediums") or release.get("media") or []
    track_number = None
    for medium in media:
        tracks = medium.get("tracks") or []
        if tracks:
            track_number = parse_track_number(tracks[0].get("number") or tracks[0].get("position"))
            break
    return {"id": release.get("id"), "title": release.get("title"), "track_number": track_number}


def artist_credit(artists: list) -> str | None:
    names = []
    for artist in artists:
        if isinstance(artist, dict):
            names.append(artist.get("name") or artist.get("artist", {}).get("name"))
    return " & ".join(name for name in names if name) or None


def parse_track_number(value) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def escape_query(value: str) -> str:
    return value.replace('"', "")


def cover_art_url(release_id: str | None, release: dict | None = None) -> str | None:
    if not release_id:
        return None
    archive = (release or {}).get("cover-art-archive") or {}
    if archive and not archive.get("front"):
        return None
    return f"https://coverartarchive.org/release/{release_id}/front-250"


def normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
