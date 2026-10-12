"""Offline chart regression tests.

Run ``python -m unittest test_stock_charts -v`` for the pure model tests;
``python test_stock_charts.py --tk`` also exercises a real, hidden Tk window.
No files are exported and no market endpoint is called.
"""

import copy
import sys
import unittest

from stock_charts import (
    StockChart, actual_date, build_chart_model, finite_number, map_value,
    nearest_index, padded_domain, time_seconds,
)


def minute(time="09:30", price=10, volume=2, amount=2000, date="2026-09-30", main=1):
    return {"时间": time, "交易日期": date, "价格": price, "成交量": volume, "成交额": amount, "计入主交易统计": main}


def sample_result():
    return {
        "quote": {"昨收": 10, "行情时间": "2026-09-30 15:00:03"},
        "intraday": [minute("09:30", 10, 2, 2000), minute("09:31", 12, 3, 3600)],
        "daily_indicators": [
            {"日期": "2026-09-29", "开盘": 9.8, "收盘": 10, "最高": 10.4, "最低": 9.7, "成交量": 125, "MA5": 9.9, "MA10": 9.5, "MA20": 9.0, "复权口径": "前复权", "DIF": -.2, "DEA": -.1, "MACD": -.2},
            {"日期": "2026-09-30", "开盘": 10, "收盘": 12, "最高": 12.3, "最低": 9.9, "成交量": 150, "MA5": 10.4, "MA10": 9.9, "MA20": 9.1, "复权口径": "前复权", "DIF": .3, "DEA": .1, "MACD": .4},
        ],
        "sources": {"intraday": "Tencent minute", "daily": "Eastmoney qfq"},
    }


class ChartHelpersTests(unittest.TestCase):
    def test_numbers_are_finite_and_not_boolean(self):
        for value in (None, "", "--", float("nan"), float("inf"), "-inf", True, {}):
            self.assertIsNone(finite_number(value))
        self.assertEqual(finite_number("10.5"), 10.5)
        self.assertEqual(finite_number(0), 0)

    def test_date_formats(self):
        for value in ("20260930", "2026-09-30 15:00:00", "2026/09/30", "2026-09-30T15:00:00"):
            self.assertEqual(actual_date(value), "2026-09-30")
        for value in (None, "09:30", "2026-02-30", "20261301"):
            self.assertEqual(actual_date(value), "")

    def test_time_formats_preserve_seconds(self):
        for value in ("0930", "09:30", "093000", "09:30:00", "2026-09-30 09:30:00"):
            self.assertEqual(time_seconds(value), 34200)
        self.assertEqual(time_seconds("15:00:03"), 54003)
        for value in (None, "", "25:00", "09:60", "09:30:99", "unknown"):
            self.assertIsNone(time_seconds(value))

    def test_empty_and_flat_domains(self):
        self.assertEqual(padded_domain([]), (0, 1))
        low, high = padded_domain([10, 10])
        self.assertLess(low, 10)
        self.assertGreater(high, 10)
        self.assertLess(padded_domain([0, 0])[0], 0)
        self.assertGreater(padded_domain([0, 0])[1], 0)

    def test_reference_domain_is_symmetric(self):
        low, high = padded_domain([9, 10.3], 10, True)
        self.assertAlmostEqual(10 - low, high - 10)

    def test_nonfinite_reference_cannot_corrupt_axis(self):
        low, high = padded_domain([10, float("nan")], float("inf"), True)
        self.assertLess(low, 10)
        self.assertGreater(high, 10)

    def test_map_value_supports_reverse_axis(self):
        self.assertEqual(map_value(10, (0, 20), 100, 0), 50)
        self.assertEqual(map_value(10, (10, 10), 30, 50), 40)

    def test_nearest_index_is_bounded_and_tie_stable(self):
        self.assertIsNone(nearest_index([], 4))
        self.assertEqual(nearest_index([30], 999), 0)
        self.assertEqual(nearest_index([10, 20, 30], -1), 0)
        self.assertEqual(nearest_index([10, 20, 30], 22), 1)
        self.assertEqual(nearest_index([10, 20, 30], 25), 1)
        self.assertEqual(nearest_index([10, 20, 30], 999), 2)


