"""Evidence-based experiment results, separate from model and exit marks.

Official outcomes resolve simulated inventory; these are never actual account
returns. No outcome, payout, win count or completed return is inferred from a
forecast or a display mark.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math
import re

from .neutral_telemetry import action_metrics, number, text


ZERO = Decimal('0')
TOLERANCE = Decimal('0.00000001')
HASH = re.compile(r'[a-f0-9]{64}\Z')
MAX_SETTLEMENTS = 256


def _count(value):
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ValueError('Invalid experiment count')
    return value


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise ValueError('Invalid experiment evidence time')
    return value


def _same(left, right):
    return abs(number(left)-number(right)) <= TOLERANCE


def settlement_records(raw):
    """Validate bounded authoritative outcome facts and their ledger entries."""
    rows = raw.get('settlements')
    if rows is None:
        if any(key in raw for key in ('settlement_payouts', 'settlement_realized_pnl', 'official_settled_market_count')):
            raise ValueError('Settlement counters lack outcome evidence')
        return None
    if not isinstance(rows, list) or len(rows) > MAX_SETTLEMENTS:
        raise ValueError('Invalid official outcome history')
    stamp, seen, result = (_stamp(raw.get('observed_at')) if rows else raw.get('observed_at')), set(), []
    total_payout, total_pnl = ZERO, ZERO
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid official outcome record')
        ticker = row.get('ticker')
        if (not isinstance(ticker, str) or not 1 <= len(ticker) <= 128 or not ticker.isprintable()
                or ticker in seen):
            raise ValueError('Invalid or repeated official outcome identity')
        seen.add(ticker)
        if (row.get('source') != 'GET /markets/{ticker}' or row.get('status') != 'finalized'
                or row.get('result') not in ('yes', 'no')
                or not isinstance(row.get('contract_identity'), str) or not HASH.fullmatch(row['contract_identity'])
                or not isinstance(row.get('evidence_sha256'), str) or not HASH.fullmatch(row['evidence_sha256'])
                or type(row.get('synthetic')) is not bool
                or row['synthetic'] and raw.get('synthetic') is not True):
            raise ValueError('Official outcome provenance is inconsistent')
        terms = row.get('market_terms')
        if not isinstance(terms, dict):
            raise ValueError('Official outcome lacks recorded market terms')
        try:
            proof = {key: row[key] for key in ('ticker', 'contract_identity', 'source', 'status', 'result',
                'yes_payout', 'settled_at', 'window_end', 'market_terms')}
            encoded = json.dumps(proof, sort_keys=True, allow_nan=False, separators=(',', ':'))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError('Invalid official outcome evidence') from None
        if len(encoded) > 40000 or hashlib.sha256(encoded.encode('utf-8')).hexdigest() != row['evidence_sha256']:
            raise ValueError('Official outcome evidence fingerprint differs')
        received, settled, end = (_stamp(row.get(key)) for key in ('received_at', 'settled_at', 'window_end'))
        if not end <= settled <= received <= stamp:
            raise ValueError('Official outcome evidence is not causal')
        quantity = row.get('quantity')
        if type(quantity) is not int or abs(quantity) > 1_000_000:
            raise ValueError('Invalid settled inventory quantity')
        values = {key: number(row.get(key)) for key in ('yes_payout', 'payout', 'premium', 'entry_fees', 'realized_pnl', 'cash_after')}
        yes = values['yes_payout']
        if (yes != (1 if row['result'] == 'yes' else 0)
                or not 0 <= values['premium'] <= abs(quantity) or values['entry_fees'] < 0
                or quantity == 0 and (values['premium'] != 0 or values['entry_fees'] != 0)
                or not _same(values['payout'], abs(quantity)*(yes if quantity > 0 else 1-yes))
                or not _same(values['realized_pnl'], values['payout']-values['premium']-values['entry_fees'])
                or values['cash_after'] < 0 or raw.get('positions', {}).get(ticker, 0)):
            raise ValueError('Official settlement cash, basis or inventory is inconsistent')
        total_payout += values['payout']; total_pnl += values['realized_pnl']
        result.append({**{key: row[key] for key in ('ticker', 'contract_identity', 'source', 'status', 'result',
            'settled_at', 'received_at', 'window_end', 'quantity', 'evidence_sha256', 'synthetic')},
            **{key: text(value) for key, value in values.items()}})
    if (not _same(raw.get('settlement_payouts'), total_payout)
            or not _same(raw.get('settlement_realized_pnl'), total_pnl)
            or _count(raw.get('official_settled_market_count')) != len(result)
            or sum((number(row['entry_fees']) for row in result), ZERO) > number(raw.get('fees'))+TOLERANCE):
        raise ValueError('Official outcome totals do not reconcile to evidence')
    return result


def execution_totals(raw, actions, omitted):
    """Use durable totals when supplied; legacy windows remain explicitly scoped."""
    retained = action_metrics(actions)
    supplied = raw.get('cumulative_execution')
    if supplied is None:
        return retained, omitted == 0
    if not isinstance(supplied, dict):
        raise ValueError('Invalid cumulative execution evidence')
    result = {}
    for key in ('completed_baskets', 'partial_baskets', 'unfilled_baskets', 'requested_contracts', 'filled_contracts'):
        result[key] = _count(supplied.get(key))
        if result[key] < retained[key] or omitted == 0 and result[key] != retained[key]:
            raise ValueError('Cumulative execution count does not match retained fills')
    if (result['filled_contracts'] > result['requested_contracts']
            or sum(result[k] for k in ('completed_baskets', 'partial_baskets', 'unfilled_baskets')) != len(actions)+omitted):
        raise ValueError('Cumulative execution coverage is inconsistent')
    turnover = number(supplied.get('turnover'))
    if turnover < number(retained['turnover']) or omitted == 0 and not _same(turnover, retained['turnover']):
        raise ValueError('Cumulative turnover does not match retained fills')
    result['turnover'] = text(turnover)
    result['fill_rate'] = result['filled_contracts']/result['requested_contracts'] if result['requested_contracts'] else None
    return result, True


def experiment_scorecard(raw, settlements, *, remaining_basis, execution_complete):
    bankroll, cash, realized, fees = (number(raw[key]) for key in ('bankroll', 'cash', 'realized_pnl', 'fees'))
    closed = raw.get('closed_at')
    if closed is not None and (_stamp(closed) > _stamp(raw['observed_at'])):
        raise ValueError('Experiment closure is in the future')
    known = settlements is not None
    held = sum(q != 0 for q in raw.get('positions', {}).values())
    rows = [] if settlements is None else settlements
    inventory_rows = [row for row in rows if row['quantity']]
    reconciliation = raw.get('settlement_reconciliation')
    reconciliation_status = None
    if reconciliation is not None:
        if (not isinstance(reconciliation, dict)
                or reconciliation.get('kind') != 'kalshi_neutral_paper_reconciliation'
                or reconciliation.get('version') != 1
                or reconciliation.get('status') not in ('complete', 'pending', 'blocked')):
            raise ValueError('Invalid outcome reconciliation status')
        reconciliation_status = reconciliation['status']
    known_contracts = raw.get('known_contracts')
    all_observed_resolved = True
    if known_contracts is not None:
        if not isinstance(known_contracts, list) or len(known_contracts) > MAX_SETTLEMENTS:
            raise ValueError('Invalid observed contract evidence')
        terms = {}
        for contract in known_contracts:
            if (not isinstance(contract, dict) or not isinstance(contract.get('ticker'), str)
                    or not 1 <= len(contract['ticker']) <= 128 or not contract['ticker'].isprintable()
                    or contract['ticker'] in terms or not isinstance(contract.get('contract_identity'), str)
                    or not HASH.fullmatch(contract['contract_identity'])):
                raise ValueError('Repeated observed contract evidence')
            _stamp(contract.get('window_end'))
            terms[contract.get('ticker')] = contract
        for row in rows:
            contract = terms.get(row['ticker'])
            if contract is None or any(contract.get(key) != row[key] for key in ('contract_identity', 'window_end')):
                raise ValueError('Outcome terms differ from observed contract evidence')
        all_observed_resolved = set(terms) == {row['ticker'] for row in rows}
        if closed is not None and any(contract['window_end'] != closed for contract in known_contracts):
            raise ValueError('Experiment closure differs from its recorded settlement horizon')
    complete = (known and bool(rows) and closed is not None and not held and raw.get('pending') is None
        and all_observed_resolved and reconciliation_status in (None, 'complete'))
    status = ('blocked' if reconciliation_status == 'blocked' else 'complete' if complete else 'awaiting_settlement' if known and (closed is not None or rows)
              else 'collecting' if known else 'unavailable')
    source = 'synthetic' if raw.get('synthetic') else ('actual_market_data' if raw.get('input_source') == 'publisher_snapshot'
        else 'recorded_replay' if raw.get('input_source') == 'replay' else 'producer_report')
    settlement_pnl = number(raw['settlement_realized_pnl']) if known else None
    return dict(kind='kalshi_neutral_experiment_scorecard', version=1, status=status,
        evidence_source=source, official_outcomes_available=known, execution_totals_complete=execution_complete,
        reconciliation_status=reconciliation_status,
        session_resume_supported=raw.get('session_resume_supported') is True,
        initial_capital=text(bankroll), cash=text(cash), fees=text(fees), remaining_basis=text(remaining_basis),
        net_realized_pnl=text(realized), settlement_payouts=text(number(raw['settlement_payouts'])) if known else None,
        settlement_realized_pnl=text(settlement_pnl),
        trading_realized_pnl=text(realized-settlement_pnl) if known else None,
        completed_experiment_pnl=text(cash-bankroll) if complete else None,
        completed_return_fraction=text((cash-bankroll)/bankroll) if complete else None,
        official_resolved_markets=len(rows) if known else None,
        settled_inventory_markets=len(inventory_rows) if known else None,
        open_markets=held,
        settled_lot_wins=sum(number(row['realized_pnl']) > 0 for row in inventory_rows) if known else None,
        settled_lot_losses=sum(number(row['realized_pnl']) < 0 for row in inventory_rows) if known else None,
        settled_lot_flat=sum(number(row['realized_pnl']) == 0 for row in inventory_rows) if known else None,
        last_outcome_received_at=max((row['received_at'] for row in rows), default=None),
        closed_at=closed, settlements=rows,
        notes=[
            'Official exchange outcomes are applied to simulated fills; these are paper results, not account returns.',
            'Settlement P&L includes only remaining inventory basis; realized reductions are reported separately.',
            'Outcome rows and settled-lot wins can share one event and are not independent experiments.',
            'Completed return is withheld until the pinned experiment is closed and all inventory is resolved.',
            'Paid trading fees are already included in cash and P&L; do not subtract them again.'])
