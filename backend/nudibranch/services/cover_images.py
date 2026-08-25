"""Cover art normalisation — every cover the user supplies is stored square.

Cover art is drawn in square frames on every surface there is: the web grids and the player, the
iOS grids, the lock screen, CarPlay. A 16:9 photo handed to those either letterboxes into dead
bands or gets cropped a different way by each one, so the same artist ends up looking different
depending on where you are. Cropping once, on the way in, is what makes them agree.

⚠️ This belongs on the SERVER rather than in each client. It is the one choke point every cover
passes through — the multipart uploads from any client, and the `/cover-from-url` candidate picks
the server downloads itself — so a client that forgets to crop, or a client written later, still
cannot store a cover that isn't square. Clients may crop as well (iOS does, so the bytes it caches
locally match what it just uploaded), but nothing depends on them doing it.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Big enough for a retina full-screen player, small enough that a 6000px phone photo doesn't sit in
# the library folder forever. Only applied to art we are already re-encoding.
MAX_COVER_EDGE = 2048
JPEG_QUALITY = 92


def square_cover_bytes(content: bytes, ext: str) -> tuple[bytes, str]:
    """Centre-crop `content` to a square, returning (bytes, extension).

    Art that is already square is returned **byte-for-byte untouched**: re-encoding a cover that is
    already the right shape only costs quality, and most real cover art arrives square. The
    extension can change (a cropped image is re-encoded as JPEG, or PNG when it has transparency),
    so callers must use the one returned here to name the file.

    Anything Pillow cannot read comes back unchanged. Callers have already checked the file's magic
    bytes, so a failure here means an image we can serve but not parse — better stored as-is than
    rejected.
    """
    try:
        with Image.open(io.BytesIO(content)) as image:
            # Orientation first: a phone photo can be stored landscape with an EXIF flag saying
            # "rotate me". Cropping before honouring that crops the wrong axis.
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if width <= 0 or height <= 0:
                return content, ext
            if width == height and max(width, height) <= MAX_COVER_EDGE:
                return content, ext

            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            square = image.crop((left, top, left + side, top + side))
            if side > MAX_COVER_EDGE:
                square = square.resize((MAX_COVER_EDGE, MAX_COVER_EDGE), Image.LANCZOS)

            # Transparency survives as PNG; flattening it onto white or black would show as a hard
            # box behind a logo that was meant to sit on the page.
            has_alpha = square.mode in ("RGBA", "LA") or (square.mode == "P" and "transparency" in square.info)
            buffer = io.BytesIO()
            if has_alpha:
                square.convert("RGBA").save(buffer, format="PNG", optimize=True)
                return buffer.getvalue(), ".png"
            square.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return buffer.getvalue(), ".jpg"
    except Exception:  # noqa: BLE001 - a cover we cannot parse is still a cover we can serve
        logger.warning("Could not square-crop cover art; storing it unchanged", exc_info=True)
        return content, ext
