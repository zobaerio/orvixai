"""Real audio level measurement for the floating core.

Everything in this module is pure/stdlib and headless-testable. The
levels come from the actual PCM frames the app captures (microphone)
or from the actual synthesised audio file it plays back — never from
an animation parameter. See ``orvix/speech.py`` for the wiring and
``FLOATING_CORE.md`` for the honest limits.

- :func:`pcm_rms` — RMS of a PCM chunk, normalised to 0..1.
- :func:`level_from_rms` — perceptual-ish mapping of RMS to a 0..1
  meter level (square-root curve so quiet speech still registers).
- :class:`LevelMeter` — thread-safe smoothed meter with staleness:
  :meth:`LevelMeter.value` returns ``None`` when no fresh frame has
  arrived, so callers fall back to state-driven animation instead of
  showing a frozen (fabricated) level.
- :class:`PlaybackEnvelope` — per-bin levels precomputed from a real
  audio buffer, looked up by playback position.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from array import array

# RMS at which the meter reads full scale (before clamping). Typical
# close-mic speech peaks around 0.1–0.4 RMS on a 16-bit capture.
_LEVEL_FULL_SCALE_RMS = 0.45

_TYPECODES = {2: "h", 4: "i"}
_FULL_SCALE = {2: 32768.0, 4: 2147483648.0}


def pcm_rms(data: bytes, sample_width: int = 2) -> float:
    """RMS of a PCM chunk as a 0..1 fraction of full scale.

    Only the sample widths the app actually records/plays (16-bit and
    32-bit signed PCM) are supported; anything else reads as silence
    rather than guessing at an encoding.
    """
    if not data or sample_width not in _TYPECODES:
        return 0.0
    usable = len(data) - (len(data) % sample_width)
    if usable <= 0:
        return 0.0
    samples = array(_TYPECODES[sample_width])
    samples.frombytes(data[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    mean_square = sum(int(s) * int(s) for s in samples) / len(samples)
    return math.sqrt(mean_square) / _FULL_SCALE[sample_width]


def level_from_rms(rms: float) -> float:
    """Map an RMS value (0..1 of full scale) to a 0..1 meter level."""
    if rms <= 0.0:
        return 0.0
    return min(1.0, math.sqrt(rms / _LEVEL_FULL_SCALE_RMS))


class LevelMeter:
    """Thread-safe smoothed level with staleness detection.

    The capture thread calls :meth:`update` per audio chunk; the UI
    thread polls :meth:`value`. Smoothing uses a fast attack and a
    slower decay so speech onsets register immediately while the meter
    does not flicker between syllables. A value older than
    ``stale_after`` seconds is reported as ``None`` (no live source),
    which is what keeps the core honest when nothing is recording.
    """

    def __init__(self, stale_after: float = 0.4) -> None:
        self._lock = threading.Lock()
        self._value = 0.0
        self._updated_at = 0.0
        self._stale_after = float(stale_after)

    def update(self, level: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        level = min(1.0, max(0.0, float(level)))
        with self._lock:
            factor = 0.7 if level >= self._value else 0.3
            self._value += (level - self._value) * factor
            self._updated_at = now

    def value(self, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._updated_at <= 0.0:
                return None
            if now - self._updated_at > self._stale_after:
                return None
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = 0.0
            self._updated_at = 0.0


class PlaybackEnvelope:
    """Precomputed per-bin meter levels for one audio clip.

    Built from the real samples of the file about to be played (see
    ``SpeechEngine._build_playback_envelope``), so the SPEAKING level
    tracks the actual loudness of the reply at each moment instead of
    a generic pulse.
    """

    def __init__(self, levels: list[float], bin_ms: float = 100.0) -> None:
        self._levels = [min(1.0, max(0.0, float(v))) for v in levels]
        self._bin_ms = float(bin_ms) if bin_ms > 0 else 100.0

    def __len__(self) -> int:
        return len(self._levels)

    @property
    def duration_ms(self) -> float:
        return len(self._levels) * self._bin_ms

    def level_at(self, elapsed_ms: float) -> float:
        if not self._levels:
            return 0.0
        index = int(max(0.0, elapsed_ms) // self._bin_ms)
        if index >= len(self._levels):
            index = len(self._levels) - 1
        return self._levels[index]
