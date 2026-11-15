"""Headless tests for the Floating AI Core (state machine + geometry).

No Tk root is created: the state machine and CoreModel are pure, and
the widget is exercised against a recording fake canvas.
"""
import math
import unittest

from orvix.core_states import (
    STATE_PARAMS, CoreState, CoreStateMachine, frame_interval_ms,
)
from orvix.floating_core import CoreModel, FloatingCoreWidget


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.machine = CoreStateMachine(clock=self.clock)

    def test_starts_idle(self):
        self.assertIs(self.machine.state, CoreState.IDLE)

    def test_all_eight_states_have_params(self):
        self.assertEqual(len(list(CoreState)), 8)
        for state in CoreState:
            self.assertIn(state, STATE_PARAMS)
            self.assertGreater(STATE_PARAMS[state].fps, 0)

    def test_transition_and_noop(self):
        self.assertTrue(self.machine.set_state(CoreState.LISTENING))
        self.assertFalse(self.machine.set_state(CoreState.LISTENING))
        self.assertIs(self.machine.state, CoreState.LISTENING)

    def test_completed_auto_returns_to_idle_after_hold(self):
        self.machine.set_state(CoreState.COMPLETED)
        self.clock.now += 1.0
        self.assertIs(self.machine.tick(), CoreState.COMPLETED)
        self.clock.now += 2.0
        self.assertIs(self.machine.tick(), CoreState.IDLE)

    def test_error_is_sticky_until_cleared(self):
        self.machine.set_state(CoreState.ERROR)
        self.clock.now += 60.0
        self.assertIs(self.machine.tick(), CoreState.ERROR)
        self.assertTrue(self.machine.clear_error())
        self.assertIs(self.machine.state, CoreState.IDLE)

    def test_error_cleared_by_next_interaction(self):
        self.machine.set_state(CoreState.ERROR)
        self.machine.set_state(CoreState.LISTENING)
        self.assertIs(self.machine.state, CoreState.LISTENING)

    def test_history_is_bounded(self):
        states = list(CoreState)
        for i in range(200):
            self.machine.set_state(states[i % len(states)])
        self.assertLessEqual(len(self.machine.history), 64)

    def test_level_clamped_and_none_ignored(self):
        self.machine.set_level(2.5)
        self.machine.tick()
        self.assertLessEqual(self.machine.level, 1.0)
        before = self.machine.level
        self.machine.set_level(None)
        self.assertEqual(self.machine._level_target, 1.0)
        self.assertGreaterEqual(before, 0.0)

    def test_level_smoothing_moves_toward_target(self):
        self.machine.set_level(1.0)
        first = (self.machine.tick(), self.machine.level)[1]
        self.assertGreater(first, 0.0)
        self.assertLess(first, 1.0)
        for _ in range(40):
            self.machine.tick()
        self.assertAlmostEqual(self.machine.level, 1.0, places=2)

    def test_level_reactive_states_scale_with_level(self):
        idle_amp = self.machine.effective_wave_amp()
        self.machine.set_state(CoreState.LISTENING)
        quiet = self.machine.effective_wave_amp()
        self.machine.set_level(1.0)
        for _ in range(40):
            self.machine.tick()
        loud = self.machine.effective_wave_amp()
        self.assertGreater(loud, quiet)
        self.assertGreater(quiet, 0.0)
        self.assertGreater(idle_amp, 0.0)


