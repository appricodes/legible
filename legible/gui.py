"""Accessible wxPython GUI for Legible.

Why wxPython (and not Tkinter): wxPython wraps the platform's *native* controls
(on Windows, real Win32 controls). Screen readers such as NVDA, JAWS and
Narrator read those natively through MSAA / UI Automation. Tkinter draws its
own widgets and exposes no accessibility tree, so NVDA saw only an empty
window - which is why the old UI was unreadable.

Accessibility choices (work with NVDA / JAWS / Narrator / VoiceOver / Orca):
  * Native controls throughout (buttons, list box, text control, gauge, a
    status line) - every control announces its name, role and state.
  * Scan/import/OCR/export run behind a blocking ProgressDialog: it explains
    what's happening (e.g. "Saving page 3 of 11...") and offers Cancel, so
    the user always knows the app is working and can stop a long job rather
    than having to wait it out or guess whether it's frozen.
  * Keyboard mnemonics via "&" in labels (Alt+A add page, Alt+E export,
    Alt+M open the More menu). wx draws the underline and wires the
    accelerator automatically - same mechanism inside the Export/More popup
    menus themselves (their own items each have a mnemonic, active once
    that menu is open) and the page list's right-click/Menu-key context
    menu (Move up/down, Rename, Delete). The three top buttons also have
    dedicated global accelerators - Ctrl+1 Add page, Ctrl+2 Export, Ctrl+3
    More - active anywhere in the window regardless of focus, in addition
    to (not instead of) their Alt+ mnemonics.
  * The page list is a native wx.ListBox: arrowing through it makes the screen
    reader speak the item text and its position ("3 of 8"). Move up/down,
    Rename, and Delete are reached via that context menu (mouse right-click,
    or the keyboard Menu key/Shift+F10 - no mouse required) or their
    existing shortcuts (Ctrl+Up/Down, F2, Delete), shown alongside each item
    in the menu so they stay discoverable without a dedicated button each.
  * State changes move focus to the relevant control so the result is spoken
    (after Add page the new item is selected and the list is focused; after
    Move up the item stays selected at its new position; etc.).
  * A read-only status line is the visible source of truth. For messages that
    should be *spoken* immediately (OCR phases, guidance), it is briefly
    focused so the screen reader reads the new text, then focus is restored.
  * OCR runs on a background thread and posts results back to the UI thread
    with wx.CallAfter, so the window never freezes or goes silent.
  * Add page scans silently with no chooser dialog: which scanner/device and
    what settings (DPI, color mode) to use are configured once in Settings,
    not asked on every scan. Both the scan and the OCR step run on a
    background thread now that neither shows a UI.
  * Import lets you load existing image files or multi-page PDFs instead of
    scanning from hardware.
  * The page-detail pane is a real wx.html2.WebView (native Edge/WebView2 on
    Windows) rendering the page's actual generated HTML (export.
    render_page_preview_html - the same per-block HTML export_html() writes),
    not a flattened text dump: a screen reader entering it meets real
    headings/tables/paragraphs it can navigate by structure, matching what
    Export to HTML produces. wx.html.HtmlWindow (the classic, self-drawn
    HTML widget) was deliberately not used - unlike a real embedded browser
    engine, it doesn't expose a proper accessibility tree. Esc still moves
    focus back to the page list from inside it (see _make_page_preview) -
    keystrokes inside a real browser engine don't reach wx's usual
    EVT_CHAR_HOOK, so this is done via a small injected script reporting
    back through AddScriptMessageHandler instead.
  * Every scan gets a best-effort upside-down check (legible/orientation.py);
    a page found rotated 180 degrees is OCR'd and exported right-side up
    automatically, its list label gets a text note, and it's announced both
    ways at once - a direct spoken TTS cue via legible/tts.py ("Upside down
    page", heard regardless of whether a screen reader is running) plus the
    usual screen-reader-readable status announcement.
  * Short synthesized sound effects (legible/sounds.py - richer than a plain
    beep, never blocking) mark scan/OCR start and finish, import/export
    success, and errors - an audible channel alongside the visual/spoken
    ones above, useful even when not looking at the screen.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import webbrowser

import wx
import wx.html2

from . import __version__, export, importer, languages, ocr, scanner, sounds, tts
from . import settings as app_settings
from .model import Session


def _help_html_path() -> str:
    """Path to the bundled help page, whether running from source
    (legible/help.html next to this file) or a frozen PyInstaller build
    (added via --add-data "legible/help.html;legible" at build time, landing
    at sys._MEIPASS/legible/help.html)."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "legible", "help.html")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "help.html")


def _new_html_view(parent):
    """Create a real HTML-rendering widget (native Edge/WebView2 on
    Windows) - used for both the page-detail pane and the About box, so
    both give a screen reader real navigable structure instead of a text
    dump. Falls back to a plain read-only text control only if no WebView
    backend is available at all (e.g. no WebView2 runtime and no other
    backend present - rare on a supported Windows install).

    Returns (widget, is_webview) - callers branch on is_webview to call
    SetPage() (WebView) or SetValue() (the plain-text fallback) when
    loading content.
    """
    try:
        if wx.html2.WebView.IsBackendAvailable(wx.html2.WebViewBackendEdge):
            view = wx.html2.WebView.New(parent, backend=wx.html2.WebViewBackendEdge)
        else:
            view = wx.html2.WebView.New(parent)
        if view is not None:
            return view, True
    except Exception:
        pass
    return (wx.TextCtrl(parent, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2),
            False)


_IMPORT_WILDCARD = (
    "Images and PDFs|*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.bmp;*.pdf|"
    "Images (*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.bmp)|"
    "*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.bmp|"
    "PDF files (*.pdf)|*.pdf|"
    "All files (*.*)|*.*"
)

_BACKEND_DISPLAY = {
    "auto": "Automatic (first available)",
    "sane": "SANE",
    "twain": "TWAIN",
    "wia": "WIA",
}
_COLOR_MODE_CHOICES = [
    ("color", "Color"),
    ("gray", "Grayscale"),
    ("bw", "Black && White"),
]
_OCR_ENGINE_CHOICES = [
    ("easyocr", "EasyOCR (broadest language support, slower, needs PyTorch)"),
    ("rapidocr", "RapidOCR (fast, lightweight, no PyTorch)"),
]


class _JobCancelled(Exception):
    """Raised from inside a progress callback to unwind out of a background
    job's current engine/scanner call once the user has clicked Cancel on
    its ProgressDialog. Only checked/raised at the few points where the
    work is actually interruptible (between pages/items, and between OCR
    blocks within a page) - it can't abort a single opaque call (a scanner
    driver's acquire, or an OCR engine's one-shot recognize step) already
    in flight, only stop before starting the next one.
    """


