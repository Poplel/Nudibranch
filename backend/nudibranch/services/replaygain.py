"""ReplayGain: measure a track's loudness (ffmpeg ebur128) and write the gain tag.

Non-destructive volume normalization — we never touch the audio samples. We measure
integrated loudness and store the gain needed to reach the ReplayGain 2.0 reference
(-18 LUFS) both on the Track row (the player applies it at playback) and in the file's
REPLAYGAIN_TRACK_GAIN tag (so Jellyfin / other ReplayGain-aware players honour it too).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ReplayGain 2.0 reference loudness.
REFERENCE_LUFS = -18.0
# Below this the track is effectively silent — don't propose a (huge) gain.
SILENCE_LUFS = -70.0

_INTEGRATED_RE = re.compile(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")


def measure_track_gain(path: Path) -> float | None:
    """Return the ReplayGain track gain in dB (rounded to 2dp), or None if unmeasurable.

    Uses ffmpeg's EBU R128 filter to read integrated loudness; gain = reference - measured.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stderr or result.stdout or ""
    matches = _INTEGRATED_RE.findall(output)
    if not matches:
        return None
    try:
        integrated = float(matches[-1])  # the summary block prints the final integrated value last
    except ValueError:
        return None
    if integrated <= SILENCE_LUFS:
        return None
    return round(REFERENCE_LUFS - integrated, 2)


def write_replaygain_tag(file_path: Path, track_gain_db: float | None) -> None:
    """Write (or clear) the REPLAYGAIN_TRACK_GAIN tag on the file, per-format via mutagen.

    A None gain removes the tag. Never raises — tagging failures must not break the apply.
    """
    suffix = file_path.suffix.lower()
    gain_str = f"{track_gain_db:.2f} dB" if track_gain_db is not None else None
    try:
        if suffix == ".mp3":
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError

            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TXXX:REPLAYGAIN_TRACK_GAIN")
            if gain_str is not None:
                tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[gain_str]))
            tags.save(file_path)
        elif suffix in {".m4a", ".mp4", ".aac", ".alac"}:
            from mutagen.mp4 import MP4, MP4FreeForm

            audio = MP4(file_path)
            key = "----:com.apple.iTunes:replaygain_track_gain"
            audio.pop(key, None)
            if gain_str is not None:
                audio[key] = [MP4FreeForm(gain_str.encode("utf-8"))]
            audio.save()
        else:
            # FLAC / Ogg / Opus / Wav etc. — Vorbis-comment style mapping.
            from mutagen import File as MutagenFile

            audio = MutagenFile(file_path)
            if audio is None:
                return
            if audio.tags is None:
                try:
                    audio.add_tags()
                except Exception:  # noqa: BLE001 - some formats already have tags
                    pass
            try:
                if "REPLAYGAIN_TRACK_GAIN" in audio:
                    del audio["REPLAYGAIN_TRACK_GAIN"]
            except Exception:  # noqa: BLE001
                pass
            if gain_str is not None:
                audio["REPLAYGAIN_TRACK_GAIN"] = gain_str
            audio.save()
    except Exception:  # noqa: BLE001 - tagging is best-effort; the DB value is the source of truth.
        return
