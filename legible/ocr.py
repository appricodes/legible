"""OCR: PaddleOCR (PP-StructureV3), RapidOCR, or Tesseract - chosen and
configured (including language) via Settings.

Three real engines are available, trading structure-awareness and accuracy
for speed and install simplicity:

* PaddleOCR / PP-StructureV3 (_PaddleEngine) runs layout detection + OCR +
  table recognition as one pipeline: `PPStructureV3().predict(image_path)`
  returns a result whose `parsing_res_list` is a reading-order list of layout
  blocks, each with a `label` (e.g. "text", "doc_title", "table"), a `bbox`,
  and `content` - plain text for most labels, or ready-made
  `<table>...</table>` HTML for tables. Slowest (loads ~10 CPU models) but
  structure-aware: headings and tables come out as real headings and tables.

* RapidOCR (_RapidEngine) is a lightweight ONNX det+rec pipeline: just text
  boxes and strings, no layout/heading/table awareness. Every non-empty
  detected line becomes its own "Text" block. Faster and lighter than
  PaddleOCR.

* Tesseract (_TesseractEngine, via pytesseract) is the fastest and lightest
  of all three, but is the one engine that needs a separate, non-pip system
  install (the `tesseract` binary, plus a per-language .traineddata file -
  see README). Word-level detection, grouped into line blocks using
  Tesseract's own block/paragraph/line layout analysis.

All three need a *language* to recognize non-English/Chinese text correctly
(PaddleOCR and RapidOCR both default to Chinese-tuned models that mangle
Latin-script diacritics and punctuation; Tesseract needs the matching
language pack installed at all). legible/languages.py maps one user-facing
language choice (AppSettings.ocr_lang) to each engine's own code/model.

All engines are loaded lazily and only once, so switching in Settings doesn't
cost anything until you actually add a page. If the selected engine's package
isn't installed (or, for Tesseract, its binary/language pack is missing), a
mock engine is used so the rest of the app (GUI + export) still works for
development and testing. Set LEGIBLE_FORCE_MOCK=1 to always force mock,
overriding the Settings choice.

Windows note: paddlex (a paddleocr dependency) unconditionally imports
`modelscope` as one of its optional model-download backends, and real
modelscope unconditionally imports `torch`. Importing torch in the same
process as paddle crashes on Windows (a DLL-search-order conflict between the
two ML frameworks' bundled native libraries). We never use the modelscope
download backend (the default and only source we use is Hugging Face), so
`_stub_modelscope()` below pre-registers a harmless empty module to stop the
real one from ever being imported.
"""

from __future__ import annotations

import os
import re
import sys
import types
from html import escape, unescape
from typing import List, Optional

from PIL import Image

from . import languages, settings as app_settings
from .model import OcrBlock, Page


