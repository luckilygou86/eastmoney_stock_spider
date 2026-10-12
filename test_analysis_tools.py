"""Offline checks for truthful, source-aware dashboard observations."""
import copy
import json
import math
import unittest
from datetime import date, timedelta

from analysis_tools import build_quality_checks, summarize_dashboard


def fixture(count=65, *, flat=False):
    daily = []
    start = date(2026, 7, 1)
    for i in range(count):
        close = 10.0 if flat else 10.0 + i * 0.02
        row = {"日期": (start + timedelta(days=i)).isoformat(), "开盘": close,
               "收盘": close, "最高": close + 0.1, "最低": close - 0.1,
               "成交量": 100, "成交额": 100000,
               "可用历史换手率(%)": 0.8, "不复权开盘": close,
               "不复权收盘": close, "不复权最高": close + 0.1,
               "不复权最低": close - 0.1,
               "DIF": 0.05, "DEA": 0.03, "MACD": 0.04,
               "RSI6": 75, "RSI12": 65}
        for period in (5, 10, 20, 60):
            row[f"MA{period}"] = close if flat else close - period * 0.005
        daily.append(row)
    last_date = daily[-1]["日期"] if daily else "2026-09-04"
    return {
        "identity": {"code": "301307", "name": "fixture"},
        "fetched_at": last_date + "T16:00:00",
        "quote": {"最新价": daily[-1]["收盘"] if daily else 10,
                  "行情时间": last_date.replace("-", "") + "153000",
                  "成交量(手)": 100, "成交额": 100000},
        "daily_history": daily, "daily_indicators": daily,
        "benchmark_history": [{"日期": last_date, "收盘": 1000}],
        "benchmark_name": "测试基准", "sources": {"daily": "offline source"},
        "intraday": [{"时间": "09:30", "交易日期": last_date, "价格": 10,
                      "成交量": 60, "成交额": 60000, "计入主交易统计": 1,
                      "交易阶段": "连续竞价"},
                     {"时间": "15:00", "交易日期": last_date, "价格": 10,
                      "成交量": 40, "成交额": 40000, "计入主交易统计": 1,
                      "交易阶段": "收盘集合竞价"},
                     {"时间": "15:10", "交易日期": last_date, "价格": 10,
                      "成交量": 12, "成交额": 12000, "计入主交易统计": 0,
                      "交易阶段": "盘后定价交易"}],
        "details": [{"时间": "09:24:30", "交易日期": last_date, "价格": 10,
                     "成交量": 500, "金额": None, "计入主交易统计": 0,
                     "记录性质": "行情快照", "交易阶段": "开盘集合竞价"},
                    {"时间": "09:30:01", "交易日期": last_date, "价格": 10,
                     "成交量": 100, "金额": None, "估算金额": 100000,
                     "计入主交易统计": 1, "记录性质": "成交记录", "交易阶段": "连续竞价"},
                    {"时间": "15:10:00", "交易日期": last_date, "价格": 10,
                     "成交量": 12, "金额": None, "估算金额": 12000,
                     "计入主交易统计": 0, "记录性质": "成交记录", "交易阶段": "盘后定价交易"}],
        "details_meta": {"format": "eastmoney", "interface_complete": True,
                         "final_complete": True, "volume_reconciled": True},
        "errors": {},
    }


def check(result, item):
    return next(row for row in build_quality_checks(result) if row["item"] == item)


