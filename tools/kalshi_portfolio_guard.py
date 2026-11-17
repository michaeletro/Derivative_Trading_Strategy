#!/usr/bin/env python3
"""Assess shared portfolio capital and loss limits; never submit orders.

The decision is advisory. A real executor must refresh, check, and reserve it
under its own exclusive coordinator lock before any exchange submission.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from itertools import product
import json
import os
from pathlib import Path
import sys
import time

from kalshi.basket_scan import demo_basket_frame, scan_baskets
from kalshi.basket_watch import acquire_basket_frame
from kalshi.client import KalshiClient
from kalshi.neutral_market import NeutralMarketDiscovery, NeutralPublicClient
from kalshi.portfolio_admission import assess_admission
from kalshi.portfolio_admission_account import build_admission_state
from kalshi.value_sizing import ValueSizingConfig, worst_value_buy_unit_cost
from kalshi_portfolio_assess import _read_snapshot


class GuardCLIError(ValueError):
    """Fixed public configuration error."""


def _validate_limits(args):
    for name in ('capital_limit', 'loss_limit', 'unhedged_loss_limit'):
        try:
            raw = getattr(args, name)
            if len(raw) > 80:
                raise ValueError()
            value = Decimal(raw)
            if (not value.is_finite() or not 0 <= value <= 1000000
                    or name == 'capital_limit' and value == 0
                    or len(value.as_tuple().digits) > 40
                    or not -18 <= value.as_tuple().exponent <= 18):
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise GuardCLIError('Use finite nonnegative dollar limits and a positive capital limit') from None


def demo_admission_state(now=None):
    now = time.time() if now is None else now
    # A matched holding consumes capital even though its payoff floor is fixed.
    return dict(kind='kalshi_portfolio_admission_state', version=1,
        observed_at=now, complete=True,
        holdings=[dict(market='DEMO-HELD', side='yes', quantity='90', premium='36', fees='.8'),
                  dict(market='DEMO-HELD', side='no', quantity='90', premium='45', fees='.8')],
        working_orders=[dict(id='DEMO-WORKING-ORDER', market='DEMO-WORKING', side='yes',
            remaining_quantity='5', limit_price='.5', premium_bound='2.5', fee_allowance='.1',
            reservation='2.6', status='resting')])


def proposal_from_scan(frame, candidate):
    """Reserve worst permitted fill cash, including price-dependent fee changes."""
    proposal = dict(legs=deepcopy(candidate['legs']), observed_at=frame['observed_at'])
    contracts = {row['ticker']: row for row in frame['contracts']}
    for leg in proposal['legs']:
        contract = contracts[leg['market']]
        count, price = Decimal(leg['quantity']), Decimal(leg['limit_price'])
        config = ValueSizingConfig(fee_type=contract['fee_type'],
            fee_multiplier=contract['fee_multiplier'], fee_quantum=Decimal('.01'))
        fee = max(Decimal(leg['fee_allowance']),
                  count * (worst_value_buy_unit_cost(price, config)-price))
        leg['fee_allowance'] = format(fee, 'f')
        leg['all_in_cost'] = format(Decimal(leg['gross_cost'])+fee+
                                    Decimal(leg.get('slippage_allowance', '0')), 'f')
    return proposal


def attach_joint_states(state, proposal, scan):
    """Keep proven threshold inclusion and allow every other market combination."""
    state = deepcopy(state)
    markets = {row['market'] for row in state['holdings'] + state['working_orders'] + proposal['legs']}
    if len(markets) > 12:
        raise GuardCLIError('Joint admission supports at most twelve distinct markets')
    quoted = set(scan['scenarios'][0]['outcomes']) & markets
    outside = sorted(markets-quoted)
    projected = {tuple(sorted((key, value) for key, value in row['outcomes'].items() if key in quoted))
                 for row in scan['scenarios']}
    scenarios = []
    for common in sorted(projected):
        for values in product((0, 1), repeat=len(outside)):
            outcomes = dict(common)
            outcomes.update(zip(outside, values))
            scenarios.append(dict(name='joint_' + str(len(scenarios)), outcomes=outcomes))
    state.update(scenarios=scenarios, exhaustive=True)
    return state


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true')
    source.add_argument('--state', type=Path, help='Complete current holdings/working-order state JSON')
    source.add_argument('--account', action='store_true', help='Acquire read-only production account and supplied journal evidence')
    proposal = parser.add_mutually_exclusive_group()
    proposal.add_argument('--proposal', type=Path, help='JSON object containing proposed scanner-shaped legs')
    proposal.add_argument('--live', action='store_true', help='Acquire a fresh public basket proposal; no order authority')
    parser.add_argument('--journal', action='append', type=Path, default=[],
                        help='Existing strategy journal to inspect read-only; repeat for every writer in scope')
    parser.add_argument('--capital-limit', default='100')
    parser.add_argument('--loss-limit', default='100')
    parser.add_argument('--unhedged-loss-limit', default='25')
    parser.add_argument('--max-contracts', type=int, default=20)
    parser.add_argument('--max-pages', type=int, default=5)
    return parser


def _account(args):
    credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
                       private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
    if not all(credentials.values()):
        raise GuardCLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
    credentials['private_key_path'] = str(Path(credentials['private_key_path']).expanduser())
    client = KalshiClient(environment='production', allow_writes=False, timeout=3, **credentials)
    try:
        return build_admission_state(client, journal_paths=args.journal, max_pages=args.max_pages)
    finally:
        client.close()


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.demo and (args.proposal is not None or args.live or args.journal):
        _parser().error('--demo supplies its own state and proposal')
    if not args.demo and args.proposal is None and not args.live:
        _parser().error('Supply --proposal or --live with --state or --account')
    if args.journal and not args.account:
        _parser().error('--journal requires --account')
    if not 1 <= args.max_contracts <= 100 or not 1 <= args.max_pages <= 20:
        _parser().error('Use 1-100 contracts per leg and 1-20 discovery pages')
    try:
        _validate_limits(args)
        account = None
        supplied = None if args.proposal is None else _read_snapshot(args.proposal)
        if args.demo:
            state = demo_admission_state()
        elif args.state:
            state = _read_snapshot(args.state)
        else:
            account = _account(args)
            state = account.get('state')
            if state is None or account.get('status') != 'ready' or state.get('complete') is not True:
                report = dict(kind='kalshi_portfolio_admission', version=1, decision='block',
                    execution_eligible=False, assessment_only=True,
                    reason=account.get('reason', 'Account state is incomplete'),
                    account_status=account.get('status'), issues=account.get('issues', []))
                print(json.dumps(report, indent=2))
                return 2
        scan = None
        if args.demo or args.live:
            if args.demo:
                frame = demo_basket_frame()
            else:
                client = NeutralPublicClient(environment='production', timeout=3)
                try:
                    frame = acquire_basket_frame(NeutralMarketDiscovery(client, max_pages=args.max_pages))
                finally:
                    client.close()
            if frame.get('kind') != 'kalshi_basket_quote_frame':
                print(json.dumps(dict(kind='kalshi_portfolio_admission', version=1,
                    decision='block', execution_eligible=False, assessment_only=True,
                    reason='A fresh verified basket proposal is unavailable', discovery=frame), indent=2))
                return 2
            scan = scan_baskets(frame, bankroll=args.capital_limit, max_contracts=args.max_contracts,
                                max_unhedged_loss=args.unhedged_loss_limit)
            candidates = [row for row in scan['candidates'] if row['passed']]
            if not candidates:
                print(json.dumps(dict(kind='kalshi_portfolio_admission', version=1,
                    decision='block', execution_eligible=False, assessment_only=True,
                    reason='No quoted candidate passes standalone cost and leg-risk checks', scan=scan), indent=2))
                return 2
            proposal = proposal_from_scan(frame, candidates[0])
            if state.get('complete') is True:
                state = attach_joint_states(state, proposal, scan)
        else:
            proposal = supplied
        report = assess_admission(state, proposal, capital_limit=args.capital_limit,
            loss_limit=args.loss_limit, unhedged_loss_limit=args.unhedged_loss_limit)
        report.update(input_source='demo' if args.demo else 'account' if args.account else 'supplied_state',
            synthetic=args.demo, proposal_source='public_quotes' if args.live else
            'synthetic_quotes' if args.demo else 'supplied_cost_assumptions',
            reprice_required=report['decision'] == 'resize')
        if account is not None:
            report.update(account_status=account.get('status'), account_issues=account.get('issues', []))
        print(json.dumps(report, indent=2, allow_nan=False))
        return 2 if report['decision'] == 'block' else 0
    except FileNotFoundError:
        print('Guard input file not found. Use an existing state/proposal file or --demo.', file=sys.stderr)
    except GuardCLIError as error:
        print('Portfolio guard: ' + str(error), file=sys.stderr)
    except Exception:
        print('Portfolio guard could not verify its inputs; no admission or account changes were made.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
