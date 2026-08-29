"""
print_service.py

Requirement #9: Print engine for vouchers and reports.

Uses Jinja2 for HTML templating and xhtml2pdf (pisa) to convert HTML/CSS to PDF.
xhtml2pdf is chosen because it's pure Python and works without external system
dependencies (unlike WeasyPrint which needs GTK on Windows).

Each print function takes a database object or query result and returns
PDF bytes ready to be served via a FileResponse or BytesIO attachment.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from database.models import PurchaseMilk

# Template directory is at project root / templates
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def _html_to_pdf(html_string: str) -> bytes:
    """Convert HTML string to PDF bytes using xhtml2pdf."""
    result = BytesIO()
    pdf = pisa.CreatePDF(BytesIO(html_string.encode("utf-8")), dest=result)

    if pdf.err:
        raise RuntimeError(f"PDF generation failed with error code {pdf.err}")

    return result.getvalue()


def render_purchase_milk_voucher(voucher: PurchaseMilk) -> bytes:
    """
    Render one Purchase Milk voucher as a PDF.

    Args:
        voucher: PurchaseMilk database record

    Returns:
        PDF file bytes
    """
    template = _jinja_env.get_template("purchase_milk_voucher.html")

    html_content = template.render(
        voucher=voucher,
        now=datetime.now(),
    )

    return _html_to_pdf(html_content)


def render_purchase_milk_vouchers_batch(vouchers: list[PurchaseMilk]) -> bytes:
    """
    Render multiple Purchase Milk vouchers as a single multi-page PDF.

    Args:
        vouchers: List of PurchaseMilk database records

    Returns:
        PDF file bytes with one voucher per page
    """
    template = _jinja_env.get_template("purchase_milk_voucher.html")

    # Build a combined HTML document with page breaks between vouchers
    pages = []
    now = datetime.now()

    for voucher in vouchers:
        html_content = template.render(voucher=voucher, now=now)
        pages.append(html_content)

    # Insert CSS page break between vouchers
    # xhtml2pdf uses -pdf-page-break CSS property
    combined_html = '''<html><head><style>
        @page { margin: 1.5cm; }
        .page-break { -pdf-page-break-after: always; }
    </style></head><body>'''

    combined_html += '<div class="page-break">'.join(pages)
    combined_html += '</div></body></html>'

    return _html_to_pdf(combined_html)
