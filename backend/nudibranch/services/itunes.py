import datetime
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from nudibranch.services.app_log import write_app_log

ITUNES_BASE = "https://itunes.apple.com"
ITUNES_TIMEOUT = 10
_PODCAST_SEARCH_CACHE: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_PODCAST_SEARCH_CACHE_SECONDS = 300


def _art_url(artwork_url_100: str | None) -> str | None:
    if not artwork_url_100:
        return None
    return re.sub(r"\d+x\d+bb", "1200x1200bb", artwork_url_100)


def _normalize_artist(r: dict) -> dict:
    return {
        "id": str(r["artistId"]),
        "name": r.get("artistName", ""),
        "disambiguation": r.get("primaryGenreName", ""),
        "image_url": None,
        "albums": [],
    }


_COLLECTION_TYPE_PRIORITY = {"album": 0, "ep": 1, "single": 2}


def _album_sort_key(a: dict) -> tuple:
    """Sort key: Album < EP < Single < other, then newest-first within each tier.

    ⚠️ The title suffix is checked first because **iTunes reports `collectionType: "Album"` for
    singles too**, so the field alone put three 2025 remix singles above every real album when
    browsing an artist.
    """
    title = (a.get("title") or "").lower()
    if _TRAILING_KIND.search(title) or title.endswith("- single"):
        priority = 2 if "single" in title[-10:] else 1
    else:
        priority = _COLLECTION_TYPE_PRIORITY.get((a.get("collection_type") or "").lower(), 3)
    date_str = a.get("date") or "0000-00-00"
    try:
        ts = datetime.date.fromisoformat(date_str).toordinal()
    except ValueError:
        ts = 0
    return (priority, -ts)


def _normalize_album(r: dict) -> dict:
    return {
        "id": str(r["collectionId"]),
        "title": r.get("collectionName", ""),
        "artist": r.get("artistName", ""),
        "artist_id": str(r.get("artistId") or ""),
        "date": (r.get("releaseDate") or "")[:10],
        "track_count": r.get("trackCount", 0),
        "collection_type": (r.get("collectionType") or "").lower(),
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
        return sorted(albums, key=_album_sort_key)
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


def podcast_search(query: str, limit: int = 25) -> list[dict]:
    """Search podcasts via the iTunes Search API (free, no key). Returns rows with the RSS
    feed URL so a result can be subscribed directly."""
    cache_key = (query.strip().casefold(), limit)
    cached = _PODCAST_SEARCH_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _PODCAST_SEARCH_CACHE_SECONDS:
        return [dict(row) for row in cached[1]]
    try:
        resp = httpx.get(f"{ITUNES_BASE}/search", params={
            "term": query, "country": "US", "media": "podcast", "entity": "podcast", "limit": limit,
        }, timeout=ITUNES_TIMEOUT)
        resp.raise_for_status()
        results = []
        for r in resp.json().get("results", []):
            feed_url = r.get("feedUrl")
            if not feed_url:
                continue
            results.append({
                "id": str(r.get("collectionId") or ""),
                "title": r.get("collectionName") or r.get("trackName") or "",
                "author": r.get("artistName") or "",
                "feed_url": feed_url,
                "artwork_url": _art_url(r.get("artworkUrl100") or r.get("artworkUrl60")),
                "store_url": r.get("collectionViewUrl"),
                "genre": r.get("primaryGenreName") or "",
                "episode_count": r.get("trackCount"),
            })
        _PODCAST_SEARCH_CACHE[cache_key] = (time.monotonic(), results)
        return [dict(row) for row in results]
    except Exception as error:
        write_app_log("iTunes podcast search failed", level="warning", feature="podcasts", error=str(error))
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
        # ⚠️ Search the conjunction variants too. iTunes matches title text literally, so "smoke and
        # mirrors" does not find *Smoke + Mirrors* — the album simply is not in the result set, and
        # no amount of ranking can rescue what was never fetched. One extra request, and only when
        # the query actually contains a conjunction.
        found = list(album_search(query, limit=limit))
        for variant in _conjunction_variants(query):
            found.extend(album_search(variant, limit=limit))
        # iTunes returns results in its own relevance/popularity order, and that is the only
        # popularity signal available here. Keeping the position is what separates a dozen records
        # all genuinely titled "Smoke and Mirrors" — they score identically, so without it the
        # winner is whichever happened to be inserted first.
        for position, album in enumerate(found):
            album.setdefault("_rank", position)
        for album in found:
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
        for track_position, track in enumerate(track_search(query, limit=limit)):
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
                    "_rank": track_position,
                }
                album_map[alb_id] = alb
                artist_map[aid]["albums"].append(alb)
            clean = {k: track[k] for k in ("id", "title", "track_number", "disc_number", "length", "duration_ms", "musicbrainz_recording_id") if k in track}
            album_map[alb_id]["tracks"].append(clean)
        write_app_log("Discover track search completed", feature="discover", query=query)

    # Deduplicate. An album arrives from both `album_search` and the track-search grouping carrying
    # different track lists (empty vs. only the songs that matched), and iTunes also returns the
    # same release under several collection ids — "Loud (Deluxe)" appeared twice in one result.
    # Key on title+artist rather than id so both cases collapse, keeping whichever copy knows more.
    for artist in artist_map.values():
        best_by_key: dict[str, dict] = {}
        for album in artist.get("albums") or []:
            key = f"{_norm(album.get('title', ''))}::{_norm(album.get('artist', ''))}"
            existing = best_by_key.get(key)
            if existing is None or _album_detail_rank(album) > _album_detail_rank(existing):
                # Keep the better search position across copies — the discarded one may be the copy
                # iTunes ranked highest, and that is the popularity signal.
                if existing is not None:
                    album["_rank"] = min(album.get("_rank", _UNRANKED), existing.get("_rank", _UNRANKED))
                best_by_key[key] = album
            else:
                existing["_rank"] = min(existing.get("_rank", _UNRANKED), album.get("_rank", _UNRANKED))
        artist["albums"] = list(best_by_key.values())

    return _rank_discover_results(artist_map, query)