class DashboardTests(unittest.TestCase):
    def test_empty_is_not_validated_as_complete(self):
        dashboard = summarize_dashboard({})
        self.assertIn("暂无", dashboard["summary"])
        self.assertEqual(check({}, "日线价格")["status"], "unavailable")
        self.assertEqual(check({}, "成交明细完整性")["status"], "unavailable")

    def test_does_not_mutate_source(self):
        result = fixture()
        expected = copy.deepcopy(result)
        summarize_dashboard(result)
        self.assertEqual(result, expected)

    def test_output_contract_is_json_serializable(self):
        dashboard = summarize_dashboard(fixture())
        json.dumps(dashboard, allow_nan=False, ensure_ascii=False)
        for row in dashboard["metrics"]:
            self.assertEqual(set(row), {"label", "value", "note"})
            self.assertIsInstance(row["value"], str)
        for row in dashboard["quality_checks"]:
            self.assertEqual(set(row), {"item", "status", "detail", "severity"})

    def test_flat_series_has_zero_risk_not_missing(self):
        dashboard = summarize_dashboard(fixture(flat=True))
        self.assertEqual(dashboard["risk"]["max_drawdown"], 0)
        self.assertEqual(dashboard["risk"]["annualized_volatility"], 0)
        self.assertEqual(dashboard["returns"]["20日"], 0)
        self.assertTrue(any("持平" in text for text in dashboard["observations"]))

    def test_range_is_observed_not_forecast(self):
        dashboard = summarize_dashboard(fixture())
        self.assertEqual(dashboard["range"]["window"], 20)
        self.assertTrue(any("不是未来" in row["note"] for row in dashboard["metrics"]))

    def test_short_series_has_no_macd_cross(self):
        result = fixture(10)
        result["daily_indicators"][-2]["DIF"] = -0.1
        texts = summarize_dashboard(result)["observations"]
        self.assertFalse(any("（上穿）" in text for text in texts))
        self.assertTrue(any("预热不足" in text for text in texts))

    def test_macd_cross_requires_previous_data(self):
        result = fixture(36)
        result["daily_indicators"][-2]["DIF"] = 0.01
        texts = summarize_dashboard(result)["observations"]
        self.assertTrue(any("（上穿）" in text for text in texts))

    def test_no_macd_cross_if_prior_indicator_missing(self):
        result = fixture(36)
        result["daily_indicators"][-2]["DIF"] = None
        texts = summarize_dashboard(result)["observations"]
        self.assertFalse(any("（上穿）" in text or "（下穿）" in text for text in texts))

    def test_rsi_high_zone_is_not_reversal_prediction(self):
        texts = summarize_dashboard(fixture())["observations"]
        self.assertTrue(any("较高区间" in text and "不是反转" in text for text in texts))

    def test_rsi_low_zone(self):
        result = fixture()
        result["daily_indicators"][-1]["RSI6"] = 25
        self.assertTrue(any("较低区间" in text for text in summarize_dashboard(result)["observations"]))

    def test_invalid_rsi_is_not_displayed(self):
        result = fixture()
        result["daily_indicators"][-1]["RSI6"] = 101
        self.assertFalse(any("RSI6" in text for text in summarize_dashboard(result)["observations"]))

    def test_missing_close_breaks_return_window(self):
        result = fixture()
        result["daily_history"][-10]["收盘"] = None
        dashboard = summarize_dashboard(result)
        self.assertIsNone(dashboard["returns"]["20日"])
        self.assertIsNone(dashboard["risk"]["annualized_volatility"])

    def test_partial_history_is_explicit(self):
        dashboard = summarize_dashboard(fixture(1))
        self.assertEqual(dashboard["range"]["window"], 1)
        self.assertIsNone(dashboard["risk"]["max_drawdown"])
        self.assertIsNone(dashboard["returns"]["5日"])

    def test_short_history_does_not_trust_supplied_ma20(self):
        dashboard = summarize_dashboard(fixture(3))
        self.assertTrue(any("MA20 尚未就绪" in text for text in dashboard["observations"]))

    def test_bad_date_order_disables_historical_statistics(self):
        result = fixture()
        result["daily_history"][0]["日期"] = result["daily_history"][-1]["日期"]
        dashboard = summarize_dashboard(result)
        self.assertIsNone(dashboard["returns"]["20日"])
        self.assertIsNone(dashboard["risk"]["max_drawdown"])
        self.assertIsNone(dashboard["risk"]["annualized_volatility"])

    def test_drawdown_matches_peak_to_trough(self):
        result = fixture(4)
        for row, value in zip(result["daily_history"], (10, 20, 15, 18)):
            row["收盘"] = value
        self.assertAlmostEqual(summarize_dashboard(result)["risk"]["max_drawdown"], -25)

    def test_main_stats_exclude_auction_and_after_hours(self):
        dashboard = summarize_dashboard(fixture())
        session = dashboard["session_stats"]
        self.assertEqual(session["main_details"]["volume"], 100)
        self.assertEqual(session["main_minutes"]["volume"], 100)
        self.assertEqual(session["after_hours_details"]["volume"], 12)
        self.assertEqual(session["auction_snapshots"], 1)
        self.assertEqual(check(fixture(), "明细主交易量对账")["status"], "ok")

    def test_estimated_detail_amount_is_not_exact(self):
        dashboard = summarize_dashboard(fixture())
        self.assertIsNone(dashboard["session_stats"]["main_details"]["amount"])
        self.assertEqual(check(fixture(), "明细金额覆盖")["status"], "warning")

    def test_missing_one_minute_amount_prevents_vwap(self):
        result = fixture()
        result["intraday"][0]["成交额"] = None
        dashboard = summarize_dashboard(result)
        vwap = next(row for row in dashboard["metrics"] if row["label"] == "主交易分时均价")
        self.assertEqual(vwap["value"], "未获取")
        self.assertEqual(check(result, "分时主交易金额对账")["status"], "unavailable")

    def test_source_list_and_snapshot_time_are_preserved(self):
        result = fixture()
        dashboard = summarize_dashboard(result)
        self.assertEqual(dashboard["sources"], result["sources"])
        self.assertIn("行情", dashboard["freshness"]["text"])

    def test_numeric_unix_quote_timestamp(self):
        result = fixture()
        result["quote"]["行情时间"] = 1788507000
        self.assertTrue(summarize_dashboard(result)["freshness"]["quote_date"])


