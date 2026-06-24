"""Admin-tunable Soulseek (slskd) match scoring parameters.

The exact numbers the download matcher uses to rank/accept candidates live here as defaults and are
overridable per-instance (stored as ``AppSetting`` rows keyed ``match_tuning:<name>`` and edited in
Settings → Download matching). The worker refreshes its in-memory copy from here at every search
entry point, so the scorer can read them without threading a DB session.

Weights need not sum to 1 — they are relative contributions to a 0-1 confidence. Tweaking these
changes how aggressively the matcher trusts title vs duration vs artist and how hard it demotes
likely-wrong matches; it never bypasses the candidate review queue.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from nudibranch.db.models import AppSetting

PREFIX = "match_tuning:"

MATCH_TUNING_DEFAULTS: dict[str, float] = {
    # Acceptance / gating
    "min_confidence": 0.45,            # candidates below this 0-1 confidence are dropped (recall floor)
    "title_floor": 0.60,               # required title similarity — the title is the only real
                                       # discriminator once artist/duration/album corroborate (any
                                       # same-artist, similar-length file maxes those), so keep it
                                       # firm enough to reject coincidental short-title overlaps
    "duration_tolerance_seconds": 12,  # ± window counted as a perfect duration match
    # Ranking weights (relative contributions to the 0-1 confidence)
    "weight_title": 0.40,
    "weight_duration": 0.38,
    "weight_artist": 0.17,
    # Bonuses
    "album_bonus_strong": 0.06,        # folder album-name score >= 0.85
    "album_bonus_weak": 0.02,          # folder album-name score >= 0.60
    "track_number_bonus": 0.06,        # leading file number matches the requested track number
    "track_number_penalty": 0.12,      # leading file number contradicts it
    # Demotions (multipliers applied to the final confidence)
    "penalty_wrong_version": 0.50,     # an unrequested remix/live/instrumental/etc. marker is present
    "penalty_artist_mismatch": 0.55,   # artist doesn't match AND duration can't corroborate
}

# UI metadata: label, help text, and clamp bounds (min, max, step). Keys not listed are still stored
# but won't render a control.
MATCH_TUNING_META: dict[str, dict] = {
    "min_confidence": {"label": "Minimum match confidence", "help": "Drop candidates scoring below this. Lower = more results surface for review.", "min": 0.1, "max": 0.9, "step": 0.01},
    "title_floor": {"label": "Title match floor", "help": "A file's title must be at least this similar to be considered at all.", "min": 0.1, "max": 0.9, "step": 0.01},
    "duration_tolerance_seconds": {"label": "Duration tolerance (seconds)", "help": "Length difference still counted as a perfect duration match.", "min": 3, "max": 60, "step": 1},
    "weight_title": {"label": "Title weight", "help": "How much the filename-vs-title match contributes.", "min": 0.0, "max": 1.0, "step": 0.01},
    "weight_duration": {"label": "Duration weight", "help": "How much a matching length contributes (strongest proof two files are the same recording).", "min": 0.0, "max": 1.0, "step": 0.01},
    "weight_artist": {"label": "Artist weight", "help": "How much the artist appearing in the folder path contributes (ranking only — never required).", "min": 0.0, "max": 1.0, "step": 0.01},
    "album_bonus_strong": {"label": "Album bonus (strong)", "help": "Added when the folder name strongly matches the album.", "min": 0.0, "max": 0.3, "step": 0.01},
    "album_bonus_weak": {"label": "Album bonus (weak)", "help": "Added when the folder name loosely matches the album.", "min": 0.0, "max": 0.3, "step": 0.01},
    "track_number_bonus": {"label": "Track-number bonus", "help": "Added when the file's leading number matches the requested track number.", "min": 0.0, "max": 0.3, "step": 0.01},
    "track_number_penalty": {"label": "Track-number penalty", "help": "Subtracted when the leading number contradicts the requested track number.", "min": 0.0, "max": 0.5, "step": 0.01},
    "penalty_wrong_version": {"label": "Wrong-version penalty", "help": "Multiplier applied when an unrequested remix/live/instrumental marker is present (lower = harsher).", "min": 0.1, "max": 1.0, "step": 0.05},
    "penalty_artist_mismatch": {"label": "Artist-mismatch penalty", "help": "Multiplier when the artist doesn't match and duration can't confirm the recording (lower = harsher).", "min": 0.1, "max": 1.0, "step": 0.05},
}


def _clamp(name: str, value: float) -> float:
    meta = MATCH_TUNING_META.get(name)
    if not meta:
        return value
    return max(meta["min"], min(meta["max"], value))


def match_tuning(session: Session) -> dict[str, float]:
    """Return the effective tuning: defaults overlaid with stored admin overrides."""
    values = dict(MATCH_TUNING_DEFAULTS)
    for setting in session.query(AppSetting).filter(AppSetting.key.like(f"{PREFIX}%")):
        name = setting.key[len(PREFIX):]
        if name not in values:
            continue
        try:
            values[name] = _clamp(name, float(setting.value))
        except (TypeError, ValueError):
            continue
    return values


def update_match_tuning(session: Session, values: dict) -> dict[str, float]:
    """Upsert provided overrides (ignoring unknown/uncoercible keys), return the effective tuning."""
    for name, value in (values or {}).items():
        if name not in MATCH_TUNING_DEFAULTS:
            continue
        try:
            stored = _clamp(name, float(value))
        except (TypeError, ValueError):
            continue
        key = f"{PREFIX}{name}"
        setting = session.get(AppSetting, key)
        if not setting:
            session.add(AppSetting(key=key, value=str(stored)))
        else:
            setting.value = str(stored)
    session.flush()
    return match_tuning(session)


def match_tuning_schema() -> list[dict]:
    """UI descriptor: ordered list of {name, value(default), label, help, min, max, step}."""
    schema = []
    for name in MATCH_TUNING_META:
        meta = MATCH_TUNING_META[name]
        schema.append({
            "name": name,
            "default": MATCH_TUNING_DEFAULTS[name],
            "label": meta["label"],
            "help": meta["help"],
            "min": meta["min"],
            "max": meta["max"],
            "step": meta["step"],
        })
    return schema
