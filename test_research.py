"""Offline financial/date/unit, public-query, export, AI and Tk regressions."""
import copy
import json
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import research_data as d
import research_analysis as a
from test_stock_ui import fixture


def research_fixture():
    code, as_of = "301307", "2026-09-30"
    periods = (("2024-06-30", 50, 5), ("2024-12-31", 100, 10), ("2025-06-30", 60, 6),
               ("2025-12-31", 130, 13), ("2026-06-30", 80, 10))
    rows = {key: [] for key in ("income", "balance", "cashflow", "financial_metrics")}
    for end, revenue, profit in periods:
        meta = {"SECURITY_CODE": code, "REPORT_DATE": end, "NOTICE_DATE": str(date.fromisoformat(end) + timedelta(days=20)),
                "UPDATE_DATE": str(date.fromisoformat(end) + timedelta(days=20)), "CURRENCY": "CNY", "ORG_TYPE": "通用"}
        rows["income"].append({**meta, "OPERATE_INCOME": revenue, "PARENT_NETPROFIT": profit,
                               "NETPROFIT": profit * 2, "DEDUCT_PARENT_NETPROFIT": profit - 1})
        rows["cashflow"].append({**meta, "NETCASH_OPERATE": profit * 3, "CONSTRUCT_LONG_ASSET": profit})
        rows["balance"].append({**meta, "TOTAL_ASSETS": 200, "TOTAL_LIABILITIES": 100, "TOTAL_PARENT_EQUITY": 80,
                                "TOTAL_CURRENT_ASSETS": 120, "TOTAL_CURRENT_LIAB": 60})
        rows["financial_metrics"].append({**meta, "ROEJQ": 15, "XSMLL": 20, "ROIC": 12})
    rows["valuation"] = [{"SECURITY_CODE": code, "TRADE_DATE": as_of, "PE_TTM": 20, "PB_MRQ": 2, "TOTAL_SHARES": 1000000}]
    rows["dividends"] = [{"SECURITY_CODE": code, "NOTICE_DATE": "2026-04-20", "EX_DIVIDEND_DATE": "2026-05-01", "ASSIGN_PROGRESS": "实施分配", "PRETAX_BONUS_RMB": 2}]
    rows["profile"] = [{"SECURITY_CODE": code, "MAIN_BUSINESS": "测试业务", "BOARD_CODE_BK_2LEVEL": "BK0448"}]
    return {"code": code, "as_of": as_of, "sources": {key: {"rows": sorted(values, key=lambda r: r.get("REPORT_DATE", ""), reverse=True),
             "source": key, "truncated": False, "note": "测试来源"} for key, values in rows.items()}, "errors": {}, "limitations": []}


def issuer_financial_fixture():
    """301307 disclosed annual/interim amounts; no live requests in tests.

    2026 interim: SZSE 1494ddae-ebd8-45a8-a42d-eb3496bd502a.PDF
    2025 annual: SZSE 5d4738d4-8fdd-4693-953c-28fef2758f0d.PDF
    Balance values remain synthetic: not an independently audited balance.
    """
    research = research_fixture()
    values = {"2026-06-30": (2194552188.06, -165551006.14, -165601906.86, 86561187.85, 258112476.79, -6.86),
              "2025-12-31": (3883152998.38, -566694036.00, -567028880.28, 82591232.25, 740458914.45, -20.31),
              "2025-06-30": (1859207255.47, -104942926.22, -105205700.66, 60078448.83, 365360179.53, -3.47),
              "2024-12-31": (3658982203.62, -163835838.33, -163561726.76, 408193606.46, 1220014026.21, -5.17),
              "2024-06-30": (1653901257.26, -18471680.04, -18031802.32, 338277115.97, 644869448.62, -0.57)}
    for key in ("income", "cashflow", "financial_metrics"):
        for row in research["sources"][key]["rows"]:
            end = row["REPORT_DATE"]
            revenue, profit, consolidated, cfo, capex, roe = values[end]
            if end == "2026-06-30":
                row.update(NOTICE_DATE="2026-08-25", UPDATE_DATE="2026-08-25")
            elif end == "2025-12-31":
                row.update(NOTICE_DATE="2026-04-24", UPDATE_DATE="2026-04-24")
            if key == "income":
                row.update(OPERATE_INCOME=revenue, PARENT_NETPROFIT=profit, NETPROFIT=consolidated)
                if end == "2026-06-30": row["DEDUCT_PARENT_NETPROFIT"] = -183514158.55
            elif key == "cashflow":
                row.update(NETCASH_OPERATE=cfo, CONSTRUCT_LONG_ASSET=capex)
            else:
                row["ROEJQ"] = roe
    return research


class FinancialAlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.result, self.research = fixture(), research_fixture()

    def analysis(self):
        return a.analyze_research(self.result, self.research)

    def test_ttm_uses_three_periods_not_interim_times_two(self):
        f = self.analysis()["fundamentals"]
        self.assertEqual(f["TTM营业收入(元)"], 150)
        self.assertEqual(f["TTM归母净利润(元)"], 17)
        self.assertEqual(f["TTM合并净利润(元)"], 34)
        self.assertEqual(f["TTM经营现金净流量(元)"], 51)
        self.assertEqual(f["TTM购建长期资产现金支出(元)"], 17)
        self.assertEqual(f["TTM简化现金余量(元)"], 34)
        self.assertEqual(f["TTM经营现金/合并净利润"], 1.5)
        self.assertEqual(f["资产负债率(%)"], 50)
        self.assertAlmostEqual(f["TTM收入同比(%)"], (150 / 110 - 1) * 100)

    def test_missing_prior_period_does_not_impute_or_annualize(self):
        self.research["sources"]["cashflow"]["rows"] = self.research["sources"]["cashflow"]["rows"][:1]
        f = self.analysis()["fundamentals"]
        self.assertIsNone(f["TTM经营现金净流量(元)"])
        self.assertIsNone(f["TTM简化现金余量(元)"])

    def test_annual_ttm_is_full_year_without_other_rows(self):
        row = self.research["sources"]["income"]["rows"][1]
        self.assertEqual(a.ttm({"2025-12-31": row}, "OPERATE_INCOME", "2025-12-31"), 130)

    def test_financial_dates_future_notice_revision_and_wrong_stock_excluded(self):
        for field, value in (("NOTICE_DATE", "2026-10-01"), ("UPDATE_DATE", "2026-10-01"),
                             ("SECURITY_CODE", "300059"), ("CURRENCY", "USD")):
            with self.subTest(field=field):
                research = research_fixture()
                research["sources"]["income"]["rows"][0][field] = value
                result = a.analyze_research(self.result, research)
                self.assertEqual(result["fundamentals"]["财务报告期"], "2025-12-31")

    def test_bank_general_balance_cash_rules_disabled(self):
        for key in ("income", "balance", "cashflow", "financial_metrics"):
            for row in self.research["sources"][key]["rows"]:
                row["ORG_TYPE"] = "银行"
        result = self.analysis()
        self.assertIsNone(result["fundamentals"]["资产负债率(%)"])
        self.assertIsNone(result["fundamentals"]["流动比率"])
        self.assertEqual(next(r for r in result["rules"] if r["检查"] == "资本投入后现金")["状态"], "不适用")

    def test_loss_and_negative_base_never_cheap_peg(self):
        self.research["sources"]["income"]["rows"][0]["PARENT_NETPROFIT"] = -50
        self.research["sources"]["valuation"]["rows"][0]["PE_TTM"] = -3
        f = self.analysis()["fundamentals"]
        self.assertIsNone(f["历史PEG"])
        self.assertIsNone(f["市值/TTM归母净利(仅正利润)"])
        for first, second in ((-1, 2), (2, -1), (1, 0), (0, 1), (None, 1)):
            self.assertIsNone(a.positive_growth(first, second))

    def test_stale_valuation_not_paired_with_current_quote(self):
        self.research["sources"]["valuation"]["rows"][0]["TRADE_DATE"] = "2026-09-29"
        f = self.analysis()["fundamentals"]
        self.assertIsNone(f["PE_TTM"])
        self.assertIsNone(f["PB_MRQ"])
        self.assertIsNone(f["总股本(股)"])

    def test_dividend_per_ten_shares_units(self):
        self.result["quote"]["最新价"] = 10
        self.assertEqual(self.analysis()["fundamentals"]["税前近365日现金股息率(%)"], 2)
        self.research["sources"]["dividends"]["truncated"] = True
        self.assertIsNone(self.analysis()["fundamentals"]["税前近365日现金股息率(%)"])

    def test_future_announced_and_old_dividend_not_current_yield(self):
        rows = self.research["sources"]["dividends"]["rows"]
        rows[0]["ASSIGN_PROGRESS"] = "董事会预案"
        rows += [{**rows[0], "ASSIGN_PROGRESS": "实施分配", "EX_DIVIDEND_DATE": "2025-01-01"},
                 {**rows[0], "ASSIGN_PROGRESS": "实施分配", "EX_DIVIDEND_DATE": "2026-10-01"}]
        self.assertEqual(self.analysis()["fundamentals"]["税前近365日现金股息率(%)"], 0)

    def test_no_financial_data_has_unknown_rules_not_passing_scores(self):
        self.research["sources"] = {}
        result = self.analysis()
        self.assertEqual(result["summary"]["通过"], 0)
        self.assertEqual(result["coverage_percent"], 0)

    def test_binding_rejects_different_stock_or_date(self):
        for key, value in (("code", "300059"), ("as_of", "2026-09-29")):
            with self.assertRaises(ValueError):
                a.analyze_research(self.result, {**self.research, key: value})

    def test_cagr_requires_four_consecutive_positive_annual_reports(self):
        rows = {f"{year}-12-31": {"OPERATE_INCOME": 100 * 1.1 ** (year - 2022)} for year in range(2022, 2026)}
        self.assertAlmostEqual(a.annual_cagr(rows, "OPERATE_INCOME"), 10)
        rows.pop("2023-12-31")
        self.assertIsNone(a.annual_cagr(rows, "OPERATE_INCOME"))
        rows["2023-12-31"] = {"OPERATE_INCOME": -1}
        self.assertIsNone(a.annual_cagr(rows, "OPERATE_INCOME"))

    def test_financial_equality_check_missing_and_mismatch(self):
        self.result["research"] = self.research
        rows = self.research["sources"]["balance"]["rows"]
        for row in rows: row["TOTAL_EQUITY"] = 100
        checks = a.research_quality_checks(self.result)
        self.assertEqual(next(r for r in checks if r["item"] == "财报资产负债恒等式")["status"], "ok")
        rows[0]["TOTAL_EQUITY"] = 50
        checks = a.research_quality_checks(self.result)
        self.assertEqual(next(r for r in checks if r["item"] == "财报资产负债恒等式")["status"], "warning")
        checks = a.research_quality_checks({**self.result, "research": {**self.research, "code": "300059"}})
        self.assertEqual(checks[0]["status"], "warning")

    def test_cashflow_equation_uses_exchange_effect_and_never_imputes_missing(self):
        self.result["research"] = self.research
        rows = self.research["sources"]["cashflow"]["rows"]
        for row in rows:
            row.update(NETCASH_OPERATE=100, NETCASH_INVEST=-50, NETCASH_FINANCE=20, RATE_CHANGE_EFFECT=-3, CCE_ADD=67)
        checks = a.research_quality_checks(self.result)
        self.assertEqual(next(r for r in checks if r["item"] == "财报现金流勾稽")["status"], "ok")
        for row in rows: row["RATE_CHANGE_EFFECT"] = None
        checks = a.research_quality_checks(self.result)
        self.assertEqual(next(r for r in checks if r["item"] == "财报现金流勾稽")["status"], "unavailable")

    def test_all_modules_readable_without_mutating_inputs(self):
        before = copy.deepcopy((self.result, self.research))
        analysis = self.analysis()
        for module in a.MODULES:
            text = a.render_module(module, self.result, self.research, analysis)
            self.assertIn("301307", text)
            self.assertIn("来源与覆盖", text)
        self.assertEqual(before, (self.result, self.research))

    def test_disclosed_loss_ttm_and_cash_proxy_are_not_missing(self):
        self.research = issuer_financial_fixture()
        self.result["daily_indicators"] = [{"日期": (date(2026, 9, 30) - timedelta(days=i)).isoformat(), "收盘": 50, "成交量": 100} for i in reversed(range(70))]
        analysis = self.analysis()
        f = analysis["fundamentals"]
        self.assertAlmostEqual(f["TTM归母净利润(元)"], -627302115.92, places=2)
        self.assertAlmostEqual(f["TTM经营现金净流量(元)"], 109073971.27, places=2)
        self.assertAlmostEqual(f["TTM购建长期资产现金支出(元)"], 633211211.71, places=2)
        self.assertAlmostEqual(f["TTM简化现金余量(元)"], -524137240.44, places=2)
        self.assertAlmostEqual(f["TTM归母净利润同比增减额(元)"], -376995031.41, places=2)
        self.assertAlmostEqual(f["TTM收入同比(%)"], 9.166234783817, places=8)
        self.assertAlmostEqual(f["本报告期收入同比(%)"], 18.03698494, places=7)
        rules = {r["检查"]: r for r in analysis["rules"]}
        for key in ("利润现金兑现", "利润成长"):
            self.assertEqual(rules[key]["状态"], "不适用")
            self.assertTrue(rules[key]["数据已取得"])
        self.assertIn("亏损扩大", rules["利润成长"]["依据"])
        self.assertEqual(analysis["data_coverage_percent"], 100)
        self.assertEqual(analysis["coverage_percent"], 80)
        self.assertEqual(analysis["summary"]["缺数据"], 0)
        self.assertEqual(f["TTM期间"], "2025-07-01 至 2026-06-30")

    def test_roe_uses_annual_threshold_not_half_year_value(self):
        for row in self.research["sources"]["financial_metrics"]["rows"]:
            row["ROEJQ"] = 8 if row["REPORT_DATE"] == "2026-06-30" else 12
        analysis = self.analysis()
        rule = next(r for r in analysis["rules"] if r["检查"] == "资本回报")
        self.assertEqual(rule["值"], 12)
        self.assertEqual(rule["状态"], "通过")
        self.assertIn("2025 全年", rule["期间"])
        self.assertIn("8.00%", rule["依据"])
        self.research["sources"]["financial_metrics"]["rows"] = self.research["sources"]["financial_metrics"]["rows"][:1]
        rule = next(r for r in self.analysis()["rules"] if r["检查"] == "资本回报")
        self.assertEqual(rule["状态"], "缺数据")

    def test_missing_ttm_and_unknown_industry_are_missing_not_inapplicable(self):
        self.research["sources"]["cashflow"]["rows"] = []
        rules = {r["检查"]: r for r in self.analysis()["rules"]}
        self.assertEqual(rules["资本投入后现金"]["状态"], "缺数据")
        self.assertFalse(rules["资本投入后现金"]["数据已取得"])
        self.research["sources"]["income"]["rows"][0]["ORG_TYPE"] = None
        self.assertEqual(next(r for r in self.analysis()["rules"] if r["检查"] == "负债约束")["状态"], "缺数据")

    def test_report_units_periods_counts_and_source_labels(self):
        self.research = issuer_financial_fixture()
        text = a.render_module("综合检查", self.result, self.research, self.analysis())
        for expected in ("-6.27亿元", "-5.24亿元", "-20.31%", "-6.86%", "9.17%", "18.04%", "2025-07-01 至 2026-06-30", "不适用 2项", "利润表", "采集日不等于行情日"):
            self.assertIn(expected, text)
        self.assertNotIn("{'通过':", text)
        self.assertNotIn("未知/不适用", text)
        self.assertNotIn("：ok", text)
        financial_text = a.render_module("财务报表", self.result, self.research, self.analysis())
        self.assertIn("TTM期间：2025-07-01 至 2026-06-30", financial_text)

    def test_partial_equality_is_warning_and_cap_rounding_is_explained(self):
        rows = self.research["sources"]["balance"]["rows"]
        for row in rows: row["TOTAL_EQUITY"] = 100
        rows[0].pop("TOTAL_EQUITY")
        self.result["quote"].update(最新价=55.61, 总市值=11711000000)
        self.research["sources"]["valuation"]["rows"][0]["TOTAL_SHARES"] = 210600000
        checks = a.research_quality_checks({**self.result, "research": self.research})
        equality = next(r for r in checks if r["item"] == "财报资产负债恒等式")
        self.assertEqual(equality["status"], "warning")
        self.assertIn("1期缺少", equality["detail"])
        cap = next(r for r in checks if r["item"] == "总市值/股本/价格口径")
        self.assertEqual(cap["status"], "ok")
        self.assertIn("-46.60万元", cap["detail"])
        self.assertIn("-0.0040%", cap["detail"])

    def test_technical_window_warmup_drawdown_and_stale_trend(self):
        rows = [{"日期": (date(2026, 7, 1) + timedelta(days=i)).isoformat(), "收盘": 100 + i, "最高": 101 + i, "最低": 99 + i, "成交量": 100} for i in range(70)]
        rows[-1]["收盘"] = 140
        self.result["daily_indicators"] = rows
        result = self.analysis()
        tech = result["technical"]
        self.assertEqual(tech["样本交易日"], 70)
        self.assertAlmostEqual(tech["历史样本最大回撤(%)"], (140 / 168 - 1) * 100)
        self.assertIsNotNone(tech["近60日年化波动率(%)"])
        self.assertEqual(next(r for r in result["rules"] if r["检查"] == "中期价格趋势")["状态"], "缺数据")

    def test_peer_median_excludes_loss_and_target_stock(self):
        self.research["sources"]["peers"] = {"rows": [
            {"SECURITY_CODE": c, "TRADE_DATE": dt, "PE_TTM": pe} for c, dt, pe in (
                ("301307", "2026-09-30", 500), ("300059", "2026-09-30", -5), ("600000", "2026-09-30", 10),
                ("600001", "2026-09-30", 20), ("600002", "2026-09-29", 1000))]}
        self.assertEqual(self.analysis()["fundamentals"]["同分类正PE样本中位数"], 15)

    def test_sector_returns_join_dates_not_row_offsets(self):
        dates = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(30)]
        self.result["daily_history"] = [{"日期": d, "收盘": 100 + i} for i, d in enumerate(dates)]
        self.research["sources"]["industry"] = {"rows": [{"日期": d, "收盘": 200 + i} for i, d in enumerate(dates) if i % 4]}
        tech = self.analysis()["technical"]
        expected_start = self.research["sources"]["industry"]["rows"][-21]
        self.assertEqual(tech["行业对齐20区间起日"], expected_start["日期"])


