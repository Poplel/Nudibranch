from pathlib import Path

from nudibranch.core.config import get_settings


SUPPORTED_AUDIO_EXTENSIONS = {".flac", ".alac", ".m4a", ".wav", ".aiff", ".aif", ".mp3", ".ogg", ".opus"}


def discover_import_files(path: str | None = None) -> list[dict]:
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
            files.append(
                {
                    "path": str(file_path),
                    "relative_path": str(file_path.relative_to(allowed_root)),
                    "extension": file_path.suffix.lower(),
                    "size_bytes": file_path.stat().st_size,
                }
            )
    return files

