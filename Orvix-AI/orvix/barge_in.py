"""Barge-in detection (pure, headless-testable).

While the assistant is speaking, the microphone hears the assistant's
own voice through the speakers (echo/leakage) plus the room. This
controller decides whether a *user* is deliberately interrupting:

- The first ``guard_ms`` of playback are ignored entirely — startup
  transients and the initial burst of the reply cannot trigger it.
  The loudest RMS seen during the guard window is remembered as the
  playback-leakage baseline.
- After the guard, the trigger threshold is
  ``max(threshold, baseline * baseline_mult)``: the user must be
  clearly louder than the assistant's own leakage.
- The level must stay above that threshold for ``sustain_ms`` — a
  single spike (a cough, a door, one loud syllable of the reply
  leaking late) never interrupts.

The controller latches once triggered; ``SpeechEngine`` owns the
microphone stream and the actual playback stop. No echo cancellation
is claimed: with loud speakers in a small room the baseline can rise
high enough that only a close, loud interruption registers —
headphones make barge-in reliable. See FLOATING_CORE.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BargeInConfig:
    guard_ms: float = 300.0      # ignore the start of playback
    sustain_ms: float = 300.0    # required continuous over-threshold
    threshold: float = 0.16      # absolute minimum RMS to trigger
    baseline_mult: float = 1.8   # ...and >= guard baseline * this


class BargeInController:
    """Feed playback-time mic RMS values; :meth:`feed` fires once."""

    def __init__(self, config: BargeInConfig | None = None) -> None:
        self.config = config or BargeInConfig()
        self._elapsed_ms = 0.0
        self._baseline = 0.0
        self._above_ms = 0.0
        self._triggered = False
        self._started = False

    def start(self) -> None:
        self._elapsed_ms = 0.0
        self._baseline = 0.0
        self._above_ms = 0.0
        self._triggered = False
        self._started = True

    @property
    def triggered(self) -> bool:
        return self._triggered

    @property
    def baseline(self) -> float:
        return self._baseline

    @property
    def effective_threshold(self) -> float:
        return max(self.config.threshold,
                   self._baseline * self.config.baseline_mult)

    def feed(self, rms: float, frame_ms: float) -> bool:
        if not self._started or self._triggered:
            return self._triggered
        self._elapsed_ms += frame_ms
        if self._elapsed_ms <= self.config.guard_ms:
            if rms > self._baseline:
                self._baseline = rms
            return False
        if rms >= self.effective_threshold:
            self._above_ms += frame_ms
            if self._above_ms >= self.config.sustain_ms:
                self._triggered = True
                return True
        else:
            self._above_ms = 0.0
        return False
