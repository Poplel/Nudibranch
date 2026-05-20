import re
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from nudibranch.services.imports import read_audio_metadata


USER_AGENT = "Nudibranch/0.1 (https://github.com/Poplel/Nudibranch)"


def lookup_recording_by_musicbrainz_metadata(file_info: dict) -> list[dict]:
    metadata = file_info_metadata(file_info)
    artist = metadata.get("albumartist") or metadata.get("artist") or file_info.get("artist")
    album = metadata.get("album") or file_info.get("album")
    if not artist or not album:
        raise ValueError("Artist and album metadata are required for MusicBrainz matching")
    record = lookup_album_tracks(str(artist), str(album), metadata.get("musicbrainz_album_id") or file_info.get("musicbrainz_album_id"))
    candidates = []
    for track in record.get("tracks") or []:
        score = musicbrainz_track_score(metadata, track)
        candidates.append(
            {
                "score": score,
                "metadata": {
                    "artist": record.get("artist") or artist,
                    "albumartist": record.get("artist") or artist,
                    "album": record.get("album") or album,
                    "title": track.get("title"),
                    "track_number": track.get("track_number"),
                    "disc_number": track.get("disc_number"),
                    "duration_ms": track.get("length"),
                    "musicbrainz_recording_id": track.get("musicbrainz_recording_id"),
                    "musicbrainz_album_id": record.get("musicbrainz_album_id"),
                },
                "source": "musicbrainz",
            }
        )
    return sorted(candidates, key=lambda candidate: candidate.get("score") or 0, reverse=True)


def file_info_metadata(file_info: dict) -> dict:
    metadata = {key: value for key, value in dict(file_info.get("metadata") or {}).items() if value is not None}
    if file_info.get("path"):
        metadata = {**read_audio_metadata(Path(file_info["path"])), **metadata}
    for key in ("artist", "album", "title", "track_number", "duration_ms", "musicbrainz_album_id", "musicbrainz_recording_id"):
        if file_info.get(key) is not None and metadata.get(key) is None:
            metadata[key] = file_info[key]
    return metadata


def musicbrainz_track_score(metadata: dict, track: dict) -> float:
    title_score = text_similarity(metadata.get("title"), track.get("title"))
    number_score = number_match_score(metadata.get("track_number"), track.get("track_number"))
    duration_score = duration_score_for_musicbrainz(metadata.get("duration_ms"), track.get("length"))
    recording_score = 1.0 if metadata.get("musicbrainz_recording_id") and metadata.get("musicbrainz_recording_id") == track.get("musicbrainz_recording_id") else 0.0
    return max(recording_score, (title_score * 0.58) + (number_score * 0.24) + (duration_score * 0.18))


def text_similarity(left: object, right: object) -> float:
    left_text = normalize(left)
    right_text = normalize(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    if left_text in right_text or right_text in left_text:
        return 0.94
    return SequenceMatcher(None, left_text, right_text).ratio()


def number_match_score(left: object, right: object) -> float:
    left_number = parse_track_number(left)
    right_number = parse_track_number(right)
    if left_number is None or right_number is None:
        return 0.5
    return 1.0 if left_number == right_number else 0.0


def duration_score_for_musicbrainz(left: object, right: object) -> float:
    try:
        left_ms = int(left)
        right_ms = int(right)
    except (TypeError, ValueError):
        return 0.5
    if left_ms <= 0 or right_ms <= 0:
        return 0.5
    delta = abs(left_ms - right_ms)
    if delta <= 5000:
        return 1.0
    return max(0.0, 1.0 - (delta / max(left_ms, right_ms)) * 5)


def search_album_releases(artist: str, album: str) -> list[dict]:
    releases = find_releases(artist, album, limit=10)
    itunes_art = itunes_album_artwork(artist, album)
    return [
        {
            "id": release.get("id"),
            "title": release.get("title"),
            "artist": artist_credit(release.get("artist-credit", [])) or artist,
            "date": release.get("date"),
            "country": release.get("country"),
            "score": release.get("score"),
            "track_count": release.get("track-count"),
            "cover_art_url": cover_art_url(release.get("id"), release) or itunes_art,
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


def itunes_album_artwork(artist: str, album: str) -> str | None:
    try:
        response = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {album}", "entity": "album", "limit": 5},
            timeout=10,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    normalized_album = normalize(album)
    normalized_artist = normalize(artist)
    for result in response.json().get("results", []):
        if normalize(result.get("collectionName")) != normalized_album:
            continue
        if normalized_artist and normalized_artist not in normalize(result.get("artistName")):
            continue
        artwork = result.get("artworkUrl100")
        if artwork:
            return artwork.replace("100x100bb", "600x600bb")
    for result in response.json().get("results", []):
        artwork = result.get("artworkUrl100")
        if artwork:
            return artwork.replace("100x100bb", "600x600bb")
    return None


def normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
