"""Original evidence-check algorithm, inspired by general financial principles.

Not a reproduction of either book, not trained/backtested, not a price predictor.
Unknown data never turns into zero or a passing rule. All monetary fields are CNY.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

from research_data import REPORT_NOTES, available_financial, binding, day, number


MODULES = ("综合检查", "股票基础", "财务报表", "公司分析", "股票估值", "量价/技术分析", "资金与市场", "仓位和风控")
FIELD_LABELS = {
    "OPERATE_INCOME": "营业收入", "TOTAL_OPERATE_INCOME": "营业总收入", "OPERATE_COST": "营业成本",
    "OPERATE_PROFIT": "营业利润", "TOTAL_PROFIT": "利润总额", "NETPROFIT": "合并净利润",
    "PARENT_NETPROFIT": "归母净利润", "DEDUCT_PARENT_NETPROFIT": "扣非归母净利润", "RESEARCH_EXPENSE": "研发费用",
    "SALE_EXPENSE": "销售费用", "MANAGE_EXPENSE": "管理费用", "FINANCE_EXPENSE": "财务费用",
    "FE_INTEREST_EXPENSE": "利息费用（非现金付息）", "BASIC_EPS": "基本每股收益(元/股)",
    "TOTAL_ASSETS": "总资产", "TOTAL_LIABILITIES": "总负债", "TOTAL_EQUITY": "含少数股东总权益",
    "TOTAL_PARENT_EQUITY": "归母净资产", "TOTAL_CURRENT_ASSETS": "流动资产", "TOTAL_CURRENT_LIAB": "流动负债",
    "MONETARYFUNDS": "货币资金（不保证全部可自由使用）", "ACCOUNTS_RECE": "应收账款", "NOTE_ACCOUNTS_RECE": "应收票据及账款",
    "INVENTORY": "存货", "SHORT_LOAN": "短期借款", "LONG_LOAN": "长期借款", "NONCURRENT_LIAB_1YEAR": "一年内到期非流动负债",
    "LEASE_LIAB": "租赁负债", "BOND_PAYABLE": "应付债券", "GOODWILL": "商誉", "SHARE_CAPITAL": "股本(股)",
    "NETCASH_OPERATE": "经营现金净流量", "CONSTRUCT_LONG_ASSET": "购建长期资产现金支出", "DISPOSAL_LONG_ASSET": "处置长期资产现金流入",
    "NETCASH_INVEST": "投资现金净流量", "NETCASH_FINANCE": "筹资现金净流量", "SALES_SERVICES": "销售商品提供劳务收到现金",
    "RECEIVE_LOAN_CASH": "借款现金流入", "PAY_DEBT_CASH": "偿还债务现金", "ASSIGN_DIVIDEND_PORFIT": "股利利润及利息合计现金支出",
    "CCE_ADD": "现金等价物净增加", "BEGIN_CCE": "期初现金等价物", "END_CCE": "期末现金等价物", "RATE_CHANGE_EFFECT": "汇率对现金影响"}
SOURCE_LABELS = {"financial_metrics": "财务指标", "income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表",
                 "valuation": "历史估值", "profile": "公司资料", "dividends": "现金分红", "holders": "流通股东",
                 "announcements": "公司公告", "peers": "同分类估值", "flow": "估算资金流", "boards": "行业榜单", "industry": "所属行业指数"}
SOURCE_NOTES = {**REPORT_NOTES,
                "announcements": "仅最近公告标题，未读取正文；不覆盖全部新闻", "peers": "同分类按市值取前100家公司，不保证业务可比",
                "flow": "人民币元、占比为%；订单大小估算，不是真实机构身份", "boards": "当日榜单，涨跌为%、资金流为元；非历史轮动",
                "industry": "行业指数点位，不是个股价格；按日期对齐后比较"}


def dataset(research, key):
    entry = (research.get("sources") or {}).get(key) or {}
    rows = entry.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def financial_rows(research, key):
    # Defense in depth: collectors and exported input cannot bypass date checks.
    code, as_of = research.get("code"), research.get("as_of", "")
    result = {}
    for row in dataset(research, key):
        if row.get("SECURITY_CODE") == code and available_financial(row, as_of) and row.get("CURRENCY") == "CNY":
            report = day(row.get("REPORT_DATE"))
            # Keep the latest disclosed revision per reporting period.
            if report not in result or str(row.get("UPDATE_DATE")) > str(result[report].get("UPDATE_DATE")):
                result[report] = row
    return result


def ttm(rows, field, end):
    latest = rows.get(end) or {}
    current = number(latest.get(field))
    if current is None:
        return None
    if end.endswith("12-31"):
        return current
    year = int(end[:4]) - 1
    annual, prior = rows.get(f"{year}-12-31") or {}, rows.get(str(year) + end[4:]) or {}
    a, p = number(annual.get(field)), number(prior.get(field))
    return current + a - p if a is not None and p is not None else None


def ratio(top, bottom, percent=False):
    return top / bottom * (100 if percent else 1) if top is not None and bottom is not None and bottom > 0 else None


def positive_growth(current, prior):
    # Negative/zero bases are not economically meaningful growth rates.
    return (current / prior - 1) * 100 if current is not None and current > 0 and prior is not None and prior > 0 else None


def annual_cagr(rows, field, years=3):
    ends = sorted(k for k in rows if k.endswith("12-31"))
    if not ends:
        return None
    final_year = int(ends[-1][:4])
    values = [number((rows.get(f"{year}-12-31") or {}).get(field)) for year in range(final_year - years, final_year + 1)]
    if any(value is None or value <= 0 for value in values):
        return None
    return ((values[-1] / values[0]) ** (1 / years) - 1) * 100


def display(value, unit=""):
    value = number(value)
    if value is None:
        return "缺数据 / 未计算"
    if unit == "元" and abs(value) >= 100000000:
        return f"{value / 100000000:,.2f}亿元"
    if unit in {"元", "股"} and abs(value) >= 10000:
        return f"{value / 10000:,.2f}万{unit}"
    if unit == "%":
        return f"{value:,.2f}%"
    return f"{value:,.3f}{unit}"


def metric_display(key, value):
    unit = "元" if key.endswith("(元)") else "%" if key.endswith("(%)") else "股" if key.endswith("(股)") else ""
    if key.endswith("(前复权)"):
        unit = "元/股"
    elif "百分点" in key:
        unit = "百分点"
    elif key in {"PE_TTM", "PB_MRQ", "流动比率", "TTM经营现金/合并净利润", "市值/TTM归母净利(仅正利润)", "同分类正PE样本中位数", "同分类正PB样本中位数", "当日量/前5日均量"}:
        unit = "倍"
    return display(value, unit)


def technical(result, as_of):
    rows = {day(r.get("日期")): r for r in result.get("daily_indicators") or result.get("daily_history") or []
            if isinstance(r, dict) and day(r.get("日期")) and day(r.get("日期")) <= as_of and number(r.get("收盘")) is not None and number(r["收盘"]) > 0}
    rows = [rows[key] for key in sorted(rows)][-252:]
    prices = [number(r["收盘"]) for r in rows]
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    peak, drawdown = 0, 0
    for price in prices:
        peak = max(peak, price)
        drawdown = min(drawdown, price / peak - 1)
    metrics = {"日线截止": day(rows[-1].get("日期")) if rows else "", "样本交易日": len(rows),
               "MA20(前复权)": statistics.mean(prices[-20:]) if len(prices) >= 20 else None,
               "MA60(前复权)": statistics.mean(prices[-60:]) if len(prices) >= 60 else None,
               "近20交易日涨跌(%)": (prices[-1] / prices[-21] - 1) * 100 if len(prices) >= 21 else None,
               "历史样本最大回撤(%)": drawdown * 100 if len(prices) >= 2 else None,
               "近60日年化波动率(%)": statistics.stdev(returns[-60:]) * math.sqrt(252) * 100 if len(returns) >= 60 else None}
    ranges = []
    for i in range(1, len(rows)):
        high, low, prev = number(rows[i].get("最高")), number(rows[i].get("最低")), prices[i - 1]
        ranges.append(max(high - low, abs(high - prev), abs(low - prev)) if high is not None and low is not None and high >= low > 0 else None)
    metrics["ATR14简单均值(前复权)"] = statistics.mean(ranges[-14:]) if len(ranges) >= 14 and all(v is not None for v in ranges[-14:]) else None
    if rows and len(rows) >= 20:
        highs, lows = [number(r.get("最高")) for r in rows[-20:]], [number(r.get("最低")) for r in rows[-20:]]
        metrics["20日高点参考(前复权)"] = max(highs) if all(v is not None for v in highs) else None
        metrics["20日低点参考(前复权)"] = min(lows) if all(v is not None for v in lows) else None
    volumes = [number(r.get("成交量")) for r in rows]
    metrics["当日量/前5日均量"] = ratio(volumes[-1], statistics.mean(volumes[-6:-1])) if len(volumes) >= 6 and all(v is not None for v in volumes[-6:]) else None
    return metrics


def analyze_research(result, research):
    code, as_of = binding(result)
    if not isinstance(research, dict) or research.get("code") != code or research.get("as_of") != as_of:
        raise ValueError("研究数据与当前股票或行情日期不匹配，请重新查询。")
    income, balance, cash, main = (financial_rows(research, k) for k in ("income", "balance", "cashflow", "financial_metrics"))
    end = max(income) if income else ""
    b = balance.get(end) or {}
    m = main.get(end) or {}
    latest = income.get(end) or {}
    types = [r.get("ORG_TYPE") for r in (latest, b) if r]
    common = bool(types) and all(t == "通用" for t in types)
    unsupported = any(t and t != "通用" for t in types)
    industry_reason = "企业类型不适用通用行业规则，需专门行业模型。" if unsupported else "缺少明确的通用企业类型，不能套用一般行业规则。" if not common else ""
    revenue, profit, consolidated, cfo, capex = (ttm(rows, field, end) if end else None for rows, field in (
        (income, "OPERATE_INCOME"), (income, "PARENT_NETPROFIT"), (income, "NETPROFIT"), (cash, "NETCASH_OPERATE"), (cash, "CONSTRUCT_LONG_ASSET")))
    if capex is not None and capex < 0:
        capex = None
    prior_end = str(int(end[:4]) - 1) + end[4:] if end else ""
    prior_revenue = ttm(income, "OPERATE_INCOME", prior_end) if prior_end else None
    prior_profit = ttm(income, "PARENT_NETPROFIT", prior_end) if prior_end else None
    revenue_growth = positive_growth(revenue, prior_revenue)
    profit_growth = positive_growth(profit, prior_profit)
    annual_end = max((k for k in main if k.endswith("12-31")), default="")
    annual_main = main.get(annual_end) or {}
    period = f"{end[:4]}-01-01 至 {end}" if end else "缺少有效财报"
    ttm_start = (date.fromisoformat(prior_end) + timedelta(days=1)).isoformat() if prior_end else ""
    ttm_period = f"{ttm_start} 至 {end}" if end else "缺少有效财报"
    val_rows = [r for r in dataset(research, "valuation") if r.get("SECURITY_CODE") == code and day(r.get("TRADE_DATE")) and day(r["TRADE_DATE"]) <= as_of]
    val = max(val_rows, key=lambda r: day(r["TRADE_DATE"])) if val_rows else {}
    valuation_day = day(val.get("TRADE_DATE"))
    same_day = valuation_day == as_of
    price = number((result.get("quote") or {}).get("最新价"))
    # Never mix historical shares/PE with today's price as though same dated.
    pe = number(val.get("PE_TTM")) if same_day else None
    pb = number(val.get("PB_MRQ")) if same_day else None
    shares = number(val.get("TOTAL_SHARES")) if same_day else None
    cap = number((result.get("quote") or {}).get("总市值"))
    fundamentals = {"财务报告期": end, "公告日": day(latest.get("NOTICE_DATE")), "企业类型": latest.get("ORG_TYPE"),
        "TTM期间": ttm_period, "累计报告期间": period, "最近年报ROE报告期": annual_end,
        "最近年报加权ROE(%)": number(annual_main.get("ROEJQ")),
        "TTM营业收入(元)": revenue, "TTM归母净利润(元)": profit, "TTM合并净利润(元)": consolidated,
        "TTM经营现金净流量(元)": cfo, "TTM购建长期资产现金支出(元)": capex,
        "TTM简化现金余量(元)": cfo - capex if cfo is not None and capex is not None else None,
        "TTM经营现金/合并净利润": ratio(cfo, consolidated) if common else None,
        "TTM归母净利率(%)": ratio(profit, revenue, True) if common else None,
        "TTM收入同比(%)": revenue_growth, "TTM归母利润同比(%)": profit_growth,
        "上年同期TTM营业收入(元)": prior_revenue, "上年同期TTM归母净利润(元)": prior_profit,
        "TTM归母净利润同比增减额(元)": profit - prior_profit if profit is not None and prior_profit is not None else None,
        "本报告期收入同比(%)": positive_growth(number(latest.get("OPERATE_INCOME")), number((income.get(prior_end) or {}).get("OPERATE_INCOME"))),
        "连续年度收入3年CAGR(%)": annual_cagr(income, "OPERATE_INCOME"),
        "连续年度归母利润3年CAGR(%)": annual_cagr(income, "PARENT_NETPROFIT"),
        "资产负债率(%)": ratio(number(b.get("TOTAL_LIABILITIES")), number(b.get("TOTAL_ASSETS")), True) if common else None,
        "流动比率": ratio(number(b.get("TOTAL_CURRENT_ASSETS")), number(b.get("TOTAL_CURRENT_LIAB"))) if common else None,
        "报告期加权ROE(%)": number(m.get("ROEJQ")), "报告期毛利率(%)": number(m.get("XSMLL")),
        "报告期ROIC(%)": number(m.get("ROIC")), "估值日期": valuation_day, "PE_TTM": pe, "PB_MRQ": pb,
        "历史PEG": pe / profit_growth if pe is not None and pe > 0 and profit is not None and profit > 0 and profit_growth is not None and profit_growth > 0 else None,
        "总股本(股)": shares, "总市值(元)": cap, "市值/TTM归母净利(仅正利润)": ratio(cap, profit), "税前近365日现金股息率(%)": None}
    fundamentals["估值分类行业"] = val.get("BOARD_NAME") or "缺失"
    dividend_source = (research.get("sources") or {}).get("dividends")
    if isinstance(dividend_source, dict) and dividend_source.get("truncated") is False and price is not None and price > 0:
        start = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
        eligible = [r for r in dataset(research, "dividends") if r.get("SECURITY_CODE") == code and
                    "实施" in str(r.get("ASSIGN_PROGRESS")) and day(r.get("NOTICE_DATE")) and day(r["NOTICE_DATE"]) <= as_of and
                    start < day(r.get("EX_DIVIDEND_DATE")) <= as_of]
        if all(number(r.get("PRETAX_BONUS_RMB")) is not None and number(r["PRETAX_BONUS_RMB"]) >= 0 for r in eligible):
            fundamentals["税前近365日现金股息率(%)"] = sum(number(r["PRETAX_BONUS_RMB"]) / 10 for r in eligible) / price * 100
    tech = technical(result, as_of)
    peer_rows = [r for r in dataset(research, "peers") if day(r.get("TRADE_DATE")) == as_of and r.get("SECURITY_CODE") != code]
    peer_pe = [number(r.get("PE_TTM")) for r in peer_rows if number(r.get("PE_TTM")) is not None and number(r["PE_TTM"]) > 0]
    peer_pb = [number(r.get("PB_MRQ")) for r in peer_rows if number(r.get("PB_MRQ")) is not None and number(r["PB_MRQ"]) > 0]
    fundamentals["同分类正PE样本中位数"] = statistics.median(peer_pe) if peer_pe else None
    fundamentals["同分类正PB样本中位数"] = statistics.median(peer_pb) if peer_pb else None
    fundamentals["同分类正PE样本数"] = len(peer_pe)
    sector = {day(r.get("日期")): number(r.get("收盘")) for r in dataset(research, "industry")
              if day(r.get("日期")) and day(r["日期"]) <= as_of and number(r.get("收盘")) is not None and number(r["收盘"]) > 0}
    stock = {day(r.get("日期")): number(r.get("收盘")) for r in result.get("daily_history") or result.get("daily_indicators") or []
             if isinstance(r, dict) and day(r.get("日期")) and number(r.get("收盘")) is not None and number(r["收盘"]) > 0}
    aligned = sorted(set(sector) & set(stock))
    if len(aligned) >= 21 and aligned[-1] == as_of:
        industry_return = (sector[aligned[-1]] / sector[aligned[-21]] - 1) * 100
        stock_return = (stock[aligned[-1]] / stock[aligned[-21]] - 1) * 100
        tech.update({"行业对齐20区间起日": aligned[-21], "行业对齐20区间末日": aligned[-1], "行业20区间涨跌(%)": industry_return,
                     "个股20区间相对行业差(百分点)": stock_return - industry_return})
    rules = []
    def check(name, value, predicate, rule, bad, unit="", when="", unavailable="", inapplicable="", evidence="", data_known=None):
        known = number(value) is not None
        status = "不适用" if inapplicable else "缺数据" if unavailable or not known else "通过" if predicate(value) else "需复核"
        rules.append({"检查": name, "状态": status, "值": value if known else None, "单位": unit, "期间": when,
                      "规则": rule, "提示": inapplicable or unavailable or ("缺少有效同期输入，未计算；不补零、不当作通过。" if not known else bad if status == "需复核" else ""),
                      "依据": evidence, "数据已取得": bool(known if data_known is None else data_known)})
    common_args = {"inapplicable": industry_reason if unsupported else "", "unavailable": industry_reason if not common and not unsupported else ""}
    cash_loss = common and consolidated is not None and consolidated <= 0
    check("盈利状态", profit, lambda x: x > 0, "TTM归母净利润>0", "利润非正，PE/PEG不能用于便宜判断。", "元", ttm_period)
    check("利润现金兑现", fundamentals["TTM经营现金/合并净利润"], lambda x: x >= 1, "经营现金/正合并净利润≥1（通用行业启发式）", "利润现金兑现偏弱，检查应收与存货。", "倍", ttm_period,
          inapplicable=common_args["inapplicable"] or ("合并净利润非正，现金兑现比率不适用；不是财报未抓到。" if cash_loss else ""), unavailable=common_args["unavailable"],
          evidence=f"TTM经营现金 {display(cfo, '元')}；TTM合并净利润 {display(consolidated, '元')}", data_known=cfo is not None and consolidated is not None)
    check("资本投入后现金", fundamentals["TTM简化现金余量(元)"], lambda x: x > 0, "经营现金−购建长期资产现金支出>0", "投入后现金为负，可能扩张或经营承压，应结合项目和融资。", "元", ttm_period,
          evidence=f"经营现金 {display(cfo, '元')} − 购建长期资产支出 {display(capex, '元')}；不是严格自由现金流", **common_args)
    check("负债约束", fundamentals["资产负债率(%)"], lambda x: 0 <= x < 65, "资产负债率<65%（非金融行业复核阈值）", "杠杆偏高或净资产异常，检查付息和到期债务。", "%", f"{end} 期末余额", **common_args)
    check("短期偿债", fundamentals["流动比率"], lambda x: x >= 1, "流动比率≥1（通用行业复核阈值）", "短期负债超过流动资产，需查现金受限与再融资。", "倍", f"{end} 期末余额", **common_args)
    def growth_args(current, prior):
        known = current is not None and prior is not None
        return {"inapplicable": "本期或上年同期基数非正，不计算增长百分比；原始金额已保留。" if known and (current <= 0 or prior <= 0) else "", "data_known": known}
    check("收入成长", revenue_growth, lambda x: x >= 10, "正基数TTM收入同比≥10%", "成长不足阈值，不代表公司没有价值。", "%", ttm_period, **growth_args(revenue, prior_revenue))
    change = fundamentals["TTM归母净利润同比增减额(元)"]
    comparison = "；亏损扩大" if profit is not None and prior_profit is not None and profit < prior_profit < 0 else "；亏损收窄" if profit is not None and prior_profit is not None and prior_profit < profit < 0 else ""
    check("利润成长", profit_growth, lambda x: x >= 10, "正基数TTM归母净利润同比≥10%", "利润增长不足，需核查周期和一次性收益。", "%", ttm_period,
          evidence=f"本期TTM {display(profit, '元')}；上年同期TTM {display(prior_profit, '元')}；增减额 {display(change, '元')}{comparison}", **growth_args(profit, prior_profit))
    check("资本回报", fundamentals["最近年报加权ROE(%)"], lambda x: x >= 10, "最近年报加权ROE≥10%；中报单列，不套全年阈值、不自动年化", "资本回报不足阈值，注意杠杆对ROE的影响。", "%", f"{annual_end[:4]} 全年（截至 {annual_end}）" if annual_end else "缺少年报",
          evidence=f"最新累计报告期 {end} 的加权ROE {display(fundamentals['报告期加权ROE(%)'], '%')}，不与全年阈值直接比较")
    check("扣非盈利", number(latest.get("DEDUCT_PARENT_NETPROFIT")), lambda x: x > 0, "报告期扣非归母净利润>0", "扣非利润非正，需查非经常性损益。", "元", period)
    check("中期价格趋势", tech.get("MA20(前复权)") - tech.get("MA60(前复权)") if tech.get("MA20(前复权)") is not None and tech.get("MA60(前复权)") is not None else None,
          lambda x: x > 0, "前复权MA20>MA60；显示MA20−MA60差值，仅描述过去", "中期均线未形成该多头关系。", "元/股", f"日线截止 {tech.get('日线截止') or '缺失'}",
          unavailable="日线未与行情日对齐，不作为当前趋势结论。" if tech.get("日线截止") != as_of else "",
          data_known=tech.get("日线截止") == as_of and tech.get("MA20(前复权)") is not None and tech.get("MA60(前复权)") is not None)
    evaluated = sum(r["状态"] in {"通过", "需复核"} for r in rules)
    return {"algorithm": "现金—成长—估值证据检查 v2", "code": code, "as_of": as_of, "fundamentals": fundamentals,
            "technical": tech, "rules": rules, "coverage_percent": evaluated / len(rules) * 100,
            "data_coverage_percent": sum(r["数据已取得"] for r in rules) / len(rules) * 100,
            "summary": {status: sum(r["状态"] == status for r in rules) for status in ("通过", "需复核", "不适用", "缺数据")},
            "method": ["独立规则算法，参考通用现金流估值、公司成长与风险原则；不是书籍原文公式，也非作者背书。",
                "TTM=本期累计+上年全年−上年同期；无同期报表不计算。资产负债表不累计。",
                "简化现金余量=CFO−购建长期资产支出，不是严格FCFF/FCFE，不自动用于DCF。",
                "历史PEG=正PE÷正利润增长百分数，仅为历史对照，不等于预期PEG。负基数增长不计算。",
                "3年CAGR需4个连续年度且所有年度数值为正；不把短期同比机械当作长期成长。",
                "最近年报ROE与全年阈值比较；最新中报/季报ROE单列且不年化。",
                "规则评价率=(通过+需复核)/全部10条；数据取得率按有效输入统计，含亏损导致不适用的已取得数据。均非来源完整率、投资评分或上涨概率。",
                "银行、证券、保险等非通用企业禁用一般流动性/负债/现金兑现规则，需行业专门模型。",
                "没有策略回测、未来盈利预测或收益保证；护城河、行业空间和管理层需要独立证据。"]}


def research_quality_checks(result):
    research = result.get("research")
    if not isinstance(research, dict):
        return []
    code, as_of = binding(result)
    def item(name, status, detail):
        return {"item": name, "status": status, "severity": status, "detail": detail}
    if (research.get("code"), research.get("as_of")) != (code, as_of):
        return [item("研究数据股票/日期", "warning", "较早研究数据与行情不匹配，不用于当前分析/导出。")]
    checks = [item("研究数据股票/日期", "ok", f"{code} / {as_of}；仅日期级匹配，各来源独立截止时间，不作同一时刻回测。")]
    sources = research.get("sources") or {}
    missing_keys = set(research.get("errors") or {}) | set(research.get("missing_sources") or []) | (set(SOURCE_LABELS) - set(sources))
    empty = [k for k, v in sources.items() if not v.get("rows")]
    checks.append(item("研究来源覆盖", "warning" if missing_keys or empty else "ok",
                       f"计划13类来源，本次返回{len(sources)}类；未取得{len(missing_keys)}类、返回空记录{len(empty)}类。"
                       + ("未取得：" + "、".join(SOURCE_LABELS.get(k, k) for k in sorted(missing_keys)) + "。" if missing_keys else "")
                       + "与规则评价率分开统计；截取窗口不是全部历史，空记录不等于零。"))
    for key, fields, label in (
        ("balance", ("TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"), "财报资产负债恒等式"),
        ("cashflow", ("NETCASH_OPERATE", "NETCASH_INVEST", "NETCASH_FINANCE", "RATE_CHANGE_EFFECT", "CCE_ADD"), "财报现金流勾稽")):
        rows = financial_rows(research, key)
        evaluated, errors = 0, 0
        for row in rows.values():
            values = [number(row.get(k)) for k in fields]
            if any(v is None for v in values):
                continue
            if key == "balance":
                difference, scale = values[0] - values[1] - values[2], abs(values[0])
            else:
                difference, scale = sum(values[:-1]) - values[-1], max(map(abs, values))
            evaluated += 1
            errors += abs(difference) > max(1, scale * 1e-8)
        checks.append(item(label, "unavailable" if not evaluated else "warning" if errors or evaluated < len(rows) else "ok",
                           f"可核验{evaluated}/{len(rows)}期，超出舍入容差{errors}期；{len(rows) - evaluated}期缺少核验字段。仅说明可核验部分，不代表财务健康；缺字段不补零。"))
    val = [r for r in dataset(research, "valuation") if day(r.get("TRADE_DATE")) == as_of and r.get("SECURITY_CODE") == code]
    quote = result.get("quote") or {}
    expected = number(val[0].get("TOTAL_SHARES")) if val else None
    price, cap = number(quote.get("最新价")), number(quote.get("总市值"))
    if expected is not None and expected > 0 and price is not None and price > 0 and cap is not None:
        difference = cap - expected * price
        checks.append(item("总市值/股本/价格口径", "ok" if abs(difference) <= max(100, abs(cap) * 0.001) else "warning",
                           f"行情总市值−同日总股本×报价={display(difference, '元')}"
                           + (f"（相对总市值{difference / cap * 100:+.4f}%）" if cap > 0 else "")
                           + "；0.1%容差考虑报价和跨源舍入，不覆盖历史股本。"))
    return checks


def dcf_fcfe(fcfe, shares, growth_percent, discount_percent, terminal_percent, years=5):
    values = [number(v) for v in (fcfe, shares, growth_percent, discount_percent, terminal_percent)]
    if any(v is None for v in values) or isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= 10:
        raise ValueError("DCF需要有限数值与1～10年预测期。")
    fcfe, shares, growth, discount, terminal = values
    if fcfe <= 0 or shares <= 0 or not -50 <= growth <= 50 or not 0 < discount <= 50 or not -5 <= terminal <= 5 or terminal >= discount:
        raise ValueError("FCFE/股本须为正，增长−50～50%，折现0～50%，永续−5～5%且低于折现率。")
    growth, discount, terminal = growth / 100, discount / 100, terminal / 100
    flows = [fcfe * (1 + growth) ** year for year in range(1, years + 1)]
    present = sum(flow / (1 + discount) ** (i + 1) for i, flow in enumerate(flows))
    terminal_value = flows[-1] * (1 + terminal) / (discount - terminal) / (1 + discount) ** years
    return {"股权情景价值(元)": present + terminal_value, "每股情景值(元)": (present + terminal_value) / shares,
            "终值占比(%)": terminal_value / (present + terminal_value) * 100,
            "假设": {"确认FCFE(元/年)": fcfe, "股本(股)": shares, "增长(%)": growth_percent, "股权折现率(%)": discount_percent, "永续增长(%)": terminal_percent, "年数": years},
            "限制": "手填股权自由现金流FCFE情景，不是预测目标价；不再扣净负债。CFO减资本支出不能直接冒充FCFE。"}


def position_plan(capital, entry, stop, target, risk_percent, max_position_percent):
    values = [number(v) for v in (capital, entry, stop, target, risk_percent, max_position_percent)]
    if any(v is None for v in values):
        raise ValueError("请填写有限数值。")
    capital, entry, stop, target, risk, limit = values
    if not capital > 0 or not 0 < stop < entry < target or not 0 < risk <= 10 or not 0 < limit <= 100:
        raise ValueError("资金须为正，0<止损<入场<目标；单笔风险0～10%，仓位上限0～100%。")
    count = int(min(capital * risk / 100 / (entry - stop), capital * limit / 100 / entry) // 100) * 100
    return {"示例数量(股)": count, "示例金额(元)": count * entry, "名义止损风险(元)": count * (entry - stop),
            "盈亏比(未计费)": (target - entry) / (entry - stop),
            "限制": "手工假设、100股整手简化；非所有市场买入单位，未计手续费、跳空、滑点或无法成交，不保证止损上限。"}


def render_module(module, result, research, analysis):
    """Readable modules with source/date/units, not a fabricated professional report."""
    f, tech = analysis["fundamentals"], analysis["technical"]
    quote = result.get("quote") or {}
    lines = [f"{analysis['code']} {quote.get('名称') or ''} · 行情日 {analysis['as_of']} · {module}",
             "缺失≠0；这是证据检查，不是价格预测或买卖评分。",
             f"财报截至 {f['财务报告期'] or '未取得'} · 最新利润表公告 {f['公告日'] or '未取得'} · 研究采集 {(research.get('fetched_at') or research.get('captured_at') or result.get('fetched_at') or '未标注')}",
             "采集日不等于行情日；各来源独立截止时间，不能当作同一时刻截面。仅用于研究，不构成买卖建议。", ""]
    if module == "综合检查":
        counts = analysis["summary"]
        lines += [analysis["algorithm"], " · ".join(f"{k} {counts.get(k, 0)}项" for k in ("通过", "需复核", "不适用", "缺数据")),
                  f"规则评价率 {analysis['coverage_percent']:.0f}%（通过+需复核/全部规则） · 数据取得率 {analysis['data_coverage_percent']:.0f}%（含已知但不适用）",
                  "两者都不是来源完整率、财务健康分或上涨概率。需复核=未达到启发式阈值；不适用≠没抓到；缺数据=输入不足/日期或口径不匹配。", "",
                  f"TTM期间：{f['TTM期间']}（不是最新半年累计数）",
                  f"经营现金 {display(f['TTM经营现金净流量(元)'], '元')}；购建长期资产支出 {display(f['TTM购建长期资产现金支出(元)'], '元')}",
                  f"本报告期累计收入同比 {display(f['本报告期收入同比(%)'], '%')}；TTM收入同比 {display(f['TTM收入同比(%)'], '%')}（期间不同）", ""]
        for r in analysis["rules"]:
            value = "不计算" if r["状态"] == "不适用" else "未计算" if r["状态"] == "缺数据" else display(r["值"], r["单位"])
            lines += [f"[{r['状态']}] {r['检查']}：{value} · {r['期间']}"]
            if r["依据"]:
                lines.append("依据：" + r["依据"])
            lines.append(f"规则：{r['规则']}" + (f"；{r['提示']}" if r['提示'] else ""))
            lines.append("")
        labels = {"ok": "核验通过", "warning": "需核对/部分核验", "unavailable": "无法核验"}
        lines += ["数据质量核验（核验通过不代表经营健康）", *[f"{r['item']}：{labels.get(r['status'], r['status'])} · {r['detail']}" for r in research_quality_checks({**result, "research": research})], "", "算法说明", *analysis["method"]]
    elif module == "股票基础":
        quote = result.get("quote") or {}
        lines += [f"名称：{quote.get('名称') or '缺失'}", f"最新价：{display(quote.get('最新价'), '元/股')}",
                  f"总市值：{display(quote.get('总市值'), '元')}", f"流通市值：{display(quote.get('流通市值'), '元')}",
                  f"换手率：{display(quote.get('换手率(%)'), '%')}", f"行情时间：{quote.get('行情时间') or '缺失'}"]
        lines += [f"{k}：{metric_display(k, f.get(k))}" for k in ("总股本(股)", "PE_TTM", "PB_MRQ", "报告期加权ROE(%)", "最近年报加权ROE(%)", "税前近365日现金股息率(%)")]
        lines += [f"估值日期 {f['估值日期'] or '缺失'}，财务报告期 {f['财务报告期'] or '缺失'}；ROE为报告期加权值。",
                  "税前股息：已实施除息方案近365日每10股派现÷10÷当前价格，非预期收益；分页不全时不计算。"]
    elif module == "财务报表":
        for key, label in (("income", "利润表"), ("balance", "资产负债表"), ("cashflow", "现金流量表")):
            rows = financial_rows(research, key)
            lines += ["", label + "（元；股本与EPS例外）", "累计发生额，不是单季度" if key != "balance" else "期末余额，不是累计发生额"]
            for end in sorted(rows, reverse=True):
                row = rows[end]
                lines += [f"报告期 {end} · 公告 {day(row.get('NOTICE_DATE'))} · {row.get('ORG_TYPE') or '未知类型'}"]
                lines += [f"  {FIELD_LABELS[k]}：{display(row.get(k), '股' if k == 'SHARE_CAPITAL' else '元/股' if k == 'BASIC_EPS' else '元')}" for k in FIELD_LABELS if k in row]
        lines += ["", "同期TTM及质量指标", *[f"{k}：{v if isinstance(v, str) else metric_display(k, v)}" for k, v in f.items() if k.startswith("TTM") or k in ("资产负债率(%)", "流动比率", "报告期ROIC(%)")]]
    elif module == "公司分析":
        profiles = dataset(research, "profile")
        p = profiles[0] if profiles else {}
        labels = {"ORG_NAME": "公司", "MAIN_BUSINESS": "主营业务", "ORG_PROFILE": "公司概况", "ORG_PROFIE": "公司介绍",
                  "EM2016": "行业分类", "BOARD_NAME_2LEVEL": "行情行业", "BLGAINIAN": "概念标签", "CHAIRMAN": "董事长", "PRESIDENT": "总经理",
                  "REAL_CONTROLER": "实际控制人", "CONTROL_HOLDER": "控股股东", "ACCOUNT_FIRM": "审计机构", "LISTING_DATE": "上市日期", "TOTAL_NUM": "员工人数"}
        lines += [f"{label}：{p.get(k) or '缺失'}" for k, label in labels.items()]
        lines += ["", "定性核查（不自动认定有护城河）", "商业模式：客户、产品、收入来源、毛利率稳定性和现金回收。",
                  "壁垒：切换成本、专利、成本优势、客户集中度须核对原始年报与竞争对手。",
                  "行业空间：竞争格局、需求和周期，不能把概念标签当作业绩成长。", "管理层：关联交易、资本配置、承诺兑现、审计意见和激励约束。",
                  "林奇式公司类型核查：缓慢成长、稳定成长、快速成长、周期、困境反转、资产价值。仅凭代码/概念/单季增长不能自动归类。",
                  f"连续年度收入3年CAGR：{display(f.get('连续年度收入3年CAGR(%)'), '%')}；归母利润3年CAGR：{display(f.get('连续年度归母利润3年CAGR(%)'), '%')}",
                  "公司资料是当前版本；历史回测不得直接回填。", "", "近期公告标题（需打开核实全文）"]
        lines += [f"{r.get('日期')} {r.get('标题')}\n{r.get('链接')}" for r in dataset(research, "announcements")]
    elif module == "股票估值":
        lines += [f"估值分类行业：{f['估值分类行业']}"]
        lines += [f"{k}：{metric_display(k, f.get(k))}" for k in ("PE_TTM", "PB_MRQ", "市值/TTM归母净利(仅正利润)", "历史PEG", "TTM简化现金余量(元)", "同分类正PE样本中位数", "同分类正PB样本中位数", "同分类正PE样本数")]
        lines += ["", "PE负数或利润非正不代表便宜；成长率为负或基数非正时PEG不计算。",
                  "PB需要结合资产质量和ROE，不能跨行业机械比较。相对估值需同业及盈利周期对照。",
                  "DCF仅在下方手工填入确认的FCFE和情景假设后计算；终值占比高时更敏感。",
                  "简化现金余量不是严格FCFF/FCFE，未自动计算目标价。", "", "同业估值原始对照（分类相同不保证业务可比）"]
        lines += [str(r) for r in dataset(research, "peers")]
    elif module == "量价/技术分析":
        lines += [f"{k}：{metric_display(k, v) if number(v) is not None else v or '缺失'}" for k, v in tech.items()]
        lines += ["", "高低点仅为历史支撑/压力参考，不是已验证支撑。历史价为前复权口径，不直接当作可交易报价。",
                  "盘中当日K线、均线、量比仍会变化。ATR为14日简单均值，与Wilder平滑ATR可能不同。",
                  "详细MACD/RSI/BOLL等已在历史指标页；没有价格成交量不能还原真实筹码持仓。"]
    elif module == "资金与市场":
        lines += ["资金流（元，行情商订单大小估算，不是真实主力身份）"]
        lines += [str(row) for row in dataset(research, "flow")[-30:]]
        lines += ["", "最新已公告流通股东（季报快照，不是实时持仓）"]
        holders = dataset(research, "holders")
        latest = max((day(r.get("END_DATE")) for r in holders), default="")
        lines += [str({k: r.get(k) for k in ("END_DATE", "NOTICE_DATE", "HOLDER_NAME", "HOLD_NUM", "FREE_HOLDNUM_RATIO", "HOLDER_STATE")}) for r in holders if day(r.get("END_DATE")) == latest]
        lines += ["北向仅展示季报中香港中央结算等名义持有人，不等于实际受益人或每日净买入。",
                  "龙虎榜在现有页，宽基指数在走势图；它们不等于该公司行业指数。", "", "行业板块当日强弱（非历史轮动）"]
        lines += [str(row) for row in dataset(research, "boards")[:30]]
        industry = (research.get("sources") or {}).get("industry") or {}
        lines += ["", f"所属行业指数：{industry.get('name', '缺失')} {industry.get('code', '')}",
                  *[f"{k}：{v}" for k, v in tech.items() if "行业" in k], "最近10个行业交易日"]
        lines += [str(r) for r in dataset(research, "industry")[-10:]]
    elif module == "仓位和风控":
        lines += [f"{k}：{metric_display(k, tech.get(k))}" for k in ("历史样本最大回撤(%)", "近60日年化波动率(%)", "ATR14简单均值(前复权)")]
        lines += ["", "下方可按手工入场、止损、目标和资金做名义风险预算；不会替你设置买卖点或下单。",
                  "计算数量=min(风险预算÷每股风险, 仓位预算÷入场价)，向下取100股整手。",
                  "最大回撤是历史样本回撤，不是未来亏损上限。", "交易心理清单：记录理由和失效条件，不因亏损盲目加仓；检查集中度与关联行业暴露。"]
    lines += ["", "来源与覆盖"]
    for key, source in (research.get("sources") or {}).items():
        lines.append(f"{SOURCE_LABELS.get(key, key)}：{len(source.get('rows') or [])}行" + (" · 历史窗口/分页未全（不一定缺最新期）" if source.get("truncated") else " · 本次返回窗口")
                     + f" · {SOURCE_NOTES.get(key, source.get('note', ''))} · 来源 {source.get('source')}")
    lines += [f"未取得 {SOURCE_LABELS.get(k, k)}：{v}" for k, v in (research.get("errors") or {}).items()]
    lines += ["当前修订版供当前研究，不是完整历史时点库；不据此声称无前视偏差回测。"]
    lines += ["", *research.get("limitations", [])]
    return "\n".join(lines)
