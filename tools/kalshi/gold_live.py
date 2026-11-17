"""Read-only Gold observations for the separate Pyth candle-close model.

Gold uses a terminal price statistic, unlike the crypto final-minute average.
This module neither reads accounts nor submits orders. It supplies independently
identified observations to the shared live executor, with a separate archive.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import random
import sqlite3
from statistics import median
import time

from .btc15m import Btc15mError, _eligible_market, discover_market
from .client import KalshiError
from .engine import EngineError, _iso, _timestamp
from .forecast import ForecastConfig
from .minute_five import _identity
from .minute_five_policy import budget_text, recurring_budget_plan
from .pyth_stream import PythObservation
from .shadow import book_summary


GOLD_MODEL_VERSION = 'pyth-1ozgold-terminal-median-v1'
GOLD_UNDERLYING = 'Metal.Index.1OZGOLD/USD'
GOLD_SOURCE = 'PYTH_1OZGOLD_USD'
GOLD_SERIES = 'KXGOLD15M'
_ARCHIVE_KIND = 'kalshi_gold_live_archive_v1'
_EPSILON = .000001
_ASSUMPTIONS = (
    'Uncalibrated physical-probability Gold model; the executor separately authorizes orders.',
    'Exact source: Pyth Metal.Index.1OZGOLD/USD; spot XAU and other gold indices are not substituted.',
    'Frozen arithmetic diffusion dS=a*dW, with a=latest Gold price times trailing EWMA log volatility.',
    'Settlement statistic is the terminal one-minute Pyth candle close, not a final-minute average.',
    'The terminal price is modeled at the verified contract close; exact source candle conventions remain authoritative.',
    'Missing source observations are never filled or interpolated, and history gaps over fifteen seconds block the model.',
    'The BTC fit and crypto volatility scale are not applied to Gold.',
    'Monte Carlo intervals quantify numerical sampling uncertainty, excluding model, parameter and feed error.',
    'Arithmetic diffusion can produce negative simulated prices; these local-model values are not clamped.',
)


class GoldLiveError(EngineError):
    """Fixed, non-sensitive Gold model or archive error."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def gold_model_definition(config=None):
    config = ForecastConfig(min_history_seconds=300) if config is None else config
    if not isinstance(config, ForecastConfig):
        raise GoldLiveError('A valid independent Gold forecast configuration is required')
    return dict(model_version=GOLD_MODEL_VERSION, asset='GOLD', source=GOLD_SOURCE,
        underlying=GOLD_UNDERLYING, settlement_statistic='terminal_candle_close',
        forecast=asdict(config), fitted_parameters=None, volatility_scale=1)


def _unavailable(reason, now, ticker=None):
    return dict(kind='kalshi_minute_five_forecast', version=1, status='unavailable',
        asset='GOLD', source=GOLD_SOURCE, model_version=GOLD_MODEL_VERSION,
        settlement_statistic='terminal_candle_close', underlying=GOLD_UNDERLYING,
        ticker=ticker, as_of=now if _finite(now) else None, model_time=None,
        probability_yes=None, probability_no=None, median_settlement_average=None,
        median_settlement_close=None, settlement_centered_median=None,
        calibrated=False, execution_eligible=False, reason=reason, assumptions=list(_ASSUMPTIONS))


def _validated_market(market, now):
    if (not isinstance(market, dict) or not _eligible_market(market, now, 0, series=GOLD_SERIES)):
        raise GoldLiveError('A current verified Gold fifteen-minute binary contract is required')
    binding = market.get('_crypto15m', {})
    if (not isinstance(binding, dict) or binding.get('asset') != 'GOLD'
            or binding.get('series_ticker') != GOLD_SERIES or binding.get('source') != GOLD_SOURCE
            or binding.get('index_id') != GOLD_UNDERLYING):
        raise GoldLiveError('The Gold contract lacks its exact verified Pyth feed binding')
    try:
        strike = float(market['floor_strike'])
        closed = _timestamp(market['close_time'])
    except (ValueError, TypeError, KeyError, OverflowError):
        raise GoldLiveError('The Gold target or settlement clock is invalid') from None
    if not _finite(strike) or not 0 < strike <= 100000000:
        raise GoldLiveError('The Gold target is invalid')
    return strike, closed


