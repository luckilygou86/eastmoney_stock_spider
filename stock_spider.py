"""Stock data desktop collector. Standard library only."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from py_mini_racer import py_mini_racer
    from sina_decoder import SINA_KLC_DECODER
except ImportError:  # The collector keeps working with the existing fallbacks.
    py_mini_racer = None
    SINA_KLC_DECODER = ""


APP_TITLE = "\u80a1\u7968\u6570\u636e\u91c7\u96c6\u5668"
DEFAULT_QUERY = "301307"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


class SpiderError(RuntimeError):
    pass


@dataclass
class StockIdentity:
    code: str
    market: str
    secid: str
    name: str = ""
    source: str = ""


def parse_query(value: str) -> StockIdentity:
    value = (value or "").strip()
    if not value:
        raise SpiderError("\u8bf7\u8f93\u5165\u80a1\u7968\u540d\u79f0\u6216 6 \u4f4d\u4ee3\u7801\u3002")
    match = re.fullmatch(r"(sh|sz|bj)?(\d{6})", value.lower())
    if match:
        market, code = match.group(1), match.group(2)
        if not market:
            market = "sh" if code[:2] in {"60", "68", "51", "56"} else "sz"
        return StockIdentity(code, market, f"{1 if market == 'sh' else 0}.{code}", source=value)
    match = re.search(r"/(sh|sz|bj)(\d{6})(?:\.html)?", value.lower())
    if match:
        market, code = match.groups()
        return StockIdentity(code, market, f"{1 if market == 'sh' else 0}.{code}", source=value)
    # 东方财富分时成交页使用 ?newcode=0.301307 这样的 secid 参数。
    match = re.search(r"(?:[?&]newcode=)([01])\.(\d{6})", value.lower())
    if match:
        market_flag, code = match.groups()
        market = "sh" if market_flag == "1" else "sz"
        return StockIdentity(code, market, f"{market_flag}.{code}", source=value)
    if re.search(r"[/:?#]", value) or re.search(r"\.(com|cn|net)(/|$)", value, re.I):
        raise SpiderError("\u65e0\u6cd5\u89e3\u6790\u7f51\u5740\uff0c\u8bf7\u8f93\u5165\u80a1\u7968\u540d\u79f0\u6216 6 \u4f4d\u4ee3\u7801\u3002")
    return StockIdentity("", "", "", name=value, source=value)


def num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def amount(value: Any) -> str:
    n = num(value)
    if n is None:
        return "-"
    a = abs(n)
    sign = "-" if n < 0 else ""
    if a >= 100000000:
        return f"{sign}{a / 100000000:.2f} \u4ebf"
    if a >= 10000:
        return f"{sign}{a / 10000:.2f} \u4e07"
    return f"{n:,.2f}"


def fmt(value: Any, decimals: int = 2) -> str:
    n = num(value)
    return "-" if n is None else f"{n:,.{decimals}f}"


class Client:
    def __init__(self, timeout: int = 12, pause: float = 0.08):
        self.timeout = timeout
        self.pause = pause

    def json(self, url: str, referer: str = "https://quote.eastmoney.com/", powershell_fallback: bool = True) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer, "Connection": "close"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            raise SpiderError(f"HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            fallback = self._powershell_json(url, referer) if powershell_fallback else None
            if fallback is not None:
                result = fallback
            else:
                raise SpiderError(f"network: {getattr(exc, 'reason', None) or exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpiderError("invalid JSON response") from exc
        if not isinstance(result, dict):
            raise SpiderError("unexpected response")
        time.sleep(self.pause)
        return result

    @staticmethod
    def _powershell_json(url: str, referer: str) -> dict[str, Any] | None:
        """Use Windows' request stack when a local proxy closes urllib connections."""
        if os.name != "nt":
            return None
        escaped = url.replace("'", "''")
        command = (
            "$ProgressPreference='SilentlyContinue'; "
            "$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 18 "
            f"-Uri '{escaped}' -Headers @{{'User-Agent'='Mozilla/5.0';'Referer'='{referer}'}}; "
            "$r.Content"
        )
        try:
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=25, check=False)
            if completed.returncode != 0 or not completed.stdout:
                return None
            return json.loads(completed.stdout.decode("utf-8-sig", errors="replace"))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None

    def text(self, url: str, referer: str = "https://gu.qq.com/") -> str:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer, "Connection": "close"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
            try:
                result = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                result = raw.decode("gb18030", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpiderError(f"network: {getattr(exc, 'reason', None) or exc}") from exc
        time.sleep(self.pause)
        return result

    def resolve(self, identity: StockIdentity) -> StockIdentity:
        if identity.code:
            return identity
        q = urllib.parse.quote(identity.name, safe="")
        url = "https://searchapi.eastmoney.com/api/suggest/get?input=" + q + "&type=14&token=4dbc5e0fda3df2f7d9e7a9e7a9e7"
        rows = (self.json(url).get("QuotationCodeTable") or {}).get("Data") or []
        if not rows:
            raise SpiderError(f"\u6ca1\u6709\u627e\u5230\u80a1\u7968: {identity.name}")
        row = rows[0]
        code = str(row.get("Code") or "")
        if not re.fullmatch(r"\d{6}", code):
            raise SpiderError("\u80a1\u7968\u540d\u79f0\u89e3\u6790\u5931\u8d25")
        market = "sz" if str(row.get("JYS")) == "80" else ("sh" if str(row.get("MarketType")) in {"1", "3"} else "sz")
        return StockIdentity(code, market, f"{1 if market == 'sh' else 0}.{code}", str(row.get("Name") or identity.name), identity.source)

    def tencent_quote(self, identity: StockIdentity) -> dict[str, Any]:
        prefix = identity.market if identity.market in {"sh", "sz", "bj"} else "sz"
        raw = self.text(f"https://qt.gtimg.cn/q={prefix}{identity.code}")
        match = re.search(r'="(.*?)"', raw, re.S)
        if not match:
            raise SpiderError("Tencent quote empty")
        p = match.group(1).split("~")
        if len(p) < 40:
            raise SpiderError("Tencent quote fields incomplete")
        data = {
            "f57": p[2], "f58": p[1], "f43": p[3], "f60": p[4], "f46": p[5], "f47": p[6],
            "f48": (p[35].split("/")[-1] if len(p) > 35 and "/" in p[35] else (p[37] if len(p) > 37 else None)), "f44": p[33] if len(p) > 33 else None,
            "f45": p[34] if len(p) > 34 else None, "f168": p[38] if len(p) > 38 else None,
            # Tencent: 31=涨跌额, 32=涨跌幅(%), 44=流通市值(亿元),
            # 45=总市值(亿元), 46=市净率。统一换算市值为“元”。
            "f169": p[32] if len(p) > 32 else None,
            "f116": (num(p[45]) * 100000000 if len(p) > 45 and num(p[45]) is not None else None),
            "f117": (num(p[44]) * 100000000 if len(p) > 44 and num(p[44]) is not None else None),
            "f86": p[30] if len(p) > 30 else None,
        }
        return {"data": data}

    def eastmoney_quote_precision(self, identity: StockIdentity) -> dict[str, Any]:
        """Fetch exact-yuan market-cap fields to replace Tencent's 亿-level rounding."""
        secid = urllib.parse.quote(identity.secid, safe=".")
        query = (
            "/api/qt/stock/get?"
            f"secid={secid}&fields=f48,f57,f58,f116,f117,f168"
        )
        last_error: SpiderError | None = None
        for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
            try:
                payload = self.json("https://" + host + query)
                data = payload.get("data") or {}
                if data.get("f116") is not None or data.get("f117") is not None:
                    return payload
            except SpiderError as exc:
                last_error = exc
        raise last_error or SpiderError("Eastmoney precise quote empty")

    def tencent_intraday(self, identity: StockIdentity) -> dict[str, Any]:
        prefix = identity.market if identity.market in {"sh", "sz", "bj"} else "sz"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={prefix}{identity.code}"
        payload = self.json(url, "https://gu.qq.com/")
        box = ((payload.get("data") or {}).get(prefix + identity.code) or {}).get("data") or {}
        lines = []
        prev = None
        prev_volume = 0.0
        prev_turnover = 0.0
        for raw in box.get("data") or []:
            p = raw.split()
            if len(p) < 4:
                continue
            price = num(p[1])
            cumulative_volume = num(p[2], 0) or 0
            cumulative_turnover = num(p[3], 0) or 0
            # minute/query 返回累计成交量/额；转换成该分钟增量，避免把
            # 09:31 的累计值误当成 09:31 单分钟成交量。
            volume = max(0.0, cumulative_volume - prev_volume)
            turnover = round(max(0.0, cumulative_turnover - prev_turnover), 2)
            change = 0 if prev is None or price is None else round(price - prev, 4)
            lines.append(f"{p[0]},{price if price is not None else ''},{change},{volume if volume is not None else ''},{turnover if turnover is not None else ''}")
            prev = price
            prev_volume = cumulative_volume
            prev_turnover = cumulative_turnover
        return {"data": {"trends": lines}}

    def tencent_deals(self, identity: StockIdentity) -> dict[str, Any]:
        """Return all available pages from Tencent's three-second detail feed."""
        prefix = identity.market if identity.market in {"sh", "sz", "bj"} else "sz"
        # 腾讯接口每页约 70 条，p=0 从当日最早成交开始，随后按成交序号递增。
        def get_page(page: int) -> tuple[int, list[str] | None]:
            url = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={prefix}{identity.code}&p={page}"
            for _ in range(3):
                try:
                    raw = self.text(url)
                    match = re.search(r"=\[\d+,\"(.*?)\"\]", raw, re.S)
                    return page, (match.group(1).split("|") if match else [])
                except SpiderError:
                    continue
            return page, None

        page_rows: dict[int, list[str]] = {}
        failed_pages: set[int] = set()
        tail_page: int | None = None
        # 按批次继续请求，直到遇到不足 70 条的尾页。活跃股票可能超过
        # 64 页，所以不再使用固定 64 页上限。
        for start in range(0, 200, 8):
            batch_results: dict[int, list[str] | None] = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(get_page, page) for page in range(start, start + 8)]
                for future in as_completed(futures):
                    try:
                        page, items = future.result()
                        batch_results[page] = items
                    except Exception:
                        continue
            for page in range(start, start + 8):
                items = batch_results.get(page)
                if items is None:
                    failed_pages.add(page)
                    continue
                page_rows[page] = items
                if len(items) < 70:
                    tail_page = page if tail_page is None else min(tail_page, page)
            if tail_page is not None:
                break

        # 尾页之前的网络失败页不能被当作“没有更多数据”；单独重试，避免
        # 少一页仍被标记为完整。
        required_end = tail_page if tail_page is not None else max(page_rows, default=-1)
        for page in sorted(page for page in failed_pages if page <= required_end):
            _, items = get_page(page)
            if items is not None:
                page_rows[page] = items

        indexed: dict[int, str] = {}
        for page in sorted(page_rows):
            if tail_page is not None and page > tail_page:
                continue
            for item in page_rows[page]:
                parts = item.split("/")
                if len(parts) >= 7 and parts[0].isdigit():
                    indexed[int(parts[0])] = item
        lines = []
        for index in sorted(indexed):
            _, time_text, price, _change, volume, turnover, side = indexed[index].split("/")[:7]
            lines.append(f"{time_text},{price},{volume},{side},{turnover}")
        if not lines:
            raise SpiderError("Tencent deal detail empty")
        expected = max(indexed) + 1
        missing = sorted(set(range(expected)) - set(indexed))
        coverage_complete = tail_page is not None and not missing
        return {"data": {"details": lines}, "meta": {"complete": False, "coverage_complete": coverage_complete, "count": len(lines), "pages": len(page_rows), "format": "tencent", "source_note": "腾讯3秒汇总备用（不是原始逐笔）", "index_max": max(indexed), "missing_indices": missing[:20], "missing_count": len(missing)}}

    def deals(self, identity: StockIdentity) -> dict[str, Any]:
        """Page-equivalent逐笔优先，网络关闭时切换到可用备用流。"""
        try:
            return self.eastmoney_details(identity)
        except SpiderError:
            return self.tencent_deals(identity)

    def yahoo_daily(self, identity: StockIdentity) -> dict[str, Any]:
        suffix = "SS" if identity.market == "sh" else identity.market.upper()
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{identity.code}.{suffix}?range=2y&interval=1d"
        root = ((self.json(url).get("chart") or {}).get("result") or [None])[0] or {}
        timestamps = root.get("timestamp") or []
        quote = (((root.get("indicators") or {}).get("quote") or [None])[0] or {})
        adjusted = (((root.get("indicators") or {}).get("adjclose") or [None])[0] or {}).get("adjclose") or []
        lines = []
        for i, timestamp in enumerate(timestamps):
            try:
                date = datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                continue
            values = []
            for key in ("open", "close", "high", "low", "volume"):
                seq = quote.get(key) or []
                values.append(seq[i] if i < len(seq) else None)
            if values[1] is None:
                continue
            # Match Eastmoney fqt=1: scale historical OHLC by Yahoo's adjusted
            # close ratio. Today's row is replaced by the real-time quote later.
            adjusted_close = adjusted[i] if i < len(adjusted) else None
            factor = (float(adjusted_close) / float(values[1])) if adjusted_close is not None and values[1] else 1.0
            values[:4] = [round(float(value) * factor, 4) if value is not None else None for value in values[:4]]
            # Yahoo volume is shares while Eastmoney/Tencent use lots (手).
            volume_lots = float(values[4] or 0) / 100
            # Yahoo does not provide actual turnover. Close * volume is only an
            # approximation and must not be presented as exact transaction data.
            turnover = ""
            lines.append(",".join(str(x if x is not None else "") for x in [date, *values[:4], volume_lots, turnover]))
        return {"data": {"klines": lines}}

    def tencent_daily(self, identity: StockIdentity) -> dict[str, Any]:
        """Tencent qfq daily lines plus raw OHLC used to expose adjustment factors."""
        prefix = identity.market if identity.market in {"sh", "sz", "bj"} else "sz"
        symbol = prefix + identity.code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,640,qfq"
        payload = self.json(url, "https://gu.qq.com/")
        box = ((payload.get("data") or {}).get(symbol) or {})
        rows = box.get("qfqday") or box.get("day") or []
        lines: list[str] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            date, open_price, close, high, low, volume = row[:6]
            turnover = row[6] if len(row) > 6 else ""
            lines.append(",".join(str(value if value is not None else "") for value in (date, open_price, close, high, low, volume, turnover)))
        if not lines:
            raise SpiderError("Tencent daily kline empty")
        raw_by_date: dict[str, dict[str, Any]] = {}
        try:
            raw_url = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={symbol},day,,,640"
            raw_payload = self.json(raw_url, "https://gu.qq.com/")
            raw_box = ((raw_payload.get("data") or {}).get(symbol) or {})
            for row in raw_box.get("day") or []:
                if isinstance(row, list) and len(row) >= 6:
                    raw_by_date[str(row[0])] = {
                        "不复权开盘": num(row[1]), "不复权收盘": num(row[2]),
                        "不复权最高": num(row[3]), "不复权最低": num(row[4]),
                    }
        except SpiderError:
            # qfq series remains usable; the exported field status makes the
            # missing raw-price companion explicit instead of silently guessing.
            raw_by_date = {}
        return {"data": {"klines": lines}, "meta": {"raw_by_date": raw_by_date, "adjustment": "qfq"}}

    def sina_daily_exact(self, identity: StockIdentity) -> dict[str, dict[str, Any]]:
        """Return exact daily amount and historical float-share turnover from Sina."""
        if py_mini_racer is None or not SINA_KLC_DECODER:
            raise SpiderError("py-mini-racer 未安装，无法解码新浪精确历史成交额")
        symbol = (identity.market if identity.market in {"sh", "sz"} else "sz") + identity.code
        kline_url = f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js"
        payload = self.text(kline_url, "https://finance.sina.com.cn/")
        match = re.search(r'="([^"]+)"', payload)
        if not match:
            raise SpiderError("Sina exact daily kline empty")
        try:
            context = py_mini_racer.MiniRacer()
            context.eval(SINA_KLC_DECODER)
            decoded = context.call("d", match.group(1)) or []
        except Exception as exc:
            raise SpiderError(f"Sina daily decode failed: {exc}") from exc

        share_url = (
            "https://stock.finance.sina.com.cn/stock/api/jsonp.php/"
            f"var%20KKE_ShareAmount_{symbol}=/StockService.getAmountBySymbol?_=20&symbol={symbol}"
        )
        share_text = self.text(share_url, "https://finance.sina.com.cn/")
        share_match = re.search(r"\((\[.*?\])\)", share_text, re.S)
        try:
            share_changes = json.loads(share_match.group(1)) if share_match else []
        except json.JSONDecodeError:
            share_changes = []
        shares_by_date: list[tuple[str, float]] = []
        for row in share_changes:
            value = num(row.get("amount"))
            if row.get("date") and value is not None:
                shares_by_date.append((str(row["date"])[:10], value * 10000))
        shares_by_date.sort()

        exact: dict[str, dict[str, Any]] = {}
        current_shares: float | None = None
        share_index = 0
        for row in sorted(decoded, key=lambda item: str(item.get("date") or "")):
            trade_date = str(row.get("date") or "")[:10]
            if not trade_date:
                continue
            while share_index < len(shares_by_date) and shares_by_date[share_index][0] <= trade_date:
                current_shares = shares_by_date[share_index][1]
                share_index += 1
            volume_shares = num(row.get("volume"))
            exact[trade_date] = {
                "不复权开盘": num(row.get("open")),
                "不复权收盘": num(row.get("close")),
                "不复权最高": num(row.get("high")),
                "不复权最低": num(row.get("low")),
                "精确成交额": num(row.get("amount")),
                "历史流通股本(股)": current_shares,
                "精确历史换手率(%)": volume_shares / current_shares * 100 if volume_shares is not None and current_shares else None,
                "盘后成交量(股)": num(row.get("postVol")),
                "盘后成交额": num(row.get("postAmt")),
                "精确历史字段来源": "Sina KLC original daily + historical outstanding-share changes",
            }
        if not exact:
            raise SpiderError("Sina exact daily data empty")
        return exact

    def eastmoney_kline(self, identity: StockIdentity) -> dict[str, Any]:
        """日线页 fullScreenChart 使用的东方财富历史 K 线接口。"""
        secid = urllib.parse.quote(identity.secid, safe=".")
        # 两年数据足够覆盖界面月线分析和 120 日成本估算；请求全历史会让
        # 部分 CDN 节点直接断开连接，并显著拖慢查询。
        begin = (datetime.now() - timedelta(days=900)).strftime("%Y%m%d")
        urls = [
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid
            + f"&klt=101&fqt=1&beg={begin}&end=20991231"
            + "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57",
        ]
        klines: list[str] = []
        last_error: SpiderError | None = None
        for url in urls:
            try:
                payload = self.json(url, "https://quote.eastmoney.com/", powershell_fallback=False)
                rows = (payload.get("data") or {}).get("klines") or []
                if isinstance(rows, list):
                    klines.extend(str(row) for row in rows)
                if klines:
                    break
            except SpiderError as exc:
                last_error = exc
        if not klines:
            raise last_error or SpiderError("Eastmoney kline empty")
        return {"data": {"klines": sorted(set(klines))}, "meta": {"adjustment": "qfq", "turnover": "exact"}}

    def eastmoney(self, identity: StockIdentity) -> dict[str, Any]:
        secid = urllib.parse.quote(identity.secid, safe=".")
        code = identity.code
        result: dict[str, Any] = {"details": {}, "billboard": {}}
        errors: dict[str, str] = {}
        endpoints = {
            "details": "https://push2.eastmoney.com/api/qt/stock/details/get?secid=" + secid + "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65&ndays=1&pos=-1",
            "billboard": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILS&columns=ALL&filter=(SECURITY_CODE=%22" + code + "%22)&pageNumber=1&pageSize=50&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB",
        }
        for key, url in endpoints.items():
            try:
                result[key] = self.json(url)
            except SpiderError as exc:
                errors[key] = str(exc)
        result["errors"] = errors
        return result

    def eastmoney_details(self, identity: StockIdentity) -> dict[str, Any]:
        """逐笔页 f1.html 使用的东方财富明细接口。"""
        secid = urllib.parse.quote(identity.secid, safe=".")
        # pos=-1 只返回最新一条，pos=-100 只返回最新 100 条；pos=0 才是
        # f1 页面当天的完整明细。此前使用分页或负数窗口都会少抓。
        query = (
            "/api/qt/stock/details/get?"
            f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
            "&ndays=1&pos=0"
        )
        payload: dict[str, Any] | None = None
        last_error: SpiderError | None = None
        # push2 is the live host; push2delay carries the same full-day list and is
        # much more stable on networks where the live CDN occasionally resets.
        for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
            try:
                payload = self.json("https://" + host + query, "https://quote.eastmoney.com/f1.html?newcode=" + identity.secid)
                if (payload.get("data") or {}).get("details"):
                    break
            except SpiderError as exc:
                last_error = exc
        rows = ((payload or {}).get("data") or {}).get("details") or []
        lines = [str(row) for row in rows if isinstance(row, str) and row]
        if not lines:
            raise last_error or SpiderError("Eastmoney detail empty")
        return {"data": {"details": lines}, "meta": {"complete": True, "count": len(lines), "format": "eastmoney", "source_note": "东方财富 f1 页面当日全量"}}

    def eastmoney_billboard(self, identity: StockIdentity) -> dict[str, Any]:
        code = identity.code
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILS"
            "&columns=ALL&filter=(SECURITY_CODE=%22" + code
            + "%22)&pageNumber=1&pageSize=50&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB"
        )
        return self.json(url, "https://quote.eastmoney.com/", powershell_fallback=False)

    def eastmoney_endpoint(self, identity: StockIdentity, key: str) -> dict[str, Any]:
        payload = self.eastmoney(identity)
        if payload.get("errors", {}).get(key):
            raise SpiderError(payload["errors"][key])
        return payload.get(key, {})

    def chip_distribution(self, identity: StockIdentity, daily: list[dict[str, Any]]) -> dict[str, Any]:
        """Estimate cost distribution from recent OHLCV."""
        buckets: dict[float, float] = {}
        for row in daily[-120:]:
            close = num(row.get("\u6536\u76d8")); volume = num(row.get("\u6210\u4ea4\u91cf"), 0) or 0
            if close is None or volume <= 0:
                continue
            price = round(close * 2) / 2
            buckets[price] = buckets.get(price, 0) + volume
        total = sum(buckets.values())
        distribution = [{"\u6210\u672c\u4ef7": price, "\u4f30\u7b97\u7b79\u7801\u91cf": volume, "\u5360\u6bd4(%)": volume / total * 100 if total else 0} for price, volume in sorted(buckets.items())]
        weighted = sum(row["\u6210\u672c\u4ef7"] * row["\u4f30\u7b97\u7b79\u7801\u91cf"] for row in distribution) / total if total else None
        return {"\u53e3\u5f84": "\u57fa\u4e8e\u6700\u8fd1OHLCV\u7684\u4f30\u7b97\uff0c\u4e0d\u662f\u4ea4\u6613\u6240\u539f\u59cb\u7b79\u7801\u6570\u636e", "\u5e73\u5747\u6210\u672c": weighted, "\u7b79\u7801\u5cf0": distribution}
    def fetch(self, identity: StockIdentity) -> dict[str, Any]:
        identity = self.resolve(identity)
        result: dict[str, Any] = {"identity": asdict(identity), "fetched_at": datetime.now().isoformat(timespec="seconds"), "errors": {}, "sources": {}}
        for key, action in (("quote", lambda: self.tencent_quote(identity)), ("intraday", lambda: self.tencent_intraday(identity))):
            try:
                result[key] = action()
                result["sources"][key] = "Tencent"
            except SpiderError as exc:
                result[key] = {}
                result["errors"][key] = str(exc)
        if (result.get("quote") or {}).get("data"):
            try:
                precise = (self.eastmoney_quote_precision(identity).get("data") or {})
                for field in ("f48", "f116", "f117"):
                    if precise.get(field) is not None:
                        result["quote"]["data"][field] = precise[field]
                result["sources"]["quote"] = "Tencent realtime + Eastmoney exact-yuan market cap"
            except SpiderError as exc:
                result["errors"]["eastmoney_market_cap"] = str(exc)
        try:
            result["details"] = self.eastmoney_details(identity)
            result["sources"]["details"] = "Eastmoney f1 full-day detail"
        except SpiderError as exc:
            result["errors"]["eastmoney_detail"] = str(exc)
            try:
                result["details"] = self.tencent_deals(identity)
                result["sources"]["details"] = "Tencent fallback"
            except SpiderError as fallback_exc:
                result["details"] = {}
                result["errors"]["details"] = str(fallback_exc)
        try:
            result["daily"] = self.eastmoney_kline(identity)
            result["sources"]["daily"] = "Eastmoney forward-adjusted daily"
        except SpiderError as exc:
            result["errors"]["eastmoney_daily"] = str(exc)
            try:
                result["daily"] = self.tencent_daily(identity)
                result["sources"]["daily"] = "Tencent forward-adjusted daily fallback"
                if (result["daily"].get("meta") or {}).get("raw_by_date"):
                    result["sources"]["raw_daily"] = "Tencent unadjusted daily companion"
            except SpiderError as tencent_exc:
                result["errors"]["tencent_daily"] = str(tencent_exc)
                try:
                    result["daily"] = self.yahoo_daily(identity)
                    result["sources"]["daily"] = "Yahoo adjusted daily fallback"
                except SpiderError as yahoo_exc:
                    result["daily"] = {}
                    result["errors"]["daily"] = str(yahoo_exc)
        # Eastmoney supplies exact amount but not a raw-price companion. Tencent's
        # unadjusted series is joined by date only, so the primary OHLCV source and
        # exact amount are never overwritten.
        if (result.get("daily") or {}).get("data") and not (result.get("daily") or {}).get("meta", {}).get("raw_by_date"):
            try:
                companion = self.tencent_daily(identity)
                raw_map = (companion.get("meta") or {}).get("raw_by_date") or {}
                result["daily"].setdefault("meta", {})["raw_by_date"] = raw_map
                if raw_map:
                    result["sources"]["raw_daily"] = "Tencent unadjusted daily companion"
            except SpiderError as exc:
                result["errors"]["raw_daily"] = str(exc)
        if (result.get("daily") or {}).get("data"):
            try:
                exact_daily = self.sina_daily_exact(identity)
                result["daily"].setdefault("meta", {})["exact_by_date"] = exact_daily
                result["sources"]["daily_amount_turnover"] = "Sina exact daily amount + historical outstanding shares"
            except SpiderError as exc:
                result["errors"]["exact_daily_amount_turnover"] = str(exc)
        try:
            benchmark_name, benchmark_payload = self.benchmark_daily(identity)
            result["benchmark_name"] = benchmark_name
            result["benchmark_daily"] = benchmark_payload
            result["sources"]["benchmark"] = "Tencent forward-adjusted index daily"
        except SpiderError as exc:
            result["benchmark_daily"] = {}
            result["errors"]["benchmark"] = str(exc)
        try:
            result["billboard"] = self.eastmoney_billboard(identity)
            result["sources"]["billboard"] = "Eastmoney"
        except SpiderError as exc:
            result["billboard"] = {}
            result["errors"]["eastmoney_billboard"] = str(exc)
        result.setdefault("details", {})
        result.setdefault("billboard", {})
        if not any(result.get(k, {}).get("data") or result.get(k, {}).get("result") for k in ("quote", "intraday", "daily", "details", "billboard")):
            raise SpiderError("\u6240\u6709\u6570\u636e\u63a5\u53e3\u5747\u5931\u8d25")
        normalized = normalize(result)
        sync_current_daily(normalized)
        all_daily = normalized["daily"]
        normalized["daily_history"] = all_daily
        normalized["daily_indicators"] = enrich_daily_indicators(all_daily, normalized.get("identity"), normalized.get("quote"))
        normalized["benchmark_indicators"] = enrich_daily_indicators(normalized.get("benchmark_history") or [])
        normalized["daily"] = all_daily[-60:]
        cutoff = (datetime.now() - timedelta(days=31)).date().isoformat()
        normalized["billboard"] = [row for row in normalized["billboard"] if row.get("交易日期", "") >= cutoff]
        normalized["chips"] = self.chip_distribution(identity, all_daily)
        normalized["analysis"] = build_analysis(normalized)
        return normalized

    def health(self, identity: StockIdentity) -> list[dict[str, Any]]:
        identity = self.resolve(identity)
        checks: list[tuple[str, Callable[[], Any]]] = [
            ("\u80a1\u7968\u540d\u79f0\u89e3\u6790", lambda: identity),
            ("\u817e\u8baf\u5b9e\u65f6\u884c\u60c5", lambda: self.tencent_quote(identity)),
            ("\u817e\u8baf\u5206\u65f6\u6570\u636e", lambda: self.tencent_intraday(identity)),
            ("Yahoo \u65e5\u7ebf\u6570\u636e", lambda: self.yahoo_daily(identity)),
            ("\u4e1c\u8d22\u9010\u7b14\u660e\u7ec6", lambda: self.eastmoney_endpoint(identity, "details")),
            ("\u4e1c\u8d22\u9f99\u864e\u699c", lambda: self.eastmoney_endpoint(identity, "billboard")),
            ("\u5de8\u6f6e\u80a1\u7968\u4ee3\u7801\u8868", lambda: self.json("https://www.cninfo.com.cn/new/data/szse_stock.json")),
        ]
        report = []
        for name, action in checks:
            started = time.perf_counter()
            try:
                action()
                report.append({"\u63a5\u53e3": name, "\u72b6\u6001": "\u6b63\u5e38", "\u8017\u65f6(ms)": round((time.perf_counter() - started) * 1000), "\u8bf4\u660e": "\u5df2\u6536\u5230\u53ef\u89e3\u6790\u54cd\u5e94"})
            except Exception as exc:
                report.append({"\u63a5\u53e3": name, "\u72b6\u6001": "\u5931\u8d25", "\u8017\u65f6(ms)": round((time.perf_counter() - started) * 1000), "\u8bf4\u660e": str(exc)})
        return report

    def benchmark_daily(self, identity: StockIdentity) -> tuple[str, dict[str, Any]]:
        if identity.code.startswith(("688", "689")):
            benchmark = StockIdentity("000688", "sh", "1.000688", "科创50")
        elif identity.code.startswith(("300", "301")):
            benchmark = StockIdentity("399006", "sz", "0.399006", "创业板指")
        elif identity.market == "sh":
            benchmark = StockIdentity("000001", "sh", "1.000001", "上证指数")
        else:
            benchmark = StockIdentity("399001", "sz", "0.399001", "深证成指")
        return benchmark.name, self.tencent_daily(benchmark)


