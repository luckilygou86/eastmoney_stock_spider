"""Deterministic long stream clocks plus real, local blocked socket checks.

All inference is simulated. Never read user tokens or make remote AI requests.
"""
import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import ai_analysis as a
from test_ai_analysis import KEY, MESSAGES, Response, client, response_payload, sse


class ClockResponse(Response):
    def __init__(self, clock, chunks, delays, mime="text/event-stream"):
        super().__init__(b"", mime, {"x-request-id": "offline-timer-request"})
        self.clock, self.chunks, self.delays = clock, list(chunks), list(delays)

    def read1(self, size):
        self.clock[0] += self.delays.pop(0) if self.delays else 0
        if not self.chunks:
            return b""
        part = self.chunks.pop(0)
        if len(part) > size:
            self.chunks.insert(0, part[size:])
            self.delays.insert(0, 0)
        return part[:size]


@contextmanager
def stalled_server(initial=b""):
    stop = threading.Event()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            if initial:
                self.wfile.write(initial)
                self.wfile.flush()
            stop.wait(3)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


class StreamDeadlineTests(unittest.TestCase):
    def native(self):
        result = client(auth_mode="chatgpt", base_url=a.OFFICIAL_BASE_URL)
        result._opener = MagicMock()
        return result

    def test_active_stream_over_sixty_seconds_completes_and_progress_is_safe(self):
        clock = [0.0]
        wire = [sse({"type": "response.created", "response": {"id": "timer"}}).encode(),
                sse({"type": "response.reasoning.delta", "delta": "hidden-reasoning-fixture"}).encode(),
                sse({"type": "response.output_text.delta", "delta": "完整"}).encode(),
                sse({"type": "response.completed", "response": response_payload("完整")}).encode()]
        response = ClockResponse(clock, wire, [30, 75, 75, 75])
        instance = self.native()
        instance._opener.open.return_value = response
        progress = []
        with patch.object(a.time, "monotonic", side_effect=lambda: clock[0]):
            result = instance.complete(MESSAGES, progress_callback=progress.append)
        self.assertEqual(result["text"], "完整")
        self.assertGreater(clock[0], 60)
        self.assertEqual(instance._opener.open.call_count, 1)
        self.assertEqual(progress[-1]["phase"], "completed")
        self.assertNotIn("hidden-reasoning-fixture", json.dumps(progress))
        self.assertNotIn(KEY, json.dumps(progress))
        self.assertEqual(progress[-1]["bytes_received"], sum(map(len, wire)))
        self.assertEqual(progress[-1]["text_chars"], 2)
        self.assertTrue(response.closed)

    def test_idle_before_first_byte_has_precise_failure(self):
        clock = [0.0]
        response = ClockResponse(clock, [], [121])
        instance = self.native()
        instance._opener.open.return_value = response
        with patch.object(a.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(a.AiError, "连续 120 秒") as caught:
                instance.complete(MESSAGES)
        self.assertIn("offline-timer-request", str(caught.exception))
        self.assertIn("接收 0 字节", str(caught.exception))
        self.assertEqual(instance._opener.open.call_count, 1)
        self.assertTrue(response.closed)

    def test_heartbeats_reset_idle_but_do_not_disable_total_limit(self):
        clock = [0.0]
        response = ClockResponse(clock, [b": heartbeat\n\n"] * 10, [100] * 10)
        instance = self.native()
        instance._opener.open.return_value = response
        with patch.object(a.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(a.AiError, "总等待已达到 600 秒"):
                instance.complete(MESSAGES)
        self.assertEqual(instance._opener.open.call_count, 1)

    def test_long_line_chunks_count_as_network_activity_not_line_end(self):
        clock = [0.0]
        wire = sse({"type": "response.output_text.delta", "delta": "x" * 300},
                   {"type": "response.completed", "response": response_payload("x" * 300)}).encode()
        chunks = [wire[i:i + 200] for i in range(0, len(wire), 200)]
        response = ClockResponse(clock, chunks, [40] * len(chunks))
        instance = self.native()
        instance._opener.open.return_value = response
        with patch.object(a.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(instance.complete(MESSAGES)["text"], "x" * 300)
        self.assertGreater(clock[0], 60)

    def test_cancelled_progress_callback_never_returns_complete_result(self):
        instance = self.native()
        event = threading.Event()
        instance._opener.open.return_value = Response(sse({"type": "response.completed", "response": response_payload()}), "text/event-stream")
        def progress(value):
            if value["phase"] == "connected": event.set()
        with self.assertRaises(a.AiCancelled):
            instance.complete(MESSAGES, cancel_event=event, progress_callback=progress)
        self.assertEqual(instance._opener.open.call_count, 1)

    def test_ui_callback_exception_cannot_turn_valid_answer_into_failure(self):
        instance = self.native()
        instance._opener.open.return_value = Response(sse({"type": "response.completed", "response": response_payload()}), "text/event-stream")
        self.assertTrue(instance.complete(MESSAGES, progress_callback=MagicMock(side_effect=ValueError("private-fixture")))["text"])

    def test_real_socket_watchdog_interrupts_stalled_read_and_joins(self):
        # Lower only the internal test reader clock after headers; production
        # validation still enforces 5s minimum idle, tested in ConfigTests.
        original = a._StreamResponse.__init__
        def fast_init(reader, *args, **kwargs):
            original(reader, *args, **kwargs)
            reader._idle_limit = 0.3
            reader._total_limit = 2
        with stalled_server(b": hello\n\n") as (base, calls):
            instance = client(base_url=base, protocol="responses")
            started = time.monotonic()
            with patch.object(a._StreamResponse, "__init__", fast_init), patch.object(a._StreamResponse, "use_stream_limits"), self.assertRaisesRegex(a.AiError, "连续 0.3 秒"):
                instance.complete(MESSAGES)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(calls, ["/v1/responses"])
        self.assertFalse(any(t.name == "stock-ai-stream-watch" and t.is_alive() for t in threading.enumerate()))

    def test_real_socket_cancel_stops_read_without_waiting_for_idle(self):
        event, connected = threading.Event(), threading.Event()
        failures = []
        with stalled_server() as (base, calls):
            instance = client(base_url=base, protocol="responses")
            def work():
                try:
                    instance.complete(MESSAGES, cancel_event=event,
                                      progress_callback=lambda p: connected.set() if p["phase"] == "connected" else None)
                except Exception as exc:
                    failures.append(exc)
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(connected.wait(1))
            event.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(failures[0], a.AiCancelled)
            self.assertEqual(calls, ["/v1/responses"])
