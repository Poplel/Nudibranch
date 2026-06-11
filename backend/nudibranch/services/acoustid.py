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
    candidates = result["candidates"]
    if not candidates:
        return {
            "matched": None,
            "confidence": 0.0,
            "message": "No AcoustID match found for this audio.",
            "detected": [],
            "duration": duration,
        }
    # Check for recording ID match first
    if claimed_recording_id:
        for candidate in candidates:
            if candidate.get("recording_id") == claimed_recording_id:
                return {
                    "matched": True,
                    "confidence": round(float(candidate["score"]), 3),
                    "message": "Audio matches the expected recording (MusicBrainz id confirmed).",
                    "detected": candidates[:5],
                    "duration": duration,
                }
    # Fall back to title similarity
    best_candidate = None
    best_title_sim = -1.0
    norm_claimed_title = normalize(claimed_title or "")
    for candidate in candidates:
        title_sim = text_similarity(
            norm_claimed_title,
            normalize(candidate.get("title") or ""),
        )
        if title_sim > best_title_sim:
            best_title_sim = title_sim
            best_candidate = candidate
    confidence = best_title_sim
    matched = confidence >= 0.85
    message = (
        "Audio appears to match the claimed title."
        if matched
        else "Audio does NOT match what the file claims — top AcoustID result differs."
    )
    return {
        "matched": matched,
        "confidence": round(confidence, 3),
        "message": message,
        "detected": candidates[:5],
        "duration": duration,
    }
