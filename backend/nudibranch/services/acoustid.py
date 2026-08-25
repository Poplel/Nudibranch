import json
import logging
import subprocess
from pathlib import Path

import httpx

from nudibranch.services.metadata_lookup import normalize, text_similarity

logger = logging.getLogger(__name__)

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def _join_artists(artists) -> str | None:
    if not artists:
        return None
    return ", ".join(a.get("name") for a in artists if a.get("name"))


def fingerprint_file(path) -> tuple[int, str] | None:
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("fpcalc returned non-zero for %s: %s", path, result.stderr)
            return None
        data = json.loads(result.stdout)
        duration = data.get("duration")
        fingerprint = data.get("fingerprint")
        if duration is None or fingerprint is None:
            logger.warning("fpcalc output missing fields for %s", path)
            return None
        return (int(round(float(duration))), fingerprint)
    except Exception as exc:
        logger.warning("fingerprint_file failed for %s: %s", path, exc)
        return None


def lookup_acoustid(duration: int, fingerprint: str, api_key: str) -> list[dict]:
    if not api_key or not fingerprint:
        return []
    try:
        response = httpx.get(
            ACOUSTID_LOOKUP_URL,
            params={
                "client": api_key,
                "duration": duration,
                "fingerprint": fingerprint,
                "meta": "recordings",
            },
            timeout=20.0,
            headers={"User-Agent": "Nudibranch/0.1"},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "ok":
            return []
        candidates: list[dict] = []
        for result in data.get("results", []):
            score = float(result.get("score") or 0)
            recordings = result.get("recordings")
            if not recordings:
                continue
            for recording in recordings:
                candidates.append(
                    {
                        "score": score,
                        "recording_id": recording.get("id"),
                        "title": recording.get("title"),
                        "artist": _join_artists(recording.get("artists")),
                    }
                )
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("AcoustID lookup failed: %s", exc)
        return []


def identify_audio(path, api_key: str) -> dict:
    fp = fingerprint_file(path)
    if fp is None:
        return {
            "ok": False,
            "error": "Could not fingerprint the audio file (fpcalc failed).",
            "duration": None,
            "candidates": [],
        }
    duration, fingerprint = fp
    if not api_key:
        return {
            "ok": False,
            "error": "AcoustID API key is not configured.",
            "duration": duration,
            "candidates": [],
        }
    candidates = lookup_acoustid(duration, fingerprint, api_key)
    return {
        "ok": True,
        "error": None,
        "duration": duration,
        "candidates": candidates,
    }


# A stored recording-id match is only trusted when the recording it points to is itself
# consistent with the claim (its real title agrees, its artist doesn't clearly conflict).
# Otherwise a recording-id that was mis-stamped onto the WRONG audio — the audio genuinely
# matches an id whose recording is a different song/artist — would mask foreign audio.
_REC_ID_TITLE_CONSISTENCY = 0.6
# Below this claimed-vs-detected artist similarity we treat the artist as a POSITIVE mismatch:
# a same-title different-artist recording (e.g. a metal band's track sharing a lullaby's name).
_ARTIST_MISMATCH_THRESHOLD = 0.45
# Non-discriminating artist strings — don't veto on these (they legitimately differ from the
# real performer AcoustID reports).
_GENERIC_ARTISTS = {"", "various", "various artists", "va", "unknown", "unknown artist"}


def _artist_conflicts(claimed_artist_norm: str, candidate: dict) -> bool:
    """True only when we can positively tell the candidate's artist differs from the claim."""
    if not claimed_artist_norm or claimed_artist_norm in _GENERIC_ARTISTS:
        return False
    cand_artist = normalize(candidate.get("artist") or "")
    if not cand_artist or cand_artist in _GENERIC_ARTISTS:
        return False
    return text_similarity(claimed_artist_norm, cand_artist) < _ARTIST_MISMATCH_THRESHOLD


def evaluate_claim(
    candidates: list[dict],
    duration,
    claimed_title: str | None,
    claimed_artist: str | None,
    claimed_recording_id: str | None,
) -> dict:
    """Decide whether already-fetched AcoustID candidates match a claimed identity.

    Same verdict shape as :func:`audio_matches_claim`, but works on candidates that were
    already fetched — so a single fingerprint + lookup can be reused to test several claims
    (e.g. the file's own tags vs. an expected album slot) instead of re-fingerprinting.

    `matched` is tri-state: True (audio is the claimed recording/title), False (candidates
    exist but none match — the audio is something else), None (no candidates to judge).

    ⚠ Does NOT blindly trust the stored recording-id, and DOES check the artist. Wrong-audio
    files are routinely stamped with a recording-id that matches the wrong audio (the id points
    to the OTHER song), and same-title-different-artist audio (a metal "Hush Little Baby" vs a
    lullaby) matches on title alone — both slipped through the old recording-id-first + title-only
    logic. A recording-id match is trusted only when its recording is title/artist-consistent
    with the claim; a title match is vetoed by a clear artist mismatch.
    """
    if not candidates:
        return {
            "matched": None,
            "confidence": 0.0,
            "message": "No AcoustID match found for this audio.",
            "detected": [],
            "duration": duration,
        }
    norm_claimed_title = normalize(claimed_title or "")
    norm_claimed_artist = normalize(claimed_artist or "")

    # 1. Recording-id corroboration — trusted ONLY when the matched recording is actually
    #    consistent with the claim (title agrees AND artist doesn't clearly conflict).
    if claimed_recording_id:
        for candidate in candidates:
            if candidate.get("recording_id") != claimed_recording_id:
                continue
            title_consistent = (
                not norm_claimed_title
                or text_similarity(norm_claimed_title, normalize(candidate.get("title") or "")) >= _REC_ID_TITLE_CONSISTENCY
            )
            if title_consistent and not _artist_conflicts(norm_claimed_artist, candidate):
                return {
                    "matched": True,
                    "confidence": round(float(candidate["score"]), 3),
                    "message": "Audio matches the expected recording (MusicBrainz id confirmed).",
                    "detected": candidates[:5],
                    "duration": duration,
                }
            break  # id present but its recording contradicts the claim — judge by content below

    # 2. Content match. AcoustID returns EVERY recording the fingerprint matches, so the same audio
    #    is routinely listed under several recordings/artists (re-releases, compilations, MB
    #    mis-merges). Accept when ANY title-matching candidate ALSO fits the artist; only when
    #    EVERY same-title candidate is a clearly different artist is it genuinely foreign (a metal
    #    "Hush Little Baby" vs the lullaby). ⚠ Vetoing on just the first/top same-title candidate
    #    wrongly rejected files whose correct recording was present but not first — e.g. Eminem
    #    "My Name Is" is also listed as Robbie Williams, Baby Keem "family ties" also under another
    #    artist — so we must scan ALL title-matching candidates before vetoing.
    best_title_sim = 0.0
    title_matches: list[dict] = []
    for candidate in candidates:
        title_sim = text_similarity(norm_claimed_title, normalize(candidate.get("title") or ""))
        if title_sim > best_title_sim:
            best_title_sim = title_sim
        if title_sim >= 0.85:
            title_matches.append(candidate)
    confidence = round(best_title_sim, 3)
    if not title_matches:
        # ⚠ AcoustID matched a fingerprint but, for underground / unsubmitted recordings, the match
        # frequently has NO title/artist metadata at all (title=None) even at score ~0.97. We then
        # cannot say WHAT the audio is, so we must NOT declare it foreign — doing so mass-flagged
        # whole underground albums (all of glaive's "Y'all"/"God Save The Three") whose files were
        # fine. Only call it foreign when at least one candidate carries a real title that differs.
        if not any(normalize(candidate.get("title") or "") for candidate in candidates):
            return {
                "matched": None,
                "confidence": 0.0,
                "message": "AcoustID matched a fingerprint but returned no usable title — cannot identify the audio.",
                "detected": candidates[:5],
                "duration": duration,
            }
        return {
            "matched": False,
            "confidence": confidence,
            "message": "Audio does NOT match what the file claims — top AcoustID result differs.",
            "detected": candidates[:5],
            "duration": duration,
        }
    if any(not _artist_conflicts(norm_claimed_artist, candidate) for candidate in title_matches):
        return {
            "matched": True,
            "confidence": confidence,
            "message": "Audio appears to match the claimed title.",
            "detected": candidates[:5],
            "duration": duration,
        }
    conflicting_artists = ", ".join(sorted({str(c.get("artist")) for c in title_matches if c.get("artist")}))
    return {
        "matched": False,
        "confidence": confidence,
        "message": (
            f"Audio title matches but every candidate's artist differs (AcoustID: {conflicting_artists}) "
            "— a different recording that shares the title."
        ),
        "detected": candidates[:5],
        "duration": duration,
    }


def audio_matches_claim(
    path,
    claimed_title: str | None,
    claimed_artist: str | None,
    claimed_recording_id: str | None,
    api_key: str,
) -> dict:
    result = identify_audio(path, api_key)
    duration = result.get("duration")
    if not result["ok"]:
        return {
            "matched": None,
            "confidence": 0.0,
            "message": result["error"],
            "detected": [],
            "duration": duration,
        }
    return evaluate_claim(
        result["candidates"], duration, claimed_title, claimed_artist, claimed_recording_id
    )
