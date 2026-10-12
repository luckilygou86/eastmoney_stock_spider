"""Offline/local-only AI protocol, cancellation, size and credential checks."""
import io
import json
import threading
import unittest
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import ai_analysis as a


KEY = "offline-memory-secret"
MESSAGES = [{"role": "user", "content": "分析已有走势，注明数据缺失。"}]


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, content_type="application/json", headers=None):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        super().__init__(body)
        self.headers = {"Content-Type": content_type, **(headers or {})}


def completion(text="已有走势向上；不代表未来上涨。", reason="stop"):
    return {"id": "chat-1", "model": "offline-model", "choices": [{"message": {"content": text}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}}


def response_payload(text="已有数据不足以确认行业走势。"):
    return {"id": "response-1", "model": "offline-model", "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 10, "output_tokens": 20}}


def sse(*events):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)


def client(**overrides):
    settings = {"base_url": "https://confirmed.example/v1", "model": "offline-model", "api_key": KEY}
    settings.update(overrides)
    return a.AiClient(a.AiConfig(**settings))


@contextmanager
def local_server(callback):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            self.handle_call()
        def do_POST(self):
            self.handle_call()
        def handle_call(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append({"method": self.command, "path": self.path,
                          "authorization": self.headers.get("Authorization"), "body": body})
            status, headers, output = callback(calls[-1])
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class ConfigTests(unittest.TestCase):
    def test_secret_not_in_config_repr(self):
        self.assertNotIn(KEY, repr(a.AiConfig(api_key=KEY)))

    def test_stream_deadlines_validate_independently(self):
        for key, value in (("stream_timeout", 0), ("stream_timeout", 1801), ("stream_timeout", True),
                           ("stream_timeout", float("inf")), ("idle_timeout", 4), ("idle_timeout", 301), ("idle_timeout", float("nan"))):
            with self.assertRaises(a.AiError):
                client(**{key: value})
        self.assertEqual(client(stream_timeout=900, idle_timeout=180).config.stream_timeout, 900)

    def test_root_gets_v1_once(self):
        for base in ("https://confirmed.example", "https://confirmed.example/", "https://confirmed.example/v1/", "https://confirmed.example/v1"):
            self.assertEqual(a.normalize_base_url(base), "https://confirmed.example/v1")

    def test_custom_versioned_prefix_preserved(self):
        self.assertEqual(a.normalize_base_url("https://confirmed.example/api/provider/custom/v1"), "https://confirmed.example/api/provider/custom/v1")

    def test_http_only_loopback(self):
        for base in ("http://localhost:8317", "http://127.0.0.1:8317", "http://[::1]:8317"):
            self.assertTrue(a.normalize_base_url(base).endswith("/v1"))
        for base in ("http://example.com", "http://192.168.1.2:8317", "http://localhost.example.com", "http://127.0.0.1.evil.example"):
            with self.assertRaises(a.AiError):
                a.normalize_base_url(base)

    def test_url_credentials_and_parameters_rejected(self):
        for base in ("https://user:password@example.com", "https://example.com/?key=abc", "https://example.com/#abc", "https://example.com/?", "https://example.com/#"):
            with self.assertRaises(a.AiError):
                a.normalize_base_url(base)

    def test_bad_urls_rejected(self):
        for base in ("", "example.com", "ftp://example.com", "https://example.com:bad", "https://example.com/a/../v1", "https://example.com/a%2fv1", "https://example.com/v1/responses", "https://example.com\\@evil.com"):
            with self.assertRaises(a.AiError):
                a.normalize_base_url(base)

    def test_official_chatgpt_credentials_cannot_target_proxy(self):
        with self.assertRaises(a.AiError):
            client(auth_mode="chatgpt")
        with self.assertRaises(a.AiError):
            client(auth_mode="chatgpt", base_url="https://chatgpt.com/backend-api/codex")

    def test_official_default_and_protocol_enforced(self):
        instance = client(auth_mode="chatgpt", base_url="")
        self.assertEqual(instance.config.base_url, a.OFFICIAL_BASE_URL)
        self.assertEqual(instance.config.protocol, "responses")

    def test_key_required_and_header_injection_rejected(self):
        for key in ("", "\r\ninjected", "key with space", "非ASCII"):
            with self.assertRaises(a.AiError):
                client(api_key=key)

    def test_unknown_mode_protocol_and_bad_timeout(self):
        for settings in ({"auth_mode": "guess"}, {"protocol": "guess"}, {"timeout": 0}, {"timeout": float("inf")}, {"timeout": True}):
            with self.assertRaises(a.AiError):
                client(**settings)

    def test_model_optional_for_listing_not_inference(self):
        instance = client(model="")
        instance._opener.open = MagicMock(return_value=Response({"data": [{"id": "available"}]}))
        self.assertEqual(instance.list_models(), ["available"])
        with self.assertRaises(a.AiError):
            instance.complete(MESSAGES)
        self.assertEqual(instance._opener.open.call_count, 1)


class ModelTests(unittest.TestCase):
    def test_standard_models_keep_order_and_deduplicate(self):
        instance = client()
        instance._opener.open = MagicMock(return_value=Response({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}, {"id": "bad\nname"}]}))
        self.assertEqual(instance.list_models(), ["b", "a"])
        request = instance._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://confirmed.example/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)

    def test_oauth_catalog_filters_visibility_and_keeps_labels(self):
        instance = client(auth_mode="chatgpt", base_url="")
        instance._opener.open = MagicMock(return_value=Response({"models": [{"slug": "first", "display_name": "官方显示名称", "visibility": "list"},
                                                                            {"slug": "hidden", "visibility": "hide"}, {"slug": "unknown"}]}))
        self.assertEqual(instance.list_models(), ["first"])
        self.assertEqual(instance.model_labels, {"first": "官方显示名称"})

    def test_invalid_catalog_error(self):
        instance = client()
        instance._opener.open = MagicMock(return_value=Response({"unrelated": []}))
        with self.assertRaises(a.AiError):
            instance.list_models()

    def test_empty_catalog_is_not_guessed(self):
        instance = client()
        instance._opener.open = MagicMock(return_value=Response({"data": []}))
        self.assertEqual(instance.list_models(), [])


