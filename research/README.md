# Local Research Evidence

This directory is the lightweight knowledge layer for deterministic backtests and forward experiments.

- Raw 1m bars stay in Parquet under `data/`.
- Each run writes immutable config, dataset manifest, metrics, trades, report and hashes under `research/runs/<run-id>/`.
- `research/index.json` is the machine-readable catalog.
- `research/index.md` is the human/LLM-readable experiment index.
- Generated runs and indexes are local and ignored by Git; this README and the generating code are versioned.

Example:

```bash
python -m triple_resonance.research.catalog_cli \
  --symbols SPY QQQ DIA IWM NVDA AAPL MSFT AMZN META \
  --feed iex --start 2026-08-02 --end 2026-09-01

python -m triple_resonance.research.run \
  --provider massive --feed sip \
  --symbols QQQ DIA IWM NVDA AAPL MSFT AMZN META --benchmark SPY \
  --start 2026-03-09 --end 2026-09-09 \
  --require-spy-persistence --run-id massive-six-month-nine-symbols

python -m triple_resonance.research.run \
  --symbols NVDA \
  --benchmark SPY \
  --feed iex \
  --start 2026-08-02 \
  --end 2026-09-01 \
  --capital-per-symbol 10000 \
  --run-id 2026-08-nvda-30d
```

Do not use the Markdown index as a calculation source. An LLM may retrieve it to find a run, then must use the referenced structured artifacts or rerun the deterministic engine.

Verify a preserved run before relying on it:

```bash
python -m triple_resonance.research.verify research/runs/2026-08-nvda-30d
```

Run all nine watched symbols on Yahoo's separately cataloged recent 8-day 1m window:

```bash
python -m triple_resonance.research.yahoo_run \
  --symbols QQQ DIA IWM NVDA AAPL MSFT AMZN META \
  --benchmark SPY \
  --run-id yahoo-recent-8d-nine-symbols \
  --require-spy-persistence
```

The same command accepts `--snapshot-dir <previous-run>/data` to rerun a preserved Yahoo window without downloading mutable current data again.
