"""Persisted app settings: scanner choice/config and OCR engine choice.

Kept deliberately simple - a dataclass plus JSON load/save - and free of any
GUI imports so it is easy to test and reason about.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields


SETTINGS_PATH = os.path.expanduser("~/.legible/settings.json")

# "auto" picks the first backend whose library is importable (see
# scanner.has_hardware_backend / scanner.list_backends).
SCANNER_BACKENDS = ("auto", "sane", "twain", "wia")
COLOR_MODES = ("color", "gray", "bw")
# "auto" leaves the device's current/default paper source untouched (a single
# page) - except for WIA network scanners, where it scans the whole feeder
# whenever paper is loaded in it (see scanner._scan_wia). "flatbed"/"adf" force that source; "adf" scans every page currently
# loaded in the feeder in one Add page action. Only offered in Settings when
# scanner.list_sources() reports the selected device actually supports it.
SCANNER_SOURCES = ("auto", "flatbed", "adf")
# Docling always runs the pipeline (layout/table/heading detection); these
# are the two text-recognition backends the user can pick for it in
# Settings. "mock" is not user-selectable - it's an automatic fallback (see
# legible/ocr.py) when Docling or the chosen backend isn't installed.
OCR_ENGINES = ("easyocr", "rapidocr")
# Offered as a dropdown in Settings rather than a free-entry spinner.
PDF_IMAGE_QUALITY_CHOICES = tuple(range(5, 101, 5))  # 5, 10, 15, ..., 100
PDF_IMAGE_DPI_CHOICES = (50, 100, 150, 200, 250, 300)


@dataclass
class AppSettings:
    scanner_backend: str = "auto"
    # Device identifier as returned by scanner.list_devices(); "" means "use
    # the first device the backend finds" rather than a specific one.
    scanner_device: str = ""
    scanner_dpi: int = 300
    scanner_color_mode: str = "color"  # one of COLOR_MODES
    scanner_source: str = "auto"  # one of SCANNER_SOURCES
    ocr_engine: str = "easyocr"  # one of OCR_ENGINES (Docling's OCR backend)
    # Canonical language code (see legible/languages.py), applied to
    # whichever backend is active - EasyOCR's and RapidOCR's own defaults
    # are Chinese/English-tuned and mishandle other languages otherwise
    # (dropped diacritics, wrong punctuation, or an outright unsupported
    # language for EasyOCR - see ocr.check_ocr_backend).
    ocr_lang: str = "de"
    # JPEG quality for each page's scanned image in the PDF export - one of
    # PDF_IMAGE_QUALITY_CHOICES. Page images are re-encoded as JPEG at this
    # quality before embedding - without this, reportlab embeds non-JPEG
    # sources as lossless raster, which is far larger for photographic scan
    # content.
    pdf_image_quality: int = 85
    # Maximum resolution (one of PDF_IMAGE_DPI_CHOICES) for each page's image
    # as embedded in the PDF, relative to the printed page size - images are
    # downsampled to at most this DPI before embedding (never upsampled, so
    # a lower-resolution scan/import is left untouched). Lowers file size
    # further on top of pdf_image_quality with no visible difference at
    # normal viewing/printing sizes.
    pdf_image_dpi: int = 300


def load_settings() -> AppSettings:
    """Read settings from disk, filling in defaults for anything missing or
    unreadable. Never raises - a corrupt/missing settings file just means
    defaults, so the app always starts usable."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    valid_keys = {f.name for f in fields(AppSettings)}
    return AppSettings(**{k: v for k, v in data.items() if k in valid_keys})


def save_settings(settings: AppSettings) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(asdict(settings), fh, indent=2)
