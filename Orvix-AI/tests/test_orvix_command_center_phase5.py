"""Command Center Phase 5 — Memory Center page.

Coverage, all headless with fakes at the existing boundaries:

* The payload's ``memory`` block, built from a REAL MemoryStore in
  a temp dir (the store itself is untouched by this phase — its
  existing tests keep their expectations): entries grouped by the
  store's real categories, true per-category counts and total, the
  latest-100 cap honoured with the true total preserved, and the
  store's ``updated_at`` carried as ``updatedAt``.
* Store resolution: the app accessor (``_web_memory_store``), a
  plain ``memory_store`` attribute, and the shared-state objects
  slot — plus absent / broken stores yielding a null block, never
  an exception.
* Search as the codebase's established live-state pattern: the
  bridge's ``memorySearch`` records the filter and the next
  payload (``build_state_payload(..., memory_query=...)``) carries
  the store's own search results for it.
* Bridge writes ``memoryForget`` / ``memoryClear`` enqueue onto
  ``app.events`` with the right arguments and do nothing before
  the drain (the established bridge test pattern).
* The app-side handlers (real methods on a ``__new__`` desktop
  double) actually mutating the store — forget resolves the entry
  id to the key ``MemoryStore.forget`` is keyed by, persists
  across a fresh store instance, and clear empties the store.
* Static checks on the web Memory page: the nav destination, the
  category sections, the search box, per-entry delete buttons,
  the clear-all in-page confirm step gating the bridge call, and
  text-only (textContent) rendering of entry text.

No Tk root, no pywebview window, no live anything.
"""
import json
import queue
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.home_data import SystemStats
from orvix.memory import MemoryStore
from orvix.webview_home import (
    MAX_PAYLOAD_MEMORY_ENTRIES,
    OrvixWebHomeApi,
    build_state_payload,
)

APP_DIR = Path(__file__).resolve().parents[1]
WEB_HOME_DIR = APP_DIR / "web_home"
STATS = SystemStats(None, None, None, "—", "—", "—", None)


def drain(app):
    executed = 0
    while True:
        try:
            callback, args = app.events.get_nowait()
        except queue.Empty:
            return executed
        callback(*args)
        executed += 1


