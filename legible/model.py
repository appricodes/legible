"""Data model: a Page holds the scanned image plus its OCR result.

Kept deliberately simple and free of any GUI or OCR imports so it is easy
to test and reason about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class OcrBlock:
    """One layout block returned by the OCR engine.

    `label`   - block type, e.g. "Text", "SectionHeader", "Table", "Picture".
    `html`    - block content as HTML (tables come as <table>..</table>).
    `text`    - plain-text version of the block (tables flattened to rows).
    `bbox`    - [x0, y0, x1, y1] position on the page image (pixels).
    """

    label: str
    html: str
    text: str
    bbox: List[float] = field(default_factory=lambda: [0, 0, 0, 0])


@dataclass
class Page:
    """A single scanned + OCR'd page."""

    image_path: str
    blocks: List[OcrBlock] = field(default_factory=list)
    # Width/height of the source image in pixels (needed for PDF layout).
    image_width: int = 0
    image_height: int = 0
    # A user-given name. When empty, the UI shows the text preview instead.
    custom_name: str = ""
    page_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Assigned once by Session.add() and frozen from then on (see
    # list_label) - 0 means "not yet added to a session".
    auto_number: int = 0
    # Degrees (0 or 180) legible.orientation detected the *source* scan
    # image needs rotating to be upright - set by ocr.py, which already
    # OCR's the corrected orientation and export.py, which already draws
    # the corrected orientation into the PDF. Kept here only so the
    # original scan file's actual orientation can still be reported to the
    # user (see list_label) - image_path itself is never modified.
    rotation: int = 0

    # ---- derived text helpers -------------------------------------------

    @property
    def full_text(self) -> str:
        """All block texts joined in reading order, blank line between blocks."""
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())

    def preview(self, length: int = 128) -> str:
        """First `length` characters of the OCR text, single-line."""
        flat = " ".join(self.full_text.split())
        if len(flat) <= length:
            return flat
        return flat[:length].rstrip() + "\u2026"  # ellipsis

    def list_label(self, length: int = 128) -> str:
        """The page's title: the custom name verbatim once the user has
        renamed it (they own the whole string then), otherwise a default of
        "Page {auto_number}: {preview}". `auto_number` is assigned once by
        Session.add() and stays fixed after that - reordering pages (via
        move up/down) never renumbers them, only Rename changes a title.

        When the *auto-generated* title is shown (never a Rename'd one -
        the user owns that string once they've set it) and the original
        scan was detected upside down, a "(scanned upside down...)" note is
        inserted right after the page number - before the text preview, not
        after it - so it's the first thing read/seen, without having to
        open the page to notice, and so the user knows which way to
        physically turn the paper original when filing it."""
        if self.custom_name.strip():
            return self.custom_name.strip()
        prefix = f"Page {self.auto_number}"
        if self.rotation == 180:
            prefix += " (scanned upside down - rotate 180° before filing)"
        text = self.preview(length)
        return f"{prefix}: {text}" if text else f"{prefix} (no text detected)"


def back_side_index(page_count: int, scan_index: int) -> int:
    """Where to insert the `scan_index`-th (0-based) page of a back-sides
    batch, for two-sided documents scanned as fronts first, then the stack
    turned over: the backs arrive in reverse order (3B, 2B, 1B), so the
    first belongs after the last existing page, the next after the one
    before it, and so on. `page_count` is the page count before the batch
    began. Backs beyond the existing pages go to the start of the list."""
    return max(0, page_count - scan_index)


@dataclass
class Session:
    """An ordered collection of pages."""

    pages: List[Page] = field(default_factory=list)
    # Monotonic counter behind Page.auto_number - only ever goes up, so a
    # deleted/reordered page's number is never reused or shifted.
    _next_number: int = field(default=1, init=False, repr=False)

    # ---- list operations -------------------------------------------------

    def add(self, page: Page) -> int:
        return self.insert(len(self.pages), page)

    def insert(self, index: int, page: Page) -> int:
        """Insert `page` at `index` (clamped to the list). Returns its index."""
        page.auto_number = self._next_number
        self._next_number += 1
        index = max(0, min(index, len(self.pages)))
        self.pages.insert(index, page)
        return index

    def delete(self, index: int) -> None:
        if 0 <= index < len(self.pages):
            del self.pages[index]

    def move_up(self, index: int) -> int:
        """Swap the page with the one above it. Returns the new index."""
        if index <= 0 or index >= len(self.pages):
            return index
        self.pages[index - 1], self.pages[index] = (
            self.pages[index],
            self.pages[index - 1],
        )
        return index - 1

    def move_down(self, index: int) -> int:
        """Swap the page with the one below it. Returns the new index."""
        if index < 0 or index >= len(self.pages) - 1:
            return index
        self.pages[index + 1], self.pages[index] = (
            self.pages[index],
            self.pages[index + 1],
        )
        return index + 1

    def rename(self, index: int, name: str) -> None:
        if 0 <= index < len(self.pages):
            self.pages[index].custom_name = name

    def clear(self) -> None:
        self.pages.clear()
        self._next_number = 1

    def __len__(self) -> int:
        return len(self.pages)