class QualityTests(unittest.TestCase):
    def test_valid_ohlc(self):
        self.assertEqual(check(fixture(), "日线 OHLC 边界")["status"], "ok")

    def test_impossible_ohlc_is_error(self):
        result = fixture()
        result["daily_history"][0]["最高"] = 8
        self.assertEqual(check(result, "日线 OHLC 边界")["status"], "error")

    def test_nan_prices_are_error(self):
        result = fixture()
        result["daily_history"][-1]["收盘"] = math.nan
        self.assertEqual(check(result, "日线价格")["status"], "error")
        json.dumps(summarize_dashboard(result), allow_nan=False)

    def test_infinite_high_is_error(self):
        result = fixture()
        result["daily_history"][-1]["最高"] = math.inf
        self.assertEqual(check(result, "日线 OHLC 边界")["status"], "error")

    def test_negative_raw_price_is_error(self):
        result = fixture()
        result["daily_history"][-1]["不复权开盘"] = -5
        self.assertEqual(check(result, "日线 OHLC 边界")["status"], "error")

    def test_zero_quote_is_error(self):
        result = fixture()
        result["quote"]["最新价"] = 0
        self.assertEqual(check(result, "行情价格")["status"], "error")

    def test_missing_ohlc_is_warning_not_proven_error(self):
        result = fixture()
        result["daily_history"][-1]["开盘"] = None
        self.assertEqual(check(result, "日线 OHLC 边界")["status"], "warning")

    def test_duplicate_date(self):
        result = fixture()
        result["daily_history"][-1]["日期"] = result["daily_history"][-2]["日期"]
        self.assertEqual(check(result, "日线日期顺序")["status"], "error")

    def test_out_of_order(self):
        result = fixture()
        result["daily_history"][0], result["daily_history"][1] = result["daily_history"][1], result["daily_history"][0]
        self.assertEqual(check(result, "日线日期顺序")["status"], "error")

    def test_invalid_calendar_date(self):
        result = fixture()
        result["daily_history"][0]["日期"] = "2026-02-30"
        self.assertEqual(check(result, "日线日期顺序")["status"], "error")

    def test_missing_raw_coverage(self):
        result = fixture()
        del result["daily_history"][0]["不复权开盘"]
        self.assertEqual(check(result, "不复权价格覆盖")["status"], "warning")

    def test_estimated_daily_amount_does_not_fill_coverage(self):
        result = fixture()
        result["daily_history"][0]["成交额"] = None
        result["daily_history"][0]["估算成交额"] = 100000
        self.assertEqual(check(result, "历史成交额覆盖")["status"], "warning")

    def test_turnover_unknown_not_correct(self):
        result = fixture()
        for row in result["daily_history"]:
            row.pop("可用历史换手率(%)")
        self.assertEqual(check(result, "历史换手率覆盖")["status"], "warning")

    def test_negative_volume_is_proven_error(self):
        result = fixture()
        result["details"][1]["成交量"] = -1
        self.assertEqual(check(result, "成交量/金额非负")["status"], "error")

    def test_infinite_volume_is_proven_error(self):
        result = fixture()
        result["details"][1]["成交量"] = math.inf
        self.assertEqual(check(result, "成交量/金额非负")["status"], "error")
        self.assertIsNone(summarize_dashboard(result)["session_stats"]["main_details"]["volume"])

    def test_negative_amount_is_not_displayed_as_valid_vwap(self):
        result = fixture()
        result["intraday"][0]["成交额"] = -60000
        dashboard = summarize_dashboard(result)
        self.assertIsNone(dashboard["session_stats"]["main_minutes"]["amount"])

    def test_mismatched_dates_disable_reconciliation(self):
        result = fixture()
        result["intraday"][0]["交易日期"] = "2026-09-01"
        self.assertEqual(check(result, "行情与分时交易日期")["status"], "warning")
        self.assertEqual(check(result, "分时主交易量对账")["status"], "warning")
        self.assertEqual(check(result, "成交明细完整性")["status"], "warning")

    def test_missing_minute_dates_not_assumed_today(self):
        result = fixture()
        for row in result["intraday"]:
            row.pop("交易日期")
        self.assertEqual(check(result, "行情与分时交易日期")["status"], "unavailable")

    def test_stale_snapshot_can_be_holiday(self):
        result = fixture()
        result["fetched_at"] = "2026-10-02T10:00:00"
        row = check(result, "行情时间")
        self.assertEqual(row["status"], "warning")
        self.assertIn("节假日", row["detail"])

    def test_future_snapshot_warning(self):
        result = fixture()
        result["fetched_at"] = result["fetched_at"][:10] + "T12:00:00"
        self.assertEqual(check(result, "行情时间")["status"], "warning")

    def test_benchmark_cutoff_mismatch(self):
        result = fixture()
        result["benchmark_history"][-1]["日期"] = "2026-08-31"
        self.assertEqual(check(result, "基准指数截止日期")["status"], "warning")

    def test_provider_small_rounding_is_warning_not_error(self):
        result = fixture()
        result["quote"]["成交量(手)"] = 100.3
        row = check(result, "分时主交易量对账")
        self.assertEqual(row["status"], "warning")
        self.assertIn("舍入", row["detail"])

    def test_large_volume_difference_is_not_false_complete(self):
        result = fixture()
        result["details"][1]["成交量"] = 20
        self.assertEqual(check(result, "成交明细完整性")["status"], "warning")
        self.assertIn("不能宣称", check(result, "明细主交易量对账")["detail"])

    def test_tencent_fallback_never_complete_even_volume_matches(self):
        result = fixture()
        result["details_meta"].update(format="tencent", interface_complete=False, coverage_complete=True)
        self.assertEqual(check(result, "成交明细完整性")["status"], "warning")

    def test_complete_eastmoney_is_qualified(self):
        row = check(fixture(), "成交明细完整性")
        self.assertEqual(row["status"], "ok")
        self.assertIn("不等同", row["detail"])

    def test_detail_unknown_date_not_complete(self):
        result = fixture()
        for row in result["details"]:
            row.pop("交易日期")
        self.assertEqual(check(result, "成交明细完整性")["status"], "warning")

    def test_source_error_not_silenced(self):
        result = fixture()
        result["errors"] = {"sina": "timeout"}
        self.assertEqual(check(result, "数据源异常")["status"], "warning")
        self.assertIn("timeout", check(result, "数据源异常")["detail"])

    def test_no_after_hours_is_not_zero_guarantee(self):
        result = fixture()
        result["intraday"] = result["intraday"][:2]
        dashboard = summarize_dashboard(result)
        self.assertIsNone(dashboard["session_stats"]["after_hours_minutes"]["volume"])
        self.assertEqual(next(row["value"] for row in dashboard["metrics"] if row["label"] == "盘后定价成交量"), "未发现盘后记录")


if __name__ == "__main__":
    unittest.main()