class FakeWebApp:
    """The established FakeApp pattern, plus a memory store slot
    and the two Phase 5 app-side handlers recording their calls."""

    def __init__(self, store=None):
        self.settings = {
            "model": "qwen2.5:3b",
            "api_base_url": "http://localhost:11434/v1",
        }
        self.characters = {
            "luna": {"name": "LUNA", "role": "Calm strategy companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "luna.jpg"},
            "ruby": {"name": "RUBY", "role": "Confident action companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "ruby.jpg"},
            "maya": {"name": "MAYA", "role": "Warm creative companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "maya.jpg"},
        }
        self.selected_character = "luna"
        self.core_mode = "READY"
        self.wake_enabled = False
        self._busy = False
        self.events = queue.Queue()
        self.calls = []
        if store is not None:
            self.memory_store = store

    def _forget_memory_from_web(self, entry_id):
        self.calls.append(("_forget_memory_from_web", entry_id))
        return True

    def _clear_memory_from_web(self):
        self.calls.append(("_clear_memory_from_web",))


def seeded_store(tmp, entries):
    """A real MemoryStore in ``tmp`` seeded with (key, value,
    category) triples; returns (store, [entry dicts as stored])."""
    store = MemoryStore(Path(tmp) / "memory.json")
    stored = [store.remember(key, value, category)
              for key, value, category in entries]
    return store, stored


# ---------------------------------------------------------------------------
# Payload block — shape, grouping, counts, cap
# ---------------------------------------------------------------------------

class MemoryPayloadTests(unittest.TestCase):
    def test_block_shape_grouping_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, stored = seeded_store(tmp, [
                ("language", "Bangla", "preference"),
                ("theme", "dark", "preference"),
                ("city", "Dhaka", "fact"),
                ("editor", "VS Code", "fact"),
                ("deploy", "small draft PRs", "strategy"),
                ("idea", "voice first", "note"),
            ])
            app = FakeWebApp(store)
            payload = build_state_payload(app, stats=STATS)
            json.dumps(payload)  # must stay JSON-serializable
            block = payload["memory"]
            self.assertIsNotNone(block)
            self.assertEqual(block["total"], 6)
            self.assertEqual(block["shown"], 6)
            self.assertFalse(block["truncated"])
            self.assertEqual(block["cap"], MAX_PAYLOAD_MEMORY_ENTRIES)
            self.assertEqual(block["query"], "")
            self.assertEqual(block["counts"], {
                "preference": 2, "fact": 2, "strategy": 1, "note": 1})
            # Grouped by the store's real categories, in order.
            self.assertEqual(list(block["byCategory"]),
                             ["preference", "fact", "strategy", "note"])
            prefs = block["byCategory"]["preference"]
            # Newest first within the category.
            self.assertEqual([e["key"] for e in prefs], ["theme", "language"])
            entry = prefs[1]
            self.assertEqual(entry["id"], stored[0]["id"])
            self.assertEqual(entry["key"], "language")
            self.assertEqual(entry["text"], "Bangla")
            self.assertEqual(entry["category"], "preference")
            # The store records updated_at (no separate created
            # timestamp exists) — it is carried through as-is.
            self.assertEqual(entry["updatedAt"], stored[0]["updated_at"])
            self.assertIsInstance(entry["updatedAt"], float)

    def test_empty_store_block_is_present_and_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = seeded_store(tmp, [])
            payload = build_state_payload(FakeWebApp(store), stats=STATS)
            block = payload["memory"]
            self.assertIsNotNone(block)
            self.assertEqual(block["total"], 0)
            self.assertEqual(block["shown"], 0)
            self.assertEqual(block["counts"], {
                "preference": 0, "fact": 0, "strategy": 0, "note": 0})
            self.assertEqual(block["byCategory"], {
                "preference": [], "fact": [], "strategy": [], "note": []})

    def test_cap_honoured_and_true_total_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            total = MAX_PAYLOAD_MEMORY_ENTRIES + 5
            for i in range(total):
                store.remember(f"k{i}", f"value {i}", "fact")
            payload = build_state_payload(FakeWebApp(store), stats=STATS)
            block = payload["memory"]
            # The lists are capped, the counts/total are NOT.
            self.assertEqual(block["total"], total)
            self.assertEqual(block["counts"]["fact"], total)
            self.assertEqual(block["shown"], MAX_PAYLOAD_MEMORY_ENTRIES)
            self.assertTrue(block["truncated"])
            facts = block["byCategory"]["fact"]
            self.assertEqual(len(facts), MAX_PAYLOAD_MEMORY_ENTRIES)
            # The cap keeps the LATEST entries, newest first.
            self.assertEqual(facts[0]["key"], f"k{total - 1}")
            self.assertEqual(facts[-1]["key"], "k5")

    def test_absent_store_yields_null_block(self):
        payload = build_state_payload(FakeWebApp(), stats=STATS)
        json.dumps(payload)
        self.assertIsNone(payload["memory"])

    def test_minimal_app_yields_null_block(self):
        class MinimalApp:
            settings = {}
            core_mode = "READY"
            wake_enabled = False

        payload = build_state_payload(MinimalApp(), stats=STATS)
        self.assertIsNone(payload["memory"])

    def test_broken_store_yields_null_block_never_raises(self):
        class BrokenStore:
            def list(self, category=None):
                raise RuntimeError("store unreadable")

            def search(self, query):
                raise RuntimeError("store unreadable")

        payload = build_state_payload(FakeWebApp(BrokenStore()), stats=STATS)
        self.assertIsNone(payload["memory"])
        payload = build_state_payload(
            FakeWebApp(BrokenStore()), stats=STATS, memory_query="x")
        self.assertIsNone(payload["memory"])

    def test_store_resolved_via_app_accessor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = seeded_store(tmp, [("city", "Dhaka", "fact")])
            app = FakeWebApp()
            app._web_memory_store = lambda: store
            payload = build_state_payload(app, stats=STATS)
            self.assertEqual(payload["memory"]["total"], 1)
            self.assertEqual(
                payload["memory"]["byCategory"]["fact"][0]["text"], "Dhaka")

    def test_store_resolved_via_shared_state_objects_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = seeded_store(tmp, [("city", "Dhaka", "fact")])
            app = FakeWebApp()
            app._hud_cc_objects = (None, None, None, store, None)
            payload = build_state_payload(app, stats=STATS)
            self.assertEqual(payload["memory"]["total"], 1)

    def test_broken_accessor_falls_through_to_attribute(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = seeded_store(tmp, [("city", "Dhaka", "fact")])

            def boom():
                raise RuntimeError("accessor unavailable")

            app = FakeWebApp(store)
            app._web_memory_store = boom
            payload = build_state_payload(app, stats=STATS)
            self.assertEqual(payload["memory"]["total"], 1)


# ---------------------------------------------------------------------------
# Search — filter requested via the bridge, reflected in the payload
# ---------------------------------------------------------------------------

class MemorySearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store, _ = seeded_store(self._tmp.name, [
            ("language", "Bangla", "preference"),
            ("city", "Dhaka", "fact"),
            ("editor", "VS Code", "note"),
        ])
        self.app = FakeWebApp(self.store)
        self.api = OrvixWebHomeApi(self.app)

    def test_payload_filter_uses_store_search(self):
        payload = build_state_payload(
            self.app, stats=STATS, memory_query="bangla")
        block = payload["memory"]
        self.assertEqual(block["query"], "bangla")
        self.assertEqual(block["total"], 1)
        self.assertEqual(block["counts"], {
            "preference": 1, "fact": 0, "strategy": 0, "note": 0})
        self.assertEqual(
            block["byCategory"]["preference"][0]["key"], "language")
        self.assertEqual(block["byCategory"]["fact"], [])

    def test_payload_filter_matches_key_and_value(self):
        # "dhaka" matches the VALUE of the city entry; "editor"
        # matches the KEY of the note entry — the store's own
        # search semantics, unmodified.
        by_value = build_state_payload(
            self.app, stats=STATS, memory_query="dhaka")["memory"]
        self.assertEqual(by_value["total"], 1)
        self.assertEqual(by_value["byCategory"]["fact"][0]["key"], "city")
        by_key = build_state_payload(
            self.app, stats=STATS, memory_query="editor")["memory"]
        self.assertEqual(by_key["total"], 1)
        self.assertEqual(by_key["byCategory"]["note"][0]["key"], "editor")

    def test_payload_filter_no_matches_is_honest_empty(self):
        block = build_state_payload(
            self.app, stats=STATS, memory_query="zzzz")["memory"]
        self.assertEqual(block["total"], 0)
        self.assertEqual(block["query"], "zzzz")
        self.assertEqual(block["byCategory"]["fact"], [])

    def test_bridge_search_records_filter_for_next_payload(self):
        self.assertEqual(self.api.memorySearch("  Dhaka "),
                         {"ok": True, "query": "Dhaka"})
        self.assertEqual(self.api._memory_query, "Dhaka")
        # The filter the push loop would pass on the next push:
        block = build_state_payload(
            self.app, stats=STATS,
            memory_query=self.api._memory_query)["memory"]
        self.assertEqual(block["total"], 1)
        # Searching never enqueues an app write and never mutates
        # the store.
        self.assertTrue(self.app.events.empty())
        self.assertEqual(len(self.store.list()), 3)

    def test_bridge_search_empty_query_clears_filter(self):
        self.api.memorySearch("dhaka")
        self.assertEqual(self.api.memorySearch(""), {"ok": True, "query": ""})
        self.assertEqual(self.api._memory_query, "")
        self.assertEqual(self.api.memorySearch(None), {"ok": True, "query": ""})

    def test_bridge_search_works_without_any_app_capability(self):
        # The filter is bridge-local state (like the portrait
        # cache): even a bare app object accepts it, and the
        # payload for such an app stays null rather than raising.
        class MinimalApp:
            events = queue.Queue()

        api = OrvixWebHomeApi(MinimalApp())
        self.assertEqual(api.memorySearch("x"), {"ok": True, "query": "x"})


# ---------------------------------------------------------------------------
# Bridge writes — enqueue discipline
# ---------------------------------------------------------------------------

class MemoryBridgeWriteTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeWebApp()
        self.api = OrvixWebHomeApi(self.app)

    def test_forget_enqueues_with_entry_id(self):
        self.assertEqual(self.api.memoryForget("abc123"), {"ok": True})
        self.assertEqual(self.app.calls, [])  # nothing before the drain
        drain(self.app)
        self.assertEqual(self.app.calls,
                         [("_forget_memory_from_web", "abc123")])

    def test_forget_strips_identifier(self):
        self.assertEqual(self.api.memoryForget("  abc123 "), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls,
                         [("_forget_memory_from_web", "abc123")])

    def test_forget_missing_id_is_rejected(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                result = self.api.memoryForget(bad)
                self.assertFalse(result["ok"])
        self.assertTrue(self.app.events.empty())
        self.assertEqual(self.app.calls, [])

    def test_clear_enqueues_app_clear(self):
        self.assertEqual(self.api.memoryClear(), {"ok": True})
        self.assertEqual(self.app.calls, [])
        drain(self.app)
        self.assertEqual(self.app.calls, [("_clear_memory_from_web",)])

    def test_missing_app_handlers_report_not_ok(self):
        class MinimalApp:
            events = queue.Queue()

        api = OrvixWebHomeApi(MinimalApp())
        self.assertFalse(api.memoryForget("abc123")["ok"])
        self.assertFalse(api.memoryClear()["ok"])


# ---------------------------------------------------------------------------
# App-side handlers — real methods mutating a real store
# ---------------------------------------------------------------------------

def make_desktop(testcase):
    try:
        import app as app_module
    except Exception as exc:  # tkinter unavailable on this runner
        testcase.skipTest(f"app module unavailable: {exc}")
    desktop = app_module.OrvixDesktop.__new__(app_module.OrvixDesktop)
    return app_module, desktop


class AppMemoryHandlerTests(unittest.TestCase):
    def _desktop_with_store(self, tmp, entries=()):
        _app_module, desktop = make_desktop(self)
        store, stored = seeded_store(tmp, list(entries))
        # The cached shared-state objects: element 3 is the store.
        desktop._hud_cc_objects = (None, None, None, store, None)
        return desktop, store, stored

    def test_web_memory_store_is_the_shared_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop, store, _ = self._desktop_with_store(tmp)
            # Identity, not just equality: the bridge must expose
            # the ONE shared store, never a second instance.
            self.assertIs(desktop._web_memory_store(), store)

    def test_web_memory_store_none_when_unobtainable(self):
        _app_module, desktop = make_desktop(self)
        desktop._hud_cc_objects = (None, None, None, None, None)
        desktop._shared_orvix_state = lambda: (None, None, None, None, None)
        self.assertIsNone(desktop._web_memory_store())

    def test_forget_by_id_removes_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop, store, stored = self._desktop_with_store(tmp, [
                ("language", "Bangla", "preference"),
                ("city", "Dhaka", "fact"),
            ])
            self.assertTrue(
                desktop._forget_memory_from_web(stored[0]["id"]))
            self.assertEqual([e["key"] for e in store.list()], ["city"])
            # Persisted: a fresh store over the same file agrees.
            fresh = MemoryStore(Path(tmp) / "memory.json")
            self.assertEqual([e["key"] for e in fresh.list()], ["city"])
            self.assertIsNone(fresh.recall("language"))

    def test_forget_by_key_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop, store, _ = self._desktop_with_store(tmp, [
                ("language", "Bangla", "preference"),
            ])
            self.assertTrue(desktop._forget_memory_from_web("language"))
            self.assertEqual(store.list(), [])

    def test_forget_unknown_identifier_is_honest_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop, store, _ = self._desktop_with_store(tmp, [
                ("language", "Bangla", "preference"),
            ])
            self.assertFalse(desktop._forget_memory_from_web("no-such-id"))
            self.assertFalse(desktop._forget_memory_from_web(""))
            self.assertFalse(desktop._forget_memory_from_web(None))
            self.assertEqual(len(store.list()), 1)

    def test_forget_without_store_returns_false(self):
        _app_module, desktop = make_desktop(self)
        desktop._hud_cc_objects = (None, None, None, None, None)
        desktop._shared_orvix_state = lambda: (None, None, None, None, None)
        self.assertFalse(desktop._forget_memory_from_web("anything"))

    def test_clear_empties_store_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop, store, _ = self._desktop_with_store(tmp, [
                ("language", "Bangla", "preference"),
                ("city", "Dhaka", "fact"),
            ])
            desktop._clear_memory_from_web()
            self.assertEqual(store.list(), [])
            fresh = MemoryStore(Path(tmp) / "memory.json")
            self.assertEqual(fresh.list(), [])

    def test_clear_without_store_is_noop(self):
        _app_module, desktop = make_desktop(self)
        desktop._hud_cc_objects = (None, None, None, None, None)
        desktop._shared_orvix_state = lambda: (None, None, None, None, None)
        desktop._clear_memory_from_web()  # must not raise


# ---------------------------------------------------------------------------
# Static web checks for the Memory page
# ---------------------------------------------------------------------------

@unittest.skipIf(
    not WEB_HOME_DIR.is_dir()
    or any(not (WEB_HOME_DIR / name).is_file()
           for name in ("index.html", "app.js", "style.css")),
    "web_home/ not fully created yet (delivered separately)")
class StaticPhase5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")

    def test_memory_nav_destination_and_view_exist(self):
        self.assertIn('data-tab="memory"', self.index_html)
        self.assertIn('id="memoryView"', self.index_html)
        self.assertIn("Memory Center", self.index_html)
        self.assertIn('id="memoryBackHome"', self.index_html)
        self.assertIn('tab === "memory"', self.app_js)
        self.assertIn("memoryView", self.app_js)
        self.assertIn("renderMemory(lastState)", self.app_js)

    def test_memory_controls_exist(self):
        for element_id in ("memorySearchInput", "memorySearchButton",
                           "memorySearchClear", "memorySummary",
                           "memoryList", "memoryEmpty",
                           "memoryClearButton", "memoryClearConfirm",
                           "memoryClearConfirmButton", "memoryClearCancel"):
            self.assertIn(f'id="{element_id}"', self.index_html)
        self.assertIn("No memories saved yet", self.index_html)
        self.assertIn(".memory-entry", self.style_css)
        self.assertIn(".memory-category", self.style_css)
        self.assertIn(".memory-search-row", self.style_css)
        self.assertIn(".memory-clear-confirm", self.style_css)

    def test_renderer_reads_payload_block_and_categories(self):
        self.assertIn("renderMemory", self.app_js)
        self.assertIn("renderMemory(state)", self.app_js)
        self.assertIn("state.memory", self.app_js)
        self.assertIn("MEMORY_CATEGORIES", self.app_js)
        for label in ("Preferences", "Facts", "Strategies", "Notes"):
            self.assertIn(label, self.app_js)
        for key in ("preference", "fact", "strategy", "note"):
            self.assertIn(f'"{key}"', self.app_js)
        self.assertIn("byCategory", self.app_js)

    def test_search_is_a_bridge_filter_not_a_local_filter(self):
        self.assertIn('callBridge("memorySearch"', self.app_js)
        self.assertIn("runMemorySearch", self.app_js)
        self.assertIn("memorySearchInput", self.app_js)
        # The no-results state is rendered from the payload block.
        self.assertIn("No memories match", self.app_js)

    def test_entries_render_as_text_never_markup(self):
        body = self.app_js.split("function renderMemory(state)")[1].split(
            "// ---------------------------------------------------------------------------\n// Emergency zone")[0]
        self.assertIn("textContent = String(entry.key", body)
        self.assertIn("textContent = String(entry.text)", body)
        self.assertNotIn("innerHTML", body)

    def test_delete_buttons_carry_id_and_route_via_bridge(self):
        self.assertIn("memory-delete-button", self.app_js)
        self.assertIn(".memory-delete-button", self.style_css)
        self.assertIn("dataset.entryId", self.app_js)
        self.assertIn('callBridge("memoryForget"', self.app_js)

    def test_clear_all_requires_the_in_page_confirm_step(self):
        # The confirm box exists in the markup (hidden by default),
        # the clear button's handler only reveals it, and the bridge
        # memoryClear call lives in the confirm button's handler —
        # an accidental click on Clear-all fires nothing.
        self.assertIn('id="memoryClearConfirm"', self.index_html)
        self.assertIn("memory-clear-confirm hidden", self.index_html)
        clear_handler = self.app_js.split(
            '$("#memoryClearButton").addEventListener')[1].split("});")[0]
        self.assertNotIn('callBridge("memoryClear"', clear_handler)
        self.assertIn("memoryClearConfirm", clear_handler)
        confirm_handler = self.app_js.split(
            '$("#memoryClearConfirmButton").addEventListener')[1].split("});")[0]
        self.assertIn('callBridge("memoryClear"', confirm_handler)

    def test_standalone_presentation_is_honest(self):
        self.assertIn("Memory is unavailable", self.app_js)
        self.assertIn("renderMemory(null)", self.app_js)


class NoBannedWordPhase5Tests(unittest.TestCase):
    def test_phase5_files_have_no_banned_word(self):
        # Fragments are joined at runtime so this guard does not
        # itself contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("app.py", "orvix/webview_home.py",
                    "web_home/index.html", "web_home/app.js",
                    "web_home/style.css",
                    "tests/test_orvix_command_center_phase5.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
