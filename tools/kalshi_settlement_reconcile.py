#!/usr/bin/env python3
"""Audit original crypto settlements against predeclared native sample grids."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS
from kalshi.settlement_reconciliation import demo_reconciliation,reconcile_settlements
from kalshi_bot import CLIError,_private_path,_write_report
from kalshi_stochastic_research import _load_input


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--history-archive',action='append',default=[],metavar='ASSET=PATH')
    source.add_argument('--demo',action='store_true')
    result.add_argument('--input',type=Path,action='append',default=[],help='Optional original extracted feature JSON; repeat per file')
    result.add_argument('--asset',action='append',choices=tuple(ASSETS))
    result.add_argument('--maximum-details',type=int,default=100)
    result.add_argument('--report',type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        bindings = {}
        for value in args.history_archive:
            asset,separator,path = value.partition('=')
            if not separator or asset not in ASSETS or not path or asset in bindings:
                raise CLIError('Supply one unique supported ASSET=PATH per archive')
            bindings[asset] = _private_path(Path(path))
        inputs = [_private_path(path) for path in args.input]
        if args.demo and (inputs or args.asset):
            raise CLIError('Synthetic demonstration does not read input features or select real assets')
        target = None if args.report is None else _private_path(args.report)
        reserved = {Path(str(path)+suffix) for path in [*bindings.values(),*inputs] for suffix in ('','-wal','-shm','-journal')}
        if target in reserved:
            raise CLIError('Audit output must differ from every input and SQLite sidecar')
        if target is not None and target.exists():
            with target.open('rb') as stream:
                previous = stream.read(64000001)
            decoded = json.loads(previous)
            if len(previous)>64000000 or not isinstance(decoded,dict) or decoded.get('kind')!='kalshi_settlement_reconciliation_report' or decoded.get('version')!=1:
                raise CLIError('Refusing to replace an unrelated output file')
        rows = []
        for path in inputs:
            rows.extend(_load_input(path,200000)['rows'])
            if len(rows)>200000:
                raise CLIError('Combined feature references exceed 200000 rows')
        report = demo_reconciliation() if args.demo else reconcile_settlements(bindings,
            feature_rows=rows or None,assets=args.asset,maximum_details=args.maximum_details)
        report['created_at'] = datetime.now(timezone.utc).isoformat()
        if target is not None:
            _write_report(target,report)
        print(json.dumps(report,indent=2,allow_nan=False))
        return 0
    except (ValueError,TypeError,OSError,KeyError) as error:
        print('Settlement reconciliation: '+str(error),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
