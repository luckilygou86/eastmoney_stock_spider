"""Dependency-free, date-aware charts for the stock desktop application.

The model helpers are independent of Tk, so missing values and chart coordinates
can be checked offline.  A chart never substitutes a price for missing OHLC or
invented cash turnover for an unavailable VWAP.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
import math
import re
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any


def finite_number(value: Any) -> float | None:
    """Accept numeric strings but reject missing, NaN and infinite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def actual_date(value: Any) -> str:
    """Read a supplied calendar date; never use today's date as a substitute."""
    text = str(value or "").strip()
    match = re.search(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", text)
    if not match:
        return ""
    try:
        from datetime import date
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return ""


def time_seconds(value: Any) -> int | None:
    """Parse the provider's HHMM / HHMMSS / HH:MM[:SS] time label."""
    text = str(value or "").strip()
    if " " in text or "T" in text:
        text = re.split(r"[ T]", text)[-1]
    match = re.fullmatch(r"(\d{2}):?(\d{2})(?::?(\d{2}))?", text)
    if not match:
        return None
    hour, minute, second = (int(part or 0) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 3600 + minute * 60 + second


def padded_domain(values: list[float | None], reference: float | None = None,
                  symmetric: bool = False) -> tuple[float, float]:
    valid = [number for value in values if (number := finite_number(value)) is not None]
    reference = finite_number(reference)
    if reference is not None:
        valid.append(reference)
    if not valid:
        return 0.0, 1.0
    low, high = min(valid), max(valid)
    if symmetric and reference is not None:
        distance = max(abs(high - reference), abs(low - reference), abs(reference) * .002, .01)
        return reference - distance * 1.08, reference + distance * 1.08
    pad = max((high - low) * .08, abs(high) * .002, .01)
    return low - pad, high + pad


def map_value(value: float, domain: tuple[float, float], start: float, end: float) -> float:
    low, high = domain
    if high <= low:
        return (start + end) / 2
    return start + (value - low) / (high - low) * (end - start)


def nearest_index(positions: list[float], x: float) -> int | None:
    if not positions:
        return None
    right = bisect_left(positions, x)
    if right == 0:
        return 0
    if right == len(positions):
        return len(positions) - 1
    return right - 1 if x - positions[right - 1] <= positions[right] - x else right


@dataclass
class ChartPoint:
    label: str
    x: float
    row: dict[str, Any]
    price: float | None
    volume: float | None
    vwap: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    ma: dict[str, float | None] = field(default_factory=dict)


@dataclass
class ChartModel:
    mode: str
    points: list[ChartPoint]
    date_label: str = "交易日期未提供"
    source: str = "未记录来源"
    adjustment: str = ""
    reference: float | None = None
    warnings: list[str] = field(default_factory=list)


def build_chart_model(result: dict[str, Any], mode: str = "分时", days: int = 60) -> ChartModel:
    """Build an immutable-to-the-caller view over the existing normalized data."""
    if mode not in {"分时", "日线"}:
        raise ValueError("chart mode must be 分时 or 日线")
    quote = result.get("quote") or {}
    sources = result.get("sources") or {}
    model = ChartModel(mode, [], source=str(sources.get("intraday" if mode == "分时" else "daily") or "未记录来源"))
    if mode == "日线":
        rows = result.get("daily_indicators") or result.get("daily_history") or result.get("daily") or []
        rows = sorted((dict(row) for row in rows), key=lambda row: str(row.get("日期") or ""))[-max(1, int(days)):]
        dates = [str(row.get("日期") or "日期未提供") for row in rows]
        if dates:
            model.date_label = dates[0] if len(rows) == 1 else f"{dates[0]} — {dates[-1]}"
        adjustments = list(dict.fromkeys(str(row.get("复权口径")) for row in rows if row.get("复权口径")))
        model.adjustment = " / ".join(adjustments) if adjustments else "复权口径未提供"
        if len(adjustments) > 1:
            model.warnings.append("存在不同复权口径，跨口径走势需谨慎比较")
        for index, row in enumerate(rows):
            volume = finite_number(row.get("成交量"))
            volume = volume if volume is not None and volume >= 0 else None
            point = ChartPoint(dates[index], float(index), row, finite_number(row.get("收盘")), volume,
                               open=finite_number(row.get("开盘")), high=finite_number(row.get("最高")), low=finite_number(row.get("最低")),
                               ma={name: finite_number(row.get(name)) for name in ("MA5", "MA10", "MA20")})
            model.points.append(point)
        return model

    rows = [dict(row) for row in result.get("intraday") or []]
    dates = [actual_date(row.get("交易日期") or row.get("时间")) for row in rows]
    supplied_dates = sorted(set(date for date in dates if date))
    if supplied_dates:
        date = supplied_dates[-1]
        # Joining curves across days is misleading; undated rows are not presumed
        # to belong to the latest dated session either.
        rows = [row for row, row_date in zip(rows, dates) if row_date == date]
        model.date_label = date
        if len(supplied_dates) > 1 or any(not value for value in dates):
            model.warnings.append("仅显示最新有日期的交易日，未混合其它日期")
    else:
        date = ""
        model.warnings.append("分时接口未提供交易日期；不套用行情昨收")
    quote_date = actual_date(quote.get("交易日期") or quote.get("行情时间"))
    reference = finite_number(quote.get("昨收"))
    if date and quote_date == date and reference is not None and reference > 0:
        model.reference = reference
    elif reference is not None:
        model.warnings.append("昨收与分时日期未核对，不绘制昨收参考线")
    times = [time_seconds(row.get("时间")) for row in rows]
    real_time_axis = bool(rows) and all(value is not None for value in times)
    if real_time_axis:
        rows = [row for _, row in sorted(zip(times, rows), key=lambda pair: pair[0])]
    elif rows:
        model.warnings.append("部分时间格式不明，按接口顺序显示")
    volume_sum = amount_sum = 0.0
    vwap_reliable = True
    for index, row in enumerate(rows):
        volume = finite_number(row.get("成交量"))
        volume = volume if volume is not None and volume >= 0 else None
        amount = finite_number(row.get("成交额"))
        vwap = None
        if row.get("计入主交易统计") == 1:
            if volume is None or amount is None or amount < 0 or (volume == 0 and amount > 0):
                vwap_reliable = False
            elif vwap_reliable:
                volume_sum += volume
                amount_sum += amount
                if volume_sum > 0:
                    vwap = amount_sum / (volume_sum * 100)
        point = ChartPoint(str(row.get("时间") or "时间未提供"), float(time_seconds(row.get("时间")) if real_time_axis else index),
                           row, finite_number(row.get("价格")), volume, vwap=vwap)
        model.points.append(point)
    if rows and not any(point.vwap is not None for point in model.points):
        model.warnings.append("均价线不可用：缺少可核验的主交易成交量 / 额")
    elif not vwap_reliable:
        model.warnings.append("主交易分钟金额缺失，缺口之后不绘制均价线")
    return model


class StockChart(ttk.Frame):
    """Responsive price/volume chart; all methods are called on the Tk thread."""

    BG = "#101b2b"
    GRID = "#253449"
    TEXT = "#a8b9ce"
    PRICE = "#59c8ff"
    UP = "#f87171"
    DOWN = "#37cba4"
    MA_COLORS = {"MA5": "#f8ca62", "MA10": "#a995ef", "MA20": "#59c8ff"}

    def __init__(self, master: Any, **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, background=self.BG, highlightthickness=0, height=330, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.result: dict[str, Any] = {}
        self.mode = "分时"
        self.days = 60
        self.show_vwap = True
        self.indicator = "volume"
        self.model = ChartModel(self.mode, [])
        self._redraw_job: str | None = None
        self._positions: list[float] = []
        self._bounds = (0.0, 0.0, 0.0, 0.0)
        self._price_domain = (0.0, 1.0)
        self._info_font = tkfont.Font(root=self.canvas, family="Microsoft YaHei UI", size=8)
        self.canvas.bind("<Configure>", self._schedule_redraw)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _event: self.canvas.delete("hover"))
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event: Any) -> None:
        if event.widget == self and self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
            self._redraw_job = None

    def set_data(self, result: dict[str, Any]) -> None:
        self.result = result
        self.model = build_chart_model(result, self.mode, self.days)
        self.draw()

    def set_mode(self, mode: str) -> None:
        if mode not in {"分时", "日线"}:
            raise ValueError("chart mode must be 分时 or 日线")
        self.mode = mode
        self.set_data(self.result)

    def set_daily_range(self, days: int) -> None:
        if int(days) not in {30, 60, 120, 250}:
            raise ValueError("daily range must be 30, 60, 120 or 250")
        self.days = int(days)
        self.set_data(self.result)

    def set_vwap_visible(self, visible: bool) -> None:
        self.show_vwap = bool(visible)
        self.draw()

    def set_indicator(self, name: str) -> None:
        if name not in {"volume", "MACD"}:
            raise ValueError("indicator must be volume or MACD")
        self.indicator = name
        self.draw()

    def _schedule_redraw(self, _event: Any = None) -> None:
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(45, self._redraw)

    def _redraw(self) -> None:
        self._redraw_job = None
        self.draw()

    @staticmethod
    def _format(value: float | None, digits: int = 2) -> str:
        return "—" if value is None else f"{value:,.{digits}f}"

    @staticmethod
    def _volume_label(value: float) -> str:
        if abs(value) >= 100000000:
            return f"{value / 100000000:.1f}亿"
        if abs(value) >= 10000:
            return f"{value / 10000:.1f}万"
        return f"{value:.0f}"

    def _fit_text(self, text: str, width: float) -> str:
        if self._info_font.measure(text) <= width:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self._info_font.measure(text[:middle] + "…") <= width:
                low = middle
            else:
                high = middle - 1
        return text[:low] + "…"

    def _draw_legend(self, entries: list[tuple[str, str]], right: float, y: float) -> None:
        for label, color in reversed(entries):
            self.canvas.create_text(right, y, text=label, fill=color, anchor="e", font=self._info_font)
            right -= self._info_font.measure(label) + 12

    def _draw_series(self, values: list[float | None], color: str, domain: tuple[float, float], top: float, bottom: float,
                     width: float = 1.5) -> None:
        segment: list[float] = []

        def flush() -> None:
            if len(segment) >= 4:
                self.canvas.create_line(*segment, fill=color, width=width, smooth=False)
            elif len(segment) == 2:
                x, y = segment
                self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")
            segment.clear()

        for x, value in zip(self._positions, values):
            if value is None:
                flush()
            else:
                segment.extend((x, map_value(value, domain, bottom, top)))
        flush()

    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        self._positions = []
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 220 or height < 180:
            canvas.create_text(max(1, width) / 2, max(1, height) / 2, text="放大窗口查看走势图", fill=self.TEXT)
            return
        model = self.model
        title = "分时价格 · 主交易累计均价" if self.mode == "分时" and self.show_vwap else "分时价格" if self.mode == "分时" else "日 K · MA5 / MA10 / MA20"
        canvas.create_text(12, 15, text=title, anchor="w", fill="#edf4ff", font=("Microsoft YaHei UI", 10, "bold"))
        note = f"{model.date_label}  ·  {model.adjustment or '不复权实际价格'}  ·  {model.source}"
        canvas.create_text(12, 35, text=self._fit_text(note, width - 24), anchor="w", fill=self.TEXT, font=self._info_font)
        points = model.points
        if not points:
            canvas.create_text(width / 2, height / 2, text="暂无分时数据" if self.mode == "分时" else "暂无日线数据", fill=self.TEXT)
            return
        left, right = 60.0, float(width - (66 if self.mode == "分时" and model.reference else 18))
        top, bottom = 61.0, float(height - 74 if height < 260 else height - 106)
        volume_top, volume_bottom = bottom + 18, float(height - 28)
        if model.warnings:
            bottom -= 15
            volume_top -= 15
            volume_bottom -= 15
            canvas.create_text(12, height - 10, text=self._fit_text("；".join(model.warnings), width - 24), anchor="w", fill="#c4a967", font=self._info_font)
        self._bounds = (left, top, right, volume_bottom)
        self._plot_bottom = bottom
        values: list[float | None] = []
        for point in points:
            values.append(point.price)
            if all(value is not None and value > 0 for value in (point.open, point.price, point.high, point.low)) and point.high >= max(point.open, point.price) and point.low <= min(point.open, point.price):
                values.extend((point.high, point.low))
            if self.mode == "日线":
                values.extend(point.ma.values())
            elif self.show_vwap:
                values.append(point.vwap)
        self._price_domain = padded_domain(values, model.reference, self.mode == "分时")
        if not any(point.price is not None for point in points):
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="价格缺失；仅展示可用指标 / 成交量", fill=self.TEXT, font=self._info_font)
        x_domain = (points[0].x, points[-1].x)
        if len(points) == 1:
            self._positions = [(left + right) / 2]
        elif self.mode == "日线":
            spacing = (right - left) / len(points)
            self._positions = [left + (index + .5) * spacing for index in range(len(points))]
        else:
            self._positions = [map_value(point.x, x_domain, left, right) for point in points]
        for tick in range(5):
            value = self._price_domain[0] + tick / 4 * (self._price_domain[1] - self._price_domain[0])
            y = map_value(value, self._price_domain, bottom, top)
            canvas.create_line(left, y, right, y, fill=self.GRID)
            canvas.create_text(left - 6, y, text=f"{value:.2f}", anchor="e", fill=self.TEXT, font=("Consolas", 8))
            if self.mode == "分时" and model.reference:
                pct = (value / model.reference - 1) * 100
                canvas.create_text(right + 6, y, text=f"{pct:+.2f}%", anchor="w", fill=self.UP if pct > 0 else self.DOWN if pct < 0 else self.TEXT, font=("Consolas", 8))
        if self.mode == "分时":
            if model.reference:
                y = map_value(model.reference, self._price_domain, bottom, top)
                canvas.create_line(left, y, right, y, fill="#788ba5", dash=(4, 3))
            self._draw_series([point.price for point in points], self.PRICE, self._price_domain, top, bottom, 1.8)
            if self.show_vwap:
                self._draw_series([point.vwap for point in points], "#f8ca62", self._price_domain, top, bottom)
        else:
            candle_width = max(1, min(11, (right - left) / len(points) * .65))
            for x, point in zip(self._positions, points):
                color = self.UP if point.price is not None and point.open is not None and point.price >= point.open else self.DOWN
                valid_ohlc = all(value is not None and value > 0 for value in (point.open, point.price, point.high, point.low))
                if valid_ohlc and point.high >= max(point.open, point.price) and point.low <= min(point.open, point.price):
                    wick_top = map_value(point.high, self._price_domain, bottom, top)
                    wick_bottom = map_value(point.low, self._price_domain, bottom, top)
                    canvas.create_line(x, wick_top, x, wick_bottom, fill=color)
                    a = map_value(point.open, self._price_domain, bottom, top)
                    b = map_value(point.price, self._price_domain, bottom, top)
                    canvas.create_rectangle(x - candle_width / 2, min(a, b), x + candle_width / 2, max(a, b, min(a, b) + 1), outline=color, fill=color)
                elif point.price is not None:
                    # A close-only record remains a visible point, not a synthetic candle.
                    y = map_value(point.price, self._price_domain, bottom, top)
                    canvas.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5, fill=self.TEXT, outline="")
            for name, color in self.MA_COLORS.items():
                self._draw_series([point.ma.get(name) for point in points], color, self._price_domain, top, bottom)
        indicator_macd = self.mode == "日线" and self.indicator == "MACD"
        if indicator_macd:
            bars = [finite_number(point.row.get("MACD")) for point in points]
            dif = [finite_number(point.row.get("DIF")) for point in points]
            dea = [finite_number(point.row.get("DEA")) for point in points]
            domain = padded_domain(bars + dif + dea, 0.0)
            baseline = map_value(0, domain, volume_bottom, volume_top)
            for x, value in zip(self._positions, bars):
                if value is not None:
                    y = map_value(value, domain, volume_bottom, volume_top)
                    canvas.create_line(x, baseline, x, y, fill=self.UP if value >= 0 else self.DOWN, width=2)
            canvas.create_line(left, baseline, right, baseline, fill=self.GRID)
            self._draw_series(dif, "#f8ca62", domain, volume_top, volume_bottom)
            self._draw_series(dea, "#a995ef", domain, volume_top, volume_bottom)
            canvas.create_text(left - 6, volume_top, text="MACD", anchor="e", fill=self.TEXT, font=("Consolas", 8))
            if not any(value is not None for value in bars + dif + dea):
                canvas.create_text((left + right) / 2, (volume_top + volume_bottom) / 2, text="MACD 数据未提供", fill=self.TEXT, font=self._info_font)
        else:
            volumes = [point.volume for point in points if point.volume is not None and point.volume >= 0]
            maximum = max(volumes, default=0)
            canvas.create_text(left - 6, volume_top, text="量(手)", anchor="e", fill=self.TEXT, font=("Microsoft YaHei UI", 8))
            canvas.create_text(left - 6, volume_top + 14, text=self._volume_label(maximum) if volumes else "—", anchor="e", fill=self.TEXT, font=("Consolas", 8))
            if not volumes:
                canvas.create_text((left + right) / 2, (volume_top + volume_bottom) / 2, text="成交量未提供", fill=self.TEXT, font=self._info_font)
            bar_width = max(1, min(10, (right - left) / max(1, len(points)) * .65))
            previous = model.reference
            for x, point in zip(self._positions, points):
                if point.volume is not None and point.volume > 0 and maximum > 0:
                    y = map_value(point.volume, (0, maximum), volume_bottom, volume_top)
                    reference = point.open if self.mode == "日线" else previous
                    color = self.UP if reference is not None and point.price is not None and point.price >= reference else self.DOWN if reference is not None and point.price is not None else self.TEXT
                    canvas.create_rectangle(x - bar_width / 2, y, x + bar_width / 2, volume_bottom, fill=color, outline="")
                if point.price is not None:
                    previous = point.price
        ticks = min(5 if width > 500 else 3, len(points))
        tick_indices = list(dict.fromkeys(round(index * (len(points) - 1) / max(1, ticks - 1)) for index in range(ticks)))
        for index in tick_indices:
            x = self._positions[index]
            label = points[index].label
            canvas.create_line(x, top, x, volume_bottom, fill=self.GRID, dash=(2, 5))
            canvas.create_text(x, volume_bottom + 12, text=label, fill=self.TEXT, font=("Consolas", 8), anchor="w" if index == 0 else "e" if index == len(points) - 1 else "center")
        if self.mode == "日线":
            self._draw_legend(list(self.MA_COLORS.items()), right, top - 8)
        else:
            entries = [("价格", self.PRICE)]
            if self.show_vwap:
                entries.append(("主交易均价", "#f8ca62"))
            if model.reference is not None:
                entries.append(("昨收", "#788ba5"))
            self._draw_legend(entries, right, top - 8)

    def _hover(self, event: Any) -> None:
        self.canvas.delete("hover")
        left, top, right, bottom = self._bounds
        if not self._positions or not (left <= event.x <= right and top <= event.y <= bottom):
            return
        index = nearest_index(self._positions, event.x)
        if index is None:
            return
        point = self.model.points[index]
        x = self._positions[index]
        self.canvas.create_line(x, top, x, bottom, fill="#b5c6dc", dash=(3, 3), tags="hover")
        if event.y <= self._plot_bottom:
            self.canvas.create_line(left, event.y, right, event.y, fill="#b5c6dc", dash=(3, 3), tags="hover")
        if self.mode == "日线":
            text = f"{point.label}  开 {self._format(point.open)}  高 {self._format(point.high)}  低 {self._format(point.low)}  收 {self._format(point.price)}  量 {self._format(point.volume, 0)}手\n"
            text += "  ".join(f"{name} {self._format(point.ma.get(name))}" for name in self.MA_COLORS)
            if self.indicator == "MACD":
                text += f"  MACD {self._format(finite_number(point.row.get('MACD')), 3)}"
        else:
            text = f"{self.model.date_label}  {point.label}  价 {self._format(point.price)}  量 {self._format(point.volume, 0)}手\n主交易累计均价 {self._format(point.vwap, 3)}  分钟额 {self._format(finite_number(point.row.get('成交额')), 0)}元"
        box_x = left + 8
        box_y = top + 7
        item = self.canvas.create_text(box_x + 7, box_y + 5, text=text, anchor="nw", fill="#edf4ff", font=("Microsoft YaHei UI", 8), tags="hover", width=max(1, right - left - 34))
        bbox = self.canvas.bbox(item)
        if bbox:
            background = self.canvas.create_rectangle(bbox[0] - 6, bbox[1] - 4, bbox[2] + 6, bbox[3] + 4, fill="#1a2a40", outline="#637895", tags="hover")
            self.canvas.tag_lower(background, item)