# --------------------------------------------------------------------------
# HTML -> plain text helpers
# --------------------------------------------------------------------------
def _table_html_to_text(html: str) -> str:
    """Flatten a <table> into tab-separated rows so screen readers and the
    text area still convey the table structure."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S)
    lines = []
    for row in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.I | re.S)
        cells = [unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        if any(cells):
            lines.append("\t".join(cells))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PP-StructureV3 label taxonomy -> our OcrBlock shape
# (see paddlex/inference/pipelines/layout_parsing/setting.py: BLOCK_LABEL_MAP)
# --------------------------------------------------------------------------
_DOC_TITLE_LABELS = {"doc_title"}
_HEADING_LABELS = {
    "paragraph_title", "abstract_title", "reference_title", "content_title",
    "table_title", "chart_title", "figure_title", "figure_table_chart_title",
}


def _convert_paddle_block(raw_block) -> Optional[OcrBlock]:
    """Turn one PP-StructureV3 LayoutBlock into an OcrBlock, or None to skip
    it (empty content - e.g. a pure image/figure region with no text)."""
    label = getattr(raw_block, "label", "text") or "text"
    content = (getattr(raw_block, "content", "") or "").strip()
    if not content:
        return None

    raw_bbox = list(getattr(raw_block, "bbox", None) or [0, 0, 0, 0])
    bbox = [float(v) for v in raw_bbox] if len(raw_bbox) == 4 else [0.0, 0.0, 0.0, 0.0]

    if label == "table":
        # content is already <table>...</table> HTML (from the table
        # recognition sub-model's pred_html).
        return OcrBlock(
            label="Table", html=content, text=_table_html_to_text(content),
            bbox=bbox,
        )

    escaped = escape(content).replace("\n", "<br>")
    if label in _DOC_TITLE_LABELS:
        html = f"<h1>{escaped}</h1>"
    elif label in _HEADING_LABELS:
        html = f"<h2>{escaped}</h2>"
    else:
        html = f"<p>{escaped}</p>"

    out_label = "SectionHeader" if (label in _DOC_TITLE_LABELS or label in _HEADING_LABELS) else "Text"
    return OcrBlock(label=out_label, html=html, text=content, bbox=bbox)


def _paddle_weights_cached() -> bool:
    """Best-effort check for whether PaddleOCR's model weights are already on
    disk (PP-StructureV3 downloads ~10 sub-models to ~/.paddlex on first use).

    Used only to choose between a "downloading model" and a "loading model"
    message. If we can't tell, we assume cached (the less alarming message).
    """
    home = os.path.expanduser("~/.paddlex/official_models")
    if not os.path.isdir(home):
        return False
    try:
        return len(os.listdir(home)) >= 3
    except OSError:
        return False


def _stub_modelscope() -> None:
    """See the module docstring: prevents paddlex from importing real
    modelscope (and therefore torch), which crashes on Windows alongside
    paddle. Safe because we never set PADDLE_PDX_MODEL_SOURCE=modelscope."""
    sys.modules.setdefault("modelscope", types.ModuleType("modelscope"))


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------
class _MockEngine:
    """Stand-in engine used when the configured OCR engine is unavailable
    (its package not installed or, for Tesseract, its binary/language pack
    missing).

    Produces deterministic, structurally realistic blocks (a heading, a
    paragraph, and a small table) so exports can be exercised end to end.
    """

    def ocr_image(self, image_path: str, progress=None) -> List[OcrBlock]:
        import time

        with Image.open(image_path) as im:
            w, h = im.size
        blocks = [
            OcrBlock(
                label="SectionHeader",
                html="<h2>Sample Document Heading</h2>",
                text="Sample Document Heading",
                bbox=[0.08 * w, 0.05 * h, 0.92 * w, 0.11 * h],
            ),
            OcrBlock(
                label="Text",
                html=(
                    "<p>This is mock OCR output produced because the "
                    "configured OCR engine isn't available in this "
                    "environment. Check Settings, or install the missing "
                    "package (see README).</p>"
                ),
                text=(
                    "This is mock OCR output produced because the "
                    "configured OCR engine isn't available in this "
                    "environment. Check Settings, or install the missing "
                    "package (see README)."
                ),
                bbox=[0.08 * w, 0.13 * h, 0.92 * w, 0.30 * h],
            ),
            OcrBlock(
                label="Table",
                html=(
                    "<table><tr><th>Item</th><th>Qty</th></tr>"
                    "<tr><td>Widget</td><td>3</td></tr>"
                    "<tr><td>Gadget</td><td>7</td></tr></table>"
                ),
                text="Item\tQty\nWidget\t3\nGadget\t7",
                bbox=[0.08 * w, 0.33 * h, 0.60 * w, 0.50 * h],
            ),
        ]
        # Simulate the same progress signals the real engine emits.
        if progress:
            progress("layout")
            total = len(blocks)
            for i in range(total):
                progress("recognize", i + 1, total)
                # Tiny sleep so the block counter is perceptible in the UI.
                if os.environ.get("LEGIBLE_MOCK_SLOW") == "1":
                    time.sleep(0.4)
        return blocks


class _PaddleEngine:
    """Real PaddleOCR PP-StructureV3 engine. Loaded lazily on first use."""

    def __init__(self, lang: str = "de"):
        self._pipeline = None
        self._lang = lang

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    def _ensure_loaded(self, progress=None):
        if self._pipeline is not None:
            return
        # First-time load. If the model weights are not cached yet,
        # PaddleOCR will download them now (can take a few minutes for the
        # ~10 sub-models PP-StructureV3 uses). Tell the caller which it is.
        if progress:
            progress("download" if not _paddle_weights_cached() else "load")
        _stub_modelscope()
        from paddleocr import PPStructureV3

        # Keep to the sub-pipelines this app actually uses (text + tables);
        # skip chart/seal/formula recognition and doc-orientation/unwarping
        # correction, which are irrelevant for a scanner feed and only add
        # CPU cost.
        #
        # lang: PaddleOCR's default (lang=None) resolves to "ch" (Chinese),
        # which uses a CJK-tuned recognition model - on Latin-script text it
        # drops diacritics (e.g. German "für" -> "fur") and mis-normalizes
        # punctuation (ASCII "(" -> full-width "（"). Set in Settings
        # (AppSettings.ocr_lang); "de" (German) is the default since it's a
        # good general Latin-script choice and this app's primary target.
        self._pipeline = PPStructureV3(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_seal_recognition=False,
            use_chart_recognition=False,
            use_formula_recognition=False,
            lang=self._lang,
        )

    def ocr_image(self, image_path: str, progress=None) -> List[OcrBlock]:
        self._ensure_loaded(progress=progress)

        if progress:
            progress("layout")
        results = list(self._pipeline.predict(image_path))
        if not results:
            return []
        raw_blocks = list(results[0]["parsing_res_list"])

        # PP-StructureV3's predict() call is a single blocking step with no
        # per-block callback, so real-time progress isn't available; report
        # completion as each block is converted instead, which still drives
        # the UI's "block N of M" counter and progress bar.
        total = len(raw_blocks)
        blocks: List[OcrBlock] = []
        for i, rb in enumerate(raw_blocks):
            block = _convert_paddle_block(rb)
            if block is not None:
                blocks.append(block)
            if progress:
                progress("recognize", i + 1, total)
        return blocks


def _rapid_model_filename(lang_type: str) -> str:
    """The rec model filename RapidOCR will use for a given language family -
    "ch" (the default multilingual PP-OCRv6 model, already bundled with the
    package) or a PP-OCRv5 language-family model ("latin", "en", ...), which
    downloads on first use like PaddleOCR's own models."""
    if lang_type == "ch":
        return "PP-OCRv6_rec_small.onnx"
    return f"{lang_type}_PP-OCRv5_rec_mobile.onnx"