class ProgressDialog(wx.Dialog):
    """Blocking (modal) dialog shown for the duration of a background job
    (scan, import, re-OCR, export): explains what's happening and offers a
    Cancel button. Modal so the main window can't be used mid-job (e.g.
    deleting a page while it's being exported) - but the worker thread
    keeps posting updates into it via wx.CallAfter, same as the rest of the
    app; wx's modal dialogs still pump the event loop, so that works fine.

    Accessibility: the message is a focusable read-only text control (like
    the main window's status line) rather than a plain label, so a screen
    reader can review it; when the *phase* changes (not on every minor
    tick, to avoid flooding), it's briefly focused to force it to be
    spoken, then focus is restored - same pattern as LegibleFrame.announce().
    """

    def __init__(self, parent, title):
        super().__init__(parent, title=title, style=wx.CAPTION | wx.SYSTEM_MENU)
        self.cancel_requested = False
        self._last_phase_key = object()  # never equals a real key -> first update always speaks
        self._restore_focus_to = None

        outer = wx.BoxSizer(wx.VERTICAL)
        self.message_ctrl = wx.TextCtrl(
            self, value="", size=(380, -1), style=wx.TE_READONLY | wx.BORDER_NONE,
        )
        self.message_ctrl.SetName("Progress")
        outer.Add(self.message_ctrl, 0, wx.EXPAND | wx.ALL, 14)

        self.gauge = wx.Gauge(self, range=100, size=(380, -1))
        outer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        self._pulse_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_pulse, self._pulse_timer)
        self._indeterminate = False

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        self.btn_cancel = wx.Button(self, label="&Cancel")
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda evt: self._request_cancel())
        btn_row.Add(self.btn_cancel)
        outer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 14)

        # The titlebar close button / Alt+F4 behaves like Cancel rather than
        # closing outright - only the worker's own completion may actually
        # end this dialog, so it can't disappear while still posting
        # updates to it (or mutating pages) from a background thread.
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.SetSizer(outer)
        self.Fit()
        self.CentreOnParent()
        self.btn_cancel.SetFocus()
        self._start_pulse()

    def _on_close(self, event):
        event.Veto()
        self._request_cancel()

    def _request_cancel(self):
        if self.cancel_requested:
            return
        self.cancel_requested = True
        self.btn_cancel.Disable()
        self.set_message("Cancelling...", phase_key="__cancelling__")

    def set_message(self, text, phase_key=None, current=None, total=None):
        self.message_ctrl.SetValue(text)
        if current is not None and total:
            self._stop_pulse()
            self.gauge.SetRange(total)
            self.gauge.SetValue(min(current, total))
        else:
            self._start_pulse()
        if phase_key is not None and phase_key != self._last_phase_key:
            self._last_phase_key = phase_key
            prev = wx.Window.FindFocus()
            if prev is self.message_ctrl:
                prev = None
            self._restore_focus_to = prev
            self.message_ctrl.SetFocus()
            wx.CallLater(700, self._restore_focus)

    def _restore_focus(self):
        target = self._restore_focus_to
        self._restore_focus_to = None
        if target and target.IsEnabled() and target.IsShownOnScreen():
            target.SetFocus()

    def _start_pulse(self):
        if not self._indeterminate:
            self._indeterminate = True
            self._pulse_timer.Start(100)

    def _stop_pulse(self):
        if self._indeterminate:
            self._indeterminate = False
            self._pulse_timer.Stop()

    def _on_pulse(self, event):
        if self._indeterminate:
            self.gauge.Pulse()

    def finish(self, result=wx.ID_OK):
        """Called by the worker's completion handler once it has actually
        stopped - the only legitimate way this dialog closes."""
        self._pulse_timer.Stop()
        if self.IsModal():
            self.EndModal(result)


def _about_html() -> str:
    """Static About/credits page shown by AboutDialog: a short English
    summary of what Legible is, plus attribution for the open-source
    libraries and models it's built on. Full license text for every
    dependency lives in THIRD_PARTY_NOTICES.md alongside the source - this
    is a human-readable pointer to that, not a replacement for it."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>About Legible</title>
<style>
  body {{ font-family: Georgia, serif; line-height: 1.6; color: #1a1a1a;
          margin: 1.1rem; }}
  h1 {{ margin-bottom: 0; }}
  .tagline {{ color: #555; margin-top: .2rem; }}
  h2 {{ border-bottom: 1px solid #ccc; padding-bottom: .2rem; margin-top: 1.5rem; }}
  dt {{ font-weight: bold; margin-top: .7rem; }}
  dd {{ margin: .15rem 0 0 0; color: #333; }}
  .note {{ color: #555; font-size: .9em; margin-top: 1.5rem; }}
  a {{ color: #0645ad; }}
</style>
</head>
<body>
<h1>Legible</h1>
<p class="tagline">An accessible document scanner &mdash; version {__version__}</p>
<p>Legible scans paper documents (or imports existing images and PDFs),
runs optical character recognition to turn them into real text, and
exports clean, structured HTML or a tagged, screen-reader-readable PDF.
It's built around native accessible controls throughout, so it works well
with NVDA, JAWS, Narrator, VoiceOver, and Orca.</p>

<h2>Built with</h2>
<dl>
  <dt><a href="https://github.com/docling-project/docling">Docling</a></dt>
  <dd>Document layout analysis - finds paragraphs, headings, and tables on
  the page.</dd>

  <dt><a href="https://github.com/JaidedAI/EasyOCR">EasyOCR</a></dt>
  <dd>Deep-learning text-recognition backend, covering 80+ languages.</dd>

  <dt><a href="https://github.com/RapidAI/RapidOCR">RapidOCR</a></dt>
  <dd>Lightweight ONNX text-recognition backend - fast, no GPU needed.</dd>

  <dt><a href="https://pytorch.org/">PyTorch</a></dt>
  <dd>Deep-learning framework powering the EasyOCR backend.</dd>

  <dt><a href="https://github.com/tesseract-ocr/tesseract">Tesseract OCR</a></dt>
  <dd>Used only for its orientation detector, to spot pages scanned
  upside down - never to read page text.</dd>

  <dt><a href="https://wxpython.org/">wxPython</a></dt>
  <dd>The native, accessible GUI toolkit this whole interface is built on
  (including this About box).</dd>

  <dt><a href="https://www.reportlab.com/opensource/">ReportLab</a></dt>
  <dd>Generates the tagged, accessible PDF export.</dd>

  <dt><a href="https://github.com/pypdfium2-team/pypdfium2">pypdfium2</a></dt>
  <dd>Renders multi-page PDFs into page images for Import.</dd>

  <dt><a href="https://numpy.org/">NumPy</a></dt>
  <dd>Synthesizes the app's UI sound effects.</dd>

  <dt>Windows SAPI</dt>
  <dd>Text-to-speech for spoken cues (e.g. "Upside down page"), reached
  through pywin32.</dd>
</dl>

<p class="note">Full license details for every dependency are listed in
THIRD_PARTY_NOTICES.md, alongside the source code.</p>
</body>
</html>
"""


class AboutDialog(wx.Dialog):
    """About/credits box, opened from the More menu's &About... item: app
    description plus open-source attribution, shown in a real
    HTML-rendering widget (see _new_html_view) so a screen reader can
    navigate it by heading, the same way it navigates the page-detail
    pane."""

    def __init__(self, parent):
        super().__init__(parent, title="About Legible",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
                         size=(560, 520))
        outer = wx.BoxSizer(wx.VERTICAL)

        view, is_webview = _new_html_view(self)
        view.SetName("About Legible")
        if is_webview:
            view.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
            view.SetPage(_about_html(), "")
        else:
            # No WebView backend at all (rare) - crude tag-stripped
            # fallback rather than leaving raw HTML markup on screen.
            view.SetValue(re.sub(r"<[^>]+>", " ", _about_html()).strip())
        outer.Add(view, 1, wx.EXPAND | wx.ALL, 12)

        btns = self.CreateButtonSizer(wx.CLOSE)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)

        self.SetSizer(outer)
        self.CentreOnParent()

    def _on_navigating(self, event):
        # Credit links should open in the system browser, not navigate this
        # dialog's embedded view away from the About page itself - same
        # webbrowser.open() pattern the Help button uses, just triggered by
        # a link click instead of F1.
        url = event.GetURL()
        if url and url.startswith(("http://", "https://")):
            event.Veto()
            webbrowser.open(url)


