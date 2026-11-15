"""Headless tests for the small overlay orb + HUD home panel data.

No Tk root is created: overlay geometry/interaction math and the HUD
panel assembly are pure modules, and the app wiring is guarded by
source-level checks (same style as test_floating_hud_initialization).
"""
import unittest
from pathlib import Path

from orvix.overlay import (
    CLICK_TRAVEL_PX, OVERLAY_HEIGHT, OVERLAY_WIDTH, PALETTE_HEIGHT,
    clamp_overlay_position, is_click, normalize_overlay_command,
    overlay_window_height, palette_open_geometry, submit_overlay_command,
)
from orvix.hud_data import build_hud_panels

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def fake_snapshot(**overrides):
    snap = {
        "agents": [{"name": f"Agent {i}"} for i in range(16)],
        "models": [
            {"provider": "openai", "configured": True, "healthy": None},
            {"provider": "anthropic", "configured": True, "healthy": None},
            {"provider": "gemini", "configured": False, "healthy": None},
        ],
        "tasks": {"pending": 1, "running": 0, "completed": 4, "failed": 1},
        "recent_tasks": [{"id": "abcdef12", "title": "bg job", "status": "completed", "attempts": 1}],
        "budget": {"limit_usd": 5.0, "spent_usd": 1.25, "calls": 7, "remaining_usd": 3.75},
        "audit_tail": [
            {"at": 1.0, "event": "task_completed", "actor": "chief"},
            {"at": 2.0, "event": "tool_call", "actor": "coding"},
            {"at": 3.0, "event": "confirmation_required", "actor": "security"},
            {"at": 4.0, "event": "task_started", "actor": "chief"},
        ],
    }
    snap.update(overrides)
    return snap


def fake_app_tasks():
    return [
        {"id": "1", "command": "YouTube kholo", "status": "running"},
        {"id": "2", "command": "Downloads kholo", "status": "completed"},
        {"id": "3", "command": "a very long command that should be clipped in the panel", "status": "failed"},
        {"id": "4", "command": "Notepad kholo", "status": "queued"},
    ]


class TestOverlayGeometry(unittest.TestCase):
    def test_clamp_inside_screen_unchanged(self):
        self.assertEqual(clamp_overlay_position(50, 60, 100, 100, 1920, 1080), (50, 60))

    def test_clamp_overflow_and_negative(self):
        self.assertEqual(clamp_overlay_position(1900, 1050, 100, 100, 1920, 1080), (1820, 980))
        self.assertEqual(clamp_overlay_position(-5, -9, 100, 100, 1920, 1080), (0, 0))

    def test_window_height_palette_states(self):
        self.assertEqual(overlay_window_height(False), OVERLAY_HEIGHT)
        self.assertEqual(overlay_window_height(True), OVERLAY_HEIGHT + PALETTE_HEIGHT)

    def test_palette_geometry_grows_down_when_room(self):
        x, y, h = palette_open_geometry(300, 200, palette_open=True,
                                        screen_width=1920, screen_height=1080)
        self.assertEqual((x, y), (300, 200))
        self.assertEqual(h, OVERLAY_HEIGHT + PALETTE_HEIGHT)

    def test_palette_geometry_shifts_up_near_bottom(self):
        x, y, h = palette_open_geometry(300, 1000, palette_open=True,
                                        screen_width=1920, screen_height=1080)
        self.assertEqual(y + h, 1080)  # fully on screen
        self.assertLess(y, 1000)

    def test_palette_geometry_closed_keeps_position(self):
        x, y, h = palette_open_geometry(10, 20, palette_open=False,
                                        screen_width=800, screen_height=600)
        self.assertEqual((x, y, h), (10, 20, OVERLAY_HEIGHT))

    def test_overlay_size_within_spec_range(self):
        # Owner asked for a small orb, roughly 84-110 px wide overall.
        self.assertGreaterEqual(OVERLAY_WIDTH, 84)
        self.assertLessEqual(OVERLAY_WIDTH, 110)


class TestClickDiscrimination(unittest.TestCase):
    def test_stationary_release_is_click(self):
        self.assertTrue(is_click((100, 100), (100, 100)))
        self.assertTrue(is_click((100, 100), (100 + CLICK_TRAVEL_PX, 100)))

    def test_moved_release_is_drag(self):
        self.assertFalse(is_click((100, 100), (100 + CLICK_TRAVEL_PX + 1, 100)))
        self.assertFalse(is_click((100, 100), (40, 300)))

    def test_bad_input_is_not_click(self):
        self.assertFalse(is_click((100,), (100, 100)))


