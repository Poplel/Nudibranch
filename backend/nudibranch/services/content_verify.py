"""Shared audio-content verification used by the bulk check tool AND download imports.

The single source of truth for "does this audio file actually contain the song it claims
to be?". Two cheap-to-expensive layers:

  1. **Dead-air heuristic** (no AcoustID key needed) — decode the file and measure how much
     of it is digital silence. A corrupt rip is often full-length (so its duration matches
     and looks fine) yet holds only a few seconds of audio then silence.
  2. **AcoustID fingerprint** (needs a key) — fingerprint the audio and ask whether it
     matches the claimed identity (recording-id first, then title similarity). Only this can
     catch a *wrong-but-audible* song that happens to be the right length.

Callers layer their own preliminary heuristics (e.g. the bulk tool's MusicBrainz duration
slot check) BEFORE calling this, then use this to confirm. The verdict is deliberately
conservative: `replace` is only ever True on a positive signal (measured dead air, or a
confident AcoustID mismatch). "Couldn't tell" never proposes a replacement — we never
overwrite / requeue a good file on a guess.
"""

from __future__ import annotations

from pathlib import Path

from nudibranch.services.acoustid import evaluate_claim, identify_audio
from nudibranch.services.audio_content import DEAD_AIR_THRESHOLD, measure_silence_fraction
from nudibranch.services.metadata_lookup import normalize, text_similarity


# verdict values
OK = "ok"                      # audio matches its claimed identity (or is a known album sibling)
DEAD_AIR = "dead_air"          # mostly digital silence — corrupt rip
FOREIGN_AUDIO = "foreign_audio"  # AcoustID confidently identifies a DIFFERENT song
UNDETERMINED = "undetermined"  # AcoustID found nothing to judge against
UNVERIFIABLE = "unverifiable"  # no key / fingerprint failed / missing file — can't deep-check


def _detected_belongs_to_album(
    candidates: list[dict],
    album_recording_ids: set[str] | None,
    album_titles: list[str] | None,
) -> bool:
    """True when the detected audio is just another track of the SAME album.

    Used only by the library bulk tool: a mis-numbered library file is the wrong *slot* but
    the right *album*, so we should not replace it. (A fresh download that returns a different
    track of the album is still wrong for the request — callers that want strictness pass no
    album context, so this always returns False for them.)
    """
    if album_recording_ids:
        detected_ids = {c.get("recording_id") for c in candidates if c.get("recording_id")}
        if album_recording_ids & detected_ids:
            return True
    if album_titles:
        norm_album_titles = [normalize(t or "") for t in album_titles if t]
        for candidate in candidates[:5]:
            d_title = normalize(candidate.get("title") or "")
            if d_title and any(text_similarity(d_title, t) >= 0.85 for t in norm_album_titles):
                return True
    return False