def _rapid_weights_cached(lang_type: str) -> bool:
    """Best-effort check for whether the rec model *for this language* is
    already on disk. Same "download" vs "load" phase purpose as
    _paddle_weights_cached; if we can't tell, assume cached."""
    try:
        import rapidocr

        models_dir = os.path.join(os.path.dirname(rapidocr.__file__), "models")
        return os.path.isfile(os.path.join(models_dir, _rapid_model_filename(lang_type)))
    except Exception:
        return True


class _RapidEngine:
    """Real RapidOCR engine. Loaded lazily on first use.

    RapidOCR is a lightweight text detection + recognition pipeline with no
    layout/heading/table awareness (unlike PP-StructureV3), so every non-empty
    detected line becomes its own "Text" OcrBlock with a real bbox.
    """

    def __init__(self, lang_type: str = "latin"):
        self._engine = None
        self._lang_type = lang_type

    @property
    def is_loaded(self) -> bool:
        return self._engine is not None

    def _ensure_loaded(self, progress=None):
        if self._engine is not None:
            return
        if progress:
            progress("download" if not _rapid_weights_cached(self._lang_type) else "load")
        from rapidocr import RapidOCR, LangRec, OCRVersion
        from rapidocr.utils.typings import ModelType

        # The default ("ch") model is Chinese-tuned and mangles Latin-script
        # diacritics/punctuation; for any other language family, pick the
        # matching PP-OCRv5 recognition model (its language-specific models
        # only ship in the "mobile" size - required alongside lang_type/
        # ocr_version, or RapidOCR rejects the combination outright).
        if self._lang_type == "ch":
            self._engine = RapidOCR()
        else:
            self._engine = RapidOCR(params={
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.lang_type": LangRec(self._lang_type),
                "Rec.model_type": ModelType.MOBILE,
            })

    def ocr_image(self, image_path: str, progress=None) -> List[OcrBlock]:
        self._ensure_loaded(progress=progress)

        if progress:
            progress("recognize")
        result = self._engine(image_path)
        boxes = result.boxes
        txts = result.txts or ()
        if boxes is None or not len(txts):
            return []

        total = len(txts)
        blocks: List[OcrBlock] = []
        for i, (box, text) in enumerate(zip(boxes, txts)):
            text = (text or "").strip()
            if text:
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                bbox = [min(xs), min(ys), max(xs), max(ys)]
                escaped = escape(text)
                blocks.append(
                    OcrBlock(label="Text", html=f"<p>{escaped}</p>",
                             text=text, bbox=bbox)
                )
            if progress:
                progress("recognize", i + 1, total)
        return blocks


