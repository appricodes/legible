"""Import existing image files or multi-page PDFs as scanned pages.

This is the counterpart to scanner.py: instead of acquiring a page from
hardware, it turns files the user already has into the same thing a scan
produces - an image path ready for legible.ocr.ocr_page(). A PDF becomes one
image per page (rendered via pypdfium2, no poppler/Ghostscript dependency).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Iterator, List

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
PDF_EXTENSION = ".pdf"

# DPI used to rasterize PDF pages. pypdfium2's native scale is 1.0 == 72 DPI.
RENDER_DPI = 300


@dataclass
class ImportItem:
    """One page ready for OCR, plus a human-readable label for progress UI."""

    image_path: str
    label: str  # e.g. "report.pdf (page 2 of 5)" or "scan1.png"


class ImportError_(Exception):
    """Raised when a file can't be read as an image or PDF."""


def _tmp_image_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="legible_import_")
    os.close(fd)
    return path


def _iter_pdf_pages(path: str) -> Iterator[ImportItem]:
    import pypdfium2 as pdfium

    name = os.path.basename(path)
    doc = pdfium.PdfDocument(path)
    try:
        total = len(doc)
        scale = RENDER_DPI / 72
        for i in range(total):
            page = doc[i]
            try:
                bitmap = page.render(scale=scale)
                try:
                    pil_image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            out_path = _tmp_image_path()
            pil_image.save(out_path)
            yield ImportItem(
                image_path=out_path,
                label=f"{name} (page {i + 1} of {total})",
            )
    finally:
        doc.close()


def iter_import_items(paths: List[str]) -> Iterator[ImportItem]:
    """Yield one ImportItem per page across all given files, in order.

    Raster images (png/jpg/tiff/bmp) yield a single item pointing at the
    original file. PDFs yield one item per page, each rendered to a temp PNG
    (the caller does not need to clean these up for correctness, but they do
    accumulate in the temp dir like scanner.py's own temp files).
    """
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext == PDF_EXTENSION:
            yield from _iter_pdf_pages(path)
        elif ext in IMAGE_EXTENSIONS:
            yield ImportItem(image_path=path, label=os.path.basename(path))
        else:
            raise ImportError_(f"Unsupported file type: {os.path.basename(path)}")


def count_pages(paths: List[str]) -> int:
    """Total page count across all files, for progress totals. PDFs are
    opened just to read their page count (cheap - no rendering)."""
    total = 0
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext == PDF_EXTENSION:
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(path)
            try:
                total += len(doc)
            finally:
                doc.close()
        elif ext in IMAGE_EXTENSIONS:
            total += 1
        else:
            raise ImportError_(f"Unsupported file type: {os.path.basename(path)}")
    return total
