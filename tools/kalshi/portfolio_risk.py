"""Shared, fee-inclusive portfolio admission independent of trading signals.

The existing executor owns prices, forecasts, account checks and submissions.
This journal adds an admission check immediately before its durable intent is
created, under the same exclusive journal lock. It never submits an order or
releases a reservation, and it preserves the engine's settlement evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import json
import sqlite3
import threading
from types import MappingProxyType

from .asset_universe import universe_asset_from_ticker, universe_asset_spec
from .engine import (EngineError, Journal, ONE, ZERO, _execution_metadata,
                     _identifier, _iso, decimal_value)
from .exit_reentry import exit_cycle_state


_GROUPS = frozenset(('crypto', 'metals', 'energy'))
_META = 'portfolio_limits'
_LIMIT = Decimal('1000000')
_RESERVE_LIMIT = Decimal('1000000000000')
_EXIT_KINDS = frozenset(('kalshi_bid_exit_sizing', 'kalshi_late_model_exit_sizing'))


class PortfolioRiskError(EngineError):
    """A fixed, non-sensitive portfolio policy or admission error."""


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _dollars(value, name, *, positive=False):
    try:
        result = decimal_value(value, name, maximum=_LIMIT)
    except EngineError:
        raise PortfolioRiskError('Portfolio dollar caps must be finite nonnegative amounts with at most four decimal places') from None
    if positive and result <= ZERO:
        raise PortfolioRiskError('The portfolio total budget must be positive')
    return result.normalize()


def _asset(symbol):
    try:
        spec = universe_asset_spec(symbol)
        if spec.group not in _GROUPS:
            raise ValueError()
        return spec
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PortfolioRiskError('A portfolio asset is outside the supported onboarding universe') from None


def _ticker(ticker):
    try:
        spec = universe_asset_from_ticker(_identifier(ticker))
        if spec.group not in _GROUPS:
            raise ValueError()
        return spec
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PortfolioRiskError('An active portfolio contract has an unsupported identity') from None


def _reserve(value):
    try:
        return decimal_value(value, 'portfolio reservation', places=18, maximum=_RESERVE_LIMIT)
    except EngineError:
        raise PortfolioRiskError('An active portfolio reservation is invalid') from None


@dataclass(frozen=True)
class PortfolioLimits:
    total_budget: Decimal
    asset_caps: dict
    group_caps: dict
    max_open_markets: int

    def __post_init__(self):
        total = _dollars(self.total_budget, 'portfolio total', positive=True)
        if (not isinstance(self.asset_caps, (dict, MappingProxyType))
                or not 1 <= len(self.asset_caps) <= 100):
            raise PortfolioRiskError('Portfolio limits require explicit asset caps')
        assets = {}
        for symbol, value in self.asset_caps.items():
            spec = _asset(symbol)
            if spec.symbol != symbol:
                raise PortfolioRiskError('Portfolio asset symbols must be exact canonical identities')
            assets[symbol] = _dollars(value, 'portfolio asset cap')
        if (not isinstance(self.group_caps, (dict, MappingProxyType))
                or not self.group_caps or set(self.group_caps) - _GROUPS):
            raise PortfolioRiskError('Portfolio groups must be crypto, metals or energy')
        groups = {group: _dollars(value, 'portfolio group cap') for group, value in self.group_caps.items()}
        if any(_asset(symbol).group not in groups for symbol in assets):
            raise PortfolioRiskError('Every portfolio asset requires an explicit group cap')
        if any(value > total for value in (*assets.values(), *groups.values())):
            raise PortfolioRiskError('Asset and group caps cannot exceed the shared portfolio total')
        if type(self.max_open_markets) is not int or not 1 <= self.max_open_markets <= 1000:
            raise PortfolioRiskError('Choose between one and one thousand simultaneous portfolio markets')
        object.__setattr__(self, 'total_budget', total)
        object.__setattr__(self, 'asset_caps', MappingProxyType(dict(sorted(assets.items()))))
        object.__setattr__(self, 'group_caps', MappingProxyType(dict(sorted(groups.items()))))

    @classmethod
    def from_dict(cls, raw):
        if (not isinstance(raw, dict)
                or set(raw) != {'kind', 'version', 'total_budget', 'asset_caps', 'group_caps', 'max_open_markets'}
                or raw.get('kind') != 'kalshi_portfolio_limits'
                or type(raw.get('version')) is not int or raw['version'] != 1):
            raise PortfolioRiskError('Unsupported portfolio limits document')
        return cls(raw['total_budget'], raw['asset_caps'], raw['group_caps'], raw['max_open_markets'])

    def to_dict(self):
        return dict(kind='kalshi_portfolio_limits', version=1, total_budget=_text(self.total_budget),
            asset_caps={key: _text(value) for key, value in self.asset_caps.items()},
            group_caps={key: _text(value) for key, value in self.group_caps.items()},
            max_open_markets=self.max_open_markets)


def _canonical(limits):
    return json.dumps(limits.to_dict(), sort_keys=True, separators=(',', ':'), allow_nan=False)


class PortfolioJournal(Journal):
    """One account journal with additional entry concentration limits.

    Changes to ``portfolio_limits`` require the explicit constructor flag
    ``update_portfolio_limits=True``. Updates append policy history and never
    rewrite orders, cash baselines, risk settings or settlement releases.
    """

    def __init__(self, path, mode='paper', environment='demo', risk=None, *,
                 portfolio_limits, update_portfolio_limits=False):
        if not isinstance(portfolio_limits, PortfolioLimits):
            raise PortfolioRiskError('A validated PortfolioLimits instance is required')
        if type(update_portfolio_limits) is not bool:
            raise PortfolioRiskError('The portfolio policy update flag must be Boolean')
        self._portfolio_limits = portfolio_limits
        self._portfolio_policy = _canonical(portfolio_limits)
        self._portfolio_lock_owner = None
        super().__init__(path, mode=mode, environment=environment, risk=risk)
        try:
            with self.exclusive():
                previous = self._meta(_META)
                if previous is not None:
                    try:
                        decoded = json.loads(previous)
                        saved = PortfolioLimits.from_dict(decoded)
                    except (ValueError, TypeError):
                        raise PortfolioRiskError('The saved portfolio limits are invalid') from None
                    if previous != _canonical(saved):
                        raise PortfolioRiskError('The saved portfolio limits are not canonical')
                    if previous != self._portfolio_policy and not update_portfolio_limits:
                        raise PortfolioRiskError('Portfolio limits differ; stop the old coordinator and explicitly update portfolio limits')
                if previous != self._portfolio_policy:
                    reserved = _reserve(self.active_committed())
                    with self.connection:
                        self.connection.execute('''CREATE TABLE IF NOT EXISTS portfolio_limit_changes (
                            id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL, account_fingerprint TEXT,
                            previous_limits TEXT, new_limits TEXT NOT NULL, intent_count INTEGER NOT NULL,
                            active_reserved TEXT NOT NULL)''')
                        self.connection.execute('INSERT INTO portfolio_limit_changes '
                            '(changed_at,account_fingerprint,previous_limits,new_limits,intent_count,active_reserved) '
                            'VALUES (?,?,?,?,?,?)', (_iso(), self._meta('account_fingerprint'), previous,
                                self._portfolio_policy, len(self.intents()), _text(reserved)))
                        self.connection.execute('INSERT OR REPLACE INTO kalshi_meta VALUES (?,?)',
                            (_META, self._portfolio_policy))
        except sqlite3.Error:
            self.close()
            raise PortfolioRiskError('The portfolio limits could not be committed atomically') from None
        except BaseException:
            self.close()
            raise

    @property
    def portfolio_limits(self):
        return self._portfolio_limits

    @contextmanager
    def exclusive(self):
        with super().exclusive():
            self._portfolio_lock_owner = threading.get_ident()
            try:
                yield
            finally:
                self._portfolio_lock_owner = None

    def _locked(self):
        return self._portfolio_lock_owner == threading.get_ident()

    def _check_policy(self):
        if self._meta(_META) != self._portfolio_policy:
            raise PortfolioRiskError('Portfolio limits changed; stop and restart the coordinator')

    def _state(self):
        self._check_policy()
        rows = self.intents()
        released = self.released_intent_ids() | self.settled_exposure_intent_ids()
        active, asset_used, group_used, markets = [], {}, {group: ZERO for group in _GROUPS}, set()
        total = ZERO
        for row in rows:
            if row['client_order_id'] in released:
                continue
            cost = _reserve(row['reserved_cost'])
            spec = _ticker(row['ticker'])
            if row['status'] in ('sending', 'unknown'):
                count = decimal_value(row['count'], 'pending portfolio quantity', places=2,
                    maximum=Decimal('100000000'))
                price = decimal_value(row['price'], 'pending portfolio price', maximum=ONE)
                fill = decimal_value(row['fill_count'], 'pending portfolio fill', places=2, maximum=count)
                if (count <= ZERO or count != count.to_integral_value() or fill != fill.to_integral_value()
                        or not ZERO < price < ONE or cost < count * (price + self.risk.fee_reserve_per_contract)):
                    raise PortfolioRiskError('An uncertain portfolio order is not fully reserved')
            total += cost
            asset_used[spec.symbol] = asset_used.get(spec.symbol, ZERO) + cost
            group_used[spec.group] += cost
            if cost > ZERO or row['status'] in ('sending', 'unknown'):
                markets.add(row['ticker'])
            active.append(row)
        if total != _reserve(self.active_committed()):
            raise PortfolioRiskError('The shared portfolio reservations changed during admission')
        return dict(rows=rows, active=active, total=total, asset_used=asset_used,
            group_used=group_used, markets=markets,
            outside_plan=sorted(symbol for symbol, value in asset_used.items()
                if value > ZERO and symbol not in self.portfolio_limits.asset_caps))

    def _snapshot(self, state):
        limits = self.portfolio_limits
        return dict(kind='kalshi_portfolio_risk', version=1, execution_eligible=False,
            limits=limits.to_dict(), active_reserved=_text(state['total']),
            gross_reserved=_text(_reserve(self.committed())),
            remaining=_text(max(ZERO, limits.total_budget - state['total'])),
            asset_reserved={symbol: _text(value) for symbol, value in sorted(state['asset_used'].items())},
            asset_remaining={symbol: _text(max(ZERO, cap - state['asset_used'].get(symbol, ZERO)))
                for symbol, cap in limits.asset_caps.items()},
            group_reserved={group: _text(value) for group, value in sorted(state['group_used'].items())},
            group_remaining={group: _text(max(ZERO, cap - state['group_used'].get(group, ZERO)))
                for group, cap in limits.group_caps.items()},
            open_markets=sorted(state['markets']), open_market_count=len(state['markets']),
            active_assets_outside_plan=state['outside_plan'])

    def portfolio_snapshot(self):
        """Read reservation accounting only; never imply an account preflight."""
        if not self._locked():
            with self.exclusive():
                return self.portfolio_snapshot()
        return self._snapshot(self._state())

    def _verified_reduction(self, state, ticker, outcome, count, signal_id, sizing, execution):
        claim = (isinstance(signal_id, str) and signal_id.startswith('minute-exit-live-')
            or isinstance(sizing, dict) and ('reduce_only' in sizing or sizing.get('action') == 'sell'))
        if isinstance(sizing, dict) and any('bypass' in str(key).lower() or str(key).lower().startswith('ignore_') for key in sizing):
            raise PortfolioRiskError('Unrecognized portfolio risk bypass metadata is prohibited')
        if not claim:
            return False
        if (not isinstance(signal_id, str) or not signal_id.startswith('minute-exit-live-')
                or signal_id.startswith('minute-exit-live-manual-') or not isinstance(sizing, dict)
                or sizing.get('kind') not in _EXIT_KINDS or sizing.get('version') != 1
                or type(sizing.get('version')) is not int or sizing.get('action') != 'sell'
                or sizing.get('reduce_only') is not True or outcome not in ('yes', 'no')
                or count is None or execution is None):
            raise PortfolioRiskError('A portfolio entry-limit exemption requires a verified reducing exit')
        checked = _execution_metadata(execution, count)
        if checked['order_type'] != 'ioc' or checked['cancel_requested'] or Decimal(checked['remaining_count']) != ZERO:
            raise PortfolioRiskError('A reducing portfolio exit requires an immediate-or-cancel order')
        market_rows = [row for row in state['active'] if row['ticker'] == ticker]
        if not market_rows or any(row['status'] in ('sending', 'unknown') for row in market_rows):
            raise PortfolioRiskError('An uncertain or unowned market cannot receive a reducing-exit exemption')
        for row in market_rows:
            if row['status'] == 'filled' and (not isinstance(row.get('order_id'), str) or not row['order_id']):
                raise PortfolioRiskError('The owned portfolio position lacks confirmed order evidence')
        position = exit_cycle_state(market_rows)['position']
        try:
            before = decimal_value(sizing.get('net_position_before'), 'reducing exit position', places=2,
                minimum=-Decimal('100000000'), maximum=Decimal('100000000'))
        except EngineError:
            raise PortfolioRiskError('The reducing-exit position evidence is invalid') from None
        if (position == ZERO or before != position
                or sizing.get('held_outcome') != ('yes' if position > ZERO else 'no')
                or outcome != ('no' if position > ZERO else 'yes')
                or not ZERO < count <= abs(position)):
            raise PortfolioRiskError('The proposed exit does not reduce the confirmed owned position')
        return True

    def assess_admission(self, ticker, cost, *, outcome=None, count=None, price=None,
                         signal_id=None, sizing=None, execution=None):
        """Assess local caps without recording or submitting an order.

        Passing ticker/cost alone evaluates an ordinary entry. Complete proposal
        fields are required for a reducing-exit exemption. Venue cash, current
        ownership, model eligibility and exchange state remain executor gates.
        """
        if not self._locked():
            with self.exclusive():
                return self.assess_admission(ticker, cost, outcome=outcome, count=count, price=price,
                    signal_id=signal_id, sizing=sizing, execution=execution)
        spec, value = _ticker(ticker), _reserve(cost)
        if value <= ZERO:
            raise PortfolioRiskError('A proposed order requires a positive full cost reservation')
        if count is not None:
            count = decimal_value(count, 'portfolio order quantity', places=2, maximum=Decimal('100000000'))
            if count <= ZERO or count != count.to_integral_value():
                raise PortfolioRiskError('Portfolio proposals require positive whole contract quantities')
        if price is not None:
            price = decimal_value(price, 'portfolio order price', maximum=ONE)
            if not ZERO < price < ONE or count is None or value < count * (price + self.risk.fee_reserve_per_contract):
                raise PortfolioRiskError('The proposed portfolio order is not fully reserved including fees')
        state = self._state()
        reduction = self._verified_reduction(state, ticker, outcome, count, signal_id, sizing, execution)
        reasons, limits = [], self.portfolio_limits
        if not reduction:
            if any(row['ticker'] == ticker and row['status'] in ('sending', 'unknown') for row in state['active']):
                reasons.append('The proposed contract has an unresolved order')
            if spec.symbol not in limits.asset_caps:
                reasons.append('The proposed asset is outside the portfolio plan')
            if state['outside_plan']:
                reasons.append('Active reservations exist outside the portfolio asset caps')
            if state['total'] + value > limits.total_budget:
                reasons.append('The shared portfolio budget would be exceeded')
            if spec.symbol in limits.asset_caps and state['asset_used'].get(spec.symbol, ZERO) + value > limits.asset_caps[spec.symbol]:
                reasons.append('The per-asset portfolio cap would be exceeded')
            if spec.group not in limits.group_caps or state['group_used'][spec.group] + value > limits.group_caps[spec.group]:
                reasons.append('The portfolio sector cap would be exceeded')
            if len(state['markets'] | {ticker}) > limits.max_open_markets:
                reasons.append('The simultaneous unsettled-market limit would be exceeded')
        result = self._snapshot(state)
        result.update(kind='kalshi_portfolio_admission', allowed=not reasons,
            reason='Verified reducing exit; entry concentration caps do not block risk reduction' if reduction else
                reasons[0] if reasons else 'Within portfolio concentration caps; executor preflight is still required',
            violations=reasons, risk_reducing=reduction, entry_limits_exempt=reduction,
            proposal=dict(ticker=ticker, asset=spec.symbol, group=spec.group, reserved_cost=_text(value)))
        return result

    def record(self, signal, outcome, count, price, cost, now, sizing=None, execution=None):
        if not self._locked():
            raise PortfolioRiskError('Portfolio order admission requires the caller to hold the exclusive journal lock')
        if not isinstance(signal, dict) or outcome not in ('yes', 'no'):
            raise PortfolioRiskError('The proposed portfolio signal or outcome is invalid')
        signal_id = _identifier(signal.get('signal_id'))
        if execution is None:
            raise PortfolioRiskError('Portfolio execution requires explicit order metadata')
        count = decimal_value(count, 'portfolio order quantity', places=2, maximum=Decimal('100000000'))
        price = decimal_value(price, 'portfolio order price', maximum=ONE)
        cost = _reserve(cost)
        checked = _execution_metadata(execution, count)
        if checked['order_type'] != 'ioc':
            raise PortfolioRiskError('The portfolio coordinator currently admits only immediate-or-cancel orders')
        assessment = self.assess_admission(signal.get('ticker'), cost, outcome=outcome,
            count=count, price=price, signal_id=signal_id, sizing=sizing, execution=execution)
        if not assessment['allowed']:
            raise PortfolioRiskError(assessment['reason'])
        return super().record(signal, outcome, count, price, cost, now, sizing=sizing, execution=execution)
