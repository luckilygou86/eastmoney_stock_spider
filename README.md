# 股票数据采集器

输入股票名称、6 位代码，或直接粘贴东方财富页面地址即可查询。例如：

- `https://quote.eastmoney.com/f1.html?newcode=0.301307`：分时成交/逐笔页面
- `https://quote.eastmoney.com/sz300059.html#fullScreenChart`：日线页面

支持：

- 当日分时成交，数据截止到当前接口返回时间
- 当日逐笔成交，包含时间、价格、成交量、买卖方向和成交额
- 最近一个月日线数据
- 最近 31 天龙虎榜
- 基于 OHLCV 的估算筹码分布和平均成本
- 可选的接口测试（仅界面显示，不写入导出报告）
- 导出单个 Markdown 走势分析报告（不含接口测试表）

页面地址本身是 JavaScript 展示页，HTML 只返回页面框架。逐笔数据现在按腾讯接口的 `p=0、1、2...` 分页抓取，每页约 70 条并合并去重；东方财富接口如果只返回首屏，会被标记为不完整，不会覆盖分页结果。

## 运行

```powershell
cd D:\CodexProjects\eastmoney_stock_spider
python stock_spider.py
```

也可以自检：

```powershell
python stock_spider.py --self-test 301307
```

筹码分布为估算口径，不应视为交易所原始持仓数据。不同软件可能使用不同数据源、复权方式、成交口径和更新时间。
