"""triple_resonance — 底仓 + 当日 T 系统（v3）。

包结构（P0 起步）：
  domain/     领域模型与事件（全系统只传这些对象，策略层不感知数据源专有类型）
  portfolio/  T bucket 状态机
  risk/       定仓公式
  data/       数据接入协议（P1 才落地 Alpaca adapter）
  execution/  执行协议（P4 才落地 Alpaca adapter）
  strategy/   策略协议（P2/P3 才落地 Setup A）
  state/      本地 SQLite 状态库（进程重启恢复 + reconcile）

设计前提见 docs/v3-design.md。v2.x 信号层至今仍处于 observe 模式，
T_BUY/T_SELL 等动作须过 1m 回测门槛才恢复输出。
"""

__version__ = "3.0.0"
