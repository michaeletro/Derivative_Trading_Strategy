"""Independent Python replay of the bounded, direct IBKR displayed-depth format.

Not an individual-order feed, queue/fill simulator, or full-exchange-book model.
Only the adapter's local sequence determines order. Prices retain source double
precision through price_repr; sizes remain decimal strings until diagnostics.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

TERMINAL = {'stop', 'error', 'gap', 'interrupted'}
MAX_EVENTS = 200_000


def digest(payload: dict) -> str:
    data = {k: v for k, v in payload.items() if k != 'sha256'}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def size_number(value: str) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 64 or any(c not in '0123456789.eE+-' for c in value):
        raise ValueError('Invalid displayed size')
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('Invalid displayed size') from error
    if not result.is_finite() or not 0 <= result < Decimal('1e30'):
        raise ValueError('Invalid displayed size')
    return result


class Book:
    def __init__(self, rows: int):
        if type(rows) is not int or not 1 <= rows <= 10:
            raise ValueError('Rows must be 1..10')
        self.rows = rows
        self.asks, self.bids = [], []
        self.sequence = self.epoch = 0
        self.active = self.valid = self.started = self.terminal = False
        self.reason = 'not_started'

    def clear(self):
        self.asks.clear()
        self.bids.clear()

    def invalidate(self, reason):
        self.clear()
        self.valid = False
        self.reason = reason

    def apply(self, event: dict):
        seq = int(event['sequence'])
        contiguous = seq == self.sequence + 1
        if seq <= self.sequence:
            self.invalidate('nonincreasing_local_sequence')
            return
        self.sequence = seq
        kind = event['kind']
        if kind == 'start' and (seq != 1 or self.started):
            self.invalidate('unexpected_start')
            return
        if kind == 'reset' and (not self.started or self.terminal):
            self.invalidate('unexpected_reset')
            return
        if kind in ('start', 'reset'):
            self.started = True
            self.clear()
            self.valid, self.active = contiguous, True
            self.epoch += 1
            self.reason = 'building' if contiguous else 'local_sequence_gap'
            return
        if kind in TERMINAL:
            self.clear()
            self.active = self.valid = False
            self.terminal = True
            self.reason = kind
            return
        if not contiguous:
            self.invalidate('local_sequence_gap')
            return
        if not self.active or not self.valid:
            return
        op, side, pos = event['operation'], event['side'], event['position']
        maker = event.get('market_maker', '')
        if (kind != 'update' or event.get('smart_depth', False) or
                type(op) is not int or op not in (0, 1, 2) or
                type(side) is not int or side not in (0, 1) or
                type(pos) is not int or not 0 <= pos < self.rows or
                not isinstance(maker, str) or len(maker.encode()) > 64 or '\0' in maker):
            self.invalidate('invalid_depth_update')
            return
        levels = self.asks if side == 0 else self.bids
        if pos > len(levels) or (op != 0 and pos == len(levels)):
            self.invalidate('missing_row_position')
            return
        if op == 2:
            levels.pop(pos)
            return
        try:
            size_number(event['size'])
        except (ValueError, KeyError):
            self.invalidate('invalid_depth_size')
            return
        try:
            price = float(event['price_repr'] if 'price_repr' in event else event['price'])
        except (ValueError, TypeError):
            price = float('nan')
        if not math.isfinite(price) or not 0 < price < 1e12:
            self.invalidate('invalid_depth_price')
            return
        row = {'price': price, 'size': event['size'], 'market_maker': maker}
        if op == 0:
            levels.insert(pos, row)
            del levels[self.rows:]
        else:
            levels[pos] = row

    def quality(self) -> str:
        if not self.active or not self.valid:
            return self.reason
        if not self.asks or not self.bids:
            return 'one_sided_or_building'
        if any(a['price'] > b['price'] for a, b in zip(self.asks, self.asks[1:])) or any(a['price'] < b['price'] for a, b in zip(self.bids, self.bids[1:])):
            return 'unordered_rows'
        if any(size_number(r['size']) == 0 for r in self.asks + self.bids):
            return 'zero_size_row'
        bid, ask = self.bids[0]['price'], self.asks[0]['price']
        return 'crossed' if bid > ask else 'locked' if bid == ask else 'two_sided_unverified'

    def state(self) -> dict:
        return dict(sequence=str(self.sequence), epoch=str(self.epoch), active=self.active,
                    structural_valid=self.valid, quality=self.quality(),
                    bids=copy.deepcopy(self.bids), asks=copy.deepcopy(self.asks))

    def metrics(self) -> dict:
        result = dict(spread=None, midpoint=None, bid_depth=None, ask_depth=None,
                      depth_imbalance=None, top_imbalance=None)
        if self.quality() != 'two_sided_unverified':
            return result
        bid, ask = self.bids[0]['price'], self.asks[0]['price']
        bd = sum(size_number(r['size']) for r in self.bids)
        ad = sum(size_number(r['size']) for r in self.asks)
        # Aggregate rows at the same best price: rows can be market-maker quotes.
        b0 = sum(size_number(r['size']) for r in self.bids if r['price'] == bid)
        a0 = sum(size_number(r['size']) for r in self.asks if r['price'] == ask)
        result.update(spread=ask-bid, midpoint=(ask+bid)/2,
                      bid_depth=float(bd), ask_depth=float(ad),
                      depth_imbalance=float((bd-ad)/(bd+ad)), top_imbalance=float((b0-a0)/(b0+a0)))
        return result

    def displayed_cost(self, signed_quantity: float) -> float | None:
        """Hypothetical book-crossing cost against midpoint, NOT a fill promise.

        Returns None beyond recorded quantity or when the displayed book is not
        usable. Excludes latency, odd lots, hidden liquidity, impact and fees.
        """
        if not math.isfinite(signed_quantity) or signed_quantity == 0:
            raise ValueError('Choose a finite nonzero size')
        if self.quality() != 'two_sided_unverified':
            return None
        remaining = Decimal(str(abs(signed_quantity)))
        notional = Decimal(0)
        levels = self.asks if signed_quantity > 0 else self.bids
        for row in levels:
            amount = min(remaining, size_number(row['size']))
            notional += amount * Decimal(str(row['price']))
            remaining -= amount
            if remaining == 0:
                break
        if remaining:
            return None
        midpoint = Decimal(str((self.asks[0]['price']+self.bids[0]['price'])/2))
        reference = Decimal(str(abs(signed_quantity))) * midpoint
        return float(notional-reference if signed_quantity > 0 else reference-notional)


def validate_export(payload: dict) -> dict:
    if (not isinstance(payload, dict) or payload.get('schema_version') != 1 or
            payload.get('kind') != 'displayed_depth_export' or
            payload.get('complete_exchange_book') is not False):
        raise ValueError('Not a compatible displayed-depth export')
    if payload.get('sha256') != digest(payload):
        raise ValueError('Export integrity mismatch')
    session, events = payload['session'], payload['events']
    if session.get('source') not in ('ibkr_tws', 'mock') or session.get('smart_depth') != 0 or session.get('state') not in TERMINAL:
        raise ValueError('Unknown or aggregated depth source')
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError('Export too large')
    if len(events) != int(session['event_count']):
        raise ValueError('Incomplete export: event count does not match stopped session')
    previous = 0
    for e in events:
        i = int(e['event_id'])
        if i <= previous:
            raise ValueError('Nonincreasing archive event ID')
        previous = i
    if events and int(events[-1]['sequence']) != int(session['last_sequence']):
        raise ValueError('Export does not reach the recorded terminal sequence')
    return payload


def load_export(path: str | Path) -> dict:
    path = Path(path).expanduser()
    if path.stat().st_size > 160_000_000:
        raise ValueError('Research input exceeds 160 MB bound')
    return validate_export(json.loads(path.read_text(encoding='utf-8')))


def replay(payload: dict):
    """Yield a copy after every archived event; never fill across invalid states."""
    validate_export(payload)
    book = Book(payload['session']['requested_rows'])
    for event in payload['events']:
        book.apply(event)
        yield {**book.state(), **book.metrics(), 'event_id': event['event_id'],
               'received_unix_us': event['received_unix_us'],
               'received_monotonic_ns': event['received_monotonic_ns'],
               'kind': event['kind'], 'origin': event['origin']}


def synthetic_fixture() -> dict:
    """A small deterministic protocol exercise; not market or forecasting data."""
    events = []
    def emit(kind='update', **values):
        seq = len(events)+1
        e = dict(event_id=str(seq), sequence=str(seq), kind=kind, origin='synthetic_fixture',
                 received_unix_us=str(1_700_000_000_000_000+seq*100_000),
                 received_monotonic_ns=str(1_000_000_000+seq*100_000_000),
                 operation=-1, side=-1, position=-1, price=None, price_repr='not_applicable',
                 size='', market_maker='', smart_depth=0, code=0)
        e.update(values)
        if e['price'] is not None:
            e['price_repr'] = format(e['price'], '.17g')
        events.append(e)
    def initialize():
        for side in (0, 1):
            for pos in range(5):
                emit(side=side, position=pos, operation=0,
                     price=100+(1 if side == 0 else -1)*(.01+.01*pos),
                     size=str(100+pos*20))
    emit('start'); initialize()
    for i in range(80):
        if i == 40:
            emit('reset', code=317); initialize()
        emit(side=i%2, position=i%5, operation=1,
             price=100+(1 if i%2 == 0 else -1)*(.01+.01*(i%5)),
             size=str(90+(i*13)%180))
    emit('stop')
    data = dict(schema_version=1, kind='displayed_depth_export', complete_exchange_book=False,
                time_basis='local_callback_receipt', exchange_timestamp=None,
                session=dict(session_id='1', run_id='1', source='mock', symbol='SYNTHETIC',
                    contract_id='9001', contract_route='TESTEX', venue='TESTEX', requested_rows=5,
                    smart_depth=0, state='stop', last_sequence=str(len(events)), event_count=str(len(events))),
                through_id=str(len(events)), events=events)
    data['sha256'] = digest(data)
    return data
