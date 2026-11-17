#!/usr/bin/env python3
"""Export preserved offline snapshots as one auditable wide table per day."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "liquidity_aware_hedging"))

from orderbook import daily_snapshot_matrix
from orderbook.python_data import DataError
from orderbook.slope_chronology import load_chronological_cache
from orderbook.daily_snapshot_matrix import (
    build_daily_snapshot_matrices, write_daily_snapshot_matrices,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True,
                        help="Existing complete, unchanged chronological cache")
    parser.add_argument("--output", type=Path, required=True,
                        help="New output directory; existing directories are preserved")
    args = parser.parse_args(argv)
    try:
        if args.output.expanduser().exists():
            raise DataError("Choose a new output directory; existing data is preserved")
        source = load_chronological_cache(args.cache)
        meta = source["metadata"]
        config = meta["config"]
        identity = {
            "chronology_fingerprint": meta["fingerprint"],
            "source_fingerprint": config["source_fingerprint"],
            "cache_manifest_sha256": hashlib.sha256(
                (source["cache_dir"] / "cache.json").read_bytes()).hexdigest(),
            "cache_artifacts": meta["artifacts"],
            "verification": "Existing loader verified cache bytes and hashes, frozen "
                            "input stat identities, and replay implementation hashes",
            "levels": config["levels"],
            "step_seconds": config["step_seconds"],
            "max_side_age_seconds": config["max_side_age_seconds"],
            "raw_callbacks_in_matrix": False,
            "matrix_transform_sha256": hashlib.sha256(
                Path(daily_snapshot_matrix.__file__).read_bytes()).hexdigest(),
            "export_command_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        data = build_daily_snapshot_matrices(
            source["frames"], source["sessions"],
            levels=config["levels"], step_seconds=config["step_seconds"],
            source_identity=identity)
        result = write_daily_snapshot_matrices(data, args.output)
    except (DataError, OSError, ValueError) as error:
        print("Daily snapshot export failed: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"status": "completed", "output": str(args.output.expanduser().resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
