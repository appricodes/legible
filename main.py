#!/usr/bin/env python3
"""Run the Legible application.

    python main.py

Set LEGIBLE_FORCE_MOCK=1 to always use the built-in mock OCR (no model),
useful for trying the interface and export features on any machine.

OCR engine and language are chosen in the Settings dialog (PaddleOCR,
RapidOCR, or Tesseract - see legible/ocr.py). None need a separate setup
step except Tesseract's own binary/language pack (not a pip package - see
README): model weights for the others download automatically from Hugging
Face the first time you add a page and are cached after that.
"""

from legible.gui import main as gui_main


if __name__ == "__main__":
    gui_main()
