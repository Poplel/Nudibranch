import re
import shutil
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import httpx

from nudibranch.core.config import get_settings
from nudibranch.services.imports import read_audio_metadata


USER_AGENT = "Nudibranch/0.1 (https://github.com/Poplel/Nudibranch)"
DISCOVER_CACHE_TTL_SECONDS = 24 * 60 * 60
DISCOVER_CACHE_MAX_HITS = 3


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
    seen = set()
    results = []
    for release in releases:
        if not release.get("id") or not release.get("title"):
            continue
        key = (normalize(artist_credit(release.get("artist-credit", [])) or artist), normalize(release.get("title")))
        if key in seen:
            continue
        seen.add(key)
        results.append(
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
        )
    return results


def discover_music(query: str) -> dict:
    query = query.strip()
    if not query:
        return {"artists": [], "albums": [], "tracks": [], "focus": None}
    artists = search_artists(query, limit=5)
    albums = search_releases(query, limit=8)
    tracks = search_recordings(query, limit=8)
    artist_map = {artist["id"]: artist for artist in artists if artist.get("id")}
    for album in albums:
        if album.get("artist_id") and album["artist_id"] not in artist_map:
            artist_map[album["artist_id"]] = {"id": album["artist_id"], "name": album["artist"], "albums": []}
    for track in tracks:
        if track.get("artist_id") and track["artist_id"] not in artist_map:
            artist_map[track["artist_id"]] = {"id": track["artist_id"], "name": track["artist"], "albums": []}
    for artist in artist_map.values():
        artist["albums"] = discover_artist_albums(artist["id"], artist["name"], limit=8)
        for album in albums:
            if album.get("artist_id") == artist.get("id"):
                ensure_artist_album(artist, album)
        for track in tracks:
            if track.get("artist_id") == artist.get("id") and track.get("album_id"):
                ensure_artist_album(
                    artist,
                    {
                        "id": track.get("album_id"),
                        "title": track.get("album") or "Singles",
                        "artist": track.get("artist") or artist.get("name"),
                        "artist_id": artist.get("id"),
                        "tracks": [track],
                    },
                )
        artist["image_url"] = next((album.get("cover_art_url") for album in artist["albums"] if album.get("cover_art_url")), None)
    focus = None
    if tracks:
        focus = {"kind": "track", "artist_id": tracks[0].get("artist_id"), "album_id": tracks[0].get("album_id"), "track_id": tracks[0].get("id")}
    elif albums:
        focus = {"kind": "album", "artist_id": albums[0].get("artist_id"), "album_id": albums[0].get("id")}
    elif artists:
        focus = {"kind": "artist", "artist_id": artists[0].get("id")}
    return {"artists": list(artist_map.values()), "albums": albums, "tracks": tracks, "focus": focus}


def ensure_artist_album(artist: dict, album: dict) -> None:
    albums = artist.setdefault("albums", [])
    existing = next((entry for entry in albums if entry.get("id") == album.get("id") or normalize(entry.get("title")) == normalize(album.get("title"))), None)
    if existing:
        existing_tracks = existing.setdefault("tracks", [])
        seen = {normalize(track.get("title")) for track in existing_tracks}
        for track in album.get("tracks") or []:
            if normalize(track.get("title")) not in seen:
                existing_tracks.append(track)
        return
    hydrated = dict(album)
    if not hydrated.get("tracks") and hydrated.get("id"):
        record = lookup_album_tracks(hydrated.get("artist") or artist.get("name"), hydrated.get("title") or "", hydrated.get("id"))
        hydrated["tracks"] = dedupe_tracks(record.get("tracks") or [])
    albums.insert(0, hydrated)


def search_artists(query: str, limit: int = 5) -> list[dict]:
    response = httpx.get(
        "https://musicbrainz.org/ws/2/artist/",
        params={"fmt": "json", "query": escape_query(query), "limit": limit},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    seen = set()
    artists = []
    for artist in response.json().get("artists", []):
        artist_id = artist.get("id")
        name = artist.get("name")
        if not artist_id or not name or artist_id in seen:
            continue
        seen.add(artist_id)
        artists.append(
            {
                "id": artist_id,
                "name": name,
                "sort_name": artist.get("sort-name"),
                "disambiguation": artist.get("disambiguation"),
                "score": artist.get("score"),
                "albums": [],
            }
        )
    return artists


def search_releases(query: str, limit: int = 8) -> list[dict]:
    response = httpx.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"fmt": "json", "query": escape_query(query), "limit": limit},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return normalize_release_results(response.json().get("releases", []))


