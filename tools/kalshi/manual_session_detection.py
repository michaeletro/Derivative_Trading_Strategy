"""Detect external fills in one active contract without adopting their inventory.

The account history is read from the contract open, independently of current
net holdings. A buy and sell that cancel each other still count as intervention.
Exchange mutations and release of existing strategy reservations are absent.
"""
from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from types import MappingProxyType
import time

from .engine import EngineError, _identifier, _timestamp, decimal_value
from .journal_revision import journal_read_revision
from .manual_reductions import (_clock, _external_order, _history, _market,
                                is_manual_reduction)
from .manual_session_pause import get_manual_session_pause, record_manual_session_pause


class _OrderEvidence:
    """Let the shared attribution validator inspect an already fetched order."""

    def __init__(self, value):
        self.value = value

    def get_order(self, order_id, ticker=None):
        return self.value


def _bot_fill(fill, row):
    if row['ticker'] != fill['ticker'] or row.get('outcome') not in ('yes', 'no'):
        raise EngineError('The authenticated fill conflicts with its strategy order identity')
    if fill['sign'] != (1 if row['outcome'] == 'yes' else -1):
        raise EngineError('The authenticated fill conflicts with its strategy order direction')
    if _timestamp(fill['created_at']) + 1e-6 < _timestamp(row['created_at']):
        raise EngineError('The authenticated strategy fill precedes its order intent')
    requested = decimal_value(row.get('count'), 'strategy order quantity', places=2,
                              maximum=Decimal('100000000'))
    if Decimal(fill['quantity']) > requested:
        raise EngineError('The authenticated strategy fill exceeds its order quantity')
    if row.get('status') not in ('sending', 'unknown', 'filled', 'canceled'):
        raise EngineError('The authenticated strategy fill requires order reconciliation')


def _ownership_maps(journal):
    """Cache immutable journal attribution, never fresh venue-fill results."""
    before = journal_read_revision(journal)
    key = None if before is None else (before, journal.mode, journal.environment,
                                       journal._meta('account_fingerprint'))
    saved = getattr(journal, '_manual_session_ownership', None)
    if (key is not None and isinstance(saved, tuple) and saved[0] == key
            and journal_read_revision(journal) == before):
        return saved
    journal._manual_session_ownership = None
    by_client, by_order = {}, {}
    names = ('client_order_id', 'order_id', 'ticker', 'outcome', 'count', 'status', 'created_at')
    for full in journal.intents():
        if is_manual_reduction(full):
            continue
        row = MappingProxyType({name: full.get(name) for name in names})
        client_id = _identifier(row['client_order_id'])
        if client_id in by_client:
            raise EngineError('Duplicate strategy client order identities require reconciliation')
        by_client[client_id] = row
        if row.get('order_id'):
            order_id = _identifier(row['order_id'])
            if order_id in by_order:
                raise EngineError('Duplicate strategy exchange order identities require reconciliation')
            by_order[order_id] = row
    after = journal_read_revision(journal)
    result = (key, MappingProxyType(by_client), MappingProxyType(by_order), OrderedDict())
    if key is not None:
        if before != after:
            raise EngineError('Journal ownership changed during acquisition; retry with fresh evidence')
        journal._manual_session_ownership = result
    return result


def check_manual_session(client, journal, market, *, clock=time.time, max_pages=5):
    """Return a durable contract pause after any proven non-journal order fill.

    The caller owns the journal lock. Human orders and another program's orders
    are both external: account APIs cannot establish which interface placed one.
    A complete bounded primary-account history and authoritative attribution
    are required; unavailable or contradictory evidence raises EngineError.
    """
    if (getattr(client, 'environment', None) != 'production'
            or journal.mode != 'live' or journal.environment != 'production'
            or type(max_pages) is not int or not 1 <= max_pages <= 20):
        raise EngineError('Manual session detection requires a bounded production account context')
    journal.bind_account(getattr(client, 'account_fingerprint', None))
    if not isinstance(market, dict):
        raise EngineError('The manual session contract is unavailable')
    ticker = _identifier(market.get('ticker'))
    identity, index, opened, closed = _market(market, ticker, _clock(clock()))
    previous = get_manual_session_pause(journal, ticker)
    if previous is not None:
        if previous['contract_identity'] != identity or previous['exchange_index'] != index:
            raise EngineError('The paused session differs from the current contract terms')
        return previous

    ownership = _ownership_maps(journal)
    by_client, by_order = ownership[1:3]
    fills = _history(client, ticker, opened, index, clock, max_pages)
    if ownership[0] is not None and journal_read_revision(journal) != ownership[0][0]:
        journal.bind_account(getattr(client, 'account_fingerprint', None))
        ownership = _ownership_maps(journal)
        by_client, by_order = ownership[1:3]
    if _clock(clock()) >= closed:
        raise EngineError('The contract closed during manual session detection')
    attributed, reads = {}, 0
    totals = {}
    for fill in fills:
        if not opened <= _timestamp(fill['created_at']) < closed:
            raise EngineError('The authenticated fill is outside the current contract session')
        order_id = fill['order_id']
        own = by_order.get(order_id)
        if own is None:
            if order_id not in attributed:
                cache_key = (ticker, index, order_id)
                value = ownership[3].get(cache_key)
                if value is None:
                    if reads >= 50:
                        raise EngineError('Manual session order attribution exceeds its bounded read limit')
                    value = client.get_order(order_id, ticker=ticker)
                    reads += 1
                else:
                    value = dict(value)
                # Validate the complete order identity/scope before classifying
                # its client ID. An in-flight bot order may have no saved
                # exchange ID yet; its authoritative client ID still owns it.
                _external_order(_OrderEvidence(value), order_id, ticker, index, set())
                if ownership[0] is not None:
                    names = ('ticker', 'market_ticker', 'order_id', 'client_order_id',
                             'subaccount', 'subaccount_number', 'exchange_index')
                    ownership[3][cache_key] = tuple((name, value[name]) for name in names if name in value)
                    ownership[3].move_to_end(cache_key)
                    while len(ownership[3]) > 256:
                        ownership[3].popitem(last=False)
                client_id = value['client_order_id']
                own = by_client.get(client_id)
                if own is not None and own.get('order_id') not in (None, '', order_id):
                    raise EngineError('The strategy client and exchange order identities disagree')
                attributed[order_id] = own
            own = attributed[order_id]
        if own is not None:
            _bot_fill(fill, own)
            totals[order_id] = totals.get(order_id, Decimal(0)) + Decimal(fill['quantity'])
            if totals[order_id] > Decimal(own['count']):
                raise EngineError('Authenticated strategy fills exceed their requested quantity')
            continue
        observed = _clock(clock())
        if observed >= closed:
            raise EngineError('The contract closed before its manual session pause was recorded')
        return record_manual_session_pause(journal, identity, [fill], observed)
    return None
