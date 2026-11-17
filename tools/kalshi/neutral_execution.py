"""Explicit, bounded live execution for the isolated $100 neutral experiment.

Importing performs no I/O.  A caller must supply an armed client and a trusted
causal model validator.  This adapter never adopts existing/manual positions,
inherits directional calibration, resolves ambiguous orders, or fabricates
settlement proceeds.  Each IOC leg has a durable intent before submission.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .client import KalshiError, _known_error_code
from .engine import _valid_tick
from .neutral_model import normalize_neutral_snapshot, _contract
from .neutral_sizing import (NeutralSizingConfig, plan_neutral_spread, _worst_cash_delta,
                            _validated, _metrics, _wealth, _utility, _violations)
from .value_sizing import ValueSizingConfig, evaluate_value_fill, worst_value_buy_unit_cost


ZERO, ONE = Decimal(0), Decimal(1)


class NeutralExecutionError(ValueError):
    """Fixed, non-sensitive experiment policy or execution validation failure."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _number(value, name='amount', *, maximum=Decimal('1000000000'), signed=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NeutralExecutionError('Invalid neutral ' + name)
    raw = str(value)
    if len(raw) > 80:
        raise NeutralExecutionError('Invalid neutral ' + name)
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise NeutralExecutionError('Invalid neutral ' + name) from None
    if (not result.is_finite() or abs(result) > maximum or not signed and result < 0
            or len(result.as_tuple().digits) > 40 or not -18 <= result.as_tuple().exponent <= 18):
        raise NeutralExecutionError('Invalid neutral ' + name)
    return result


def _count(value):
    number = _number(value, 'whole contract quantity', signed=True)
    if number != number.to_integral_value():
        raise NeutralExecutionError('Neutral live quantities must be whole contracts')
    return int(number)


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise NeutralExecutionError('Invalid neutral execution clock')
    return float(value)


def _text(value):
    return format(value, 'f')


def _model_definition(raw):
    acquisition = raw.get('acquisition') or {}
    diagnostics = (raw.get('model') or {}).get('diagnostics') or {}
    values = [value for value in (acquisition.get('model_definition_fingerprint'),
        diagnostics.get('model_definition_fingerprint')) if value is not None]
    if (not values or len(set(values)) != 1 or not isinstance(values[0], str) or len(values[0]) != 64
            or any(c not in '0123456789abcdef' for c in values[0])):
        raise NeutralExecutionError('A consistent canonical neutral model-definition fingerprint is required')
    return values[0]


