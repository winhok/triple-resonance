"""本地状态库（P0 起步：SQLite，无外部依赖）。

为什么需要（用户 P0 复审，从"脚本"到"交易系统"的分水岭）：
  程序崩了 / 网络断了 / 重启，必须能恢复——
    现在有多少 T 仓？有没有挂单？stop order 在不在？今天做了几次 T？

  startup: local state + broker positions + open orders → reconcile
  不一致 → BLOCK_TRADING（不自动猜）。

表：sessions / positions / orders / fills / t_trades / events。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..domain.models import TBucket, Fill


class ReconcileStatus(Enum):
    OK = "OK"
    BLOCK_TRADING = "BLOCK_TRADING"


@dataclass
class ReconcileResult:
    status: ReconcileStatus
    reasons: list[str]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    trading_date     TEXT PRIMARY KEY,
    open_at          TEXT,
    close_at         TEXT,
    force_flatten_at TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol           TEXT PRIMARY KEY,
    base_shares      INT,
    base_cost        REAL,
    t_max_shares     INT,
    t_shares         INT,
    t_avg_cost       REAL,
    t_stop_px        REAL,
    t_entry_ts       TEXT,
    daily_t_pnl      REAL,
    cumulative_t_pnl REAL,
    round_trips_today INT,
    t_flattened      INT,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id         TEXT PRIMARY KEY,
    symbol     TEXT,
    side       TEXT,
    qty        INT,
    status     TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id       TEXT PRIMARY KEY,
    order_id TEXT,
    symbol   TEXT,
    side     TEXT,
    qty      INT,
    price    REAL,
    ts       TEXT
);
CREATE TABLE IF NOT EXISTS t_trades (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT,
    ts      TEXT,
    side    TEXT,
    qty     INT,
    price   REAL,
    realized REAL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    type    TEXT,
    payload TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ positions

    def save_position(self, s: TBucket) -> None:
        self._conn.execute(
            """INSERT INTO positions
               (symbol, base_shares, base_cost, t_max_shares, t_shares, t_avg_cost,
                t_stop_px, t_entry_ts, daily_t_pnl, cumulative_t_pnl,
                round_trips_today, t_flattened, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
               base_shares=excluded.base_shares, base_cost=excluded.base_cost,
               t_max_shares=excluded.t_max_shares, t_shares=excluded.t_shares,
               t_avg_cost=excluded.t_avg_cost, t_stop_px=excluded.t_stop_px,
               t_entry_ts=excluded.t_entry_ts, daily_t_pnl=excluded.daily_t_pnl,
               cumulative_t_pnl=excluded.cumulative_t_pnl,
               round_trips_today=excluded.round_trips_today,
               t_flattened=excluded.t_flattened, updated_at=excluded.updated_at""",
            (s.symbol, s.base_shares, s.base_cost, s.t_max_shares, s.t_shares,
             s.t_avg_cost, s.t_stop_px, s.t_entry_ts, s.daily_t_pnl,
             s.cumulative_t_pnl, s.round_trips_today, 1 if s.t_flattened else 0,
             _now()),
        )
        self._conn.commit()

    def load_position(self, symbol: str) -> Optional[TBucket]:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE symbol=?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return TBucket(
            symbol=row["symbol"],
            base_shares=row["base_shares"],
            base_cost=row["base_cost"],
            t_max_shares=row["t_max_shares"],
            t_shares=row["t_shares"],
            t_avg_cost=row["t_avg_cost"],
            t_stop_px=row["t_stop_px"],
            t_entry_ts=row["t_entry_ts"],
            daily_t_pnl=row["daily_t_pnl"],
            cumulative_t_pnl=row["cumulative_t_pnl"],
            round_trips_today=row["round_trips_today"],
            t_flattened=bool(row["t_flattened"]),
        )

    # ------------------------------------------------------------ orders / fills

    def record_order(self, id: str, symbol: str, side: str, qty: int,
                     status: str = "open") -> None:
        self._conn.execute(
            """INSERT INTO orders (id, symbol, side, qty, status, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
               symbol=excluded.symbol, side=excluded.side, qty=excluded.qty,
               status=excluded.status, created_at=excluded.created_at""",
            (id, symbol, side, qty, status, _now()),
        )
        self._conn.commit()

    def record_fill(self, f: Fill) -> None:
        self._conn.execute(
            "INSERT INTO fills (id, order_id, symbol, side, qty, price, ts) VALUES (?,?,?,?,?,?,?)",
            (f.id, f.order_id, f.symbol, f.side, f.qty, f.price, f.ts),
        )
        self._conn.commit()

    def open_orders(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM orders WHERE status='open'"
        ).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------ t_trades / events

    def record_t_trade(self, symbol: str, ts: str, side: str, qty: int,
                       price: float, realized: float = 0.0) -> None:
        self._conn.execute(
            "INSERT INTO t_trades (symbol, ts, side, qty, price, realized) VALUES (?,?,?,?,?,?)",
            (symbol, ts, side, qty, price, realized),
        )
        self._conn.commit()

    def append_event(self, etype: str, payload: str, ts: Optional[str] = None) -> None:
        self._conn.execute(
            "INSERT INTO events (ts, type, payload) VALUES (?,?,?)",
            (ts or _now(), etype, payload),
        )
        self._conn.commit()

    # ------------------------------------------------------------ sessions

    def save_session(self, ms) -> None:
        self._conn.execute(
            """INSERT INTO sessions (trading_date, open_at, close_at, force_flatten_at)
               VALUES (?,?,?,?)
               ON CONFLICT(trading_date) DO UPDATE SET
               open_at=excluded.open_at, close_at=excluded.close_at,
               force_flatten_at=excluded.force_flatten_at""",
            (ms.trading_date.isoformat(), ms.open_at.isoformat(),
             ms.close_at.isoformat(), ms.force_flatten_at.isoformat()),
        )
        self._conn.commit()

    # ------------------------------------------------------------ reconcile

    def reconcile(self, broker_positions: dict[str, int],
                  open_orders: Optional[list[str]] = None) -> ReconcileResult:
        """启动对账：本地状态 vs 券商回报 + 挂单。

        broker_positions : {symbol: 券商报告的该标的总股数}
        open_orders       : 券商侧当前挂单 id 列表
        任一 symbol 本地总股数 != 券商总股数 → BLOCK_TRADING；
        存在未确认挂单 → 保守 BLOCK_TRADING。
        """
        reasons: list[str] = []
        local_rows = self._conn.execute(
            "SELECT symbol, base_shares, t_shares FROM positions"
        ).fetchall()
        local = {r["symbol"]: r["base_shares"] + r["t_shares"] for r in local_rows}

        for sym in set(local) | set(broker_positions):
            local_total = local.get(sym, 0)
            broker_total = broker_positions.get(sym, 0)
            if local_total != broker_total:
                reasons.append(
                    f"{sym}: local_total={local_total} != broker_total={broker_total}"
                )

        pending = list(open_orders or []) + self.open_orders()
        if pending:
            reasons.append(f"{len(pending)} 个挂单未确认：{pending}")

        status = ReconcileStatus.BLOCK_TRADING if reasons else ReconcileStatus.OK
        self.append_event(
            "reconcile",
            status.value + (("; " + "; ".join(reasons)) if reasons else ""),
        )
        return ReconcileResult(status, reasons)