class CompletionTests(unittest.TestCase):
    def request_for(self, instance, output, content_type="application/json", messages=None, **kwargs):
        instance._opener.open = MagicMock(return_value=Response(output, content_type))
        result = instance.complete(messages or MESSAGES, **kwargs)
        return result, instance._opener.open.call_args.args[0]

    def test_chat_default_nonstream_payload(self):
        result, request = self.request_for(client(), completion(), instructions="仅分析给定数据。")
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://confirmed.example/v1/chat/completions")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(result["response_id"], "chat-1")
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertNotIn(KEY, request.data.decode())

    def test_chat_structured_text_content(self):
        result, _ = self.request_for(client(), completion([{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]))
        self.assertEqual(result["text"], "第一段\n第二段")

    def test_chat_refusal_is_displayable(self):
        payload = completion()
        payload["choices"][0]["message"] = {"content": None, "refusal": "不能保证上涨。"}
        self.assertIn("不能保证", self.request_for(client(), payload)[0]["text"])

    def test_chat_truncated_or_tool_call_not_success(self):
        for reason in (None, "length", "content_filter", "tool_calls"):
            with self.assertRaises(a.AiError):
                self.request_for(client(), completion(reason=reason))

    def test_chat_missing_finish_reason_is_not_complete(self):
        payload = completion("部分文字，尚未确认完成")
        payload["choices"][0].pop("finish_reason")
        with self.assertRaises(a.AiError):
            self.request_for(client(), payload)

    def test_empty_or_missing_chat_text_rejected(self):
        for payload in ({"choices": []}, completion(""), {"choices": [{"message": None}]}):
            with self.assertRaises(a.AiError):
                self.request_for(client(), payload)

    def test_responses_json(self):
        result, request = self.request_for(client(protocol="responses"), response_payload(), instructions="以事实为准")
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://confirmed.example/v1/responses")
        self.assertEqual(body["input"], MESSAGES)
        self.assertFalse(body["store"])
        self.assertFalse(body["stream"])
        self.assertEqual(result["response_id"], "response-1")

    def test_responses_json_incomplete_rejected(self):
        payload = response_payload()
        payload["status"] = "incomplete"
        with self.assertRaises(a.AiError):
            self.request_for(client(protocol="responses"), payload)

    def test_official_chatgpt_exact_supported_payload(self):
        instance = client(auth_mode="chatgpt", base_url="")
        output = sse({"type": "response.completed", "response": response_payload()})
        result, request = self.request_for(instance, output, "text/event-stream", messages=[{"role": "system", "content": "说明"}, *MESSAGES], instructions="开发者说明")
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertEqual(set(body), {"model", "input", "instructions", "store", "stream"})
        self.assertEqual(result["model"], "offline-model")

    def test_official_json_instead_of_sse_rejected(self):
        with self.assertRaises(a.AiError):
            self.request_for(client(auth_mode="chatgpt", base_url=""), response_payload())

    def test_sse_delta_completed_is_required(self):
        output = sse({"type": "response.created", "response": {"id": "stream-1", "model": "offline-model"}},
                     {"type": "response.output_text.delta", "delta": "已有"},
                     {"type": "response.output_text.delta", "delta": "走势"},
                     {"type": "response.completed", "response": {"status": "completed", "usage": {"total_tokens": 30}}})
        result, _ = self.request_for(client(protocol="responses"), output, "text/event-stream")
        self.assertEqual(result["text"], "已有走势")
        self.assertEqual(result["response_id"], "stream-1")

    def test_sse_completed_output_not_duplicate_deltas(self):
        output = sse({"type": "response.output_text.delta", "delta": "已有"}, {"type": "response.completed", "response": response_payload("已有")})
        self.assertEqual(self.request_for(client(protocol="responses"), output, "text/event-stream")[0]["text"], "已有")

    def test_sse_truncated_and_done_only_rejected(self):
        for output in (sse({"type": "response.output_text.delta", "delta": "只有部分"}), "data: [DONE]\n\n"):
            with self.assertRaises(a.AiError):
                self.request_for(client(protocol="responses"), output, "text/event-stream")

    def test_sse_failure_after_delta_is_not_success(self):
        output = sse({"type": "response.output_text.delta", "delta": "部分"},
                     {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}})
        with self.assertRaisesRegex(a.AiError, "共享额度已用尽"):
            self.request_for(client(auth_mode="chatgpt", base_url=""), output, "text/event-stream")

    def test_sse_incomplete_error_and_invalid_json(self):
        for output in (sse({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}),
                       sse({"type": "error", "message": "服务失败"}), "data: not json\n\n"):
            with self.assertRaises(a.AiError):
                self.request_for(client(protocol="responses"), output, "text/event-stream")

    def test_sse_event_header_and_no_final_blank_line(self):
        output = "event: response.completed\ndata: " + json.dumps({"response": response_payload()})
        self.assertEqual(self.request_for(client(protocol="responses"), output, "text/event-stream")[0]["response_id"], "response-1")

    def test_unexpected_chat_sse_rejected(self):
        with self.assertRaises(a.AiError):
            self.request_for(client(), sse({"type": "response.completed", "response": response_payload()}), "text/event-stream")

    def test_bad_history_roles_and_length(self):
        for messages in ([], [{"role": "tool", "content": "bad"}], [{"role": "user", "content": ""}], MESSAGES * (a.MAX_MESSAGES + 1), [{"role": "assistant", "content": "No user"}]):
            instance = client()
            instance._opener.open = MagicMock()
            with self.assertRaises(a.AiError):
                instance.complete(messages)
            instance._opener.open.assert_not_called()

    def test_payload_size_prevents_network_request(self):
        instance = client()
        instance._opener.open = MagicMock()
        with patch.object(a, "MAX_REQUEST_BYTES", 50), self.assertRaises(a.AiError):
            instance.complete(MESSAGES)
        instance._opener.open.assert_not_called()

    def test_response_content_length_and_stream_size_bounds(self):
        instance = client()
        instance._opener.open = MagicMock(return_value=Response(completion(), headers={"Content-Length": "99999999"}))
        with self.assertRaises(a.AiError):
            instance.complete(MESSAGES)
        with patch.object(a, "MAX_RESPONSE_BYTES", 30), self.assertRaises(a.AiError):
            self.request_for(client(protocol="responses"), sse({"type": "response.output_text.delta", "delta": "x" * 100}), "text/event-stream")

    def test_invalid_json_nonfinite_and_structure(self):
        for raw in ("<html>login</html>", "{\"usage\":NaN}", "[]"):
            with self.assertRaises(a.AiError):
                self.request_for(client(), raw)


