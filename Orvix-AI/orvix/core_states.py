"""State machine for the ORVIX Floating AI Core.

Pure logic only: this module never imports tkinter, so the full state
behaviour (transitions, audio-level smoothing, FPS throttling, the
COMPLETED auto-return and the sticky ERROR state) is unit-testable
headless. The Canvas widget in :mod:`orvix.floating_core` is a thin
rendering shell over :class:`CoreStateMachine` and the per-state
:class:`AnimationParams` defined here.

States are driven by real application events only (see FLOATING_CORE.md
for the wiring map in app.py). Nothing in this module invents activity:
LISTENING / SPEAKING accept an external audio *level* (0..1) through
:meth:`CoreStateMachine.set_level`; when no level source is connected
the level stays at 0 and those states fall back to their state-driven
pulse, which is an animation parameter, not fabricated audio data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class CoreState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    PROCESSING = "processing"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    COMPLETED = "completed"
    ERROR = "error"


# Palette (grounded in the owner's references: electric blue / cyan /
# turquoise energy rings, subtle purple accents, an amber inner ring,
# dark transparent centre, on near-black).
ELECTRIC_BLUE = "#2fb9ff"
CYAN = "#22d3ee"
TURQUOISE = "#2dd4bf"
PURPLE = "#8b7cff"
AMBER = "#ffb36b"
CORE_BG = "#050b17"
ERROR_RED = "#ff5a5a"
ERROR_AMBER = "#f59e0b"


@dataclass(frozen=True)
class AnimationParams:
    """Per-state animation tuning. Speeds are relative multipliers."""

    rotation_speed: float   # ring rotation speed multiplier
    pulse_rate: float       # breathing/pulse cycles per second
    pulse_depth: float      # 0..1 radius modulation depth
    particle_rate: float    # 0..1 fraction of the particle field active
    particle_speed: float   # particle orbit speed multiplier
    glow: float             # 0..1 glow layer intensity
    wave_amp: float         # fluid wave ring amplitude (fraction of radius)
    fps: int                # frame-rate cap for this state
    accent: str             # dominant accent colour for the state
    level_reactive: bool    # True when an audio level modulates amplitude


STATE_PARAMS: dict[CoreState, AnimationParams] = {
    CoreState.IDLE: AnimationParams(
        rotation_speed=0.25, pulse_rate=0.22, pulse_depth=0.03,
        particle_rate=0.35, particle_speed=0.3, glow=0.45, wave_amp=0.012,
        fps=12, accent=ELECTRIC_BLUE, level_reactive=False),
    CoreState.LISTENING: AnimationParams(
        rotation_speed=0.5, pulse_rate=0.9, pulse_depth=0.06,
        particle_rate=0.6, particle_speed=0.6, glow=0.7, wave_amp=0.03,
        fps=30, accent=CYAN, level_reactive=True),
    CoreState.THINKING: AnimationParams(
        rotation_speed=1.5, pulse_rate=0.6, pulse_depth=0.04,
        particle_rate=0.8, particle_speed=1.2, glow=0.75, wave_amp=0.02,
        fps=24, accent=PURPLE, level_reactive=False),
    CoreState.PROCESSING: AnimationParams(
        rotation_speed=1.1, pulse_rate=1.1, pulse_depth=0.05,
        particle_rate=0.9, particle_speed=1.0, glow=0.85, wave_amp=0.028,
        fps=30, accent=ELECTRIC_BLUE, level_reactive=False),
    CoreState.EXECUTING: AnimationParams(
        rotation_speed=1.3, pulse_rate=0.8, pulse_depth=0.05,
        particle_rate=1.0, particle_speed=1.4, glow=0.9, wave_amp=0.035,
        fps=30, accent=TURQUOISE, level_reactive=False),
    CoreState.SPEAKING: AnimationParams(
        rotation_speed=0.45, pulse_rate=1.3, pulse_depth=0.07,
        particle_rate=0.55, particle_speed=0.5, glow=0.8, wave_amp=0.04,
        fps=30, accent=CYAN, level_reactive=True),
    CoreState.COMPLETED: AnimationParams(
        rotation_speed=0.6, pulse_rate=0.5, pulse_depth=0.09,
        particle_rate=1.0, particle_speed=0.8, glow=1.0, wave_amp=0.02,
        fps=24, accent=TURQUOISE, level_reactive=False),
    CoreState.ERROR: AnimationParams(
        rotation_speed=0.2, pulse_rate=0.55, pulse_depth=0.08,
        particle_rate=0.25, particle_speed=0.25, glow=0.65, wave_amp=0.015,
        fps=15, accent=ERROR_RED, level_reactive=False),
}

#: How long COMPLETED stays on screen before returning to IDLE.
COMPLETED_HOLD_SECONDS = 2.2

#: Level smoothing: fast attack, slower decay (per tick call).
_LEVEL_ATTACK = 0.55
_LEVEL_DECAY = 0.18


def frame_interval_ms(state: CoreState, *, viewable: bool = True,
                      minimised: bool = False) -> Optional[int]:
    """Milliseconds until the next frame, or None to pause entirely.

    Throttle rules (Part 16 performance requirement):
    - not viewable or minimised -> None (the driver re-checks cheaply
      on a slow timer instead of redrawing an invisible core);
    - otherwise 1000/fps for the state's cap (IDLE is deliberately
      slow: a breathing core does not need 30 FPS).
    """
    if not viewable or minimised:
        return None
    fps = max(1, STATE_PARAMS[state].fps)
    return max(16, int(round(1000.0 / fps)))


class CoreStateMachine:
    """Current core state + level smoothing + timed auto-return.

    ``clock`` is injectable for tests; production uses time.monotonic.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 completed_hold: float = COMPLETED_HOLD_SECONDS) -> None:
        self._clock = clock
        self._completed_hold = float(completed_hold)
        self._state = CoreState.IDLE
        self._entered_at = self._clock()
        self._level = 0.0
        self._level_target = 0.0
        # Bounded transition history for inspection/tests: (state, at).
        self.history: list[tuple[CoreState, float]] = [(self._state, self._entered_at)]

    # -- state ---------------------------------------------------------
    @property
    def state(self) -> CoreState:
        return self._state

    @property
    def params(self) -> AnimationParams:
        return STATE_PARAMS[self._state]

    def set_state(self, state: CoreState) -> bool:
        """Transition to ``state``. Returns False for a no-op repeat."""
        if state is self._state:
            return False
        self._state = state
        self._entered_at = self._clock()
        self.history.append((state, self._entered_at))
        if len(self.history) > 64:
            del self.history[: len(self.history) - 64]
        return True

    def clear_error(self) -> bool:
        """Leave the sticky ERROR state (next interaction also does)."""
        if self._state is CoreState.ERROR:
            return self.set_state(CoreState.IDLE)
        return False

    def time_in_state(self, now: Optional[float] = None) -> float:
        return (self._clock() if now is None else now) - self._entered_at

    def tick(self, now: Optional[float] = None) -> CoreState:
        """Advance time-based behaviour; returns the effective state.

        COMPLETED auto-returns to IDLE after the hold time. ERROR never
        auto-clears. Level smoothing advances one step per tick.
        """
        now = self._clock() if now is None else now
        if self._state is CoreState.COMPLETED and (now - self._entered_at) >= self._completed_hold:
            self.set_state(CoreState.IDLE)
        # Smooth the audio level toward its target.
        diff = self._level_target - self._level
        step = _LEVEL_ATTACK if diff > 0 else _LEVEL_DECAY
        self._level += diff * step
        if abs(self._level - self._level_target) < 0.001:
            self._level = self._level_target
        return self._state

    # -- audio level ---------------------------------------------------
    def set_level(self, level: Optional[float]) -> None:
        """Set the target audio level (0..1). None leaves it unchanged."""
        if level is None:
            return
        self._level_target = min(1.0, max(0.0, float(level)))

    @property
    def level(self) -> float:
        """Smoothed level actually used for amplitude modulation."""
        return self._level

    def effective_wave_amp(self) -> float:
        """Wave amplitude for this frame: level-reactive states scale
        with the smoothed audio level; others use their base amplitude."""
        params = self.params
        if params.level_reactive:
            return params.wave_amp * (0.35 + 1.3 * self._level)
        return params.wave_amp

    def effective_pulse_depth(self) -> float:
        params = self.params
        if params.level_reactive:
            return params.pulse_depth * (0.5 + 1.1 * self._level)
        return params.pulse_depth
