"""OCR: always runs through Docling (layout detection - paragraphs,
headings, tables), with the actual text-recognition backend chosen and
configured (including language) via Settings: EasyOCR or RapidOCR.

Docling itself only finds *where* content is; reading the text inside each
detected region is delegated to one of two backends, each with its own
speed/language/dependency trade-off:

* EasyOCR ("easyocr") supports its full ~86-language list (see
  legible/languages.py), but depends on PyTorch (a large, GB-scale
  download) - notably Greek has no EasyOCR model at all, the one language
  here it doesn't cover (see check_ocr_backend()).
* RapidOCR ("rapidocr") is a lightweight ONNX pipeline - faster, no PyTorch
  dependency - but only covers about a dozen script *families* rather than
  86 individual languages, so several EasyOCR-only languages (Bengali,
  Kannada, most non-Hindi Devanagari languages, ...) have no RapidOCR
  equivalent at all.

Before OCR proper, every page also gets a best-effort upside-down check
(legible/orientation.py, via Tesseract's OSD classifier - independent of
which backend above is configured): if the scan is detected rotated 180
degrees, the *corrected* orientation is what actually gets OCR'd and later
drawn into the PDF export, and Page.rotation records the flip for the UI
(page title note, spoken warning) - see _run_ocr().

Either way, `DocumentConverter(...).convert(image_path)` returns a
`ConversionResult` whose `.document` is a reading-order tree of layout
items - `iterate_items()` walks it, each item carrying a `label` (e.g.
"text", "section_header", "table") and its own text (or, for a table,
`TableItem.export_to_html()`).

The engine is loaded lazily and only once, so switching in Settings doesn't
cost anything until you actually add a page. If Docling or the selected
backend's package isn't installed, or the selected language isn't supported
by it, a mock engine is used so the rest of the app (GUI + export) still
works for development and testing. Set LEGIBLE_FORCE_MOCK=1 to always force
mock, overriding the Settings choice.
"""

from __future__ import annotations

import os
import re
from html import escape, unescape
from typing import List, Optional

from PIL import Image

