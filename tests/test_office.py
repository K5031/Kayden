"""Word, Excel and PowerPoint files as text (shell_files.office): attached to a message,
and read by the file tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from kadence.api import File
from kadence.tools.shell_files import attachments, office
from tests.test_tools_shell_files import run, tools, workspace  # noqa: F401


def make_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("Launch plan", level=1)
    document.add_paragraph("We ship in March.")
    document.add_paragraph("Book the venue", style="List Bullet")
    table = document.add_table(rows=2, cols=2)
    for cell, text in zip((c for row in table.rows for c in row.cells),
                          ("Owner", "Task", "Ada", "Invites"), strict=True):
        cell.text = text
    document.save(str(path))


def make_xlsx(path: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook()
    sales = book.active
    sales.title = "Sales"
    for row in (("Region", "Units"), ("North", 12), ("South", 7.5)):
        sales.append(row)
    book.create_sheet("Notes").append(("Checked by Ada",))
    book.save(str(path))


def make_pptx(path: Path) -> None:
    import pptx

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Roadmap"
    slide.placeholders[1].text = "Beta in May"
    slide.notes_slide.notes_text_frame.text = "Mention the waitlist"
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Questions"
    deck.save(str(path))


def test_a_word_file_reads_as_headings_paragraphs_lists_and_tables(tmp_path: Path):
    path = tmp_path / "plan.docx"
    make_docx(path)
    assert office.to_text(path) == ("# Launch plan\n\nWe ship in March.\n\n- Book the venue\n\n"
                                    "| Owner | Task |\n| Ada | Invites |")


def test_an_excel_file_reads_as_its_sheets_rows(tmp_path: Path):
    path = tmp_path / "sales.xlsx"
    make_xlsx(path)
    assert office.to_text(path) == ("## Sheet: Sales\n| Region | Units |\n| North | 12 |\n"
                                    "| South | 7.5 |\n\n## Sheet: Notes\n| Checked by Ada |")


def test_a_powerpoint_file_reads_as_its_slides_with_their_notes(tmp_path: Path):
    path = tmp_path / "deck.pptx"
    make_pptx(path)
    assert office.to_text(path) == ("## Slide 1\nRoadmap\nBeta in May\n[Notes] Mention the "
                                    "waitlist\n\n## Slide 2\nQuestions")


def test_a_file_that_is_not_what_its_name_says_cannot_be_read(tmp_path: Path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="cannot be read"):
        office.to_text(path)


def test_an_attached_office_file_goes_with_its_text(tmp_path: Path):
    plan, broken = tmp_path / "plan.docx", tmp_path / "broken.xlsx"
    make_docx(plan)
    broken.write_bytes(b"nope")
    read, unread = attachments.read(plan), attachments.read(broken)
    assert read == File(str(plan), office.to_text(plan))
    assert unread == File(str(broken))  # by its path alone


async def test_the_file_tool_reads_office_files_like_text(tools, workspace, tmp_path):  # noqa: F811
    make_xlsx(workspace / "sales.xlsx")
    whole = await run(tools, tmp_path, "read_file", path="sales.xlsx")
    assert whole.content.startswith("## Sheet: Sales\n| Region | Units |")
    part = await run(tools, tmp_path, "read_file", path="sales.xlsx", offset=3, limit=1)
    assert part.content.startswith("| North | 12 |") and "use offset 4" in part.content
    (workspace / "broken.pptx").write_bytes(b"nope")
    assert (await run(tools, tmp_path, "read_file", path="broken.pptx")).is_error
