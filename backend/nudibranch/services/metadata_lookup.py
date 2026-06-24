import re
import shutil
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import httpx

from nudibranch.core.config import get_settings
from nudibranch.services.app_log import write_app_log
from nudibranch.services.imports import read_audio_metadata


USER_AGENT = "Nudibranch/0.1 (https://github.com/Poplel/Nudibranch)"
MUSICBRAINZ_TIMEOUT_SECONDS = 20
MUSICBRAINZ_RETRY_COUNT = 3
DISCOVER_ARTIST_ALBUM_LIMIT = 6
DISCOVER_ALBUM_TRACK_HYDRATION_LIMIT = 4


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


def discover_music(query: str, type: str = "all") -> dict:
    query = query.strip()
    if not query:
        return {"artists": [], "albums": [], "tracks": [], "focus": None}
    write_app_log("Discover search started", feature="discover", query=query, type=type)
    search_artists_flag = type in ("all", "artist")
    search_albums_flag = type in ("all", "album")
    search_tracks_flag = type in ("all", "track")
    artists = safe_discover_lookup("artist", query, lambda: search_artists(query, limit=5), []) if search_artists_flag else []
    write_app_log("Discover artist search completed", feature="discover", query=query, artists=len(artists))
    albums = safe_discover_lookup("album", query, lambda: search_releases(query, limit=8), []) if search_albums_flag else []
    write_app_log("Discover album search completed", feature="discover", query=query, albums=len(albums))
    tracks = safe_discover_lookup("track", query, lambda: search_recordings(query, limit=8), []) if search_tracks_flag else []
    write_app_log("Discover track search completed", feature="discover", query=query, tracks=len(tracks))
    artist_map = {artist["id"]: artist for artist in artists if artist.get("id")}
    for album in albums:
        artist_key = album.get("artist_id") or discover_artist_key(album.get("artist"))
        if artist_key and artist_key not in artist_map:
            artist_map[artist_key] = {"id": artist_key, "name": album["artist"], "albums": []}
        if artist_key:
            album["artist_id"] = artist_key
    for track in tracks:
        artist_key = track.get("artist_id") or discover_artist_key(track.get("artist"))
        if artist_key and artist_key not in artist_map:
            artist_map[artist_key] = {"id": artist_key, "name": track["artist"], "albums": []}
        if artist_key:
            track["artist_id"] = artist_key
    should_expand_artist_albums = not albums and not tracks
    expanded_artists = 0
    hydrated_albums = 0
    for artist in artist_map.values():
        if str(artist.get("id") or "").startswith("artist-name:"):
            artist["albums"] = []
            write_app_log("Discover artist album lookup skipped for name-only artist", feature="discover", query=query, artist=artist.get("name"))
        elif should_expand_artist_albums and expanded_artists < 2:
            try:
                artist["albums"] = discover_artist_albums(artist["id"], artist["name"], limit=DISCOVER_ARTIST_ALBUM_LIMIT)
                expanded_artists += 1
                write_app_log("Discover artist album lookup completed", feature="discover", query=query, artist=artist.get("name"), albums=len(artist["albums"]))
            except httpx.HTTPError as error:
                artist["albums"] = []
                write_app_log("Discover artist album lookup failed", level="warning", feature="discover", query=query, artist=artist.get("name"), error=str(error))
        else:
            artist["albums"] = []
            write_app_log("Discover artist album lookup deferred", feature="discover", query=query, artist=artist.get("name"))
        for album in albums:
            if album.get("artist_id") == artist.get("id"):
                hydrate_tracks = hydrated_albums < DISCOVER_ALBUM_TRACK_HYDRATION_LIMIT
                if ensure_artist_album(artist, album, hydrate_tracks=hydrate_tracks):
                    hydrated_albums += 1
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
                    hydrate_tracks=False,
                )
        artist["image_url"] = next((album.get("cover_art_url") for album in artist["albums"] if album.get("cover_art_url")), None)
    focus = None
    if tracks:
        focus = {"kind": "track", "artist_id": tracks[0].get("artist_id"), "album_id": tracks[0].get("album_id"), "track_id": tracks[0].get("id")}
    elif albums:
        focus = {"kind": "album", "artist_id": albums[0].get("artist_id"), "album_id": albums[0].get("id")}
    elif artists:
        focus = {"kind": "artist", "artist_id": artists[0].get("id")}
    result = {"artists": list(artist_map.values()), "albums": albums, "tracks": tracks, "focus": focus}
    write_app_log(
        "Discover search completed",
        feature="discover",
        query=query,
        artists=len(result["artists"]),
        albums=len(albums),
        tracks=len(tracks),
    )
    return result


