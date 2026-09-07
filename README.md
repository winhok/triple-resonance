# triple-resonance 0.3.2 — 手动日 T 操作助手

**只接行情；计划、手动成交台账、持仓核算、风险复核、尾盘提醒和录制回放均在本地。没有券商下单接口。**

主入口：`t-assist` 或 `python -m triple_resonance.assistant.cli`。Setup A 保持 `UNVALIDATED / RESEARCH_CANDIDATE`：软件操作流程与策略盈利分别验收，候选不是买入指令。

## 安装或升级

```bash
git pull --ff-only
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python -m pytest -q
python tests/test_backtest_engine.py
t-assist --help
```

Python 3.11+。升级前停止旧 watch，备份本地数据库及其 WAL/SHM（或关闭所有连接后复制数据库）。已有 `state.db` 可继续使用，**不要再次 init，不要重建真实持仓**。旧策略与观察脚本仍保留，安装后用上述新入口。

## 首次初始化

以下资金、股数、价格均为命令示例，不是仓位建议。`--cash` 是额外预留的 T 现金，不包含底仓市值。先核对券商账户规则、真实持仓和可用现金。

```bash
# 只做一次。按实际风险预算、费用与可用现金填写。
t-assist --db state.db init --cash 1000 --risk 10 --daily-loss 25 \
  --estimated-fees 2 --estimated-exit-fee 1
t-assist --db state.db register NVDA --base-qty 80 --base-cost 100 --max-t-qty 10
```

`--estimated-fees` 是新仓位往返的预计费用；`--estimated-exit-fee` 是每个已有 T 仓仍待支付的退出费用。旧配置或省略退出费时，保守地用全部往返费用预留退出费，不假设买卖费用对称。实际手续费仍由每笔 `fill --fee` 记账。分批卖出的总退出费、滑点与跳空可能超过估算，应按实际使用方式设置预算。

碎股可在初始化时配置 `--quantity-step 0.001`；实际可交易单位与资格由券商决定。默认不自动复用卖出回款；只有核对后才 `confirm-cash`，不自动推断结算规则。

## 观察与录制

在本机安全配置 `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`。不要把密钥写入命令参数、源码或 GitHub。

```bash
mkdir -p audit
t-assist --db state.db watch --symbols NVDA --benchmark SPY --feed iex \
  --record audit/session-001.ndjson
```

多个已登记个股可一起传给 `--symbols`；启动时会把台账中已有的未平 T 仓加入监控。运行中新增另一只未订阅的持仓会告警数据缺失，应停止并重新指定监控池。IEX/SIP 的数据口径不混用；分钟完整性不足时不伪造 VWAP 或买点。

每次使用**新的录制文件名**，不会覆盖或追加到旧文件。Ctrl+C 正常退出会写结束校验记录，仓位不会被标为已卖出。进程关闭后提醒停止。

## 手动成交、核对与计划

在券商实际成交后，另开终端登记真实成交编号、价格、费用和带时区时间。

```bash
t-assist --db state.db fill --id fill-001 --symbol NVDA --side buy \
  --qty 2 --price 100 --fee 1 --at '2026-09-08T10:00:20-04:00' --stop 97 --target 104.5

t-assist --db state.db status

t-assist --db state.db fill --id fill-002 --symbol NVDA --side sell \
  --qty 2 --price 102 --fee 1 --at '2026-09-08T11:05:10-04:00'

t-assist --db state.db reconcile NVDA --actual-total 80
t-assist --db state.db confirm-cash 1002
```

例中现金 `1000 - 201 + 203 = 1002`，净 T 盈利为2。相同 ID/相同内容重复登记不重复记账，冲突 ID 拒绝。支持碎股、部分成交、重启恢复；卖超本地 T 仓拒绝，不能借 T 引擎误动底仓。

