#!/usr/bin/env python3
"""Verify historical source-time math against the versioned preclose estimator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi_bot import CLIError, _write_report
from kalshi_preclose_cli_common import binding, existing_input, output_plan, read_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--history-archive', metavar='ASSET=PATH', required=True)
    result.add_argument('--features-input', type=Path,
                        help='Optional causal historical feature cache, verified against the same native archive')
    result.add_argument('--max-rows', type=int, default=200000)
    result.add_argument('--report', type=Path, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from kalshi.preclose_equivalence import verify_preclose_equivalence
        asset, archive = binding(args.history_archive)
        features = None if args.features_input is None else existing_input(args.features_input)
        destination = output_plan({'report': (args.report, 'kalshi_preclose_runtime_equivalence_report')},
                                  [archive, *([] if features is None else [features])])['report']
        if not 1 <= args.max_rows <= 200000:
            raise CLIError('Use a positive preclose row bound up to 200000')
        dataset = None if features is None else read_json(features)
        report = verify_preclose_equivalence({asset: archive}, feature_rows=dataset, max_rows=args.max_rows)
        if (not isinstance(report, dict) or report.get('kind') != 'kalshi_preclose_runtime_equivalence_report'
                or report.get('execution_eligible') is not False):
            raise CLIError('Preclose equivalence did not produce its declared report')
        _write_report(destination, report)
        print(json.dumps(dict(kind=report['kind'], status=report.get('status'), report=str(destination),
                              asset=asset, execution_eligible=False), allow_nan=False))
        return 0 if report.get('status') == 'equivalent' else 2
    except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
        message = str(error) if isinstance(error, CLIError) else 'Native preclose source evidence is unavailable or incompatible'
        print('Error: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
