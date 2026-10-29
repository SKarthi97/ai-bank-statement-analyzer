"""Regenerate the synthetic PDF fixtures: ``python tests/fixtures/build_pdf_fixtures.py``.

All data is invented. The PDFs are committed so tests do not depend on reportlab being run.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).parent
STYLES = getSampleStyleSheet()

# Same transactions as deposit_basic.csv, so the same hand-computed oracle applies.
BASIC = [
    ("2024-01-02", "SALARY ACME CORP", "3000.00", "3500.00"),
    ("2024-01-03", "GROCERY MART #1234 CARD 5678", "-85.40", "3414.60"),
    ("2024-01-15", "CITY POWER AND LIGHT", "-120.00", "3294.60"),
    ("2024-01-31", "NETFLIX.COM", "-15.99", "3278.61"),
    ("2024-02-01", "ATM WITHDRAWAL MAIN ST", "-200.00", "3078.61"),
    ("2024-02-05", "GROCERY MART #1234 CARD 5678", "-92.10", "2986.51"),
    ("2024-02-05", "GROCERY MART #1234 CARD 5678", "-92.10", "2894.41"),
    ("2024-02-10", "REFUND GROCERY MART", "12.50", "2906.91"),
    ("2024-02-29", "SALARY ACME CORP", "3000.00", "5906.91"),
]
HEADER = ["Date", "Description", "Amount", "Balance"]


def _grid(rows: list[list[str]], widths: list[int]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=0)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    return table


def statement_ruled() -> None:
    """Ruled table over two pages; the header row is repeated on page 2."""
    doc = SimpleDocTemplate(str(HERE / "statement_ruled.pdf"), pagesize=A4)
    widths = [70, 230, 70, 70]
    story = [
        Paragraph("Statement for Checking (synthetic)", STYLES["Title"]),
        Spacer(1, 12),
        _grid([HEADER, *[list(r) for r in BASIC[:5]]], widths),
        PageBreak(),
        _grid([HEADER, *[list(r) for r in BASIC[5:]]], widths),
    ]
    doc.build(story)


def statement_debit_credit() -> None:
    """Ruled debit/credit layout with blank cells (same data as bank_preamble_footer.csv)."""
    rows = [
        ["Transaction Date", "Details", "Debit", "Credit", "Balance"],
        ["2024-03-01", "RENT, MARCH", "900.00", "", "1100.00"],
        ["2024-03-05", "SUPERMARKET BRANCH 12", "54.30", "", "1045.70"],
        ["2024-03-09", "PAYMENT RECEIVED", "", "200.00", "1245.70"],
        ["", "Total", "954.30", "200.00", ""],
    ]
    doc = SimpleDocTemplate(str(HERE / "statement_debit_credit.pdf"), pagesize=A4)
    doc.build(
        [Paragraph("Acme Bank (synthetic)", STYLES["Title"]), _grid(rows, [80, 180, 60, 60, 70])]
    )


def statement_borderless() -> None:
    """No drawn lines and multi-word descriptions: the text detector fragments the cells."""
    c = canvas.Canvas(str(HERE / "statement_borderless.pdf"), pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, 800, "Statement for Checking (synthetic)")
    c.setFont("Helvetica", 9)
    y = 760
    c.drawString(50, y, "Date")
    c.drawString(140, y, "Description")
    c.drawRightString(420, y, "Amount")
    c.drawRightString(500, y, "Balance")
    for date, desc, amount, balance in BASIC:
        y -= 18
        c.drawString(50, y, date)
        c.drawString(140, y, desc)
        c.drawRightString(420, y, amount)
        c.drawRightString(500, y, balance)
    c.save()


def statement_borderless_simple() -> None:
    """Borderless but with short single-word descriptions: columns are cleanly separable."""
    rows = [
        ("2024-01-02", "SALARY", "3000.00", "3500.00"),
        ("2024-01-03", "GROCERY", "-85.40", "3414.60"),
        ("2024-01-15", "POWER", "-120.00", "3294.60"),
    ]
    c = canvas.Canvas(str(HERE / "statement_borderless_simple.pdf"), pagesize=A4)
    c.setFont("Helvetica", 9)
    y = 760
    c.drawString(50, y, "Date")
    c.drawString(140, y, "Description")
    c.drawRightString(420, y, "Amount")
    c.drawRightString(500, y, "Balance")
    for date, desc, amount, balance in rows:
        y -= 18
        c.drawString(50, y, date)
        c.drawString(140, y, desc)
        c.drawRightString(420, y, amount)
        c.drawRightString(500, y, balance)
    c.save()


def image_only() -> None:
    """A page that is only a raster image: no text layer (what a scan looks like to the parser)."""
    png = HERE / "_scan_tmp.png"
    Image.new("L", (400, 300), 230).save(png)
    c = canvas.Canvas(str(HERE / "image_only.pdf"), pagesize=A4)
    c.drawImage(str(png), 50, 400, width=400, height=300)
    c.save()
    png.unlink()


def encrypted() -> None:
    c = canvas.Canvas(str(HERE / "encrypted.pdf"), pagesize=A4, encrypt="s3cret")
    c.drawString(50, 800, "Date Description Amount")
    c.save()


def active_content() -> None:
    """A page carrying a JavaScript action: must be reported, never run.

    Built by hand because reportlab has no JavaScript API. Byte offsets are computed so the
    result is a well-formed PDF.
    """
    stream = b"BT /F1 12 Tf 50 700 Td (harmless page) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /OpenAction 5 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
        b"/BaseFont /Helvetica >> >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /S /JavaScript /JS (app.alert\\('should never run'\\);) >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    (HERE / "active_content.pdf").write_bytes(bytes(out))


if __name__ == "__main__":
    for build in (
        statement_ruled,
        statement_debit_credit,
        statement_borderless,
        statement_borderless_simple,
        image_only,
        encrypted,
        active_content,
    ):
        build()
    print("fixtures written to", HERE)
