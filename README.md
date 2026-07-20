# Legible

An accessible desktop app to **scan documents**, run **AI OCR** (PaddleOCR,
RapidOCR, or Tesseract — your choice), and **export to HTML or PDF**. Built
with screen readers in mind.

## What it does

- **Add page** — scans and runs OCR, with no chooser dialog: which
  scanner/device, paper source, and OCR settings to use are configured once
  in **Settings**. In **Automatic Document Feeder** mode, one Add page scans
  *every page currently loaded in the feeder* and adds them all.
- **Import files...** — load existing image files or **multi-page PDFs**
  instead of scanning; each PDF page runs through OCR just like a scan.
- **Settings** — pick the scanner backend/device, **paper source** (Flatbed /
  Automatic Document Feeder, when the selected scanner supports it),
  resolution (DPI), and color mode; pick which **OCR engine** to use and your
  **preferred language**; and set the **PDF image quality** and **PDF image
  DPI** used when exporting (both dropdowns, not free-entry fields).
- **Export to HTML** — text only, but keeps formatting (headings, tables, paragraphs).
- **Export to PDF** — each page shows the scanned image with an **invisible text
  layer** underneath that screen readers can read. Layout and tables are
  preserved. Page images are re-encoded as JPEG at Settings' **PDF image
  quality** (a dropdown, 5-100 in steps of 5; default 85) before embedding —
  a scan that would otherwise embed losslessly (large) shrinks several times
  over with no visible difference at the default; lower it further for
  smaller files, raise it for closer-to-original quality. Settings' **PDF
  image DPI** (a dropdown: 50/100/150/200/250/300; default 300) additionally
  caps each image's *resolution* relative to its printed size on the page —
  images are downsampled to at most that DPI before embedding, shrinking the
  file further on top of the quality setting; a scan or import already at or
  below the chosen DPI is left untouched (never upsampled).
- **Start new session** — clears everything to begin again.
- Reorder pages (**move up / move down**), **delete**, and **rename** pages.
- **Re-OCR all pages...** — pick a different OCR engine from a list and
  re-run it on every page in the session, discarding each page's current OCR
  result and replacing it with the new one. Handy for comparing engines, or
  redoing a whole session after realizing the wrong engine/language was
  configured. Doesn't touch page order, images, or custom titles - only the
  recognized text. Runs on a background thread like Add page/Import, with the
  same per-page progress and a confirmation dialog first (it can't be undone).
- Selecting a page shows its **full text** in an editable-looking text area.
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
- Every button has a keyboard shortcut (Alt + underlined letter):
  Alt+A add, Alt+I import, Alt+H HTML, Alt+P PDF, Alt+N new, Alt+S settings,
  Alt+E (or F1) help, Alt+U up, Alt+D down, Alt+L delete, Alt+R rename,
  Alt+O re-OCR all pages. (Help's mnemonic is E, not H - H was already
  taken by Export to HTML in this window.)
  The **Re-OCR all pages** button sits after Rename, separated from the
  per-page buttons by a visual divider (it acts on the whole session, not
  just the selected page) - the divider is purely visual and isn't part of
  the tab order, so keyboard/screen-reader navigation still flows straight
  from Rename to it in one natural sequence.
- The page list is a native list — screen readers announce the item and its
  position (e.g. "3 of 8"). With the list focused:
  - **Delete** deletes the selected page (with confirmation).
  - **Ctrl+Up** / **Ctrl+Down** move the selected page up/down.
  - **Insert** scans a new page (same as Add page).
  - **Enter** moves focus to the Full page text box.
  - **F2** renames the selected page.
  With the **Full page text** box focused, **Esc** moves focus back to the
  page list.
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

## Install

**Python 3.12 is recommended** (3.10–3.12 all work).

```bash
pip install -r requirements.txt
```

This installs **wxPython** (the GUI toolkit — see the Accessibility section)
and all three OCR engines. You don't need all three; see
[OCR engines](#ocr-engines) below to decide, and trim `requirements.txt` if
you'd rather not install ones you won't use.

### OCR engines

Pick one (or install several and switch anytime in **Settings**):

| Engine | Speed | Structure awareness | Setup |
|---|---|---|---|
| **PaddleOCR** (PP-StructureV3) | Slowest | Real headings + tables | `pip` only |
| **RapidOCR** | Fast | None (line-level text) | `pip` only |
| **Tesseract** | Fastest | None (line-level text) | `pip` **+ a separate system binary** |

All three need weights/models that download automatically on first use
(cached after that) — **except** Tesseract, whose `tesseract` binary and
language packs are not pip-installable at all:

1. Install the binary separately:
   [Windows](https://github.com/UB-Mannheim/tesseract/wiki) · macOS:
   `brew install tesseract` · Linux: `apt install tesseract-ocr`.
2. Install a `.traineddata` language pack matching your Settings language
   choice (e.g. `deu.traineddata` for German) from
   [tesseract-ocr/tessdata](https://github.com/tesseract-ocr/tessdata) into
   tesseract's `tessdata` folder, or point the `TESSDATA_PREFIX` environment
   variable at a folder containing it.

If a selected engine's package (or, for Tesseract, its binary/language pack)
isn't available, the app falls back to a **mock** OCR engine so the rest of
the interface and exports still work — Settings will show which engine is
actually active. Specifically for Tesseract, Settings actively checks this:
picking Tesseract (or changing the language while it's picked, or opening
Settings when it's already the saved engine) pops up a warning naming
exactly what's missing — the `tesseract` binary itself, or the selected
language's `.traineddata` pack — with the install steps above, rather than
letting it fail silently until the next scan.

**Language:** whichever engine you use, set your **preferred language** in
Settings (default: German). This matters a lot: PaddleOCR's and RapidOCR's
*own* defaults are Chinese-tuned models that drop Latin-script diacritics
(German "für" → "fur") and mis-normalize punctuation (ASCII `(` → full-width
`（`); Tesseract simply won't have the right language pack unless you told it
which one you need. `legible/languages.py` maps one language choice to each
engine's own code/model.

**GPU note (PaddleOCR):** the requirements install the CPU build of
PaddlePaddle. On a CUDA machine, install a matching GPU build of
`paddlepaddle-gpu` instead (see
https://www.paddlepaddle.org.cn/en/install/quick) for much faster OCR.

**Windows note (PaddleOCR):** if you also have PyTorch installed in the same
environment (e.g. from another project), importing it in the same process as
PaddlePaddle can crash with a DLL-loading error (`OSError: ...
torch\lib\shm.dll`) — a native-library conflict between the two frameworks,
not a Legible bug. `legible/ocr.py` works around the one place this could
bite (a PaddleOCR dependency, `paddlex`, optionally imports `modelscope`,
which imports torch) by stubbing that import out; Legible itself never
touches PyTorch.

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
| `legible/settings.py` | Persisted app settings (scanner + OCR engine/language) |
| `legible/languages.py` | Maps one language choice to each OCR engine's own code |
| `legible/scanner.py` | Silent scan via SANE / TWAIN / WIA, using saved settings |
| `legible/importer.py` | Import existing images / multi-page PDFs as pages |
| `legible/ocr.py` | PaddleOCR / RapidOCR / Tesseract wrappers (+ mock fallback) |
| `legible/export.py` | HTML and accessible-PDF exporters |
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

## License

Apache License 2.0 - see [LICENSE](LICENSE). Third-party dependency
licenses are summarized in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md);
notably, the optional TWAIN scanner backend (`pytwain`) is GPL-licensed and
should **not** be bundled into a redistributed build - see that file for
details before publishing a built exe.
