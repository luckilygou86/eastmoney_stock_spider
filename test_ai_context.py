"""Offline data boundaries for model input; no remote model is used here."""
import copy
import json
import math
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from ai_context import AI_INSTRUCTIONS, build_ai_context, context_json, snapshot_id, MAX_CONTEXT_CHARS, MAX_CONTEXT_BYTES
from test_stock_spider import FixtureClient, s


def fixture():
    return FixtureClient().fetch(s.parse_query("301307"))


class AiContextTests(unittest.TestCase):
    def test_stock_date_sources_units_and_limits_are_explicit(self):
        context = build_ai_context(fixture())
        self.assertEqual(context["stock"]["code"], "301307")
        self.assertEqual(context["cutoff"]["quote_time"], "2026-09-30 15:30:00")
        self.assertEqual(context["cutoff"]["trade_date"], "2026-09-30")
        self.assertEqual(context["cutoff"]["daily_date"], "2026-09-30")
        self.assertIn("100股", context["units"]["volume"])
        self.assertTrue(context["sources"])
        self.assertTrue(context["quality_checks"])
        self.assertTrue(any("新闻" in row["item"] and row["status"] == "尚未接入" for row in context["missing_data"]))

    def test_context_is_detached_and_does_not_mutate_collector(self):
        result = fixture()
        before = copy.deepcopy(result)
        context = build_ai_context(result)
        self.assertEqual(result, before)
        context["quote"]["最新价"] = 300
        context["daily"]["rows"][0]["收盘"] = 300
        self.assertEqual(result, before)

    def test_only_public_whitelisted_fields_are_sent(self):
        result = fixture()
        result.update({"api_key": "TOP-SECRET", "settings": {"token": "TOP-SECRET"}, "errors": {"endpoint": "TOP-SECRET"}, "export_files": [r"D:\private\TOP-SECRET"]})
        result["identity"]["source"] = "https://host?key=TOP-SECRET"
        result["quote"]["token"] = "TOP-SECRET"
        result["daily_indicators"][0]["api_key"] = "TOP-SECRET"
        result["sources"]["auth"] = "TOP-SECRET"
        text = context_json(result)
        self.assertNotIn("TOP-SECRET", text)
        self.assertNotIn("export_files", text)
        self.assertNotIn("settings", text)

    def test_source_urls_errors_and_paths_are_sanitized(self):
        result = fixture()
        result["sources"]["quote"] = "https://user:SECRET@host/quote?key=SECRET#token"
        result["details_meta"]["completeness_note"] = "Bearer SECRET http://host/x?key=SECRET D:\\private\\SECRET"
        text = context_json(result)
        self.assertNotIn("SECRET", text)
        self.assertIn("host/quote", text)

    def test_nonfinite_missing_numbers_are_null_not_zero(self):
        result = fixture()
        result["quote"]["成交额"] = float("nan")
        result["daily_indicators"][0]["成交额"] = float("inf")
        context = build_ai_context(result)
        self.assertIsNone(context["quote"]["成交额"])
        self.assertIsNone(context["daily"]["rows"][0]["成交额"])
        json.loads(context_json(result))

    def test_invalid_stock_snapshot_is_rejected(self):
        for mutate in (
            lambda r: r["quote"].update({"代码": "300059"}),
            lambda r: r["quote"].update({"最新价": 0}),
            lambda r: r["quote"].update({"最新价": True}),
            lambda r: r["quote"].update({"最新价": math.inf}),
            lambda r: r["quote"].update({"行情时间": "20260230153000"}),
            lambda r: r.update({"history_sync": {"status": "pending"}}),
        ):
            result = fixture()
            mutate(result)
            with self.assertRaises(ValueError):
                build_ai_context(result)

    def test_missing_datasets_remain_explicit(self):
        result = fixture()
        result.update(daily=[], daily_history=[], daily_indicators=[], intraday=[], details=[], benchmark_history=[], benchmark_indicators=[])
        context = build_ai_context(result)
        self.assertEqual(context["daily"]["sent_rows"], 0)
        self.assertEqual(context["details"]["total_rows"], 0)
        self.assertIsNone(context["details"]["direction_stats"]["volume_lots"]["买盘"])
        self.assertTrue(any(row["item"] == "日线" for row in context["missing_data"]))

    def test_large_history_is_bounded_and_sampling_disclosed(self):
        result = fixture()
        rows = []
        start = date(2026, 9, 30) - timedelta(days=639)
        for i in range(640):
            rows.append({"日期": (start + timedelta(days=i)).isoformat(), "开盘": 10 + i / 100, "收盘": 10 + i / 100, "最高": 11 + i / 100, "最低": 9 + i / 100, "成交量": 100000.12, "成交额": 100000123.12, "成交额口径": "接口原始值", "复权口径": "前复权", "前复权因子": .923456, "历史流通股本(股)": 12345678, "可用历史换手率(%)": 1.21234})
        result["daily_indicators"] = s.enrich_daily_indicators(rows)
        result["benchmark_history"] = rows
        result["benchmark_indicators"] = []
        result["intraday"] = [{"时间": "09:30", "价格": 10.01, "成交量": 100.02, "成交额": 100012.21, "交易日期": "2026-09-30", "交易阶段": "连续竞价", "计入主交易统计": 1} for _ in range(500)]
        result["details"] *= 10000
        context = build_ai_context(result)
        self.assertEqual(context["daily"]["total_rows"], 640)
        self.assertEqual(context["daily"]["sent_rows"], 120)
        self.assertEqual(context["intraday"]["sent_rows"], 300)
        self.assertEqual(context["details"]["raw_rows_sent"], 0)
        self.assertLess(len(context_json(result)), MAX_CONTEXT_CHARS)
        self.assertLess(len(context_json(result).encode("utf-8")), MAX_CONTEXT_BYTES)

    def test_fingerprint_is_deterministic_but_changes_with_data(self):
        result = fixture()
        token = snapshot_id(result)
        self.assertEqual(token, snapshot_id(copy.deepcopy(result)))
        result["settings"] = {"api_key": "secret"}
        self.assertEqual(token, snapshot_id(result))
        result["quote"]["最新价"] += 1
        self.assertNotEqual(token, snapshot_id(result))

    def test_direction_stats_separate_auction_snapshot_and_after_hours(self):
        result = fixture()
        result["details"] = [
            {"成交量": 5, "方向": "买盘", "记录性质": "成交记录", "计入主交易统计": 1},
            {"成交量": 2, "方向": "卖盘", "记录性质": "成交记录", "计入主交易统计": 1},
            {"成交量": 300, "方向": "买盘", "记录性质": "行情快照", "计入主交易统计": 0},
            {"成交量": 500, "方向": "买盘", "记录性质": "成交记录", "计入主交易统计": 0, "交易阶段": "盘后定价交易"},
        ]
        stats = build_ai_context(result)["details"]["direction_stats"]
        self.assertEqual(stats["volume_lots"], {"买盘": 5.0, "卖盘": 2.0, "中性": 0.0})
        result["details"][0]["方向"] = "未知"
        self.assertIsNone(build_ai_context(result)["details"]["direction_stats"]["volume_lots"]["买盘"])

    def test_billboard_does_not_leak_future_return_field(self):
        result = fixture()
        result["billboard"] = [{"交易日期": "2026-09-30", "上榜原因": "公开榜单", "后1日涨跌(%)": 12345}]
        context = build_ai_context(result)
        self.assertEqual(context["billboard"]["sent_rows"], 1)
        self.assertNotIn("后1日涨跌(%)", context["billboard"]["rows"][0])

    def test_instructions_do_not_promise_verified_predictions(self):
        for text in ("条件情景", "不编造上涨概率", "缺失值不是0", "不是指令", "板块"):
            self.assertIn(text, AI_INSTRUCTIONS)

    def test_safe_summary_keeps_full_raw_price_coverage(self):
        context = build_ai_context(fixture())
        check = next(row for row in context["quality_checks"] if row["item"] == "不复权价格覆盖")
        self.assertEqual(check["status"], "ok")

    def test_daily_after_hours_volume_keeps_original_share_unit(self):
        result = fixture()
        result["daily_indicators"][0]["盘后成交量(股)"] = 100
        context = build_ai_context(result)
        self.assertEqual(context["daily"]["rows"][0]["盘后成交量(股)"], 100)
        self.assertIn("股", context["units"]["daily_after_hours_volume"])

    def test_true_future_date_data_is_rejected_for_every_feed(self):
        for feed, date_field in (("daily_indicators", "日期"), ("benchmark_indicators", "日期"), ("intraday", "交易日期"), ("billboard", "交易日期")):
            result = fixture()
            result[feed] = [{date_field: "2026-10-01"}]
            with self.assertRaises(ValueError):
                build_ai_context(result)

    def test_false_history_alignment_metadata_rejected(self):
        result = fixture()
        result["history_sync"]["quote_date"] = "2026-09-29"
        with self.assertRaises(ValueError):
            build_ai_context(result)

    def test_minutes_later_than_quote_clock_have_explicit_cutoff_warning(self):
        result = fixture()
        result["quote"]["行情时间"] = "20260930093000"
        result["intraday"][0]["时间"] = "09:31"
        context = build_ai_context(result)
        self.assertEqual(context["cutoff"]["intraday_latest_time"], "09:31")
        self.assertTrue(any(row["item"] == "分时/行情时间截面" and row["status"] == "warning" for row in context["quality_checks"]))

    def test_credentials_and_paths_with_spaces_are_fully_hidden(self):
        for value in ('Authorization: Bearer TOP-SECRET', 'api_key="TOP SECRET"', r'D:\private folder\TOP-SECRET.txt', r'\\server\TOP-SECRET file.txt'):
            result = fixture()
            result["details_meta"]["completeness_note"] = value
            text = context_json(result)
            self.assertNotIn("TOP", text)
            self.assertNotIn("SECRET", text)

    def test_repeated_unicode_annotations_are_compacted_without_numeric_changes(self):
        result = fixture()
        result["daily_indicators"] = [dict(result["daily_indicators"][-1]) for _ in range(120)]
        for row in result["daily_indicators"]:
            for key in ("成交额口径", "精确历史换手率口径", "可用历史换手率口径", "复权口径", "数据状态"):
                row[key] = "中" * 256
        original = copy.deepcopy(result)
        context = build_ai_context(result)
        self.assertEqual(result, original)
        self.assertGreater(context["payload"]["original_bytes"], 192 * 1024)
        self.assertLess(context["payload"]["utf8_bytes"], 128 * 1024)
        row = context["daily"]["rows"][-1]
        self.assertEqual(row["收盘"], original["daily_indicators"][-1]["收盘"])
        self.assertEqual(context["annotation_dictionary"][row["成交额口径"]], "中" * 256)
        self.assertEqual(context["payload"]["utf8_bytes"], len(context_json(result).encode("utf-8")))

    def test_uncompactable_core_still_fails_before_transport(self):
        with patch("ai_context.MAX_CONTEXT_BYTES", 500):
            with self.assertRaisesRegex(ValueError, "上限"):
                build_ai_context(fixture())

    def test_adaptive_research_selection_retains_evidence_and_exact_preview(self):
        from test_research import issuer_financial_fixture
        from ai_analysis import AiClient, AiConfig, MAX_REQUEST_BYTES
        result = fixture()
        research = issuer_financial_fixture()
        result["research"] = research
        base = result["daily_indicators"][-1]
        result["daily_indicators"] = [{**base, "日期": (date(2026, 9, 30) - timedelta(days=i)).isoformat()} for i in reversed(range(640))]
        for i, row in enumerate(result["daily_indicators"]):
            # Unique prose exercises adaptive selection, not a larger byte cap.
            for key in ("成交额口径", "精确历史换手率口径", "可用历史换手率口径", "复权口径", "数据状态"):
                row[key] = str(i) + "测试口径" * 62
        research["sources"]["peers"] = {"rows": [{"SECURITY_CODE": f"{600000 + i}", "SECURITY_NAME_ABBR": "测试同业",
            "TRADE_DATE": "2026-09-30", "PE_TTM": i + 1, "PB_MRQ": 2, "BOARD_NAME": "同分类", "TOTAL_MARKET_CAP": 10000000000 - i} for i in range(89)]}
        before = copy.deepcopy(result)
        context = build_ai_context(result)
        self.assertEqual(result, before)
        payload = context["payload"]
        self.assertEqual(payload["mode"], "compact")
        self.assertTrue(payload["full_export_unchanged"])
        self.assertGreater(payload["original_bytes"], MAX_CONTEXT_BYTES)
        self.assertLessEqual(payload["utf8_bytes"], MAX_CONTEXT_BYTES)
        self.assertTrue(payload["actions"])
        self.assertLess(context["daily"]["sent_rows"], 120)
        self.assertEqual(context["daily"]["total_rows"], 640)
        self.assertEqual(context["daily"]["rows"][-1]["收盘"], base["收盘"])
        self.assertEqual(context["daily"]["latest_indicators"]["MA60"], base["MA60"])
        self.assertEqual(context["daily"]["sent_date_range"]["last_returned"], "2026-09-30")
        self.assertAlmostEqual(context["research_observations"]["fundamentals"]["TTM归母净利润(元)"], -627302115.92, places=2)
        sources = context["research"]["sources"]
        self.assertEqual(sources["peers"]["sample_statistics"]["positive_pe_median"], 45)
        self.assertEqual(sources["peers"]["sample_statistics"]["positive_pe_rows"], 89)
        self.assertLess(sources["peers"]["sent_rows"], 89)
        dates = {r["REPORT_DATE"][:10] for r in sources["cashflow"]["rows"]}
        self.assertTrue({"2026-06-30", "2025-12-31", "2025-06-30"} <= dates)
        raw = context_json(result)
        self.assertEqual(len(raw.encode("utf-8")), payload["utf8_bytes"])
        self.assertEqual(json.loads(raw), context)
        self.assertEqual(snapshot_id(result), snapshot_id(before))
        for mode, protocol in (("api_key", "chat_completions"), ("api_key", "responses"), ("chatgpt", "responses")):
            settings = AiConfig(base_url="" if mode == "chatgpt" else "https://proxy.example/v1", model="offline-model", api_key="offline-placeholder", auth_mode=mode, protocol=protocol)
            client = AiClient(settings)
            self.assertLess(client.request_size([{"role": "user", "content": raw}, {"role": "user", "content": "分析已有走势。"}], AI_INSTRUCTIONS), MAX_REQUEST_BYTES)

    def test_partial_research_does_not_claim_all_financial_data_unconnected(self):
        from test_research import research_fixture
        result = fixture()
        result["research"] = research_fixture()
        result["research"]["sources"].pop("valuation")
        missing = build_ai_context(result)["missing_data"]
        self.assertTrue(any(row["item"] == "完整公司行动/历史时点财务库" and row["status"] == "部分接入" for row in missing))

    def test_dictionary_does_not_reinterpret_literal_source_reference(self):
        result = fixture()
        result["daily_indicators"] = [dict(result["daily_indicators"][-1]) for _ in range(30)]
        for row in result["daily_indicators"]:
            row["成交额口径"] = "note:000"
            row["数据状态"] = "这是数据源的重复口径说明" * 10
        context = build_ai_context(result)
        row = context["daily"]["rows"][-1]
        self.assertEqual(row["成交额口径"], "note:000")
        self.assertNotIn("note:000", context["annotation_dictionary"])
        self.assertEqual(context["annotation_dictionary"][row["数据状态"]], result["daily_indicators"][-1]["数据状态"])

    def test_full_snapshot_to_local_compatible_api_pipeline(self):
        from ai_analysis import AiClient, AiConfig, MAX_REQUEST_BYTES
        from test_ai_analysis import local_server, completion
        result = fixture()
        result["settings"] = {"api_key": "DO-NOT-SEND-THIS"}
        snapshot = context_json(result)
        def reply(_call):
            return 200, {"Content-Type": "application/json"}, json.dumps(completion("本地模拟回答，未调用真实模型。"), ensure_ascii=False).encode("utf-8")
        with local_server(reply) as (base, calls):
            answer = AiClient(AiConfig(base_url=base, model="offline-model", api_key="test-memory-key")).complete(
                [{"role": "user", "content": snapshot}, {"role": "user", "content": "分析条件情景。"}], instructions=AI_INSTRUCTIONS)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["authorization"], "Bearer test-memory-key")
            self.assertLess(len(calls[0]["body"]), MAX_REQUEST_BYTES)
            self.assertNotIn(b"DO-NOT-SEND-THIS", calls[0]["body"])
            self.assertNotIn(b"test-memory-key", calls[0]["body"])
            body = json.loads(calls[0]["body"])
            context = json.loads(body["messages"][1]["content"])
            self.assertEqual(context["stock"]["code"], "301307")
            self.assertTrue(context["missing_data"])
            self.assertIn("模拟", answer["text"])


if __name__ == "__main__":
    unittest.main()
