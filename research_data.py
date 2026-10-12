"""Public, bounded research queries. No AI, account access or order placement.

Financial flows are cumulative year-to-date; balance sheets are point-in-time.
The current vendor revision is not a point-in-time historical database.
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode


DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
META = ("SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE", "ORG_TYPE", "CURRENCY", "REPORT_TYPE")
REPORTS = {
    "income": ("RPT_F10_FINANCE_GINCOME", "REPORT_DATE", 80, META + (
        "OPERATE_INCOME", "TOTAL_OPERATE_INCOME", "OPERATE_COST", "OPERATE_PROFIT", "TOTAL_PROFIT",
        "NETPROFIT", "PARENT_NETPROFIT", "DEDUCT_PARENT_NETPROFIT", "RESEARCH_EXPENSE",
        "SALE_EXPENSE", "MANAGE_EXPENSE", "FINANCE_EXPENSE", "FE_INTEREST_EXPENSE", "BASIC_EPS", "OPINION_TYPE")),
    "balance": ("RPT_F10_FINANCE_GBALANCE", "REPORT_DATE", 80, META + (
        "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY", "TOTAL_PARENT_EQUITY", "TOTAL_CURRENT_ASSETS",
        "TOTAL_CURRENT_LIAB", "MONETARYFUNDS", "ACCOUNTS_RECE", "NOTE_ACCOUNTS_RECE", "INVENTORY",
        "SHORT_LOAN", "LONG_LOAN", "NONCURRENT_LIAB_1YEAR", "LEASE_LIAB", "BOND_PAYABLE", "GOODWILL", "SHARE_CAPITAL")),
    "cashflow": ("RPT_F10_FINANCE_GCASHFLOW", "REPORT_DATE", 80, META + (
        "NETCASH_OPERATE", "CONSTRUCT_LONG_ASSET", "DISPOSAL_LONG_ASSET", "NETCASH_INVEST", "NETCASH_FINANCE",
        "SALES_SERVICES", "RECEIVE_LOAN_CASH", "PAY_DEBT_CASH", "ASSIGN_DIVIDEND_PORFIT", "CCE_ADD", "BEGIN_CCE", "END_CCE", "RATE_CHANGE_EFFECT")),
    "financial_metrics": ("RPT_F10_FINANCE_MAINFINADATA", "REPORT_DATE", 80, META + (
        "ROEJQ", "ROIC", "XSMLL", "XSJLL", "ZCFZL", "LD", "SD", "RDEXPEND", "BPS", "EPSJB")),
    "profile": ("RPT_F10_ORG_BASICINFO", "SECURITY_CODE", 1, (
        "SECURITY_CODE", "ORG_NAME", "ORG_TYPE", "MAIN_BUSINESS", "ORG_PROFILE", "ORG_PROFIE", "CHAIRMAN",
        "PRESIDENT", "REAL_CONTROLER", "CONTROL_HOLDER", "TOTAL_NUM", "LISTING_DATE", "ACCOUNT_FIRM",
        "EM2016", "CSRC_INDUSTRY_NAME", "BOARD_CODE_BK_2LEVEL", "BOARD_NAME_2LEVEL", "BLGAINIAN", "MAX_DATE")),
    "valuation": ("RPT_VALUEANALYSIS_DET", "TRADE_DATE", 80, (
        "SECURITY_CODE", "TRADE_DATE", "CLOSE_PRICE", "TOTAL_MARKET_CAP", "NOTLIMITED_MARKETCAP_A", "TOTAL_SHARES",
        "FREE_SHARES_A", "PE_TTM", "PE_LAR", "PB_MRQ", "PS_TTM", "PCF_OCF_TTM", "BOARD_CODE", "BOARD_NAME")),
    "dividends": ("RPT_SHAREBONUS_DET", "EX_DIVIDEND_DATE", 80, (
        "SECURITY_CODE", "NOTICE_DATE", "EX_DIVIDEND_DATE", "PRETAX_BONUS_RMB", "ASSIGN_PROGRESS", "REPORT_DATE")),
    "holders": ("RPT_F10_EH_FREEHOLDERS", "END_DATE,HOLDER_RANK", 80, (
        "SECURITY_CODE", "END_DATE", "NOTICE_DATE", "UPDATE_DATE", "HOLDER_NAME", "HOLD_NUM", "FREE_HOLDNUM_RATIO",
        "HOLD_RATIO", "HOLDER_RANK", "HOLDER_TYPE", "HOLDER_STATE", "IS_LANDSTOCK")),
}
REPORT_NOTES = {
    "income": "累计发生额，人民币元；EPS为元/股。",
    "balance": "期末余额，人民币元；股本为股。",
    "cashflow": "累计发生额，人民币元。",
    "financial_metrics": "ROE/ROIC/毛净利率/负债率为%；流动与速动比率为倍，BPS/EPS为元/股。",
    "profile": "当前公司业务、分类及管理层资料，不是历史时点档案。",
    "valuation": "估值倍数为倍、市值为人民币元、股本为股。",
    "dividends": "每10股税前现金分红为元，仅计已实施且已除息方案。",
    "holders": "持股数为股，比例为%；已公告流通股东季报，不是实时实际受益人持仓。",
}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def day(value):
    value = str(value or "")
    token = value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else value[:8]
    try:
        return datetime.strptime(token, "%Y-%m-%d" if "-" in token else "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def binding(result):
    if not isinstance(result, dict):
        return "", ""
    return str((result.get("identity") or {}).get("code") or ""), day((result.get("quote") or {}).get("行情时间"))


def clean_row(row, fields):
    """Only vendor fields selected above; no arbitrary payload or personal contacts."""
    clean = {}
    for key in fields:
        value = row.get(key)
        if value is None or isinstance(value, bool):
            clean[key] = None
        elif isinstance(value, str):
            clean[key] = value[:2400]
        elif isinstance(value, (int, float)):
            clean[key] = value if number(value) is not None else None
    return clean


def available_financial(row, as_of):
    report, notice, update = (day(row.get(key)) for key in ("REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE"))
    # Never use a report merely because its reporting period has ended.
    return bool(report and notice and report <= as_of and notice <= as_of and (not update or update <= as_of))


class ResearchClient:
    def __init__(self, client):
        self.client = client

    def _json(self, url):
        # Keep the collector's established Windows HTTPS fallback for quote
        # hosts that intermittently close Python connections. No credentials.
        quote_host = url.startswith(("https://push2.eastmoney.com/", "https://push2his.eastmoney.com/"))
        return self.client.json(url, "https://quote.eastmoney.com/", powershell_fallback=quote_host)

    def report(self, key, code, as_of):
        if not re.fullmatch(r"\d{6}", code) or day(as_of) != as_of:
            raise ValueError("股票代码或研究日期无效")
        name, sort, limit, fields = REPORTS[key]
        query = {"reportName": name, "columns": ",".join(fields), "filter": f'(SECURITY_CODE="{code}")',
                 "pageSize": limit, "pageNumber": 1, "sortColumns": sort,
                 "sortTypes": "-1,1" if "," in sort else "-1", "source": "WEB", "client": "WEB"}
        if key == "valuation":
            query["filter"] += f'(TRADE_DATE<=\'{as_of}\')'
        url = DATA_URL + "?" + urlencode(query)
        payload = self._json(url)
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or payload.get("success") is not True or not isinstance(result.get("data"), list):
            raise ValueError("数据源未返回有效研究数据")
        rows, excluded = [], 0
        for row in result["data"][:limit]:
            if not isinstance(row, dict) or str(row.get("SECURITY_CODE")) != code:
                excluded += 1
                continue
            valid = True
            if key in {"income", "balance", "cashflow", "financial_metrics"}:
                valid = available_financial(row, as_of)
            elif key == "valuation":
                valid = bool(day(row.get("TRADE_DATE")) and day(row["TRADE_DATE"]) <= as_of)
            elif key == "holders":
                valid = bool(day(row.get("END_DATE")) and day(row.get("NOTICE_DATE")) and
                             day(row["END_DATE"]) <= as_of and day(row["NOTICE_DATE"]) <= as_of and
                             (not day(row.get("UPDATE_DATE")) or day(row["UPDATE_DATE"]) <= as_of))
            elif key == "dividends":
                valid = bool(day(row.get("NOTICE_DATE")) and day(row["NOTICE_DATE"]) <= as_of and
                             day(row.get("EX_DIVIDEND_DATE")) and day(row["EX_DIVIDEND_DATE"]) <= as_of)
            if valid:
                rows.append(clean_row(row, fields))
            else:
                excluded += 1
        count = number(result.get("count"))
        coverage_uncertain = count is None or count > limit or count > len(result["data"]) or len(result["data"]) > limit
        return {"rows": rows, "source": name, "url": url, "as_of": as_of, "reported_count": count,
                "truncated": coverage_uncertain, "excluded_rows": excluded,
                "note": "当前修订版，不是历史时点数据库；" + REPORT_NOTES[key]}

    def flow(self, code, secid, as_of):
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?" + urlencode({
            "lmt": 120, "klt": 101, "secid": secid, "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"})
        data = (self._json(url) or {}).get("data")
        if not isinstance(data, dict) or str(data.get("code")) != code or not isinstance(data.get("klines"), list):
            raise ValueError("资金流来源无有效返回")
        rows = []
        for line in data["klines"][:120]:
            cells = str(line).split(",")
            if len(cells) == 13 and day(cells[0]) and day(cells[0]) <= as_of:
                rows.append({"日期": day(cells[0]), "主力净流入(元)": number(cells[1]), "小单净流入(元)": number(cells[2]),
                             "中单净流入(元)": number(cells[3]), "大单净流入(元)": number(cells[4]),
                             "超大单净流入(元)": number(cells[5]), "主力净流入占比(%)": number(cells[6])})
        return {"rows": sorted(rows, key=lambda r: r["日期"]), "source": "东方财富资金流分类", "url": url,
                "as_of": as_of, "note": "行情商按订单大小分类的估算，不是机构真实买卖或持仓；最多120日。"}

    def announcements(self, code, as_of):
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann?" + urlencode({
            "sr": -1, "page_size": 30, "page_index": 1, "ann_type": "A", "stock_list": code})
        data = (self._json(url) or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError("公告来源无有效返回")
        rows = []
        for row in data["list"][:30]:
            if not isinstance(row, dict) or not any(str(c.get("stock_code")) == code for c in row.get("codes", []) if isinstance(c, dict)):
                continue
            date = day(row.get("notice_date"))
            article = str(row.get("art_code") or "")
            if date and date <= as_of and re.fullmatch(r"AN\d{10,30}", article):
                rows.append({"日期": date, "标题": str(row.get("title") or "")[:500],
                             "链接": f"https://data.eastmoney.com/notices/detail/{code}/{article}.html"})
        return {"rows": rows, "source": "东方财富公司公告索引", "url": url, "as_of": as_of,
                "note": "最近30项公告标题，不等于读过公告正文；并非全部新闻。"}

    def boards(self, as_of):
        url = "https://push2.eastmoney.com/api/qt/clist/get?" + urlencode({"pn": 1, "pz": 100, "po": 1,
            "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14,f3,f62,f124", "fltt": 2})
        data = (self._json(url) or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("diff"), (dict, list)):
            raise ValueError("板块来源无有效返回")
        diff = data["diff"].values() if isinstance(data["diff"], dict) else data["diff"]
        rows = []
        for row in diff:
            if not isinstance(row, dict) or not re.fullmatch(r"BK\d{4}", str(row.get("f12"))):
                continue
            stamp = number(row.get("f124"))
            from datetime import timezone, timedelta
            try:
                date = datetime.fromtimestamp(stamp, timezone(timedelta(hours=8))).date().isoformat() if stamp else ""
            except (OSError, ValueError, OverflowError):
                date = ""
            # Undated/current data must never be silently paired with an old quote.
            if date and date <= as_of:
                rows.append({"代码": row["f12"], "板块": str(row.get("f14"))[:80], "日期": date,
                             "涨跌幅(%)": number(row.get("f3")), "估算主力净流入(元)": number(row.get("f62"))})
        return {"rows": rows, "source": "东方财富行业板块当日排名", "url": url, "as_of": as_of,
                "note": "按当日涨幅排序的前100项，不是历史行业轮动回测库；不同分类层级可重叠。"}

    def peers(self, board, as_of):
        if not re.fullmatch(r"\d{6}", board):
            raise ValueError("行业分类代码无效")
        fields = ("SECURITY_CODE", "SECURITY_NAME_ABBR", "TRADE_DATE", "PE_TTM", "PB_MRQ", "TOTAL_MARKET_CAP", "BOARD_NAME")
        url = DATA_URL + "?" + urlencode({"reportName": "RPT_VALUEANALYSIS_DET", "columns": ",".join(fields),
            "filter": f'(BOARD_CODE="{board}")(TRADE_DATE=\'{as_of}\')', "pageSize": 100, "pageNumber": 1,
            "sortColumns": "TOTAL_MARKET_CAP", "sortTypes": "-1", "source": "WEB", "client": "WEB"})
        payload = self._json(url)
        data = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("success") is not True or not isinstance(data.get("data"), list):
            raise ValueError("同业来源无有效返回")
        rows = [clean_row(r, fields) for r in data["data"][:100] if isinstance(r, dict) and
                re.fullmatch(r"\d{6}", str(r.get("SECURITY_CODE"))) and day(r.get("TRADE_DATE")) == as_of]
        count = number(data.get("count"))
        return {"rows": rows, "source": "东方财富同分类估值对照", "url": url, "as_of": as_of,
                "reported_count": count, "truncated": count is None or count > 100,
                "note": "同分类按市值取前100家公司；分类不保证业务/财务可比，不计算买卖排名。"}

    def industry(self, board, as_of):
        if not re.fullmatch(r"BK\d{4}", board):
            raise ValueError("行业行情代码无效")
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode({"secid": "90." + board,
            "klt": 101, "fqt": 0, "lmt": 120, "end": as_of.replace("-", ""), "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"})
        data = (self._json(url) or {}).get("data")
        if not isinstance(data, dict) or data.get("code") != board or not isinstance(data.get("klines"), list):
            raise ValueError("行业日线来源无有效返回")
        rows = []
        for line in data["klines"][:120]:
            cells = str(line).split(",")
            if len(cells) >= 7 and day(cells[0]) and day(cells[0]) <= as_of:
                rows.append({"日期": day(cells[0]), "开盘": number(cells[1]), "收盘": number(cells[2]),
                             "最高": number(cells[3]), "最低": number(cells[4]), "成交量": number(cells[5]), "成交额": number(cells[6])})
        return {"rows": sorted(rows, key=lambda r: r["日期"]), "source": "东方财富行业指数日线", "name": str(data.get("name"))[:80],
                "code": board, "url": url, "as_of": as_of, "note": "当前分类所属行业指数，最多120日；不是历史成分/PIT轮动回测库。"}

    def collect(self, result, cancel_event=None):
        code, as_of = binding(result)
        secid = str((result.get("identity") or {}).get("secid") or "")
        quote_code = str((result.get("quote") or {}).get("代码") or "")
        if not re.fullmatch(r"\d{6}", code) or quote_code != code or not as_of or not re.fullmatch(r"[01]\." + code, secid):
            raise ValueError("先取得代码和行情日期一致的股票数据，再查询研究数据。")
        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError("研究查询已取消")
        cancelled()
        tasks = {key: (lambda k=key: self.report(k, code, as_of)) for key in REPORTS}
        tasks.update(flow=lambda: self.flow(code, secid, as_of), announcements=lambda: self.announcements(code, as_of),
                     boards=lambda: self.boards(as_of))
        sources, errors = {}, {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="stock-research") as pool:
            pending = {pool.submit(work): key for key, work in tasks.items()}
            for future in as_completed(pending):
                if cancel_event is not None and cancel_event.is_set():
                    for job in pending:
                        job.cancel()
                    break
                key = pending[future]
                try:
                    sources[key] = future.result()
                except Exception:
                    errors[key] = "来源暂不可用或数据格式不符合约定；未用0代替。"
        cancelled()
        latest_val = max(sources.get("valuation", {}).get("rows", []), key=lambda r: day(r.get("TRADE_DATE")), default={})
        profile = (sources.get("profile", {}).get("rows") or [{}])[0]
        extra = {}
        if latest_val.get("BOARD_CODE") and day(latest_val.get("TRADE_DATE")) == as_of:
            extra["peers"] = lambda: self.peers(str(latest_val["BOARD_CODE"]), as_of)
        if profile.get("BOARD_CODE_BK_2LEVEL"):
            extra["industry"] = lambda: self.industry(str(profile["BOARD_CODE_BK_2LEVEL"]), as_of)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="stock-research-industry") as pool:
            pending = {pool.submit(work): key for key, work in extra.items()}
            for future in as_completed(pending):
                cancelled()
                key = pending[future]
                try:
                    sources[key] = future.result()
                except Exception:
                    errors[key] = "行业来源暂不可用；未编造行业或同业数据。"
        cancelled()
        return {"schema_version": 1, "code": code, "as_of": as_of, "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "sources": sources, "errors": errors,
                "limitations": ["当前修订版仅供当前研究，不可用于声称无前视偏差的历史回测。",
                    "财报与公告按日期核对，不保证行情同日该时刻前已公开；不同来源不是同一时刻截面。",
                    "北向个股持仓仅按可得季报显示，不提供实时北向净买入。", "融资融券、完整新闻、基金全部持仓与历史行业成分尚未接入。"]}
