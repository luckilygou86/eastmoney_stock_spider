"""Tk research workspace. Public requests are explicit and workers never use Tk."""
from __future__ import annotations

import copy
import queue
import threading
import tkinter as tk
from tkinter import ttk

from research_data import ResearchClient, binding, number
from research_analysis import MODULES, analyze_research, dcf_fcfe, display, position_plan, render_module


class ResearchPanel(ttk.Frame):
    def __init__(self, master, on_result=None):
        super().__init__(master, padding=8)
        self.on_result = on_result
        self.snapshot = None
        self.research = self.analysis = None
        self.ready = self.busy = self.closed = False
        self.generation = 0
        self.events = queue.Queue()
        self.cancel_event = None
        self.module = tk.StringVar(value=MODULES[0])
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="研究模块").pack(side="left")
        self.selector = ttk.Combobox(toolbar, textvariable=self.module, values=MODULES, state="readonly", width=19)
        self.selector.pack(side="left", padx=6)
        self.selector.bind("<<ComboboxSelected>>", lambda _e: self.render())
        self.query_button = ttk.Button(toolbar, text="查询研究数据", command=self.start, state="disabled")
        self.query_button.pack(side="left", padx=3)
        self.cancel_button = ttk.Button(toolbar, text="取消", command=self.cancel, state="disabled", width=6)
        self.cancel_button.pack(side="left", padx=3)
        ttk.Button(toolbar, text="情景计算器…", command=self.open_calculators).pack(side="right")
        self.status = tk.StringVar(value="先查询股票，再点击“查询研究数据”；不会自动调用AI。")
        self.note = ttk.Label(self, textvariable=self.status, foreground="#475569", wraplength=700)
        self.note.pack(fill="x", pady=6)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="word", state="disabled", relief="flat", bg="#f8fafc", fg="#334155",
                            font=("Microsoft YaHei UI", 10), padx=10, pady=8, height=8)
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.bind("<Configure>", lambda e: self.note.configure(wraplength=max(200, e.width - 25)))
        self.bind("<Destroy>", lambda e: self.close() if e.widget is self else None)
        self.job = self.after(150, self.poll)
        self.render()

    def update_snapshot(self, result, ready=True):
        key, old = binding(result), binding(self.snapshot)
        eligible = bool(ready and key[0] and key[1] and str((result.get("quote") or {}).get("代码")) == key[0] and number((result.get("quote") or {}).get("最新价")) is not None and number(result["quote"]["最新价"]) > 0) if isinstance(result, dict) else False
        if key != old or (self.ready and not eligible):
            self.generation += 1
            if self.cancel_event:
                self.cancel_event.set()
            self.research = self.analysis = None
        self.ready = eligible
        # No ticks/trade-detail archive or personal state is needed by research.
        self.snapshot = copy.deepcopy({key: result.get(key) for key in ("identity", "quote", "daily_history", "daily_indicators", "benchmark_history")}) if isinstance(result, dict) else None
        incoming = result.get("research") if isinstance(result, dict) else None
        if eligible and isinstance(incoming, dict) and incoming.get("code") == key[0] and incoming.get("as_of") == key[1]:
            self.research = incoming
        if self.research and eligible:
            try:
                self.analysis = analyze_research(self.snapshot, self.research)
            except ValueError:
                self.research = self.analysis = None
        self.query_button.configure(state="normal" if eligible and not self.busy else "disabled")
        if not self.busy:
            self.status.set(f"{key[0]} · {key[1]}：研究数据已加载，可切换模块。" if self.analysis else "点击查询研究数据：获取财报、估值、公司、股东、公告和板块。" if eligible else "股票查询或跨日对齐完成后，再查询研究数据。")
        # Keep the text selection/scroll position during ordinary live ticks.
        if key != old or not eligible or self.analysis is None:
            self.render()

    def start(self):
        if not self.ready or self.busy or self.closed:
            return
        from stock_spider import Client
        snapshot, generation = copy.deepcopy(self.snapshot), self.generation
        cancel_event, events = threading.Event(), self.events
        self.cancel_event = cancel_event
        self.busy = True
        self.query_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("正在查询研究数据；财报与行情不会自动刷新或触发AI费用。")
        def worker():
            try:
                research = ResearchClient(Client(timeout=7, pause=0)).collect(snapshot, cancel_event)
                analysis = analyze_research(snapshot, research)
                events.put((generation, cancel_event, research, analysis))
            except Exception:
                events.put((generation, cancel_event, None, None))
        threading.Thread(target=worker, name="stock-research-query", daemon=True).start()

    def cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.status.set("已取消；等待正在读取的公开来源结束，不会采用较早结果。")
            self.cancel_button.configure(state="disabled")

    def poll(self):
        if self.closed:
            return
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        try:
            while True:
                generation, event, research, analysis = self.events.get_nowait()
                if event is self.cancel_event:
                    self.busy = False
                    self.cancel_event = None
                    self.cancel_button.configure(state="disabled")
                    self.query_button.configure(state="normal" if self.ready else "disabled")
                if generation != self.generation or event.is_set() or not self.ready:
                    if not self.busy:
                        self.status.set("较早查询已取消或股票日期已变化；可重新查询研究数据。")
                    continue
                if research is None:
                    self.status.set("研究查询未完成，请检查网络或重新查询。")
                    continue
                self.research = research
                # Include the latest same-day technical observations, not a stale worker price.
                self.analysis = analyze_research(self.snapshot, research)
                counts = self.analysis["summary"]
                self.status.set(f"研究已完成：{len(research['sources'])}类返回来源，{len(research['errors'])}类未取得；需复核{counts['需复核']}项、不适用{counts['不适用']}项、缺数据{counts['缺数据']}项。不是投资评分。")
                if self.on_result:
                    self.on_result(research, self.analysis)
                self.render()
        except queue.Empty:
            pass
        self.job = self.after(150, self.poll)

    def render(self):
        text = render_module(self.module.get(), self.snapshot, self.research, self.analysis) if self.analysis and self.ready else (
            "研究分析包含：股票基础、财务报表、公司分析、股票估值、量价技术、资金与市场、仓位和风控。\n\n"
            "先查询股票，再点击上方查询研究数据。公开来源失败或未披露的值保留为缺失。\n\n"
            "原创算法参考现金流、资本回报、成长和风险的一般原则；并非两本书的原文实现，也不能保证预测未来走势。")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")

    def open_calculators(self):
        dialog = tk.Toplevel(self)
        dialog.title("研究情景计算器（不发送AI、不保存个人资金）")
        dialog.geometry("590x450")
        dialog.minsize(520, 420)
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        for label, fields, calculate in (
            ("DCF股权情景", ("确认的年度FCFE(元)", "当前总股本(股)", "未来5年增长(%)", "股权折现率(%)", "永续增长(%)"), dcf_fcfe),
            ("仓位与盈亏比", ("研究资金(元)", "假设入场价(元)", "假设止损价(元)", "假设目标价(元)", "单笔风险预算(%)", "仓位上限(%)"), position_plan)):
            frame = ttk.Frame(notebook, padding=10)
            notebook.add(frame, text=label)
            entries = []
            for i, name in enumerate(fields):
                ttk.Label(frame, text=name).grid(row=i, column=0, sticky="w", pady=3)
                variable = tk.StringVar()
                ttk.Entry(frame, textvariable=variable, width=24).grid(row=i, column=1, sticky="ew", pady=3)
                entries.append(variable)
            frame.columnconfigure(1, weight=1)
            answer = tk.Text(frame, height=7, wrap="word", state="disabled", relief="flat", bg="#f8fafc")
            def run(entries=entries, calculate=calculate, answer=answer):
                try:
                    values = calculate(*(v.get() for v in entries))
                    text = "\n".join(f"{k}：{display(v) if number(v) is not None else v}" for k, v in values.items())
                except ValueError as exc:
                    text = str(exc)
                answer.configure(state="normal")
                answer.delete("1.0", "end")
                answer.insert("1.0", text)
                answer.configure(state="disabled")
            ttk.Button(frame, text="按手工假设计算", command=run).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=8)
            answer.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="nsew")
            frame.rowconfigure(len(fields) + 1, weight=1)
        ttk.Label(dialog, text="结果对假设敏感；未做预测回测，跳空与成交限制可能使风险超过预算。", wraplength=550).pack(fill="x", padx=10, pady=(0, 10))

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.cancel_event:
            self.cancel_event.set()
        if self.job:
            self.after_cancel(self.job)
            self.job = None