@dataclass(frozen=True)
class NeutralExecutionConfig:
    sizing: NeutralSizingConfig = field(default_factory=NeutralSizingConfig)
    initial_cash: Decimal = Decimal('100')
    min_interval_seconds: float = 1
    max_age_seconds: float = 5
    min_seconds_to_close: float = 2
    allow_uncalibrated: bool = False

    def __post_init__(self):
        if not isinstance(self.sizing, NeutralSizingConfig):
            raise NeutralExecutionError('A validated neutral sizing policy is required')
        cash = _number(self.initial_cash, 'initial allocation', maximum=Decimal(100))
        if cash <= ZERO:
            raise NeutralExecutionError('The initial neutral allocation must be positive and at most $100')
        for name, low, high in (('min_interval_seconds', 1, 60),
                               ('max_age_seconds', .01, 5), ('min_seconds_to_close', 1, 300)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise NeutralExecutionError('Invalid neutral execution timing policy')
        if type(self.allow_uncalibrated) is not bool:
            raise NeutralExecutionError('The experimental model override must be explicit')
        if self.sizing.max_pair_cost > cash or self.sizing.max_worst_case_loss > cash:
            raise NeutralExecutionError('Neutral basket and loss caps cannot exceed the trial allocation')
        object.__setattr__(self, 'initial_cash', cash)

    def to_dict(self):
        return dict(kind='kalshi_neutral_execution_config', version=1,
            sizing=self.sizing.to_dict(), initial_cash=_text(self.initial_cash),
            min_interval_seconds=self.min_interval_seconds, max_age_seconds=self.max_age_seconds,
            min_seconds_to_close=self.min_seconds_to_close, allow_uncalibrated=self.allow_uncalibrated)


class NeutralLiveJournal:
    """Standalone durable cash, inventory and independent-leg execution audit."""

    def __init__(self, path, config=None):
        self.config = NeutralExecutionConfig() if config is None else config
        if not isinstance(self.config, NeutralExecutionConfig):
            raise NeutralExecutionError('A validated neutral execution policy is required')
        original = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (original, *original.parents)):
            raise NeutralExecutionError('Neutral journal paths cannot contain symbolic links')
        self.path = original.resolve()
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise NeutralExecutionError('Neutral journals cannot use aliased or non-file paths')
        existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = Path(str(self.path) + '.lock')
        self._policy = _json(self.config.to_dict())
        self.connection = sqlite3.connect(str(self.path), timeout=0)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute('PRAGMA synchronous=FULL')
            if existed:
                if self._get('kind') != 'kalshi_neutral_live_journal_v1' or self._get('config') != self._policy:
                    raise NeutralExecutionError('Neutral journal kind or saved configuration differs')
            else:
                with self.connection:
                    self.connection.executescript('''
                        CREATE TABLE neutral_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE neutral_positions(ticker TEXT PRIMARY KEY,quantity INTEGER NOT NULL,
                            identity_json TEXT NOT NULL);
                        CREATE TABLE neutral_baskets(id TEXT PRIMARY KEY,created_at REAL NOT NULL,
                            status TEXT NOT NULL,plan_json TEXT NOT NULL,reservation TEXT NOT NULL);
                        CREATE TABLE neutral_orders(id TEXT PRIMARY KEY,basket_id TEXT NOT NULL,
                            ticker TEXT NOT NULL,status TEXT NOT NULL,leg_json TEXT NOT NULL,
                            wire_json TEXT NOT NULL,order_id TEXT,ack_json TEXT);
                    ''')
                    for key, value in dict(kind='kalshi_neutral_live_journal_v1', config=self._policy,
                            cash=_text(self.config.initial_cash), fees='0', repair='false', event='').items():
                        self._set(key, value)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        except Exception as error:
            self.connection.close()
            if isinstance(error, NeutralExecutionError):
                raise
            raise NeutralExecutionError('The isolated neutral journal is unavailable or invalid') from None

    def _get(self, key):
        row = self.connection.execute('SELECT value FROM neutral_meta WHERE key=?', (key,)).fetchone()
        return None if row is None else row[0]

    def _set(self, key, value):
        self.connection.execute('INSERT INTO neutral_meta VALUES (?,?) ON CONFLICT(key) '
            'DO UPDATE SET value=excluded.value', (key, str(value)))

    @contextmanager
    def exclusive(self):
        token = str(uuid.uuid4())
        try:
            descriptor = os.open(str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise NeutralExecutionError('Another runner or an abandoned lock owns this neutral journal') from None
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            if self._get('config') != self._policy:
                raise NeutralExecutionError('The saved neutral policy changed')
            yield
        finally:
            try:
                if self.lock_path.read_text(encoding='utf-8') == token:
                    self.lock_path.unlink()
            except OSError:
                pass

    def bind(self, account, event):
        if (not isinstance(account, str) or len(account) != 64
                or any(c not in '0123456789abcdef' for c in account)):
            raise NeutralExecutionError('A canonical account fingerprint is required')
        if not isinstance(event, str) or not event.startswith('KXBTCD-'):
            raise NeutralExecutionError('An exact hourly BTC event identity is required')
        with self.connection:
            for key, value in (('account', account), ('event', event)):
                old = self._get(key)
                if old and old != value:
                    raise NeutralExecutionError('The neutral trial is bound to another account or event')
                self._set(key, value)

    def cash(self):
        return _number(self._get('cash'), 'saved strategy cash')

    def bind_model_definition(self, fingerprint):
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
            raise NeutralExecutionError('A canonical neutral model-definition fingerprint is required')
        with self.connection:
            old = self._get('model_definition')
            if old and old != fingerprint:
                raise NeutralExecutionError('This neutral journal is bound to a different source/model definition')
            self._set('model_definition', fingerprint)

    def positions(self):
        return {row['ticker']: row['quantity'] for row in self.connection.execute(
            'SELECT ticker,quantity FROM neutral_positions WHERE quantity<>0')}

    def identities(self):
        return {row['ticker']: json.loads(row['identity_json']) for row in self.connection.execute(
            'SELECT ticker,identity_json FROM neutral_positions')}

    def halted(self):
        return bool(self._get('halt_reason')) or self.connection.execute("SELECT 1 FROM neutral_orders WHERE status IN ('prepared','sending','unknown') LIMIT 1").fetchone() is not None

    def halt(self, reason):
        with self.connection:
            self._set('halt_reason', reason)
            self._set('repair', 'true')

    def repair(self):
        return self._get('repair') == 'true'

    def last_intent(self):
        row = self.connection.execute('SELECT MAX(created_at) FROM neutral_baskets').fetchone()
        return row[0]

    def begin(self, plan, frame, wires, now, *, audit=None):
        basket = str(uuid.uuid4())
        saved_plan = dict(plan, research_evidence=audit or {})
        encoded_plan = _json(saved_plan)
        if len(encoded_plan.encode('utf-8')) > 2_000_000:
            raise NeutralExecutionError('Neutral intent evidence exceeds the two-megabyte audit bound')
        identities = {c['ticker']: c['contract_identity'] for c in frame['contracts']}
        with self.connection:
            if self.halted():
                raise NeutralExecutionError('An unresolved neutral order prevents a new basket')
            self.connection.execute('INSERT INTO neutral_baskets VALUES (?,?,?,?,?)',
                (basket, now, 'prepared', encoded_plan, plan.get('worst_cash_reservation', '0')))
            for leg, wire in zip(plan['legs'], wires):
                ticker = leg['ticker']
                old = self.identities().get(ticker)
                if old is not None and old != identities[ticker]:
                    raise NeutralExecutionError('The saved neutral contract identity changed')
                self.connection.execute('INSERT OR IGNORE INTO neutral_positions VALUES (?,?,?)',
                    (ticker, 0, _json(identities[ticker])))
                self.connection.execute('INSERT INTO neutral_orders VALUES (?,?,?,?,?,?,?,?)',
                    (wire['client_order_id'], basket, ticker, 'prepared', _json(leg), _json(wire), None, None))
        return basket

    def sending(self, identifier):
        with self.connection:
            cursor = self.connection.execute("UPDATE neutral_orders SET status='sending' WHERE id=? AND status='prepared'", (identifier,))
            if cursor.rowcount != 1:
                raise NeutralExecutionError('A neutral intent cannot be submitted twice')

    def unknown(self, identifier, order_id=None):
        with self.connection:
            self.connection.execute("UPDATE neutral_orders SET status='unknown',order_id=? WHERE id=?", (order_id, identifier))
            self._set('repair', 'true')

    def finish_order(self, identifier, *, status, ack=None, count=0, cash_delta=ZERO, fees=ZERO, order_id=None):
        with self.connection:
            row = self.connection.execute('SELECT * FROM neutral_orders WHERE id=?', (identifier,)).fetchone()
            if row is None or row['status'] not in ('prepared', 'sending'):
                raise NeutralExecutionError('The neutral acknowledgement has no matching pending intent')
            leg = json.loads(row['leg_json'])
            count = _count(count)
            if not 0 <= count <= leg['count']:
                raise NeutralExecutionError('The acknowledged neutral count exceeds its intent')
            if count:
                old = self.positions().get(leg['ticker'], 0)
                sign = 1 if leg['outcome'] == 'yes' else -1
                if leg['action'] == 'sell':
                    if not old or (old > 0) != (sign > 0) or count > abs(old):
                        raise NeutralExecutionError('A neutral sale exceeds confirmed owned inventory')
                    change = -sign * count
                else:
                    if old and (old > 0) != (sign > 0):
                        raise NeutralExecutionError('Neutral reversals must close existing inventory first')
                    change = sign * count
                cash_delta, fees = _number(cash_delta, 'actual cash flow', signed=True), _number(fees, 'actual fees')
                if self.cash() + cash_delta < ZERO:
                    raise NeutralExecutionError('Actual neutral cash flow exceeds the allocated strategy cash')
                self.connection.execute('UPDATE neutral_positions SET quantity=quantity+? WHERE ticker=?', (change, leg['ticker']))
                self._set('cash', _text(self.cash() + cash_delta))
                self._set('fees', _text(_number(self._get('fees')) + fees))
            self.connection.execute('UPDATE neutral_orders SET status=?,order_id=?,ack_json=? WHERE id=?',
                (status, order_id, _json(ack or {}), identifier))

    def finish_basket(self, identifier, status, *, incomplete=False):
        with self.connection:
            self.connection.execute("UPDATE neutral_orders SET status='skipped' WHERE basket_id=? AND status='prepared'", (identifier,))
            self.connection.execute('UPDATE neutral_baskets SET status=? WHERE id=?', (status, identifier))
            if incomplete and self.positions():
                self._set('repair', 'true')
            if not self.positions() and not self.halted():
                self._set('repair', 'false')

    def snapshot(self):
        orders = [dict(row) for row in self.connection.execute(
            'SELECT id,basket_id,ticker,status,order_id,ack_json FROM neutral_orders ORDER BY rowid DESC LIMIT 1000')]
        for row in orders:
            row['ack'] = json.loads(row.pop('ack_json')) if row['ack_json'] else None
        return dict(kind='kalshi_neutral_live_state', version=1, mode='live',
            config=self.config.to_dict(), initial_allocation=_text(self.config.initial_cash),
            cash=_text(self.cash()), positions=self.positions(), fees=self._get('fees'),
            halted=self.halted(), repair=self.repair(), pinned_event=self._get('event') or None,
            halt_reason=self._get('halt_reason'),
            model_definition_fingerprint=self._get('model_definition'),
            orders=orders, calibrated=False, experimental_uncalibrated=self.config.allow_uncalibrated,
            limitations=['No neutral calibration qualification is inferred from the directional model.',
                'IOC legs are independent; liquidity and an immediate hedge are not guaranteed.',
                'Official settlement accounting is not implemented; this trial remains pinned to one event.'])

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class NeutralLiveExecutor:
    """User-armed, independently acknowledged IOC legs with fail-closed repair."""

    def __init__(self, client, journal, config=None, *, clock=time.time,
                 market_validator=None, model_validator=None, refresh_snapshot=None, refresh_quotes=None):
        self.client, self.journal, self.clock = client, journal, clock
        self.config = journal.config if config is None else config
        if not isinstance(journal, NeutralLiveJournal) or self.config.to_dict() != journal.config.to_dict():
            raise NeutralExecutionError('The executor and durable neutral policy differ')
        if getattr(client, 'environment', None) != 'production' or getattr(client, '_allow_writes', False) is not True:
            raise NeutralExecutionError('Neutral execution requires an explicitly armed production client')
        if model_validator is None or not callable(model_validator):
            raise NeutralExecutionError('A trusted causal neutral model validator is required')
        if market_validator is None:
            from .neutral_market import parse_threshold_contract
            market_validator = parse_threshold_contract
        self.market_validator, self.model_validator = market_validator, model_validator
        if any(callback is not None and not callable(callback) for callback in (refresh_snapshot, refresh_quotes)):
            raise NeutralExecutionError('The neutral snapshot refresh must be callable')
        self.refresh_snapshot = refresh_snapshot
        self.refresh_quotes = refresh_quotes
        self._venue_checks = {}
        self._venue_evidence = {}

    def _result(self, status, reason, **details):
        return dict(status=status, reason=reason, execution_eligible=status in ('filled', 'partial'),
                    experimental_uncalibrated=self.config.allow_uncalibrated,
                    portfolio=self.journal.snapshot(), **details)

    def snapshot(self):
        return self.journal.snapshot()

    def _quote_frame(self, raw, now):
        if not isinstance(raw, dict) or raw.get('synthetic') is True:
            raise NeutralExecutionError('Synthetic or missing quotes cannot liquidate exchange positions')
        receipt = _clock(raw.get('observed_at'))
        if receipt > now or now - receipt > self.config.max_age_seconds:
            raise NeutralExecutionError('Fresh neutral quotes are required for owned-risk liquidation')
        contracts = raw.get('contracts')
        if not isinstance(contracts, list) or not 1 <= len(contracts) <= 6:
            raise NeutralExecutionError('Owned-risk liquidation requires current contract books')
        contracts = [_contract(row, receipt, now) for row in contracts]
        if (len({row['ticker'] for row in contracts}) != len(contracts)
                or max(row['quote_time'] for row in contracts) - min(row['quote_time'] for row in contracts) > 1):
            raise NeutralExecutionError('Neutral risk books are repeated or unsynchronized')
        return dict(raw, contracts=contracts, observed_at=receipt)

    def _fresh(self, raw, now):
        if raw.get('synthetic') is True:
            raise NeutralExecutionError('Synthetic snapshots cannot submit neutral exchange orders')
        if self.model_validator(raw, now) is not True:
            raise NeutralExecutionError('The neutral model is not bound to current causal source evidence')
        frame = normalize_neutral_snapshot(raw, now=now)
        if now - frame['observed_at'] > self.config.max_age_seconds:
            raise NeutralExecutionError('The neutral observation became stale')
        return frame

    def _venue(self, frame):
        self._venue_checks = {}
        series = self.client.get_series('KXBTCD')
        markets = {row['ticker']: self.client.get_market(row['ticker']) for row in frame['contracts']}
        events = {m.get('event_ticker') for m in markets.values()}
        if len(events) != 1 or not all(isinstance(x, str) and x.startswith('KXBTCD-') for x in events):
            raise NeutralExecutionError('The neutral legs do not share one hourly event')
        event_name = next(iter(events))
        event = self.client.get_event(event_name)
        self._venue_evidence = dict(series=series, event=event, markets=markets)
        shards = set()
        keys = ('ticker', 'underlying', 'source', 'settlement_statistic', 'window_start',
                'window_end', 'payout', 'strike', 'comparator', 'fee_type', 'fee_multiplier')
        numeric = {'window_start', 'window_end', 'payout', 'strike', 'fee_multiplier'}
        for contract in frame['contracts']:
            market = markets[contract['ticker']]
            checked = self.market_validator(series, market, event, _clock(self.clock()))
            if any((_number(checked.get(k), maximum=Decimal('1000000000000')) !=
                    _number(contract.get(k), maximum=Decimal('1000000000000'))) if k in numeric
                   else str(checked.get(k)) != str(contract.get(k)) for k in keys):
                raise NeutralExecutionError('Current venue terms or fees differ from the neutral source snapshot')
            self._venue_checks[contract['ticker']] = checked
            shard = market.get('exchange_index')
            if type(shard) is not int or not 0 <= shard <= 1000:
                raise NeutralExecutionError('An exact neutral market exchange shard is required')
            shards.add(shard)
        if len(shards) != 1:
            raise NeutralExecutionError('This neutral trial requires a common exchange shard')
        self.journal.bind(getattr(self.client, 'account_fingerprint', None), event_name)
        return markets, next(iter(shards))

    def _account(self, shard):
        exchange = self.client.get_exchange_status()
        if not isinstance(exchange, dict) or exchange.get('exchange_active') is not True or exchange.get('trading_active') is not True:
            raise NeutralExecutionError('Exchange trading is unavailable')
        balance = self.client.get_balance()
        cash = _number(balance['balance_dollars']) if 'balance_dollars' in balance else _number(balance.get('balance')) / 100
        scoped = self.client.get_exchange_balance(shard)
        if not isinstance(scoped, dict) or scoped.get('exchange_index', shard) != shard:
            raise NeutralExecutionError('The neutral exchange-shard balance identity differs')
        shard_cash = (_number(scoped['balance_dollars']) if 'balance_dollars' in scoped
                      else _number(scoped.get('balance')) / 100)
        positions, orders = self.client.get_positions(), self.client.get_orders(status='resting')
        if not isinstance(positions, list) or not isinstance(orders, list) or orders:
            raise NeutralExecutionError('Neutral execution requires complete positions and no resting orders')
        actual, seen = {}, set()
        for row in positions:
            if not isinstance(row, dict) or not isinstance(row.get('ticker'), str):
                raise NeutralExecutionError('The neutral account holdings response is incomplete')
            if not row['ticker'].startswith('KXBTCD-'):
                continue
            if (any(type(row[key]) is not int or row[key] != 0 for key in ('subaccount_number', 'subaccount') if key in row)
                    or row['ticker'] in seen):
                raise NeutralExecutionError('Duplicate or unsupported neutral account holdings exist')
            seen.add(row['ticker'])
            count = _count(row.get('position_fp'))
            if count:
                actual[row['ticker']] = count
        if actual != self.journal.positions():
            raise NeutralExecutionError('Hourly BTC account positions differ from confirmed neutral journal fills')
        return min(cash, self.journal.cash()), min(cash, shard_cash, self.journal.cash())

    def _refresh(self, raw, frame, *, risk_only=False, required=()):
        callback = self.refresh_quotes if risk_only else self.refresh_snapshot
        candidate = raw if callback is None else callback()
        if not isinstance(candidate, dict):
            raise NeutralExecutionError('The refreshed neutral snapshot is unavailable')
        old = {row['ticker']: row for row in frame['contracts']}
        # Discovery may add new nearby strikes, but this durable basket uses
        # only contracts already checked against current venue terms.
        candidate = dict(candidate, contracts=[row for row in candidate.get('contracts', [])
            if isinstance(row, dict) and row.get('ticker') in old])
        validator = self._quote_frame if risk_only else self._fresh
        fresh = validator(candidate, _clock(self.clock()))
        new = {row['ticker']: row for row in fresh['contracts']}
        if not set(required) <= new.keys():
            raise NeutralExecutionError('A durable neutral leg is absent from refreshed evidence')
        for ticker, row in new.items():
            previous = old[ticker]
            if (row['contract_identity'] != previous['contract_identity']
                    or row['fee_type'] != previous['fee_type']
                    or Decimal(row['fee_multiplier']) != Decimal(previous['fee_multiplier'])):
                raise NeutralExecutionError('Refreshed neutral settlement terms or fees differ')
        return candidate, fresh

    def _project_remaining(self, frame, plan, remaining, starting_cash, starting_positions, *, risk_only):
        contracts = {row['ticker']: row for row in frame['contracts']}
        projected = self.journal.positions()
        cash = self.journal.cash()
        current_positions, current_cash, corner_legs = dict(projected), cash, []
        for leg in remaining:
            row = contracts[leg['ticker']]
            fee_config = ValueSizingConfig(fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'])
            fill = evaluate_value_fill(action=leg['action'], outcome=leg['outcome'], count=leg['count'],
                yes_bids=row['yes_bids'], no_bids=row['no_bids'], config=fee_config, limit_price=leg['limit_price'])
            if fill['count'] != leg['count']:
                raise NeutralExecutionError('The original neutral limit no longer has complete visible liquidity')
            bound = _worst_cash_delta(leg, fee_config)
            corner_legs.append((leg, bound))
            cash += bound
            projected[leg['ticker']] = projected.get(leg['ticker'], 0) + leg['signed_position_delta']
        if cash < ZERO:
            raise NeutralExecutionError('The remaining neutral basket exceeds allocated strategy cash')
        if risk_only:
            return
        inputs = dict(frame, cash=_text(starting_cash), bankroll=_text(self.config.initial_cash),
            positions=starting_positions, force_reduce=False)
        _, bankroll, _, parsed, positions, scenarios = _validated(inputs)
        projected = {ticker: projected.get(ticker, 0) for ticker in parsed}
        current_positions = {ticker: current_positions.get(ticker, 0) for ticker in parsed}
        current = _metrics(current_cash, current_positions, parsed, scenarios, bankroll, self.config.sizing)
        delta_limit = max(self.config.sizing.max_unhedged_delta, abs(Decimal(current['residual_delta'])))
        loss_limit = max(self.config.sizing.max_worst_case_loss, Decimal(current['worst_case_loss']))
        # A refresh can change Greeks after the original pair was admitted.
        # Recheck every independent leg-only corner using the CURRENT owned
        # portfolio, not merely the completed basket's near-zero delta.
        for leg, bound in corner_legs:
            corner_positions = dict(current_positions)
            corner_positions[leg['ticker']] += leg['signed_position_delta']
            allowance = self.config.sizing.slippage_per_contract * leg['count']
            if current_cash + bound - allowance < ZERO:
                raise NeutralExecutionError('A refreshed independent leg exceeds allocated strategy cash')
            corner = _metrics(current_cash + bound, corner_positions, parsed, scenarios, bankroll,
                self.config.sizing, allowance=allowance)
            if (abs(Decimal(corner['residual_delta'])) > delta_limit
                    or Decimal(corner['worst_case_loss']) > loss_limit
                    or Decimal(corner['minimum_terminal_wealth']) < self.config.sizing.wealth_floor):
                raise NeutralExecutionError('A refreshed independent neutral leg exceeds interim risk limits')
        if plan['action'] == 'reduce':
            return
        allowance = self.config.sizing.slippage_per_contract * sum(leg['count'] for leg in plan['legs'])
        if starting_cash - cash + allowance > self.config.sizing.max_pair_cost:
            raise NeutralExecutionError('The completed neutral basket no longer fits its cost cap')
        before = _metrics(starting_cash, positions, parsed, scenarios, bankroll, self.config.sizing)
        after = _metrics(cash, projected, parsed, scenarios, bankroll, self.config.sizing, allowance=allowance)
        if any(_violations(after, self.config.sizing)):
            raise NeutralExecutionError('The completed neutral portfolio no longer satisfies its risk limits')
        probabilities = [s['probability'] for s in scenarios]
        old_utility = _utility(_wealth(starting_cash, positions, scenarios), probabilities,
            bankroll, self.config.sizing, fractional=True)
        new_utility = _utility(_wealth(cash - allowance, projected, scenarios), probabilities,
            bankroll, self.config.sizing, fractional=True)
        gain = None if old_utility is None or new_utility is None else new_utility - old_utility
        expected = Decimal(after['expected_terminal_wealth']) - Decimal(before['expected_terminal_wealth'])
        equivalent = ZERO if gain is None else bankroll * Decimal(str(math.expm1(gain)))
        if (gain is None or gain <= 1e-12 or gain < float(self.config.sizing.min_log_growth)
                or equivalent < self.config.sizing.min_expected_improvement
                or expected < self.config.sizing.min_expected_improvement):
            raise NeutralExecutionError('The completed neutral basket no longer preserves its net modeled value')

    def _local_check(self, raw, leg, should_stop, *, risk_only=False):
        now = _clock(self.clock())
        validator = self._quote_frame if risk_only else self._fresh
        fresh = validator(raw, now)
        if should_stop is not None and should_stop():
            raise NeutralExecutionError('The neutral runner was stopped before submission')
        closed = next(c['window_end'] for c in fresh['contracts'] if c['ticker'] == leg['ticker'])
        cutoff = self.config.min_seconds_to_close if leg['action'] == 'buy' else 1
        if closed - now < cutoff:
            raise NeutralExecutionError('The neutral submission cutoff has been reached')
        if leg['action'] == 'buy' and _model_definition(raw) != self.journal._get('model_definition'):
            raise NeutralExecutionError('The neutral source/model definition changed before submission')

    def _pre_submit(self, raw, frame, market, leg, should_stop, *, risk_only=False):
        self._local_check(raw, leg, should_stop, risk_only=risk_only)
        current = self.client.get_market(leg['ticker'])
        immutable = ('ticker', 'event_ticker', 'floor_strike', 'cap_strike', 'strike_type',
            'open_time', 'close_time', 'rules_primary', 'rules_secondary', 'market_type',
            'settlement_bounds_type', 'notional_value_dollars', 'exchange_index',
            'price_level_structure', 'price_ranges', 'is_provisional', 'custom_strike',
            'early_close_condition', 'can_close_early', 'functional_strike',
            'mve_collection_ticker', 'mve_selected_legs', 'result')
        if current.get('status') != 'active' or any(current.get(k) != market.get(k) for k in immutable):
            raise NeutralExecutionError('The neutral contract changed before submission')
        self._local_check(raw, leg, should_stop, risk_only=risk_only)

    def _acknowledgement(self, ack, leg, wire, sent_at):
        received = _clock(self.clock())
        if not isinstance(ack, dict) or not isinstance(ack.get('order_id'), str) or not ack['order_id']:
            raise NeutralExecutionError('The neutral IOC acknowledgement is incomplete')
        if self.journal.connection.execute('SELECT 1 FROM neutral_orders WHERE order_id=? LIMIT 1', (ack['order_id'],)).fetchone():
            raise NeutralExecutionError('A venue order ID was already assigned to another neutral intent')
        if ack.get('client_order_id', wire['client_order_id']) != wire['client_order_id'] or ack.get('ticker', leg['ticker']) != leg['ticker']:
            raise NeutralExecutionError('The neutral acknowledgement identity differs')
        count = _count(ack.get('fill_count'))
        if not 0 <= count <= leg['count'] or _number(ack.get('remaining_count')) != ZERO:
            raise NeutralExecutionError('The neutral IOC count or unclosed remainder is ambiguous')
        for alias, expected in (('fill_count_fp', count), ('remaining_count_fp', 0)):
            if alias in ack and _count(ack[alias]) != expected:
                raise NeutralExecutionError('The neutral acknowledgement quantity aliases disagree')
        if 'status' in ack and ack['status'] not in ('executed', 'canceled', 'cancelled'):
            raise NeutralExecutionError('The neutral IOC has no final acknowledgement status')
        stamp = ack.get('ts_ms')
        if type(stamp) is not int or not sent_at - 5 <= stamp / 1000 <= received + 5:
            raise NeutralExecutionError('The neutral matching timestamp is invalid')
        average = _number(ack.get('average_fill_price'), maximum=ONE) if count else ZERO
        fee = _number(ack.get('average_fee_paid'), maximum=Decimal(1000)) if count else ZERO
        price = _number(wire['price'])
        if count and (not ZERO < average < ONE or wire['side'] == 'bid' and average > price
                      or wire['side'] == 'ask' and average < price):
            raise NeutralExecutionError('The neutral actual fill violates its durable limit')
        outcome_price = average if leg['outcome'] == 'yes' else ONE - average
        cash_delta = (outcome_price * count if leg['action'] == 'sell' else -outcome_price * count) - fee * count
        return dict(order_id=ack['order_id'], client_order_id=wire['client_order_id'],
            fill_count=count, average_fill_price=_text(average), average_fee_paid=_text(fee),
            fees=_text(fee * count), cash_delta=_text(cash_delta), ts_ms=stamp)

    def execute(self, raw_snapshot, *, should_stop=None):
        """Attempt a durable, independently acknowledged basket; never retry it."""
        return self._execute(raw_snapshot, should_stop=should_stop)

    def repair(self, quote_frame, reason='Incomplete basket', *, should_stop=None):
        """Reduce confirmed owned risk using quotes, independently of CF forecasts."""
        return self._execute(quote_frame, should_stop=should_stop, risk_only=True, repair_reason=reason)

    def _execute(self, raw_snapshot, *, should_stop=None, risk_only=False, repair_reason=None):
        basket, fills, plan = None, [], None
        try:
            with self.journal.exclusive():
                if self.journal.halted():
                    return self._result('halted', 'An unresolved neutral order must be reconciled before any submission')
                validator = self._quote_frame if risk_only else self._fresh
                frame = validator(raw_snapshot, _clock(self.clock()))
                markets, shard = self._venue(frame)
                eligible_legs = sum(self._venue_checks[row['ticker']].get('settlement_rounding_verified') is True
                    and not self._venue_checks[row['ticker']].get('execution_blockers') for row in frame['contracts'])
                if not risk_only and not self.journal.positions() and eligible_legs < 2:
                    return self._result('blocked',
                        'Current venue settlement rounding or payout semantics require independent verification')
                economic_cash, funded_cash = self._account(shard)
                own = self.journal.positions()
                raw_snapshot, frame = self._refresh(raw_snapshot, frame, risk_only=risk_only, required=own)
                known = {c['ticker']: c for c in frame['contracts']}
                if not own.keys() <= known.keys():
                    raise NeutralExecutionError('A held neutral contract is missing; its exposure cannot be assumed away')
                for ticker, identity in self.journal.identities().items():
                    if ticker in known and identity != known[ticker]['contract_identity']:
                        raise NeutralExecutionError('A saved neutral contract identity differs from the current snapshot')
                if risk_only:
                    legs = []
                    for ticker, count in own.items():
                        row = known[ticker]
                        fee_config = ValueSizingConfig(fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'])
                        leg = evaluate_value_fill(action='sell', outcome='yes' if count > 0 else 'no',
                            count=min(abs(count), self.config.sizing.max_leg_contracts),
                            yes_bids=row['yes_bids'], no_bids=row['no_bids'], config=fee_config)
                        if leg['count']:
                            legs.append(dict(leg, ticker=ticker))
                        if len(legs) == 2:
                            break
                    plan = dict(status='proposed' if legs else 'hold', action='reduce', legs=legs,
                        worst_cash_reservation='0', reason=repair_reason or 'Reduce confirmed owned risk using fresh books')
                else:
                    inputs = dict(frame, cash=_text(economic_cash), bankroll=_text(self.config.initial_cash),
                        positions=own, force_reduce=self.journal.repair())
                    plan = plan_neutral_spread(inputs, self.config.sizing)
                if plan['status'] != 'proposed':
                    return self._result('hold', plan['reason'], plan=plan)
                if plan['action'] == 'open' and any(
                        self._venue_checks[leg['ticker']].get('settlement_rounding_verified') is not True
                        or self._venue_checks[leg['ticker']].get('execution_blockers') for leg in plan['legs']):
                    return self._result('blocked', 'Current venue settlement rounding or payout semantics require independent verification', plan=plan)
                if plan['action'] == 'open' and not self.config.allow_uncalibrated:
                    return self._result('blocked', 'No qualified neutral calibration is available; the explicit experimental override is disabled', plan=plan)
                if plan['action'] == 'open':
                    self.journal.bind_model_definition(_model_definition(raw_snapshot))
                reservation = sum((max(ZERO, -_worst_cash_delta(leg, ValueSizingConfig(
                    fee_type=known[leg['ticker']]['fee_type'], fee_multiplier=known[leg['ticker']]['fee_multiplier'])))
                    for leg in plan['legs']), ZERO)
                if reservation > funded_cash:
                    return self._result('blocked', 'The complete neutral basket cannot fit strategy and exchange-shard cash', plan=plan)
                last = self.journal.last_intent()
                if last is not None and self.clock() - last < self.config.min_interval_seconds:
                    return self._result('hold', 'Waiting for the neutral interval after its previous durable intent', plan=plan)
                wires = []
                for leg in plan['legs']:
                    ticker, action, outcome = leg['ticker'], leg['action'], leg['outcome']
                    if action == 'sell' and (not own.get(ticker) or (own[ticker] > 0) != (outcome == 'yes') or leg['count'] > abs(own[ticker])):
                        raise NeutralExecutionError('A neutral reduction exceeds confirmed inventory')
                    limit = _number(leg['limit_price'], maximum=ONE)
                    price = limit if outcome == 'yes' else ONE - limit
                    if not _valid_tick(price, markets[ticker].get('price_ranges')):
                        raise NeutralExecutionError('A neutral limit is outside the current venue price grid')
                    wires.append(dict(ticker=ticker, client_order_id=str(uuid.uuid4()),
                        side='bid' if (action == 'buy') == (outcome == 'yes') else 'ask',
                        count=format(Decimal(leg['count']), '.2f'), price=format(price, '.4f'),
                        time_in_force='immediate_or_cancel', self_trade_prevention_type='taker_at_cross',
                        cancel_order_on_pause=True, reduce_only=action == 'sell', exchange_index=shard))
                validator(raw_snapshot, _clock(self.clock()))
                audit = {key: raw_snapshot[key] for key in ('kind', 'version', 'observed_at', 'model',
                    'contracts', 'market_evidence', 'acquisition') if key in raw_snapshot}
                audit['venue_evidence'] = self._venue_evidence
                basket = self.journal.begin(plan, frame, wires, _clock(self.clock()), audit=audit)
                starting_cash, starting_positions = economic_cash, dict(own)
                for index, (leg, wire) in enumerate(zip(plan['legs'], wires)):
                    identifier, order_id = wire['client_order_id'], None
                    try:
                        # Confirm the prior acknowledged leg is reflected in live
                        # ownership before submitting another independent IOC.
                        if index:
                            _, current_funding = self._account(shard)
                            if leg['action'] == 'buy':
                                unit = worst_value_buy_unit_cost(leg['limit_price'], ValueSizingConfig(
                                    fee_type=known[leg['ticker']]['fee_type'], fee_multiplier=known[leg['ticker']]['fee_multiplier']))
                                if leg['count'] * unit > current_funding:
                                    raise NeutralExecutionError('The remaining neutral leg no longer fits confirmed funding')
                        raw_snapshot, frame = self._refresh(raw_snapshot, frame, risk_only=risk_only,
                            required=[row['ticker'] for row in plan['legs']] + list(self.journal.positions()))
                        self._project_remaining(frame, plan, plan['legs'][index:], starting_cash,
                            starting_positions, risk_only=risk_only)
                        self._pre_submit(raw_snapshot, frame, markets[leg['ticker']], leg, should_stop, risk_only=risk_only)
                    except Exception:
                        self.journal.finish_basket(basket, 'partial' if fills else 'skipped', incomplete=bool(fills))
                        return self._result('partial' if fills else 'skipped',
                            'The remaining neutral leg was withheld after a fresh account, model or stop check', plan=plan, fills=fills)
                    self.journal.sending(identifier)
                    try:
                        # A clock/model check AFTER the fsync-backed intent is
                        # still mandatory. No HTTP write happens if this fails.
                        self._local_check(raw_snapshot, leg, should_stop, risk_only=risk_only)
                    except Exception:
                        self.journal.finish_order(identifier, status='skipped')
                        self.journal.finish_basket(basket, 'partial' if fills else 'skipped', incomplete=bool(fills))
                        return self._result('partial' if fills else 'skipped',
                            'The neutral intent expired during its durable commit; no remaining leg was sent', plan=plan, fills=fills)
                    sent_at = _clock(self.clock())
                    try:
                        ack = self.client.create_order(wire)
                        if isinstance(ack, dict):
                            order_id = ack.get('order_id')
                        evidence = self._acknowledgement(ack, leg, wire, sent_at)
                        self.journal.finish_order(identifier, status='filled' if evidence['fill_count'] else 'canceled',
                            ack=evidence, count=evidence['fill_count'], cash_delta=evidence['cash_delta'],
                            fees=evidence['fees'], order_id=evidence['order_id'])
                        fills.append(evidence)
                        contract = known[leg['ticker']]
                        fee_config = ValueSizingConfig(fee_type=contract['fee_type'], fee_multiplier=contract['fee_multiplier'])
                        bound = _worst_cash_delta(dict(leg, count=evidence['fill_count']), fee_config)
                        if Decimal(evidence['cash_delta']) < bound:
                            self.journal.halt('Actual acknowledged fees or cash flow exceed the supported venue fee bound')
                            self.journal.finish_basket(basket, 'halted', incomplete=True)
                            return self._result('halted',
                                'Actual neutral cash flow exceeded its fee bound; known fills are recorded and further orders require review',
                                plan=plan, fills=fills)
                    except Exception as error:
                        if (type(error) is KalshiError and error.status_code in (400, 401, 403, 429)
                                and error.error_code != 'order_already_exists' and order_id is None):
                            self.journal.finish_order(identifier, status='rejected', ack=dict(
                                http_status=error.status_code, error_code=_known_error_code(error.error_code)))
                            self.journal.finish_basket(basket, 'partial' if fills else 'rejected', incomplete=bool(fills))
                            return self._result('partial' if fills else 'rejected',
                                'The venue definitively rejected a neutral leg; remaining additions were withheld', plan=plan, fills=fills)
                        self.journal.unknown(identifier, order_id)
                        self.journal.finish_basket(basket, 'unknown', incomplete=True)
                        return self._result('unknown', 'A neutral IOC outcome is uncertain; the entire journal is halted without retry', plan=plan, fills=fills)
                    if evidence['fill_count'] != leg['count']:
                        self.journal.finish_basket(basket, 'partial' if evidence['fill_count'] or len(fills) > 1 else 'unfilled',
                            incomplete=bool(self.journal.positions()))
                        return self._result('partial' if self.journal.positions() else 'unfilled',
                            'An IOC leg filled incompletely; new risk is blocked while confirmed inventory is reduced', plan=plan, fills=fills)
                self.journal.finish_basket(basket, 'filled')
                return self._result('filled', 'Independent neutral IOC legs and actual cash flows were durably acknowledged', plan=plan, fills=fills)
        except Exception as error:
            reason = str(error) if isinstance(error, NeutralExecutionError) else 'Neutral account, public evidence or durable state is unavailable'
            return self._result('blocked', reason, plan=plan)
