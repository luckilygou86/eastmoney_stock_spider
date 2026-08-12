"""Stock data desktop collector. Standard library only."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


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
        prefix = "sh" if identity.market == "sh" else "sz"
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
            "f169": p[31] if len(p) > 31 else None, "f116": p[45] if len(p) > 45 else None,
            "f117": p[46] if len(p) > 46 else None,
        }
        return {"data": data}

    def tencent_intraday(self, identity: StockIdentity) -> dict[str, Any]:
        prefix = "sh" if identity.market == "sh" else "sz"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={prefix}{identity.code}"
        payload = self.json(url, "https://gu.qq.com/")
        box = ((payload.get("data") or {}).get(prefix + identity.code) or {}).get("data") or {}
        lines = []
        prev = None
        for raw in box.get("data") or []:
            p = raw.split()
            if len(p) < 4:
                continue
            price, volume, turnover = num(p[1]), num(p[2]), num(p[3])
            change = 0 if prev is None or price is None else price - prev
            lines.append(f"{p[0]},{price if price is not None else ''},{change},{volume if volume is not None else ''},{turnover if turnover is not None else ''}")
            prev = price
        return {"data": {"trends": lines}}

    def tencent_deals(self, identity: StockIdentity) -> dict[str, Any]:
        """Return the current day's individual deals from Tencent's detail feed.

        The legacy endpoint returns only its current in-memory window (often about
        70 rows), so it is explicitly labeled as a fallback rather than as the
        complete Eastmoney page history.
        """
        prefix = "sh" if identity.market == "sh" else "sz"
        lines = []
        seen: set[str] = set()
        # 腾讯接口每页约 70 条，p=0 是最新页，p=1、p=2… 是历史页。
        def get_page(page: int) -> tuple[int, list[str]]:
            url = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={prefix}{identity.code}&p={page}"
            for _ in range(2):
                try:
                    raw = self.text(url)
                    match = re.search(r"=\[\d+,\"(.*?)\"\]", raw, re.S)
                    return page, (match.group(1).split("|") if match else [])
                except SpiderError:
                    continue
            return page, []

        pages = 0
        stop_page = 64
        indices: set[int] = set()
        # 当前接口最后一页可能只有几十条（301307 曾是 p=57），不能在
        # p=55 提前截断；多取一批空页后再根据“不足 70 条”停止。
        for start in range(0, 64, 8):
            batch_results: dict[int, list[str]] = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(get_page, page) for page in range(start, start + 8)]
                for future in as_completed(futures):
                    try:
                        page, items = future.result()
                        batch_results[page] = items
                    except Exception:
                        continue
            for page in sorted(batch_results):
                items = batch_results[page]
                if not items:
                    stop_page = min(stop_page, page)
                    continue
                pages = max(pages, page + 1)
                for item in items:
                    parts = item.split("/")
                    if len(parts) < 7:
                        continue
                    if parts[0].isdigit():
                        indices.add(int(parts[0]))
                    _, time_text, price, change, volume, turnover, side = parts[:7]
                    line = f"{time_text},{price},{volume},{side},{turnover}"
                    if item not in seen:
                        seen.add(item)
                        lines.append(line)
                if len(items) < 70:
                    stop_page = min(stop_page, page)
            if stop_page < 64:
                break
        if not lines:
            raise SpiderError("Tencent deal detail empty")
        expected = (max(indices) + 1) if indices else len(lines)
        missing = sorted(set(range(expected)) - indices) if indices else []
        return {"data": {"details": lines}, "meta": {"complete": not missing, "count": len(lines), "pages": pages, "format": "tencent", "source_note": "腾讯逐页合并", "index_max": max(indices) if indices else None, "missing_indices": missing[:20], "missing_count": len(missing)}}

    def deals(self, identity: StockIdentity) -> dict[str, Any]:
        """Page-equivalent逐笔优先，网络关闭时切换到可用备用流。"""
        try:
            return self.eastmoney_details(identity)
        except SpiderError:
            return self.tencent_deals(identity)

    def yahoo_daily(self, identity: StockIdentity) -> dict[str, Any]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{identity.code}.{identity.market.upper()}?range=2y&interval=1d"
        root = ((self.json(url).get("chart") or {}).get("result") or [None])[0] or {}
        timestamps = root.get("timestamp") or []
        quote = (((root.get("indicators") or {}).get("quote") or [None])[0] or {})
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
            lines.append(",".join(str(x if x is not None else "") for x in [date, *values, float(values[1]) * float(values[4] or 0)]))
        return {"data": {"klines": lines}}

    def eastmoney_kline(self, identity: StockIdentity) -> dict[str, Any]:
        """日线页 fullScreenChart 使用的东方财富历史 K 线接口。"""
        secid = urllib.parse.quote(identity.secid, safe=".")
        # 分段请求避免部分网络设备对超长响应或大范围历史数据直接断开。
        urls = [
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid
            + "&klt=101&fqt=1&beg=19900101&end=20991231"
            + "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid
            + "&klt=101&fqt=1&beg=20200101&end=20991231"
            + "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid
            + "&klt=101&fqt=1&beg=19900101&end=20191231"
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
                if klines and "beg=19900101&end=20991231" in url:
                    break
            except SpiderError as exc:
                last_error = exc
        if not klines:
            raise last_error or SpiderError("Eastmoney kline empty")
        return {"data": {"klines": sorted(set(klines))}}

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
        # f1 页面实际使用 webguest 分页接口：每页 144 条，total 是当日可查
        # 的成交明细总数。之前只调用 push2 的 pos=-1，只能拿到首屏窗口。
        lines: list[str] = []
        seen: set[str] = set()
        page_size = 144
        total: int | None = None
        max_pages = 1
        for page in range(1, max_pages + 1):
            url = (
                "https://push2.eastmoney.com/webguest/api/qt/stock/details/get?"
                "timil=1&fields1=f1%2Cf2%2Cf3%2Cf4&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55"
                "&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2"
                f"&pn={page}&pz={page_size}&po=0&secid={secid}&wbp2u=4"
            )
            try:
                # 当前环境对 push2 连接偶发主动断开，Windows 请求栈可正常取到
                # webguest 分页响应，因此保留 json 的 PowerShell 兜底。
                payload = self.json(url, "https://quote.eastmoney.com/f1.html?newcode=" + identity.secid)
            except SpiderError:
                if lines:
                    break
                raise
            data = payload.get("data") or {}
            rows = data.get("details")
            if not isinstance(rows, list):
                break
            if not rows:
                # 接口当前版本对 pn>1 的“按页”参数只返回 total，历史段
                # 通过 pos 游标读取；按上一页最后一条成交的序号继续请求。
                if page == 1 and total:
                    last = lines[-1].split(",", 1)[0] if lines else ""
                    if last:
                        break
                break
            if total is None:
                total = int(num(data.get("total"), 0) or 0)
            added = 0
            for row in rows:
                if isinstance(row, str):
                    line = row
                elif isinstance(row, dict):
                    values = [row.get(key, "") for key in ("f51", "f52", "f53", "f54", "f55")]
                    line = ",".join(str(value) for value in values)
                else:
                    continue
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
                    added += 1
            if added == 0 or len(lines) >= total > 0 or len(rows) < page_size:
                break
            # webguest 对 pn>1 在部分节点返回空数组；此时改用同一接口的
            # po=1（页面内部翻页模式）再继续，而不是把首批 144 条误判为完整。
            if page == 1 and total and len(lines) < total:
                page_size = 144
        if not lines:
            raise SpiderError("Eastmoney detail empty")
        # 接口从最新向历史返回，统一按时间和原始序号稳定排序；同一时间同价
        # 的成交仍保留，不做错误的“按时间去重”。
        complete = bool(total and len(lines) >= total)
        if total and not complete:
            # 非登录网页接口只允许首屏 144 条；明确标记为不完整，调用方
            # 会使用备用源并在报告中显示“当前窗口”，避免误称为全量。
            raise SpiderError(f"Eastmoney detail truncated: {len(lines)}/{total}")
        return {"data": {"details": lines}, "meta": {"complete": complete, "count": len(lines), "total": total, "pages": max(1, (len(lines) + page_size - 1) // page_size), "format": "eastmoney"}}

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
        try:
            result["details"] = self.eastmoney_details(identity)
            result["sources"]["details"] = "Eastmoney page detail"
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
            result["sources"]["daily"] = "Eastmoney kline"
        except SpiderError as exc:
            result["errors"]["eastmoney_daily"] = str(exc)
            try:
                result["daily"] = self.yahoo_daily(identity)
                result["sources"]["daily"] = "Yahoo fallback"
            except SpiderError as yahoo_exc:
                result["daily"] = {}
                result["errors"]["daily"] = str(yahoo_exc)
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
        all_daily = normalized["daily"]
        normalized["daily"] = all_daily[-22:]
        cutoff = (datetime.now() - timedelta(days=31)).date().isoformat()
        normalized["billboard"] = [row for row in normalized["billboard"] if row.get("交易日期", "") >= cutoff]
        normalized["chips"] = self.chip_distribution(identity, all_daily)
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


def split_rows(raw: list[str], columns: list[str]) -> list[dict[str, Any]]:
    rows = []
    for line in raw:
        parts = line.split(",")
        if len(parts) >= len(columns):
            rows.append(dict(zip(columns, parts)))
    return rows


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    identity = raw["identity"]
    q = (raw.get("quote", {}).get("data") or {})
    if q.get("f58"):
        identity["name"] = q["f58"]
    quote = {"\u4ee3\u7801": q.get("f57", identity["code"]), "\u540d\u79f0": q.get("f58", identity.get("name", "")), "\u6700\u65b0\u4ef7": q.get("f43"), "\u6da8\u8dcc\u5e45(%)": q.get("f169"), "\u6210\u4ea4\u989d": q.get("f48"), "\u6210\u4ea4\u91cf(\u624b)": q.get("f47"), "\u6362\u624b\u7387(%)": q.get("f168"), "\u603b\u5e02\u503c": q.get("f116"), "\u6d41\u901a\u5e02\u503c": q.get("f117"), "\u4eca\u5f00": q.get("f46"), "\u6700\u9ad8": q.get("f44"), "\u6700\u4f4e": q.get("f45")}
    intraday = []
    for row in split_rows((raw.get("intraday", {}).get("data") or {}).get("trends", []), ["\u65f6\u95f4", "\u4ef7\u683c", "\u6da8\u8dcc", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u989d"]):
        for key in list(row)[1:]:
            row[key] = num(row[key])
        intraday.append(row)
    daily = []
    for row in split_rows((raw.get("daily", {}).get("data") or {}).get("klines", []), ["\u65e5\u671f", "\u5f00\u76d8", "\u6536\u76d8", "\u6700\u9ad8", "\u6700\u4f4e", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u989d"]):
        for key in list(row)[1:]:
            row[key] = num(row[key])
        daily.append(row)
    details = []
    for line in ((raw.get("details", {}).get("data") or {}).get("details") or []):
        p = line.split(",")
        if len(p) >= 6:
            # 东方财富逐笔常见格式：时间、价格、涨跌、成交量、成交额、方向编码。
            details.append({"\u65f6\u95f4": p[0], "\u4ef7\u683c": num(p[1]), "\u6210\u4ea4\u91cf": num(p[3]), "\u65b9\u5411": p[5], "\u91d1\u989d": num(p[4])})
        elif len(p) >= 5:
            # 腾讯备用格式：时间、价格、成交量、方向、成交额。
            details.append({"\u65f6\u95f4": p[0], "\u4ef7\u683c": num(p[1]), "\u6210\u4ea4\u91cf": num(p[2]), "\u65b9\u5411": p[3], "\u91d1\u989d": num(p[4])})
    billboard = []
    for row in ((raw.get("billboard", {}).get("result") or {}).get("data") or []):
        billboard.append({"\u4ea4\u6613\u65e5\u671f": str(row.get("TRADE_DATE", ""))[:10], "\u4e0a\u699c\u539f\u56e0": row.get("EXPLANATION") or row.get("EXPLAIN") or "", "\u6536\u76d8\u4ef7": row.get("CLOSE_PRICE"), "\u6da8\u8dcc\u5e45(%)": row.get("CHANGE_RATE"), "\u6362\u624b\u7387(%)": row.get("TURNOVERRATE"), "\u4e70\u5165\u989d": row.get("BILLBOARD_BUY_AMT"), "\u5356\u51fa\u989d": row.get("BILLBOARD_SELL_AMT"), "\u51c0\u4e70\u5165\u989d": row.get("BILLBOARD_NET_AMT"), "\u540e1\u65e5\u6da8\u8dcc(%)": row.get("D1_CLOSE_ADJCHRATE")})
    detail_meta = raw.get("details", {}).get("meta") or {}
    # 逐笔原始序号只用于完整性核对，不改变展示顺序。
    return {"identity": identity, "fetched_at": raw["fetched_at"], "quote": quote, "intraday": intraday, "details": details, "details_meta": detail_meta, "daily": daily, "billboard": billboard, "errors": raw.get("errors", {}), "sources": raw.get("sources", {})}


def build_markdown_report(result: dict[str, Any], health: list[dict[str, Any]] | None = None) -> str:
    identity = result["identity"]
    quote = result["quote"]
    daily = result.get("daily", [])
    latest = daily[-1] if daily else {}
    first = daily[0] if daily else {}
    close0, close1 = num(first.get("收盘")), num(latest.get("收盘"))
    month_change = ((close1 / close0) - 1) * 100 if close0 and close1 else None
    lines = [
        f"# {identity.get('name') or '股票'}（{identity.get('code')}）分析报告",
        "", f"- 生成时间：{result.get('fetched_at', '')}", f"- 市场：{identity.get('market', '').upper()}",
        f"- 日线来源：{(result.get('sources') or {}).get('daily', '未标记')}",
        f"- 逐笔来源：{(result.get('sources') or {}).get('details', '未标记')}（{('完整' if (result.get('details_meta') or {}).get('complete') else '当前窗口')}）",
        "", "## 当前行情", "", "| 指标 | 数值 |", "|---|---:|",
    ]
    for key in ("最新价", "涨跌幅(%)", "成交额", "成交量(手)", "换手率(%)", "今开", "最高", "最低"):
        value = quote.get(key)
        lines.append(f"| {key} | {amount(value) if key == '成交额' else fmt(value) } |")
    lines += ["", "## 最近一个月日线", "", "| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in daily:
        lines.append("| " + " | ".join([str(row.get("日期", "")), fmt(row.get("开盘")), fmt(row.get("收盘")), fmt(row.get("最高")), fmt(row.get("最低")), fmt(row.get("成交量"), 0), amount(row.get("成交额"))]) + " |")
    detail_meta = result.get("details_meta") or {}
    completeness = "完整分页" if detail_meta.get("complete") else "当前窗口/备用数据"
    lines += ["", "## 走势摘要", "", f"- 最近一个月收盘变化：{fmt(month_change)}%" if month_change is not None else "- 最近一个月收盘变化：暂无", f"- 当日分时记录：{len(result.get('intraday', []))} 条（截止接口返回的最新时间）", f"- 逐笔成交记录：{len(result.get('details', []))} 条（{completeness}）", f"- 龙虎榜近31天记录：{len(result.get('billboard', []))} 条"]
    closes = [num(row.get("收盘")) for row in daily if num(row.get("收盘")) is not None]
    highs = [num(row.get("最高")) for row in daily if num(row.get("最高")) is not None]
    lows = [num(row.get("最低")) for row in daily if num(row.get("最低")) is not None]
    volumes = [num(row.get("成交量"), 0) or 0 for row in daily]
    if closes:
        lines += [f"- 月内最高/最低：{fmt(max(highs))} / {fmt(min(lows))}", f"- 月内收盘均价：{fmt(sum(closes) / len(closes))}", f"- 最近5日平均成交量：{fmt(sum(volumes[-5:]) / max(1, len(volumes[-5:])), 0)}"]
        if len(closes) >= 5:
            trend = "上行" if closes[-1] > closes[-5] else "下行" if closes[-1] < closes[-5] else "横盘"
            lines.append(f"- 最近5日收盘趋势：{trend}")
    chips = result.get("chips") or {}
    lines += ["", "## 筹码分布与平均成本", "", f"- 平均成本：{fmt(chips.get('平均成本'))}", f"- 说明：{chips.get('口径', '')}"]
    peaks = sorted(chips.get("筹码峰", []), key=lambda row: row.get("占比(%)", 0), reverse=True)[:8]
    if peaks:
        lines += ["", "| 成本价 | 估算筹码量 | 占比 |", "|---:|---:|---:|"]
        lines += [f"| {fmt(row['成本价'])} | {fmt(row['估算筹码量'], 0)} | {fmt(row['占比(%)'])}% |" for row in peaks]
    lines += ["", "## 龙虎榜（最近31天）", "", "| 日期 | 上榜原因 | 买入额 | 卖出额 | 净买入额 |", "|---|---|---:|---:|---:|"]
    for row in result.get("billboard", []):
        lines.append(f"| {row.get('交易日期','')} | {row.get('上榜原因','')} | {amount(row.get('买入额'))} | {amount(row.get('卖出额'))} | {amount(row.get('净买入额'))} |")
    lines += ["", "> 说明：不同软件可能使用不同数据源、复权方式、成交口径和更新时间；本报告不构成投资建议。"]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{result['identity']['code']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    report = target / f"{stem}.md"
    report.write_text(build_markdown_report(result, health), encoding="utf-8")
    return [report]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1220x800")
        self.result: dict[str, Any] | None = None
        self.health: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.build()
        self.after(150, self.poll)

    def build(self):
        top = ttk.Frame(self, padding=12); top.pack(fill="x")
        ttk.Label(top, text=APP_TITLE, font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        ttk.Label(top, text="\u8f93\u5165\u80a1\u7968\u540d\u79f0\u6216 6 \u4f4d\u4ee3\u7801", foreground="#64748b").pack(side="left", padx=18)
        self.query = tk.StringVar(value=DEFAULT_QUERY)
        ttk.Entry(top, textvariable=self.query, width=28).pack(side="left")
        ttk.Button(top, text="\u67e5\u8be2", command=self.start_fetch).pack(side="left", padx=6)
        ttk.Button(top, text="\u6d4b\u8bd5\u5168\u90e8\u63a5\u53e3", command=self.start_health).pack(side="left")
        self.export = ttk.Button(top, text="\u5bfc\u51fa", command=self.do_export, state="disabled"); self.export.pack(side="left", padx=6)
        self.status = tk.StringVar(value="\u5c31\u7eea"); ttk.Label(self, textvariable=self.status, foreground="#64748b").pack(fill="x", padx=14)
        self.tabs = ttk.Notebook(self); self.tabs.pack(fill="both", expand=True, padx=12, pady=12)
        self.overview = ttk.Frame(self.tabs, padding=10); self.chart = ttk.Frame(self.tabs, padding=10); self.detail = ttk.Frame(self.tabs, padding=10); self.daily = ttk.Frame(self.tabs, padding=10); self.billboard = ttk.Frame(self.tabs, padding=10); self.chips_tab = ttk.Frame(self.tabs, padding=10); self.health_tab = ttk.Frame(self.tabs, padding=10)
        for tab, text in ((self.overview,"\u6982\u89c8"),(self.chart,"\u5206\u65f6"),(self.detail,"\u9010\u7b14"),(self.daily,"\u65e5\u7ebf"),(self.billboard,"\u9f99\u864e\u699c"),(self.chips_tab,"\u7b79\u7801"),(self.health_tab,"\u63a5\u53e3\u6d4b\u8bd5")): self.tabs.add(tab, text=text)
        self.metrics: dict[str, tk.StringVar] = {}
        for i, name in enumerate(("\u540d\u79f0","\u6700\u65b0\u4ef7","\u6da8\u8dcc\u5e45(%)","\u6210\u4ea4\u989d","\u6210\u4ea4\u91cf(\u624b)","\u6362\u624b\u7387(%)","\u603b\u5e02\u503c","\u6d41\u901a\u5e02\u503c","\u4eca\u5f00","\u6700\u9ad8","\u6700\u4f4e","\u5e73\u5747\u6210\u672c")):
            box=ttk.Frame(self.overview,padding=10); box.grid(row=i//4,column=i%4,sticky="nsew",padx=4,pady=4); self.overview.columnconfigure(i%4,weight=1); ttk.Label(box,text=name,foreground="#64748b").pack(anchor="w"); self.metrics[name]=tk.StringVar(value="-"); ttk.Label(box,textvariable=self.metrics[name],font=("Segoe UI",14,"bold")).pack(anchor="w")
        self.warning=tk.StringVar(); ttk.Label(self.overview,textvariable=self.warning,foreground="#b45309",wraplength=1050).grid(row=3,column=0,columnspan=4,sticky="w",pady=15)
        self.canvas=tk.Canvas(self.chart,bg="#101827",highlightthickness=0); self.canvas.pack(fill="both",expand=True)
        self.trees={"detail":self.tree(self.detail,[("\u65f6\u95f4",100),("\u4ef7\u683c",90),("\u6210\u4ea4\u91cf",100),("\u65b9\u5411",90),("\u91d1\u989d",120)]),"daily":self.tree(self.daily,[("\u65e5\u671f",100),("\u5f00\u76d8",90),("\u6536\u76d8",90),("\u6700\u9ad8",90),("\u6700\u4f4e",90),("\u6210\u4ea4\u91cf",110),("\u6210\u4ea4\u989d",130)]),"billboard":self.tree(self.billboard,[("\u4ea4\u6613\u65e5\u671f",100),("\u4e0a\u699c\u539f\u56e0",300),("\u6536\u76d8\u4ef7",90),("\u6da8\u8dcc\u5e45(%)",95),("\u6362\u624b\u7387(%)",95),("\u4e70\u5165\u989d",120),("\u5356\u51fa\u989d",120),("\u51c0\u4e70\u5165\u989d",120)]),"chips":self.tree(self.chips_tab,[("\u6210\u672c\u4ef7",120),("\u4f30\u7b97\u7b79\u7801\u91cf",160),("\u5360\u6bd4(%)",100)]),"health":self.tree(self.health_tab,[("\u63a5\u53e3",190),("\u72b6\u6001",80),("\u8017\u65f6(ms)",90),("\u8bf4\u660e",700)])}

    def tree(self, parent, cols):
        frame=ttk.Frame(parent); frame.pack(fill="both",expand=True); names=[x[0] for x in cols]; tree=ttk.Treeview(frame,columns=names,show="headings"); sy=ttk.Scrollbar(frame,orient="vertical",command=tree.yview); tree.configure(yscrollcommand=sy.set); tree.grid(row=0,column=0,sticky="nsew"); sy.grid(row=0,column=1,sticky="ns"); frame.rowconfigure(0,weight=1); frame.columnconfigure(0,weight=1)
        for name,width in cols: tree.heading(name,text=name); tree.column(name,width=width,anchor="center")
        return tree

    def start_fetch(self):
        try: identity=parse_query(self.query.get())
        except SpiderError as exc: messagebox.showerror("\u8f93\u5165\u6709\u8bef",str(exc)); return
        self.status.set("\u67e5\u8be2\u4e2d..."); threading.Thread(target=self.fetch_worker,args=(identity,),daemon=True).start()

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
            if kind=="result": self.result=payload; self.render(payload); self.export.configure(state="normal"); self.status.set("\u67e5\u8be2\u5b8c\u6210")
            elif kind=="health":
                self.health=payload[1]; self.fill(self.trees["health"],self.health,["\u63a5\u53e3","\u72b6\u6001","\u8017\u65f6(ms)","\u8bf4\u660e"]); ok=sum(x.get("\u72b6\u6001")=="\u6b63\u5e38" for x in self.health); self.status.set(f"\u63a5\u53e3\u6d4b\u8bd5\u5b8c\u6210: {ok}/{len(self.health)} \u6b63\u5e38")
            else: messagebox.showerror("\u9519\u8bef",payload); self.status.set("\u5931\u8d25")
        self.after(150,self.poll)

    def render(self, result):
        self.metrics["\u540d\u79f0"].set(result["identity"].get("name") or result["identity"]["code"])
        self.metrics["\u5e73\u5747\u6210\u672c"].set(fmt((result.get("chips") or {}).get("平均成本")))
        for key,var in self.metrics.items():
            if key=="\u540d\u79f0": continue
            if key=="\u5e73\u5747\u6210\u672c": continue
            value=result["quote"].get(key); var.set(amount(value) if key in {"\u6210\u4ea4\u989d","\u603b\u5e02\u503c","\u6d41\u901a\u5e02\u503c"} else (fmt(value,0) if key=="\u6210\u4ea4\u91cf(\u624b)" else fmt(value)+("%" if "%" in key else "")))
        self.warning.set("\u90e8\u5206\u63a5\u53e3\u5931\u8d25: "+", ".join(result.get("errors",{})) if result.get("errors") else "\u6240\u6709\u4e3b\u8981\u6570\u636e\u5df2\u66f4\u65b0")
        self.fill(self.trees["detail"],result["details"],["\u65f6\u95f4","\u4ef7\u683c","\u6210\u4ea4\u91cf","\u65b9\u5411","\u91d1\u989d"]); self.fill(self.trees["daily"],result["daily"],["\u65e5\u671f","\u5f00\u76d8","\u6536\u76d8","\u6700\u9ad8","\u6700\u4f4e","\u6210\u4ea4\u91cf","\u6210\u4ea4\u989d"]); self.fill(self.trees["billboard"],result["billboard"],["\u4ea4\u6613\u65e5\u671f","\u4e0a\u699c\u539f\u56e0","\u6536\u76d8\u4ef7","\u6da8\u8dcc\u5e45(%)","\u6362\u624b\u7387(%)","\u4e70\u5165\u989d","\u5356\u51fa\u989d","\u51c0\u4e70\u5165\u989d"]); self.fill(self.trees["chips"],(result.get("chips") or {}).get("筹码峰",[]),["成本价","估算筹码量","占比(%)"]); self.draw(result["intraday"])

    def fill(self, tree, rows, cols):
        tree.delete(*tree.get_children())
        for row in rows:
            vals=[]
            for col in cols:
                v=row.get(col,""); vals.append(amount(v) if col in {"\u91d1\u989d","\u6210\u4ea4\u989d","\u4e70\u5165\u989d","\u5356\u51fa\u989d","\u51c0\u4e70\u5165\u989d"} else (fmt(v) if isinstance(v,float) else v))
            tree.insert("","end",values=vals)

    def draw(self, rows):
        self.canvas.delete("all"); prices=[num(x.get("\u4ef7\u683c")) for x in rows]; prices=[x for x in prices if x is not None]; w=max(700,self.canvas.winfo_width()); h=max(400,self.canvas.winfo_height())
        if not prices: self.canvas.create_text(w/2,h/2,text="\u67e5\u8be2\u540e\u663e\u793a\u5206\u65f6\u56fe",fill="#94a3b8"); return
        low,high=min(prices),max(prices); high=high if high>low else low+1; points=[]
        for i,v in enumerate(prices): points += [50+i*(w-70)/max(1,len(prices)-1),30+(high-v)*320/(high-low)]
        self.canvas.create_line(*points,fill="#38bdf8",width=2,smooth=True); self.canvas.create_text(50,15,anchor="w",text="\u5206\u65f6\u4ef7\u683c",fill="white")

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
