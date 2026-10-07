"""Audio feature analysis for the suggestion engine (the `analyze_audio` worker task).

Per track: a 60 s excerpt (from ~30% into the file) is decoded by ffmpeg to mono 22.05 kHz float PCM
and reduced with numpy to a tempo estimate, an energy figure and a brightness figure. Genres and
year come from the file's tags, and a BPM tag, when present, wins over the estimate.

The worker's main module only owns the tick and the task dispatch; everything else lives here.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from mutagen import File as MutagenFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from nudibranch.db.models import Artist, ArtistSimilarity, Task, TaskStatus, Track, TrackFeatures
from nudibranch.services.app_log import write_app_log
from nudibranch.services.tasks import ScanProgress, append_task_log, enqueue_task

#: Bump to re-analyze every track (rows with an older version are picked up again).
ANALYSIS_VERSION = 1
#: Small on purpose: the worker runs one task at a time, so a batch holds up approvals and imports
#: for as long as it runs. ~25 decodes is tens of seconds; the tick comes back for the rest.
BATCH_TRACKS = 25
#: Artists refreshed per task run (each costs ~2 paced network requests).
BATCH_ARTISTS = 10
ARTIST_REFRESH_AFTER = timedelta(days=30)

SAMPLE_RATE = 22050
N_FFT = 1024
HOP = 512
EXCERPT_SECONDS = 60
BPM_MIN = 60.0
BPM_MAX = 200.0
BPM_PRIOR_CENTER = 120.0
#: Width of the log-normal tempo prior, in octaves.
BPM_PRIOR_SIGMA_OCTAVES = 0.8


# --------------------------------------------------------------------------------------------------
# Signal analysis (pure numpy; unit-testable without ffmpeg)
# --------------------------------------------------------------------------------------------------

def _stft_magnitude(samples: np.ndarray) -> np.ndarray:
    """Magnitude spectrogram, shape (frames, N_FFT // 2 + 1)."""
    if len(samples) < N_FFT:
        samples = np.pad(samples, (0, N_FFT - len(samples)))
    n_frames = 1 + (len(samples) - N_FFT) // HOP
    window = np.hanning(N_FFT).astype(np.float32)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = samples[idx] * window
    return np.abs(np.fft.rfft(frames, axis=1))


def onset_envelope(magnitude: np.ndarray) -> np.ndarray:
    """Spectral flux: half-wave rectified frame-to-frame increase of the log-compressed spectrum."""
    compressed = np.log1p(10.0 * magnitude)
    flux = np.maximum(compressed[1:] - compressed[:-1], 0.0).sum(axis=1)
    return np.concatenate([[0.0], flux])


def estimate_bpm(onsets: np.ndarray, frame_rate: float) -> float | None:
    """Tempo from the autocorrelation of the onset envelope.

    Lags covering BPM_MIN..BPM_MAX are scored by their (normalized) autocorrelation times a
    log-normal prior centred on 120 BPM, which settles the usual half/double-tempo ambiguity. The
    winning lag is refined by parabolic interpolation, since integer lags are ~2% apart.
    """
    if len(onsets) < 4 * frame_rate or not np.any(onsets):
        return None
    env = onsets - onsets.mean()
    if not np.any(env):
        return None
    n = len(env)
    size = 1 << (2 * n - 1).bit_length()
    spectrum = np.fft.rfft(env, size)
    acf = np.fft.irfft(spectrum * np.conj(spectrum), size)[:n]
    if acf[0] <= 0:
        return None
    acf = acf / acf[0]
    # Unbiased: longer lags have fewer overlapping samples.
    acf = acf * (n / np.maximum(n - np.arange(n), 1))

    min_lag = max(2, int(math.floor(frame_rate * 60.0 / BPM_MAX)) - 1)
    max_lag = min(n - 2, int(math.ceil(frame_rate * 60.0 / BPM_MIN)) + 1)
    if max_lag <= min_lag + 2:
        return None
    lags = np.arange(min_lag, max_lag + 1)
    bpms = 60.0 * frame_rate / lags
    prior = np.exp(-0.5 * (np.log2(bpms / BPM_PRIOR_CENTER) / BPM_PRIOR_SIGMA_OCTAVES) ** 2)
    score = acf[lags] * prior
    best = int(np.argmax(score))
    if acf[lags[best]] <= 0:
        return None
    lag = float(lags[best])
    # A slow pick whose half-period lag correlates about as strongly is really the faster pulse
    # (a click train at 140 correlates at both 1x and 2x the period; the prior then favours 70).
    half = int(round(lags[best] / 2))
    def peak(i: int) -> float:  # a non-integer period splits its peak across two adjacent lags
        return float(max(acf[i - 1] + acf[i], acf[i] + acf[i + 1]))

    if 60.0 * frame_rate / lags[best] < 90.0 and half >= min_lag and peak(half) >= 0.9 * peak(int(lags[best])):
        best = int(np.argmin(np.abs(lags - half)))
        lag = float(lags[best])
    # Parabolic interpolation over the raw autocorrelation around the winning lag.
    i = int(lags[best])
    if 1 <= i < n - 1:
        y0, y1, y2 = acf[i - 1], acf[i], acf[i + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            lag = i + 0.5 * (y0 - y2) / denom
    bpm = 60.0 * frame_rate / lag
    return float(bpm) if BPM_MIN - 5 <= bpm <= BPM_MAX + 5 else None


def analyze_samples(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> dict:
    """bpm / energy / brightness for a mono float32 excerpt."""
    if len(samples) < N_FFT:
        return {"bpm": None, "energy": None, "brightness": None}
    magnitude = _stft_magnitude(samples)
    onsets = onset_envelope(magnitude)
    bpm = estimate_bpm(onsets, sample_rate / HOP)

    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
    energy = float(min(1.0, max(0.0, (dbfs + 40.0) / 40.0)))

    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sample_rate)
    frame_energy = magnitude.sum(axis=1)
    live = frame_energy > 1e-6
    if np.any(live):
        centroid = (magnitude[live] * freqs).sum(axis=1) / frame_energy[live]
        brightness = float(min(1.0, max(0.0, centroid.mean() / (sample_rate / 2.0))))
    else:
        brightness = None
    return {"bpm": bpm, "energy": energy, "brightness": brightness}


def decode_excerpt(path: Path, duration_s: float | None) -> np.ndarray:
    """60 s of mono 22.05 kHz float PCM from ~30% into the file (clamped to fit short tracks)."""
    start = 0.0
    if duration_s and duration_s > 0:
        start = max(0.0, min(duration_s * 0.30, duration_s - EXCERPT_SECONDS))
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", "-t", str(EXCERPT_SECONDS),
        "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]
    result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError((result.stderr or b"ffmpeg produced no audio").decode("utf-8", "replace")[:200])
    return np.frombuffer(result.stdout[: len(result.stdout) // 4 * 4], dtype=np.float32)


# --------------------------------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------------------------------

_GENRE_SPLIT = re.compile(r"[;/,\x00]")


def split_genres(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        for part in _GENRE_SPLIT.split(str(value)):
            genre = part.strip().lower()
            if genre and genre not in seen:
                seen.append(genre)
    return seen


def _first_number(value: object) -> float | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    match = re.search(r"\d+(?:\.\d+)?", str(value if value is not None else ""))
    return float(match.group(0)) if match else None


def read_tag_features(path: Path) -> dict:
    """BPM tag, genres and year from the file's tags (all optional)."""
    out: dict = {"bpm": None, "genres": [], "year": None}
    try:
        raw = MutagenFile(path)
    except Exception:  # noqa: BLE001 - an unreadable tag block is just "no tags".
        raw = None
    if raw is not None and raw.tags is not None:
        tags = raw.tags
        try:
            if "TBPM" in tags:  # ID3
                out["bpm"] = _first_number(tags["TBPM"].text)
            elif "tmpo" in tags:  # MP4
                out["bpm"] = _first_number(tags["tmpo"])
            else:
                for key in ("bpm", "BPM", "tempo"):
                    if key in tags:
                        out["bpm"] = _first_number(tags[key])
                        break
        except Exception:  # noqa: BLE001
            out["bpm"] = None
    if out["bpm"] is not None and not (30 <= out["bpm"] <= 300):
        out["bpm"] = None
    try:
        easy = MutagenFile(path, easy=True)
        tags = easy.tags if easy is not None else None
    except Exception:  # noqa: BLE001
        tags = None
    if tags:
        try:
            out["genres"] = split_genres([str(v) for v in (tags.get("genre") or [])])
            date = (tags.get("date") or tags.get("originaldate") or [None])[0]
            match = re.match(r"\s*(\d{4})", str(date or ""))
            if match:
                out["year"] = int(match.group(1))
        except Exception:  # noqa: BLE001
            pass
    return out


def analyze_file(path: Path, duration_ms: int | None) -> dict:
    """Everything the engine wants for one file. Raises on an undecodable file."""
    tags = read_tag_features(path)
    samples = decode_excerpt(path, (duration_ms or 0) / 1000.0 or None)
    audio = analyze_samples(samples)
    return {
        "bpm": tags["bpm"] if tags["bpm"] is not None else audio["bpm"],
        "energy": audio["energy"],
        "brightness": audio["brightness"],
        "genres": tags["genres"],
        "year": tags["year"],
    }


# --------------------------------------------------------------------------------------------------
# Task plumbing
# --------------------------------------------------------------------------------------------------

def _tracks_needing_analysis(session: Session, limit: int) -> list[Track]:
    return list(
        session.scalars(
            select(Track)
            .outerjoin(TrackFeatures, TrackFeatures.track_id == Track.id)
            .where(or_(TrackFeatures.track_id.is_(None), TrackFeatures.analysis_version < ANALYSIS_VERSION))
            .where(Track.path.is_not(None))
            .order_by(Track.created_at.desc())
            .limit(limit)
        )
    )


def _artists_needing_refresh(session: Session, limit: int) -> list[Artist]:
    cutoff = datetime.now(timezone.utc) - ARTIST_REFRESH_AFTER
    return list(
        session.scalars(
            select(Artist)
            .outerjoin(ArtistSimilarity, ArtistSimilarity.artist_id == Artist.id)
            .where(Artist.musicbrainz_id.is_not(None))
            .where(or_(ArtistSimilarity.artist_id.is_(None), ArtistSimilarity.fetched_at < cutoff))
            .order_by(ArtistSimilarity.fetched_at.asc().nullsfirst())
            .limit(limit)
        )
    )


def analysis_pending(session: Session) -> bool:
    return bool(_tracks_needing_analysis(session, 1) or _artists_needing_refresh(session, 1))


def enqueue_analysis_if_needed(session: Session) -> bool:
    """Worker tick: queue one `analyze_audio` task when there is work and none is already pending."""
    active = session.scalar(
        select(func.count()).select_from(Task).where(
            Task.type == "analyze_audio", Task.status.in_([TaskStatus.queued, TaskStatus.running])
        )
    )
    if active or not analysis_pending(session):
        return False
    enqueue_task(session, "analyze_audio", {})
    return True


def run_analyze_audio(session: Session, payload: dict, task: Task | None = None) -> dict:
    """Analyze up to BATCH_TRACKS tracks, then refresh up to BATCH_ARTISTS artists' cached
    genres/similar artists. Cancellable; never raises for a single bad track or artist."""
    from nudibranch.services.artist_similarity import refresh_artist

    tracks = _tracks_needing_analysis(session, BATCH_TRACKS)
    artists = _artists_needing_refresh(session, BATCH_ARTISTS)
    progress = ScanProgress(session, task, len(tracks) + len(artists))
    analyzed = 0
    failed = 0
    for track in tracks:
        if not progress.step(f"Analyzing audio: {track.title}"):
            break
        path = Path(track.path) if track.path else None
        features = {"bpm": None, "energy": None, "brightness": None, "genres": [], "year": None}
        error = None
        try:
            if path is None or not path.exists():
                raise FileNotFoundError("file missing on disk")
            features = analyze_file(path, track.duration_ms)
        except Exception as exc:  # noqa: BLE001 - recorded so the track is not retried forever.
            error = str(exc)[:300] or type(exc).__name__
            failed += 1
        row = session.get(TrackFeatures, track.id)
        if row is None:
            row = TrackFeatures(track_id=track.id)
            session.add(row)
        row.bpm = features["bpm"]
        row.energy = features["energy"]
        row.brightness = features["brightness"]
        row.genres = json.dumps(features["genres"])
        row.year = features["year"]
        row.analysis_version = ANALYSIS_VERSION
        row.analyzed_at = datetime.now(timezone.utc)
        row.error = error
        session.commit()
        if error is None:
            analyzed += 1
    progress.log_if_canceled("Audio analysis")

    refreshed = 0
    for artist in artists:
        if progress.canceled or not progress.step(f"Fetching artist genres: {artist.name}"):
            break
        try:
            refresh_artist(session, artist)
            refreshed += 1
        except Exception as exc:  # noqa: BLE001 - one artist must never fail the task.
            session.rollback()
            write_app_log(f"Artist similarity refresh failed for {artist.name}: {exc}", "warning")
    progress.log_if_canceled("Artist similarity refresh")
    append_task_log(session, task, f"Audio analysis: {analyzed} analyzed, {failed} failed, {refreshed} artists refreshed")
    return {"analyzed": analyzed, "failed": failed, "artists_refreshed": refreshed}
