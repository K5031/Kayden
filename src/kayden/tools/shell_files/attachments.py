"""A file the user attached to a message, read into the block the message carries
(Tools.attach)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pypdf
import pypdf.errors

from kayden.api import Attachment, Document, File, Image
from kayden.tools.shell_files import office

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # what model providers accept for one image
MAX_TEXT_BYTES = 200_000  # of a file whose content goes into the message
# A PDF goes to the provider whole, base64, in a request of at most 32 MB and 600 pages.
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 600
MAX_OFFICE_BYTES = 20 * 1024 * 1024  # of a Word, Excel or PowerPoint file read as text


def read(path: Path) -> Attachment:
    """The file as a block: an image the model can see, a PDF with its pages and text, a
    Word, Excel or PowerPoint file or a text file with its content, or any other by its
    path alone. ValueError when it cannot be read. Blocking."""
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"{path} is not a file")
        size = path.stat().st_size
        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if media_type is not None and size <= MAX_IMAGE_BYTES:
            return Image(media_type, base64.b64encode(path.read_bytes()).decode(), path.name)
        if path.suffix.lower() == ".pdf" and size <= MAX_PDF_BYTES:
            return _pdf(path)
        if path.suffix.lower() in office.KINDS and size <= MAX_OFFICE_BYTES:
            return _office(path)
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


def _pdf(path: Path) -> Document | File:
    """The PDF with the text of its pages; by its path alone when it cannot be read as one
    or has too many pages."""
    raw = path.read_bytes()
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if len(reader.pages) > MAX_PDF_PAGES:
            return File(str(path))
        text = "\n\n".join(page.extract_text().strip() for page in reader.pages).strip()
    except (pypdf.errors.PyPdfError, ValueError, KeyError, OSError):
        return File(str(path))
    return Document(str(path), base64.b64encode(raw).decode(), text, len(reader.pages))


def _office(path: Path) -> File:
    """A Word, Excel or PowerPoint file with its text; by its path alone when it cannot be
    read or its text is too long to include."""
    try:
        text = office.to_text(path)
    except ValueError:
        return File(str(path))
    return File(str(path), text if len(text.encode()) <= MAX_TEXT_BYTES else None)
