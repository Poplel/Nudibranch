import json
from pathlib import Path
import re
import subprocess

from mutagen import File

from nudibranch.core.config import get_settings


SUPPORTED_AUDIO_EXTENSIONS = {".flac", ".alac", ".m4a", ".wav", ".aiff", ".aif", ".mp3", ".ogg", ".opus"}
UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"
UNKNOWN_TITLE = "Unknown Title"


def discover_import_files(path: str | None = None, include_fingerprint: bool = False) -> list[dict]:
    settings = get_settings()
    root = Path(path) if path else settings.import_path
    root = root.resolve()
    allowed_root = settings.import_path.resolve()
    if allowed_root not in [root, *root.parents]:
        raise ValueError("Import scans must stay inside /app/import")

    files = []
    if not root.exists():
        return files

    for file_path in sorted(root.rglob("*")):
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
            stat = file_path.stat()
            metadata = read_audio_metadata(file_path)
            fingerprint = fingerprint_audio(file_path) if include_fingerprint else None
            suggested_path = suggest_library_path(metadata, file_path)
            files.append(
                {
                    "path": str(file_path),
                    "relative_path": str(file_path.relative_to(allowed_root)),
                    "extension": file_path.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "metadata": metadata,
                    "fingerprint": fingerprint,
                    "suggested_library_path": str(suggested_path),
                }
            )
    return files


def read_audio_metadata(file_path: Path) -> dict:
    fallback = metadata_from_path(file_path)
    try:
        audio = File(file_path, easy=True)
    except Exception:
        return fallback
    if audio is None:
        return fallback

    tags = audio.tags or {}
    metadata = {
        "artist": first_tag(tags, "artist") or fallback["artist"],
        "album": first_tag(tags, "album") or fallback["album"],
        "title": first_tag(tags, "title") or fallback["title"],
        "albumartist": first_tag(tags, "albumartist"),
        "track_number": parse_number(first_tag(tags, "tracknumber")),
        "disc_number": parse_number(first_tag(tags, "discnumber")),
        "genre": first_tag(tags, "genre"),
        "date": first_tag(tags, "date"),
        "musicbrainz_artist_id": first_tag(tags, "musicbrainz_artistid"),
        "musicbrainz_album_id": first_tag(tags, "musicbrainz_albumid"),
        "musicbrainz_recording_id": first_tag(tags, "musicbrainz_trackid"),
        "duration_ms": int(audio.info.length * 1000) if getattr(audio.info, "length", None) else None,
        "bitrate": getattr(audio.info, "bitrate", None),
        "format": file_path.suffix.lower().lstrip("."),
    }
    metadata["is_lossless"] = metadata["format"] in {"flac", "alac", "wav", "aiff", "aif"}
    return metadata


def write_audio_metadata(file_path: Path, metadata: dict) -> None:
    audio = File(file_path, easy=True)
    if audio is None:
        raise ValueError(f"{file_path} is not a supported audio file")
    if audio.tags is None:
        audio.add_tags()
    tag_values = {
        "artist": metadata.get("artist"),
        "albumartist": metadata.get("albumartist") or metadata.get("artist"),
        "album": metadata.get("album"),
        "title": metadata.get("title"),
        "tracknumber": metadata_number(metadata.get("track_number")),
        "discnumber": metadata_number(metadata.get("disc_number")),
        "date": metadata.get("date"),
        "genre": metadata.get("genre"),
        "musicbrainz_trackid": metadata.get("musicbrainz_recording_id"),
        "musicbrainz_albumid": metadata.get("musicbrainz_album_id"),
        "musicbrainz_artistid": metadata.get("musicbrainz_artist_id"),
    }
    for key, value in tag_values.items():
        if value is None or value == "":
            continue
        try:
            audio[key] = [str(value)]
        except Exception:
            continue
    audio.save()


def metadata_number(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def metadata_from_path(file_path: Path) -> dict:
    settings = get_settings()
    try:
        relative_parts = file_path.resolve().relative_to(settings.import_path.resolve()).parts
    except ValueError:
        relative_parts = file_path.parts

    title = clean_name(file_path.stem) or UNKNOWN_TITLE
    album = clean_name(relative_parts[-2]) if len(relative_parts) >= 2 else UNKNOWN_ALBUM
    artist = clean_name(relative_parts[-3]) if len(relative_parts) >= 3 else UNKNOWN_ARTIST
    return {
        "artist": artist or UNKNOWN_ARTIST,
        "album": album or UNKNOWN_ALBUM,
        "title": title,
        "albumartist": None,
        "track_number": parse_number(title),
        "disc_number": None,
        "genre": None,
        "date": None,
        "musicbrainz_artist_id": None,
        "musicbrainz_album_id": None,
        "musicbrainz_recording_id": None,
        "duration_ms": None,
        "bitrate": None,
        "format": file_path.suffix.lower().lstrip("."),
        "is_lossless": file_path.suffix.lower() in {".flac", ".alac", ".wav", ".aiff", ".aif"},
    }


def first_tag(tags: dict, key: str) -> str | None:
    value = tags.get(key)
    if isinstance(value, list) and value:
        return str(value[0])
    if value:
        return str(value)
    return None


def parse_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def clean_name(value: str) -> str:
    return re.sub(r"^\s*\d+\s*[-_. ]\s*", "", value).strip()


def safe_path_part(value: str | None, fallback: str) -> str:
    cleaned = (value or fallback).strip() or fallback
    cleaned = re.sub(r"[/:*?\"<>|]", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(". ")


def suggest_library_path(metadata: dict, file_path: Path) -> Path:
    settings = get_settings()
    artist = safe_path_part(metadata.get("albumartist") or metadata.get("artist"), UNKNOWN_ARTIST)
    album = safe_path_part(metadata.get("album"), UNKNOWN_ALBUM)
    title = safe_path_part(metadata.get("title"), UNKNOWN_TITLE)
    track_number = metadata.get("track_number")
    prefix = f"{track_number:02d}" if isinstance(track_number, int) else "#"
    return settings.library_path / artist / album / f"{prefix}-{title}{file_path.suffix.lower()}"


def fingerprint_audio(file_path: Path) -> dict | None:
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(file_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout}
