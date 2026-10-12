"""Pure, offline dashboard summaries and source-aware quality checks.

Nothing here fetches data, mutates a collector result, or produces trading advice.
Prices used for historical comparisons are the collector's adjusted daily prices;
cash-value reconciliation only uses the provider's reported main-session totals.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _positive(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _rows(result: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = result.get(key)
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, dict)]
    return []


def _date(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}(?:\d{6})?", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    else:
        text = text[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _quote_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{14}", text):
            return datetime.strptime(text, "%Y%m%d%H%M%S")
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d")
        if re.fullmatch(r"\d{10}(?:\.\d+)?", text):
            return datetime.fromtimestamp(float(text), timezone(timedelta(hours=8))).replace(tzinfo=None)
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, OverflowError, OSError):
        return None


def _display(value: Any, decimals: int = 2, suffix: str = "") -> str:
    parsed = _number(value)
    return "未获取" if parsed is None else f"{parsed:,.{decimals}f}{suffix}"


def _cash(value: Any) -> str:
    parsed = _number(value)
    if parsed is None:
        return "未获取"
    if abs(parsed) >= 100000000:
        return f"{parsed / 100000000:.2f} 亿元"
    if abs(parsed) >= 10000:
        return f"{parsed / 10000:.2f} 万元"
    return f"{parsed:,.2f} 元"


def _session(rows: list[dict[str, Any]], *, details: bool = False) -> dict[str, Any]:
    """Keep absent amounts unknown; never replace missing exact values with zero."""
    volumes = [_nonnegative(row.get("成交量")) for row in rows]
    amounts = [_nonnegative(row.get("金额" if details else "成交额")) for row in rows]
    known_volume = sum(value for value in volumes if value is not None)
    known_amount = sum(value for value in amounts if value is not None)
    return {
        "rows": len(rows),
        "volume": known_volume if rows and all(value is not None for value in volumes) else None,
        "amount": known_amount if rows and all(value is not None for value in amounts) else None,
        "known_volume": known_volume,
        "known_amount": known_amount,
        "volume_coverage": sum(value is not None for value in volumes),
        "amount_coverage": sum(value is not None for value in amounts),
    }


def _session_stats(result: dict[str, Any]) -> dict[str, Any]:
    minutes = _rows(result, "intraday")
    details = _rows(result, "details")
    return {
        "main_minutes": _session([row for row in minutes if row.get("计入主交易统计") == 1]),
        "after_hours_minutes": _session([row for row in minutes if row.get("交易阶段") == "盘后定价交易"]),
        "main_details": _session([row for row in details if row.get("计入主交易统计") == 1 and row.get("记录性质") == "成交记录"], details=True),
        "after_hours_details": _session([row for row in details if row.get("交易阶段") == "盘后定价交易" and row.get("记录性质") == "成交记录"], details=True),
        "auction_snapshots": sum(row.get("记录性质") == "行情快照" for row in details),
        "classification_missing": sum("计入主交易统计" not in row or "记录性质" not in row for row in details),
    }


def _freshness(result: dict[str, Any]) -> dict[str, Any]:
    quote = result.get("quote") or {}
    stamp = _quote_datetime(quote.get("行情时间"))
    captured = _quote_datetime(result.get("fetched_at"))
    daily = _rows(result, "daily_indicators", "daily_history", "daily")
    quote_date = stamp.date().isoformat() if stamp else ""
    capture_date = captured.date().isoformat() if captured else ""
    minute_dates = sorted({_date(row.get("交易日期")) for row in _rows(result, "intraday") if _date(row.get("交易日期"))})
    age = (captured - stamp).total_seconds() if captured and stamp else None
    text = f"行情 {stamp:%Y-%m-%d %H:%M:%S}" if stamp else "行情时间未获取"
    if capture_date and quote_date and quote_date != capture_date:
        text += "；跨日历史快照（节假日、停市或源延迟均可能）"
    elif age is not None and age > 900:
        text += "；行情时间落后采集时间，收盘后不代表故障"
    return {
        "quote_time": stamp.isoformat(sep=" ") if stamp else "",
        "quote_date": quote_date,
        "capture_time": captured.isoformat(sep=" ") if captured else "",
        "daily_date": _date(daily[-1].get("日期")) if daily else "",
        "intraday_dates": minute_dates,
        "age_seconds": age,
        "text": text,
    }


def build_quality_checks(result: dict[str, Any]) -> list[dict[str, str]]:
    """Return display-ready checks with stable item/status/detail/severity keys.

    Status is ok/warning/error/unavailable. Error means an internally impossible
    value, not merely a cross-provider difference or a delayed market snapshot.
    """
    checks: list[dict[str, str]] = []

    def add(item: str, status: str, detail: str) -> None:
        checks.append({"item": item, "status": status, "detail": detail,
                       "severity": "error" if status == "error" else "warning" if status in {"warning", "unavailable"} else "info"})

    quote = result.get("quote") or {}
    daily = _rows(result, "daily_indicators", "daily_history", "daily")
    minutes = _rows(result, "intraday")
    details = _rows(result, "details")
    benchmark = _rows(result, "benchmark_indicators", "benchmark_history")
    meta = result.get("details_meta") or {}
    freshness = _freshness(result)
    session = _session_stats(result)

    groups = [("日线", daily, "收盘"), ("分时", minutes, "价格"), ("明细", details, "价格")]
    for label, rows, field in groups:
        if not rows:
            add(f"{label}价格", "unavailable", "没有可核验记录")
            continue
        missing = sum(row.get(field) in (None, "", "-", "--") for row in rows)
        invalid = sum(row.get(field) not in (None, "", "-", "--") and _positive(row.get(field)) is None for row in rows)
        status = "error" if invalid else "warning" if missing else "ok"
        add(f"{label}价格", status, f"{len(rows)} 条；缺失 {missing} 条，非有限数/非正价格 {invalid} 条")

    if quote:
        value = quote.get("最新价")
        add("行情价格", "unavailable" if value is None else "ok" if _positive(value) is not None else "error", f"最新价 {_display(value)}；必须为有限正数")
    else:
        add("行情价格", "unavailable", "未获取行情快照")

    invalid_ohlc = missing_ohlc = invalid_price_fields = 0
    for row in daily:
        invalid_price_fields += sum(row.get(key) not in (None, "", "-", "--") and _positive(row.get(key)) is None for key in ("开盘", "收盘", "最高", "最低", "不复权开盘", "不复权收盘", "不复权最高", "不复权最低"))
        values = [_positive(row.get(key)) for key in ("开盘", "收盘", "最高", "最低")]
        if any(value is None for value in values):
            missing_ohlc += 1
        else:
            opening, close, high, low = values
            if high < low or not low - 1e-8 <= opening <= high + 1e-8 or not low - 1e-8 <= close <= high + 1e-8:
                invalid_ohlc += 1
    add("日线 OHLC 边界", "unavailable" if not daily else "error" if invalid_ohlc or invalid_price_fields else "warning" if missing_ohlc else "ok",
        f"最高/最低必须包住开盘及收盘；边界错误 {invalid_ohlc} 条，字段缺失 {missing_ohlc} 条，无效价格字段 {invalid_price_fields} 项")

    dates = [_date(row.get("日期")) for row in daily]
    duplicates = len([value for value in dates if value]) - len({value for value in dates if value})
    reversed_pairs = sum(bool(a and b and a > b) for a, b in zip(dates, dates[1:]))
    invalid_dates = dates.count("")
    add("日线日期顺序", "unavailable" if not dates else "error" if duplicates or reversed_pairs or invalid_dates else "ok",
        f"重复日期 {duplicates} 个，逆序 {reversed_pairs} 处，无效日期 {invalid_dates} 条；不在检查时自动排序")

    coverage = (
        ("不复权价格覆盖", lambda row: all(_positive(row.get(key)) is not None for key in ("不复权开盘", "不复权收盘", "不复权最高", "不复权最低")), "现金值和涨跌停核验需要完整不复权 OHLC"),
        ("历史成交额覆盖", lambda row: _number(row.get("成交额")) is not None and _number(row.get("成交额")) >= 0, "仅统计源返回成交额，不计典型价估算"),
        ("历史换手率覆盖", lambda row: any(_number(row.get(key)) is not None and _number(row.get(key)) >= 0 for key in ("可用历史换手率(%)", "历史行情换手率(%)", "精确历史换手率(%)")), "行情显示值与历史股本计算值口径不同；不称为交易所验证精确值"),
    )
    for item, test, note in coverage:
        count = sum(test(row) for row in daily)
        add(item, "unavailable" if not daily else "ok" if count == len(daily) else "warning", f"{count}/{len(daily)} 条；{note}")

    invalid_count_values = sum(row.get(key) not in (None, "", "-", "--") and _nonnegative(row.get(key)) is None for row in daily + minutes + details for key in ("成交量", "成交额", "金额"))
    add("成交量/金额非负", "unavailable" if not daily and not minutes and not details else "error" if invalid_count_values else "ok", f"发现负数或非有限数 {invalid_count_values} 项；缺失金额另列，不能填零冒充已取得")

    quote_date = freshness["quote_date"]
    minute_dates = freshness["intraday_dates"]
    if not quote_date or not minute_dates:
        add("行情与分时交易日期", "unavailable", "至少一方缺少交易日期，不能仅凭时分确认同日")
    elif minute_dates != [quote_date]:
        add("行情与分时交易日期", "warning", f"行情 {quote_date}；分时 {', '.join(minute_dates)}；禁止作为同一交易日合并对账")
    else:
        add("行情与分时交易日期", "ok", f"均为 {quote_date}；明细日期仍按行情日期推定，不是逐条源日期")

    if not freshness["quote_time"]:
        add("行情时间", "unavailable", "未获取或格式无效，不能判断数据新鲜度")
    elif freshness["age_seconds"] is not None and freshness["age_seconds"] < -60:
        add("行情时间", "warning", "行情时间晚于采集时间，需检查时区或计算机时间")
    elif freshness["capture_time"] and freshness["quote_date"] != freshness["capture_time"][:10]:
        add("行情时间", "warning", freshness["text"])
    else:
        add("行情时间", "ok", freshness["text"] + "；以提供商时间为准，不等同于零延迟")

    if daily and quote_date and freshness["daily_date"] != quote_date:
        add("行情与日线截止日期", "warning", f"行情 {quote_date}；日线 {freshness['daily_date']}；当前指标以日线截止日为准")
    else:
        add("行情与日线截止日期", "ok" if daily and quote_date else "unavailable", f"日线截止 {freshness['daily_date'] or '未知'}；行情日期 {quote_date or '未知'}")

    detail_dates = sorted({_date(row.get("交易日期")) for row in details if _date(row.get("交易日期"))})
    same_day = bool(quote_date and minute_dates == [quote_date])
    details_same_day = bool(quote_date and detail_dates == [quote_date] and not meta.get("date_mismatch"))
    add("明细交易日期", "unavailable" if not details or not quote_date or not detail_dates else "ok" if details_same_day else "warning",
        f"明细 {', '.join(detail_dates) or '未获取'}；行情 {quote_date or '未获取'}；明细日期通常由行情推定，不是独立源日期")
    for item, metric, field, stats_key in (
        ("分时主交易量对账", "volume", "成交量(手)", "main_minutes"),
        ("分时主交易金额对账", "amount", "成交额", "main_minutes"),
        ("明细主交易量对账", "volume", "成交量(手)", "main_details"),
    ):
        total = session[stats_key][metric]
        expected = _number(quote.get(field))
        if total is None or expected is None:
            add(item, "unavailable", "缺少可加总的主交易数据或行情总量；排除竞价快照及盘后定价")
        elif not (details_same_day if stats_key == "main_details" else same_day):
            add(item, "warning", "交易日期未确认一致；不进行数值一致性认证")
        else:
            difference = total - expected
            tolerance = max(1.0, abs(expected) * 0.00001) if metric == "volume" else max(100.0, abs(expected) * 0.001)
            if abs(difference) < 0.000001:
                add(item, "ok", "主交易合计与行情值一致；不含竞价快照/盘后定价，不保证交易所原始逐笔完整")
            elif abs(difference) <= tolerance:
                add(item, "warning", f"差值 {difference:,.4f} {'手' if metric == 'volume' else '元'}；小幅差异，可能为显示舍入或采集时点不同，尚未证实错误")
            else:
                add(item, "warning", f"差值 {difference:,.4f} {'手' if metric == 'volume' else '元'}；需核对采集时点/口径/缺失，不能宣称已抓全")

    actual_detail_total = session["main_details"]["volume"]
    expected_detail_total = _nonnegative(quote.get("成交量(手)"))
    actual_volume_matches = actual_detail_total is not None and expected_detail_total is not None and abs(actual_detail_total - expected_detail_total) < 0.001
    if not details:
        add("成交明细完整性", "unavailable", "未取得明细；不存在记录不代表市场没有成交")
    elif meta.get("format") != "eastmoney" or not meta.get("interface_complete"):
        add("成交明细完整性", "warning", meta.get("completeness_note") or "备用汇总或覆盖未验证；不是交易所原始逐笔全量")
    elif meta.get("final_complete") and meta.get("volume_reconciled") and details_same_day and same_day and actual_volume_matches:
        add("成交明细完整性", "ok", "接口声称全量且主交易量对账一致；仍不等同于交易所逐笔完整保证")
    else:
        add("成交明细完整性", "warning", meta.get("completeness_note") or "已取得接口记录，但最终完整性尚未验证")

    add("主交易/盘后分类", "unavailable" if not details else "warning" if session["classification_missing"] else "ok",
        f"竞价快照 {session['auction_snapshots']} 条，盘后成交 {session['after_hours_details']['rows']} 条，缺少分类 {session['classification_missing']} 条；非主交易记录不加入对账")
    detail_exact = session["main_details"]
    add("明细金额覆盖", "unavailable" if not detail_exact["rows"] else "ok" if detail_exact["amount_coverage"] == detail_exact["rows"] else "warning",
        f"源返回金额 {detail_exact['amount_coverage']}/{detail_exact['rows']} 条；价格×手数×100 是估算，不替代源金额")

    benchmark_date = _date(benchmark[-1].get("日期")) if benchmark else ""
    stock_date = freshness["daily_date"]
    add("基准指数截止日期", "unavailable" if not stock_date or not benchmark_date else "ok" if stock_date == benchmark_date else "warning",
        f"股票 {stock_date or '未获取'}；{result.get('benchmark_name') or '基准指数'} {benchmark_date or '未获取'}；超额收益必须按相同交易日期对齐")

    errors = result.get("errors") or {}
    if errors:
        add("数据源异常", "warning", "；".join(f"{key}: {str(value)[:140]}" for key, value in errors.items()))
    else:
        add("数据源异常", "ok", "采集器未报告请求异常；不代表所有字段必然齐全")
    if isinstance(result.get("research"), dict):
        from research_analysis import research_quality_checks
        checks.extend(research_quality_checks(result))
    return checks


def summarize_dashboard(result: dict[str, Any]) -> dict[str, Any]:
    """Produce factual technical observations and display-ready dashboard fields."""
    daily = _rows(result, "daily_indicators", "daily_history", "daily")
    latest = daily[-1] if daily else {}
    previous = daily[-2] if len(daily) > 1 else {}
    close = _positive(latest.get("收盘"))
    dates = [_date(row.get("日期")) for row in daily]
    ordered = bool(dates) and all(dates) and all(a < b for a, b in zip(dates, dates[1:]))
    observations: list[str] = []
    metrics: list[dict[str, str]] = []

    def metric(label: str, value: str, note: str = "") -> None:
        metrics.append({"label": label, "value": value, "note": note})

    moving = {
        period: _positive(latest.get(f"MA{period}"))
        if ordered and len(daily) >= period and all(_positive(row.get("收盘")) is not None for row in daily[-period:])
        else None for period in (5, 10, 20, 60)
    }
    if close is not None and moving[20] is not None:
        displacement = (close / moving[20] - 1) * 100
        observations.append(f"日线收盘位于 MA20 {'上方' if displacement > 1e-8 else '下方' if displacement < -1e-8 else '附近'}，偏离 {displacement:+.2f}%。")
    else:
        observations.append("MA20 尚未就绪；至少需要 20 个连续有效交易日收盘价。")
    if all(moving[period] is not None for period in (5, 10, 20)):
        if moving[5] > moving[10] > moving[20]:
            observations.append("MA5 > MA10 > MA20，短期均线呈上行排列；这是已有价格的描述。")
        elif moving[5] < moving[10] < moving[20]:
            observations.append("MA5 < MA10 < MA20，短期均线呈下行排列；这是已有价格的描述。")
        else:
            observations.append("MA5/MA10/MA20 交织或持平，没有严格单向排列。")

    dif, dea, macd = (_number(latest.get(key)) for key in ("DIF", "DEA", "MACD"))
    valid_tail = ordered and len(daily) >= 35 and all(_positive(row.get("收盘")) is not None for row in daily[-35:])
    if valid_tail and dif is not None and dea is not None and macd is not None:
        observations.append(f"MACD 柱值 {macd:+.4f}，{'正值' if macd > 0 else '负值' if macd < 0 else '零值'}；DIF {'高于' if dif > dea else '低于' if dif < dea else '等于'} DEA。")
        old_dif, old_dea = _number(previous.get("DIF")), _number(previous.get("DEA"))
        if len(daily) >= 36 and old_dif is not None and old_dea is not None:
            if old_dif <= old_dea and dif > dea:
                observations.append("最新日线 DIF 由 DEA 下方/相等移到上方（上穿）；盘中指标仍会改变。")
            elif old_dif >= old_dea and dif < dea:
                observations.append("最新日线 DIF 由 DEA 上方/相等移到下方（下穿）；盘中指标仍会改变。")
    else:
        observations.append("MACD 历史预热不足或字段缺失；不据初始 EMA 值判定交叉。")

    for period in (6, 12):
        value = _number(latest.get(f"RSI{period}"))
        if value is None or not 0 <= value <= 100 or len(daily) < period + 1 or not ordered:
            continue
        zone = "较高区间（≥70，近期上涨占优）" if value >= 70 else "较低区间（≤30，近期下跌占优）" if value <= 30 else "中间区间（30～70）"
        observations.append(f"RSI{period} {value:.2f}，处于{zone}；不是反转或买卖保证。")

    returns: dict[str, float | None] = {}
    for days in (5, 20, 60, 120):
        window = daily[-days - 1:] if len(daily) > days else []
        valid = ordered and window and all(_positive(row.get("收盘")) is not None for row in window)
        value = (close / _positive(window[0].get("收盘")) - 1) * 100 if valid and close is not None else None
        returns[f"{days}日"] = value
        metric(f"{days}日收益", _display(value, suffix="%"), f"前复权收盘首尾比较；需 {days + 1} 个连续有效交易日")

    recent = daily[-20:]
    complete_range = bool(recent) and all(_positive(row.get("最高")) is not None and _positive(row.get("最低")) is not None for row in recent)
    high = max(_positive(row.get("最高")) for row in recent) if complete_range else None
    low = min(_positive(row.get("最低")) for row in recent) if complete_range else None
    observed_range = {"low": low, "high": high, "window": len(recent), "start_date": _date(recent[0].get("日期")) if recent else "", "end_date": _date(latest.get("日期")), "label": "近 20 个交易日已观察区间（前复权）"}
    metric("近 20 日区间", f"{_display(low)} ～ {_display(high)}", f"实际 {len(recent)} 个交易日的最高/最低；不是未来支撑/阻力预测")

    tail120 = daily[-120:]
    drawdown = None
    if ordered and len(tail120) >= 2 and all(_positive(row.get("收盘")) is not None for row in tail120):
        peak = _positive(tail120[0].get("收盘"))
        drawdown = 0.0
        for row in tail120:
            value = _positive(row.get("收盘"))
            peak = max(peak, value)
            drawdown = min(drawdown, (value / peak - 1) * 100)
    metric("近 120 日最大回撤", _display(drawdown, suffix="%"), f"实际 {len(tail120)} 个交易日收盘峰谷；不预测未来风险")

    volatility = None
    log_window = daily[-21:]
    if ordered and len(log_window) == 21 and all(_positive(row.get("收盘")) is not None for row in log_window):
        logs = [math.log(_positive(b.get("收盘")) / _positive(a.get("收盘"))) for a, b in zip(log_window, log_window[1:])]
        mean = sum(logs) / len(logs)
        volatility = math.sqrt(sum((value - mean) ** 2 for value in logs) / len(logs)) * math.sqrt(250) * 100
    metric("20 日年化波动率", _display(volatility, suffix="%"), "20 个对数收益的总体标准差 × √250；历史统计")

    session = _session_stats(result)
    main = session["main_minutes"]
    after = session["after_hours_minutes"]
    vwap = main["amount"] / (main["volume"] * 100) if main["amount"] is not None and main["volume"] is not None and main["volume"] > 0 else None
    metric("主交易分时均价", _display(vwap), "源金额 ÷ (主交易手数 ×100)，排除盘后定价及竞价快照")
    metric("主交易成交额", _cash(main["amount"]), f"分时金额可用 {main['amount_coverage']}/{main['rows']} 条")
    metric("盘后定价成交量", _display(after["volume"], suffix=" 手") if after["rows"] else "未发现盘后记录", "单独列示，不加入主交易统计；无记录不保证无盘后成交")

    checks = build_quality_checks(result)
    warnings = [f"{check['item']}：{check['detail']}" for check in checks if check["status"] != "ok"]
    identity = result.get("identity") or {}
    title = " ".join(str(identity.get(key) or "") for key in ("name", "code")).strip() or "未选择股票"
    summary = f"{title} · 日线截至 {_date(latest.get('日期')) or '未取得'} · {len(daily)} 条历史记录"
    if not daily:
        summary += "；暂无可分析的日线走势"
    return {
        "summary": summary, "observations": observations, "metrics": metrics,
        "range": observed_range, "returns": returns,
        "risk": {"max_drawdown": drawdown, "annualized_volatility": volatility},
        "sources": dict(result.get("sources") or {}), "freshness": _freshness(result),
        "quality_checks": checks, "warnings": warnings, "session_stats": session,
        "disclaimer": "技术指标仅描述已有行情，不构成投资建议；盘中数据与指标未定稿。",
    }
