"""Read manual paper fills without reconciling or mutating an order journal."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
import time

from .engine import Journal
from .positions import _number, _stamp

_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_ZERO = Decimal(0)


def paper_journal_ledger(path, *, mode, environment, risk, client_factory, now):
    """Return estimated-fee paper actions and only proven final settlements.

    Live journals are never inspected here. Reservations are fee assumptions,
    not account fees, and no cancellation/reconciliation method is available.
    """
    if mode != 'paper':
        return None
    events, lots, unknown = [], {}, 0
    if Path(path).exists():
        with Journal(path, mode='paper', environment=environment, risk=risk) as journal:
            # A read transaction keeps intent rows and their metadata coherent.
            journal.connection.execute('BEGIN')
            try:
                intents = journal.intents()
            finally:
                journal.connection.rollback()
        for row in intents:
            ticker = row.get('ticker')
            if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
                continue
            if row.get('status') in ('unknown', 'sending', 'cancel_pending'):
                unknown += 1
                continue
            execution = row.get('execution') or {}
            confirmed = row.get('status') == 'paper_filled' or (
                row.get('status') in ('paper_resting', 'canceled') and execution.get('order_type') == 'limit')
            if not confirmed:
                continue
            try:
                quantity, count = _number(row['fill_count']), _number(row['count'])
                if quantity == 0:
                    continue
                price, reservation = _number(row['price']), _number(row['reserved_cost'])
                outcome = row['outcome']
                stamp = _stamp(row['created_at'])
                if (not 0 < quantity <= count or not 0 < price < 1 or reservation < count * price
                        or outcome not in ('yes', 'no') or stamp > now):
                    raise ValueError()
                fees = (reservation - count * price) * quantity / count
                identity = str(row['client_order_id'])
                event = dict(id='paper-fill-' + identity, time=stamp, ticker=ticker,
                    action='buy', outcome=outcome, quantity=str(quantity), count=str(quantity),
                    price=str(price), fees=str(fees), realized_pnl='0', closing_quantity='0',
                    closing_basis='0', status='paper_filled')
                events.append(event)
                lots.setdefault(ticker, []).append((identity, outcome, quantity, quantity * price + fees, stamp))
            except (KeyError, ValueError, TypeError, ArithmeticError):
                unknown += 1
    checked = failed = 0
    if lots:
        client = client_factory()
        try:
            if (getattr(client, '_allow_writes', None) is not False
                    or getattr(client, 'environment', None) != environment):
                raise ValueError('Paper performance needs a read-only data client')
            deadline = time.monotonic() + 8
            for ticker, entries in sorted(lots.items()):
                if checked >= 40 or time.monotonic() >= deadline:
                    failed += 1
                    continue
                checked += 1
                try:
                    market = client.get_market(ticker)
                    if (not isinstance(market, dict) or market.get('ticker') != ticker
                            or market.get('market_type') != 'binary'
                            or market.get('settlement_bounds_type', 'default') != 'default'
                            or _number(market.get('notional_value_dollars', '1')) != 1):
                        raise ValueError()
                    if market.get('status') != 'finalized':
                        if _stamp(market['close_time']) <= now:
                            failed += 1
                        continue
                    result = market.get('result')
                    closed = _stamp(market['close_time'])
                    settled = _stamp(market.get('settlement_ts'))
                    if (result not in ('yes', 'no') or market.get('is_provisional', False) is not False
                            or not closed <= settled <= now
                            or any(entry[4] >= closed for entry in entries)):
                        raise ValueError()
                    explicit_payout = market.get('settlement_value_dollars')
                    if explicit_payout is not None and _number(explicit_payout) != Decimal(result == 'yes'):
                        raise ValueError()
                    for identity, outcome, quantity, basis, _ in entries:
                        payout = Decimal(1) if outcome == result else _ZERO
                        events.append(dict(id='paper-settle-' + identity, time=settled, ticker=ticker,
                            action='settle', outcome=outcome, quantity=str(quantity), count=str(quantity),
                            price=str(payout), fees='0', realized_pnl=str(quantity * payout - basis),
                            closing_quantity=str(quantity), closing_basis=str(basis), status='paper_filled'))
                except Exception:
                    failed += 1
        finally:
            client.close()
    complete = not unknown and not failed
    reason = ('Manual paper journal; entry fees are prorated configured allowances, not actual account fees. '
              'Entry timestamps are journal intent times.')
    if unknown:
        reason += f' {unknown} unresolved or invalid intent(s) were excluded.'
    if failed:
        reason += f' Final settlement could not be established for {failed} market(s).'
    return dict(events=events, initial_cash=None, environment=environment,
        accounting_method='paper_journal_estimated_fees', fetched_at=now,
        coverage=dict(complete=complete, history_complete=complete, truncated=False,
                      omitted_count=unknown, scope='supported_15m', reason=reason))
