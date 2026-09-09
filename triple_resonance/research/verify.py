"""Verify an immutable local experiment bundle."""
from __future__ import annotations

import argparse
import json

from .catalog import verify_experiment


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify research run hashes and snapshots")
    parser.add_argument("run_dir")
    args = parser.parse_args(argv)
    result = verify_experiment(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
