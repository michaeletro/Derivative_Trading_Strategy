"""Offline replay of the actual minute-ladder strategy against recorded books.

The adapter deliberately runs ``MinuteFiveLiveExecutor`` rather than inventing
a uniform-buy proxy. Its production-labelled client is an in-memory simulator;
the only SQLite journal is created inside a temporary directory. No credentials,
network client, real execution journal or account mutation is available here.

Decisions see only their current observation. A submitted fixed-price IOC can
fill only against a later received book, after the configured latency. Official
outcomes change cash and release reservations only at their recorded receipt.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import math
from pathlib import Path
import tempfile

from .engine import Journal, RiskConfig, _iso, _timestamp, _valid_tick
from .btc15m import verified_crypto_settlement_source
from .entry_schedule import EntrySchedule
from .minute_five import _identity
from .minute_five_execution import MinuteFiveLiveExecutor
from .regime import RegimeConfig
from .settlement_allowance import refresh_settled_allowances
from .time_sizing import UniformSizing
from .value_sizing import ValueSizingConfig, evaluate_value_fill


ZERO, ONE = Decimal(0), Decimal(1)
_ACCOUNT = 'c' * 64


class LegacyReplayError(ValueError):
    """Invalid legacy replay evidence or bounded simulation configuration."""


def _number(value, name, *, maximum=Decimal('1000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise LegacyReplayError('Invalid legacy replay ' + name)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise LegacyReplayError('Invalid legacy replay ' + name) from None
    if not result.is_finite() or not ZERO <= result <= maximum:
        raise LegacyReplayError('Invalid legacy replay ' + name)
    return result


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise LegacyReplayError('Invalid legacy replay receipt time')
    return float(value)


def _text(value):
    return format(value, 'f')


def _stamp(row):
    return _time(row.get('_time', row.get('observed_at', row.get('available_at'))))


def _book_time(row):
    """The original HTTP completion clock when an archived wrapper aliases it."""
    return _time(row.get('book_evidence_quote_time', row['quote_time'])
        if row.get('book_evidence_id') is not None else row['quote_time'])


def _empty(initial, reason):
    return dict(status='unavailable', reason=reason, initial_cash=_text(initial), cash=_text(initial),
        realized_pnl='0', fees_paid='0', gross_turnover='0', entry_fills=0, exit_fills=0,
        filled_actions=0, settlement_events=0, open_positions=[], final_equity=None,
        all_positions_settled=False, omitted_action_rows=0, marked_equity=_text(initial),
        max_drawdown='0', max_drawdown_fraction=0.0, actions=[], equity_curve=[],
        assumptions=['No baseline is synthesized when native strategy evidence is missing.'],
        audit=dict(legacy_executor='MinuteFiveLiveExecutor', execution_eligible=False))


def _native(row):
    """Require native forecast, regime and tick-grid evidence for this baseline."""
    native, market = row.get('native_snapshot'), row.get('market')
    if not isinstance(native, dict) or not isinstance(market, dict):
        raise LegacyReplayError('The exact legacy comparison requires native_snapshot and full market metadata')
    if not isinstance(market.get('price_ranges'), list) or not market['price_ranges']:
        raise LegacyReplayError('The exact legacy comparison requires recorded market price ranges')
    # Validate every range even when fifty cents is outside the active grid.
    _valid_tick(Decimal('.5'), market['price_ranges'])
    identity = row.get('_identity', row.get('contract_identity'))
    if _identity(market) != identity:
        raise LegacyReplayError('The native legacy market differs from its recorded payout identity')
    evidence = (market.get('_crypto15m') or {}).get('settlement_source_evidence')
    if not isinstance(evidence, dict):
        raise LegacyReplayError('The exact legacy comparison requires recorded settlement-source documents')
    try:
        verified_crypto_settlement_source(evidence['series'], market, evidence['event'], asset='BTC')
    except (ValueError, KeyError, TypeError):
        raise LegacyReplayError('The recorded legacy settlement source or precision is unverified') from None
    forecast = native.get('forecast')
    if (native.get('kind') not in ('kalshi_minute_five_observation', 'kalshi_algorithm_state')
            or native.get('status') != 'ready' or not isinstance(forecast, dict)
            or forecast.get('ticker') != row.get('ticker') or forecast.get('contract_identity') != identity
            or forecast.get('model_time') != row.get('model_time')
            or forecast.get('source') != row.get('source')
            or forecast.get('model_version') != row.get('model_version')):
        raise LegacyReplayError('The portable and native legacy forecasts disagree')
    if abs(_number(forecast.get('probability_yes'), 'native probability', maximum=ONE)
            - _number(row.get('probability_yes'), 'portable probability', maximum=ONE)) > Decimal('0.0000000001'):
        raise LegacyReplayError('The portable and native legacy probabilities disagree')
    quote = native.get('market_snapshot')
    if (not isinstance(quote, dict) or _timestamp(quote.get('fetched_at')) != row.get('quote_time')
            or _timestamp(native.get('fetched_at')) > _stamp(row) + 1e-6):
        raise LegacyReplayError('The portable and native legacy receipt clocks disagree')
    result = deepcopy(native)
    proposal = result.get('regime_proposal', row.get('regime_proposal'))
    if not isinstance(proposal, dict):
        raise LegacyReplayError('The exact legacy comparison requires native regime evidence')
    try:
        if RegimeConfig.from_dict(proposal.get('config')) != RegimeConfig(
                transition_action='model', range_rule='between_trends'):
            raise LegacyReplayError('The native regime policy differs from the current legacy profile')
    except (ValueError, TypeError):
        raise LegacyReplayError('The native regime policy differs from the current legacy profile') from None
    result['regime_proposal'] = deepcopy(proposal)
    return result


class _ReadOnly:
    environment, _allow_writes, account_fingerprint = 'production', False, _ACCOUNT

    def __init__(self, simulator):
        self.simulator = simulator

    def get_balance(self):
        return self.simulator.get_balance()

    def get_market(self, ticker):
        return self.simulator.get_market(ticker)


class _Simulation:
    """Minimal in-memory venue interface; intentionally has no transport object."""
    environment, _allow_writes, account_fingerprint = 'production', True, _ACCOUNT

    def __init__(self, records, config):
        self.records, self.config = records, config
        self.now = _stamp(records[0])
        self.cash = _number(config.initial_cash, 'initial cash')
        self.initial = self.cash
        self.positions, self.basis, self.latest, self.settled = {}, {}, {}, {}
        self.market_context, self.quote_watermarks, self.quote_gaps = {}, {}, {}
        self.processed_settlements, self.used = set(), {}
        self.current_index, self.consumed_through = -1, -1
        self.next_event_index = 0
        self.actions, self.equity_curve = [], []
        self.reasons, self.audit = Counter(), Counter()
        self.realized, self.fees, self.turnover = ZERO, ZERO, ZERO
        self.entries = self.exits = self.settlements = 0
        self.peak, self.drawdown, self.drawdown_fraction = self.cash, ZERO, 0.0
        self.journal = None
        self.ordinal = 0
        self.independent_books = any(row.get('kind') in ('kalshi_value_book', 'kalshi_value_book_gap')
            or row.get('book_evidence_id') is not None for row in records)

    def action(self, row):
        if len(self.actions) < int(self.config.max_actions):
            self.actions.append(row)
        else:
            self.audit['omitted_action_rows'] += 1

    def advance(self, stamp):
        stamp = _time(stamp)
        if stamp < self.now:
            raise LegacyReplayError('The legacy replay clock cannot move backwards')
        while self.next_event_index < len(self.records):
            index = self.next_event_index
            row = self.records[index]
            when = _stamp(row)
            if when > stamp:
                break
            self.next_event_index += 1
            if row['kind'] in ('kalshi_value_observation', 'kalshi_value_book', 'kalshi_value_book_gap'):
                ticker, quote_time = row['ticker'], _book_time(row)
                if isinstance(row.get('market'), dict):
                    self.market_context[ticker] = deepcopy(row['market'])
                watermark = self.quote_watermarks.get(ticker, 0)
                if row['kind'] == 'kalshi_value_book_gap':
                    if quote_time >= watermark:
                        self.latest.pop(ticker, None)
                        self.quote_watermarks[ticker] = quote_time
                        self.quote_gaps[ticker] = index
                # A forecast may finish after a newer independent quote while
                # still embedding its older acquisition. Do not roll books back.
                elif (not self.independent_books or quote_time >= watermark
                        and (ticker not in self.quote_gaps or quote_time > watermark)):
                    self.latest[ticker] = index
                    self.quote_watermarks[ticker] = quote_time
                    self.quote_gaps.pop(ticker, None)
            elif row['kind'] == 'kalshi_value_settlement' and index not in self.processed_settlements:
                self.processed_settlements.add(index)
                self._settle(row, when)
        self.now = stamp

    def _settle(self, row, when):
        ticker = row['ticker']
        identity = row.get('_identity', row['contract_identity'])
        market = dict(identity, status='finalized', result=row['result'], is_provisional=False,
            settlement_value_dollars='1' if row['result'] == 'yes' else '0',
            settlement_ts=_iso(row['settled_at']))
        self.settled[ticker] = market
        quantity = self.positions.pop(ticker, 0)
        self.audit['official_outcome_receipts'] += 1
        if not quantity:
            return
        basis = self.basis.pop(ticker, ZERO)
        won = (quantity > 0) == (row['result'] == 'yes')
        payout = Decimal(abs(quantity)) if won else ZERO
        profit = payout - basis
        self.cash += payout
        self.realized += profit
        self.settlements += 1
        self.action(dict(action='settle', ticker=ticker, observed_at=when, time=when,
            count=abs(quantity), outcome='yes' if quantity > 0 else 'no',
            result=row['result'], payout=_text(payout), realized_pnl=_text(profit), fee='0',
            net_position_before=quantity, net_position_after=0))

    def get_exchange_status(self):
        return dict(exchange_active=True, trading_active=True)

    def get_balance(self):
        return dict(balance_dollars=_text(self.cash))

    def get_exchange_balance(self, exchange_index):
        return dict(balance_dollars=_text(self.cash), exchange_index=exchange_index)

    def get_positions(self):
        return [dict(ticker=ticker, position_fp=format(Decimal(quantity), '.2f'))
            for ticker, quantity in self.positions.items() if quantity]

    def get_orders(self, status=None):
        return []

    def get_market(self, ticker):
        if ticker in self.settled:
            return deepcopy(self.settled[ticker])
        index = self.latest.get(ticker)
        if index is None and ticker not in self.market_context:
            raise LegacyReplayError('The current legacy replay market is unavailable')
        result = deepcopy(self.market_context[ticker] if index is None else self.records[index]['market'])
        result['status'] = 'active' if self.now < _timestamp(result['close_time']) else 'closed'
        return result

    def _record(self):
        if self.current_index < 0:
            raise LegacyReplayError('A current replay observation is required')
        return self.records[self.current_index]

    def get_series(self, ticker):
        row = self._record()
        evidence = row['market']['_crypto15m']['settlement_source_evidence']
        return dict(deepcopy(evidence['series']), ticker=ticker, frequency='fifteen_min',
            fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'])

    def get_event(self, ticker):
        evidence = self._record()['market']['_crypto15m']['settlement_source_evidence']
        return dict(deepcopy(evidence['event']), event_ticker=ticker, series_ticker='KXBTC15M')

    def get_orderbook(self, ticker):
        index = self.latest.get(ticker)
        if index is None:
            raise LegacyReplayError('A recorded legacy book is required')
        row = self.records[index]
        return dict(orderbook_fp=dict(yes_dollars=deepcopy(row['yes_bids']), no_dollars=deepcopy(row['no_bids'])))

    def _book(self, index):
        row, fraction = self.records[index], _number(self.config.depth_fraction, 'depth fraction', maximum=ONE)
        output = {}
        for side in ('yes', 'no'):
            levels = []
            for price, quantity in row[side + '_bids']:
                available = (Decimal(quantity) * fraction).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
                available -= self.used.get(self._liquidity_key(index, side, price), ZERO)
                if available > ZERO:
                    levels.append([price, _text(available)])
            output[side] = levels
        return output

    def _liquidity_key(self, index, side, price):
        row = self.records[index]
        token = row.get('book_evidence_id')
        return ((index, side, price) if token is None else
            ('recorded-book', token, row['ticker'], side, Decimal(price)))

    def _consume(self, index, side, count):
        left = Decimal(count)
        for price, quantity in reversed(self._book(index)[side]):
            taken = min(left, Decimal(quantity))
            key = self._liquidity_key(index, side, price)
            self.used[key] = self.used.get(key, ZERO) + taken
            left -= taken
            if not left:
                break
        if left:
            raise LegacyReplayError('The legacy simulated fill exceeds its visible depth')

    def _future(self, wire, submitted):
        arrival = submitted + float(self.config.execution_latency_seconds)
        deadline = submitted + float(self.config.max_execution_wait_seconds)
        current_identity = self._record().get('_identity', self._record()['contract_identity'])
        current_fees = (self._record().get('fee_type'), Decimal(str(self._record().get('fee_multiplier', 1))),
            Decimal(str(self._record().get('fee_quantum', '.0001'))))
        latest_quote = self.quote_watermarks.get(wire['ticker'], 0)
        gap_barrier = wire['ticker'] in self.quote_gaps
        for index in range(self.current_index + 1, len(self.records)):
            row = self.records[index]
            when = _stamp(row)
            if when > deadline:
                break
            if (row['kind'] not in ('kalshi_value_observation', 'kalshi_value_book', 'kalshi_value_book_gap')
                    or row['ticker'] != wire['ticker']):
                continue
            quote_time = _book_time(row)
            if self.independent_books and quote_time < latest_quote:
                continue
            if row['kind'] == 'kalshi_value_book_gap':
                latest_quote, gap_barrier = quote_time, True
                if when >= arrival:
                    return None, when
                continue
            if self.independent_books and gap_barrier and quote_time <= latest_quote:
                continue
            latest_quote = max(latest_quote, quote_time)
            gap_barrier = False
            if (row.get('_identity', row['contract_identity']) != current_identity
                    or self.independent_books and (row.get('fee_type'),
                        Decimal(str(row.get('fee_multiplier', 1))),
                        Decimal(str(row.get('fee_quantum', '.0001')))) != current_fees
                    or when < arrival or quote_time < arrival
                    or when - quote_time > float(self.config.max_quote_age_seconds)
                    or when >= row.get('_closed', _timestamp(current_identity['close_time']))):
                continue
            return index, when
        return None, arrival

    def create_order(self, wire):
        submitted, before = self.now, self.positions.get(wire['ticker'], 0)
        requested = int(Decimal(wire['count']))
        direction = 1 if wire['side'] == 'bid' else -1
        if wire.get('reduce_only'):
            requested = min(requested, abs(before)) if before * direction < 0 else 0
        index, acknowledged = self._future(wire, submitted)
        self.advance(acknowledged)
        count, gross, fee = 0, ZERO, ZERO
        canonical_outcome = 'yes' if direction > 0 else 'no'
        quote = None
        if index is not None and requested:
            row, book = self.records[index], self._book(index)
            fee_config = ValueSizingConfig(fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'],
                fee_quantum=row.get('fee_quantum', '.0001'))
            limit = Decimal(wire['price']) if direction > 0 else ONE - Decimal(wire['price'])
            quote = evaluate_value_fill(action='buy', outcome=canonical_outcome, count=requested,
                yes_bids=book['yes'], no_bids=book['no'], config=fee_config, limit_price=_text(limit))
            count, gross, fee = quote['count'], Decimal(quote['gross_cost']), Decimal(quote['fee'])
            closing = min(count, abs(before)) if before * direction < 0 else 0
            # Opening risk is cash bounded even in the simulator. Gross limits
            # remain those selected by the real legacy planner before latency.
            while count and self.cash + Decimal(closing) - gross - fee < ZERO:
                count -= 1
                if not count:
                    gross, fee = ZERO, ZERO
                    break
                quote = evaluate_value_fill(action='buy', outcome=canonical_outcome, count=count,
                    yes_bids=book['yes'], no_bids=book['no'], config=fee_config, limit_price=_text(limit))
                gross, fee = Decimal(quote['gross_cost']), Decimal(quote['fee'])
                closing = min(count, abs(before)) if before * direction < 0 else 0
            if count:
                close_gross = ZERO
                if closing:
                    closed_quote = evaluate_value_fill(action='buy', outcome=canonical_outcome, count=closing,
                        yes_bids=book['yes'], no_bids=book['no'], config=fee_config, limit_price=_text(limit))
                    close_gross = Decimal(closed_quote['gross_cost'])
                self._apply(wire, before, direction, count, gross, fee, closing, close_gross,
                    submitted, acknowledged, index)
                self._consume(index, 'no' if direction > 0 else 'yes', count)
            self.consumed_through = max(self.consumed_through, index)
        else:
            self.audit['no_post_arrival_book'] += 1
        self.ordinal += 1
        self.audit['submitted_orders'] += 1
        if count < requested:
            self.audit['partial_or_unfilled_orders'] += 1
        return dict(order_id='legacy-replay-' + str(self.ordinal),
            client_order_id=wire['client_order_id'], ticker=wire['ticker'],
            fill_count=format(Decimal(count), '.2f'), remaining_count='0.00')

    def _apply(self, wire, before, direction, count, gross, fee, closing, close_gross,
               submitted, acknowledged, index):
        ticker = wire['ticker']
        opening = count - closing
        close_fee = fee * Decimal(closing) / Decimal(count)
        removed_basis = (self.basis.get(ticker, ZERO) * Decimal(closing) / Decimal(abs(before))
            if closing else ZERO)
        sale_gross = Decimal(closing) - close_gross
        profit = sale_gross - close_fee - removed_basis
        basis = self.basis.get(ticker, ZERO) - removed_basis + gross - close_gross + fee - close_fee
        after = before + direction * count
        self.cash += Decimal(closing) - gross - fee
        self.positions[ticker], self.basis[ticker] = after, basis if after else ZERO
        self.realized += profit
        self.fees += fee
        self.turnover += sale_gross + gross - close_gross
        self.entries += bool(opening)
        self.exits += bool(closing)
        self.audit['filled_actions'] += 1
        self.action(dict(action='flip' if closing and opening else 'sell' if closing else 'buy',
            ticker=ticker, submitted_at=submitted, executed_at=acknowledged,
            time=acknowledged, decision_at=submitted,
            execution_book_at=_book_time(self.records[index]),
            outcome=('yes' if before > 0 else 'no') if closing and not opening else ('yes' if direction > 0 else 'no'),
            count=count, close_quantity=closing, open_quantity=opening, gross=_text(gross),
            fee=_text(fee), cash_delta=_text(Decimal(closing) - gross - fee),
            net_position_before=before, net_position_after=after, realized_pnl=_text(profit),
            limit_price=wire['price'], reduce_only=bool(wire.get('reduce_only')),
            paper_only=True))

    def mark(self):
        marked, unmarked = self.cash, 0
        for ticker, position in self.positions.items():
            if not position:
                continue
            index = self.latest.get(ticker)
            if index is None:
                unmarked += abs(position)
                continue
            row = self.records[index]
            if self.now - _book_time(row) > float(self.config.max_quote_age_seconds) or self.now >= row.get(
                    '_closed', _timestamp(row['contract_identity']['close_time'])):
                unmarked += abs(position)
                continue
            book = self._book(index)
            quote = evaluate_value_fill(action='sell', outcome='yes' if position > 0 else 'no',
                count=abs(position), yes_bids=book['yes'], no_bids=book['no'],
                config=ValueSizingConfig(fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'],
                    fee_quantum=row.get('fee_quantum', '.0001')))
            marked += Decimal(quote['cash_delta'])
            unmarked += abs(position) - quote['count']
        value = marked if not unmarked else None
        self.equity_curve.append(dict(observed_at=self.now, time=self.now, cash=_text(self.cash),
            marked_equity=None if value is None else _text(value),
            conservative_equity=_text(marked), unmarked_contracts=unmarked))
        if value is not None:
            self.peak = max(self.peak, value)
            self.drawdown = max(self.drawdown, self.peak - value)
            if self.peak:
                self.drawdown_fraction = max(self.drawdown_fraction, float((self.peak - value) / self.peak))
        return value


def replay_legacy(records, *, config):
    """Run the complete current BTC legacy profile on validated portable records.

    The caller normally uses ``validate_replay_records`` first. Missing native
    strategy/tick-grid evidence returns an unavailable comparison, never a
    fabricated all-YES or uniform-random benchmark.
    """
    initial = _number(config.initial_cash, 'initial cash')
    if not isinstance(records, (list, tuple)) or not records:
        return _empty(initial, 'The exact legacy comparison requires recorded native observations')
    if len(records) > int(config.max_events):
        raise LegacyReplayError('Legacy replay exceeds its event limit')
    previous, snapshots = None, {}
    try:
        for index, row in enumerate(records):
            when = _stamp(row)
            if previous is not None and when < previous:
                raise LegacyReplayError('Legacy replay records are not receipt ordered')
            previous = when
            if row['kind'] == 'kalshi_value_observation':
                if row.get('asset', 'BTC') != 'BTC':
                    raise LegacyReplayError('The exact current legacy comparison supports BTC only')
                snapshots[index] = _native(row)
    except (LegacyReplayError, ValueError, TypeError, KeyError) as error:
        return _empty(initial, str(error) if isinstance(error, LegacyReplayError) else
            'The exact legacy comparison requires complete compatible native evidence')
    if not snapshots:
        return _empty(initial, 'The exact legacy comparison has no native model observations')
    simulation = _Simulation(records, config)
    try:
        profile = UniformSizing(budget=_text(_number(config.uniform_market_budget, 'uniform budget')))
        schedule = EntrySchedule(0, int(config.uniform_cadence_seconds))
        maximum = max(profile.budgets(schedule))
    except (ValueError, TypeError):
        return _empty(initial, 'The recorded comparison cannot initialize the configured legacy uniform schedule')
    risk = RiskConfig(sizing_mode='fixed', max_total_cost=profile.budget,
        max_order_cost=maximum, max_market_cost=profile.budget,
        max_contracts=min(10000, max(1, int(maximum / Decimal('.03')))),
        min_seconds_to_close=1, max_signal_age_seconds=5, max_quote_age_seconds=5,
        fee_reserve_per_contract='.03', required_series='KXBTC15M')
    regime = RegimeConfig(transition_action='model', range_rule='between_trends')
    with tempfile.TemporaryDirectory(prefix='kalshi-value-legacy-replay-') as directory:
        with Journal(Path(directory) / 'legacy.sqlite3', mode='live', environment='production', risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal = journal
            executor = MinuteFiveLiveExecutor(simulation, journal, risk, assets=('BTC',),
                clock=lambda: simulation.now, bet_budget=_text(maximum), uniform_sizing=profile,
                entry_schedule='every-minute', warmup_seconds=0, cadence_seconds=schedule.cadence_seconds,
                allowance_mode='continuous', continue_unresolved=True,
                skip_probability_at_or_above='.90', exit_bid_above='.95', exit_seconds_before_close=900,
                resume_after_exit=True, stop_bid_at_or_below='.05', stop_seconds_before_close=180,
                opposite_bid_at_or_below='.45', opposite_after_seconds=450,
                late_model_exit_probability_above='.90', late_model_exit_seconds_before_close=180,
                regime_config=regime)
            reader = _ReadOnly(simulation)
            simulation.mark()
            for index, row in enumerate(records):
                when = _stamp(row)
                if when < simulation.now or index <= simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution'] += 1
                    continue
                simulation.advance(when)
                simulation.current_index = index
                with journal.exclusive():
                    refresh_settled_allowances(reader, journal, clock=lambda: simulation.now, max_markets=100)
                if row['kind'] != 'kalshi_value_observation':
                    simulation.mark()
                    continue
                if simulation.audit['submitted_orders'] >= int(config.max_actions):
                    simulation.audit['action_limit_observations_skipped'] += 1
                    simulation.mark()
                    continue
                native = snapshots[index]
                simulation.audit['decision_observations'] += 1
                before_orders = simulation.audit['submitted_orders']
                before_actions = len(simulation.actions)
                price = executor.execute_exit('BTC')
                execution = price
                if (price is None or price.get('status') == 'skipped' and not price.get('client_order_id')):
                    model = executor.execute_exit('BTC', model_snapshot=native)
                    execution = model if model is not None else price
                    if execution is None:
                        execution = executor.execute(native)
                if execution is not None:
                    simulation.reasons[execution.get('reason', 'Unknown decision')] += 1
                    if (simulation.audit['submitted_orders'] > before_orders
                            and len(simulation.actions) > before_actions
                            and simulation.actions[-1].get('action') != 'settle'):
                        simulation.actions[-1].setdefault('decision_reason', execution.get('reason'))
                simulation.mark()
            simulation.audit['journal_intents'] = len(journal.intents())
            simulation.audit['active_reserved_cost'] = _text(journal.active_committed())
            simulation.audit['lifetime_reserved_cost'] = _text(journal.committed())
    positions = [dict(ticker=ticker, net_position=position, outcome='yes' if position > 0 else 'no',
        quantity=abs(position), cost_basis=_text(simulation.basis.get(ticker, ZERO)))
        for ticker, position in sorted(simulation.positions.items()) if position]
    marked = simulation.mark()
    return dict(status='ready', reason='The native current minute-ladder executor was replayed offline',
        initial_cash=_text(initial), cash=_text(simulation.cash), realized_pnl=_text(simulation.realized),
        fees_paid=_text(simulation.fees), gross_turnover=_text(simulation.turnover),
        entry_fills=simulation.entries, exit_fills=simulation.exits, settlement_events=simulation.settlements,
        filled_actions=simulation.audit['filled_actions'],
        all_positions_settled=not positions, omitted_action_rows=simulation.audit['omitted_action_rows'],
        open_positions=positions, final_equity=None if positions else _text(simulation.cash),
        marked_equity=None if marked is None else _text(marked), max_drawdown=_text(simulation.drawdown),
        max_drawdown_fraction=simulation.drawdown_fraction, actions=simulation.actions,
        equity_curve=simulation.equity_curve, decision_reasons=dict(simulation.reasons),
        assumptions=[
            'Uses the actual MinuteFiveLiveExecutor and a temporary isolated SQLite journal; no real account was accessed.',
            'Native median/regime/contract/tick-grid evidence is required; missing evidence makes this comparison unavailable.',
            'Every recorded observation is a poll; the legacy executor independently enforces its recurring entry windows.',
            'Orders use their decision-time fixed limit and only a later received same-contract book after configured latency.',
            'Only the configured fraction of displayed depth can fill, and a consumed snapshot level cannot be reused.',
            'Orders without a qualifying post-arrival book are simulated as unfilled; this is not evidence of a real IOC outcome.',
            'Fees use the shared value fill model; known partial fills and netted opposite holdings affect actual simulated cash.',
            'Settlement cash and working-allowance releases occur only after recorded official outcome receipt.',
            'Open holdings require current executable marking; final equity is unavailable until all holdings close or settle.',
        ], audit=dict(simulation.audit, legacy_executor='MinuteFiveLiveExecutor', execution_eligible=False,
            uniform_market_budget=_text(profile.budget), entry_cadence_seconds=schedule.cadence_seconds))
