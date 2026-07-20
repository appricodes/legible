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
  * Keyboard mnemonics via "&" in labels (Alt+A add, Alt+H html, Alt+P pdf,
    Alt+N new, Alt+U up, Alt+D down, Alt+L delete, Alt+R rename). wx draws the
    underline and wires the accelerator automatically.
  * The page list is a native wx.ListBox: arrowing through it makes the screen
    reader speak the item text and its position ("3 of 8").
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
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser

import wx

from . import export, importer, languages, ocr, scanner
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
    ("paddleocr", "PaddleOCR (most accurate, slowest)"),
    ("rapidocr", "RapidOCR (fast, lightweight)"),
    ("tesseract", "Tesseract (fastest - needs separate install, see README)"),
    ("mock", "Mock (testing only - no real OCR)"),
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


class SettingsDialog(wx.Dialog):
    """Scanner (backend/device/DPI/color mode) and OCR engine choice.

    Saved via legible.settings; Add page then scans directly with no chooser
    of its own, and OCR uses whichever engine is configured here.
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

        grid.Add(wx.StaticText(self, label="OCR &engine:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.engine_choice = wx.Choice(
            self, choices=[label for _, label in _OCR_ENGINE_CHOICES])
        self.engine_choice.Bind(wx.EVT_CHOICE, self._on_engine_or_lang_changed)
        grid.Add(self.engine_choice, 1, wx.EXPAND)

        # Applies to whichever engine is selected above - see
        # legible/languages.py for how one choice maps to each engine's own
        # language code/model.
        grid.Add(wx.StaticText(self, label="Preferred &language:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.lang_list = languages.all_languages()
        self.lang_choice = wx.Choice(
            self, choices=[lang.display_name for lang in self.lang_list])
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
        lang_codes = [lang.code for lang in self.lang_list]
        self._select_by_value(self.lang_choice, lang_codes, self.cfg.ocr_lang)
        self._select_nearest(self.pdf_quality_choice,
                             app_settings.PDF_IMAGE_QUALITY_CHOICES,
                             self.cfg.pdf_image_quality)
        self._select_nearest(self.pdf_dpi_choice,
                             app_settings.PDF_IMAGE_DPI_CHOICES,
                             self.cfg.pdf_image_dpi)

        # Also check right when the dialog opens, in case Tesseract is
        # already the saved engine and isn't (or is no longer) actually
        # usable - otherwise the only sign would be Mock OCR silently
        # kicking in later, at scan time.
        self._maybe_warn_tesseract()

    def _on_engine_or_lang_changed(self, event):
        event.Skip()
        self._maybe_warn_tesseract()

    def _maybe_warn_tesseract(self):
        engine = _OCR_ENGINE_CHOICES[self.engine_choice.GetSelection()][0]
        if engine != "tesseract":
            return
        lang_code = self.lang_list[self.lang_choice.GetSelection()].code
        problem = ocr.check_tesseract(lang_code)
        if problem:
            wx.MessageBox(problem, "Tesseract isn't ready",
                          wx.OK | wx.ICON_WARNING, self)

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
    def _build_ui(self):
        panel = wx.Panel(self)
        self.panel = panel
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- top action bar --------------------------------------------
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="&Add page (scan + OCR)")
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add_page)
        self.btn_import = wx.Button(panel, label="&Import files...")
        self.btn_import.Bind(wx.EVT_BUTTON, self.on_import)
        self.btn_export_html = wx.Button(panel, label="Export to &HTML")
        self.btn_export_html.Bind(wx.EVT_BUTTON, self.on_export_html)
        self.btn_export_pdf = wx.Button(panel, label="Export to &PDF")
        self.btn_export_pdf.Bind(wx.EVT_BUTTON, self.on_export_pdf)
        self.btn_new = wx.Button(panel, label="Start &new session")
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new_session)
        self.btn_settings = wx.Button(panel, label="&Settings...")
        self.btn_settings.Bind(wx.EVT_BUTTON, self.on_settings)
        # "Help" starts with H, already taken by "Export to &HTML" in this
        # window - mnemonic goes on the second letter instead (H&elp),
        # matching the usual convention when the first letter collides.
        self.btn_help = wx.Button(panel, label="H&elp (F1)")
        self.btn_help.Bind(wx.EVT_BUTTON, self.on_help)
        for b in (self.btn_add, self.btn_import, self.btn_export_html,
                  self.btn_export_pdf, self.btn_new, self.btn_settings,
                  self.btn_help):
            top.Add(b, 0, wx.ALL, 4)

        # F1 works everywhere in the window, not just when the Help button
        # itself has focus - the usual convention for a help key.
        help_menu_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.on_help, id=help_menu_id)
        self.SetAcceleratorTable(wx.AcceleratorTable(
            [(wx.ACCEL_NORMAL, wx.WXK_F1, help_menu_id)]))
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
        left.Add(self.listbox, 1, wx.EXPAND | wx.BOTTOM, 6)

        pagebtns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_up = wx.Button(panel, label="Move &up")
        self.btn_up.Bind(wx.EVT_BUTTON, self.on_move_up)
        self.btn_down = wx.Button(panel, label="Move &down")
        self.btn_down.Bind(wx.EVT_BUTTON, self.on_move_down)
        self.btn_delete = wx.Button(panel, label="De&lete")
        self.btn_delete.Bind(wx.EVT_BUTTON, self.on_delete)
        self.btn_rename = wx.Button(panel, label="&Rename")
        self.btn_rename.Bind(wx.EVT_BUTTON, self.on_rename)
        for b in (self.btn_up, self.btn_down, self.btn_delete, self.btn_rename):
            pagebtns.Add(b, 0, wx.RIGHT, 4)
        # Visual separator: everything to its left acts on the *selected*
        # page; everything to its right (just Re-OCR) acts on the *whole*
        # session. Purely visual grouping for sighted users - it isn't
        # focusable, so tab order for screen reader users still flows
        # straight from Rename to Re-OCR in natural creation order.
        pagebtns.Add(wx.StaticLine(panel, style=wx.LI_VERTICAL),
                    0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
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
            wx.StaticText(panel, label="Full page text:"), 0, wx.BOTTOM, 4,
        )
        self.text_area = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.text_area.SetName("Full page text")
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

    def _show_page_text(self, index):
        if index is not None and 0 <= index < len(self.session):
            self.text_area.SetValue(self.session.pages[index].full_text)
        else:
            self.text_area.SetValue("")

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

                try:
                    page = ocr.ocr_page(image_path, progress=report)
                except _JobCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Page {page_index}: {exc}")
                    continue
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
        wx.MessageBox(message, "Scanner error", wx.OK | wx.ICON_ERROR, self)
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
            wx.MessageBox(str(exc), "Import error", wx.OK | wx.ICON_ERROR, self)
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

                try:
                    page = ocr.ocr_page(item.image_path, progress=report)
                except _JobCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{item.label}: {exc}")
                    continue
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

    def _on_import_finished(self, progress_dlg, imported, errors, cancelled):
        progress_dlg.finish(wx.ID_CANCEL if cancelled else wx.ID_OK)
        self._end_busy()
        if cancelled:
            message = f"Import cancelled. {imported} page(s) were added before cancelling."
        else:
            message = f"Import complete. {imported} page(s) added."
            if errors:
                message += f" {len(errors)} failed."
        self.set_status(message)
        self.announce(message)
        if errors:
            wx.MessageBox("\n".join(errors), "Some pages failed to import",
                          wx.OK | wx.ICON_WARNING, self)
        self.listbox.SetFocus()

    def _on_import_error(self, progress_dlg, message):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        wx.MessageBox(message, "Import error", wx.OK | wx.ICON_ERROR, self)
        self.set_status(f"Import failed: {message}")

    # ------------------------------------------------------------------
    # Re-OCR all pages with a chosen engine (discards existing results)
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
            self, "Choose an OCR engine to re-run on every page:",
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

                try:
                    result = ocr.ocr_page_via(engine, page.image_path, progress=report)
                except _JobCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Page {page_index}: {exc}")
                    continue
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
        wx.MessageBox(message, "Re-OCR error", wx.OK | wx.ICON_ERROR, self)
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
        self._dirty = True
        self._refresh_list(select_index=self._current_index())

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
        self.btn_add.SetFocus()

    # ------------------------------------------------------------------
    # Reorder / edit
    # ------------------------------------------------------------------
    def on_select_page(self, event=None):
        self._show_page_text(self._current_index())

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
        # Catches Enter (listbox -> full text) and Escape (full text ->
        # listbox) before Windows' dialog-navigation key handling can
        # swallow them - see the comment on _on_listbox_key_down. Bound on
        # the frame itself (the usual place for this hook), not the
        # individual controls.
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
        self.set_status(f"Exported {kind} to {os.path.basename(path)}.")
        wx.MessageBox(f"Saved:\n{path}", "Export complete",
                      wx.OK | wx.ICON_INFORMATION, self)

    def _on_export_error(self, progress_dlg, kind, message):
        progress_dlg.finish(wx.ID_CANCEL)
        self._end_busy()
        wx.MessageBox(message, "Export error", wx.OK | wx.ICON_ERROR, self)
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
        for b in (self.btn_add, self.btn_import, self.btn_export_html,
                  self.btn_export_pdf, self.btn_new, self.btn_settings,
                  self.btn_up, self.btn_down, self.btn_delete, self.btn_rename,
                  self.btn_reocr):
            b.Enable(enabled)


def main():
    app = wx.App()
    frame = LegibleFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
