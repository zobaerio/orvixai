"""Energy-based voice activity detection (pure, headless-testable).

A small state machine over per-frame RMS values:

- While idle, the noise floor adapts (EMA) toward quiet frames, and
  the speech threshold rides on top of it
  (``max(start_rms, noise_floor * start_ratio)``), so a fan or room
  tone does not trigger recording but speech above it does.
- Speech starts only after the level stays above the threshold for
  ``speech_start_ms`` — clicks and bumps are ignored.
- An utterance ends after ``silence_end_ms`` below the threshold, or
  unconditionally at ``max_utterance_ms``.
- :class:`VadSegmenter` adds the frame bookkeeping: a pre-roll ring
  buffer (so the first syllable is not clipped), collection while
  speech is active, and a minimum-utterance filter that discards
  blips too short to be a command.

No audio device is touched here; ``orvix/speech.py`` feeds real
PyAudio frames in and hands the returned utterance bytes to the
recogniser as an ``sr.AudioData``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class VadConfig:
    start_rms: float = 0.02          # absolute minimum RMS for speech
    start_ratio: float = 3.0         # ...or noise floor * this ratio
    speech_start_ms: float = 90.0    # sustained level before "start"
    silence_end_ms: float = 700.0    # trailing silence that ends it
    pre_roll_ms: float = 300.0       # audio kept from before "start"
    max_utterance_ms: float = 12000.0
    min_utterance_ms: float = 250.0
    noise_alpha: float = 0.08        # noise-floor EMA rate while idle
    initial_noise_floor: float = 0.004


class EnergyVad:
    """Feed RMS values; get ``"start"`` / ``"end"`` events back."""

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self._noise_floor = self.config.initial_noise_floor
        self._in_speech = False
        self._start_run_ms = 0.0
        self._silence_ms = 0.0
        self._utterance_ms = 0.0

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_ms(self) -> float:
        """Duration of the current utterance so far (0 when idle)."""
        return self._utterance_ms if self._in_speech else 0.0

    @property
    def threshold(self) -> float:
        return max(self.config.start_rms,
                   self._noise_floor * self.config.start_ratio)

    def feed(self, rms: float, frame_ms: float) -> str | None:
        cfg = self.config
        if not self._in_speech:
            if rms >= self.threshold:
                self._start_run_ms += frame_ms
                if self._start_run_ms >= cfg.speech_start_ms:
                    self._in_speech = True
                    self._utterance_ms = self._start_run_ms
                    self._silence_ms = 0.0
                    self._start_run_ms = 0.0
                    return "start"
            else:
                self._start_run_ms = 0.0
                # Adapt only on below-threshold frames so speech does
                # not raise its own threshold.
                self._noise_floor += (rms - self._noise_floor) * cfg.noise_alpha
                if self._noise_floor < 0.0:
                    self._noise_floor = 0.0
            return None
        self._utterance_ms += frame_ms
        if rms >= self.threshold:
            self._silence_ms = 0.0
        else:
            self._silence_ms += frame_ms
        if (self._silence_ms >= cfg.silence_end_ms
                or self._utterance_ms >= cfg.max_utterance_ms):
            self._in_speech = False
            self._start_run_ms = 0.0
            self._silence_ms = 0.0
            self._utterance_ms = 0.0
            return "end"
        return None


class VadSegmenter:
    """Turns a frame stream into complete utterance byte strings."""

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self.vad = EnergyVad(self.config)
        self._pre_roll: deque[tuple[bytes, float]] = deque()
        self._pre_roll_ms = 0.0
        self._collected: list[bytes] | None = None
        self._collected_ms = 0.0
        self._voiced_ms = 0.0

    @property
    def in_speech(self) -> bool:
        return self._collected is not None

    def feed(self, pcm: bytes, rms: float, frame_ms: float) -> bytes | None:
        threshold = self.vad.threshold
        event = self.vad.feed(rms, frame_ms)
        if self._collected is not None:
            self._collected.append(pcm)
            self._collected_ms += frame_ms
            if rms >= threshold:
                self._voiced_ms += frame_ms
        elif event == "start":
            self._collected = [frame for frame, _ in self._pre_roll] + [pcm]
            self._collected_ms = self._pre_roll_ms + frame_ms
            # The run-up frames that crossed the threshold were voiced
            # too; the VAD's utterance clock already counts them.
            self._voiced_ms = self.vad.speech_ms
            self._pre_roll.clear()
            self._pre_roll_ms = 0.0
        else:
            self._pre_roll.append((pcm, frame_ms))
            self._pre_roll_ms += frame_ms
            while (self._pre_roll_ms > self.config.pre_roll_ms
                   and len(self._pre_roll) > 1):
                _, dropped_ms = self._pre_roll.popleft()
                self._pre_roll_ms -= dropped_ms
        if event == "end" and self._collected is not None:
            collected, voiced_ms = self._collected, self._voiced_ms
            self._collected = None
            self._collected_ms = 0.0
            self._voiced_ms = 0.0
            # The minimum applies to *voiced* time, so a click or bump
            # that crossed the start threshold is discarded even though
            # pre-roll and trailing silence padded its buffer.
            if voiced_ms >= self.config.min_utterance_ms:
                return b"".join(collected)
        return None
