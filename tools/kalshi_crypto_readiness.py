#!/usr/bin/env python3
"""Measure sustained crypto history and latency on the trading REST observer.

Offline configuration is the default. --run collects public contracts/books
and authenticated CF values only; no account reads or orders are available.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient, KalshiError
from kalshi.crypto_assets import ASSETS
from kalshi.crypto_readiness import MeasuredReadClient, ReadinessError, ReadinessProbe
from kalshi.engine import RiskConfig
from kalshi.fitted_parameters import load_fitted_parameters
from kalshi.forecast import ForecastConfig
from kalshi.minute_five import MinuteFiveObservationService, minute_five_forecast_config, minute_five_model_definition
from kalshi.shadow import ShadowArchive
from kalshi.regime import RegimeConfig


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--show-config', action='store_true')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--state-dir', type=Path)
    result.add_argument('--report', type=Path)
    result.add_argument('--stop-file', type=Path)
    result.add_argument('--probability-fit', action='append', type=Path, default=[],
        help='Repeat for independently fitted assets; artifact asset identity determines its binding')
    result.add_argument('--history-seconds', type=int, default=1800)
    result.add_argument('--min-history-seconds', type=int, default=300)
    result.add_argument('--ready-seconds', type=float, default=300)
    result.add_argument('--max-ready-gap-seconds', type=float, default=15)
    result.add_argument('--interval', type=float, default=1)
    result.add_argument('--iterations', type=int, default=3600,
        help='Maximum serial four-asset rounds; intervals do not include acquisition time')
    result.add_argument('--duration-seconds', type=float,
        help='Optional wall-clock bound checked between complete rounds; current reads may finish after it')
    result.add_argument('--timeout', type=float, default=1)
    result.add_argument('--paths', type=int, default=1000)
    result.add_argument('--check-regime', action='store_true',
        help='Require usable asset-specific default regime classification inputs as well as probability observations')
    return result


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def _configuration(args):
    assets = tuple(args.asset or ASSETS)
    if len(assets) != len(set(assets)):
        raise CLIError('Select each crypto asset only once')
    if (not math.isfinite(args.interval) or not 0 <= args.interval <= 60
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3
            or not 1 <= args.iterations <= 1000000
            or not math.isfinite(args.ready_seconds) or not 0 <= args.ready_seconds <= 86400
            or not math.isfinite(args.max_ready_gap_seconds) or not 0 < args.max_ready_gap_seconds <= 15
            or args.duration_seconds is not None and (
                not math.isfinite(args.duration_seconds) or not 1 <= args.duration_seconds <= 86400)):
        raise CLIError('Use finite bounded intervals, readiness coverage, timeouts and iterations')
    base = ForecastConfig(paths=args.paths, max_history_seconds=args.history_seconds,
        min_history_seconds=args.min_history_seconds)
    state = _private_path(args.state_dir or Path.home() / '.local/state/dts-kalshi/crypto-readiness-v1')
    if state.exists() and not state.is_dir():
        raise CLIError('Readiness state requires a dedicated directory')
    report = _private_path(args.report or state / 'readiness.json')
    if report.suffix.lower() != '.json' or report == state or report in state.parents:
        raise CLIError('Use a separate JSON readiness report')
    if report.exists():
        if not report.is_file() or report.stat().st_nlink != 1 or report.stat().st_size > 4000000:
            raise CLIError('Readiness report must be a bounded regular file')
        try:
            previous = json.loads(report.read_text(encoding='utf-8'))
            if previous.get('kind') != 'kalshi_crypto_readiness' or previous.get('execution_eligible') is not False:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise CLIError('The report target contains unrelated state') from None
    stop = None if args.stop_file is None else _private_path(args.stop_file)
    if stop is not None and (_overlap(stop, report) or stop == state or stop in state.parents
            or stop.exists() and not stop.is_file()):
        raise CLIError('Readiness stop file must be separate from outputs')
    fits = {}
    inputs = []
    for path in args.probability_fit:
        path = _private_path(path)
        if _overlap(path, report) or path == state or path in state.parents:
            raise CLIError('A probability fit cannot overlap readiness outputs')
        fit = load_fitted_parameters(path)
        if fit.asset not in assets or fit.asset in fits:
            raise CLIError('Select one probability fit per selected asset')
        fits[fit.asset] = fit
        inputs.append(path)
    configs, definitions, archives = {}, {}, {}
    for asset in assets:
        configs[asset] = minute_five_forecast_config(base, asset=asset, probability_fit=fits.get(asset))
        definitions[asset] = minute_five_model_definition(configs[asset], asset=asset, probability_fit=fits.get(asset))
        digest = hashlib.sha256(json.dumps(definitions[asset], sort_keys=True).encode()).hexdigest()[:20]
        archives[asset] = _private_path(state / 'archives' / asset / (digest + '.sqlite3'))
    if args.check_regime and any(config.max_history_seconds < RegimeConfig().lookback_seconds for config in configs.values()):
        raise CLIError('Regime checks require at least the configured 1800-second lookback for every asset')
    outputs = [report, *archives.values()]
    for first in outputs:
        for second in inputs + ([] if stop is None else [stop]):
            if _overlap(first, second) or str(second) in tuple(str(first) + suffix for suffix in ('-wal', '-shm', '-journal')):
                raise CLIError('Readiness outputs must not overlap fit inputs or stop files')
        if any(first != second and _overlap(first, second) for second in outputs):
            raise CLIError('Readiness output paths overlap')
    plan = dict(kind='kalshi_crypto_readiness_configuration', version=1, mode='read_only',
        execution_eligible=False, assets=list(assets), interval_seconds=args.interval,
        iterations=args.iterations, timeout_seconds=args.timeout,
        duration_seconds=args.duration_seconds,
        duration_bound_scope='Checked between complete rounds; in-flight reads are not interrupted',
        required_ready_seconds=args.ready_seconds, max_ready_gap_seconds=args.max_ready_gap_seconds,
        state_dir=str(state), report=str(report),
        models={asset: dict(forecast=asdict(configs[asset]), model_definition=definitions[asset],
            fitted=asset in fits, archive=str(archives[asset])) for asset in assets},
        acquisition_path='MinuteFiveObservationService: CF REST and venue REST books',
        regime_check_enabled=args.check_regime,
        regime_config=RegimeConfig().to_dict() if args.check_regime else None,
        reason='Offline plan only; --run measures observations without account or order access')
    return plan, assets, fits, configs, definitions, archives, report, stop


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        plan, assets, fits, configs, definitions, archives, report, stop = _configuration(args)
        if not args.run:
            print(json.dumps(plan, indent=2, allow_nan=False))
            return 0
        key, pem = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
        if not key or not pem:
            raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for authenticated reference-data reads')
        if stop is not None and stop.exists():
            print('Readiness stop file found; no collection started.')
            return 0
        with ExitStack() as resources:
            client = resources.enter_context(KalshiClient(environment='production', key_id=key,
                private_key_path=pem, allow_writes=False, timeout=args.timeout))
            reader = MeasuredReadClient(client)
            opened, observers = {}, {}
            risk = RiskConfig(required_series=None, max_signal_age_seconds=5, max_quote_age_seconds=5)
            for asset in assets:
                opened[asset] = resources.enter_context(ShadowArchive(archives[asset], configs[asset],
                    model_definition=definitions[asset], asset=asset))
                observers[asset] = MinuteFiveObservationService(reader, opened[asset], risk,
                    assets=assets, asset=asset, config=configs[asset], probability_fit=fits.get(asset))
            probe = ReadinessProbe(reader, observers, opened, configs, ready_seconds=args.ready_seconds,
                max_ready_gap_seconds=args.max_ready_gap_seconds,
                regime_config=RegimeConfig() if args.check_regime else None)
            deadline = None if args.duration_seconds is None else time.monotonic() + args.duration_seconds
            for iteration in range(args.iterations):
                if stop is not None and stop.exists() or deadline is not None and time.monotonic() >= deadline:
                    break
                snapshot = probe.sample_round()
                _write_report(report, snapshot)
                print(json.dumps(dict(iteration=snapshot['iteration'], status=snapshot['status'],
                    sustained_observer_ready=snapshot['sustained_observer_ready'],
                    all_assets_consecutive_ready_seconds=snapshot['all_assets_consecutive_ready_seconds'],
                    assets={asset: dict(ready=row['observation_ready_at_publication'],
                        history_seconds=row['history']['span_seconds'],
                        required_history_seconds=row['history']['required_seconds'],
                        regime=None if row['regime'] is None else dict(data_ready=row['regime']['data_ready'],
                            classification_status=row['regime']['classification_status'], label=row['regime']['regime']),
                        source_age_seconds=row['source_age_at_publication_seconds'],
                        quote_age_seconds=row['quote_age_at_publication_seconds']) for asset, row in snapshot['assets'].items()}),
                    allow_nan=False), flush=True)
                if iteration + 1 < args.iterations:
                    remaining = args.interval if deadline is None else min(args.interval, max(0, deadline - time.monotonic()))
                    time.sleep(remaining)
        return 0
    except KeyboardInterrupt:
        print('Readiness collection stopped; latest completed report retained.')
        return 0
    except (CLIError, ReadinessError, KalshiError) as error:
        print('Crypto readiness stopped: ' + str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError):
        print('Crypto readiness stopped: invalid configuration or unavailable reference-data inputs; no orders submitted.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
