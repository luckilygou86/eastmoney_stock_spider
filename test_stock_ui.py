"""UI regressions use real Tk off-screen and mock every network/write action."""
import copy
import queue
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import stock_spider as s
from test_stock_spider import raw_fixture
from workspace_state import PROJECT_ROOT, sanitize_preferences


def fixture():
    result = s.normalize(raw_fixture(
        intraday={"data": {"trends": ["09:30,10,0,100,100000"]}, "meta": {"trade_date": "2026-09-30"}},
        daily={"data": {"klines": ["2026-09-29,10,10,11,9,100,100000,1", "2026-09-30,10,10,11,9,100,100000,1"]}},
    ))
    result["daily_history"] = result["daily"]
    result["daily_indicators"] = s.enrich_daily_indicators(result["daily_history"])
    result["analysis"] = s.build_analysis(result)
    return result


class UiRegressions(unittest.TestCase):
    def setUp(self):
        self.preferences = patch("workspace_state.load_preferences", return_value=sanitize_preferences({}))
        self.save = patch("workspace_state.save_preferences", return_value=True)
        self.preferences.start()
        self.mock_save = self.save.start()
        self.addCleanup(self.preferences.stop)
        self.addCleanup(self.save.stop)
        self.app = s.App()
        self.app.geometry("1040x700+20000+20000")
        self.app.update()
        self.addCleanup(self.destroy_app)

    def destroy_app(self):
        if self.app.winfo_exists():
            self.app.ai_panel.close()
            if self.app.poll_job:
                self.app.after_cancel(self.app.poll_job)
            if self.app.live_job:
                self.app.after_cancel(self.app.live_job)
            self.app.destroy()

    def install_result(self):
        result = fixture()
        self.app.result = result
        self.app.live_identity = s.parse_query("301307")
        self.app.render(result)
        return result

    def test_startup_reads_preferences_without_writing(self):
        self.mock_save.assert_not_called()
        self.assertEqual(self.app.refresh_seconds.get(), "15")
        self.assertEqual(self.app.chart_mode.get(), "日线")
        self.assertEqual(str(self.app.export["state"]), "disabled")

    def test_tabs_focus_on_analysis_not_interface_testing_or_estimated_chips(self):
        labels = [self.app.tabs.tab(tab, "text") for tab in self.app.tabs.tabs()]
        self.assertEqual(labels, ["走势概览", "走势图", "成交明细", "历史指标", "数据质量", "龙虎榜", "研究分析", "AI 分析"])

    def test_ai_panel_fits_minimum_window_and_starts_without_network_or_saved_key(self):
        import ai_panel
        self.install_result()
        with patch.object(ai_panel.AiClient, "complete") as complete, patch.object(ai_panel.AiClient, "list_models") as models:
            self.app.geometry("760x520+20000+20000")
            self.app.tabs.select(self.app.ai_panel)
            self.app.update()
            self.assertGreater(self.app.ai_panel.chat.winfo_height(), 50)
            self.assertEqual(self.app.ai_panel._key, "")
            self.assertEqual(self.app.ai_panel.settings["base_url"], "")
            complete.assert_not_called()
            models.assert_not_called()

    def test_chart_and_toolbar_fit_default_and_minimum_window(self):
        self.install_result()
        self.app.tabs.select(self.app.chart)
        for width, height in ((1040, 700), (760, 520)):
            self.app.geometry(f"{width}x{height}+20000+20000")
            self.app.update()
            self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), (width, height))
            self.assertGreater(self.app.canvas.winfo_width(), width - 90)
            self.assertGreater(self.app.canvas.winfo_height(), 200)
            for frame in (self.app.query_box.master, self.app.live_button.master):
                for widget in frame.winfo_children():
                    self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), frame.winfo_width())

    def test_chart_modes_and_indicator_can_be_switched(self):
        self.install_result()
        self.app.chart_mode.set("分时")
        self.app.daily_range.set("120")
        self.app.chart_indicator.set("MACD")
        self.app.show_vwap.set(False)
        self.app.chart_changed()
        self.assertEqual(self.app.preferences["chart_mode"], "分时")
        self.assertEqual(self.app.preferences["daily_range"], 120)
        self.mock_save.assert_called()

    def test_direction_phase_minimum_lot_filters_never_mutate_export_rows(self):
        result = self.install_result()
        template = {"时间": "10:00:00", "价格": 10, "记录性质": "成交记录", "计入主交易统计": 1,
                    "交易阶段": "连续竞价", "金额": None, "估算金额": 100000}
        result["details"] = [dict(template, **{"方向": "买盘", "成交量": 100}),
                             dict(template, **{"方向": "卖盘", "成交量": 200}),
                             dict(template, **{"方向": "买盘", "成交量": 10})]
        original = copy.deepcopy(result["details"])
        self.app.direction_filter.set("买盘")
        self.app.phase_filter.set("主交易")
        self.app.minimum_lots.set("50")
        self.app.filter_details()
        self.assertEqual(len(self.app.trees["detail"].get_children()), 1)
        self.assertEqual(result["details"], original)
        self.assertIn("1 / 3", self.app.detail_count.get())

    def test_invalid_minimum_lots_has_meaningful_message(self):
        self.install_result()
        self.app.minimum_lots.set("bad")
        self.app.filter_details()
        self.assertIn("请输入", self.app.detail_count.get())

    def test_favorites_and_recents_use_bounded_local_preferences(self):
        self.install_result()
        self.app.remember_query()
        self.assertEqual(self.app.preferences["recent_queries"], ["301307"])
        self.app.toggle_favorite()
        self.assertEqual(self.app.preferences["watchlist"], ["301307"])
        self.app.toggle_favorite()
        self.assertEqual(self.app.preferences["watchlist"], [])
        self.mock_save.assert_called()

    def test_lightweight_refresh_does_not_request_details_before_thirty_seconds(self):
        self.install_result()
        self.app.live_enabled = True
        self.app.live_generation = 2
        self.app.last_details_refresh = time.monotonic()
        with patch.object(s.threading, "Thread") as thread:
            self.app.live_tick()
        args = thread.call_args.kwargs["args"]
        self.assertFalse(args[2])
        self.assertEqual(self.app.live_inflight_generation, 2)

    def test_details_are_requested_when_refresh_is_due(self):
        self.install_result()
        self.app.live_enabled = True
        self.app.last_details_refresh = 0
        with patch.object(s.threading, "Thread") as thread:
            self.app.live_tick()
        targets = [call.kwargs["target"] for call in thread.call_args_list]
        self.assertIn(self.app.details_worker, targets)
        self.assertIn(self.app.live_worker, targets)
        self.assertTrue(self.app.details_busy)

    def test_partial_live_failure_keeps_valid_quote_and_old_same_day_minutes(self):
        result = self.install_result()
        original_minutes = copy.deepcopy(result["intraday"])
        original_details = copy.deepcopy(result["details"])
        raw = raw_fixture()["quote"]
        raw["data"]["f43"] = 11
        raw["data"]["f86"] = "20260930153100"
        self.app._live_errors = {"intraday": "网络断开"}
        self.app._detail_attempted = False
        self.app.apply_live((raw, {}, {}, "15:31:00"))
        self.assertEqual(result["quote"]["最新价"], 11)
        self.assertEqual(result["intraday"], original_minutes)
        self.assertEqual(result["details"], original_details)
        self.assertEqual(result["errors"]["实时分时"], "网络断开")

    def test_new_day_never_relabels_old_minutes_or_details(self):
        result = self.install_result()
        raw = raw_fixture()["quote"]
        raw["data"]["f86"] = "20261001093000"
        self.app._detail_attempted = False
        with patch.object(self.app, "start_fetch") as resync:
            self.app.apply_live((raw, {}, {}, "09:30:00"))
        resync.assert_called_once_with(resume_live=False)
        self.assertEqual(result["intraday"], [])
        self.assertEqual(result["details"], [])
        self.assertEqual(self.app.last_details_refresh, 0)
        self.assertIn("旧日明细已清空", result["details_meta"]["completeness_note"])
        self.assertTrue(self.app.history_resync_pending)
        self.assertEqual(result["quote"]["行情时间"], "20260930153000")

    def test_live_volume_changes_invalidate_previous_detail_reconciliation(self):
        result = self.install_result()
        raw = raw_fixture()["quote"]
        raw["data"]["f47"] = 200
        raw["data"]["f86"] = "20260930153100"
        self.app._detail_attempted = False
        self.app.apply_live((raw, {}, {}, "15:31:00"))
        self.assertFalse(result["details_meta"]["volume_reconciled"])
        self.assertFalse(result["details_meta"]["final_complete"])
        self.assertIn("不是同一时点", result["details_meta"]["completeness_note"])

    def test_export_defaults_to_original_project_code_folder_and_copies_snapshot(self):
        result = self.install_result()
        with patch.object(s.threading, "Thread") as thread:
            self.app.do_export()
        snapshot, destination = thread.call_args.kwargs["args"]
        self.assertEqual(destination, PROJECT_ROOT / "导出数据" / "301307")
        self.assertIsNot(snapshot, result)
        self.assertTrue(self.app.export_busy)
        self.assertEqual(str(self.app.export["state"]), "disabled")
        self.assertFalse(thread.call_args.kwargs["daemon"])

    def test_wrong_code_and_invalid_prices_never_replace_current_quote(self):
        result = self.install_result()
        before = copy.deepcopy(result)
        for code, price in (("300059", 11), ("301307", 0), ("301307", -5), ("301307", float("nan"))):
            raw = {"data": {"f57": code, "f43": price, "f86": "20260930153100"}}
            self.app.apply_live((raw, {}, {}, "15:31:00"))
            self.assertEqual(result, before)

    def test_truthy_empty_wrappers_keep_valid_same_day_records(self):
        result = self.install_result()
        minutes = copy.deepcopy(result["intraday"])
        details = copy.deepcopy(result["details"])
        raw = raw_fixture()["quote"]
        raw["data"]["f86"] = "20260930153100"
        self.app._detail_attempted = True
        self.app.apply_live((raw, {"data": {"trends": []}}, {"data": {"details": []}}, "15:31:00"))
        self.assertEqual(result["intraday"], minutes)
        self.assertEqual(result["details"], details)
        self.assertIn("实时分时", result["errors"])
        self.assertIn("实时明细", result["errors"])

    def test_source_variant_closing_auction_matches_phase_filter(self):
        result = self.install_result()
        result["details"][0]["交易阶段"] = "收盘集合竞价（腾讯汇总时间）"
        self.app.phase_filter.set("收盘集合竞价")
        self.app.filter_details()
        self.assertEqual(len(self.app.trees["detail"].get_children()), 1)

    def test_duplicate_same_symbol_query_does_not_spawn_another_fetch(self):
        with patch.object(s.threading, "Thread") as thread:
            self.app.start_fetch()
            self.app.start_fetch()
            self.assertEqual(thread.call_count, 1)
            self.app.query.set("300059")
            self.app.start_fetch()
            self.assertEqual(thread.call_count, 2)

    def test_export_cannot_use_old_stock_while_new_query_is_pending(self):
        self.install_result()
        self.app.pending_query = ("sz", "300059")
        with patch.object(s.threading, "Thread") as thread:
            self.app.do_export()
        thread.assert_not_called()
        self.assertIn("等待一致", self.app.status.get())

    def test_independent_details_reconcile_against_latest_quote_volume(self):
        result = self.install_result()
        result["quote"]["成交量(手)"] = 200
        self.app.apply_details(raw_fixture()["details"], "2026-09-30")
        self.assertEqual(result["details_meta"]["main_volume_difference_lots"], -100)
        self.assertFalse(result["details_meta"]["final_complete"])

    def test_old_day_independent_details_are_discarded(self):
        result = self.install_result()
        before = copy.deepcopy(result["details"])
        self.app.apply_details({"data": {"details": ["09:25:00,99,100,1,2,0"]}}, "2026-09-29")
        self.assertEqual(result["details"], before)

    def test_history_table_note_uses_actual_adjustment(self):
        result = self.install_result()
        for row in result["daily_indicators"]:
            row["复权口径"] = "后复权"
        self.app.render_daily(result)
        self.assertIn("后复权", self.app.daily_note.get())

    def test_slow_details_do_not_delay_lightweight_quote_event(self):
        self.install_result()
        self.app.live_enabled = True
        self.app.last_details_refresh = 0
        self.app.live_generation = 4
        gate = threading.Event()
        self.addCleanup(gate.set)
        client = MagicMock()
        client.quote_snapshot.return_value = raw_fixture()["quote"]
        client.tencent_intraday.return_value = {"data": {"trends": ["09:30,10,0,100,100000"]}, "meta": {"trade_date": "2026-09-30"}}
        def delayed_details(_identity):
            gate.wait(2)
            return raw_fixture()["details"]
        client.deals.side_effect = delayed_details
        with patch.object(s, "Client", return_value=client):
            self.app.live_tick()
            kind, payload = self.app.events.get(timeout=1)
            self.assertEqual(kind, "live")
            self.assertEqual(payload[0], 4)
            self.assertFalse(gate.is_set())
            gate.set()
            kind, payload = self.app.events.get(timeout=1)
            self.assertEqual(kind, "details")

    def test_pending_source_history_cannot_complete_new_day_resync(self):
        result = self.install_result()
        result["daily_history"] = result["daily_history"][:1]
        result["history_sync"] = {"status": "pending", "daily_source_date": "2026-09-29", "quote_date": "2026-09-30"}
        self.app.history_resync_pending = True
        self.app.history_resync_target = "2026-09-30"
        self.app.history_resync_code = "301307"
        self.app.resume_live_after_fetch = True
        self.app.fetch_generation = 1
        self.app.events.put(("result", (1, result)))
        self.app.after_cancel(self.app.poll_job)
        self.app.poll()
        self.assertTrue(self.app.history_resync_pending)
        self.assertFalse(self.app.live_enabled)
        self.assertEqual(str(self.app.export["state"]), "disabled")

    def test_switching_stock_does_not_inherit_previous_stock_resync_target(self):
        self.install_result()
        self.app.history_resync_pending = True
        self.app.history_resync_target = "2026-10-01"
        self.app.history_resync_code = "301307"
        self.app.query.set("300059")
        with patch.object(s.threading, "Thread"):
            self.app.start_fetch()
        self.assertFalse(self.app.history_resync_pending)
        self.assertEqual(self.app.history_resync_target, "")

    def test_stale_pending_response_never_lowers_existing_resync_target(self):
        result = self.install_result()
        result["history_sync"] = {"status": "pending", "daily_source_date": "2026-09-29", "quote_date": "2026-09-30"}
        self.app.history_resync_pending = True
        self.app.history_resync_target = "2026-10-01"
        self.app.history_resync_code = "301307"
        self.app.fetch_generation = 1
        self.app.events.put(("result", (1, result)))
        self.app.after_cancel(self.app.poll_job)
        self.app.poll()
        self.assertEqual(self.app.history_resync_target, "2026-10-01")
        self.assertTrue(self.app.history_resync_pending)

    def test_cross_day_resync_does_not_schedule_an_orphan_refresh_timer(self):
        self.install_result()
        self.app.live_enabled = True
        self.app.live_generation = 3
        self.app.live_inflight_generation = 3
        raw = raw_fixture()["quote"]
        raw["data"]["f86"] = "20261001093000"
        self.app.events.put(("live", (3, raw, {}, {}, "09:30:00", 2, {}, False)))
        self.app.after_cancel(self.app.poll_job)
        with patch.object(s.threading, "Thread"):
            self.app.poll()
        self.assertFalse(self.app.live_enabled)
        self.assertIsNone(self.app.live_job)

    def test_quality_rows_and_quote_time_are_visible(self):
        self.install_result()
        self.assertGreater(len(self.app.trees["quality"].get_children()), 10)
        self.assertIn("2026-09-30", self.app.snapshot_info.get())


