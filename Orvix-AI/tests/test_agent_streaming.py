"""Streaming-response tests for the desktop agent (Premium Phase 8b).

Fake transports only — no real network. The SSE fakes are small
hand-written response objects (status/headers/iter_lines/close); the
single-shot fakes reuse the Mock style of
test_agent_provider_response.py. The bridge tests follow
test_orvix_webview_home.py's FakeApp style, and the Tk/app.js wiring
is pinned statically like test_chat_polish.py.
"""
from __future__ import annotations

import ast
import json
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.agent import OrvixAgent
from orvix.webview_home import WebViewHome

APP_DIR = Path(__file__).resolve().parents[1]
APP_SOURCE = (APP_DIR / "app.py").read_text(encoding="utf-8")
APP_JS = (APP_DIR / "web_home" / "app.js").read_text(encoding="utf-8")


def sse_chunk(delta, finish_reason=None):
    return "data: " + json.dumps(
        {"choices": [{"delta": delta, "finish_reason": finish_reason}]})


def content_chunk(text):
    return sse_chunk({"role": "assistant", "content": text})


class FakeStreamResponse:
    """Minimal requests.Response stand-in carrying SSE lines."""

    def __init__(self, lines, status_code=200, fail_at=None, on_line=None):
        self.status_code = status_code
        self.headers = {"Content-Type": "text/event-stream"}
        self._lines = list(lines)
        self._fail_at = fail_at
        self._on_line = on_line
        self.closed = False
        self.lines_yielded = 0

    def iter_lines(self, decode_unicode=True):
        for index, line in enumerate(self._lines):
            if self._fail_at is not None and index == self._fail_at:
                raise requests.ConnectionError("simulated mid-stream failure")
            if self._on_line is not None:
                self._on_line(index)
            self.lines_yielded += 1
            yield line

    def close(self):
        self.closed = True

    def json(self):
        raise ValueError("SSE body is not JSON")


def make_agent():
    return OrvixAgent(
        {"api_base_url": "http://localhost:11434/v1", "model": "test-model"},
        approval=lambda *_: True,
    )


def run_agent(agent, post_side_effect, **respond_kwargs):
    with patch("orvix.agent.automation.direct_command", return_value=None), patch(
        "orvix.agent.get_api_key", return_value=""
    ), patch("orvix.agent.requests.post", side_effect=post_side_effect) as post:
        result = agent.respond("Say hello.", **respond_kwargs)
    return result, post


class StreamingAccumulationTests(unittest.TestCase):
    def test_sse_deltas_accumulate_and_callback_is_incremental(self):
        stream = FakeStreamResponse([
            content_chunk("Hello "),
            content_chunk("streamed "),
            content_chunk("world"),
            "data: [DONE]",
        ])
        deltas: list[str] = []
        agent = make_agent()
        result, post = run_agent(agent, [stream], on_delta=deltas.append)
        self.assertEqual(result, "Hello streamed world")
        self.assertEqual(agent.last_outcome, "completed")
        # The callback receives the cumulative text, in delta order.
        self.assertEqual(
            deltas, ["Hello ", "Hello streamed ", "Hello streamed world"])
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertTrue(kwargs["stream"])
        self.assertTrue(kwargs["json"]["stream"])
        self.assertTrue(stream.closed)

    def test_done_terminates_and_later_lines_are_ignored(self):
        stream = FakeStreamResponse([
            content_chunk("Stop here"),
            "data: [DONE]",
            content_chunk("NEVER SEEN"),
        ])
        agent = make_agent()
        result, _post = run_agent(agent, [stream])
        self.assertEqual(result, "Stop here")
        self.assertEqual(stream.lines_yielded, 2)

    def test_malformed_sse_lines_are_tolerated(self):
        stream = FakeStreamResponse([
            "",
            ": a comment line",
            "event: message",
            "this is not sse at all",
            "data: {not valid json",
            'data: {"choices": []}',
            'data: {"unexpected": true}',
            content_chunk("Survived"),
            "data: [DONE]",
        ])
        deltas: list[str] = []
        agent = make_agent()
        result, _post = run_agent(agent, [stream], on_delta=deltas.append)
        self.assertEqual(result, "Survived")
        self.assertEqual(deltas, ["Survived"])

    def test_non_sse_json_body_on_stream_request_is_handled(self):
        response = Mock(status_code=200)
        response.headers = {}
        response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Plain JSON answer"}}]
        }
        agent = make_agent()
        result, post = run_agent(agent, [response])
        self.assertEqual(result, "Plain JSON answer")
        # Handled inline: no second (fallback) request was needed.
        post.assert_called_once()