def _history(observations, now, config):
    if not isinstance(observations, (tuple, list)) or not 1 <= len(observations) <= 100000:
        raise GoldLiveError('Gold source history is missing or exceeds its supported bound')
    validated = []
    for row in observations:
        if (not isinstance(row, PythObservation) or row.source != GOLD_SOURCE
                or row.underlying != GOLD_UNDERLYING):
            raise GoldLiveError('Only the exact Pyth 1-Ounce Gold source is supported')
        if (any(not _finite(value) for value in (row.event_time, row.received_at, row.price))
                or not 0 < row.price <= 1000000000 or row.event_time < 0
                or row.event_time > now or row.received_at > now
                or row.received_at < row.event_time - _EPSILON):
            raise GoldLiveError('Gold source event, receipt or price metadata is invalid')
        venue = row.venue_received_at
        if venue is not None and (not _finite(venue)
                or not row.event_time - _EPSILON <= venue <= row.received_at + _EPSILON):
            raise GoldLiveError('Gold venue and local receipt clocks are inconsistent')
        validated.append(row)
    validated.sort(key=lambda row: row.event_time)
    if any(right.event_time - left.event_time <= _EPSILON for left, right in zip(validated, validated[1:])):
        raise GoldLiveError('Duplicate or revised Gold source timestamps require reconciliation')
    latest = validated[-1]
    if now - latest.event_time > config.max_age_seconds or now - latest.received_at > config.max_age_seconds:
        raise GoldLiveError('The exact Gold source observation is stale')
    history = [row for row in validated if row.event_time >= now - config.max_history_seconds]
    if not history or history[-1].event_time - history[0].event_time < config.min_history_seconds:
        raise GoldLiveError('Insufficient independent Gold history for its volatility model')
    if any(right.event_time - left.event_time > 15 + _EPSILON for left, right in zip(history, history[1:])):
        raise GoldLiveError('Gold source history has a gap exceeding fifteen seconds')
    return history


def _volatility(history, now, config):
    anchor, increments = history[0], []
    for row in history[1:]:
        elapsed = row.event_time - anchor.event_time
        if elapsed < 5 - _EPSILON:
            continue
        if elapsed > 15 + _EPSILON:
            raise GoldLiveError('Gold nonoverlapping volatility returns contain an unsupported gap')
        change = math.log(row.price / anchor.price)
        weight = math.exp(-math.log(2) * (now - row.event_time) / config.ewma_half_life_seconds)
        increments.append((change, elapsed, weight))
        anchor = row
    if len(increments) < config.min_returns:
        raise GoldLiveError('Insufficient independent Gold volatility returns')
    weighted_time = math.fsum(elapsed * weight for _, elapsed, weight in increments)
    variance = math.fsum(change * change * weight for change, _, weight in increments) / weighted_time
    weights = math.fsum(weight for _, _, weight in increments)
    effective = weights * weights / math.fsum(weight * weight for _, _, weight in increments)
    return math.sqrt(variance), len(increments), effective


def _wilson(successes, paths):
    z = 1.959963984540054
    p = successes / paths
    divisor = 1 + z * z / paths
    center = (p + z * z / (2 * paths)) / divisor
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * paths)) / paths) / divisor
    return dict(method='wilson_95', lower=0.0 if successes == 0 else max(0.0, center - half),
        upper=1.0 if successes == paths else min(1.0, center + half))


