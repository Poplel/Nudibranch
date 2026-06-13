"""Fuzzy library search.

A SQLite **FTS5 trigram** virtual table (`library_fts`) provides fast, typo-tolerant
substring candidate retrieval over artist/album/track names; **RapidFuzz** then scores
each candidate against the query to produce a 0–1 confidence used for ranking and
threshold culling.

The index is kept fresh by triggers on the `artists`/`albums`/`tracks` tables (so
imports and metadata edits update it automatically); `rebuild_search_index` does a full
repopulate (used to backfill an existing library the first time, and via an admin route).

Names are stored lowercased in the indexed `name` column (trigram matching is
case-sensitive); the original-case string lives in the UNINDEXED `display` column.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

try:  # pragma: no cover - rapidfuzz is provided by the image
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover
    fuzz = None

_KINDS = ("artist", "album", "track")

_INSERT_COLS = "entry_key,kind,ref_id,artist_id,album_id,display,name"

_TRIGGERS = [
    # artists
    f"""CREATE TRIGGER IF NOT EXISTS library_fts_artist_ai AFTER INSERT ON artists BEGIN
        INSERT INTO library_fts({_INSERT_COLS})
        VALUES('artist:'||new.id,'artist',new.id,new.id,NULL,new.name,lower(new.name));
    END;""",
    """CREATE TRIGGER IF NOT EXISTS library_fts_artist_ad AFTER DELETE ON artists BEGIN
        DELETE FROM library_fts WHERE entry_key='artist:'||old.id;
    END;""",
    f"""CREATE TRIGGER IF NOT EXISTS library_fts_artist_au AFTER UPDATE ON artists BEGIN
        DELETE FROM library_fts WHERE entry_key='artist:'||new.id;
        INSERT INTO library_fts({_INSERT_COLS})
        VALUES('artist:'||new.id,'artist',new.id,new.id,NULL,new.name,lower(new.name));
    END;""",
    # albums
    f"""CREATE TRIGGER IF NOT EXISTS library_fts_album_ai AFTER INSERT ON albums BEGIN
        INSERT INTO library_fts({_INSERT_COLS})
        VALUES('album:'||new.id,'album',new.id,new.artist_id,new.id,new.title,lower(new.title));
    END;""",
    """CREATE TRIGGER IF NOT EXISTS library_fts_album_ad AFTER DELETE ON albums BEGIN
        DELETE FROM library_fts WHERE entry_key='album:'||old.id;
    END;""",
    f"""CREATE TRIGGER IF NOT EXISTS library_fts_album_au AFTER UPDATE ON albums BEGIN
        DELETE FROM library_fts WHERE entry_key='album:'||new.id;
        INSERT INTO library_fts({_INSERT_COLS})
        VALUES('album:'||new.id,'album',new.id,new.artist_id,new.id,new.title,lower(new.title));
    END;""",
    # tracks (artist_id resolved through the album)
    f"""CREATE TRIGGER IF NOT EXISTS library_fts_track_ai AFTER INSERT ON tracks BEGIN
        INSERT INTO library_fts({_INSERT_COLS})
        VALUES('track:'||new.id,'track',new.id,(SELECT artist_id FROM albums WHERE id=new.album_id),new.album_id,new.title,lower(new.title));
    END;""",
    """CREATE TRIGGER IF NOT EXISTS library_fts_track_ad AFTER DELETE ON tracks BEGIN
        DELETE FROM library_fts WHERE entry_key='track:'||old.id;
    END;""",
    f"""CREATE TRIGGER IF NOT EXISTS library_fts_track_au AFTER UPDATE ON tracks BEGIN
        DELETE FROM library_fts WHERE entry_key='track:'||new.id;
        INSERT INTO library_fts({_INSERT_COLS})
        VALUES('track:'||new.id,'track',new.id,(SELECT artist_id FROM albums WHERE id=new.album_id),new.album_id,new.title,lower(new.title));
    END;""",
]


def ensure_search_schema(session: Session) -> None:
    """Create the FTS5 table and sync triggers if they don't exist (idempotent)."""
    session.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5("
        "entry_key UNINDEXED, kind UNINDEXED, ref_id UNINDEXED, "
        "artist_id UNINDEXED, album_id UNINDEXED, display UNINDEXED, "
        "name, tokenize='trigram')"
    ))
    for trigger in _TRIGGERS:
        session.execute(text(trigger))
    session.commit()


def rebuild_search_index(session: Session) -> int:
    """Wipe and fully repopulate the index from the current library. Returns row count."""
    session.execute(text("DELETE FROM library_fts"))
    session.execute(text(
        f"INSERT INTO library_fts({_INSERT_COLS}) "
        "SELECT 'artist:'||id,'artist',id,id,NULL,name,lower(name) FROM artists"
    ))
    session.execute(text(
        f"INSERT INTO library_fts({_INSERT_COLS}) "
        "SELECT 'album:'||id,'album',id,artist_id,id,title,lower(title) FROM albums"
    ))
    session.execute(text(
        f"INSERT INTO library_fts({_INSERT_COLS}) "
        "SELECT 'track:'||t.id,'track',t.id,a.artist_id,t.album_id,t.title,lower(t.title) "
        "FROM tracks t JOIN albums a ON a.id=t.album_id"
    ))
    session.commit()
    return session.scalar(text("SELECT count(*) FROM library_fts")) or 0


def ensure_populated(session: Session) -> None:
    """Ensure schema exists and, if the index is empty but the library isn't, backfill it."""
    ensure_search_schema(session)
    indexed = session.scalar(text("SELECT count(*) FROM library_fts")) or 0
    if indexed == 0 and (session.scalar(text("SELECT count(*) FROM artists")) or 0) > 0:
        rebuild_search_index(session)


def _fts_phrase(query_lower: str) -> str:
    """Quote the query as an FTS5 phrase so punctuation isn't parsed as query syntax."""
    return '"' + query_lower.replace('"', '""') + '"'


def search_library(
    session: Session,
    query: str,
    kinds=None,
    min_confidence: float = 0.4,
    limit: int = 50,
) -> list[dict]:
    """Return ranked fuzzy matches: [{kind,id,name,artist_id,album_id,confidence}]."""
    query = (query or "").strip()
    if not query:
        return []
    kind_set = {k for k in (kinds or _KINDS) if k in _KINDS} or set(_KINDS)
    q_lower = query.lower()

    rows = []
    if len(q_lower) >= 3:
        try:
            rows = session.execute(
                text("SELECT kind, ref_id, artist_id, album_id, display FROM library_fts WHERE name MATCH :q LIMIT 4000"),
                {"q": _fts_phrase(q_lower)},
            ).all()
        except Exception:
            rows = []
    if not rows:
        # Short queries (<3 chars, below trigram length) or an FTS miss: substring fallback.
        rows = session.execute(
            text("SELECT kind, ref_id, artist_id, album_id, display FROM library_fts WHERE name LIKE :like LIMIT 4000"),
            {"like": f"%{q_lower}%"},
        ).all()

    results: list[dict] = []
    for kind, ref_id, artist_id, album_id, display in rows:
        if kind not in kind_set:
            continue
        score = float(fuzz.WRatio(query, display or "")) if fuzz else 100.0
        confidence = score / 100.0
        if confidence < min_confidence:
            continue
        results.append({
            "kind": kind,
            "id": ref_id,
            "name": display,
            "artist_id": artist_id,
            "album_id": album_id,
            "confidence": round(confidence, 4),
        })
    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results[:limit]
