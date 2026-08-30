"""Short, synthesized UI sound effects ("earcons") for the app's key
moments: scan start, OCR start/finish, import/export success, and errors.

Deliberately not plain sine-wave beeps - each one layers a few harmonics,
an amplitude envelope, and (for some) a frequency sweep or filtered noise
so it has real auditory texture and each cue is easy to tell apart by ear
alone, closer to a phone's notification sounds than a single test tone.

Synthesized once per process with numpy, cached as ready-to-play WAV bytes,
and played via winsound.PlaySound(..., SND_MEMORY | SND_ASYNC) - nothing
touches disk, and playback never blocks the caller (winsound hands the
clip to the system mixer and returns immediately).

Optional, like the rest of the app's add-on features: winsound is
Windows-only (stdlib there); if it or numpy is unavailable, or synthesis
fails for any reason, play() is a silent no-op rather than raising.
"""

from __future__ import annotations

_SAMPLE_RATE = 22050
_CACHE: dict = {}

_NAMES = ("scan_start", "ocr_start", "ocr_done", "import_done", "export_done", "error")


def _envelope(n, attack, decay, sustain_level=1.0):
    """Attack/decay/fade-out amplitude envelope (`attack`/`decay` in
    samples) - avoids the clicks a hard on/off tone would have."""
    import numpy as np

    env = np.full(n, sustain_level, dtype=float)
    a = max(min(attack, n), 0)
    d = max(min(decay, n - a), 0)
    if a:
        env[:a] = np.linspace(0.0, 1.0, a)
    if d:
        env[a:a + d] = np.linspace(1.0, sustain_level, d)
    tail = min(n // 6, n)
    if tail:
        env[-tail:] *= np.linspace(1.0, 0.0, tail)
    return env


def _tone(freq, duration, harmonics=((1, 1.0),), sweep_to=None):
    """One tone: a sum of `harmonics` (frequency-ratio, relative-amplitude)
    pairs around `freq`, optionally sweeping linearly to `sweep_to` Hz over
    the duration (a short chirp), with an attack/decay envelope applied."""
    import numpy as np

    n = int(_SAMPLE_RATE * duration)
    t = np.arange(n) / _SAMPLE_RATE
    if sweep_to is not None:
        f = np.linspace(freq, sweep_to, n)
        phase = 2 * np.pi * np.cumsum(f) / _SAMPLE_RATE
    else:
        phase = 2 * np.pi * freq * t
    total_amp = sum(amp for _, amp in harmonics) or 1.0
    wave = sum(amp * np.sin(phase * ratio) for ratio, amp in harmonics) / total_amp
    env = _envelope(n, attack=int(0.005 * _SAMPLE_RATE), decay=int(0.03 * _SAMPLE_RATE))
    return wave * env


def _noise_swell(duration, peak=0.25):
    """A short volume-swelling burst of noise (a soft "whoosh") - a fixed
    seed keeps the synthesized clip identical (and its peak/RMS
    predictable) across runs."""
    import numpy as np

    n = int(_SAMPLE_RATE * duration)
    noise = np.random.RandomState(1234).rand(n) * 2 - 1
    env = np.linspace(0, 1, n) ** 2 * peak
    return noise * env


def _concat(*parts, gap=0.0):
    import numpy as np

    if not gap:
        return np.concatenate(parts)
    silence = np.zeros(int(_SAMPLE_RATE * gap))
    out = []
    for i, part in enumerate(parts):
        out.append(part)
        if i < len(parts) - 1:
            out.append(silence)
    return np.concatenate(out)


def _to_wav_bytes(samples) -> bytes:
    """Float samples (roughly [-1, 1]) -> 16-bit mono PCM WAV bytes."""
    import io
    import wave

    import numpy as np

    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    normalized = samples / peak * 0.9 if peak > 0.9 else samples
    pcm = np.clip(normalized * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _build(name: str) -> bytes:
    if name == "scan_start":
        # Bright upward sweep with a quiet octave overtone - "beginning".
        clip = _tone(500, 0.18, harmonics=((1, 1.0), (2, 0.25)), sweep_to=900)
    elif name == "ocr_start":
        # Two short ascending notes - a distinct shape from the scan sweep.
        a = _tone(440, 0.09, harmonics=((1, 1.0), (2, 0.3)))
        b = _tone(660, 0.11, harmonics=((1, 1.0), (2, 0.3)))
        clip = _concat(a, b, gap=0.01)
    elif name == "ocr_done":
        # Three-note rising "bell" arpeggio (a C major triad: C5-E5-G5).
        notes = [_tone(f, 0.11, harmonics=((1, 1.0), (2, 0.5), (3, 0.2)))
                for f in (523.25, 659.25, 783.99)]
        clip = _concat(*notes, gap=0.015)
    elif name == "import_done":
        # Classic two-note "ding-dong" chime (a rising perfect fifth).
        a = _tone(523.25, 0.16, harmonics=((1, 1.0), (2, 0.4), (4, 0.15)))
        b = _tone(783.99, 0.24, harmonics=((1, 1.0), (2, 0.4), (4, 0.15)))
        clip = _concat(a, b, gap=0.02)
    elif name == "export_done":
        # A soft noise "whoosh" swelling into a bright chime - a different
        # texture from the two other success sounds above, so all three
        # stay easy to tell apart by ear.
        swell = _noise_swell(0.12, peak=0.2)
        chime = _tone(880, 0.22, harmonics=((1, 1.0), (2, 0.5), (3, 0.25)))
        clip = _concat(swell, chime)
    elif name == "error":
        # Low, descending tritone with odd harmonics for a buzzy edge
        # rather than a clean alarm tone.
        a = _tone(440, 0.14, harmonics=((1, 1.0), (3, 0.35), (5, 0.2)))
        b = _tone(311, 0.22, harmonics=((1, 1.0), (3, 0.35), (5, 0.2)))
        clip = _concat(a, b)
    else:
        return b""
    return _to_wav_bytes(clip)


def play(name: str) -> None:
    """Play the named earcon - one of "scan_start", "ocr_start",
    "ocr_done", "import_done", "export_done", "error" - asynchronously.
    Silently does nothing if numpy/winsound aren't available or synthesis
    fails for any reason: never raises, never blocks the caller."""
    try:
        import winsound
    except ImportError:
        return
    try:
        data = _CACHE.get(name)
        if data is None:
            data = _build(name)
            _CACHE[name] = data
        if data:
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        pass
