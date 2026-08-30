# Legible

An accessible desktop app to **scan documents**, run **AI OCR** (always via
**Docling**, with **EasyOCR** or **RapidOCR** as the text-recognition
backend — your choice), and **export to HTML or PDF**. Built with screen
readers in mind.

## What it does

- **Add page** — scans and runs OCR, with no chooser dialog: which
  scanner/device, paper source, and OCR settings to use are configured once
  in **Settings**. In **Automatic Document Feeder** mode, one Add page scans
  *every page currently loaded in the feeder* and adds them all.
- **Upside-down detection** — every scan/import gets a best-effort check
  (via Tesseract's OSD classifier, if installed - see
  [Upside-down page detection](#upside-down-page-detection) below) for
  whether it was fed in rotated 180°. If so, it's OCR'd and exported
  right-side up automatically, its default title in the page list gets a
  note right after the page number - before the text preview -
  ("Page 3 (scanned upside down - rotate 180° before filing): ..."), and
  you hear a spoken "Upside down page" cue plus the usual status
  announcement - so you know which way to turn the *paper original* when
  filing it later.
- **Import files...** — load existing image files or **multi-page PDFs**
  instead of scanning; each PDF page runs through OCR just like a scan.
- **Settings** — pick the scanner backend/device, **paper source** (Flatbed /
  Automatic Document Feeder, when the selected scanner supports it),
  resolution (DPI), and color mode; pick which **OCR backend** (EasyOCR or
  RapidOCR) Docling should use and your **preferred language**; and set the
  **PDF image quality** and **PDF image DPI** used when exporting (both
  dropdowns, not free-entry fields).
- **Export to HTML** — text only, but keeps formatting (headings, tables, paragraphs).
- **Export to PDF** — each page shows the scanned image with an **invisible,
  individually tagged text layer** underneath: every OCR block (heading or
  paragraph) is its own real PDF structure element in a proper
  `/StructTreeRoot`, not one undivided run of text per page — see
  [Notes on the exports](#notes-on-the-exports) for why that distinction
  actually matters for how Adobe Reader / a screen reader reads it aloud.
  Page images are re-encoded as JPEG at Settings' **PDF image quality** (a
  dropdown, 5-100 in steps of 5; default 85) before embedding — a scan that
  would otherwise embed losslessly (large) shrinks several times over with
  no visible difference at the default; lower it further for smaller files,
  raise it for closer-to-original quality. Settings' **PDF image DPI** (a
  dropdown: 50/100/150/200/250/300; default 300) additionally caps each
  image's *resolution* relative to its printed size on the page — images
  are downsampled to at most that DPI before embedding, shrinking the file
  further on top of the quality setting; a scan or import already at or
  below the chosen DPI is left untouched (never upsampled).
- Both exports finish **without a confirmation popup** to dismiss — the
  status line announcement (spoken immediately) and a short **export_done**
  sound cue are enough; only export *errors* still show a dialog.
- **Start new session** — clears everything to begin again.
- Reorder pages (**move up / move down**), **delete**, and **rename** pages.
- **Re-OCR all pages...** — pick a different OCR backend from a list and
  re-run it on every page in the session, discarding each page's current OCR
  result and replacing it with the new one. Handy for comparing backends, or
  redoing a whole session after realizing the wrong backend/language was
  configured. Doesn't touch page order, images, or custom titles - only the
  recognized text. Runs on a background thread like Add page/Import, with the
  same per-page progress and a confirmation dialog first (it can't be undone).
- Selecting a page renders its recognized content — real headings, tables,
  and paragraphs, not a flattened text dump — in the **Page preview (HTML)**
  pane: the exact same HTML **Export to HTML** would write for that page.
- Every page gets a title automatically the moment it's added: **"Page 1:
  [first 128 characters of its text]"**. That number is assigned once and
  **stays fixed** from then on - moving a page up/down (or adding/deleting
  others around it) never renumbers it; e.g. moving page 2 to the top of the
  list still shows "Page 2: ..." as the first item, not "Page 1: ...". Use
  **Rename** to replace the whole title with your own text - once renamed, a
  page keeps exactly what you typed, with no auto-numbering added back.
- **Help** (or **F1**) opens a full usage guide and keyboard shortcut
  reference in your default web browser.
- If you've added, deleted, reordered, or re-OCR'd any pages since your last
  export, **starting a new session or closing the app asks first** so you
  don't lose the changes. Renaming a page doesn't count - titles aren't part
  of what gets exported.

## Accessibility

- The GUI is built with **wxPython**, which wraps the operating system's
  **native controls**. Screen readers (NVDA, JAWS, Narrator, VoiceOver, Orca)
  read those natively. (Tkinter is deliberately *not* used — it draws its own
  widgets and exposes no accessibility tree, so NVDA can't read it on Windows.)
- Three buttons sit in the top bar, each with a mnemonic *and* a dedicated
  global Ctrl+number shortcut (active anywhere in the window, in addition
  to the mnemonic): **Add page** (Alt+A / **Ctrl+1**), **Export** (Alt+E /
  **Ctrl+2** - a popup menu with **Export to PDF** and **Export to HTML**),
  and **More** (Alt+M / **Ctrl+3** - a popup menu with Import, Start new
  session, Settings, About, and Help). Each menu item has its own mnemonic,
  active once that menu is open (e.g. press Alt+M then I for Import). F1
  still opens Help directly, with either menu open or closed.
  **Re-OCR all pages...** (Alt+O) is the only button left below the page
  list - it acts on the whole session, not a single page, unlike Move up/
  down/Rename/Delete, which live in the page list's own context menu (see
  below) rather than being buttons at all.
- The page list is a native list — screen readers announce the item and its
  position (e.g. "3 of 8"). With the list focused:
  - **Ctrl+Up** / **Ctrl+Down** move the selected page up/down.
  - **Delete** deletes the selected page (with confirmation).
  - **Insert** scans a new page (same as Add page).
  - **Enter** moves focus to the Page preview box.
  - **F2** renames the selected page.
  - **Right-click** (or the keyboard **Menu key** / **Shift+F10** - no
    mouse needed) opens a context menu for the page under the cursor (or
    the current selection, for the keyboard case) with **Move up**
    (Ctrl+Up), **Move down** (Ctrl+Down), **Rename** (F2), and **Delete**
    (Delete) - each keyboard shortcut is shown right in the menu, so it
    stays discoverable without a dedicated button for each.
  With the **Page preview** box focused, **Esc** moves focus back to the
  page list - including from inside the rendered page content itself (a
  real embedded browser engine handles its own keystrokes, so this needs a
  small injected script reporting back to the app rather than an ordinary
  wx key event; see `legible/gui.py`'s `_make_page_preview`).
- The **Page preview** pane is a real `wx.html2.WebView` (native Edge/
  WebView2 on Windows), not the classic `wx.html.HtmlWindow` — a genuine
  embedded browser engine that exposes a real accessibility tree, so a
  screen reader can navigate the rendered page by heading/table/paragraph
  the same way it would navigate a web page, rather than hearing one long
  flattened block of text. Falls back to a plain read-only text control (the
  previous behavior) if no WebView backend is available at all (e.g. no
  WebView2 runtime installed and no other backend present) — rare on a
  supported Windows install.
- A status line announces what just happened (page added, moved, exported).
- **Scan, Import, Re-OCR all pages, and Export (HTML/PDF) all run behind a
  blocking progress dialog** with a **Cancel** button — the main window is
  unusable until the job finishes or is cancelled (so you can't, say, delete
  a page while it's mid-export), but the dialog itself never freezes: the
  actual work runs on a background thread and posts updates into it.
  - The dialog explains what's happening at page/item granularity (e.g.
    **"Page 2 of 5: Reading the page text (block 3 of 8)..."** while
    scanning, **"Saving page 3 of 11..."** while exporting) and shows a
    progress bar — sweeping (indeterminate) during scan/download/layout,
    filled block-by-block while reading text, or filled page-by-page while
    exporting.
  - **Cancel** stops the job at the next safe point: between pages/items,
    and (for OCR) between recognized blocks within a page — it can't
    interrupt a scanner driver or an OCR engine's one-shot recognize step
    already in flight, only stop before starting the next one. Whatever
    completed before cancelling is kept (pages already added/re-OCR'd stay
    in the session); a cancelled export never leaves a partial file on disk.
  - The dialog's message is a focusable, screen-reader-readable control (not
    a plain label); it's briefly focused on every phase change to force it
    to be spoken, same technique as the main status line, then focus returns
    to Cancel.
- **Sound and speech cues** add an audible channel alongside the visual/
  screen-reader ones: short synthesized sound effects at scan/OCR start and
  finish and import/export success/errors (see
  [Sound effects](#sound-effects)), plus a direct spoken "Upside down page"
  cue via Windows SAPI text-to-speech when a page is detected scanned
  rotated 180° (see [Upside-down page detection](#upside-down-page-detection))
  - both audible even when a screen reader isn't running at all.

## Install

**Python 3.12 is recommended** (3.10–3.12 all work).

```bash
pip install -r requirements.txt
```

This installs **wxPython** (the GUI toolkit — see the Accessibility section),
**Docling**, and both its OCR backends. You don't need both; see
[OCR: Docling + a backend](#ocr-docling--a-backend) below to decide, and
trim `requirements.txt` if you'd rather not install the one you won't use.

### OCR: Docling + a backend

Legible always runs OCR **through Docling**: its own layout model finds the
page structure (paragraphs, headings, tables) first, then hands each region
to a text-recognition **backend** — pick one (or install both and switch
anytime in **Settings**):

| Backend | Speed | Language coverage | Setup |
|---|---|---|---|
| **EasyOCR** | Slower | 80+ languages, but not Greek | `pip` only (pulls in PyTorch) |
| **RapidOCR** | Fast | Every language Legible offers | `pip` only |

Both backends' models, plus Docling's own layout/table-structure model,
download automatically on first use and are cached after that.

If Docling or the selected backend's package isn't installed, the app falls
back to a **mock** OCR engine so the rest of the interface and exports still
work — Settings will show which backend is actually active. Settings
actively checks this: picking a backend (or changing the language while
it's picked, or opening Settings when it's already the saved backend) pops
up a warning naming exactly what's missing or unsupported, rather than
letting it fail silently until the next scan.

**Language:** whichever backend you use, set your **preferred language** in
Settings (default: German). This matters a lot: RapidOCR's *own* default is
a Chinese-tuned model that drops Latin-script diacritics (German "für" →
"fur") and mis-normalizes punctuation (ASCII `(` → full-width `（`); and
EasyOCR doesn't cover every language Legible offers — Greek notably has no
EasyOCR model at all. The **Preferred language** dropdown only ever lists
languages the currently-selected **OCR backend** actually supports, so this
can't come up by picking a bad combination in the first place: switching
backend re-filters the list to what it supports, and if your current
language isn't in the new list, it falls back to English automatically.
`legible/languages.py` maps one language choice to each backend's own
code/model.

**GPU note (EasyOCR):** the requirements install the CPU build of PyTorch,
which is enough — EasyOCR runs fine on CPU, just slower than with a GPU. If
you have a CUDA-capable GPU and want faster OCR, install a matching GPU
build of `torch`/`torchvision` yourself (see
https://pytorch.org/get-started/locally/) before installing
`docling[easyocr]`.

### Upside-down page detection

Optional (`pytesseract>=0.3.10` in `requirements.txt`, plus the same
`tesseract` binary the old Tesseract OCR engine used to need - see the
install steps just below): before OCR runs, every page gets a fast
classification pass (Tesseract's OSD mode - orientation only, it never
reads the page's actual text) checking whether it was scanned rotated 180°.

- **Install:** same binary as before -
  [Windows](https://github.com/UB-Mannheim/tesseract/wiki) · macOS:
  `brew install tesseract` · Linux: `apt install tesseract-ocr` - plus
  `pip install pytesseract`. This is unrelated to the OCR **backend** you
  pick in Settings (EasyOCR/RapidOCR) - Tesseract here is only ever used for
  orientation, never to read text.
- If it's not installed, this is simply skipped - pages are OCR'd and
  exported as scanned, with no rotation note. Nothing else about the app
  changes.
- If it detects 180°: the page is OCR'd in the *corrected* orientation (so
  the recognized text is real, not upside-down noise), the PDF export draws
  it right-side up, the page's auto-generated title gets a
  "(scanned upside down - rotate 180° before filing)" note right after the
  page number (before the text preview, so it's the first thing read), and
  you get a direct spoken "Upside down page" cue (via Windows SAPI - see
  `legible/tts.py`) plus the usual status-line announcement. The original
  scan file on disk is never modified.
- Only 180° is corrected (the common "fed the page in upside-down"
  mistake) - 90°/270° rotations aren't currently acted on.

### Sound effects

Short synthesized cues (`legible/sounds.py`, using `numpy` - deliberately
not plain sine-wave beeps, each with a few harmonics/an envelope/sometimes
a noise sweep so they're easy to tell apart by ear) play at: scan start,
OCR start, OCR finished, import finished, export finished, and any error -
an audible channel alongside the visual/spoken ones, useful even when
you're not looking at the screen. No setup needed beyond `numpy` (already
in `requirements.txt`); on non-Windows platforms (no `winsound`) they're
silently skipped.

### Scanner backend

Install the one for your OS to scan from real hardware, then pick the
backend/device/paper source/resolution/color mode once in **Settings**:

- **Windows (recommended):** `pip install pywin32` — uses WIA, the built-in
  Windows scanner interface (no extra SDK).
- **Windows (alternative):** `pip install pytwain` — uses TWAIN drivers.
- **Linux / macOS:** `pip install python-sane`

**Add page** then scans directly with **no chooser dialog** — both the scan
and the OCR step run on a background thread, so the window stays responsive.
If no scanner backend is installed, none is configured, or nothing responds,
**Add page** reports a clear error; use **Import files...** to load an
existing image or PDF instead.

**Flatbed vs. Automatic Document Feeder:** Settings' **Paper source** dropdown
only offers Flatbed/ADF when the selected scanner is actually reported to
have them - "Automatic" (the default) leaves the scanner's own current
source untouched. Choosing **ADF** scans every page currently loaded in the
feeder in one Add page action, stopping automatically once it's empty; each
page becomes its own entry in the list, in feed order. Verified against real
WIA hardware (flatbed + ADF combo); TWAIN and SANE support the same choice
using their own capability/source APIs, best-effort (not verified against
real hardware for either backend) - if source selection has no effect on
your scanner, it will still scan a single page as before.

## Run

```bash
python main.py
```

To try the interface and exports **without** any real OCR engine (uses
built-in mock OCR):

```bash
LEGIBLE_FORCE_MOCK=1 python main.py
```

## How the pieces fit

| File | Job |
|------|-----|
| `legible/model.py` | Page / Session data and list operations |
| `legible/settings.py` | Persisted app settings (scanner + OCR backend/language) |
| `legible/languages.py` | Maps one language choice to each OCR backend's own code |
| `legible/scanner.py` | Silent scan via SANE / TWAIN / WIA, using saved settings |
| `legible/importer.py` | Import existing images / multi-page PDFs as pages |
| `legible/ocr.py` | Docling engine (EasyOCR / RapidOCR backend) + mock fallback |
| `legible/orientation.py` | Best-effort upside-down (180°) page detection via Tesseract OSD |
| `legible/export.py` | HTML and accessible-PDF exporters |
| `legible/sounds.py` | Synthesized UI sound effects |
| `legible/tts.py` | Text-to-speech (Windows SAPI) for spoken cues |
| `legible/gui.py` | Accessible wxPython interface |
| `legible/help.html` | Standalone help page opened by the Help button/F1 |
| `main.py` | Entry point |
| `verify.py` | Non-GUI checks for model, OCR, and exports |

**Building a PyInstaller exe:** `legible/help.html` is a plain data file, not
Python code, so PyInstaller won't pick it up automatically - pass
`--add-data "legible/help.html;legible"` when building, so the Help button
still works in the frozen exe.

## Notes on the exports

- **HTML** is intentionally image-free — it is the clean text version, with
  structure kept as real HTML tags.
- **PDF** keeps the original scan as the visible page. The text sits on top in
  render mode 3 (invisible), positioned by each block's bounding box so reading
  order matches the page. Tables are flattened into rows so their structure is
  still spoken.
- **Each PDF block is individually tagged**, not just correctly positioned:
  reportlab's `Canvas` API has no PDF tagging/accessibility support at all
  (checked directly against the installed version - there's no config flag
  for it), so without doing this by hand, a page's blocks all land in one
  continuous, undifferentiated content-stream run - Adobe Reader's Read
  Aloud and screen readers would then read the *whole page* as one run-on
  sentence with no pause between paragraphs, even though the positions
  were correct. `export_pdf()` in `legible/export.py` hand-builds a real
  (if minimal) `/StructTreeRoot` via reportlab's lower-level
  `reportlab.pdfbase.pdfdoc` object model - one `/H1` or `/P` structure
  element per OCR block, each wrapped as its own `BDC`/`EMC` marked-content
  sequence in the content stream - so a reader has an actual place to
  pause between blocks. See that function's "STRUCTURE TAGGING" comment
  for what's deliberately left out (the `/StructParents`/`ParentTree`
  reverse lookup, and per-cell `/TR`/`/TD` table structure) and why.

## License

Apache License 2.0 - see [LICENSE](LICENSE). Third-party dependency
licenses are summarized in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md);
notably, the optional TWAIN scanner backend (`pytwain`) is GPL-licensed and
should **not** be bundled into a redistributed build - see that file for
details before publishing a built exe.