class ScenarioTests(unittest.TestCase):
    def test_dcf_is_fcfe_equity_value_without_debt_double_subtraction(self):
        result = a.dcf_fcfe(100, 10, 0, 10, 0)
        self.assertAlmostEqual(result["股权情景价值(元)"], 1000)
        self.assertAlmostEqual(result["每股情景值(元)"], 100)
        self.assertIn("不再扣净负债", result["限制"])

    def test_dcf_bad_inputs_rejected(self):
        for values in ((-1, 10, 0, 10, 0), (100, 0, 0, 10, 0), (100, 10, 0, 5, 5),
                       (100, 10, 80, 10, 0), (float("nan"), 10, 0, 10, 0), (True, 10, 0, 10, 0)):
            with self.assertRaises(ValueError): a.dcf_fcfe(*values)

    def test_position_risk_and_allocation_lot_rounding(self):
        result = a.position_plan(100000, 10, 9, 13, 1, 5)
        self.assertEqual(result["示例数量(股)"], 500)
        self.assertEqual(result["名义止损风险(元)"], 500)
        self.assertEqual(result["盈亏比(未计费)"], 3)
        self.assertEqual(a.position_plan(100, 10, 9, 13, 1, 5)["示例数量(股)"], 0)

    def test_bad_position_inputs_rejected(self):
        for values in ((100, 10, 11, 13, 1, 5), (100, 10, 9, 8, 1, 5), (100, 10, 9, 13, 0, 5),
                       (100, 10, 9, 13, 1, 110), (True, 10, 9, 13, 1, 5)):
            with self.assertRaises(ValueError): a.position_plan(*values)


class ResearchQueryTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.collector = d.ResearchClient(self.client)

    def test_financial_report_bounded_columns_identity_and_date(self):
        row = research_fixture()["sources"]["income"]["rows"][0]
        invalid = {**row, "NOTICE_DATE": "2026-10-01"}
        self.client.json.return_value = {"success": True, "result": {"count": 100, "data": [row, invalid, {**row, "SECURITY_CODE": "300059"}]}}
        result = self.collector.report("income", "301307", "2026-09-30")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["excluded_rows"], 2)
        self.assertTrue(result["truncated"])
        query = parse_qs(urlsplit(self.client.json.call_args.args[0]).query)
        self.assertEqual(query["pageSize"], ["80"])
        self.assertNotIn("ALL", query["columns"])
        self.assertEqual(query["filter"], ['(SECURITY_CODE="301307")'])

    def test_query_failure_never_generates_dummy_rows(self):
        for payload in ({"success": False}, {"success": True, "result": None}, {"success": True, "result": {"data": "bad"}}):
            self.client.json.return_value = payload
            with self.assertRaises(ValueError): self.collector.report("income", "301307", "2026-09-30")

    def test_dividend_inconsistent_count_cannot_create_false_zero_yield(self):
        self.client.json.return_value = {"success": True, "result": {"count": 5, "data": []}}
        self.assertTrue(self.collector.report("dividends", "301307", "2026-09-30")["truncated"])

    def test_bad_query_inputs_cannot_change_public_filters(self):
        with self.assertRaises(ValueError): self.collector.report("income", '301307")', "2026-09-30")
        with self.assertRaises(ValueError): self.collector.report("income", "301307", "2026-02-30")
        self.client.json.assert_not_called()

    def test_flow_units_future_and_cross_stock(self):
        self.client.json.return_value = {"data": {"code": "301307", "klines": [
            "2026-09-30,10,20,30,40,50,1,2,3,4,5,6,7", "2026-10-01,99,20,30,40,50,1,2,3,4,5,6,7"]}}
        result = self.collector.flow("301307", "0.301307", "2026-09-30")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["主力净流入(元)"], 10)
        self.client.json.return_value["data"]["code"] = "300059"
        with self.assertRaises(ValueError): self.collector.flow("301307", "0.301307", "2026-09-30")

    def test_announcements_only_target_stock_and_known_public_article(self):
        valid = {"codes": [{"stock_code": "301307"}], "notice_date": "2026-09-30", "art_code": "AN202609301234567890", "title": "测试公告"}
        self.client.json.return_value = {"data": {"list": [valid, {**valid, "notice_date": "2026-10-01"}, {**valid, "art_code": "https://evil.example"}]}}
        rows = self.collector.announcements("301307", "2026-09-30")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["链接"].startswith("https://data.eastmoney.com/notices/detail/301307/AN"))

    def test_collect_partial_sources_and_no_credentials_or_ai(self):
        snapshot = fixture()
        with patch.object(self.collector, "report", side_effect=ValueError), patch.object(self.collector, "flow", side_effect=ValueError), patch.object(self.collector, "boards", side_effect=ValueError), patch.object(self.collector, "announcements", return_value={"rows": []}):
            result = self.collector.collect(snapshot)
        self.assertEqual(result["sources"], {"announcements": {"rows": []}})
        self.assertEqual(len(result["errors"]), 10)
        self.client.json.assert_not_called()

    def test_collect_cancelled_and_bad_identity_never_network(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(ValueError): self.collector.collect(fixture(), event)
        with self.assertRaises(ValueError): self.collector.collect({"identity": {"code": 'bad"'}, "quote": {}})
        self.client.json.assert_not_called()

    def test_clean_row_whitelist_finite_no_sensitive_fields(self):
        row = d.clean_row({"OPERATE_INCOME": float("inf"), "api_key": "secret", "token": "secret", "NETPROFIT": True}, ("OPERATE_INCOME", "NETPROFIT"))
        self.assertEqual(row, {"OPERATE_INCOME": None, "NETPROFIT": None})


class IntegrationTests(unittest.TestCase):
    def test_ai_includes_bounded_financial_evidence_not_private_calculators(self):
        from ai_context import build_ai_context
        result = fixture()
        result["research"] = research_fixture()
        result["research"]["personal_capital"] = "private-capital-marker"
        result["research"]["api_key"] = "private-key-marker"
        result["research"]["sources"]["income"]["rows"][0]["token"] = "private-row-marker"
        result["research_analysis"] = {"manual_position": "private-position-marker"}
        context = build_ai_context(result)
        text = json.dumps(context)
        self.assertIn("research_observations", context)
        self.assertEqual(context["research_observations"]["fundamentals"]["TTM归母净利润(元)"], 17)
        self.assertNotIn("private-", text)

    def test_ai_discards_wrong_stock_date_and_future_financial_rows(self):
        from ai_context import build_ai_context
        result = fixture()
        result["research"] = {**research_fixture(), "code": "300059"}
        self.assertNotIn("research", build_ai_context(result))
        result["research"] = research_fixture()
        result["research"]["sources"]["income"]["rows"][0]["NOTICE_DATE"] = "2026-10-01"
        context = build_ai_context(result)
        self.assertEqual(len(context["research"]["sources"]["income"]["rows"]), 4)

    def test_ai_reports_current_research_and_not_all_book_rules_as_known(self):
        from ai_context import build_ai_context
        result = fixture()
        result["research"] = research_fixture()
        context = build_ai_context(result)
        self.assertIn("完整公司行动/历史时点财务库", {r["item"] for r in context["missing_data"]})
        self.assertIn("研究数据股票/日期", {r["item"] for r in context["quality_checks"]})
        self.assertLess(len(json.dumps(context, ensure_ascii=False).encode()), 192 * 1024)

    def test_original_ai_chat_not_silently_rebound_to_new_research(self):
        from test_ai_panel import PanelTests
        harness = PanelTests()
        harness.setUp()
        try:
            harness.initial_answer()
            before = harness.panel.context_text
            result = copy.deepcopy(harness.result)
            result["research"] = research_fixture()
            harness.panel.update_snapshot(result)
            self.assertEqual(harness.panel.context_text, before)
            self.assertIn("研究数据已新增", harness.panel.snapshot_info.get())
        finally:
            harness.doCleanups()

    def test_export_adds_research_to_existing_json_no_extra_reports(self):
        from stock_spider import export_result
        result = fixture()
        result["research"] = research_fixture()
        with tempfile.TemporaryDirectory() as folder:
            paths = export_result(result, folder)
            json_paths = [p for p in paths if p.suffix == ".json"]
            self.assertEqual(len(json_paths), 1)
            payload = json.loads(json_paths[0].read_text(encoding="utf-8"))
            self.assertIn("research", payload)
            self.assertIn("research_analysis", payload)
            self.assertFalse(any(p.suffix in {".md", ".txt", ".docx"} for p in paths))

    def test_export_excludes_stale_research(self):
        from stock_spider import export_result
        result = fixture()
        result["research"] = {**research_fixture(), "as_of": "2026-09-29"}
        with tempfile.TemporaryDirectory() as folder:
            paths = export_result(result, folder)
            payload = json.loads(next(p for p in paths if p.suffix == ".json").read_text(encoding="utf-8"))
            self.assertNotIn("research", payload)


class ResearchUiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from research_panel import ResearchPanel
        self.root = tk.Tk()
        self.root.geometry("740x340+20000+20000")
        self.callback = MagicMock()
        self.panel = ResearchPanel(self.root, self.callback)
        self.panel.pack(fill="both", expand=True)
        self.root.update()
        self.addCleanup(self.root.destroy)

    def test_no_network_until_clicked_and_fits_small_window(self):
        with patch("research_data.ResearchClient.collect") as collect:
            self.panel.update_snapshot(fixture())
            self.root.update()
            collect.assert_not_called()
            self.assertGreater(self.panel.text.winfo_height(), 100)
            toolbar = self.panel.selector.master
            for widget in toolbar.winfo_children():
                self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), toolbar.winfo_width())

    def test_background_query_publishes_only_current_data(self):
        event = threading.Event()
        def collect(*_):
            event.wait(2)
            return research_fixture()
        self.panel.update_snapshot(fixture())
        with patch("research_data.ResearchClient.collect", side_effect=collect):
            self.panel.start()
            self.assertTrue(self.panel.busy)
            self.assertEqual(str(self.panel.query_button["state"]), "disabled")
            event.set()
            for _ in range(100):
                self.panel.poll()
                if not self.panel.busy: break
                time.sleep(0.005)
            self.callback.assert_called_once()
            self.assertIsNotNone(self.panel.analysis)

    def test_stock_change_discards_late_research_and_close_cancels(self):
        self.panel.update_snapshot(fixture())
        old_generation = self.panel.generation
        event = threading.Event()
        self.panel.cancel_event = event
        self.panel.busy = True
        current = fixture()
        current["identity"]["code"] = "300059"
        current["quote"]["代码"] = "300059"
        self.panel.update_snapshot(current)
        self.assertTrue(event.is_set())
        self.panel.events.put((old_generation, event, research_fixture(), {}))
        self.panel.poll()
        self.callback.assert_not_called()
        self.assertIsNone(self.panel.analysis)
        self.panel.close()
        self.assertTrue(self.panel.closed)