class ChartModelTests(unittest.TestCase):
    def test_intraday_vwap_uses_real_turnover_and_lot_conversion(self):
        model = build_chart_model(sample_result())
        self.assertEqual(model.date_label, "2026-09-30")
        self.assertEqual(model.reference, 10)
        self.assertEqual(model.points[0].vwap, 10)
        self.assertEqual(model.points[1].vwap, 11.2)

    def test_missing_amount_does_not_create_estimated_vwap(self):
        data = sample_result()
        data["intraday"][1]["成交额"] = None
        data["intraday"].append(minute("09:32", 12, 3, 3600))
        model = build_chart_model(data)
        self.assertEqual(model.points[0].vwap, 10)
        self.assertIsNone(model.points[1].vwap)
        self.assertIsNone(model.points[2].vwap)
        self.assertTrue(model.warnings)

    def test_zero_volume_does_not_divide_by_zero(self):
        data = sample_result()
        data["intraday"] = [minute(volume=0, amount=0)]
        self.assertIsNone(build_chart_model(data).points[0].vwap)

    def test_amount_with_zero_volume_is_not_used_in_average(self):
        data = sample_result()
        data["intraday"] = [minute(volume=0, amount=20), minute("09:31", volume=10, amount=10000)]
        self.assertTrue(all(point.vwap is None for point in build_chart_model(data).points))

    def test_after_hours_does_not_pollute_main_vwap(self):
        data = sample_result()
        data["intraday"].append(minute("15:15", 20, 50, 100000, main=0))
        model = build_chart_model(data)
        self.assertEqual(model.points[1].vwap, 11.2)
        self.assertIsNone(model.points[2].vwap)

    def test_latest_actual_day_is_selected_without_mixing_undated_rows(self):
        data = sample_result()
        data["intraday"].extend([minute("09:29", date="2026-09-29"), minute("09:32", date="")])
        model = build_chart_model(data)
        self.assertEqual(len(model.points), 2)
        self.assertEqual(model.date_label, "2026-09-30")
        self.assertTrue(model.warnings)

    def test_mismatched_quote_date_does_not_supply_previous_close(self):
        data = sample_result()
        data["quote"]["行情时间"] = "2026-10-01 15:00:00"
        self.assertIsNone(build_chart_model(data).reference)

    def test_unknown_intraday_date_is_not_assigned_quote_or_current_date(self):
        data = sample_result()
        for row in data["intraday"]:
            row["交易日期"] = ""
        model = build_chart_model(data)
        self.assertEqual(model.date_label, "交易日期未提供")
        self.assertIsNone(model.reference)

    def test_times_use_actual_elapsed_seconds_and_are_sorted(self):
        data = sample_result()
        data["intraday"] = [minute("13:00"), minute("11:30"), minute("09:30")]
        model = build_chart_model(data)
        self.assertEqual([p.label for p in model.points], ["09:30", "11:30", "13:00"])
        self.assertEqual(model.points[2].x - model.points[1].x, 5400)

    def test_unknown_time_uses_original_order_not_fabricated_time(self):
        data = sample_result()
        data["intraday"][0]["时间"] = "bad time"
        model = build_chart_model(data)
        self.assertEqual(model.points[0].label, "bad time")
        self.assertEqual([p.x for p in model.points], [0, 1])
        self.assertTrue(model.warnings)

    def test_nonfinite_price_and_negative_volume_are_missing(self):
        data = sample_result()
        data["intraday"] = [minute(price=float("nan"), volume=-3)]
        point = build_chart_model(data).points[0]
        self.assertIsNone(point.price)
        self.assertIsNone(point.volume)
        self.assertIsNone(point.vwap)

    def test_daily_uses_existing_indicators_and_adjusted_ohlc(self):
        model = build_chart_model(sample_result(), "日线", 60)
        self.assertEqual(model.adjustment, "前复权")
        self.assertEqual(model.source, "Eastmoney qfq")
        self.assertEqual(model.date_label, "2026-09-29 — 2026-09-30")
        self.assertEqual(model.points[-1].ma["MA5"], 10.4)
        self.assertEqual(model.points[-1].open, 10)

    def test_daily_missing_ohlc_is_not_fabricated_from_close(self):
        data = {"daily": [{"日期": "2026-09-30", "收盘": 10}]}
        point = build_chart_model(data, "日线").points[0]
        self.assertEqual(point.price, 10)
        self.assertIsNone(point.open)
        self.assertIsNone(point.high)
        self.assertIsNone(point.low)

    def test_daily_negative_volume_is_missing(self):
        point = build_chart_model({"daily": [{"日期": "2026-09-30", "收盘": 10, "成交量": -5}]}, "日线").points[0]
        self.assertIsNone(point.volume)

    def test_chart_does_not_compute_replacement_indicators(self):
        point = build_chart_model({"daily": [{"日期": "2026-09-30", "收盘": 10}]}, "日线").points[0]
        self.assertTrue(all(value is None for value in point.ma.values()))

    def test_daily_range_sorts_and_limits_real_rows(self):
        data = sample_result()
        data["daily_indicators"].reverse()
        model = build_chart_model(data, "日线", 1)
        self.assertEqual(len(model.points), 1)
        self.assertEqual(model.points[0].label, "2026-09-30")

    def test_mixed_adjustments_are_explicit(self):
        data = sample_result()
        data["daily_indicators"][0]["复权口径"] = "不复权"
        model = build_chart_model(data, "日线")
        self.assertEqual(model.adjustment, "不复权 / 前复权")
        self.assertTrue(model.warnings)

    def test_model_does_not_mutate_result(self):
        data = sample_result()
        before = copy.deepcopy(data)
        model = build_chart_model(data)
        model.points[0].row["价格"] = 999
        self.assertEqual(data, before)
        build_chart_model(data, "日线")
        self.assertEqual(data, before)

    def test_empty_data_and_invalid_mode(self):
        self.assertEqual(build_chart_model({}).points, [])
        self.assertEqual(build_chart_model({}, "日线").points, [])
        with self.assertRaises(ValueError):
            build_chart_model({}, "weekly")