class SettingsDialog(wx.Dialog):
    """Scanner (backend/device/DPI/color mode) and OCR backend choice.

    Saved via legible.settings; Add page then scans directly with no chooser
    of its own, and OCR (always via Docling) uses whichever backend is
    configured here.
    """

    def __init__(self, parent):
        super().__init__(parent, title="Settings",
                         style=wx.DEFAULT_DIALOG_STYLE)
        self.cfg = app_settings.load_settings()
        self._build_ui()
        self.Fit()
        self.CentreOnParent()

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=10, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(self, label="&Scanner backend:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.backend_ids = ["auto"]
        for b in scanner.list_backends():
            if b not in self.backend_ids:
                self.backend_ids.append(b)
        self.backend_choice = wx.Choice(
            self, choices=[_BACKEND_DISPLAY.get(b, b) for b in self.backend_ids])
        self.backend_choice.Bind(wx.EVT_CHOICE, self._on_backend_changed)
        grid.Add(self.backend_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Scanner de&vice:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.device_choice = wx.Choice(self, choices=[])
        self.device_choice.Bind(wx.EVT_CHOICE, self._on_device_changed)
        grid.Add(self.device_choice, 1, wx.EXPAND)

        # Flatbed vs Automatic Document Feeder - only offered when the
        # selected device is reported to actually support them (see
        # scanner.list_sources). ADF scans every page currently loaded in
        # the feeder in one Add page action.
        grid.Add(wx.StaticText(self, label="&Paper source:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.source_choice = wx.Choice(self, choices=[])
        grid.Add(self.source_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="&Resolution (DPI):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.dpi_spin = wx.SpinCtrl(self, min=75, max=1200,
                                    initial=self.cfg.scanner_dpi)
        grid.Add(self.dpi_spin, 0)

        grid.Add(wx.StaticText(self, label="Color &mode:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.color_choice = wx.Choice(
            self, choices=[label for _, label in _COLOR_MODE_CHOICES])
        grid.Add(self.color_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.TOP, 4)
        grid.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.TOP, 4)

        grid.Add(wx.StaticText(self, label="OCR &backend:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.engine_choice = wx.Choice(
            self, choices=[label for _, label in _OCR_ENGINE_CHOICES])
        self.engine_choice.Bind(wx.EVT_CHOICE, self._on_engine_changed)
        grid.Add(self.engine_choice, 1, wx.EXPAND)

        # Applies to whichever backend is selected above - filtered to that
        # backend's own supported languages (see legible/languages.py); if
        # switching backend drops the current language from the list, falls
        # back to English (see _refresh_languages).
        grid.Add(wx.StaticText(self, label="Preferred &language:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.lang_list = []
        self.lang_choice = wx.Choice(self, choices=[])
        self.lang_choice.Bind(wx.EVT_CHOICE, self._on_engine_or_lang_changed)
        grid.Add(self.lang_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.TOP, 4)
        grid.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.TOP, 4)

        # Page images are re-encoded as JPEG at this quality before being
        # embedded in the PDF export - see export._jpeg_for_pdf. Lower =
        # smaller file, higher = closer to the original scan.
        grid.Add(wx.StaticText(self, label="PDF image &quality:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.pdf_quality_choice = wx.Choice(
            self, choices=[str(v) for v in app_settings.PDF_IMAGE_QUALITY_CHOICES])
        grid.Add(self.pdf_quality_choice, 0)

        # Caps each page image's resolution relative to its printed size in
        # the PDF (never upsampled) - see export.export_pdf's image_dpi.
        # Lowering this shrinks the file further, on top of image quality
        # above, with no visible difference at normal viewing/print sizes.
        grid.Add(wx.StaticText(self, label="PDF image &DPI:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.pdf_dpi_choice = wx.Choice(
            self, choices=[f"{v} DPI" for v in app_settings.PDF_IMAGE_DPI_CHOICES])
        grid.Add(self.pdf_dpi_choice, 0)

        outer.Add(grid, 1, wx.EXPAND | wx.ALL, 14)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        self.SetSizer(outer)

        self._select_by_value(self.backend_choice, self.backend_ids,
                              self.cfg.scanner_backend)
        self._refresh_devices(select_device=self.cfg.scanner_device,
                              select_source=self.cfg.scanner_source)
        self._select_by_value(self.color_choice,
                              [v for v, _ in _COLOR_MODE_CHOICES],
                              self.cfg.scanner_color_mode)
        self._select_by_value(self.engine_choice,
                              [v for v, _ in _OCR_ENGINE_CHOICES],
                              self.cfg.ocr_engine)
        self._refresh_languages(select_lang=self.cfg.ocr_lang)
        self._select_nearest(self.pdf_quality_choice,
                             app_settings.PDF_IMAGE_QUALITY_CHOICES,
                             self.cfg.pdf_image_quality)
        self._select_nearest(self.pdf_dpi_choice,
                             app_settings.PDF_IMAGE_DPI_CHOICES,
                             self.cfg.pdf_image_dpi)

        # Also check right when the dialog opens, in case the saved
        # backend/language combo isn't (or is no longer) actually usable -
        # otherwise the only sign would be Mock OCR silently kicking in
        # later, at scan time.
        self._maybe_warn_engine()

    def _on_engine_changed(self, event):
        event.Skip()
        # Switching backend can drop the currently-selected language from
        # the list entirely (e.g. Greek only exists under RapidOCR) - keep
        # the language choice restricted to what the new backend actually
        # supports, falling back to English if the old choice doesn't fit.
        engine = _OCR_ENGINE_CHOICES[self.engine_choice.GetSelection()][0]
        current_code = (self.lang_list[self.lang_choice.GetSelection()].code
                        if self.lang_list and self.lang_choice.GetSelection() != wx.NOT_FOUND
                        else self.cfg.ocr_lang)
        self._refresh_languages(select_lang=current_code)
        self._maybe_warn_engine()

    def _refresh_languages(self, select_lang: str):
        """Populate the language dropdown with only the languages the
        currently-selected backend supports (see
        languages.supported_languages). Selects `select_lang` if the new
        backend still supports it, otherwise falls back to English."""
        engine = _OCR_ENGINE_CHOICES[self.engine_choice.GetSelection()][0]
        self.lang_list = languages.supported_languages(engine)
        self.lang_choice.Set([lang.display_name for lang in self.lang_list])
        lang_codes = [lang.code for lang in self.lang_list]
        fallback = "en" if "en" in lang_codes else (lang_codes[0] if lang_codes else "")
        target = select_lang if select_lang in lang_codes else fallback
        self._select_by_value(self.lang_choice, lang_codes, target)

    def _on_engine_or_lang_changed(self, event):
        event.Skip()
        self._maybe_warn_engine()

    def _maybe_warn_engine(self):
        engine = _OCR_ENGINE_CHOICES[self.engine_choice.GetSelection()][0]
        lang_code = self.lang_list[self.lang_choice.GetSelection()].code
        problem = ocr.check_ocr_backend(engine, lang_code)
        title = "OCR backend isn't ready"
        if problem:
            wx.MessageBox(problem, title, wx.OK | wx.ICON_WARNING, self)

    def _select_by_value(self, choice_ctrl, values, value):
        try:
            idx = values.index(value)
        except ValueError:
            idx = 0
        choice_ctrl.SetSelection(idx)

    def _select_nearest(self, choice_ctrl, values, value):
        """Like _select_by_value, but for a fixed set of numeric choices -
        picks the closest one instead of falling back to index 0, so a
        settings.json value from an older version (a free-form 0-100
        quality, say) still lands somewhere sensible rather than at the
        lowest/highest end of the new dropdown."""
        idx = min(range(len(values)), key=lambda i: abs(values[i] - value))
        choice_ctrl.SetSelection(idx)

    def _current_backend(self):
        backend = self.backend_ids[self.backend_choice.GetSelection()]
        if backend == "auto":
            available = scanner.list_backends()
            backend = available[0] if available else None
        return backend

    def _on_backend_changed(self, event):
        self._refresh_devices(select_device="", select_source="auto")

    def _on_device_changed(self, event):
        self._refresh_sources(select_source="auto")

    def _refresh_devices(self, select_device, select_source):
        backend = self._current_backend()
        devices = scanner.list_devices(backend) if backend else []
        self.device_ids = [""] + [d for d, _ in devices]
        labels = ["(first available)"] + [name for _, name in devices]
        self.device_choice.Set(labels)
        self._select_by_value(self.device_choice, self.device_ids, select_device)
        self._refresh_sources(select_source)

    def _refresh_sources(self, select_source):
        backend = self._current_backend()
        device_id = self.device_ids[self.device_choice.GetSelection()] if self.device_ids else ""
        sources = scanner.list_sources(backend, device_id) if backend else [("auto", "Automatic")]
        self.source_ids = [s for s, _ in sources]
        labels = [label for _, label in sources]
        self.source_choice.Set(labels)
        self._select_by_value(self.source_choice, self.source_ids, select_source)

    def _on_ok(self, event):
        self.cfg.scanner_backend = self.backend_ids[self.backend_choice.GetSelection()]
        self.cfg.scanner_device = self.device_ids[self.device_choice.GetSelection()]
        self.cfg.scanner_source = self.source_ids[self.source_choice.GetSelection()]
        self.cfg.scanner_dpi = self.dpi_spin.GetValue()
        self.cfg.scanner_color_mode = _COLOR_MODE_CHOICES[self.color_choice.GetSelection()][0]
        self.cfg.ocr_engine = _OCR_ENGINE_CHOICES[self.engine_choice.GetSelection()][0]
        self.cfg.ocr_lang = self.lang_list[self.lang_choice.GetSelection()].code
        self.cfg.pdf_image_quality = app_settings.PDF_IMAGE_QUALITY_CHOICES[
            self.pdf_quality_choice.GetSelection()]
        self.cfg.pdf_image_dpi = app_settings.PDF_IMAGE_DPI_CHOICES[
            self.pdf_dpi_choice.GetSelection()]
        app_settings.save_settings(self.cfg)
        ocr.reset_engine()
        self.EndModal(wx.ID_OK)


class LegibleFrame(wx.Frame):
    _PHASE_MESSAGES = {
        "scan": "Scanning the page",
        "orientation": "Checking page orientation",
        "download": (
            "Downloading the OCR model (first run only). This can take a few "
            "minutes; it is saved for next time"
        ),
        "load": "Loading the OCR model into memory",
        "layout": "Analyzing the page layout",
        "recognize": "Reading the page text",
    }

    def __init__(self):
        super().__init__(
            None,
            title="Legible - Accessible Document Scanner",
            size=(900, 620),
        )
        self.SetMinSize((760, 520))

        self.session = Session()
        self._busy = False
        self._last_added_index = None
        self._restore_focus_to = None
        # True once anything has changed the session's *content* (pages
        # added/deleted/reordered/re-OCR'd) since the last successful
        # export - renaming a page doesn't count (it isn't saved into the
        # PDF/HTML output). Drives the unsaved-changes warning on New
        # session / closing the window.
        self._dirty = False

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self._build_ui()
        self._refresh_list(select_index=None)
        self._announce_engine_mode()
        self.btn_add.SetFocus()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _make_page_preview(self, panel):
        """Build the page-detail pane via _new_html_view(): the selected
        page's *generated HTML* (export.render_page_preview_html - real
        headings/tables/paragraphs) is shown as structured content a screen
        reader can navigate, not a flattened text blob.

        Sets self._preview_is_webview so _show_page_text() knows whether to
        call SetPage() (WebView) or SetValue() (the plain-text fallback).

        For the WebView case, also wires up Esc-inside-the-page-content ->
        focus back to the page list: keystrokes inside a real embedded
        browser control are handled by the browser engine itself, not
        wx's usual EVT_CHAR_HOOK on the frame (unlike every other control
        in this window), so catching Esc there needs a small injected
        script (see _show_page_text) reporting back through
        AddScriptMessageHandler/EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED rather
        than a wx key event."""
        view, self._preview_is_webview = _new_html_view(panel)
        if self._preview_is_webview:
            view.AddScriptMessageHandler("legible")
            view.Bind(wx.html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED,
                     self._on_preview_script_message)
        return view

    def _on_preview_script_message(self, event):
        if event.GetString() == "escape":
            self.listbox.SetFocus()

    def _build_ui(self):
        panel = wx.Panel(self)
        self.panel = panel
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- top action bar --------------------------------------------
        # Three buttons: Add page, Export (its own popup menu - PDF/HTML),
        # and More (everything else - see on_more_menu/on_export_menu).
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="&Add page (scan + OCR)")
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add_page)
        top.Add(self.btn_add, 0, wx.ALL, 4)
        self.btn_export = wx.Button(panel, label="&Export")
        self.btn_export.Bind(wx.EVT_BUTTON, self.on_export_menu)
        top.Add(self.btn_export, 0, wx.ALL, 4)
        self.btn_more = wx.Button(panel, label="&More")
        self.btn_more.Bind(wx.EVT_BUTTON, self.on_more_menu)
        top.Add(self.btn_more, 0, wx.ALL, 4)

        # Global accelerators, active anywhere in the window regardless of
        # focus - Ctrl+1/2/3 for the three buttons above (in addition to
        # their own Alt+ mnemonics), and F1 for Help everywhere, not just
        # from inside the More menu.
        add_page_accel_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.on_add_page, id=add_page_accel_id)
        export_accel_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.on_export_menu, id=export_accel_id)
        more_accel_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.on_more_menu, id=more_accel_id)
        help_menu_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.on_help, id=help_menu_id)
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord("1"), add_page_accel_id),
            (wx.ACCEL_CTRL, ord("2"), export_accel_id),
            (wx.ACCEL_CTRL, ord("3"), more_accel_id),
            (wx.ACCEL_NORMAL, wx.WXK_F1, help_menu_id),
        ]))
        outer.Add(top, 0, wx.EXPAND | wx.ALL, 6)

        # --- middle: list (left) + text (right) ------------------------
        middle = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(
            wx.StaticText(panel, label="Scanned pages (use arrow keys):"),
            0, wx.BOTTOM, 4,
        )
        self.listbox = wx.ListBox(panel, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
        self.listbox.SetName("Scanned pages")
        self.listbox.Bind(wx.EVT_LISTBOX, self.on_select_page)
        self.listbox.Bind(wx.EVT_KEY_DOWN, self._on_listbox_key_down)
        # Move up/down, Rename, and Delete all live in a right-click (or
        # Menu key/Shift+F10 - EVT_CONTEXT_MENU fires for both, so this is
        # reachable without a mouse too) context menu on the page itself
        # instead of separate buttons - their keyboard shortcuts
        # (Ctrl+Up/Down, F2, Delete, still wired in _on_listbox_key_down)
        # are unchanged and shown alongside each item in that menu.
        self.listbox.Bind(wx.EVT_CONTEXT_MENU, self._on_listbox_context_menu)
        left.Add(self.listbox, 1, wx.EXPAND | wx.BOTTOM, 6)

        # Re-OCR all pages is the only button left below the list - it acts
        # on the whole session, not a single page, so it stays a direct
        # button rather than joining the per-page context menu above.
        pagebtns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_reocr = wx.Button(panel, label="Re-&OCR all pages...")
        self.btn_reocr.SetToolTip(
            "Re-run OCR on every page using a different engine, discarding "
            "the current OCR results."
        )
        self.btn_reocr.Bind(wx.EVT_BUTTON, self.on_reocr_all)
        pagebtns.Add(self.btn_reocr, 0, wx.RIGHT, 4)
        left.Add(pagebtns, 0)
        middle.Add(left, 1, wx.EXPAND | wx.ALL, 6)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(
            wx.StaticText(panel, label="Page preview (HTML):"), 0, wx.BOTTOM, 4,
        )
        self.text_area = self._make_page_preview(panel)
        self.text_area.SetName("Page preview")
        right.Add(self.text_area, 1, wx.EXPAND)
        middle.Add(right, 2, wx.EXPAND | wx.ALL, 6)

        outer.Add(middle, 1, wx.EXPAND)

        # --- status line (accessible, focusable live region) -----------
        statusrow = wx.BoxSizer(wx.HORIZONTAL)
        statusrow.Add(
            wx.StaticText(panel, label="Status:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.status_ctrl = wx.TextCtrl(
            panel, value="Ready.", style=wx.TE_READONLY,
        )
        self.status_ctrl.SetName("Status")
        statusrow.Add(self.status_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(statusrow, 0, wx.EXPAND | wx.ALL, 6)

        panel.SetSizer(outer)
        self.Layout()

    # ------------------------------------------------------------------
    # Status / announcements
    # ------------------------------------------------------------------
    def set_status(self, message: str):
        """Update the visible status line (not spoken until queried)."""
        self.status_ctrl.SetValue(message)

    def announce(self, message: str):
        """Update the status line and speak it now: briefly focus the status
        control (a native, readable control) so the screen reader reads the new
        text, then restore focus to where it was."""
        self.status_ctrl.SetValue(message)
        prev = wx.Window.FindFocus()
        if prev is self.status_ctrl:
            prev = None
        self._restore_focus_to = prev
        self.status_ctrl.SetFocus()
        wx.CallLater(700, self._restore_focus)

    def _restore_focus(self):
        target = self._restore_focus_to
        self._restore_focus_to = None
        if target and target.IsEnabled() and target.IsShownOnScreen():
            target.SetFocus()

    def _announce_engine_mode(self):
        if ocr.is_mock():
            self.set_status(
                "Ready. NOTE: the configured OCR engine isn't installed - "
                "using MOCK OCR. Check Settings, or install the missing "
                "package."
            )
        else:
            self.set_status(f"Ready. {ocr.engine_display_name()} engine loaded.")

    def _show_error(self, message: str, title: str) -> None:
        """Show an error dialog paired with the app's "error" earcon (see
        legible/sounds.py), so a failure is audible even if the user isn't
        looking at the screen when it happens - every error dialog in the
        app goes through this instead of calling wx.MessageBox directly."""
        sounds.play("error")
        wx.MessageBox(message, title, wx.OK | wx.ICON_ERROR, self)

    def _maybe_announce_rotation(self, page):
        """If legible.orientation detected this page scanned upside down
        (Page.rotation == 180 - see ocr._run_ocr), speak a direct TTS cue
        (audible regardless of whether a screen reader is running - see
        legible/tts.py) plus the usual screen-reader-readable status
        announcement. The page's list label already carries a matching
        text note (see Page.list_label) so the flag isn't lost once this
        one-off announcement has passed."""
        if page.rotation != 180:
            return
        tts.speak_async("Upside down page")
        self.announce(
            f"Warning: {page.list_label(60)} was scanned upside down. It "
            "will appear right-side up in exports, but rotate the "
            "original paper 180 degrees before filing it."
        )

    # ------------------------------------------------------------------
    # List management
    # ------------------------------------------------------------------
    def _refresh_list(self, select_index):
        items = [page.list_label(128) for page in self.session.pages]
        self.listbox.Set(items)
        if select_index is not None and 0 <= select_index < len(self.session):
            self.listbox.SetSelection(select_index)
            self.listbox.EnsureVisible(select_index)
            self._show_page_text(select_index)
        else:
            self.listbox.SetSelection(wx.NOT_FOUND)
            self._show_page_text(None)

    def _current_index(self):
        sel = self.listbox.GetSelection()
        return sel if sel != wx.NOT_FOUND else None

    # Reports Esc back to Python via the "legible" script message handler
    # registered in _make_page_preview - see its docstring for why this,
    # rather than a wx key event, is what's needed for a real embedded
    # browser control.
    _ESCAPE_SCRIPT = (
        "<script>document.addEventListener('keydown', function(e) {"
        "if (e.key === 'Escape') { window.legible.postMessage('escape'); }"
        "});</script>"
    )

    def _show_page_text(self, index):
        if index is not None and 0 <= index < len(self.session):
            page = self.session.pages[index]
            html = export.render_page_preview_html(page)
            text = page.full_text
        else:
            html = ""
            text = ""
        if self._preview_is_webview:
            # Inject the Esc-handling script right before </body> (falling
            # back to appending it if that's ever not found - export.py's
            # template always has one, but this shouldn't hard-fail either
            # way) - SetPage(html, baseUrl) loads the string directly, no
            # temp file needed. An empty string is a valid (blank) page.
            if "</body>" in html:
                html = html.replace("</body>", self._ESCAPE_SCRIPT + "</body>", 1)
            else:
                html += self._ESCAPE_SCRIPT
            self.text_area.SetPage(html, "")
        else:
            self.text_area.SetValue(text)

    # ------------------------------------------------------------------
    # Export menu (its own button - PDF / HTML) and More menu (everything
    # else: Import / New session / Settings / About / Help)
    # ------------------------------------------------------------------
    def _popup_menu_item(self, menu, label, handler):
        """Append one item to `menu` that calls `handler()` (no event arg,
        same as a button click via event=None) - each item gets a private
        id via wx.NewIdRef() so binding it doesn't collide with anything
        else in the window."""
        item_id = wx.NewIdRef()
        menu.Append(item_id, label)
        self.Bind(wx.EVT_MENU, lambda evt, h=handler: h(), id=item_id)

    def on_export_menu(self, event=None):
        menu = wx.Menu()
        self._popup_menu_item(menu, "Export to &PDF", self.on_export_pdf)
        self._popup_menu_item(menu, "Export to &HTML", self.on_export_html)
        self.btn_export.PopupMenu(menu, (0, self.btn_export.GetSize().GetHeight()))
        menu.Destroy()

    def on_more_menu(self, event=None):
        menu = wx.Menu()
        self._popup_menu_item(menu, "&Import files...", self.on_import)
        self._popup_menu_item(menu, "Start &new session", self.on_new_session)
        menu.AppendSeparator()
        self._popup_menu_item(menu, "&Settings...", self.on_settings)
        self._popup_menu_item(menu, "&About Legible...", self.on_about)
        menu.AppendSeparator()
        # \tF1 is an accurate hint, not a fabricated one - F1 opens Help
        # globally regardless of this menu (see the accelerator table in
        # _build_ui), so it still works with the menu closed too.
        self._popup_menu_item(menu, "&Help\tF1", self.on_help)
        self.btn_more.PopupMenu(menu, (0, self.btn_more.GetSize().GetHeight()))
        menu.Destroy()

    def on_about(self, event=None):
        with AboutDialog(self) as dlg:
            dlg.ShowModal()
        self.btn_more.SetFocus()

    # ------------------------------------------------------------------
    # Add page (scan + OCR) - scan_pages() returns one image per page; more
    # than one when scanning from an Automatic Document Feeder (Settings).
    # ------------------------------------------------------------------
    def on_add_page(self, event=None):
        if self._busy:
            return

        # Both the scan and the OCR step now run on a background thread:
        # scanning is silent (which device/source and DPI/color-mode
        # settings to use come from Settings, not a chooser dialog), so
        # unlike before there is no UI-thread-bound acquisition dialog to
        # wait for. A blocking ProgressDialog explains what's happening and
        # offers Cancel while that thread works.
        self._busy = True
        self._last_added_index = None
        self._set_actions_enabled(False)
        progress_dlg = ProgressDialog(self, "Add page")
        progress_dlg.set_message(self._PHASE_MESSAGES["scan"] + "...", phase_key="scan")
        sounds.play("scan_start")

        def worker():
            cfg = app_settings.load_settings()
            try:
                image_paths = scanner.scan_pages(cfg)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_scan_error, progress_dlg, str(exc))
                return
            if not image_paths:
                wx.CallAfter(self._on_scan_cancelled, progress_dlg)
                return

            total = len(image_paths)
            errors = []
            added = 0
            cancelled = False
            for page_index, image_path in enumerate(image_paths, start=1):
                if progress_dlg.cancel_requested:
                    cancelled = True
                    break

                def report(phase, current=None, blocks_total=None,
                          _idx=page_index, _total=total):
                    if progress_dlg.cancel_requested:
                        raise _JobCancelled()
                    wx.CallAfter(self._on_add_page_phase, progress_dlg, _idx, _total,
                                phase, current, blocks_total)

                sounds.play("ocr_start")
                try:
                    page = ocr.ocr_page(image_path, progress=report)
                except _JobCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Page {page_index}: {exc}")
                    continue
                sounds.play("ocr_done")
                added += 1
                wx.CallAfter(self._on_add_page_one_done, page)

            wx.CallAfter(self._on_add_page_finished, progress_dlg, total, added,
                        errors, cancelled)

        threading.Thread(target=worker, daemon=True).start()
        progress_dlg.ShowModal()
        progress_dlg.Destroy()

    def _on_scan_error(self, progress_dlg, message):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        self._show_error(message, "Scanner error")
        self.set_status(f"Scan failed: {message}")

    def _on_scan_cancelled(self, progress_dlg):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        self.set_status("Scan cancelled.")

    def _phase_text(self, phase, current=None, total=None):
        base = self._PHASE_MESSAGES.get(phase, "Working")
        if phase == "recognize" and current is not None and total:
            return f"{base} (block {current} of {total})"
        return base

    def _on_add_page_phase(self, progress_dlg, page_index, page_total, phase,
                           current, blocks_total):
        base = self._phase_text(phase, current, blocks_total)
        # Only mention "page N of M" once there's more than one to scan (ADF
        # batch) - keeps the common single-page case's messages unchanged.
        text = f"Page {page_index} of {page_total}: {base}" if page_total > 1 else base
        progress_dlg.set_message(
            text + "...", phase_key=(page_index, phase),
            current=current if phase == "recognize" else None,
            total=blocks_total if phase == "recognize" else None,
        )

    def _on_add_page_one_done(self, page):
        # Add and show progress as each page completes, rather than waiting
        # for a whole ADF batch, so scanning several pages still gives live
        # feedback.
        index = self.session.add(page)
        self._dirty = True
        self._refresh_list(select_index=index)
        self._last_added_index = index
        self._maybe_announce_rotation(page)

    def _on_add_page_finished(self, progress_dlg, total, added, errors, cancelled):
        progress_dlg.finish(wx.ID_CANCEL if cancelled else wx.ID_OK)
        self._end_busy()
        if cancelled:
            message = f"Add page cancelled. {added} of {total} scanned page(s) were added."
        elif total > 1:
            message = (
                f"Scan complete. {added} of {total} page(s) added. "
                f"Now {len(self.session)} page(s) total."
            )
        elif added:
            message = (
                f"Page added. Now {len(self.session)} page(s). "
                f"Selected page {self._last_added_index + 1}."
            )
        else:
            message = "No pages were added."
        self.set_status(message)
        self.announce(message)
        if errors:
            wx.MessageBox("\n".join(errors), "Some pages failed to scan",
                          wx.OK | wx.ICON_WARNING, self)
        # Focusing the list makes the screen reader speak the newest item and
        # its position ("N of N").
        self.listbox.SetFocus()

    def _end_busy(self):
        self._busy = False
        self._set_actions_enabled(True)

    # ------------------------------------------------------------------
    # Import (existing images / multi-page PDFs)
    # ------------------------------------------------------------------
    def on_import(self, event=None):
        if self._busy:
            return

        with wx.FileDialog(
            self, "Import images or PDFs", wildcard=_IMPORT_WILDCARD,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            paths = list(dlg.GetPaths())
        if not paths:
            return

        try:
            total_pages = importer.count_pages(paths)
        except importer.ImportError_ as exc:
            self._show_error(str(exc), "Import error")
            return
        if total_pages == 0:
            self.set_status("Nothing to import.")
            return

        self._busy = True
        self._set_actions_enabled(False)
        progress_dlg = ProgressDialog(self, "Import")
        progress_dlg.set_message(f"Importing {total_pages} page(s)...", phase_key="start")

        def worker():
            try:
                items = list(importer.iter_import_items(paths))
            except importer.ImportError_ as exc:
                wx.CallAfter(self._on_import_error, progress_dlg, str(exc))
                return

            imported = 0
            errors = []
            cancelled = False
            for item_index, item in enumerate(items, start=1):
                if progress_dlg.cancel_requested:
                    cancelled = True
                    break

                def report(phase, current=None, total=None,
                          _item=item, _idx=item_index):
                    if progress_dlg.cancel_requested:
                        raise _JobCancelled()
                    wx.CallAfter(self._on_import_phase, progress_dlg, _item, _idx,
                                len(items), phase, current, total)

                sounds.play("ocr_start")
                try:
                    page = ocr.ocr_page(item.image_path, progress=report)
                except _JobCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{item.label}: {exc}")
                    continue
                sounds.play("ocr_done")
                imported += 1
                wx.CallAfter(self._on_import_page_done, page)

            wx.CallAfter(self._on_import_finished, progress_dlg, imported, errors, cancelled)

        threading.Thread(target=worker, daemon=True).start()
        progress_dlg.ShowModal()
        progress_dlg.Destroy()

    def _on_import_phase(self, progress_dlg, item, item_index, item_total, phase,
                         current, total):
        base = self._phase_text(phase, current, total)
        text = f"Importing {item.label} ({item_index} of {item_total}): {base}"
        progress_dlg.set_message(
            text + "...", phase_key=(item_index, phase),
            current=current if phase == "recognize" else None,
            total=total if phase == "recognize" else None,
        )

    def _on_import_page_done(self, page):
        # Add and show progress as each page completes, rather than waiting
        # for the whole batch, so a large import still gives live feedback.
        index = self.session.add(page)
        self._dirty = True
        self._refresh_list(select_index=index)
        self._maybe_announce_rotation(page)

    def _on_import_finished(self, progress_dlg, imported, errors, cancelled):
        progress_dlg.finish(wx.ID_CANCEL if cancelled else wx.ID_OK)
        self._end_busy()
        if cancelled:
            message = f"Import cancelled. {imported} page(s) were added before cancelling."
        else:
            message = f"Import complete. {imported} page(s) added."
            if errors:
                message += f" {len(errors)} failed."
            if imported:
                sounds.play("import_done")
        self.set_status(message)
        self.announce(message)
        if errors:
            wx.MessageBox("\n".join(errors), "Some pages failed to import",
                          wx.OK | wx.ICON_WARNING, self)
        self.listbox.SetFocus()

    def _on_import_error(self, progress_dlg, message):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        self._show_error(message, "Import error")
        self.set_status(f"Import failed: {message}")

    # ------------------------------------------------------------------
    # Re-OCR all pages with a chosen backend (discards existing results)
    # ------------------------------------------------------------------
    def on_reocr_all(self, event=None):
        if self._busy:
            return
        if not len(self.session):
            self.announce("Nothing to re-OCR - add a page first.")
            return

        cfg = app_settings.load_settings()
        labels = [label for _, label in _OCR_ENGINE_CHOICES]
        values = [v for v, _ in _OCR_ENGINE_CHOICES]
        try:
            default_idx = values.index(cfg.ocr_engine)
        except ValueError:
            default_idx = 0

        with wx.SingleChoiceDialog(
            self, "Choose an OCR backend to re-run on every page:",
            "Re-OCR all pages", labels,
        ) as dlg:
            dlg.SetSelection(default_idx)
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            sel = dlg.GetSelection()
        # Short name (drop the parenthetical description) for status/confirm
        # text - the full descriptive label already did its job in the picker.
        engine_name, engine_label = values[sel], labels[sel].split(" (")[0]

        count = len(self.session)
        if wx.MessageBox(
            f"Re-OCR all {count} page(s) using {engine_label}?\n\n"
            "This discards the existing OCR results for every page and "
            "replaces them with new ones.",
            "Re-OCR all pages", wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return

        self._busy = True
        self._set_actions_enabled(False)
        progress_dlg = ProgressDialog(self, "Re-OCR all pages")
        progress_dlg.set_message(f"Preparing {engine_label}...", phase_key="prepare")

        # Snapshot the page list up front: it's read-only from the worker
        # thread's point of view (actions that could reorder/delete pages
        # are disabled for the whole re-OCR run).
        pages = list(self.session.pages)

        def worker():
            try:
                engine, _is_mock = ocr.build_engine(engine_name, cfg.ocr_lang)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_reocr_build_error, progress_dlg, str(exc))
                return

            total = len(pages)
            errors = []
            updated = 0
            cancelled = False
            for page_index, page in enumerate(pages, start=1):
                if progress_dlg.cancel_requested:
                    cancelled = True
                    break

                def report(phase, current=None, blocks_total=None,
                          _idx=page_index, _total=total):
                    if progress_dlg.cancel_requested:
                        raise _JobCancelled()
                    wx.CallAfter(self._on_reocr_phase, progress_dlg, _idx, _total,
                                phase, current, blocks_total)

                sounds.play("ocr_start")
                try:
                    result = ocr.ocr_page_via(engine, page.image_path, progress=report)
                except _JobCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Page {page_index}: {exc}")
                    continue
                sounds.play("ocr_done")
                updated += 1
                wx.CallAfter(self._on_reocr_page_done, page, result)

            wx.CallAfter(self._on_reocr_finished, progress_dlg, total, updated,
                        errors, engine_label, cancelled)

        threading.Thread(target=worker, daemon=True).start()
        progress_dlg.ShowModal()
        progress_dlg.Destroy()

    def _on_reocr_build_error(self, progress_dlg, message):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        self._show_error(message, "Re-OCR error")
        self.set_status(f"Re-OCR failed: {message}")

    def _on_reocr_phase(self, progress_dlg, page_index, page_total, phase,
                        current, blocks_total):
        base = self._phase_text(phase, current, blocks_total)
        text = f"Re-OCR page {page_index} of {page_total}: {base}"
        progress_dlg.set_message(
            text + "...", phase_key=(page_index, phase),
            current=current if phase == "recognize" else None,
            total=blocks_total if phase == "recognize" else None,
        )

    def _on_reocr_page_done(self, page, result):
        # Replace this page's OCR results in place - same identity/position/
        # custom name, discarded old blocks - then refresh so its list label
        # (which embeds a text preview) and the text pane pick up the change.
        page.blocks = result.blocks
        page.image_width = result.image_width
        page.image_height = result.image_height
        page.rotation = result.rotation
        self._dirty = True
        self._refresh_list(select_index=self._current_index())
        self._maybe_announce_rotation(page)

    def _on_reocr_finished(self, progress_dlg, total, updated, errors, engine_label,
                           cancelled):
        progress_dlg.finish(wx.ID_CANCEL if cancelled else wx.ID_OK)
        self._end_busy()
        if cancelled:
            message = (
                f"Re-OCR cancelled. {updated} of {total} page(s) were "
                f"updated with {engine_label}."
            )
        else:
            message = f"Re-OCR complete with {engine_label}. {updated} of {total} page(s) updated."
        self.set_status(message)
        self.announce(message)
        if errors:
            wx.MessageBox("\n".join(errors), "Some pages failed to re-OCR",
                          wx.OK | wx.ICON_WARNING, self)
        self.listbox.SetFocus()

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    def on_help(self, event=None):
        path = _help_html_path()
        try:
            opened = webbrowser.open(f"file:///{path}")
        except Exception:  # noqa: BLE001
            opened = False
        if opened:
            self.set_status("Opened Help in your web browser.")
        else:
            wx.MessageBox(
                f"Couldn't open a web browser automatically. The help page "
                f"is at:\n\n{path}",
                "Help", wx.OK | wx.ICON_INFORMATION, self,
            )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def on_settings(self, event=None):
        if self._busy:
            return
        with SettingsDialog(self) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.set_status("Settings saved.")
                self._announce_engine_mode()
            else:
                self.set_status("Settings unchanged.")
        self.btn_more.SetFocus()

    # ------------------------------------------------------------------
    # Reorder / edit
    # ------------------------------------------------------------------
    def on_select_page(self, event=None):
        self._show_page_text(self._current_index())

    def _on_listbox_context_menu(self, event):
        # EVT_CONTEXT_MENU fires for a real right-click *and* for the
        # keyboard Menu key / Shift+F10 - covering mouse and keyboard-only
        # users the same way a native Explorer-style context menu would.
        screen_pos = event.GetPosition()
        client_pos = wx.DefaultPosition
        if screen_pos != wx.DefaultPosition:
            # Real right-click at a known position - select whichever page
            # is under the cursor first (like Explorer does), rather than
            # acting on a possibly different current selection.
            client_pos = self.listbox.ScreenToClient(screen_pos)
            hit = self.listbox.HitTest(client_pos)
            if hit == wx.NOT_FOUND:
                return
            if hit != self._current_index():
                self.listbox.SetSelection(hit)
                self._show_page_text(hit)
        if self._current_index() is None:
            return

        menu = wx.Menu()
        self._popup_menu_item(menu, "Move &up\tCtrl+Up", self.on_move_up)
        self._popup_menu_item(menu, "Move &down\tCtrl+Down", self.on_move_down)
        self._popup_menu_item(menu, "&Rename\tF2", self.on_rename)
        menu.AppendSeparator()
        self._popup_menu_item(menu, "De&lete\tDelete", self.on_delete)
        self.listbox.PopupMenu(menu, client_pos)
        menu.Destroy()

    def _on_listbox_key_down(self, event):
        # Enter/Escape are NOT handled here - see _on_char_hook. Windows
        # treats Return/Escape/Tab as dialog-navigation keys and routes them
        # via IsDialogMessage() before a focused control's own EVT_KEY_DOWN
        # ever sees them, for every wx top-level window (not just
        # wx.Dialog) - EVT_CHAR_HOOK on the frame runs earlier, ahead of
        # that interception, which is the documented way to catch them.
        code = event.GetKeyCode()
        if code in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            self.on_delete()
        elif code == wx.WXK_UP and event.ControlDown():
            self.on_move_up()
        elif code == wx.WXK_DOWN and event.ControlDown():
            self.on_move_down()
        elif code == wx.WXK_INSERT:
            self.on_add_page()
        elif code == wx.WXK_F2:
            self.on_rename()
        else:
            event.Skip()  # let the listbox handle everything else (arrows, etc.)

    def _on_char_hook(self, event):
        # Catches Enter (listbox -> page preview) and Escape (page preview ->
        # listbox) before Windows' dialog-navigation key handling can
        # swallow them - see the comment on _on_listbox_key_down. Bound on
        # the frame itself (the usual place for this hook), not the
        # individual controls.
        #
        # The Escape branch below only ever fires when text_area is the
        # plain-text fallback control (no WebView backend available) or
        # when the WebView itself, not its rendered content, has focus -
        # once focus is inside the actual browser-rendered page, keystrokes
        # are handled by the browser engine and never reach this hook at
        # all; that case is covered separately, via a small injected script
        # and AddScriptMessageHandler - see _make_page_preview.
        code = event.GetKeyCode()
        focus = wx.Window.FindFocus()
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and focus is self.listbox:
            self.text_area.SetFocus()
            return
        if code == wx.WXK_ESCAPE and focus is self.text_area:
            self.listbox.SetFocus()
            return
        event.Skip()

    def on_move_up(self, event=None):
        idx = self._current_index()
        if idx is None:
            self.announce("Select a page first.")
            return
        new = self.session.move_up(idx)
        if new != idx:
            self._dirty = True
        self._refresh_list(select_index=new)
        self.set_status(f"Moved to position {new + 1}.")
        self.listbox.SetFocus()

    def on_move_down(self, event=None):
        idx = self._current_index()
        if idx is None:
            self.announce("Select a page first.")
            return
        new = self.session.move_down(idx)
        if new != idx:
            self._dirty = True
        self._refresh_list(select_index=new)
        self.set_status(f"Moved to position {new + 1}.")
        self.listbox.SetFocus()

    def on_delete(self, event=None):
        # The Delete key (unlike the button) isn't disabled by busy state, so
        # guard here too - a scan/import/export in progress shouldn't be
        # interrupted by deleting a page out from under it.
        if self._busy:
            return
        idx = self._current_index()
        if idx is None:
            self.announce("Select a page first.")
            return
        label = self.session.pages[idx].list_label(60)
        if wx.MessageBox(
            f"Delete this page?\n\n{label}", "Delete page",
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        self.session.delete(idx)
        self._dirty = True
        new_sel = min(idx, len(self.session) - 1)
        self._refresh_list(select_index=new_sel if len(self.session) else None)
        self.set_status(f"Page deleted. {len(self.session)} page(s) left.")
        self.listbox.SetFocus()

    def on_rename(self, event=None):
        idx = self._current_index()
        if idx is None:
            self.announce("Select a page first.")
            return
        page = self.session.pages[idx]
        # Pre-fill with the *current displayed title* (the default "Page N:
        # ..." if not yet renamed) so the user edits the whole visible
        # string, not an empty field - whatever they confirm becomes the
        # new title verbatim, no more auto "Page N:" prefix applied to it.
        current = page.custom_name.strip() or page.list_label()
        with wx.TextEntryDialog(
            self, "Enter a title for this page:", "Rename page", value=current,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            name = dlg.GetValue()
        self.session.rename(idx, name)
        self._refresh_list(select_index=idx)
        self.set_status(f"Page {idx + 1} renamed.")
        self.listbox.SetFocus()

    def on_new_session(self, event=None):
        if self._dirty and not self._confirm_discard_changes("Start a new session"):
            return
        self.session.clear()
        self._dirty = False
        self._refresh_list(select_index=None)
        self.set_status("New session started.")
        self.btn_add.SetFocus()

    def _confirm_discard_changes(self, action):
        # Custom button labels ("Discard changes" / "Cancel") instead of
        # generic Yes/No - wx.MessageBox can't relabel its buttons, so this
        # needs the underlying wx.MessageDialog directly.
        with wx.MessageDialog(
            self,
            "You have unsaved changes - pages added, deleted, reordered, or "
            f"re-OCR'd since the last export.\n\n{action} anyway?",
            "Unsaved changes", wx.YES_NO | wx.ICON_WARNING,
        ) as dlg:
            dlg.SetYesNoLabels("Discard changes", "Cancel")
            return dlg.ShowModal() == wx.ID_YES

    def on_close(self, event):
        if self._dirty and not self._confirm_discard_changes("Close Legible"):
            event.Veto()
            return
        event.Skip()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def on_export_html(self, event=None):
        if self._busy:
            return
        if not len(self.session):
            self.announce("Nothing to export - add a page first.")
            return
        path = self._ask_save("Export to HTML", "*.html",
                              "HTML file (*.html)|*.html")
        if not path:
            return

        def export_fn(progress, should_cancel):
            return export.export_html(self.session, path, progress=progress,
                                      should_cancel=should_cancel)

        self._run_export(path, "HTML", export_fn)

    def on_export_pdf(self, event=None):
        if self._busy:
            return
        if not len(self.session):
            self.announce("Nothing to export - add a page first.")
            return
        path = self._ask_save("Export to PDF", "*.pdf",
                              "PDF file (*.pdf)|*.pdf")
        if not path:
            return
        pdf_cfg = app_settings.load_settings()

        def export_fn(progress, should_cancel):
            return export.export_pdf(self.session, path, jpeg_quality=pdf_cfg.pdf_image_quality,
                                     image_dpi=pdf_cfg.pdf_image_dpi,
                                     progress=progress, should_cancel=should_cancel)

        self._run_export(path, "PDF", export_fn)

    def _run_export(self, path, kind, export_fn):
        # export_fn(progress, should_cancel) wraps export.export_html/pdf
        # with this session/path/(quality) already bound - runs on a
        # background thread with a blocking ProgressDialog, same pattern as
        # scan/import/re-OCR, so a large export doesn't freeze the window
        # and can be stopped mid-way (see export.ExportCancelled).
        self._busy = True
        self._set_actions_enabled(False)
        progress_dlg = ProgressDialog(self, f"Export to {kind}")
        progress_dlg.set_message("Preparing export...", phase_key="prepare")

        def worker():
            def progress(current, total):
                wx.CallAfter(self._on_export_progress, progress_dlg, current, total)

            def should_cancel():
                return progress_dlg.cancel_requested

            try:
                export_fn(progress, should_cancel)
            except export.ExportCancelled:
                wx.CallAfter(self._on_export_finished, progress_dlg, kind, path, True)
                return
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_export_error, progress_dlg, kind, str(exc))
                return
            wx.CallAfter(self._on_export_finished, progress_dlg, kind, path, False)

        threading.Thread(target=worker, daemon=True).start()
        progress_dlg.ShowModal()
        progress_dlg.Destroy()

    def _on_export_progress(self, progress_dlg, current, total):
        progress_dlg.set_message(
            f"Saving page {current} of {total}...", phase_key=current,
            current=current, total=total,
        )

    def _on_export_finished(self, progress_dlg, kind, path, cancelled):
        progress_dlg.finish(wx.ID_CANCEL if cancelled else wx.ID_OK)
        self._end_busy()
        if cancelled:
            message = f"{kind} export cancelled."
            self.set_status(message)
            self.announce(message)
            return
        self._dirty = False
        # No confirmation dialog - the status announcement (spoken
        # immediately via announce(), same as every other completed job)
        # plus the export_done sound cue are enough; a modal popup here
        # would just be one more dialog to dismiss after every export.
        message = f"Exported {kind} to {os.path.basename(path)}."
        self.set_status(message)
        self.announce(message)
        sounds.play("export_done")

    def _on_export_error(self, progress_dlg, kind, message):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        self._show_error(message, "Export error")
        self.set_status(f"{kind} export failed: {message}")

    def _ask_save(self, title, default_ext, wildcard):
        with wx.FileDialog(
            self, title, wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return None
            path = dlg.GetPath()
        # Ensure the intended extension is present.
        ext = default_ext.lstrip("*")
        if not path.lower().endswith(ext):
            path += ext
        return path

    # ------------------------------------------------------------------
    def _set_actions_enabled(self, enabled: bool):
        # Disabling btn_export/btn_more also blocks everything in their
        # popup menus (Export to PDF/HTML; Import/New session/Settings/
        # About/Help) - same effect as disabling each of those individually
        # used to have. Move up/down/Rename/Delete (now the listbox's
        # right-click menu, not buttons) and F1 Help are, as before, not
        # gated by busy state here.
        for b in (self.btn_add, self.btn_export, self.btn_more, self.btn_reocr):
            b.Enable(enabled)


def main():
    app = wx.App()
    frame = LegibleFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
