"""Export a Session to HTML or PDF.

HTML export:
  * Text only (no images), but keeps per-block formatting: headings stay
    headings, tables stay tables, paragraphs stay paragraphs.

PDF export:
  * For each page, the scanned image is drawn full-page.
  * An *invisible* text layer is placed on top, positioned to follow the
    page layout. The invisible text uses render mode 3 (neither fill nor
    stroke) so it is selectable / readable but not visible over the image.
  * Each OCR block gets its own real PDF structure element (/P for text and
    table blocks, /H1 for headings), individually wrapped in the content
    stream as its own tagged marked-content sequence (BDC/EMC) carrying an
    explicit /ActualText for that block - see export_pdf()'s "STRUCTURE
    TAGGING" comment for why this, specifically, is what makes a screen
    reader or Adobe Reader's Read Aloud pause between blocks instead of
    running the whole page together as one sentence, and why reportlab's
    plain Canvas API can't produce that on its own (it has no tagging
    support at all - this hand-builds the PDF/UA-style /StructTreeRoot
    needed, both directions of it: the forward structure -> content /MCID
    links and the /StructParents + /ParentTree reverse map, without which
    Acrobat rejects the tag tree outright and infers the layout instead).
    Table blocks are tagged as /P, not /Table -
    Legible doesn't have per-cell layout to build real /TR/TD structure
    from, so a full table structure element without row/cell children would
    be misleading rather than helpful; the flattened, tab-separated row
    text is still there and still read correctly, just not tagged as a
    table.
  * The scanned page image itself is marked as a PDF /Artifact (background
    content, not part of the reading order) rather than left untagged -
    an untagged image on an otherwise-tagged page reads to Adobe Reader's
    Read Aloud / a screen reader as an unnamed "Graphic" interrupting the
    real (invisible, tagged) text layer that already carries the page's
    actual content.
  * A page legible.orientation detected scanned upside down (Page.rotation,
    set by ocr.py) is drawn corrected - right-side up - in the PDF; the
    original scan file on disk is never modified.
"""

from __future__ import annotations

import codecs
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


# Shared by export_html()'s multi-page document and render_page_preview_html()'s
# single-page one, so the GUI's live preview looks like what actually gets
# exported.
_HTML_STYLE = """\
  body { font-family: Georgia, serif; line-height: 1.6; color: #1a1a1a; }
  table { border-collapse: collapse; margin: 1rem 0; }
  th, td { border: 1px solid #999; padding: .35rem .6rem; text-align: left; }
  th { background: #f0f0f0; }
"""


def render_page_preview_html(page: Page) -> str:
    """Build a standalone HTML document for on-screen preview of a single
    page (used by the GUI's page-detail pane).

    This is the exact same per-block HTML export_html() writes for this
    page (via _block_to_html) - real headings, real tables, real
    paragraphs - wrapped in a minimal styled document so it renders
    legibly on its own. Feeding this into a real HTML-rendering widget
    (rather than the flattened plain text of Page.full_text) is what lets a
    screen reader navigate the preview by heading/table, the same way it
    would navigate the exported HTML.
    """
    body = _block_to_html(page)
    if not body.strip():
        body = "<p><em>No text detected on this page.</em></p>"
    title = html_lib.escape(page.list_label(80))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{_HTML_STYLE}</style>
