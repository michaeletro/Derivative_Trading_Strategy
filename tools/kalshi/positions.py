"""Read-only position marks, with paper estimates separated from account facts.

Liquidation values consume visible bid depth and exclude future exit fees.
Journal limit prices and fee reservations are estimates, not reconciled fills.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
import time

from .engine import _levels
from .crypto_assets import view_asset_from_ticker


ZERO = Decimal('0')
ONE = Decimal('1')
MAX_MARKETS = 20
MARK_BUDGET_SECONDS = 8.0
_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_FIXED = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?\Z')


def _account_ticker(ticker):
    """Account monitoring accepts explicit view assets, without execution rights."""
    try:
        view_asset_from_ticker(ticker)
        return True
    except ValueError:
        return False


class PositionError(ValueError):
    """Fixed public validation errors that never echo an account response."""


def _number(value, *, signed=False, maximum=Decimal('1000000000')):
    if not isinstance(value, str) or not _FIXED.fullmatch(value):
        raise PositionError('Position amount is unavailable or invalid')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PositionError('Position amount is unavailable or invalid') from None
    if not result.is_finite() or abs(result) > maximum or not signed and result < ZERO:
        raise PositionError('Position amount is outside supported bounds')
    return result


def _money(value):
    return None if value is None else format(value.quantize(Decimal('.000000000001')), 'f').rstrip('0').rstrip('.') or '0'


def _iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _stamp(value):
    if not isinstance(value, str):
        raise PositionError('Position market timestamp is unavailable')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise PositionError('Position market timestamp is invalid') from None


def _optional(row, field, *, signed=False):
    return None if row.get(field) is None else _number(row[field], signed=signed)


def _balance_field(row, dollars, cents):
    if row.get(dollars) is not None:
        return _number(row[dollars])
    value = row.get(cents)
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 100000000000:
        raise PositionError('Account balance is unavailable or invalid')
    return Decimal(value) / 100


def _sum_complete(values):
    values = list(values)
    return None if any(value is None for value in values) else sum(values, ZERO)


def _empty_row(ticker, outcome, quantity):
    return dict(ticker=ticker, outcome=outcome, quantity=_money(quantity), cost_basis=None,
                average_entry_price=None, mark_price=None, marked_value=None,
                unrealized_pnl=None, realized_pnl=None, fees_paid=None, status='unknown',
                mark_basis='unavailable', warning=None)


def _journal_positions(intents, wanted_status):
    """Aggregate confirmed quantities; estimated entry cost prorates partial fills."""
    if not isinstance(intents, list) or len(intents) > 100000:
        raise PositionError('Position journal exceeds the supported bound')
    result = {}
    uncertain = 0
    for intent in intents:
        if not isinstance(intent, dict):
            raise PositionError('Position journal is invalid')
        ticker = intent.get('ticker')
        if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
            continue
        if intent.get('status') in ('unknown', 'sending'):
            uncertain += 1
        execution = intent.get('execution')
        confirmed_limit = (wanted_status == 'filled' and intent.get('status') in ('resting', 'cancel_pending', 'canceled')
                           and isinstance(execution, dict) and execution.get('order_type') == 'limit')
        if intent.get('status') != wanted_status and not confirmed_limit:
            continue
        outcome = intent.get('outcome')
        if outcome not in ('yes', 'no'):
            raise PositionError('Position journal outcome is invalid')
        count = _number(intent.get('count'))
        filled = _number(intent.get('fill_count'))
        price = _number(intent.get('price'), maximum=ONE)
        reserved = _number(intent.get('reserved_cost'))
        if count <= ZERO or filled > count or price <= ZERO or price >= ONE or reserved < count * price:
            raise PositionError('Position journal fill or reservation is inconsistent')
        if filled == ZERO:
            continue
        fees = (reserved - count * price) * filled / count
        key = ticker, outcome
        aggregate = result.setdefault(key, dict(quantity=ZERO, entry=ZERO, fee_allowance=ZERO))
        aggregate['quantity'] += filled
        aggregate['entry'] += filled * price
        aggregate['fee_allowance'] += fees
    return result, uncertain


def _account_groups(positions):
    """Preserve exchange partitions and aggregate gross quantities by outcome.

    GET positions can return one ticker on multiple exchange shards. Opposing
    holdings on different shards must not be silently netted. Legacy records
    without an exchange identity remain usable only for an unambiguous ticker.
    """
    if not isinstance(positions, list) or len(positions) > 100000:
        raise PositionError('Account position response is incomplete or exceeds supported bounds')
    seen, partitions, grouped, realized, fees = set(), {}, {}, [], []
    for position in positions:
        if not isinstance(position, dict):
            raise PositionError('Account position response is invalid')
        ticker = position.get('ticker')
        if not _account_ticker(ticker):
            continue
        for field in ('subaccount', 'subaccount_number'):
            if position.get(field) is not None and (type(position[field]) is not int or position[field] != 0):
                raise PositionError('Account position response differs from primary-account scope')
        exchange = position.get('exchange_index')
        if 'exchange_index' in position and (type(exchange) is not int or not 0 <= exchange <= 1000):
            raise PositionError('Account position exchange partition is invalid')
        key = ticker, exchange
        if key in seen:
            raise PositionError('Duplicate account position requires reconciliation')
        seen.add(key)
        partition = partitions.setdefault(ticker, set())
        partition.add(exchange)
        if None in partition and len(partition) > 1:
            raise PositionError('Mixed unidentified account partitions require reconciliation')
        quantity = _number(position.get('position_fp'), signed=True)
        pnl = _optional(position, 'realized_pnl_dollars', signed=True)
        fee = _optional(position, 'fees_paid_dollars')
        realized.append(pnl)
        fees.append(fee)
        if quantity == ZERO:
            continue
        outcome = 'yes' if quantity > ZERO else 'no'
        group = grouped.setdefault((ticker, outcome), dict(quantity=ZERO, realized=[], fees=[], exchanges=[]))
        group['quantity'] += abs(quantity)
        group['realized'].append(pnl)
        group['fees'].append(fee)
        if exchange is not None:
            group['exchanges'].append(exchange)
    return grouped, partitions, realized, fees


def _position_read(cache, ticker, name, reader):
    """Reuse one read only within this position-report acquisition."""
    entry = cache.setdefault(ticker, {})
    if entry.get(name + '_failed'):
        raise PositionError('Position market data is unavailable; no current mark')
    if name not in entry:
        try:
            entry[name] = reader()
        except Exception:
            entry[name + '_failed'] = True
            raise
    return entry[name]


def _mark(client, ticker, outcome, quantity, now, deadline, clock, started, cache=None):
    """Return a full-depth gross mark, or a fixed message explaining its absence."""
    if clock() >= deadline:
        return dict(status='unknown', value=None, price=None, basis='unavailable',
                    warning='Position marking time budget reached; refresh to try again')
    try:
        market = (client.get_market(ticker) if cache is None else
                  _position_read(cache, ticker, 'market', lambda: client.get_market(ticker)))
        checked_at = clock()
        if checked_at > deadline:
            raise PositionError('Position marking time budget exceeded')
        if (not isinstance(market, dict) or market.get('ticker') != ticker
                or market.get('market_type') != 'binary'
                or market.get('settlement_bounds_type', 'default') != 'default'):
            raise PositionError('Position market identity or payout type is unavailable')
        payout = _number(market.get('notional_value_dollars', '1'), maximum=ONE)
        if payout != ONE:
            raise PositionError('Only one-dollar position payouts are supported')
        status = market.get('status')
        if not isinstance(status, str) or not status or len(status) > 64 or not re.fullmatch(r'[a-z_]+', status):
            raise PositionError('Position market status is invalid')
        close_time = _stamp(market.get('close_time'))
        estimated_wall_time = now + max(0, checked_at - started)
        if (status == 'finalized' and market.get('is_provisional', False) is False
                and market.get('result') in ('yes', 'no')):
            if close_time > estimated_wall_time:
                raise PositionError('Finalized position market has not reached its closing time')
            price = ONE if market['result'] == outcome else ZERO
            return dict(status=status, value=quantity * price, price=price,
                        basis='settlement', warning=None)
        if status != 'active' or close_time <= estimated_wall_time:
            return dict(status=status, value=None, price=None, basis='unavailable',
                        warning='Market is closed or awaiting final settlement; no live liquidation mark')
        book = (client.get_orderbook(ticker) if cache is None else
                _position_read(cache, ticker, 'book', lambda: client.get_orderbook(ticker)))
        if not isinstance(book, dict) or not isinstance(book.get('orderbook_fp'), dict):
            raise PositionError('Position order book is unavailable')
        yes = _levels(book['orderbook_fp'].get('yes_dollars'))
        no = _levels(book['orderbook_fp'].get('no_dollars'))
        if yes and no and yes[-1][0] + no[-1][0] >= ONE:
            raise PositionError('Position order book is locked or crossed')
        bids = yes if outcome == 'yes' else no
        remaining, value = quantity, ZERO
        for price, available in reversed(bids):
            filled = min(remaining, available)
            value += filled * price
            remaining -= filled
            if remaining == ZERO:
                break
        if remaining:
            return dict(status=status, value=None, price=None, basis='unavailable',
                        warning='Visible bids cannot liquidate the full quantity; no complete mark')
        completed_at = clock()
        if completed_at > deadline:
            raise PositionError('Position marking time budget exceeded')
        if close_time <= now + max(0, completed_at - started):
            raise PositionError('Position market closed during quote acquisition; live mark withheld')
        return dict(status=status, value=value, price=value / quantity, basis='executable_bid_depth_gross',
                    warning='Gross liquidation estimate excludes exit fees and quote changes')
    except PositionError as error:
        message = str(error)
    except Exception:
        message = 'Position market data is unavailable; no current mark'
    return dict(status='unknown', value=None, price=None, basis='unavailable', warning=message)


def build_position_report(client, intents, *, mode, environment, source, now, clock=time.monotonic):
    """Read supported 15-minute exposure without changing account or journal state."""
    report = dict(kind='kalshi_position_report', version=1, mode=mode, environment=environment,
                  source=source, status='unavailable', fetched_at=None, reason='Position data is unavailable',
                  summary=dict(cash_balance=None, portfolio_value=None, open_count=0,
                               realized_pnl=None, fees_paid=None, unrealized_pnl=None, unknown_intent_count=0),
                  positions=[])
    try:
        if (mode not in ('paper', 'demo', 'live') or environment not in ('demo', 'production')
                or source not in ('paper_journal', 'kalshi_account') or isinstance(now, bool)
                or not isinstance(now, (int, float)) or not math.isfinite(now) or not 0 < now <= 253402300799):
            raise PositionError('Position request mode, source or timestamp is invalid')
        if client is None:
            raise PositionError('Account position access is not enabled for this console')
        if getattr(client, 'environment', environment) != environment:
            raise PositionError('Position client environment differs from the console')
        started = clock()
        deadline = started + MARK_BUDGET_SECONDS
        rows, raw_realized, raw_fees = [], [], []
        holdings_only = False
        journal, uncertain = _journal_positions(intents, 'paper_filled' if source == 'paper_journal' else 'filled')
        report['summary']['unknown_intent_count'] = uncertain
        if source == 'paper_journal':
            for (ticker, outcome), holding in journal.items():
                row = _empty_row(ticker, outcome, holding['quantity'])
                basis = holding['entry'] + holding['fee_allowance']
                row.update(cost_basis=_money(basis), average_entry_price=_money(holding['entry'] / holding['quantity']),
                           fees_paid=_money(holding['fee_allowance']))
                rows.append((row, basis, holding['quantity']))
            reason = ('Paper journal only. Entry costs and fees use simulated limit prices and conservative fee allowances; '
                      'liquidation values exclude exit fees. This is not account performance.')
        else:
            primary_balance = getattr(client, 'get_primary_account_balance', None)
            scoped = callable(primary_balance)
            balance = primary_balance() if scoped else client.get_balance()
            report['account_scope'] = 'primary_subaccount_0' if scoped else 'unverified'
            report['account_scope_verified'] = scoped
            report['exchange_scope'] = 'all'
            report['venue_updated_at'] = balance.get('updated_ts') if isinstance(balance, dict) else None
            if clock() > deadline:
                raise PositionError('Account balance acquisition exceeded the freshness budget')
            if not isinstance(balance, dict):
                raise PositionError('Account balance response is invalid')
            report['summary']['cash_balance'] = _money(_balance_field(balance, 'balance_dollars', 'balance'))
            report['summary']['portfolio_value'] = _money(_balance_field(balance, 'portfolio_value_dollars', 'portfolio_value'))
            # The current-holdings capability is deliberately explicit. Older
            # readers retain their all-settlement-state contract; a failed or
            # incomplete current read must never fall back to a different scope.
            current_reader = getattr(type(client), 'get_current_account_positions', None)
            holdings_only = callable(current_reader)
            positions = (client.get_current_account_positions() if holdings_only
                         else client.get_account_positions())
            report.update(holdings_scope='current_nonzero_unsettled' if holdings_only else 'recent_all_settlement_states',
                          accounting_scope='separate_performance_history' if holdings_only else 'recent_position_rows')
            if clock() > deadline:
                raise PositionError('Account position acquisition exceeded the freshness budget')
            groups, partitions, raw_realized, raw_fees = _account_groups(positions)
            for (ticker, outcome), group in groups.items():
                quantity = group['quantity']
                row = _empty_row(ticker, outcome, quantity)
                row.update(realized_pnl=_money(_sum_complete(group['realized'])),
                           fees_paid=_money(_sum_complete(group['fees'])),
                           exchange_indexes=sorted(group['exchanges']),
                           shard_scope_verified=None not in partitions[ticker])
                own = journal.get((ticker, outcome))
                opposite = journal.get((ticker, 'no' if outcome == 'yes' else 'yes'))
                basis = None
                if len(partitions[ticker]) == 1 and own and opposite is None and own['quantity'] == quantity:
                    basis = own['entry'] + own['fee_allowance']
                    row.update(cost_basis=_money(basis), average_entry_price=_money(own['entry'] / quantity))
                rows.append((row, basis, quantity))
            reason = (('Current non-zero unsettled supported 15-minute account holdings (BTC, ETH, SOL, XRP and Gold). '
                       'Closed contracts awaiting settlement remain included. '
                       'Realized P&L and fee totals are unavailable in this holdings-only query; '
                       'broader settled and exited accounting belongs to Performance. ')
                      if holdings_only else
                      ('Recent supported 15-minute account positions (BTC, ETH, SOL, XRP and Gold), '
                       'not lifetime or all-account performance. '
                       'Realized P&L and fees are reported separately by Kalshi. '))
            reason += ('Entry basis is an estimate only when journal fills reconcile; '
                       'other basis and unrealized P&L are unavailable.')

        if len(rows) > 1000:
            raise PositionError('Position display exceeds the supported thousand-row bound')
        marked_tickers, mark_cache = set(), {}
        open_pnl, paper_realized, paper_fees = [], [], []
        partial = bool(uncertain)
        for row, basis, quantity in rows:
            if row['ticker'] in marked_tickers or len(marked_tickers) < MAX_MARKETS:
                mark = _mark(client, row['ticker'], row['outcome'], quantity, now, deadline, clock, started,
                             cache=mark_cache)
                marked_tickers.add(row['ticker'])
            else:
                mark = dict(status='unknown', value=None, price=None, basis='unavailable',
                            warning='Only twenty position marks are fetched per refresh')
            if (source == 'kalshi_account' and len(partitions[row['ticker']]) > 1
                    and mark['basis'] == 'executable_bid_depth_gross'):
                # A ticker book is not proof that holdings on different venue
                # shards can be liquidated there. Retain its depth benchmark
                # separately, without promoting it to an executable mark.
                row.update(book_benchmark_value=_money(mark['value']),
                           book_benchmark_price=_money(mark['price']))
                mark = dict(mark, value=None, price=None, basis='unavailable',
                    warning='Ticker-book depth benchmark only; liquidation across reported exchange shards is unverified')
            row.update(status=mark['status'], mark_price=_money(mark['price']),
                       marked_value=_money(mark['value']), mark_basis=mark['basis'], warning=mark['warning'])
            if source == 'kalshi_account':
                basis_note = ('Journal entry basis uses limit prices plus a fee allowance; actual fills may differ.'
                              if basis is not None else 'Entry cost basis is not reconciled; unrealized P&L is unavailable.')
                row['warning'] = ' '.join(filter(None, (row['warning'], basis_note)))
            if mark['basis'] == 'settlement':
                if source == 'paper_journal':
                    row['realized_pnl'] = _money(mark['value'] - basis)
                    paper_realized.append(mark['value'] - basis)
            else:
                report['summary']['open_count'] += 1
                value = mark['value'] - basis if mark['value'] is not None and basis is not None else None
                row['unrealized_pnl'] = _money(value)
                open_pnl.append(value)
                if mark['value'] is None or basis is None:
                    partial = True
            if source == 'paper_journal':
                paper_fees.append(_number(row['fees_paid']))
            report['positions'].append(row)
        if source == 'paper_journal':
            report['summary']['realized_pnl'] = _money(sum(paper_realized, ZERO))
            report['summary']['fees_paid'] = _money(sum(paper_fees, ZERO))
        else:
            report['summary']['realized_pnl'] = None if holdings_only else _money(_sum_complete(raw_realized))
            report['summary']['fees_paid'] = None if holdings_only else _money(_sum_complete(raw_fees))
        report['summary']['unrealized_pnl'] = _money(_sum_complete(open_pnl))
        if uncertain:
            reason += f' {uncertain} unresolved order intent(s) are not counted as confirmed holdings.'
        elapsed = clock() - started
        if elapsed < 0 or elapsed > MARK_BUDGET_SECONDS:
            partial = True
            for row in report['positions']:
                if row['mark_basis'] != 'settlement':
                    row.update(mark_price=None, marked_value=None, unrealized_pnl=None, mark_basis='unavailable',
                               warning='Position acquisition exceeded the freshness budget; live marks withheld')
            if report['summary']['open_count']:
                report['summary']['unrealized_pnl'] = None
            reason += ' Position acquisition exceeded the freshness budget; live marks are withheld.'
        report.update(status='partial' if partial else 'ready', fetched_at=_iso(now + max(0, elapsed)), reason=reason)
    except PositionError as error:
        report['reason'] = str(error)
        report['positions'] = []
        report['summary'] = dict(cash_balance=None, portfolio_value=None, open_count=0,
                                 realized_pnl=None, fees_paid=None, unrealized_pnl=None, unknown_intent_count=0)
    except Exception:
        report['reason'] = 'Account or journal position data is unavailable'
        report['positions'] = []
        report['summary'] = dict(cash_balance=None, portfolio_value=None, open_count=0,
                                 realized_pnl=None, fees_paid=None, unrealized_pnl=None, unknown_intent_count=0)
    return report
