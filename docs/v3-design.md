# triple-resonance v3 设计文档 — 底仓当日 T 系统

> 状态：分阶段的施工蓝图。**P0 ✅ 已实现**（领域模型 + 状态机 + Session + 定仓 + 本地 SQLite 状态库，
> 纯本地、无数据依赖、36 项单测全 PASS）；**P1 ✅ 已实现**（Alpaca 历史 1m + Parquet Store，
> 已拉取 NVDA+SPY 2025-01-02~2026-08-31 的 1m 数据并落地，feed=IEX 元信息完整，见 §10）。
> **P2 ✅ 已实现**（session 锚定聚合 + MarketContext + Setup A 纯函数）；
> **P3 修复后待重跑**（旧结果含 5m 前视与买入费漏算，状态为 `INVALIDATED_LOOKAHEAD_AND_ENTRY_FEE`，不得引用）；
> **P4 已实现（观察-only）**：Alpaca 实时数据接入 + 辅助观察层 —— 按用户 2026-09-07 约束
> **绝不接下单账号、不调用 TradingClient**，买入/卖出由用户手动执行，工具只输出信号建议并记录手动成交。
> v2.2 的结论（详见 [backtest-report-v2.md](backtest-report-v2.md)）是本设计的前提：
> **"60m 三指标整仓择时"已被证伪为负贡献（final 超额 -8.44pp），
> 且它本来就不是当日 T——平均持仓 22 个交易日的波段器被拿去验证"今天买今天卖"。**
> v3 不是修指标参数，而是换结构。

## 0.1 实施路线（复审后定稿）

**领域边界先于数据源**：先锁死 `domain` 对象与状态机，再接 Alpaca。
SDK 对象不允许渗透进策略/状态机层（见 §9 包结构）。P0 完成前不写一行 Alpaca API。

```text
P0  Domain + Session + T Bucket + Interfaces + StateStore   ✅ 已实现
P1  Alpaca Historical 1m + Data Store（parquet，含 feed 元信息）  ✅ 已实现
P2  1m → 5m/15m 聚合（按 session 锚定）+ MarketContext + Setup A（纯函数）  ✅ 已实现
P3  Same-day Backtester（1m 事件引擎，A/B/C 三基准，Signal Contribution = C−B）  ✅ 已实现
P4  Alpaca 实时数据 + 辅助观察（观察-only，不下单；TradingClient 不接）  ✅ 已实现
P5  Forward Test（手动观察，无需下单账号）
P6  Setup B / Reverse T（后置，独立回测）
```

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
| `daily_t_pnl` | 今日 T 已实现盈亏（reset_day 清零） |
| `cumulative_t_pnl` | 跨日累计 T 盈亏（用于 effective_cost，**不清零**） |
| `round_trips_today` | 今日 T 往返次数（适配日内交易限制，见 §8） |

**两个成本字段必须严格区分**（用户 P0 复审重点纠正）：

```text
broker_cost_basis       = base_cost × base_shares        # 券商/税务口径，T 盈亏绝不写回它
effective_cost_after_t  = (base_cost × base_shares − cumulative_t_pnl) / base_shares
                                                          # 系统内部"经济等效成本"，仅研究指标
```

例：100 股 @150，累计 T 盈利 +600 → effective_cost = (15000 − 600)/100 = 144。
`broker_cost_basis` 仍为 15000，不受 T 影响。对账务必以 `broker_cost_basis` 为准。

**不变式**（由 `portfolio.t_bucket.TBucketEngine` 保证，见 tests/test_t_bucket.py）：
1. `total = base_shares + t_shares` 恒成立，且 `total >= base_shares`（T 引擎碰不到 base）
2. `t_shares >= 0` 且 `<= t_max_shares`
3. 收盘（force_flatten）后 `t_shares == 0`
4. T 盈亏只写 `daily_t_pnl` / `cumulative_t_pnl`，`base_cost` 原字段永不被修改

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
force_flatten_at = close_at − 15min        # 见 §3 Session 模型
  ↓
T_FLATTEN：卖出全部 t_shares
  ↓
