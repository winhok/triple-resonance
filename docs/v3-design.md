# triple-resonance v3 设计文档 — 底仓当日 T 系统

> 状态：分阶段的施工蓝图。**P0 已实现**（纯本地仓位模型 + 单元测试，见下方 §10），
> P1（Alpaca 接入）待用户启动。本文档其余章节为后续阶段的门槛与约束。
> v2.2 的结论（详见 [backtest-report-v2.md](backtest-report-v2.md)）是本设计的前提：
> **"60m 三指标整仓择时"已被证伪为负贡献（final 超额 -8.44pp），
> 且它本来就不是当日 T——平均持仓 22 个交易日的波段器被拿去验证"今天买今天卖"。**
> v3 不是修指标参数，而是换结构。

## 0. 设计原则

1. **v2.x 的教训全部制度化**：三段状态隔离、flat 起跑、统一 warmup、
   1.0 基准曲线、统一时间戳边界、数据集指纹——这些是地基，v3 回测直接继承
2. **一次只做一个 setup**：v3 第一版只有 Setup A（顺势低吸 T）。
   反向 T、突破追涨、做空全部后置——上一轮参数地狱的教训
3. **信号层与风控层分离**：v2.x 的止损纪律层（建仓锁定 + 破位告警）独立保留，
   不与 T 引擎耦合
4. **每个动作必须先过回测**：T_BUY/T_SELL 恢复输出之前，
   1m 粒度回测必须通过（v2.x 的信号层至今仍处于 observe 模式，原因即此）

## 1. 目标与非目标

**目标**：回答一个具体问题——

> "我今天做 T 到底有没有降低底仓的有效成本？"

围绕已有的底仓（如 NVDA 100 股），用当日 T bucket 增厚/降本，
当日强制平仓，不改变底仓敞口。

**非目标**：
- 不追求跑赢 B&H（v2.2 已证伪）
- 不做隔夜波段（v2.2 已证伪）
- 不做空（第一版）
- 不预测市场方向——环境判断只做"今天适不适合 T"的过滤

## 2. 仓位结构（核心数据模型）

```json
{
  "symbol": "NVDA",
  "base_shares": 80,
  "base_cost": 150.0,
  "base_stop_px": 145.5,

  "t_max_shares": 20,
  "t_shares": 0,
  "t_avg_cost": null,
  "t_entry_ts": null,
  "t_stop_px": null,

  "t_realized_pnl_today": 0.0,
  "round_trips_today": 0,
  "t_flattened": false
}
```

| 字段 | 含义 |
|---|---|
| `base_shares` | 永久底仓，**T 引擎永远不可动**（风控层只监控 base_stop_px） |
| `t_max_shares` | 当日 T 仓上限（如底仓的 20%） |
| `t_shares` | 当前 T 仓（0 ~ t_max_shares） |
| `t_avg_cost` / `t_stop_px` | T 仓成本与结构止损（见 §5） |
| `t_realized_pnl_today` | 今日 T 已实现盈亏 |
| `round_trips_today` | 今日 T 往返次数（适配日内交易限制，见 §8） |
| `base_effective_cost` | 派生指标：base_cost × base_shares − 累计 T 盈利 ÷ (base_shares + …)，即"T 盈利折算后的底仓有效成本" |

**不变式**：收盘后 `t_shares == 0`（T_FLATTEN 强制，见 §6）。

## 3. 时间框架与管线

```
1m feed（券商 WebSocket）
  ↓ bar aggregator
  ├── 5m bars  → setup/trigger
  └── 15m bars → market context
        ↓
MarketContext（每 5m 更新）
  · SPY 当日状态（强/中/弱）
  · 个股相对 SPY 强弱（RS）
  · session VWAP ± σ 带
  · Opening Range（09:30–09:45 的 ORH/ORL）
        ↓
Setup Detector（Setup A only，v3 MVP）
        ↓
T Position Engine（T bucket 状态机）
        ↓
Risk（结构止损 + 定仓公式 + 日内限制）
        ↓
Execution（paper → broker）
```

- **09:30–09:45**：不交易。只建立 OR / VWAP / SPY 状态 / RS（开盘 1m 假突破多，
  等 15m opening range 成型）
- **09:45–15:30**：唯一允许开 T 的窗口
- **15:45–15:50**：T_FLATTEN 窗口（无论信号如何）

## 4. Setup A：顺势低吸 T（v3 唯一策略）

**Context（全满足才允许做多 T）**：

```
SPY 非明显弱势（如 SPY > 当日 VWAP，或 15m 结构未破位）
+ 个股相对 SPY 强（当日 RS > 0）
+ 价格 > VWAP
+ OR 结构未破坏（价格未跌破 ORL 等）
```

**Setup**：5m 上升趋势中回踩关键位——VWAP / ORH（突破回踩）/ 前高突破位。

**Trigger**：不直接接刀。等 **5m reclaim / confirmation close**（收回关键位的
5m 闭合 K）+ 成交量确认。

**不是**：`RSI=58 + MACD金叉 + EMA9>21 = BUY`。RSI/MACD/EMA 保留，但降级为
**filter / feature**（如"动量过滤：MACD 柱不为深负"），不做主触发器。

**入场**：T_BUY，size 由定仓公式决定（§5），只允许一次（当日 T 仓上限内）。

## 5. 风险：结构止损 + 定仓

**止损用结构位，不用固定百分比**（-3% 对当日 T 太宽）：

```
pullback swing low（5m）
或 VWAP 失守（5m close < VWAP）
或 OR 关键位失守
```