class _TesseractEngine:
    """Real Tesseract engine, via pytesseract. Loaded lazily on first use.

    Unlike PaddleOCR/RapidOCR, the `pytesseract` pip package is just a thin
    CLI wrapper: the actual OCR engine (the `tesseract` binary) and each
    language's .traineddata file are separate, non-pip system installs (see
    README). Word-level detection (image_to_data) is grouped into line
    blocks using Tesseract's own block_num/par_num/line_num layout analysis,
    matching RapidOCR's per-line granularity - no heading/table awareness.
    """

    def __init__(self, lang: str = "deu"):
        self._lang = lang
        self._verified = False

    @property
    def is_loaded(self) -> bool:
        return self._verified

    def _ensure_available(self):
        if self._verified:
            return
        import pytesseract

        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:
            raise RuntimeError(
                "Tesseract binary not found or not runnable. It is not a "
                "pip package - install it separately (see README), then "
                f"make sure it's on PATH. ({exc})"
            )
        self._verified = True

    def ocr_image(self, image_path: str, progress=None) -> List[OcrBlock]:
        self._ensure_available()
        import pytesseract
        from pytesseract import Output

        if progress:
            progress("recognize")
        try:
            data = pytesseract.image_to_data(
                image_path, lang=self._lang, output_type=Output.DICT,
            )
        except pytesseract.TesseractError as exc:
            raise RuntimeError(
                f"Tesseract OCR failed - language pack '{self._lang}' may "
                f"not be installed (see README): {exc}"
            )

        # Group word-level entries (level 5) into lines using Tesseract's own
        # block/paragraph/line layout analysis, preserving reading order.
        n = len(data["text"])
        groups: "dict[tuple, list[int]]" = {}
        order: List[tuple] = []
        for i in range(n):
            if int(data["level"][i]) != 5:
                continue
            if not data["text"][i].strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(i)

        total = len(order)
        blocks: List[OcrBlock] = []
        for gi, key in enumerate(order):
            idxs = groups[key]
            text = " ".join(data["text"][i] for i in idxs)
            lefts = [data["left"][i] for i in idxs]
            tops = [data["top"][i] for i in idxs]
            rights = [data["left"][i] + data["width"][i] for i in idxs]
            bottoms = [data["top"][i] + data["height"][i] for i in idxs]
            bbox = [float(min(lefts)), float(min(tops)),
                   float(max(rights)), float(max(bottoms))]
            escaped = escape(text)
            blocks.append(
                OcrBlock(label="Text", html=f"<p>{escaped}</p>",
                         text=text, bbox=bbox)
            )
            if progress:
                progress("recognize", gi + 1, total)
        return blocks


# --------------------------------------------------------------------------
# Public engine factory
# --------------------------------------------------------------------------
# Cached as (engine, is_mock) plus the (engine_name, lang) key it was built
# for, so a Settings change is picked up automatically on the next get_engine()
# call without every caller needing to remember to invoke reset_engine().
_engine = None
_engine_key = None


def _build_engine(engine_name: str, lang_code: str):
    lang = languages.get(lang_code)

    if engine_name == "rapidocr":
        try:
            import rapidocr  # noqa: F401

            return _RapidEngine(lang_type=lang.rapid_lang_type), False
        except ImportError:
            return _MockEngine(), True
    if engine_name == "tesseract":
        try:
            import pytesseract  # noqa: F401

            return _TesseractEngine(lang=lang.tesseract_lang), False
        except ImportError:
            return _MockEngine(), True
    if engine_name == "mock":
        return _MockEngine(), True
    # "paddleocr" (also the fallback for an unrecognised engine_name)
    try:
        _stub_modelscope()
        import paddleocr  # noqa: F401

        return _PaddleEngine(lang=lang.paddle_lang), False
    except ImportError:
        return _MockEngine(), True


def get_engine():
    """Return the (engine, is_mock) for the currently configured OCR engine.

    Rebuilds automatically when the Settings choice (engine or language)
    changes since the last call. LEGIBLE_FORCE_MOCK=1 always wins.
    """
    global _engine, _engine_key
    if os.environ.get("LEGIBLE_FORCE_MOCK") == "1":
        key = ("mock", "")
    else:
        cfg = app_settings.load_settings()
        key = (cfg.ocr_engine, cfg.ocr_lang)
    if _engine is None or _engine_key != key:
        engine_name, lang = key
        _engine = _build_engine(engine_name, lang)
        _engine_key = key
    return _engine


def reset_engine():
    """Forget the cached engine so the next call rebuilds it.

    Not required after a normal Settings save (get_engine() already detects
    the change) - useful for explicitly forcing a rebuild, e.g. after
    installing a package at runtime.
    """
    global _engine, _engine_key
    _engine = None
    _engine_key = None


