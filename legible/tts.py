"""Best-effort text-to-speech via Windows SAPI5 (through pywin32), used for
short, one-off spoken cues (e.g. "Upside down page") that should be heard
immediately regardless of whether a screen reader is running - unlike
LegibleFrame.announce() (which only speaks if a screen reader is actually
active and reads the focused status control), this drives the OS's own TTS
engine directly.

Optional, like the rest of the app's add-on features: if pywin32 or SAPI
itself isn't available, speak_async() is a silent no-op rather than
raising.

Each call runs on its own short-lived daemon thread (SAPI's Speak() blocks
synchronously by default) so the GUI thread is never held up waiting for
speech to finish.
"""

from __future__ import annotations

import threading


def is_available() -> bool:
    """True if SAPI text-to-speech can actually be used (pywin32 installed
    and SAPI.SpVoice constructible)."""
    try:
        import win32com.client

        win32com.client.Dispatch("SAPI.SpVoice")
        return True
    except Exception:
        return False


def speak_async(text: str) -> None:
    """Speak `text` on a background thread. Fire-and-forget: never raises
    and never blocks the caller, even if SAPI isn't available at all."""
    def worker():
        import pythoncom

        pythoncom.CoInitialize()
        try:
            import win32com.client

            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Speak(text)
        except Exception:
            pass
        finally:
            pythoncom.CoUninitialize()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        pass
