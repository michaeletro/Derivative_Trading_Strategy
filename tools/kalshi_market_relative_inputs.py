#!/usr/bin/env python3
"""Reconstruct an offline Gaussian-versus-market dataset from recorded evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from kalshi.market_relative_inputs import build_market_relative_dataset
from kalshi_bot import CLIError, _private_path, _write_report


MAX_INPUT_BYTES = 64_000_000


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CLIError('Input JSON contains duplicate fields')
        result[key] = value
    return result


def _load(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise CLIError('Outcome input exceeds 64 MB')
    value = json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Input JSON must be finite')))
    return value, hashlib.sha256(raw).hexdigest()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--recorded-archive', action='append', required=True, metavar='ASSET=PATH')
    result.add_argument('--outcome-input', type=Path,
        help='Existing normalized recorded/official-outcome records; original availability clocks are retained')
    result.add_argument('--sample-seconds', type=int, default=30,
        help='Keep the first recorded forecast per contract/source-time bucket')
    result.add_argument('--max-rows', type=int, default=200000)
    result.add_argument('--output', type=Path, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        bindings = {}
        for supplied in args.recorded_archive:
            asset, separator, location = supplied.partition('=')
            if not separator or asset not in ('BTC', 'ETH', 'SOL', 'XRP') or not location or asset in bindings:
                raise CLIError('Supply one unique supported ASSET=PATH binding per asset')
            bindings[asset] = _private_path(Path(location))
        if not 1 <= args.sample_seconds <= 900 or not 1 <= args.max_rows <= 200000:
            raise CLIError('Sampling and row bounds are unsupported')
        source = None if args.outcome_input is None else _private_path(args.outcome_input)
        target = _private_path(args.output)
        inputs = set(bindings.values()) | (set() if source is None else {source})
        reserved = {Path(str(path)+suffix) for path in inputs for suffix in ('', '-wal', '-shm', '-journal')}
        if target in reserved:
            raise CLIError('Output must differ from all inputs and SQLite sidecars')
        if target.exists():
            old, _ = _load(target)
            if (not isinstance(old, dict) or old.get('kind') != 'kalshi_market_relative_dataset'
                    or type(old.get('version')) is not int or old['version'] != 1):
                raise CLIError('Refusing to replace an unrelated output')
        outcomes, input_digest = [], None
        if source is not None:
            existing, input_digest = _load(source)
            if (not isinstance(existing, dict) or type(existing.get('version')) is not int
                    or existing['version'] != 1 or not isinstance(existing.get('records'), list)
                    or len(existing['records']) > 200000):
                raise CLIError('Outcome input requires a bounded version-one records envelope')
            for record in existing['records']:
                if not isinstance(record, dict):
                    raise CLIError('Outcome records must be JSON objects')
                if record.get('kind') in ('kalshi_scheduled_settlement', 'kalshi_value_settlement'):
                    outcomes.append(dict(record, synthetic=True) if existing.get('synthetic') is True else record)
        dataset = build_market_relative_dataset(bindings, outcome_records=outcomes,
            sample_seconds=args.sample_seconds, max_rows=args.max_rows)
        if source is not None:
            dataset['external_outcome_input'] = dict(path=str(source), sha256=input_digest,
                selection='settlement_records_only_original_availability_preserved')
        _write_report(target, dataset)
        print(json.dumps(dict(kind=dataset['kind'], version=dataset['version'],
            rows=len(dataset['rows']), synthetic=dataset['synthetic'],
            coverage=dataset.get('coverage'), output=str(target)), indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        print('Market-relative inputs: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
