"""Chronological, offline value/Kelly replay of explicitly recorded evidence.

This module never constructs an exchange client. Displayed depth at a later
recorded book supports a simulated IOC counterfactual, not a claim that a real
order would have filled. Official outcomes affect cash only at receipt time.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
import math
from pathlib import Path
import re

from .execution_assets import execution_asset_from_ticker
from .engine import _timestamp
from .opposite_bias import _identity as _binary_identity


ZERO, ONE = Decimal(0), Decimal(1)
MAX_EVENTS = 200_000
_HASH = re.compile(r'[a-f0-9]{64}\Z')


class ValueReplayError(ValueError):
    """Bounded archive, chronology, or simulation configuration error."""


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise ValueReplayError('Replay timestamps must be finite positive UTC epoch numbers')
    return float(value)


def _decimal(value, name, *, positive=False, maximum=Decimal('1000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueReplayError('Replay ' + name + ' is invalid')
    text = str(value)
    if len(text) > 60:
        raise ValueReplayError('Replay ' + name + ' is too long')
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ValueReplayError('Replay ' + name + ' is invalid') from None
    if not result.is_finite() or not (ZERO < result if positive else ZERO <= result) or result > maximum:
        raise ValueReplayError('Replay ' + name + ' is outside its supported bounds')
    return result


def _money(value):
    return format(value, 'f')


@dataclass(frozen=True)
class ValueReplayConfig:
    initial_cash: Decimal = Decimal('100')
    max_drawdown_fraction: Decimal = Decimal('.10')
    decision_interval_seconds: float = 10
    execution_latency_seconds: float = .25
    max_execution_wait_seconds: float = 5
    max_model_age_seconds: float = 5
    max_quote_age_seconds: float = 5
    depth_fraction: Decimal = Decimal('.5')
    uniform_market_budget: Decimal = Decimal('100')
    uniform_cadence_seconds: int = 10
    allow_uncalibrated: bool = False
    walk_forward_calibration: bool = False
    calibration_refit_contracts: int = 50
    max_calibration_refits: int = 100
    max_events: int = MAX_EVENTS
    max_actions: int = 20_000

    def __post_init__(self):
        for name in ('initial_cash', 'uniform_market_budget'):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True,
                maximum=Decimal('1000000')))
        object.__setattr__(self, 'depth_fraction', _decimal(self.depth_fraction, 'depth fraction', positive=True,
            maximum=ONE))
        object.__setattr__(self, 'max_drawdown_fraction', _decimal(self.max_drawdown_fraction,
            'drawdown fraction', positive=True, maximum=ONE))
        if self.max_drawdown_fraction >= ONE:
            raise ValueReplayError('The drawdown fraction must be strictly below one')
        for name, low, high in (('decision_interval_seconds', .001, 900),
                ('execution_latency_seconds', 0, 60), ('max_execution_wait_seconds', .001, 60),
                ('max_model_age_seconds', .001, 5), ('max_quote_age_seconds', .001, 5)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueReplayError('Replay timing setting is outside its supported bounds')
        if self.execution_latency_seconds > self.max_execution_wait_seconds:
            raise ValueReplayError('Replay latency cannot exceed its maximum execution wait')
        if type(self.uniform_cadence_seconds) is not int or not 1 <= self.uniform_cadence_seconds <= 900:
            raise ValueReplayError('Uniform comparison cadence must be an integer from one to 900')
        if type(self.allow_uncalibrated) is not bool:
            raise ValueReplayError('Uncalibrated replay permission must be Boolean')
        if type(self.walk_forward_calibration) is not bool:
            raise ValueReplayError('Walk-forward calibration permission must be Boolean')
        for name in ('calibration_refit_contracts', 'max_calibration_refits'):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 1000:
                raise ValueReplayError('Walk-forward refit limits must be integers from one through 1000')
        if type(self.max_events) is not int or not 1 <= self.max_events <= MAX_EVENTS:
            raise ValueReplayError('Replay event limit must be between one and 200000')
        if type(self.max_actions) is not int or not 1 <= self.max_actions <= 100_000:
            raise ValueReplayError('Replay action limit must be between one and 100000')


def _levels(value):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueReplayError('Recorded order-book depth exceeds its supported bound')
    result, previous = [], ZERO
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or any(not isinstance(item, str) for item in row):
            raise ValueReplayError('Recorded bids require fixed-point price and quantity strings')
        price = _decimal(row[0], 'bid price', positive=True, maximum=ONE)
        quantity = _decimal(row[1], 'bid quantity', positive=True, maximum=Decimal('100000000'))
        if price >= ONE or price <= previous:
            raise ValueReplayError('Recorded bids must have unique ascending prices below one')
        result.append([_money(price), _money(quantity)])
        previous = price
    return result


_OBS_REQUIRED = {'kind', 'version', 'observed_at', 'model_time', 'quote_time', 'ticker',
    'contract_identity', 'probability_yes', 'yes_bids', 'no_bids', 'fee_type',
    'fee_multiplier', 'source', 'model_version', 'model_fingerprint'}
_OBS_OPTIONAL = {'fee_quantum', 'asset', 'volatility_regime', 'median_outcome',
    'native_snapshot', 'regime_proposal', 'market', 'volatility_regime_spec'}
_SET_REQUIRED = {'kind', 'version', 'available_at', 'settled_at', 'ticker', 'contract_identity', 'result'}
_SET_OPTIONAL = {'asset', 'source', 'model_version', 'model_fingerprint', 'market'}
_VOLATILITY_SPEC = 'projected_sigma_sqrt900_v1_0.0025_0.005'
_BINDING_FIELDS = ('source', 'model_version', 'model_fingerprint')


def _model_binding(raw, profile, *, required):
    present = [key in raw for key in _BINDING_FIELDS]
    if not any(present) and not required:
        return None
    expected_version = ('pyth-1ozgold-terminal-median-v1' if profile.symbol == 'GOLD' else
        'cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-median-v1')
    if (not all(present) or raw['source'] != profile.source or raw['model_version'] != expected_version
            or not isinstance(raw['model_fingerprint'], str)
            or _HASH.fullmatch(raw['model_fingerprint']) is None):
        raise ValueReplayError('Replay model provenance does not match its settlement asset')
    return tuple(raw[key] for key in _BINDING_FIELDS)


def _official_market(raw, identity, settled, result):
    if not isinstance(raw, dict):
        raise ValueReplayError('Official settlement market evidence must be a JSON object')
    try:
        candidate = {key: raw.get(key) for key in identity}
        for key in ('floor_strike', 'notional_value_dollars'):
            candidate[key] = str(candidate[key])
        checked, _, _ = _binary_identity(candidate)
        stamp = _timestamp(raw.get('settlement_ts'))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueReplayError('Official settlement market evidence is invalid') from None
    if (checked != identity or raw.get('status') != 'finalized'
            or raw.get('is_provisional', False) is not False
            or raw.get('result') != result or abs(stamp - settled) > .000001):
        raise ValueReplayError('Official settlement market evidence differs from the recorded final result')
    payout = raw.get('settlement_value_dollars')
    if payout is not None and _decimal(payout, 'settlement payout', maximum=ONE) != Decimal(result == 'yes'):
        raise ValueReplayError('The official settlement payout differs from the recorded final result')


def validate_replay_records(records, config=None):
    """Preserve receipt ordering; reject revised identities and contradictory outcomes."""
    config = ValueReplayConfig() if config is None else config
    if not isinstance(config, ValueReplayConfig) or not isinstance(records, (list, tuple)):
        raise ValueReplayError('Replay requires validated configuration and a record sequence')
    if not 1 <= len(records) <= config.max_events:
        raise ValueReplayError('Replay records are empty or exceed their configured limit')
    output, identities, bindings, outcomes, seen, previous = [], {}, {}, {}, {}, None
    for ordinal, original in enumerate(records):
        if not isinstance(original, dict):
            raise ValueReplayError('Replay records must be JSON objects')
        raw = {key: value for key, value in original.items() if not key.startswith('_')}
        kind = raw.get('kind')
        required, optional = ((_OBS_REQUIRED, _OBS_OPTIONAL) if kind == 'kalshi_value_observation' else
            (_SET_REQUIRED, _SET_OPTIONAL) if kind == 'kalshi_value_settlement' else (set(), set()))
        if (not required or not required <= set(raw) <= required | optional
                or type(raw.get('version')) is not int or raw['version'] != 1):
            raise ValueReplayError('The recorded value observation or settlement schema is invalid')
        try:
            identity, opened, closed = _binary_identity(raw['contract_identity'])
            profile = execution_asset_from_ticker(raw['ticker'])
        except (ValueError, TypeError, KeyError):
            raise ValueReplayError('Replay requires an exact supported fifteen-minute contract identity') from None
        if identity['ticker'] != raw['ticker'] or raw.get('asset', profile.symbol) != profile.symbol:
            raise ValueReplayError('The recorded contract and asset identity differ')
        prior = identities.setdefault(raw['ticker'], identity)
        if prior != identity:
            raise ValueReplayError('Recorded contract payout terms were revised')
        binding = _model_binding(raw, profile, required=kind == 'kalshi_value_observation')
        if binding is not None:
            prior_binding = bindings.setdefault(raw['ticker'], binding)
            if prior_binding != binding:
                raise ValueReplayError('Recorded model provenance changed within a contract')
        value = deepcopy(raw)
        value.update(_identity=identity, _opened=opened, _closed=closed, _ordinal=ordinal,
            asset=profile.symbol)
        if kind == 'kalshi_value_observation':
            stamp, model, quote = (_time(raw[key]) for key in ('observed_at', 'model_time', 'quote_time'))
            if not opened <= model <= stamp < closed or not opened <= quote <= stamp:
                raise ValueReplayError('Recorded model/book clocks are future dated or outside the contract')
            probability = _decimal(raw['probability_yes'], 'probability', maximum=ONE)
            if ('volatility_regime_spec' in raw
                    and raw['volatility_regime_spec'] != _VOLATILITY_SPEC):
                raise ValueReplayError('The recorded volatility feature specification is unsupported')
            if raw.get('median_outcome') not in (None, 'yes', 'no'):
                raise ValueReplayError('The recorded median selection is invalid')
            if raw.get('volatility_regime', 'all') not in ('all', 'low', 'normal', 'high'):
                raise ValueReplayError('The recorded volatility regime is invalid')
            if any(key in raw and not isinstance(raw[key], dict) for key in ('native_snapshot', 'regime_proposal', 'market')):
                raise ValueReplayError('Native replay evidence must be a JSON object')
            multiplier = _decimal(raw['fee_multiplier'], 'fee multiplier', maximum=Decimal('1000'))
            quantum = _decimal(raw.get('fee_quantum', '.0001'), 'fee quantum', positive=True, maximum=ONE)
            if raw['fee_type'] not in ('quadratic', 'quadratic_with_maker_fees'):
                raise ValueReplayError('Only explicitly recorded quadratic venue fees are supported')
            yes, no = _levels(raw['yes_bids']), _levels(raw['no_bids'])
            if yes and no and Decimal(yes[-1][0]) + Decimal(no[-1][0]) >= ONE:
                raise ValueReplayError('The recorded book is locked or crossed')
            value.update(probability_yes=float(probability), yes_bids=yes, no_bids=no,
                fee_multiplier=_money(multiplier), fee_quantum=_money(quantum),
                volatility_regime=raw.get('volatility_regime', 'all'))
            dedup = (raw['ticker'], stamp)
            signature = json.dumps(raw, sort_keys=True, allow_nan=False, separators=(',', ':'))
            if dedup in seen:
                if seen[dedup] != signature:
                    raise ValueReplayError('A same-time recorded observation was revised')
                continue
            seen[dedup] = signature
        else:
            stamp, settled = _time(raw['available_at']), _time(raw['settled_at'])
            if not closed <= settled <= stamp or raw['result'] not in ('yes', 'no'):
                raise ValueReplayError('An official final result must be available after settlement and close')
            if 'market' in raw:
                _official_market(raw['market'], identity, settled, raw['result'])
            outcome = (raw['result'], settled)
            if raw['ticker'] in outcomes and outcomes[raw['ticker']] != outcome:
                raise ValueReplayError('The recorded official settlement is contradictory')
            outcomes[raw['ticker']] = outcome
        if previous is not None and stamp < previous:
            raise ValueReplayError('Replay records must remain in chronological receipt order')
        previous = stamp
        value['_time'] = stamp
        output.append(value)
    return output


def load_replay_records(path, *, max_events=MAX_EVENTS):
    """Read bounded portable JSONL without consulting any account or network."""
    if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
        raise ValueReplayError('The replay file limit is invalid')
    source = Path(path).expanduser()
    if not source.is_file() or source.stat().st_size > 256 * 1024 * 1024:
        raise ValueReplayError('Replay JSONL must exist and be at most 256 MiB')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    result = []
    try:
        with source.open('r', encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                if len(line.encode('utf-8')) > 1_000_000 or len(result) >= max_events:
                    raise ValueReplayError('Replay JSONL exceeds its row or line-size limit')
                result.append(json.loads(line, object_pairs_hook=unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueReplayError('Replay JSONL contains invalid or duplicate-key JSON') from None
    return result


def _book(frame, fraction=ONE):
    result = []
    for key in ('yes_bids', 'no_bids'):
        rows = []
        for price, quantity in frame[key]:
            available = (Decimal(quantity) * fraction).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
            if available > ZERO:
                rows.append([price, _money(available)])
        result.append(rows)
    return result


def _fee_config(config, frame):
    return replace(config, fee_type=frame['fee_type'], fee_multiplier=frame['fee_multiplier'],
        fee_quantum=frame['fee_quantum'])


def _reservation(plan, frame):
    if plan['action'] != 'buy':
        return ZERO
    # The later book can fill every unit at the submitted worst limit. Reserve
    # that notional and the maximum quadratic taker fee, including rounding.
    return Decimal(plan['count']) * (Decimal(plan['limit_price'])
        + Decimal('.0175') * Decimal(frame['fee_multiplier']) + Decimal('.0001')
        + 2 * Decimal(frame['fee_quantum']))


class ReplayLedger:
    """Small net-inventory ledger; fills and official settlements are separate."""
    def __init__(self, cash, max_actions=20_000):
        self.initial_cash = self.cash = Decimal(cash)
        self.positions, self.basis = {}, {}
        self.realized = self.fees = self.turnover = ZERO
        self.counts = Counter()
        self.actions, self.equity_curve = [], []
        self.max_actions = max_actions
        self.peak, self.drawdown = self.cash, ZERO
        self.drawdown_fraction = 0.0

    def _action(self, row):
        if len(self.actions) < self.max_actions:
            self.actions.append(row)
        else:
            self.counts['omitted_action_rows'] += 1

    def fill(self, ticker, action, outcome, count, gross, fee, now, *, reason, decision_at,
             closing_gross=None, closing_fee=None):
        count = int(count)
        if count <= 0 or action not in ('buy', 'sell') or outcome not in ('yes', 'no'):
            raise ValueReplayError('A replay fill action is invalid')
        gross, fee = Decimal(gross), Decimal(fee)
        before, basis = self.positions.get(ticker, 0), self.basis.get(ticker, ZERO)
        sign = 1 if outcome == 'yes' else -1
        realized = ZERO
        if action == 'sell':
            if before * sign <= 0 or count > abs(before):
                raise ValueReplayError('A replay sale cannot exceed its owned side')
            removed = basis * Decimal(count) / Decimal(abs(before))
            cash_delta, after = gross - fee, before - sign * count
            realized, basis = cash_delta - removed, basis - removed
            self.counts['exit_fills'] += 1
        elif before == 0 or before * sign > 0:
            cash_delta, after = -gross - fee, before + sign * count
            basis += gross + fee
            self.counts['entry_fills'] += 1
        else:
            # Opposite purchases are a close followed by a possible new opening.
            closed = min(abs(before), count)
            close_cost = (gross * Decimal(closed) / Decimal(count)
                if closing_gross is None else Decimal(closing_gross))
            fee_close = fee * Decimal(closed) / Decimal(count) if closing_fee is None else Decimal(closing_fee)
            if not ZERO <= close_cost <= gross or not ZERO <= fee_close <= fee:
                raise ValueReplayError('Replay closing-fill cost allocation is invalid')
            proceeds = Decimal(closed) - close_cost - fee_close
            removed = basis * Decimal(closed) / Decimal(abs(before))
            realized, basis = proceeds - removed, basis - removed
            opened = count - closed
            opening_cost = gross - close_cost + fee - fee_close
            cash_delta, after = proceeds - opening_cost, before + sign * count
            basis += opening_cost
            self.counts['exit_fills'] += 1
            self.counts['entry_fills'] += int(opened > 0)
        if self.cash + cash_delta < ZERO:
            raise ValueReplayError('A simulated fill exceeds available cash')
        self.cash += cash_delta
        self.positions[ticker], self.basis[ticker] = after, ZERO if after == 0 else basis
        self.realized += realized
        self.fees += fee
        self.turnover += gross
        self.counts['filled_actions'] += 1
        self._action(dict(time=now, decision_at=decision_at, ticker=ticker, action=action,
            outcome=outcome, count=count, gross=_money(gross), fee=_money(fee),
            cash_delta=_money(cash_delta), realized_pnl=_money(realized),
            net_position_before=before, net_position_after=after, reason=reason))

    def settle(self, frame):
        ticker, position = frame['ticker'], self.positions.get(frame['ticker'], 0)
        if position == 0:
            return
        payout = Decimal(max(position, 0) if frame['result'] == 'yes' else max(-position, 0))
        profit = payout - self.basis.get(ticker, ZERO)
        self.cash += payout
        self.realized += profit
        self.positions[ticker], self.basis[ticker] = 0, ZERO
        self.counts['settlement_events'] += 1
        self._action(dict(time=frame['_time'], ticker=ticker, action='settle', result=frame['result'],
            count=abs(position), payout=_money(payout), realized_pnl=_money(profit),
            net_position_before=position, net_position_after=0))

    def mark(self, now, books, sizing_config, *, max_quote_age_seconds=5):
        from .value_sizing import evaluate_value_fill
        equity, unmarked = self.cash, 0
        for ticker, position in self.positions.items():
            if not position:
                continue
            frame = books.get(ticker)
            if (frame is None or now - frame['quote_time'] > max_quote_age_seconds
                    or not frame['_opened'] <= now < frame['_closed']):
                unmarked += abs(position)
                continue
            yes, no = _book(frame)
            mark = evaluate_value_fill(action='sell', outcome='yes' if position > 0 else 'no',
                count=abs(position), yes_bids=yes, no_bids=no, config=_fee_config(sizing_config, frame))
            equity += Decimal(mark['cash_delta'])
            unmarked += abs(position) - int(mark['count'])
        if not unmarked:
            self.peak = max(self.peak, equity)
            self.drawdown = max(self.drawdown, self.peak - equity)
            self.drawdown_fraction = max(self.drawdown_fraction,
                float((self.peak - equity) / self.peak) if self.peak > ZERO else 0.0)
        self.equity_curve.append(dict(time=now, cash=_money(self.cash),
            marked_equity=None if unmarked else _money(equity), conservative_equity=_money(equity),
            unmarked_contracts=unmarked))
        return equity

    def summary(self):
        open_positions = [dict(ticker=ticker, net_position=quantity,
            cost_basis=_money(self.basis.get(ticker, ZERO))) for ticker, quantity in sorted(self.positions.items()) if quantity]
        marked = self.equity_curve[-1]['marked_equity'] if self.equity_curve else _money(self.cash)
        return dict(initial_cash=_money(self.initial_cash), cash=_money(self.cash),
            realized_pnl=_money(self.realized), fees_paid=_money(self.fees), gross_turnover=_money(self.turnover),
            entry_fills=self.counts['entry_fills'], exit_fills=self.counts['exit_fills'],
            filled_actions=self.counts['filled_actions'], settlement_events=self.counts['settlement_events'],
            open_positions=open_positions, final_equity=_money(self.cash) if not open_positions else None,
            all_positions_settled=not open_positions, marked_equity=marked,
            max_drawdown=_money(self.drawdown), max_drawdown_fraction=self.drawdown_fraction,
            drawdown_scope='Fresh, fully depth-marked observations only; unknown marks do not establish a drawdown',
            actions=self.actions, omitted_action_rows=self.counts['omitted_action_rows'], equity_curve=self.equity_curve)


def _probabilities(frame, calibration, allow_raw, *, require_live_eligibility=False):
    if calibration is None:
        if not allow_raw:
            return None
        value = frame['probability_yes']
        return dict(probability_yes=value, probability_lower=value, probability_upper=value,
            calibrated=False, reason='Explicit uncalibrated research replay')
    from .value_calibration import calibrate_probability
    result = calibrate_probability(calibration, frame['probability_yes'], frame['_closed'] - frame['model_time'],
        frame.get('volatility_regime', 'all'), source=frame['source'], model_id=frame['model_version'],
        fit_sha256=frame['model_fingerprint'], as_of=frame['_time'], for_live=require_live_eligibility)
    if result.get('status') != 'ready':
        return None
    return dict(probability_yes=result['p'], probability_lower=result['p_lower'],
        probability_upper=result['p_upper'], calibrated=True, reason=result.get('reason', 'Calibrated probability'))


class _WalkForwardCalibration:
    """Refit from labels already received; retain separate model bindings."""
    def __init__(self, config, fit_options):
        from .value_calibration import ValueCalibrationError, _configuration
        self.config = config
        self.fit_options = dict(fit_options or {})
        allowed = {'train_fraction', 'min_train_contracts', 'min_validation_contracts',
            'min_bucket_contracts', 'min_validation_days', 'purge_seconds',
            'shrinkage_contracts', 'bootstrap_samples', 'max_live_age_seconds'}
        if set(self.fit_options) - allowed:
            raise ValueReplayError('Unsupported walk-forward calibration option')
        defaults = dict(train_fraction=.7, min_train_contracts=100, min_validation_contracts=50,
            min_bucket_contracts=20, min_validation_days=3, purge_seconds=900,
            shrinkage_contracts=20, bootstrap_samples=300, max_live_age_seconds=604800)
        defaults.update(self.fit_options)
        try:
            self.fit_options = _configuration(**defaults)
        except ValueCalibrationError as error:
            raise ValueReplayError(str(error)) from None
        self.observations = defaultdict(dict)
        self.rows = defaultdict(list)
        self.completed = defaultdict(set)
        self.artifacts, self.last_attempt = {}, {}
        self.audit = []
        self.counts = Counter()

    @staticmethod
    def binding(frame):
        return (frame['source'], frame['model_version'], frame['model_fingerprint'], frame['asset'])

    def observe(self, frame):
        key = frame['ticker'], frame['model_time'], self.binding(frame)
        # Select the first receipt of a model timestamp causally. Revisions do
        # not retrospectively choose a more attractive training observation.
        self.observations[frame['ticker']].setdefault(key, frame)

    def settle(self, frame):
        for observation in self.observations.pop(frame['ticker'], {}).values():
            binding = self.binding(observation)
            self.completed[binding].add(frame['ticker'])
            self.rows[binding].append(dict(ticker=frame['ticker'], as_of=observation['model_time'],
                close_time=frame['_closed'], settled_at=frame['available_at'],
                probability_yes=observation['probability_yes'], result=frame['result'],
                volatility_regime=observation.get('volatility_regime', 'all')))

    def at(self, frame):
        from .value_calibration import ValueCalibrationError, fit_value_calibration, validate_value_calibration
        binding, now = self.binding(frame), frame['_time']
        count = len(self.completed[binding])
        minimum = self.fit_options.get('min_train_contracts', 100) + self.fit_options.get('min_validation_contracts', 50)
        previous = self.last_attempt.get(binding)
        due = count >= minimum and (previous is None or count - previous >= self.config.calibration_refit_contracts)
        if due and len(self.audit) < self.config.max_calibration_refits:
            self.last_attempt[binding] = count
            rows = self.rows[binding]
            latest = max(row['settled_at'] for row in rows)
            if latest > now:
                raise ValueReplayError('Walk-forward attempted to use an unavailable future outcome')
            audit = dict(fit_at=now, completed_contracts=count, latest_label_available_at=latest,
                source=binding[0], model_version=binding[1], model_fingerprint=binding[2], asset=binding[3],
                purge_seconds=self.fit_options.get('purge_seconds', 900))
            try:
                artifact = fit_value_calibration(rows, source=binding[0], model_id=binding[1],
                    fit_sha256=binding[2], asset=binding[3], now=now, **self.fit_options)
                self.artifacts[binding] = validate_value_calibration(artifact)
                audit.update(status=artifact['status'], artifact_sha256=artifact['artifact_sha256'])
            except ValueCalibrationError as error:
                audit.update(status='unavailable', reason=str(error))
            self.audit.append(audit)
        artifact = self.artifacts.get(binding)
        result = None if artifact is None else _probabilities(frame, artifact, False, require_live_eligibility=True)
        self.counts['ready_decisions' if result is not None else 'withheld_decisions'] += 1
        return result

    def summary(self):
        return dict(enabled=True, refit_contracts=self.config.calibration_refit_contracts,
            fit_attempts=self.audit, max_refits=self.config.max_calibration_refits, **self.counts)


def _uniform_plan(frame, ledger, pending_reserve, consumed, spent, config, sizing_config):
    from .value_sizing import evaluate_value_fill
    ticker = frame['ticker']
    slot = int((frame['_time'] - frame['_opened']) // config.uniform_cadence_seconds)
    if (ticker, slot) in consumed:
        return None
    consumed.add((ticker, slot))
    outcome = frame.get('median_outcome') or ('yes' if frame['probability_yes'] > .5 else 'no')
    if frame['probability_yes'] == .5 and 'median_outcome' not in frame:
        return None
    opportunities = math.ceil(900 / config.uniform_cadence_seconds)
    allowance = (config.uniform_market_budget / opportunities).quantize(Decimal('.0001'), rounding=ROUND_FLOOR)
    budget = min(allowance, max(ZERO, config.uniform_market_budget - spent[ticker]),
        max(ZERO, ledger.cash - pending_reserve), sizing_config.max_order_cost)
    yes, no = _book(frame)
    opposite = no if outcome == 'yes' else yes
    if not opposite:
        return None
    price = ONE - Decimal(opposite[-1][0])
    # Bound the discrete search; fees and depth are evaluated on the whole order.
    maximum = min(sizing_config.max_contracts, int((budget / price).to_integral_value(rounding=ROUND_FLOOR)))
    before, sign = ledger.positions.get(ticker, 0), 1 if outcome == 'yes' else -1
    maximum = min(maximum, sizing_config.max_position_contracts - abs(before)
        if before * sign >= 0 else abs(before) + sizing_config.max_position_contracts)
    chosen = None
    for count in range(1, maximum + 1):
        fill = evaluate_value_fill(action='buy', outcome=outcome, count=count, yes_bids=yes,
            no_bids=no, config=_fee_config(sizing_config, frame), limit_price=price)
        if int(fill['count']) != count or -Decimal(fill['cash_delta']) > budget:
            break
        chosen = dict(action='buy', outcome=outcome, count=count, limit_price=_money(price),
            reduce_only=False, reason='Uniform directional ablation',
            reserved_cash=_money(-Decimal(fill['cash_delta'])))
    return chosen


def _forced_value_plan(frame, ledger, sizing, *, liquidate):
    """Mirror model-independent live reductions using only the current book."""
    from .value_sizing import evaluate_value_fill
    position = ledger.positions.get(frame['ticker'], 0)
    if position == 0:
        return None
    side = 'yes' if position > 0 else 'no'
    yes, no = _book(frame)
    selected = _fee_config(sizing, frame)
    def mark(quantity):
        if quantity == 0:
            return ZERO
        fill = evaluate_value_fill(action='sell', outcome=side, count=quantity,
            yes_bids=yes, no_bids=no, config=selected)
        if int(fill['count']) < quantity:
            own = yes if side == 'yes' else no
            return Decimal(quantity) * (Decimal(own[-1][0]) if own else ONE)
        return max(ZERO, Decimal(fill['cash_delta']))
    retained = 0
    if not liquidate:
        maximum = min(abs(position), selected.max_position_contracts)
        if mark(abs(position)) <= selected.max_market_risk and abs(position) <= selected.max_position_contracts:
            return None
        if selected.fee_multiplier * Decimal('.07') <= ONE:
            left, right = 0, maximum
            while left < right:
                middle = (left + right + 1) // 2
                if mark(middle) <= selected.max_market_risk:
                    left = middle
                else:
                    right = middle - 1
            retained = left
        else:
            own = yes if side == 'yes' else no
            bid = Decimal(own[-1][0]) if own else ONE
            retained = min(maximum, int((selected.max_market_risk / bid).to_integral_value(rounding=ROUND_FLOOR)))
    quantity = min(max(1, abs(position) - retained), selected.max_contracts)
    plan = evaluate_value_fill(action='sell', outcome=side, count=quantity,
        yes_bids=yes, no_bids=no, config=selected)
    plan.update(action='sell', outcome=side, reduce_only=True, forced_risk_reduction=True,
        reason=('Latched strategy drawdown liquidation' if liquidate else
            'Reduce confirmed inventory to the hard market or quantity cap'))
    return plan


def _calibration_metrics(samples, outcomes):
    by_horizon = defaultdict(lambda: defaultdict(list))
    for frame, prediction in samples:
        outcome = outcomes.get(frame['ticker'])
        if outcome is None:
            continue
        p = min(1 - 1e-12, max(1e-12, float(prediction)))
        y = float(outcome['result'] == 'yes')
        remaining = frame['_closed'] - frame['model_time']
        horizon = '0-60' if remaining <= 60 else '60-180' if remaining <= 180 else '180-450' if remaining <= 450 else '450-900'
        by_horizon[horizon][frame['ticker']].append(((p - y) ** 2, -math.log(p if y else 1 - p)))
    summary, all_contracts = {}, defaultdict(list)
    for horizon, contracts in by_horizon.items():
        scores = [(math.fsum(x[0] for x in rows) / len(rows), math.fsum(x[1] for x in rows) / len(rows))
            for rows in contracts.values()]
        summary[horizon] = dict(count_contracts=len(contracts), count_forecasts=sum(map(len, contracts.values())),
            brier_score=math.fsum(x[0] for x in scores) / len(scores), log_loss=math.fsum(x[1] for x in scores) / len(scores))
        for ticker, rows in contracts.items():
            all_contracts[ticker].extend(rows)
    scores = [(math.fsum(x[0] for x in rows) / len(rows), math.fsum(x[1] for x in rows) / len(rows))
        for rows in all_contracts.values()]
    return dict(weighting='Equal weight per completed contract; repeated forecasts are averaged within contract',
        count_contracts=len(scores), brier_score=math.fsum(x[0] for x in scores) / len(scores) if scores else None,
        log_loss=math.fsum(x[1] for x in scores) / len(scores) if scores else None, by_seconds_remaining=summary)


def compare_value_replay(records, *, sizing_config=None, calibration=None, config=None, include_legacy=True,
                         calibration_fit_options=None):
    """Compare one fixed policy on receipt-ordered evidence without fitting to it."""
    from .value_sizing import ValueSizingConfig, evaluate_value_fill, plan_value_trade
    config = ValueReplayConfig() if config is None else config
    sizing = ValueSizingConfig() if sizing_config is None else sizing_config
    if not isinstance(sizing, ValueSizingConfig) or type(include_legacy) is not bool:
        raise ValueReplayError('Replay requires a validated sizing policy and Boolean legacy selection')
    if config.walk_forward_calibration and calibration is not None:
        raise ValueReplayError('Choose either a fixed prior calibration artifact or causal walk-forward refitting')
    if config.walk_forward_calibration and config.allow_uncalibrated:
        raise ValueReplayError('Walk-forward trading cannot bypass calibration warmup with raw probabilities')
    if calibration_fit_options is not None and not config.walk_forward_calibration:
        raise ValueReplayError('Calibration fit options require walk-forward mode')
    frames = validate_replay_records(records, config)
    walk_forward = _WalkForwardCalibration(config, calibration_fit_options) if config.walk_forward_calibration else None
    first_calibrated_decision = None
    ledgers = {name: ReplayLedger(config.initial_cash, config.max_actions) for name in ('value', 'uniform_directional')}
    pending = {name: {} for name in ledgers}
    counts = {name: Counter() for name in ledgers}
    books, outcomes, latest_decision, uniform_consumed = {}, {}, {}, set()
    uniform_spent = defaultdict(lambda: ZERO)
    predictions = []
    value_drawdown_halted = False
    last_value_order = None

    for frame in frames:
        now, ticker = frame['_time'], frame['ticker']
        for name in ledgers:
            for held_ticker, order in list(pending[name].items()):
                if now >= order['closed'] or now - order['decision_at'] > config.max_execution_wait_seconds:
                    counts[name]['expired_ioc'] += 1
                    del pending[name][held_ticker]
        if frame['kind'] == 'kalshi_value_settlement':
            if ticker not in outcomes:
                outcomes[ticker] = frame
                if walk_forward is not None:
                    walk_forward.settle(frame)
                for ledger in ledgers.values():
                    ledger.settle(frame)
            for ledger in ledgers.values():
                    ledger.mark(now, books, sizing, max_quote_age_seconds=config.max_quote_age_seconds)
            continue
        books[ticker] = frame
        for name, ledger in ledgers.items():
            order = pending[name].get(ticker)
            if order is None or now < order['eligible_at']:
                continue
            if frame['quote_time'] < order['eligible_at']:
                # A newly received row can repeat an older quote. It cannot be
                # an execution observation merely because its receipt is later.
                continue
            del pending[name][ticker]
            if (now - frame['quote_time'] > config.max_quote_age_seconds
                    or (frame['fee_type'], frame['fee_multiplier'], frame['fee_quantum']) != order['fee_identity']):
                counts[name]['unavailable_execution_book'] += 1
                continue
            yes, no = _book(frame, config.depth_fraction)
            fill = evaluate_value_fill(action=order['action'], outcome=order['outcome'], count=order['count'],
                yes_bids=yes, no_bids=no, config=_fee_config(sizing, frame), limit_price=order['limit_price'])
            quantity = int(fill['count'])
            if quantity <= 0:
                counts[name]['unfilled_ioc'] += 1
                continue
            if order.get('reduce_only') and (ledger.positions.get(ticker, 0) == 0
                    or quantity > abs(ledger.positions[ticker])):
                raise ValueReplayError('A delayed reduce-only fill exceeds confirmed simulated holdings')
            fee = Decimal(fill.get('fee', fill.get('fees', '0')))
            # cash_delta is a standalone buy debit or sell credit from the fill helper.
            cash_delta = Decimal(fill['cash_delta'])
            gross = -cash_delta - fee if order['action'] == 'buy' else cash_delta + fee
            allocation = {}
            before = ledger.positions.get(ticker, 0)
            if order['action'] == 'buy' and before * (1 if order['outcome'] == 'yes' else -1) < 0:
                closing = evaluate_value_fill(action='buy', outcome=order['outcome'], count=min(abs(before), quantity),
                    yes_bids=yes, no_bids=no, config=_fee_config(sizing, frame), limit_price=order['limit_price'])
                allocation = dict(closing_gross=closing['gross_cost'], closing_fee=min(fee, Decimal(closing['fee'])))
            ledger.fill(ticker, order['action'], order['outcome'], quantity, gross, fee, now,
                reason=order['reason'], decision_at=order['decision_at'], **allocation)
            counts[name]['partial_ioc'] += int(quantity < order['count'])
            if name == 'uniform_directional':
                uniform_spent[ticker] += gross + fee
        for ledger in ledgers.values():
            ledger.mark(now, books, sizing, max_quote_age_seconds=config.max_quote_age_seconds)
        fresh_model = now - frame['model_time'] <= config.max_model_age_seconds
        fresh_quote = now - frame['quote_time'] <= config.max_quote_age_seconds
        fresh_decision = fresh_model and fresh_quote
        if not fresh_decision:
            for count in counts.values():
                count['stale_decision'] += 1
        if walk_forward is not None and fresh_decision:
            walk_forward.observe(frame)
        probability = ((walk_forward.at(frame) if walk_forward is not None else
            _probabilities(frame, calibration, config.allow_uncalibrated)) if fresh_decision else None)
        if probability is not None:
            predictions.append((frame, probability['probability_yes']))
        for name, ledger in ledgers.items():
            if ticker in pending[name]:
                counts[name]['pending_ioc'] += 1
                continue
            if name == 'value':
                mark = ledger.equity_curve[-1]
                if (not value_drawdown_halted and mark['marked_equity'] is not None
                        and Decimal(mark['marked_equity']) <= ledger.peak * (ONE - config.max_drawdown_fraction)):
                    value_drawdown_halted = True
                    counts[name]['drawdown_triggers'] += 1
                if any(other != ticker and quantity for other, quantity in ledger.positions.items()):
                    counts[name]['other_unsettled_market'] += 1
                    continue
                if any(other != ticker for other in pending[name]):
                    counts[name]['other_pending_market'] += 1
                    continue
                plan = (_forced_value_plan(frame, ledger, sizing, liquidate=value_drawdown_halted)
                    if fresh_quote else None)
                if value_drawdown_halted and plan is None:
                    counts[name]['drawdown_halted'] += 1
                    continue
                if plan is not None:
                    if last_value_order is not None and now - last_value_order < 1:
                        continue
                else:
                    if mark['marked_equity'] is None:
                        counts[name]['unmarked_inventory'] += 1
                        continue
                    if not fresh_decision:
                        continue
                    previous = latest_decision.get(ticker)
                    if previous is not None and now - previous < config.decision_interval_seconds:
                        continue
                    latest_decision[ticker] = now
                    if probability is None:
                        counts[name]['calibration_unavailable'] += 1
                        continue
                    if walk_forward is not None and first_calibrated_decision is None:
                        first_calibrated_decision = now
                    reserve = sum((Decimal(row['reserved_cash']) for row in pending[name].values()), ZERO)
                    yes, no = _book(frame)
                    plan = plan_value_trade(**{key: probability[key] for key in
                        ('probability_yes', 'probability_lower', 'probability_upper')},
                        cash=max(ZERO, ledger.cash - reserve), net_position=ledger.positions.get(ticker, 0),
                        yes_bids=yes, no_bids=no, bankroll=Decimal(ledger.equity_curve[-1]['conservative_equity']),
                        config=_fee_config(sizing, frame))
                if plan.get('action') == 'hold' or int(plan.get('count', 0)) <= 0:
                    counts[name]['hold'] += 1
                    continue
            else:
                if not fresh_decision:
                    continue
                reserve = sum((Decimal(row['reserved_cash']) for row in pending[name].values()), ZERO)
                plan = _uniform_plan(frame, ledger, reserve, uniform_consumed, uniform_spent, config, sizing)
                if plan is None:
                    counts[name]['hold'] += 1
                    continue
            reserve = _reservation(plan, frame)
            other_reserve = sum((Decimal(row['reserved_cash']) for row in pending[name].values()), ZERO)
            if reserve > max(ZERO, ledger.cash - other_reserve):
                counts[name]['cash_reservation_blocked'] += 1
                continue
            plan = dict(plan, reserved_cash=_money(reserve))
            counts[name]['order_attempts'] += 1
            if name == 'value':
                last_value_order = now
            pending[name][ticker] = dict(plan, decision_at=now,
                eligible_at=now + config.execution_latency_seconds, closed=frame['_closed'],
                reason=plan.get('reason', 'Cost-aware aggregate fractional Kelly rebalance'),
                fee_identity=(frame['fee_type'], frame['fee_multiplier'], frame['fee_quantum']))
    arms = {}
    for name, ledger in ledgers.items():
        report = ledger.summary()
        report.update(audit=dict(counts[name], pending_ioc=len(pending[name])),
            comparison_scope=('Cost-aware aggregate fractional Kelly' if name == 'value' else
                'Uniform directional ablation; excludes the legacy regime, reversal and exit heuristics'))
        arms[name] = report
        if name == 'value':
            report.update(drawdown_halted=value_drawdown_halted,
                max_drawdown_limit=_money(config.max_drawdown_fraction),
                position_scope='One unsettled market; model-independent drawdown and hard-cap reductions')
    native_count = sum(frame['kind'] == 'kalshi_value_observation' and 'native_snapshot' in frame for frame in frames)
    if walk_forward is not None and first_calibrated_decision is not None:
        # Align comparator bankrolls and trading windows with the first genuine
        # out-of-sample decision. Training-period baseline gains cannot finance
        # its held-out comparison against a freshly started value portfolio.
        matched = compare_value_replay([row for row in frames if row['_time'] >= first_calibrated_decision],
            sizing_config=sizing, config=replace(config, walk_forward_calibration=False, allow_uncalibrated=False),
            include_legacy=include_legacy)
        arms['uniform_directional'] = matched['uniform_directional']
        if include_legacy:
            arms['legacy'] = matched['legacy']
    elif include_legacy and native_count:
        from .value_legacy_replay import replay_legacy
        arms['legacy'] = replay_legacy(frames, config=config)
    elif include_legacy:
        arms['legacy'] = dict(status='unavailable', reason='Exact legacy comparison requires native_snapshot evidence; the uniform ablation is not the legacy strategy')
    return dict(kind='kalshi_value_replay', version=1, status='ready', read_only=True,
        execution_eligible=False, probability_mode='walk_forward_calibrated' if walk_forward is not None else
            'calibrated' if calibration is not None else
            'uncalibrated_research' if config.allow_uncalibrated else 'calibration_required',
        audit=dict(records=len(frames), observation_records=sum(frame['kind'] == 'kalshi_value_observation' for frame in frames),
            settlement_records=len(outcomes), native_observations=native_count),
        assumptions=['Only later same-contract books at or after the configured latency can simulate IOC fills.',
            'Visible depth is scaled by the explicit depth_fraction scenario; snapshots do not prove real fills.',
            'Fees are conservative estimates from the recorded venue schedule and rounded execution scenario.',
            'No passive queue fills are assumed. Orders expire if no eligible fresh execution book arrives in time.',
            'Official outcomes affect available cash only at their recorded available_at receipt.',
            'Marked equity is unavailable for stale, expired or incompletely depth-marked holdings; unknown marks block model trading and hard caps use gross bid upper bounds.',
            'Policy parameters are fixed during replay; this comparison is not independent proof of profitability.'],
        execution_scenario=dict(latency_seconds=config.execution_latency_seconds,
            max_execution_wait_seconds=config.max_execution_wait_seconds, depth_fraction=_money(config.depth_fraction)),
        strategy_config=sizing.to_dict(),
        walk_forward=(dict(walk_forward.summary(), first_calibrated_decision_at=first_calibrated_decision,
            matched_comparison_window=first_calibrated_decision is not None) if walk_forward is not None else dict(enabled=False)),
        fixed_calibration_scope=('Prior artifact, research replay; live eligibility is not granted by replay'
            if calibration is not None else None),
        calibration_metrics=_calibration_metrics(predictions, outcomes), **arms)