def gold_forecast(market, observations, now, config=None):
    """Model the actual Gold threshold with a separate terminal-price kernel."""
    ticker = market.get('ticker') if isinstance(market, dict) else None
    try:
        if not _finite(now) or not 0 < now <= 253402300799:
            raise GoldLiveError('The Gold forecast observation clock is invalid')
        config = ForecastConfig(min_history_seconds=300) if config is None else config
        if not isinstance(config, ForecastConfig):
            raise GoldLiveError('A valid independent Gold forecast configuration is required')
        strike, closed = _validated_market(market, now)
        history = _history(observations, now, config)
        sigma, return_count, effective = _volatility(history, now, config)
        latest = history[-1]
        horizon = closed - latest.event_time
        dollar_volatility = latest.price * sigma
        deviation = dollar_volatility * math.sqrt(horizon)
        if not _finite(deviation) or deviation < 0:
            raise GoldLiveError('The Gold simulation volatility exceeds its numerical bound')
        random_source = random.Random(config.seed)
        offset = latest.price - strike
        centered = [offset + deviation * random_source.gauss(0, 1) for _ in range(config.paths)]
        if any(not _finite(value) for value in centered):
            raise GoldLiveError('The Gold terminal simulation exceeds its numerical bound')
        inclusive = market['strike_type'] == 'greater_or_equal'
        successes = sum(value >= 0 if inclusive else value > 0 for value in centered)
        p_yes = successes / config.paths
        central = median(centered)
        deterministic = deviation == 0
        analytic = (float(offset >= 0 if inclusive else offset > 0) if deterministic else
            .5 * math.erfc(-offset / (deviation * math.sqrt(2))))
        return dict(kind='kalshi_minute_five_forecast', version=1, status='ready',
            asset='GOLD', source=GOLD_SOURCE, model_version=GOLD_MODEL_VERSION,
            underlying=GOLD_UNDERLYING, settlement_statistic='terminal_candle_close',
            ticker=ticker, as_of=now, model_time=latest.event_time, forecast_completed_at=now,
            probability_yes=p_yes, probability_no=1 - p_yes, analytic_probability_yes=analytic,
            median_settlement_average=strike + central, median_settlement_close=strike + central,
            legacy_median_field='median_settlement_average aliases the terminal Gold candle close',
            settlement_centered_median=central,
            median_outcome='yes' if (central >= 0 if inclusive else central > 0) else 'no',
            target_price=str(market['floor_strike']), strike_type=market['strike_type'],
            contract_identity=_identity(market), paths=config.paths,
            monte_carlo_se=math.sqrt(p_yes * (1 - p_yes) / config.paths),
            sampling_interval=_wilson(successes, config.paths),
            parameters=dict(volatility_log_per_sqrt_second=sigma,
                dollar_volatility_per_sqrt_second=dollar_volatility,
                ewma_half_life_seconds=config.ewma_half_life_seconds,
                volatility_scale=1, seconds_remaining=horizon,
                terminal_variance=deviation * deviation, return_count=return_count,
                effective_return_count=effective),
            diagnostics=dict(paths=config.paths, terminal_variance=deviation * deviation,
                settlement_statistic='terminal_candle_close'),
            calibrated=False, fitted_parameters=None, execution_eligible=False,
            reason='Median of simulated terminal Pyth Gold candle closes; independent unfitted Gold model',
            assumptions=list(_ASSUMPTIONS))
    except (GoldLiveError, EngineError, ValueError, TypeError, KeyError, OverflowError) as error:
        message = str(error) if isinstance(error, GoldLiveError) else 'Gold model data is unavailable or invalid'
        return _unavailable(message, now, ticker)


