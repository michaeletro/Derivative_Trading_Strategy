"""Read-only, bounded neutral-paper performance reports for the local console."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import math
from pathlib import Path
import threading
import time

from .neutral_telemetry import NeutralTelemetry, action_metrics, actual_risk, current_marks, number, text
from .neutral_scorecard import execution_totals, experiment_scorecard, settlement_records


MAX_REPORT_BYTES = 16_000_000
_DECIMALS = ('initial_bankroll', 'cash', 'fair_value_equity', 'liquidation_equity',
    'fair_value_pnl', 'liquidation_pnl', 'realized_pnl', 'unrealized_pnl', 'fees',
    'turnover', 'return_fraction', 'drawdown_fraction', 'max_drawdown_fraction')
_COUNTS = ('completed_baskets', 'partial_baskets', 'unfilled_baskets', 'requested_contracts',
    'filled_contracts', 'open_contracts', 'open_markets', 'evaluations', 'proposals', 'holds', 'observations', 'outcome_observations')
_RISK = ('residual_delta', 'residual_gamma', 'worst_case_loss', 'cvar_loss',
         'expected_terminal_profit', 'maximum_interim_abs_delta')
_HISTORY = ('cash', 'fair_value_equity', 'liquidation_equity', 'fair_value_pnl',
    'liquidation_pnl', 'realized_pnl', 'fees', 'drawdown_fraction',
    'residual_delta', 'residual_gamma', 'worst_case_loss')

_TOLERANCE = Decimal('0.00000001')


def _same(left, right):
    return abs(number(left)-number(right)) <= _TOLERANCE


def _basis(lots, positions):
    """Remaining basis must describe exactly the confirmed nonzero inventory."""
    found, total = {}, Decimal(0)
    for lot in lots:
        ticker = _identifier(lot.get('market'))
        if ticker in found or ticker not in positions or not positions[ticker]:
            raise ValueError('Remaining basis does not match owned inventory')
        quantity, premium, fees = (number(lot.get(k)) for k in ('quantity', 'premium', 'fees'))
        if (quantity != abs(positions[ticker]) or quantity <= 0
                or premium < 0 or premium > quantity or fees < 0
                or lot['side'] != ('yes' if positions[ticker] > 0 else 'no')):
            raise ValueError('Remaining basis does not match owned inventory')
        found[ticker] = lot
        total += premium+fees
    if set(found) != {t for t, q in positions.items() if q}:
        raise ValueError('Remaining basis is incomplete')
    return total


def _execution_evidence(actions, omitted, bankroll, cash, fees, positions, stamp, settlements=()):
    """Check filled quantities/cash flows; a truncated prefix stays explicit."""
    running_cash = bankroll if omitted == 0 else None
    held, basis, known_fees, previous_time = {}, {}, Decimal(0), None
    settled_horizons = {row['ticker']: row['window_end'] for row in settlements}
    action_times = [_stamp(action.get('fill_time')) for action in actions]
    if action_times != sorted(action_times):
        raise ValueError('Execution observations are not causal')
    events = [(a['fill_time'], 0, a) for a in actions]
    events.extend((row['received_at'], 1, row) for row in settlements)
    # Snapshot producers may sort outcomes by ticker. At an identical receipt
    # clock, nonnegative payouts order their own cash-after ledger evidence.
    for _, event_type, action in sorted(events, key=lambda item: (item[0], item[1],
            number(item[2]['cash_after']) if item[1] else Decimal(0))):
        if event_type:
            ticker, quantity = action['ticker'], action['quantity']
            if omitted == 0:
                if held.get(ticker, 0) != quantity:
                    raise ValueError('Settled quantity differs from confirmed simulated inventory')
                premium, entry_fees = basis.get(ticker, (Decimal(0), Decimal(0)))
                if not _same(premium, action['premium']) or not _same(entry_fees, action['entry_fees']):
                    raise ValueError('Settlement basis differs from confirmed simulated fills')
                held.pop(ticker, None); basis.pop(ticker, None)
            if running_cash is not None:
                running_cash += number(action['payout'])
                if not _same(running_cash, action['cash_after']):
                    raise ValueError('Settlement cash does not reconcile to the preceding ledger')
            continue
        intent, filled_at = _stamp(action.get('intent_time')), _stamp(action.get('fill_time'))
        if intent > filled_at or stamp is None or filled_at > stamp or previous_time is not None and filled_at < previous_time:
            raise ValueError('Execution observations are not causal')
        previous_time = filled_at
        change, filled_total, requested_total = Decimal(0), 0, 0
        for leg in action['legs']:
            requested, filled = _count(leg['requested_count']), _count(leg['filled_count'])
            if requested <= 0:
                raise ValueError('Invalid neutral requested quantity')
            filled_total += filled; requested_total += requested
            expected = 'filled' if filled == requested else 'partial' if filled else 'unfilled'
            if leg.get('status') != expected:
                raise ValueError('Inconsistent neutral leg status')
            fill = leg.get('fill')
            if fill is None:
                if filled:
                    raise ValueError('Positive fill lacks cash evidence')
                continue
            if (not isinstance(fill, dict) or fill.get('count') != filled
                    or type(fill.get('count')) is not int
                    or fill.get('action') not in ('buy', 'sell') or fill.get('outcome') not in ('yes', 'no')):
                raise ValueError('Invalid neutral fill evidence')
            gross, fee, delta, average = (number(fill.get(k)) for k in ('gross', 'fee', 'cash_delta', 'average_price'))
            expected_delta = -gross-fee if fill['action'] == 'buy' else gross-fee
            if (gross < 0 or gross > filled or fee < 0 or not 0 <= average <= 1
                    or not _same(gross, average*filled) or not _same(delta, expected_delta)):
                raise ValueError('Inconsistent neutral fill cash flow')
            change += delta; known_fees += fee
            if filled and leg['ticker'] in settled_horizons and filled_at >= settled_horizons[leg['ticker']]:
                raise ValueError('A simulated fill occurs after its finalized contract horizon')
            if omitted == 0:
                ticker, sign = leg['ticker'], 1 if fill['outcome'] == 'yes' else -1
                prior = held.get(ticker, 0)
                if fill['action'] == 'buy':
                    if prior and prior*sign < 0:
                        raise ValueError('A purchase crosses existing inventory')
                    held[ticker] = prior+sign*filled
                    premium, entry_fees = basis.get(ticker, (Decimal(0), Decimal(0)))
                    basis[ticker] = (premium+gross, entry_fees+fee)
                else:
                    if filled and (prior*sign <= 0 or abs(prior) < filled):
                        raise ValueError('A sale exceeds confirmed inventory')
                    held[ticker] = prior-sign*filled
                    if filled:
                        premium, entry_fees = basis[ticker]
                        remainder = Decimal(abs(prior)-filled)/abs(prior)
                        basis[ticker] = (premium*remainder, entry_fees*remainder)
        expected_status = 'filled' if filled_total == requested_total else 'partial' if filled_total else 'unfilled'
        if action['status'] != expected_status:
            raise ValueError('Inconsistent neutral basket status')
        after = number(action.get('cash_after'))
        if running_cash is not None and not _same(after, running_cash+change):
            raise ValueError('Execution cash does not reconcile')
        running_cash = after
    if running_cash is not None and not _same(running_cash, cash):
        raise ValueError('Final execution cash does not match the report')
    if omitted == 0:
        if not _same(known_fees, fees) or {t:q for t,q in held.items() if q} != {t:q for t,q in positions.items() if q}:
            raise ValueError('Fees or holdings do not reconcile to fills')
    elif known_fees > fees+_TOLERANCE:
        raise ValueError('Retained fees exceed the session fee total')


def _plain(value, *, depth=0, budget=None):
    """Bound optional diagnostics; imported strings are never executable markup."""
    budget = [1000000] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 16:
        raise ValueError('Oversized neutral diagnostics')
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        if len(value) > 1000:
            raise ValueError('Oversized neutral text')
        return value
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError('Invalid neutral number')
        return value
    if isinstance(value, list):
        if len(value) > 10000:
            raise ValueError('Oversized neutral list')
        return [_plain(v, depth=depth+1, budget=budget) for v in value]
    if isinstance(value, dict):
        if len(value) > 1000 or any(not isinstance(k, str) or len(k) > 128 for k in value):
            raise ValueError('Invalid neutral diagnostics')
        return {k: _plain(v, depth=depth+1, budget=budget) for k, v in value.items()}
    raise ValueError('Invalid neutral diagnostics')


def _stamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise ValueError('Invalid neutral timestamp')
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= 1000000000:
        raise ValueError('Invalid neutral count')
    return value


def _decimal(value, *, nullable=False):
    return text(number(value, nullable=nullable))


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable():
        raise ValueError('Invalid neutral identity')
    return value


def unavailable_neutral(reason, *, source='report'):
    return dict(kind='kalshi_neutral_monitor', version=1, status='unavailable', reason=reason,
        source=source, input_source='demo' if source == 'demo' else 'report',
        synthetic=source == 'demo', research_only=True, execution_eligible=False,
        session_resume_supported=False, observed_at=None, session_id=None,
        summary=dict.fromkeys(_DECIMALS+_COUNTS+('fill_rate',)), risk=dict.fromkeys(_RISK),
        limits={}, config={}, model={}, contracts=[], history=[], actions=[], positions={},
        open_lots=[], pending=None, repair_required=False, last_decision={},
        experiment=None,
        payoff_assessment=None, scenarios=[], coverage=dict(telemetry=False,
            history_start=None, history_points=0, omitted_history_points=0, omitted_actions=0,
            current_risk_available=False, history_basis='unavailable',
            drawdown_basis='full-depth liquidation equity', reason=reason),
        limitations=['Research performance is separate from actual account results.'])


def normalize_neutral_report(raw, *, source='report', now=None):
    """Project an explicit paper report; never infer actual risk from a target."""
    now = time.time() if now is None else _stamp(now)
    if (not isinstance(raw, dict) or raw.get('kind') != 'kalshi_neutral_paper_state'
            or type(raw.get('version')) is not int or raw['version'] != 1
            or raw.get('execution_eligible') is not False or raw.get('research_only') is not True
            or raw.get('mode', 'paper') != 'paper' or type(raw.get('synthetic')) is not bool):
        raise ValueError('A neutral paper report is required')
    stamp = _stamp(raw.get('observed_at'), nullable=True)
    if stamp is not None and stamp > now+0.001:
        raise ValueError('Neutral report has a future observation')
    result = unavailable_neutral('', source=source)
    synthetic = raw['synthetic']
    if source == 'demo' and not synthetic:
        raise ValueError('Synthetic example provenance is missing')
    input_source = raw.get('input_source', 'report')
    if input_source not in ('demo', 'publisher_snapshot', 'replay', 'report'):
        raise ValueError('Invalid neutral report source')
    result.update(observed_at=stamp, synthetic=synthetic, input_source=input_source)
    config = _plain(raw.get('config'))
    if not isinstance(config, dict) or not isinstance(config.get('sizing'), dict):
        raise ValueError('Neutral report settings are unavailable')
    result['config'] = config
    result['limits'] = config['sizing']
    positions = raw.get('positions')
    if not isinstance(positions, dict) or len(positions) > 6:
        raise ValueError('Invalid neutral positions')
    for ticker, quantity in positions.items():
        _identifier(ticker)
        if type(quantity) is not int or not -1000000 <= quantity <= 1000000:
            raise ValueError('Invalid neutral position quantity')
    result['positions'] = dict(positions)
    lots = _plain(raw.get('open_lots', []))
    if not isinstance(lots, list) or len(lots) > 6:
        raise ValueError('Invalid neutral remaining basis')
    for lot in lots:
        if not isinstance(lot, dict) or lot.get('side') not in ('yes', 'no'):
            raise ValueError('Invalid neutral remaining lot')
        _identifier(lot.get('market'))
        for key in ('quantity', 'premium', 'fees'):
            if number(lot.get(key)) < 0:
                raise ValueError('Invalid neutral remaining lot amount')
    result['open_lots'] = lots
    result['pending'] = _plain(raw.get('pending'))
    if result['pending'] is not None and not isinstance(result['pending'], dict):
        raise ValueError('Invalid neutral pending intent')
    if type(raw.get('force_reduce')) is not bool:
        raise ValueError('Invalid neutral repair state')
    result['repair_required'] = raw['force_reduce']
    decision = _plain(raw.get('last_decision', {}))
    if not isinstance(decision, dict):
        raise ValueError('Invalid neutral decision')
    result['last_decision'] = decision
    assessment = _plain(raw.get('payoff_assessment'))
    if assessment is not None and (not isinstance(assessment, dict)
            or assessment.get('kind') != 'kalshi_portfolio_payoff_assessment'
            or assessment.get('execution_eligible') is not False):
        raise ValueError('Invalid neutral payoff assessment')
    result['payoff_assessment'] = assessment
    actions = raw.get('actions', [])
    if not isinstance(actions, list) or len(actions) > 10000:
        raise ValueError('Oversized neutral execution history')
    actions = _plain(actions)
    for action in actions:
        if not isinstance(action, dict) or action.get('status') not in ('filled', 'partial', 'unfilled'):
            raise ValueError('Invalid neutral execution status')
        _stamp(action.get('fill_time'))
        _stamp(action.get('intent_time'))
        if not isinstance(action.get('legs'), list) or not 1 <= len(action['legs']) <= 2:
            raise ValueError('Invalid neutral execution legs')
        for leg in action['legs']:
            _identifier(leg.get('ticker'))
            requested, filled = _count(leg.get('requested_count')), _count(leg.get('filled_count'))
            if filled > requested:
                raise ValueError('Neutral fills exceed the intent')
    omitted = _count(raw.get('omitted_action_count', 0))
    result['actions'] = actions[-300:]
    summary = result['summary']
    bankroll, cash = number(raw.get('bankroll')), number(raw.get('cash'))
    if bankroll <= 0 or cash < 0:
        raise ValueError('Invalid neutral cash')
    fair = number(raw.get('fair_value_equity'), nullable=True)
    liquidation = number(raw.get('visible_liquidation_equity'), nullable=True)
    realized, fees = number(raw.get('realized_pnl')), number(raw.get('fees'))
    if fees < 0:
        raise ValueError('Invalid neutral fees')
    remaining_basis = _basis(lots, positions)
    if not _same(realized, cash+remaining_basis-bankroll):
        raise ValueError('Realized P&L does not reconcile to cash and remaining basis')
    settlements = settlement_records(raw)
    reconciliation = _plain(raw.get('settlement_reconciliation'))
    if reconciliation is not None and (not isinstance(reconciliation, dict)
            or _stamp(reconciliation.get('checked_at')) > now+0.001):
        raise ValueError('Official reconciliation check has an invalid receipt clock')
    _execution_evidence(actions, omitted, bankroll, cash, fees, positions, stamp, settlements or ())
    totals, totals_complete = execution_totals(raw, actions, omitted)
    result['experiment'] = experiment_scorecard(raw, settlements,
        remaining_basis=remaining_basis, execution_complete=totals_complete)
    summary.update(initial_bankroll=text(bankroll), cash=text(cash),
        fair_value_equity=text(fair), liquidation_equity=text(liquidation),
        fair_value_pnl=text(None if fair is None else fair-bankroll),
        liquidation_pnl=text(None if liquidation is None else liquidation-bankroll),
        realized_pnl=text(realized), unrealized_pnl=text(None if liquidation is None else liquidation-bankroll-realized),
        fees=text(fees), return_fraction=text(None if liquidation is None else (liquidation-bankroll)/bankroll),
        open_contracts=sum(abs(q) for q in positions.values()),
        open_markets=sum(q != 0 for q in positions.values()), **totals)
    telemetry = raw.get('telemetry')
    if telemetry is not None:
        if (not isinstance(telemetry, dict) or telemetry.get('kind') != 'kalshi_neutral_telemetry'
                or telemetry.get('version') != 1):
            raise ValueError('Invalid neutral observation history')
        result['session_id'] = _identifier(telemetry.get('session_id'))
        rows = telemetry.get('history')
        if not isinstance(rows, list) or len(rows) > 1800:
            raise ValueError('Oversized neutral observation history')
        history, previous, sequence = [], None, 0
        previous_fees, reconstructed_high, observed_max_drawdown = None, bankroll, Decimal(0)
        omitted_history = _count(telemetry.get('omitted_history_points', 0))
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid neutral history point')
            point_time, point_seq = _stamp(row.get('observed_at')), _count(row.get('sequence'))
            if ((previous is not None and point_time <= previous) or point_seq <= sequence
                    or stamp is None or point_time > stamp):
                raise ValueError('Neutral history is not causal')
            point = dict(observed_at=point_time, sequence=point_seq,
                **{key: _decimal(row.get(key), nullable=True) for key in _HISTORY})
            point['observation_kind'] = row.get('observation_kind', 'market')
            if point['observation_kind'] not in ('market', 'settlement'):
                raise ValueError('Invalid observation kind')
            for equity_key, pnl_key in (('fair_value_equity', 'fair_value_pnl'), ('liquidation_equity', 'liquidation_pnl')):
                equity_value, pnl_value = point[equity_key], point[pnl_key]
                if ((equity_value is None) != (pnl_value is None)
                        or equity_value is not None and not _same(number(equity_value)-bankroll, pnl_value)):
                    raise ValueError('Historical P&L does not match its equity basis')
            point_fees = number(point['fees'])
            if point_fees < 0 or previous_fees is not None and point_fees < previous_fees:
                raise ValueError('Historical paid fees are not cumulative')
            previous_fees = point_fees
            drawdown = number(point['drawdown_fraction'], nullable=True)
            if point['liquidation_equity'] is None:
                if drawdown is not None:
                    raise ValueError('Missing liquidation mark cannot establish drawdown')
            elif drawdown is None or not 0 <= drawdown <= 1:
                raise ValueError('Invalid observed drawdown')
            else:
                reconstructed_high = max(reconstructed_high, number(point['liquidation_equity']))
                recorded_high = number(row.get('high_water_equity'), nullable=True)
                if recorded_high is not None:
                    if recorded_high < reconstructed_high:
                        raise ValueError('Historical high-water equity is inconsistent')
                    if not _same(drawdown, (recorded_high-number(point['liquidation_equity']))/recorded_high):
                        raise ValueError('Historical drawdown does not match its high water')
                    reconstructed_high = recorded_high
                elif not omitted_history and not _same(drawdown, (reconstructed_high-number(point['liquidation_equity']))/reconstructed_high):
                    raise ValueError('Historical drawdown does not match observed equity')
                observed_max_drawdown = max(observed_max_drawdown, drawdown)
            history.append(point)
            previous, sequence = point_time, point_seq
        result['history'] = history
        evaluations = _count(telemetry.get('evaluations'))
        market_evaluations = _count(telemetry.get('market_evaluations', evaluations))
        outcome_observations = _count(telemetry.get('outcome_observations', 0))
        if (evaluations != omitted_history+len(history)
                or market_evaluations+outcome_observations != evaluations
                or history and (history[-1]['observed_at'] != stamp or sequence != evaluations)
                or sum(row['observation_kind'] == 'settlement' for row in history) > outcome_observations
                or sum(row['observation_kind'] == 'market' for row in history) > market_evaluations):
            raise ValueError('Observation coverage does not match the session counter')
        proposals, holds = _count(telemetry.get('proposals')), _count(telemetry.get('holds'))
        maximum_drawdown = number(telemetry.get('max_drawdown_fraction'))
        if (proposals+holds > market_evaluations or not 0 <= maximum_drawdown <= 1
                or maximum_drawdown+_TOLERANCE < observed_max_drawdown
                or not omitted_history and not _same(maximum_drawdown, observed_max_drawdown)):
            raise ValueError('Session drawdown or decision counters are inconsistent')
        summary.update(evaluations=market_evaluations, observations=evaluations, outcome_observations=outcome_observations,
            proposals=proposals, holds=holds,
            max_drawdown_fraction=text(maximum_drawdown),
            drawdown_fraction=history[-1]['drawdown_fraction'] if history else None)
        result['model'] = _plain(telemetry.get('model', {}))
        contracts = _plain(telemetry.get('contracts', []))
        if not isinstance(contracts, list) or len(contracts) > 6:
            raise ValueError('Invalid neutral contract context')
        for contract in contracts:
            ticker = _identifier(contract.get('ticker'))
            quantity = positions.get(ticker, 0)
            contract.update(position=quantity, quantity=abs(quantity),
                            side='yes' if quantity > 0 else 'no' if quantity < 0 else None)
            for key in ('strike', 'probability_yes', 'delta_yes', 'gamma_yes'):
                contract[key] = _decimal(contract.get(key))
        result['contracts'] = contracts
        # Reconstruct actual risk from current inventory and the supplied common
        # model. Never trust a candidate's after metrics or unaudited risk copy.
        from .neutral_model import normalize_neutral_snapshot
        frame_time = _stamp(telemetry.get('frame_observed_at', stamp))
        if frame_time > stamp:
            raise ValueError('Model context is later than report evidence')
        frame = normalize_neutral_snapshot(dict(kind='kalshi_neutral_snapshot', version=1,
            observed_at=frame_time, model=result['model'], contracts=contracts), now=frame_time)
        for row in settlements or ():
            contract = next((c for c in frame['contracts'] if c['ticker'] == row['ticker']), None)
            if contract is not None and (row['contract_identity'] != contract['contract_identity']
                    or row['window_end'] != contract['window_end']):
                raise ValueError('Settlement evidence belongs to different contract terms')
        by_basis = {lot['market']: lot for lot in lots}
        result['contracts'] = [dict(contract, position=positions.get(contract['ticker'], 0),
            quantity=abs(positions.get(contract['ticker'], 0)),
            side='yes' if positions.get(contract['ticker'], 0) > 0 else 'no' if positions.get(contract['ticker'], 0) < 0 else None,
            basis=text(number(by_basis[contract['ticker']]['premium'])+number(by_basis[contract['ticker']]['fees']))
                if contract['ticker'] in by_basis else '0') for contract in frame['contracts']]
        computed_fair, computed_liquidation = current_marks(raw, frame)
        for reported, computed in ((fair, computed_fair), (liquidation, computed_liquidation)):
            if reported is not None and (computed is None or not _same(reported, computed)):
                raise ValueError('Current equity mark does not reconcile to held inventory and source books')
        if history:
            last = history[-1]
            for key, expected in (('cash', cash), ('realized_pnl', realized), ('fees', fees),
                                  ('fair_value_equity', fair), ('liquidation_equity', liquidation)):
                if ((last[key] is None) != (expected is None)
                        or expected is not None and not _same(last[key], expected)):
                    raise ValueError('Latest observation does not match current report values')
        result['risk'], scenarios = actual_risk(raw, frame)
        if history:
            for key in ('residual_delta', 'residual_gamma', 'worst_case_loss'):
                actual, plotted = result['risk'][key], history[-1][key]
                if ((actual is None) != (plotted is None)
                        or actual is not None and not _same(actual, plotted)):
                    raise ValueError('Latest plotted risk differs from actual inventory')
        if set(positions) <= {c['ticker'] for c in frame['contracts']}:
            from .portfolio_payoffs import assess
            result['payoff_assessment'] = assess(lots, frame['scenarios'], exhaustive=True,
                probabilities={row['name']: row['probability'] for row in frame['scenarios']})
        for scenario in scenarios:
            _identifier(scenario.get('name'))
            for key in ('probability', 'payout', 'terminal_wealth', 'total_pnl', 'open_lot_pnl'):
                scenario[key] = _decimal(scenario.get(key), nullable=True)
        result['scenarios'] = scenarios
        result['coverage'].update(telemetry=True,
            history_start=history[0]['observed_at'] if history else None,
            history_points=len(history), omitted_history_points=_count(telemetry.get('omitted_history_points', 0)),
            current_risk_available=result['risk']['residual_delta'] is not None,
            history_basis='producer observations',
            reason='History begins with this producer session; missing marks remain gaps.')
    else:
        result['coverage'].update(reason='Legacy snapshot has no producer history, session identity or current portfolio Greeks.',
                                  history_basis='snapshot only')
        if assessment:
            result['scenarios'] = [dict(name=row['name'], probability=None, payout=row['payout'],
                terminal_wealth=text(cash+number(row['payout'])),
                total_pnl=text(cash+number(row['payout'])-bankroll), open_lot_pnl=row['profit'])
                for row in assessment.get('scenarios', [])]
    result['coverage']['omitted_actions'] = omitted
    result['coverage']['displayed_action_limit'] = 300
    result['coverage']['display_omitted_actions'] = omitted+max(0, len(actions)-300)
    result['coverage']['summary_action_count'] = len(actions)
    result['coverage']['accounting_basis'] = ('Cash and remaining basis reconcile to complete fill and official-outcome history.'
        if omitted == 0 and settlements else 'Cash and remaining basis reconcile to complete fill history.'
        if omitted == 0 else 'Cash and remaining basis reconcile; execution-prefix history is omitted.')
    result['session_resume_supported'] = raw.get('session_resume_supported') is True
    result['limitations'] = [
        'Paper research only; these are not exchange fills or actual account returns.',
        'Model fair value and executable liquidation value are different marks.',
        'Fees are already included in cash and cost basis; they are not subtracted again.',
        'Drawdown uses observed complete visible-liquidation marks; missing depth remains unavailable.',
        ('Session action counters include durable totals; the displayed action table is bounded.' if totals_complete
         else 'Retained action counts exclude omitted execution history.'),
        ('This paper session resumes from its dedicated journal, not from a display report.'
         if result['session_resume_supported'] else 'This paper report does not establish restart recovery support.')]
    if source == 'demo':
        result.update(status='ready', reason='Static synthetic three-frame example; not a live market stream.')
    elif stamp is None:
        result.update(status='unavailable', reason='The paper producer has not observed a compatible contract yet.')
    elif now-stamp > 5 or raw.get('input_status') == 'blocked':
        result.update(status='stale', reason='Last-known paper report; a fresh compatible producer observation is required.')
    else:
        result.update(status='ready', reason='Paper report from the explicitly configured local producer.')
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate neutral report field')
        result[key] = value
    return result


class NeutralMonitor:
    """No account client, arbitrary request paths, order API or background loop."""

    def __init__(self, path=None, *, clock=time.time):
        self.path = Path(path).expanduser().absolute() if path is not None else None
        self.clock = clock
        self._lock = threading.RLock()
        self._demo = None
        if self.path is not None:
            resolved = self.path.resolve()
            root = Path(__file__).resolve().parents[2]
            if (self.path.suffix.lower() != '.json' or resolved == root or root in resolved.parents
                    or any((p/'.git').exists() for p in (resolved.parent, *resolved.parents))):
                raise ValueError('Keep the neutral report JSON outside Git checkouts')

    def read(self, *, source='report'):
        if source not in ('report', 'demo'):
            raise ValueError('Choose report or demo')
        with self._lock:
            try:
                if source == 'demo':
                    if self._demo is None:
                        from .neutral_model import demo_neutral_snapshot
                        from .neutral_paper import NeutralPaperEngine
                        engine, telemetry = NeutralPaperEngine(), NeutralTelemetry()
                        # Stay inside one completed synthetic contract even
                        # when a request arrives at an actual rollover boundary.
                        base = math.floor(self.clock()/900)*900-450
                        for offset in (0, 1, 2):
                            engine.step(demo_neutral_snapshot(now=base+offset), now=base+offset)
                            telemetry.observe(engine.snapshot(), engine._last_frame)
                        self._demo = telemetry.attach(engine.snapshot())
                        self._demo.update(input_source='demo', mode='paper')
                    return normalize_neutral_report(self._demo, source=source, now=self.clock())
                if self.path is None:
                    return unavailable_neutral('No paper report connected. Start the console with --neutral-report pointing to a neutral paper JSON report.')
                with self.path.open('rb') as stream:
                    content = stream.read(MAX_REPORT_BYTES+1)
                if len(content) > MAX_REPORT_BYTES:
                    raise ValueError('Oversized neutral report')
                raw = json.loads(content, object_pairs_hook=_unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite report')))
                return normalize_neutral_report(raw, source=source, now=self.clock())
            except (ValueError, TypeError, KeyError, ArithmeticError, OSError, UnicodeError, RecursionError):
                return unavailable_neutral(
                    'The synthetic paper example is unavailable; check the prototype model/planner compatibility.'
                    if source == 'demo' else 'The configured neutral paper report is missing, invalid or unavailable.',
                    source=source)