```bash
# 只计算风险/现金允许数量，不是信号，也不预留现金。
t-assist --db state.db plan NVDA --entry 100 --stop 97
# 明确修正当前 T 仓的保护参数。
t-assist --db state.db set-risk NVDA --stop 98 --target 104.5
```

### 账户级风险门禁

任一标的有未解决的数量/资金/对账异常，即使该标的本地 T 仓为零，也阻止其他股票的新计划。失效保护、隔夜 T 仓、已触发但尚未处理的退出锁同样不会被当作“零风险”。

`status` 给出独立的 `operational_blocks` 与 `protection_issues`：

- `POSITION_MISMATCH`：核对实际数量，用 `reconcile` 确认；不修改真实成交。
- `CASH_REVIEW_REQUIRED`：核对券商可用现金，用 `confirm-cash` 确认；数量对账不能代替现金确认。
- `T_QUANTITY_EXCEEDED`：只有登记实际减仓至上限内才解除，不能靠确认数量洗掉。
- `LEGACY_REVIEW_REQUIRED`：旧版只有一个异常布尔值，来源无法区分；依次核对数量和现金，不自动放行。

保护参数修正不会解除独立的操作阻断或退出锁。记录已经发生的实际买卖仍然允许；提醒与计划永远不生成虚构成交。

每日预算同时预留已有仓位的止损价差风险与**剩余退出费用**。计划读取同一事务快照，不能在并发登记成交时混用不同时间的现金与仓位。两个并行计划仍不是资金预约，手动成交前须再次核对最新计划与实际报价。

## 提醒

`STOP_REVIEW / TAKE_PROFIT_REVIEW` 是已收到分钟 K 的触及复核，不是逐笔实时止损。`RISK_DATA_STALE / DATA_INVALID` 表示不应依赖旧数据操作。

尾盘按交易日历触发：收盘前15分钟 initial、前5分钟 urgent、收盘时或其后首次检查 closed；每阶段每段持仓一次，持久化去重。提前收盘跟随实际 session。断流时本地计时器仍提醒；**只有登记真实卖出才更新股数**。

## 可验证录制与回放

v2 录制保存初始台账、历史回补、实时/修订分钟、定时检查、行情断流状态、实际 session，以及另一个终端登记的成交/风险/现金/对账变化。按真实处理顺序回放，并验证每个输入后的输出与台账哈希。

```bash
# replay-001.db 必须不存在；不需要先 init/register，也不要传 symbols。
t-assist --db audit/replay-001.db replay --events audit/session-001.ndjson
# 成功才输出 REPLAY_VERIFIED 并创建回放数据库。
t-assist --db audit/replay-001.db status
t-assist --db state.db export audit/ledger-001.json
```

回放不会联网，不打开或覆盖现有真实数据库。序号/哈希错误、截断、缺少结束记录、语义不一致均失败，不发布“验证成功”的台账。旧版仅记录实时 bar 的文件缺少输入，不能自动补造成完整回放。录像保留版本；使用匹配版本回放。

录制含私人资金与交易数据，不含 API 密钥；不要提交到公开仓库。哈希用于检测意外修改与缺失，不是防恶意重签的数字签名。录制写入失败会停止监控并明确警告，不能继续宣称审计完整。

## 回测与适用边界

```bash
python -m triple_resonance.backtest.run --symbol NVDA --spy SPY --feed iex
python -m triple_resonance.backtest.run --symbol NVDA --t-cash 1000 --fee 1 \
  --slippage 0.0005 --execution-delay-minutes 1
```

保留已有 next-minute-open、入场分钟 SL/TP、跳空与双边费用/滑点模型。A/B/C 使用相同初始总资金，T 仓有现金约束；持仓期缺数不伪造成交。旧收益数字不能证明当前策略盈利。

软件提供人工看护下的操作辅助流程，不承诺信号收益、实盘成交价或自动保护。首次在自己的机器上核对行情权限、网络、台账与提醒；真实买卖始终由用户决定并执行。详见 `docs/operating-release-0.3.2.md`。