def search_recordings(query: str, limit: int = 8) -> list[dict]:
    response = httpx.get(
        "https://musicbrainz.org/ws/2/recording/",
        params={"fmt": "json", "query": escape_query(query), "limit": limit, "inc": "artist-credits+releases"},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    tracks = []
    seen = set()
    for recording in response.json().get("recordings", []):
        release = first_release(recording)
        artist_id = first_artist_id(recording.get("artist-credit", []))
        artist = artist_credit(recording.get("artist-credit", []))
        key = (normalize(artist), normalize(recording.get("title")), normalize(release.get("title")))
        if not recording.get("id") or not recording.get("title") or key in seen:
            continue
        seen.add(key)
        tracks.append(
            {
                "id": recording.get("id"),
                "title": recording.get("title"),
                "artist": artist,
                "artist_id": artist_id,
                "album": release.get("title"),
                "album_id": release.get("id"),
                "track_number": release.get("track_number"),
                "duration_ms": recording.get("length"),
                "score": recording.get("score"),
            }
        )
    return tracks


def discover_artist_albums(artist_id: str, artist_name: str, limit: int = 8) -> list[dict]:
    response = httpx.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"fmt": "json", "artist": artist_id, "status": "official", "limit": min(limit * 3, 25), "inc": "artist-credits"},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    albums = normalize_release_results(response.json().get("releases", []), fallback_artist=artist_name, fallback_artist_id=artist_id)
    albums = sorted(albums, key=lambda album: (album.get("date") or "9999", album.get("title") or ""))
    hydrated = []
    for album in albums[:limit]:
        record = lookup_album_tracks(album["artist"], album["title"], album["id"])
        hydrated.append({**album, "tracks": dedupe_tracks(record.get("tracks") or [])})
    return hydrated


def normalize_release_results(releases: list[dict], fallback_artist: str | None = None, fallback_artist_id: str | None = None) -> list[dict]:
    seen = set()
    albums = []
    for release in releases:
        release_id = release.get("id")
        title = release.get("title")
        artist = artist_credit(release.get("artist-credit", [])) or fallback_artist
        artist_id = first_artist_id(release.get("artist-credit", [])) or fallback_artist_id
        if not release_id or not title or not artist:
            continue
        key = (normalize(artist), normalize(title))
        if key in seen:
            continue
        seen.add(key)
        albums.append(
            {
                "id": release_id,
                "title": title,
                "artist": artist,
                "artist_id": artist_id,
                "date": release.get("date"),
                "country": release.get("country"),
                "score": release.get("score"),
                "track_count": release.get("track-count"),
                "cover_art_url": cover_art_url(release_id, release) or itunes_album_artwork(artist, title),
                "tracks": [],
            }
        )
    return albums


def dedupe_tracks(tracks: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for index, track in enumerate(tracks, start=1):
        key = (track.get("disc_number") or 1, track.get("track_number") or index, normalize(track.get("title")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(track)
    return deduped


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


def first_artist_id(artists: list) -> str | None:
    for artist in artists or []:
        if isinstance(artist, dict):
            artist_id = artist.get("artist", {}).get("id") or artist.get("id")
            if artist_id:
                return artist_id
    return None


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


def cache_discover_art(url: str | None, cache_key: str) -> str | None:
    if not url:
        return None
    cache_dir = get_settings().config_path / "discover-art-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "-", cache_key).strip("-")[:160] or "art"
    image_path = cache_dir / f"{safe_key}{ext}"
    meta_path = cache_dir / f"{safe_key}.json"
    now = time.time()
    if image_path.exists() and meta_path.exists():
        try:
            metadata = json_load(meta_path)
            age = now - float(metadata.get("fetched_at") or 0)
            hits = int(metadata.get("hits") or 0)
            if age <= DISCOVER_CACHE_TTL_SECONDS and hits < DISCOVER_CACHE_MAX_HITS:
                metadata["hits"] = hits + 1
                meta_path.write_text(json_dumps(metadata), encoding="utf-8")
                return f"/api/v1/discover/art/{image_path.name}"
        except (OSError, ValueError, TypeError):
            pass
    try:
        response = httpx.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        image_path.write_bytes(response.content)
        meta_path.write_text(json_dumps({"source_url": url, "fetched_at": now, "hits": 1}), encoding="utf-8")
        return f"/api/v1/discover/art/{image_path.name}"
    except httpx.HTTPError:
        return url


def clear_discover_art_cache() -> int:
    cache_dir = get_settings().config_path / "discover-art-cache"
    if not cache_dir.exists():
        return 0
    count = sum(1 for path in cache_dir.iterdir() if path.is_file())
    shutil.rmtree(cache_dir)
    return count


def json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def json_dumps(value: dict) -> str:
    import json

    return json.dumps(value, indent=2)


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