class TestCommandRouting(unittest.TestCase):
    def test_normalize_collapses_whitespace(self):
        self.assertEqual(normalize_overlay_command("  open   YouTube \n"), "open YouTube")

    def test_normalize_empty_is_none(self):
        self.assertIsNone(normalize_overlay_command("   "))
        self.assertIsNone(normalize_overlay_command(""))
        self.assertIsNone(normalize_overlay_command(None))

    def test_submit_routes_through_send(self):
        sent = []
        self.assertTrue(submit_overlay_command("  Notepad kholo ", sent.append))
        self.assertEqual(sent, ["Notepad kholo"])

    def test_submit_empty_does_not_route(self):
        sent = []
        self.assertFalse(submit_overlay_command("   ", sent.append))
        self.assertEqual(sent, [])


class TestHudPanelData(unittest.TestCase):
    def test_agents_and_models_lines(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks())
        self.assertEqual(data.agents, "AGENTS  16 REGISTERED")
        self.assertEqual(data.models, "PROVIDERS  2/3 CONFIGURED")

    def test_health_not_checked_is_honest(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks())
        self.assertIn("NOT CHECKED", data.health)

    def test_health_counts_when_known(self):
        snap = fake_snapshot(models=[
            {"provider": "a", "configured": True, "healthy": True},
            {"provider": "b", "configured": True, "healthy": False},
            {"provider": "c", "configured": False, "healthy": None},
        ])
        data = build_hud_panels(snap, None)
        self.assertEqual(data.health, "PROVIDER HEALTH  1/2 UP")

    def test_queue_line_prefers_app_tasks(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks())
        self.assertEqual(data.queue, "TASKS  1 QUEUED · 1 RUNNING · 1 DONE · 1 FAILED")

    def test_queue_line_falls_back_to_snapshot_stats(self):
        data = build_hud_panels(fake_snapshot(), None)
        self.assertEqual(data.queue, "TASKS  1 QUEUED · 0 RUNNING · 4 DONE · 1 FAILED")

    def test_budget_line(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks())
        self.assertEqual(data.budget, "BUDGET  $1.25 SPENT / $5.00 CAP · 7 CALLS")

    def test_memory_line(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks(),
                                memory_count=3, lesson_count=9)
        self.assertEqual(data.memory, "MEMORY  3 ITEMS · 9 LESSONS")
        self.assertEqual(build_hud_panels(fake_snapshot(), []).memory, "MEMORY  —")

    def test_recent_tasks_marks_and_clipping(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks())
        self.assertEqual(len(data.recent_tasks), 3)
        self.assertTrue(data.recent_tasks[0].startswith("● "))   # running
        self.assertTrue(data.recent_tasks[1].startswith("✓ "))   # completed
        self.assertTrue(data.recent_tasks[2].startswith("✗ "))   # failed
        self.assertTrue(data.recent_tasks[2].endswith("…"))      # clipped

    def test_recent_tasks_snapshot_fallback_and_empty(self):
        data = build_hud_panels(fake_snapshot(), None)
        self.assertEqual(data.recent_tasks, ("✓ bg job",))
        empty = build_hud_panels(fake_snapshot(recent_tasks=[]), [])
        self.assertEqual(empty.recent_tasks, ("NO TASKS RECORDED YET",))

    def test_log_lines_newest_first_max_three(self):
        data = build_hud_panels(fake_snapshot(), fake_app_tasks())
        self.assertEqual(len(data.log_lines), 3)
        self.assertEqual(data.log_lines[0], "task_started · chief")
        self.assertEqual(data.log_lines[-1], "tool_call · coding")

    def test_log_lines_empty_is_explicit(self):
        data = build_hud_panels(fake_snapshot(audit_tail=[]), [])
        self.assertEqual(data.log_lines, ("NO AUDIT EVENTS RECORDED",))

    def test_missing_snapshot_is_all_dashes(self):
        data = build_hud_panels(None, None)
        self.assertIn("—", data.agents)
        self.assertIn("—", data.models)
        self.assertIn("—", data.queue)
        self.assertIn("—", data.budget)
        self.assertEqual(data.recent_tasks, ("NO TASKS RECORDED YET",))


class TestAppWiring(unittest.TestCase):
    """Source-level guards: the overlay palette must route through the
    same pipeline as the main composer, and the HUD panels must be fed
    by the pure assembly module."""

    @classmethod
    def setUpClass(cls):
        cls.source = APP_PATH.read_text(encoding="utf-8")

    def test_palette_submit_uses_main_pipeline(self):
        self.assertIn("submit_overlay_command(entry.get(), self._send_text)", self.source)

    def test_palette_toggle_and_quit_exist(self):
        self.assertIn("def _toggle_overlay_palette", self.source)
        self.assertIn("def _quit_floating_overlay", self.source)
        self.assertIn('label="Quit Overlay"', self.source)
        self.assertIn('label="Command Palette"', self.source)

    def test_hud_panels_use_pure_assembly(self):
        self.assertIn("build_hud_panels(snapshot, app_tasks", self.source)
        self.assertIn("def _refresh_hud_panels", self.source)

    def test_double_click_cancels_palette_toggle(self):
        self.assertIn("def _floating_double_click", self.source)
        self.assertIn("self._cancel_overlay_click()", self.source)


if __name__ == "__main__":
    unittest.main()