**定仓公式（先风险后仓位，不是先仓位后止损）**：

```
size = 允许亏损金额 ÷ (entry − stop)

例：本次最多亏 $30，entry=150.00，stop=149.50
   → 风险 $0.50/股 → size = 60 股
   → 实际取 min(60, t_max_shares, base 的可 T 上限)
```

**止盈**：回到 VWAP 上方前高 / 1.5R–2R / 尾盘 flatten，v3 第一版用最简单的
固定 R 倍数 + 15:45 flatten，避免过早进入参数调优。

## 6. 当日强制平仓（不可关闭）

```
15:45 ET（可配置 15:45–15:50 窗口）
  ↓
T_FLATTEN：卖出/买回全部 t_shares
  ↓
t_shares = 0（当日不再开 T）
```

这条规则的存在理由：v2.x 的教训——"今天做 T → 套住 → 明天再等等 →
变成 22 天波段仓"。状态机层面禁止 T 仓过夜，不依赖自律。

## 7. 回测（必须 1m 粒度）

**为什么**：5m OHLC 无法分辨同 bar 内 TP/SL 先后：

```
5m bar: Open 100, High 102, Low 98, Close 101
TP = 101.5, SL = 99 —— 谁先到？OHLC 不知道。
```

**结构**：

```
Signal timeframe  = 5m
Context timeframe = 15m
Execution/backtest resolution = 1m（最好另有 bid/ask quote）
```

**继承 v2.2 的全部实验设计**：三段 flat 起跑隔离、统一时间戳边界、
统一 warmup（OR/VWAP 按日重置，warmup 问题天然消失）、1.0 基准组合曲线、
数据集指纹、默认联网刷新。**每日为独立样本**（当日开当日平，无跨日状态），
统计口径按日聚合。

**数据需求**：≥ 半年~2 年的 1m 历史。yfinance 不满足（1m 仅约 7 天），
需要券商/付费数据源（见 §8）。回测先行，实盘后置。

**通过门槛（T 动作恢复输出的前提）**：
1. 1m 回测中 Setup A 相对"底仓 B&H + 不做 T"有正贡献（以降低有效成本计）
2. 成本敏感性：含 spread/slippage 假设（如 2–5bps + $0.005/share）后仍为正
3. 最差日亏损 ≤ 定仓公式的允许亏损金额 × 1.5（滑点/跳空余量）

## 8. 数据源与券商

| 用途 | 选择 |
|---|---|
| 日线/基本面/快速原型/粗回测 | yfinance（研究 fallback，现役） |
| 1m 长历史 | 券商/正式行情供应商 |
| 实时 trades/quotes/minute bars | 券商 WebSocket（**首选 Alpaca**：一套 API 有 trades/quotes/bars/order updates，bar schema 自带 `vw`；注意免费档是 IEX 单交易所，与 consolidated SIP 有差异） |
| 实时止损 | **券商订单系统**（stop order），不是本地轮询 |
| 成交回报 | broker order-update WebSocket |

原则：**行情 + 下单尽可能同一 broker**，减少两边 timestamp/price 不一致。

**监管适配**：FINRA 自 2026-06-04 起实施新 intraday margin 框架，
过渡期至 2027-10-20，各券商适用规则不一。日内交易次数限制作为
broker/account adapter 的配置项（`round_trips_limit`），**引擎不硬编码**。

## 9. 目录结构（演进目标）

```
data/
  historical.py      # 1m 历史拉取与缓存
  live.py            # WebSocket 接入（Alpaca 优先）
strategy/
  context.py         # MarketContext: OR / VWAP / SPY / RS
  trend_pullback.py  # Setup A（v3 MVP）
  reverse_t.py       # Setup B（后置，独立回测）
portfolio/
  base_position.py   # 底仓（T 引擎不可动）
  t_bucket.py        # T 仓状态机 + 当日 PnL + flatten
risk/
  position_size.py   # 定仓公式
  stop.py            # 结构止损 + v2.x 锁定止损（保留）
execution/
  paper.py           # 纸面成交（先跑通）
  broker.py          # 券商下单（Alpaca）
backtest/
  intraday.py        # 1m 粒度回测，继承 v2.2 实验设计
intraday.py          # 过渡期：现有观察引擎（保留至 v3 主体可用）
backtest.py          # v2.2 回测（保留作历史基线）
```

## 10. 实施顺序（每步可独立验收）

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | t_bucket 仓位模型 + 当日 PnL/有效成本计算（纯本地，无数据依赖） | ✅ 单元测试 `tests/test_t_bucket.py`（24/24 PASS，2026-09-07）：`portfolio/base_position.py`·`portfolio/t_bucket.py`·`portfolio/portfolio.py` + `risk/position_size.py` |
| P1 | Alpaca 数据接入（历史 1m + paper trading） | 拉 6 个月 NVDA/SPY 1m 数据成功 |
| P2 | MarketContext（OR/VWAP/RS）+ Setup A 信号（纯计算，不下单） | 历史信号复盘：每笔 T 的 context/setup/trigger 可解释 |
| P3 | 1m 回测引擎（继承 v2.2 实验设计） | 合成数据回归测试 + 通过 §7 门槛 |
| P4 | paper execution（Alpaca paper） | 一周模拟盘，T 仓每日清零、无隔夜 |
| P5 | 实盘（小仓）+ broker stop order | 用户确认后 |

**在 P3 通过 §7 门槛之前，T_BUY/T_SELL 不恢复输出。**

---

*v3 design draft · 2026-09-07 · 不构成投资建议*
