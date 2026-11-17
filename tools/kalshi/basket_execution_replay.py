"""Causal simulated IOC baskets and excess-leg repair from recorded books.

This module owns no client, account, or persistent state. Quotes become fills
only under the explicit depth/latency simulation. No end-of-file payout or
unrealized gain is manufactured when settlement evidence is absent.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
import math

from .basket_scan import BasketScanError, _validated, scan_baskets
from .value_sizing import (ValueSizingConfig, evaluate_value_fill,
                           worst_value_buy_unit_cost)


ZERO = Decimal(0)
_MAX_FRAMES = 200000
_MAX_EVENTS = 20000


class BasketExecutionReplayError(ValueError):
    """Fixed public replay validation or cash-conservation error."""


def _number(value, label, *, maximum=Decimal('1000000')):
    try:
        if isinstance(value, bool) or len(str(value)) > 80:
            raise ValueError()
        result = Decimal(str(value))
        if (not result.is_finite() or not 0 <= result <= maximum
                or not -18 <= result.as_tuple().exponent <= 18):
            raise ValueError()
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise BasketExecutionReplayError('Invalid replay ' + label) from None


def _time(value):
    return float(_number(value, 'timestamp', maximum=Decimal('253402300799')))


def _text(value):
    return format(value, 'f')


def _hash(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise BasketExecutionReplayError('Replay frames require bounded finite JSON') from None
    if len(raw) > 2000000:
        raise BasketExecutionReplayError('Replay frame exceeds its size bound')
    return hashlib.sha256(raw).hexdigest()


def _config(contract):
    return ValueSizingConfig(fee_type=contract['fee_type'],
        fee_multiplier=contract['fee_multiplier'], fee_quantum=Decimal('.01'))


def _book(contract, fraction):
    result = {}
    for side in ('yes_bids', 'no_bids'):
        levels = []
        for price, quantity in contract[side]:
            usable = (Decimal(quantity) * fraction).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
            if usable > ZERO:
                levels.append([price, _text(usable)])
        result[side] = levels
    return result


def _identity(contract):
    return tuple(contract[field] for field in ('contract_identity', 'fee_type', 'fee_multiplier',
                                               'exchange_index', 'event_ticker'))


def _experiment(contract):
    return tuple(contract[field] for field in ('underlying', 'source', 'settlement_statistic',
        'window_start', 'window_end', 'payout', 'comparator'))


def _settlements(rows, registry):
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > 1000:
        raise BasketExecutionReplayError('Supply a bounded list of finalized settlement evidence')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise BasketExecutionReplayError('Each settlement evidence row must be an object')
        ticker, outcome, evidence = row.get('market'), row.get('outcome'), row.get('market_evidence')
        if (not isinstance(ticker, str) or ticker not in registry or ticker in seen
                or outcome not in ('yes', 'no') or row.get('provenance') != 'venue_finalized'
                or not isinstance(evidence, dict)):
            raise BasketExecutionReplayError('Settlement requires unique known markets and finalized venue provenance')
        contract = registry[ticker]
        at = _time(row.get('settled_at'))
        try:
            close = datetime.fromisoformat(evidence['close_time'].replace('Z', '+00:00'))
            if close.tzinfo is None:
                raise ValueError()
            closed = close.timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise BasketExecutionReplayError('Settlement requires its exact market closing time') from None
        if (evidence.get('ticker') != ticker or evidence.get('market_type') != 'binary'
                or evidence.get('status') != 'finalized' or evidence.get('result') != outcome
                or evidence.get('is_provisional') is not False
                or evidence.get('settlement_bounds_type', 'default') != 'default'
                or _number(evidence.get('notional_value_dollars'), 'settlement payout') != 1
                or evidence.get('strike_type') != 'greater'
                or _number(evidence.get('floor_strike'), 'settlement strike') != Decimal(contract['strike'])
                or closed != contract['window_end'] or at < closed):
            raise BasketExecutionReplayError('Settlement evidence disagrees with the held binary contract')
        seen.add(ticker)
        result.append(dict(market=ticker, outcome=outcome, time=at))
    # Results of one scalar experiment cannot violate threshold monotonicity.
    for first in result:
        for second in result:
            a, b = registry[first['market']], registry[second['market']]
            if (_experiment(a) == _experiment(b) and Decimal(a['strike']) < Decimal(b['strike'])
                    and first['outcome'] == 'no' and second['outcome'] == 'yes'):
                raise BasketExecutionReplayError('Finalized threshold outcomes are mutually inconsistent')
    return sorted(result, key=lambda row: (row['time'], row['market']))


def replay_baskets(frames, *, bankroll='100', max_contracts=20,
                   max_unhedged_loss='25', latency_seconds=.25, depth_fraction='.5',
                   settlements=None):
    """Replay independent IOC legs on later observed books, without account I/O.

    One basket per settlement experiment limits repeated liquidity assumptions.
    Remaining IOC quantity is simulated canceled after its first eligible book.
    Excess inventory alone is repaired, using a strictly later quote. Matched
    holdings stay open unless explicit non-provisional final results are given.
    """
    initial = _number(bankroll, 'bankroll')
    cap = _number(max_unhedged_loss, 'unhedged loss cap')
    fraction = _number(depth_fraction, 'depth fraction', maximum=Decimal(1))
    if (initial <= ZERO or fraction <= ZERO or type(max_contracts) is not int
            or not 1 <= max_contracts <= 100
            or type(latency_seconds) not in (int, float)
            or not math.isfinite(latency_seconds) or not 0 <= latency_seconds <= 30):
        raise BasketExecutionReplayError('Replay allocation, depth, quantity or latency is invalid')
    cash, realized, fees = initial, ZERO, ZERO
    events, inventory, registry, used_experiments = [], {}, {}, set()
    pending, repair = None, None
    previous, previous_hash, synthetic, last_at = None, None, None, None
    quotes, consumed = {}, set()
    totals = dict(entry_premium=ZERO, entry_fees=ZERO, repair_gross=ZERO,
                  repair_fees=ZERO, repair_basis=ZERO, settlement_payoff=ZERO,
                  quoted_surplus=ZERO, limit_price_improvement=ZERO)
    frame_count = duplicate_frames = blocked_frames = 0

    def event(kind, at, **fields):
        if len(events) >= _MAX_EVENTS:
            raise BasketExecutionReplayError('Replay exceeds its event bound')
        events.append(dict(event_type=kind, observed_at=at, simulation=True, **fields))

    def reservation():
        return sum((leg['reservation'] for leg in pending['legs'] if not leg['attempted']), ZERO) if pending else ZERO

    def close_inventory(ticker, quantity, gross, exit_fee):
        nonlocal realized
        lot = inventory[ticker]
        ratio = Decimal(quantity) / lot['quantity']
        basis = (lot['premium'] + lot['fees']) * ratio
        lot['premium'] *= 1-ratio
        lot['fees'] *= 1-ratio
        lot['quantity'] -= quantity
        realized += gross-exit_fee-basis
        if not lot['quantity']:
            del inventory[ticker]
        return basis

    for raw in frames:
        frame_count += 1
        if frame_count > _MAX_FRAMES:
            raise BasketExecutionReplayError('Replay exceeds its frame bound')
        if not isinstance(raw, dict):
            raise BasketExecutionReplayError('Each replay frame must be an object')
        at, fingerprint = _time(raw.get('observed_at')), _hash(raw)
        if previous is not None and at < previous:
            raise BasketExecutionReplayError('Replay observations must be chronological')
        if at == previous:
            if fingerprint != previous_hash:
                raise BasketExecutionReplayError('Conflicting frames share one observation timestamp')
            duplicate_frames += 1
            continue
        try:
            contracts = _validated(raw, at)
            # The general payoff normalizer deliberately omits venue routing.
            # An execution intent must bind both payoff and exact venue scope.
            raw_contracts = {contract['ticker']: contract for contract in raw['contracts']}
            for contract in contracts:
                source = raw_contracts[contract['ticker']]
                missing_scope = '<synthetic-unscoped>' if raw['synthetic'] else None
                contract.update(exchange_index=source.get('exchange_index', missing_scope),
                                event_ticker=source.get('event_ticker', missing_scope))
            report = scan_baskets(raw, now=at, bankroll=_text(max(cash, Decimal('.000001'))),
                                  max_contracts=max_contracts, max_unhedged_loss=_text(cap))
        except (BasketScanError, ValueError, TypeError, ArithmeticError):
            raise BasketExecutionReplayError('A replay frame has invalid or incompatible quote evidence') from None
        if synthetic is not None and synthetic != raw['synthetic']:
            raise BasketExecutionReplayError('Synthetic and public frames cannot share an execution replay')
        synthetic = raw['synthetic']
        current = {contract['ticker']: contract for contract in contracts}
        identity_conflict = False
        for ticker, contract in current.items():
            if ticker in registry and _identity(registry[ticker]) != _identity(contract):
                # A changed leg is never silently adopted for execution.
                identity_conflict = True
                continue
            registry.setdefault(ticker, deepcopy(contract))
            key = (ticker, contract['quote_time'])
            book_hash = _hash([contract['yes_bids'], contract['no_bids']])
            if key in quotes and quotes[key] != book_hash:
                raise BasketExecutionReplayError('Conflicting books share one quote receipt timestamp')
            quotes[key] = book_hash
        if len(registry) > 1000 or len(quotes) > 100000:
            raise BasketExecutionReplayError('Replay exceeds its contract or quote identity bound')
        if identity_conflict:
            blocked_frames += 1
            previous, previous_hash, last_at = at, fingerprint, at
            continue
        action_taken = False
        if repair is not None:
            ticker = repair['market']
            contract = current.get(ticker)
            if (contract is None or _identity(contract) != repair['identity']
                    or at <= repair['created_at'] or contract['quote_time'] <= repair['after_quote']):
                blocked_frames += 1
            else:
                key = (ticker, contract['quote_time'], 'sell')
                if key not in consumed:
                    consumed.add(key)
                    quote = evaluate_value_fill(action='sell', outcome=repair['outcome'],
                        count=repair['quantity'], config=_config(contract), **_book(contract, fraction))
                    count = quote['count']
                    if count:
                        gross, fee = Decimal(quote['gross']), Decimal(quote['fee'])
                        # A reduce-only repair cannot spend cash that does not exist.
                        if cash + gross-fee >= ZERO:
                            basis = close_inventory(ticker, count, gross, fee)
                            cash += gross-fee
                            fees += fee
                            totals['repair_gross'] += gross
                            totals['repair_fees'] += fee
                            totals['repair_basis'] += basis
                            repair['quantity'] -= count
                            event('repair_fill', at, market=ticker, outcome=repair['outcome'],
                                  quantity=str(count), gross_proceeds=_text(gross), fees=_text(fee),
                                  closing_basis=_text(basis), realized_pnl=_text(gross-fee-basis))
                            action_taken = True
                    repair['after_quote'] = contract['quote_time']
                    if repair['quantity'] == 0:
                        repair = None
        elif pending is not None:
            for leg in pending['legs']:
                if leg['attempted']:
                    continue
                ticker, contract = leg['market'], current.get(leg['market'])
                if (contract is None or _identity(contract) != leg['identity']
                        or at <= pending['created_at']
                        or contract['quote_time'] <= leg['after_quote']
                        or contract['quote_time'] < pending['created_at']+latency_seconds):
                    blocked_frames += 1
                    continue
                key = (ticker, contract['quote_time'], 'buy')
                if key in consumed:
                    continue
                consumed.add(key)
                quote = evaluate_value_fill(action='buy', outcome=leg['outcome'], count=leg['quantity'],
                    limit_price=leg['limit_price'], config=_config(contract), **_book(contract, fraction))
                count = quote['count']
                gross, fee = Decimal(quote['gross']), Decimal(quote['fee'])
                cost = gross+fee
                if cost > leg['reservation'] or cash-cost < ZERO:
                    raise BasketExecutionReplayError('A simulated fill exceeds its reserved cash')
                leg.update(attempted=True, filled=count, execution_quote=contract['quote_time'])
                if count:
                    cash -= cost
                    fees += fee
                    totals['entry_premium'] += gross
                    totals['entry_fees'] += fee
                    totals['limit_price_improvement'] += count*Decimal(leg['limit_price'])-gross
                    inventory[ticker] = dict(quantity=Decimal(count), outcome=leg['outcome'],
                                             premium=gross, fees=fee, identity=leg['identity'])
                    event('entry_fill', at, market=ticker, outcome=leg['outcome'], quantity=str(count),
                          premium=_text(gross), fees=_text(fee), original_limit=leg['limit_price'])
                event('ioc_terminal', at, market=ticker, filled=str(count),
                      simulated_canceled_quantity=str(leg['quantity']-count))
                action_taken = True
            if all(leg['attempted'] for leg in pending['legs']):
                first, second = pending['legs']
                excess = first if first['filled'] > second['filled'] else second
                difference = abs(first['filled']-second['filled'])
                if difference:
                    repair = dict(market=excess['market'], outcome=excess['outcome'], quantity=difference,
                        identity=excess['identity'], created_at=at,
                        after_quote=max(leg['execution_quote'] for leg in pending['legs']))
                    event('repair_required', at, market=excess['market'], quantity=str(difference))
                pending = None
        if pending is None and repair is None and not action_taken:
            experiment = _experiment(contracts[0])
            if experiment not in used_experiments:
                for candidate in report['candidates']:
                    if not candidate['passed']:
                        continue
                    legs = []
                    for leg in candidate['legs']:
                        contract = current[leg['market']]
                        quantity = int(leg['quantity'])
                        reserved = quantity*worst_value_buy_unit_cost(leg['limit_price'], _config(contract))
                        legs.append(dict(market=leg['market'], outcome=leg['outcome'], quantity=quantity,
                            limit_price=leg['limit_price'], identity=_identity(contract),
                            reservation=reserved, after_quote=contract['quote_time'], attempted=False, filled=0))
                    if sum((leg['reservation'] for leg in legs), ZERO) > cash:
                        continue
                    pending = dict(created_at=at, legs=legs)
                    used_experiments.add(experiment)
                    totals['quoted_surplus'] += Decimal(candidate['surplus'])
                    event('basket_intent', at, quantity=candidate['quantity'],
                        quote_surplus=candidate['surplus'], reservation=_text(reservation()),
                        markets=[leg['market'] for leg in legs])
                    break
        if cash < ZERO or reservation() > cash:
            raise BasketExecutionReplayError('Replay cash or reservation conservation failed')
        previous, previous_hash, last_at = at, fingerprint, at
    if not frame_count:
        raise BasketExecutionReplayError('Replay requires at least one quote frame')
    for settlement in _settlements(settlements, registry):
        ticker, outcome, at = settlement['market'], settlement['outcome'], settlement['time']
        if ticker not in inventory:
            continue
        held = inventory[ticker]
        quantity = held['quantity']
        payout = quantity if held['outcome'] == outcome else ZERO
        basis = close_inventory(ticker, quantity, payout, ZERO)
        cash += payout
        totals['settlement_payoff'] += payout
        event('settlement', at, market=ticker, result=outcome, quantity=_text(quantity),
              payout=_text(payout), closing_basis=_text(basis), realized_pnl=_text(payout-basis),
              evidence_basis='caller_supplied_finalized_market_evidence')
        if repair is not None and repair['market'] == ticker:
            repair = None
        last_at = max(last_at, at)
    open_basis = sum((lot['premium']+lot['fees'] for lot in inventory.values()), ZERO)
    if abs(cash+open_basis-initial-realized) > Decimal('.000000000001'):
        raise BasketExecutionReplayError('Replay P&L attribution failed cash conservation')
    pending_expired = bool(pending and any(last_at >= registry[leg['market']]['window_end']
                                          for leg in pending['legs'] if not leg['attempted']))
    events.sort(key=lambda row: row['observed_at'])
    return dict(kind='kalshi_basket_execution_replay', version=1, execution_eligible=False,
        simulation=True, actual_account_accessed=False, synthetic=synthetic, as_of=last_at,
        status='repair_required' if repair else 'pending_quotes' if pending else 'ready',
        inputs=dict(bankroll=_text(initial), max_contracts=max_contracts,
            max_unhedged_loss=_text(cap), latency_seconds=latency_seconds, depth_fraction=_text(fraction)),
        summary=dict(cash=_text(cash), initial_cash=_text(initial), open_entry_basis=_text(open_basis),
            realized_pnl=_text(realized), fees=_text(fees), reserved_cash=_text(reservation()),
            available_cash=_text(cash-reservation()), pending_baskets=int(pending is not None),
            pending_expired=pending_expired,
            repair_required=repair is not None, frames=frame_count, duplicate_frames=duplicate_frames,
            blocked_frame_actions=blocked_frames),
        positions=[dict(market=ticker, outcome=lot['outcome'], quantity=_text(lot['quantity']),
                        premium=_text(lot['premium']), fees=_text(lot['fees']))
                   for ticker, lot in sorted(inventory.items())],
        pending=None if pending is None else dict(created_at=pending['created_at'],
            legs=[dict(market=leg['market'], outcome=leg['outcome'], quantity=leg['quantity'],
                attempted=leg['attempted'], filled=leg['filled'], reservation=_text(leg['reservation']))
                for leg in pending['legs']]),
        repair=None if repair is None else dict(market=repair['market'], outcome=repair['outcome'],
            quantity=repair['quantity'], created_at=repair['created_at']),
        attribution={key: _text(value) for key, value in totals.items()}, events=events,
        basis_convention='pro_rata_aggregate_entry_leg_basis',
        limitations=['All fills and IOC cancellations are simulations from later visible books, not venue acknowledgements.',
            'Depth fractions and latency are assumptions; queue position, hidden liquidity and market impact are not modeled.',
            'Fees use conservative micro-fill bounds and cent rounding, not actual account fee receipts.',
            'Repair basis is allocated pro rata from the aggregated simulated entry leg; it is not account FIFO accounting.',
            'One basket per settlement experiment is admitted; this replay is not a live account admission guard.',
            'Quoted floor surplus is prospective. Open positions have no fabricated mark or end-of-file settlement.',
            'Pending orders without eligible later quote evidence retain their reservation, including at expiry.',
            'Settlement facts, when supplied, are validated caller evidence and are not fetched by this replay.',
            'Settlement evidence is applied after quote replay; its payouts are not reinvested in earlier replay decisions.'])
