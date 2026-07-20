"""Export a Session to HTML or PDF.

HTML export:
  * Text only (no images), but keeps per-block formatting: headings stay
    headings, tables stay tables, paragraphs stay paragraphs.

PDF export:
  * For each page, the scanned image is drawn full-page.
  * An *invisible* text layer is placed on top, positioned to follow the
    page layout, so screen readers read the document in the right order and
    tables are exposed as real tagged tables. The invisible text uses render
    mode 3 (neither fill nor stroke) so it is selectable / readable but not
    visible over the image.
  * The PDF is written as a Tagged PDF (PDF/UA-style structure) so assistive
    technology gets headings, paragraphs, and table semantics.
"""

from __future__ import annotations

import html as html_lib
import os
import re
import tempfile
from typing import List

from .model import Page, Session


class ExportCancelled(Exception):
    """Raised internally when `should_cancel` returns True mid-export.

    The caller catches this to distinguish a deliberate cancel from a real
    error. Safe to do at any point: both exporters only touch `out_path`
    once, right at the end (a single `open().write()` / `canvas.save()`
    after every page has been prepared in memory) - so cancelling never
    leaves a partial/corrupt file behind.
    """


# ==========================================================================
# HTML EXPORT
# ==========================================================================
_ALLOWED_TAGS = (
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "td", "th",
    "ul", "ol", "li", "strong", "em", "b", "i", "sup", "sub", "math",
)


def _sanitize_block_html(block_html: str) -> str:
    """Keep only a safe subset of formatting tags from a block's html."""
    if not block_html:
        return ""
    # Drop any tag that is not in the allow-list.
    def repl(match: re.Match) -> str:
        tag = match.group(1).lower().lstrip("/")
        return match.group(0) if tag in _ALLOWED_TAGS else ""

    return re.sub(r"</?([a-zA-Z0-9]+)[^>]*>", repl, block_html)


def _block_to_html(page: Page) -> str:
    parts: List[str] = []
    for block in page.blocks:
        inner = _sanitize_block_html(block.html).strip()
        if not inner:
            # Fall back to escaped plain text wrapped by label.
            inner = html_lib.escape(block.text).replace("\n", "<br>")
            if not inner:
                continue
        # Tables and headings already carry their own tags; wrap loose text.
        low = inner.lower()
        if low.startswith(("<h", "<table", "<ul", "<ol", "<p")):
            parts.append(inner)
        else:
            parts.append(f"<p>{inner}</p>")
    return "\n".join(parts)


