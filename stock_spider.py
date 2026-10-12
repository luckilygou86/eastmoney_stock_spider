"""Stock data desktop collector with optional historical-data decoder."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import re
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from sina_decoder import SINA_KLC_DECODER
from analysis_tools import build_quality_checks, summarize_dashboard

try:
    from py_mini_racer import py_mini_racer
except ImportError:  # The collector keeps working with the existing fallbacks.
    py_mini_racer = None


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
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
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


def market_timestamp(value: Any) -> str:
    """Canonical China-market timestamp; supports provider Unix timestamps."""
    digits = re.sub(r"\D", "", str(value or ""))
    try:
        if len(digits) == 14:
            datetime.strptime(digits, "%Y%m%d%H%M%S")
            return digits
        if len(digits) == 8:
            datetime.strptime(digits, "%Y%m%d")
            return digits
        timestamp = num(value)
        if timestamp is not None and 1000000000 <= timestamp < 10000000000:
            return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%Y%m%d%H%M%S")
    except (ValueError, OverflowError, OSError):
        pass
    return ""


def market_date(value: Any) -> str:
    stamp = market_timestamp(value)
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else ""


def merge_quote_precision(base: dict[str, Any], precise: dict[str, Any]) -> bool:
    """Only mix snapshots when their security, trading date, volume and price agree."""
    base_date, other_date = market_date(base.get("f86")), market_date(precise.get("f86"))
    volume, other_volume = num(base.get("f47")), num(precise.get("f47"))
    price, other_price = num(base.get("f43")), num(precise.get("f43"))
    if not base_date or base_date != other_date or str(base.get("f57")) != str(precise.get("f57")):
        return False
    if volume is None or other_volume is None or abs(volume - other_volume) > 0.001:
        return False
    if price is None or other_price is None or abs(price - other_price) > 0.001:
        return False
    for field in ("f48", "f116", "f117"):
        if num(precise.get(field)) is not None:
            base[field] = num(precise[field])
    return True


def decode_sina_daily(encoded: str) -> list[dict[str, Any]]:
    if py_mini_racer is not None:
        try:
            context = py_mini_racer.MiniRacer()
            context.eval(SINA_KLC_DECODER)
            return context.call("d", encoded) or []
        except Exception as exc:
            raise SpiderError(f"Sina daily decode failed: {exc}") from exc
    node = shutil.which("node")
    if not node:
        raise SpiderError("历史日线解码需要 py-mini-racer 或已安装的 Node.js；原有行情数据仍可使用")
    script = SINA_KLC_DECODER + "\nconst x=require('fs').readFileSync(0,'utf8');process.stdout.write(JSON.stringify(d(JSON.parse(x))));"
    try:
        completed = subprocess.run(
            [node, "-e", script], input=json.dumps(encoded), text=True,
            encoding="utf-8", capture_output=True, timeout=30, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        decoded = json.loads(completed.stdout)
        if not isinstance(decoded, list):
            raise ValueError("decoder did not return daily records")
        return decoded
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SpiderError(f"Sina daily decode failed: {exc}") from exc


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
            fallback = self._powershell_json(url, referer, timeout=max(1, min(self.timeout, 5))) if powershell_fallback else None
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
    def _powershell_json(url: str, referer: str, timeout: int = 5) -> dict[str, Any] | None:
        """Use Windows' request stack when a local proxy closes urllib connections."""
        if os.name != "nt":
            return None
        escaped = url.replace("'", "''")
        command = (
            "$ProgressPreference='SilentlyContinue'; "
            f"$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec {timeout} "
            f"-Uri '{escaped}' -Headers @{{'User-Agent'='Mozilla/5.0';'Referer'='{referer}'}}; "
            "$r.Content"
        )
        try:
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=timeout + 2, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        if p[2] != identity.code:
            raise SpiderError("腾讯行情返回的证券代码与查询不一致，已拒绝合并")
        if num(p[3]) is None or num(p[3]) <= 0 or not market_timestamp(p[30]):
            raise SpiderError("腾讯行情价格或时间戳无效，已保留原快照")
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
            f"secid={secid}&fltt=2&fields=f43,f47,f48,f57,f58,f86,f116,f117,f168"
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

    def quote_snapshot(self, identity: StockIdentity, include_precision: bool = True) -> dict[str, Any]:
        payload = self.tencent_quote(identity)
        if not include_precision:
            payload["meta"] = {"precision_matched": False, "precision_deferred": True}
            return payload
        try:
            precise = (self.eastmoney_quote_precision(identity).get("data") or {})
            matched = merge_quote_precision(payload.get("data") or {}, precise)
            payload["meta"] = {"precision_matched": matched}
        except SpiderError as exc:
            payload["meta"] = {"precision_matched": False, "precision_error": str(exc)}
        return payload

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
        trade_date = market_date(box.get("date"))
        return {"data": {"trends": lines}, "meta": {"trade_date": trade_date}}

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
                    if not match:
                        raise SpiderError("Tencent detail response format invalid")
                    return page, (match.group(1).split("|") if match.group(1) else [])
                except SpiderError:
                    continue
            return page, None

        page_rows: dict[int, list[str]] = {}
        failed_pages: set[int] = set()
        tail_page: int | None = None
        consecutive_failed_batches = 0
        network_interrupted = False
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
            if not any(items is not None for items in batch_results.values()):
                consecutive_failed_batches += 1
                if not page_rows:
                    raise SpiderError("腾讯成交明细连续重试失败，已停止本次请求；不会把网络中断当作空尾页")
                if consecutive_failed_batches >= 2:
                    network_interrupted = True
            else:
                consecutive_failed_batches = 0
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
            if network_interrupted:
                break

        # 尾页之前的网络失败页不能被当作“没有更多数据”；单独重试，避免
        # 少一页仍被标记为完整。
        required_end = tail_page if tail_page is not None else max(page_rows, default=-1)
        for page in sorted(page for page in failed_pages if page <= required_end and not network_interrupted):
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
        coverage_complete = tail_page is not None and not missing and not network_interrupted
        return {"data": {"details": lines}, "meta": {"complete": False, "coverage_complete": coverage_complete, "count": len(lines), "pages": len(page_rows), "format": "tencent", "source_note": "腾讯3秒汇总备用（不是原始逐笔）", "index_max": max(indexed), "missing_indices": missing[:20], "missing_count": len(missing), "network_interrupted": network_interrupted, "page_limit_reached": tail_page is None and not network_interrupted}}

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

    def tencent_daily(self, identity: StockIdentity, include_raw: bool = True) -> dict[str, Any]:
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
        if not include_raw:
            return {"data": {"klines": lines}, "meta": {"adjustment": "qfq"}}
        raw_by_date: dict[str, dict[str, Any]] = {}
        try:
            raw_by_date = self.tencent_raw_daily(identity)
        except SpiderError:
            # qfq series remains usable; the exported field status makes the
            # missing raw-price companion explicit instead of silently guessing.
            raw_by_date = {}
        return {"data": {"klines": lines}, "meta": {"raw_by_date": raw_by_date, "adjustment": "qfq"}}

    def tencent_raw_daily(self, identity: StockIdentity) -> dict[str, dict[str, Any]]:
        symbol = identity.market + identity.code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={symbol},day,,,640"
        box = ((self.json(url, "https://gu.qq.com/").get("data") or {}).get(symbol) or {})
        raw_by_date = {}
        for row in box.get("day") or []:
            if isinstance(row, list) and len(row) >= 6:
                raw_by_date[str(row[0])] = {
                    "不复权开盘": num(row[1]), "不复权收盘": num(row[2]),
                    "不复权最高": num(row[3]), "不复权最低": num(row[4]),
                }
        if not raw_by_date:
            raise SpiderError("Tencent raw daily kline empty")
        return raw_by_date

    def sina_daily_exact(self, identity: StockIdentity) -> dict[str, dict[str, Any]]:
        """Return exact daily amount and historical float-share turnover from Sina."""
        symbol = (identity.market if identity.market in {"sh", "sz"} else "sz") + identity.code
        kline_url = f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js"
        payload = self.text(kline_url, "https://finance.sina.com.cn/")
        match = re.search(r'="([^"]+)"', payload)
        if not match:
            raise SpiderError("Sina exact daily kline empty")
        decoded = decode_sina_daily(match.group(1))

        share_url = (
            "https://stock.finance.sina.com.cn/stock/api/jsonp.php/"
            f"var%20KKE_ShareAmount_{symbol}=/StockService.getAmountBySymbol?_=20&symbol={symbol}"
        )
        share_error = ""
        try:
            share_text = self.text(share_url, "https://finance.sina.com.cn/")
            share_match = re.search(r"\((\[.*?\])\)", share_text, re.S)
            share_changes = json.loads(share_match.group(1)) if share_match else []
            if not isinstance(share_changes, list):
                raise ValueError("Sina share changes are not a list")
            if not share_changes:
                share_error = "历史流通股本接口未提供有效记录"
        except (SpiderError, ValueError) as exc:
            share_changes = []
            share_error = str(exc)
        shares_by_date: list[tuple[str, float]] = []
        for row in share_changes:
            if not isinstance(row, dict):
                continue
            value = num(row.get("amount"))
            if row.get("date") and value is not None and value > 0:
                shares_by_date.append((str(row["date"])[:10], round(value * 10000)))
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
                "新浪成交量(股)": volume_shares,
                "历史流通股本(股)": current_shares,
                "精确历史换手率(%)": volume_shares / current_shares * 100 if volume_shares is not None and current_shares else None,
                "盘后成交量(股)": num(row.get("postVol")),
                "盘后成交额": num(row.get("postAmt")),
                "精确历史字段来源": "Sina KLC original daily + historical outstanding-share changes",
                "历史股本获取说明": share_error,
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
            + "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f61",
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
        # Independent primary feeds can wait on network failures concurrently.
        # Keep fallbacks and all result mutation in the assembling thread.
        actions = {
            "quote": self.tencent_quote, "intraday": self.tencent_intraday,
            "details": self.eastmoney_details, "daily": self.eastmoney_kline,
            "precision": self.eastmoney_quote_precision,
            "benchmark": self.benchmark_daily, "billboard": self.eastmoney_billboard,
            "exact": self.sina_daily_exact,
        }
        with ThreadPoolExecutor(max_workers=6) as pool:
            pending = {key: pool.submit(action, identity) for key, action in actions.items()}
            return self._assemble_fetch(identity, pending)

    def _assemble_fetch(self, identity: StockIdentity, pending: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"identity": asdict(identity), "fetched_at": datetime.now().isoformat(timespec="seconds"), "errors": {}, "sources": {}}
        for key in ("quote", "intraday"):
            try:
                result[key] = pending[key].result()
                result["sources"][key] = "Tencent"
            except SpiderError as exc:
                result[key] = {}
                result["errors"][key] = str(exc)
        if (result.get("quote") or {}).get("data"):
            try:
                precise = (pending["precision"].result().get("data") or {})
                if merge_quote_precision(result["quote"]["data"], precise):
                    result["sources"]["quote"] = "Tencent + Eastmoney matched-date/volume precision"
                else:
                    result["sources"]["quote_precision_note"] = "东方财富快照未通过日期/价格/成交量对齐，保留腾讯原始快照"
            except SpiderError as exc:
                result["errors"]["eastmoney_market_cap"] = str(exc)
        try:
            result["details"] = pending["details"].result()
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
            result["daily"] = pending["daily"].result()
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
                raw_map = self.tencent_raw_daily(identity)
                result["daily"].setdefault("meta", {})["raw_by_date"] = raw_map
                if raw_map:
                    result["sources"]["raw_daily"] = "Tencent unadjusted daily companion"
            except SpiderError as exc:
                result["errors"]["raw_daily"] = str(exc)
        if (result.get("daily") or {}).get("data"):
            try:
                exact_daily = pending["exact"].result()
                result["daily"].setdefault("meta", {})["exact_by_date"] = exact_daily
                result["sources"]["daily_amount_turnover"] = "Sina exact daily amount + historical outstanding shares"
            except SpiderError as exc:
                result["errors"]["exact_daily_amount_turnover"] = str(exc)
        try:
            benchmark_name, benchmark_payload = pending["benchmark"].result()
            result["benchmark_name"] = benchmark_name
            result["benchmark_daily"] = benchmark_payload
            result["sources"]["benchmark"] = "Tencent forward-adjusted index daily"
        except SpiderError as exc:
            result["benchmark_daily"] = {}
            result["errors"]["benchmark"] = str(exc)
        try:
            result["billboard"] = pending["billboard"].result()
            result["sources"]["billboard"] = "Eastmoney"
        except SpiderError as exc:
            result["billboard"] = {}
            result["errors"]["eastmoney_billboard"] = str(exc)
        result.setdefault("details", {})
        result.setdefault("billboard", {})
        if not any(result.get(k, {}).get("data") or result.get(k, {}).get("result") for k in ("quote", "intraday", "daily", "details", "billboard")):
            raise SpiderError("\u6240\u6709\u6570\u636e\u63a5\u53e3\u5747\u5931\u8d25")
        result["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        normalized = normalize(result)
        quote_date = market_date((normalized.get("quote") or {}).get("行情时间"))
        daily_source_date = max((str(row.get("日期") or "") for row in normalized["daily"]), default="")
        history_aligned = bool(quote_date and daily_source_date == quote_date)
        normalized["history_sync"] = {"quote_date": quote_date, "daily_source_date": daily_source_date,
                                      "status": "aligned" if history_aligned else "pending" if quote_date else "unavailable",
                                      "note": "只有主日线源已经包含行情交易日，才使用实时价格同步该日；不把新不复权价格追加到未重新复权的旧历史。"}
        if quote_date and not history_aligned:
            normalized.setdefault("errors", {})["daily_adjustment_sync"] = f"行情日期 {quote_date}，主日线源截止 {daily_source_date or '未获取'}；保留原历史，等待复权序列同步。"
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
            ("东方财富主日线", lambda: self.eastmoney_kline(identity)),
            ("腾讯备用日线", lambda: self.tencent_daily(identity)),
            ("新浪历史成交额/股本", lambda: self.sina_daily_exact(identity)),
            ("\u4e1c\u8d22\u9010\u7b14\u660e\u7ec6", lambda: self.eastmoney_details(identity)),
            ("\u4e1c\u8d22\u9f99\u864e\u699c", lambda: self.eastmoney_billboard(identity)),
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
        return benchmark.name, self.tencent_daily(benchmark, include_raw=False)


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


def detail_trading_phase(value: Any, detail_format: str = "") -> tuple[str, str, int]:
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
    if detail_format == "tencent" and 150000 < tick <= 150003:
        # The three-second feed can publish the closing auction at 15:00:01.
        # This is a provider timestamp tolerance, not an extension of market hours.
        return "收盘集合竞价（腾讯汇总时间）", "成交记录", 1
    if 150000 < tick < 150500:
        return "收盘后阶段待确认", "成交记录", 0
    if 150500 <= tick <= 153000:
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
    if 150000 < tick < 150500:
        return "收盘后阶段待确认", 0
    if 150500 <= tick <= 153000:
        return "盘后定价交易", 0
    return "非交易时段", 0


def sync_current_daily(result: dict[str, Any]) -> None:
    """Make today's daily row agree with the real-time quote snapshot."""
    if result.get("history_sync") and result["history_sync"].get("status") != "aligned":
        return
    quote = result.get("quote") or {}
    stamp = market_timestamp(quote.get("行情时间"))
    if len(stamp) < 8 or num(quote.get("最新价")) is None or num(quote.get("最新价")) <= 0:
        return
    trade_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    existing_history = result.get("daily_history") or result.get("daily") or []
    latest_date = max((str(item.get("日期") or "") for item in existing_history), default="")
    if trade_date < latest_date:
        return
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
    row = {key: value for key, value in row.items() if value is not None}
    if num(quote.get("换手率(%)")) is not None:
        row["历史行情换手率(%)"] = num(quote["换手率(%)"])
        row["可用历史换手率(%)"] = num(quote["换手率(%)"])
        row["可用历史换手率口径"] = "当前行情商显示值（可能四舍五入）"
    if "成交额" not in row:
        row.pop("成交额口径", None)
    stamp_time = int(stamp[8:14] or 0) if len(stamp) >= 14 else 0
    row["数据状态"] = "盘中" if stamp_time < 150000 else "收盘"
    targets = [result.setdefault("daily", [])]
    history = result.get("daily_history")
    if isinstance(history, list) and history is not targets[0]:
        targets.append(history)
    for daily in targets:
        for current in daily:
            if current.get("日期") == trade_date:
                historical_shares = num(current.get("历史流通股本(股)"))
                if historical_shares and row.get("数据状态") == "盘中" and "成交量" in row and num(current.get("成交量")) != row["成交量"]:
                    current["精确历史换手率(%)"] = row["成交量"] * 100 / historical_shares * 100
                    current["精确历史换手率口径"] = "兼容字段名；当前主交易成交量÷历史流通股本计算值"
                current.update(row)
                break
        else:
            daily.append(dict(row))
            daily.sort(key=lambda item: str(item.get("日期") or ""))
    if isinstance(history, list) and history is not targets[0]:
        result["daily"] = history[-60:]


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
    gains: list[float | None] = [None] if closes else []
    losses: list[float | None] = [None] if closes else []
    for i in range(1, len(closes)):
        change = (closes[i] - closes[i - 1]) if closes[i] is not None and closes[i - 1] is not None else None
        gains.append(max(change, 0) if change is not None else None)
        losses.append(max(-change, 0) if change is not None else None)
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
        row["行情显示换手率(%)"] = num((quote or {}).get("换手率(%)")) if market_date((quote or {}).get("行情时间")) == row.get("日期") else None
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
    dates = [market_date(row.get("日期")) for row in daily]
    sequence_ok = bool(dates) and all(dates) and all(a < b for a, b in zip(dates, dates[1:]))

    def period_return(days: int) -> float | None:
        if not sequence_ok or len(closes) <= days or any(value is None or value <= 0 for value in closes[-days - 1:]):
            return None
        return (closes[-1] / closes[-days - 1] - 1) * 100

    benchmark_rows = result.get("benchmark_indicators") or enrich_daily_indicators(result.get("benchmark_history") or [])
    benchmark_by_date = {row.get("日期"): num(row.get("收盘")) for row in benchmark_rows}
    aligned = [(num(row.get("收盘")), benchmark_by_date.get(row.get("日期"))) for row in daily]
    relative: dict[str, float | None] = {}
    for days in (20, 60, 120, 250):
        if sequence_ok and len(aligned) > days and all(value is not None and value > 0 for pair in aligned[-days - 1:] for value in pair):
            stock_return = (aligned[-1][0] / aligned[-days - 1][0] - 1) * 100
            benchmark_return = (aligned[-1][1] / aligned[-days - 1][1] - 1) * 100
            relative[f"{days}日"] = stock_return - benchmark_return
        else:
            relative[f"{days}日"] = None

    valid_risk_window = sequence_ok and len(closes) >= 21 and all(value is not None and value > 0 for value in closes[-21:])
    recent_returns = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:])] if valid_risk_window else []
    volatility = math.sqrt(sum((value - sum(recent_returns) / len(recent_returns)) ** 2 for value in recent_returns) / len(recent_returns)) * math.sqrt(250) * 100 if len(recent_returns) >= 2 else None
    peak = None; max_drawdown = 0.0
    valid_drawdown = sequence_ok and len(closes) >= 2 and all(value is not None and value > 0 for value in closes[-120:])
    for close in closes[-120:] if valid_drawdown else []:
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
        trend = "价格位于MA20上方" if close > ma20 else "价格位于MA20下方" if close < ma20 else "价格接近MA20"
    else:
        trend = "历史数据不足"
    high20 = max((num(row.get("最高")) for row in daily[-20:] if num(row.get("最高")) is not None), default=None)
    low20 = min((num(row.get("最低")) for row in daily[-20:] if num(row.get("最低")) is not None), default=None)

    intraday = result.get("intraday") or []
    main_intraday = [row for row in intraday if row.get("计入主交易统计") == 1]
    after_intraday = [row for row in intraday if row.get("交易阶段") == "盘后定价交易"]

    def minute_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        complete_volumes = bool(rows) and all(num(row.get("成交量")) is not None and num(row.get("成交量")) >= 0 for row in rows)
        volume = sum(num(row.get("成交量")) for row in rows) if complete_volumes else None
        complete_amounts = bool(rows) and all(num(row.get("成交额")) is not None and num(row.get("成交额")) >= 0 for row in rows)
        turnover = sum(num(row.get("成交额"), 0) or 0 for row in rows) if complete_amounts else None
        return {"记录数": len(rows), "成交量(手)": volume, "成交额": turnover, "VWAP": turnover / (volume * 100) if turnover is not None and volume else None}

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
    main_volume_ready = bool(main_details) and all(num(row.get("成交量")) is not None and num(row.get("成交量")) >= 0 for row in main_details)
    after_volume_ready = bool(after_details) and all(num(row.get("成交量")) is not None and num(row.get("成交量")) >= 0 for row in after_details)
    direction_ready = main_volume_ready and all(row.get("方向") in side_volume for row in main_details)
    main_detail_volume = sum(num(row.get("成交量")) for row in main_details) if main_volume_ready else None
    after_detail_volume = sum(num(row.get("成交量")) for row in after_details) if after_volume_ready else None
    return {
        "趋势判断": trend,
        "阶段收益(%)": {"5日": period_return(5), "10日": period_return(10), "20日": period_return(20), "60日": period_return(60), "120日": period_return(120), "250日": period_return(250)},
        "相对强弱": {"基准": result.get("benchmark_name") or "未获取", "截止日期": latest.get("日期"), "超额收益(百分点)": relative},
        "均线": {"MA5": ma5, "MA10": ma10, "MA20": ma20, "MA60": ma60, "MA120": num(latest.get("MA120")), "MA250": num(latest.get("MA250"))},
        "动量指标": {"DIF": num(latest.get("DIF")), "DEA": num(latest.get("DEA")), "MACD": num(latest.get("MACD")), "RSI6": num(latest.get("RSI6")), "RSI12": num(latest.get("RSI12")), "K": num(latest.get("K")), "D": num(latest.get("D")), "J": num(latest.get("J"))},
        "波动风险": {"ATR14": num(latest.get("ATR14")), "20日年化波动率(%)": volatility, "近120日最大回撤(%)": max_drawdown * 100 if valid_drawdown else None, "20日最高": high20, "20日最低": low20},
        "量价": {"当日量比_MA5": num(latest.get("量比_MA5")), "5日均量(手)": num(latest.get("VOL_MA5")), "10日均量(手)": num(latest.get("VOL_MA10")), "OBV": num(latest.get("OBV")), "当日成交量(手)": volumes[-1] if volumes else None},
        "分时": {"主交易VWAP": intraday_vwap, "最新价相对主交易VWAP(%)": ((current_price / intraday_vwap) - 1) * 100 if current_price and intraday_vwap else None, "主交易": main_minute, "盘后定价": after_minute, "接口全时段": full_minute},
        "主动成交": {"统计口径": "仅开盘集合竞价成交、连续竞价及收盘集合竞价；排除竞价快照和盘后定价；仅描述已取回记录，完整性另见质量检查", "主交易记录数": len(main_details), "主交易成交量(手)": main_detail_volume, "买盘量(手)": side_volume["买盘"] if direction_ready else None, "卖盘量(手)": side_volume["卖盘"] if direction_ready else None, "中性量(手)": side_volume["中性"] if direction_ready else None, "买盘占主动成交(%)": side_volume["买盘"] / active_total * 100 if direction_ready and active_total else None, "净主动量(手)": side_volume["买盘"] - side_volume["卖盘"] if direction_ready else None, "竞价快照记录数": len(auction_snapshots), "盘后定价记录数": len(after_details), "盘后定价成交量(手)": after_detail_volume},
        "数据限制": [f"历史接口成交额覆盖 {sum(num(row.get('成交额')) is not None for row in daily)}/{len(daily)} 行，历史流通股本覆盖 {sum(num(row.get('历史流通股本(股)')) is not None for row in daily)}/{len(daily)} 行；缺失不视为已取得", "历史股本口径换手率是计算值，不保证交易所精度；与行情商显示值分列，差异可能来自股本生效日、交易口径和四舍五入", "理论涨跌停价按当前板块/名称规则推算，IPO无涨跌幅期和历史ST状态需要交易所日历或证券状态库复核", "仅有宽基指数比较，所属行业指数和同业横向排名仍未接入", "逐笔买卖方向按数据源规则分类，不等同于交易所认定的真实主动买卖；备用汇总数据不能冒充交易所原始逐笔", "财务、估值、公告、新闻、资金流和融资融券属于另一类低频/事件数据，当前仍未接入", "盘中日K和技术指标会随实时行情变化，不是最终收盘值"],
    }


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    identity = raw["identity"]
    q = (raw.get("quote", {}).get("data") or {})
    if q.get("f58"):
        identity["name"] = q["f58"]
    quote = {"\u4ee3\u7801": q.get("f57", identity["code"]), "\u540d\u79f0": q.get("f58", identity.get("name", "")), "\u6700\u65b0\u4ef7": q.get("f43"), "昨收": q.get("f60"), "\u6da8\u8dcc\u5e45(%)": q.get("f169"), "\u6210\u4ea4\u989d": q.get("f48"), "\u6210\u4ea4\u91cf(\u624b)": q.get("f47"), "\u6362\u624b\u7387(%)": q.get("f168"), "\u603b\u5e02\u503c": q.get("f116"), "\u6d41\u901a\u5e02\u503c": q.get("f117"), "\u4eca\u5f00": q.get("f46"), "\u6700\u9ad8": q.get("f44"), "\u6700\u4f4e": q.get("f45"), "行情时间": q.get("f86")}
    for key in quote:
        if key not in {"代码", "名称", "行情时间"}:
            quote[key] = num(quote[key])
    quote["行情时间"] = market_timestamp(quote.get("行情时间")) or None
    quote_date = market_date(quote.get("行情时间"))
    quote["涨跌额"] = (num(quote.get("最新价")) - num(quote.get("昨收"))) if num(quote.get("最新价")) is not None and num(quote.get("昨收")) is not None else None
    intraday = []
    for row in split_rows((raw.get("intraday", {}).get("data") or {}).get("trends", []), ["\u65f6\u95f4", "\u4ef7\u683c", "\u6da8\u8dcc", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u989d"]):
        for key in list(row)[1:]:
            row[key] = num(row[key])
        phase, main_session = intraday_trading_phase(row.get("时间"))
        row["交易阶段"] = phase
        row["计入主交易统计"] = main_session
        row["交易日期"] = (raw.get("intraday", {}).get("meta") or {}).get("trade_date") or ""
        intraday.append(row)
    daily = []
    raw_by_date = (raw.get("daily", {}).get("meta") or {}).get("raw_by_date") or {}
    exact_by_date = (raw.get("daily", {}).get("meta") or {}).get("exact_by_date") or {}
    daily_adjustment = (raw.get("daily", {}).get("meta") or {}).get("adjustment") or "qfq"
    daily_lines = []
    for line in (raw.get("daily", {}).get("data") or {}).get("klines", []):
        if isinstance(line, str) and len(line.split(",")) == 7:
            line += ","
        daily_lines.append(line)
    for row in split_rows(daily_lines, ["\u65e5\u671f", "\u5f00\u76d8", "\u6536\u76d8", "\u6700\u9ad8", "\u6700\u4f4e", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u989d", "历史行情换手率(%)"]):
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
        row["新浪成交额"] = exact_amount
        row["成交额跨源差值"] = num(row.get("成交额")) - exact_amount if num(row.get("成交额")) is not None and exact_amount is not None else None
        if exact_amount is not None and num(row.get("成交额")) is None:
            row["成交额"] = exact_amount
            row["成交额口径"] = "新浪原始日线精确值（元）"
        row["历史流通股本(股)"] = num(exact.get("历史流通股本(股)"))
        row["历史股本口径成交量(股)"] = num(exact.get("新浪成交量(股)"))
        row["精确历史换手率(%)"] = num(exact.get("精确历史换手率(%)"))
        row["盘后成交量(股)"] = num(exact.get("盘后成交量(股)"))
        row["盘后成交额"] = num(exact.get("盘后成交额"))
        row["精确历史字段来源"] = exact.get("精确历史字段来源") or ""
        row["历史股本获取说明"] = exact.get("历史股本获取说明") or ""
        if exact:
            row["精确历史换手率口径"] = "兼容字段名；历史股本口径计算值，并非交易所验证值；与行情商展示口径分列"
        row["可用历史换手率(%)"] = num(row.get("历史行情换手率(%)"), num(row.get("精确历史换手率(%)")))
        row["可用历史换手率口径"] = "东方财富日线显示值（可能四舍五入）" if num(row.get("历史行情换手率(%)")) is not None else "新浪历史流通股本口径计算值" if num(row.get("精确历史换手率(%)")) is not None else "未获取"
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
            phase, nature, main_session = detail_trading_phase(p[0], detail_format)
            price, volume = num(p[1]), num(p[2])
            details.append({"序号": sequence, "\u65f6\u95f4": p[0], "\u4ef7\u683c": price, "\u6210\u4ea4\u91cf": volume, "\u65b9\u5411": direction_name(p[4]), "\u91d1\u989d": None, "估算金额": price * volume * 100 if price is not None and volume is not None and nature == "成交记录" else None, "金额口径": "价格×成交量×100估算；非接口精确金额" if nature == "成交记录" else "非成交记录", "成交笔数/标记": num(p[3]), "交易阶段": phase, "记录性质": nature, "计入主交易统计": main_session})
        elif len(p) >= 5:
            if detail_format == "eastmoney":
                # Eastmoney webguest: time, price, volume, trades/flag, direction.
                volume = num(p[2])
                price = num(p[1]); phase, nature, main_session = detail_trading_phase(p[0], detail_format)
                details.append({"序号": sequence, "\u65f6\u95f4": p[0], "\u4ef7\u683c": price, "\u6210\u4ea4\u91cf": volume, "\u65b9\u5411": direction_name(p[4]), "\u91d1\u989d": None, "估算金额": price * volume * 100 if price is not None and volume is not None and nature == "成交记录" else None, "金额口径": "价格×成交量×100估算；非接口精确金额" if nature == "成交记录" else "非成交记录", "成交笔数/标记": num(p[3]), "交易阶段": phase, "记录性质": nature, "计入主交易统计": main_session})
            else:
                # 腾讯备用格式：时间、价格、成交量、方向、成交额。
                price, volume = num(p[1]), num(p[2]); phase, nature, main_session = detail_trading_phase(p[0], detail_format)
                details.append({"序号": sequence, "\u65f6\u95f4": p[0], "\u4ef7\u683c": price, "\u6210\u4ea4\u91cf": volume, "\u65b9\u5411": direction_name(p[3]), "\u91d1\u989d": num(p[4]), "估算金额": price * volume * 100 if price is not None and volume is not None else None, "金额口径": "腾讯3秒汇总接口值", "成交笔数/标记": None, "交易阶段": phase, "记录性质": nature, "计入主交易统计": main_session})
    billboard = []
    for row in ((raw.get("billboard", {}).get("result") or {}).get("data") or []):
        billboard.append({"\u4ea4\u6613\u65e5\u671f": str(row.get("TRADE_DATE", ""))[:10], "\u4e0a\u699c\u539f\u56e0": row.get("EXPLANATION") or row.get("EXPLAIN") or "", "\u6536\u76d8\u4ef7": row.get("CLOSE_PRICE"), "\u6da8\u8dcc\u5e45(%)": row.get("CHANGE_RATE"), "\u6362\u624b\u7387(%)": row.get("TURNOVERRATE"), "\u4e70\u5165\u989d": row.get("BILLBOARD_BUY_AMT"), "\u5356\u51fa\u989d": row.get("BILLBOARD_SELL_AMT"), "\u51c0\u4e70\u5165\u989d": row.get("BILLBOARD_NET_AMT"), "\u540e1\u65e5\u6da8\u8dcc(%)": row.get("D1_CLOSE_ADJCHRATE")})
    detail_meta = dict(raw.get("details", {}).get("meta") or {})
    fetched_at = datetime.fromisoformat(raw["fetched_at"])
    stamp = market_timestamp(quote.get("行情时间"))
    final_cutoff_reached = len(stamp) == 14 and int(stamp[8:]) >= 153000
    interface_complete = bool(details and detail_meta.get("complete") and detail_meta.get("format") == "eastmoney")
    main_volume = sum(num(row.get("成交量"), 0) or 0 for row in details if row.get("计入主交易统计") == 1 and row.get("记录性质") == "成交记录")
    quote_volume = num(quote.get("成交量(手)"))
    difference = main_volume - quote_volume if quote_volume is not None else None
    volume_reconciled = difference is not None and abs(difference) < 0.001 and bool(details)
    minute_date = (raw.get("intraday", {}).get("meta") or {}).get("trade_date") or ""
    date_mismatch = bool(minute_date and quote_date and minute_date != quote_date)
    detail_meta["interface_complete"] = interface_complete
    detail_meta["volume_reconciled"] = volume_reconciled
    detail_meta["main_volume_difference_lots"] = difference
    detail_meta["trade_date"] = quote_date
    detail_meta["date_basis"] = "根据行情日期推定；明细接口未提供逐条日期"
    detail_meta["date_mismatch"] = date_mismatch
    detail_meta["final_complete"] = bool(interface_complete and final_cutoff_reached and volume_reconciled and not date_mismatch)
    if not interface_complete:
        note = "备用汇总数据或接口覆盖不完整；不能标记为原始逐笔全量"
        if detail_meta.get("network_interrupted"):
            note += "；后续分页持续断网，已停止本次请求并保留已取回部分"
        elif detail_meta.get("page_limit_reached"):
            note += "；达到分页保护上限，尚未确认尾页"
    elif date_mismatch:
        note = "分时与行情交易日期不一致，不能确认明细完整性"
    elif not volume_reconciled:
        note = f"接口全量已取回，主交易量与行情相差{difference:g}手；可能存在舍入、时间差或缺失，未验证最终完整性" if difference is not None else "接口全量已取回，但缺少行情成交量用于核验"
    else:
        note = "收盘后接口全量，主交易量核对一致（非交易所原始逐笔保证）" if detail_meta["final_complete"] else "截至采集时刻接口全量，主交易量核对一致；后续仍可能增加"
    detail_meta["completeness_note"] = note
    detail_meta["main_trade_rows"] = sum(row.get("计入主交易统计") == 1 for row in details)
    detail_meta["auction_snapshot_rows"] = sum(row.get("记录性质") == "行情快照" for row in details)
    detail_meta["after_hours_rows"] = sum(row.get("交易阶段") == "盘后定价交易" for row in details)
    for row in details:
        row["交易日期"] = quote_date
        row["交易日期口径"] = detail_meta["date_basis"]
    # 逐笔原始序号只用于完整性核对，不改变展示顺序。
    return {"identity": identity, "fetched_at": raw["fetched_at"], "quote": quote, "intraday": intraday, "details": details, "details_meta": detail_meta, "daily": daily, "benchmark_name": raw.get("benchmark_name", ""), "benchmark_history": benchmark, "billboard": billboard, "errors": raw.get("errors", {}), "sources": raw.get("sources", {})}




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
        volumes = [num(row.get("成交量")) for row in selected]
        cash = [num(row.get("金额")) for row in selected]
        estimates = [num(row.get("估算金额")) for row in selected]
        all_volumes = bool(selected) and all(value is not None and value >= 0 for value in volumes)
        all_cash = bool(selected) and all(value is not None and value >= 0 for value in cash)
        all_estimates = bool(selected) and all(value is not None and value >= 0 for value in estimates)
        rows.append({
            "类别": category, "记录数": len(selected),
            "成交量/快照量(手)": sum(volumes) if all_volumes else None,
            "接口精确金额": sum(cash) if all_cash else None,
            "估算金额": round(sum(estimates), 2) if all_estimates else None,
            "已知成交量/快照量(手)": sum(value for value in volumes if value is not None) if any(value is not None for value in volumes) else None,
            "已知接口金额": sum(value for value in cash if value is not None) if any(value is not None for value in cash) else None,
            "成交量覆盖": f"{sum(value is not None for value in volumes)}/{len(selected)}",
            "接口金额覆盖": f"{sum(value is not None for value in cash)}/{len(selected)}",
            "汇总口径": "整类合计仅在各行字段齐全且非负时提供；已知合计不代表全类合计",
            "统计处理": treatment,
        })
    return rows


def build_data_quality_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    daily = result.get("daily_indicators") or []
    details = result.get("details") or []
    meta = result.get("details_meta") or {}
    raw_count = sum(all(num(row.get(key)) is not None and num(row.get(key)) > 0 for key in ("不复权开盘", "不复权收盘", "不复权最高", "不复权最低")) for row in daily)
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
    source_checks = {row["item"]: row for row in build_quality_checks(result)}
    def reconciliation_status(item: str) -> str:
        status = (source_checks.get(item) or {}).get("status")
        return {"ok": "通过", "warning": "需复核", "error": "异常"}.get(status, "无法核验")
    rows = [
        {"数据项": "前复权日线OHLCV", "状态": "已导出" if daily else "缺失", "覆盖": f"{len(daily)}行", "来源/口径": (result.get("sources") or {}).get("daily", "未标记"), "限制": "当日盘中值会变化"},
        {"数据项": "不复权日线OHLC", "状态": "已导出" if raw_count else "缺失", "覆盖": f"{raw_count}/{len(daily)}行", "来源/口径": (result.get("sources") or {}).get("raw_daily", "Tencent companion or current quote"), "限制": "用于核验复权和涨跌停，不替代前复权技术分析"},
        {"数据项": "前复权因子", "状态": "已计算" if raw_count else "缺失", "覆盖": f"{raw_count}/{len(daily)}行", "来源/口径": "前复权收盘÷不复权收盘", "限制": "价格序列比值，不是公司行动明细表"},
        {"数据项": "历史精确成交额", "状态": "已导出" if daily and exact_daily_amount == len(daily) else "部分/已导出" if exact_daily_amount else "源未提供", "覆盖": f"{exact_daily_amount}/{len(daily)}行", "来源/口径": "东方财富日线优先，新浪原始 amount（元）补缺/当日实时行情", "限制": "接口原始值；跨源差值另列，不互相静默覆盖"},
        {"数据项": "历史精确换手率", "状态": "已导出" if daily and exact_turnover_count == len(daily) else "部分/已导出" if exact_turnover_count else "源未提供", "覆盖": f"{exact_turnover_count}/{len(daily)}行", "来源/口径": "兼容字段名：新浪成交股数÷对应日期历史流通股本×100%", "限制": "属于历史股本口径计算值，与行情商显示值并列；不保证交易所精度"},
        {"数据项": "历史行情换手率", "状态": "已导出" if daily and all(num(row.get("历史行情换手率(%)")) is not None for row in daily) else "部分/已导出" if any(num(row.get("历史行情换手率(%)")) is not None for row in daily) else "源未提供", "覆盖": f"{sum(num(row.get('历史行情换手率(%)')) is not None for row in daily)}/{len(daily)}行", "来源/口径": "东方财富日线 f61", "限制": "行情商显示精度，与历史股本计算值并列"},
        {"数据项": "最新换手率口径对照", "状态": "已导出" if latest_exact_turnover is not None and latest_display_turnover is not None else "口径缺失", "覆盖": f"历史股本口径{latest_exact_turnover:.4f}% / 行情显示{latest_display_turnover:.4f}%" if latest_exact_turnover is not None and latest_display_turnover is not None else "口径之一缺失", "来源/口径": "历史流通股本计算值 vs 实时行情商显示值", "限制": "两个字段并存，不互相覆盖"},
        {"数据项": "历史流通股本", "状态": "已导出" if daily and exact_share_count == len(daily) else "部分/已导出" if exact_share_count else "源未提供", "覆盖": f"{exact_share_count}/{len(daily)}行", "来源/口径": "新浪历史流通股本变更序列前向匹配", "限制": "单位：股"},
        {"数据项": "历史盘后成交量/额", "状态": "已导出" if after_amount_count else "源未提供", "覆盖": f"{after_amount_count}/{len(daily)}行", "来源/口径": "新浪原始日线 postVol/postAmt", "限制": "非盘后交易股票或无成交日通常为0"},
        {"数据项": "历史估算成交额（备用）", "状态": "已保留" if daily else "缺失", "覆盖": f"{len(daily)}行", "来源/口径": "不复权典型价×成交量×100", "限制": "仅用于精确源失败时的降级，不替代精确成交额"},
        {"数据项": "按当前流通股本估算换手率（备用）", "状态": "已保留" if daily else "缺失", "覆盖": f"{len(daily)}行", "来源/口径": "成交股数÷当前流通股本", "限制": "仅用于精确历史流通股本源失败时的降级"},
        {"数据项": "理论涨跌停/触板标记", "状态": "已导出" if limit_count else "缺失", "覆盖": f"{limit_count}/{len(daily)}行", "来源/口径": "不复权昨收×当前板块规则", "限制": "首行无昨收；IPO无涨跌幅期和历史ST状态需复核"},
        {"数据项": "逐笔主交易量", "状态": "已导出" if details else "缺失", "覆盖": f"{meta.get('main_trade_rows', 0)}条", "来源/口径": "排除竞价快照和盘后定价", "限制": meta.get("completeness_note", "")},
        {"数据项": "逐笔主交易量与行情对账", "状态": reconciliation_status("明细主交易量对账"), "覆盖": f"已知逐笔{main_detail_volume:.0f}手 / 行情{quote_volume:.0f}手" if quote_volume is not None else f"已知逐笔{main_detail_volume:.0f}手 / 行情缺失", "来源/口径": "主交易成交记录 vs 行情成交量", "限制": (source_checks.get("明细主交易量对账") or {}).get("detail", "缺少可核验数据")},
        {"数据项": "分钟主交易量与行情对账", "状态": reconciliation_status("分时主交易量对账"), "覆盖": f"已知分钟{main_minute_volume:.0f}手 / 行情{quote_volume:.0f}手" if quote_volume is not None else f"已知分钟{main_minute_volume:.0f}手 / 行情缺失", "来源/口径": "15:00前分钟增量 vs 行情成交量", "限制": (source_checks.get("分时主交易量对账") or {}).get("detail", "缺少可核验数据")},
        {"数据项": "逐笔精确金额", "状态": "已导出" if exact_detail_amount else "接口未提供", "覆盖": f"{exact_detail_amount}/{len(details)}行", "来源/口径": "原接口字段", "限制": "缺失时另列价格×成交量×100估算金额"},
        {"数据项": "行业指数和同业排名", "状态": "尚未接入", "覆盖": "0", "来源/口径": "—", "限制": "需要证券行业分类和成分历史库"},
        {"数据项": "财务/估值/公告/新闻/资金流/融资融券", "状态": "尚未接入", "覆盖": "0", "来源/口径": "—", "限制": "属于低频和事件数据，需单独数据源及时间对齐"},
    ]
    from research_data import binding
    research = result.get("research")
    if isinstance(research, dict) and (research.get("code"), research.get("as_of")) == binding(result):
        rows = [r for r in rows if r["数据项"] not in {"行业指数和同业排名", "财务/估值/公告/新闻/资金流/融资融券"}]
        for key, source in (research.get("sources") or {}).items():
            rows.append({"数据项": "研究 / " + key, "状态": "已取得" if source.get("rows") else "本次无可用记录",
                         "覆盖": f"{len(source.get('rows') or [])}行", "来源/口径": source.get("source"),
                         "限制": source.get("note", "") + ("；窗口截取，非完整历史" if source.get("truncated") else "")})
        for key in research.get("errors") or {}:
            rows.append({"数据项": "研究 / " + key, "状态": "本次未获取", "覆盖": "缺失不是0", "来源/口径": "—", "限制": "来源失败或结构不符"})
        rows.append({"数据项": "完整新闻/融资融券/历史行业成分/预测回测", "状态": "尚未接入", "覆盖": "0", "来源/口径": "—", "限制": "不能据此声称预测精度"})
    return rows


def export_result(result: dict[str, Any], folder: str, health: list[dict[str, Any]] | None = None) -> list[Path]:
    """Publish a complete unique batch, with JSON last as its completion marker."""
    target = Path(folder)
    stem = f"{result['identity']['code']}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
    daily_rows = result.get("daily_indicators") or enrich_daily_indicators(result.get("daily_history") or result.get("daily") or [], result.get("identity"), result.get("quote"))
    datasets = {
        "daily_indicators": daily_rows,
        "benchmark": result.get("benchmark_indicators") or result.get("benchmark_history") or [],
        "intraday": result.get("intraday") or [],
        "details": result.get("details") or [],
    }
    filenames = {key: f"{stem}_{key}.csv" for key, rows in datasets.items() if rows}
    filenames["analysis"] = f"{stem}_analysis.json"
    export_view = {**result, "daily_indicators": daily_rows}
    payload = {
        "schema_version": 2,
        "identity": result.get("identity"), "fetched_at": result.get("fetched_at"),
        "quote": result.get("quote"), "analysis": result.get("analysis") or build_analysis(result),
        "details_meta": result.get("details_meta"), "trade_summary": build_trade_summary_rows(result),
        "history_sync": result.get("history_sync"),
        "data_quality": build_data_quality_rows(export_view), "sources": result.get("sources"), "errors": result.get("errors"),
        "quality_checks": build_quality_checks(export_view), "dashboard": summarize_dashboard(export_view),
        "dataset_counts": {key: len(rows) for key, rows in datasets.items()},
        "export_files": filenames,
        "missing_datasets": [key for key, rows in datasets.items() if not rows],
        "export_note": "只导出已有数据；缺失数据集不生成空CSV。技术观察不是价格预测或买卖建议。",
    }
    from research_data import binding
    code, as_of = binding(result)
    research = result.get("research")
    if isinstance(research, dict) and research.get("code") == code and research.get("as_of") == as_of:
        from research_analysis import analyze_research
        payload["research"] = research
        payload["research_analysis"] = analyze_research(result, research)
    # Validate all rows before any final file is visible, including non-finite CSV values.
    json.dumps(datasets, ensure_ascii=False, allow_nan=False)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    target.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=".stock-export-", dir=target) as staging:
        stage = Path(staging)
        for key, rows in datasets.items():
            if rows:
                write_csv(stage / filenames[key], rows)
        (stage / filenames["analysis"]).write_text(serialized, encoding="utf-8")
        try:
            for filename in filenames.values():
                destination = target / filename
                # UUID names avoid overwriting existing exports; guard unexpected collisions too.
                if destination.exists():
                    raise FileExistsError(f"导出文件已经存在：{destination.name}")
                os.replace(stage / filename, destination)
                published.append(destination)
        except Exception:
            for destination in published:
                destination.unlink(missing_ok=True)
            raise
    return published


class App(tk.Tk):
    """Compact analysis workspace; acquisition work never touches Tk widgets."""
    def __init__(self):
        super().__init__()
        from workspace_state import load_preferences
        self.preferences = load_preferences()
        self.title("股票走势工作台")
        self.geometry("1040x700")
        self.minsize(760, 520)
        self.result: dict[str, Any] | None = None
        self.live_identity: StockIdentity | None = None
        self.live_enabled = False
        self.live_busy = False
        self.live_job = None
        self.live_generation = 0
        self.live_inflight_generation = None
        self.details_busy = False
        self.details_inflight_generation = None
        self.last_details_refresh = 0.0
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.fetch_generation = 0
        self.pending_query = None
        self.history_resync_pending = False
        self.history_resync_target = ""
        self.history_resync_code = ""
        self.resume_live_after_fetch = False
        self.export_busy = False
        self.closing = False
        self._live_errors: dict[str, str] = {}
        self._detail_attempted = False
        self.health: list[dict[str, Any]] = []
        self.current_intraday: list[dict[str, Any]] = []
        self.build()
        self.poll_job = self.after(150, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def build(self):
        from stock_charts import StockChart
        from ai_panel import AiPanel
        from research_panel import ResearchPanel
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=25, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Dashboard.TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))

        toolbar = ttk.Frame(self, padding=(12, 10, 12, 4))
        toolbar.pack(fill="x")
        first = ttk.Frame(toolbar)
        first.pack(fill="x")
        ttk.Label(first, text="股票走势工作台", style="Title.TLabel").grid(row=0, column=0, padx=(0, 14))
        ttk.Label(first, text="名称 / 代码 / 网址", foreground="#64748b").grid(row=0, column=1, padx=(0, 6))
        recent = self.preferences.get("recent_queries") or []
        self.query = tk.StringVar(value=recent[0] if recent else DEFAULT_QUERY)
        self.query_box = ttk.Combobox(first, textvariable=self.query, width=23)
        self.query_box.grid(row=0, column=2, sticky="ew")
        first.columnconfigure(2, weight=1)
        self.query_box.bind("<Return>", lambda _event: self.start_fetch())
        ttk.Button(first, text="查询 / 刷新", command=self.start_fetch, width=12).grid(row=0, column=3, padx=(8, 0))

        second = ttk.Frame(toolbar)
        second.pack(fill="x", pady=(8, 0))
        ttk.Label(second, text="刷新间隔").pack(side="left")
        self.refresh_seconds = tk.StringVar(value=str(self.preferences.get("refresh_seconds", 15)))
        interval = ttk.Combobox(second, textvariable=self.refresh_seconds, values=("5", "10", "15", "30", "60"), state="readonly", width=4)
        interval.pack(side="left", padx=(6, 2))
        interval.bind("<<ComboboxSelected>>", self.interval_changed)
        ttk.Label(second, text="秒").pack(side="left", padx=(0, 8))
        self.live_button = ttk.Button(second, text="开启实时", command=self.toggle_live, width=11)
        self.live_button.pack(side="left")
        self.favorite_text = tk.StringVar(value="☆ 收藏")
        self.favorite_button = ttk.Button(second, textvariable=self.favorite_text, command=self.toggle_favorite, width=11, state="disabled")
        self.favorite_button.pack(side="left", padx=6)
        self.export = ttk.Button(second, text="导出分析数据", command=self.do_export, state="disabled", width=14)
        self.export.pack(side="left")
        self.choose_export_button = ttk.Button(second, text="另选目录…", command=self.choose_export_folder, state="disabled", width=11)
        self.choose_export_button.pack(side="left", padx=6)

        self.status = tk.StringVar(value="输入股票代码后查询；F5 刷新，Ctrl+E 导出。")
        self.status_label = ttk.Label(self, textvariable=self.status, foreground="#64748b", wraplength=960)
        self.status_label.pack(fill="x", padx=14, pady=(3, 2))
        self.snapshot_info = tk.StringVar(value="行情时间 —   ·   交易日 —   ·   数据来源 —")
        self.snapshot_label = ttk.Label(self, textvariable=self.snapshot_info, foreground="#475569", wraplength=960)
        self.snapshot_label.pack(fill="x", padx=14, pady=(0, 5))

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.overview = ttk.Frame(self.tabs, padding=8)
        self.chart = ttk.Frame(self.tabs, padding=8)
        self.detail = ttk.Frame(self.tabs, padding=8)
        self.daily = ttk.Frame(self.tabs, padding=8)
        self.quality_tab = ttk.Frame(self.tabs, padding=8)
        self.billboard = ttk.Frame(self.tabs, padding=8)
        self.ai_panel = AiPanel(self.tabs, self.preferences, self.persist_preferences)
        self.research_panel = ResearchPanel(self.tabs, self.research_loaded)
        for tab, label in ((self.overview, "走势概览"), (self.chart, "走势图"), (self.detail, "成交明细"),
                           (self.daily, "历史指标"), (self.quality_tab, "数据质量"), (self.billboard, "龙虎榜"), (self.research_panel, "研究分析"), (self.ai_panel, "AI 分析")):
            self.tabs.add(tab, text=label)

        self.metrics: dict[str, tk.StringVar] = {}
        self.metric_labels = {}
        cards = ttk.Frame(self.overview)
        cards.pack(fill="x")
        for i, name in enumerate(("最新价", "涨跌幅(%)", "成交额", "成交量(手)", "换手率(%)", "今开", "最高", "最低")):
            card = ttk.LabelFrame(cards, text=name, padding=(9, 7))
            card.grid(row=i // 4, column=i % 4, sticky="nsew", padx=3, pady=3)
            cards.columnconfigure(i % 4, weight=1, uniform="cards")
            self.metrics[name] = tk.StringVar(value="—")
            label = ttk.Label(card, textvariable=self.metrics[name], font=("Segoe UI", 15, "bold"))
            label.pack(anchor="w")
            self.metric_labels[name] = label
        self.warning = tk.StringVar(value="未查询。成交明细完整性需成交量对账，备用汇总不等于交易所原始逐笔。")
        self.warning_label = ttk.Label(self.overview, textvariable=self.warning, foreground="#a16207", wraplength=900)
        self.warning_label.pack(fill="x", padx=4, pady=(7, 5))
        overview_body = ttk.Frame(self.overview)
        overview_body.pack(fill="both", expand=True, pady=(3, 0))
        self.overview_text = tk.Text(overview_body, wrap="word", bg="#f8fafc", fg="#334155", relief="flat",
                                    font=("Microsoft YaHei UI", 10), padx=12, pady=10, height=8, state="disabled")
        overview_scroll = ttk.Scrollbar(overview_body, orient="vertical", command=self.overview_text.yview)
        self.overview_text.configure(yscrollcommand=overview_scroll.set)
        self.overview_text.pack(side="left", fill="both", expand=True)
        overview_scroll.pack(side="right", fill="y")
        self.overview_text.tag_configure("heading", font=("Microsoft YaHei UI", 11, "bold"), foreground="#0f172a")
        self.overview_text.tag_configure("muted", foreground="#64748b")
        self.set_overview_text([("查询后展示均线、阶段涨跌、量价与波动情况。", "heading"),
                                ("这里描述已有走势，不提供买卖建议；盘中日线及指标尚未定稿。", "muted")])

        controls = ttk.Frame(self.chart)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Label(controls, text="图表").pack(side="left")
        self.chart_mode = tk.StringVar(value=self.preferences.get("chart_mode", "日线"))
        mode = ttk.Combobox(controls, textvariable=self.chart_mode, values=("日线", "分时"), state="readonly", width=6)
        mode.pack(side="left", padx=(6, 14))
        mode.bind("<<ComboboxSelected>>", self.chart_changed)
        ttk.Label(controls, text="日线范围").pack(side="left")
        self.daily_range = tk.StringVar(value=str(self.preferences.get("daily_range", 60)))
        range_box = ttk.Combobox(controls, textvariable=self.daily_range, values=("30", "60", "120", "250"), state="readonly", width=5)
        range_box.pack(side="left", padx=(6, 3))
        range_box.bind("<<ComboboxSelected>>", self.chart_changed)
        ttk.Label(controls, text="日", foreground="#64748b").pack(side="left", padx=5)
        self.chart_indicator = tk.StringVar(value="成交量")
        indicator = ttk.Combobox(controls, textvariable=self.chart_indicator, values=("成交量", "MACD"), state="readonly", width=7)
        indicator.pack(side="left", padx=8)
        indicator.bind("<<ComboboxSelected>>", self.chart_changed)
        self.show_vwap = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="分时均价", variable=self.show_vwap, command=self.chart_changed).pack(side="left", padx=3)
        ttk.Label(controls, text="鼠标查看数据", foreground="#64748b").pack(side="right", padx=3)
        self.stock_chart = StockChart(self.chart)
        self.stock_chart.pack(fill="both", expand=True)
        self.canvas = self.stock_chart.canvas
        self.stock_chart.set_mode(self.chart_mode.get())
        self.stock_chart.set_daily_range(int(self.daily_range.get()))

        filters = ttk.Frame(self.detail)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text="方向").pack(side="left")
        self.direction_filter = tk.StringVar(value="全部")
        directions = ttk.Combobox(filters, textvariable=self.direction_filter, values=("全部", "买盘", "卖盘", "中性"), width=6, state="readonly")
        directions.pack(side="left", padx=(5, 12))
        directions.bind("<<ComboboxSelected>>", self.filter_details)
        ttk.Label(filters, text="阶段").pack(side="left")
        self.phase_filter = tk.StringVar(value="全部")
        phases = ttk.Combobox(filters, textvariable=self.phase_filter,
                             values=("全部", "主交易", "连续竞价", "开盘集合竞价", "收盘集合竞价", "盘后定价交易", "竞价快照"),
                             width=12, state="readonly")
        phases.pack(side="left", padx=(5, 12))
        phases.bind("<<ComboboxSelected>>", self.filter_details)
        ttk.Label(filters, text="最小手数").pack(side="left")
        self.minimum_lots = tk.StringVar(value="0")
        lots = ttk.Combobox(filters, textvariable=self.minimum_lots, values=("0", "10", "50", "100", "500"), width=6)
        lots.pack(side="left", padx=(5, 6))
        lots.bind("<Return>", self.filter_details)
        lots.bind("<<ComboboxSelected>>", self.filter_details)
        ttk.Button(filters, text="筛选", command=self.filter_details, width=6).pack(side="left")
        self.detail_count = tk.StringVar(value="暂无成交明细")
        ttk.Label(self.detail, textvariable=self.detail_count, foreground="#64748b").pack(fill="x", pady=(0, 5))
        self.detail_columns = ["时间", "价格", "成交量(手)", "方向", "交易阶段", "记录性质", "金额/估算金额"]
        self.daily_columns = ["日期", "收盘", "涨跌幅(%)", "成交量(手)", "成交额", "换手率(%)", "MA5", "MA20", "MA60", "MACD", "RSI6"]
        self.trees = {
            "detail": self.tree(self.detail, list(zip(self.detail_columns, (95, 85, 105, 75, 135, 105, 150)))),
            "daily": self.tree(self.daily, list(zip(self.daily_columns, (105, 90, 105, 115, 135, 110, 90, 90, 90, 90, 90)))),
            "quality": self.tree(self.quality_tab, [("检查项", 160), ("状态", 90), ("说明", 820)]),
            "billboard": self.tree(self.billboard, [("交易日期", 105), ("上榜原因", 370), ("收盘价", 90),
                              ("涨跌幅(%)", 105), ("换手率(%)", 105), ("买入额", 130), ("卖出额", 130), ("净买入额", 130)]),
        }
        self.daily_note = tk.StringVar(value="前复权价格用于趋势指标；成交额与换手率保留各自来源口径。横向滚动可查看全部指标。")
        ttk.Label(self.daily, textvariable=self.daily_note, foreground="#64748b", wraplength=920).pack(fill="x", pady=(5, 0))
        self.billboard_note = tk.StringVar(value="龙虎榜为补充事件数据；没有记录不代表接口异常。")
        ttk.Label(self.billboard, textvariable=self.billboard_note, foreground="#64748b").pack(fill="x", pady=(5, 0))
        ttk.Label(self.quality_tab, text="完整性、来源与时点一起查看；“已对账”不等于交易所原始逐笔已获取。",
                  foreground="#64748b", wraplength=920).pack(fill="x", pady=(5, 0))
        self.bind("<F5>", lambda _event: self.start_fetch())
        self.bind("<Control-e>", lambda _event: self.do_export())
        self.bind("<Control-E>", lambda _event: self.do_export())
        self.bind("<Configure>", self.window_resized, add="+")
        self.update_query_choices()

    def window_resized(self, event):
        if event.widget is not self:
            return
        width = max(600, event.width - 44)
        for label in (self.status_label, self.snapshot_label, self.warning_label):
            label.configure(wraplength=width)

    def tree(self, parent, cols):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        names = [item[0] for item in cols]
        tree = ttk.Treeview(frame, columns=names, show="headings")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for name, width in cols:
            tree.heading(name, text=name)
            tree.column(name, width=width, minwidth=70, anchor="center", stretch=False)
        return tree

    def persist_preferences(self):
        from workspace_state import save_preferences
        self.preferences.update(refresh_seconds=int(self.refresh_seconds.get()),
                                chart_mode=self.chart_mode.get(), daily_range=int(self.daily_range.get()))
        return save_preferences(self.preferences)

    def update_query_choices(self):
        values = list(dict.fromkeys([*(self.preferences.get("watchlist") or []), *(self.preferences.get("recent_queries") or [])]))
        self.query_box.configure(values=values)
        code = (self.result or {}).get("identity", {}).get("code")
        self.favorite_text.set("★ 取消收藏" if code and code in (self.preferences.get("watchlist") or []) else "☆ 收藏")

    def remember_query(self):
        code = (self.result or {}).get("identity", {}).get("code")
        if code:
            recent = self.preferences.get("recent_queries") or []
            self.preferences["recent_queries"] = [code, *[item for item in recent if item != code]][:15]
            self.persist_preferences()
            self.update_query_choices()

    def toggle_favorite(self):
        code = (self.result or {}).get("identity", {}).get("code")
        if not code:
            return
        favorites = list(self.preferences.get("watchlist") or [])
        if code in favorites:
            favorites.remove(code)
        else:
            favorites = [code, *favorites][:30]
        self.preferences["watchlist"] = favorites
        self.persist_preferences()
        self.update_query_choices()
        self.status.set("收藏已更新；下拉框可选择收藏和最近查询的股票。")

    def interval_changed(self, _event=None):
        self.persist_preferences()
        if self.live_enabled and self.live_job is not None:
            self.after_cancel(self.live_job)
            self.live_job = self.after(int(self.refresh_seconds.get()) * 1000, self.live_tick)
        self.status.set(f"实时刷新间隔：{self.refresh_seconds.get()} 秒；成交明细至少间隔 30 秒更新。")

    def chart_changed(self, _event=None):
        self.stock_chart.set_mode(self.chart_mode.get())
        self.stock_chart.set_daily_range(int(self.daily_range.get()))
        self.stock_chart.set_indicator("MACD" if self.chart_indicator.get() == "MACD" else "volume")
        self.stock_chart.set_vwap_visible(self.show_vwap.get())
        self.persist_preferences()
        if self.result:
            self.render_daily(self.result)

    def update_action_states(self):
        ready = bool(self.result) and self.pending_query is None and not self.history_resync_pending
        if hasattr(self, "ai_panel"):
            self.ai_panel.update_snapshot(self.result, ready=ready)
        if hasattr(self, "research_panel"):
            self.research_panel.update_snapshot(self.result, ready=ready)
        state = "normal" if ready and not self.export_busy else "disabled"
        self.export.configure(state=state)
        self.choose_export_button.configure(state=state)
        self.live_button.configure(state="normal" if self.pending_query is None and not self.history_resync_pending else "disabled")
        self.favorite_button.configure(state="normal" if self.result and self.pending_query is None else "disabled")

    def start_fetch(self, resume_live=False):
        try:
            identity = parse_query(self.query.get())
        except SpiderError as exc:
            self.status.set(str(exc))
            return
        query_key = (identity.market, identity.code) if identity.code else ("name", identity.name.casefold())
        if self.pending_query == query_key:
            self.status.set("这只股票正在查询，请等待完成；仍可查询其他股票。")
            return
        self.resume_live_after_fetch = resume_live
        if self.history_resync_pending and identity.code and identity.code != self.history_resync_code:
            self.history_resync_pending = False
            self.history_resync_target = ""
            self.history_resync_code = ""
        if self.live_enabled:
            self.toggle_live()
        self.fetch_generation += 1
        self.pending_query = query_key
        self.update_action_states()
        self.status.set("正在采集行情、历史日线和成交明细…已有结果保留，完成后统一更新。")
        threading.Thread(target=self.fetch_worker, args=(identity, self.fetch_generation), daemon=True).start()

    def toggle_live(self):
        if self.live_enabled:
            self.live_enabled = False
            self.live_generation += 1
            if self.live_job is not None:
                self.after_cancel(self.live_job)
                self.live_job = None
            self.live_button.configure(text="开启实时")
            self.status.set("实时刷新已暂停；当前快照仍可分析和导出。")
            return
        if not self.result or not self.live_identity:
            self.status.set("请先查询一只股票，再开启实时刷新。")
            return
        if self.history_resync_pending or self.pending_query is not None:
            self.status.set("等待历史数据重新同步完成后，才能开启实时刷新。")
            return
        self.live_enabled = True
        self.live_generation += 1
        self.last_details_refresh = time.monotonic()
        self.live_button.configure(text="暂停实时")
        self.status.set(f"实时刷新已开启：{self.refresh_seconds.get()} 秒；成交明细至少间隔 30 秒更新。")
        self.live_tick()

    def live_tick(self):
        self.live_job = None
        if not self.live_enabled or not self.live_identity:
            return
        if self.live_busy:
            self.live_job = self.after(250, self.live_tick)
            return
        self.live_busy = True
        self.live_inflight_generation = self.live_generation
        interval = max(30, int(self.refresh_seconds.get()))
        fetch_details = not self.details_busy and time.monotonic() - self.last_details_refresh >= interval
        if fetch_details:
            self.last_details_refresh = time.monotonic()
            self.details_busy = True
            self.details_inflight_generation = self.live_generation
            target_date = market_date((self.result.get("quote") or {}).get("行情时间"))
            threading.Thread(target=self.details_worker, args=(self.live_identity, self.live_generation, target_date), daemon=True).start()
        threading.Thread(target=self.live_worker, args=(self.live_identity, self.live_generation, False), daemon=True).start()

    def live_worker(self, identity, generation, fetch_details=False):
        started = time.perf_counter()
        try:
            client = Client(timeout=8, pause=0.03)
            errors = {}
            values = {"quote": {}, "intraday": {}, "details": {}}
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = {"quote": pool.submit(client.quote_snapshot, identity, include_precision=False),
                        "intraday": pool.submit(client.tencent_intraday, identity)}
                for name, future in jobs.items():
                    try:
                        values[name] = future.result()
                        if name == "intraday" and not ((values[name] or {}).get("data") or {}).get("trends"):
                            errors[name] = "分时接口没有返回有效记录"
                    except Exception as exc:
                        errors[name] = str(exc)
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if not values["quote"]:
                self.events.put(("live_error", (generation, errors.get("quote", "本次未取得有效行情"), elapsed_ms)))
                return
            self.events.put(("live", (generation, values["quote"], values["intraday"], values["details"],
                                     datetime.now().strftime("%H:%M:%S"), elapsed_ms, errors, False)))
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            self.events.put(("live_error", (generation, str(exc), elapsed_ms)))

    def details_worker(self, identity, generation, target_date):
        try:
            details = Client(timeout=8, pause=0.03).deals(identity)
            if not (details.get("data") or {}).get("details"):
                raise SpiderError("明细接口没有返回成交记录")
            self.events.put(("details", (generation, details, target_date)))
        except Exception as exc:
            self.events.put(("details_error", (generation, str(exc), target_date)))

    def fetch_worker(self, identity, generation):
        try:
            self.events.put(("result", (generation, Client().fetch(identity))))
        except Exception as exc:
            self.events.put(("fetch_error", (generation, str(exc))))

    def start_health(self):
        # Compatibility for diagnostic callers; no standalone interface-test UI.
        try:
            identity = parse_query(self.query.get())
        except SpiderError as exc:
            self.status.set(str(exc))
            return
        threading.Thread(target=self.health_worker, args=(identity,), daemon=True).start()

    def health_worker(self, identity):
        try:
            client = Client()
            identity = client.resolve(identity)
            self.events.put(("health", (identity, client.health(identity))))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def poll(self):
        if getattr(self, "closing", False):
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "result":
                generation, result = payload
                if generation != self.fetch_generation:
                    continue
                self.pending_query = None
                self.result = result
                if self.history_resync_code and result.get("identity", {}).get("code") != self.history_resync_code:
                    self.history_resync_target = ""
                    self.history_resync_code = ""
                history_sync = result.get("history_sync") or {}
                quote_date = market_date((result.get("quote") or {}).get("行情时间"))
                if history_sync and history_sync.get("status") != "aligned":
                    self.history_resync_target = max(self.history_resync_target, quote_date)
                    self.history_resync_code = result.get("identity", {}).get("code") or ""
                source_rows = result.get("daily_history") or result.get("daily") or []
                source_date = history_sync.get("daily_source_date") or max((str(row.get("日期") or "") for row in source_rows), default="")
                target_met = not self.history_resync_target or (
                    quote_date >= self.history_resync_target and source_date >= self.history_resync_target
                    and (not history_sync or history_sync.get("status") == "aligned"))
                self.history_resync_pending = not target_met
                if target_met:
                    self.history_resync_target = ""
                    self.history_resync_code = ""
                    result.pop("session_resync", None)
                else:
                    result["session_resync"] = {"pending": True, "target_date": self.history_resync_target}
                identity = result.get("identity", {})
                self.live_identity = StockIdentity(**{key: identity.get(key, "") for key in ("code", "market", "secid", "name", "source")})
                self.render(result)
                self.update_action_states()
                self.remember_query()
                failures = len(result.get("errors") or {})
                self.status.set(f"查询完成：{len(result.get('daily_history') or [])} 日日线，{len(result.get('details') or [])} 条明细"
                                + (f"；{failures} 项采集问题详见数据质量。" if failures else "。"))
                if not target_met:
                    self.status.set("数据源仍返回旧交易日，历史同步尚未完成；请稍后刷新。")
                elif self.resume_live_after_fetch:
                    self.resume_live_after_fetch = False
                    self.toggle_live()
            elif kind == "fetch_error":
                generation, error = payload
                if generation != self.fetch_generation:
                    continue
                self.pending_query = None
                self.update_action_states()
                self.status.set("查询失败：" + error + ("；保留上一次结果。" if self.result else "；可检查网络后重试。"))
            elif kind == "live":
                generation = payload[0]
                if generation == self.live_inflight_generation:
                    self.live_busy = False
                    self.live_inflight_generation = None
                if self.live_enabled and generation == self.live_generation:
                    self._live_errors = payload[6] if len(payload) > 6 else {}
                    self._detail_attempted = payload[7] if len(payload) > 7 else True
                    self.apply_live(payload[1:5])
                    if self.live_enabled and generation == self.live_generation and self.live_job is None:
                        interval = int(self.refresh_seconds.get()) * 1000
                        self.live_job = self.after(max(200, interval - payload[5]), self.live_tick)
            elif kind == "live_error":
                generation, error, elapsed_ms = payload
                if generation == self.live_inflight_generation:
                    self.live_busy = False
                    self.live_inflight_generation = None
                if self.live_enabled and generation == self.live_generation:
                    self.result.setdefault("errors", {})["实时行情"] = error
                    self.render_dashboard(self.result)
                    self.status.set(f"行情刷新失败，{self.refresh_seconds.get()} 秒后重试：{error}")
                    if self.live_job is None:
                        interval = int(self.refresh_seconds.get()) * 1000
                        self.live_job = self.after(max(200, interval - elapsed_ms), self.live_tick)
            elif kind in {"details", "details_error"}:
                generation, value, target_date = payload
                if generation == self.details_inflight_generation:
                    self.details_busy = False
                    self.details_inflight_generation = None
                if not self.live_enabled or generation != self.live_generation:
                    continue
                if kind == "details":
                    self.apply_details(value, target_date)
                else:
                    self.result.setdefault("errors", {})["实时明细"] = value
                    self.result.setdefault("details_meta", {})["final_complete"] = False
                    self.result["details_meta"]["completeness_note"] = "明细刷新失败，保留原交易日的上次记录；当前快照完整性未验证。"
                    self.render_dashboard(self.result)
                    self.status.set("行情继续刷新，成交明细获取失败；详见数据质量。")
            elif kind == "exported":
                self.export_busy = False
                self.update_action_states()
                paths, folder = payload
                self.preferences["last_export_dir"] = str(folder)
                self.persist_preferences()
                self.status.set(f"已导出 {len(paths)} 个文件至 {folder}；当前筛选不会截断导出数据。")
                messagebox.showinfo("导出完成", f"已保存 {len(paths)} 个文件至：\n{folder}\n\n" +
                                    "\n".join(path.name for path in paths) + "\n\n未取得的数据不会补造，缺失项见分析快照。")
            elif kind == "export_error":
                self.export_busy = False
                self.update_action_states()
                self.status.set("导出失败：" + payload)
                messagebox.showerror("导出失败", payload)
            elif kind == "health":
                self.health = payload[1]
                self.status.set("诊断完成；正常采集流程不再单独生成接口测试文件。")
            else:
                self.status.set("发生错误：" + str(payload))
        self.poll_job = self.after(150, self.poll)

    def set_overview_text(self, paragraphs):
        self.overview_text.configure(state="normal")
        self.overview_text.delete("1.0", "end")
        for text, tag in paragraphs:
            self.overview_text.insert("end", text + "\n\n", tag)
        self.overview_text.configure(state="disabled")

    def render(self, result):
        self.current_intraday = result.get("intraday") or []
        self.update_quote_view(result)
        self.filter_details()
        self.render_daily(result)
        columns = ["交易日期", "上榜原因", "收盘价", "涨跌幅(%)", "换手率(%)", "买入额", "卖出额", "净买入额"]
        self.fill(self.trees["billboard"], result.get("billboard") or [], columns)
        self.billboard_note.set(f"取得 {len(result.get('billboard') or [])} 条龙虎榜记录；无记录不等于采集失败。")
        self.render_dashboard(result)
        chart_result = dict(result, daily=[], daily_history=[], daily_indicators=[]) if self.history_resync_pending else result
        self.stock_chart.set_data(chart_result)
        self.ai_panel.update_snapshot(result, ready=self.pending_query is None and not self.history_resync_pending)
        self.research_panel.update_snapshot(result, ready=self.pending_query is None and not self.history_resync_pending)

    def research_loaded(self, research, analysis):
        from research_data import binding
        code, as_of = binding(self.result)
        if self.pending_query is not None or self.history_resync_pending or research.get("code") != code or research.get("as_of") != as_of:
            return
        self.result["research"] = research
        self.result["research_analysis"] = analysis
        self.ai_panel.update_snapshot(self.result, ready=True)
        self.render_dashboard(self.result)

    def update_quote_view(self, result):
        quote = result.get("quote") or {}
        for key, variable in self.metrics.items():
            value = quote.get(key)
            variable.set(amount(value) if key in {"成交额", "总市值", "流通市值"}
                         else fmt(value, 0) if key == "成交量(手)" else fmt(value) + ("%" if "%" in key and num(value) is not None else ""))
        change = num(quote.get("涨跌幅(%)"))
        color = "#dc2626" if change is not None and change > 0 else "#15803d" if change is not None and change < 0 else "#334155"
        for key in ("最新价", "涨跌幅(%)"):
            if key in getattr(self, "metric_labels", {}):
                self.metric_labels[key].configure(foreground=color)
        if hasattr(self, "snapshot_info"):
            identity = result.get("identity") or {}
            stamp = market_timestamp(quote.get("行情时间"))
            display_time = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}" if len(stamp) == 14 else "未获取"
            date = market_date(stamp) or "未获取"
            source = (result.get("sources") or {}).get("quote") or "未标注"
            self.snapshot_info.set(f"{identity.get('name') or '股票'} {identity.get('code') or ''}  ·  行情 {display_time}  ·  交易日 {date}\n来源：{source}")

    def render_dashboard(self, result):
        from analysis_tools import summarize_dashboard
        dashboard = summarize_dashboard(result)
        if self.history_resync_pending:
            dashboard["summary"] = "行情与历史日线尚未同步，等待最新复权日线与基准数据。"
            dashboard["observations"] = ["先核对行情日期与日线截止日期；不把实时未复权价格拼到滞后的复权序列。"]
            dashboard["metrics"] = []
            dashboard.setdefault("quality_checks", []).insert(0, {"item": "新交易日历史同步", "status": "warning", "detail": "等待同一股票的完整历史请求完成；技术指标和导出暂时停用。"})
        paragraphs = [(dashboard.get("summary") or "暂无可分析数据。", "heading")]
        observations = dashboard.get("observations") or []
        if observations:
            paragraphs.append(("\n".join("• " + item for item in observations), ""))
        metrics = dashboard.get("metrics") or []
        if metrics:
            paragraphs.append(("分析指标", "heading"))
            paragraphs.append(("\n".join(f"{row.get('label', '')}：{row.get('value', '—')}" +
                                        (f"  ·  {row['note']}" if row.get("note") else "") for row in metrics), ""))
        paragraphs.append(("指标描述历史与当前快照，不构成买卖建议。分时/成交明细日期以数据源为准；盘中指标会变化。", "muted"))
        self.set_overview_text(paragraphs)
        warnings = dashboard.get("warnings") or []
        note = (result.get("details_meta") or {}).get("completeness_note") or ""
        self.warning.set("；".join(dict.fromkeys([note, *warnings][:4])).strip("；") or "已完成数据质量检查，详见“数据质量”。")
        labels = {"ok": "通过", "warning": "待注意", "error": "异常", "unavailable": "未获取"}
        rows = [{"检查项": row.get("item"), "状态": labels.get(row.get("status"), row.get("status")),
                 "说明": row.get("detail")} for row in dashboard.get("quality_checks") or []]
        self.fill(self.trees["quality"], rows, ["检查项", "状态", "说明"])

    def filter_details(self, _event=None):
        if not self.result:
            return
        minimum = num(self.minimum_lots.get())
        if minimum is None or minimum < 0:
            self.detail_count.set("最小手数请输入大于或等于 0 的数字；导出仍包含全部明细。")
            return
        direction = self.direction_filter.get()
        phase = self.phase_filter.get()
        rows = []
        original = self.result.get("details") or []
        for row in original:
            if direction != "全部" and row.get("方向") != direction:
                continue
            if phase == "主交易" and not row.get("计入主交易统计"):
                continue
            if phase == "竞价快照" and row.get("记录性质") != "行情快照":
                continue
            if phase not in {"全部", "主交易", "竞价快照"} and not str(row.get("交易阶段") or "").startswith(phase):
                continue
            if (num(row.get("成交量"), 0) or 0) < minimum:
                continue
            exact = num(row.get("金额"))
            shown = dict(row)
            shown["成交量(手)"] = row.get("成交量")
            shown["金额/估算金额"] = amount(exact) if exact is not None else "估 " + amount(row.get("估算金额")) if row.get("估算金额") is not None else "—"
            rows.append(shown)
        self.fill(self.trees["detail"], rows, self.detail_columns)
        trade_date = (self.result.get("details_meta") or {}).get("trade_date") or market_date((self.result.get("quote") or {}).get("行情时间")) or "未确认"
        self.detail_count.set(f"显示 {len(rows)} / {len(original)} 条  ·  交易日 {trade_date}  ·  手数单位：100 股/手；“估”表示估算金额")

    def render_daily(self, result):
        if self.history_resync_pending:
            self.fill(self.trees["daily"], [], self.daily_columns)
            self.daily_note.set("交易日变化，等待新的复权历史数据；未把新日行情写入旧日复权序列。")
            return
        source = result.get("daily_indicators") or result.get("daily_history") or result.get("daily") or []
        rows = []
        for row in reversed(source[-int(self.daily_range.get()):]):
            display = dict(row)
            display["成交量(手)"] = row.get("成交量")
            display["换手率(%)"] = row.get("可用历史换手率(%)", row.get("历史行情换手率(%)"))
            rows.append(display)
        self.fill(self.trees["daily"], rows, self.daily_columns)
        scopes = list(dict.fromkeys(str(row.get("复权口径") or "未标注") for row in source))
        scope_text = " / ".join(scopes) if scopes else "暂无数据"
        self.daily_note.set(f"显示最近 {len(rows)} 日日线，完整导出 {len(source)} 日。价格口径：{scope_text}；换手率优先行情商显示值、其次历史股本口径值。")

    def apply_live(self, payload):
        quote, intraday, details, updated_at = payload
        if not self.result:
            return
        live_fetched_at = datetime.now().isoformat(timespec="seconds")
        previous_quote = dict(self.result.get("quote") or {})
        temp = normalize({"identity": dict(self.result["identity"]), "fetched_at": live_fetched_at,
                          "quote": quote, "intraday": intraday, "daily": {}, "details": details,
                          "billboard": {}, "errors": {}, "sources": {}})
        previous_stamp = market_timestamp(previous_quote.get("行情时间"))
        new_stamp = market_timestamp(temp["quote"].get("行情时间"))
        expected_code = str(self.result["identity"].get("code") or "")
        new_code = str(temp["quote"].get("代码") or "")
        new_price = num(temp["quote"].get("最新价"))
        if not new_stamp or new_price is None or new_price <= 0 or new_code != expected_code or (previous_stamp and new_stamp < previous_stamp):
            self.status.set("行情代码、价格或时间异常/过期，已保留最新快照。")
            return
        date_changed = market_date(previous_stamp) != market_date(new_stamp)
        if date_changed:
            self.result["intraday"] = []
            self.result["details"] = []
            self.result["details_meta"] = {"final_complete": False, "completeness_note": "交易日已变化，旧日明细已清空，等待完整历史同步及明细更新。"}
            self.last_details_refresh = 0.0
            self.history_resync_pending = True
            self.history_resync_target = market_date(new_stamp)
            self.history_resync_code = expected_code
            self.result["session_resync"] = {"pending": True, "target_date": self.history_resync_target}
            self.current_intraday = []
            self.filter_details()
            self.render_daily(self.result)
            self.render_dashboard(self.result)
            chart_snapshot = dict(self.result, daily=[], daily_history=[], daily_indicators=[])
            self.stock_chart.set_data(chart_snapshot)
            self.query.set(expected_code)
            self.start_fetch(resume_live=self.live_enabled)
            return
        self.result["quote"] = temp["quote"]
        self.result["fetched_at"] = live_fetched_at
        errors = self.result.setdefault("errors", {})
        live_errors = getattr(self, "_live_errors", {})
        for key, label in (("quote", "实时行情"), ("intraday", "实时分时"), ("details", "实时明细")):
            if key in live_errors:
                errors[label] = live_errors[key]
            elif key != "details" or getattr(self, "_detail_attempted", True):
                errors.pop(label, None)
        if temp["intraday"]:
            minute_dates = {row.get("交易日期") for row in temp["intraday"] if row.get("交易日期")}
            if minute_dates and minute_dates != {market_date(new_stamp)}:
                errors["实时分时"] = "分时日期与行情日期不一致，未混合使用。"
                if date_changed:
                    self.result["intraday"] = []
            else:
                self.result["intraday"] = temp["intraday"]
        elif intraday:
            errors["实时分时"] = "本次分时接口返回空记录，保留上次同交易日的分时。"
        if temp["details"]:
            self.result["details"] = temp["details"]
            self.result["details_meta"] = temp["details_meta"]
            self.result["details_meta"]["last_refreshed_at"] = live_fetched_at
            self.result.setdefault("sources", {})["details"] = "Eastmoney f1 full-day detail" if (details.get("meta") or {}).get("format") == "eastmoney" else "Tencent fallback"
        else:
            meta = self.result.setdefault("details_meta", {})
            if getattr(self, "_detail_attempted", True):
                errors["实时明细"] = "本次明细接口未返回有效记录，保留上次同交易日的记录。"
                meta["final_complete"] = False
                meta["completeness_note"] = "明细刷新失败，保留上次明细；并非当前快照全量。"
            elif num(previous_quote.get("成交量(手)")) != num(temp["quote"].get("成交量(手)")):
                meta["final_complete"] = False
                meta["volume_reconciled"] = False
                meta["completeness_note"] = "行情已更新，成交明细等待下次更新；当前两者不是同一时点。"
        precision = (quote.get("meta") or {}).get("precision_matched")
        self.result.setdefault("sources", {})["quote"] = "Tencent + Eastmoney matched precision" if precision else "Tencent realtime"
        sync_current_daily(self.result)
        self.result["daily_indicators"] = enrich_daily_indicators(self.result.get("daily_history") or self.result.get("daily") or [],
                                                                  self.result.get("identity"), self.result.get("quote"))
        self.result["analysis"] = build_analysis(self.result)
        self.current_intraday = self.result.get("intraday") or []
        self.update_quote_view(self.result)
        if temp["details"]:
            self.filter_details()
        self.render_daily(self.result)
        self.render_dashboard(self.result)
        self.stock_chart.set_data(self.result)
        self.ai_panel.update_snapshot(self.result, ready=self.pending_query is None and not self.history_resync_pending)
        self.research_panel.update_snapshot(self.result, ready=self.pending_query is None and not self.history_resync_pending)
        if self.live_enabled:
            self.status.set(f"实时更新：{updated_at}" + ("；部分数据未更新，详见数据质量。" if live_errors else "；行情/分时与明细分频更新。"))

    def apply_details(self, details, target_date):
        if not self.result:
            return
        current_quote = self.result.get("quote") or {}
        current_date = market_date(current_quote.get("行情时间"))
        source_date = (details.get("meta") or {}).get("trade_date")
        if current_date != target_date or self.history_resync_pending or (source_date and source_date != target_date):
            self.last_details_refresh = 0.0
            self.status.set("旧交易日的明细请求已忽略；等待本交易日的明细。")
            return
        quote_fields = {"f57": current_quote.get("代码"), "f58": current_quote.get("名称"),
                        "f43": current_quote.get("最新价"), "f47": current_quote.get("成交量(手)"),
                        "f48": current_quote.get("成交额"), "f86": current_quote.get("行情时间")}
        dates = {row.get("交易日期") for row in self.result.get("intraday") or [] if row.get("交易日期")}
        minute_date = next(iter(dates)) if len(dates) == 1 else ""
        fetched_at = datetime.now().isoformat(timespec="seconds")
        temp = normalize({"identity": dict(self.result["identity"]), "fetched_at": fetched_at,
                          "quote": {"data": quote_fields}, "intraday": {"meta": {"trade_date": minute_date}},
                          "daily": {}, "details": details, "billboard": {}, "errors": {}, "sources": {}})
        if not temp["details"]:
            self.result.setdefault("errors", {})["实时明细"] = "本次明细没有有效成交记录，保留上次记录。"
            self.render_dashboard(self.result)
            return
        self.result["details"] = temp["details"]
        self.result["details_meta"] = temp["details_meta"]
        self.result["details_meta"]["last_refreshed_at"] = fetched_at
        self.result.setdefault("errors", {}).pop("实时明细", None)
        self.result.setdefault("sources", {})["details"] = "Eastmoney f1 full-day detail" if (details.get("meta") or {}).get("format") == "eastmoney" else "Tencent fallback"
        self.result["analysis"] = build_analysis(self.result)
        self.filter_details()
        self.render_dashboard(self.result)
        self.status.set("成交明细已独立更新，并与当前行情成交量重新对账。")
        self.ai_panel.update_snapshot(self.result, ready=self.pending_query is None and not self.history_resync_pending)
        self.research_panel.update_snapshot(self.result, ready=self.pending_query is None and not self.history_resync_pending)

    def fill(self, tree, rows, cols):
        children = list(tree.get_children())
        for index, row in enumerate(rows):
            values = []
            for col in cols:
                value = row.get(col, "")
                values.append(amount(value) if col in {"金额", "成交额", "买入额", "卖出额", "净买入额"}
                              else fmt(value) if isinstance(value, float) else "—" if value is None else value)
            if index < len(children):
                iid = children[index]
                if tuple(map(str, tree.item(iid, "values"))) != tuple(map(str, values)):
                    tree.item(iid, values=values)
            else:
                tree.insert("", "end", values=values)
        if len(children) > len(rows):
            tree.delete(*children[len(rows):])

    def draw(self, rows):
        # Kept for old callers and small, headless Canvas regression checks.
        if hasattr(self, "stock_chart"):
            self.stock_chart.set_data(self.result or {"intraday": rows})
            return
        self.canvas.delete("all")
        prices = [num(row.get("价格")) for row in rows]
        prices = [value for value in prices if value is not None]
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        if not prices:
            self.canvas.create_text(width / 2, height / 2, text="暂无分时数据", fill="#94a3b8")
            return
        left, right, top, bottom = 52, 18, 30, 32
        plot_width, plot_height = max(1, width - left - right), max(1, height - top - bottom)
        low, high = min(prices), max(prices)
        if high <= low:
            high = low + 1
        points = []
        for index, value in enumerate(prices):
            points += [left + index * plot_width / max(1, len(prices) - 1), top + (high - value) * plot_height / (high - low)]
        if len(points) >= 4:
            self.canvas.create_line(*points, fill="#38bdf8", width=2, smooth=False)
        else:
            x, y = points
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#38bdf8", outline="")
        self.canvas.create_text(left, 12, anchor="w", text="分时价格", fill="white")
        self.canvas.create_text(left, height - 10, anchor="w", text=f"{low:.2f}", fill="#94a3b8")
        self.canvas.create_text(left, top, anchor="w", text=f"{high:.2f}", fill="#94a3b8")

    def choose_export_folder(self):
        from workspace_state import PROJECT_ROOT
        if not self.result or self.export_busy:
            return
        initial = self.preferences.get("last_export_dir") or str(PROJECT_ROOT / "导出数据")
        if not Path(initial).is_dir():
            initial = str(PROJECT_ROOT)
        folder = filedialog.askdirectory(title="选择分析数据导出目录", initialdir=initial)
        if folder:
            self.do_export(folder)

    def do_export(self, folder=None):
        from workspace_state import PROJECT_ROOT
        if not self.result or self.export_busy:
            return
        if self.pending_query is not None or self.history_resync_pending:
            self.status.set("查询或交易日同步尚未完成，请等待一致的分析快照后导出。")
            return
        import copy
        target = Path(folder) if folder else PROJECT_ROOT / "导出数据" / str(self.result["identity"]["code"])
        snapshot = copy.deepcopy(self.result)
        self.export_busy = True
        self.export.configure(state="disabled")
        self.choose_export_button.configure(state="disabled")
        self.status.set(f"正在保存完整分析快照至 {target}…")
        # Publishing must finish cleanly even if the user closes the window.
        threading.Thread(target=self.export_worker, args=(snapshot, target), daemon=False).start()

    def export_worker(self, snapshot, folder):
        try:
            paths = export_result(snapshot, folder)
            self.events.put(("exported", (paths, folder)))
        except Exception as exc:
            self.events.put(("export_error", str(exc)))

    def close(self):
        self.closing = True
        self.ai_panel.close()
        self.research_panel.close()
        self.live_enabled = False
        self.live_generation += 1
        if self.live_job is not None:
            self.after_cancel(self.live_job)
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
        self.persist_preferences()
        self.destroy()


def self_test(query: str) -> int:
    """Read-only live check: do not create exports or repeat unrelated probes."""
    started = time.perf_counter()
    result = Client().fetch(parse_query(query))
    summary = {
        "code": result["identity"]["code"], "elapsed_seconds": round(time.perf_counter() - started, 2),
        "rows": {key: len(result.get(key) or []) for key in ("daily_history", "intraday", "details", "benchmark_history")},
        "quote_time": (result.get("quote") or {}).get("行情时间"),
        "details_meta": result.get("details_meta"), "errors": result.get("errors"),
        "quality_checks": build_quality_checks(result),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    valid_quote = num((result.get("quote") or {}).get("最新价"))
    return 0 if valid_quote is not None and valid_quote > 0 and all(result.get(key) for key in ("daily_history", "intraday", "details")) else 1


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--self-test",metavar="QUERY"); args=parser.parse_args()
    if args.self_test:
        try: return self_test(args.self_test)
        except SpiderError as exc: print("error",exc); return 2
    App().mainloop(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