t_shares = 0，t_flattened = True（当日不再开 T）
```

- **15:45 不写死**：由 `session.force_flatten_at` 派生。提前收盘日（如 13:00 ET 收）自动变成 12:45。
- `domain.session.MarketSession` 由 `open_at`/`close_at` 派生 `opening_range_end`(open+15m)、
  `entry_cutoff`(close−30min)、`force_flatten_at`(close−15min)；`BacktestSessionProvider` 注入
  early_closes 表，`AlpacaSessionProvider`（P4）接 `get_calendar()/get_clock()`。

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
triple_resonance/
├── domain/
│   ├── models.py        # Bar / TBucket / MarketContext / SetupSignal / OrderIntent / Fill / RiskDecision
│   ├── session.py        # MarketSession + SessionProvider（Backtest/Alpaca 两实现）
│   └── events.py         # TTrade / SystemEvent（状态库落盘用）
├── data/
│   ├── protocol.py       # HistoricalProvider / LiveProvider（策略层只依赖接口）
│   ├── alpaca_historical.py   # P1
│   ├── alpaca_live.py          # P4
│   └── parquet_store.py        # P1
├── bars/
│   └── aggregator.py     # P2：1m → 5m/15m（session 锚定）
├── strategy/
│   ├── protocol.py        # SetupDetector（纯函数 detect）
│   ├── context.py         # P2：MarketContext
│   └── trend_pullback.py  # P2：Setup A
├── portfolio/
│   └── t_bucket.py        # T bucket 状态机（P0 ✅）
├── risk/
│   ├── sizing.py          # 定仓公式（P0 ✅）
│   └── stops.py           # 结构止损 + 锁定止损（P2）
├── execution/
│   ├── protocol.py        # ExecutionProvider（OrderIntent/Fill 建模）
│   └── manual.py          # P4（观察-only）：建议单 + 记录手动成交，绝不下单
├── state/
│   └── sqlite.py          # 本地状态库 + reconcile（P0 ✅）
├── backtest/
│   ├── __init__.py
│   ├── intraday.py        # P3：1m same-day 事件引擎（A/B/C 三基准）
│   └── run.py             # P3 CLI：python -m triple_resonance.backtest.run
└── assist.py              # P4：实时辅助观察（流→context→setup→建议，不下单）

# v2 legacy baseline（保留，不删）：
intraday.py   backtest.py   indicators.py   # 旧三共振仅 observe；新回测在 backtest/intraday.py
```

## 10. 实施顺序（每步可独立验收）

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | Domain + Session + T Bucket + Interfaces + SQLite StateStore（纯本地，无数据依赖） | ✅ 36 项单测 PASS：`tests/test_t_bucket.py`(19)·`test_session.py`(4)·`test_position_sizing.py`(6)·`test_state_store.py`(7)。覆盖 7 条不变式 + effective_cost + 定仓 + reconcile |
| P1 | Alpaca Historical 1m + Parquet Store（含 feed=IEX/SIP 元信息） | ✅ 已实现 + 实跑验证。`tests/test_parquet_store.py`(round-trip+meta)·`test_alpaca_historical.py`(Bar 转换+oauth profile，无网络) 共 11 项 PASS；`python -m triple_resonance.data.download --symbols NVDA SPY --start 2025-01-01 --end 2026-09-01 --timeframe 1m --feed iex` 落地 `data/alpaca/iex/{NVDA,SPY}/{2025,2026}.parquet` + `meta.json`（NVDA 167,493 / SPY 164,766 根，415 交易日，跨 2025-01-02~2026-08-31）。**注意**：Alpaca `end` 为右开区间，欲含某日需 end≥次日；IEX free 层历史回溯至 2025-01 可用；个别日（如 2025-01-02 SPY）首根落在 10:30 ET 属 IEX 开盘缺口，P2/P3 按 ts 对齐即可 |
| P2 | 1m→5m/15m 聚合（session 锚定）+ MarketContext + Setup A（纯函数） | ✅ `tests/test_aggregator.py`(5)·`test_context.py`(2)·`test_trend_pullback.py`(7) 共 14 项 PASS：session 锚定（09:30/09:45 桶）、OHLCV/vwap、原始 feature、detect 条件全锁死 |
| P3 | 1m same-day Backtester（Base + percentage-of-base T overlay） | ✅ 已修 closed-5m、next-1m-open、入场分钟 SL/TP、跳空止损、双边费用/滑点、RTH/OR quality gate 与 50/25/25 flat-start 分段；柱内同时触及按止损优先，尾盘在 force-flatten 分钟 Open 退出。冻结 Setup A 重跑：train `+1.28% / PF 1.15`，validation `-1.27% / PF 0.66`，final `+0.20% / PF 1.05`；因 validation 为负，只能判定候选 edge 未通过确认 |
| P4 | Alpaca 实时数据 + 辅助观察（观察-only，不下单） | ✅ `tests/test_alpaca_live.py`(4)·`test_manual_exec.py`(2) 共 6 项 PASS：Bar 转换、subscribe 接线、凭据解析（无 key/secret 明确报错）、建议单、手动成交记录。**约束**：不调用 TradingClient、不建任何订单；`assist.py` 实时流→context→setup→输出 OrderIntent 建议，买卖由用户手动 |
| P5 | Forward Test（手动观察） | 用户手动执行期间观察信号质量，无需下单账号 |
| P6 | Setup B / Reverse T | 独立回测通过后再加 |

**用户 2026-09-07 决策：本工具纯做"辅助做 T"——信号/上下文给你看，买入/卖出动作你自己来；不接任何能下单的账号。** 因此 P4 不含自动执行层，`execution/` 仅保留 `manual.py`（记录手动成交到 StateStore 供对账）。旧 P3 数字因时间语义与费用缺陷已作废；修复后结果仍只属于 discovery/分段研究，不代表 forward validation。

**P0 关键边界（已锁死）**：flatten 后当日禁止再开 T（隔夜保护）、stop≥entry 拒单、
空仓卖出/重复入场拒单、费用计入净盈亏、`effective_cost_after_t` 数学正确、
`broker_cost_basis` 永不被 T 修改、early-close 日 `force_flatten_at = close−15min`、
`size = risk_budget/(entry−stop)`、`reconcile` 不一致即 BLOCK_TRADING。

---

*v3 design draft · 2026-09-07 · 不构成投资建议*
