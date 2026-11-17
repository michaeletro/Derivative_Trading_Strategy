#!/usr/bin/env python3
"""Isolated crypto value/Kelly capture, paper trading and explicitly armed live use.

Without --run this prints configuration offline. --capture-only records market
evidence without opening a strategy journal. Real orders require --mode live,
--run, --allow-live and a matching empirically qualified calibration artifact.
Select one of BTC, ETH, SOL or XRP. A live journal owns its account inventory;
separate value processes cannot share one account. Concurrent crypto execution
uses the shared-account rolling-Kelly runner.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient, KalshiError
from kalshi.crypto_assets import asset_spec
from kalshi.engine import RiskConfig
from kalshi.fitted_parameters import load_fitted_parameters
from kalshi.forecast import ForecastConfig
from kalshi.minute_five import MinuteFiveObservationService, minute_five_forecast_config, minute_five_model_definition
from kalshi.regime import RegimeConfig
from kalshi.shadow import ShadowArchive
from kalshi.time_sizing import UniformSizing
from kalshi.value_calibration import load_value_calibration, ValueCalibrationError
from kalshi.value_capture import (ValueCapture, ValueCaptureError, ValueCaptureStaleError,
    export_shadow_archive, model_fingerprint)
from kalshi.value_execution import ValueExecutionConfig, ValueExecutor, ValueJournal
from kalshi.value_files import json_output_path
from kalshi.value_sizing import ValueSizingConfig, ValueSizingError


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--asset', choices=('BTC', 'ETH', 'SOL', 'XRP'), default='BTC',
        help='Exactly one settlement asset; model history, calibration and journal stay asset-bound')
    result.add_argument('--mode', choices=('paper', 'live'), default='paper')
    result.add_argument('--run', action='store_true')
    result.add_argument('--allow-live', action='store_true')
    result.add_argument('--paper-raw', action='store_true',
        help='Explicit uncalibrated paper research; never accepted in live mode')
    result.add_argument('--capture-only', action='store_true')
    result.add_argument('--show-config', action='store_true')
    result.add_argument('--export-shadow', type=Path,
        help='Offline, read-only export of an existing forecast archive to a NEW --capture file')
    for name in ('journal', 'archive', 'capture', 'report', 'calibration', 'probability-fit', 'stop-file'):
        result.add_argument('--' + name, type=Path)
    result.add_argument('--bankroll', default='100')
    result.add_argument('--kelly-fraction', default='0.25')
    result.add_argument('--min-edge', default='0.02')
    result.add_argument('--min-trade-improvement', default='0.01',
        help='Minimum cash-equivalent utility improvement after costs, in dollars')
    result.add_argument('--min-log-growth', default='0.000001')
    result.add_argument('--slippage-buffer', default='0')
    result.add_argument('--max-order-cost', default='10')
    result.add_argument('--max-market-risk', default='100')
    result.add_argument('--max-contracts', type=int, default=100)
    result.add_argument('--max-position-contracts', type=int, default=1000)
    result.add_argument('--max-drawdown', default='0.10')
    result.add_argument('--interval', type=float, default=10)
    result.add_argument('--timeout', type=float, default=1)
    result.add_argument('--paths', type=int, default=4000)
    result.add_argument('--history-seconds', type=int)
    result.add_argument('--iterations', type=int)
    return result


def _validate_paths(paths, inputs):
    reserved = {}
    suffixes = {
        'journal': ('', '.lock', '.runner.lock', '-journal', '-wal', '-shm'),
        'archive': ('', '.lock', '-journal', '-wal', '-shm'),
        'capture': ('', '.lock', '.meta.json'),
        'report': ('',),
        'stop_file': ('',),
    }
    for name, path in paths.items():
        if path is None:
            continue
        for suffix in suffixes[name]:
            candidate = _private_path(Path(str(path) + suffix)).resolve()
            if candidate in reserved:
                raise CLIError('Strategy outputs and their lock/SQLite/metadata sidecars must be distinct')
            reserved[candidate] = name
    if set(reserved) & {path.expanduser().resolve() for path in inputs if path is not None}:
        raise CLIError('Strategy outputs and sidecars must be separate from input artifacts')


def _configuration(args):
    if args.mode != 'live' and args.allow_live:
        raise CLIError('--allow-live requires --mode live')
    if args.mode == 'live' and args.paper_raw:
        raise CLIError('Raw probability experiments are paper-only')
    if args.mode == 'live' and args.run and (not args.allow_live or args.calibration is None):
        raise CLIError('Live orders require --run --allow-live and --calibration')
    if args.allow_live and not args.run:
        raise CLIError('--allow-live requires --run')
    if args.capture_only and (args.mode != 'paper' or not args.run):
        raise CLIError('--capture-only requires --mode paper --run')
    if args.show_config and (args.run or args.allow_live or args.export_shadow):
        raise CLIError('--show-config is offline and cannot run or export')
    if args.export_shadow and (args.run or args.allow_live or args.capture_only):
        raise CLIError('Archive export is an offline action')
    if (not math.isfinite(args.interval) or not 1 <= args.interval <= 60
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3):
        raise CLIError('Interval must be 1 through 60 seconds; timeout must be positive and at most 3 seconds')
    if args.iterations is not None and (not args.run or not 1 <= args.iterations <= 1000000):
        raise CLIError('--iterations requires --run and a count from one through 1000000')
    money = Decimal(args.bankroll)
    drawdown = Decimal(args.max_drawdown)
    if (not money.is_finite() or not 0 < money <= 1000000
            or not drawdown.is_finite() or not 0 < drawdown < 1):
        raise CLIError('Bankroll must be positive and bounded; drawdown must be strictly between zero and one')
    sizing = ValueSizingConfig(kelly_fraction=args.kelly_fraction, min_edge=args.min_edge,
        rebalance_hysteresis=args.min_trade_improvement, min_log_growth=args.min_log_growth,
        max_order_cost=args.max_order_cost, max_market_risk=args.max_market_risk,
        max_contracts=args.max_contracts, max_position_contracts=args.max_position_contracts,
        slippage_per_contract=args.slippage_buffer)
    history = 1800 if args.history_seconds is None else args.history_seconds
    ForecastConfig(paths=args.paths, max_history_seconds=history, min_history_seconds=min(300, history))
    base = Path.home() / '.local/state/dts-kalshi'
    asset_suffix = '' if args.asset == 'BTC' else '-' + args.asset.lower()
    paths = {
        'journal': args.journal or base / ('value-kelly-' + args.mode + asset_suffix + '-v1.sqlite3'),
        'archive': args.archive,
        'capture': args.capture or base / ('value-observations' + asset_suffix + '-v1.jsonl'),
        'report': args.report or base / ('value-kelly-' + args.mode + asset_suffix + '-status-v1.json'),
        'stop_file': args.stop_file,
    }
    for key, path in paths.items():
        if path is not None:
            paths[key] = _private_path(path)
    _validate_paths(paths, (args.calibration, args.probability_fit, args.export_shadow))
    plan = dict(kind='kalshi_value_configuration', version=1, mode=args.mode, asset=args.asset,
        reference_index=asset_spec(args.asset).index_id, account_ownership='exclusive_value_journal',
        execution_eligible=False, run=args.run, capture_only=args.capture_only,
        raw_paper=args.paper_raw, bankroll=format(money, 'f'), sizing=sizing.to_dict(),
        max_drawdown_fraction=format(drawdown, 'f'), scan_interval_seconds=args.interval,
        forecast=dict(paths=args.paths, history_seconds=history),
        paths={key: None if value is None else str(value) for key, value in paths.items()},
        calibration=None if args.calibration is None else str(args.calibration.expanduser().absolute()),
        probability_fit=None if args.probability_fit is None else str(args.probability_fit.expanduser().absolute()))
    return plan, sizing, paths


def _make_client(args, allow_writes=False):
    key = os.environ.get('KALSHI_API_KEY_ID')
    pem = os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not key or not pem:
        raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for authenticated crypto data')
    return KalshiClient(environment='production', key_id=key, private_key_path=pem,
        allow_writes=allow_writes, timeout=args.timeout)


def _settlements(reader, archive, capture, executor, now):
    tickers = set(archive.pending_tickers(now, limit=20))
    # An outcome may have reached the SQLite archive immediately before a
    # crash but never reached the portable stream. Recover it on a fresh
    # official read instead of losing that contract's label permanently.
    tickers.update(ticker for ticker, identity in capture.identities.items()
        if ticker not in capture.settled and
        _contract_close(identity) <= now)
    if executor is not None:
        tickers.update(executor.journal.open_tickers())
    completed = []
    for ticker in sorted(tickers)[:20]:
        try:
            market = reader.get_market(ticker)
            received = time.time()
        except Exception:
            completed.append(dict(ticker=ticker, status='unavailable',
                reason='The official market settlement is unavailable'))
            continue
        try:
            archive.record_outcome(market, received)
        except Exception:
            completed.append(dict(ticker=ticker, status='unavailable',
                reason='The archive could not grade this official outcome'))
        try:
            if ticker in capture.identities:
                # This append independently verifies the captured terms and
                # official final result. A missing replacement archive must
                # not suppress either the portable label or strategy payout.
                # Reusing an older receipt could insert a label into the past.
                capture.record_settlement(market, received)
        except Exception:
            completed.append(dict(ticker=ticker, status='unavailable',
                reason='The official result could not be matched to the portable capture'))
        try:
            if executor is not None and isinstance(market, dict) and market.get('status') == 'finalized':
                completed.append(executor.settle(market))
        except Exception:
            completed.append(dict(ticker=ticker, status='unavailable',
                reason='The strategy settlement could not be reconciled; its position is retained'))
    return completed


def _contract_close(identity):
    from kalshi.engine import _timestamp
    return _timestamp(identity['close_time'])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        plan, sizing, paths = _configuration(args)
        if args.export_shadow is not None:
            print(json.dumps(export_shadow_archive(args.export_shadow, paths['capture'], asset=args.asset), indent=2))
            return 0
        if args.show_config or not args.run:
            print(json.dumps(plan, indent=2, allow_nan=False))
            return 0
        if paths['stop_file'] is not None and paths['stop_file'].exists():
            print('Stop file found; value evaluation did not start.')
            return 0
        paths['report'] = json_output_path(paths['report'], 'kalshi_value_state',
            inputs=(args.calibration, args.probability_fit, args.export_shadow))
        fit = None if args.probability_fit is None else load_fitted_parameters(args.probability_fit)
        if fit is not None and fit.asset != args.asset:
            raise CLIError('The probability fit belongs to another settlement asset')
        history = (fit.history_window_seconds if fit is not None and args.history_seconds is None
            else plan['forecast']['history_seconds'])
        if fit is not None and history != fit.history_window_seconds:
            raise CLIError('The requested history differs from the probability fit')
        forecast_config = minute_five_forecast_config(ForecastConfig(paths=args.paths,
            max_history_seconds=history, min_history_seconds=min(300, history)), asset=args.asset, probability_fit=fit)
        definition = minute_five_model_definition(forecast_config, asset=args.asset, probability_fit=fit)
        fingerprint = model_fingerprint(definition)
        calibration = None if args.calibration is None else load_value_calibration(args.calibration)
        if calibration is not None and dict(calibration.binding) != dict(asset=args.asset,
                source=asset_spec(args.asset).source, model_id=definition['model_version'], fit_sha256=fingerprint):
            raise CLIError('The value calibration belongs to another settlement asset or model configuration')
        config = ValueExecutionConfig(sizing=sizing, initial_cash=args.bankroll,
            max_drawdown_fraction=args.max_drawdown, min_trade_interval_seconds=args.interval,
            allow_raw_paper=args.paper_raw, model_id=definition['model_version'], fit_sha256=fingerprint, asset=args.asset)
        if paths['archive'] is None:
            paths['archive'] = _private_path(paths['journal'].parent / 'value-forecasts' / (fingerprint + '.sqlite3'))
        _validate_paths(paths, (args.calibration, args.probability_fit, args.export_shadow))
        if history < 1800:
            # The probability forecast remains usable; regime comparison
            # metadata may be unavailable until its configured history exists.
            regime = None
        else:
            regime = RegimeConfig(transition_action='model', range_rule='between_trends')
        collector_risk = RiskConfig(max_total_cost=Decimal(args.bankroll),
            max_market_cost=Decimal(args.bankroll),
            max_order_cost=min(Decimal(args.bankroll), Decimal(args.max_order_cost)),
            max_contracts=args.max_contracts, min_edge=Decimal('.01'),
            fee_reserve_per_contract=Decimal('.03'), max_signal_age_seconds=5,
            max_quote_age_seconds=5, min_seconds_to_close=2, required_series=asset_spec(args.asset).series)
        with ExitStack() as resources:
            reader = resources.enter_context(_make_client(args, allow_writes=False))
            archive = resources.enter_context(ShadowArchive(paths['archive'], forecast_config,
                model_definition=definition, asset=args.asset))
            capture = resources.enter_context(ValueCapture(paths['capture'], definition))
            observer = MinuteFiveObservationService(reader, archive, collector_risk, asset=args.asset,
                assets=(args.asset,), config=forecast_config, probability_fit=fit,
                entry_schedule='every-minute', warmup_seconds=0, cadence_seconds=10,
                uniform_sizing=UniformSizing('100'), regime_config=regime)
            executor, journal = None, None
            if not args.capture_only:
                client = reader if args.mode == 'paper' else resources.enter_context(
                    _make_client(args, allow_writes=True))
                journal = resources.enter_context(ValueJournal(paths['journal'], config, mode=args.mode))
                executor = ValueExecutor(client, journal, config=config,
                    calibration=calibration, mode=args.mode)
            print('Value profile started: scans evaluate net edge and aggregate Kelly targets; '
                'the clock alone does not create an order.', flush=True)
            if args.paper_raw:
                print('Explicit RAW PAPER research: these simulations have no live calibration qualification.', flush=True)
            next_settlement, iteration = 0.0, 0
            while args.iterations is None or iteration < args.iterations:
                started = time.monotonic()
                if paths['stop_file'] is not None and paths['stop_file'].exists():
                    break
                settled = []
                if started >= next_settlement:
                    settled = _settlements(reader, archive, capture, executor, time.time())
                    next_settlement = time.monotonic() + 15
                observation = observer.sample()
                try:
                    record = capture.record_snapshot(observation, archive)
                except ValueCaptureStaleError as error:
                    record = None
                    observation = dict(kind='kalshi_minute_five_observation', version=1,
                        observation_only=True, strategy='minute_five', asset=args.asset,
                        status='unavailable', forecast=None, reason=str(error))
                if record is not None:
                    observation = record['native_snapshot']
                execution = dict(status='capture_only', reason='Market evidence recorded; no strategy journal opened')
                if executor is not None:
                    execution = executor.execute(observation,
                        should_stop=(None if paths['stop_file'] is None else lambda: paths['stop_file'].exists()))
                report = dict(kind='kalshi_value_state', version=1, mode=args.mode, asset=args.asset,
                    updated_at=time.time(), model_fingerprint=fingerprint, config=config.to_dict(),
                    capture=capture.snapshot(), observation=observation, decision=execution,
                    settlements=settled, portfolio=None if journal is None else journal.snapshot())
                _write_report(paths['report'], report)
                print(args.asset + ' value: ' + str(execution.get('status')) + ' - ' +
                    str(execution.get('reason', '')), flush=True)
                iteration += 1
                if journal is not None and journal.halted():
                    print('Value journal halted; reconcile its recorded order outcome before resuming.', flush=True)
                    break
                if args.iterations is None or iteration < args.iterations:
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
        return 0
    except KeyboardInterrupt:
        print('Value evaluation stopped; filled positions remain recorded.')
        return 0
    except (ValueError, TypeError, ArithmeticError, OSError, KalshiError) as error:
        print('Value strategy: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