def _conjunction_variants(query: str) -> list[str]:
    """Other ways the same title might be written: "a and b" ⇄ "a + b" ⇄ "a & b".

    Returns at most two extra spellings, and nothing at all when the query has no conjunction, so
    the common case costs no additional requests.
    """
    lowered = query.lower()
    variants: list[str] = []
    if re.search(r"\band\b", lowered):
        variants = [re.sub(r"\band\b", "+", lowered), re.sub(r"\band\b", "&", lowered)]
    elif "+" in query:
        variants = [query.replace("+", " and "), query.replace("+", "&")]
    elif "&" in query:
        variants = [query.replace("&", " and "), query.replace("&", "+")]
    return [re.sub(r"\s+", " ", v).strip() for v in variants]


def _album_detail_rank(album: dict) -> tuple[int, int]:
    """Which of two copies of the same release to keep: the one with real tracks attached, then the
    one claiming more of them."""
    return (len(album.get("tracks") or []), album.get("track_count") or 0)


def _norm(value: str) -> str:
    """Lowercase, collapse whitespace, and spell out `&`/`+` as "and".

    ⚠️ The conjunction rule is load-bearing, not cosmetic. Imagine Dragons' album is *Smoke +
    Mirrors*; searching "smoke and mirrors" left the `and` unexplained, which cost enough score to
    push the band below the noise cutoff and drop it from the results entirely. Titles use `&`, `+`
    and `and` interchangeably and users type whichever they remember, so they have to compare equal.
    """
    value = re.sub(r"[&+]", " and ", (value or "").lower())
    return re.sub(r"\s+", " ", value.strip())