def _run_ocr(engine, image_path: str, progress=None) -> Page:
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        width, height = im.size
    blocks = engine.ocr_image(image_path, progress=progress)
    return Page(
        image_path=image_path,
        blocks=blocks,
        image_width=width,
        image_height=height,
    )


def ocr_page(image_path: str, progress=None) -> Page:
    """Run OCR on an image file and return a fully populated Page.

    `progress` is an optional callable: progress(phase, current=None, total=None)
    Phases:
      "download"  - model weights are being downloaded (first run only)
      "load"      - cached model is loading into memory
      "layout"    - detecting the page layout (finding blocks)
      "recognize" - reading text; when current/total are given, that is the
                    block being finished (e.g. 3 of 8)
    It lets the UI show distinct, granular progress.
    """
    engine, _is_mock = get_engine()
    return _run_ocr(engine, image_path, progress=progress)


def build_engine(engine_name: str, lang_code: str):
    """Build a one-off (engine, is_mock) pair for a specific engine/language,
    bypassing Settings and the cached singleton entirely - e.g. for "Re-OCR
    all pages" with a chosen engine without changing the configured default.

    Build once and reuse across multiple images via ocr_page_via() - building
    per-image would reload heavy models (PaddleOCR's in particular) on every
    single page.
    """
    return _build_engine(engine_name, lang_code)


def ocr_page_via(engine, image_path: str, progress=None) -> Page:
    """Like ocr_page(), but uses an already-built engine (see build_engine)
    instead of the configured default."""
    return _run_ocr(engine, image_path, progress=progress)


def is_mock() -> bool:
    """True if the mock engine is in use (for showing a UI notice)."""
    _engine_obj, mock = get_engine()
    return mock


_TESSERACT_INSTALL_HELP = (
    "Windows: https://github.com/UB-Mannheim/tesseract/wiki\n"
    "macOS: brew install tesseract\n"
    "Linux: apt install tesseract-ocr"
)


def check_tesseract(lang_code: str) -> "str | None":
    """Diagnose whether Tesseract is actually ready to use for `lang_code`.

    Unlike PaddleOCR/RapidOCR, Tesseract's pip package (pytesseract) is only
    a thin CLI wrapper - the real engine binary and each language's
    .traineddata file are separate, non-pip installs (see README), so
    picking "Tesseract" in Settings can silently succeed while the actual
    engine is unusable. Returns a human-readable message explaining what's
    missing and how to fix it, or None if everything checks out.
    """
    try:
        import pytesseract
    except ImportError:
        return (
            "Tesseract engine selected, but its Python package "
            "(pytesseract) isn't installed.\n\nRun: pip install pytesseract\n\n"
            "Until then, Legible will use Mock OCR instead when this "
            "engine is selected."
        )

    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return (
            "Tesseract engine selected, but the tesseract program itself "
            "isn't installed (it's a separate, non-pip install).\n\n"
            f"{_TESSERACT_INSTALL_HELP}\n\n"
            "After installing, make sure the 'tesseract' command is on "
            "PATH. Until then, Legible will use Mock OCR instead when this "
            "engine is selected."
        )

    lang = languages.get(lang_code)
    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception:
        # Can't enumerate languages (unusual, but don't block on it) - the
        # binary itself is confirmed working, so leave language checking
        # aside rather than raising a possibly-wrong warning.
        return None

    if lang.tesseract_lang not in available:
        return (
            f"Tesseract is installed, but its '{lang.display_name}' "
            f"language pack ({lang.tesseract_lang}.traineddata) isn't.\n\n"
            f"Download it from "
            "https://github.com/tesseract-ocr/tessdata and place it in "
            "tesseract's tessdata folder, or point the TESSDATA_PREFIX "
            "environment variable at a folder containing it.\n\n"
            "Until then, OCR with Tesseract will fail for this language."
        )
    return None


_ENGINE_DISPLAY_NAMES = {
    "paddleocr": "PaddleOCR (PP-StructureV3)",
    "rapidocr": "RapidOCR",
    "tesseract": "Tesseract",
    "mock": "Mock",
}


def engine_display_name() -> str:
    """Human-readable name of the currently configured OCR engine, for UI
    status messages. Reads the configured choice directly rather than
    forcing an engine build."""
    if os.environ.get("LEGIBLE_FORCE_MOCK") == "1":
        return "Mock"
    cfg = app_settings.load_settings()
    return _ENGINE_DISPLAY_NAMES.get(cfg.ocr_engine, cfg.ocr_engine)
