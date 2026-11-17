"""Durable model-driven PAPER positions, exits and mean-reversion rebalances.

This module has no venue transport or exchange order API. Cash, fees and fills
are explicit simulations against supplied fixed-point order-book snapshots.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_EVEN
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from .engine import EngineError, RiskConfig, _levels
from .crypto_assets import ASSETS, asset_spec, asset_from_ticker

ZERO, ONE = Decimal(0), Decimal(1)
GRID = Decimal('0.000000000001')
PROB_GRID = Decimal('0.000001')
TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_MINUTE_FIVE_SETTINGS = dict(entry_elapsed_seconds=300, entry_window_seconds=60,
    budget='1', hold_to_settlement=True, decision='median_settlement_average')


class PortfolioError(ValueError):
    """Fixed public errors without raw account data or credentials."""


def _number(value, *, maximum=Decimal('1000000000'), places=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PortfolioError('Invalid paper portfolio numeric input')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PortfolioError('Invalid paper portfolio numeric input') from None
    if (not number.is_finite() or number < ZERO or number > maximum
            or places is not None and number.as_tuple().exponent < -places):
        raise PortfolioError('Paper portfolio numeric input exceeds supported bounds')
    return number


def _money(value):
    if value is None:
        return None
    return format(value.quantize(GRID, rounding=ROUND_HALF_EVEN), 'f').rstrip('0').rstrip('.') or '0'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 253402300799):
        raise PortfolioError('Invalid paper decision timestamp')
    return float(value)


def _stamp(value):
    if not isinstance(value, str) or len(value) > 48:
        raise PortfolioError('Invalid paper contract timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise PortfolioError('Invalid paper contract timestamp') from None


def _identity(market):
    if not isinstance(market, dict):
        raise PortfolioError('Verified paper contract identity is unavailable')
    ticker, event = market.get('ticker'), market.get('event_ticker')
    try:
        profile = asset_from_ticker(ticker)
        matching_asset = asset_from_ticker(event).symbol == profile.symbol
    except ValueError:
        matching_asset = False
    if (not matching_asset
            or not ticker.startswith(event + '-') or market.get('market_type') != 'binary'
            or market.get('settlement_bounds_type') != 'default'
            or market.get('strike_type') not in ('greater', 'greater_or_equal')
            or market.get('cap_strike') is not None
            or _number(market.get('notional_value_dollars'), maximum=ONE) != ONE):
        raise PortfolioError('Paper strategy requires a standard one-dollar supported crypto contract')
    opened, closed = _stamp(market.get('open_time')), _stamp(market.get('close_time'))
    target = _number(market.get('floor_strike'), maximum=Decimal('100000000'), places=8)
    if closed - opened != 900 or target <= ZERO:
        raise PortfolioError('Paper strategy requires a posted fifteen-minute target')
    return dict(ticker=ticker, event_ticker=event, market_type='binary',
        open_time=opened, close_time=closed, floor_strike=_money(target),
        strike_type=market['strike_type'], cap_strike=None,
        notional_value_dollars='1', settlement_bounds_type='default')


def _empty(reason, *, status='none', outcome=None):
    return dict(status=status, reason=reason, action='hold', outcome=outcome,
        count=None, price=None, reserved_cost=None, realized_pnl=None, client_order_id=None,
        model_probability=None, market_probability=None, gap=None, direction=None)


class PaperPortfolio:
    """One worker-owned SQLite ledger; capital is recycled after simulated exits."""

    def __init__(self, path, risk, *, take_profit=None, entry_edge=None, exit_edge='0.01', assets=('BTC',),
                 entry_fee=None, exit_fee=None, strategy='ensemble', strategy_config=None):
        if not isinstance(strategy, str) or strategy not in ('ensemble', 'rolling', 'minute_five'):
            raise PortfolioError('Choose the ensemble, rolling or minute-five paper strategy')
        if strategy == 'ensemble' and strategy_config not in (None, {}):
            raise PortfolioError('Rolling strategy settings require the rolling paper strategy')
        if strategy in ('rolling', 'minute_five') and not isinstance(strategy_config, dict):
            raise PortfolioError('Paper strategy settings must be an explicit finite configuration')
        if strategy == 'minute_five' and (strategy_config != _MINUTE_FIVE_SETTINGS
                or type(strategy_config.get('entry_elapsed_seconds')) is not int
                or type(strategy_config.get('entry_window_seconds')) is not int
                or strategy_config.get('hold_to_settlement') is not True):
            raise PortfolioError('Minute-five settings require a one-dollar budget, minute-five entry and hold to settlement')
        try:
            serialized_strategy = _json(strategy_config) if strategy != 'ensemble' else None
            if serialized_strategy is not None and len(serialized_strategy) > 4096:
                raise ValueError()
            self.strategy_config = json.loads(serialized_strategy) if serialized_strategy is not None else None
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise PortfolioError('Paper strategy settings must be an explicit finite configuration') from None
        self.strategy = strategy
        if (not isinstance(assets, (list, tuple)) or not 1 <= len(assets) <= len(ASSETS)
                or any(not isinstance(asset, str) or asset not in ASSETS for asset in assets)
                or len(set(assets)) != len(assets)):
            raise PortfolioError('Select a nonempty distinct supported crypto asset list')
        self.assets = tuple(symbol for symbol in ASSETS if symbol in assets)
        expected_series = asset_spec(self.assets[0]).series if len(self.assets) == 1 else None
        if (not isinstance(risk, RiskConfig) or risk.sizing_mode != 'kelly'
                or risk.required_series != expected_series
                and not (self.assets != ('BTC',) and risk.required_series is None)):
            raise PortfolioError('Paper portfolio requires a matching crypto Kelly risk policy')
        self.risk = risk
        self.entry_fee = risk.fee_reserve_per_contract if entry_fee is None else _number(entry_fee, maximum=ONE, places=4)
        self.exit_fee = risk.fee_reserve_per_contract if exit_fee is None else _number(exit_fee, maximum=ONE, places=4)
        if not ZERO < self.entry_fee < ONE or not ZERO < self.exit_fee < ONE:
            raise PortfolioError('Entry and exit fee allowances must be positive and below one dollar per contract')
        self.take_profit = None if take_profit is None else _number(take_profit, maximum=Decimal(1000), places=6)
        self.entry_edge = risk.min_edge if entry_edge is None else _number(entry_edge, maximum=ONE, places=6)
        self.exit_edge = _number(exit_edge, maximum=ONE, places=6)
        if strategy == 'minute_five' and self.take_profit is not None:
            raise PortfolioError('Minute-five paper positions are held to official settlement')
        if strategy != 'minute_five' and (self.take_profit is not None and self.take_profit <= ZERO
                or self.entry_edge < risk.min_edge or not ZERO <= self.exit_edge < self.entry_edge):
            raise PortfolioError('Entry/exit bands or take-profit target are invalid')
        from kalshi_bot import _private_path
        self.path = _private_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'portfolio_meta' not in tables:
                raise PortfolioError('Choose a separate model paper portfolio database')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS portfolio_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions(
                    ticker TEXT PRIMARY KEY,outcome TEXT NOT NULL,quantity TEXT NOT NULL,
                    cost_basis TEXT NOT NULL,identity TEXT NOT NULL,opened_at REAL NOT NULL,
                    last_bid TEXT,marked_value TEXT,mark_as_of REAL,model_probability TEXT);
                CREATE TABLE IF NOT EXISTS decisions(
                    decision_key TEXT PRIMARY KEY,ticker TEXT NOT NULL,as_of REAL NOT NULL,
                    filled INTEGER NOT NULL,payload TEXT NOT NULL);
            ''')
            settings = dict(version=1, mode='paper', risk=risk.to_dict(),
                take_profit=_money(self.take_profit), entry_edge=_money(self.entry_edge), exit_edge=_money(self.exit_edge))
            if self.assets != ('BTC',):
                settings['assets'] = list(self.assets)
            # Legacy portfolios charged the risk allowance on both legs. Keep
            # that exact immutable identity when the effective fees match it.
            if self.entry_fee != risk.fee_reserve_per_contract or self.exit_fee != risk.fee_reserve_per_contract:
                settings.update(entry_fee=_money(self.entry_fee), exit_fee=_money(self.exit_fee))
            if self.strategy != 'ensemble':
                settings.update(strategy=self.strategy, strategy_config=self.strategy_config)
            configuration = _json(settings)
            with self._transaction():
                previous = self._get('configuration')
                if previous is not None and previous != configuration:
                    raise PortfolioError('Paper portfolio configuration changed; use a separate portfolio')
                if previous is None:
                    initial = min(risk.kelly_bankroll, risk.max_total_cost)
                    for key, value in dict(configuration=configuration, initial_cash=_money(initial),
                            cash=_money(initial), realized_pnl='0', fees_paid='0', gross_turnover='0').items():
                        self._set(key, value)
        except Exception:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _get(self, key):
        row = self.db.execute('SELECT value FROM portfolio_meta WHERE key=?', (key,)).fetchone()
        return None if row is None else row[0]

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO portfolio_meta VALUES (?,?)', (key, str(value)))

    @property
    def cash(self):
        return Decimal(self._get('cash'))

    def committed(self, ticker=None):
        query = 'SELECT cost_basis FROM positions' + (' WHERE ticker=?' if ticker is not None else '')
        return sum((Decimal(row[0]) for row in self.db.execute(query, () if ticker is None else (ticker,))), ZERO)

    def exposure(self, ticker):
        row = self.db.execute('SELECT outcome,quantity,cost_basis FROM positions WHERE ticker=?', (ticker,)).fetchone()
        return dict(outcome=None, quantity=ZERO, cost_basis=ZERO) if row is None else dict(
            outcome=row['outcome'], quantity=Decimal(row['quantity']), cost_basis=Decimal(row['cost_basis']))

    def _target(self, probability, unit, cash, quantity):
        if not ZERO < unit < ONE:
            return ZERO
        bankroll = min(self.risk.kelly_bankroll, cash + quantity * unit)
        fraction = max(ZERO, (probability - unit) / (ONE - unit))
        return (bankroll * self.risk.kelly_fraction * fraction / unit).to_integral_value(rounding=ROUND_FLOOR)

    def _mark(self, ticker, bids, now, raw_probability):
        row = self.exposure(ticker)
        remaining, value = row['quantity'], ZERO
        for price, available in reversed(bids):
            if remaining <= ZERO:
                break
            if price <= self.exit_fee:
                break
            count = min(remaining, available.to_integral_value(rounding=ROUND_FLOOR))
            value += count * (price - self.exit_fee)
            remaining -= count
        marked = _money(value) if remaining == ZERO else None
        self.db.execute('UPDATE positions SET last_bid=?,marked_value=?,mark_as_of=?,model_probability=? WHERE ticker=?',
            (_money(bids[-1][0]) if bids else None, marked, now, _money(raw_probability), ticker))

    def _record(self, key, ticker, now, action, fees=ZERO):
        saved = dict(action, ticker=ticker, as_of=now, fees=_money(fees))
        self.db.execute('INSERT INTO decisions VALUES (?,?,?,?,?)',
            (key, ticker, now, int(action['status'] == 'paper_filled'), _json(saved)))
        return action

    def _fixed_budget_inputs(self, market, book):
        if self.strategy != 'minute_five':
            raise PortfolioError('Fixed-budget entries require the minute-five paper strategy')
        identity = _identity(market)
        if asset_from_ticker(identity['ticker']).symbol not in self.assets:
            raise PortfolioError('The crypto asset is outside this paper portfolio selection')
        if not isinstance(book, dict) or not isinstance(book.get('orderbook_fp'), dict):
            raise PortfolioError('Paper fixed-point order book is unavailable')
        bids = {side: _levels(book['orderbook_fp'].get(side + '_dollars')) for side in ('yes', 'no')}
        if bids['yes'] and bids['no'] and bids['yes'][-1][0] + bids['no'][-1][0] >= ONE:
            raise PortfolioError('Paper order book is locked or crossed')
        return identity, bids

    def mark_hold_position(self, market, book, now, *, probability_yes=None):
        """Update a hypothetical liquidation mark, without entering or exiting.

        This strategy actually holds to official settlement. The descriptive
        bid mark retains the existing portfolio's net-liquidation convention;
        it does not charge an exit fee to cash or trigger an early sale.
        """
        try:
            now = _time(now)
            identity, bids = self._fixed_budget_inputs(market, book)
            raw_yes = None if probability_yes is None else _number(probability_yes, maximum=ONE)
            ticker = identity['ticker']
            with self._transaction():
                row = self.db.execute('SELECT identity FROM positions WHERE ticker=?', (ticker,)).fetchone()
                if row is not None and json.loads(row[0]) != identity:
                    raise PortfolioError('Held paper contract terms changed; reconciliation is required')
                held = self.exposure(ticker)
                if held['quantity'] == ZERO:
                    return _empty('No minute-five holding requires a mark')
                if market.get('status') == 'active' and identity['open_time'] <= now < identity['close_time']:
                    raw = None if raw_yes is None else raw_yes if held['outcome'] == 'yes' else ONE - raw_yes
                    self._mark(ticker, bids[held['outcome']], now, raw)
                return _empty('Minute-five position is held to official settlement', outcome=held['outcome'])
        except PortfolioError as error:
            return _empty(str(error), status='skipped')
        except (EngineError, ValueError, TypeError, InvalidOperation, OverflowError):
            return _empty('Paper hold-mark inputs are incomplete or inconsistent', status='skipped')

    def buy_once_fixed_budget(self, market, book, now, *, outcome, probability,
                              raw_probability=None, budget='1'):
        """One atomic, whole-contract paper buy during elapsed [300,360).

        ``probability`` and ``raw_probability`` refer to the selected outcome.
        They are descriptive inputs, not Kelly or expected-edge entry filters.
        The one-dollar cap includes the entry fee allowance and every depth fill.
        Source/model/quote freshness must be verified by the acquiring service.
        """
        try:
            now = _time(now)
            identity, bids = self._fixed_budget_inputs(market, book)
            if not isinstance(outcome, str) or outcome not in ('yes', 'no'):
                raise PortfolioError('Choose the median model YES or NO outcome')
            probability = _number(probability, maximum=ONE)
            raw = probability if raw_probability is None else _number(raw_probability, maximum=ONE)
            if _number(budget, maximum=ONE, places=4) != ONE:
                raise PortfolioError('Minute-five entries require a one-dollar all-in budget')
            ticker = identity['ticker']
            # This stable decision key survives position settlement and does not
            # depend on a changing forecast timestamp or a process restart.
            key = 'minute-five-entry-' + hashlib.sha256(ticker.encode('utf-8')).hexdigest()
            with self._transaction():
                marker = self.db.execute('SELECT 1 FROM decisions WHERE decision_key=?', (key,)).fetchone()
                prior = self.db.execute('SELECT identity FROM positions WHERE ticker=?', (ticker,)).fetchone()
                if prior is not None and json.loads(prior[0]) != identity:
                    raise PortfolioError('Held paper contract terms changed; reconciliation is required')
                held = self.exposure(ticker)
                if marker is not None or held['quantity'] > ZERO:
                    held_outcome = held['outcome'] or outcome
                    if (held['quantity'] > ZERO and market.get('status') == 'active'
                            and identity['open_time'] <= now < identity['close_time']):
                        held_raw = raw if held_outcome == outcome else ONE - raw
                        self._mark(ticker, bids[held_outcome], now, held_raw)
                    return _empty('This contract already received its minute-five entry; hold to official settlement',
                                  outcome=held_outcome)
                elapsed = now - identity['open_time']
                if market.get('status') != 'active' or not 300 <= elapsed < 360 or now >= identity['close_time']:
                    return _empty('Minute-five entries require a fresh decision between 300 and 360 elapsed seconds',
                                  outcome=outcome)
                if identity['close_time'] - now < self.risk.min_seconds_to_close:
                    return _empty('The startup minimum time-to-close cutoff blocks this minute-five entry',
                                  outcome=outcome)
                venue = market.get('_crypto15m', market.get('_btc15m'))
                if venue is not None:
                    if not isinstance(venue, dict):
                        raise PortfolioError('Verified venue fee information is unavailable')
                    if self.entry_fee < _number(venue.get('fee_reserve_lower_bound'), maximum=ONE):
                        raise PortfolioError('Paper entry fee allowance is below the current verified venue fee bound')
                # The configured order/cash/exposure ceilings remain hard caps.
                # No exit allowance is added: this entry holds to settlement.
                available_budget = min(ONE, self.cash, self.risk.max_order_cost,
                    self.risk.max_total_cost - self.committed(),
                    self.risk.max_market_cost - held['cost_basis'])
                asks = [(ONE - price, count) for price, count in reversed(bids['no' if outcome == 'yes' else 'yes'])]
                count, gross, fees = ZERO, ZERO, ZERO
                for price, available in asks:
                    unit = price + self.entry_fee
                    remaining = available_budget - gross - fees
                    if remaining < unit or count >= self.risk.max_contracts:
                        break
                    take = min(available.to_integral_value(rounding=ROUND_FLOOR),
                        (remaining / unit).to_integral_value(rounding=ROUND_FLOOR),
                        Decimal(self.risk.max_contracts) - count)
                    if take <= ZERO:
                        continue
                    count += take
                    gross += take * price
                    fees += take * self.entry_fee
                if count == ZERO:
                    return _empty('No whole contract fits the one-dollar entry budget, visible liquidity and risk ceilings',
                                  outcome=outcome)
                basis = gross + fees
                self.db.execute('INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (ticker, outcome, _money(count), _money(basis), _json(identity), now,
                     None, None, None, _money(raw)))
                self._set('cash', _money(self.cash - basis))
                self._set('fees_paid', _money(Decimal(self._get('fees_paid')) + fees))
                self._set('gross_turnover', _money(Decimal(self._get('gross_turnover')) + gross))
                self._mark(ticker, bids[outcome], now, raw)
                average = gross / count
                action = dict(status='paper_filled',
                    reason='Minute-five median-choice paper entry within a one-dollar all-in cap; held to official settlement',
                    action='buy', outcome=outcome, count=_money(count), price=_money(average),
                    reserved_cost=_money(basis), realized_pnl='0', client_order_id=str(uuid.uuid4()),
                    model_probability=_money(raw), market_probability=_money(average),
                    gap=_money(raw - average), direction='long_yes' if outcome == 'yes' else 'short_yes_via_no',
                    budget='1', budget_unused=_money(ONE - basis), fees=_money(fees))
                return self._record(key, ticker, now, action, fees)
        except PortfolioError as error:
            return _empty(str(error), status='skipped')
        except (EngineError, ValueError, TypeError, InvalidOperation, OverflowError):
            return _empty('Fixed-budget paper inputs are incomplete or inconsistent', status='skipped')

    def _buy(self, ticker, outcome, asks, probability, raw_probability, identity, now):
        held = self.exposure(ticker)
        cash, quantity = self.cash, held['quantity']
        current_total, current_market = self.committed(), held['cost_basis']
        count, gross, fees = ZERO, ZERO, ZERO
        e = self.entry_fee
        for price, available in asks:
            available = min(available.to_integral_value(rounding=ROUND_FLOOR), Decimal(self.risk.max_contracts) - count)
            for _ in range(max(0, int(available))):
                unit = price + e
                target = self._target(probability, unit, cash, quantity)
                spent = gross + fees
                round_trip_edge = probability - price - e - self.exit_fee - self.exit_edge
                if (unit >= ONE or round_trip_edge < self.entry_edge or quantity >= target
                        or unit > cash or spent + unit > self.risk.max_order_cost
                        or current_total + spent + unit > self.risk.max_total_cost
                        or current_market + spent + unit > self.risk.max_market_cost):
                    break
                count += ONE; quantity += ONE; cash -= unit; gross += price; fees += e
            if count >= self.risk.max_contracts:
                break
        if count == ZERO:
            return _empty('No entry clears round-trip edge, Kelly target, liquidity and exposure limits', outcome=outcome)
        basis = current_market + gross + fees
        if held['quantity'] == ZERO:
            self.db.execute('INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (ticker, outcome, _money(quantity), _money(basis), _json(identity), now, None, None, None, _money(raw_probability)))
        else:
            self.db.execute('UPDATE positions SET quantity=?,cost_basis=? WHERE ticker=?', (_money(quantity), _money(basis), ticker))
        self._set('cash', _money(cash))
        self._set('fees_paid', _money(Decimal(self._get('fees_paid')) + fees))
        self._set('gross_turnover', _money(Decimal(self._get('gross_turnover')) + gross))
        average = gross / count
        return dict(status='paper_filled', reason='Mean-reversion entry: market price is below the conservative model by the round-trip entry band',
            action='buy', outcome=outcome, count=_money(count), price=_money(average), reserved_cost=_money(gross + fees),
            realized_pnl='0', client_order_id=str(uuid.uuid4()), model_probability=_money(raw_probability),
            market_probability=_money(average), gap=_money(raw_probability - average),
            direction='long_yes' if outcome == 'yes' else 'short_yes_via_no')

    def _flip(self, ticker, held_outcome, bids, decision, model, identity, now, selected_side, entry_allowed=None):
        """One best-level YES-book execution may close one side and open the other.

        Both legs consume the same displayed order-book side and share one
        quantity/turnover budget. The ledger transition is atomic with its
        enclosing decision transaction; a failed flip makes no mutations.
        """
        opposite = 'no' if held_outcome == 'yes' else 'yes'
        if entry_allowed is not None and not entry_allowed[opposite]:
            return None
        if selected_side not in ('auto', opposite) or identity['close_time'] - now < self.risk.min_seconds_to_close:
            return None
        levels = bids[held_outcome]
        if not levels:
            return None
        held = self.exposure(ticker)
        old_quantity, old_basis = held['quantity'], held['cost_basis']
        old_bid, available = levels[-1]
        entry_fee, exit_fee = self.entry_fee, self.exit_fee
        new_ask = ONE - old_bid
        new_cost = new_ask + entry_fee
        # A configured winning threshold closes risk first; it does not also
        # establish authorization to reopen the opposite direction.
        if (self.take_profit is not None and old_quantity > ZERO
                and old_bid - exit_fee >= old_basis / old_quantity * (ONE + self.take_profit)):
            return None
        if (old_quantity <= ZERO or old_bid <= exit_fee or new_cost >= ONE
                or decision[opposite] - new_ask - entry_fee - exit_fee - self.exit_edge < self.entry_edge):
            return None
        close_gross = old_quantity * old_bid
        close_proceeds = old_quantity * (old_bid - exit_fee)
        cash_after_close = self.cash + close_proceeds
        opening_target = self._target(decision[opposite], new_cost, cash_after_close, ZERO)
        capacity = min(Decimal(self.risk.max_contracts), available.to_integral_value(rounding=ROUND_FLOOR)) - old_quantity
        other_basis = self.committed() - old_basis
        open_quantity = min(opening_target, capacity,
            (cash_after_close / new_cost).to_integral_value(rounding=ROUND_FLOOR),
            (self.risk.max_market_cost / new_cost).to_integral_value(rounding=ROUND_FLOOR),
            (max(ZERO, self.risk.max_total_cost - other_basis) / new_cost).to_integral_value(rounding=ROUND_FLOOR),
            (max(ZERO, self.risk.max_order_cost - close_gross - old_quantity * exit_fee) / new_cost).to_integral_value(rounding=ROUND_FLOOR))
        if open_quantity < ONE:
            return None
        count = old_quantity + open_quantity
        open_gross, open_basis = open_quantity * new_ask, open_quantity * new_cost
        fees = old_quantity * exit_fee + open_quantity * entry_fee
        realized = close_proceeds - old_basis
        cash_change = close_proceeds - open_basis
        self.db.execute('UPDATE positions SET outcome=?,quantity=?,cost_basis=?,opened_at=?,last_bid=NULL,marked_value=NULL,mark_as_of=NULL,model_probability=? WHERE ticker=?',
            (opposite, _money(open_quantity), _money(open_basis), now, _money(model[opposite]), ticker))
        self._set('cash', _money(self.cash + cash_change))
        self._set('realized_pnl', _money(Decimal(self._get('realized_pnl')) + realized))
        self._set('fees_paid', _money(Decimal(self._get('fees_paid')) + fees))
        self._set('gross_turnover', _money(Decimal(self._get('gross_turnover')) + close_gross + open_gross))
        before = old_quantity if held_outcome == 'yes' else -old_quantity
        after = open_quantity if opposite == 'yes' else -open_quantity
        return dict(status='paper_filled', reason='Model net-position reversal: opposite outcome clears the full round-trip entry band',
            action='flip', outcome=opposite, count=_money(count),
            price=_money(old_bid if held_outcome == 'yes' else new_ask), reserved_cost=None,
            realized_pnl=_money(realized), client_order_id=str(uuid.uuid4()),
            net_cash_flow=_money(cash_change), from_signed_position=_money(before),
            target_signed_position=_money(after), signed_delta=_money(after - before),
            execution_side='ask' if held_outcome == 'yes' else 'bid',
            close_quantity=_money(old_quantity), open_quantity=_money(open_quantity),
            model_probability=_money(model[opposite]), market_probability=_money(new_ask),
            gap=_money(model[opposite] - new_ask),
            direction='long_yes' if opposite == 'yes' else 'short_yes_via_no')

    def _sell(self, ticker, outcome, bids, probability, raw_probability, now, *, force_exit=False, exit_reason=None):
        held = self.exposure(ticker)
        initial, basis = held['quantity'], held['cost_basis']
        cash, quantity = self.cash, initial
        count, gross, fees = ZERO, ZERO, ZERO
        e = self.exit_fee
        best = bids[-1][0] if bids else ZERO
        profit = (self.take_profit is not None and initial > ZERO
                  and best - e >= basis / initial * (ONE + self.take_profit))
        statistical_exit = force_exit and not profit
        convergence = not profit and not statistical_exit and best >= raw_probability - self.exit_edge
        reason = ('Take-profit exit: net executable proceeds reached the configured return target' if profit else
                  (exit_reason or 'Rolling probability convergence exit: the statistical exit gate is open') if statistical_exit else
                  'Probability convergence exit: executable bid reached the current model exit band' if convergence else
                  'Model risk-target reduction: held quantity exceeds the sell-side fractional-Kelly target')
        for price, available in reversed(bids):
            available = min(available.to_integral_value(rounding=ROUND_FLOOR), Decimal(self.risk.max_contracts) - count, quantity)
            for _ in range(max(0, int(available))):
                net = price - e
                if net <= ZERO or gross + price > self.risk.max_order_cost:
                    break
                if profit:
                    removed = basis if count + ONE == initial else (basis * (count + ONE) / initial).quantize(GRID, rounding=ROUND_HALF_EVEN)
                    allowed = gross + price - fees - e >= removed * (ONE + self.take_profit)
                elif statistical_exit:
                    allowed = True
                elif convergence:
                    allowed = price >= raw_probability - self.exit_edge
                else:
                    allowed = quantity > self._target(probability, net, cash, quantity)
                if not allowed:
                    break
                count += ONE; quantity -= ONE; cash += net; gross += price; fees += e
            if count >= self.risk.max_contracts or quantity == ZERO:
                break
        if count == ZERO:
            return _empty('Held position remains inside the model buy/sell band or lacks economical exit liquidity', outcome=outcome)
        remaining_basis = ZERO if quantity == ZERO else (basis * quantity / initial).quantize(GRID, rounding=ROUND_HALF_EVEN)
        removed_basis = basis - remaining_basis
        realized = gross - fees - removed_basis
        if quantity == ZERO:
            self.db.execute('DELETE FROM positions WHERE ticker=?', (ticker,))
        else:
            self.db.execute('UPDATE positions SET quantity=?,cost_basis=? WHERE ticker=?', (_money(quantity), _money(remaining_basis), ticker))
        self._set('cash', _money(cash))
        self._set('realized_pnl', _money(Decimal(self._get('realized_pnl')) + realized))
        self._set('fees_paid', _money(Decimal(self._get('fees_paid')) + fees))
        self._set('gross_turnover', _money(Decimal(self._get('gross_turnover')) + gross))
        average = gross / count
        return dict(status='paper_filled', reason=reason, action='sell', outcome=outcome,
            count=_money(count), price=_money(average), reserved_cost=_money(gross - fees),
            realized_pnl=_money(realized), client_order_id=str(uuid.uuid4()),
            model_probability=_money(raw_probability), market_probability=_money(average),
            gap=_money(raw_probability - average), direction='long_yes' if outcome == 'yes' else 'short_yes_via_no')

    def rebalance(self, forecast, market, book, now, selected_side='auto', *,
                  entry_allowed=None, exit_trigger=False, signal_reason=None):
        try:
            if self.strategy == 'minute_five':
                raise PortfolioError('Minute-five positions use fixed-budget entry and official settlement only')
            now = _time(now)
            if (entry_allowed is not None and (not isinstance(entry_allowed, dict)
                    or set(entry_allowed) != {'yes', 'no'}
                    or any(type(value) is not bool for value in entry_allowed.values()))):
                raise PortfolioError('Statistical entry permissions must be explicit YES and NO booleans')
            if type(exit_trigger) is not bool:
                raise PortfolioError('Statistical exit permission must be a boolean')
            if (signal_reason is not None and (not isinstance(signal_reason, str) or len(signal_reason) > 500
                    or re.search(r'[\x00-\x1f\x7f]', signal_reason))):
                raise PortfolioError('Statistical signal reason must be a bounded text label')
            constrained_entries = entry_allowed is not None or self.strategy == 'rolling'
            allowed_entries = ({'yes': self.strategy != 'rolling', 'no': self.strategy != 'rolling'}
                               if entry_allowed is None else dict(entry_allowed))
            if selected_side not in ('auto', 'yes', 'no') or not isinstance(selected_side, str):
                raise PortfolioError('Choose automatic, YES or NO paper positioning')
            identity = _identity(market)
            profile = asset_from_ticker(identity['ticker'])
            if profile.symbol not in self.assets:
                raise PortfolioError('The crypto asset is outside this paper portfolio selection')
            venue = market.get('_crypto15m', market.get('_btc15m'))
            entry_fee_valid = True
            if venue is not None:
                if not isinstance(venue, dict):
                    raise PortfolioError('Verified venue fee information is unavailable')
                fee_bound = _number(venue.get('fee_reserve_lower_bound'))
                entry_fee_valid = self.entry_fee >= fee_bound
                if self.exit_fee < fee_bound:
                    raise PortfolioError('Paper exit fee allowance is below the current verified venue fee bound')
            if not entry_fee_valid:
                # Existing exposure can still close with a sufficient exit
                # allowance. Neither additions nor a flip's opening leg may
                # use an understated entry cost.
                constrained_entries = True
                allowed_entries = dict(yes=False, no=False)
            if (market.get('status') != 'active' or not identity['open_time'] <= now < identity['close_time']
                    or not isinstance(forecast, dict) or forecast.get('kind') != 'kalshi_settlement_ensemble'
                    or forecast.get('version') != 1 or forecast.get('status') != 'ready'
                    or forecast.get('source') != profile.source or forecast.get('calibrated') is not False
                    or forecast.get('execution_eligible') is not False
                    or _identity(forecast.get('contract_identity')) != identity):
                raise PortfolioError('A ready matching crypto benchmark paper ensemble is required')
            as_of = _time(forecast.get('as_of'))
            if not ZERO <= now - as_of <= min(5, self.risk.max_quote_age_seconds, self.risk.max_signal_age_seconds):
                raise PortfolioError('Paper model forecast is stale or future-dated')
            raw = _number(forecast.get('probability_yes'), maximum=ONE)
            lower = _number(forecast.get('probability_yes_lower'), maximum=ONE)
            upper = _number(forecast.get('probability_yes_upper'), maximum=ONE)
            if not lower <= raw <= upper:
                raise PortfolioError('Paper model sensitivity bounds are inconsistent')
            decision = {'yes': lower.quantize(PROB_GRID, rounding=ROUND_FLOOR),
                        'no': (ONE - upper).quantize(PROB_GRID, rounding=ROUND_FLOOR)}
            model = {'yes': raw, 'no': ONE - raw}
            if not isinstance(book, dict) or not isinstance(book.get('orderbook_fp'), dict):
                raise PortfolioError('Paper fixed-point order book is unavailable')
            bids = {outcome: _levels(book['orderbook_fp'].get(outcome + '_dollars')) for outcome in ('yes', 'no')}
            if bids['yes'] and bids['no'] and bids['yes'][-1][0] + bids['no'][-1][0] >= ONE:
                raise PortfolioError('Paper order book is locked or crossed')
            key = hashlib.sha256(_json([identity['ticker'], as_of, forecast.get('model_version'),
                forecast.get('config_hash')]).encode()).hexdigest()
            ticker = identity['ticker']
            with self._transaction():
                if self.db.execute('SELECT 1 FROM decisions WHERE decision_key=?', (key,)).fetchone():
                    return _empty('This model snapshot was already processed', status='skipped')
                prior = self.db.execute('SELECT identity FROM positions WHERE ticker=?', (ticker,)).fetchone()
                if prior and json.loads(prior[0]) != identity:
                    raise PortfolioError('Held paper contract terms changed; reconciliation is required')
                held = self.exposure(ticker)
                action = _empty('No contract clears the mean-reversion entry band')
                if held['quantity'] > ZERO:
                    side = held['outcome']
                    self._mark(ticker, bids[side], now, model[side])
                    action = self._flip(ticker, side, bids, decision, model, identity, now, selected_side, allowed_entries)
                    if action is None:
                        action = self._sell(ticker, side, bids[side], decision[side], model[side], now,
                                            force_exit=exit_trigger, exit_reason=signal_reason)
                if action['status'] != 'paper_filled' and identity['close_time'] - now >= self.risk.min_seconds_to_close:
                    choices = []
                    for side in ('yes', 'no'):
                        if (not allowed_entries[side] or selected_side != 'auto' and side != selected_side
                                or held['quantity'] > ZERO and side != held['outcome']):
                            continue
                        opposite = bids['no' if side == 'yes' else 'yes']
                        if opposite:
                            asks = [(ONE - price, count) for price, count in reversed(opposite)]
                            choices.append((decision[side] - asks[0][0], side, asks))
                    if choices:
                        _, side, asks = max(choices, key=lambda item: item[:2])
                        action = self._buy(ticker, side, asks, decision[side], model[side], identity, now)
                    elif (constrained_entries and held['quantity'] == ZERO
                            and (selected_side == 'auto' and not any(allowed_entries.values())
                                 or selected_side in ('yes', 'no') and not allowed_entries[selected_side])):
                        action = (_empty('Paper entry fee allowance is below the current verified venue fee bound', status='skipped')
                                  if not entry_fee_valid else
                                  _empty(signal_reason or 'The rolling signal does not authorize an entry for the selected side'))
                elif action['status'] != 'paper_filled' and held['quantity'] == ZERO:
                    action = _empty('New entries are blocked inside the configured close-time cutoff')
                held_after = self.exposure(ticker)
                if held_after['quantity'] > ZERO:
                    marking_bids = bids[held_after['outcome']]
                    if action['status'] == 'paper_filled' and action['action'] == 'sell':
                        # The same visible contracts cannot both fill our sale
                        # and value the residual holding in this simulation.
                        consumed, residual = Decimal(action['count']), []
                        for price, available in reversed(marking_bids):
                            taken = min(consumed, available.to_integral_value(rounding=ROUND_FLOOR))
                            consumed -= taken
                            if available - taken > ZERO:
                                residual.append((price, available - taken))
                        marking_bids = list(reversed(residual))
                    self._mark(ticker, marking_bids, now, model[held_after['outcome']])
                if action['count'] is None:
                    fee = ZERO
                elif action['action'] == 'flip':
                    fee = Decimal(action['close_quantity']) * self.exit_fee + Decimal(action['open_quantity']) * self.entry_fee
                else:
                    fee = Decimal(action['count']) * (self.exit_fee if action['action'] == 'sell' else self.entry_fee)
                if action['status'] == 'paper_filled' and action['action'] in ('buy', 'flip') and signal_reason:
                    action['reason'] = signal_reason + ' | ' + action['reason']
                return self._record(key, ticker, as_of, action, fee)
        except PortfolioError as error:
            return _empty(str(error), status='skipped')
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            return _empty('Paper rebalance inputs are stale, incomplete or inconsistent', status='skipped')

    def settle_market(self, market, now):
        now = _time(now)
        identity = _identity(market)
        ticker = identity['ticker']
        if asset_from_ticker(ticker).symbol not in self.assets:
            raise PortfolioError('The crypto asset is outside this paper portfolio selection')
        if (market.get('status') != 'finalized' or market.get('result') not in ('yes', 'no')
                or market.get('is_provisional', False) is not False or now < identity['close_time']):
            return None
        if not market.get('settlement_ts') or not identity['close_time'] <= _stamp(market['settlement_ts']) <= now:
            return None
        key = 'settlement-' + hashlib.sha256(_json(identity).encode()).hexdigest()
        with self._transaction():
            if self.db.execute('SELECT 1 FROM decisions WHERE decision_key=?', (key,)).fetchone():
                return None
            row = self.db.execute('SELECT * FROM positions WHERE ticker=?', (ticker,)).fetchone()
            if row is None:
                return None
            if json.loads(row['identity']) != identity:
                raise PortfolioError('Finalized paper contract identity differs from its recorded position')
            quantity, basis = Decimal(row['quantity']), Decimal(row['cost_basis'])
            payout = ONE if row['outcome'] == market['result'] else ZERO
            proceeds = quantity * payout
            realized = proceeds - basis
            self.db.execute('DELETE FROM positions WHERE ticker=?', (ticker,))
            self._set('cash', _money(self.cash + proceeds))
            self._set('realized_pnl', _money(Decimal(self._get('realized_pnl')) + realized))
            action = dict(status='paper_filled', reason='Official finalized contract settlement applied to the paper holding',
                action='settle', outcome=row['outcome'], count=_money(quantity), price=_money(payout),
                reserved_cost=_money(proceeds), realized_pnl=_money(realized), client_order_id=str(uuid.uuid4()),
                model_probability=None, market_probability=_money(payout), gap=None,
                direction='long_yes' if row['outcome'] == 'yes' else 'short_yes_via_no')
            return self._record(key, ticker, now, action)

    def performance_ledger(self, limit=2000):
        """Read recorded simulated realizations on the owning SQLite thread.

        This separate snapshot keeps a long performance history out of the
        frequently polled strategy state. It never marks or mutates holdings.
        """
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise PortfolioError('Performance history limit must be one to two thousand executions')
        total = self.db.execute('SELECT COUNT(*) FROM decisions WHERE filled=1').fetchone()[0]
        rows = self.db.execute('SELECT decision_key,payload FROM decisions WHERE filled=1 ORDER BY rowid DESC LIMIT ?',
                               (limit,)).fetchall()
        events = []
        for row in reversed(rows):
            event = json.loads(row['payload'])
            event['id'] = row['decision_key']
            event['time'] = event['as_of']
            event['quantity'] = event['count']
            if event['action'] == 'buy':
                event['closing_quantity'] = '0'
            else:
                closed = Decimal(event['close_quantity'] if event['action'] == 'flip' else event['count'])
                event['closing_quantity'] = _money(closed)
                if event['action'] == 'flip':
                    yes_price = Decimal(event['price'])
                    close_price = yes_price if Decimal(event['from_signed_position']) > ZERO else ONE - yes_price
                    proceeds = closed * (close_price - self.exit_fee)
                else:
                    proceeds = Decimal(event['reserved_cost'])
                event['closing_basis'] = _money(proceeds - Decimal(event['realized_pnl']))
            events.append(event)
        complete = total <= limit
        return dict(events=events, initial_cash=self._get('initial_cash'), environment='production',
            accounting_method='recorded_model_average_cost', coverage=dict(
                complete=complete, history_complete=complete, truncated=not complete,
                omitted_count=max(0, total - limit), scope='supported_15m',
                reason='Recorded model paper executions; fees are the experiment allowances.' if complete else
                         f'Only the latest {limit} model paper executions are available in this view.'))

    def snapshot(self):
        positions = []
        for row in self.db.execute('SELECT * FROM positions ORDER BY ticker'):
            quantity, basis = Decimal(row['quantity']), Decimal(row['cost_basis'])
            mark = None if row['marked_value'] is None else Decimal(row['marked_value'])
            model = None if row['model_probability'] is None else Decimal(row['model_probability'])
            bid = None if row['last_bid'] is None else Decimal(row['last_bid'])
            positions.append(dict(ticker=row['ticker'], outcome=row['outcome'], quantity=_money(quantity),
                cost_basis=_money(basis), average_entry_price=_money(basis / quantity), last_bid=_money(bid),
                marked_value=_money(mark), unrealized_pnl=_money(None if mark is None else mark - basis),
                mark_as_of=row['mark_as_of'], model_probability=_money(model), market_probability=_money(bid),
                gap=_money(None if model is None or bid is None else model - bid),
                direction='long_yes' if row['outcome'] == 'yes' else 'short_yes_via_no'))
        marked = None if any(row['marked_value'] is None for row in positions) else self.cash + sum(
            (Decimal(row['marked_value']) for row in positions), ZERO)
        return dict(kind='kalshi_model_portfolio', version=1, mode='paper', initial_cash=self._get('initial_cash'),
            cash=_money(self.cash), open_cost_basis=_money(self.committed()), equity_cost_basis=_money(self.cash + self.committed()),
            marked_equity=_money(marked), realized_pnl=self._get('realized_pnl'), fees_paid=self._get('fees_paid'),
            gross_turnover=self._get('gross_turnover'), positions=positions,
            recent_trades=[json.loads(row[0]) for row in self.db.execute('SELECT payload FROM decisions WHERE filled=1 ORDER BY rowid DESC LIMIT 100')])

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
