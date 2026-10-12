"""Offline regression checks; all export fixtures use disposable temp folders."""
import copy
import csv
import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
import queue
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

source = Path(sys.argv.pop(1)) if len(sys.argv) > 1 and Path(sys.argv[1]).name == "stock_spider.py" else Path(__file__).with_name("stock_spider.py")
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("stock_spider", source)
s = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = s
spec.loader.exec_module(s)


def raw_fixture(**overrides):
    result = {
        "identity": {"code": "301307", "market": "sz", "secid": "0.301307"},
        "fetched_at": "2026-09-30T16:00:00",
        "quote": {"data": {"f43": 10, "f47": 100, "f86": "20260930153000"}},
        "details": {"data": {"details": ["09:25:00,10,100,1,2,0"]},
                    "meta": {"format": "eastmoney", "complete": True}},
        "daily": {}, "intraday": {}, "billboard": {},
    }
    result.update(overrides)
    return result


def daily_fixture(closes):
    return [{"日期": f"2026-09-{i + 1:02d}", "开盘": c, "收盘": c,
             "最高": c + 1, "最低": c - 1, "成交量": 100}
            for i, c in enumerate(closes)]


class FixtureClient(s.Client):
    """Exercise the real parallel assembly without making any network requests."""
    def __init__(self):
        super().__init__(pause=0)
        self.barrier = None
    def tencent_quote(self, identity):
        if self.barrier: self.barrier.wait(timeout=2)
        return {"data": {"f57": identity.code, "f43": 10, "f47": 100,
                         "f48": 100000, "f86": "20260930153000", "f46": 10, "f44": 11, "f45": 9}}
    def tencent_intraday(self, identity):
        if self.barrier: self.barrier.wait(timeout=2)
        return {"data": {"trends": ["09:30,10,0,100,100000"]}, "meta": {"trade_date": "2026-09-30"}}
    def eastmoney_quote_precision(self, identity):
        return {"data": {"f57": identity.code, "f43": 10, "f47": 100,
                         "f48": 100000, "f86": "20260930153000", "f116": 1000000, "f117": 1000000}}
    def eastmoney_details(self, identity): return raw_fixture()["details"]
    def tencent_deals(self, identity):
        return {"data": {"details": ["09:25:00,10,100,B,100000"]},
                "meta": {"format": "tencent", "complete": False, "coverage_complete": True}}
    def eastmoney_kline(self, identity):
        return {"data": {"klines": ["2026-09-29,10,10,11,9,100,100000,1", "2026-09-30,10,10,11,9,100,100000,1"]}}
    def tencent_daily(self, identity, include_raw=True):
        return {"data": {"klines": ["2026-09-29,10,10,11,9,100,", "2026-09-30,10,10,11,9,100,"]},
                "meta": {"raw_by_date": self.tencent_raw_daily(identity)}}
    def tencent_raw_daily(self, identity):
        return {date: {"不复权开盘": 10, "不复权收盘": 10, "不复权最高": 11, "不复权最低": 9}
                for date in ("2026-09-29", "2026-09-30")}
    def sina_daily_exact(self, identity):
        return {date: {"精确成交额": 100000, "历史流通股本(股)": 1000000,
                       "精确历史换手率(%)": 1, "新浪成交量(股)": 10000}
                for date in ("2026-09-29", "2026-09-30")}
    def benchmark_daily(self, identity): return "fixture index", self.eastmoney_kline(identity)
    def eastmoney_billboard(self, identity): return {"result": {"data": []}}


class FakeCanvas:
    def __init__(self, width=500, height=250):
        self.width, self.height = width, height
        self.lines, self.ovals = [], []
    def delete(self, *_): pass
    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def create_text(self, *_, **__): pass
    def create_line(self, *coords, **kwargs):
        if len(coords) < 4:
            raise s.tk.TclError("wrong # coordinates: expected at least 4, got 2")
        self.lines.append(coords)
    def create_oval(self, *coords, **kwargs): self.ovals.append(coords)