def split_rows(raw: list[str], columns: list[str]) -> list[dict[str, Any]]:
    rows = []
    for line in raw:
        parts = line.split(",")
        if len(parts) >= len(columns):
            rows.append(dict(zip(columns, parts)))
    return rows


def direction_name(value: Any) -> str:
    """Normalize direction codes used by Eastmoney and Tencent."""
    code = str(value or "").strip().upper()
    return {"1": "卖盘", "2": "买盘", "4": "中性", "S": "卖盘", "B": "买盘", "M": "中性"}.get(code, code or "-")


def clock_value(value: Any) -> int | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 4:
        return None
    if len(digits) == 4:
        digits += "00"
    try:
        return int(digits[:6])
    except ValueError:
        return None


def detail_trading_phase(value: Any) -> tuple[str, str, int]:
    """Classify f1 records so indicative auction snapshots are not summed as trades."""
    tick = clock_value(value)
    if tick is None:
        return "未知", "未知", 0
    if tick < 92500:
        return "开盘集合竞价", "行情快照", 0
    if 92500 <= tick < 93000:
        return "开盘集合竞价", "成交记录", 1
    if 93000 <= tick <= 113000 or 130000 <= tick < 145700:
        return "连续竞价", "成交记录", 1
    if 145700 <= tick <= 150000:
        return "收盘集合竞价", "成交记录", 1
    if 150000 < tick <= 153000:
        return "盘后定价交易", "成交记录", 0
    return "非交易时段", "未知", 0


