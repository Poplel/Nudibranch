"""Suggestion engine: library tracks that sound like a set of seed tracks.

`suggest(...)` is a pure read over the local library plus the cached feature tables the worker fills
(`track_features`, `artist_similarity`). It never touches the network, and it works on a library with
no analysis and no ListenBrainz data at all: a term with no data for a candidate contributes nothing
and its weight is redistributed over the terms that do have data.

Terms (each 0-1) and weights: artist 0.30, genre 0.30, audio 0.30, year 0.10.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from statistics import median

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nudibranch.db.models import Album, ArtistSimilarity, Artist, PlayEvent, PlaylistTrack, Track, TrackFeatures, User

W_ARTIST = 0.30
W_GENRE = 0.30
W_AUDIO = 0.30
W_YEAR = 0.10

MAX_SEEDS = 50
POOL_SIZE = 60
#: Distance scale for tempo (BPM) after octave folding.
BPM_SCALE = 50.0
YEAR_SCALE = 20.0
PLAY_HISTORY_LIMIT = 3000


def _loads_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _sample_evenly(items: list[str], limit: int) -> list[str]:
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def _artist_cooccurrence(
    session: Session, user: User, seed_artists: set[str]
) -> tuple[dict[str, dict[str, float]], bool]:
    """seed artist -> {artist -> share of baskets containing the seed artist that also contain it}.

    Baskets are every user's native playlists plus the caller's plays grouped by day. Returns
    (table, has_data); has_data is False when no basket contains a seed artist.
    """
    baskets: list[set[str]] = []
    playlist_ids = set(
        session.scalars(
            select(PlaylistTrack.playlist_id)
            .join(Track, Track.id == PlaylistTrack.track_id)
            .join(Album, Album.id == Track.album_id)
            .where(Album.artist_id.in_(seed_artists))
        )
    )
    if playlist_ids:
        by_playlist: dict[str, set[str]] = defaultdict(set)
        for playlist_id, artist_id in session.execute(
            select(PlaylistTrack.playlist_id, Album.artist_id)
            .join(Track, Track.id == PlaylistTrack.track_id)
            .join(Album, Album.id == Track.album_id)
            .where(PlaylistTrack.playlist_id.in_(playlist_ids))
        ):
            by_playlist[playlist_id].add(artist_id)
        baskets.extend(by_playlist.values())
    by_day: dict[str, set[str]] = defaultdict(set)
    for played_at, artist_id in session.execute(
        select(PlayEvent.played_at, Album.artist_id)
        .join(Track, Track.id == PlayEvent.track_id)
        .join(Album, Album.id == Track.album_id)
        .where(PlayEvent.user_id == user.id)
        .order_by(PlayEvent.played_at.desc())
        .limit(PLAY_HISTORY_LIMIT)
    ):
        by_day[played_at.date().isoformat() if played_at else ""].add(artist_id)
    baskets.extend(by_day.values())

    containing: Counter[str] = Counter()
    together: dict[str, Counter[str]] = defaultdict(Counter)
    for basket in baskets:
        present = basket & seed_artists
        for seed in present:
            containing[seed] += 1
            together[seed].update(basket)
    table = {
        seed: {artist: count / containing[seed] for artist, count in counts.items() if artist != seed}
        for seed, counts in together.items()
    }
    return table, bool(containing)


def _audio_matrix(features: list[tuple[float, float, float] | None]) -> np.ndarray:
    arr = np.full((len(features), 3), np.nan, dtype=np.float64)
    for i, row in enumerate(features):
        if row is not None:
            arr[i] = [np.nan if v is None else v for v in row]
    return arr


def _audio_similarity(candidates: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    """1 - normalized distance to the nearest seed; NaN where no dimension is comparable.

    Tempo is octave-folded: the candidate's bpm, bpm x 2 and bpm / 2 are all compared with the seed's
    and the smallest difference wins.
    """
    if len(candidates) == 0 or len(seeds) == 0:
        return np.full(len(candidates), np.nan)
    c = candidates[:, None, :]
    s = seeds[None, :, :]
    bpm_c, bpm_s = c[..., 0], s[..., 0]
    d_bpm = np.minimum.reduce([np.abs(bpm_c - bpm_s), np.abs(bpm_c * 2 - bpm_s), np.abs(bpm_c / 2 - bpm_s)])
    d_bpm = np.minimum(d_bpm / BPM_SCALE, 1.0)
    d_energy = np.abs(c[..., 1] - s[..., 1])
    d_bright = np.abs(c[..., 2] - s[..., 2])
    stacked = np.stack([d_bpm, d_energy, d_bright], axis=-1)  # (N, S, 3)
    valid = ~np.isnan(stacked)
    counts = valid.sum(axis=-1)
    sums = np.where(valid, stacked, 0.0).sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        dist = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)  # (N, S)
    all_nan = np.all(np.isnan(dist), axis=1)
    dist = np.where(np.isnan(dist), np.inf, dist)
    nearest = dist.min(axis=1)
    return np.where(all_nan, np.nan, 1.0 - nearest)


def suggest(
    session: Session,
    user: User,
    seed_track_ids: list[str],
    exclude_track_ids: list[str] | set[str] | None,
    limit: int,
    rng: random.Random | None = None,
) -> list[Track]:
    """Up to `limit` library tracks resembling the seeds, never a seed or an excluded id."""
    rng = rng or random.Random()
    limit = max(0, limit)
    seed_ids = _sample_evenly(list(dict.fromkeys(seed_track_ids)), MAX_SEEDS)
    excluded = set(seed_track_ids) | set(exclude_track_ids or ())
    if not seed_ids or limit == 0:
        return []

    # One bulk read of the whole candidate table, columns only (never a Track object per row).
    rows = session.execute(
        select(Track.id, Album.artist_id).join(Album, Album.id == Track.album_id).where(Track.path.is_not(None))
    ).all()
    artist_of: dict[str, str] = {track_id: artist_id for track_id, artist_id in rows}
    seeds = [t for t in seed_ids if t in artist_of]
    if not seeds:
        return []
    candidate_ids = [t for t in artist_of if t not in excluded]
    if not candidate_ids:
        return []

    features: dict[str, TrackFeatures] = {f.track_id: f for f in session.scalars(select(TrackFeatures))}
    similarity: dict[str, ArtistSimilarity] = {s.artist_id: s for s in session.scalars(select(ArtistSimilarity))}
    mbid_of: dict[str, str] = {
        artist_id: mbid
        for artist_id, mbid in session.execute(select(Artist.id, Artist.musicbrainz_id)).all()
        if mbid
    }
    artist_for_mbid = {mbid: artist_id for artist_id, mbid in mbid_of.items()}

    def artist_genres(artist_id: str) -> list[str]:
        row = similarity.get(artist_id)
        return [str(g) for g in _loads_list(row.genres)] if row else []

    def track_genres(track_id: str) -> set[str]:
        row = features.get(track_id)
        tags = {str(g) for g in _loads_list(row.genres)} if row else set()
        return tags | set(artist_genres(artist_of[track_id]))

    seed_artists = {artist_of[t] for t in seeds}

    # --- artist term inputs ---------------------------------------------------------------------
    lb_score: dict[str, float] = {}  # artist id -> best normalized ListenBrainz score vs any seed artist
    for seed_artist in seed_artists:
        row = similarity.get(seed_artist)
        if not row:
            continue
        entries = [e for e in _loads_list(row.similar) if isinstance(e, dict)]
        top = max((float(e.get("score") or 0.0) for e in entries), default=0.0)
        if top <= 0:
            continue
        for entry in entries:
            other = artist_for_mbid.get(str(entry.get("mbid") or ""))
            if other:
                lb_score[other] = max(lb_score.get(other, 0.0), float(entry.get("score") or 0.0) / top)
    # Reverse direction: a candidate's own list naming a seed artist counts too.
    seed_mbids = {mbid_of[a] for a in seed_artists if a in mbid_of}
    if seed_mbids:
        for artist_id, row in similarity.items():
            if artist_id in seed_artists:
                continue
            entries = [e for e in _loads_list(row.similar) if isinstance(e, dict)]
            top = max((float(e.get("score") or 0.0) for e in entries), default=0.0)
            if top <= 0:
                continue
            for entry in entries:
                if str(entry.get("mbid") or "") in seed_mbids:
                    lb_score[artist_id] = max(lb_score.get(artist_id, 0.0), float(entry.get("score") or 0.0) / top)
    cooccurrence, has_cooccurrence = _artist_cooccurrence(session, user, seed_artists)
    has_artist_data = bool(lb_score) or has_cooccurrence

    # --- genre term inputs ----------------------------------------------------------------------
    histogram: Counter[str] = Counter()
    for t in seeds:
        histogram.update(track_genres(t))
    max_count = max(histogram.values(), default=0)
    seed_weight = {g: c / max_count for g, c in histogram.items()} if max_count else {}

    # --- audio / year inputs --------------------------------------------------------------------
    def audio_row(track_id: str):
        f = features.get(track_id)
        if f is None or f.error:
            return None
        if f.bpm is None and f.energy is None and f.brightness is None:
            return None
        return (f.bpm, f.energy, f.brightness)

    seed_audio = _audio_matrix([audio_row(t) for t in seeds])
    seed_audio = seed_audio[~np.all(np.isnan(seed_audio), axis=1)]
    cand_audio = _audio_matrix([audio_row(t) for t in candidate_ids])
    audio_sim = _audio_similarity(cand_audio, seed_audio)
    seed_years = [features[t].year for t in seeds if t in features and features[t].year]
    median_year = median(seed_years) if seed_years else None

    # --- score ----------------------------------------------------------------------------------
    scored: list[tuple[str, float]] = []
    for i, track_id in enumerate(candidate_ids):
        artist_id = artist_of[track_id]
        terms: list[tuple[float, float]] = []

        if artist_id in seed_artists:
            terms.append((W_ARTIST, 1.0))
        elif has_artist_data:
            co = max((table.get(artist_id, 0.0) for table in cooccurrence.values()), default=0.0)
            terms.append((W_ARTIST, min(1.0, max(lb_score.get(artist_id, 0.0), co))))

        if seed_weight:
            genres = track_genres(track_id)
            if genres:
                union = set(seed_weight) | genres
                num = sum(min(1.0 if g in genres else 0.0, seed_weight.get(g, 0.0)) for g in union)
                den = sum(max(1.0 if g in genres else 0.0, seed_weight.get(g, 0.0)) for g in union)
                terms.append((W_GENRE, num / den if den else 0.0))

        if not np.isnan(audio_sim[i]):
            terms.append((W_AUDIO, float(audio_sim[i])))

        if median_year is not None:
            f = features.get(track_id)
            if f is not None and f.year:
                terms.append((W_YEAR, max(0.0, 1.0 - abs(f.year - median_year) / YEAR_SCALE)))

        weight = sum(w for w, _ in terms)
        score = sum(w * v for w, v in terms) / weight if weight else 0.0
        scored.append((track_id, score))

    scored.sort(key=lambda row: row[1], reverse=True)
    chosen = _pick(scored, artist_of, limit, rng)
    if not chosen:
        return []
    loaded = {
        t.id: t
        for t in session.scalars(
            select(Track).where(Track.id.in_(chosen)).options(selectinload(Track.album).selectinload(Album.artist))
        )
    }
    return [loaded[t] for t in chosen if t in loaded]


def _pick(scored: list[tuple[str, float]], artist_of: dict[str, str], limit: int, rng: random.Random) -> list[str]:
    """Sample `limit` ids without replacement from the top POOL_SIZE, weighted by score squared, with
    at most one per artist (relaxed to two, then widened to the rest of the library, when the pool is
    too thin to fill the request)."""

    def draw(pool: list[tuple[str, float]]) -> list[str]:
        # Efraimidis-Spirakis: u^(1/w) keys give a weighted shuffle without replacement.
        keyed = sorted(
            pool, key=lambda row: rng.random() ** (1.0 / (row[1] * row[1] + 1e-6)), reverse=True
        )
        return [track_id for track_id, _ in keyed]

    top = scored[:POOL_SIZE]
    rest = scored[POOL_SIZE:]
    chosen: list[str] = []
    per_artist: Counter[str] = Counter()
    for pool, cap in ((top, 1), (top, 2), (rest, 1), (rest, 2), (top + rest, 10**9)):
        if len(chosen) >= limit:
            break
        taken = set(chosen)
        for track_id in draw([row for row in pool if row[0] not in taken]):
            if len(chosen) >= limit:
                break
            artist = artist_of[track_id]
            if per_artist[artist] >= cap:
                continue
            chosen.append(track_id)
            per_artist[artist] += 1
    return chosen


# --------------------------------------------------------------------------------------------------
# Smart shuffle planning (the one implementation of the spacing rule; clients only apply the result)
# --------------------------------------------------------------------------------------------------

#: How many items past the current one a plan looks at.
PLAN_HORIZON = 40
#: Seeds are weighted toward this many items either side of the current one.
PLAN_SEED_WINDOW = 20


def plan_insert_indexes(items: list[dict], current_index: int, every: int) -> list[int]:
    """Indexes (into `items` as sent) before which a suggestion should be inserted.

    Walk from `current_index + 1` up to PLAN_HORIZON items ahead, counting consecutive non-smart
    tracks since the last smart item; the count starts from the current item backwards, so a smart
    track that just played resets it. Episodes neither count nor break a run. When the run reaches
    `every`, a suggestion goes straight after that track and the run resets, unless the next item is
    already smart (then the list is already spaced there). Ascending and duplicate-free.
    """
    if not items or every < 1:
        return []
    current_index = max(0, min(current_index, len(items) - 1))

    run = 0
    for j in range(current_index, -1, -1):
        entry = items[j]
        if entry.get("type") != "track":
            continue
        if entry.get("smart"):
            break
        run += 1

    def next_is_smart(after: int) -> bool:
        following = next((items[k] for k in range(after + 1, len(items)) if items[k].get("type") == "track"), None)
        return following is not None and bool(following.get("smart"))

    inserts: list[int] = []
    if run >= every and not next_is_smart(current_index):
        inserts.append(current_index + 1)  # the run is already long enough: next in line
        run = 0
    last = min(len(items) - 1, current_index + PLAN_HORIZON)
    for i in range(current_index + 1, last + 1):
        entry = items[i]
        if entry.get("type") != "track":
            continue
        if entry.get("smart"):
            run = 0
            continue
        run += 1
        if run >= every:
            if next_is_smart(i):
                continue  # already spaced: the next song is a suggestion, which resets the run
            inserts.append(i + 1)
            run = 0
    return inserts


def plan_seed_ids(items: list[dict], current_index: int) -> list[str]:
    """Non-smart track ids, at most MAX_SEEDS, favouring those within PLAN_SEED_WINDOW of current."""
    near: list[str] = []
    far: list[str] = []
    for i, entry in enumerate(items):
        if entry.get("type") != "track" or entry.get("smart"):
            continue
        (near if abs(i - current_index) <= PLAN_SEED_WINDOW else far).append(entry["id"])
    near = list(dict.fromkeys(near))
    if len(near) >= MAX_SEEDS:
        return _sample_evenly(near, MAX_SEEDS)
    far = [t for t in dict.fromkeys(far) if t not in set(near)]
    return near + _sample_evenly(far, MAX_SEEDS - len(near))


def plan_smart_shuffle(
    session: Session, user: User, items: list[dict], current_index: int, every: int,
    rng: random.Random | None = None,
) -> list[tuple[int, Track]]:
    """[(index, track)] ascending by index. Empty when nothing needs inserting or nothing can seed."""
    indexes = plan_insert_indexes(items, current_index, every)
    if not indexes:
        return []
    seeds = plan_seed_ids(items, current_index)
    if not seeds:
        return []
    exclude = {entry["id"] for entry in items if entry.get("type") == "track"}
    tracks = suggest(session, user, seeds, exclude, len(indexes), rng)
    return list(zip(indexes, tracks))
