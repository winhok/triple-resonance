# triple-resonance — 手动日 T 操作助手

**仅行情、研究、计划、手动成交台账与提醒，不连接任何券商下单接口。**
本次改造基于 `d6aeda7`。新入口：`t-assist` 或 `python -m triple_resonance.assistant.cli`。

软件功能与策略盈利是两项不同的验收。Setup A 始终为 **UNVALIDATED**，输出 `RESEARCH_CANDIDATE`，不承诺收益。
根目录 `intraday.py` / `backtest.py` 保留为 v2 观察与历史基线；旧设计文档中的阶段状态和收益数字不代表本版本验收。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python -m pytest -q
```

Python 3.11+。本地交易日历不需要券商交易账户。行情凭据只读取环境变量，不应提交到 GitHub。

## 初始化与看盘

以下价格、股数和资金仅是命令格式示例，不是仓位建议。`--cash` 是额外预留的 T 现金，不是底仓市值。
已有台账不能被 init 覆盖；新表使用独立命名空间，不会自动迁移或修改旧持仓表。

```bash
# 初始化只做一次。按实际资金与风险预算填写。
t-assist --db state.db init --cash 1000 --risk 10 --daily-loss 25
t-assist --db state.db register NVDA --base-qty 80 --base-cost 100 --max-t-qty 10

# 预先在本机安全设置 APCA_API_KEY_ID / APCA_API_SECRET_KEY。
# 不在命令参数中填写 secret；不需要接任何券商下单接口。
t-assist --db state.db watch --symbols NVDA --benchmark SPY --feed iex
```

可在 `--symbols` 后提供多个已登记个股。SPY 默认仅作市场环境基准。
碎股台账可在 init 使用 `--quantity-step 0.001`；是否可以碎股交易仍须向自己的券商确认。
候选形态附带风险/现金计划，但计划不预留资金，不是保证可成交的报价。

## 登记实际手动成交

在自己的券商完成交易后，在另一终端登记真实成交。时间必须带时区，ID 应采用唯一成交编号。

```bash
t-assist --db state.db fill --id broker-fill-001 --symbol NVDA --side buy \
  --qty 2 --price 100 --fee 1 --at '2026-09-08T10:00:20-04:00' --stop 97 --target 104.5

t-assist --db state.db status

t-assist --db state.db fill --id broker-fill-002 --symbol NVDA --side sell \
  --qty 2 --price 102 --fee 1 --at '2026-09-08T11:05:10-04:00'

t-assist --db state.db reconcile NVDA --actual-total 80
# 只有向券商核对过可用现金，才确认卖出回款可再次使用。
t-assist --db state.db confirm-cash 1002
```

本例现金为 `1000 - 201 + 203 = 1002`，净 T 收益为2。
相同成交ID、相同内容重复登记不会重复记账；冲突内容报错。支持部分成交和碎股。
卖出数量超过本地 T 仓会报错，防止误动底仓；真实账户发生额外操作时须先核对，不应伪造一笔T成交。
买入事实已发生但超过现金或数量上限时，会记录该事实并标记异常，而不是假装成交没有发生。

`effective_cost_after_t` 是经济等效成本，不是券商或税务成本。
新计划按实际现金、费用预算、已有持仓风险和日内次数限制计算；无法保证跳空时亏损不超过预算。

## 提醒语义

| 输出 | 含义 |
|---|---|
| `RESEARCH_CANDIDATE` | 完整闭合、时点同步的形态候选；策略未验证，不是买入指令 |
| `STOP_REVIEW` / `TAKE_PROFIT_REVIEW` | 已接收的分钟 K 触及止损/目标；是事后复核提醒，不是成交 |
| `T_FLATTEN_REQUIRED` | 收盘前15分钟提醒退出；提前收盘自动调整，断流时定时器仍工作 |
| `RISK_DATA_STALE` / `DATA_INVALID` | 数据过期或不完整，不应依据旧信息操作 |
| `OVERNIGHT_T_REQUIRES_REVIEW` | 前日 T 仓仍未登记平仓；不会跨日自动抹掉 |
| `UNPROTECTED_POSITION` | 没有登记完整止损和目标位 |

**提醒不会修改持仓数量。只有实际成交登记改变台账。**
分钟级提醒不是逐笔实时保护，也不代替券商保护性订单。
终端/进程退出后提醒也停止；退出时会提示检查未平仓 T 仓。

## 回测、前向记录与导出

```bash
# 本地已下载的数据。默认三段，--full-discovery 可只跑全样本。
python -m triple_resonance.backtest.run --symbol NVDA --spy SPY --feed iex
# 资金与执行延迟压力测试，不调用任何交易接口。
python -m triple_resonance.backtest.run --symbol NVDA --t-cash 1000 --fee 1 \
  --slippage 0.0005 --execution-delay-minutes 1

mkdir -p audit
t-assist --db state.db watch --symbols NVDA --record audit/events.ndjson
t-assist --db state.db export audit/ledger.json
# 用另一个已初始化/登记的DB回放，避免污染真实台账的去重记录。
t-assist --db replay.db replay --symbols NVDA --events audit/events.ndjson
```

新回测与实时共用决策时间边界：完整5m关闭、个股与基准同一时点、数据质量及截止门禁。
入场分钟 SL/TP、跳空、双边费用与滑点、尾盘开盘退出都纳入模型。
A 是相同总资金全部投入；B 是底仓加预留现金；C 是相同底仓加现金出资的 T。
**C−B 用相同初始总资金作分母，不使用额外免费资金。**
收益从实际现金变化派生，并断言等于所有交易净盈亏之和。
持仓期缺分钟会标记 `INVALID_EXECUTION_COVERAGE`；没有有效尾盘数据则报错，不伪造陈旧价格成交。
下一分钟开盘和1分钟延迟只是执行模型，不是实际收到信号后可拿到的价格。

Parquet 增量合并保留历史，单写者锁和原子替换防冲突，manifest 含真实文件 SHA-256。
后续更新可能改变文件；复现需归档当次数据和 manifest，不能只保留 hash。
IEX 与 SIP 不可混作同口径数据；严格完整性门禁可能拒绝很多 IEX 稀疏分钟日，不能用填充数据伪造成交证据。

## 使用边界

本版本提供手动操作所需的软件流程，但没有把 Setup A 变成已验证策略。
先用独立测试DB核对台账和提醒，再仅观察真实行情。是否据此交易应另行验证，不由单元测试决定。
账户准入、结算、保证金及交易限制需自行核对，软件不推断券商规则，也没有下单权限。
验收范围、未验证事项及变更详见 [docs/manual-assistant-release.md](docs/manual-assistant-release.md)。
