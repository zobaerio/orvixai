"""Static/wiring tests for the Phase 5 Tk chat polish (app.py).

app.py builds a real Tk root on import-time construction, so — like
the other app.py test modules — these tests work at source/AST
level: they pin that ``_write_message`` stamps each message with the
shared HH:MM formatter, that the chat Text carries the right-click
copy menu, that Clear Conversation is wired in the composer with a
confirmation that never touches task history, and that the chat
view's growth is bounded by whole-message trimming.
"""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


def _source():
    return APP_SOURCE.read_text(encoding="utf-8")


def _desktop_class(tree):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "OrvixDesktop":
            return node
    raise AssertionError("OrvixDesktop class not found in app.py")


def _method(tree, name):
    for node in _desktop_class(tree).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found on OrvixDesktop")


class TimestampWiringTests(unittest.TestCase):
    def setUp(self):
        self.source = _source()
        self.tree = ast.parse(self.source)

    def test_app_imports_shared_message_time_formatter(self):
        self.assertIn(
            "from orvix.ui_formatters import", self.source)
        self.assertIn("format_message_time", self.source)

    def test_write_message_stamps_speaker_line_with_time(self):
        segment = ast.get_source_segment(self.source, _method(self.tree, "_write_message"))
        self.assertIn("format_message_time(datetime.now())", segment)
        # Speaker and stamp are inserted on the same header line.
        self.assertIn('f"{speaker} "', segment)
        self.assertIn("stamp", segment)

    def test_write_message_still_mirrors_to_web_home(self):
        segment = ast.get_source_segment(self.source, _method(self.tree, "_write_message"))
        self.assertIn("home.push_message(speaker, text, kind)", segment)


class CopyMenuWiringTests(unittest.TestCase):
    def setUp(self):
        self.source = _source()
        self.tree = ast.parse(self.source)

    def test_chat_text_has_right_click_context_menu(self):
        self.assertIn(
            'self.chat.bind("<Button-3>", self._show_chat_context_menu)',
            self.source)
        segment = ast.get_source_segment(
            self.source, _method(self.tree, "_show_chat_context_menu"))
        self.assertIn('label="Copy"', segment)
        self.assertIn('label="Copy Last Response"', segment)
        self.assertIn('label="Clear Conversation"', segment)

    def test_copy_helpers_use_selection_last_response_and_clipboard(self):
        selection = ast.get_source_segment(
            self.source, _method(self.tree, "_copy_chat_selection"))
        self.assertIn('"sel.first"', selection)
        self.assertIn('"sel.last"', selection)
        last = ast.get_source_segment(
            self.source, _method(self.tree, "_copy_last_response"))
        self.assertIn("_last_response_text", last)
        clip = ast.get_source_segment(
            self.source, _method(self.tree, "_copy_to_clipboard"))
        self.assertIn("clipboard_append", clip)

    def test_write_message_tracks_last_assistant_response(self):
        segment = ast.get_source_segment(self.source, _method(self.tree, "_write_message"))
        self.assertIn('kind == "assistant"', segment)
        self.assertIn("self._last_response_text = str(text)", segment)


class ClearConversationWiringTests(unittest.TestCase):
    def setUp(self):
        self.source = _source()
        self.tree = ast.parse(self.source)

    def test_composer_has_clear_button_wired_to_confirming_clear(self):
        self.assertIn("self.clear_chat_btn", self.source)
        self.assertIn("command=self._clear_conversation", self.source)

    def test_clear_asks_and_states_task_history_is_kept(self):
        segment = ast.get_source_segment(
            self.source, _method(self.tree, "_clear_conversation"))
        self.assertIn("messagebox.askyesno", segment)
        self.assertIn("Task history and settings are NOT deleted.", segment)
        self.assertIn("_clear_conversation_views()", segment)

    def test_web_bridge_clear_skips_second_prompt(self):
        segment = ast.get_source_segment(
            self.source, _method(self.tree, "_clear_conversation_from_web"))
        self.assertIn("_clear_conversation_views()", segment)
        self.assertNotIn("askyesno", segment)

    def test_clear_views_never_touches_task_store_or_settings(self):
        # The safety rule, pinned at source level: clearing the
        # conversation must not delete task records or settings.
        segment = ast.get_source_segment(
            self.source, _method(self.tree, "_clear_conversation_views"))
        self.assertNotIn("task_store", segment)
        self.assertNotIn("settings", segment)
        # It clears the Tk view and the bridge message buffer.
        self.assertIn('chat.delete("1.0", "end")', segment)
        self.assertIn("home.clear_messages()", segment)


class BoundedChatGrowthTests(unittest.TestCase):
    def setUp(self):
        self.source = _source()
        self.tree = ast.parse(self.source)

    def test_chat_bound_constant_and_trim_wiring(self):
        self.assertIn("MAX_CHAT_MESSAGES = 200", self.source)
        write = ast.get_source_segment(self.source, _method(self.tree, "_write_message"))
        self.assertIn("_trim_chat_history(text)", write)

    def test_trim_deletes_whole_oldest_messages_only(self):
        segment = ast.get_source_segment(
            self.source, _method(self.tree, "_trim_chat_history"))
        self.assertIn("len(counts) > MAX_CHAT_MESSAGES", segment)
        self.assertIn("counts.pop(0)", segment)
        # Deletion always starts at 1.0 and ends on a line boundary,
        # so a message is removed whole, never split mid-message.
        self.assertIn('self.chat.delete("1.0", f"{oldest_lines + 1}.0")', segment)


if __name__ == "__main__":
    unittest.main()
