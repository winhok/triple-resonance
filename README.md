# triple-resonance

**美股底仓做 T 辅助工具** — 底仓观察 + 锁定止损（v2.x），演进目标：当日 T 系统（v3，见 [docs/v3-design.md](docs/v3-design.md)）。

## 当前状态（v2.2，如实）

五轮外部复审驱动的修复链（执行时序 → 统计口径 → 实验设计）之后，回测结论明确：

- **三共振 60m 择时是负贡献**：段隔离后 final 段 +12.58% vs 等权 B&H +21.01%（超额 -8.44pp），Sharpe 0.89 vs 1.17
- 因此**信号层已降级为观察模式**：只输出共振方向/分数/条件明细，不再产生 加仓/减仓/建仓 建议
- **止损层保留**：这是当前工具仅存的实盘价值——止损位建仓时一次锁定（entry 锚定，不随价格/波动率漂移），破位即提示，上根 Low 破位事后告警
- v3 方向：放弃"低频指标整仓择时"，改为**当日 T 结构**（15m 环境 → 5m setup → 1m/quote 执行，独立 T bucket，当日强制平仓）——设计文档见 [docs/v3-design.md](docs/v3-design.md)

## 它做什么（现在）

```
美股持仓 → 15m/60m K 线 → RSI/MACD/EMA 三共振（观察）+ 锁定止损（风控）→ 终端状态
```

- **三共振观察**：趋势（EMA9>EMA21）+ 动量（MACD 柱同向且近期金叉/死叉）+ RSI 位置；输出方向与分数，明确告诉你**缺哪条**——但不构成交易建议
- **只用已闭合 K 线**：丢弃正在形成的 bar，杜绝信号重绘
- **成本锚定止损**：止损位在**建仓时一次锁定**（成本 − 1.5×ATR，或手动指定），持久化保存。止损**位**计算与回测一致；实时**触发**依赖券商止损单（本工具用已闭合 K 事后检查并告警"上根 Low 已破位"）
- **持仓联动**：记录持仓后自动算浮盈亏、市值、锁定止损位
- **内置回测引擎**：逐 bar 状态机、防前视、含交易成本、三段验证（train/validation/final **各自 flat 起跑，无跨段状态继承**）、统一时间戳分段边界、统一参数 warmup，组合收益/MaxDD/Sharpe 出自同一条初始 1.0 基准曲线，配有 9 项合成数据回归测试

## 快速开始

```bash
pip install -r requirements.txt

# 扫描共振状态（默认 60m；15m 观察用 --interval 15m）
python intraday.py scan AAPL NVDA TSLA

# 只看指标
python intraday.py quote AAPL

# 记录持仓（之后扫描联动止损监控）
python intraday.py position add AAPL 100 310.5 --note "底仓"
python intraday.py position add AAPL 100 310.5 --stop 300   # 手动锁定止损价
python intraday.py position set-stop AAPL 295               # 改锁定止损
python intraday.py position list

# 回测（60m / 2 年，默认联网刷新数据；--use-cache 复用本地缓存）
python backtest.py --interval 60m --period 730d
# 固定日期窗口（可复现，报告数字绑定数据集指纹）
python backtest.py --interval 60m --start 2024-09-06 --end 2026-09-06

# 回归测试（执行时序 4 项 + 统计口径/段隔离/warmup/止损锁定 5 项）
python tests/test_backtest_engine.py
```

## 信号规则（观察参考，非交易建议）

| 方向 | 趋势 | 动量 | 位置 |
|---|---|---|---|
| 偏多（buy） | EMA9 > EMA21 | MACD 柱 > 0 且近 3 根内金叉 | RSI 35–65 健康区，或从 <30 上穿回 30 |
| 偏空（sell） | EMA9 < EMA21 | MACD 柱 < 0 且近 3 根内死叉 | RSI > 65（严格，避免 65 同值双侧），或从 >70 回落 |

多空分数相同一律无方向。**持仓时止损优先于任何信号。**

## 回测现状

详见 [docs/backtest-report-v2.md](docs/backtest-report-v2.md)（v2.2，段隔离验证）。核心事实：

- 60m + 门槛 3 在 final 验证段（flat 起跑）收益 +12.58% vs 等权 B&H +21.01%（**超额 -8.44pp**）；此前"≈持平"是跨段持仓状态造成的假象
- 15m 段隔离后超额 +1.31pp，但 5bps 成本即转负、Exposure 仅 25%，无可用优势
- 择时信号不作为加减仓依据；工具定位 = **止损纪律层**

## Roadmap（v3：当日 T 全链路）

详细设计：[docs/v3-design.md](docs/v3-design.md)

- [ ] **数据层**：券商 WebSocket（Alpaca 优先：trades/quotes/minute bars/order updates），yfinance 降级为研究 fallback；1m 历史数据（≥半年）
- [ ] **仓位结构**：base（永久底仓，T 引擎不可动）+ T bucket（当日 T 仓，15:45 ET 强制清零）
- [ ] **Setup A：顺势低吸 T**（第一版唯一策略）：15m opening range + VWAP + SPY/个股相对强弱定环境，5m 回踩关键位 + reclaim 确认入场
- [ ] **结构止损 + 风险定仓**：pullback swing low / VWAP 失守 / OR 关键位失守；size = 允许亏损金额 ÷ (entry − stop)
- [ ] **1m 粒度回测**（信号 5m / 环境 15m / 成交模拟 1m）——5m OHLC 无法分辨同 bar 内 TP/SL 先后
- [ ] **Setup B：反向 T**（均值回归，独立回测，不与 Setup A 混一个 score）
- [ ] T 引擎动作恢复（T_BUY/T_SELL/T_FLATTEN）——须先通过 1m 回测
- [ ] 日内交易限制适配（FINRA 2026-06 新 intraday margin 框架过渡期至 2027-10，规则由 broker adapter 决定，不硬编码）

## 项目结构

```
intraday.py    盘面观察引擎：取数 + 三共振（观察）+ 持仓联动 + 止损监控
backtest.py    回测引擎：逐 bar 状态机、三段隔离验证、成本敏感性、paired 网格
indicators.py  指标计算：RSI(Wilder)/MACD/EMA/ATR，纯 pandas，无前视
docs/          策略参数说明 + 回测报告 v2.2 + v3 设计文档
tests/         9 项回归测试（执行时序 4 + 统计口径/段隔离/warmup/止损锁定 5）
```

## 免责声明

本工具输出仅供研究参考，不构成投资建议。数据来自 Yahoo Finance（有延迟，非实盘级；实盘触发/止损成交请依赖券商订单系统）。日内交易受 PDT 与日内保证金规则约束——**FINRA 自 2026-06-04 起实施新 intraday margin 框架，过渡期内（至 2027-10-20）各券商规则不一**，请向自己的券商确认当前适用的日内交易限制。交易决策与盈亏由使用者自行承担。

## License

[MIT](LICENSE)