class LiveWorkerRegressions(unittest.TestCase):
    def test_lightweight_worker_never_calls_detail_interface(self):
        app = SimpleNamespace(events=queue.Queue())
        client = MagicMock()
        client.quote_snapshot.return_value = {"data": {"f43": 10}}
        client.tencent_intraday.return_value = {"data": {"trends": []}}
        with patch.object(s, "Client", return_value=client):
            s.App.live_worker(app, s.parse_query("301307"), 8, False)
        kind, payload = app.events.get_nowait()
        self.assertEqual((kind, payload[0], payload[7]), ("live", 8, False))
        client.deals.assert_not_called()
        client.quote_snapshot.assert_called_once_with(s.parse_query("301307"), include_precision=False)

    def test_intraday_failure_does_not_discard_successful_quote(self):
        app = SimpleNamespace(events=queue.Queue())
        client = MagicMock()
        client.quote_snapshot.return_value = {"data": {"f43": 10}}
        client.tencent_intraday.side_effect = s.SpiderError("分钟失败")
        with patch.object(s, "Client", return_value=client):
            s.App.live_worker(app, s.parse_query("301307"), 8, False)
        kind, payload = app.events.get_nowait()
        self.assertEqual(kind, "live")
        self.assertEqual(payload[6]["intraday"], "分钟失败")

    def test_quote_failure_preserves_existing_result(self):
        app = SimpleNamespace(events=queue.Queue())
        client = MagicMock()
        client.quote_snapshot.side_effect = s.SpiderError("行情失败")
        with patch.object(s, "Client", return_value=client):
            s.App.live_worker(app, s.parse_query("301307"), 9, False)
        kind, payload = app.events.get_nowait()
        self.assertEqual((kind, payload[0], payload[1]), ("live_error", 9, "行情失败"))


if __name__ == "__main__":
    unittest.main()