def safe_discover_lookup(kind: str, query: str, lookup, fallback):
    try:
        write_app_log("Discover lookup started", feature="discover", kind=kind, query=query)
        return lookup()
    except httpx.HTTPError as error:
        write_app_log("Discover lookup failed", level="warning", feature="discover", kind=kind, query=query, error=str(error))
        return fallback


def discover_artist_key(artist: str | None) -> str | None:
    normalized = normalize(artist)
    return f"artist-name:{normalized}" if normalized else None


def ensure_artist_album(artist: dict, album: dict, hydrate_tracks: bool = True) -> bool:
    albums = artist.setdefault("albums", [])
    existing = next((entry for entry in albums if entry.get("id") == album.get("id") or normalize(entry.get("title")) == normalize(album.get("title"))), None)
    if existing:
        existing_tracks = existing.setdefault("tracks", [])
        seen = {normalize(track.get("title")) for track in existing_tracks}
        for track in album.get("tracks") or []:
            if normalize(track.get("title")) not in seen:
                existing_tracks.append(track)
        return False
    hydrated = dict(album)
    did_hydrate = False
    if hydrate_tracks and not hydrated.get("tracks") and hydrated.get("id"):
        try:
            write_app_log("Discover album track hydration started", feature="discover", artist=artist.get("name"), album=hydrated.get("title"))
            record = lookup_album_tracks(hydrated.get("artist") or artist.get("name"), hydrated.get("title") or "", hydrated.get("id"))
            hydrated["tracks"] = dedupe_tracks(record.get("tracks") or [])
            did_hydrate = True
            write_app_log("Discover album track hydration completed", feature="discover", artist=artist.get("name"), album=hydrated.get("title"), tracks=len(hydrated["tracks"]))
        except httpx.HTTPError as error:
            hydrated["tracks"] = []
            write_app_log("Discover album track hydration failed", level="warning", feature="discover", artist=artist.get("name"), album=hydrated.get("title"), error=str(error))
    albums.insert(0, hydrated)
    return did_hydrate