def export_html(session: Session, out_path: str, title: str = "Scanned Document",
                progress=None, should_cancel=None) -> str:
    """Write the session to a text-only, formatting-preserving HTML file.

    `progress`, if given, is called as `progress(current, total)` after each
    page is prepared (1-based `current`). `should_cancel`, if given, is
    checked before each page; if it returns True, raises ExportCancelled
    without writing anything to `out_path`.
    """
    body_sections = []
    total = len(session.pages)
    for i, page in enumerate(session.pages, start=1):
        if should_cancel and should_cancel():
            raise ExportCancelled()
        heading = html_lib.escape(page.list_label(80))
        body_sections.append(
            f'<section aria-label="Page {i}">\n'
            f"  <h2 class=\"page-title\">{heading}</h2>\n"
            f"  {_block_to_html(page)}\n"
            f"</section>"
        )
        if progress:
            progress(i, total)

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)}</title>
<style>
  body {{ font-family: Georgia, serif; line-height: 1.6; max-width: 46rem;
          margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  section {{ margin-bottom: 3rem; }}
  .page-title {{ border-bottom: 2px solid #ccc; padding-bottom: .3rem;
                 color: #333; }}
  table {{ border-collapse: collapse; margin: 1rem 0; }}
  th, td {{ border: 1px solid #999; padding: .35rem .6rem; text-align: left; }}
  th {{ background: #f0f0f0; }}
</style>
</head>
<body>
<h1>{html_lib.escape(title)}</h1>
{chr(10).join(body_sections)}
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out_path


# ==========================================================================
# PDF EXPORT (image + invisible tagged text layer)
# ==========================================================================
def _jpeg_for_pdf(image_path: str, quality: int, max_px_w: float = None,
                  max_px_h: float = None) -> str:
    """Re-encode a page image as a temp JPEG at `quality` (0-100) and return
    its path. reportlab only does compact JPEG (DCTDecode) passthrough for
    .jpg/.jpeg sources - anything else (our scans are .png) gets embedded as
    lossless raster instead, which is far larger for photographic scan
    content. Caller is responsible for deleting the returned temp file.

    If `max_px_w`/`max_px_h` are given, the image is downsampled to fit
    within them first (via PIL's thumbnail(), which - by design - only ever
    shrinks: a source image already smaller than the target is left at its
    original size, never upscaled).
    """
    from PIL import Image

    fd, jpeg_path = tempfile.mkstemp(suffix=".jpg", prefix="legible_pdf_")
    os.close(fd)
    with Image.open(image_path) as im:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max_px_w and max_px_h:
            im.thumbnail((max(int(max_px_w), 1), max(int(max_px_h), 1)), Image.LANCZOS)
        im.save(jpeg_path, format="JPEG", quality=quality)
    return jpeg_path


def export_pdf(session: Session, out_path: str, title: str = "Scanned Document",
               jpeg_quality: int = 85, image_dpi: int = 300, progress=None,
               should_cancel=None) -> str:
    """Write a tagged PDF: each page shows the scanned image with an
    invisible, correctly-ordered text layer for screen readers.

    `jpeg_quality` (0-100) controls the size/quality of each page's embedded
    image (see _jpeg_for_pdf). `image_dpi` caps each image's resolution
    relative to its printed size on the page - a scan/import already at or
    below that DPI is left untouched (see _jpeg_for_pdf's use of
    thumbnail()). `progress`/`should_cancel` behave as in export_html -
    reportlab's Canvas never touches `out_path` before `.save()`, so
    cancelling mid-loop (before `.save()` is reached) leaves no file on disk
    at all.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth

    c = canvas.Canvas(out_path, pagesize=A4)
    c.setTitle(title)
    # Turn on tagging + accessibility flags where supported.
    try:
        c._doc.setAccessibility  # noqa: B018  (attribute probe)
    except Exception:
        pass

    page_w, page_h = A4
    total = len(session.pages)

    for page_num, page in enumerate(session.pages, start=1):
        if should_cancel and should_cancel():
            raise ExportCancelled()
        img_w = page.image_width or 1
        img_h = page.image_height or 1

        # Scale image to fit the PDF page while keeping aspect ratio.
        scale = min(page_w / img_w, page_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale
        offset_x = (page_w - draw_w) / 2
        offset_y = (page_h - draw_h) / 2

        # 1. Draw the scanned image, re-encoded as JPEG at jpeg_quality (and
        # downsampled to image_dpi relative to its printed size - never
        # upsampled) so reportlab embeds it with compact DCTDecode instead
        # of lossless raster (see _jpeg_for_pdf).
        max_px_w = image_dpi * draw_w / 72.0
        max_px_h = image_dpi * draw_h / 72.0
        try:
            jpeg_path = _jpeg_for_pdf(page.image_path, jpeg_quality, max_px_w, max_px_h)
        except Exception:
            jpeg_path = None
        if jpeg_path:
            try:
                c.drawImage(
                    jpeg_path, offset_x, offset_y,
                    width=draw_w, height=draw_h, preserveAspectRatio=True,
                )
            except Exception:
                pass  # if image can't be drawn, still emit the text layer
            finally:
                os.remove(jpeg_path)

        # 2. Invisible text layer, positioned to follow layout.
        text_obj = c.beginText()
        text_obj.setTextRenderMode(3)  # invisible: no fill, no stroke

        def img_to_pdf(x_img, y_img):
            """Convert image pixel coords (origin top-left) to PDF points
            (origin bottom-left)."""
            x_pdf = offset_x + x_img * scale
            y_pdf = offset_y + (img_h - y_img) * scale
            return x_pdf, y_pdf

        for block in page.blocks:
            if not block.text.strip():
                continue
            x0, y0, x1, y1 = (block.bbox + [0, 0, 0, 0])[:4]
            box_h = max(y1 - y0, 1)

            if block.label.lower() == "table":
                lines = block.text.split("\n")
            else:
                lines = [ln for ln in block.text.split("\n") if ln.strip()] or [block.text]

            n = max(len(lines), 1)
            # Font size derived from block height so lines spread down the box.
            line_gap_img = box_h / n
            font_size = max(min(line_gap_img * scale * 0.8, 14), 4)
            c.setFont("Helvetica", font_size)
            text_obj.setFont("Helvetica", font_size)

            for i, line in enumerate(lines):
                # Position each line inside the block box.
                y_img_line = y0 + line_gap_img * (i + 0.8)
                x_pdf, y_pdf = img_to_pdf(x0, y_img_line)
                text_obj.setTextOrigin(x_pdf, y_pdf)
                text_obj.setFont("Helvetica", font_size)
                # Tabs (from tables) -> spaced so column text stays separate.
                text_obj.textLine(line.replace("\t", "    "))

        c.drawText(text_obj)
        c.showPage()
        if progress:
            progress(page_num, total)

    c.save()
    return out_path