def intraday_trading_phase(value: Any) -> tuple[str, int]:
    tick = clock_value(value)
    if tick is None:
        return "未知", 0
    if tick == 93000:
        return "开盘集合竞价/首分钟", 1
    if 93000 < tick <= 113000 or 130000 <= tick < 145700:
        return "连续竞价", 1
    if 145700 <= tick <= 150000:
        return "收盘集合竞价", 1
    if 150000 < tick <= 153000:
        return "盘后定价交易", 0
    return "非交易时段", 0


def sync_current_daily(result: dict[str, Any]) -> None:
    """Make today's daily row agree with the real-time quote snapshot."""
    quote = result.get("quote") or {}
    stamp = re.sub(r"\D", "", str(quote.get("行情时间") or ""))
    if len(stamp) < 8 or num(quote.get("最新价")) is None:
        return
    trade_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    row = {
        "日期": trade_date,
        "开盘": num(quote.get("今开")),
        "收盘": num(quote.get("最新价")),
        "最高": num(quote.get("最高")),
        "最低": num(quote.get("最低")),
        "成交量": num(quote.get("成交量(手)")),
        "成交额": num(quote.get("成交额")),
        "不复权开盘": num(quote.get("今开")),
        "不复权收盘": num(quote.get("最新价")),
        "不复权最高": num(quote.get("最高")),
        "不复权最低": num(quote.get("最低")),
        "前复权因子": 1.0,
        "复权口径": "前复权；当日因子=1",
        "成交额口径": "实时行情精确值",
    }
    stamp_time = int(stamp[8:14] or 0) if len(stamp) >= 14 else 0
    row["数据状态"] = "盘中" if stamp_time < 150000 else "收盘"
    targets = [result.setdefault("daily", [])]
    history = result.get("daily_history")
    if isinstance(history, list) and history is not targets[0]:
        targets.append(history)
    for daily in targets:
        for current in daily:
            if current.get("日期") == trade_date:
                current.update(row)
                break
        else:
            daily.append(dict(row))
            daily.sort(key=lambda item: str(item.get("日期") or ""))