def verify_audio_content(
    path: Path,
    *,
    claimed_title: str | None,
    claimed_artist: str | None = None,
    claimed_recording_id: str | None = None,
    total_ms: int | float | None = None,
    api_key: str | None = None,
    album_recording_ids: set[str] | None = None,
    album_titles: list[str] | None = None,
    check_dead_air: bool = True,
    dead_air_threshold: float = DEAD_AIR_THRESHOLD,
) -> dict:
    """Verify one audio file against the identity it is supposed to hold.

    Returns a dict:
      {
        "verdict":  ok | dead_air | foreign_audio | undetermined | unverifiable,
        "replace":  bool,          # True only for dead_air / foreign_audio
        "reason":   short machine tag (e.g. "no_acoustid_key", "no_candidates"),
        "log":      human-readable one-liner for the task/activity log,
        "silent_fraction": float | None,
        "confidence": float | None,      # AcoustID own-tag match confidence
        "detected_title": str | None,    # top AcoustID candidate title (for logging)
      }
    """
    label = claimed_title or (path.name if path else "?")

    if not path or not Path(path).exists():
        return {
            "verdict": UNVERIFIABLE, "replace": False, "reason": "missing_file",
            "log": f"{label}: file is missing — cannot verify audio content",
            "silent_fraction": None, "confidence": None, "detected_title": None,
        }
    path = Path(path)

    # ---- Layer 1: dead-air (no key required) -------------------------------------------
    silent_fraction = None
    if check_dead_air:
        total_s = (float(total_ms) / 1000.0) if total_ms else 0.0
        if total_s <= 0:
            # Fall back to the file's own decoded duration so a track with no stored
            # duration is still checked.
            try:
                from nudibranch.services.imports import read_audio_metadata

                total_s = (read_audio_metadata(path).get("duration_ms") or 0) / 1000.0
            except Exception:
                total_s = 0.0
        if total_s > 0:
            silent_fraction = measure_silence_fraction(path, total_s)
            if silent_fraction is not None and silent_fraction >= dead_air_threshold:
                pct = round(silent_fraction * 100)
                audible_s = round(total_s * (1 - silent_fraction))
                return {
                    "verdict": DEAD_AIR, "replace": True, "reason": "dead_air",
                    "log": f"{label}: {pct}% dead air (audio stops after ~{audible_s}s) — corrupt rip",
                    "silent_fraction": silent_fraction, "confidence": None, "detected_title": None,
                }

    # ---- Layer 2: AcoustID fingerprint (needs a key) -----------------------------------
    if not api_key:
        return {
            "verdict": UNVERIFIABLE, "replace": False, "reason": "no_acoustid_key",
            "log": f"{label}: no AcoustID key — dead-air only, cannot confirm the audio is correct",
            "silent_fraction": silent_fraction, "confidence": None, "detected_title": None,
        }

    ident = identify_audio(path, api_key)
    if not ident.get("ok"):
        return {
            "verdict": UNVERIFIABLE, "replace": False, "reason": "fingerprint_failed",
            "log": f"{label}: could not fingerprint audio ({ident.get('error')}) — skipping content check",
            "silent_fraction": silent_fraction, "confidence": None, "detected_title": None,
        }
    candidates = ident.get("candidates") or []
    if not candidates:
        return {
            "verdict": UNDETERMINED, "replace": False, "reason": "no_candidates",
            "log": f"{label}: AcoustID returned no match for this audio — cannot confirm right or wrong",
            "silent_fraction": silent_fraction, "confidence": None, "detected_title": None,
        }

    detected_title = (candidates[0].get("title") if candidates else None)
    own = evaluate_claim(candidates, ident.get("duration"), claimed_title, claimed_artist, claimed_recording_id)
    confidence = own.get("confidence")

    if own.get("matched") is True:
        return {
            "verdict": OK, "replace": False, "reason": "match_own_tags",
            "log": f"{label}: audio matches its claimed identity (AcoustID conf {confidence})",
            "silent_fraction": silent_fraction, "confidence": confidence, "detected_title": detected_title,
        }

    # matched is None ⇒ AcoustID could not IDENTIFY the audio (fingerprint matched a recording with
    # no usable title/artist — common for underground/unsubmitted tracks). We cannot conclude the
    # file is wrong, so we leave it (never replace on "couldn't tell"). This is the guard that stops
    # whole underground albums being mass-flagged just because AcoustID lacks their metadata.
    if own.get("matched") is None:
        return {
            "verdict": UNDETERMINED, "replace": False, "reason": "unidentified",
            "log": f"{label}: {own.get('message')} — leaving it",
            "silent_fraction": silent_fraction, "confidence": confidence, "detected_title": detected_title,
        }

    # Audio does NOT match the claimed identity. Album-sibling leniency: spare a track that
    # legitimately belongs to this album (its OWN title IS on the album) but whose file plays a
    # DIFFERENT album track — a genuine intra-album mis-number, better fixed by re-tagging than a
    # re-download. ⚠ A track whose own title is NOT on the album is NOT mis-numbered — it is
    # mislabeled/misfiled (a single dropped into a wrong-named album, e.g. a "Girl Crush" file
    # inside album "Sugar Coat", or "Miss Me More" inside "hole in the bottle"), so it is treated
    # as foreign and replaced rather than silently kept.
    claimed_on_album = bool(album_titles) and any(
        text_similarity(normalize(claimed_title or ""), normalize(t)) >= 0.85 for t in album_titles
    )
    if claimed_on_album and _detected_belongs_to_album(candidates, album_recording_ids, album_titles):
        return {
            "verdict": OK, "replace": False, "reason": "album_sibling",
            "log": f"{label}: audio is another track from this album (mis-numbered, not wrong) — leaving it",
            "silent_fraction": silent_fraction, "confidence": confidence, "detected_title": detected_title,
        }

    return {
        "verdict": FOREIGN_AUDIO, "replace": True, "reason": "foreign_audio",
        "log": (
            f"{label}: audio does NOT match — AcoustID identifies '{detected_title or 'unknown'}' "
            f"(claimed-title conf {confidence})"
        ),
        "silent_fraction": silent_fraction, "confidence": confidence, "detected_title": detected_title,
    }
