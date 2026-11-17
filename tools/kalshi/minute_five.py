"""Minute-five model observations and fixed-budget paper entries.

This service never constructs an execution client or a Kelly signal. One atomic
entry is held to official settlement; model changes cannot reverse the holding.
Analytic and fitted probability-calibration variants use the observation-only
service, with a separate explicitly configured executor owning order decisions.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import time

from .btc15m import Btc15mError, discover_market
from .client import KalshiError
from .calibrated_model import CalibrationModel, CalibrationModelError, apply_calibration
from .population_probability import population_probability
from .preclose_probability import preclose_probability
from .crypto_assets import asset_spec
from .engine import EngineError, _levels
from .fitted_parameters import FittedParameters
from .forecast import ForecastConfig
from .entry_schedule import EntrySchedule, EntryScheduleError
from .live_probability import live_probability, analytic_probability
from .portfolio import PortfolioError
from .shadow import ShadowError, _payout_identity, book_summary


ENTRY_ELAPSED_SECONDS = 300
ENTRY_WINDOW_SECONDS = 60
BET_BUDGET = '1'
STRATEGY_SETTINGS = dict(entry_elapsed_seconds=ENTRY_ELAPSED_SECONDS,
    entry_window_seconds=ENTRY_WINDOW_SECONDS, budget=BET_BUDGET,
    hold_to_settlement=True, decision='median_settlement_average')


class MinuteFiveError(ValueError):
    """Fixed, non-sensitive strategy configuration or input error."""


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        stamp = parsed.timestamp()
        if not math.isfinite(stamp) or stamp <= 0:
            raise ValueError()
        return stamp
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        raise MinuteFiveError('The minute-five contract timestamp is invalid') from None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _matching_fit(asset, probability_fit):
    if probability_fit is not None and not isinstance(probability_fit, FittedParameters):
        raise MinuteFiveError('A verified historical probability fit is required')
    return probability_fit if probability_fit is not None and probability_fit.asset == asset else None


def minute_five_forecast_config(config=None, *, asset='BTC', probability_fit=None, probability_calibration=None,
                                garch_model=None):
    """Use the same fitted arithmetic model as the live monitor, if configured."""
    asset_spec(asset)
    config = ForecastConfig() if config is None else config
    if not isinstance(config, ForecastConfig):
        raise MinuteFiveError('A valid minute-five Monte Carlo configuration is required')
    if garch_model is not None:
        from .garch_live import LiveGarchModel
        if (not isinstance(garch_model, LiveGarchModel) or asset != 'ETH' or garch_model.asset != asset
                or probability_fit is not None or probability_calibration is not None):
            raise MinuteFiveError('ETH GARCH requires its own verified model without another probability fit or calibration')
        # GARCH has a separate minute-return prefix. Its raw preclose geometry
        # retains the frozen bounded forecast configuration and native proof.
        return ForecastConfig(**garch_model.base_model_definition['config']['forecast'])
    if probability_calibration is not None and not isinstance(probability_calibration, CalibrationModel):
        raise MinuteFiveError('A verified asset probability calibration is required')
    if probability_calibration is not None and probability_calibration.base_decision_model == 'preclose':
        if probability_calibration.asset != asset or probability_fit is not None:
            raise MinuteFiveError('The preclose calibration requires its own asset-bound volatility model')
        bound = ForecastConfig(**probability_calibration.base_model_definition['config']['forecast'])
        if config.paths != bound.paths or config.seed != bound.seed:
            raise MinuteFiveError('Path identity differs from the selected preclose calibration')
        return bound
    fit = _matching_fit(asset, probability_fit)
    return (config if fit is None else replace(config,
        ewma_half_life_seconds=fit.half_life_seconds,
        max_history_seconds=fit.history_window_seconds,
        min_history_seconds=max(config.min_history_seconds, fit.min_live_history_seconds)))


def minute_five_model_definition(config=None, *, asset='BTC', probability_fit=None,
                                 decision_model='monte-carlo', probability_calibration=None,
                                 preclose_volatility_scale=1.0, garch_model=None):
    if decision_model not in ('monte-carlo', 'analytic', 'population', 'preclose', 'calibrated', 'garch'):
        raise MinuteFiveError('Choose the Monte Carlo, analytic or calibrated decision model')
    if decision_model == 'garch':
        from .garch_live import LiveGarchModel
        if not isinstance(garch_model, LiveGarchModel):
            raise MinuteFiveError('The GARCH model requires a verified ETH GARCH artifact')
        minute_five_forecast_config(config, asset=asset, probability_fit=probability_fit,
            probability_calibration=probability_calibration, garch_model=garch_model)
        return garch_model.definition()
    if garch_model is not None:
        raise MinuteFiveError('An ETH GARCH artifact requires the GARCH decision model')
    if decision_model == 'calibrated':
        if not isinstance(probability_calibration, CalibrationModel) or probability_calibration.asset != asset:
            raise MinuteFiveError('The calibrated model requires an asset-matched fitted probability artifact')
        actual = minute_five_forecast_config(config, asset=asset, probability_fit=probability_fit,
            probability_calibration=probability_calibration)
        raw = minute_five_model_definition(actual, asset=asset, probability_fit=probability_fit,
            decision_model=probability_calibration.base_decision_model,
            preclose_volatility_scale=probability_calibration.base_model_definition['config'].get('volatility_scale', 1.0))
        if json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False) != json.dumps(
                probability_calibration.base_model_definition, sort_keys=True, separators=(',', ':'), allow_nan=False):
            raise MinuteFiveError('The calibration artifact differs from the configured base probability model')
        profile = asset_spec(asset)
        result = dict(model_version='cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-calibrated-v1',
            config=dict(raw['config']))
        result['config'].update(decision='calibrated_probability_majority',
            estimator='logistic_probability_calibration',
            base_decision_model=probability_calibration.base_decision_model,
            probability_calibration=dict(artifact_sha256=probability_calibration.sha256,
                parameters=probability_calibration.parameters, live_validated=False))
        return result
    if probability_calibration is not None:
        raise MinuteFiveError('A probability calibration artifact requires the calibrated decision model')
    profile = asset_spec(asset)
    actual = minute_five_forecast_config(config, asset=asset, probability_fit=probability_fit)
    fit = _matching_fit(asset, probability_fit)
    result = dict(model_version='cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-median-v1',
        config=dict(forecast=asdict(actual), **STRATEGY_SETTINGS, fit=None if fit is None else dict(
            artifact_sha256=fit.artifact_sha256, half_life_seconds=fit.half_life_seconds,
            volatility_scale=fit.volatility_scale)))
    if decision_model == 'preclose':
        from .asset_calibration_artifact import preclose_base_model_definition
        if fit is not None:
            raise MinuteFiveError('A preclose model cannot import a legacy volatility fit')
        result = preclose_base_model_definition(asset, dict(half_life_seconds=actual.ewma_half_life_seconds,
            history_seconds=int(actual.max_history_seconds), volatility_scale=preclose_volatility_scale))
        if result['config']['forecast'] != asdict(actual):
            raise MinuteFiveError('The preclose forecast configuration differs from its exact model definition')
    elif decision_model == 'analytic':
        result['model_version'] = ('cf-' + profile.index_id.lower().replace('_', '-')
                                   + '-minute-five-analytic-v1')
        result['config'].update(decision='analytic_settlement_probability',
            estimator='analytic_gaussian', settlement_payoff='verified_rounded_average_v1')
    elif decision_model == 'population':
        if (fit is not None or actual.ewma_half_life_seconds != 300
                or actual.max_history_seconds != 1800):
            raise MinuteFiveError('The historical population base requires an unfitted 300-second EWMA and 1800-second history')
        result['model_version'] = ('cf-' + profile.index_id.lower().replace('_', '-')
                                   + '-minute-five-population-v1')
        result['config'].update(decision='population_settlement_probability',
            estimator='population_gaussian', settlement_payoff='legacy_unrounded_average_v1')
    return result


def _safe(error):
    if isinstance(error, (MinuteFiveError, PortfolioError, ShadowError, EngineError, Btc15mError,
                          KalshiError, CalibrationModelError)):
        return str(error)
    return 'Minute-five paper inputs or local configuration are unavailable'


def _identity(market):
    result = {key: market.get(key) for key in (
        'ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
        'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type')}
    for key in ('floor_strike', 'notional_value_dollars'):
        result[key] = str(result[key])
    return result


class MinuteFivePaperService:
    """Record fresh CF inputs and place at most one simulated fixed-budget buy."""

    def __init__(self, client, archive, portfolio, *, config=None, clock=time.time,
                 asset='BTC', probability_fit=None, observation_only=False,
                 decision_model='monte-carlo', probability_calibration=None, garch_model=None):
        if type(observation_only) is not bool:
            raise MinuteFiveError('Observation-only mode must be Boolean')
        if decision_model not in ('monte-carlo', 'analytic', 'population', 'preclose', 'calibrated', 'garch'):
            raise MinuteFiveError('Choose the Monte Carlo, analytic or calibrated decision model')
        if decision_model in ('analytic', 'population', 'preclose', 'calibrated', 'garch') and not observation_only:
            raise MinuteFiveError('Analytic and calibrated decisions require the observation-only service')
        self.decision_model = decision_model
        self.observation_only = observation_only
        self.profile = asset_spec(asset)
        self.asset, self.client, self.archive, self.portfolio = asset, client, archive, portfolio
        self.config = minute_five_forecast_config(config, asset=asset, probability_fit=probability_fit,
            probability_calibration=probability_calibration, garch_model=garch_model)
        self.fit = _matching_fit(asset, probability_fit)
        self.clock, self._settlement_cursor = clock, 0
        self.definition = minute_five_model_definition(self.config, asset=asset, probability_fit=self.fit,
            decision_model=decision_model, probability_calibration=probability_calibration,
            garch_model=garch_model)
        self.probability_calibration = probability_calibration
        self.garch_model = garch_model
        self.base_decision_model = (probability_calibration.base_decision_model
            if decision_model == 'calibrated' else 'preclose' if decision_model == 'garch' else decision_model)
        self.raw_definition = (garch_model.base_model_definition if decision_model == 'garch' else
            minute_five_model_definition(self.config, asset=asset, probability_fit=self.fit,
            decision_model=self.base_decision_model,
            preclose_volatility_scale=(probability_calibration.base_model_definition['config'].get('volatility_scale', 1.0)
                if probability_calibration is not None else 1.0)))
        self.risk = portfolio.risk
        if (getattr(client, 'environment', None) != 'production'
                or getattr(client, '_allow_writes', None) is not False
                or getattr(portfolio, 'strategy', None) != 'minute_five'
                or getattr(portfolio, 'strategy_config', None) != STRATEGY_SETTINGS
                or self.asset not in getattr(portfolio, 'assets', ())
                or self.risk.required_series not in (None, self.profile.series)
                or self.risk.max_signal_age_seconds > 5 or self.risk.max_quote_age_seconds > 5):
            raise MinuteFiveError('The minute-five strategy requires isolated production reads and its fixed-budget paper portfolio')
        if (getattr(archive, 'asset', None) != asset or archive.config != self.config
                or archive.model_definition != self.definition):
            raise MinuteFiveError('Minute-five configuration differs from its immutable forecast archive')

    def _unavailable(self, reason):
        result = dict(kind='kalshi_algorithm_state', version=1, status='unavailable',
            asset=self.asset, strategy='minute_five', reason=reason, fetched_at=None,
            forecast=None, eligibility=None, paper_order=None)
        try:
            result['portfolio'] = self.portfolio.snapshot()
        except Exception:
            pass
        return result

    def _settle_one(self):
        positions = [row for row in self.portfolio.snapshot()['positions']
                     if row['ticker'].startswith(self.profile.series + '-')]
        if not positions:
            return None
        position = positions[self._settlement_cursor % len(positions)]
        self._settlement_cursor += 1
        try:
            return self.portfolio.settle_market(self.client.get_market(position['ticker']), self.clock())
        except Exception:
            # Settlement errors retain positions. They never authorize entry or
            # fabricate a payout from a current model or proxy spot price.
            return None

    def _fresh(self, now, started, received, quote_started, quote_received, model_time):
        return (all(_finite(value) for value in (now, started, received, quote_started, quote_received, model_time))
            and started <= received <= quote_started <= quote_received <= now
            and 0 <= now - started <= 5 and 0 <= now - model_time <= 5)

    def sample(self):
        try:
            settlement = self._settle_one()
            started = self.clock()
            asset_kw = {} if self.asset == 'BTC' else {'asset': self.asset}
            market = None
            acquisition_started = started
            if self.observation_only:
                # Discover the exact contract before requesting its source
                # observations, so discovery latency does not age those ticks.
                # ``started`` still bounds the entire acquisition to five
                # seconds; the archive records the actual index request time.
                market = discover_market(self.client, started, min_seconds_to_close=0, **asset_kw)
                acquisition_started = self.clock()
            payload = (self.client.get_cfbenchmarks_values() if self.asset == 'BTC' else
                       self.client.get_cfbenchmarks_values(index_id=self.profile.index_id))
            received = self.clock()
            acquisition = self.archive.ingest(payload, acquisition_started, received)
            quote_started = self.clock()
            if market is None:
                market = discover_market(self.client, quote_started, min_seconds_to_close=0, **asset_kw)
            book = self.client.get_orderbook(market['ticker'])
            quote_received = self.clock()
            features = book_summary(book)
            now = self.clock()
            if not started <= received <= quote_started <= quote_received <= now or now - started > 5:
                raise MinuteFiveError('Minute-five index and quote acquisition exceeded the freshness limit')
            self.portfolio.mark_hold_position(market, book, quote_received)
            estimator = (analytic_probability if self.base_decision_model == 'analytic' else
                         preclose_probability if self.base_decision_model == 'preclose' else
                         population_probability if self.base_decision_model == 'population' else live_probability)
            history = (self.archive.history(now, lookback_seconds=self.config.max_history_seconds + 5)
                       if self.base_decision_model in ('population', 'preclose') else self.archive.history(now))
            model = estimator(market, history, now, self.config,
                volatility_scale=(self.raw_definition['config']['volatility_scale']
                    if self.base_decision_model == 'preclose' else 1.0 if self.fit is None else self.fit.volatility_scale),
                **(dict(include_scenarios=False) if self.observation_only
                   and self.base_decision_model == 'monte-carlo' else {}), **asset_kw)
            if model['status'] != 'ready':
                raise MinuteFiveError(model['reason'])
            completed = self.clock()
            if not self._fresh(completed, started, received, quote_started, quote_received, model['model_time']):
                raise MinuteFiveError('The minute-five Monte Carlo estimate became stale before the decision')
            diagnostics = model['diagnostics']
            median = diagnostics.get('settlement_average_median')
            centered = diagnostics.get('settlement_centered_median')
            p_yes, p_no = model.get('probability_yes'), model.get('probability_no')
            if (not _finite(median) or not _finite(centered)
                    or any(not _finite(p) or not 0 <= p <= 1 for p in (p_yes, p_no))
                    or abs(p_yes + p_no - 1) > 1e-10):
                raise MinuteFiveError('The Monte Carlo settlement median or probabilities are unavailable')
            outcome = 'yes' if (centered >= 0 if market['strike_type'] == 'greater_or_equal' else centered > 0) else 'no'
            if self.base_decision_model in ('analytic', 'preclose'):
                outcome = 'yes' if (p_yes >= .5 if market['strike_type'] == 'greater_or_equal' else p_yes > .5) else 'no'
            opened, closed = _stamp(market['open_time']), _stamp(market['close_time'])
            forecast = dict(kind='kalshi_minute_five_forecast', version=1, status='ready',
                asset=self.asset, source=self.profile.source, model_version=self.raw_definition['model_version'],
                as_of=model['as_of'], model_time=model['model_time'], ticker=market['ticker'],
                probability_yes=p_yes, probability_no=p_no,
                median_settlement_average=median, settlement_centered_median=centered,
                median_outcome=outcome, target_price=str(market['floor_strike']),
                strike_type=market['strike_type'], contract_identity=_identity(market),
                monte_carlo_se=model['monte_carlo_se'], paths=diagnostics['paths'],
                sampling_interval=model['sampling_interval'], forecast_completed_at=completed,
                calibrated=False, execution_eligible=False,
                reason='Median of simulated final-minute settlement averages; fixed-budget paper decision')
            if self.base_decision_model == 'analytic':
                forecast.update(decision_model='analytic', decision_estimator='analytic_gaussian',
                    payoff_metadata=model['payoff_metadata'],
                    analytic_probability_yes=model['analytic_probability_yes'],
                    reason='Analytic verified rounded-average probability; observation-only decision')
            elif self.base_decision_model == 'population':
                forecast.update(decision_model='population', decision_estimator='population_gaussian',
                    population_proof=model['population_proof'],
                    analytic_probability_yes=model['analytic_probability_yes'],
                    reason='Exact population probability for the historically bound unrounded settlement approximation')
            elif self.base_decision_model == 'preclose':
                forecast.update(decision_model='preclose', decision_estimator='preclose_gaussian',
                    preclose_proof=model['preclose_proof'], payoff_metadata=model['payoff_metadata'],
                    settlement_grid='preclose_60_seconds_v1',
                    analytic_probability_yes=model['analytic_probability_yes'],
                    reason='Exact native preclose rounded-average probability with asset-bound volatility calibration')
            if self.fit is not None:
                forecast['fitted_parameters'] = self.fit.metadata()
            if self.decision_model == 'calibrated':
                forecast = apply_calibration(forecast, self.probability_calibration)
                forecast['forecast_completed_at'] = self.clock()
                if not self._fresh(forecast['forecast_completed_at'], started, received,
                                   quote_started, quote_received, model['model_time']):
                    raise MinuteFiveError('The calibrated probability became stale before the decision')
                p_yes, p_no, outcome = (forecast['probability_yes'], forecast['probability_no'],
                                       forecast['median_outcome'])
            elif self.decision_model == 'garch':
                from .garch_live import apply_live_garch
                try:
                    minute_history = self.archive.garch_minute_history(now)
                    forecast = apply_live_garch(forecast, minute_history, self.garch_model)
                except ValueError as error:
                    raise MinuteFiveError(str(error)) from None
                forecast['forecast_completed_at'] = self.clock()
                if not self._fresh(forecast['forecast_completed_at'], started, received,
                                   quote_started, quote_received, model['model_time']):
                    raise MinuteFiveError('The GARCH probability became stale before the decision')
                p_yes, p_no, outcome = (forecast['probability_yes'], forecast['probability_no'],
                                       forecast['median_outcome'])
            self.archive.record_forecast(market, book, features, forecast,
                quote_started_at=quote_started, quote_received_at=quote_received, index_acquisition_id=acquisition)
            yes = _levels(book['orderbook_fp']['yes_dollars'])
            no = _levels(book['orderbook_fp']['no_dollars'])
            fee_meta = market.get('_crypto15m') or market.get('_btc15m') or {}
            required_fee = Decimal(str(fee_meta['fee_reserve_lower_bound']))
            if not required_fee.is_finite() or not 0 <= required_fee < 1:
                raise MinuteFiveError('The current entry fee bound is invalid')
            quote = dict(ticker=market['ticker'], target_price=str(market['floor_strike']),
                open_time=market['open_time'], close_time=market['close_time'],
                contract_identity=_identity(market), fetched_at=_iso(quote_received),
                fee_reserve=str(required_fee), yes_ask=format(Decimal(1) - no[-1][0], '.4f'),
                no_ask=format(Decimal(1) - yes[-1][0], '.4f'),
                yes_available=format(no[-1][1], '.2f'), no_available=format(yes[-1][1], '.2f'))
            decision_now = self.clock()
            elapsed, model_elapsed = decision_now - opened, model['model_time'] - opened
            held = self.portfolio.exposure(market['ticker'])['quantity'] > 0
            in_window = ENTRY_ELAPSED_SECONDS <= elapsed < ENTRY_ELAPSED_SECONDS + ENTRY_WINDOW_SECONDS
            source_in_window = model_elapsed >= ENTRY_ELAPSED_SECONDS
            fresh = self._fresh(decision_now, started, received, quote_started, quote_received, model['model_time'])
            fee_valid = self.portfolio.entry_fee >= required_fee
            cutoff_valid = closed - decision_now >= self.risk.min_seconds_to_close
            checks = [
                dict(id='fixed_budget', label='Fixed one-dollar budget', passed=True,
                     reason='At most one dollar including entry fees; whole contracts only.'),
                dict(id='minute_five_window', label='Minute-five entry window', passed=in_window,
                     reason='New entries are permitted only from elapsed 300 through 359.999 seconds.'),
                dict(id='model_clock', label='Minute-five source observation', passed=source_in_window,
                     reason='The current CF model observation must be at or after elapsed 300 seconds.'),
                dict(id='entry_cutoff', label='Startup entry cutoff', passed=cutoff_valid,
                     reason='The entry must preserve the startup minimum seconds remaining.'),
                dict(id='fresh_inputs', label='Fresh model and executable quote', passed=fresh,
                     reason='Acquisition, model and quote must remain within five seconds.'),
                dict(id='entry_fee', label='Current entry fee allowance', passed=fee_valid,
                     reason='The configured entry fee allowance must cover the supplied venue fee bound.'),
                dict(id='one_entry', label='One entry per contract', passed=not held,
                     reason='A filled contract is held to official settlement without top-ups or reversals.'),
            ]
            phase = 'held' if held else 'waiting' if elapsed < ENTRY_ELAPSED_SECONDS else 'entry' if in_window else 'missed'
            reason = ('Holding this paper position to official settlement.' if held else
                      'Waiting for five minutes after the contract opens.' if phase == 'waiting' else
                      'The minute-five window passed; no late catch-up entry will be made.' if phase == 'missed' else
                      next((row['reason'] for row in checks if not row['passed']), 'Evaluating the fixed-budget entry.'))
            order = dict(status='none', action='hold', reason=reason, outcome=outcome,
                count='0', reserved_cost='0', client_order_id=None)
            if all(row['passed'] for row in checks) and not self.observation_only:
                current = self.client.get_market(market['ticker'])
                if current.get('status') != 'active':
                    raise MinuteFiveError('The contract is no longer active for a minute-five paper entry')
                if _payout_identity(current) != _payout_identity(market):
                    raise MinuteFiveError('Contract terms changed before the minute-five paper entry')
                decision_now = self.clock()
                elapsed = decision_now - opened
                if (not self._fresh(decision_now, started, received, quote_started, quote_received, model['model_time'])
                        or not ENTRY_ELAPSED_SECONDS <= elapsed < ENTRY_ELAPSED_SECONDS + ENTRY_WINDOW_SECONDS
                        or closed - decision_now < self.risk.min_seconds_to_close
                        or decision_now >= closed):
                    raise MinuteFiveError('Minute-five inputs or entry window expired before the paper entry')
                side_probability = p_yes if outcome == 'yes' else p_no
                order = self.portfolio.buy_once_fixed_budget(market, book, decision_now,
                    outcome=outcome, probability=side_probability,
                    raw_probability=side_probability, budget=BET_BUDGET)
                reason = order['reason']
            filled = order.get('status') == 'paper_filled' and order.get('action') == 'buy'
            checks.append(dict(id='fixed_budget_fill', label='Affordable whole-contract entry', passed=filled,
                reason=reason))
            gate = dict(kind='kalshi_fixed_budget_eligibility', version=1, mode='paper',
                status='eligible' if filled else 'blocked', asset=self.asset, as_of=decision_now,
                selected_outcome=outcome, paper_eligible=filled, live_eligible=False, execution_eligible=False,
                budget=BET_BUDGET, entry_elapsed_seconds=ENTRY_ELAPSED_SECONDS,
                entry_window_seconds=ENTRY_WINDOW_SECONDS, elapsed_seconds=elapsed,
                model_elapsed_seconds=model_elapsed, phase=phase,
                hold_to_settlement=True, checks=checks, reason=reason,
                additional_contracts=order.get('count', '0') if filled else '0',
                reserved_cost=order.get('reserved_cost', '0') if filled else '0')
            if self._fresh(self.clock(), started, received, quote_started, quote_received, model['model_time']):
                self.portfolio.mark_hold_position(market, book, quote_received, probability_yes=p_yes)
            if order['status'] == 'none' and settlement is not None:
                order = settlement
            return dict(kind='kalshi_algorithm_state', version=1, status='ready',
                asset=self.asset, strategy='minute_five', fetched_at=_iso(self.clock()),
                forecast=forecast, eligibility=gate, paper_order=order, portfolio=self.portfolio.snapshot(),
                market_snapshot=dict(market_status='ready', fetched_at=quote['fetched_at'],
                    market={key: quote[key] for key in ('ticker', 'target_price', 'open_time', 'close_time', 'yes_ask', 'no_ask')}),
                reason='Minute-five median-choice paper experiment; one fixed-budget entry held to settlement')
        except Exception as error:
            reason = _safe(error)
            try:
                self.archive.record_error(reason, self.clock())
            except Exception:
                pass
            return self._unavailable(reason)


class _ObservationPortfolio:
    """No ledger or cash: supplies only the read-only model policy interface."""

    strategy = 'minute_five'
    strategy_config = STRATEGY_SETTINGS

    def __init__(self, risk, assets):
        self.risk, self.assets = risk, tuple(assets)
        self.entry_fee = risk.fee_reserve_per_contract

    def snapshot(self):
        return {'positions': []}

    def exposure(self, ticker):
        return {'quantity': 0}

    def mark_hold_position(self, *args, **kwargs):
        return None


class MinuteFiveObservationService(MinuteFivePaperService):
    """Collect the identical median forecast without creating simulated fills.

    A separately authorized executor owns account reads, durable order intents,
    price limits and all exchange mutations. This collector remains read-only.
    """

    def __init__(self, client, archive, risk, *, assets=('BTC',), bet_budget='1',
                 initial_bet_budget=None, early_bet_budget=None, gaussian_sizing=None, uniform_sizing=None,
                 entry_schedule='once',
                 warmup_seconds=300, cadence_seconds=60, regime_config=None, **kwargs):
        from .minute_five_policy import parse_bet_budget
        self.regime_config = regime_config
        if regime_config is not None:
            from .regime import RegimeConfig
            if not isinstance(regime_config, RegimeConfig):
                raise MinuteFiveError('A validated crypto regime configuration is required')
            if (not isinstance(assets, (list, tuple)) or not assets
                    or len(set(assets)) != len(assets)
                    or not set(assets) <= {'BTC', 'ETH', 'SOL', 'XRP'}
                    or kwargs.get('asset', 'BTC') not in assets):
                raise MinuteFiveError('Regime observations require a selected supported crypto asset')
        self.observation_bet_budget = parse_bet_budget(bet_budget)
        self.observation_initial_bet_budget = (self.observation_bet_budget if initial_bet_budget is None
            else parse_bet_budget(initial_bet_budget))
        self.observation_early_bet_budget = None if early_bet_budget is None else parse_bet_budget(early_bet_budget)
        self.gaussian_sizing, self.observation_slot_budgets = gaussian_sizing, None
        self.uniform_sizing = uniform_sizing
        self.time_sizing = gaussian_sizing if gaussian_sizing is not None else uniform_sizing
        self.time_sizing_name = ('gaussian' if gaussian_sizing is not None else
            'uniform' if uniform_sizing is not None else 'fixed')
        try:
            self.schedule = EntrySchedule(warmup_seconds, cadence_seconds,
                early_entries=self.observation_early_bet_budget is not None)
        except EntryScheduleError as error:
            raise MinuteFiveError(str(error)) from None
        if entry_schedule not in ('once', 'every-minute'):
            raise MinuteFiveError('Choose a once-only entry or the recurring fixed-budget schedule')
        self.entry_schedule = entry_schedule
        if self.time_sizing is not None and entry_schedule != 'every-minute':
            raise MinuteFiveError(f'{self.time_sizing_name.capitalize()} sizing requires recurring entries')
        if entry_schedule == 'once' and self.schedule != EntrySchedule():
            raise MinuteFiveError('Custom warmup and cadence require recurring entries')
        if entry_schedule == 'once' and self.observation_initial_bet_budget != self.observation_bet_budget:
            raise MinuteFiveError('A separate initial bet budget requires recurring entries')
        if entry_schedule == 'once' and self.observation_early_bet_budget is not None:
            raise MinuteFiveError('Early bet budgets require recurring entries')
        if self.time_sizing is not None:
            from .minute_five_policy import recurring_budget_plan
            self.schedule, self.observation_slot_budgets, self.observation_market_budget = recurring_budget_plan(
                self.observation_bet_budget, warmup_seconds=warmup_seconds, cadence_seconds=cadence_seconds,
                initial_bet_budget=initial_bet_budget, early_bet_budget=early_bet_budget,
                gaussian_sizing=gaussian_sizing, uniform_sizing=uniform_sizing)
        else:
            self.observation_market_budget = ((self.observation_early_bet_budget or Decimal(0)) * self.schedule.early_entry_count +
                self.observation_initial_bet_budget + (self.schedule.main_entry_count - 1) * self.observation_bet_budget)
        if entry_schedule == 'every-minute' and kwargs.get('config') is None:
            kwargs['config'] = ForecastConfig(min_history_seconds=(300 if self.time_sizing is not None else
                max(150, min(warmup_seconds, 1800))))
        super().__init__(client, archive, _ObservationPortfolio(risk, assets),
                         observation_only=True, **kwargs)
        if regime_config is not None and self.config.max_history_seconds < regime_config.lookback_seconds:
            raise MinuteFiveError('The probability archive history is shorter than the crypto regime lookback')

    def sample(self):
        result = super().sample()
        result['kind'] = 'kalshi_minute_five_observation'
        result['observation_only'] = True
        result.pop('portfolio', None)
        result.pop('paper_order', None)
        if result.get('forecast') is not None and self.decision_model not in ('calibrated', 'garch'):
            result['forecast']['reason'] = ('Analytic verified rounded-average probability; observation only'
                if self.decision_model == 'analytic' else
                'Verified rounded preclose-average probability; observation only'
                if self.decision_model == 'preclose' else
                'Exact population probability for the unrounded settlement approximation; observation only'
                if self.decision_model == 'population' else
                'Median of simulated final-minute settlement averages; observation only')
        if result.get('eligibility') is not None:
            from .minute_five_policy import budget_text
            gate = result['eligibility']
            gate['mode'] = 'observation'
            gate['budget'] = budget_text(self.observation_bet_budget)
            gate['regular_budget'] = None if self.time_sizing is not None else budget_text(self.observation_bet_budget)
            gate['initial_budget'] = None if self.time_sizing is not None else budget_text(self.observation_initial_bet_budget)
            gate['early_budget'] = (None if self.observation_early_bet_budget is None
                else budget_text(self.observation_early_bet_budget))
            for check in gate['checks']:
                if check['id'] == 'fixed_budget':
                    check['label'] = 'Configured live per-bet budget'
                    check['reason'] = f"At most ${gate['budget']} including entry fees; the live executor verifies affordable whole contracts."
            if self.entry_schedule == 'every-minute':
                forecast = result['forecast']
                now = self.clock()
                identity = forecast['contract_identity']
                opened, closed = _stamp(identity['open_time']), _stamp(identity['close_time'])
                elapsed = now - opened
                slot = self.schedule.slot_at(elapsed)
                early_phase = slot is not None and slot < self.schedule.early_entry_count
                initial_phase = slot == self.schedule.early_entry_count
                active_cadence = 60 if early_phase else self.schedule.cadence_seconds
                active_budget = (self.observation_early_bet_budget if early_phase else
                    self.observation_initial_bet_budget if initial_phase else self.observation_bet_budget)
                budget_phase = None if slot is None else 'early' if early_phase else 'initial' if initial_phase else 'regular'
                if self.observation_slot_budgets is not None:
                    active_budget = None if slot is None else self.observation_slot_budgets[slot]
                    budget_phase = None if slot is None else self.time_sizing_name
                gate['budget'] = None if active_budget is None else budget_text(active_budget)
                in_window = slot is not None and closed - now >= 1
                max_source_age = min(5, self.risk.max_signal_age_seconds)
                fast_slot = slot is not None and self.schedule.is_fast_slot(slot, max_source_age)
                source_floor = (None if slot is None else
                    self.schedule.source_floor(slot, max_source_age))
                source_start = None if source_floor is None else opened + source_floor
                source_current = source_start is not None and source_start <= forecast['model_time'] <= now
                checks = [row for row in gate['checks'] if row['id'] in ('fixed_budget', 'fresh_inputs', 'entry_fee')]
                for check in checks:
                    if check['id'] == 'fixed_budget':
                        check['reason'] = (f'No current {self.time_sizing_name.capitalize()} entry window is available.' if active_budget is None else
                            f"At most ${gate['budget']} including entry fees; the live executor verifies affordable whole contracts.")
                checks.extend([
                    dict(id='warmup', label=f'Market warmup ({self.schedule.warmup_seconds}s)',
                         passed=elapsed >= 0 and (self.schedule.early_entries or elapsed >= self.schedule.warmup_seconds),
                         reason=(f'Early entries may begin at market opening; the main schedule begins at {self.schedule.warmup_seconds} seconds.'
                                 if self.schedule.early_entries else
                                 f'New entries begin {self.schedule.warmup_seconds} seconds after the verified market opens.')),
                    dict(id='minute_slot', label='Current recurring entry window', passed=in_window,
                         reason=f'One entry window every {active_cadence} seconds; no missed-window catch-up.'),
                    dict(id='slot_model_clock', label=('Fresh observation after warmup' if fast_slot else
                             'Observation from this entry window'), passed=source_current,
                         reason=('The source observation must follow the market warmup; its freshness is checked '
                                 'separately from the faster entry cadence.' if fast_slot else
                                 'The Monte Carlo source observation must be at or after the current entry window starts.')),
                ])
                ready = all(row['passed'] for row in checks)
                schedule_details = dict(self.schedule.to_dict(),
                    market_budget=budget_text(self.observation_market_budget))
                result['eligibility'] = dict(kind='kalshi_minute_ladder_timing', version=1,
                    mode='observation', entry_schedule='every-minute', status='ready' if ready else 'blocked',
                    phase='entry' if in_window else 'warming' if elapsed < self.schedule.warmup_seconds else 'closed',
                    asset=self.asset, as_of=now, budget=gate['budget'],
                    regular_budget=gate['regular_budget'],
                    initial_budget=gate['initial_budget'],
                    early_budget=gate['early_budget'], budget_phase=budget_phase, **schedule_details,
                    slot_index=slot, minutes_remaining=None if slot is None else (900 - self.schedule.slot_start(slot)) / 60,
                    seconds_remaining_at_slot_start=None if slot is None else 900 - self.schedule.slot_start(slot),
                    source_floor_elapsed_seconds=source_floor,
                    source_clock_policy='fresh_after_warmup' if fast_slot else 'current_slot',
                    seconds_to_close=closed - now, selected_outcome=forecast['median_outcome'],
                    execution_eligible=False, live_eligible=False, checks=checks,
                    reason=('Timing observations only; source freshness is independent of the fast entry cadence. '
                            'The executor checks account, current-window attempts and actual order limits.' if fast_slot else
                            'Timing observations only; the executor checks account, minute markers and actual order limits.'))
                if self.time_sizing is not None:
                    planned_total = sum(self.observation_slot_budgets, Decimal(0))
                    result['eligibility'].update(sizing_profile=self.time_sizing_name,
                        **{self.time_sizing_name + '_sizing': self.time_sizing.to_dict()},
                        planned_total=format(planned_total, 'f'),
                        unspent_budget=format(self.observation_market_budget - planned_total, 'f'))
                result['entry_schedule'] = 'every-minute'
        result['reason'] = ('Read-only minute-five forecast; the live executor independently checks the account and order'
                            if result['status'] == 'ready' else result['reason'])
        if self.regime_config is not None and result.get('status') == 'ready' and result.get('forecast') is not None:
            from .regime import classify_regime
            now = self.clock()
            try:
                proposal = classify_regime(self.archive.history(now), now, self.regime_config, asset=self.asset)
                if not isinstance(proposal, dict):
                    raise MinuteFiveError('The crypto regime proposal is unavailable')
            except Exception:
                # A regime input failure withholds its proposal without changing
                # the independently collected Monte Carlo probability estimate.
                proposal = dict(kind=('kalshi_btc_regime_proposal' if self.asset == 'BTC' else 'kalshi_crypto_regime_proposal'),
                    version=1, status='unavailable',
                    asset=self.asset, source=asset_spec(self.asset).source, as_of=now, model_time=None, regime='transition',
                    config=self.regime_config.to_dict(), features={}, calibrated=False,
                    execution_eligible=False, reason='Crypto regime history or classification is unavailable')
            proposal['contract_identity'] = dict(result['forecast']['contract_identity'])
            result['regime_proposal'] = proposal
        return result
