"""Reports of slab checks, in Word (python-docx) and PDF (fpdf2), decision 0005. The
text is for the engineer and so in Russian. A PDF is first asked of Word, from the
.docx (adapters/word.py), so that it looks the same; where Word cannot, ``to_pdf``
writes it from the same content."""

import logging
import struct
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.shared import Cm, Pt
from fpdf import FPDF
from fpdf.enums import XPos, YPos

PAGE_CM = (29.7, 21.0)
MARGIN_CM = 1.5
RESERVE_CM = 0.4  # so that a picture filling a page does not spill over to the next one
FONTS = Path("C:/Windows/Fonts")

# fpdf2 subsets fonts with fontTools, which logs every table it touches at INFO.
logging.getLogger("fontTools").setLevel(logging.WARNING)


@dataclass
class SlabPart:
    """One slab's section of a report: a heading, lines of text and pictures."""

    heading: str
    lines: list[str] = field(default_factory=list)
    pictures: list[Path] = field(default_factory=list)


@dataclass
class Report:
    """A report: title, what it is about, a summary table, then a section per slab."""

    title: str
    about: list[str]
    header: list[str]
    rows: list[list[str]]
    parts: list[SlabPart]
    notes: list[str] = field(default_factory=list)
    day: date = field(default_factory=date.today)


def to_docx(report: Report, path: Path) -> Path:
    doc = Document()
    section = doc.sections[0]
    _turn(section, False)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10)
    doc.add_heading(report.title, level=1)
    for line in report.about:
        doc.add_paragraph(line)
    doc.add_paragraph(f"Дата: {report.day:%d.%m.%Y}")
    doc.add_heading("Сводная таблица", level=2)
    table = doc.add_table(rows=1, cols=len(report.header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for cell, text in zip(table.rows[0].cells, report.header, strict=True):
        cell.text = text
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in report.rows:
        for cell, text in zip(table.add_row().cells, row, strict=True):
            cell.text = text
    for cell_row in table.rows:
        for cell in cell_row.cells:
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(8.5)
    for note in report.notes:
        doc.add_paragraph(note, style="List Bullet")
    for part in report.parts:
        _turn(doc.add_section(WD_SECTION.NEW_PAGE), False)
        doc.add_heading(part.heading, level=2)
        for line in part.lines:
            doc.add_paragraph(line)
        for picture in part.pictures:
            wide, width, height = _fitted(picture)
            _turn(doc.add_section(WD_SECTION.NEW_PAGE), wide)
            doc.add_picture(str(picture), width=Cm(width), height=Cm(height))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def to_pdf(report: Report, path: Path) -> Path:
    """The same report as a PDF without Word."""
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(20, 15, 20)
    pdf.set_auto_page_break(True, margin=15)
    family = _font(pdf)
    pdf.add_page()
    pdf.set_font(family, "B", 15)
    pdf.multi_cell(0, 7, report.title, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    for line in [*report.about, f"Дата: {report.day:%d.%m.%Y}"]:
        pdf.multi_cell(0, 5, line, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)
    pdf.ln(2)
    pdf.set_font(family, "B", 12)
    pdf.cell(0, 7, "Сводная таблица", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(family, "", 8)
    with pdf.table(text_align="CENTER", line_height=4.5) as table:
        head = table.row()
        for text in report.header:
            head.cell(text)
        for row in report.rows:
            r = table.row()
            for text in row:
                r.cell(text)
    pdf.set_font(family, "", 10)
    for note in report.notes:
        pdf.multi_cell(0, 5, "• " + note, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    for part in report.parts:
        pdf.add_page(orientation="P")
        pdf.set_font(family, "B", 12)
        pdf.multi_cell(0, 7, part.heading, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(family, "", 10)
        for line in part.lines:
            pdf.multi_cell(0, 5, line, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
        for picture in part.pictures:
            wide, width, height = _fitted(picture)
            pdf.add_page(orientation="L" if wide else "P")
            pdf.image(str(picture), x=MARGIN_CM * 10, y=MARGIN_CM * 10, w=width * 10, h=height * 10)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def _turn(section, wide: bool) -> None:
    """A section of A4, across or upright, with the report's margins."""
    long, short = Cm(PAGE_CM[0]), Cm(PAGE_CM[1])
    section.orientation = WD_ORIENT.LANDSCAPE if wide else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (long, short) if wide else (short, long)
    section.left_margin = section.right_margin = Cm(MARGIN_CM)
    section.top_margin = section.bottom_margin = Cm(MARGIN_CM)


def _fitted(picture: Path) -> tuple[bool, float, float]:
    """How a picture fills a page of its own: across or upright, its width and height, cm."""
    # A PNG holds its size in its header, right after the signature and the chunk's start.
    pixels_wide, pixels_high = struct.unpack(">II", picture.read_bytes()[16:24])
    wide = pixels_wide >= pixels_high
    long, short = PAGE_CM
    room_x, room_y = (long, short) if wide else (short, long)
    room_x, room_y = room_x - 2 * MARGIN_CM, room_y - 2 * MARGIN_CM - RESERVE_CM
    scale = min(room_x / pixels_wide, room_y / pixels_high)
    return wide, pixels_wide * scale, pixels_high * scale


def _font(pdf: FPDF) -> str:
    """Arial of Windows, else DejaVu Sans that comes with matplotlib: both have Cyrillic."""
    regular, bold = FONTS / "arial.ttf", FONTS / "arialbd.ttf"
    if not (regular.is_file() and bold.is_file()):
        import matplotlib

        ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        regular, bold = ttf / "DejaVuSans.ttf", ttf / "DejaVuSans-Bold.ttf"
    pdf.add_font("body", "", str(regular))
    pdf.add_font("body", "B", str(bold))
    return "body"