def rolling_mean(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    window: list[float] = []
    for value in values:
        if value is None:
            result.append(None)
            window.clear()
            continue
        window.append(value)
        if len(window) > period:
            window.pop(0)
        result.append(sum(window) / period if len(window) == period else None)
    return result


def rolling_std(values: list[float | None], period: int) -> list[float | None]:
    means = rolling_mean(values, period)
    result: list[float | None] = []
    for i, mean in enumerate(means):
        window = values[max(0, i - period + 1):i + 1]
        if mean is None or len(window) < period or any(value is None for value in window):
            result.append(None)
        else:
            result.append(math.sqrt(sum((float(value) - mean) ** 2 for value in window) / period))
    return result


def ema(values: list[float | None], period: int) -> list[float | None]:
    alpha = 2 / (period + 1)
    result: list[float | None] = []
    current: float | None = None
    for value in values:
        if value is None:
            result.append(None)
            continue
        current = value if current is None else alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def wilder_average(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    current: float | None = None
    seed: list[float] = []
    for value in values:
        if value is None:
            result.append(None)
            continue
        if current is None:
            seed.append(value)
            if len(seed) < period:
                result.append(None)
                continue
            current = sum(seed[-period:]) / period
        else:
            current = (current * (period - 1) + value) / period
        result.append(current)
    return result


def price_limit_rate(identity: dict[str, Any] | None) -> float | None:
    if not identity:
        return None
    code = str(identity.get("code") or "")
    market = str(identity.get("market") or "")
    name = str(identity.get("name") or "").upper()
    if market == "bj":
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if "ST" in name:
        return 0.05
    return 0.10 if re.fullmatch(r"\d{6}", code) else None


def limit_price(previous_close: float, rate: float, upward: bool) -> float:
    multiplier = Decimal("1") + Decimal(str(rate)) if upward else Decimal("1") - Decimal(str(rate))
    return float((Decimal(str(previous_close)) * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def enrich_daily_indicators(rows: list[dict[str, Any]], identity: dict[str, Any] | None = None, quote: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Add common trend, momentum, volatility and volume indicators."""
    enriched = [dict(row) for row in rows]
    closes = [num(row.get("收盘")) for row in rows]
    highs = [num(row.get("最高")) for row in rows]
    lows = [num(row.get("最低")) for row in rows]
    volumes = [num(row.get("成交量")) for row in rows]
    float_market_cap = num((quote or {}).get("流通市值"))
    latest_raw_price = num((quote or {}).get("最新价"))
    current_float_shares = float_market_cap / latest_raw_price if float_market_cap and latest_raw_price else None
    board_limit_rate = price_limit_rate(identity)
    for period in (5, 10, 20, 30, 60, 120, 250):
        values = rolling_mean(closes, period)
        for row, value in zip(enriched, values):
            row[f"MA{period}"] = value
    volume_ma5, volume_ma10 = rolling_mean(volumes, 5), rolling_mean(volumes, 10)
    ema12, ema26 = ema(closes, 12), ema(closes, 26)
    dif = [(a - b) if a is not None and b is not None else None for a, b in zip(ema12, ema26)]
    dea = ema(dif, 9)
    macd = [(2 * (a - b)) if a is not None and b is not None else None for a, b in zip(dif, dea)]
    ma20, std20 = rolling_mean(closes, 20), rolling_std(closes, 20)

    rsi6: list[float | None] = []
    rsi12: list[float | None] = []
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, len(closes)):
        change = (closes[i] - closes[i - 1]) if closes[i] is not None and closes[i - 1] is not None else 0
        gains.append(max(change, 0)); losses.append(max(-change, 0))
    for period, target in ((6, rsi6), (12, rsi12)):
        avg_gain, avg_loss = wilder_average(gains, period), wilder_average(losses, period)
        for gain, loss in zip(avg_gain, avg_loss):
            target.append(100 if loss == 0 and gain not in (None, 0) else (50 if gain == 0 and loss == 0 else (100 - 100 / (1 + gain / loss)) if gain is not None and loss not in (None, 0) else None))

    k_values: list[float | None] = []
    d_values: list[float | None] = []
    j_values: list[float | None] = []
    k = d = 50.0
    tr_values: list[float | None] = []
    obv_values: list[float | None] = []
    obv = 0.0
    for i in range(len(rows)):
        if i == 0 or closes[i] is None or closes[i - 1] is None or highs[i] is None or lows[i] is None:
            tr = (highs[i] - lows[i]) if highs[i] is not None and lows[i] is not None else None
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_values.append(tr)
        if i > 0 and closes[i] is not None and closes[i - 1] is not None and volumes[i] is not None:
            obv += volumes[i] if closes[i] > closes[i - 1] else -volumes[i] if closes[i] < closes[i - 1] else 0
        obv_values.append(obv)
        start = max(0, i - 8)
        high_window = [value for value in highs[start:i + 1] if value is not None]
        low_window = [value for value in lows[start:i + 1] if value is not None]
        if closes[i] is None or not high_window or not low_window or max(high_window) == min(low_window):
            k_values.append(None); d_values.append(None); j_values.append(None)
        else:
            rsv = (closes[i] - min(low_window)) / (max(high_window) - min(low_window)) * 100
            k = 2 / 3 * k + 1 / 3 * rsv; d = 2 / 3 * d + 1 / 3 * k
            k_values.append(k); d_values.append(d); j_values.append(3 * k - 2 * d)
    atr14 = wilder_average(tr_values, 14)

    for i, row in enumerate(enriched):
        previous = closes[i - 1] if i else None
        row["涨跌幅(%)"] = ((closes[i] / previous) - 1) * 100 if closes[i] is not None and previous else None
        row["振幅(%)"] = ((highs[i] - lows[i]) / previous) * 100 if highs[i] is not None and lows[i] is not None and previous else None
        row["VOL_MA5"] = volume_ma5[i]; row["VOL_MA10"] = volume_ma10[i]
        row["量比_MA5"] = (volumes[i] / volume_ma5[i]) if volumes[i] is not None and volume_ma5[i] else None
        row["DIF"] = dif[i]; row["DEA"] = dea[i]; row["MACD"] = macd[i]
        row["RSI6"] = rsi6[i]; row["RSI12"] = rsi12[i]
        row["K"] = k_values[i]; row["D"] = d_values[i]; row["J"] = j_values[i]
        row["BOLL中轨"] = ma20[i]
        row["BOLL上轨"] = ma20[i] + 2 * std20[i] if ma20[i] is not None and std20[i] is not None else None
        row["BOLL下轨"] = ma20[i] - 2 * std20[i] if ma20[i] is not None and std20[i] is not None else None
        row["ATR14"] = atr14[i]; row["OBV"] = obv_values[i]
        exact_turnover = num(row.get("成交额"))
        # Monetary turnover must use the contemporaneous unadjusted price. Qfq
        # prices are suitable for returns/indicators but would distort cash value.
        turnover_high = num(rows[i].get("不复权最高"), highs[i])
        turnover_low = num(rows[i].get("不复权最低"), lows[i])
        turnover_close = num(rows[i].get("不复权收盘"), closes[i])
        typical_price = (turnover_high + turnover_low + turnover_close) / 3 if turnover_high is not None and turnover_low is not None and turnover_close is not None else None
        estimated_turnover = typical_price * volumes[i] * 100 if identity and typical_price is not None and volumes[i] is not None else None
        row["估算成交额"] = estimated_turnover
        row["可用成交额"] = exact_turnover if exact_turnover is not None else estimated_turnover
        row["可用成交额口径"] = "接口精确值" if exact_turnover is not None else "典型价×成交量×100估算"
        row["估算换手率_按当前流通股本(%)"] = volumes[i] * 100 / current_float_shares * 100 if volumes[i] is not None and current_float_shares else None
        row["流通股本估算(股)"] = current_float_shares
        row["行情显示换手率(%)"] = num((quote or {}).get("换手率(%)")) if i == len(enriched) - 1 else None
        raw_previous = num(rows[i - 1].get("不复权收盘")) if i else None
        raw_high = num(rows[i].get("不复权最高"))
        raw_low = num(rows[i].get("不复权最低"))
        if raw_previous and board_limit_rate:
            up_limit = limit_price(raw_previous, board_limit_rate, True)
            down_limit = limit_price(raw_previous, board_limit_rate, False)
            row["理论涨停价"] = up_limit
            row["理论跌停价"] = down_limit
            row["触及理论涨停"] = 1 if raw_high is not None and raw_high >= up_limit - 0.001 else 0
            row["触及理论跌停"] = 1 if raw_low is not None and raw_low <= down_limit + 0.001 else 0
            row["涨跌停口径"] = "按当前板块/证券名称规则推算；IPO无涨跌幅期及历史ST变更需另核验"
        else:
            row["理论涨停价"] = None; row["理论跌停价"] = None
            row["触及理论涨停"] = None; row["触及理论跌停"] = None
            row["涨跌停口径"] = "缺少不复权昨收或证券规则，未推算"
    return enriched


def build_analysis(result: dict[str, Any]) -> dict[str, Any]:
    daily = result.get("daily_indicators") or enrich_daily_indicators(result.get("daily_history") or result.get("daily") or [], result.get("identity"), result.get("quote"))
    latest = daily[-1] if daily else {}
    closes = [num(row.get("收盘")) for row in daily]
    volumes = [num(row.get("成交量")) for row in daily]

    def period_return(days: int) -> float | None:
        if len(closes) <= days or closes[-1] is None or not closes[-days - 1]:
            return None
        return (closes[-1] / closes[-days - 1] - 1) * 100

    benchmark_rows = result.get("benchmark_indicators") or enrich_daily_indicators(result.get("benchmark_history") or [])
    benchmark_by_date = {row.get("日期"): num(row.get("收盘")) for row in benchmark_rows}
    aligned = [(num(row.get("收盘")), benchmark_by_date.get(row.get("日期"))) for row in daily]
    relative: dict[str, float | None] = {}
    for days in (20, 60, 120, 250):
        usable = [(stock, index) for stock, index in aligned if stock is not None and index is not None]
        if len(usable) > days and usable[-days - 1][0] and usable[-days - 1][1]:
            stock_return = (usable[-1][0] / usable[-days - 1][0] - 1) * 100
            benchmark_return = (usable[-1][1] / usable[-days - 1][1] - 1) * 100
            relative[f"{days}日"] = stock_return - benchmark_return
        else:
            relative[f"{days}日"] = None

    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i] and closes[i - 1]]
    recent_returns = returns[-20:]
    volatility = math.sqrt(sum((value - sum(recent_returns) / len(recent_returns)) ** 2 for value in recent_returns) / len(recent_returns)) * math.sqrt(250) * 100 if recent_returns else None
    peak = None; max_drawdown = 0.0
    for close in [value for value in closes[-120:] if value is not None]:
        peak = close if peak is None else max(peak, close)
        if peak:
            max_drawdown = min(max_drawdown, close / peak - 1)

    ma5, ma10, ma20, ma60 = (num(latest.get(key)) for key in ("MA5", "MA10", "MA20", "MA60"))
    close = num(latest.get("收盘"))
    if close is not None and ma5 and ma10 and ma20 and close > ma5 > ma10 > ma20:
        trend = "短期多头排列"
    elif close is not None and ma5 and ma10 and ma20 and close < ma5 < ma10 < ma20:
        trend = "短期空头排列"
    elif close is not None and ma20:
        trend = "价格位于MA20上方" if close > ma20 else "价格位于MA20下方"
    else:
        trend = "历史数据不足"
    high20 = max((num(row.get("最高")) for row in daily[-20:] if num(row.get("最高")) is not None), default=None)
    low20 = min((num(row.get("最低")) for row in daily[-20:] if num(row.get("最低")) is not None), default=None)

    intraday = result.get("intraday") or []
    main_intraday = [row for row in intraday if row.get("计入主交易统计") == 1]
    after_intraday = [row for row in intraday if row.get("交易阶段") == "盘后定价交易"]

    def minute_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        volume = sum(num(row.get("成交量"), 0) or 0 for row in rows)
        turnover = sum(num(row.get("成交额"), 0) or 0 for row in rows)
        return {"记录数": len(rows), "成交量(手)": volume, "成交额": turnover, "VWAP": turnover / (volume * 100) if volume else None}

    main_minute = minute_summary(main_intraday)
    after_minute = minute_summary(after_intraday)
    full_minute = minute_summary(intraday)
    intraday_vwap = num(main_minute.get("VWAP"))
    current_price = num((result.get("quote") or {}).get("最新价"))

    details = result.get("details") or []
    side_volume = {"买盘": 0.0, "卖盘": 0.0, "中性": 0.0}
    main_details = [row for row in details if row.get("计入主交易统计") == 1 and row.get("记录性质") == "成交记录"]
    after_details = [row for row in details if row.get("交易阶段") == "盘后定价交易" and row.get("记录性质") == "成交记录"]
    auction_snapshots = [row for row in details if row.get("记录性质") == "行情快照"]
    for row in main_details:
        side = row.get("方向")
        if side in side_volume:
            side_volume[side] += num(row.get("成交量"), 0) or 0
    active_total = side_volume["买盘"] + side_volume["卖盘"]
    main_detail_volume = sum(num(row.get("成交量"), 0) or 0 for row in main_details)
    after_detail_volume = sum(num(row.get("成交量"), 0) or 0 for row in after_details)
    return {
        "趋势判断": trend,
        "阶段收益(%)": {"5日": period_return(5), "10日": period_return(10), "20日": period_return(20), "60日": period_return(60), "120日": period_return(120), "250日": period_return(250)},
        "相对强弱": {"基准": result.get("benchmark_name") or "未获取", "超额收益(百分点)": relative},
        "均线": {"MA5": ma5, "MA10": ma10, "MA20": ma20, "MA60": ma60, "MA120": num(latest.get("MA120")), "MA250": num(latest.get("MA250"))},
        "动量指标": {"DIF": num(latest.get("DIF")), "DEA": num(latest.get("DEA")), "MACD": num(latest.get("MACD")), "RSI6": num(latest.get("RSI6")), "RSI12": num(latest.get("RSI12")), "K": num(latest.get("K")), "D": num(latest.get("D")), "J": num(latest.get("J"))},
        "波动风险": {"ATR14": num(latest.get("ATR14")), "20日年化波动率(%)": volatility, "近120日最大回撤(%)": max_drawdown * 100, "20日最高": high20, "20日最低": low20},
        "量价": {"当日量比_MA5": num(latest.get("量比_MA5")), "5日均量(手)": num(latest.get("VOL_MA5")), "10日均量(手)": num(latest.get("VOL_MA10")), "OBV": num(latest.get("OBV")), "当日成交量(手)": volumes[-1] if volumes else None},
        "分时": {"主交易VWAP": intraday_vwap, "最新价相对主交易VWAP(%)": ((current_price / intraday_vwap) - 1) * 100 if current_price and intraday_vwap else None, "主交易": main_minute, "盘后定价": after_minute, "接口全时段": full_minute},
        "主动成交": {"统计口径": "仅开盘集合竞价成交、连续竞价及收盘集合竞价；排除竞价快照和盘后定价", "主交易记录数": len(main_details), "主交易成交量(手)": main_detail_volume, "买盘量(手)": side_volume["买盘"], "卖盘量(手)": side_volume["卖盘"], "中性量(手)": side_volume["中性"], "买盘占主动成交(%)": side_volume["买盘"] / active_total * 100 if active_total else None, "净主动量(手)": side_volume["买盘"] - side_volume["卖盘"], "竞价快照记录数": len(auction_snapshots), "盘后定价记录数": len(after_details), "盘后定价成交量(手)": after_detail_volume},
        "数据限制": ["历史精确成交额、历史流通股本和据此计算的历史换手率已接入；估算列仅作为精确源失败时的备用", "不同行情商可能因无限售流通股本生效日、盘后交易计入口径和四舍五入而出现细微换手率差异", "理论涨跌停价按当前板块/名称规则推算，IPO无涨跌幅期和历史ST状态需要交易所日历或证券状态库复核", "仅有宽基指数比较，所属行业指数和同业横向排名仍未接入", "逐笔买卖方向按数据源规则分类，不等同于交易所认定的真实主动买卖", "财务、估值、公告、新闻、资金流和融资融券属于另一类低频/事件数据，当前仍未接入", "盘中日K和技术指标会随实时行情变化，不是最终收盘值"],
    }


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    identity = raw["identity"]
    q = (raw.get("quote", {}).get("data") or {})
    if q.get("f58"):
        identity["name"] = q["f58"]
    quote = {"\u4ee3\u7801": q.get("f57", identity["code"]), "\u540d\u79f0": q.get("f58", identity.get("name", "")), "\u6700\u65b0\u4ef7": q.get("f43"), "昨收": q.get("f60"), "\u6da8\u8dcc\u5e45(%)": q.get("f169"), "\u6210\u4ea4\u989d": q.get("f48"), "\u6210\u4ea4\u91cf(\u624b)": q.get("f47"), "\u6362\u624b\u7387(%)": q.get("f168"), "\u603b\u5e02\u503c": q.get("f116"), "\u6d41\u901a\u5e02\u503c": q.get("f117"), "\u4eca\u5f00": q.get("f46"), "\u6700\u9ad8": q.get("f44"), "\u6700\u4f4e": q.get("f45"), "行情时间": q.get("f86")}
    quote["涨跌额"] = (num(quote.get("最新价")) - num(quote.get("昨收"))) if num(quote.get("最新价")) is not None and num(quote.get("昨收")) is not None else None
    intraday = []
    for row in split_rows((raw.get("intraday", {}).get("data") or {}).get("trends", []), ["\u65f6\u95f4", "\u4ef7\u683c", "\u6da8\u8dcc", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u989d"]):
        for key in list(row)[1:]:
            row[key] = num(row[key])
        phase, main_session = intraday_trading_phase(row.get("时间"))
        row["交易阶段"] = phase
        row["计入主交易统计"] = main_session
        intraday.append(row)
    daily = []
    raw_by_date = (raw.get("daily", {}).get("meta") or {}).get("raw_by_date") or {}
    exact_by_date = (raw.get("daily", {}).get("meta") or {}).get("exact_by_date") or {}
    daily_adjustment = (raw.get("daily", {}).get("meta") or {}).get("adjustment") or "qfq"
    for row in split_rows((raw.get("daily", {}).get("data") or {}).get("klines", []), ["\u65e5\u671f", "\u5f00\u76d8", "\u6536\u76d8", "\u6700\u9ad8", "\u6700\u4f4e", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u989d"]):
        for key in list(row)[1:]:
            row[key] = num(row[key])
        companion = raw_by_date.get(str(row.get("日期") or "")) or {}
        for key in ("不复权开盘", "不复权收盘", "不复权最高", "不复权最低"):
            row[key] = num(companion.get(key))
        raw_close = num(row.get("不复权收盘"))
        qfq_close = num(row.get("收盘"))
        row["前复权因子"] = qfq_close / raw_close if qfq_close is not None and raw_close else None
        row["复权口径"] = "前复权" if daily_adjustment == "qfq" else str(daily_adjustment)
        row["成交额口径"] = "接口精确值" if num(row.get("成交额")) is not None else "数据源未提供"
        exact = exact_by_date.get(str(row.get("日期") or "")) or {}
        for key in ("不复权开盘", "不复权收盘", "不复权最高", "不复权最低"):
            if num(row.get(key)) is None and num(exact.get(key)) is not None:
                row[key] = num(exact.get(key))
        raw_close = num(row.get("不复权收盘"))
        qfq_close = num(row.get("收盘"))
        row["前复权因子"] = qfq_close / raw_close if qfq_close is not None and raw_close else None
        exact_amount = num(exact.get("精确成交额"))
        if exact_amount is not None:
            row["成交额"] = exact_amount
            row["成交额口径"] = "新浪原始日线精确值（元）"
        row["历史流通股本(股)"] = num(exact.get("历史流通股本(股)"))
        row["精确历史换手率(%)"] = num(exact.get("精确历史换手率(%)"))
        row["盘后成交量(股)"] = num(exact.get("盘后成交量(股)"))
        row["盘后成交额"] = num(exact.get("盘后成交额"))
        row["精确历史字段来源"] = exact.get("精确历史字段来源") or ""
        if exact:
            row["精确历史换手率口径"] = "成交量(股)÷当日匹配的历史流通股本×100%；与行情商展示口径分列"
        daily.append(row)
    benchmark = []
    for row in split_rows((raw.get("benchmark_daily", {}).get("data") or {}).get("klines", []), ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]):
        for key in list(row)[1:]:
            row[key] = num(row[key])
        benchmark.append(row)
    details = []
    detail_format = (raw.get("details", {}).get("meta") or {}).get("format", "")
    for sequence, line in enumerate(((raw.get("details", {}).get("data") or {}).get("details") or []), start=1):
        p = line.split(",")
        if len(p) >= 6:
            # Eastmoney push2: time, price, volume, trades/flag, direction, ...
            # It does not contain exact turnover, so do not invent one.
            phase, nature, main_session = detail_trading_phase(p[0])
            price, volume = num(p[1]), num(p[2])
            details.append({"序号": sequence, "\u65f6\u95f4": p[0], "\u4ef7\u683c": price, "\u6210\u4ea4\u91cf": volume, "\u65b9\u5411": direction_name(p[4]), "\u91d1\u989d": None, "估算金额": price * volume * 100 if price is not None and volume is not None and nature == "成交记录" else None, "金额口径": "价格×成交量×100估算；非接口精确金额" if nature == "成交记录" else "非成交记录", "成交笔数/标记": num(p[3]), "交易阶段": phase, "记录性质": nature, "计入主交易统计": main_session})
        elif len(p) >= 5:
            if detail_format == "eastmoney":
                # Eastmoney webguest: time, price, volume, trades/flag, direction.
                volume = num(p[2])
                price = num(p[1]); phase, nature, main_session = detail_trading_phase(p[0])
                details.append({"序号": sequence, "\u65f6\u95f4": p[0], "\u4ef7\u683c": price, "\u6210\u4ea4\u91cf": volume, "\u65b9\u5411": direction_name(p[4]), "\u91d1\u989d": None, "估算金额": price * volume * 100 if price is not None and volume is not None and nature == "成交记录" else None, "金额口径": "价格×成交量×100估算；非接口精确金额" if nature == "成交记录" else "非成交记录", "成交笔数/标记": num(p[3]), "交易阶段": phase, "记录性质": nature, "计入主交易统计": main_session})
            else:
                # 腾讯备用格式：时间、价格、成交量、方向、成交额。
                price, volume = num(p[1]), num(p[2]); phase, nature, main_session = detail_trading_phase(p[0])
                details.append({"序号": sequence, "\u65f6\u95f4": p[0], "\u4ef7\u683c": price, "\u6210\u4ea4\u91cf": volume, "\u65b9\u5411": direction_name(p[3]), "\u91d1\u989d": num(p[4]), "估算金额": price * volume * 100 if price is not None and volume is not None else None, "金额口径": "腾讯3秒汇总接口值", "成交笔数/标记": None, "交易阶段": phase, "记录性质": nature, "计入主交易统计": main_session})
    billboard = []
    for row in ((raw.get("billboard", {}).get("result") or {}).get("data") or []):
        billboard.append({"\u4ea4\u6613\u65e5\u671f": str(row.get("TRADE_DATE", ""))[:10], "\u4e0a\u699c\u539f\u56e0": row.get("EXPLANATION") or row.get("EXPLAIN") or "", "\u6536\u76d8\u4ef7": row.get("CLOSE_PRICE"), "\u6da8\u8dcc\u5e45(%)": row.get("CHANGE_RATE"), "\u6362\u624b\u7387(%)": row.get("TURNOVERRATE"), "\u4e70\u5165\u989d": row.get("BILLBOARD_BUY_AMT"), "\u5356\u51fa\u989d": row.get("BILLBOARD_SELL_AMT"), "\u51c0\u4e70\u5165\u989d": row.get("BILLBOARD_NET_AMT"), "\u540e1\u65e5\u6da8\u8dcc(%)": row.get("D1_CLOSE_ADJCHRATE")})
    detail_meta = dict(raw.get("details", {}).get("meta") or {})
    fetched_at = datetime.fromisoformat(raw["fetched_at"])
    final_cutoff_reached = fetched_at.time() >= datetime.strptime("15:30:00", "%H:%M:%S").time()
    interface_complete = bool(detail_meta.get("complete") or detail_meta.get("coverage_complete"))
    detail_meta["interface_complete"] = interface_complete
    detail_meta["final_complete"] = bool(interface_complete and final_cutoff_reached)
    detail_meta["completeness_note"] = ("收盘后最终全量" if detail_meta["final_complete"] else "截至采集时刻的接口全量；盘后定价尚可能增加") if interface_complete else "备用数据或序号覆盖不完整"
    detail_meta["main_trade_rows"] = sum(row.get("计入主交易统计") == 1 for row in details)
    detail_meta["auction_snapshot_rows"] = sum(row.get("记录性质") == "行情快照" for row in details)
    detail_meta["after_hours_rows"] = sum(row.get("交易阶段") == "盘后定价交易" for row in details)
    # 逐笔原始序号只用于完整性核对，不改变展示顺序。
    return {"identity": identity, "fetched_at": raw["fetched_at"], "quote": quote, "intraday": intraday, "details": details, "details_meta": detail_meta, "daily": daily, "benchmark_name": raw.get("benchmark_name", ""), "benchmark_history": benchmark, "billboard": billboard, "errors": raw.get("errors", {}), "sources": raw.get("sources", {})}


def build_markdown_report(result: dict[str, Any], health: list[dict[str, Any]] | None = None) -> str:
    identity = result["identity"]
    quote = result["quote"]
    daily = result.get("daily", [])
    latest = daily[-1] if daily else {}
    first = daily[0] if daily else {}
    close0, close1 = num(first.get("收盘")), num(latest.get("收盘"))
    window_change = ((close1 / close0) - 1) * 100 if close0 and close1 else None
    close20 = num(daily[-21].get("收盘")) if len(daily) >= 21 else None
    change20 = ((close1 / close20) - 1) * 100 if close1 and close20 else None
    detail_meta = result.get("details_meta") or {}
    lines = [
        f"# {identity.get('name') or '股票'}（{identity.get('code')}）分析报告",
        "", f"- 生成时间：{result.get('fetched_at', '')}", f"- 市场：{identity.get('market', '').upper()}",
        f"- 日线来源：{(result.get('sources') or {}).get('daily', '未标记')}",
        f"- 逐笔来源：{(result.get('sources') or {}).get('details', '未标记')}（{detail_meta.get('completeness_note', '完整性未标记')}）",
        "", "## 当前行情", "", "| 指标 | 数值 |", "|---|---:|",
    ]
    for key in ("最新价", "昨收", "涨跌额", "涨跌幅(%)", "成交额", "成交量(手)", "换手率(%)", "今开", "最高", "最低", "总市值", "流通市值"):
        value = quote.get(key)
        lines.append(f"| {key} | {amount(value) if key in {'成交额', '总市值', '流通市值'} else fmt(value) } |")
    analysis = result.get("analysis") or build_analysis(result)
    returns = analysis.get("阶段收益(%)") or {}
    relative = analysis.get("相对强弱") or {}
    averages = analysis.get("均线") or {}
    momentum = analysis.get("动量指标") or {}
    risk = analysis.get("波动风险") or {}
    volume_price = analysis.get("量价") or {}
    intraday_analysis = analysis.get("分时") or {}
    active = analysis.get("主动成交") or {}
    lines += [
        "", "## 技术走势分析", "",
        f"- 趋势：{analysis.get('趋势判断', '暂无')}",
        "- 阶段收益：" + "；".join(f"{key} {fmt(value)}%" for key, value in returns.items()),
        f"- 相对强弱（基准：{relative.get('基准', '未获取')}）：" + "；".join(f"{key}超额 {fmt(value)} 个百分点" for key, value in (relative.get('超额收益(百分点)') or {}).items()),
        "- 均线：" + "；".join(f"{key} {fmt(value)}" for key, value in averages.items()),
        f"- MACD：DIF {fmt(momentum.get('DIF'))}，DEA {fmt(momentum.get('DEA'))}，柱值 {fmt(momentum.get('MACD'))}",
        f"- RSI：RSI6 {fmt(momentum.get('RSI6'))}，RSI12 {fmt(momentum.get('RSI12'))}",
        f"- KDJ：K {fmt(momentum.get('K'))}，D {fmt(momentum.get('D'))}，J {fmt(momentum.get('J'))}",
        f"- 波动风险：ATR14 {fmt(risk.get('ATR14'))}；20日年化波动率 {fmt(risk.get('20日年化波动率(%)'))}%；近120日最大回撤 {fmt(risk.get('近120日最大回撤(%)'))}%",
        f"- 20日价格区间（不是严格支撑/压力位）：最低 {fmt(risk.get('20日最低'))} / 最高 {fmt(risk.get('20日最高'))}",
        f"- 量价：当日量比(MA5) {fmt(volume_price.get('当日量比_MA5'))}；5日均量 {fmt(volume_price.get('5日均量(手)'), 0)} 手；OBV {fmt(volume_price.get('OBV'), 0)}",
        f"- 分时主交易：VWAP {fmt(intraday_analysis.get('主交易VWAP'))}；最新价相对主交易VWAP {fmt(intraday_analysis.get('最新价相对主交易VWAP(%)'))}%",
        f"- 盘后定价：{fmt((intraday_analysis.get('盘后定价') or {}).get('成交量(手)'), 0)} 手，{(intraday_analysis.get('盘后定价') or {}).get('记录数', 0)} 条分钟时间点（含零增量时间点）",
        f"- 主交易主动成交：买盘 {fmt(active.get('买盘量(手)'), 0)} 手；卖盘 {fmt(active.get('卖盘量(手)'), 0)} 手；净主动量 {fmt(active.get('净主动量(手)'), 0)} 手；买盘占比 {fmt(active.get('买盘占主动成交(%)'))}%",
        f"- 已排除：竞价快照 {active.get('竞价快照记录数', 0)} 条；盘后定价逐笔 {active.get('盘后定价记录数', 0)} 条 / {fmt(active.get('盘后定价成交量(手)'), 0)} 手",
    ]
    lines += ["", "## 最近60个交易日日线", "", "| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in daily:
        lines.append("| " + " | ".join([str(row.get("日期", "")), fmt(row.get("开盘")), fmt(row.get("收盘")), fmt(row.get("最高")), fmt(row.get("最低")), fmt(row.get("成交量"), 0), amount(row.get("成交额"))]) + " |")
    completeness = detail_meta.get("completeness_note") or "完整性未标记"
    lines += ["", "## 走势摘要", "", f"- 最近{len(daily)}个交易日首尾收盘变化：{fmt(window_change)}%" if window_change is not None else f"- 最近{len(daily)}个交易日首尾收盘变化：暂无", f"- 20个交易日收益：{fmt(change20)}%" if change20 is not None else "- 20个交易日收益：暂无", f"- 当日分时记录：{len(result.get('intraday', []))} 条（主交易和盘后定价已分列）", f"- 逐笔接口记录：{len(result.get('details', []))} 条（{completeness}）", f"- 其中主交易成交记录：{detail_meta.get('main_trade_rows', 0)} 条；竞价快照：{detail_meta.get('auction_snapshot_rows', 0)} 条；盘后定价：{detail_meta.get('after_hours_rows', 0)} 条", f"- 龙虎榜近31天记录：{len(result.get('billboard', []))} 条"]
    closes = [num(row.get("收盘")) for row in daily if num(row.get("收盘")) is not None]
    highs = [num(row.get("最高")) for row in daily if num(row.get("最高")) is not None]
    lows = [num(row.get("最低")) for row in daily if num(row.get("最低")) is not None]
    volumes = [num(row.get("成交量"), 0) or 0 for row in daily]
    if closes:
        lines += [f"- 最近{len(daily)}个交易日最高/最低：{fmt(max(highs))} / {fmt(min(lows))}", f"- 最近{len(daily)}个交易日收盘均价：{fmt(sum(closes) / len(closes))}", f"- 最近5日平均成交量：{fmt(sum(volumes[-5:]) / max(1, len(volumes[-5:])), 0)}"]
        if len(closes) >= 5:
            trend = "上行" if closes[-1] > closes[-5] else "下行" if closes[-1] < closes[-5] else "横盘"
            lines.append(f"- 最近5日收盘趋势：{trend}")
    chips = result.get("chips") or {}
    lines += ["", "## 估算筹码分布", "", f"- 估算平均成本：{fmt(chips.get('平均成本'))}", f"- 说明：{chips.get('口径', '')}"]
    peaks = sorted(chips.get("筹码峰", []), key=lambda row: row.get("占比(%)", 0), reverse=True)[:8]
    if peaks:
        lines += ["", "| 成本价 | 估算筹码量 | 占比 |", "|---:|---:|---:|"]
        lines += [f"| {fmt(row['成本价'])} | {fmt(row['估算筹码量'], 0)} | {fmt(row['占比(%)'])}% |" for row in peaks]
    lines += ["", "## 龙虎榜（最近31天）", "", "| 日期 | 上榜原因 | 买入额 | 卖出额 | 净买入额 |", "|---|---|---:|---:|---:|"]
    for row in result.get("billboard", []):
        lines.append(f"| {row.get('交易日期','')} | {row.get('上榜原因','')} | {amount(row.get('买入额'))} | {amount(row.get('卖出额'))} | {amount(row.get('净买入额'))} |")
    lines += ["", "## 尚未包含的数据", ""]
    lines += [f"- {item}" for item in analysis.get("数据限制", [])]
    lines += ["", "> 说明：不同软件可能使用不同数据源、复权方式、成交口径和更新时间；本报告不构成投资建议。"]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_trade_summary_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    details = result.get("details") or []
    definitions = [
        ("开盘竞价快照", lambda row: row.get("记录性质") == "行情快照", "不计入成交量"),
        ("主交易成交", lambda row: row.get("计入主交易统计") == 1 and row.get("记录性质") == "成交记录", "计入行情成交量"),
        ("盘后定价成交", lambda row: row.get("交易阶段") == "盘后定价交易" and row.get("记录性质") == "成交记录", "单独统计，不并入15:00主交易量"),
    ]
    rows: list[dict[str, Any]] = []
    for category, predicate, treatment in definitions:
        selected = [row for row in details if predicate(row)]
        rows.append({
            "类别": category, "记录数": len(selected),
            "成交量/快照量(手)": sum(num(row.get("成交量"), 0) or 0 for row in selected),
            "接口精确金额": sum(num(row.get("金额"), 0) or 0 for row in selected) if any(num(row.get("金额")) is not None for row in selected) else None,
            "估算金额": round(sum(num(row.get("估算金额"), 0) or 0 for row in selected), 2) if any(num(row.get("估算金额")) is not None for row in selected) else None,
            "统计处理": treatment,
        })
    return rows


def build_data_quality_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    daily = result.get("daily_indicators") or []
    details = result.get("details") or []
    meta = result.get("details_meta") or {}
    raw_count = sum(num(row.get("不复权收盘")) is not None for row in daily)
    exact_daily_amount = sum(num(row.get("成交额")) is not None for row in daily)
    exact_turnover_count = sum(num(row.get("精确历史换手率(%)")) is not None for row in daily)
    exact_share_count = sum(num(row.get("历史流通股本(股)")) is not None for row in daily)
    after_amount_count = sum(num(row.get("盘后成交额")) is not None for row in daily)
    latest_exact_turnover = num(daily[-1].get("精确历史换手率(%)")) if daily else None
    latest_display_turnover = num(daily[-1].get("行情显示换手率(%)")) if daily else None
    exact_detail_amount = sum(num(row.get("金额")) is not None for row in details)
    limit_count = sum(num(row.get("理论涨停价")) is not None for row in daily)
    main_detail_volume = sum(num(row.get("成交量"), 0) or 0 for row in details if row.get("计入主交易统计") == 1 and row.get("记录性质") == "成交记录")
    main_minute_volume = sum(num(row.get("成交量"), 0) or 0 for row in result.get("intraday") or [] if row.get("计入主交易统计") == 1)
    quote_volume = num((result.get("quote") or {}).get("成交量(手)"))
    rows = [
        {"数据项": "前复权日线OHLCV", "状态": "已导出" if daily else "缺失", "覆盖": f"{len(daily)}行", "来源/口径": (result.get("sources") or {}).get("daily", "未标记"), "限制": "当日盘中值会变化"},
        {"数据项": "不复权日线OHLC", "状态": "已导出" if raw_count else "缺失", "覆盖": f"{raw_count}/{len(daily)}行", "来源/口径": (result.get("sources") or {}).get("raw_daily", "Tencent companion or current quote"), "限制": "用于核验复权和涨跌停，不替代前复权技术分析"},
        {"数据项": "前复权因子", "状态": "已计算" if raw_count else "缺失", "覆盖": f"{raw_count}/{len(daily)}行", "来源/口径": "前复权收盘÷不复权收盘", "限制": "价格序列比值，不是公司行动明细表"},
        {"数据项": "历史精确成交额", "状态": "已导出" if exact_daily_amount == len(daily) else "部分/已导出" if exact_daily_amount else "源未提供", "覆盖": f"{exact_daily_amount}/{len(daily)}行", "来源/口径": "新浪原始日线 amount（元）/当日实时行情", "限制": "按日期与主OHLCV序列合并并对账"},
        {"数据项": "历史精确换手率", "状态": "已导出" if exact_turnover_count == len(daily) else "部分/已导出" if exact_turnover_count else "源未提供", "覆盖": f"{exact_turnover_count}/{len(daily)}行", "来源/口径": "新浪成交股数÷对应日期历史流通股本×100%", "限制": "与行情商显示值可能因股本生效日、无限售/自由流通口径或四舍五入存在差异"},
        {"数据项": "最新换手率口径对照", "状态": "已导出", "覆盖": f"历史股本口径{latest_exact_turnover:.4f}% / 行情显示{latest_display_turnover:.4f}%" if latest_exact_turnover is not None and latest_display_turnover is not None else "口径之一缺失", "来源/口径": "历史流通股本计算值 vs 实时行情商显示值", "限制": "两个字段并存，不互相覆盖"},
        {"数据项": "历史流通股本", "状态": "已导出" if exact_share_count == len(daily) else "部分/已导出", "覆盖": f"{exact_share_count}/{len(daily)}行", "来源/口径": "新浪历史流通股本变更序列前向匹配", "限制": "单位：股"},
        {"数据项": "历史盘后成交量/额", "状态": "已导出" if after_amount_count else "源未提供", "覆盖": f"{after_amount_count}/{len(daily)}行", "来源/口径": "新浪原始日线 postVol/postAmt", "限制": "非盘后交易股票或无成交日通常为0"},
        {"数据项": "历史估算成交额（备用）", "状态": "已保留", "覆盖": f"{len(daily)}行", "来源/口径": "不复权典型价×成交量×100", "限制": "仅用于精确源失败时的降级，不替代精确成交额"},
        {"数据项": "按当前流通股本估算换手率（备用）", "状态": "已保留", "覆盖": f"{len(daily)}行", "来源/口径": "成交股数÷当前流通股本", "限制": "仅用于精确历史流通股本源失败时的降级"},
        {"数据项": "理论涨跌停/触板标记", "状态": "已导出", "覆盖": f"{limit_count}/{len(daily)}行", "来源/口径": "不复权昨收×当前板块规则", "限制": "首行无昨收；IPO无涨跌幅期和历史ST状态需复核"},
        {"数据项": "逐笔主交易量", "状态": "已导出", "覆盖": f"{meta.get('main_trade_rows', 0)}条", "来源/口径": "排除竞价快照和盘后定价", "限制": meta.get("completeness_note", "")},
        {"数据项": "逐笔主交易量与行情对账", "状态": "通过" if quote_volume is not None and abs(main_detail_volume - quote_volume) < 0.001 else "不一致", "覆盖": f"逐笔{main_detail_volume:.0f}手 / 行情{quote_volume:.0f}手" if quote_volume is not None else f"逐笔{main_detail_volume:.0f}手 / 行情缺失", "来源/口径": "主交易成交记录 vs 行情成交量", "限制": "盘中抓取时两接口时间点可能相差数秒"},
        {"数据项": "分钟主交易量与行情对账", "状态": "通过" if quote_volume is not None and abs(main_minute_volume - quote_volume) < 0.001 else "不一致", "覆盖": f"分钟{main_minute_volume:.0f}手 / 行情{quote_volume:.0f}手" if quote_volume is not None else f"分钟{main_minute_volume:.0f}手 / 行情缺失", "来源/口径": "15:00前分钟增量 vs 行情成交量", "限制": "盘中抓取时两接口时间点可能相差数秒"},
        {"数据项": "逐笔精确金额", "状态": "已导出" if exact_detail_amount else "接口未提供", "覆盖": f"{exact_detail_amount}/{len(details)}行", "来源/口径": "原接口字段", "限制": "缺失时另列价格×成交量×100估算金额"},
        {"数据项": "行业指数和同业排名", "状态": "尚未接入", "覆盖": "0", "来源/口径": "—", "限制": "需要证券行业分类和成分历史库"},
        {"数据项": "财务/估值/公告/新闻/资金流/融资融券", "状态": "尚未接入", "覆盖": "0", "来源/口径": "—", "限制": "属于低频和事件数据，需单独数据源及时间对齐"},
    ]
    return rows


def write_svg(path: Path, rows: list[dict[str, Any]], title: str) -> None:
    prices = [num(row.get("\u4ef7\u683c")) for row in rows]
    prices = [x for x in prices if x is not None]
    if not prices:
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='800' height='400'><text x='20' y='30'>No intraday data</text></svg>", encoding="utf-8")
        return
    width, height, left, top = 1000, 500, 60, 40
    low, high = min(prices), max(prices) or min(prices) + 1
    if high == low:
        high += 1
    def x(i: int) -> float:
        return left + i * (width - left - 20) / max(1, len(prices) - 1)
    def y(v: float) -> float:
        return top + (high - v) * 320 / (high - low)
    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(prices))
    path.write_text(f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'><rect width='100%' height='100%' fill='#101827'/><text x='{left}' y='25' fill='white'>{title}</text><polyline points='{points}' fill='none' stroke='#38bdf8' stroke-width='2'/></svg>", encoding="utf-8")


def export_result(result: dict[str, Any], folder: str, health: list[dict[str, Any]] | None = None) -> list[Path]:
    """Export only machine-readable datasets needed for subsequent analysis."""
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{result['identity']['code']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    daily = target / f"{stem}_daily_indicators.csv"
    benchmark = target / f"{stem}_benchmark.csv"
    intraday = target / f"{stem}_intraday.csv"
    details = target / f"{stem}_details.csv"
    snapshot = target / f"{stem}_analysis.json"
    daily_rows = result.get("daily_indicators") or enrich_daily_indicators(result.get("daily_history") or result.get("daily") or [], result.get("identity"), result.get("quote"))
    write_csv(daily, daily_rows)
    write_csv(benchmark, result.get("benchmark_indicators") or result.get("benchmark_history") or [])
    write_csv(intraday, result.get("intraday") or [])
    write_csv(details, result.get("details") or [])
    payload = {
        "identity": result.get("identity"), "fetched_at": result.get("fetched_at"),
        "quote": result.get("quote"), "analysis": result.get("analysis") or build_analysis(result),
        "details_meta": result.get("details_meta"), "trade_summary": build_trade_summary_rows(result),
        "data_quality": build_data_quality_rows(result), "sources": result.get("sources"), "errors": result.get("errors"),
    }
    snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return [path for path in (daily, benchmark, intraday, details, snapshot) if path.exists()]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x650")
        self.minsize(760, 480)
        self.result: dict[str, Any] | None = None
        self.live_identity: StockIdentity | None = None
        self.live_enabled = False
        self.live_busy = False
        self.live_job = None
        self.live_generation = 0
        self.health: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.build()
        self.after(150, self.poll)

    def build(self):
        top = ttk.Frame(self, padding=12); top.pack(fill="x")
        ttk.Label(top, text=APP_TITLE, font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ttk.Label(top, text="\u540d\u79f0/\u4ee3\u7801/\u7f51\u5740", foreground="#64748b").pack(side="left", padx=(14, 6))
        self.query = tk.StringVar(value=DEFAULT_QUERY)
        ttk.Entry(top, textvariable=self.query, width=20).pack(side="left")
        ttk.Button(top, text="\u67e5\u8be2", command=self.start_fetch).pack(side="left", padx=6)
        self.live_button = ttk.Button(top, text="\u5f00\u542f\u5b9e\u65f6\u5237\u65b0", command=self.toggle_live)
        self.live_button.pack(side="left", padx=6)
        ttk.Button(top, text="\u63a5\u53e3\u6d4b\u8bd5", command=self.start_health).pack(side="left")
        self.export = ttk.Button(top, text="\u5bfc\u51fa", command=self.do_export, state="disabled"); self.export.pack(side="left", padx=6)
        self.status = tk.StringVar(value="\u5c31\u7eea"); ttk.Label(self, textvariable=self.status, foreground="#64748b").pack(fill="x", padx=14)
        self.tabs = ttk.Notebook(self); self.tabs.pack(fill="both", expand=True, padx=12, pady=12)
        self.overview = ttk.Frame(self.tabs, padding=10); self.chart = ttk.Frame(self.tabs, padding=10); self.detail = ttk.Frame(self.tabs, padding=10); self.daily = ttk.Frame(self.tabs, padding=10); self.billboard = ttk.Frame(self.tabs, padding=10); self.chips_tab = ttk.Frame(self.tabs, padding=10); self.health_tab = ttk.Frame(self.tabs, padding=10)
        for tab, text in ((self.overview,"\u6982\u89c8"),(self.chart,"\u5206\u65f6"),(self.detail,"\u9010\u7b14"),(self.daily,"\u65e5\u7ebf"),(self.billboard,"\u9f99\u864e\u699c"),(self.chips_tab,"\u7b79\u7801"),(self.health_tab,"\u63a5\u53e3\u6d4b\u8bd5")): self.tabs.add(tab, text=text)
        self.metrics: dict[str, tk.StringVar] = {}
        for i, name in enumerate(("\u540d\u79f0","\u6700\u65b0\u4ef7","\u6da8\u8dcc\u5e45(%)","\u6210\u4ea4\u989d","\u6210\u4ea4\u91cf(\u624b)","\u6362\u624b\u7387(%)","\u603b\u5e02\u503c","\u6d41\u901a\u5e02\u503c","\u4eca\u5f00","\u6700\u9ad8","\u6700\u4f4e","估算平均成本")):
            box=ttk.Frame(self.overview,padding=10); box.grid(row=i//4,column=i%4,sticky="nsew",padx=4,pady=4); self.overview.columnconfigure(i%4,weight=1); ttk.Label(box,text=name,foreground="#64748b").pack(anchor="w"); self.metrics[name]=tk.StringVar(value="-"); ttk.Label(box,textvariable=self.metrics[name],font=("Segoe UI",14,"bold")).pack(anchor="w")
        self.warning=tk.StringVar(); ttk.Label(self.overview,textvariable=self.warning,foreground="#b45309",wraplength=1050).grid(row=3,column=0,columnspan=4,sticky="w",pady=15)
        self.canvas=tk.Canvas(self.chart,bg="#101827",highlightthickness=0); self.canvas.pack(fill="both",expand=True)
        self.current_intraday: list[dict[str, Any]] = []
        self.canvas.bind("<Configure>", lambda _event: self.draw(self.current_intraday))
        self.trees={"detail":self.tree(self.detail,[("\u65f6\u95f4",100),("\u4ef7\u683c",90),("\u6210\u4ea4\u91cf",100),("\u65b9\u5411",90),("\u91d1\u989d",120)]),"daily":self.tree(self.daily,[("\u65e5\u671f",100),("\u5f00\u76d8",90),("\u6536\u76d8",90),("\u6700\u9ad8",90),("\u6700\u4f4e",90),("\u6210\u4ea4\u91cf",110),("\u6210\u4ea4\u989d",130)]),"billboard":self.tree(self.billboard,[("\u4ea4\u6613\u65e5\u671f",100),("\u4e0a\u699c\u539f\u56e0",300),("\u6536\u76d8\u4ef7",90),("\u6da8\u8dcc\u5e45(%)",95),("\u6362\u624b\u7387(%)",95),("\u4e70\u5165\u989d",120),("\u5356\u51fa\u989d",120),("\u51c0\u4e70\u5165\u989d",120)]),"chips":self.tree(self.chips_tab,[("\u6210\u672c\u4ef7",120),("\u4f30\u7b97\u7b79\u7801\u91cf",160),("\u5360\u6bd4(%)",100)]),"health":self.tree(self.health_tab,[("\u63a5\u53e3",190),("\u72b6\u6001",80),("\u8017\u65f6(ms)",90),("\u8bf4\u660e",700)])}

    def tree(self, parent, cols):
        frame=ttk.Frame(parent); frame.pack(fill="both",expand=True); names=[x[0] for x in cols]; tree=ttk.Treeview(frame,columns=names,show="headings"); sy=ttk.Scrollbar(frame,orient="vertical",command=tree.yview); tree.configure(yscrollcommand=sy.set); tree.grid(row=0,column=0,sticky="nsew"); sy.grid(row=0,column=1,sticky="ns"); frame.rowconfigure(0,weight=1); frame.columnconfigure(0,weight=1)
        for name,width in cols: tree.heading(name,text=name); tree.column(name,width=width,anchor="center")
        return tree

    def start_fetch(self):
        try: identity=parse_query(self.query.get())
        except SpiderError as exc: messagebox.showerror("\u8f93\u5165\u6709\u8bef",str(exc)); return
        if self.live_enabled:
            self.toggle_live()
        self.status.set("\u67e5\u8be2\u4e2d..."); threading.Thread(target=self.fetch_worker,args=(identity,),daemon=True).start()

    def toggle_live(self):
        if self.live_enabled:
            self.live_enabled = False
            self.live_generation += 1
            if self.live_job is not None:
                self.after_cancel(self.live_job)
                self.live_job = None
            self.live_button.configure(text="\u5f00\u542f\u5b9e\u65f6\u5237\u65b0")
            self.status.set("\u5b9e\u65f6\u5237\u65b0\u5df2\u505c\u6b62")
            return
        if not self.result or not self.live_identity:
            messagebox.showinfo("\u63d0\u793a", "\u8bf7\u5148\u67e5\u8be2\u4e00\u53ea\u80a1\u7968")
            return
        self.live_enabled = True
        self.live_generation += 1
        self.live_button.configure(text="\u505c\u6b62\u5b9e\u65f6\u5237\u65b0")
        self.status.set("\u5b9e\u65f6\u5237\u65b0\u5df2\u5f00\u542f（5\u79d2\u4e00\u6b21）")
        self.live_tick()

    def live_tick(self):
        if not self.live_enabled or not self.live_identity or self.live_busy:
            return
        self.live_busy = True
        threading.Thread(target=self.live_worker, args=(self.live_identity, self.live_generation), daemon=True).start()

    def live_worker(self, identity, generation):
        started = time.perf_counter()
        try:
            client = Client(timeout=8, pause=0.03)
            with ThreadPoolExecutor(max_workers=3) as pool:
                quote_future = pool.submit(client.tencent_quote, identity)
                intraday_future = pool.submit(client.tencent_intraday, identity)
                details_future = pool.submit(client.eastmoney_details, identity)
                quote = quote_future.result()
                intraday = intraday_future.result()
                try:
                    details = details_future.result()
                except SpiderError:
                    details = {}
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            self.events.append(("live", (generation, quote, intraday, details, datetime.now().strftime("%H:%M:%S"), elapsed_ms)))
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            self.events.append(("live_error", (generation, str(exc), elapsed_ms)))
        finally:
            self.live_busy = False

    def fetch_worker(self, identity):
        try: self.events.append(("result",Client().fetch(identity)))
        except Exception as exc: self.events.append(("error",str(exc)))

    def start_health(self):
        try: identity=parse_query(self.query.get())
        except SpiderError as exc: messagebox.showerror("\u8f93\u5165\u6709\u8bef",str(exc)); return
        self.status.set("\u63a5\u53e3\u6d4b\u8bd5\u4e2d..."); threading.Thread(target=self.health_worker,args=(identity,),daemon=True).start()

    def health_worker(self, identity):
        try:
            c=Client(); identity=c.resolve(identity); self.events.append(("health",(identity,c.health(identity))))
        except Exception as exc: self.events.append(("error",str(exc)))

    def poll(self):
        while self.events:
            kind,payload=self.events.pop(0)
            if kind=="result":
                self.result=payload
                identity_data = payload.get("identity", {})
                self.live_identity = StockIdentity(**{key: identity_data.get(key, "") for key in ("code", "market", "secid", "name", "source")})
                self.render(payload); self.export.configure(state="normal"); self.status.set("\u67e5\u8be2\u5b8c\u6210")
            elif kind=="live":
                generation = payload[0]
                if self.live_enabled and generation == self.live_generation:
                    self.apply_live(payload[1:5])
                    self.live_job = self.after(max(200, 5000 - payload[5]), self.live_tick)
            elif kind=="live_error":
                generation, _error, elapsed_ms = payload
                if self.live_enabled and generation == self.live_generation:
                    self.status.set("\u5b9e\u65f6\u66f4\u65b0\u5931\u8d25，5\u79d2\u540e\u91cd\u8bd5")
                    self.live_job = self.after(max(200, 5000 - elapsed_ms), self.live_tick)
            elif kind=="health":
                self.health=payload[1]; self.fill(self.trees["health"],self.health,["\u63a5\u53e3","\u72b6\u6001","\u8017\u65f6(ms)","\u8bf4\u660e"]); ok=sum(x.get("\u72b6\u6001")=="\u6b63\u5e38" for x in self.health); self.status.set(f"\u63a5\u53e3\u6d4b\u8bd5\u5b8c\u6210: {ok}/{len(self.health)} \u6b63\u5e38")
            else: messagebox.showerror("\u9519\u8bef",payload); self.status.set("\u5931\u8d25")
        self.after(150,self.poll)

    def render(self, result):
        self.current_intraday = result.get("intraday", [])
        self.metrics["\u540d\u79f0"].set(result["identity"].get("name") or result["identity"]["code"])
        self.metrics["估算平均成本"].set(fmt((result.get("chips") or {}).get("平均成本")))
        for key,var in self.metrics.items():
            if key=="\u540d\u79f0": continue
            if key=="估算平均成本": continue
            value=result["quote"].get(key); var.set(amount(value) if key in {"\u6210\u4ea4\u989d","\u603b\u5e02\u503c","\u6d41\u901a\u5e02\u503c"} else (fmt(value,0) if key=="\u6210\u4ea4\u91cf(\u624b)" else fmt(value)+("%" if "%" in key else "")))
        self.warning.set("\u90e8\u5206\u63a5\u53e3\u5931\u8d25: "+", ".join(result.get("errors",{})) if result.get("errors") else "\u6240\u6709\u4e3b\u8981\u6570\u636e\u5df2\u66f4\u65b0")
        self.fill(self.trees["detail"],result["details"],["\u65f6\u95f4","\u4ef7\u683c","\u6210\u4ea4\u91cf","\u65b9\u5411","\u91d1\u989d"]); self.fill(self.trees["daily"],result["daily"],["\u65e5\u671f","\u5f00\u76d8","\u6536\u76d8","\u6700\u9ad8","\u6700\u4f4e","\u6210\u4ea4\u91cf","\u6210\u4ea4\u989d"]); self.fill(self.trees["billboard"],result["billboard"],["\u4ea4\u6613\u65e5\u671f","\u4e0a\u699c\u539f\u56e0","\u6536\u76d8\u4ef7","\u6da8\u8dcc\u5e45(%)","\u6362\u624b\u7387(%)","\u4e70\u5165\u989d","\u5356\u51fa\u989d","\u51c0\u4e70\u5165\u989d"]); self.fill(self.trees["chips"],(result.get("chips") or {}).get("筹码峰",[]),["成本价","估算筹码量","占比(%)"]); self.draw(result["intraday"])

    def update_quote_view(self, result):
        self.metrics["\u540d\u79f0"].set(result["identity"].get("name") or result["identity"]["code"])
        self.metrics["估算平均成本"].set(fmt((result.get("chips") or {}).get("平均成本")))
        for key, var in self.metrics.items():
            if key in {"\u540d\u79f0", "估算平均成本"}:
                continue
            value = result["quote"].get(key)
            var.set(amount(value) if key in {"\u6210\u4ea4\u989d", "\u603b\u5e02\u503c", "\u6d41\u901a\u5e02\u503c"} else (fmt(value, 0) if key == "\u6210\u4ea4\u91cf(\u624b)" else fmt(value) + ("%" if "%" in key else "")))

    def apply_live(self, payload):
        quote, intraday, details, updated_at = payload
        if not self.result:
            return
        live_fetched_at = datetime.now().isoformat(timespec="seconds")
        previous_quote = dict(self.result.get("quote") or {})
        temp = normalize({"identity": dict(self.result["identity"]), "fetched_at": live_fetched_at, "quote": quote, "intraday": intraday, "daily": {}, "details": details, "billboard": {}, "errors": {}, "sources": {}})
        previous_price = num(previous_quote.get("最新价"))
        live_price = num(temp["quote"].get("最新价"))
        if previous_price and live_price:
            for key in ("总市值", "流通市值"):
                previous_cap = num(previous_quote.get(key))
                if previous_cap:
                    temp["quote"][key] = previous_cap / previous_price * live_price
        self.result["quote"] = temp["quote"]
        self.result["fetched_at"] = live_fetched_at
        self.result["intraday"] = temp["intraday"]
        if details:
            self.result["details"] = temp["details"]
            self.result["details_meta"] = temp["details_meta"]
        sync_current_daily(self.result)
        self.result["daily_indicators"] = enrich_daily_indicators(self.result.get("daily_history") or self.result.get("daily") or [], self.result.get("identity"), self.result.get("quote"))
        self.result["analysis"] = build_analysis(self.result)
        self.current_intraday = temp["intraday"]
        self.update_quote_view(self.result)
        if details:
            self.fill(self.trees["detail"], self.result["details"], ["时间", "价格", "成交量", "方向", "金额"])
        self.fill(self.trees["daily"], self.result["daily"], ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"])
        self.draw(self.current_intraday)
        if self.live_enabled:
            self.status.set(f"\u5b9e\u65f6\u66f4\u65b0：{updated_at}")

    def fill(self, tree, rows, cols):
        tree.delete(*tree.get_children())
        for row in rows:
            vals=[]
            for col in cols:
                v=row.get(col,""); vals.append(amount(v) if col in {"\u91d1\u989d","\u6210\u4ea4\u989d","\u4e70\u5165\u989d","\u5356\u51fa\u989d","\u51c0\u4e70\u5165\u989d"} else (fmt(v) if isinstance(v,float) else v))
            tree.insert("","end",values=vals)

    def draw(self, rows):
        self.canvas.delete("all")
        prices = [num(x.get("\u4ef7\u683c")) for x in rows]
        prices = [x for x in prices if x is not None]
        w, h = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        if not prices:
            self.canvas.create_text(w / 2, h / 2, text="\u67e5\u8be2\u540e\u663e\u793a\u5206\u65f6\u56fe", fill="#94a3b8")
            return
        left, right, top, bottom = 52, 18, 30, 32
        plot_w, plot_h = max(1, w - left - right), max(1, h - top - bottom)
        low, high = min(prices), max(prices)
        if high <= low:
            high = low + 1
        points = []
        for i, value in enumerate(prices):
            x = left + i * plot_w / max(1, len(prices) - 1)
            y = top + (high - value) * plot_h / (high - low)
            points += [x, y]
        self.canvas.create_line(*points, fill="#38bdf8", width=2, smooth=True)
        self.canvas.create_text(left, 12, anchor="w", text="\u5206\u65f6\u4ef7\u683c", fill="white")
        self.canvas.create_text(left, h - 10, anchor="w", text=f"{low:.2f}", fill="#94a3b8")
        self.canvas.create_text(left, top, anchor="w", text=f"{high:.2f}", fill="#94a3b8")

    def do_export(self):
        if not self.result: return
        folder=filedialog.askdirectory(title="\u9009\u62e9\u5bfc\u51fa\u76ee\u5f55")
        if folder: messagebox.showinfo("\u5bfc\u51fa\u5b8c\u6210","\n".join(map(str,export_result(self.result,folder))))


def self_test(query: str) -> int:
    c=Client(); identity=c.resolve(parse_query(query)); result=c.fetch(identity); print("resolved",identity.code,identity.market); print("rows",len(result["intraday"]),len(result["details"]),len(result["daily"]),len(result["billboard"])); print("errors",result["errors"]); print("health");
    for row in c.health(identity): print(row)
    paths=export_result(result,str(Path.cwd()/"exports")); print("exported",len(paths)); return 0


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--self-test",metavar="QUERY"); args=parser.parse_args()
    if args.self_test:
        try: return self_test(args.self_test)
        except SpiderError as exc: print("error",exc); return 2
    App().mainloop(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