</head>
<body>
{body}
</body>
</html>
"""


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
{_HTML_STYLE}  body {{ max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }}
  section {{ margin-bottom: 3rem; }}
  .page-title {{ border-bottom: 2px solid #ccc; padding-bottom: .3rem;
                 color: #333; }}
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
                  max_px_h: float = None, rotation: int = 0) -> str:
    """Re-encode a page image as a temp JPEG at `quality` (0-100) and return
    its path. reportlab only does compact JPEG (DCTDecode) passthrough for
    .jpg/.jpeg sources - anything else (our scans are .png) gets embedded as
    lossless raster instead, which is far larger for photographic scan
    content. Caller is responsible for deleting the returned temp file.

    If `max_px_w`/`max_px_h` are given, the image is downsampled to fit
    within them first (via PIL's thumbnail(), which - by design - only ever
    shrinks: a source image already smaller than the target is left at its
    original size, never upscaled).

    `rotation` (0 or 180 - see Page.rotation) rotates the image before
    saving, so a scan legible.orientation detected upside down is drawn
    right-side up in the PDF - the original file at `image_path` is never
    touched. 180 degrees doesn't change width/height, so this stays
    consistent with the already-rotated bboxes export_pdf's text layer
    positions against (OCR ran against the same corrected orientation -
    see ocr._run_ocr).
    """
    from PIL import Image

    fd, jpeg_path = tempfile.mkstemp(suffix=".jpg", prefix="legible_pdf_")
    os.close(fd)
    with Image.open(image_path) as im:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if rotation:
            im = im.rotate(rotation, expand=False)
        if max_px_w and max_px_h:
            im.thumbnail((max(int(max_px_w), 1), max(int(max_px_h), 1)), Image.LANCZOS)
        im.save(jpeg_path, format="JPEG", quality=quality)
    return jpeg_path


def _pdf_string_literal(text: str) -> str:
    """Build a raw PDF string-literal token, e.g. "(Caf\\351)" - parens plus
    reportlab's own octal-escaping, ready to splice directly into a
    hand-built content-stream operator (used below for /ActualText).

    This is deliberately NOT reportlab.pdfbase.pdfdoc.PDFString(...).format()
    - that method returns *bytes*, correct for reportlab's normal top-level
    PDF-object formatting pipeline, but not for this one: everything
    appended to Canvas._code is a plain `str`, later joined and run through
    PDFStreamFilterZCompress.encode(), which does `text.encode('utf8')` on
    the *whole joined content stream* before compressing it. Splicing raw
    high-byte bytes into that str (e.g. via a naive latin-1 decode) would
    then get silently re-encoded as UTF-8 and corrupted. Reportlab's own
    glyph-drawing code sidesteps this the same way this function does: every
    character actually used in the content stream stays plain ASCII, with
    any non-ASCII content represented as literal backslash-octal escape
    sequences (`\\ddd`) - a byte value spelled out in ASCII digits round-trips
    through a UTF-8 encode unchanged, unlike the byte itself.

    Text that fits PDFDocEncoding (the Latin-1 superset used for
    ISO-8859-1-ish scripts) is escaped as single-byte octal sequences;
    anything else (e.g. Persian, or any script outside that range - this
    app supports ~86 OCR languages) falls back to UTF-16BE with a leading
    BOM, the standard way to put arbitrary Unicode in a PDF string, still
    entirely octal-escaped the same way.
    """
    from reportlab.lib.rl_accel import escapePDF

    try:
        raw = text.encode("pdfdoc")
    except (UnicodeEncodeError, LookupError):
        raw = codecs.BOM_UTF16_BE + text.encode("utf_16_be")
    return "(" + escapePDF(raw) + ")"


def export_pdf(session: Session, out_path: str, title: str = "Scanned Document",
               jpeg_quality: int = 85, image_dpi: int = 300, progress=None,
               should_cancel=None) -> str:
    """Write a tagged PDF: each page shows the scanned image with an
    invisible, correctly-ordered text layer for screen readers, structured
    as real per-block PDF structure elements (see "STRUCTURE TAGGING"
    below) rather than one undifferentiated run of text per page.

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
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase.pdfdoc import PDFArray, PDFDictionary, PDFName, PDFtrue

    c = canvas.Canvas(out_path, pagesize=A4)
    c.setTitle(title)
    c.setCatalogEntry("Lang", "en")

    # ======================================================================
    # STRUCTURE TAGGING
    #
    # reportlab's Canvas has no tagging/accessibility API at all (no
    # StructTreeRoot, no marked-content helpers - checked against the
    # actually-installed reportlab; this isn't a config flag we forgot to
    # flip). Without it, every block's invisible text on a page ends up as
    # one continuous, undifferentiated run in the content stream - Adobe
    # Reader's Read Aloud and screen readers then read the whole page as
    # one run-on sentence with no pauses between paragraphs/headings, even
    # though the *positions* are correct. Fixing that means hand-building
    # the minimum real PDF structure tree via reportlab's lower-level
    # object model (reportlab.pdfbase.pdfdoc) - still reportlab's own
    # supported API for PDF objects, just not exposed through Canvas:
    #
    #   /StructTreeRoot -> one /Document element -> one /Part element per
    #   page -> one /P (or /H1, for headings) element per OCR block, each
    #   wrapping that block's own separate text object in the content
    #   stream as its own BDC/EMC marked-content sequence with a unique
    #   (per-page) /MCID. That per-block BDC/EMC boundary - not just the
    #   structure tree itself - is what actually gives readers something to
    #   pause at; a structure tree over one giant undivided text run
    #   wouldn't fix anything.
    #
    #   Each block's BDC also carries an explicit /ActualText holding that
    #   block's real text (see _pdf_string_literal) with a trailing blank
    #   line. /ActualText is the PDF mechanism specifically meant for this:
    #   it tells a screen reader or Read Aloud exactly what to speak for a
    #   marked-content span, instead of leaving it to re-derive text/pauses
    #   from raw glyph positions - which is brittle for an *invisible*
    #   (render-mode-3) text layer like this one, since there's nothing
    #   visible for a position-based reading-order heuristic to measure.
    #   This is what actually stops adjacent blocks reading as one run-on
    #   sentence, on top of the BDC/EMC split above.
    #
    #   The scanned page image is wrapped in its own /Artifact BMC/EMC
    #   (see the loop below, before the text blocks) - marking it as
    #   background content outside the reading order, so it isn't
    #   surfaced as an untagged "Graphic" interrupting the real text.
    #
    #   The tree is also built *complete*, not just forward-navigable, and
    #   that turned out to be the whole ballgame for Acrobat. A structure
    #   tree needs both directions wired up:
    #     - forward  (structure -> content): each element's /K holds the
    #       /MCID of the marked-content sequence it owns.
    #     - backward (content -> structure): the page carries a
    #       /StructParents key, and /StructTreeRoot /ParentTree is a number
    #       tree mapping that key to an array of the elements owning each
    #       MCID on the page, indexed by MCID.
    #   The PDF spec makes /ParentTree *required* as soon as any structure
    #   element contains content items (ours all do), and every element
    #   needs a /P back to its parent - including the top /Document
    #   element, whose /P is the /StructTreeRoot itself. Acrobat enforces
    #   that strictly: given a tree missing either, it treats the document
    #   as effectively untagged and silently falls back to inferring the
    #   layout itself. That fallback is what produced *both* of the
    #   symptoms this code exists to prevent - the whole page read as one
    #   run-on block (Acrobat re-deriving reading order from raw glyph
    #   positions, ignoring our per-block tags *and* /ActualText), and the
    #   page scan announced as a bare "Graphic" (its /Artifact marking
    #   ignored right along with everything else, so the image came back
    #   as an inferred, unlabelled figure ahead of the real text).
    #   Everything below is therefore emitted, not just the forward half.
    # ======================================================================
    doc = c._doc
    # The tree root, referenced up-front so the /Document element below can
    # point its /P at it; its /K and /ParentTree are filled in at the end,
    # once every page has contributed (reportlab only formats objects at
    # save() time, so populating it later is fine).
    struct_root = PDFDictionary({"Type": PDFName("StructTreeRoot")})
    struct_root_ref = doc.Reference(struct_root)
    doc_elem = PDFDictionary({
        "Type": PDFName("StructElem"), "S": PDFName("Document"),
        "P": struct_root_ref,
    })
    doc_ref = doc.Reference(doc_elem)
    doc_kids = []
    # Flat [key, value, key, value, ...] contents of /ParentTree's /Nums -
    # one entry per page, key == that page's /StructParents.
    parent_tree_nums = []

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
            jpeg_path = _jpeg_for_pdf(page.image_path, jpeg_quality, max_px_w, max_px_h,
                                      rotation=page.rotation)
        except Exception:
            jpeg_path = None
        if jpeg_path:
            # /Artifact BMC/EMC (bare marked content, no structure element -
            # that's what makes something an Artifact rather than tagged
            # content) - see "STRUCTURE TAGGING" above for why: without
            # this the image reads as an untagged "Graphic" ahead of the
            # real (invisible) text layer.
            c._code.append("/Artifact BMC")
            try:
                c.drawImage(
                    jpeg_path, offset_x, offset_y,
                    width=draw_w, height=draw_h, preserveAspectRatio=True,
                )
            except Exception:
                pass  # if image can't be drawn, still emit the text layer
            finally:
                c._code.append("EMC")
                os.remove(jpeg_path)

        # This page's own /Part element (see STRUCTURE TAGGING above) -
        # thisPageRef() must be called before showPage() below, while this
        # page is still the "current" one, to get a valid /Pg reference for
        # each block's structure element.
        part_elem = PDFDictionary(
            {"Type": PDFName("StructElem"), "S": PDFName("Part"), "P": doc_ref})
        part_ref = doc.Reference(part_elem)
        doc_kids.append(part_ref)
        part_kids = []
        page_ref = doc.thisPageRef()
        mcid = 0

        # 2. Invisible text layer, positioned to follow layout - one
        # separate text object (and structure element) per block, not one
        # for the whole page, so each becomes its own tagged, pausable
        # chunk (see STRUCTURE TAGGING above).
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

            text_obj = c.beginText()
            text_obj.setTextRenderMode(3)  # invisible: no fill, no stroke
            text_obj.setFont("Helvetica", font_size)

            for i, line in enumerate(lines):
                # Position each line inside the block box.
                y_img_line = y0 + line_gap_img * (i + 0.8)
                x_pdf, y_pdf = img_to_pdf(x0, y_img_line)
                text_obj.setTextOrigin(x_pdf, y_pdf)
                text_obj.setFont("Helvetica", font_size)
                # Tabs (from tables) -> spaced so column text stays separate.
                text_obj.textLine(line.replace("\t", "    "))

            # Headings get /H1; everything else (including tables - see
            # the module docstring for why not /Table) gets /P. The BDC
            # operator's tag name must match the structure element's /S.
            struct_type = "H1" if block.label == "SectionHeader" else "P"
            block_elem = PDFDictionary({
                "Type": PDFName("StructElem"), "S": PDFName(struct_type),
                "P": part_ref, "Pg": page_ref, "K": mcid,
            })
            part_kids.append(doc.Reference(block_elem))
            # /ActualText is the block's real text plus a trailing blank
            # line, so a reader gets an explicit pause/break here instead
            # of concatenating this block straight into the next one (see
            # "STRUCTURE TAGGING" above and _pdf_string_literal).
            actual_text = block.text.replace("\t", "    ").strip() + "\n\n"
            mc_props = "<</MCID %d /ActualText %s>>" % (
                mcid, _pdf_string_literal(actual_text))
            c._code.append("/%s %s BDC" % (struct_type, mc_props))
            c.drawText(text_obj)
            c._code.append("EMC")
            mcid += 1

        part_elem["K"] = PDFArray(part_kids)

        # Backward half of the structure wiring (see STRUCTURE TAGGING).
        # part_kids is already in MCID order - each block appends its
        # element then bumps mcid - so it doubles as this page's
        # MCID-indexed /ParentTree entry. It goes in as its own indirect
        # object (what Acrobat expects for a /Nums value) and is a separate
        # PDFArray from the one in /K above, since that one stays direct.
        struct_parents_key = page_num - 1
        parent_tree_nums.append(struct_parents_key)
        parent_tree_nums.append(doc.Reference(PDFArray(list(part_kids))))

        c.showPage()

        # showPage() is what actually builds the PDFPage object, so the
        # page-level keys have to be set on it afterwards. reportlab only
        # emits attributes listed in the object's __NoDefault__, and
        # PDFPage's list has neither of these, so extend it per-instance
        # (it's formatted lazily at save() time, so this still lands).
        # /Tabs /S = tab/focus order follows the structure tree, expected
        # alongside /StructParents by Acrobat's accessibility checker.
        page_obj = doc.Pages.pages[-1]
        page_obj.__NoDefault__ = list(page_obj.__NoDefault__) + [
            "StructParents", "Tabs"]
        page_obj.StructParents = struct_parents_key
        page_obj.Tabs = PDFName("S")

        if progress:
            progress(page_num, total)

    doc_elem["K"] = PDFArray(doc_kids)
    struct_root["K"] = PDFArray([doc_ref])
    struct_root["ParentTree"] = doc.Reference(
        PDFDictionary({"Nums": PDFArray(parent_tree_nums)}))
    struct_root["ParentTreeNextKey"] = len(session.pages)
    # struct_root was already registered above (for the /Document element's
    # /P), so this hands the catalog the same indirect reference rather
    # than a second copy of the tree root.
    c.setCatalogEntry("StructTreeRoot", struct_root)
    c.setCatalogEntry("MarkInfo", PDFDictionary({"Marked": PDFtrue}))
    # Required companion to a real title/tag tree: tells the reader to show
    # (and announce) the document title rather than the file name.
    c.setCatalogEntry("ViewerPreferences", PDFDictionary({"DisplayDocTitle": PDFtrue}))

    c.save()
    return out_path
