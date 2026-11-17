"""Offline, paired rolling/RSI paper replay of native ensemble evidence.

This module never imports an exchange client. Quotes are simulated fills, and
outcomes can replenish cash only when their recorded receipt becomes available.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import hashlib
import math
from pathlib import Path
import sqlite3
import tempfile

from .crypto_assets import ASSETS
from .engine import EngineError, RiskConfig, _levels
from .portfolio import PaperPortfolio, PortfolioError, _identity as portfolio_identity
from .rolling import RollingConfig, RollingIndicator, RollingError, _inputs
from .settlement_ensemble import EnsembleConfig, model_version_for_asset
from .shadow import _payout_identity, _stamp


class RollingReplayError(ValueError):
    """Bounded, non-sensitive archive or comparison configuration error."""


class _WrongForecastType(RollingReplayError):
    pass


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 1_000_000:
        raise RollingReplayError('Recorded JSON exceeds the supported bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise RollingReplayError('Recorded JSON is invalid') from None


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise RollingReplayError('Recorded clocks must be finite nonnegative numbers')
    return float(value)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _payout_hash(market):
    raw = json.dumps(_payout_identity(market), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _metadata(db):
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
        experiment = _json(row['value']) if row is not None else None
        if (not isinstance(experiment, dict) or type(experiment.get('version')) is not int
                or experiment['version'] != 1):
            raise ValueError()
        matches = [spec for spec in ASSETS.values() if spec.source == experiment.get('source')]
        if len(matches) != 1:
            raise ValueError()
        profile = matches[0]
        definition = experiment.get('model_definition')
        if (not isinstance(definition, dict)
                or definition.get('model_version') != model_version_for_asset(profile.symbol)
                or experiment.get('model_version') != definition['model_version']
                or not isinstance(definition.get('config'), dict)):
            raise ValueError()
        ensemble = EnsembleConfig(**definition['config'])
        # A different normalized model definition must not be silently replayed
        # under the hash belonging to these recorded forecasts.
        if ensemble.to_dict() != definition['config']:
            raise ValueError()
        return profile, ensemble.config_hash, experiment
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise RollingReplayError('Use a native settlement-ensemble forecast archive; historical tick and minute-five archives cannot supply this comparison') from None


def _frame(row, profile, config_hash, config):
    forecast, market, book = (_json(row[key]) for key in ('forecast', 'market', 'book'))
    if not isinstance(forecast, dict) or forecast.get('kind') != 'kalshi_settlement_ensemble':
        raise _WrongForecastType('The archive contains a different forecast type; only settlement-ensemble records can be compared')
    as_of, quoted, started, completed, acquired = (_time(value) for value in (
        row['as_of'], row['quote_received_at'], row['quote_started_at'],
        forecast.get('forecast_completed_at'), row['index_received_at']))
    if (forecast.get('as_of') != as_of or not acquired <= started <= quoted <= as_of <= completed
            or row['index_kind'] != profile.source or forecast.get('source') != profile.source
            or forecast.get('model_version') != model_version_for_asset(profile.symbol)
            or forecast.get('config_hash') != config_hash or forecast.get('asset', 'BTC') != profile.symbol
            or not isinstance(market, dict) or market.get('ticker') != row['ticker'] or market.get('status') != 'active'
            or forecast.get('ticker') != row['ticker']
            or _time(row['close_time']) != portfolio_identity(market)['close_time']
            or portfolio_identity(forecast.get('contract_identity')) != portfolio_identity(market)):
        raise ValueError('Forecast provenance differs')
    yes = _levels(book['orderbook_fp']['yes_dollars'])
    no = _levels(book['orderbook_fp']['no_dollars'])
    if not yes or not no:
        raise ValueError('Missing executable book')
    quote = dict(ticker=row['ticker'], contract_identity=forecast['contract_identity'],
        fetched_at=_iso(quoted), yes_ask=str(1 - no[-1][0]), no_ask=str(1 - yes[-1][0]))
    _inputs(forecast, quote, completed, config)
    # PaperPortfolio requires the conservative probability envelope as well.
    probability = [Decimal(str(forecast.get(key))) for key in
        ('probability_yes_lower', 'probability_yes', 'probability_yes_upper')]
    if not all(value.is_finite() for value in probability) or not 0 <= probability[0] <= probability[1] <= probability[2] <= 1:
        raise ValueError('Missing conservative probability bounds')
    return completed, forecast, quote, market, book


def _summary(portfolio, counts):
    snapshot = portfolio.snapshot()
    return dict(counts, cash=snapshot['cash'], initial_cash=snapshot['initial_cash'],
        realized_pnl=snapshot['realized_pnl'], fees_paid=snapshot['fees_paid'],
        gross_turnover=snapshot['gross_turnover'], open_cost_basis=snapshot['open_cost_basis'],
        positions=snapshot['positions'], final_equity=snapshot['cash'] if not snapshot['positions'] else None,
        all_positions_settled=not snapshot['positions'])


def compare_archive(archive_path, *, config=None, rsi_period=14, rsi_entry=55,
                    risk=None, max_rows=20_000):
    """Replay one recorded asset with identical models, quotes and cash policies.

    The source archive is read-only. Each arm gets a temporary indicator and
    paper ledger. This is a parameter-fixed ablation, not model fitting or an
    independent claim of expected performance.
    """
    if type(max_rows) is not int or not 1 <= max_rows <= 100_000:
        raise RollingReplayError('Replay row limit must be an integer from 1 through 100000')
    base = RollingConfig() if config is None else config
    if not isinstance(base, RollingConfig):
        raise RollingReplayError('A validated rolling configuration is required')
    configs = dict(baseline=replace(base, rsi_filter=False, rsi_period=rsi_period, rsi_entry=rsi_entry),
                   rsi=replace(base, rsi_filter=True, rsi_period=rsi_period, rsi_entry=rsi_entry))
    from kalshi_bot import _private_path
    source = _private_path(Path(archive_path))
    if not source.is_file():
        raise RollingReplayError('The ensemble forecast archive does not exist')
    db = None
    try:
        db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        with ExitStack() as resources:
            profile, config_hash, experiment = _metadata(db)
            total = db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
            if not 1 <= total <= max_rows:
                raise RollingReplayError('Forecast archive is empty or exceeds --max-rows; replay a bounded native archive without truncating its warmup')
            if db.execute('SELECT 1 FROM forecasts WHERE length(forecast)>1000000 OR length(market)>1000000 OR length(book)>1000000 LIMIT 1').fetchone():
                raise RollingReplayError('A forecast evidence row exceeds the supported bound')
            if db.execute('SELECT 1 FROM outcomes WHERE length(market)>1000000 LIMIT 1').fetchone():
                raise RollingReplayError('An outcome evidence row exceeds the supported bound')
            if db.execute('SELECT COUNT(*) FROM outcomes WHERE ticker IN (SELECT DISTINCT ticker FROM forecasts)').fetchone()[0] > max_rows:
                raise RollingReplayError('The outcome evidence exceeds the replay row limit')
            policy = risk or RiskConfig(sizing_mode='kelly', required_series=profile.series,
                fee_reserve_per_contract='0.03', min_edge='0.01', max_signal_age_seconds=5)
            if (policy.sizing_mode != 'kelly' or policy.required_series not in (None, profile.series)
                    or policy.max_quote_age_seconds > 5 or policy.max_signal_age_seconds > 5
                    or not 0 < policy.fee_reserve_per_contract < 1):
                raise RollingReplayError('Replay requires matching paper Kelly risk with at most five-second freshness')
            policy = replace(policy, required_series=profile.series)
            directory = resources.enter_context(tempfile.TemporaryDirectory(prefix='kalshi-rsi-replay-'))
            portfolios, indicators, counts = {}, {}, {}
            for name, selected in configs.items():
                indicators[name] = resources.enter_context(RollingIndicator(Path(directory) / (name + '-indicator.sqlite3'), selected))
                portfolios[name] = resources.enter_context(PaperPortfolio(Path(directory) / (name + '-portfolio.sqlite3'),
                    policy, assets=(profile.symbol,), strategy='rolling', strategy_config=selected.to_dict(),
                    entry_edge=policy.min_edge, exit_edge=str(min(Decimal('0.005'), policy.min_edge / 2))))
                counts[name] = dict(signal_yes=0, signal_no=0, entry_fills=0, exit_fills=0,
                    settlement_fills=0, filled_actions=0, skipped_actions=0, rsi_blocked_signals=0)
            audit = dict(forecast_rows=total, replayed_rows=0, invalid_rows=0,
                duplicate_rows=0, revised_rows=0, fee_blocked_rows=0,
                outcome_rows=0, invalid_outcomes=0, applied_outcome_events=0)
            known, seen, official, latest, first_time, last_time = {}, {}, set(), None, None, None
            outcome_cursor = db.execute('SELECT * FROM outcomes WHERE ticker IN (SELECT DISTINCT ticker FROM forecasts) ORDER BY received_at,ticker')
            pending = next(outcome_cursor, None)

            def settle_through(until):
                nonlocal pending
                while pending is not None:
                    try:
                        received = _time(pending['received_at'])
                    except RollingReplayError:
                        audit['invalid_outcomes'] += 1
                        pending = next(outcome_cursor, None)
                        continue
                    if received > until:
                        break
                    row = pending
                    pending = next(outcome_cursor, None)
                    audit['outcome_rows'] += 1
                    try:
                        market = _json(row['market'])
                        if (row['ticker'] not in known or not isinstance(market, dict)
                                or market.get('ticker') != row['ticker'] or market.get('result') != row['result']
                                or row['result'] not in ('yes', 'no') or market.get('status') != 'finalized'
                                or market.get('is_provisional', False) is not False
                                or _payout_hash(market) != known[row['ticker']]
                                or received < portfolio_identity(market)['close_time']
                                or not market.get('settlement_ts')
                                or not portfolio_identity(market)['close_time'] <= _stamp(market['settlement_ts']) <= received):
                            raise ValueError()
                        for name, portfolio in portfolios.items():
                            action = portfolio.settle_market(market, received)
                            if action is not None:
                                counts[name]['settlement_fills'] += 1
                                counts[name]['filled_actions'] += 1
                        audit['applied_outcome_events'] += 1
                        official.add(row['ticker'])
                    except (ValueError, KeyError, TypeError, PortfolioError):
                        audit['invalid_outcomes'] += 1

            cursor = db.execute('''SELECT f.*,a.kind AS index_kind,a.received_at AS index_received_at
                FROM forecasts f LEFT JOIN acquisitions a ON a.id=f.index_acquisition_id
                ORDER BY CASE WHEN json_valid(f.forecast) THEN
                    COALESCE(json_extract(f.forecast,'$.forecast_completed_at'),f.as_of)
                    ELSE f.as_of END,f.id''')
            for row in cursor:
                try:
                    now, forecast, quote, market, book = _frame(row, profile, config_hash, base)
                    ticker = market['ticker']
                    identity = _payout_hash(market)
                    if ticker in known and identity != known[ticker]:
                        raise ValueError('Recorded payout identity changed')
                    known[ticker] = identity
                    fingerprint_hash = hashlib.sha256()
                    for value in (row['market'], row['book'], row['forecast'], str(row['quote_received_at'])):
                        fingerprint_hash.update(value.encode('utf-8'))
                        fingerprint_hash.update(b'\x00')
                    fingerprint = fingerprint_hash.hexdigest()
                    key = (ticker, forecast['as_of'])
                    if key in seen:
                        audit['duplicate_rows' if seen[key] == fingerprint else 'revised_rows'] += 1
                        continue
                    seen[key] = fingerprint
                    if latest is not None and forecast['as_of'] < latest:
                        raise ValueError('Out-of-order model clock')
                    latest = forecast['as_of']
                    settle_through(now)
                    first_time = now if first_time is None else first_time
                    last_time = now
                    audit['replayed_rows'] += 1
                    venue = market.get('_crypto15m', market.get('_btc15m'))
                    fee_valid = isinstance(venue, dict)
                    try:
                        fee = Decimal(str(venue.get('fee_reserve_lower_bound')))
                        fee_valid = fee_valid and fee.is_finite() and 0 <= fee < 1 and policy.fee_reserve_per_contract >= fee
                    except (AttributeError, ArithmeticError, ValueError, TypeError):
                        fee_valid = False
                    if not fee_valid:
                        audit['fee_blocked_rows'] += 1
                    for name, selected in configs.items():
                        signal = indicators[name].evaluate(forecast, quote, now)
                        for side in ('yes', 'no'):
                            counts[name]['signal_' + side] += int(signal['entry_allowed'][side])
                            counts[name]['rsi_blocked_signals'] += int(selected.rsi_filter
                                and signal.get('baseline_entry_allowed', {}).get(side, False)
                                and not signal['entry_allowed'][side])
                        if not fee_valid:
                            counts[name]['skipped_actions'] += 1
                            continue
                        held = portfolios[name].exposure(ticker)['outcome']
                        action = portfolios[name].rebalance(forecast, market, book, now,
                            entry_allowed=signal['entry_allowed'],
                            exit_trigger=bool(held and signal['exit_allowed'].get(held, False)),
                            signal_reason=signal['reason'])
                        if action['status'] == 'paper_filled':
                            counts[name]['filled_actions'] += 1
                            counts[name]['entry_fills'] += int(action['action'] in ('buy', 'flip'))
                            counts[name]['exit_fills'] += int(action['action'] in ('sell', 'flip'))
                        elif action['status'] == 'skipped':
                            counts[name]['skipped_actions'] += 1
                except _WrongForecastType:
                    raise
                except RollingReplayError:
                    audit['invalid_rows'] += 1
                except (ValueError, KeyError, TypeError, ArithmeticError, EngineError, PortfolioError, RollingError):
                    audit['invalid_rows'] += 1
            # Later official outcomes determine terminal realized results, but
            # were never available to earlier entry/sizing decisions.
            settle_through(float('inf'))
            audit['contracts'] = len(known)
            audit['missing_official_outcomes'] = len(set(known) - official)
            summaries = {name: _summary(portfolios[name], counts[name]) for name in configs}
            difference = dict(entry_fills=summaries['rsi']['entry_fills'] - summaries['baseline']['entry_fills'],
                realized_pnl=str(Decimal(summaries['rsi']['realized_pnl']) - Decimal(summaries['baseline']['realized_pnl'])),
                final_equity=None)
            if all(summaries[name]['final_equity'] is not None for name in configs):
                difference['final_equity'] = str(Decimal(summaries['rsi']['final_equity']) - Decimal(summaries['baseline']['final_equity']))
            return dict(kind='kalshi_rolling_rsi_comparison', version=1, mode='paper_replay',
                status='ready' if audit['replayed_rows'] else 'insufficient_data',
                asset=profile.symbol, source=profile.source, model_version=experiment['model_version'],
                config_hash=config_hash, first_decision_at=first_time, last_decision_at=last_time,
                baseline_config=configs['baseline'].to_dict(), rsi_config=configs['rsi'].to_dict(),
                risk=policy.to_dict(), audit=audit, baseline=summaries['baseline'], rsi=summaries['rsi'],
                difference=difference, calibrated=False, execution_eligible=False,
                limitations=['Recorded depth is simulated executable liquidity, not a guarantee of exchange fills.',
                    'Realized P&L already includes configured entry and exit fee allowances.',
                    'Settlements use only official recorded outcomes at their recorded receipt time.',
                    'Open positions have no asserted terminal equity; missing outcomes are never invented.',
                    'A fixed-parameter historical comparison is not independent validation or evidence of future profitability.'])
    except (sqlite3.Error, OSError) as error:
        raise RollingReplayError('The native ensemble archive is unavailable, locked or has an unsupported schema') from None
    finally:
        if db is not None:
            db.close()