class FakeTree:
    def __init__(self):
        self.items, self.insertions, self.deletions = {}, 0, 0
    def get_children(self): return tuple(self.items)
    def delete(self, *ids):
        self.deletions += len(ids)
        for iid in ids: self.items.pop(iid, None)
    def insert(self, parent, position, values, iid=None):
        self.insertions += 1
        iid = iid or f"item{self.insertions}"
        self.items[iid] = tuple(values)
        return iid
    def item(self, iid, option=None, **kwargs):
        if "values" in kwargs: self.items[iid] = tuple(kwargs["values"])
        return self.items[iid] if option == "values" else {"values": self.items[iid]}


class RegressionTests(unittest.TestCase):
    def test_parallel_pipeline_preserves_all_primary_feeds(self):
        c = FixtureClient()
        c.barrier = threading.Barrier(2)
        result = c.fetch(s.parse_query("301307"))
        self.assertEqual(len(result["daily_history"]), 2)
        self.assertEqual(result["daily_indicators"][0]["历史行情换手率(%)"], 1)
        self.assertEqual(result["errors"], {})
        self.assertTrue(result["details_meta"]["volume_reconciled"])
    def test_parallel_pipeline_fallback_retains_exact_amount(self):
        c = FixtureClient()
        with patch.object(c, "eastmoney_kline", side_effect=s.SpiderError("daily offline")), \
             patch.object(c, "eastmoney_details", side_effect=s.SpiderError("details offline")):
            result = c.fetch(s.parse_query("301307"))
        self.assertEqual(len(result["daily_history"]), 2)
        self.assertEqual(result["daily_indicators"][0]["成交额"], 100000)
        self.assertFalse(result["details_meta"]["final_complete"])
        self.assertIn("eastmoney_daily", result["errors"])
    def test_stale_history_not_extended_with_new_raw_quote(self):
        c = FixtureClient()
        with patch.object(c, "eastmoney_kline", return_value={"data": {"klines": ["2026-09-29,10,10,11,9,100,100000,1"]}}):
            result = c.fetch(s.parse_query("301307"))
        self.assertEqual(result["history_sync"]["status"], "pending")
        self.assertEqual(result["daily_history"][-1]["日期"], "2026-09-29")
        self.assertIn("daily_adjustment_sync", result["errors"])
    def test_pending_adjustment_history_not_mutated(self):
        result = {"daily": daily_fixture([10]), "quote": {"行情时间": "20260902100000", "最新价": 5},
                  "history_sync": {"status": "pending"}}
        before = copy.deepcopy(result["daily"])
        s.sync_current_daily(result)
        self.assertEqual(result["daily"], before)
    def test_parallel_pipeline_total_core_failure_is_not_success(self):
        c = FixtureClient()
        methods = ("tencent_quote", "tencent_intraday", "eastmoney_details", "tencent_deals",
                   "eastmoney_kline", "tencent_daily", "yahoo_daily", "eastmoney_billboard")
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in methods:
                stack.enter_context(patch.object(c, name, side_effect=s.SpiderError("offline")))
            with self.assertRaises(s.SpiderError): c.fetch(s.parse_query("301307"))
    def test_windows_fallback_has_bounded_wait(self):
        with patch.object(s.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b'{}')) as run:
            s.Client._powershell_json("https://example.invalid", "https://example.invalid", timeout=5)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertIn("-TimeoutSec 5", run.call_args.args[0][-1])
    def test_tencent_total_outage_stops_after_first_failed_batch(self):
        c = s.Client(pause=0)
        with patch.object(c, "text", side_effect=s.SpiderError("network unavailable")) as request:
            with self.assertRaises(s.SpiderError):
                c.tencent_deals(s.parse_query("301307"))
        self.assertEqual(request.call_count, 8 * 3)
    def test_tencent_malformed_response_not_empty_tail(self):
        c = s.Client(pause=0)
        with patch.object(c, "text", return_value="<html>upstream error</html>"):
            with self.assertRaises(s.SpiderError):
                c.tencent_deals(s.parse_query("301307"))
    def test_tencent_quote_rejects_invalid_price_code_and_time(self):
        c = s.Client(pause=0)
        for code, price, stamp in (("300059", "10", "20260930150000"),
                                   ("301307", "0", "20260930150000"),
                                   ("301307", "NaN", "20260930150000"),
                                   ("301307", "10", "invalid")):
            fields = [""] * 50
            fields[2], fields[3], fields[30] = code, price, stamp
            with patch.object(c, "text", return_value='v="' + "~".join(fields) + '";'):
                with self.assertRaises(s.SpiderError):
                    c.tencent_quote(s.parse_query("301307"))
    def test_light_quote_skips_optional_precision_network_wait(self):
        c = s.Client(pause=0)
        with patch.object(c, "tencent_quote", return_value=raw_fixture()["quote"]), \
             patch.object(c, "eastmoney_quote_precision") as precise:
            result = c.quote_snapshot(s.parse_query("301307"), include_precision=False)
        precise.assert_not_called()
        self.assertTrue(result["meta"]["precision_deferred"])
    def test_full_quote_still_requests_precision(self):
        c = s.Client(pause=0)
        with patch.object(c, "tencent_quote", return_value=raw_fixture()["quote"]), \
             patch.object(c, "eastmoney_quote_precision", side_effect=s.SpiderError("offline")) as precise:
            result = c.quote_snapshot(s.parse_query("301307"))
        precise.assert_called_once()
        self.assertFalse(result["meta"]["precision_matched"])
    def test_tencent_late_network_outage_retains_partial_not_complete(self):
        c = s.Client(pause=0)
        def response(url):
            page = int(url.rsplit("=", 1)[1])
            if page >= 8:
                raise s.SpiderError("network lost")
            records = "|".join(f"{page * 70 + i}/09:30:00/10/0/1/1000/B" for i in range(70))
            return f'v=[{page},"{records}"]'
        with patch.object(c, "text", side_effect=response) as request:
            result = c.tencent_deals(s.parse_query("301307"))
        self.assertEqual(len(result["data"]["details"]), 560)
        self.assertFalse(result["meta"]["coverage_complete"])
        self.assertTrue(result["meta"]["network_interrupted"])
        self.assertLessEqual(request.call_count, 8 + 16 * 3)
    def test_tencent_high_activity_not_capped_at_64_pages(self):
        c = s.Client(pause=0)
        def response(url):
            page = int(url.rsplit("=", 1)[1])
            size = 70 if page < 65 else 3 if page == 65 else 0
            records = "|".join(f"{page * 70 + i}/09:30:00/10/0/1/1000/B" for i in range(size))
            return f'v=[{page},"{records}"]'
        with patch.object(c, "text", side_effect=response):
            result = c.tencent_deals(s.parse_query("301307"))
        self.assertEqual(len(result["data"]["details"]), 65 * 70 + 3)
        self.assertTrue(result["meta"]["coverage_complete"])
    def test_benchmark_skips_unneeded_raw_companion(self):
        c = s.Client()
        with patch.object(c, "tencent_daily", return_value={"data": {}}) as fetch:
            c.benchmark_daily(s.parse_query("301307"))
        self.assertFalse(fetch.call_args.kwargs["include_raw"])
    def test_raw_daily_uses_one_request(self):
        c = s.Client()
        payload = {"data": {"sz301307": {"day": [["2026-09-30", "10", "11", "12", "9", "100"]]}}}
        with patch.object(c, "json", return_value=payload) as fetch:
            rows = c.tencent_raw_daily(s.parse_query("301307"))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(rows["2026-09-30"]["不复权收盘"], 11)
    def test_tencent_index_daily_uses_one_request(self):
        c = s.Client()
        payload = {"data": {"sz399006": {"day": [["2026-09-30", "10", "11", "12", "9", "100"]]}}}
        with patch.object(c, "json", return_value=payload) as fetch:
            rows = c.tencent_daily(s.parse_query("sz399006"), include_raw=False)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(rows["data"]["klines"]), 1)
    def test_unix_quote_timestamp(self):
        value = datetime(2026, 9, 30, 15, 30, tzinfo=s.timezone(s.timedelta(hours=8))).timestamp()
        self.assertEqual(s.market_timestamp(value), "20260930153000")
    def test_quote_precision_rejects_other_date(self):
        base = {"f57": "301307", "f86": "20260930150000", "f47": 100, "f43": 10, "f48": 1}
        other = {**base, "f86": "20260929150000", "f48": 999}
        self.assertFalse(s.merge_quote_precision(base, other))
        self.assertEqual(base["f48"], 1)
    def test_quote_precision_rejects_other_volume(self):
        base = {"f57": "301307", "f86": "20260930150000", "f47": 100, "f43": 10, "f48": 1}
        self.assertFalse(s.merge_quote_precision(base, {**base, "f47": 101, "f48": 999}))
        self.assertEqual(base["f48"], 1)
    def test_quote_precision_accepts_matched_snapshot(self):
        base = {"f57": "301307", "f86": "20260930150000", "f47": 100, "f43": 10, "f48": 1}
        self.assertTrue(s.merge_quote_precision(base, {**base, "f48": 100001.23}))
        self.assertEqual(base["f48"], 100001.23)
    def test_tencent_delayed_closing_auction(self):
        r = raw_fixture(details={"data": {"details": ["15:00:01,55.61,527,M,2930647"]}, "meta": {"format": "tencent"}})
        row = s.normalize(r)["details"][0]
        self.assertEqual(row["计入主交易统计"], 1)
        self.assertNotEqual(row["交易阶段"], "盘后定价交易")
    def test_1501_is_not_assumed_after_hours(self):
        self.assertNotEqual(s.detail_trading_phase("15:01:00")[0], "盘后定价交易")
    def test_1505_is_after_hours(self):
        self.assertEqual(s.detail_trading_phase("15:05:00")[0], "盘后定价交易")
    def test_benchmark_missing_latest_day_is_not_earlier_return(self):
        daily = daily_fixture(list(range(10, 35)))
        result = {"daily_indicators": s.enrich_daily_indicators(daily), "benchmark_history": daily[:-1]}
        self.assertIsNone(s.build_analysis(result)["相对强弱"]["超额收益(百分点)"]["20日"])
    def test_query_worker_tags_generation(self):
        app = SimpleNamespace(events=queue.Queue())
        with patch.object(s.Client, "fetch", return_value={"stock": "A"}):
            s.App.fetch_worker(app, s.parse_query("301307"), 7)
        kind, payload = app.events.get_nowait()
        self.assertEqual((kind, payload[0]), ("result", 7))
    def test_stale_query_event_is_ignored(self):
        events = queue.Queue()
        events.put(("result", (1, {"identity": {"code": "old"}})))
        app = SimpleNamespace(events=events, fetch_generation=2, result="current",
                              after=lambda *_: None, poll=lambda: None)
        s.App.poll(app)
        self.assertEqual(app.result, "current")
    def test_only_matching_refresh_releases_busy(self):
        events = queue.Queue()
        events.put(("live_error", (1, "old request", 5)))
        app = SimpleNamespace(events=events, live_inflight_generation=2, live_busy=True,
            live_enabled=False, after=lambda *_: None, poll=lambda: None)
        s.App.poll(app)
        self.assertTrue(app.live_busy)
    def test_expired_refresh_releases_its_own_busy_slot(self):
        events = queue.Queue()
        events.put(("live_error", (1, "old request", 5)))
        app = SimpleNamespace(events=events, live_inflight_generation=1, live_busy=True,
            live_enabled=False, after=lambda *_: None, poll=lambda: None)
        s.App.poll(app)
        self.assertFalse(app.live_busy)
    def test_date_mismatch_does_not_claim_final(self):
        r = raw_fixture(intraday={"meta": {"trade_date": "2026-09-29"}})
        self.assertFalse(s.normalize(r)["details_meta"]["final_complete"])
    def test_incremental_table_updates_values(self):
        tree = FakeTree()
        app = SimpleNamespace()
        s.App.fill(app, tree, [{"价格": 10.0}], ["价格"])
        s.App.fill(app, tree, [{"价格": 11.0}, {"价格": 12.0}], ["价格"])
        self.assertEqual(tree.insertions, 2)
        self.assertEqual(tree.items["item1"], ("11.00",))
    def test_daily_history_and_view_stay_limited(self):
        history = [{"日期": f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}", "收盘": 10} for i in range(61)]
        r = {"daily_history": history, "daily": history[-60:], "quote": {"行情时间": "20260401150000", "最新价": 11}}
        s.sync_current_daily(r)
        self.assertEqual(len(r["daily"]), 60)
        self.assertEqual(len(r["daily_history"]), 62)
    def test_live_stale_quote_does_not_apply(self):
        r = s.normalize(raw_fixture())
        before = copy.deepcopy(r)
        app = SimpleNamespace(result=r, status=SimpleNamespace(set=lambda *_: None))
        s.App.apply_live(app, ({"data": {"f43": 999, "f86": "20260929150000"}}, {}, {}, "15:00:00"))
        self.assertEqual(r, before)
    def test_number_rejects_nan(self): self.assertIsNone(s.num("NaN"))
    def test_number_rejects_infinity(self): self.assertIsNone(s.num(float("inf")))
    def test_number_accepts_finite(self): self.assertEqual(s.num("12.34"), 12.34)
    def test_f1_url_parse(self):
        self.assertEqual(s.parse_query("https://quote.eastmoney.com/f1.html?newcode=0.301307").code, "301307")
    def test_daily_url_parse(self):
        self.assertEqual(s.parse_query("https://quote.eastmoney.com/sz300059.html#fullScreenChart").secid, "0.300059")
    def test_auction_snapshot_not_added(self): self.assertEqual(s.detail_trading_phase("09:20:00")[2], 0)
    def test_after_hours_not_added(self): self.assertEqual(s.detail_trading_phase("15:10:00")[2], 0)
    def test_minute_cumulative_to_increment(self):
        c = s.Client(pause=0)
        with patch.object(c, "json", return_value={"data": {"sz301307": {"data": {
                "date": "20260930", "data": ["0930 10 100 100000", "0931 11 150 155000"]}}}}):
            rows = c.tencent_intraday(s.parse_query("301307"))["data"]["trends"]
        self.assertEqual(float(rows[1].split(",")[3]), 50)
        self.assertEqual(float(rows[1].split(",")[4]), 55000)
    def test_minute_date_preserved(self):
        c = s.Client(pause=0)
        with patch.object(c, "json", return_value={"data": {"sz301307": {"data": {
                "date": "20260930", "data": ["0930 10 100 100000"]}}}}):
            result = c.tencent_intraday(s.parse_query("301307"))
        self.assertEqual(result.get("meta", {}).get("trade_date"), "2026-09-30")
    def test_rsi_warmup(self):
        r = s.enrich_daily_indicators(daily_fixture([10, 11, 10, 12, 10, 11, 10]))
        self.assertIsNone(r[5]["RSI6"])
    def test_rsi_six_actual_changes(self):
        r = s.enrich_daily_indicators(daily_fixture([10, 11, 10, 12, 10, 11, 10]))
        self.assertAlmostEqual(r[6]["RSI6"], 50)
    def test_flat_rsi(self):
        self.assertEqual(s.enrich_daily_indicators(daily_fixture([10] * 15))[-1]["RSI12"], 50)
    def test_stale_quote_does_not_overwrite(self):
        rows = daily_fixture([8, 9])
        before = copy.deepcopy(rows)
        result = {"daily": rows, "quote": {"行情时间": "20260901150000", "最新价": 999}}
        s.sync_current_daily(result)
        self.assertEqual(rows, before)
    def test_incomplete_quote_preserves_existing_fields(self):
        result = {"daily": daily_fixture([10]), "quote": {"行情时间": "20260901150000", "最新价": 11}}
        s.sync_current_daily(result)
        self.assertEqual(result["daily"][0]["开盘"], 10)
        self.assertEqual(result["daily"][0]["成交量"], 100)
    def test_legacy_quote_time_does_not_label_other_day_turnover(self):
        r = s.enrich_daily_indicators(daily_fixture([10, 11]), quote={"行情时间": "20260901150000", "换手率(%)": 9})
        self.assertIsNone(r[-1]["行情显示换手率(%)"])
    def test_tencent_coverage_not_exact_tick_complete(self):
        r = raw_fixture(details={"data": {"details": ["09:25:00,10,100,B,100000"]},
                                 "meta": {"format": "tencent", "complete": False, "coverage_complete": True}})
        self.assertFalse(s.normalize(r)["details_meta"]["final_complete"])
    def test_volume_mismatch_not_final_complete(self):
        r = raw_fixture(quote={"data": {"f43": 10, "f47": 200, "f86": "20260930153000"}})
        self.assertFalse(s.normalize(r)["details_meta"]["final_complete"])
    def test_matching_source_volume_reconciled(self):
        self.assertTrue(s.normalize(raw_fixture())["details_meta"].get("volume_reconciled"))
    def test_empty_detail_not_complete(self):
        r = raw_fixture(details={"data": {"details": []}, "meta": {"format": "eastmoney", "complete": True}})
        self.assertFalse(s.normalize(r)["details_meta"]["interface_complete"])
    def test_primary_amount_not_overwritten(self):
        r = raw_fixture(daily={"data": {"klines": ["2026-09-30,10,10,11,9,100,100000"]},
            "meta": {"exact_by_date": {"2026-09-30": {"精确成交额": 99999}}}})
        self.assertEqual(s.normalize(r)["daily"][0]["成交额"], 100000)
    def test_provider_historical_turnover_preserved(self):
        r = raw_fixture(daily={"data": {"klines": ["2026-09-30,10,10,11,9,100,100000,4.35"]}})
        self.assertEqual(s.normalize(r)["daily"][0].get("历史行情换手率(%)"), 4.35)
    def test_single_point_chart(self):
        canvas = FakeCanvas()
        s.App.draw(SimpleNamespace(canvas=canvas), [{"价格": 10}])
        self.assertEqual(len(canvas.ovals), 1)
    def test_chart_two_points(self):
        canvas = FakeCanvas()
        s.App.draw(SimpleNamespace(canvas=canvas), [{"价格": 10}, {"价格": 11}])
        self.assertEqual(len(canvas.lines), 1)
    def test_chart_empty(self): s.App.draw(SimpleNamespace(canvas=FakeCanvas()), [])
    def test_refresh_busy_reschedules(self):
        scheduled = []
        app = SimpleNamespace(live_enabled=True, live_identity=s.parse_query("301307"),
            live_busy=True, live_job=None, after=lambda delay, callback: scheduled.append(delay), live_tick=lambda: None)
        s.App.live_tick(app)
        self.assertTrue(scheduled)
    def test_table_does_not_reinsert_unchanged_rows(self):
        tree = FakeTree()
        app = SimpleNamespace()
        s.App.fill(app, tree, [{"价格": 10.0}], ["价格"])
        s.App.fill(app, tree, [{"价格": 10.0}], ["价格"])
        self.assertEqual(tree.insertions, 1)
        self.assertEqual(tree.deletions, 0)
    def test_export_never_overwrites_previous_export(self):
        r = s.normalize(raw_fixture())
        with tempfile.TemporaryDirectory(prefix="stock-export-") as folder:
            with patch.object(s, "datetime") as dt:
                dt.now.return_value = datetime(2026, 9, 30, 16)
                first = s.export_result(r, folder)
                second = s.export_result(r, folder)
            self.assertTrue(set(first).isdisjoint(second))
    def test_export_does_not_create_extra_reports(self):
        with tempfile.TemporaryDirectory(prefix="stock-export-") as folder:
            paths = s.export_result(s.normalize(raw_fixture()), folder)
            self.assertFalse(any(p.suffix in {".md", ".svg", ".html"} for p in paths))
            payload = json.loads(next(p for p in paths if p.suffix == ".json").read_text(encoding="utf-8"))
            self.assertIn("data_quality", payload)
    def test_selftest_does_not_export_implicitly(self):
        with patch.object(s.Client, "resolve", side_effect=lambda i: i), \
             patch.object(s.Client, "fetch", return_value=s.normalize(raw_fixture())), \
             patch.object(s.Client, "health", return_value=[]), \
             patch.object(s, "export_result") as exporter, patch("builtins.print"):
            s.self_test("301307")
            exporter.assert_not_called()
    def test_quality_empty_daily_not_reported_exported(self):
        q = s.build_data_quality_rows({})
        for row in q:
            if row["数据项"] in {"历史精确成交额", "历史精确换手率"}:
                self.assertNotEqual(row["状态"], "已导出")
    def test_quality_empty_shares_and_zero_volume_not_healthy(self):
        rows = s.build_data_quality_rows({"quote": {"成交量(手)": 0}})
        by_item = {row["数据项"]: row["状态"] for row in rows}
        self.assertEqual(by_item["历史流通股本"], "源未提供")
        self.assertEqual(by_item["逐笔主交易量与行情对账"], "无法核验")
        self.assertEqual(by_item["分钟主交易量与行情对账"], "无法核验")
    def test_analysis_empty_risk_not_zero(self):
        risk = s.build_analysis({})["波动风险"]
        self.assertIsNone(risk["近120日最大回撤(%)"])
        self.assertIsNone(risk["20日年化波动率(%)"])
    def test_analysis_missing_details_not_zero_active_flow(self):
        active = s.build_analysis({})["主动成交"]
        for key in ("主交易成交量(手)", "买盘量(手)", "卖盘量(手)", "中性量(手)", "净主动量(手)"):
            self.assertIsNone(active[key])
    def test_analysis_missing_minute_amount_not_fake_vwap(self):
        result = {"intraday": [{"价格": 10, "成交量": 100, "成交额": None,
                                "计入主交易统计": 1}]}
        self.assertIsNone(s.build_analysis(result)["分时"]["主交易VWAP"])
    def test_analysis_missing_price_gap_not_skipped_for_returns(self):
        rows = daily_fixture([10, 11, 12, 12, 13, 14])
        rows[2]["收盘"] = None
        result = s.build_analysis({"daily": rows})
        self.assertIsNone(result["阶段收益(%)"]["5日"])
        self.assertIsNone(result["波动风险"]["近120日最大回撤(%)"])
    def test_analysis_unordered_dates_not_usable_returns(self):
        rows = daily_fixture([10, 11, 12, 12, 13, 14])
        rows[1], rows[2] = rows[2], rows[1]
        self.assertIsNone(s.build_analysis({"daily": rows})["阶段收益(%)"]["5日"])
    def test_analysis_missing_minute_volume_not_fake_vwap(self):
        result = {"intraday": [{"成交量": 100, "成交额": 100000, "计入主交易统计": 1},
                               {"成交量": None, "成交额": 100000, "计入主交易统计": 1}]}
        self.assertIsNone(s.build_analysis(result)["分时"]["主交易VWAP"])
    def test_legacy_quality_uses_date_aware_reconciliation(self):
        r = s.normalize(raw_fixture(intraday={"data": {"trends": ["09:30,10,0,100,100000"]},
                                               "meta": {"trade_date": "2026-09-30"}}))
        r["intraday"][0]["交易日期"] = "2026-09-29"
        r["details"][0]["交易日期"] = "2026-09-29"
        rows = {row["数据项"]: row for row in s.build_data_quality_rows(r)}
        for key in ("逐笔主交易量与行情对账", "分钟主交易量与行情对账"):
            self.assertNotEqual(rows[key]["状态"], "通过")
    def test_trade_summary_partial_amount_is_not_full_exact_total(self):
        r = {"details": [{"成交量": 1, "金额": 1000, "计入主交易统计": 1, "记录性质": "成交记录"},
                         {"成交量": None, "金额": None, "计入主交易统计": 1, "记录性质": "成交记录"}]}
        main = s.build_trade_summary_rows(r)[1]
        self.assertIsNone(main["接口精确金额"])
        self.assertIsNone(main["成交量/快照量(手)"])
        self.assertEqual(main["已知接口金额"], 1000)
        self.assertEqual(main["接口金额覆盖"], "1/2")
    def test_export_schema_and_manifest(self):
        with tempfile.TemporaryDirectory(prefix="stock-export-") as folder:
            paths = s.export_result(s.normalize(raw_fixture()), folder)
            payload = json.loads(next(p for p in paths if p.suffix == ".json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(set(payload["export_files"].values()), {p.name for p in paths})
            self.assertIn("benchmark", payload["missing_datasets"])
            self.assertIn("quality_checks", payload)
            self.assertIn("dashboard", payload)
    def test_full_pipeline_export_csv_matches_manifest(self):
        result = FixtureClient().fetch(s.parse_query("301307"))
        before = copy.deepcopy(result)
        with tempfile.TemporaryDirectory(prefix="stock-export-") as folder:
            paths = s.export_result(result, folder)
            self.assertEqual(len(paths), 5)
            snapshot = next(path for path in paths if path.suffix == ".json")
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            for key, count in payload["dataset_counts"].items():
                with (Path(folder) / payload["export_files"][key]).open(encoding="utf-8-sig", newline="") as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), count)
            self.assertEqual(payload["missing_datasets"], [])
            self.assertEqual(payload["history_sync"]["status"], "aligned")
            self.assertEqual(set(Path(folder).iterdir()), set(paths))
        self.assertEqual(result, before)
    def test_export_serialization_failure_leaves_no_batch(self):
        r = s.normalize(raw_fixture())
        r["details"][0]["价格"] = float("nan")
        with tempfile.TemporaryDirectory(prefix="stock-export-") as folder:
            with self.assertRaises(ValueError):
                s.export_result(r, folder)
            self.assertEqual(list(Path(folder).iterdir()), [])
    def test_export_publish_failure_rolls_back_own_batch(self):
        original_replace = s.os.replace
        count = 0
        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated publish failure")
            return original_replace(source, destination)
        with tempfile.TemporaryDirectory(prefix="stock-export-") as folder:
            sentinel = Path(folder) / "previous.csv"
            sentinel.touch()
            with patch.object(s.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    s.export_result(s.normalize(raw_fixture()), folder)
            self.assertEqual(list(Path(folder).iterdir()), [sentinel])
    def test_share_failure_keeps_valid_sina_amount(self):
        class Decoder:
            def eval(self, *_): pass
            def call(self, *_): return [{"date": "2026-09-30", "open": 10, "close": 10,
                                        "high": 11, "low": 9, "volume": 10000, "amount": 100000}]
        c = s.Client(pause=0)
        with patch.object(s, "py_mini_racer", SimpleNamespace(MiniRacer=Decoder)), \
             patch.object(s, "SINA_KLC_DECODER", "function d(){}"), \
             patch.object(c, "text", side_effect=['var x="abc";', s.SpiderError("share temporarily unavailable")]):
            result = c.sina_daily_exact(s.parse_query("301307"))
        self.assertEqual(result["2026-09-30"]["精确成交额"], 100000)
        self.assertIsNone(result["2026-09-30"]["精确历史换手率(%)"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
