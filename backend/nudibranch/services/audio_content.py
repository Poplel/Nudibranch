"""Detect dead air (digital silence) in audio files via ffmpeg silencedetect.

A corrupt rip can be full-length (so its duration matches the expected track and looks
fine) yet contain only a few seconds of real audio followed by pure digital silence.
`measure_silence_fraction` decodes the file and reports how much of it is silent so the
audio-content check can propose re-downloading tracks that are mostly dead air.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Sustained level below this, for at least MIN_SILENCE_SECONDS, counts as dead air.
# Real (even very quiet) music rarely stays below -70 dB for seconds at a time; truly
# corrupt files are digital silence (~-91 dB), so this is conservative.
SILENCE_NOISE_DB = -70.0
MIN_SILENCE_SECONDS = 2.0
# A track that is at least this fraction dead air is treated as corrupt.
DEAD_AIR_THRESHOLD = 0.25

_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_DURATION_RE = re.compile(r"silence_duration:\s*([0-9.]+)")


def measure_silence_fraction(path: Path, total_seconds: float) -> float | None:
    """Return the fraction (0..1) of the track that is dead air, or None if unmeasurable.

    Uses ffmpeg's silencedetect filter and sums every detected silent region. A silent
    region that runs to the end of the file may be reported with only a `silence_start`
    (no `silence_duration`), so that trailing tail is added explicitly.
    """
    if not total_seconds or total_seconds <= 0:
        return None
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                "-map", "0:a",
                "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={MIN_SILENCE_SECONDS}",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stderr or result.stdout or ""
    starts = [float(x) for x in _START_RE.findall(output)]
    durations = [float(x) for x in _DURATION_RE.findall(output)]
    silent = sum(durations)
    # A final region that extends to EOF can lack a silence_duration line — add its tail.
    if len(starts) > len(durations):
        silent += max(0.0, total_seconds - starts[-1])
    return max(0.0, min(1.0, silent / total_seconds))
