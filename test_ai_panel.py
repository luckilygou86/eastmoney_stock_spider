"""Real Tk UI, fake AI/auth: never open a browser, sign in or incur AI charges."""
import copy
import gc
import json
import threading
import time
import tkinter as tk
import types
import unittest
from unittest.mock import MagicMock, patch

import ai_panel as a
from ai_analysis import AiError
from test_ai_analysis import Response
from test_stock_ui import fixture
from workspace_state import sanitize_preferences


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("760x480+20000+20000")
        self.preferences = sanitize_preferences({})
        self.save = MagicMock(return_value=True)
        self.panel = a.AiPanel(self.root, self.preferences, self.save)
        self.panel.pack(fill="both", expand=True)
        self.root.update()
        self.result = fixture()
        self.panel.update_snapshot(self.result, ready=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.panel.close()
        worker = self.panel._worker_thread
        if worker is not None:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), "mock worker must stop before destroying Tk")
        self.root.destroy()
        self.panel = None
        self.root = None
        # Collect Tk variable/widget cycles on their owning thread, never a later worker.
        gc.collect()

    def configure(self):
        self.panel.save_settings({"auth_mode": "api_key", "base_url": "https://proxy.example/v1",
                                  "model": "unit-model", "protocol": "chat_completions"}, "unit-private-key")

    def drain(self, timeout=2):
        deadline = time.monotonic() + timeout
        while self.panel._events.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.panel._events.empty(), "fake worker must complete")
        self.panel.after_cancel(self.panel._poll_job)
        self.panel.poll()

    def answer(self, text="已有走势描述，不保证未来。"):
        return {"text": text, "model": "unit-model", "usage": {"total_tokens": 12}}

    def initial_answer(self, text="已有走势描述，不保证未来。"):
        self.configure()
        with patch.object(a.AiClient, "complete", return_value=self.answer(text)):
            self.panel.analyze_current()
            self.drain()

    def test_startup_and_live_updates_never_connect_automatically(self):
        with patch.object(a.AiClient, "complete") as complete, patch.object(a.AiClient, "list_models") as models, patch.object(a.webbrowser, "open") as browser:
            self.panel.update_snapshot(self.result)
            self.panel.open_settings()
            self.root.update_idletasks()
            self.panel._dialog.destroy()
            complete.assert_not_called()
            models.assert_not_called()
            browser.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.panel.settings["base_url"], "")
        self.assertEqual(self.panel.settings["model"], "")

    def test_incomplete_settings_fail_locally_without_paid_calls(self):
        with patch.object(a.AiClient, "complete") as complete:
            self.panel.analyze_current()
        complete.assert_not_called()
        self.assertIn("模型", self.panel.status.get())

    def test_large_chinese_followup_history_is_rejected_before_worker(self):
        self.initial_answer()
        self.panel.messages = [{"role": "user", "content": "历史问题"}, {"role": "assistant", "content": "中" * 100000}]
        before = copy.deepcopy(self.panel.messages)
        with patch.object(a.AiClient, "complete") as complete, patch.object(self.panel, "_submit") as submit:
            self.assertFalse(self.panel._send_question("继续分析。"))
            complete.assert_not_called()
            submit.assert_not_called()
        self.assertEqual(self.panel.messages, before)
        self.assertIn("安全发送上限", self.panel.status.get())

    def test_oauth_oversize_preflight_does_not_refresh_or_send_credentials(self):
        self.initial_answer()
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": True, "plan_enabled": True}
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        # Settings change deliberately clears the old conversation. Re-pin
        # locally without starting a worker, then check the oversized history.
        from ai_context import context_json
        self.panel.pinned = copy.deepcopy(self.result)
        self.panel.context_text = context_json(self.result)
        self.panel.messages = [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "中" * 100000}]
        with patch.object(a.AiClient, "complete") as complete, patch.object(self.panel, "_submit") as submit:
            self.assertFalse(self.panel._send_question("继续。"))
            submit.assert_not_called()
            complete.assert_not_called()
        auth.access_token.assert_not_called()
        self.assertIn("安全发送上限", self.panel.status.get())

    def test_snapshot_scope_and_preview_match_sent_payload(self):
        self.initial_answer()
        self.assertIn("完整导出不变", self.panel.snapshot_info.get())
        self.assertIn("KiB", self.panel.snapshot_info.get())
        self.panel.preview_context()
        preview = [w for w in self.panel.winfo_children() if isinstance(w, tk.Toplevel)][-1]
        content = [w for frame in preview.winfo_children() if isinstance(frame, a.ttk.Frame) for w in frame.winfo_children() if isinstance(w, tk.Text)][0]
        self.assertEqual(content.get("1.0", "end-1c"), self.panel.context_text)
        preview.destroy()

    def test_settings_only_persist_nonsensitive_fields_and_mask_key(self):
        self.configure()
        serialized = json.dumps(self.preferences)
        self.assertNotIn("unit-private-key", serialized)
        self.assertFalse(any("key" in name for name in self.preferences))
        self.panel.open_settings()
        self.assertEqual(str(self.panel.key_entry["show"]), "•")
        self.assertEqual(self.panel._key, "unit-private-key")

    def test_wait_settings_are_visible_saved_in_seconds_and_bounded(self):
        self.configure()
        self.assertEqual(self.panel.settings["stream_timeout"], 600)
        self.assertEqual(self.panel.settings["idle_timeout"], 120)
        self.panel.open_settings()
        self.assertEqual(self.panel._dialog_vars["stream_timeout"].get(), "10")
        self.assertEqual(self.panel._dialog_vars["idle_timeout"].get(), "120")
        self.panel._dialog_vars["stream_timeout"].set("15")
        self.panel._dialog_vars["idle_timeout"].set("180")
        self.assertTrue(self.panel._save_dialog(close=False))
        self.assertEqual(self.preferences["ai_stream_timeout"], 900)
        self.assertEqual(self.preferences["ai_idle_timeout"], 180)
        self.assertNotIn("unit-private-key", repr(self.preferences))
        for name, value in (("stream_timeout", 29), ("stream_timeout", 1801),
                            ("stream_timeout", float("nan")), ("stream_timeout", True),
                            ("idle_timeout", 4), ("idle_timeout", 301), ("idle_timeout", float("inf"))):
            with self.assertRaises(AiError):
                self.panel.save_settings(dict(self.panel.settings, **{name: value}), self.panel._key)
        self.panel.save_settings(dict(self.panel.settings, stream_timeout=30, idle_timeout=5), self.panel._key)
        self.assertEqual(self.panel.settings["stream_timeout"], 30)
        self.assertEqual(self.panel.settings["idle_timeout"], 5)

    def test_progress_is_bounded_metadata_not_reply_text_and_never_completes_early(self):
        self.configure()
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        answer = self.answer("只有完整结束后的正式回复")
        seen_config = []
        def complete(client, *_args, **kwargs):
            seen_config.append(client.config)
            callback = kwargs["progress_callback"]
            for number in range(2000):
                callback({"phase": "receiving", "elapsed_seconds": 12, "bytes_received": number,
                          "events_received": number, "text_chars": number,
                          "text": "不可显示的部分正文", "token": "unit-private-key"})
            entered.set()
            gate.wait(2)
            return answer
        with patch.object(a.AiClient, "complete", autospec=True, side_effect=complete) as called:
            self.panel.analyze_current()
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.panel._progress_events.maxsize, 1)
            self.assertEqual(self.panel._progress_events.qsize(), 1)
            self.assertTrue(self.panel._events.empty())
            self.panel.after_cancel(self.panel._poll_job)
            self.panel.poll()
            self.assertIn("1999", self.panel.status.get())
            self.assertIn("等待完整", self.panel.status.get())
            self.assertNotIn("不可显示的部分正文", self.panel.status.get() + self.panel.chat.get("1.0", "end"))
            self.assertNotIn("unit-private-key", self.panel.status.get())
            self.assertEqual(self.panel.messages, [])
            self.assertIsNotNone(self.panel._inflight)
            self.assertEqual(str(self.panel.send_button["state"]), "disabled")
            self.assertEqual(seen_config[0].stream_timeout, 600)
            self.assertEqual(seen_config[0].idle_timeout, 120)
            gate.set()
            self.drain()
            called.assert_called_once()
        self.assertEqual(len(self.panel.messages), 2)
        self.assertIn("分析完成", self.panel.status.get())

    def test_snapshot_is_copied_and_followups_stay_on_original_data(self):
        self.initial_answer()
        original = self.panel.pinned["quote"]["最新价"]
        self.result["quote"]["最新价"] = 999
        self.result["quote"]["行情时间"] = "20260930153100"
        self.panel.update_snapshot(self.result)
        self.assertEqual(self.panel.pinned["quote"]["最新价"], original)
        with patch.object(a.AiClient, "complete", return_value=self.answer()) as complete:
            self.panel.question.insert("1.0", "说明波动情况")
            self.panel.send_followup()
            self.drain()
        sent = complete.call_args.args[0]
        self.assertNotIn('"最新价":999', sent[0]["content"])
        self.assertEqual(len(self.panel.messages), 4)
        self.assertIn("当前行情已更新", self.panel.snapshot_info.get())

    def test_empty_questions_and_followup_before_initial_analysis_do_not_send(self):
        with patch.object(a.AiClient, "complete") as complete:
            self.panel.send_followup()
            self.panel.question.insert("1.0", "为什么波动？")
            self.panel.send_followup()
        complete.assert_not_called()

    def test_empty_ai_result_is_not_appended_or_automatically_retried(self):
        self.configure()
        with patch.object(a.AiClient, "complete", return_value=self.answer("  ")) as complete:
            self.panel.analyze_current()
            self.drain()
        self.assertEqual(self.panel.messages, [])
        self.assertEqual(complete.call_count, 1)
        self.assertIn("空内容", self.panel.status.get())

    def test_error_response_redacts_memory_key_and_jwt(self):
        self.configure()
        error = "unit-private-key Bearer unit-private-key eyJab.eyJcd.abcdefgh"
        with patch.object(a.AiClient, "complete", side_effect=AiError(error)):
            self.panel.analyze_current()
            self.drain()
        shown = self.panel.status.get() + self.panel.chat.get("1.0", "end")
        self.assertNotIn("unit-private-key", shown)
        self.assertNotIn("eyJab", shown)

    def test_credential_echo_from_provider_is_not_added_to_chat_or_queue(self):
        self.configure()
        with patch.object(a.AiClient, "complete", return_value=self.answer("echo unit-private-key")):
            self.panel.analyze_current()
            self.drain()
        self.assertNotIn("unit-private-key", self.panel.chat.get("1.0", "end"))
        self.assertNotIn("unit-private-key", repr(self.panel.messages))

    def test_stock_switch_cancels_and_discards_old_answer(self):
        self.configure()
        gate = threading.Event()
        entered = threading.Event()
        self.addCleanup(gate.set)
        def complete(*_args, **_kwargs):
            entered.set()
            gate.wait(2)
            return self.answer("旧股票的答案")
        with patch.object(a.AiClient, "complete", side_effect=complete):
            self.panel.analyze_current()
            self.assertTrue(entered.wait(1))
            other = copy.deepcopy(self.result)
            other["identity"]["code"] = "300059"
            other["quote"]["代码"] = "300059"
            self.panel.update_snapshot(other)
            gate.set()
            self.drain()
        self.assertIsNone(self.panel.pinned)
        self.assertEqual(self.panel.messages, [])
        self.assertNotIn("旧股票的答案", self.panel.chat.get("1.0", "end"))

    def test_cross_day_clears_conversation_and_requires_reanalysis(self):
        self.initial_answer()
        self.result["quote"]["行情时间"] = "20261001093000"
        self.panel.update_snapshot(self.result)
        self.assertIsNone(self.panel.pinned)
        self.assertEqual(str(self.panel.send_button["state"]), "disabled")

    def test_query_or_history_sync_disables_ai_and_cancels_pending_answer(self):
        self.configure()
        self.panel.update_snapshot(self.result, ready=False)
        with patch.object(a.AiClient, "complete") as complete:
            self.panel.analyze_current()
        complete.assert_not_called()
        self.assertEqual(str(self.panel.analyze_button["state"]), "disabled")

    def test_history_pending_and_mismatched_code_are_rejected_by_context(self):
        self.configure()
        for result in (dict(self.result, history_sync={"status": "pending"}),
                       dict(self.result, quote=dict(self.result["quote"], **{"代码": "300059"}))):
            self.panel.update_snapshot(result, ready=True)
            with patch.object(a.AiClient, "complete") as complete:
                self.panel.analyze_current()
            complete.assert_not_called()

    def test_clear_chat_discards_pending_answer_but_does_not_spawn_overlapping_calls(self):
        self.configure()
        gate = threading.Event()
        entered = threading.Event()
        self.addCleanup(gate.set)
        def complete(*_args, **_kwargs):
            entered.set()
            gate.wait(2)
            return self.answer("已经清空的答案")
        with patch.object(a.AiClient, "complete", side_effect=complete) as call:
            self.panel.analyze_current()
            self.assertTrue(entered.wait(1))
            self.panel.clear_chat()
            self.panel.analyze_current()
            self.assertEqual(call.call_count, 1)
            gate.set()
            self.drain()
        self.assertNotIn("已经清空的答案", self.panel.chat.get("1.0", "end"))

    def test_settings_changes_cancel_old_answer_and_preserve_original_request_endpoint(self):
        self.configure()
        gate = threading.Event()
        entered = threading.Event()
        self.addCleanup(gate.set)
        seen = []
        def complete(client, *_args, **_kwargs):
            seen.append(client.config)
            entered.set()
            gate.wait(2)
            return self.answer("旧配置的答案")
        with patch.object(a.AiClient, "complete", autospec=True, side_effect=complete):
            self.panel.analyze_current()
            self.assertTrue(entered.wait(1))
            self.panel.save_settings({"auth_mode": "api_key", "base_url": "https://new.example/v1", "model": "new-model", "protocol": "responses"}, "new-private-key")
            gate.set()
            self.drain()
        self.assertEqual(seen[0].base_url, "https://proxy.example/v1")
        self.assertEqual(seen[0].api_key, "unit-private-key")
        self.assertNotIn("旧配置的答案", self.panel.chat.get("1.0", "end"))

    def test_max_conversation_pairs_does_not_silently_truncate_or_request(self):
        self.initial_answer()
        self.panel.messages = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}] * a.MAX_PAIRS
        self.panel.question.insert("1.0", "还可以继续吗？")
        with patch.object(a.AiClient, "complete") as complete:
            self.panel.send_followup()
        complete.assert_not_called()
        self.assertEqual(len(self.panel.messages), 20)
        self.assertIn("上限", self.panel.status.get())

    def test_overlong_or_credential_question_does_not_send(self):
        self.initial_answer()
        for question in ("x" * (a.MAX_QUESTION_CHARS + 1), "密钥 unit-private-key"):
            self.panel.question.delete("1.0", "end")
            self.panel.question.insert("1.0", question)
            with patch.object(a.AiClient, "complete") as complete:
                self.panel.send_followup()
            complete.assert_not_called()

    def test_model_list_is_explicit_and_never_infers_stock(self):
        self.configure()
        with patch.object(a.AiClient, "list_models", return_value=["model-a", "model-b"]) as models, patch.object(a.AiClient, "complete") as complete:
            self.panel.list_models()
            self.drain()
        self.assertEqual(len(self.panel._model_options), 2)
        self.assertEqual(models.call_count, 1)
        self.assertIsNotNone(models.call_args.kwargs["cancel_event"])
        complete.assert_not_called()

    def test_proxy_draft_and_key_are_not_silently_retargeted_to_openai_on_mode_switch(self):
        self.configure()
        self.panel._auth = MagicMock()
        self.panel._auth.status.return_value = {"signed_in": False, "plan_enabled": False}
        self.panel.open_settings()
        self.panel._dialog_vars["auth_mode"].set("chatgpt")
        self.panel._dialog_mode_changed()
        self.assertEqual(self.panel._dialog_vars["base_url"].get(), a.OFFICIAL_BASE_URL)
        self.panel._save_dialog(close=False)
        self.assertEqual(self.panel.preferences["ai_base_url"], "https://proxy.example/v1")
        self.assertEqual(self.panel.preferences["ai_protocol"], "chat_completions")
        self.panel._dialog_vars["auth_mode"].set("api_key")
        self.panel._dialog_mode_changed()
        self.assertEqual(self.panel._dialog_vars["base_url"].get(), "https://proxy.example/v1")
        self.panel._save_dialog(close=False)
        self.assertEqual(self.panel.settings["base_url"], "https://proxy.example/v1")
        self.assertEqual(self.panel._key, "unit-private-key")

    def test_provider_change_resets_model_catalog(self):
        self.configure()
        self.panel._model_options = {"Friendly [old-model]": "old-model"}
        self.panel._model_source = self.panel._service_key(self.panel.settings)
        self.panel.save_settings({"auth_mode": "api_key", "base_url": "https://new.example/v1", "model": "", "protocol": "responses"}, "new-private-key")
        self.assertEqual(self.panel._model_options, {})

    def test_same_endpoint_account_key_change_resets_old_catalog_and_does_not_map_old_label(self):
        self.configure()
        self.panel._model_options = {"Friendly [old-model]": "old-model"}
        self.panel._model_source = self.panel._service_key(self.panel.settings)
        self.panel.open_settings()
        self.panel._dialog_vars["model"].set("Friendly [old-model]")
        self.panel._dialog_vars["api_key"].set("new-account-private-key")
        self.panel._save_dialog(close=False)
        self.assertEqual(self.panel.settings["model"], "")
        self.assertEqual(self.panel._model_options, {})
        self.assertNotIn("new-account-private-key", repr(self.preferences))

    def test_settings_dialog_fits_640_by_410(self):
        self.panel.open_settings()
        self.panel._dialog.geometry("640x410+20000+20000")
        self.root.update()
        frame = self.panel.base_entry.master
        for widget in frame.winfo_children():
            self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), frame.winfo_width())
            self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), frame.winfo_height())

    def test_plan_reenable_requires_explicit_click_and_confirmation(self):
        auth = self.panel._auth = MagicMock()
        self.panel._auth_status = {"signed_in": True, "plan_enabled": False}
        auth.status.return_value = self.panel._auth_status
        auth.sign_in.return_value = {"signed_in": True, "plan_enabled": True, "welcome_seen": True}
        with patch.object(a.messagebox, "askyesno", return_value=False):
            self.panel.start_sign_in()
        auth.sign_in.assert_not_called()
        with patch.object(a.messagebox, "askyesno", return_value=True):
            self.panel.start_sign_in()
            self.drain()
        self.assertTrue(auth.sign_in.call_args.kwargs["enable_plan"])

    def test_official_login_is_only_explicit_and_first_plan_confirmation_once(self):
        auth = self.panel._auth = MagicMock()
        auth.sign_in.return_value = {"signed_in": True, "email": "test@example.test", "plan_enabled": True, "welcome_seen": False}
        auth.status.return_value = auth.sign_in.return_value
        with patch.object(a.messagebox, "showinfo") as welcome:
            auth.sign_in.assert_not_called()
            self.panel.start_sign_in()
            self.drain()
            welcome.assert_called_once()
            auth.mark_welcome_seen.assert_called_once()
            auth.sign_in.return_value = dict(auth.sign_in.return_value, welcome_seen=True)
            self.panel.start_sign_in()
            self.drain()
            self.assertEqual(welcome.call_count, 1)
        self.assertIn("Using ChatGPT plan", self.panel.provider_info.get())

    def test_identity_without_plan_cannot_infer_or_fall_back_to_proxy_key(self):
        self.configure()
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": True, "plan_enabled": False}
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        with patch.object(a.AiClient, "complete") as complete:
            self.panel.analyze_current()
        complete.assert_not_called()
        auth.access_token.assert_not_called()
        self.assertIn("不会自动", self.panel.status.get())

    def test_oauth_token_goes_only_to_official_endpoint_and_is_not_persisted(self):
        self.configure()
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": True, "plan_enabled": True}
        auth.access_token.return_value = "own-oauth-private-token"
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        seen = []
        def complete(client, *_args, **_kwargs):
            seen.append(client.config)
            return self.answer()
        with patch.object(a.AiClient, "complete", autospec=True, side_effect=complete):
            self.panel.analyze_current()
            self.drain()
        self.assertEqual(seen[0].base_url, a.OFFICIAL_BASE_URL)
        self.assertEqual(seen[0].protocol, "responses")
        self.assertEqual(seen[0].api_key, "own-oauth-private-token")
        self.assertNotIn("own-oauth-private-token", repr(self.preferences) + repr(self.panel.messages))

    def test_official_service_error_diagnostics_survive_ui_queue_without_success_or_fallback(self):
        self.configure()
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": True, "plan_enabled": True}
        auth.access_token.return_value = "own-oauth-private-token"
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        opener = MagicMock()
        opener.open.return_value = Response({"error": {"message": "服务拒绝了当前模型。",
                                                       "code": "model_not_found", "param": "model"}},
                                            headers={"x-request-id": "req_test_initial_response"})
        with patch("ai_analysis.urllib.request.build_opener", return_value=opener):
            self.panel.analyze_current()
            self.drain()
            self.root.update()
            opener.open.assert_called_once()
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, a.OFFICIAL_BASE_URL + "/responses")
            self.assertEqual(request.get_header("Authorization"), "Bearer own-oauth-private-token")
            self.assertIs(json.loads(request.data)["stream"], True)
            self.assertIs(json.loads(request.data)["store"], False)
        auth.access_token.assert_called_once()
        for displayed in (self.panel.status.get(), self.panel.chat.get("1.0", "end")):
            for diagnostic in ("HTTP 200", "错误码=model_not_found", "参数=model", "请求编号=req_test_initial_response"):
                self.assertIn(diagnostic, displayed)
            self.assertNotIn("unit-private-key", displayed)
            self.assertNotIn("own-oauth-private-token", displayed)
        self.assertEqual(self.panel.settings["auth_mode"], "chatgpt")
        self.assertEqual(self.panel.messages, [])
        self.assertIsNotNone(self.panel.pinned)
        self.assertNotIn("AI ·", self.panel.chat.get("1.0", "end"))

    def test_official_followup_error_keeps_snapshot_and_prior_conversation_without_retry(self):
        self.configure()
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": True, "plan_enabled": True}
        auth.access_token.return_value = "own-oauth-private-token"
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        with patch.object(a.AiClient, "complete", return_value=self.answer()):
            self.panel.analyze_current()
            self.drain()
            self.panel.question.insert("1.0", "说明当前量价关系")
            self.panel.send_followup()
            self.drain()
        pinned, pinned_id, context = self.panel.pinned, self.panel.pinned_id, self.panel.context_text
        history = copy.deepcopy(self.panel.messages)
        self.assertEqual(len(history), 4)
        refreshed = copy.deepcopy(self.result)
        refreshed["quote"]["行情时间"] = "20260930153100"
        self.panel.update_snapshot(refreshed)
        opener = MagicMock()
        # Exercise a real transport error after a text delta: partial content
        # must not become a successful assistant message or damage old turns.
        events = ({"type": "response.output_text.delta", "delta": "不完整的测试回复"},
                  {"type": "error", "message": "当前计划额度不足。",
                   "code": "insufficient_quota", "param": "model"})
        stream = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
        opener.open.return_value = Response(stream, "text/event-stream",
                                            {"x-request-id": "req_test_followup_response"})
        with patch("ai_analysis.urllib.request.build_opener", return_value=opener):
            self.panel.question.insert("1.0", "这些观察条件有哪些限制？")
            self.panel.send_followup()
            self.drain()
            self.root.update()
            opener.open.assert_called_once()
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, a.OFFICIAL_BASE_URL + "/responses")
            self.assertEqual(request.get_header("Authorization"), "Bearer own-oauth-private-token")
            sent_messages = json.loads(request.data)["input"]
            self.assertEqual(sent_messages[1:-1], history)
            self.assertIn(context, sent_messages[0]["content"])
        for displayed in (self.panel.status.get(), self.panel.chat.get("1.0", "end")):
            for diagnostic in ("HTTP 200", "错误码=insufficient_quota", "参数=model", "请求编号=req_test_followup_response"):
                self.assertIn(diagnostic, displayed)
            self.assertNotIn("不完整的测试回复", displayed)
        self.assertEqual(auth.access_token.call_count, 3)
        self.assertIs(self.panel.pinned, pinned)
        self.assertEqual(self.panel.pinned_id, pinned_id)
        self.assertEqual(self.panel.context_text, context)
        self.assertEqual(self.panel.messages, history)
        self.assertEqual(self.panel.chat.get("1.0", "end").count("AI ·"), 2)
        self.assertEqual(self.panel.settings["auth_mode"], "chatgpt")

    def test_pending_authorized_login_revalidates_only_after_manual_analysis(self):
        self.configure()
        auth = self.panel._auth = MagicMock()
        pending = {"signed_in": True, "plan_enabled": False, "plan_authorized": True, "pending_verification": True}
        auth.status.return_value = pending
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        self.panel._auth_status = pending
        self.panel._refresh_provider_label()
        self.assertIn("登录校验待恢复", self.panel.provider_info.get())
        self.panel.update_snapshot(self.result)
        auth.access_token.assert_not_called()
        def recovered(**_kwargs):
            auth.status.return_value = {"signed_in": True, "plan_enabled": True, "plan_authorized": True, "pending_verification": False}
            return "own-recovered-token"
        auth.access_token.side_effect = recovered
        with patch.object(a.AiClient, "complete", return_value=self.answer()) as complete:
            self.panel.analyze_current()
            self.drain()
        auth.access_token.assert_called_once()
        complete.assert_called_once()
        self.assertIn("Using ChatGPT plan", self.panel.provider_info.get())

    def test_pending_granted_login_reauthentication_does_not_request_new_plan_consent(self):
        auth = self.panel._auth = MagicMock()
        self.panel._auth_status = {"signed_in": True, "plan_enabled": False, "plan_authorized": True, "pending_verification": True}
        auth.sign_in.return_value = {"signed_in": True, "plan_enabled": True, "welcome_seen": True}
        with patch.object(a.messagebox, "askyesno") as consent:
            self.panel.start_sign_in()
            self.drain()
        consent.assert_not_called()
        self.assertFalse(auth.sign_in.call_args.kwargs["enable_plan"])

    def test_pending_verification_failure_never_uses_proxy_key_or_retries_inference(self):
        self.configure()
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": True, "plan_enabled": False, "plan_authorized": True, "pending_verification": True}
        auth.access_token.side_effect = AiError("登录校验尚未恢复，请稍后主动重试。")
        self.panel.save_settings(dict(self.panel.settings, auth_mode="chatgpt"), self.panel._key)
        with patch.object(a.AiClient, "complete") as complete:
            self.panel.analyze_current()
            self.drain()
        complete.assert_not_called()
        auth.access_token.assert_called_once()
        self.assertEqual(self.panel.settings["auth_mode"], "chatgpt")
        self.assertEqual(self.panel.messages, [])
        self.assertIn("尚未恢复", self.panel.status.get())

    def test_logout_revocation_warning_is_visible_and_conversation_cleared(self):
        auth = self.panel._auth = MagicMock()
        auth.status.return_value = {"signed_in": False, "plan_enabled": False, "remote_revocation_unconfirmed": True, "warning": "本应用已退出，但未确认远端撤销，请在 ChatGPT 设置中断开。"}
        self.panel.start_sign_out()
        self.drain()
        self.assertIn("未确认", self.panel.status.get())
        self.assertEqual(self.panel.messages, [])

    def test_manage_usage_opens_only_documented_explicit_url(self):
        with patch.object(a.webbrowser, "open") as browser:
            browser.assert_not_called()
            self.panel.manage_usage()
            browser.assert_called_once_with("https://chatgpt.com/settings/usage")

    def test_close_cancels_and_clears_in_memory_secrets(self):
        self.configure()
        self.panel._cancel_event = threading.Event()
        event = self.panel._cancel_event
        self.panel.close()
        self.assertTrue(event.is_set())
        self.assertEqual(self.panel._key, "")
        self.assertIsNone(self.panel._poll_job)
        self.panel.poll()

    def test_background_closures_do_not_retain_tk_objects_after_close(self):
        self.configure()
        gate = threading.Event()
        entered = threading.Event()
        self.addCleanup(gate.set)
        answer = self.answer()
        def complete(*_args, **_kwargs):
            entered.set()
            gate.wait(2)
            return answer
        with patch.object(a.AiClient, "complete", side_effect=complete):
            self.panel.analyze_current()
            self.assertTrue(entered.wait(1))
            target = self.panel._worker_thread._target
            pending, seen = [target], set()
            while pending:
                value = pending.pop()
                if id(value) in seen:
                    continue
                seen.add(id(value))
                self.assertNotIsInstance(value, (tk.Misc, tk.Variable))
                if isinstance(value, types.FunctionType):
                    pending.extend(cell.cell_contents for cell in (value.__closure__ or ()))
            self.panel.close()
            gate.set()
            self.panel._worker_thread.join(timeout=1)
            self.assertFalse(self.panel._worker_thread.is_alive())
            self.panel.poll()
        self.assertEqual(self.panel.messages, [])
        self.assertEqual(self.panel._key, "")

    def test_compact_panel_has_usable_chat_and_no_overflow(self):
        self.root.geometry("716x330+20000+20000")
        self.root.update()
        self.assertGreater(self.panel.chat.winfo_height(), 50)
        for widget in self.panel.analyze_button.master.winfo_children():
            self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), widget.master.winfo_width())


if __name__ == "__main__":
    unittest.main()