def real_tk_smoke():
    """Return diagnostic counts after rendering with a real but hidden Tk root."""
    import tkinter as tk
    from datetime import date, timedelta
    root = tk.Tk()
    root.withdraw()
    chart = StockChart(root)
    chart.pack(fill="both", expand=True)
    # Explicit Canvas dimensions also work with a withdrawn Windows root.
    errors = []
    root.report_callback_exception = lambda kind, value, tb: errors.append(str(value))
    counts = {}
    try:
        for width, height in ((760, 400), (350, 230), (220, 150)):
            chart.canvas.configure(width=width, height=height)
            root.geometry(f"{width}x{height}")
            root.update_idletasks()
            chart.canvas.event_generate("<Configure>", width=width, height=height)
            # Root must be mapped briefly to allocate drawable dimensions; it
            # stays transparent and therefore does not show an interactive window.
            root.attributes("-alpha", 0.0)
            root.deiconify()
            root.update()
            for mode in ("分时", "日线"):
                chart.set_data(sample_result())
                chart.set_mode(mode)
                chart.draw()
                counts[f"{width}x{height}_{mode}"] = len(chart.canvas.find_all())
                if not chart.canvas.find_all():
                    raise AssertionError("chart rendered no canvas items")
                chart.canvas.event_generate("<Motion>", x=120, y=90)
                root.update()
            chart.set_indicator("MACD")
            chart.draw()
            chart.set_indicator("volume")
            chart.set_vwap_visible(False)
            chart.set_vwap_visible(True)
            chart.set_daily_range(30)
            for result in ({}, {"intraday": [minute()]}, {"intraday": [minute(price=None)]}, {"daily": [{"日期": "2026-09-30", "收盘": 0}]}, {"intraday": [minute(price=10), minute("09:31", price=10)]}):
                chart.set_data(result)
                chart.set_mode("分时")
                chart.set_mode("日线")
                root.update()
            dense = sample_result()
            dense["daily_indicators"] = [
                dict(dense["daily_indicators"][index % 2], 日期=(date(2025, 10, 1) + timedelta(days=index)).isoformat())
                for index in range(300)
            ]
            dense["intraday"] = [minute(f"{seconds // 3600:02}:{seconds // 60 % 60:02}", price=10 + (index % 12) * .01)
                                  for index, seconds in enumerate(list(range(34200, 41401, 60)) + list(range(46800, 54001, 60)))]
            chart.set_data(dense)
            chart.set_mode("日线")
            chart.set_daily_range(250)
            chart.set_indicator("MACD")
            chart.set_mode("分时")
            root.update()
            root.withdraw()
        if errors:
            raise AssertionError("Tk callbacks failed: " + "; ".join(errors))
        return counts
    finally:
        root.destroy()


if __name__ == "__main__":
    if "--tk" in sys.argv:
        sys.argv.remove("--tk")
        print("Real Tk smoke:", real_tk_smoke())
    unittest.main()