class TestFrameInterval(unittest.TestCase):
    def test_idle_is_slow_and_capped(self):
        interval = frame_interval_ms(CoreState.IDLE)
        self.assertEqual(interval, 1000 // 12 if 1000 % 12 == 0 else round(1000 / 12))

    def test_active_states_faster_than_idle(self):
        self.assertLess(frame_interval_ms(CoreState.EXECUTING),
                        frame_interval_ms(CoreState.IDLE))

    def test_not_viewable_pauses(self):
        self.assertIsNone(frame_interval_ms(CoreState.EXECUTING, viewable=False))

    def test_minimised_pauses(self):
        self.assertIsNone(frame_interval_ms(CoreState.EXECUTING, minimised=True))


class TestCoreModel(unittest.TestCase):
    def setUp(self):
        self.model = CoreModel(280)

    def test_frame_has_all_primitive_kinds(self):
        prims = self.model.frame(STATE_PARAMS[CoreState.EXECUTING])
        kinds = {p[0] for p in prims}
        self.assertTrue({"oval", "arc", "dot", "wave", "fill_oval"} <= kinds)

    def test_frame_geometry_within_canvas(self):
        for state in CoreState:
            prims = self.model.frame(STATE_PARAMS[state])
            for prim in prims:
                if prim[0] in ("oval", "fill_oval", "arc"):
                    _, x0, y0, x1, y1 = prim[:5]
                    self.assertGreaterEqual(x0, -20)
                    self.assertGreaterEqual(y0, -20)
                    self.assertLessEqual(x1, 300)
                    self.assertLessEqual(y1, 300)

    def test_particle_count_scales_with_state_rate(self):
        def dots(state):
            prims = self.model.frame(STATE_PARAMS[state])
            return len([p for p in prims if p[0] == "dot"])
        self.assertGreater(dots(CoreState.EXECUTING), dots(CoreState.IDLE))

    def test_advance_rotates_rings(self):
        before = list(self.model._rotation)
        self.model.advance(0.5, STATE_PARAMS[CoreState.THINKING])
        after = self.model._rotation
        self.assertTrue(any(abs(a - b) > 0.01 for a, b in zip(before, after)))

    def test_advance_clamps_large_dt(self):
        self.model.advance(60.0, STATE_PARAMS[CoreState.THINKING])
        self.model.advance(0.25, STATE_PARAMS[CoreState.THINKING])
        twin = CoreModel(280)
        twin.advance(0.25, STATE_PARAMS[CoreState.THINKING])
        twin.advance(0.25, STATE_PARAMS[CoreState.THINKING])
        for a, b in zip(self.model._rotation, twin._rotation):
            self.assertAlmostEqual(a, b, places=6)

    def test_wave_points_form_closed_ring(self):
        prims = self.model.frame(STATE_PARAMS[CoreState.IDLE])
        wave = [p for p in prims if p[0] == "wave"][0]
        points = wave[1]
        self.assertEqual(len(points) % 2, 0)
        self.assertAlmostEqual(points[0], points[-2], places=5)
        self.assertAlmostEqual(points[1], points[-1], places=5)

    def test_level_increases_wave_amplitude(self):
        quiet = self.model.frame(STATE_PARAMS[CoreState.SPEAKING],
                                 wave_amp=0.01)
        loud = self.model.frame(STATE_PARAMS[CoreState.SPEAKING],
                                wave_amp=0.08)
        def spread(prims):
            pts = [p for p in prims if p[0] == "wave"][0][1]
            radii = [math.hypot(pts[i] - 140, pts[i + 1] - 140)
                     for i in range(0, len(pts), 2)]
            return max(radii) - min(radii)
        self.assertGreater(spread(loud), spread(quiet))


class FakeCanvas:
    """Recording duck-typed stand-in for tk.Canvas (headless)."""

    def __init__(self):
        self.items = []
        self.deleted_tags = []
        self.after_calls = []
        self._after_seq = 0

    def after(self, delay, callback):
        self._after_seq += 1
        self.after_calls.append((delay, callback))
        return f"after#{self._after_seq}"

    def after_cancel(self, after_id):
        pass

    def winfo_viewable(self):
        return True

    def winfo_toplevel(self):
        class Top:
            @staticmethod
            def state():
                return "normal"
        return Top()

    def delete(self, tag):
        self.deleted_tags.append(tag)

    def create_oval(self, *args, **kwargs):
        self.items.append(("oval", args, kwargs))

    def create_arc(self, *args, **kwargs):
        self.items.append(("arc", args, kwargs))

    def create_line(self, *args, **kwargs):
        self.items.append(("line", args, kwargs))

    def configure(self, **kwargs):
        pass


class TestWidgetWithFakeCanvas(unittest.TestCase):
    def test_tick_draws_only_own_tag(self):
        canvas = FakeCanvas()
        widget = FloatingCoreWidget(canvas=canvas, size=120)
        widget.start()
        self.assertTrue(canvas.after_calls)
        delay, callback = canvas.after_calls[-1]
        callback()  # run one tick manually
        self.assertTrue(canvas.items)
        self.assertIn(FloatingCoreWidget.TAG, canvas.deleted_tags)
        for _kind, _args, kwargs in canvas.items:
            self.assertEqual(kwargs.get("tags"), FloatingCoreWidget.TAG)
        widget.stop()

    def test_level_provider_is_polled(self):
        canvas = FakeCanvas()
        widget = FloatingCoreWidget(canvas=canvas, size=120)
        widget.set_level_provider(lambda: 0.9)
        widget.set_state(CoreState.LISTENING)
        widget.start()
        _delay, callback = canvas.after_calls[-1]
        callback()
        self.assertGreater(widget.machine.level, 0.0)
        widget.stop()

    def test_broken_level_provider_does_not_kill_tick(self):
        canvas = FakeCanvas()
        widget = FloatingCoreWidget(canvas=canvas, size=120)
        def boom():
            raise RuntimeError("no level source")
        widget.set_level_provider(boom)
        widget.start()
        _delay, callback = canvas.after_calls[-1]
        callback()  # must not raise
        self.assertTrue(canvas.items)
        widget.stop()


if __name__ == "__main__":
    unittest.main()
