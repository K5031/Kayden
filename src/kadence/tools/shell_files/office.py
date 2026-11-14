"""Word, Excel and PowerPoint files as text a model can read: for a file the user attaches
(attachments.py) and one the agent reads (read_file)."""

from __future__ import annotations

from pathlib import Path
from typing import IO, Any

KINDS = {".docx": "Word", ".xlsx": "Excel", ".pptx": "PowerPoint"}


def to_text(source: Path | IO[bytes], suffix: str | None = None) -> str:
    """The document's text: headings, paragraphs, lists and tables of a Word file; the rows
    of each sheet of an Excel file; each slide of a PowerPoint file with its notes.
    `suffix` says which it is when `source` is an open file. ValueError when it cannot be
    read as one. Blocking."""
    suffix = (suffix or (source.suffix if isinstance(source, Path) else "")).lower()
    read = {".docx": _word, ".xlsx": _excel, ".pptx": _powerpoint}.get(suffix)
    if read is None:
        raise ValueError(f"{suffix or 'this file'} is not a Word, Excel or PowerPoint file")
    try:
        return read(source)
    except Exception as e:
        name = source.name if isinstance(source, Path) else "It"
        raise ValueError(f"{name} cannot be read as a {KINDS[suffix]} file: "
                         f"{type(e).__name__}") from e


def _row(cells: Any) -> str:
    return "| " + " | ".join(" ".join(str(c).split()) for c in cells) + " |"


def _word(source: Path | IO[bytes]) -> str:
    import docx
    from docx.table import Table

    blocks = []
    for block in docx.Document(str(source) if isinstance(source, Path) else source
                               ).iter_inner_content():
        if isinstance(block, Table):
            blocks.append("\n".join(_row(c.text for c in row.cells) for row in block.rows))
            continue
        text = block.text.strip()
        if not text:
            continue
        style = block.style.name if block.style is not None and block.style.name else ""
        if style == "Title":
            text = f"# {text}"
        elif style.startswith("Heading ") and style[8:].isdigit():
            text = f"{'#' * int(style[8:])} {text}"
        elif style.startswith("List Bullet"):
            text = f"- {text}"
        elif style.startswith("List Number"):
            text = f"1. {text}"
        blocks.append(text)
    return "\n\n".join(blocks)


def _excel(source: Path | IO[bytes]) -> str:
    import openpyxl

    book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        sheets = []
        for sheet in book.worksheets:
            lines = [f"## Sheet: {sheet.title}"]
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell(value) for value in row]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    lines.append(_row(cells))
            sheets.append("\n".join(lines))
        return "\n\n".join(sheets)
    finally:
        book.close()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _powerpoint(source: Path | IO[bytes]) -> str:
    import pptx

    slides = []
    deck = pptx.Presentation(str(source) if isinstance(source, Path) else source)
    for number, slide in enumerate(deck.slides, 1):
        lines = [f"## Slide {number}"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [line for p in shape.text_frame.paragraphs
                          if (line := "".join(run.text for run in p.runs).strip())]
            if getattr(shape, "has_table", False):
                lines += [_row(c.text for c in row.cells) for row in shape.table.rows]
        if slide.has_notes_slide and (notes := slide.notes_slide.notes_text_frame) is not None \
                and notes.text.strip():
            lines.append(f"[Notes] {' '.join(notes.text.split())}")
        slides.append("\n".join(lines))
    return "\n\n".join(slides)