class StreamingToolCallTests(unittest.TestCase):
    def test_tool_call_stream_is_never_surfaced_and_reaches_tool_flow(self):
        tool_stream = FakeStreamResponse([
            sse_chunk({"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "get_system_", "arguments": ""}}]}),
            sse_chunk({"tool_calls": [{
                "index": 0,
                "function": {"name": "info", "arguments": "{}"}}]}),
            "data: [DONE]",
        ])
        final_stream = FakeStreamResponse([
            content_chunk("Tool answer"),
            "data: [DONE]",
        ])
        deltas: list[str] = []
        agent = make_agent()
        with patch.object(agent, "_execute_tool", return_value="ok") as execute:
            result, post = run_agent(
                agent, [tool_stream, final_stream], on_delta=deltas.append)
        self.assertEqual(result, "Tool answer")
        execute.assert_called_once_with("get_system_info", {})
        # Only the final turn's plain content was streamed; no tool
        # name/argument fragment ever reached the delta callback.
        self.assertEqual(deltas, ["Tool answer"])
        self.assertNotIn("get_system", "".join(deltas))
        self.assertEqual(post.call_count, 2)


class StreamingFallbackTests(unittest.TestCase):
    def test_streaming_rejected_falls_back_to_single_shot_exactly_once(self):
        rejected = Mock(status_code=400)
        rejected.headers = {}
        fallback = Mock(status_code=200)
        fallback.headers = {}
        fallback.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Fallback answer"}}]
        }
        deltas: list[str] = []
        agent = make_agent()
        result, post = run_agent(
            agent, [rejected, fallback], on_delta=deltas.append)
        self.assertEqual(result, "Fallback answer")
        self.assertEqual(agent.last_outcome, "completed")
        # One streaming attempt + one single-shot request, no more.
        self.assertEqual(post.call_count, 2)
        stream_kwargs = post.call_args_list[0].kwargs
        self.assertTrue(stream_kwargs["json"]["stream"])
        fallback_kwargs = post.call_args_list[1].kwargs
        self.assertNotIn("stream", fallback_kwargs.get("json", {}))
        self.assertFalse(fallback_kwargs.get("stream", False))
        self.assertEqual(deltas, [])

    def test_empty_stream_falls_back_to_single_shot(self):
        empty_stream = FakeStreamResponse([])
        fallback = Mock(status_code=200)
        fallback.headers = {}
        fallback.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "From fallback"}}]
        }
        agent = make_agent()
        result, post = run_agent(agent, [empty_stream, fallback])
        self.assertEqual(result, "From fallback")
        self.assertEqual(post.call_count, 2)

    def test_mid_stream_failure_after_deltas_never_restarts(self):
        stream = FakeStreamResponse(
            [content_chunk("Partial "), content_chunk("text"),
             content_chunk("unreachable")],
            fail_at=2,
        )
        deltas: list[str] = []
        agent = make_agent()
        result, post = run_agent(agent, [stream], on_delta=deltas.append)
        # No restart: exactly one request was made.
        post.assert_called_once()
        self.assertEqual(deltas, ["Partial ", "Partial text"])
        # The partial is kept and honestly marked interrupted.
        self.assertIn("Partial text", result)
        self.assertIn("interrupted", result)
        self.assertIn("incomplete", result)
        self.assertNotIn("unreachable", result)
        self.assertEqual(agent.last_outcome, "partial")
        self.assertTrue(stream.closed)