from . import languages, orientation, settings as app_settings
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
# Engines
# --------------------------------------------------------------------------
class _MockEngine:
    """Stand-in engine used when Docling or the configured backend is
    unavailable (package not installed, or the selected language isn't
    supported by it).

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


# --------------------------------------------------------------------------
# Docling DocItem taxonomy -> our OcrBlock shape
# (see docling_core.types.doc.labels.DocItemLabel)
# --------------------------------------------------------------------------
_DOCLING_HEADING_LABELS = {"title", "section_header"}


def _rapid_model_filename(lang_type: str) -> str:
    """The rec model filename the `rapidocr` package will use for a given
    language family - "ch" (the default multilingual PP-OCRv6 model,
    already bundled with the package) or a PP-OCRv5 language-family model
    ("latin", "en", ...), which downloads on first use."""
    if lang_type == "ch":
        return "PP-OCRv6_rec_small.onnx"
    return f"{lang_type}_PP-OCRv5_rec_mobile.onnx"


def _rapid_weights_cached(lang_type: str) -> bool:
    """Best-effort check for whether the RapidOCR rec model *for this
    language* is already on disk (Docling's RapidOCR backend downloads
    through the same `rapidocr` package/cache a standalone RapidOCR install
    would use). Used only to choose between a "downloading model" and a
    "loading model" message; if we can't tell, assume cached (the less
    alarming message)."""
    try:
        import rapidocr

        models_dir = os.path.join(os.path.dirname(rapidocr.__file__), "models")
        return os.path.isfile(os.path.join(models_dir, _rapid_model_filename(lang_type)))
    except Exception:
        return True


#: Hugging Face repos Docling's own pipeline pulls from unconditionally
#: (layout detection, table structure) regardless of which OCR backend is
#: selected - see docling/models/stages/layout/layout_model.py
#: (DOCLING_LAYOUT_HERON) and docling/models/stages/table_structure/
#: table_structure_model.py.
_DOCLING_HF_REPOS = (
    "docling-project/docling-layout-heron",
    "docling-project/docling-models",
)


def _hf_repo_cached(repo_id: str) -> bool:
    """Best-effort check for whether a Hugging Face Hub repo's snapshot is
    already fully downloaded to the local cache.

    Docling downloads its own models via `huggingface_hub.snapshot_download()`
    with no explicit `local_dir`, so they land in the *default* HF cache
    (~/.cache/huggingface/hub, or wherever HF_HOME/HUGGINGFACE_HUB_CACHE
    points) - NOT under Docling's own ~/.cache/docling. Reading
    `huggingface_hub.constants.HF_HUB_CACHE` mirrors that resolution instead
    of guessing a path.
    """
    try:
        from huggingface_hub import constants as hf_constants

        repo_folder = "models--" + repo_id.replace("/", "--")
        snapshots_dir = os.path.join(hf_constants.HF_HUB_CACHE, repo_folder, "snapshots")
        if not os.path.isdir(snapshots_dir):
            return False
        for snapshot in os.listdir(snapshots_dir):
            snapshot_path = os.path.join(snapshots_dir, snapshot)
            if os.path.isdir(snapshot_path) and os.listdir(snapshot_path):
                return True
        return False
    except Exception:
        return True


def _docling_weights_cached(backend: str, lang_code: str) -> bool:
    """Best-effort check for whether Docling's own layout/table-structure
    models (_DOCLING_HF_REPOS, via _hf_repo_cached), plus the selected
    backend's recognition model - EasyOCR's to ~/.EasyOCR/model, or
    RapidOCR's per _rapid_weights_cached() - are already on disk.
    `lang_code` is the backend's own language code (i.e.
    _DoclingEngine._lang - RapidOCR's rapid_lang_type, unused for EasyOCR).

    Used only to choose between a "downloading model" and a "loading model"
    message. If we can't tell, we assume cached (the less alarming
    message).
    """
    if not all(_hf_repo_cached(repo_id) for repo_id in _DOCLING_HF_REPOS):
        return False

    if backend == "rapidocr":
        return _rapid_weights_cached(lang_code)

    # "easyocr"
    easyocr_dir = os.path.expanduser("~/.EasyOCR/model")
    if not os.path.isdir(easyocr_dir):
        return False
    try:
        return bool(os.listdir(easyocr_dir))
    except OSError:
        return False


def _docling_bbox(item, page_size, img_w: int, img_h: int) -> List[float]:
    """Convert a Docling item's first provenance bbox into top-left-origin
    pixel coordinates on the actual source image.

    Docling's own page "size" is expressed in points derived from the
    image's DPI metadata, which isn't necessarily equal to its pixel
    dimensions (a scan tagged 150 DPI has a page size in points, not raw
    pixels) - scale_to_size() maps it onto our own image_width/image_height
    regardless of what units Docling used internally, matching what
    OcrBlock.bbox and export.py expect (pixels on the actual source image).
    """
    from docling_core.types.doc import Size

    if not getattr(item, "prov", None):
        return [0.0, 0.0, 0.0, 0.0]
    box = item.prov[0].bbox.to_top_left_origin(page_size.height)
    if page_size.width and page_size.height:
        box = box.scale_to_size(old_size=page_size, new_size=Size(width=img_w, height=img_h))
    l, t, r, b = box.as_tuple()
    return [float(l), float(t), float(r), float(b)]


def _convert_docling_item(item, doc, page_size, img_w: int, img_h: int) -> Optional[OcrBlock]:
    """Turn one Docling DocItem into an OcrBlock, or None to skip it (a
    picture/figure region, or a text region OCR found nothing in)."""
    from docling_core.types.doc import TableItem

    if isinstance(item, TableItem):
        # Already ready-made <table>...</table> HTML (from the table
        # structure sub-model + the OCR backend's cell text).
        html = (item.export_to_html(doc=doc) or "").strip()
        if not html:
            return None
        text = _table_html_to_text(html)
        if not text.strip():
            return None
        return OcrBlock(label="Table", html=html, text=text,
                        bbox=_docling_bbox(item, page_size, img_w, img_h))

    text = (getattr(item, "text", "") or "").strip()
    if not text:
        return None

    label_value = getattr(getattr(item, "label", None), "value", "")
    escaped = escape(text).replace("\n", "<br>")
    if label_value == "title":
        html = f"<h1>{escaped}</h1>"
    elif label_value in _DOCLING_HEADING_LABELS:
        html = f"<h2>{escaped}</h2>"
    else:
        html = f"<p>{escaped}</p>"

    out_label = "SectionHeader" if label_value in ({"title"} | _DOCLING_HEADING_LABELS) else "Text"
    return OcrBlock(label=out_label, html=html, text=text,
                    bbox=_docling_bbox(item, page_size, img_w, img_h))


class _DoclingEngine:
    """Real engine: Docling's own layout model finds the page structure
    (paragraphs, headings, tables); the chosen backend (EasyOCR or
    RapidOCR) reads the text inside each detected region. Loaded lazily on
    first use.

    `ocr_options.mode` is deliberately left at its default
    (`OcrMode.DEFAULT`, which runs OCR per detected layout region) rather
    than forcing full-page OCR: region-scoped recognition is what keeps
    columns/headings/tables from bleeding into each other and is what makes
    the output structure-aware in the first place.
    """

    def __init__(self, backend: str, lang: str):
        self._converter = None
        self._backend = backend  # "easyocr" or "rapidocr"
        self._lang = lang

    @property
    def is_loaded(self) -> bool:
        return self._converter is not None

    def _ensure_loaded(self, progress=None):
        if self._converter is not None:
            return
        if progress:
            progress("download" if not _docling_weights_cached(self._backend, self._lang) else "load")
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions,
            PdfPipelineOptions,
            RapidOcrOptions,
        )
        from docling.document_converter import DocumentConverter, ImageFormatOption

        if self._backend == "rapidocr":
            ocr_options = RapidOcrOptions(lang=[self._lang])
        else:
            ocr_options = EasyOcrOptions(lang=[self._lang])

        # do_table_structure stays on (real <table> HTML); do_code_enrichment/
        # do_formula_enrichment stay off (irrelevant for a scanner feed, only
        # add CPU/GPU cost).
        pipeline_options = PdfPipelineOptions(
            do_ocr=True,
            do_table_structure=True,
            ocr_options=ocr_options,
        )
        self._converter = DocumentConverter(
            format_options={
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
            }
        )

    def ocr_image(self, image_path: str, progress=None) -> List[OcrBlock]:
        if not self._lang:
            # languages.Language.easyocr_lang is "" for the one language
            # (Greek) EasyOCR doesn't cover at all - check_ocr_backend(),
            # called from Settings, warns about this before it ever gets
            # here (rapid_lang_type, RapidOCR's equivalent, is never empty -
            # it covers every language Legible offers).
            raise RuntimeError(
                f"{_ENGINE_DISPLAY_NAMES.get(self._backend, self._backend)} doesn't "
                "support the selected language - pick the other backend "
                "instead (see Settings)."
            )
        self._ensure_loaded(progress=progress)

        if progress:
            progress("layout")
        result = self._converter.convert(image_path)
        doc = result.document

        with Image.open(image_path) as im:
            img_w, img_h = im.size
        page = doc.pages.get(1)
        if page is not None:
            page_size = page.size
        else:
            from docling_core.types.doc import Size

            page_size = Size(width=float(img_w), height=float(img_h))

        # DocumentConverter.convert() is a single blocking step with no
        # per-item callback, so real-time progress isn't available; report
        # completion as each item is converted instead, which still drives
        # the UI's "block N of M" counter and progress bar.
        items = [item for item, _level in doc.iterate_items()]
        total = len(items)
        blocks: List[OcrBlock] = []
        for i, item in enumerate(items):
            block = _convert_docling_item(item, doc, page_size, img_w, img_h)
            if block is not None:
                blocks.append(block)
            if progress:
                progress("recognize", i + 1, total)
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

    if engine_name == "mock":
        return _MockEngine(), True
    if engine_name == "rapidocr":
        try:
            import docling  # noqa: F401
            import rapidocr  # noqa: F401

            return _DoclingEngine(backend="rapidocr", lang=lang.rapid_lang_type), False
        except ImportError:
            return _MockEngine(), True
    # "easyocr" (also the fallback for an unrecognised engine_name)
    try:
        import docling  # noqa: F401
        import easyocr  # noqa: F401

        return _DoclingEngine(backend="easyocr", lang=lang.easyocr_lang), False
    except ImportError:
        return _MockEngine(), True


def get_engine():
    """Return the (engine, is_mock) for the currently configured OCR
    backend.

    Rebuilds automatically when the Settings choice (backend or language)
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

    if progress:
        progress("orientation")
    # Best-effort upside-down check (see legible/orientation.py) - a no-op
    # (rotation stays 0) if Tesseract isn't installed, so this never turns
    # into a hard requirement. 180 doesn't change width/height, only which
    # end is "up", so image_width/image_height below stay valid either way.
    rotation = orientation.detect_rotation(image_path)

    ocr_target = image_path
    rotated_temp = None
    if rotation == orientation.ROTATION_UPSIDE_DOWN:
        import tempfile

        fd, rotated_temp = tempfile.mkstemp(suffix=".png", prefix="legible_rot_")
        os.close(fd)
        with Image.open(image_path) as im:
            im.rotate(180, expand=False).save(rotated_temp)
        ocr_target = rotated_temp

    try:
        # OCR the corrected orientation (the rotated temp copy, if any) so
        # the recognized text is real, not garbled upside-down noise, and
        # so the resulting bboxes already line up with the corrected
        # orientation export.py will draw into the PDF - image_path itself
        # (the original scan) is never modified, only referenced.
        blocks = engine.ocr_image(ocr_target, progress=progress)
    finally:
        if rotated_temp:
            try:
                os.remove(rotated_temp)
            except OSError:
                pass

    return Page(
        image_path=image_path,
        blocks=blocks,
        image_width=width,
        image_height=height,
        rotation=rotation if rotation == orientation.ROTATION_UPSIDE_DOWN else 0,
    )


