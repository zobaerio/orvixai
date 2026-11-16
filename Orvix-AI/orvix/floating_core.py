"""Animated Floating AI Core for the ORVIX desktop app.

Two layers:

- :class:`CoreModel` — pure animation geometry. Given elapsed time and
  the current state's :class:`~orvix.core_states.AnimationParams` it
  produces a list of draw primitives (ovals, arcs, particles, a fluid
  wave ring). No tkinter import, fully unit-testable headless.
- :class:`FloatingCoreWidget` — a thin shell over a Tkinter Canvas.
  It owns a :class:`~orvix.core_states.CoreStateMachine`, runs a capped
  ``after()`` loop (state-dependent FPS, paused while not viewable),
  and redraws only its own tagged items so other canvas items (for
  example the floating HUD status dot) are never erased.

Visual identity (from the owner's references): concentric rotating
energy rings in electric blue / cyan / turquoise with subtle purple
accents, an amber inner ring, glowing particles, a fluid wave ring and
a dark transparent centre, on near-black. Tkinter has no alpha
blending, so volumetric glow is simulated with precomputed colour
ladders (dark -> bright concentric strokes), the standard technique
for Canvas rendering.

The widget never invents activity: its state is set by the app from
real backend events, and its audio *level* comes only from an optional
external provider callable (see :meth:`FloatingCoreWidget.set_level_provider`).
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional

from .core_states import (
    AMBER, CORE_BG, CYAN, ELECTRIC_BLUE, PURPLE, TURQUOISE,
    AnimationParams, CoreState, CoreStateMachine, frame_interval_ms,
)

# Primitive tuples produced by CoreModel.frame():
#   ("oval", x0, y0, x1, y1, outline, width)
#   ("fill_oval", x0, y0, x1, y1, fill)
#   ("arc", x0, y0, x1, y1, start_deg, extent_deg, outline, width)
#   ("dot", cx, cy, r, fill)
#   ("wave", (x0, y0, x1, y1, ...), fill, width)
Primitive = tuple

_PARTICLE_COUNT = 26
_RING_FRACTIONS = (1.0, 0.86, 0.72, 0.58)
_RING_COLOURS = (ELECTRIC_BLUE, CYAN, TURQUOISE, PURPLE)
# Glow ladders: outermost (faintest) -> core stroke (brightest).
_GLOW_LADDER = ("#0a2c47", "#0f4a6e", "#17719e")


def _hash01(index: int, salt: int) -> float:
    """Deterministic 0..1 pseudo-random value per (index, salt)."""
    value = math.sin(index * 127.1 + salt * 311.7) * 43758.5453
    return value - math.floor(value)


class CoreModel:
    """Pure geometry/animation model for one core of a given size."""

    def __init__(self, size: float = 280.0) -> None:
        self.set_size(size)
        self._rotation = [0.0, 0.0, 0.0, 0.0]  # degrees, per ring
        self._particle_phase = 0.0
        self._pulse_phase = 0.0

    def set_size(self, size: float) -> None:
        self.size = max(40.0, float(size))
        self.cx = self.size / 2.0
        self.cy = self.size / 2.0
        self.radius = self.size * 0.40

    # -- animation advance (deterministic; dt in seconds) --------------
    def advance(self, dt: float, params: AnimationParams) -> None:
        dt = min(max(dt, 0.0), 0.25)
        base_deg = 26.0 * params.rotation_speed  # degrees per second
        for i in range(len(self._rotation)):
            direction = 1.0 if i % 2 == 0 else -0.7
            self._rotation[i] = (self._rotation[i] + base_deg * direction * dt * (1.0 - i * 0.13)) % 360.0
        self._particle_phase += dt * params.particle_speed
        self._pulse_phase += dt * params.pulse_rate * math.tau

    # -- frame ----------------------------------------------------------
    def frame(self, params: AnimationParams, *, level: float = 0.0,
              wave_amp: Optional[float] = None,
              pulse_depth: Optional[float] = None) -> list[Primitive]:
        wave = params.wave_amp if wave_amp is None else wave_amp
        pulse = params.pulse_depth if pulse_depth is None else pulse_depth
        cx, cy, radius = self.cx, self.cy, self.radius
        breathe = 1.0 + pulse * math.sin(self._pulse_phase)
        prims: list[Primitive] = []

        # Concentric rings with glow ladders + rotating arcs.
        for i, frac in enumerate(_RING_FRACTIONS):
            r = radius * frac * breathe
            colour = params.accent if i == 1 else _RING_COLOURS[i]
            if params.glow > 0.05:
                for step, glow_colour in enumerate(_GLOW_LADDER):
                    gr = r + (len(_GLOW_LADDER) - step) * 2.2
                    prims.append(("oval", cx - gr, cy - gr, cx + gr, cy + gr,
                                  glow_colour, 1))
            prims.append(("oval", cx - r, cy - r, cx + r, cy + r,
                          "#12314d" if i else "#164e63", 1))
            # Two rotating arc segments per ring.
            for seg in range(2):
                start = self._rotation[i] + seg * 180.0 + i * 37.0
                extent = 96.0 - i * 14.0 + seg * 22.0
                prims.append(("arc", cx - r, cy - r, cx + r, cy + r,
                              start % 360.0, extent, colour, 3 if i < 2 else 2))

        # Fluid wave ring (radius modulated by travelling sines + level).
        wave_r = radius * 1.08 * breathe
        points: list[float] = []
        steps = 48
        t = self._particle_phase * 2.0
        for k in range(steps + 1):
            theta = math.tau * k / steps
            mod = 1.0 + wave * math.sin(3 * theta + 2.2 * t) \
                + 0.5 * wave * math.sin(5 * theta - 1.7 * t)
            points.append(cx + math.cos(theta) * wave_r * mod)
            points.append(cy + math.sin(theta) * wave_r * mod)
        prims.append(("wave", tuple(points), params.accent, 2))

        # Orbiting particles (active fraction set by the state).
        active = int(round(_PARTICLE_COUNT * params.particle_rate))
        for idx in range(active):
            orbit = 0.80 + 0.34 * _hash01(idx, 1)
            speed = 0.25 + 0.5 * _hash01(idx, 2)
            angle = self._particle_phase * speed * (1 if idx % 3 else -0.8) \
                + math.tau * idx / _PARTICLE_COUNT
            pr = radius * orbit * breathe
            px = cx + math.cos(angle) * pr
            py = cy + math.sin(angle) * pr * 0.96
            size = 1.1 + 1.7 * _hash01(idx, 3)
            colour = (CYAN, ELECTRIC_BLUE, TURQUOISE, PURPLE, AMBER)[idx % 5] \
                if idx % 7 else AMBER
            prims.append(("dot", px, py, size, colour))

        # Dark transparent centre + amber inner ring (video reference).
        centre_r = radius * 0.36 * breathe
        prims.append(("fill_oval", cx - centre_r, cy - centre_r,
                      cx + centre_r, cy + centre_r, "#07182b"))
        prims.append(("oval", cx - centre_r, cy - centre_r,
                      cx + centre_r, cy + centre_r, "#1d5f86", 1))
        amber_r = radius * 0.21 * breathe
        amber_colour = AMBER if params.glow > 0.7 else "#8a5a24"
        prims.append(("arc", cx - amber_r, cy - amber_r, cx + amber_r, cy + amber_r,
                      (self._rotation[0] * -0.6) % 360.0, 250.0, amber_colour, 2))
        prims.append(("dot", cx, cy, max(1.6, radius * 0.022), params.accent))
        return prims


class FloatingCoreWidget:
    """Thin Tkinter Canvas shell over CoreModel + CoreStateMachine.

    ``canvas`` may be an existing tk.Canvas (used for the floating HUD,
    whose drag/tooltip bindings already target that canvas); otherwise
    a new Canvas is created inside ``parent``. The widget only ever
    deletes items tagged ``orvix_core``.
    """

    TAG = "orvix_core"

    def __init__(self, parent=None, *, canvas=None, size: int = 280,
                 bg: str = CORE_BG, machine: Optional[CoreStateMachine] = None) -> None:
        if canvas is None:
            import tkinter as tk
            canvas = tk.Canvas(parent, width=size, height=size, bg=bg,
                               highlightthickness=0)
        self.canvas = canvas
        self.model = CoreModel(float(size))
        self.machine = machine or CoreStateMachine()
        self._level_provider: Optional[Callable[[], Optional[float]]] = None
        self._running = False
        self._after_id = None
        self._last_tick: Optional[float] = None

    # -- public control --------------------------------------------------
    @property
    def state(self) -> CoreState:
        return self.machine.state

    def set_state(self, state: CoreState) -> bool:
        return self.machine.set_state(state)

    def set_level(self, level: Optional[float]) -> None:
        self.machine.set_level(level)

    def set_level_provider(self, provider: Optional[Callable[[], Optional[float]]]) -> None:
        """Register a callable returning the live audio level 0..1.

        The provider is polled once per frame. Returning None means
        "no level available right now" and leaves the level unchanged;
        the widget never fabricates a level on its own.
        """
        self._level_provider = provider

    def resize(self, size: int) -> None:
        size = int(max(40, size))
        try:
            self.canvas.configure(width=size, height=size)
        except Exception:
            pass
        self.model.set_size(float(size))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_tick = time.monotonic()
        self._schedule(16)

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self.canvas.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    # -- driver ------------------------------------------------------------
    def _schedule(self, delay_ms: int) -> None:
        self._after_id = self.canvas.after(delay_ms, self._tick)

    def _is_viewable(self) -> bool:
        try:
            return bool(self.canvas.winfo_viewable())
        except Exception:
            return True

    def _is_minimised(self) -> bool:
        try:
            return self.canvas.winfo_toplevel().state() == "iconic"
        except Exception:
            return False

    def _tick(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        dt = 0.0 if self._last_tick is None else now - self._last_tick
        self._last_tick = now
        if self._level_provider is not None:
            try:
                self.machine.set_level(self._level_provider())
            except Exception:
                pass  # a broken level source must never kill the core
        self.machine.tick(now)
        interval = frame_interval_ms(self.machine.state,
                                     viewable=self._is_viewable(),
                                     minimised=self._is_minimised())
        if interval is None:
            # Invisible/minimised: do not draw; re-check cheaply.
            self._schedule(600)
            return
        params = self.machine.params
        self.model.advance(dt, params)
        self._draw(self.model.frame(
            params, level=self.machine.level,
            wave_amp=self.machine.effective_wave_amp(),
            pulse_depth=self.machine.effective_pulse_depth()))
        self._schedule(interval)

    def _draw(self, prims: list[Primitive]) -> None:
        canvas = self.canvas
        canvas.delete(self.TAG)
        for prim in prims:
            kind = prim[0]
            if kind == "oval":
                _, x0, y0, x1, y1, outline, width = prim
                canvas.create_oval(x0, y0, x1, y1, outline=outline,
                                   width=width, tags=self.TAG)
            elif kind == "fill_oval":
                _, x0, y0, x1, y1, fill = prim
                canvas.create_oval(x0, y0, x1, y1, fill=fill, outline="",
                                   tags=self.TAG)
            elif kind == "arc":
                _, x0, y0, x1, y1, start, extent, outline, width = prim
                canvas.create_arc(x0, y0, x1, y1, start=start, extent=extent,
                                  style="arc", outline=outline, width=width,
                                  tags=self.TAG)
            elif kind == "dot":
                _, cx, cy, r, fill = prim
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                   fill=fill, outline="", tags=self.TAG)
            elif kind == "wave":
                _, points, fill, width = prim
                canvas.create_line(*points, fill=fill, width=width,
                                   smooth=True, tags=self.TAG)
