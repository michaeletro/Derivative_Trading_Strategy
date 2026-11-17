#!/usr/bin/env python3
"""Export model-bound, paper-only uniform and recency calibration artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi_bot import CLIError, _write_report
from kalshi_preclose_cli_common import existing_input, output_plan, read_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--research-report', type=Path, required=True,
                        help='Completed nonsynthetic recency calibration report')
    result.add_argument('--equivalence', type=Path, required=True,
                        help='Native source-bound preclose runtime equivalence report for the same cohort')
    result.add_argument('--asset', choices=('ETH', 'BTC'), default='ETH')
    result.add_argument('--uniform-artifact', type=Path, required=True)
    result.add_argument('--recency-artifact', type=Path, required=True,
                        help='Validation-selected map; may itself select uniform weighting')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from kalshi.preclose_calibration import export_preclose_calibration, load_preclose_calibration
        from kalshi.preclose_equivalence import per_asset_equivalence
        source, evidence = existing_input(args.research_report), existing_input(args.equivalence)
        outputs = output_plan({
            'uniform': (args.uniform_artifact, 'kalshi_preclose_calibration_model'),
            'selected': (args.recency_artifact, 'kalshi_preclose_calibration_model')}, (source, evidence))
        report = read_json(source)
        if not isinstance(report, dict):
            raise CLIError('A completed recency report is required')
        report['runtime_equivalence'] = per_asset_equivalence(read_json(evidence))
        models = {variant: export_preclose_calibration(report, asset=args.asset, variant=variant)
                  for variant in ('uniform', 'selected')}
        # A valid existing artifact must retain its asset and role on replacement.
        for variant, path in outputs.items():
            if path.exists():
                previous = load_preclose_calibration(path, asset=args.asset)
                if previous.variant != variant:
                    raise CLIError('An existing artifact has a different comparison role')
        for variant, path in outputs.items():
            _write_report(path, models[variant])
        print(json.dumps(dict(kind='kalshi_preclose_calibration_export', status='ready',
            asset=args.asset, artifacts={key: str(path) for key, path in outputs.items()},
            execution_eligible=False, live_model_updated=False), allow_nan=False))
        return 0
    except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
        message = str(error) if isinstance(error, CLIError) else 'Preclose fitting evidence or runtime equivalence is incompatible'
        print('Error: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
