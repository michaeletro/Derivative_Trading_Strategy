#!/usr/bin/env python3
"""Run, archive and evaluate an ensemble-driven Bitcoin paper experiment.

CF BRTI reads require local credentials. This entry point cannot enable exchange
orders: the client is read-only and the order journal is always paper mode.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _risk, _risk_arguments, _write_report


def _weights(raw):
    try:
        values = tuple(float(value.strip()) for value in raw.split(','))
    except (TypeError, ValueError, OverflowError):
        raise argparse.ArgumentTypeError('Weights must be three comma-separated finite nonnegative numbers') from None
    if len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values) or max(values) <= 0:
        raise argparse.ArgumentTypeError('Weights must be three comma-separated nonnegative numbers with a positive sum')
    return values


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    for name, help_text in (('run-paper', 'Record ensemble forecasts and simulate mispricing entries and exits'),
                            ('grade', 'Record official outcomes for expired ensemble contracts'),
                            ('score', 'Score latest ensemble forecasts per settled contract offline')):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--archive', type=Path, required=True)
        command.add_argument('--paths', type=int, default=1000, help='Monte Carlo paths per active member')
        command.add_argument('--seed', type=int, default=7)
        command.add_argument('--weights', type=_weights, default=(1, 1, 1),
                             help='Prior weights in long_run,recent,ewma order; normalized for the experiment')
        command.add_argument('--report', type=Path, required=name == 'run-paper')
        if name != 'score':
            command.add_argument('--timeout', type=float, default=3)
        if name == 'run-paper':
            command.add_argument('--journal', type=Path, required=True)
            command.add_argument('--portfolio', type=Path,
                                 help='Paper position ledger; defaults beside the journal as STEM-portfolio.sqlite3')
            command.add_argument('--entry-edge', help='Minimum entry edge, at least the risk policy minimum')
            command.add_argument('--exit-edge', default='0.01', help='Close after the pricing edge converges below this band')
            command.add_argument('--take-profit', help='Optional positive paper profit target in dollars, at most 1000')
            command.add_argument('--side', choices=('auto', 'yes', 'no'), default='auto')
            command.add_argument('--loop', action='store_true')
            command.add_argument('--interval', type=float, default=5)
            command.add_argument('--stop-file', type=Path)
            _risk_arguments(command)
            command.set_defaults(sizing_mode='kelly', required_series='KXBTC15M',
                                 fee_reserve_per_contract='0.03', max_signal_age_seconds=5)
    return result


def _paths(args):
    """Resolve every output before constructing clients or touching databases."""
    archive = _private_path(args.archive)
    journal = _private_path(args.journal) if args.command == 'run-paper' else None
    portfolio = None
    if journal is not None:
        portfolio = _private_path(args.portfolio or journal.with_name(journal.stem + '-portfolio.sqlite3'))
    report = _private_path(args.report) if args.report else None
    stop = _private_path(args.stop_file) if args.command == 'run-paper' and args.stop_file else None
    reserved = []
    for kind, database in (('archive', archive), ('journal', journal), ('portfolio', portfolio)):
        if database is not None:
            reserved.extend((kind, Path(str(database) + suffix).resolve())
                            for suffix in ('', '-wal', '-shm', '-journal'))
    if report is not None:
        reserved.append(('report', report))
    if stop is not None:
        reserved.append(('stop', stop))
    for index, (kind, path) in enumerate(reserved):
        for other_kind, other in reserved[:index]:
            same = path == other
            if not same and path.exists() and other.exists():
                try:
                    same = path.samefile(other)
                except OSError:
                    raise CLIError('Cannot verify distinct ensemble state paths') from None
            if same:
                raise CLIError('Archive, journal, portfolio, report, stop file and SQLite sidecars must use distinct paths')
    return archive, journal, portfolio, report, stop


def _strategy_parameters(args, risk):
    from kalshi.engine import EngineError, decimal_value
    try:
        entry = decimal_value(risk.min_edge if args.entry_edge is None else args.entry_edge,
                              'entry edge', places=6, maximum=1)
        exit_edge = decimal_value(args.exit_edge, 'exit edge', places=6, maximum=1)
        take_profit = (None if args.take_profit is None else
                       decimal_value(args.take_profit, 'take-profit target', places=6, maximum=1000))
        if entry < risk.min_edge or not 0 <= exit_edge < entry or take_profit is not None and take_profit <= 0:
            raise ValueError()
    except (EngineError, ValueError):
        raise CLIError('Require risk minimum <= entry edge <= 1, 0 <= exit edge < entry edge, and an optional positive take-profit <= 1000 (at most six decimals)') from None
    return entry, exit_edge, take_profit


def main(argv=None):
    args = parser().parse_args(argv)
    client = None
    try:
        from kalshi.client import KalshiClient
        from kalshi.forecast import ForecastConfig
        from kalshi.scoring import score_forecasts
        from kalshi.settlement_ensemble import EnsembleConfig, MODEL_IDS, MODEL_VERSION
        from kalshi.shadow import ShadowArchive, ShadowService, _safe_error

        try:
            config = EnsembleConfig(paths=args.paths, seed=args.seed,
                                    weights=dict(zip(MODEL_IDS, args.weights)))
        except (TypeError, ValueError):
            raise CLIError('Invalid ensemble path count, seed or weights') from None
        archive_path, journal_path, portfolio_path, report_path, stop_path = _paths(args)
        if args.command in ('grade', 'score') and not archive_path.is_file():
            raise CLIError('The ensemble archive does not exist; run the paper experiment first')
        if args.command == 'run-paper':
            risk = _risk(args)
            if risk.sizing_mode != 'kelly' or risk.required_series != 'KXBTC15M':
                raise CLIError('The ensemble paper runner requires Bitcoin 15-minute Kelly mode')
            if risk.max_signal_age_seconds > 5 or risk.max_quote_age_seconds > 5:
                raise CLIError('Ensemble signal and quote ages must be at most five seconds')
            entry_edge, exit_edge, take_profit = _strategy_parameters(args, risk)
            if not math.isfinite(args.interval) or not 5 <= args.interval <= 60:
                raise CLIError('Ensemble sampling interval must be between five and sixty seconds')
            if stop_path and stop_path.exists():
                return 0
        if args.command != 'score':
            if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
                raise CLIError('Read timeout must be positive and at most ten seconds')
            credentials = {}
            if args.command == 'run-paper':
                credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
                                   private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
                if not all(credentials.values()):
                    raise CLIError('Configure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally for BRTI reads')
            client = KalshiClient(environment='production', allow_writes=False,
                                  timeout=args.timeout, **credentials)
        base_config = ForecastConfig(paths=config.paths, seed=config.seed,
            min_returns=config.min_returns, min_history_seconds=config.min_history_seconds,
            max_age_seconds=config.max_age_seconds, max_history_seconds=config.max_history_seconds)
        definition = dict(model_version=MODEL_VERSION, config=config.to_dict())
        with ShadowArchive(archive_path, base_config, model_definition=definition) as archive:
            if args.command == 'score':
                output = dict(score_forecasts(archive.scored_rows()), execution_eligible=False)
            elif args.command == 'grade':
                output = dict(kind='kalshi_ensemble_grading', version=1,
                              recorded_outcomes=ShadowService(client, archive).grade(), execution_eligible=False)
            else:
                from kalshi.algorithm import EnsemblePaperService
                from kalshi.engine import Journal
                from kalshi.portfolio import PaperPortfolio
                with Journal(journal_path, risk=risk, mode='paper', environment='production') as journal, \
                        PaperPortfolio(portfolio_path, risk, take_profit=take_profit,
                                       entry_edge=entry_edge, exit_edge=exit_edge) as portfolio:
                    service = EnsemblePaperService(client, archive, journal, config=config,
                                                   selected_side=args.side, portfolio=portfolio)
                    while True:
                        if stop_path and stop_path.exists():
                            return 0
                        started = time.monotonic()
                        output = service.sample()
                        _write_report(report_path, output)
                        forecast = output.get('forecast') or {}
                        eligibility = output.get('eligibility') or {}
                        paper = output.get('paper_order') or {}
                        print(json.dumps(dict(kind=output.get('kind'), status=output.get('status'),
                            probability_yes=forecast.get('probability_yes'),
                            paper_eligible=eligibility.get('paper_eligible', False),
                            selected_outcome=eligibility.get('selected_outcome'),
                            paper_status=paper.get('status'), paper_action=paper.get('action'), reason=output.get('reason'),
                            execution_eligible=False), allow_nan=False), flush=True)
                        if not args.loop:
                            return 0 if forecast.get('status') == 'ready' else 2
                        delay = max(0, args.interval - (time.monotonic() - started))
                        if delay:
                            time.sleep(delay)
            if report_path:
                _write_report(report_path, output)
            print(json.dumps(output, allow_nan=False), flush=True)
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        from kalshi.shadow import _safe_error
        from kalshi.portfolio import PortfolioError
        message = str(error) if isinstance(error, (CLIError, PortfolioError)) else _safe_error(error)
        print('Kalshi ensemble: ' + message, file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
