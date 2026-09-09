"""Evidence catalog: structured data stays calculable; Markdown stays readable."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..assistant.calendar import Calendar, session_day


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2,
                      allow_nan=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_dataset_manifest(store, *, feed, symbols, start: date, end: date):
    calendar = Calendar()
    coverage, file_hashes = {}, {}
    for symbol in sorted(set(symbols)):
        bars = [bar for bar in store.read_bars(symbol, feed)
                if start <= session_day(bar.ts) < end]
        by_day = {}
        for bar in bars:
            by_day.setdefault(session_day(bar.ts), []).append(bar)
        complete, incomplete = [], {}
        for day in sorted(by_day):
            session = calendar.session_for(day)
            if session is None:
                continue
            values = sorted([bar for bar in by_day[day]
                             if session.open_at <= bar.ts < session.close_at], key=lambda bar: bar.ts)
            expected = int((session.close_at - session.open_at).total_seconds() // 60)
            expected_ts = [session.open_at + timedelta(minutes=i) for i in range(expected)]
            if [bar.ts for bar in values] == expected_ts:
                complete.append(str(day))
            else:
                incomplete[str(day)] = {"rows": len(values), "expected": expected,
                                        "unique_timestamps": len({bar.ts for bar in values})}
        coverage[symbol] = {
            "rows": len(bars),
            "selected_rows_hash": digest([
                {"ts": bar.ts.isoformat(), "open": bar.open, "high": bar.high,
                 "low": bar.low, "close": bar.close, "volume": bar.volume,
                 "vwap": bar.vwap}
                for bar in bars
            ]),
            "first_ts": bars[0].ts.isoformat() if bars else None,
            "last_ts": bars[-1].ts.isoformat() if bars else None,
            "complete_sessions": complete,
            "incomplete_sessions": incomplete,
            "status": "READY" if bars else "DATASET_MISSING",
        }
        directory = Path(store.root) / "alpaca" / feed / symbol
        for path in sorted(directory.glob("*.parquet")):
            with path.open("rb") as handle:
                file_hashes[str(path)] = hashlib.file_digest(handle, "sha256").hexdigest()
    body = {
        "schema": 1, "provider": store.provider, "feed": feed, "timeframe": "1m",
        "request_start": str(start), "request_end": str(end), "end_semantics": "exclusive",
        "symbols": sorted(set(symbols)), "coverage": coverage,
        "file_hashes": file_hashes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    body["dataset_hash"] = digest({key: body[key] for key in
                                   ("provider", "feed", "timeframe", "request_start",
                                    "request_end", "symbols", "coverage", "file_hashes")})
    return body


def build_memory_dataset_manifest(bars_by_symbol, *, provider, feed, start: date,
                                  end: date, vwap_kind):
    calendar = Calendar()
    coverage = {}
    for symbol in sorted(bars_by_symbol):
        bars = sorted([bar for bar in bars_by_symbol[symbol]
                       if start <= session_day(bar.ts) < end], key=lambda bar: bar.ts)
        by_day = {}
        for bar in bars:
            by_day.setdefault(session_day(bar.ts), []).append(bar)
        complete, incomplete = [], {}
        for day, values in sorted(by_day.items()):
            session = calendar.session_for(day)
            if session is None:
                continue
            values = [bar for bar in values if session.open_at <= bar.ts < session.close_at]
            expected = int((session.close_at - session.open_at).total_seconds() // 60)
            expected_ts = [session.open_at + timedelta(minutes=i) for i in range(expected)]
            if [bar.ts for bar in values] == expected_ts:
                complete.append(str(day))
            else:
                incomplete[str(day)] = {"rows": len(values), "expected": expected,
                                        "unique_timestamps": len({bar.ts for bar in values})}
        coverage[symbol] = {
            "rows": len(bars),
            "selected_rows_hash": digest([
                {"ts": bar.ts.isoformat(), "open": bar.open, "high": bar.high,
                 "low": bar.low, "close": bar.close, "volume": bar.volume,
                 "vwap": bar.vwap} for bar in bars]),
            "first_ts": bars[0].ts.isoformat() if bars else None,
            "last_ts": bars[-1].ts.isoformat() if bars else None,
            "complete_sessions": complete, "incomplete_sessions": incomplete,
            "status": "READY" if bars else "DATASET_MISSING",
        }
    body = {"schema": 1, "provider": provider, "feed": feed, "timeframe": "1m",
            "vwap_kind": vwap_kind, "request_start": str(start),
            "request_end": str(end), "end_semantics": "exclusive",
            "symbols": sorted(bars_by_symbol), "coverage": coverage,
            "created_at": datetime.now(timezone.utc).isoformat()}
    body["dataset_hash"] = digest({key: body[key] for key in
                                   ("provider", "feed", "timeframe", "vwap_kind",
                                    "request_start", "request_end", "symbols", "coverage")})
    return body


def _write_snapshot(path, bars):
    import pyarrow as pa
    import pyarrow.parquet as pq
    schema = pa.schema([("ts", pa.timestamp("us", tz="UTC"))] +
                       [(name, pa.float64()) for name in
                        ("open", "high", "low", "close", "volume", "vwap")])
    rows = [{"ts": bar.ts, "open": bar.open, "high": bar.high, "low": bar.low,
             "close": bar.close, "volume": bar.volume, "vwap": bar.vwap}
            for bar in bars]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_experiment(root, *, run_id, config, dataset, metrics, trades, report,
                     snapshots=None):
    root = Path(root)
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot_files = {}
    if snapshots:
        snapshot_dir = run_dir / "data"
        snapshot_dir.mkdir()
        for symbol, bars in sorted(snapshots.items()):
            path = snapshot_dir / f"{symbol}.parquet"
            _write_snapshot(path, bars)
            with path.open("rb") as handle:
                snapshot_files[symbol] = {"path": str(path.relative_to(run_dir)),
                                          "rows": len(bars),
                                          "sha256": hashlib.file_digest(handle, "sha256").hexdigest()}
    dataset = {**dataset, "snapshot_files": snapshot_files}
    atomic_json(run_dir / "config.json", config)
    atomic_json(run_dir / "dataset-manifest.json", dataset)
    atomic_json(run_dir / "metrics.json", metrics)
    atomic_json(run_dir / "trades.json", trades)
    report = report.rstrip() + "\n"
    atomic_text(run_dir / "report.md", report)
    manifest = {
        "schema": 1, "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy": config["strategy"], "strategy_status": config["strategy_status"],
        "git_sha": config["git_sha"], "dataset_hash": dataset["dataset_hash"],
        "config_hash": digest(config), "metrics_hash": digest(metrics),
        "trades_hash": digest(trades), "report_hash": hashlib.sha256(report.encode()).hexdigest(),
        "snapshot_hashes": {symbol: item["sha256"]
                            for symbol, item in snapshot_files.items()},
    }
    manifest["run_hash"] = digest(manifest)
    atomic_json(run_dir / "manifest.json", manifest)

    index_path = root / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"schema": 1, "runs": []}
    index["runs"].append({**manifest, "path": str(run_dir),
                          "combined_pnl": metrics["combined"]["pnl"],
                          "usable_sessions": metrics["quality"]["usable_sessions"]})
    atomic_json(index_path, index)
    lines = ["# Experiment Index", ""]
    for item in reversed(index["runs"]):
        lines.extend([f"## {item['run_id']}", "",
                      f"- Strategy: `{item['strategy']}` (`{item['strategy_status']}`)",
                      f"- Git: `{item['git_sha']}`", f"- Dataset: `{item['dataset_hash']}`",
                      f"- Usable sessions: {item['usable_sessions']}",
                      f"- Combined PnL: {item['combined_pnl']:+.2f}",
                      f"- Report: `{item['path']}/report.md`", ""])
    atomic_text(root / "index.md", "\n".join(lines))
    return run_dir, manifest


def verify_experiment(run_dir):
    import pyarrow.parquet as pq
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "config.json").read_text())
    dataset = json.loads((run_dir / "dataset-manifest.json").read_text())
    metrics = json.loads((run_dir / "metrics.json").read_text())
    trades = json.loads((run_dir / "trades.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    checks = {
        "config_hash": digest(config) == manifest["config_hash"],
        "metrics_hash": digest(metrics) == manifest["metrics_hash"],
        "trades_hash": digest(trades) == manifest["trades_hash"],
        "report_hash": hashlib.sha256((run_dir / "report.md").read_bytes()).hexdigest()
                       == manifest["report_hash"],
        "dataset_hash": dataset["dataset_hash"] == manifest["dataset_hash"],
        "run_hash": digest({key: value for key, value in manifest.items()
                            if key != "run_hash"}) == manifest["run_hash"],
    }
    for symbol, info in dataset.get("snapshot_files", {}).items():
        path = run_dir / info["path"]
        with path.open("rb") as handle:
            checks[f"snapshot_bytes:{symbol}"] = (
                hashlib.file_digest(handle, "sha256").hexdigest() == info["sha256"])
        rows = pq.read_table(path).to_pylist()
        semantic = digest([
            {"ts": row["ts"].isoformat(), "open": row["open"], "high": row["high"],
             "low": row["low"], "close": row["close"], "volume": row["volume"],
             "vwap": row["vwap"]}
            for row in rows
        ])
        checks[f"snapshot_rows:{symbol}"] = len(rows) == info["rows"]
        checks[f"snapshot_content:{symbol}"] = (
            semantic == dataset["coverage"][symbol]["selected_rows_hash"])
    return {"verified": all(checks.values()), "checks": checks,
            "run_id": manifest["run_id"], "run_hash": manifest["run_hash"]}
