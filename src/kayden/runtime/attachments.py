"""The files a user attaches to a message (RuntimeAPI.send), read into its blocks."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from pathlib import Path

from kayden.api import File, Image

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # what model providers accept for one image
MAX_TEXT_BYTES = 200_000  # of a file whose content goes into the message


def read(paths: Sequence[str]) -> tuple[Image | File, ...]:
    """Each file as a block: an image the model can see, a text file with its content, or
    any other by its path alone. ValueError for one that cannot be read. Blocking."""
    return tuple(_read(Path(path).expanduser()) for path in paths)


def _read(path: Path) -> Image | File:
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"{path} is not a file")
        size = path.stat().st_size
        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if media_type is not None and size <= MAX_IMAGE_BYTES:
            return Image(media_type, base64.b64encode(path.read_bytes()).decode(), path.name)
        if size > MAX_TEXT_BYTES:
            return File(str(path))
        raw = path.read_bytes()
    except OSError as e:
        raise ValueError(f"cannot attach {path}: {e.strerror or e}") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return File(str(path))
    return File(str(path), None if "\x00" in text else text)
