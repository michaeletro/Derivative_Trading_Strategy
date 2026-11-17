"""Isolated cash-ledger execution for cost-aware binary value/Kelly decisions.

Paper is the default. Live execution needs an independently validated calibration
artifact, exact account ownership, fresh venue data, and a durable IOC intent.
An uncertain acknowledgement halts this journal; it is never blindly retried.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .btc15m import _effective_fee, _eligible_market, verified_crypto_settlement_source
from .client import KalshiError, _known_error_code
from .crypto_assets import asset_spec
from .engine import EngineError, ONE, ZERO, _identifier, _iso, _levels, _timestamp, _valid_tick
from .minute_five import _identity
from .settled_exposure import _account_settlement
from .value_calibration import calibrate_probability
from .value_sizing import (ValueSizingConfig, evaluate_value_fill, plan_value_trade,
                          worst_value_buy_unit_cost)


class ValueExecutionError(EngineError):
    """A fixed, non-sensitive value-strategy configuration or state error."""


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _number(value, name='amount', *, signed=False, maximum=Decimal('1000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueExecutionError('The value-strategy ' + name + ' is invalid')
    try:
        amount = Decimal(str(value))
    except Exception:
        raise ValueExecutionError('The value-strategy ' + name + ' is invalid') from None
    if not amount.is_finite() or abs(amount) > maximum or not signed and amount < ZERO:
        raise ValueExecutionError('The value-strategy ' + name + ' is outside its bounds')
    return amount


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise ValueExecutionError('The value-strategy clock is invalid')
    return float(value)


def _integer(value, name='count'):
    amount = _number(value, name, signed=True, maximum=Decimal('100000000'))
    if amount != amount.to_integral_value():
        raise ValueExecutionError('The value-strategy requires whole contract quantities')
    return int(amount)


def _cash_grid(value):
    return value.quantize(Decimal('.0001'), rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class ValueExecutionConfig:
    sizing: ValueSizingConfig = field(default_factory=ValueSizingConfig)
    initial_cash: str = '100'
    max_drawdown_fraction: str = '.10'
    max_age_seconds: float = 5
    min_seconds_to_close: int = 2
    min_trade_interval_seconds: float = 10
    allow_raw_paper: bool = False
    model_id: str | None = None
    fit_sha256: str | None = None
    asset: str = 'BTC'

    def __post_init__(self):
        if not isinstance(self.sizing, ValueSizingConfig):
            raise ValueExecutionError('A validated value sizing configuration is required')
        cash = _number(self.initial_cash, 'initial cash', maximum=Decimal('1000000'))
        drawdown = _number(self.max_drawdown_fraction, 'drawdown fraction', maximum=ONE)
        if cash <= ZERO or not ZERO < drawdown < ONE:
            raise ValueExecutionError('Initial cash and drawdown fraction must be positive and bounded')
        if (type(self.max_age_seconds) not in (int, float) or not math.isfinite(self.max_age_seconds)
                or not 0 < self.max_age_seconds <= 5
                or type(self.min_seconds_to_close) is not int or not 1 <= self.min_seconds_to_close <= 899
                or type(self.min_trade_interval_seconds) not in (int, float)
                or not math.isfinite(self.min_trade_interval_seconds)
                or not 0 <= self.min_trade_interval_seconds <= 900
                or type(self.allow_raw_paper) is not bool):
            raise ValueExecutionError('The value-strategy timing or research configuration is invalid')
        profile = asset_spec(self.asset)
        expected_model = 'cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-median-v1'
        if self.model_id is not None and self.model_id != expected_model:
            raise ValueExecutionError('The value settlement model differs from its selected crypto asset')
        object.__setattr__(self, 'model_id', expected_model)
        if self.fit_sha256 is not None and (not isinstance(self.fit_sha256, str)
                or len(self.fit_sha256) != 64 or any(c not in '0123456789abcdef' for c in self.fit_sha256)):
            raise ValueExecutionError('A canonical model-definition SHA256 is required')
        object.__setattr__(self, 'initial_cash', format(cash, 'f'))
        object.__setattr__(self, 'max_drawdown_fraction', format(drawdown, 'f'))

    def to_dict(self):
        result = dict(kind='kalshi_value_execution_config', version=1,
            sizing=self.sizing.to_dict(), initial_cash=self.initial_cash,
            max_drawdown_fraction=self.max_drawdown_fraction, max_age_seconds=self.max_age_seconds,
            min_seconds_to_close=self.min_seconds_to_close,
            min_trade_interval_seconds=self.min_trade_interval_seconds,
            allow_raw_paper=self.allow_raw_paper, model_id=self.model_id, fit_sha256=self.fit_sha256)
        # Preserve the existing BTC journal policy encoding. Other models carry
        # an explicit asset as well as their unique source/model identity.
        if self.asset != 'BTC':
            result['asset'] = self.asset
        return result


class ValueJournal:
    """An append-only order audit plus transactional strategy cash and inventory."""

    def __init__(self, path, config=None, *, mode='paper'):
        self.config = config or ValueExecutionConfig()
        if not isinstance(self.config, ValueExecutionConfig) or mode not in ('paper', 'live'):
            raise ValueExecutionError('Choose paper or live with a validated value configuration')
        if mode == 'live' and self.config.allow_raw_paper:
            raise ValueExecutionError('Uncalibrated research probabilities cannot enable live execution')
        self.mode = mode
        original = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (original, *original.parents)):
            raise ValueExecutionError('Value journal paths cannot contain symbolic links')
        self.path = original.resolve()
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise ValueExecutionError('Value journals cannot have hard-link aliases')
        existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = Path(str(self.path) + '.lock')
        self.connection = sqlite3.connect(str(self.path), timeout=0)
        self.connection.row_factory = sqlite3.Row
        self._policy = _json(self.config.to_dict())
        try:
            self.connection.execute('PRAGMA synchronous=FULL')
            if existed:
                if (self._get('kind') != 'kalshi_value_journal_v1' or self._get('mode') != mode
                        or self._get('config') != self._policy):
                    raise ValueExecutionError('The isolated value journal kind, mode or configuration differs')
            else:
                with self.connection:
                    self.connection.executescript('''
                        CREATE TABLE value_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE value_positions(ticker TEXT PRIMARY KEY,quantity INTEGER NOT NULL,
                            identity_json TEXT NOT NULL);
                        CREATE TABLE value_orders(client_order_id TEXT PRIMARY KEY,ticker TEXT NOT NULL,
                            created_at TEXT NOT NULL,status TEXT NOT NULL,plan_json TEXT NOT NULL,
                            wire_json TEXT NOT NULL,order_id TEXT,fill_count INTEGER NOT NULL DEFAULT 0,
                            cash_delta TEXT NOT NULL DEFAULT '0',fees TEXT NOT NULL DEFAULT '0',ack_json TEXT);
                        CREATE TABLE value_settlements(ticker TEXT PRIMARY KEY,observed_at TEXT NOT NULL,
                            payout TEXT NOT NULL,proof_json TEXT NOT NULL);
                        CREATE TABLE value_risk_events(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,
                            proof_json TEXT NOT NULL);
                    ''')
                    for key, value in dict(kind='kalshi_value_journal_v1', mode=mode, config=self._policy,
                            cash=self.config.initial_cash, high_water=self.config.initial_cash,
                            drawdown_halted='false').items():
                        self.connection.execute('INSERT INTO value_meta VALUES (?,?)', (key, value))
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        except Exception as error:
            self.connection.close()
            if isinstance(error, ValueExecutionError):
                raise
            raise ValueExecutionError('The value journal is unavailable or invalid') from None

    def _get(self, key):
        row = self.connection.execute('SELECT value FROM value_meta WHERE key=?', (key,)).fetchone()
        return None if row is None else row[0]

    def _set(self, key, value):
        self.connection.execute('INSERT INTO value_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)))

    def _check(self):
        if self._get('config') != self._policy or self._get('mode') != self.mode:
            raise ValueExecutionError('The saved value policy changed; stop and restart the runner')

    @contextmanager
    def exclusive(self):
        token = str(uuid.uuid4())
        try:
            fd = os.open(str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise ValueExecutionError('The value journal is locked by another runner') from None
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            self._check()
            yield
        finally:
            try:
                if self.lock_path.read_text(encoding='utf-8') == token:
                    self.lock_path.unlink()
            except OSError:
                pass

    def bind_account(self, value):
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueExecutionError('The authenticated account identity is unavailable')
        previous = self._get('account_fingerprint')
        if previous is not None and previous != value:
            raise ValueExecutionError('The value journal belongs to another authenticated account')
        if previous is None:
            with self.connection:
                self._set('account_fingerprint', value)

    def cash(self):
        return _number(self._get('cash'), 'saved cash')

    def positions(self):
        result = {}
        for row in self.connection.execute('SELECT * FROM value_positions ORDER BY ticker'):
            result[row['ticker']] = dict(ticker=row['ticker'], net_position=_integer(row['quantity']),
                contract_identity=json.loads(row['identity_json']))
        return result

    def open_tickers(self):
        return [ticker for ticker, row in self.positions().items() if row['net_position']]

    def orders(self):
        result = []
        for row in self.connection.execute('SELECT * FROM value_orders ORDER BY rowid'):
            item = dict(row)
            for key in ('plan_json', 'wire_json', 'ack_json'):
                item[key[:-5]] = None if item[key] is None else json.loads(item[key])
                del item[key]
            result.append(item)
        return result

    def halted(self):
        return self.connection.execute("SELECT 1 FROM value_orders WHERE status IN ('sending','unknown') LIMIT 1").fetchone() is not None

    def drawdown_halted(self):
        value = self._get('drawdown_halted')
        if value not in ('true', 'false'):
            raise ValueExecutionError('The saved drawdown state is invalid')
        return value == 'true'

    def mark(self, equity, now):
        equity, stamp = _number(equity, 'marked equity'), _clock(now)
        high = max(equity, _number(self._get('high_water'), 'high-water equity'))
        triggered = equity <= high * (ONE - Decimal(self.config.max_drawdown_fraction))
        with self.connection:
            self._set('high_water', format(high, 'f'))
            if triggered and not self.drawdown_halted():
                self._set('drawdown_halted', 'true')
                self.connection.execute('INSERT INTO value_risk_events(created_at,proof_json) VALUES (?,?)',
                    (_iso(stamp), _json(dict(kind='value_drawdown_trigger', equity=format(equity, 'f'),
                        high_water=format(high, 'f'), max_drawdown_fraction=self.config.max_drawdown_fraction))))
        return self.drawdown_halted()

    def record(self, ticker, identity, plan, wire, now):
        identifier = wire['client_order_id']
        with self.connection:
            self._check()
            if self.halted():
                raise ValueExecutionError('An unresolved value order prevents a new submission')
            old = self.positions().get(ticker)
            if old is not None and old['contract_identity'] != identity:
                raise ValueExecutionError('The saved value contract identity changed')
            if old is None:
                self.connection.execute('INSERT INTO value_positions VALUES (?,?,?)', (ticker, 0, _json(identity)))
            self.connection.execute('INSERT INTO value_orders(client_order_id,ticker,created_at,status,plan_json,wire_json) '
                'VALUES (?,?,?,?,?,?)', (identifier, ticker, _iso(now), 'sending', _json(plan), _json(wire)))
        return identifier

    def unresolved(self, identifier, order_id=None):
        with self.connection:
            self.connection.execute("UPDATE value_orders SET status='unknown',order_id=? WHERE client_order_id=? AND status='sending'",
                (order_id, identifier))

    def finish(self, identifier, *, status, order_id=None, fill_count=0, cash_delta='0', fees='0', acknowledgement=None):
        fill = _integer(fill_count)
        delta, fee = _number(cash_delta, 'fill cash change', signed=True), _number(fees, 'fill fees')
        connection = self.connection
        try:
            connection.execute('BEGIN IMMEDIATE')
            self._check()
            saved = connection.execute('SELECT * FROM value_orders WHERE client_order_id=?', (identifier,)).fetchone()
            if saved is None or saved['status'] != 'sending' or saved['fill_count'] != 0:
                raise ValueExecutionError('The pending value order changed before acknowledgement')
            plan = json.loads(saved['plan_json'])
            if (status not in ('filled', 'canceled', 'rejected', 'paper_filled') or not 0 <= fill <= plan['count']
                    or (status in ('filled', 'paper_filled')) != (fill > 0)
                    or fill == 0 and (delta != ZERO or fee != ZERO)):
                raise ValueExecutionError('The value acknowledgement quantities or state are inconsistent')
            old = self.positions()[saved['ticker']]['net_position']
            direction = 1 if plan['outcome'] == 'yes' else -1
            change = fill * direction * (1 if plan['action'] == 'buy' else -1)
            new = old + change
            if plan['action'] == 'sell' and (old * change > 0 or abs(change) > abs(old)):
                raise ValueExecutionError('A value reduction cannot create or reverse exposure')
            cash = _cash_grid(self.cash() + delta)
            if cash < ZERO:
                raise ValueExecutionError('The acknowledged fill exceeds strategy cash')
            self._set('cash', format(cash, 'f'))
            connection.execute('UPDATE value_positions SET quantity=? WHERE ticker=?', (new, saved['ticker']))
            connection.execute('UPDATE value_orders SET status=?,order_id=?,fill_count=?,cash_delta=?,fees=?,ack_json=? '
                'WHERE client_order_id=?', (status, order_id, fill, format(delta, 'f'), format(fee, 'f'),
                    None if acknowledgement is None else _json(acknowledgement), identifier))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def snapshot(self):
        orders = self.orders()
        return dict(kind='kalshi_value_portfolio', version=1, mode=self.mode, cash=format(self.cash(), 'f'),
            initial_cash=self.config.initial_cash, high_water=self._get('high_water'),
            drawdown_halted=self.drawdown_halted(), unresolved_order=self.halted(),
            positions=list(self.positions().values()),
            fees=format(sum((Decimal(row['fees']) for row in orders), ZERO), 'f'),
            order_count=len(orders), filled_orders=sum(row['fill_count'] > 0 for row in orders),
            cash_precision_policy='floor_direct_member_balance',
            calibration_required_for_live=True)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ValueExecutor:
    def __init__(self, client, journal, config=None, calibration=None, *, mode=None, clock=time.time):
        self.client, self.journal = client, journal
        self.config = config or journal.config
        self.profile = asset_spec(self.config.asset)
        self.mode, self.clock, self.calibration = mode or journal.mode, clock, calibration
        if (not isinstance(journal, ValueJournal) or self.config.to_dict() != journal.config.to_dict()
                or self.mode != journal.mode or getattr(client, 'environment', None) != 'production'
                or getattr(client, '_allow_writes', None) is not (self.mode == 'live')):
            raise ValueExecutionError('The value executor client, journal and policy contexts differ')
        if self.mode == 'live' and (self.config.fit_sha256 is None or calibration is None):
            raise ValueExecutionError('Live value trading requires a model-bound validated calibration artifact')

    def _result(self, status, reason, **details):
        return dict(kind='kalshi_value_decision', version=1, mode=self.mode, status=status, reason=reason,
            **details, portfolio=self.journal.snapshot())

    def _forecast(self, snapshot, now):
        if (not isinstance(snapshot, dict) or snapshot.get('kind') != 'kalshi_minute_five_observation'
                or snapshot.get('version') != 1 or snapshot.get('observation_only') is not True
                or snapshot.get('status') != 'ready' or snapshot.get('asset') != self.config.asset):
            raise ValueExecutionError('A ready read-only observation for the selected crypto asset is required')
        forecast = snapshot.get('forecast')
        if (not isinstance(forecast, dict) or forecast.get('kind') != 'kalshi_minute_five_forecast'
                or forecast.get('version') != 1 or forecast.get('status') != 'ready'
                or forecast.get('asset') != self.config.asset or forecast.get('source') != self.profile.source
                or forecast.get('model_version') != self.config.model_id):
            raise ValueExecutionError('The value forecast source or model version differs')
        if (self.config.fit_sha256 is not None and
                (snapshot.get('model_fingerprint') != self.config.fit_sha256
                    or forecast.get('model_fingerprint', self.config.fit_sha256) != self.config.fit_sha256)):
            raise ValueExecutionError('The value observation differs from its bound model-definition fingerprint')
        identity = forecast.get('contract_identity')
        if not isinstance(identity, dict) or identity.get('ticker') != forecast.get('ticker'):
            raise ValueExecutionError('The value forecast settlement identity is unavailable')
        ticker = _identifier(forecast['ticker'])
        if not ticker.startswith(self.profile.series + '-'):
            raise ValueExecutionError('The value forecast is not a fifteen-minute contract for its selected asset')
        opened, closed = _timestamp(identity.get('open_time')), _timestamp(identity.get('close_time'))
        quote = snapshot.get('market_snapshot')
        if (not isinstance(quote, dict) or quote.get('market_status') != 'ready'
                or not isinstance(quote.get('market'), dict) or quote['market'].get('ticker') != ticker):
            raise ValueExecutionError('The value quote identity is unavailable')
        model_time, as_of, completed = (_clock(forecast.get(key))
            for key in ('model_time', 'as_of', 'forecast_completed_at'))
        fetched, quote_time = _timestamp(snapshot.get('fetched_at')), _timestamp(quote.get('fetched_at'))
        if (not opened <= model_time <= as_of <= completed <= fetched + 1e-6
                or fetched > now + 1e-6 or quote_time > completed + 1e-6 or quote_time > now
                or now - model_time > self.config.max_age_seconds
                or now - quote_time > self.config.max_age_seconds
                or now - fetched > self.config.max_age_seconds
                or not opened <= now < closed or abs(closed - opened - 900) > 1e-6):
            raise ValueExecutionError('The value model or executable quote is stale')
        yes, no = (_number(forecast.get('probability_' + side), 'probability', maximum=ONE) for side in ('yes', 'no'))
        if abs(yes + no - ONE) > Decimal('0.0000000001'):
            raise ValueExecutionError('The value probabilities do not sum to one')
        return forecast, identity, ticker, closed

    def _venue(self, identity, started):
        ticker = identity['ticker']
        market = self.client.get_market(ticker)
        if not _eligible_market(market, _clock(self.clock()), 1, series=self.profile.series) or _identity(market) != identity:
            raise ValueExecutionError('The current venue contract differs from the value forecast')
        series, event = self.client.get_series(self.profile.series), self.client.get_event(market['event_ticker'])
        if (not isinstance(series, dict) or series.get('ticker') != self.profile.series
                or series.get('frequency') != 'fifteen_min' or not isinstance(event, dict)
                or event.get('series_ticker') != self.profile.series or event.get('event_ticker') != market['event_ticker']):
            raise ValueExecutionError('The value fee metadata belongs to another market')
        verified_crypto_settlement_source(series, market, event, asset=self.config.asset)
        self._current_source_metadata = (series, event)
        fee_type, multiplier = _effective_fee(series, event)
        raw = self.client.get_orderbook(ticker)
        if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
            raise ValueExecutionError('The current value order book is unavailable')
        book = raw['orderbook_fp']
        yes, no = _levels(book.get('yes_dollars')), _levels(book.get('no_dollars'))
        if yes and no and yes[-1][0] + no[-1][0] >= ONE:
            raise ValueExecutionError('An unlocked value order book is required')
        if _clock(self.clock()) - started > self.config.max_age_seconds:
            raise ValueExecutionError('Value venue acquisition exceeded the freshness bound')
        sizing = replace(self.config.sizing, fee_type=fee_type, fee_multiplier=format(multiplier, 'f'))
        normalized = dict(yes_dollars=[[format(price, '.4f'), format(quantity, '.2f')] for price, quantity in yes],
            no_dollars=[[format(price, '.4f'), format(quantity, '.2f')] for price, quantity in no])
        return market, normalized, yes, no, sizing

    def _account(self, market=None):
        if self.mode == 'paper':
            self._economic_cash = self.journal.cash()
            return self.journal.cash()
        self.journal.bind_account(getattr(self.client, 'account_fingerprint', None))
        state = self.client.get_exchange_status()
        if not isinstance(state, dict) or state.get('exchange_active') is not True or state.get('trading_active') is not True:
            raise ValueExecutionError('Exchange trading is unavailable')
        balance, positions, resting = self.client.get_balance(), self.client.get_positions(), self.client.get_orders(status='resting')
        if not isinstance(balance, dict) or not isinstance(positions, list) or not isinstance(resting, list) or resting:
            raise ValueExecutionError('Value execution requires complete account state and no resting orders')
        cash = (_number(balance['balance_dollars'], 'account cash') if 'balance_dollars' in balance else
            _number(balance.get('balance'), 'account cash cents') / 100)
        self._economic_cash = min(cash, self.journal.cash())
        expected = {ticker: row['net_position'] for ticker, row in self.journal.positions().items() if row['net_position']}
        actual = {}
        for row in positions:
            if not isinstance(row, dict):
                raise ValueExecutionError('The value account position is unavailable')
            ticker = _identifier(row.get('ticker'))
            if ticker in actual or row.get('subaccount_number') not in (None, 0):
                raise ValueExecutionError('A duplicate or unsupported value account position exists')
            quantity = _integer(row.get('position_fp'))
            actual[ticker] = quantity
        if {ticker: count for ticker, count in actual.items() if count} != expected:
            raise ValueExecutionError('Account positions differ from confirmed value fills; reconcile external activity before trading')
        if market is not None:
            index = market.get('exchange_index')
            if type(index) is not int or not 0 <= index <= 1000:
                raise ValueExecutionError('The value market exchange shard is unavailable')
            scoped = self.client.get_exchange_balance(index)
            if not isinstance(scoped, dict) or scoped.get('exchange_index', index) != index:
                raise ValueExecutionError('The value exchange-shard cash identity differs')
            shard_cash = (_number(scoped['balance_dollars'], 'shard cash') if 'balance_dollars' in scoped else
                _number(scoped.get('balance'), 'shard cash cents') / 100)
            cash = min(cash, shard_cash)
        return min(cash, self.journal.cash())

    def _calibrated(self, forecast, closed, now, volatility_regime='all'):
        if self.calibration is not None:
            # The probability was simulated from the source tick, so its
            # calibration horizon must match training/replay. Availability of
            # the artifact is still checked at the actual decision clock.
            calibrated = calibrate_probability(self.calibration, forecast['probability_yes'],
                closed - forecast['model_time'],
                volatility_regime, source=self.profile.source, model_id=self.config.model_id,
                fit_sha256=self.config.fit_sha256, asset=self.config.asset,
                as_of=now, for_live=self.mode == 'live')
            if (calibrated.get('status') == 'ready'
                    and (self.mode != 'live' or calibrated.get('eligible_for_live') is True)):
                return calibrated
        if self.mode == 'paper' and self.config.allow_raw_paper:
            p = forecast['probability_yes']
            return dict(status='ready', p=p, p_lower=p, p_upper=p, calibration_id=None,
                eligible_for_live=False, reason='Explicit uncalibrated paper research')
        raise ValueExecutionError('A validated supported probability-calibration cell is required; no risk is added')

    def _liquidate(self, ticker, position, book, sizing, *, count=None, reason=None):
        side = 'yes' if position > 0 else 'no'
        requested = abs(position) if count is None else min(abs(position), count)
        plan = evaluate_value_fill(action='sell', outcome=side, count=min(requested, sizing.max_contracts),
            yes_bids=book['yes_dollars'], no_bids=book['no_dollars'], config=sizing)
        plan.update(action='sell', outcome=side, net_position_before=position,
            net_position_after=position - plan['count'] * (1 if position > 0 else -1),
            target_position=(abs(position) - requested) * (1 if position > 0 else -1),
            reduce_only=True, forced_risk_reduction=True,
            reason=reason or 'Latched strategy drawdown liquidation')
        return plan

    def _risk_trim(self, ticker, position, book, sizing):
        """Keep the largest whole position inside the current hard risk caps."""
        side = 'yes' if position > 0 else 'no'
        maximum = min(abs(position), sizing.max_position_contracts)
        cap = Decimal(sizing.max_market_risk)
        def mark(quantity):
            if quantity == 0:
                return ZERO
            fill = evaluate_value_fill(action='sell', outcome=side, count=quantity,
                yes_bids=book['yes_dollars'], no_bids=book['no_dollars'], config=sizing)
            if int(fill['count']) < quantity:
                # Unknown tail liquidity is not worth zero. Use a conservative
                # gross upper bound when retaining inventory under a hard cap.
                own = book['yes_dollars'] if side == 'yes' else book['no_dollars']
                return Decimal(quantity) * Decimal(own[-1][0])
            return max(ZERO, Decimal(fill['cash_delta']))
        if Decimal(sizing.fee_multiplier) * Decimal('.07') <= ONE:
            # Net proceeds are monotone at these supported ordinary fee rates.
            left, right = 0, maximum
            while left < right:
                middle = (left + right + 1) // 2
                if mark(middle) <= cap:
                    left = middle
                else:
                    right = middle - 1
            retained = left
        else:
            # At exotic fees, use a gross bid upper bound rather than assume
            # monotonicity of proceeds after fees.
            own = book['yes_dollars'] if side == 'yes' else book['no_dollars']
            bid = Decimal(own[-1][0]) if own else ONE
            retained = min(maximum, int((cap / bid).to_integral_value(rounding=ROUND_FLOOR)))
        return self._liquidate(ticker, position, book, sizing,
            count=max(1, abs(position) - retained), reason='Reduce confirmed inventory to the hard market or quantity cap')

    def execute(self, snapshot, *, should_stop=None):
        details = {}
        try:
            with self.journal.exclusive():
                if self.journal.halted():
                    return self._result('halted', 'An unresolved value IOC prevents further submissions')
                started = _clock(self.clock())
                forced = self.journal.drawdown_halted()
                model_error = None
                if forced:
                    tickers = self.journal.open_tickers()
                    if not tickers:
                        return self._result('halted', 'The strategy drawdown limit is latched; new entries are disabled')
                    ticker = tickers[0]
                    identity = self.journal.positions()[ticker]['contract_identity']
                    forecast, closed = None, _timestamp(identity['close_time'])
                else:
                    try:
                        forecast, identity, ticker, closed = self._forecast(snapshot, started)
                    except (EngineError, ValueError, TypeError, KeyError):
                        held = self.journal.open_tickers()
                        if len(held) != 1:
                            raise
                        ticker = held[0]
                        identity = self.journal.positions()[ticker]['contract_identity']
                        forecast, closed = None, _timestamp(identity['close_time'])
                        model_error = 'A fresh matched value forecast is unavailable; no model-based trade is allowed'
                details['ticker'] = ticker
                if any(other != ticker for other in self.journal.open_tickers()):
                    return self._result('hold', 'Another value market still owns unsettled inventory', **details)
                market, book, yes, no, sizing = self._venue(identity, started)
                cash = self._account(market)
                account_checked = _clock(self.clock())
                if account_checked < started or account_checked - started > self.config.max_age_seconds:
                    raise ValueExecutionError('The value account and executable book became stale before risk assessment')
                position = self.journal.positions().get(ticker, {}).get('net_position', 0)
                liquidation_mark = ZERO
                fully_marked = True
                unmarked_contracts = 0
                if position:
                    mark = evaluate_value_fill(action='sell', outcome='yes' if position > 0 else 'no',
                        count=abs(position), yes_bids=book['yes_dollars'], no_bids=book['no_dollars'], config=sizing)
                    liquidation_mark = max(ZERO, Decimal(mark['cash_delta']))
                    unmarked_contracts = abs(position) - int(mark['count'])
                    fully_marked = unmarked_contracts == 0
                marked_equity = self._economic_cash + liquidation_mark
                details.update(marked_equity=format(marked_equity, 'f') if fully_marked else None,
                    conservative_equity=format(marked_equity, 'f'), unmarked_contracts=unmarked_contracts)
                if fully_marked:
                    forced = self.journal.mark(marked_equity, self.clock())
                risk_mark = liquidation_mark
                if position and not fully_marked:
                    own = book['yes_dollars'] if position > 0 else book['no_dollars']
                    risk_mark = max(risk_mark, Decimal(abs(position)) * Decimal(own[-1][0]))
                if forced:
                    if not position:
                        return self._result('halted', 'The strategy drawdown limit is latched; no new entries', **details)
                    plan = self._liquidate(ticker, position, book, sizing)
                elif (risk_mark > Decimal(sizing.max_market_risk)
                        or abs(position) > sizing.max_position_contracts):
                    plan = self._risk_trim(ticker, position, book, sizing)
                    forced = True
                elif not fully_marked:
                    return self._result('hold',
                        'The full position cannot be valued from displayed depth; no risk is added', **details)
                else:
                    if forecast is None:
                        return self._result('skipped', model_error, **details)
                    now = _clock(self.clock())
                    self._forecast(snapshot, now)
                    probability = self._calibrated(forecast, closed, now, snapshot.get('volatility_regime', 'all'))
                    # Cash held on another venue shard still belongs to the
                    # strategy's terminal wealth. It restricts this order's
                    # funding, rather than silently shrinking the portfolio.
                    funded_sizing = replace(sizing, max_order_cost=format(max(
                        Decimal(sizing.wealth_floor), min(Decimal(sizing.max_order_cost), cash)), 'f'))
                    def funded_plan(selected_sizing):
                        return plan_value_trade(probability_yes=probability['p'],
                            probability_lower=probability['p_lower'], probability_upper=probability['p_upper'],
                            cash=self._economic_cash, net_position=position,
                            yes_bids=book['yes_dollars'], no_bids=book['no_dollars'],
                            bankroll=marked_equity, config=selected_sizing)
                    plan = funded_plan(funded_sizing)
                    if plan['action'] == 'buy' and plan['count']:
                        # The matching engine can reserve every contract at
                        # the IOC limit, rather than its expected book VWAP.
                        worst_unit = worst_value_buy_unit_cost(plan['limit_price'], sizing)
                        risk_headroom = max(ZERO, Decimal(sizing.max_market_risk) - liquidation_mark)
                        maximum_funded = int((min(cash, Decimal(sizing.max_order_cost), risk_headroom)
                            / (worst_unit + Decimal(sizing.slippage_per_contract)))
                            .to_integral_value(rounding=ROUND_FLOOR))
                        if maximum_funded < plan['count']:
                            if maximum_funded < 1:
                                return self._result('hold', 'No whole value contract fits its conservative IOC reservation',
                                    plan=plan, **details)
                            plan = funded_plan(replace(funded_sizing,
                                max_contracts=min(funded_sizing.max_contracts, maximum_funded)))
                    details['calibration'] = probability
                details['plan'] = plan
                if plan['action'] == 'hold' or plan['count'] < 1:
                    return self._result('hold', plan.get('reason', 'The aggregate position target does not justify a trade'), **details)
                if plan['action'] == 'buy' and closed - self.clock() < self.config.min_seconds_to_close:
                    return self._result('hold', 'The value entry cutoff has been reached', **details)
                last = self.journal.connection.execute('SELECT created_at FROM value_orders WHERE ticker=? ORDER BY rowid DESC LIMIT 1',
                    (ticker,)).fetchone()
                if last is not None and self.clock() - _timestamp(last[0]) < (1 if forced else self.config.min_trade_interval_seconds):
                    return self._result('hold', 'Waiting for the minimum interval after the previous durable order', **details)
                if should_stop is not None and should_stop():
                    return self._result('hold', 'The value runner was stopped before submission', **details)
                count = _integer(plan['count'])
                outcome, action = plan['outcome'], plan['action']
                if outcome not in ('yes', 'no') or action not in ('buy', 'sell'):
                    raise ValueExecutionError('The value planner returned an invalid action')
                if action == 'sell' and (not position or (position > 0) != (outcome == 'yes') or count > abs(position)):
                    raise ValueExecutionError('The value planner reduction differs from confirmed inventory')
                if action == 'buy' and position and (position > 0) != (outcome == 'yes'):
                    raise ValueExecutionError('Value reversals must close the old position first')
                limit = _number(plan['limit_price'], 'outcome limit', maximum=ONE)
                wire_price = limit if outcome == 'yes' else ONE - limit
                side = 'bid' if (action == 'buy') == (outcome == 'yes') else 'ask'
                if not _valid_tick(wire_price, market.get('price_ranges')):
                    raise ValueExecutionError('The value IOC limit is outside the current price grid')
                if action == 'buy':
                    worst_unit = worst_value_buy_unit_cost(limit, sizing)
                    conservative_p = Decimal(str(probability['p_lower'])) if outcome == 'yes' else ONE - Decimal(str(probability['p_upper']))
                    if conservative_p - worst_unit - Decimal(sizing.slippage_per_contract) < Decimal(sizing.min_edge):
                        return self._result('hold', 'The worst permitted IOC fill does not preserve the conservative value edge', **details)
                    reservation = count * worst_unit
                    if reservation > cash or reservation > Decimal(sizing.max_order_cost):
                        return self._result('hold', 'Conservative IOC fee reservation exceeds available cash or the order cap', **details)
                    if liquidation_mark + reservation + count * Decimal(sizing.slippage_per_contract) > Decimal(sizing.max_market_risk):
                        return self._result('hold', 'The worst permitted IOC fill exceeds the remaining aggregate market risk', **details)
                current = self.client.get_market(ticker)
                now = _clock(self.clock())
                if (_identity(current) != identity or not _eligible_market(current, now, 1, series=self.profile.series)
                        or now < started or now - started > self.config.max_age_seconds):
                    raise ValueExecutionError('The value contract or data became stale before its durable intent')
                current_series, current_event = self._current_source_metadata
                verified_crypto_settlement_source(current_series, current, current_event, asset=self.config.asset)
                if not forced:
                    self._forecast(snapshot, now)
                identifier = str(uuid.uuid4())
                wire = dict(ticker=ticker, client_order_id=identifier, side=side,
                    count=format(Decimal(count), '.2f'), price=format(wire_price, '.4f'),
                    time_in_force='immediate_or_cancel', self_trade_prevention_type='taker_at_cross',
                    cancel_order_on_pause=True, reduce_only=action == 'sell')
                if self.mode == 'live':
                    wire['exchange_index'] = market['exchange_index']
                self.journal.record(ticker, identity, plan, wire, now)
                details['client_order_id'] = identifier
                sent_at = _clock(self.clock())
                expired = (sent_at < started or sent_at - started > self.config.max_age_seconds
                    or closed - sent_at < (self.config.min_seconds_to_close if action == 'buy' else 1)
                    or should_stop is not None and should_stop())
                if not forced:
                    try:
                        self._forecast(snapshot, sent_at)
                    except (EngineError, ValueError, TypeError, KeyError):
                        expired = True
                if expired:
                    self.journal.finish(identifier, status='rejected')
                    return self._result('skipped', 'The value decision expired during durable commit; no order sent', **details)
                if self.mode == 'paper':
                    fill = evaluate_value_fill(action=action, outcome=outcome, count=count,
                        yes_bids=book['yes_dollars'], no_bids=book['no_dollars'], config=sizing,
                        limit_price=limit)
                    self.journal.finish(identifier, status='paper_filled' if fill['count'] else 'canceled',
                        order_id='paper-' + identifier, fill_count=fill['count'], cash_delta=fill['cash_delta'],
                        fees=fill.get('fees', fill.get('fee', '0')),
                        acknowledgement=dict(simulation='immediate_visible_book', **fill))
                    return self._result('paper_filled' if fill['count'] else 'canceled',
                        'Paper IOC evaluated against visible depth; this is not a venue fill', fill=fill, **details)
                order_id = None
                try:
                    ack = self.client.create_order(wire)
                    received = _clock(self.clock())
                    if not isinstance(ack, dict) or not isinstance(ack.get('order_id'), str) or not ack['order_id']:
                        raise ValueExecutionError('The value IOC acknowledgement is incomplete')
                    order_id = ack['order_id']
                    if ack.get('client_order_id', identifier) != identifier or ack.get('ticker', ticker) != ticker:
                        raise ValueExecutionError('The value acknowledgement identity differs')
                    fill = _integer(ack.get('fill_count'))
                    if not 0 <= fill <= count or _number(ack.get('remaining_count'), 'IOC remainder') != ZERO:
                        raise ValueExecutionError('The value acknowledgement quantities require reconciliation')
                    for alias, expected in (('fill_count_fp', fill), ('remaining_count_fp', 0)):
                        if alias in ack and _integer(ack[alias]) != expected:
                            raise ValueExecutionError('The value acknowledgement quantity aliases disagree')
                    if type(ack.get('ts_ms')) is not int or not sent_at - 5 <= ack['ts_ms'] / 1000 <= received + 5:
                        raise ValueExecutionError('The value acknowledgement matching clock is invalid')
                    average = _number(ack.get('average_fill_price'), 'actual fill price', maximum=ONE) if fill else ZERO
                    average_fee = _number(ack.get('average_fee_paid'), 'actual fill fee', maximum=Decimal('1000')) if fill else ZERO
                    if fill and (not ZERO < average < ONE or side == 'bid' and average > wire_price
                            or side == 'ask' and average < wire_price):
                        raise ValueExecutionError('The actual value fill violates its durable limit')
                    price = average if outcome == 'yes' else ONE - average
                    fees = average_fee * fill
                    delta = (-price * fill if action == 'buy' else price * fill) - fees
                    if fill and action == 'buy' and -delta > cash:
                        raise ValueExecutionError('The actual value fill exceeds confirmed strategy cash')
                    evidence = dict(order_id=order_id, client_order_id=identifier, fill_count=fill,
                        average_fill_price=format(average, 'f'), average_fee_paid=format(average_fee, 'f'),
                        cash_delta=format(delta, 'f'), ts_ms=ack['ts_ms'])
                    self.journal.finish(identifier, status='filled' if fill else 'canceled', order_id=order_id,
                        fill_count=fill, cash_delta=delta, fees=fees, acknowledgement=evidence)
                except Exception as error:
                    if type(error) is KalshiError and error.status_code in (400, 401, 403, 429) and order_id is None:
                        self.journal.finish(identifier, status='rejected', acknowledgement=dict(
                            http_status=error.status_code, error_code=_known_error_code(error.error_code)))
                        return self._result('rejected', 'The venue definitively rejected the value IOC', **details)
                    self.journal.unresolved(identifier, order_id)
                    return self._result('unknown', 'The value IOC outcome requires reconciliation; further orders are halted', **details)
                return self._result('filled' if fill else 'canceled', 'The value IOC and actual cash flow were durably acknowledged',
                    fill=evidence, **details)
        except Exception as error:
            reason = str(error) if isinstance(error, EngineError) else 'Value account, model or durable state is unavailable'
            return self._result('skipped', reason, **details)

    def settle(self, market):
        """Credit only official final outcomes; live quantities need account proof."""
        try:
            with self.journal.exclusive():
                if self.journal.halted():
                    raise ValueExecutionError('Resolve the uncertain value order before settlement accounting')
                now = _clock(self.clock())
                ticker = _identifier(market.get('ticker'))
                saved = self.journal.positions().get(ticker)
                if saved is None or not saved['net_position']:
                    return self._result('hold', 'No unsettled value inventory exists for this market', ticker=ticker)
                if self.mode == 'live':
                    market = self.client.get_market(ticker)
                if (market.get('status') != 'finalized' or market.get('market_type') != 'binary'
                        or market.get('is_provisional', False) is not False
                        or market.get('result') not in ('yes', 'no') or _identity(market) != saved['contract_identity']
                        or not _timestamp(market['close_time']) <= _timestamp(market.get('settlement_ts')) <= now
                        or _number(market.get('settlement_value_dollars'), 'settled payout', maximum=ONE)
                            != (ONE if market['result'] == 'yes' else ZERO)):
                    raise ValueExecutionError('A matching final official binary settlement is required')
                position = saved['net_position']
                account_proof = None
                if self.mode == 'live':
                    self.journal.bind_account(getattr(self.client, 'account_fingerprint', None))
                    account_proof = _account_settlement(self.client, ticker, market,
                        _timestamp(market['close_time']), now, 5)
                    side = 'yes' if position > 0 else 'no'
                    if (_integer(account_proof[side + '_count_fp']) != abs(position)
                            or _integer(account_proof[('no' if side == 'yes' else 'yes') + '_count_fp']) != 0):
                        raise ValueExecutionError('The account settlement quantity differs from owned value inventory')
                    actual = self.client.get_positions()
                    if not isinstance(actual, list) or any(row.get('ticker') == ticker
                            and _integer(row.get('position_fp')) != 0 for row in actual):
                        raise ValueExecutionError('The settled value market still has current account exposure')
                payout = Decimal(abs(position)) if (position > 0) == (market['result'] == 'yes') else ZERO
                proof = dict(market=market, account_settlement=account_proof, position=position)
                with self.journal.connection:
                    self.journal._check()
                    if self.journal.positions()[ticker] != saved:
                        raise ValueExecutionError('Value inventory changed during settlement verification')
                    self.journal.connection.execute('INSERT INTO value_settlements VALUES (?,?,?,?)',
                        (ticker, _iso(now), format(payout, 'f'), _json(proof)))
                    self.journal.connection.execute('UPDATE value_positions SET quantity=0 WHERE ticker=?', (ticker,))
                    self.journal._set('cash', format(self.journal.cash() + payout, 'f'))
                return self._result('settled', 'Official settlement credited the confirmed value inventory',
                    ticker=ticker, payout=format(payout, 'f'))
        except Exception:
            return self._result('skipped', 'Official value settlement evidence is incomplete; inventory remains reserved')
