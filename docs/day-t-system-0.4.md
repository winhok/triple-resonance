# 0.4.0 — 用户日 T 体系适配层

## 结论

0.4.0 在 0.3.2 的手动成交台账与分钟行情之上增加一层**只会否决、不会自动下单**的交易门禁。Setup A 仍为 `UNVALIDATED`；任何通过结果都只是 `DAYT_PLAN`，不是 BUY。

## 决策链

```text
Setup A（5m VWAP reclaim，研究候选）
  -> Account Gate（券商隔离、碎股步长、标的白名单、杠杆限制）
  -> Macro Gate（显式快照、事件 blackout、normal/elevated/blocked）
  -> Rotation Gate（QQQ vs DIA，growth/defensive/mixed）
  -> Structure Gate（15m 结构；有足够数据时加入 1h；5m RVOL）
  -> Quote Gate（最新 bid/ask、时效、spread、ask 相对信号价滑移）
  -> 原 0.3.2 Ledger.plan（只使用该券商自己的现金、费用、数量步长和风险预算）
  -> MANUAL_ONLY
```

## 双账户原则

`config/accounts.example.json` 只提供结构示例，不包含真实余额。每个券商使用独立 SQLite DB，禁止把多个券商现金加总成同一购买力。真实 `config/accounts.json`、`state/` 与 `runtime/macro.json` 已加入 `.gitignore`。

账户规则必须由用户确认后把 `rules_confirmed` 设为 true；未确认时只阻止新计划，不影响登记已经真实发生的 fill。

## 行情与成交

- 1m OHLCV/VWAP：沿用 Alpaca market-data-only 路径。
- 新增最新 Level-1 quote：bid/ask 用于 spread 和实际买入 ask 的风险定仓。
- 不导入、不构造 `TradingClient`。
- IEX 与 SIP 仍不能混用；没有 SIP 权限时必须按 IEX 口径理解结果。

## 宏观门禁

程序不会编造 10Y、WTI、VIX 或 CPI/FOMC 状态。外部研究流程先把**已核验且有有效期**的快照写到 `runtime/macro.json`。缺失、过期、未来时间戳一律 fail closed。

事件窗口（例如 CPI/FOMC）内使用 `MACRO_EVENT_BLACKOUT` 硬阻止新计划。`elevated` 默认也阻止；只有显式配置才允许。

## 风格轮动

QQQ 当日收益相对 DIA：

- 超过阈值：`growth`
- 低于负阈值：`defensive`
- 中间：`mixed`

标的在账户配置中声明 `style`。growth 标的在明显 defensive regime、defensive 标的在明显 growth regime 时默认阻止。DIA/QQQ/SPY 的阈值需要 forward test，当前不是收益最优参数。

## 多周期结构

在原 5m reclaim 之外增加：

- 15m 最新结构不能同时 lower-high + lower-low；
- 当 session 已形成至少两个完整 60m bar 时，最新小时不能同时破前小时 low 且收盘更低；
- 最新完整 5m 成交量必须达到前四根完整 5m 均量的 `min_rvol`。

这些规则是保守 veto，不是新入场信号。

## 杠杆 ETF

任何 `leverage > 1` 的标的默认 `LEVERAGED_STRATEGY_UNVALIDATED`。只有显式 `allow_leveraged=true` 才解除账户门禁，但这**不等于策略已验证**；TQQQ 等应独立做成本、回撤和 forward test。

## 验收边界

0.4.0 的软件验收目标是：

1. 不跨券商混用现金；
2. 没有新鲜宏观快照时不做计划；
3. 重大事件窗口不做新计划；
4. 风格冲突、结构转弱、成交量不足、quote 过期、spread 过宽或价格已跑掉时不做计划；
5. 通过所有门禁后仍调用既有 Ledger 风险计划，不自动成交；
6. 旧 0.3.2 回放语义不修改。

策略盈利能力必须另做 forward validation。软件测试通过不代表 Setup A 有正期望。