def _tokens(value: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", (value or "").lower()) if t}


_EDITION_SUFFIX = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_TRAILING_KIND = re.compile(r"\s*-\s*(single|ep)\s*$", re.IGNORECASE)


def _base_title(title: str) -> str:
    """"Loud (Deluxe)" → "loud"; "Umbrella (feat. JAY-Z) [Remixes]" → "umbrella".

    Scoring the raw title is what made "Loud rihanna" miss: the release is called *Loud (Deluxe)*,
    so the query never accounted for all of its words and the album never ranked as the answer.
    """
    return _norm(_TRAILING_KIND.sub("", _EDITION_SUFFIX.sub("", title or "")))


def relevance(query: str, title: str, artist: str = "") -> float:
    """How well one candidate explains what the user typed. **Lower is better**; negative means it
    accounts for every word typed *and* its own name was largely typed.

    This exists because ranking by artist name alone answers the wrong question. Searching
    "Loud rihanna" matched the *artist* Rihanna and then listed the album Loud somewhere among her
    114 releases, so the thing actually asked for was the hardest thing on screen to find. Scoring
    albums and tracks with the same function lets the best match float up whatever kind it is.

    Two terms, and the second one is what stops long titles from cheating: the fraction of typed
    words the candidate cannot explain, minus a reward for how much of the candidate's *own* name
    was typed. Without that second term a bare "rihanna" ranked "Wild Thoughts (feat. Rihanna …)"
    above her actual albums, purely because the query appeared inside a longer title.
    """
    q, base, a = _norm(query), _base_title(title), _norm(artist)
    query_tokens, base_tokens, artist_tokens = _tokens(q), _tokens(base), _tokens(a)
    if not query_tokens or not base_tokens:
        return 99.0
    missing = query_tokens - (base_tokens | artist_tokens)
    score = len(missing) / len(query_tokens)
    score -= 0.4 * (len(base_tokens & query_tokens) / len(base_tokens))
    if artist_tokens and artist_tokens <= query_tokens:
        score -= 0.1
    if base == q:
        score -= 0.5
    return score


# A hit at least this good is treated as "this is what they asked for": it is expanded, its tracks
# are fetched up front, and weak artists are dropped around it.
_FOCUS_THRESHOLD = -0.2
# Artists scoring worse than the best hit by more than this are noise for this query and dropped.
_ARTIST_KEEP_BAND = 0.75


_KIND_RANK = {"album": 0, "track": 1, "artist": 2}
# Position given to anything that did not come from a ranked search result — an artist's own back
# catalogue, say. Sorts after everything iTunes actually ranked.
_UNRANKED = 999


def _rank_discover_results(artist_map: dict[str, dict], query: str) -> dict:
    """Orders the tree by what was actually searched for, and marks the single best hit as `focus`."""
    # (score, iTunes search position, -parent track count, kind rank, kind, artist, album, track)
    candidates: list[tuple] = []

    for artist in artist_map.values():
        artist_score = relevance(query, artist.get("name", ""))
        artist["_score"] = artist_score
        candidates.append((artist_score, artist.get("_rank", _UNRANKED), 0, _KIND_RANK["artist"], "artist", artist, None, None))

        for album in artist.get("albums") or []:
            album_score = relevance(query, album.get("title", ""), album.get("artist", ""))
            album["_score"] = album_score
            size = album.get("track_count") or len(album.get("tracks") or [])
            # An album *named exactly what was typed* is the most likely intent, so it edges out a
            # song of the same name sitting on someone else's record: searching "good girl gone bad"
            # otherwise focused a Tarrus Riley track, because its parent album is longer than
            # Rihanna's and parent size breaks the tie. Small enough to only decide genuine ties.
            exact = _base_title(album.get("title", "")) == _norm(query)
            rank = album.get("_rank", _UNRANKED)
            candidates.append(
                (album_score - (0.05 if exact else 0.0), rank, -size, _KIND_RANK["album"], "album", artist, album, None)
            )
            for track in album.get("tracks") or []:
                track_score = relevance(query, track.get("title", ""), album.get("artist", ""))
                # ⚠️ Tie-break on the parent album's size *before* kind. "umbrella rihanna" matches
                # the song and a pile of remix singles literally named "Umbrella" equally well;
                # preferring the bigger parent picks the song on its real album, which is what was
                # asked for, instead of a one-track remix release.
                candidates.append((track_score, rank, -size, _KIND_RANK["track"], "track", artist, album, track))

        # Matched album first, then the existing Album < EP < Single, newest-first ordering.
        artist["albums"] = sorted(
            artist.get("albums") or [],
            key=lambda al: (round(al.get("_score", 99.0), 2), *_album_sort_key(al)),
        )
        artist["_best"] = min(
            [artist_score] + [al.get("_score", 99.0) for al in artist.get("albums") or []]
        )
        # Best search position anywhere under this artist, so a dozen artists tying on an exact
        # title are ordered by how relevant iTunes thought each one was.
        artist["_bestRank"] = min(
            [artist.get("_rank", _UNRANKED)]
            + [al.get("_rank", _UNRANKED) for al in artist.get("albums") or []]
        )

    artists = sorted(
        artist_map.values(),
        key=lambda a: (a.get("_best", 99.0), a.get("_bestRank", _UNRANKED), a.get("_score", 99.0)),
    )
    best = min(candidates, key=lambda c: (round(c[0], 3), c[1], c[2], c[3])) if candidates else None

    focus = None
    if best and best[0] <= _FOCUS_THRESHOLD:
        _, _, _, _, kind, artist, album, track = best
        # The one extra request that makes this useful: a focused album arrives with its **whole**
        # track list, so the user sees the songs without expanding anything. Search results only
        # ever carry the tracks that happened to match the query text, which for an album search is
        # usually none — that is why "the tracks aren't all listed under it".
        if album is not None and len(album.get("tracks") or []) < (album.get("track_count") or 0):
            fetched = album_tracks(album["id"])
            if fetched:
                album["tracks"] = fetched
        focus = {
            "kind": kind,
            "artist_id": artist.get("id"),
            "album_id": album.get("id") if album else None,
            "track_id": track.get("id") if track else None,
        }
        # Put the answer where it can be seen. Scores tie often — "umbrella rihanna" matches the
        # song and a shelf of remix singles equally — and the tie-break that picks the *focus* is
        # not the one that orders the list, so without this the focused album could sit below the
        # releases it beat.
        if album is not None:
            artist["albums"] = [album] + [al for al in artist["albums"] if al is not album]
        artists = [artist] + [a for a in artists if a is not artist]
        # Drop artists this query cannot justify. The focused artist always survives, so a strong
        # hit can never filter away the thing it matched.
        cutoff = best[0] + _ARTIST_KEEP_BAND
        artists = [
            a for a in artists
            if a.get("_best", 99.0) <= cutoff or a.get("id") == artist.get("id")
        ]

    for artist in artists:
        # Internal scoring keys never reach the client.
        for key in ("_score", "_best", "_bestRank", "_rank"):
            artist.pop(key, None)
        for album in artist.get("albums") or []:
            album.pop("_score", None)
            album.pop("_rank", None)

    write_app_log(
        "Discover search completed", feature="discover", query=query,
        artists=len(artists), focus=(focus or {}).get("kind"),
    )
    return {"artists": artists, "albums": [], "tracks": [], "focus": focus}