class ErrorAndCancelTests(unittest.TestCase):
    def test_request_size_matches_exact_body_for_all_modes_without_io(self):
        messages = [{"role": "user", "content": '{"中文":"测试\\n快照"}'}, {"role": "assistant", "content": "完整先前回答"}, {"role": "user", "content": "继续"}]
        original = json.loads(json.dumps(messages))
        for mode, protocol in (("api_key", "chat_completions"), ("api_key", "responses"), ("chatgpt", "responses")):
            instance = client(auth_mode=mode, protocol=protocol, base_url="" if mode == "chatgpt" else "https://proxy.example/v1")
            instance._opener.open = MagicMock()
            size = instance.request_size(messages, "分析规则")
            path, body = instance.prepare_request(messages, "分析规则")
            self.assertEqual(size, len(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")))
            self.assertEqual(path, "/chat/completions" if protocol == "chat_completions" else "/responses")
            self.assertNotIn("truncation", body)
            instance._opener.open.assert_not_called()
        self.assertEqual(messages, original)

    def test_chinese_history_uses_bytes_not_characters_before_network(self):
        instance = client()
        instance._opener.open = MagicMock()
        messages = [{"role": "user", "content": "中" * 100000}]
        with self.assertRaisesRegex(a.AiError, "安全发送上限"):
            instance.request_size(messages, "保留完整历史")
        with self.assertRaisesRegex(a.AiError, "安全发送上限"):
            instance.complete(messages)
        instance._opener.open.assert_not_called()

    def http_failure(self, instance, status, payload, headers=None):
        body = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode())
        error = urllib.error.HTTPError(instance.config.base_url, status, "reason", headers or {}, body)
        instance._opener.open = MagicMock(side_effect=error)
        with self.assertRaises(a.AiError) as caught:
            instance.complete(MESSAGES)
        self.assertEqual(instance._opener.open.call_count, 1)
        self.assertTrue(body.closed)
        self.assertTrue(caught.exception.__suppress_context__)
        return caught.exception

    def test_official_direct_admission_detail_is_bounded_and_redacted(self):
        instance = client(auth_mode="chatgpt", base_url="")
        failure = self.http_failure(instance, 403, {"detail": "授权范围不足 Bearer " + KEY + " " + "x" * 1000},
                                    {"x-request-id": "req-direct-fixture"})
        self.assertIn("拒绝访问", str(failure))
        self.assertIn("授权范围不足", str(failure))
        self.assertNotIn(KEY, str(failure))
        self.assertLess(len(str(failure)), 500)
        self.assertEqual(failure.http_status, 403)
        self.assertEqual(failure.request_id, "req-direct-fixture")
        self.assertIsNone(failure.code)
        self.assertIsNone(failure.param)

    def test_structured_http_error_metadata_preserves_exact_safe_fields(self):
        failure = self.http_failure(client(), 400, {"error": {"message": "不支持该能力", "code": "subscription_sharing_unsupported_capability", "param": "tools[0].type"}},
                                    {"X-Request-ID": "req-structured-fixture"})
        self.assertEqual(failure.http_status, 400)
        self.assertEqual(failure.code, "subscription_sharing_unsupported_capability")
        self.assertEqual(failure.param, "tools[0].type")
        self.assertEqual(failure.request_id, "req-structured-fixture")

    def test_error_object_message_has_priority_over_direct_detail(self):
        failure = self.http_failure(client(), 401, {"error": {"message": "主错误"}, "detail": "次要说明"})
        self.assertIn("主错误", str(failure))
        self.assertNotIn("次要说明", str(failure))

    def test_metadata_is_redacted_bounded_and_not_a_raw_body(self):
        failure = self.http_failure(client(), 403, {"error": {"code": KEY + "x" * 300, "param": "Bearer " + KEY + " " + "p" * 300},
                                                  "extra": "must-not-be-retained"},
                                    {"x-request-id": "req-" + KEY + "r" * 300})
        self.assertNotIn(KEY, repr(failure.__dict__))
        self.assertLessEqual(len(failure.code), 120)
        self.assertLessEqual(len(failure.param), 120)
        self.assertLessEqual(len(failure.request_id), 160)
        self.assertNotIn("must-not-be-retained", repr(failure.__dict__))
        self.assertEqual(set(failure.__dict__), {"http_status", "code", "param", "request_id"})

    def test_nontext_direct_detail_and_metadata_are_not_stringified(self):
        failure = self.http_failure(client(), 503, {"error": {"code": {"secret": KEY}, "param": [KEY]}, "detail": {"secret": KEY}},
                                    {"x-request-id": [KEY]})
        self.assertNotIn(KEY, str(failure))
        self.assertNotIn(KEY, repr(failure.__dict__))
        self.assertIsNone(failure.code)
        self.assertIsNone(failure.param)
        self.assertIsNone(failure.request_id)

    def test_sse_failure_retains_structured_fields_and_safe_header(self):
        instance = client(auth_mode="chatgpt", base_url="")
        output = sse({"type": "response.output_text.delta", "delta": "部分文字"},
                     {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded", "param": "model", "message": "Bearer " + KEY}}})
        instance._opener.open = MagicMock(return_value=Response(output, "text/event-stream", {"x-request-id": "req-sse-fixture"}))
        with self.assertRaises(a.AiError) as caught:
            instance.complete(MESSAGES)
        failure = caught.exception
        self.assertIn("共享额度已用尽", str(failure))
        self.assertNotIn(KEY, str(failure))
        self.assertEqual(failure.http_status, 200)
        self.assertEqual(failure.code, "subscription_sharing_usage_limit_exceeded")
        self.assertEqual(failure.param, "model")
        self.assertEqual(failure.request_id, "req-sse-fixture")
        self.assertEqual(instance._opener.open.call_count, 1)

    def test_json_server_error_retains_structured_metadata(self):
        instance = client(protocol="responses")
        instance._opener.open = MagicMock(return_value=Response({"error": {"code": "invalid_model", "param": "model", "message": "模型不存在"}},
                                                              headers={"x-request-id": "req-json-fixture"}))
        with self.assertRaises(a.AiError) as caught:
            instance.complete(MESSAGES)
        self.assertEqual(caught.exception.http_status, 200)
        self.assertEqual(caught.exception.code, "invalid_model")
        self.assertEqual(caught.exception.param, "model")
        self.assertEqual(caught.exception.request_id, "req-json-fixture")

    def test_top_level_sse_error_event_retains_code_and_param(self):
        instance = client(protocol="responses")
        output = sse({"type": "error", "code": "server_error", "param": "input", "message": "failure " + KEY})
        instance._opener.open = MagicMock(return_value=Response(output, "text/event-stream", {"x-request-id": "req-event-fixture"}))
        with self.assertRaises(a.AiError) as caught:
            instance.complete(MESSAGES)
        self.assertEqual(caught.exception.code, "server_error")
        self.assertEqual(caught.exception.param, "input")
        self.assertEqual(caught.exception.request_id, "req-event-fixture")
        self.assertNotIn(KEY, str(caught.exception))

    def test_http_errors_redact_key_and_never_retry(self):
        for status, label in ((401, "身份验证"), (403, "拒绝访问"), (429, "额度"), (500, "HTTP 500")):
            instance = client()
            body = io.BytesIO(json.dumps({"error": {"message": "failure " + KEY}}).encode())
            error = urllib.error.HTTPError(instance.config.base_url, status, "reason", {}, body)
            instance._opener.open = MagicMock(side_effect=error)
            with self.assertRaises(a.AiError) as caught:
                instance.complete(MESSAGES)
            self.assertIn(label, str(caught.exception))
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(instance._opener.open.call_count, 1)

    def test_network_error_redacts_key(self):
        instance = client()
        instance._opener.open = MagicMock(side_effect=urllib.error.URLError("Bearer " + KEY))
        with self.assertRaises(a.AiError) as caught:
            instance.list_models()
        self.assertNotIn(KEY, str(caught.exception))

    def test_error_in_json_response_redacted(self):
        instance = client()
        instance._opener.open = MagicMock(return_value=Response({"error": {"message": "echo " + KEY}}))
        with self.assertRaises(a.AiError) as caught:
            instance.complete(MESSAGES)
        self.assertNotIn(KEY, str(caught.exception))

    def test_cancel_before_request(self):
        event = threading.Event()
        event.set()
        instance = client()
        instance._opener.open = MagicMock()
        with self.assertRaises(a.AiCancelled):
            instance.complete(MESSAGES, cancel_event=event)
        instance._opener.open.assert_not_called()

    def test_cancel_after_open_closes_response(self):
        event = threading.Event()
        output = Response(completion())
        instance = client()
        def opened(*_, **__):
            event.set()
            return output
        instance._opener.open = MagicMock(side_effect=opened)
        with self.assertRaises(a.AiCancelled):
            instance.complete(MESSAGES, cancel_event=event)
        self.assertTrue(output.closed)

    def test_cancel_during_response_discards_result(self):
        event = threading.Event()
        class CancelResponse(Response):
            def read(self, *args):
                data = super().read(*args)
                event.set()
                return data
        output = CancelResponse(completion())
        instance = client()
        instance._opener.open = MagicMock(return_value=output)
        with self.assertRaises(a.AiCancelled):
            instance.complete(MESSAGES, cancel_event=event)
        self.assertTrue(output.closed)


class OAuthResponseCompatibilityTests(unittest.TestCase):
    """Actual body format, not an unreliable MIME header, drives diagnostics."""

    def fixture(self, body, content_type="application/json", *, response_class=Response):
        instance = client(auth_mode="chatgpt", base_url="")
        output = response_class(body, content_type, {"x-request-id": "req-oauth-compat-fixture"})
        if not content_type:
            output.headers.pop("Content-Type", None)
        instance._opener.open = MagicMock(return_value=output)
        return instance, output

    def assert_single_official_attempt(self, instance, output):
        self.assertEqual(instance._opener.open.call_count, 1)
        request = instance._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, a.OFFICIAL_BASE_URL + "/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(request.get_header("Accept"), "text/event-stream")
        payload = json.loads(request.data)
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertTrue(output.closed)

    def assert_safe_diagnostics(self, error):
        self.assertEqual(error.http_status, 200)
        self.assertEqual(error.request_id, "req-oauth-compat-fixture")
        self.assertIn("req-oauth-compat-fixture", str(error))
        self.assertNotIn(KEY, str(error))
        self.assertNotIn(KEY, repr(error.__dict__))

    def test_accept_matches_streaming_inference_and_json_model_list(self):
        instance, output = self.fixture(sse({"type": "response.completed", "response": response_payload()}), "text/event-stream")
        self.assertTrue(instance.complete(MESSAGES)["text"])
        self.assert_single_official_attempt(instance, output)
        models = Response({"models": [{"slug": "offline-model", "visibility": "list"}]})
        instance._opener.open = MagicMock(return_value=models)
        self.assertEqual(instance.list_models(), ["offline-model"])
        request = instance._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, a.OFFICIAL_BASE_URL + "/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)

    def test_success_status_json_errors_and_mislabelled_sse_header_are_diagnosed(self):
        structured = {"code": "subscription_sharing_user_not_eligible", "param": "model", "message": "fixture-diagnosis " + KEY}
        cases = [({"error": structured}, "application/json", structured["code"]),
                 ({"detail": "fixture-diagnosis Bearer " + KEY}, "application/json", None),
                 ({"type": "response.failed", "response": {"status": "failed", "error": structured}}, "application/json", structured["code"]),
                 ({"error": structured}, "text/event-stream", structured["code"])]
        for body, content_type, code in cases:
            with self.subTest(content_type=content_type, shape=list(body)):
                instance, output = self.fixture(body, content_type)
                with self.assertRaises(a.AiError) as caught:
                    instance.complete(MESSAGES)
                error = caught.exception
                self.assertIn("fixture-diagnosis", str(error))
                self.assertEqual(error.code, code)
                self.assert_safe_diagnostics(error)
                if code:
                    self.assertEqual(error.param, "model")
                    self.assertIn(code, str(error))
                self.assert_single_official_attempt(instance, output)

    def test_sse_body_with_missing_wrong_headers_bom_blanks_and_comments(self):
        cases = [("", ""), ("text/plain", ""), ("application/json", "\n\n: fixture keepalive\n\n"),
                 ("", "\ufeff"), ("text/event-stream", "\ufeff"),
                 ("text/plain", "\ufeff\n\n: fixture keepalive\n\n"),
                 ("text/html; note=text/event-stream", "")]
        wire = sse({"type": "response.output_text.delta", "delta": "fixture-prefix"},
                   {"type": "response.completed", "response": {"status": "completed", "id": "response-prefix-fixture"}})
        for content_type, prefix in cases:
            with self.subTest(content_type=content_type, prefix=repr(prefix)):
                instance, output = self.fixture(prefix + wire, content_type)
                result = instance.complete(MESSAGES)
                self.assertEqual(result["text"], "fixture-prefix")
                self.assertEqual(result["response_id"], "response-prefix-fixture")
                self.assert_single_official_attempt(instance, output)

    def test_large_first_sse_line_is_replayed_without_loss_or_duplication(self):
        expected = "fixture-prefix-" + "x" * 10000 + "-fixture-suffix"
        wire = sse({"type": "response.output_text.delta", "delta": expected},
                   {"type": "response.completed", "response": {"status": "completed"}})
        self.assertGreater(len(wire.splitlines()[0].encode()), 8192)
        for content_type in ("", "application/json", "text/event-stream"):
            with self.subTest(content_type=content_type):
                instance, output = self.fixture(wire, content_type)
                self.assertEqual(instance.complete(MESSAGES)["text"], expected)
                self.assert_single_official_attempt(instance, output)

    def test_plain_completed_json_and_html_never_become_oauth_success(self):
        cases = [(response_payload(), "application/json"),
                 ("<html>private-page-fixture " + KEY + "</html>", "text/html"),
                 ("", "application/json"), (" \r\n\t", "application/json"),
                 ("private-page-fixture " + KEY, "application/octet-stream"),
                 ("[]", "application/json"), (b"\xff", "application/json")]
        for body, content_type in cases:
            with self.subTest(content_type=content_type):
                instance, output = self.fixture(body, content_type)
                with self.assertRaises(a.AiError) as caught:
                    instance.complete(MESSAGES)
                self.assert_safe_diagnostics(caught.exception)
                self.assertRegex(str(caught.exception), r"HTTP\s*200")
                self.assertIn(content_type, str(caught.exception))
                self.assertNotIn("private-page-fixture", str(caught.exception))
                self.assertNotIn("<html>", str(caught.exception))
                self.assertNotIn("超过安全识别限制", str(caught.exception))
                self.assert_single_official_attempt(instance, output)

    def test_mislabelled_sse_failure_truncation_and_limits_still_fail_closed(self):
        failures = [sse({"type": "response.output_text.delta", "delta": "only-partial"}),
                    "data: [DONE]\n\n",
                    sse({"type": "response.output_text.delta", "delta": "only-partial"},
                        {"type": "response.failed", "response": {"error": {"code": "server_error", "param": "input", "message": "fixture-stream-failure"}}})]
        for wire in failures:
            with self.subTest(wire=wire[:35]):
                instance, output = self.fixture(wire, "text/plain")
                with self.assertRaises(a.AiError) as caught:
                    instance.complete(MESSAGES)
                if "server_error" in wire:
                    self.assertEqual(caught.exception.code, "server_error")
                    self.assertEqual(caught.exception.param, "input")
                    self.assert_safe_diagnostics(caught.exception)
                    self.assertIn("server_error", str(caught.exception))
                self.assert_single_official_attempt(instance, output)
        wire = sse({"type": "response.output_text.delta", "delta": "x" * 300},
                   {"type": "response.completed", "response": {"status": "completed"}})
        for bound in ("MAX_RESPONSE_BYTES", "MAX_SSE_LINE_BYTES"):
            with self.subTest(bound=bound):
                instance, output = self.fixture(wire, "text/plain")
                with patch.object(a, bound, 128), self.assertRaises(a.AiError):
                    instance.complete(MESSAGES)
                self.assert_single_official_attempt(instance, output)

    def test_cancellation_while_sniffing_discards_and_closes_first_response(self):
        event = threading.Event()
        class CancelSniffResponse(Response):
            def read(self, *args):
                data = super().read(*args)
                event.set()
                return data
            def read1(self, *args):
                data = super().read1(*args)
                event.set()
                return data
            def readline(self, *args):
                data = super().readline(*args)
                event.set()
                return data
        wire = sse({"type": "response.completed", "response": response_payload()})
        instance, output = self.fixture(wire, "text/plain", response_class=CancelSniffResponse)
        with self.assertRaises(a.AiCancelled):
            instance.complete(MESSAGES, cancel_event=event)
        self.assert_single_official_attempt(instance, output)


class LocalHttpTests(unittest.TestCase):
    def test_real_loopback_responses_replays_mislabelled_sse_bom_and_long_line(self):
        expected = "local-fixture-prefix-" + "x" * 10000 + "-local-fixture-suffix"
        wire = ("\ufeff\n\n" + sse({"type": "response.output_text.delta", "delta": expected},
                                   {"type": "response.completed", "response": {"status": "completed", "id": "resp-local-compat-fixture"}})).encode("utf-8")
        def callback(_call):
            return 200, {"Content-Type": "application/json", "x-request-id": "req-local-compat-fixture"}, wire
        with local_server(callback) as (base, calls):
            instance = client(base_url=base, protocol="responses")
            result = instance.complete(MESSAGES)
            self.assertEqual(result["text"], expected)
            self.assertEqual(result["response_id"], "resp-local-compat-fixture")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["path"], "/v1/responses")
            self.assertEqual(calls[0]["authorization"], "Bearer " + KEY)
            body = json.loads(calls[0]["body"])
            self.assertFalse(body["store"])
            self.assertFalse(body["stream"])
            self.assertNotIn(KEY.encode(), calls[0]["body"])

    def test_real_loopback_http_paths_headers_and_request(self):
        def callback(call):
            payload = {"data": [{"id": "local-model"}]} if call["method"] == "GET" else completion()
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()
        with local_server(callback) as (base, calls):
            instance = client(base_url=base)
            self.assertEqual(instance.list_models(), ["local-model"])
            self.assertTrue(instance.complete(MESSAGES)["text"])
            self.assertEqual([call["path"] for call in calls], ["/v1/models", "/v1/chat/completions"])
            self.assertEqual(calls[0]["authorization"], "Bearer " + KEY)
            self.assertNotIn(KEY.encode(), calls[1]["body"])

    def test_redirect_does_not_send_key_to_second_server(self):
        def destination(_):
            return 200, {"Content-Type": "application/json"}, b'{"data":[]}'
        with local_server(destination) as (other, other_calls):
            def redirect(_):
                return 302, {"Location": other + "/v1/models"}, b""
            with local_server(redirect) as (base, calls):
                with self.assertRaisesRegex(a.AiError, "重定向"):
                    client(base_url=base).list_models()
                self.assertEqual(len(calls), 1)
                self.assertEqual(other_calls, [])


if __name__ == "__main__":
    unittest.main()
