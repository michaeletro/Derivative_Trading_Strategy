#!/usr/bin/env python3
"""Audit hypothetical shared admissions; no account client or order capability."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from kalshi_bot import CLIError, _private_path
from kalshi_portfolio import _publish_report, _read_json


_KIND = 'kalshi_shadow_admission_report'
_ACCOUNT = 'd' * 64


def _demo_inputs(now, scenario='budget'):
    """Clearly synthetic competition for a fixed $100 hypothetical budget."""
    if scenario not in ('budget', 'concurrent'):
        raise CLIError('Choose a supported synthetic demo scenario')
    policy = dict(kind='kalshi_shadow_admission_policy', version=1,
        account_fingerprint=_ACCOUNT, total_budget='100',
        strategy_caps={'value': '60', 'neutral': '60'}, asset_caps={'BTC': '100'},
        group_caps={'crypto': '100'}, max_open_markets=2, max_state_age_seconds=5)
    if scenario == 'concurrent':
        # Keep strategy caps from deciding this test: only $20 shared cash
        # remains after the first two $40 requests, so a $30 request cannot fit.
        policy['strategy_caps'] = {'value': '100', 'neutral': '100'}
    ledgers = [dict(kind='kalshi_strategy_reservations', version=1, ledger_id=name,
        source_kind=name, environment='production', mode='live', account_fingerprint=_ACCOUNT,
        subaccount=0, as_of=now, fresh=True, complete=True, issues=[], reservations=[],
        position_ownership_complete=True, owned_positions=[], ownership_basis='local_ledger_inventory')
        for name in ('value', 'neutral')]
    state = dict(kind='kalshi_portfolio_state', version=1, environment='production',
        execution_eligible=False, observation_only=True, synthetic=True,
        account_fingerprint=_ACCOUNT, account_scope='primary_subaccount_0',
        status='ready', calculation_complete=True, issues=[], observed_at=now,
        acquisition_started_at=now, ledgers=ledgers, reservations=[], outstanding_orders=[],
        holdings=dict(kind='kalshi_portfolio_account_snapshot', version=1,
            status='ready', as_of=now, fetched_at=now, execution_eligible=False,
            positions=[], issues=[], snapshot={'lots': []}),
        venue_cash=dict(available_balance='100', subaccount=0,
            exchange_scope='all_exchange_indexes', received_at=now, request_started_at=now),
        cash=dict(venue_available='100', paid_open_entry_basis='0', matched_venue_reservations='0',
            matched_local_buffer='0', local_only_reservations='0', unallocated_cash_estimate='100',
            exchange_specific_spendability_established=False),
        exposure=dict(ledger_active_reserved_cost='0', known_pending_cash='0',
            known_ledger_pending_cash='0', conservative_entry_commitment='0'))
    shard_cash = dict(kind='kalshi_shadow_shard_cash', version=1,
        account_fingerprint=_ACCOUNT, subaccount=0, as_of=now, balances={'0': '100'})
    proposals = []
    requests = [('value', 'KXBTC15M-26NOV142345-45', '0.59', '60'),
                ('neutral', 'KXBTCD-26NOV1423-T90000', '0.59', '60')]
    if scenario == 'concurrent':
        requests = [('value', 'KXBTC15M-26NOV142345-45', '0.39', '40'),
                    ('neutral', 'KXBTCD-26NOV1423-T90000', '0.39', '40'),
                    ('value', 'KXBTC15M-26NOV142345-45', '0.29', '30')]
    for strategy, ticker, price, budget in requests:
        proposals.append(dict(kind='kalshi_shadow_proposal', version=1,
            proposal_id='demo-' + strategy + '-' + uuid.uuid4().hex, strategy_id=strategy,
            account_fingerprint=_ACCOUNT, as_of=now, legs=[dict(ticker=ticker,
                asset='BTC', group='crypto', exchange_index=0, action='buy', outcome='yes',
                count='100', limit_price=price, fee_reserve='1', cash_required=budget, risk_required=budget)]))
    return policy, state, shard_cash, proposals


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--journal', type=Path, required=True,
        help='Separate durable SHADOW journal outside source checkouts')
    result.add_argument('--demo', action='store_true', help='Use synthetic $100 inputs; never read an account')
    result.add_argument('--demo-scenario', choices=('budget', 'concurrent'),
        help='With --demo: budget tests $60+$60; concurrent tests $40+$40+$30')
    result.add_argument('--policy', type=Path, help='Version-one shadow policy JSON')
    result.add_argument('--state', type=Path, help='Complete fresh portfolio-state JSON, explicitly supplied evidence')
    result.add_argument('--shard-cash', type=Path, help='Explicit fresh primary-account per-shard cash evidence')
    result.add_argument('--proposals', type=Path, help='Version-one proposal bundle JSON')
    result.add_argument('--transitions', type=Path, help='Explicit audited hypothetical lifecycle event bundle JSON')
    result.add_argument('--status', action='store_true', help='Inspect an existing shadow journal; create no decisions')
    result.add_argument('--report', type=Path, help='Atomic shadow report JSON outside source checkouts')
    return result


def _bundle(path, kind, member):
    value = _read_json(_private_path(path))
    if (set(value) != {'kind', 'version', member} or value.get('kind') != kind
            or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get(member), list) or len(value[member]) > 1000
            or any(not isinstance(row, dict) for row in value[member])):
        raise CLIError('Use a bounded version-one shadow input bundle')
    return value[member]


def _target(path):
    if path is None:
        return None
    target = _private_path(path)
    if target.suffix.lower() != '.json':
        raise CLIError('Use a separate JSON shadow report outside source checkouts')
    if target.exists():
        previous = _read_json(target, maximum_bytes=12_000_000)
        if (previous.get('kind') != _KIND or previous.get('mode') != 'shadow'
                or previous.get('execution_eligible') is not False):
            raise CLIError('The report target contains unrelated state')
    return target


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from kalshi.portfolio_shadow_admission import (ShadowAdmissionError,
            ShadowAdmissionJournal, ShadowAdmissionPolicy)
        if args.demo_scenario is not None and not args.demo:
            raise CLIError('--demo-scenario requires the explicitly synthetic --demo')
        journal_path = _private_path(args.journal)
        if journal_path.suffix.lower() != '.sqlite3':
            raise CLIError('Use a separate .sqlite3 shadow journal')
        if args.status and not journal_path.is_file():
            raise CLIError('Status requires an existing shadow journal')
        target = _target(args.report)
        paths = [path for path in (args.policy, args.state, args.shard_cash, args.proposals, args.transitions)
                 if path is not None]
        inputs = [_private_path(path) for path in paths]
        if len(set(inputs)) != len(inputs) or target is not None and target in inputs:
            raise CLIError('Each shadow input and report must use a separate file')
        if args.demo:
            if inputs:
                raise CLIError('--demo cannot be combined with supplied evidence or lifecycle files')
            raw_policy, state, shard_cash, proposals = _demo_inputs(time.time(), args.demo_scenario or 'budget')
            transitions = []
        else:
            if args.policy is None:
                raise CLIError('Supply --policy or use the explicitly synthetic --demo')
            if args.status:
                if args.state or args.shard_cash or args.proposals or args.transitions:
                    raise CLIError('--status accepts only its existing journal, policy and optional report')
                state, shard_cash, proposals, transitions = None, None, [], []
            elif args.transitions is not None and args.state is None and args.proposals is None and args.shard_cash is None:
                state, shard_cash, proposals = None, None, []
                transitions = _bundle(args.transitions, 'kalshi_shadow_events', 'events')
            else:
                if args.state is None or args.proposals is None:
                    raise CLIError('Supply a fresh --state and --proposals for shadow admission')
                state = _read_json(_private_path(args.state), maximum_bytes=12_000_000)
                shard_cash = (state.get('shadow_shard_cash') if args.shard_cash is None else
                    _read_json(_private_path(args.shard_cash)))
                proposals = _bundle(args.proposals, 'kalshi_shadow_proposals', 'proposals')
                transitions = [] if args.transitions is None else _bundle(
                    args.transitions, 'kalshi_shadow_events', 'events')
            raw_policy = _read_json(_private_path(args.policy))
        policy = ShadowAdmissionPolicy.from_dict(raw_policy)
        decisions, events = [], []
        with ShadowAdmissionJournal(journal_path, policy) as journal:
            if not args.status:
                for proposal in proposals:
                    decisions.append(journal.admit(proposal, state, shard_cash=shard_cash))
                for event in transitions:
                    if set(event) != {'proposal_id', 'event', 'evidence'}:
                        raise CLIError('Each hypothetical event needs proposal_id, event and evidence')
                    events.append(journal.transition(event['proposal_id'], event['event'], event['evidence']))
            snapshot = journal.snapshot()
        report = dict(kind=_KIND, version=1, mode='shadow', execution_eligible=False,
            synthetic=bool(args.demo or state is not None and state.get('synthetic') is True
                or snapshot.get('contains_synthetic_decisions') is True),
            input_source='synthetic_demo' if args.demo else 'supplied_files',
            observed_at=time.time(), decisions=decisions, events=events, snapshot=snapshot,
            reason='Hypothetical reservations and audit only; no account client, venue orders or live admission authority')
        print(_publish_report(target, report), end='', flush=True)
        return 0
    except KeyboardInterrupt:
        print('Shadow admission stopped; durable hypothetical reservations are retained.', file=sys.stderr)
        return 130
    except Exception as error:
        fixed = isinstance(error, CLIError) or 'ShadowAdmissionError' in locals() and isinstance(error, ShadowAdmissionError)
        message = str(error) if fixed else 'Shadow evidence or local journal could not be verified'
        print('Kalshi shadow admission: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
