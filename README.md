# triple-resonance

**美股做 T 辅助工具** — RSI / MACD / EMA 三指标共振信号 + 成本锚定止损。

针对美股持仓的日内/波段做 T 场景：盯什么、什么时候加仓、什么时候减仓、止损放哪，给出可执行的判断依据。

## 它做什么

```
美股持仓 → 15m/60m K 线 → RSI/MACD/EMA 三共振 → 自动判断加仓/卖出/止损 → 终端结论
```

- **三共振信号**：趋势（EMA9>EMA21）+ 动量（MACD 柱同向且近期金叉/死叉）+ RSI 位置，三条同时成立才算强信号；两条成立会明确告诉你**缺哪条**
- **只用已闭合 K 线**：丢弃正在形成的 bar，杜绝信号重绘（回测/实盘口径一致）
- **成本锚定止损**：止损位锚定你的持仓成本（成本×0.97 或 成本−1.5×ATR），现价跌破立即提示，不随价格漂移
- **持仓联动**：记录持仓后自动算浮盈亏、市值、止损位，结论直接对应动作（建仓/加仓/减仓/止损/观望）
- **内置回测引擎**：逐 bar 状态机、防前视、含交易成本、三段验证（train/validation/final），配有合成数据回归测试

## 快速开始

```bash
pip install -r requirements.txt

# 扫描信号（15m 观察，60m 为当前推荐周期）
python intraday.py scan AAPL NVDA TSLA --interval 60m

# 只看指标，不给建议
python intraday.py quote AAPL

# 记录持仓（之后扫描会自动联动止损/加减仓建议）
python intraday.py position add AAPL 100 310.5 --note "底仓"
python intraday.py position list

# 回测（60m / 2 年，含三段验证与成本敏感性）
python backtest.py --interval 60m --period 730d

# 回归测试（验证执行时序：入场当根止损、信号优先级、跳空成交）
python tests/test_backtest_engine.py
```

## 信号规则

| 方向 | 趋势 | 动量 | 位置 |
|---|---|---|---|
| 做多（buy） | EMA9 > EMA21 | MACD 柱 > 0 且近 3 根内金叉 | RSI 35–65 健康区，或从 <30 上穿回 30 |
| 做空（sell） | EMA9 < EMA21 | MACD 柱 < 0 且近 3 根内死叉 | RSI ≥ 65，或从 >70 回落 |

3/3 强信号（建议执行）｜2/3 中等（半仓或等第三条）｜≤1 无信号（观望）。
多空分数相同一律观望，不偏多。**持仓时止损优先于任何信号。**

## 回测现状（如实）

详见 [docs/backtest-report-v2.md](docs/backtest-report-v2.md)。核心事实：

- 60m + 门槛 3 在 final 验证段收益 +21.6%，与同期买入持有基本持平（无收益 alpha），但 MaxDD 与 Sharpe 略优（-12.7% vs -13.3%，1.40 vs 1.18）——**价值在风控而非超额收益**
- 15m 当前未发现稳定样本外优势（数据窗口仅约 60 天，不下死刑结论）
- 当前定位：**底仓持有 + 60m 信号管理加减仓 + 止损保护**。做 T 实时盘中打点依赖下面的 Roadmap

## Roadmap（做 T 全链路）

- [ ] 实时数据源接入（券商 API / 实时行情，替代 yfinance 延迟数据）
- [ ] 盘中自动监控（watch 模式：信号出现即提醒，含 forming bar 处理）
- [ ] 底仓 + overlay 仓位结构回测（80% 永久仓 + 信号弹性仓）
- [ ] 做T计数器（PDT 规则提醒：保证金账户 <$25k 时 5 日 3 次限制）
- [ ] 更多指标与周期（5m/30m、布林、成交量确认）
- [ ] 信号推送（webhook / 桌面通知）

## 项目结构

```
intraday.py    信号引擎：取数 + 三共振 + 持仓联动 + 建议输出
backtest.py    回测引擎：逐 bar 状态机、三段验证、成本敏感性、paired 网格
indicators.py  指标计算：RSI(Wilder)/MACD/EMA/ATR，纯 pandas，无前视
docs/          策略参数说明 + 回测报告
tests/         执行时序回归测试
```

## 免责声明

本工具输出仅供研究参考，不构成投资建议。数据来自 Yahoo Finance（有延迟，非实盘级）。日内交易受 PDT 规则约束，请自行确认账户类型与合规限制。交易决策与盈亏由使用者自行承担。

## License

[MIT](LICENSE)