class StreamingCancellationTests(unittest.TestCase):
    def test_cancel_mid_stream_stops_reading_and_delivery(self):
        cancel_event = threading.Event()

        def set_cancel_on_second_line(index):
            if index == 1:
                cancel_event.set()

        stream = FakeStreamResponse(
            [content_chunk("First "), content_chunk("Second"),
             content_chunk("Third")],
            on_line=set_cancel_on_second_line,
        )
        deltas: list[str] = []
        agent = make_agent()
        result, post = run_agent(
            agent, [stream], cancel_event=cancel_event, on_delta=deltas.append)
        post.assert_called_once()
        # Reading stopped at the cancelled line: the third chunk was
        # never pulled from the transport and never delivered.
        self.assertEqual(stream.lines_yielded, 2)
        self.assertEqual(deltas, ["First "])
        self.assertTrue(stream.closed)
        # The partial is kept and marked cancelled.
        self.assertIn("First", result)
        self.assertIn("বাতিল", result)
        self.assertEqual(agent.last_outcome, "cancelled")


class FakeBridgeApp:
    def __init__(self):
        self.settings = {}
        self.characters = {}
        self.events = queue.Queue()


class StreamBridgeBufferTests(unittest.TestCase):
    def test_partial_pushes_update_one_buffer_entry_and_final_completes(self):
        home = WebViewHome(FakeBridgeApp())
        first = home.push_stream_message("ORVIX", "Hel", "assistant")
        second = home.push_stream_message("ORVIX", "Hello", "assistant")
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["final"])
        messages = home.drain_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Hello")
        self.assertEqual(messages[0]["id"], first["id"])
        self.assertFalse(messages[0]["final"])
        self.assertIn("time", messages[0])
        # A later update re-queues the SAME single entry (the push
        # loop had drained it), still one entry, and final completes.
        home.push_stream_message("ORVIX", "Hello world", "assistant")
        home.push_stream_message("ORVIX", "Hello world", "assistant", final=True)
        messages = home.drain_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Hello world")
        self.assertTrue(messages[0]["final"])
        # The next stream starts a fresh entry with a new id.
        third = home.push_stream_message("ORVIX", "Next", "assistant")
        self.assertNotEqual(third["id"], first["id"])

    def test_clear_messages_resets_the_stream_entry(self):
        home = WebViewHome(FakeBridgeApp())
        first = home.push_stream_message("ORVIX", "Hel", "assistant")
        home.clear_messages()
        self.assertEqual(home.drain_messages(), [])
        second = home.push_stream_message("ORVIX", "Fresh", "assistant")
        self.assertNotEqual(second["id"], first["id"])


class StreamingWiringStaticTests(unittest.TestCase):
    def test_app_passes_on_delta_and_completes_streamed_message(self):
        self.assertIn("on_delta=lambda streamed_text: self.root.after(", APP_SOURCE)
        self.assertIn("_update_streamed_message", APP_SOURCE)
        self.assertIn("_complete_streamed_message", APP_SOURCE)
        tree = ast.parse(APP_SOURCE)
        finish = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_finish_response":
                finish = ast.get_source_segment(APP_SOURCE, node)
        self.assertIsNotNone(finish)
        self.assertIn("_complete_streamed_message(result)", finish)
        self.assertIn('_write_message("ORVIX", result, "assistant")', finish)

    def test_web_app_updates_streamed_node_in_place(self):
        self.assertIn("upsertStreamMessage", APP_JS)
        self.assertIn("streamNodes", APP_JS)
        self.assertIn("dataset.streamId", APP_JS)
        self.assertIn("m.final", APP_JS)
        # Partials never join the notifications collection.
        self.assertIn("m.final !== false", APP_JS)
        # Copy reads the node's current (final) text at click time.
        self.assertIn("copyMessageText(p.textContent)", APP_JS)


if __name__ == "__main__":
    unittest.main()