class _GoldArchive:
    """One-writer Gold-only evidence, separate from all account journals."""

    def __init__(self, path, definition):
        path = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise GoldLiveError('Gold archive paths cannot contain symbolic links')
        self.path = path.resolve()
        existed = self.path.exists()
        if existed and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise GoldLiveError('Gold archive paths require an isolated regular file')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(str(self.path), timeout=0)
        self.connection.row_factory = sqlite3.Row
        encoded = json.dumps(definition, sort_keys=True, allow_nan=False)
        try:
            if existed:
                values = dict(self.connection.execute('SELECT key,value FROM gold_meta'))
                if values.get('kind') != _ARCHIVE_KIND or values.get('model_definition') != encoded:
                    raise GoldLiveError('Existing file is not the matching isolated Gold forecast archive')
                self.connection.execute('SELECT event_time,price FROM gold_ticks LIMIT 0')
                self.connection.execute('SELECT observed_at,payload_json FROM gold_observations LIMIT 0')
            else:
                self.connection.execute('PRAGMA synchronous=FULL')
                with self.connection:
                    self.connection.executescript('''
                        CREATE TABLE gold_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE gold_ticks(event_time REAL PRIMARY KEY,received_at REAL NOT NULL,
                            venue_received_at REAL,price TEXT NOT NULL,underlying TEXT NOT NULL);
                        CREATE TABLE gold_observations(id INTEGER PRIMARY KEY,observed_at REAL NOT NULL,
                            ticker TEXT,payload_json TEXT NOT NULL);
                    ''')
                    self.connection.executemany('INSERT INTO gold_meta VALUES (?,?)',
                        [('kind', _ARCHIVE_KIND), ('model_definition', encoded)])
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        except (sqlite3.Error, ValueError, TypeError) as error:
            self.connection.close()
            if isinstance(error, GoldLiveError):
                raise
            raise GoldLiveError('The isolated Gold forecast archive is unavailable or invalid') from None

    def record(self, observations, snapshot, now):
        encoded = json.dumps(snapshot, sort_keys=True, allow_nan=False)
        if len(encoded) > 2_000_000:
            raise GoldLiveError('The Gold forecast evidence exceeds its size bound')
        try:
            with self.connection:
                for row in observations:
                    price = format(Decimal(str(row.price)), 'f')
                    existing = self.connection.execute('SELECT price,underlying FROM gold_ticks WHERE event_time=?',
                        (row.event_time,)).fetchone()
                    if existing is not None:
                        if Decimal(existing['price']) != Decimal(price) or existing['underlying'] != row.underlying:
                            raise GoldLiveError('Conflicting Gold source revisions require review')
                        continue
                    self.connection.execute('INSERT INTO gold_ticks VALUES (?,?,?,?,?)',
                        (row.event_time, row.received_at, row.venue_received_at, price, row.underlying))
                forecast = snapshot.get('forecast') or {}
                self.connection.execute('INSERT INTO gold_observations(observed_at,ticker,payload_json) VALUES (?,?,?)',
                    (now, forecast.get('ticker'), encoded))
        except sqlite3.Error:
            raise GoldLiveError('The Gold forecast evidence could not be committed') from None

    def close(self):
        self.connection.close()


