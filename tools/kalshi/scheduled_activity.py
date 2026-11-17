"""Read-only attribution of scheduled orders to authoritative account fills.

Order limits and conservative reservations are diagnostic evidence, never fill
prices or paid fees. Missing pages, unexplained external fills and missing
journal fills make affected accounting unknown. This module never reconciles
an order, adopts external inventory or releases a reservation.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from .engine import EngineError, ONE, ZERO, _iso, _timestamp, decimal_value
from .execution_assets import execution_asset_from_ticker
from .manual_reductions import _fill as normalize_fill
from .performance import PerformanceError, _settlement as normalize_settlement, _settlement_payout


_SETTLEMENT_FEE_POLICY = dict(venue='KalshiEX direct-member event contracts',
    venue_settlement_fee='0', source='https://kalshi.com/docs/kalshi-fee-schedule.pdf',
    effective_date='2026-07-07', verified_date='2026-11-15',
    assumption='Published venue fee policy; third-party and FCM charges are outside scope')

_TERMINAL_ORDER_STATUSES = frozenset(('filled', 'canceled', 'rejected'))


class ScheduledActivityError(EngineError):
    """A fixed, non-sensitive activity acquisition or accounting failure."""


def _text(value):
    with localcontext() as context:
        context.prec = 50
        return format(value.normalize(), 'f') if value else '0'


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise ScheduledActivityError('The activity observation clock is invalid')
    return float(value)


class ScheduledJournalSnapshot:
    """Immutable journal evidence that can be passed to a read-only worker."""
    def __init__(self, rows, metadata):
        self._rows, self._metadata = deepcopy(rows), dict(metadata)
        self.mode, self.environment = metadata.get('mode'), metadata.get('environment')

    def intents(self):
        return deepcopy(self._rows)

    def _meta(self, key):
        return self._metadata.get(key)


def capture_scheduled_journal(journal):
    """Capture SQLite-owned evidence on its caller thread, without mutations."""
    if getattr(journal, 'mode', None) != 'live' or getattr(journal, 'environment', None) != 'production':
        raise ScheduledActivityError('Activity auditing requires a production live journal')
    connection = getattr(journal, 'connection', None)
    owns_read_transaction = connection is not None and not connection.in_transaction
    try:
        if owns_read_transaction:
            connection.execute('BEGIN')
        rows = journal.intents()
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ScheduledActivityError('The scheduled journal history is unavailable or too large')
        metadata = {key: journal._meta(key) for key in (
            'kind', 'mode', 'environment', 'account_fingerprint', 'risk', 'minute_five_execution_policy')}
    except sqlite3.Error:
        raise ScheduledActivityError('A coherent read-only activity snapshot is unavailable') from None
    finally:
        if owns_read_transaction:
            connection.rollback()
    metadata.update(mode='live', environment='production')
    return ScheduledJournalSnapshot(rows, metadata)


def load_scheduled_journal(path):
    """Open an existing journal through SQLite's read-only URI mode."""
    selected = Path(path).expanduser().resolve()
    if not selected.is_file():
        raise ScheduledActivityError('The existing activity journal is unavailable')
    try:
        with closing(sqlite3.connect(selected.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            metadata = {row['key']: row['value'] for row in connection.execute('SELECT key,value FROM kalshi_meta')}
            if (metadata.get('kind') != 'kalshi_bot_journal_v1' or metadata.get('mode') != 'live'
                    or metadata.get('environment') != 'production'):
                raise ScheduledActivityError('The existing journal is not a production live journal')
            rows = [dict(row) for row in connection.execute('SELECT * FROM intents ORDER BY rowid LIMIT 100001')]
            if len(rows) > 100000:
                raise ScheduledActivityError('The activity journal exceeds the audit input bound')
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intent_sizing'").fetchone()
            if table is not None:
                sizing = {row[0]: json.loads(row[1]) for row in connection.execute(
                    'SELECT client_order_id,sizing_json FROM intent_sizing')}
                for row in rows:
                    if row['client_order_id'] in sizing:
                        row['sizing'] = sizing[row['client_order_id']]
            connection.rollback()
        return ScheduledJournalSnapshot(rows, metadata)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        raise ScheduledActivityError('The read-only activity journal is unavailable or invalid') from None


def _asset(ticker):
    try:
        result = execution_asset_from_ticker(ticker).symbol
        return result if result in ('BTC', 'ETH', 'SOL', 'XRP') else None
    except (ValueError, TypeError):
        return None


def _exit_reason(row):
    sizing = row.get('sizing', {})
    if isinstance(sizing, dict) and isinstance(sizing.get('exit_reason'), str):
        reason = sizing['exit_reason']
        if reason in ('late_stop', 'late_model_confidence', 'range_target', 'kelly_rebalance', 'model_overvaluation'):
            return reason
    signal = row.get('signal_id', '')
    if isinstance(signal, str) and signal.startswith('minute-exit-live-'):
        for marker, reason in (('stop-', 'late_stop'), ('model-', 'late_model_confidence'),
                               ('range-', 'range_target'), ('manual-', 'external_manual')):
            if signal.startswith('minute-exit-live-' + marker):
                return reason
        return 'profit_bid_threshold'
    return 'scheduled_entry'


def _page(reader, method, max_pages, *, key='fills'):
    rows, cursor, seen = [], None, set()
    for index in range(max_pages):
        try:
            payload = getattr(reader, method)(cursor=cursor, limit=1000, subaccount=0)
            if (not isinstance(payload, dict) or not isinstance(payload.get(key), list)
                    or len(payload[key]) > 1000 or not isinstance(payload.get('cursor'), str)
                    or len(payload['cursor']) > 4096):
                raise ValueError()
            if any(not isinstance(row, dict) for row in payload[key]):
                raise ValueError()
            next_cursor = payload['cursor']
            if next_cursor and (not payload[key] or next_cursor in seen
                    or any(ord(char) < 32 or ord(char) == 127 for char in next_cursor)):
                raise ValueError()
            rows.extend(payload[key])
            if not next_cursor:
                return rows, dict(status='ready', complete=True, pages=index+1, cursor_present=False)
            seen.add(next_cursor)
            cursor = next_cursor
        except Exception:
            return rows, dict(status='partial', complete=False, pages=index+1,
                cursor_present=bool(cursor), reason='Authenticated fill history was unavailable or invalid')
    return rows, dict(status='partial', complete=False, pages=max_pages, cursor_present=True,
        reason='The authenticated fill page bound was reached')


class ScheduledActivityAudit:
    def __init__(self, reader, journal, *, clock=time.time, max_pages=5):
        if (getattr(reader, 'environment', None) != 'production'
                or getattr(reader, '_allow_writes', None) is not False):
            raise ScheduledActivityError('Activity auditing requires a read-only production client')
        if not callable(clock) or type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ScheduledActivityError('Activity auditing requires one through twenty pages per history tier')
        self.reader, self.journal, self.clock, self.max_pages = reader, journal, clock, max_pages

    def refresh(self):
        snapshot = capture_scheduled_journal(self.journal)
        now = _clock(self.clock())
        saved_account = snapshot._meta('account_fingerprint')
        reader_account = getattr(self.reader, 'account_fingerprint', None)
        if saved_account is not None and saved_account != reader_account:
            raise ScheduledActivityError('The activity reader differs from the journal account')
        rows = [row for row in snapshot.intents() if _asset(row.get('ticker')) is not None
            and not str(row.get('signal_id', '')).startswith('minute-exit-live-manual-')]
        if rows and saved_account is None:
            raise ScheduledActivityError('A nonempty activity journal requires its bound account identity')
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode()).hexdigest()
        orders, clients, tickers, issues, bad_tickers = {}, {}, set(), [], set()
        hard_reasons, fifo_ambiguous = defaultdict(set), set()
        invalid_by_ticker, conflicts_by_ticker = defaultdict(int), defaultdict(set)

        def hard_failure(ticker, reason):
            bad_tickers.add(ticker)
            hard_reasons[ticker].add(reason)

        quantities_reconciled = True
        for row in rows:
            tickers.add(row['ticker'])
            client = row.get('client_order_id')
            try:
                requested = decimal_value(row.get('count'), 'journal order quantity', places=2,
                    minimum=Decimal('.01'), maximum=Decimal('100000000'))
                decimal_value(row.get('fill_count', '0'), 'journal fill quantity', places=2,
                    maximum=requested)
                limit = decimal_value(row.get('price'), 'journal order price', places=12, maximum=ONE)
                if (not ZERO < limit < ONE or not isinstance(client, str) or not client
                        or row.get('outcome') not in ('yes', 'no')
                        or _timestamp(row.get('created_at')) > now + 1):
                    raise ValueError()
            except (EngineError, TypeError, ValueError):
                hard_failure(row['ticker'], 'invalid_journal_order_evidence')
            if client in clients:
                hard_failure(row['ticker'], 'duplicate_journal_client_id')
                hard_failure(clients[client]['ticker'], 'duplicate_journal_client_id')
            clients[client] = row
            order = row.get('order_id')
            if isinstance(order, str) and order:
                if order in orders:
                    hard_failure(row['ticker'], 'duplicate_journal_order_id')
                    hard_failure(orders[order]['ticker'], 'duplicate_journal_order_id')
                    issues.append('A venue order identity belongs to multiple journal intents')
                orders[order] = row
            elif Decimal(row.get('fill_count', '0')) > ZERO:
                quantities_reconciled = False
                hard_failure(row['ticker'], 'missing_venue_order_id')
                issues.append('A confirmed journal fill lacks its venue order identity')
            if row.get('status') in ('sending', 'unknown'):
                quantities_reconciled = False
                hard_failure(row['ticker'], 'unresolved_journal_order')
            elif row.get('status') not in _TERMINAL_ORDER_STATUSES:
                # Typed evidence cannot promote an unrecognized or live order
                # status even if its currently observed quantity happens to match.
                hard_failure(row['ticker'], 'unresolved_journal_order')
        history, collection = [], {}
        if rows:
            for label, method in (('current', 'get_account_fills_page'), ('historical', 'get_historical_fills_page')):
                found, state = _page(self.reader, method, self.max_pages)
                history.extend(found)
                collection[label] = state
        else:
            collection = {label: dict(status='ready', complete=True, pages=0, cursor_present=False)
                for label in ('current', 'historical')}
        complete = all(state['complete'] for state in collection.values())
        history_complete = complete
        canonical, conflicts, invalid, filtered = {}, set(), 0, 0
        for raw in history:
            ticker = raw.get('ticker', raw.get('market_ticker'))
            if ticker not in tickers:
                filtered += 1
                continue
            try:
                exchange = raw.get('exchange_index')
                if type(exchange) is not int or not 0 <= exchange <= 1000:
                    raise ValueError()
                fill = normalize_fill(raw, ticker, exchange, now)
                identity = fill['fill_id']
                if identity in canonical and canonical[identity] != fill:
                    conflicts.add(identity)
                    for affected in (ticker, canonical[identity]['ticker']):
                        hard_failure(affected, 'conflicting_fill_identity')
                        conflicts_by_ticker[affected].add(identity)
                else:
                    canonical[identity] = fill
            except (EngineError, ValueError, TypeError, KeyError):
                invalid += 1
                invalid_by_ticker[ticker] += 1
                hard_failure(ticker, 'invalid_authoritative_fill')
        if conflicts:
            issues.append('Conflicting duplicate authoritative fills were excluded')
        if invalid:
            issues.append('Some scoped authoritative fills lacked valid canonical price, quantity, fee or account evidence')
        fills = sorted((fill for key, fill in canonical.items() if key not in conflicts),
            key=lambda fill: (_timestamp(fill['created_at']), fill['fill_id']))
        owned, external, totals = [], [], defaultdict(lambda: ZERO)
        partitions, timestamps, timestamp_basis = defaultdict(set), defaultdict(set), defaultdict(set)
        for fill in fills:
            if fill['order_id'] not in orders:
                external.append(fill)
                hard_failure(fill['ticker'], 'external_scoped_fill')
                continue
            row = orders[fill['order_id']]
            if row['ticker'] != fill['ticker'] or row.get('outcome') != ('yes' if fill['sign'] > 0 else 'no'):
                hard_failure(row['ticker'], 'fill_journal_mismatch')
                hard_failure(fill['ticker'], 'fill_journal_mismatch')
                issues.append('An authoritative fill differs from its journal contract or direction')
                continue
            owned.append((fill, row))
            try:
                if _timestamp(fill['created_at']) + 1e-6 < _timestamp(row.get('created_at')):
                    hard_failure(fill['ticker'], 'fill_before_journal_intent')
            except EngineError:
                hard_failure(fill['ticker'], 'invalid_journal_order_evidence')
            totals[fill['order_id']] += Decimal(fill['quantity'])
            partitions[fill['ticker']].add(fill['exchange_index'])
            timestamps[(fill['ticker'], fill['created_at'])].add(fill['sign'])
            unit_cost = Decimal(fill['yes_price']) if fill['sign'] > 0 else ONE-Decimal(fill['yes_price'])
            timestamp_basis[(fill['ticker'], fill['created_at'])].add(
                unit_cost + Decimal(fill['fee'])/Decimal(fill['quantity']))
        if external:
            issues.append('External fills overlap journal markets; affected strategy basis is not adopted')
        for row in rows:
            if totals[row.get('order_id')] != Decimal(row.get('fill_count', '0')):
                quantities_reconciled = False
                hard_failure(row['ticker'], 'unreconciled_order_quantity')
                issues.append('Authoritative fill quantity does not match a journal acknowledgement')
        for ticker, values in partitions.items():
            if len(values) > 1:
                hard_failure(ticker, 'conflicting_fill_partition')
                issues.append('A contract has conflicting exchange-shard fill partitions')
        for (ticker, _), signs in timestamps.items():
            if len(signs) > 1:
                bad_tickers.add(ticker)
                fifo_ambiguous.add(ticker)
                issues.append('Opposite fills share a timestamp with no authoritative ordering')
        for (ticker, _), bases in timestamp_basis.items():
            if len(bases) > 1:
                bad_tickers.add(ticker)
                fifo_ambiguous.add(ticker)
                issues.append('Same-time fills have differing unit basis and no authoritative FIFO sequence')
        if not complete:
            for ticker in tickers:
                hard_failure(ticker, 'incomplete_fill_history')
            issues.append('Bounded current and archived fill history is incomplete; affected P&L remains unknown')
        if conflicts or invalid:
            complete = False
        basis_evidence = {}
        for ticker in sorted(tickers):
            scoped_orders = [row for row in rows if row['ticker'] == ticker]
            reconciled = sum(row.get('status') in _TERMINAL_ORDER_STATUSES
                and totals[row.get('order_id')] == Decimal(row.get('fill_count', '0'))
                for row in scoped_orders)
            basis_evidence[ticker] = dict(
                hard_basis_status='unknown' if hard_reasons[ticker] else 'verified',
                hard_basis_reasons=sorted(hard_reasons[ticker]),
                fifo_ordering_status='ambiguous' if ticker in fifo_ambiguous else 'known',
                history_complete=history_complete,
                all_order_quantities_reconciled=reconciled == len(scoped_orders),
                external_fill_count=sum(fill['ticker'] == ticker for fill in external),
                owned_fill_count=sum(fill['ticker'] == ticker for fill, _ in owned),
                owned_order_count=len(scoped_orders), reconciled_order_count=reconciled,
                canonical_fill_count=sum(fill['ticker'] == ticker for fill in fills),
                conflicting_fill_count=len(conflicts_by_ticker[ticker]),
                invalid_fill_count=invalid_by_ticker[ticker], partition_count=len(partitions[ticker]))
        with localcontext() as context:
            context.prec = 50
            accounting = _account(owned, bad_tickers)
            for event in accounting['events']:
                proof = basis_evidence[event['ticker']]
                event['fill_history_basis_status'] = proof['hard_basis_status']
                event['fifo_ordering_status'] = proof['fifo_ordering_status']
            summaries = _order_summaries(rows, accounting['events'])
            settlement = _settlements(self.reader, self.max_pages, now, tickers, bad_tickers,
                owned, accounting['fill_inventory'], complete, basis_evidence)
        status = 'ready' if complete and not bad_tickers else 'partial'
        return dict(kind='kalshi_scheduled_activity_report', version=1, status=status,
            account_fingerprint=saved_account,
            status_scope='authoritative_fill_accounting; settlement_status is reported separately',
            generated_at=_iso(now), read_only=True, read_only_venue=True, execution_eligible=False,
            state_changes_applied=False, account_scope='primary_subaccount_0',
            scope='journal-owned BTC/ETH/SOL/XRP fills and exactly reconciled venue settlements; external trades excluded',
            journal_fingerprint=digest, collection=collection,
            basis_evidence_version=1, basis_evidence=basis_evidence,
            coverage=dict(complete=complete and not bad_tickers
                and settlement['settlement_status'] in ('ready', 'not_needed'),
                fill_accounting_complete=complete and not bad_tickers, history_complete=complete,
                settlement_complete=settlement['settlement_status'] in ('ready', 'not_needed'),
                unknown_basis_tickers=sorted(bad_tickers), invalid_fill_count=invalid,
                conflicting_fill_count=len(conflicts), filtered_unrelated_fill_count=filtered),
            issues=list(dict.fromkeys(issues)), external_fill_count=len(external),
            actual_fill_count=len(owned),
            actual_fees=(_text(sum((Decimal(fill['fee']) for fill, _ in owned), ZERO))
                if complete and quantities_reconciled else None),
            known_actual_fees=_text(sum((Decimal(fill['fee']) for fill, _ in owned), ZERO)),
            fees_complete=complete and quantities_reconciled,
            conservative_reserved_cost=_text(sum((Decimal(row['reserved_cost']) for row in rows), ZERO)),
            reservations_are_actual_costs=False, orders=summaries, **accounting, **settlement,
            total_realized_pnl=(_text(Decimal(accounting['realized_pnl'])+Decimal(settlement['settlement_pnl']))
                if accounting['realized_pnl'] is not None and settlement['settlement_pnl'] is not None else None),
            known_total_realized_pnl=_text(Decimal(accounting['known_realized_pnl'])
                + Decimal(settlement['known_settlement_pnl'])),
            limitations=['Only authoritative matched fill prices and fees enter execution accounting.',
                'Known realized P&L includes FIFO entry fees and exit fees; unknown basis remains null.',
                'Fill inventory is not a settlement-adjusted account position or mark-to-market equity.',
                'Archived position totals lacking explicit settlement quantities, result and revenue remain unattributed.',
                'Reservation diagnostics do not release cash, settle positions or resolve uncertain orders.'])


def _settlements(reader, max_pages, now, tickers, bad_tickers, owned, inventory, fills_complete,
        basis_evidence):
    """Preserve settlement proof independently of FIFO lot attribution.

    The settlement endpoint's ``fee_cost`` is not charged a second time: actual
    execution fees already enter FIFO basis. A timestamp tie can leave FIFO basis
    unknown while its signed inventory and final payout remain independently
    verifiable. The explicitly cited direct-member
    venue policy supplies a zero settlement charge, unless a separate explicit
    settlement-charge field is supplied. Archived summary P&L is never assigned
    to this strategy without its settlement quantities and revenue.
    """
    base = dict(settlement_status='not_collected', settlement_pnl=None,
        known_settlement_pnl='0', settlement_events=[], settlement_issues=[],
        settlement_collection={}, settlement_fee_policy=dict(_SETTLEMENT_FEE_POLICY),
        settlement_adjusted_fill_inventory=deepcopy(inventory))
    if not callable(getattr(reader, 'get_account_settlements_page', None)):
        base['settlement_issues'].append('An authoritative settlement reader is unavailable')
        return base
    raw, state = _page(reader, 'get_account_settlements_page', max_pages, key='settlements')
    base['settlement_collection']['current'] = state
    source_complete = state['complete']
    desired = {row['ticker']:row for row in inventory}
    signed_inventory = defaultdict(lambda: ZERO)
    for fill, _ in owned:
        if basis_evidence[fill['ticker']]['hard_basis_status'] == 'verified':
            signed_inventory[fill['ticker']] += Decimal(fill['quantity'])*fill['sign']
    # This map also drives archived settlement discovery. FIFO deliberately omits
    # ambiguous lots, but their nonflat quantity must still request final evidence.
    remaining = {ticker: quantity for ticker, quantity in signed_inventory.items() if quantity != ZERO}
    represented = {row.get('ticker', row.get('market_ticker')) for row in raw}
    missing = (set(desired) | set(remaining))-represented
    if missing and callable(getattr(reader, 'get_historical_positions_page', None)):
        archived, archived_state = _page(reader, 'get_historical_positions_page', max_pages, key='market_positions')
        base['settlement_collection']['historical_positions'] = archived_state
        source_complete = source_complete and archived_state['complete']
        for row in archived:
            if row.get('ticker', row.get('market_ticker')) not in missing:
                continue
            if (row.get('market_result') not in ('yes', 'no')
                    or not any(key in row for key in ('yes_count_fp', 'yes_count'))
                    or not any(key in row for key in ('no_count_fp', 'no_count'))
                    or not any(key in row for key in ('revenue_dollars', 'revenue'))
                    or not any(key in row for key in ('settled_time', 'settlement_time'))):
                base['settlement_issues'].append('Archived position totals do not establish attributable settlement quantities and revenue')
                continue
            raw.append(row)
    canonical, conflicts = {}, set()
    for record in raw:
        ticker = record.get('ticker', record.get('market_ticker'))
        if ticker not in tickers:
            continue
        try:
            for field in ('subaccount', 'subaccount_number'):
                if record.get(field) is not None and (type(record[field]) is not int or record[field] != 0):
                    raise ValueError()
            exchange = record.get('exchange_index')
            if type(exchange) is not int or not 0 <= exchange <= 1000 or record.get('market_result') not in ('yes', 'no'):
                raise ValueError()
            for side in ('yes', 'no'):
                quantity = decimal_value(record.get(side+'_count_fp', record.get(side+'_count')),
                    'settlement quantity', places=2, maximum=Decimal('100000000'))
                if side+'_count_fp' in record and side+'_count' in record:
                    if decimal_value(record[side+'_count'], 'settlement quantity alias', places=2) != quantity:
                        raise ValueError()
            if 'revenue_dollars' in record and 'revenue' in record:
                if type(record['revenue']) is not int or decimal_value(record['revenue_dollars'],
                        'settlement revenue', places=6) != Decimal(record['revenue'])/100:
                    raise ValueError()
            normalized = normalize_settlement(record, now)
            if normalized is None:
                continue
            fee = decimal_value(record.get('settlement_fee_dollars', '0'), 'explicit settlement fee',
                places=6, maximum=Decimal('100000000'))
            normalized['settlement_fee'] = fee
            normalized['fee_source'] = ('explicit_settlement_fee_dollars' if 'settlement_fee_dollars' in record
                else 'published_direct_member_venue_policy')
            normalized['reported_fee_cost'] = (None if record.get('fee_cost') is None else
                decimal_value(record['fee_cost'], 'reported settlement trading fees', places=6,
                    maximum=Decimal('100000000')))
            if ticker in canonical and canonical[ticker] != normalized:
                conflicts.add(ticker)
            else:
                canonical[ticker] = normalized
        except (EngineError, PerformanceError, ValueError, TypeError, KeyError):
            conflicts.add(ticker)
            base['settlement_issues'].append('A scoped settlement lacks valid primary-account quantity, result, revenue or fee evidence')
    subtotal, attributed, fifo_unattributed = ZERO, set(), set()
    for ticker in sorted(tickers):
        record = canonical.get(ticker)
        if record is None:
            continue
        proof = basis_evidence[ticker]
        if (ticker in conflicts or proof['hard_basis_status'] != 'verified'
                or not proof['history_complete']):
            base['settlement_issues'].append('A settlement cannot be attributed while its fill history or strategy basis is incomplete')
            continue
        position = desired.get(ticker)
        quantity = signed_inventory.get(ticker, ZERO)
        exchanges = {fill['exchange_index'] for fill, _ in owned if fill['ticker'] == ticker}
        latest_fill = max((_timestamp(fill['created_at']) for fill, _ in owned if fill['ticker'] == ticker), default=0)
        payout, convention, error = _settlement_payout(record, quantity)
        if (error is not None or exchanges != {record['key'][0]} or record['time'] < latest_fill):
            base['settlement_issues'].append('The broker settlement does not exactly reconcile to remaining owned quantity, partition and fill time')
            continue
        fifo_known = proof['fifo_ordering_status'] == 'known' and ticker not in bad_tickers
        basis = (ZERO if position is None else Decimal(position['open_premium_and_fees'])) if fifo_known else None
        pnl = None if basis is None else payout-basis-record['settlement_fee']
        if pnl is not None:
            subtotal += pnl
        else:
            fifo_unattributed.add(ticker)
        attributed.add(ticker)
        base['settlement_events'].append(dict(ticker=ticker, asset=_asset(ticker),
            exchange_index=record['key'][0], subaccount=0,
            timestamp=_iso(record['time']), market_result='yes' if record['yes_payout'] == ONE else 'no',
            owned_net_quantity=_text(quantity), reported_yes_quantity=_text(record['yes_count']),
            reported_no_quantity=_text(record['no_count']), reported_revenue=_text(record['revenue']),
            attributable_payout=_text(payout), revenue_convention=convention,
            fill_history_basis_status='verified', fifo_ordering_status=proof['fifo_ordering_status'],
            settlement_quantity_status='verified',
            remaining_entry_basis_including_fees=None if basis is None else _text(basis),
            settlement_fee=_text(record['settlement_fee']),
            settlement_fee_source=record['fee_source'],
            reported_fee_cost=None if record['reported_fee_cost'] is None else _text(record['reported_fee_cost']),
            reported_fee_cost_added_to_fill_fees=False, realized_pnl=None if pnl is None else _text(pnl)))
    unresolved = (set(desired) | set(remaining))-attributed
    base['settlement_adjusted_fill_inventory'] = [row for row in deepcopy(inventory) if row['ticker'] not in attributed]
    base['known_settlement_pnl'] = _text(subtotal)
    if unresolved:
        base['settlement_issues'].append('Some remaining fill inventory has no attributable settlement; it may be open or archived without sufficient detail')
    if fifo_unattributed:
        base['settlement_issues'].append('Settlement quantity and payout are verified; FIFO entry basis and settlement P&L remain unknown')
    settled_complete = (source_complete and not unresolved and not conflicts and not bad_tickers
        and not fifo_unattributed and fills_complete)
    base['settlement_status'] = ('ready' if settled_complete and canonical else
        'not_needed' if settled_complete else 'partial')
    base['settlement_pnl'] = _text(subtotal) if settled_complete else None
    base['settlement_issues'] = list(dict.fromkeys(base['settlement_issues']))
    return base


def _decision_midpoint(row):
    try:
        sizing = row.get('sizing', {})
        decision = sizing.get('scheduled_entry_decision', {})
        if decision.get('kind') != 'kalshi_scheduled_entry_decision' or decision.get('version') != 1:
            return None
        asks = decision['executable_asks']
        yes = decimal_value(asks['yes'], 'recorded YES ask', places=12, maximum=ONE)
        no = decimal_value(asks['no'], 'recorded NO ask', places=12, maximum=ONE)
        if not ZERO < yes < ONE or not ZERO < no < ONE or yes+no <= ONE:
            return None
        yes_mid = (yes+ONE-no)/2
        return yes_mid if row['outcome'] == 'yes' else ONE-yes_mid
    except (EngineError, KeyError, TypeError, AttributeError):
        return None


def _account(owned, bad_tickers):
    lots, events, pnl_by_asset, pnl_by_reason = defaultdict(deque), [], defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    known_pnl = entry_fees = exit_fees = unattributed_fees = net_cash = ZERO
    unknown_events, closes, wins, losses = 0, 0, 0, 0
    for fill, row in owned:
        ticker, quantity = fill['ticker'], Decimal(fill['quantity'])
        price = Decimal(fill['yes_price']) if fill['sign'] > 0 else ONE-Decimal(fill['yes_price'])
        fee, remaining, closed, basis, allocated_entry_fee = Decimal(fill['fee']), quantity, ZERO, ZERO, ZERO
        event = dict(fill_id=fill['fill_id'], order_id=fill['order_id'], client_order_id=row['client_order_id'],
            ticker=ticker, asset=_asset(ticker), timestamp=fill['created_at'], exchange_index=fill['exchange_index'],
            signed_quantity=_text(quantity*fill['sign']), quantity=_text(quantity),
            outcome='yes' if fill['sign'] > 0 else 'no', actual_price=_text(price), actual_fee=_text(fee),
            intended_limit=row['price'], order_reserved_cost=row['reserved_cost'],
            execution_cost_difference_vs_limit=_text(quantity*(price-Decimal(row['price']))),
            exit_reason=_exit_reason(row), spread_cost=None,
            spread_cost_status='A decision midpoint was not recorded',
            basis_status='known' if ticker not in bad_tickers else 'unknown',
            closing_quantity=None, realized_pnl=None, net_cash_flow=None, entry_fee=None, exit_fee=None)
        midpoint = _decision_midpoint(row)
        if midpoint is not None:
            event.update(decision_midpoint=_text(midpoint),
                spread_cost=_text(quantity*(Decimal(row['price'])-midpoint)),
                execution_cost_vs_decision_midpoint=_text(quantity*(price-midpoint)),
                spread_cost_status='Limit-to-midpoint benchmark from recorded decision quotes')
        if ticker in bad_tickers:
            unknown_events += 1
            unattributed_fees += fee
            events.append(event)
            continue
        queue = lots[ticker]
        while remaining > ZERO and queue and queue[0]['sign'] != fill['sign']:
            lot = queue[0]
            match = min(remaining, lot['quantity'])
            old_fee = lot['fee'] if match == lot['quantity'] else lot['fee']*match/lot['quantity']
            basis += match*lot['price'] + old_fee
            allocated_entry_fee += old_fee
            lot['quantity'] -= match
            lot['fee'] -= old_fee
            remaining -= match
            closed += match
            if lot['quantity'] == ZERO:
                queue.popleft()
        closing_fee = fee if closed == quantity else fee*closed/quantity
        opening_fee = fee-closing_fee
        realized = closed*(ONE-price)-basis-closing_fee
        cash = closed-quantity*price-fee
        if remaining > ZERO:
            queue.append(dict(sign=fill['sign'], quantity=remaining, price=price, fee=opening_fee))
        entry_fees += opening_fee
        exit_fees += closing_fee
        net_cash += cash
        known_pnl += realized
        if closed:
            if event['exit_reason'] == 'scheduled_entry':
                event['exit_reason'] = 'opposite_scheduled_entry'
            closes += 1
            wins += realized > ZERO
            losses += realized < ZERO
        pnl_by_asset[event['asset']] += realized
        pnl_by_reason[event['exit_reason']] += realized
        event.update(closing_quantity=_text(closed), realized_pnl=_text(realized),
            net_cash_flow=_text(cash), entry_fee=_text(opening_fee), exit_fee=_text(closing_fee),
            allocated_opening_lot_fees=_text(allocated_entry_fee))
        events.append(event)
    return dict(events=events, realized_pnl=_text(known_pnl) if not bad_tickers else None,
        known_realized_pnl=_text(known_pnl), realized_pnl_scope='FIFO closed journal-owned fill lots; settlements excluded',
        net_cash_flow=_text(net_cash) if not bad_tickers else None,
        entry_fees=_text(entry_fees) if not bad_tickers else None,
        exit_fees=_text(exit_fees) if not bad_tickers else None,
        known_entry_fees=_text(entry_fees), known_exit_fees=_text(exit_fees),
        unattributed_fees=_text(unattributed_fees),
        unknown_basis_event_count=unknown_events,
        closed_execution_count=closes, winning_closes=wins, losing_closes=losses,
        known_realized_pnl_by_asset={key:_text(value) for key,value in pnl_by_asset.items()},
        known_realized_pnl_by_exit_reason={key:_text(value) for key,value in pnl_by_reason.items()},
        fill_inventory=[dict(ticker=ticker, asset=_asset(ticker),
            net_quantity=_text(sum((lot['quantity']*lot['sign'] for lot in queue), ZERO)),
            open_premium_and_fees=_text(sum((lot['quantity']*lot['price']+lot['fee'] for lot in queue), ZERO)),
            settlement_adjusted=False) for ticker,queue in sorted(lots.items()) if queue])


def _order_summaries(rows, events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event['client_order_id']].append(event)
    result = []
    for row in rows:
        executions = grouped[row['client_order_id']]
        quantity = sum((Decimal(event['quantity']) for event in executions), ZERO)
        acknowledged = Decimal(row.get('fill_count', '0'))
        matched = quantity == acknowledged and row['status'] not in ('sending', 'unknown')
        known_pnl = sum((Decimal(event['realized_pnl']) for event in executions
            if event['realized_pnl'] is not None), ZERO)
        result.append(dict(client_order_id=row['client_order_id'], order_id=row.get('order_id'),
            ticker=row['ticker'], asset=_asset(row['ticker']), intent_time=row['created_at'],
            outcome=row['outcome'], journal_status=row['status'], exit_reason=_exit_reason(row),
            requested_quantity=_text(Decimal(row['count'])),
            acknowledged_fill_quantity=_text(acknowledged), actual_fill_quantity=_text(quantity),
            fill_quantity_reconciled=matched, actual_fill_count=len(executions),
            actual_average_price=(_text(sum((Decimal(event['quantity'])*Decimal(event['actual_price'])
                for event in executions), ZERO)/quantity) if quantity else None),
            actual_fees=(_text(sum((Decimal(event['actual_fee']) for event in executions), ZERO))
                if matched else None),
            realized_pnl=(_text(known_pnl) if matched and all(event['realized_pnl'] is not None
                for event in executions) else None),
            known_realized_pnl=_text(known_pnl), intended_limit=row['price'],
            conservative_reserved_cost=row['reserved_cost'], reservations_are_actual_costs=False))
    return result
