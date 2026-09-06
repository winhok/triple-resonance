#!/usr/bin/env python3
"""美股日内 T 信号引擎 — 15/60 分钟 K + RSI/MACD/EMA 三共振。

子命令：
  scan <代码...>          扫描信号（可多只）
  quote <代码>            只看当前指标，不给建议
  position add/list/rm    持仓管理

数据：yfinance（与 invest-cli 共用 .venv）。K 线非实盘级，有延迟。

信号基于**已闭合**的最后一根 K 线（防 repainting：盘中正在形成的 K 线
指标会反复变化，与回测口径不一致）。

止损锚定成本价（entry-anchored），不以当前价漂移。
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pandas as pd  # noqa: E402

from indicators import enrich, crossed  # noqa: E402

CACHE_DIR = Path(os.environ.get("INTRADAY_CACHE", "/tmp/invest-intraday"))
CACHE_TTL = 300  # 15m K 线本身 5 分钟一根，缓存 5 分钟足够

POSITIONS_FILE = Path(
    os.environ.get("INTRADAY_POSITIONS", Path(__file__).resolve().parent / "positions.json")
)

DEFAULT_PERIOD = "10d"
DEFAULT_INTERVAL = "15m"
STOP_LOSS_PCT = 0.03  # 固定止损 -3%
ATR_MULT = 1.5        # ATR 止损倍数
CROSS_LOOKBACK = 3    # 金叉/死叉在最近 3 根内算有效


# ---------------------------------------------------------------- 数据层

def _interval_minutes(interval: str) -> int:
    unit, val = interval[-1], interval[:-1]
    n = int(val)
    return n * (60 if unit == "h" else 1)


def drop_forming_bar(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """丢弃尚未闭合的最后一根 K 线（盘中调用时 yfinance 会返回正在形成的 bar）。

    正在形成的 bar 指标会反复变化（repainting），与回测口径不一致，必须丢弃。
    """
    if df.empty:
        return df
    last = df.index[-1]
    try:
        now = pd.Timestamp.now(tz=last.tz)
    except TypeError:
        now = pd.Timestamp.now(tz="UTC")
    if last + pd.Timedelta(minutes=_interval_minutes(interval)) > now:
        df = df.iloc[:-1]
    return df


def smart_period(interval: str) -> str:
    """按 K 线周期选合适的取样长度，保证指标预热充分。"""
    return {"15m": "10d", "30m": "20d", "60m": "60d", "5m": "5d"}.get(interval, "10d")


def fetch(symbol: str, period: str | None = None,
          interval: str = DEFAULT_INTERVAL, use_cache: bool = True) -> pd.DataFrame:
    if period is None:
        period = smart_period(interval)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{symbol.upper()}_{interval}_{period}.pkl"
    if use_cache and cache.exists() and (time.time() - cache.stat().st_mtime) < CACHE_TTL:
        with open(cache, "rb") as f:
            return pickle.load(f)

    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError(
            f"未安装 yfinance，请先：pip install -r requirements.txt ({e})"
        )

    df = yf.Ticker(symbol.upper()).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"{symbol} 取数为空（代码是否存在？是否被券商支持？）")
    cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[cols].dropna()
    # 只保留已闭合的 K 线（盘中调用时丢弃正在形成的最后一根）
    df = drop_forming_bar(df, interval)
    if len(df) < 30:
        raise RuntimeError(f"{symbol} 仅取到 {len(df)} 根已闭合 K 线，不足以计算指标")
    with open(cache, "wb") as f:
        pickle.dump(df, f)
    return df


def _last_ts(df: pd.DataFrame) -> str:
    ts = df.index[-1]
    try:
        ts = ts.tz_convert("America/New_York")
    except (AttributeError, TypeError):
        pass
    return str(ts)[:16]


# ---------------------------------------------------------------- 信号层

def evaluate(df: pd.DataFrame, min_score: int = 3) -> dict:
    """三共振信号。返回方向与逐条条件明细。

    min_score：出信号所需的最少条件数。回测（60m/2年，一次时序切分）中
    门槛 3 表现优于门槛 2（少做=少错），但尚未通过最终锁定测试——
    属于"回测中表现较优的候选配置"，不是"已验证结论"。

    判定对称：bull 与 bear 同分时不产生任何信号（避免多头偏置）。
    """
    d = enrich(df)
    last, prev = d.iloc[-1], d.iloc[-2]

    rsi_now, rsi_prev = float(last["rsi"]), float(prev["rsi"])
    hist = float(last["macd_hist"])

    gold, gold_ago = crossed(d["macd"], d["macd_signal"], CROSS_LOOKBACK, "up")
    dead, dead_ago = crossed(d["macd"], d["macd_signal"], CROSS_LOOKBACK, "down")

    bull_checks = {
        "trend": bool(last["ema_fast"] > last["ema_slow"]),          # EMA9 > EMA21
        "momentum": bool(hist > 0 and gold),                          # MACD线在信号线上方 + 近期金叉
        "rsi_ok": bool((35 <= rsi_now <= 65) or (rsi_prev < 30 <= rsi_now)),
    }
    bear_checks = {
        "trend": bool(last["ema_fast"] < last["ema_slow"]),
        "momentum": bool(hist < 0 and dead),
        # 严格 >：RSI 恰为 65 时只归多头侧，避免同值双侧成立
        "rsi_ok": bool(rsi_now > 65 or (rsi_prev > 70 >= rsi_now)),
    }

    bull_score = sum(bull_checks.values())
    bear_score = sum(bear_checks.values())

    # 对称判定：严格大于；同分一律 hold
    if bull_score >= min_score and bull_score > bear_score:
        side, score, checks = "buy", bull_score, bull_checks
    elif bear_score >= min_score and bear_score > bull_score:
        side, score, checks = "sell", bear_score, bear_checks
    else:
        side = "hold"
        # checks 跟随分数较高的一侧（hold 也要展示正确的方向条件）
        if bull_score >= bear_score:
            score, checks = bull_score, bull_checks
        else:
            score, checks = bear_score, bear_checks

    price = float(last["Close"])
    atr_val = float(last["atr"]) if pd.notna(last["atr"]) else None

    return {
        "price": round(price, 2),
        "as_of": _last_ts(d),
        "bars": len(d),
        "indicators": {
            "rsi": round(rsi_now, 1),
            "macd_hist": round(hist, 3),
            "ema9": round(float(last["ema_fast"]), 2),
            "ema21": round(float(last["ema_slow"]), 2),
            "atr": round(atr_val, 2) if atr_val else None,
            "volume": int(last["Volume"]),
        },
        "signal": {
            "side": side,
            "score": score,
            "strength": "strong" if score == 3 else ("weak" if score == 2 else "none"),
            "checks": checks,
            "cross_detail": {
                "golden_cross": gold, "golden_bars_ago": gold_ago,
                "death_cross": dead, "death_bars_ago": dead_ago,
            },
        },
        "risk": {
            # 无持仓时 = 若现在建仓的参考止损（基于现价）；有持仓时以成本锚定为准（见 advice）
            "stop_loss_pct": round(price * (1 - STOP_LOSS_PCT), 2),
            "stop_loss_atr": round(price - ATR_MULT * atr_val, 2) if atr_val else None,
            "add_ref": round(min(float(last["ema_slow"]), price * 0.995), 2),
        },
    }


# ---------------------------------------------------------------- 持仓层

def load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_positions(data: dict) -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def with_advice(res: dict, pos: dict | None) -> dict:
    """把持仓状态合并成一句可执行的动作建议。

    止损锚定：持仓止损以**成本价**为锚（entry-anchored）——
      固定止损 = cost × (1 - 3%)
      ATR 止损  = cost − 1.5 × 当前ATR（ATR 取当前值；严格版应在建仓时锁定入场 ATR）
    无持仓时展示的是"若现在建仓"的参考止损（基于现价）。
    """
    side, strength = res["signal"]["side"], res["signal"]["strength"]
    price = res["price"]

    if pos:
        shares, cost = pos.get("shares", 0), float(pos.get("cost", 0))
        pnl = (price - cost) / cost if cost else 0.0
        atr_val = res["indicators"]["atr"]
        # 成本锚定的持仓止损（不再跟随现价漂移）
        stop_fixed = cost * (1 - STOP_LOSS_PCT)
        stop_atr = (cost - ATR_MULT * atr_val) if atr_val else None
        stop = stop_atr or stop_fixed
        res["risk"]["position_stop_fixed"] = round(stop_fixed, 2)
        if stop_atr:
            res["risk"]["position_stop_atr"] = round(stop_atr, 2)

        res["position"] = {
            "shares": shares, "cost": round(cost, 2),
            "price": price, "pnl_pct": round(pnl * 100, 2),
            "market_value": round(shares * price, 2),
            "pnl_amount": round((price - cost) * shares, 2),
        }

        if price <= stop:
            res["advice"] = f"触发止损：现价 {price} 已跌破成本锚定止损位 {round(stop,2)}（成本 {cost}），优先离场，不等共振"
            res["action"] = "stop_loss"
        elif side == "buy" and strength == "strong":
            res["advice"] = f"趋势与动量共振，可在 {res['risk']['add_ref']} 附近加仓（现浮盈 {pnl*100:.1f}%）"
            res["action"] = "add"
        elif side == "buy" and strength == "weak":
            res["advice"] = f"两条件成立（{_missing(res)}），建议半仓加或等第三条件；止损位 {round(stop,2)}"
            res["action"] = "add_half"
        elif side == "sell" and strength == "strong":
            res["advice"] = f"三条件转空，建议减仓/清仓，落袋 {pnl*100:.1f}%"
            res["action"] = "reduce"
        elif side == "sell" and strength == "weak":
            res["advice"] = f"部分转空（{_missing(res)}），可减半仓，跌破 {round(stop,2)} 全出"
            res["action"] = "reduce_half"
        else:
            res["advice"] = f"无明确信号，持有观察；止损位 {round(stop,2)}（成本锚定）"
            res["action"] = "hold"
    else:
        res["position"] = None
        # 无持仓时 risk 块是基于现价的"若建仓"参考止损
        if side == "buy" and strength == "strong":
            res["advice"] = f"三共振成立，可建仓，参考止损 {res['risk']['stop_loss_atr']}"
            res["action"] = "open"
        elif side == "buy" and strength == "weak":
            res["advice"] = f"两条件成立（{_missing(res)}），可试仓半仓或等待"
            res["action"] = "open_half"
        elif side == "sell":
            res["advice"] = "当前转空，空仓观望，不宜新开多"
            res["action"] = "wait"
        else:
            res["advice"] = "无信号，观望"
            res["action"] = "wait"
    return res


def _missing(res: dict) -> str:
    names = {"trend": "EMA 排列", "momentum": "MACD", "rsi_ok": "RSI"}
    miss = [names[k] for k, v in res["signal"]["checks"].items() if not v]
    return "缺 " + "、".join(miss) if miss else "全满足"


# ---------------------------------------------------------------- 输出层

_LABEL = {"trend": "EMA9>21", "momentum": "MACD", "rsi_ok": "RSI"}


def format_terminal(res: dict) -> str:
    ind, sig, rk = res["indicators"], res["signal"], res["risk"]
    side_cn = {"buy": "加仓/买入", "sell": "卖出/减仓", "hold": "观望"}[sig["side"]]
    mark = {"strong": "★★★ 强", "weak": "★★☆ 中", "none": "★☆☆ 弱"}[sig["strength"]]

    L = [
        "",
        "=" * 62,
        f"  {res['symbol']}  —  {res.get('interval', '15m')} 信号   {side_cn}  {mark}",
        f"  数据截至 {res['as_of']} 美东  |  共 {res['bars']} 根 K 线",
        "=" * 62,
        f"  现价        {res['price']}",
        f"  RSI(14)     {ind['rsi']}",
        f"  MACD 柱     {ind['macd_hist']}",
        f"  EMA9 / 21   {ind['ema9']} / {ind['ema21']}",
        f"  ATR(14)     {ind['atr']}",
        "",
        "  共振条件",
    ]
    for k, v in sig["checks"].items():
        L.append(f"    [{'x' if v else ' '}] {_LABEL[k]}")
    cd = sig["cross_detail"]
    if cd["golden_cross"]:
        L.append(f"    · MACD 金叉发生在 {cd['golden_bars_ago']} 根前")
    if cd["death_cross"]:
        L.append(f"    · MACD 死叉发生在 {cd['death_bars_ago']} 根前")

    if res.get("position"):
        p = res["position"]
        L += [
            "",
            "  持仓",
            f"    数量 {p['shares']}  成本 {p['cost']}  现值 {p['market_value']}",
            f"    浮盈亏 {p['pnl_amount']:+}（{p['pnl_pct']:+.2f}%）",
        ]

    rk = res["risk"]
    if res.get("position"):
        L += [
            "",
            "  风控（成本锚定）",
            f"    固定止损(-3%)  {rk.get('position_stop_fixed', '—')}",
            f"    ATR 止损(1.5x) {rk.get('position_stop_atr', '—')}",
            f"    加仓参考价     {rk['add_ref']}",
        ]
    else:
        L += [
            "",
            "  风控（若建仓参考）",
            f"    固定止损(-3%)  {rk['stop_loss_pct']}",
            f"    ATR 止损(1.5x) {rk['stop_loss_atr']}",
            f"    加仓参考价     {rk['add_ref']}",
        ]

    L += [
        "",
        f"  结论: {res['advice']}",
        "",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def cmd_scan(args):
    if args.min_score == 2:
        print("⚠️  门槛 2：回测中交易次数约为门槛 3 的 14 倍、单笔质量显著下降，仅建议观察用",
              file=sys.stderr)
    positions = load_positions()
    results = []
    for sym in args.symbols:
        try:
            df = fetch(sym, period=args.period, interval=args.interval,
                       use_cache=not args.no_cache)
            res = evaluate(df, args.min_score)
            res["symbol"] = sym.upper()
            res["interval"] = args.interval
            res = with_advice(res, positions.get(sym.upper()))
            res["ok"] = True
        except Exception as e:
            res = {"ok": False, "symbol": sym.upper(), "error": str(e)}
        results.append(res)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(format_terminal(r) if r["ok"] else f"\n{r['symbol']} 失败: {r['error']}")
    return 0 if all(r["ok"] for r in results) else 1


def cmd_quote(args):
    """只看指标，不给建议——不加载持仓、不产生任何 action。"""
    results = []
    for sym in args.symbols:
        try:
            df = fetch(sym, period=args.period, interval=args.interval,
                       use_cache=not args.no_cache)
            res = evaluate(df, args.min_score)
            res["symbol"] = sym.upper()
            res["interval"] = args.interval
            res["ok"] = True
        except Exception as e:
            res = {"ok": False, "symbol": sym.upper(), "error": str(e)}
        results.append(res)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if not r["ok"]:
                print(f"\n{r['symbol']} 失败: {r['error']}")
                continue
            ind, sig = r["indicators"], r["signal"]
            print(f"\n{r['symbol']}  {r['price']}  ({r['as_of']} 美东, {r['bars']}根已闭合K线)")
            print(f"  RSI(14) {ind['rsi']}  MACD柱 {ind['macd_hist']}  "
                  f"EMA9/21 {ind['ema9']}/{ind['ema21']}  ATR {ind['atr']}")
            print(f"  当前条件计数：多头 {sum(1 for v in sig['checks'].values() if v) if sig['side'] != 'sell' else 0}/3"
                  f"（信号判定见 scan，本命令不给建议）")
    return 0 if all(r["ok"] for r in results) else 1


def cmd_position(args):
    positions = load_positions()
    if args.action == "list":
        if not positions:
            print("（无持仓记录）")
            return 0
        print(f"\n  {'代码':<8}{'数量':>8}{'成本':>12}")
        print("  " + "-" * 30)
        for k, v in positions.items():
            print(f"  {k:<8}{v.get('shares', 0):>8}{float(v.get('cost', 0)):>12.2f}")
        print()
        return 0
    if args.action == "add":
        positions[args.symbol.upper()] = {
            "shares": args.shares, "cost": args.cost,
            "note": args.note or "", "updated": time.strftime("%Y-%m-%d %H:%M"),
        }
        save_positions(positions)
        print(f"已记录 {args.symbol.upper()}：{args.shares} 股 @ {args.cost}")
        return 0
    if args.action == "rm":
        positions.pop(args.symbol.upper(), None)
        save_positions(positions)
        print(f"已删除 {args.symbol.upper()}")
        return 0
    return 1


def main():
    p = argparse.ArgumentParser(description="美股日内 T 信号引擎（15m + RSI/MACD/EMA）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描信号")
    s.add_argument("symbols", nargs="+")
    s.add_argument("--period", default=None,
                   help="默认按 interval 自动选择（15m→10d，60m→60d）")
    s.add_argument("--interval", default=DEFAULT_INTERVAL,
                   help="15m=观察参考（无样本外优势）；60m=回测中表现较优的候选周期")
    s.add_argument("--min-score", type=int, default=3, choices=(2, 3),
                   help="出信号所需条件数，默认 3（回测候选配置；2 会触发交易次数警告）")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true")
    s.set_defaults(func=cmd_scan)

    q = sub.add_parser("quote", help="只看指标，不给建议")
    q.add_argument("symbols", nargs="+")
    q.add_argument("--period", default=None)
    q.add_argument("--interval", default=DEFAULT_INTERVAL)
    q.add_argument("--min-score", type=int, default=3, choices=(2, 3))
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-cache", action="store_true")
    q.set_defaults(func=cmd_quote)

    pos = sub.add_parser("position", help="持仓管理")
    pos_sub = pos.add_subparsers(dest="action", required=True)
    a = pos_sub.add_parser("add")
    a.add_argument("symbol"); a.add_argument("shares", type=int); a.add_argument("cost", type=float)
    a.add_argument("--note", default="")
    pos_sub.add_parser("list")
    r = pos_sub.add_parser("rm"); r.add_argument("symbol")
    pos.set_defaults(func=cmd_position)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
