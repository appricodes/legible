#!/usr/bin/env python3
"""Run the Legible application.

    python main.py

Set LEGIBLE_FORCE_MOCK=1 to always use the built-in mock OCR (no model),
useful for trying the interface and export features on any machine.

OCR always runs through Docling; the text-recognition backend (EasyOCR or
RapidOCR) and language are chosen in the Settings dialog (see
legible/ocr.py). Model weights for whichever backend is selected download
automatically the first time you add a page and are cached after that.
"""

from legible.gui import main as gui_main


if __name__ == "__main__":
    gui_main()
