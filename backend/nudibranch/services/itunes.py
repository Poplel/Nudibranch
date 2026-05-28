import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from nudibranch.services.app_log import write_app_log

ITUNES_BASE = "https://itunes.apple.com"
ITUNES_TIMEOUT = 10


def _art_url(artwork_url_100: str | None) -> str | None:
    if not artwork_url_100:
        return None
    return re.sub(r"\d+x\d+bb", "600x600bb", artwork_url_100)


def _normalize_artist(r: dict) -> dict:
    return {
        "id": str(r["artistId"]),
        "name": r.get("artistName", ""),
        "disambiguation": r.get("primaryGenreName", ""),
        "image_url": None,
        "albums": [],
    }


def _normalize_album(r: dict) -> dict:
    return {
        "id": str(r["collectionId"]),
        "title": r.get("collectionName", ""),
        "artist": r.get("artistName", ""),
        "artist_id": str(r.get("artistId") or ""),
        "date": (r.get("releaseDate") or "")[:10],
        "track_count": r.get("trackCount", 0),
        "cover_art_url": _art_url(r.get("artworkUrl100")),
        "cover_art_urls": [_art_url(r.get("artworkUrl100"))] if r.get("artworkUrl100") else [],
        "tracks": [],
        "source": "itunes",
    }


def _normalize_track(r: dict) -> dict:
    return {
        "id": str(r.get("trackId") or ""),
        "title": r.get("trackName", ""),
        "track_number": r.get("trackNumber"),
        "disc_number": r.get("discNumber"),
        "length": r.get("trackTimeMillis"),
        "duration_ms": r.get("trackTimeMillis"),
        "musicbrainz_recording_id": None,
    }


def artist_search(query: str, limit: int = 5) -> list[dict]:
    try:
        resp = httpx.get(f"{ITUNES_BASE}/search", params={
            "term": query, "entity": "musicArtist",
            "attribute": "artistTerm", "limit": limit,
        }, timeout=ITUNES_TIMEOUT)
        resp.raise_for_status()
        return [_normalize_artist(r) for r in resp.json().get("results", []) if r.get("wrapperType") == "artist"]
    except Exception as error:
        write_app_log("iTunes artist search failed", level="warning", feature="discover", error=str(error))
        return []


