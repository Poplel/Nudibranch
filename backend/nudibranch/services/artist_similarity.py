"""Cached MusicBrainz genres and ListenBrainz similar artists, one row per artist.

Only the worker's `analyze_audio` task calls this; the suggestion engine reads the cached rows and
never touches the network. ListenBrainz is an enrichment: any failure (HTTP, timeout, a changed
response shape) is stored as `error` with an empty list, and `LISTENBRAINZ_ENABLED=false` skips the
call entirely. Local similarity works without either.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from nudibranch.core.config import get_settings
from nudibranch.db.models import Artist, ArtistSimilarity
from nudibranch.services.app_log import write_app_log
from nudibranch.services.metadata_lookup import USER_AGENT, is_valid_mbid, musicbrainz_get

LISTENBRAINZ_SIMILAR_URL = "https://labs.api.listenbrainz.org/similar-artists/json"
LISTENBRAINZ_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
)
LISTENBRAINZ_TIMEOUT_SECONDS = 10
#: Seconds between outbound requests (MusicBrainz asks for at most one per second).
REQUEST_PACING_SECONDS = 1.0


def fetch_genres(mbid: str) -> list[str]:
    """Genre names for an artist, most-voted first, lowercased."""
    response = musicbrainz_get(f"https://musicbrainz.org/ws/2/artist/{mbid}", {"inc": "genres", "fmt": "json"})
    genres = response.json().get("genres") or []
    genres = sorted(genres, key=lambda g: -(g.get("count") or 0))
    out: list[str] = []
    for genre in genres:
        name = str(genre.get("name") or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def fetch_similar(mbid: str) -> list[dict]:
    """ListenBrainz similar artists as [{mbid, name, score}]. Raises on any failure."""
    response = httpx.get(
        LISTENBRAINZ_SIMILAR_URL,
        params={"artist_mbids": mbid, "algorithm": LISTENBRAINZ_ALGORITHM},
        headers={"User-Agent": USER_AGENT},
        timeout=LISTENBRAINZ_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("unexpected ListenBrainz response shape")
    out: list[dict] = []
    for row in rows:
        candidate = str(row.get("artist_mbid") or "")
        if not is_valid_mbid(candidate) or candidate == mbid:
            continue
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        out.append({"mbid": candidate, "name": str(row.get("name") or ""), "score": score})
    return out


def refresh_artist(session: Session, artist: Artist) -> None:
    """Fetch and store genres + similar artists for one artist (paced ~1 req/s). Never raises for a
    network or parse failure: those land in the row's `error`."""
    mbid = artist.musicbrainz_id
    if not mbid or not is_valid_mbid(mbid):
        return
    genres: list[str] = []
    similar: list[dict] = []
    errors: list[str] = []
    try:
        genres = fetch_genres(mbid)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"genres: {exc}"[:150])
    if get_settings().listenbrainz_enabled:
        time.sleep(REQUEST_PACING_SECONDS)
        try:
            similar = fetch_similar(mbid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"listenbrainz: {exc}"[:150])
            write_app_log(f"ListenBrainz lookup failed for {artist.name}: {exc}", "info", feature="listenbrainz")
    row = session.get(ArtistSimilarity, artist.id)
    if row is None:
        row = ArtistSimilarity(artist_id=artist.id)
        session.add(row)
    row.genres = json.dumps(genres)
    row.similar = json.dumps(similar)
    row.fetched_at = datetime.now(timezone.utc)
    row.error = "; ".join(errors) or None
    session.commit()
    time.sleep(REQUEST_PACING_SECONDS)