def ocr_page(image_path: str, progress=None) -> Page:
    """Run OCR on an image file and return a fully populated Page.

    `progress` is an optional callable: progress(phase, current=None, total=None)
    Phases:
      "orientation" - checking whether the page was scanned upside down
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
    """Build a one-off (engine, is_mock) pair for a specific backend/language,
    bypassing Settings and the cached singleton entirely - e.g. for "Re-OCR
    all pages" with a chosen backend without changing the configured default.

    Build once and reuse across multiple images via ocr_page_via() - building
    per-image would reload heavy models (EasyOCR's in particular) on every
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


def check_ocr_backend(engine_name: str, lang_code: str) -> "str | None":
    """Diagnose whether the given OCR backend is actually ready to use for
    `lang_code`.

    Docling itself is a normal pip package, but each backend is a separate,
    optional extra (EasyOCR pulls in PyTorch; RapidOCR pulls in onnxruntime)
    that can be skipped at install time, and EasyOCR doesn't cover every
    language Legible offers (see legible/languages.py - Greek, notably;
    RapidOCR covers all of them). Returns a human-readable message
    explaining what's missing/unsupported, or None if everything checks
    out.
    """
    try:
        import docling  # noqa: F401
    except ImportError:
        return (
            "Docling isn't installed.\n\nRun: pip install "
            "\"docling[easyocr,rapidocr]\"\n\nUntil then, Legible will use "
            "Mock OCR instead."
        )

    lang = languages.get(lang_code)
    if engine_name == "rapidocr":
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            return (
                "RapidOCR backend selected, but the rapidocr package isn't "
                "installed.\n\nRun: pip install \"docling[rapidocr]\"\n\n"
                "Until then, Legible will use Mock OCR instead when this "
                "backend is selected."
            )
        if not lang.rapid_lang_type:
            return (
                f"RapidOCR doesn't support '{lang.display_name}'.\n\nPick "
                "EasyOCR for this language instead."
            )
        return None

    # "easyocr"
    try:
        import easyocr  # noqa: F401
    except ImportError:
        return (
            "EasyOCR backend selected, but it isn't installed (it needs a "
            "separate PyTorch install).\n\nRun: pip install "
            "\"docling[easyocr]\"\n\nUntil then, Legible will use Mock OCR "
            "instead when this backend is selected."
        )
    if not lang.easyocr_lang:
        return (
            f"EasyOCR doesn't support '{lang.display_name}'.\n\nPick "
            "RapidOCR for this language instead."
        )
    return None


_ENGINE_DISPLAY_NAMES = {
    "easyocr": "Docling (EasyOCR)",
    "rapidocr": "Docling (RapidOCR)",
    "mock": "Mock",
}


def engine_display_name() -> str:
    """Human-readable name of the currently configured OCR backend, for UI
    status messages. Reads the configured choice directly rather than
    forcing an engine build."""
    if os.environ.get("LEGIBLE_FORCE_MOCK") == "1":
        return "Mock"
    cfg = app_settings.load_settings()
    return _ENGINE_DISPLAY_NAMES.get(cfg.ocr_engine, cfg.ocr_engine)