def artist_albums(artist_id: str, limit: int = 200) -> list[dict]:
    """Fetch all albums for a given iTunes artist ID, sorted newest-first."""
    try:
        resp = httpx.get(f"{ITUNES_BASE}/lookup", params={
            "id": artist_id, "entity": "album", "limit": limit,
        }, timeout=ITUNES_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        albums = [_normalize_album(r) for r in results if r.get("wrapperType") == "collection"]
        return sorted(albums, key=lambda a: a.get("date") or "", reverse=True)
    except Exception as error:
        write_app_log("iTunes artist albums fetch failed", level="warning", feature="discover", error=str(error))
        return []


def album_tracks(album_id: str) -> list[dict]:
    """Fetch all tracks for a given iTunes album ID, sorted by disc/track number."""
    try:
        resp = httpx.get(f"{ITUNES_BASE}/lookup", params={
            "id": album_id, "entity": "song",
        }, timeout=ITUNES_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        tracks = [
            _normalize_track(r) for r in results
            if r.get("wrapperType") == "track" and r.get("kind") == "song"
        ]
        return sorted(tracks, key=lambda t: (t.get("disc_number") or 1, t.get("track_number") or 999))
    except Exception as error:
        write_app_log("iTunes album tracks fetch failed", level="warning", feature="discover", error=str(error))
        return []


def album_search(query: str, limit: int = 20) -> list[dict]:
    try:
        resp = httpx.get(f"{ITUNES_BASE}/search", params={
            "term": query, "entity": "album", "limit": limit,
        }, timeout=ITUNES_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [_normalize_album(r) for r in results if r.get("wrapperType") == "collection"]
    except Exception as error:
        write_app_log("iTunes album search failed", level="warning", feature="discover", error=str(error))
        return []


def track_search(query: str, limit: int = 20) -> list[dict]:
    """Search songs. Each result includes artist_id, album_id, cover_art_url for grouping."""
    try:
        resp = httpx.get(f"{ITUNES_BASE}/search", params={
            "term": query, "entity": "song", "limit": limit,
        }, timeout=ITUNES_TIMEOUT)
        resp.raise_for_status()
        tracks = []
        for r in resp.json().get("results", []):
            if r.get("wrapperType") != "track" or r.get("kind") != "song":
                continue
            track = _normalize_track(r)
            track["album_id"] = str(r.get("collectionId") or "")
            track["album"] = r.get("collectionName", "")
            track["artist_id"] = str(r.get("artistId") or "")
            track["artist"] = r.get("artistName", "")
            track["album_date"] = (r.get("releaseDate") or "")[:10]
            track["album_track_count"] = r.get("trackCount", 0)
            track["cover_art_url"] = _art_url(r.get("artworkUrl100"))
            tracks.append(track)
        return tracks
    except Exception as error:
        write_app_log("iTunes track search failed", level="warning", feature="discover", error=str(error))
        return []


def discover_music(query: str, type: str = "all") -> dict:
    """Build the Discover tree using iTunes as the data source."""
    query = query.strip()
    if not query:
        return {"artists": [], "albums": [], "tracks": [], "focus": None}

    write_app_log("Discover search started", feature="discover", query=query, type=type, source="itunes")
    artist_map: dict[str, dict] = {}

    if type in ("all", "artist"):
        limit = 5 if type == "artist" else 3
        raw_artists = artist_search(query, limit=limit)
        with ThreadPoolExecutor(max_workers=max(len(raw_artists), 1)) as pool:
            futures = {pool.submit(artist_albums, a["id"]): a for a in raw_artists}
            for future in as_completed(futures):
                artist = futures[future]
                artist["albums"] = future.result()
                artist_map[artist["id"]] = artist
        write_app_log("Discover artist search completed", feature="discover", query=query, artists=len(raw_artists))

    if type in ("all", "album"):
        limit = 20 if type == "album" else 5
        for album in album_search(query, limit=limit):
            aid = album["artist_id"] or f"synth-{album.get('artist', '')}"
            if aid not in artist_map:
                artist_map[aid] = {
                    "id": aid, "name": album.get("artist", "Unknown"),
                    "disambiguation": "", "image_url": None, "albums": [],
                }
            artist_map[aid]["albums"].append(album)
        write_app_log("Discover album search completed", feature="discover", query=query)

    if type in ("all", "track"):
        limit = 20 if type == "track" else 8
        album_map: dict[str, dict] = {}
        for track in track_search(query, limit=limit):
            aid = track.get("artist_id") or f"synth-{track.get('artist', '')}"
            alb_id = track.get("album_id") or f"synth-{aid}-{track.get('album', '')}"
            if aid not in artist_map:
                artist_map[aid] = {
                    "id": aid, "name": track.get("artist", "Unknown"),
                    "disambiguation": "", "image_url": None, "albums": [],
                }
            if alb_id not in album_map:
                alb = {
                    "id": alb_id, "title": track.get("album", ""),
                    "artist": track.get("artist", ""), "artist_id": aid,
                    "date": track.get("album_date", ""),
                    "track_count": track.get("album_track_count", 0),
                    "cover_art_url": track.get("cover_art_url"),
                    "cover_art_urls": [track["cover_art_url"]] if track.get("cover_art_url") else [],
                    "tracks": [], "source": "itunes",
                }
                album_map[alb_id] = alb
                artist_map[aid]["albums"].append(alb)
            clean = {k: track[k] for k in ("id", "title", "track_number", "disc_number", "length", "duration_ms", "musicbrainz_recording_id") if k in track}
            album_map[alb_id]["tracks"].append(clean)
        write_app_log("Discover track search completed", feature="discover", query=query)

    result = {"artists": list(artist_map.values()), "albums": [], "tracks": [], "focus": None}
    write_app_log("Discover search completed", feature="discover", query=query, artists=len(result["artists"]))
    return result