def search_artists(query: str, limit: int = 5) -> list[dict]:
    response = musicbrainz_get(
        "https://musicbrainz.org/ws/2/artist/",
        params={"fmt": "json", "query": escape_query(query), "limit": limit},
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
    response = musicbrainz_get(
        "https://musicbrainz.org/ws/2/release/",
        params={"fmt": "json", "query": escape_query(query), "limit": limit},
    )
    response.raise_for_status()
    return normalize_release_results(response.json().get("releases", []))


def search_recordings(query: str, limit: int = 8) -> list[dict]:
    response = musicbrainz_get(
        "https://musicbrainz.org/ws/2/recording/",
        params={"fmt": "json", "query": escape_query(query), "limit": limit, "inc": "artist-credits+releases"},
    )
    response.raise_for_status()
    tracks = []
    seen = set()
    for recording in response.json().get("recordings", []):
        release = first_release(recording)
        artist_id = first_artist_id(recording.get("artist-credit", []))
        artist = artist_credit(recording.get("artist-credit", []))
        key = (normalize(artist), normalize(recording.get("title")), normalize(release.get("title")))
        if not recording.get("id") or not recording.get("title") or not artist or key in seen:
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


def discover_artist_albums(artist_id: str, artist_name: str, limit: int = DISCOVER_ARTIST_ALBUM_LIMIT) -> list[dict]:
    response = musicbrainz_get(
        "https://musicbrainz.org/ws/2/release/",
        params={"fmt": "json", "artist": artist_id, "limit": min(limit * 3, 25), "inc": "artist-credits"},
    )
    response.raise_for_status()
    releases = [
        release
        for release in response.json().get("releases", [])
        if not release.get("status") or str(release.get("status")).lower() == "official"
    ]
    albums = normalize_release_results(releases, fallback_artist=artist_name, fallback_artist_id=artist_id)
    albums = sorted(albums, key=lambda album: (album.get("date") or "9999", album.get("title") or ""))
    hydrated = []
    for index, album in enumerate(albums[:limit]):
        if index < DISCOVER_ALBUM_TRACK_HYDRATION_LIMIT:
            try:
                write_app_log("Discover artist album hydration started", feature="discover", artist=artist_name, album=album.get("title"))
                record = lookup_album_tracks(album["artist"], album["title"], album["id"])
                hydrated.append({**album, "tracks": dedupe_tracks(record.get("tracks") or [])})
                write_app_log("Discover artist album hydration completed", feature="discover", artist=artist_name, album=album.get("title"), tracks=len(hydrated[-1].get("tracks") or []))
                continue
            except httpx.HTTPError as error:
                write_app_log("Discover artist album hydration failed", level="warning", feature="discover", artist=artist_name, album=album.get("title"), error=str(error))
        hydrated.append(album)
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
                "cover_art_urls": cover_art_urls(release_id, release, artist, title),
                "cover_art_url": first_cover_art_url(release_id, release, artist, title),
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


def lookup_musicbrainz_ids(artist: str, album: str) -> dict | None:
    releases = find_releases(artist, album, limit=15)
    if not releases:
        return None
    best = rank_releases(album, releases)[0]
    release_id = best.get("id")
    if not release_id:
        return None
    match_score = best.get("score") or 0
    response = musicbrainz_get(
        f"https://musicbrainz.org/ws/2/release/{release_id}",
        params={"fmt": "json", "inc": "recordings+media+artist-credits+release-groups"},
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
    artist_credit_list = detail.get("artist-credit") or []
    return {
        "match_score": match_score,
        "artist_mbid": (artist_credit_list[0].get("artist", {}).get("id") if artist_credit_list and isinstance(artist_credit_list[0], dict) else None),
        "artist_name": artist_credit(artist_credit_list) or artist,
        "release_id": release_id,
        "release_group_id": (detail.get("release-group") or {}).get("id") or None,
        "album_title": detail.get("title") or album,
        "tracks": tracks,
    }


def lookup_album_tracks(artist: str, album: str, release_id: str | None = None) -> dict:
    release = {"id": release_id} if release_id else find_release(artist, album)
    if not release:
        return {"artist": artist, "album": album, "tracks": [], "source": "musicbrainz"}

    release_id = release["id"]
    response = musicbrainz_get(
        f"https://musicbrainz.org/ws/2/release/{release_id}",
        params={"fmt": "json", "inc": "recordings+media+artist-credits"},
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


def relaxed_release_terms(value: str) -> str:
    """Strip Lucene operators so the album can go in an UNQUOTED `release:(…)` clause.

    A strict phrase query (`release:"The E.N.D."`) misses titles MusicBrainz stores with different
    punctuation — e.g. the real album is "The E•N•D" (bullets, not periods), so the phrase matched
    only the compilation "The Beginning & The Best of the E.N.D.". Token matching finds the real
    release; ranking (see rank_releases) then picks the right one.
    """
    cleaned = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", value or "")
    return " ".join(cleaned.split())


def find_releases(artist: str, album: str, limit: int = 15) -> list[dict]:
    terms = relaxed_release_terms(album)
    release_clause = f"release:({terms})" if terms else f'release:"{escape_query(album)}"'
    query = f'artist:"{escape_query(artist)}" AND {release_clause}'
    response = musicbrainz_get(
        "https://musicbrainz.org/ws/2/release/",
        params={"fmt": "json", "query": query, "limit": limit},
    )
    response.raise_for_status()
    return response.json().get("releases", [])


_DOWNRANK_SECONDARY_TYPES = {"compilation", "live", "dj-mix", "mixtape/street", "remix", "interview"}


def rank_releases(album: str, releases: list[dict]) -> list[dict]:
    """Order release search hits best-first: exact normalized title, then closest title, then a
    studio album over a compilation/live set, then MusicBrainz's own relevance score."""
    normalized_album = normalize(album)

    def key(release: dict) -> tuple:
        title = release.get("title") or ""
        secondary = (release.get("release-group") or {}).get("secondary-types") or []
        is_downranked = any(str(s).lower() in _DOWNRANK_SECONDARY_TYPES for s in secondary)
        return (
            normalize(title) != normalized_album,        # exact normalized-title match first
            -text_similarity(album, title),              # then the most similar title
            is_downranked,                               # prefer studio albums over comps/live
            -(release.get("score") or 0),                # then MusicBrainz relevance
        )

    return sorted(releases, key=key)


def musicbrainz_get(url: str, params: dict | None = None) -> httpx.Response:
    last_error: httpx.HTTPError | None = None
    for attempt in range(1, MUSICBRAINZ_RETRY_COUNT + 1):
        try:
            if attempt > 1:
                write_app_log("MusicBrainz request retrying", feature="musicbrainz", url=url, attempt=attempt)
                time.sleep(min(2.5, 0.75 * attempt))
            response = httpx.get(
                url,
                params=params,
                timeout=httpx.Timeout(MUSICBRAINZ_TIMEOUT_SECONDS, connect=8),
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as error:
            last_error = error
            write_app_log("MusicBrainz request failed", level="warning", feature="musicbrainz", url=url, attempt=attempt, error=str(error))
    assert last_error is not None
    raise last_error


def find_release(artist: str, album: str) -> dict | None:
    releases = find_releases(artist, album, limit=15)
    if not releases:
        return None
    return rank_releases(album, releases)[0]


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


def first_cover_art_url(release_id: str | None, release: dict | None, artist: str, album: str) -> str | None:
    urls = cover_art_urls(release_id, release, artist, album)
    return urls[0] if urls else None


def cover_art_urls(release_id: str | None, release: dict | None = None, artist: str | None = None, album: str | None = None) -> list[str]:
    urls = []
    release_url = cover_art_url(release_id, release)
    if release_url:
        urls.append(release_url)
    release_group_id = ((release or {}).get("release-group") or {}).get("id")
    if release_group_id:
        urls.append(f"https://coverartarchive.org/release-group/{release_group_id}/front-250")
    if artist and album:
        itunes_url = itunes_album_artwork(artist, album)
        if itunes_url:
            urls.append(itunes_url)
    return unique_urls(urls)


def cover_art_url(release_id: str | None, release: dict | None = None) -> str | None:
    if not release_id:
        return None
    archive = (release or {}).get("cover-art-archive") or {}
    if archive and not archive.get("front"):
        return None
    return f"https://coverartarchive.org/release/{release_id}/front-250"


def unique_urls(urls: list[str | None]) -> list[str]:
    result = []
    seen = set()
    for url in urls:
        if not url:
            continue
        key = str(url)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


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


def album_cover_candidate_urls(artist: str, album: str, results: list[dict]) -> list[str]:
    """Ordered, de-duplicated cover-art URLs from MusicBrainz release results + iTunes.

    Shared by the Check Album Covers tool and the library album cover lookup so both
    discover artwork the same way.
    """
    urls = [str(result.get("cover_art_url")) for result in results if result.get("cover_art_url")]
    itunes_url = itunes_album_artwork(artist, album)
    if itunes_url:
        urls.append(itunes_url)
    seen = set()
    unique_urls = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    return unique_urls


def deezer_artist_image_urls(name: str) -> list[str]:
    """Ordered artist photo URLs from Deezer's public search API (no API key)."""
    try:
        response = httpx.get(
            "https://api.deezer.com/search/artist",
            params={"q": name, "limit": 5},
            timeout=10,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    results = response.json().get("data", []) or []
    normalized = normalize(name)
    ordered: list[str] = []
    # Prefer an exact-ish name match, then fall back to the first result.
    for prefer_match in (True, False):
        for result in results:
            if prefer_match and normalize(result.get("name")) != normalized:
                continue
            for key in ("picture_xl", "picture_big", "picture_medium", "picture"):
                url = result.get(key)
                if url and url not in ordered:
                    ordered.append(url)
                    break
    return ordered


def artist_image_candidate_urls(name: str) -> list[str]:
    """De-duplicated artist photo URLs (currently Deezer's keyless search API)."""
    seen: set[str] = set()
    out: list[str] = []
    for url in deezer_artist_image_urls(name):
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
