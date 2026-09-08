# 做空日 T 影子账本

`short-shadow` 是与多头 forward book 同时运行的研究账本。它只读取公开行情、记录模拟卖空和回补，不导入交易客户端、不提交订单，也不把研究结果描述为真实可执行卖空。

## 标的与输出

默认观察 `SPY / QQQ / DIA / IWM / NVDA / AAPL / MSFT / AMZN / META`。SPY 是市场基准，其余八个代码分别进入多头和空头研究账本。

一次运行只下载一份 1m 数据，并写出：

- `bars.ndjson`：多空共用行情；
- `decisions.ndjson` / `paper-fills.ndjson` / `summary.json`：多头；
- `short-decisions.ndjson` / `short-paper-fills.ndjson` / `short-summary.json`：空头影子账本；
- `combined-summary.json`：两个账本的并列与合计结果，不净额混账。

## Setup B

空头候选必须同时满足：

1. SPY 当前及前一个完整 5m 收盘都低于各自会话 VWAP，且 VWAP 向下；
2. 标的和 SPY 都低于会话 VWAP；
3. 标的相对 SPY 更弱；
4. 标的已经跌破开盘 15 分钟区间；
5. 最新完整 5m 反弹触及 VWAP 后重新收在其下，并且收阴；
6. 15m 不能是 higher-high + higher-low；形成两个完整 60m 后，1h 也不能是 higher-high 且收盘更高；
7. 最新 5m RVOL 至少为 1.0；
8. 从开盘到决策时刻的分钟数据完整。

Setup B 当前状态是 `RESEARCH_ONLY`，不是已验证的卖空信号。

## 模拟成交和风险

- 信号在完整 5m 收盘确认，下一分钟 Open 减去 5 bps 作为模拟卖空价；
- 仓位由 1% 风险预算确定，并以 1 倍虚拟抵押资金限制名义规模；
- 卖空所得不加入可复用现金；
- 止损为 opening-range high，目标为 1.5R；
- 跳空高于止损时按更差的 Open 加买侧滑点回补；
- 5m 收盘重新站上会话 VWAP 后，下一分钟 Open 加买侧滑点回补；
- 同一分钟同时触及止损和目标时止损优先；
- 收盘前 30 分钟停止新开仓，收盘前 15 分钟强制回补；
- 每个标的每天最多一个空头回合。

## 借券边界

公开 Yahoo 行情不能证明标的当时可借、借券费、召回风险、SSR 状态或券商保证金资格。因此每笔空头模拟成交都带有 `borrow_status=NOT_VERIFIED_RESEARCH_ONLY`。这些缺失项不阻止影子研究，但在任何真实实施评估中必须 fail closed。

## 运行

```bash
python -m triple_resonance.dayt.forward_test \
  --output runtime/forward-tests/YYYY-MM-DD \
  --poll-seconds 60
```

多头与空头的单日结果只能用于积累 forward evidence。至少积累 20–30 个交易日并完成独立时间切分、成本和缺数审计后，才能决定是否保留 Setup B。
