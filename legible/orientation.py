"""Best-effort upside-down (180-degree) page detection.

Uses Tesseract's OSD (orientation and script detection) mode via
pytesseract - a fast, dedicated classifier (a few hundred ms), not full
OCR, so it's cheap to run on every page regardless of which OCR *backend*
(EasyOCR/RapidOCR, see legible/ocr.py) is configured for the actual text
recognition. This is a different use of Tesseract than the old "Tesseract
OCR engine" option that was removed - it is never offered as a choice in
Settings and never reads/returns page text, only a rotation estimate.

Purely optional: if the `pytesseract` package or the `tesseract` binary
isn't installed, detect_rotation() returns 0 (no correction applied)
rather than raising - orientation checking is a nice-to-have layered on
top of OCR, never a requirement for the app to work, matching how the rest
of Legible degrades gracefully when an optional dependency is missing.

Scope: only 90/180/270-degree rotation is meaningful to report; the app
only *acts* on 180 (the common "fed the page in upside-down" mistake) per
the feature request - see ocr.py's use of ROTATION_UPSIDE_DOWN.
"""

from __future__ import annotations

import re

ROTATION_UPSIDE_DOWN = 180


def is_available() -> bool:
    """True if orientation detection can actually run (pytesseract package
    + tesseract binary both present and runnable)."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def detect_rotation(image_path: str) -> int:
    """Return the degrees (0, 90, 180, or 270) Tesseract's OSD classifier
    says the image needs to be rotated *clockwise* to be upright, or 0 if
    detection isn't available or wasn't confident enough to trust.

    OSD needs a reasonable amount of text to work with - a mostly-blank
    page, a photo, or a very short scrap of text can yield a low-confidence
    or outright failing result; both are treated as "assume no rotation"
    (0) rather than risking a wrong correction on a real document.
    """
    try:
        import pytesseract
    except ImportError:
        return 0

    try:
        # --psm 0: OSD-only mode - classify orientation/script without
        # running full-page OCR, much faster than a real recognize pass.
        osd = pytesseract.image_to_osd(image_path, config="--psm 0")
    except Exception:
        return 0

    match = re.search(r"Rotate:\s*(\d+)", osd)
    if not match:
        return 0
    rotation = int(match.group(1))

    # Orientation confidence, when present, is unbounded but low single
    # digits are unreliable in practice (near-blank/sparse pages) - require
    # a modest minimum before trusting the result.
    conf_match = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    if conf_match and float(conf_match.group(1)) < 1.0:
        return 0

    return rotation if rotation in (0, 90, 180, 270) else 0