class GoldObservationService:
    """Collect independently identified Gold data; exchange operations are GETs."""

    def __init__(self, client, archive_path, risk, *, stream, config=None, clock=time.time,
                 bet_budget='1', gaussian_sizing=None, uniform_sizing=None, entry_schedule='every-minute',
                 warmup_seconds=0, cadence_seconds=60, initial_bet_budget=None, early_bet_budget=None):
        self.client, self.risk, self.stream, self.clock = client, risk, stream, clock
        self.uniform_sizing = uniform_sizing
        self.config = ForecastConfig(min_history_seconds=300) if config is None else config
        self.definition = gold_model_definition(self.config)
        if (getattr(client, 'environment', None) != 'production'
                or getattr(client, '_allow_writes', None) is not False
                or entry_schedule != 'every-minute' or risk.required_series not in (None, GOLD_SERIES)
                or risk.max_signal_age_seconds > 5 or risk.max_quote_age_seconds > 5):
            raise GoldLiveError('Gold observations require production reads and the recurring bounded schedule')
        self.schedule, self.slot_budgets, self.market_budget = recurring_budget_plan(bet_budget,
            initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
            warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
            gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing)
        self.archive = _GoldArchive(archive_path, self.definition)

    def close(self):
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _unavailable(self, reason, now):
        return dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
            asset='GOLD', strategy='minute_five', status='unavailable', reason=reason,
            fetched_at=_iso(now) if _finite(now) else None, forecast=None, market_snapshot=None,
            execution_eligible=False, model_version=GOLD_MODEL_VERSION,
            underlying=GOLD_UNDERLYING, settlement_statistic='terminal_candle_close')

    def sample(self):
        try:
            started = self.clock()
            state = self.stream.snapshot()
            if (not isinstance(state, dict) or state.get('status') != 'ready'
                    or state.get('underlying') != GOLD_UNDERLYING):
                raise GoldLiveError('The exact live Pyth Gold feed is unavailable; check its data access and connection')
            observations = state.get('observations')
            quote_started = self.clock()
            market = discover_market(self.client, quote_started, min_seconds_to_close=1, asset='GOLD')
            raw_book = self.client.get_orderbook(market['ticker'])
            features = book_summary(raw_book)
            quote_received = self.clock()
            now = self.clock()
            if not started <= quote_started <= quote_received <= now or now - started > 5:
                raise GoldLiveError('Gold quote acquisition exceeded its five-second freshness bound')
            forecast = gold_forecast(market, observations, now, self.config)
            if forecast['status'] != 'ready':
                raise GoldLiveError(forecast['reason'])
            completed = self.clock()
            if (not now <= completed or completed - started > 5
                    or completed - forecast['model_time'] > self.risk.max_signal_age_seconds
                    or completed - quote_received > self.risk.max_quote_age_seconds):
                raise GoldLiveError('The Gold model or quote became stale before its observation completed')
            forecast['forecast_completed_at'] = completed
            opened = _timestamp(market['open_time'])
            slot = self.schedule.slot_at(completed - opened)
            eligibility = dict(kind='kalshi_minute_ladder_timing', version=1, mode='observation',
                asset='GOLD', status='ready' if slot is not None else 'blocked',
                execution_eligible=False, entry_schedule='every-minute',
                market_budget=budget_text(self.market_budget), slot_index=slot,
                budget=None if slot is None else budget_text(self.slot_budgets[slot]),
                minutes_remaining=None if slot is None else (900 - self.schedule.slot_start(slot)) / 60,
                reason='Timing observation only; the shared executor checks account state and all live limits')
            if self.uniform_sizing is not None:
                planned_total = sum(self.slot_budgets, Decimal(0))
                eligibility.update(sizing_profile='uniform', uniform_sizing=self.uniform_sizing.to_dict(),
                    budget_phase=None if slot is None else 'uniform',
                    initial_budget=None, regular_budget=None, early_budget=None,
                    planned_total=format(planned_total, 'f'),
                    unspent_budget=format(self.market_budget - planned_total, 'f'))
            result = dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
                asset='GOLD', strategy='minute_five', status='ready', fetched_at=_iso(completed),
                reason='Read-only exact-feed Gold terminal-close model; the shared executor authorizes each order',
                forecast=forecast, eligibility=eligibility, execution_eligible=False,
                market_snapshot=dict(market_status='ready', fetched_at=_iso(quote_received),
                    market=market, features=features), entry_schedule='every-minute',
                model_version=GOLD_MODEL_VERSION, underlying=GOLD_UNDERLYING,
                settlement_statistic='terminal_candle_close')
            self.archive.record(observations, result, completed)
            return result
        except Exception as error:
            message = str(error) if isinstance(error, (GoldLiveError, Btc15mError, KalshiError)) else 'Gold source, quote or local forecast archive is unavailable'
            return self._unavailable(message, self.clock())
