"""Album suggestions for the wishlist request page: albums the library does not have.

Two sources are mixed, alternating: albums by artists *similar* to the caller's favourites that are not
in the library at all (cached ListenBrainz `artist_similarity` rows), and *missing* albums from artists
the caller already listens to. Everything is seeded by the caller's taste, not the whole library.

iTunes is only reached for artists missing from `discover_artist_cache`, at most MAX_LOOKUPS per call, so
a cold cache returns fewer albums rather than a slow page. Any iTunes failure just yields fewer albums.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from nudibranch.db.models import (
    Album,
    Artist,
    ArtistSimilarity,
    DiscoverArtistCache,
    PlayEvent,
    Playlist,
    PlaylistTrack,
    Track,
    User,
    WishlistItem,
)
from nudibranch.services import itunes

TASTE_ARTISTS = 15
RECENT_DAYS = 90
FAVORITE_WEIGHT = 3.0
WISHLIST_WEIGHT = 2.0
MAX_LOOKUPS = 8
#: Wall-clock budget for uncached lookups in one request. Each iTunes call may take its full 10 s
#: timeout, so the count cap alone could hold the page for minutes when iTunes is slow.
LOOKUP_BUDGET_SECONDS = 6.0
CACHE_TTL = timedelta(days=7)
FAILED_TTL = timedelta(days=1)


def _loads_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def taste_profile(session: Session, user: User) -> dict[str, float]:
    """library artist id -> weight: plays (last 90 days count double) + 3 per Favorites track + 2 per
    wishlist row in the last 90 days. Top TASTE_ARTISTS only."""
    since = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    weights: dict[str, float] = {}

    plays = func.sum(case((PlayEvent.played_at >= since, 2.0), else_=1.0))
    for artist_id, value in session.execute(
        select(Album.artist_id, plays)
        .select_from(PlayEvent)
        .join(Track, Track.id == PlayEvent.track_id)
        .join(Album, Album.id == Track.album_id)
        .where(PlayEvent.user_id == user.id)
        .group_by(Album.artist_id)
    ):
        weights[artist_id] = weights.get(artist_id, 0.0) + float(value or 0)

    favorites_id = session.scalar(
        select(Playlist.id).where(Playlist.user_id == user.id, Playlist.protected.is_(True)).limit(1)
    )
    if favorites_id:
        for artist_id, count in session.execute(
            select(Album.artist_id, func.count(PlaylistTrack.id))
            .select_from(PlaylistTrack)
            .join(Track, Track.id == PlaylistTrack.track_id)
            .join(Album, Album.id == Track.album_id)
            .where(PlaylistTrack.playlist_id == favorites_id)
            .group_by(Album.artist_id)
        ):
            weights[artist_id] = weights.get(artist_id, 0.0) + FAVORITE_WEIGHT * count

    by_name: dict[str, str] = {}
    for artist_id, name in session.execute(select(Artist.id, Artist.name)):
        by_name.setdefault(itunes._norm(name), artist_id)
    for artist_name, count in session.execute(
        select(WishlistItem.artist, func.count(WishlistItem.id))
        .where(WishlistItem.user_id == user.id, WishlistItem.created_at >= since)
        .group_by(WishlistItem.artist)
    ):
        artist_id = by_name.get(itunes._norm(artist_name))
        if artist_id:
            weights[artist_id] = weights.get(artist_id, 0.0) + WISHLIST_WEIGHT * count

    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:TASTE_ARTISTS]
    return {artist_id: w for artist_id, w in top if w > 0}


def _weighted_order(items: list[tuple[str, float]], rng: random.Random) -> list[str]:
    """Weighted shuffle without replacement (Efraimidis-Spirakis), so Refresh varies."""
    keyed = sorted(items, key=lambda row: rng.random() ** (1.0 / max(row[1], 1e-6)), reverse=True)
    return [key for key, _ in keyed]


class _Resolver:
    """iTunes artist resolution through `discover_artist_cache`, with a per-request lookup cap."""

    def __init__(self, session: Session):
        self.session = session
        self.lookups = 0
        self.started = time.monotonic()

    def albums_for(self, name: str) -> list[dict] | None:
        """Full-album list for the artist, or None when it is not cached and the cap is spent."""
        key = itunes._norm(name)
        if not key:
            return []
        now = datetime.now(timezone.utc)
        row = self.session.get(DiscoverArtistCache, key)
        if row is not None:
            albums = _loads_list(row.albums)
            ttl = CACHE_TTL if (row.itunes_artist_id and albums) else FAILED_TTL
            if now - _aware(row.fetched_at) < ttl:
                return [a for a in albums if isinstance(a, dict)]
        if self.lookups >= MAX_LOOKUPS or time.monotonic() - self.started > LOOKUP_BUDGET_SECONDS:
            return None
        self.lookups += 1
        artist_id: str | None = None
        albums: list[dict] = []
        try:
            for hit in itunes.artist_search(name, limit=1)[:1]:
                if itunes._norm(hit.get("name", "")) == key:
                    artist_id = hit["id"]
            if artist_id:
                albums = [a for a in itunes.artist_albums(artist_id) if itunes._album_sort_key(a)[0] == 0]
        except Exception:  # noqa: BLE001 - an iTunes failure only means fewer suggestions
            artist_id, albums = None, []
        if row is None:
            row = DiscoverArtistCache(name_key=key)
            self.session.add(row)
        row.itunes_artist_id = artist_id
        row.albums = json.dumps(albums)
        row.fetched_at = now
        return albums


def suggest_albums(
    session: Session,
    user: User,
    limit: int,
    exclude: set[str],
    rng: random.Random | None = None,
) -> list[dict]:
    """Up to `limit` iTunes albums (the `/discover/search` album shape), one per artist."""
    rng = rng or random.Random()
    taste = taste_profile(session, user)
    if not taste or limit < 1:
        return []

    library_artists = session.execute(select(Artist.id, Artist.name, Artist.musicbrainz_id)).all()
    name_of = {artist_id: name for artist_id, name, _ in library_artists}
    library_names = {itunes._norm(name) for name in name_of.values()}
    library_mbids = {mbid for _, _, mbid in library_artists if mbid}

    owned: set[tuple[str, str]] = set()
    for artist_name, title in session.execute(
        select(Artist.name, Album.title).join(Album, Album.artist_id == Artist.id)
    ):
        owned.add((itunes._norm(artist_name), itunes._base_title(title)))
    for artist_name, album_name in session.execute(
        select(WishlistItem.artist, WishlistItem.album).where(
            WishlistItem.user_id == user.id,
            WishlistItem.album.is_not(None),
            WishlistItem.status.not_in(("rejected", "removed")),
        )
    ):
        owned.add((itunes._norm(artist_name), itunes._base_title(album_name)))

    # New artists: similar to the taste artists, not in the library.
    new_scores: dict[str, float] = {}
    new_names: dict[str, str] = {}
    similarity = {
        row.artist_id: row for row in session.scalars(select(ArtistSimilarity).where(ArtistSimilarity.artist_id.in_(taste)))
    }
    for artist_id, weight in taste.items():
        row = similarity.get(artist_id)
        if not row:
            continue
        entries = [e for e in _loads_list(row.similar) if isinstance(e, dict) and e.get("name")]
        top = max((float(e.get("score") or 0.0) for e in entries), default=0.0)
        if top <= 0:
            continue
        for entry in entries:
            key = itunes._norm(str(entry["name"]))
            if not key or key in library_names or str(entry.get("mbid") or "") in library_mbids:
                continue
            new_scores[key] = new_scores.get(key, 0.0) + weight * float(entry.get("score") or 0.0) / top
            new_names.setdefault(key, str(entry["name"]))

    new_order = [new_names[k] for k in _weighted_order(list(new_scores.items()), rng)]
    gap_order = [name_of[a] for a in _weighted_order(list(taste.items()), rng) if a in name_of]

    resolver = _Resolver(session)
    used_artists: set[str] = set()
    picked: list[dict] = []
    queues = [new_order, gap_order]
    turn = 0
    try:
        while len(picked) < limit and any(queues):
            queue = queues[turn % 2]
            turn += 1
            if not queue:
                continue
            name = queue.pop(0)
            key = itunes._norm(name)
            if key in used_artists:
                continue
            albums = resolver.albums_for(name)
            if albums is None:
                continue  # cap spent for uncached artists; cached ones further down may still serve
            options: dict[str, dict] = {}
            for album in albums:
                if album.get("id") in exclude or itunes._norm(album.get("artist", "")) != key:
                    continue
                base = itunes._base_title(album.get("title", ""))
                if not base or (key, base) in owned:
                    continue
                options.setdefault(base, album)
            if options:
                used_artists.add(key)
                picked.append(rng.choice(list(options.values())))
        session.commit()
    except Exception:  # noqa: BLE001 - never fail the page; return what we have
        session.rollback()
    return picked
