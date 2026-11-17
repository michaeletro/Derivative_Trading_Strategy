"""Bounded source-context qualification without inventing portfolio returns."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone

from .crypto_assets import ASSETS
from .scheduled_research import MAX_EVENTS, ScheduledResearchError, _native, validate_records


OBSERVATIONS = ('kalshi_scheduled_observation', 'kalshi_value_observation')
BOOKS = ('kalshi_scheduled_book', 'kalshi_value_book')
BOOK_GAPS = ('kalshi_scheduled_book_gap', 'kalshi_value_book_gap')
SETTLEMENTS = ('kalshi_scheduled_settlement', 'kalshi_value_settlement')


def qualify_pacing_records(records, *, assets):
    """Report native context availability; this is not an execution backtest.

    Missing fee rounding gets an explicitly counted structural placeholder
    only for schema checks. Original rows are never modified or promoted.
    """
    if (not isinstance(records, (list, tuple)) or not 1 <= len(records) <= MAX_EVENTS
            or not isinstance(assets, (list, tuple)) or not assets or len(set(assets)) != len(assets)
            or not set(assets) <= set(ASSETS)):
        raise ScheduledResearchError('A bounded recorded dataset and distinct supported assets are required')
    groups = {asset:dict(kinds=Counter(), failures=Counter(), receipts=[],
        markets=set(), invalid_markets=set(), days=set(), rounding_missing=0,
            qualified_forecasts=0, qualified_books=0, qualified_gaps=0, qualified_settlements=0,
        models=set()) for asset in assets}
    for saved in records:
        if not isinstance(saved, dict) or saved.get('asset') not in groups:
            raise ScheduledResearchError('Every qualification event must bind a selected asset')
        asset, kind = saved['asset'], saved.get('kind')
        if kind not in OBSERVATIONS + BOOKS + BOOK_GAPS + SETTLEMENTS:
            raise ScheduledResearchError('Unsupported pacing qualification event')
        group = groups[asset]
        group['kinds'][kind] += 1
        row = deepcopy(saved)
        if kind in OBSERVATIONS + BOOKS and row.get('fee_quantum') is None:
            group['rounding_missing'] += 1
            row['fee_quantum'] = '.0001'
        if kind in OBSERVATIONS:
            group['markets'].add(row.get('ticker'))
            group['models'].add((row.get('model_version'), row.get('model_fingerprint')))
        try:
            validated = validate_records([row], allow_receipt_revisions=True,
                allow_scenario_fees=True, allow_book_events=True)[0]
            stamp = validated['_time']
            group['receipts'].append(stamp)
            group['days'].add(datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d'))
            if kind in OBSERVATIONS:
                _native(validated)
                group['qualified_forecasts'] += 1
            elif kind in BOOKS:
                group['qualified_books'] += 1
            elif kind in BOOK_GAPS:
                group['qualified_gaps'] += 1
            else:
                group['qualified_settlements'] += 1
        except (ValueError, TypeError, KeyError, IndexError) as error:
            reason = str(error) if isinstance(error, ScheduledResearchError) else 'Invalid native event context'
            group['failures'][reason] += 1
            if kind in OBSERVATIONS:
                group['invalid_markets'].add(row.get('ticker'))
    output = {}
    for asset, group in groups.items():
        receipts = group['receipts']
        output[asset] = dict(event_counts=dict(group['kinds']),
            forecast_count=sum(group['kinds'][name] for name in OBSERVATIONS),
            book_only_count=sum(group['kinds'][name] for name in BOOKS),
            book_gap_count=sum(group['kinds'][name] for name in BOOK_GAPS),
            settlement_receipt_count=sum(group['kinds'][name] for name in SETTLEMENTS),
            forecast_native_contexts_valid=group['qualified_forecasts'],
            valid_book_contexts=group['qualified_books'], valid_settlement_contexts=group['qualified_settlements'],
            valid_gap_contexts=group['qualified_gaps'],
            native_context_failures=dict(group['failures']),
            forecast_contract_count=len(group['markets']),
            contracts_with_invalid_native_context=len(group['invalid_markets']),
            contracts_with_all_native_contexts=len(group['markets']-group['invalid_markets']),
            fee_rounding_missing_events=group['rounding_missing'],
            model_cohort_count=len(group['models']),
            first_valid_receipt=min(receipts, default=None), last_valid_receipt=max(receipts, default=None),
            valid_receipt_utc_days=sorted(group['days']))
    total_failures = sum(sum(group['failures'].values()) for group in groups.values())
    gaps = sum(sum(group['kinds'][name] for name in BOOK_GAPS) for group in groups.values())
    return dict(kind='kalshi_pacing_data_qualification', version=1,
        status='context_checks_passed' if not total_failures and not gaps else 'partial',
        research_only=True, execution_eligible=False, live_model_updated=False,
        synthetic=any(row.get('synthetic') is True for row in records),
        event_count=len(records), assets=output, native_context_failure_count=total_failures,
        book_gap_event_count=gaps,
        performance_evaluated=False, sharpe=None,
        scope='per_event_native_context_checks_with_missing_fee_rounding_explicitly_counted',
        assumptions=['No event, forecast, label or source clock is changed by qualification.',
            'Missing fee rounding uses a counted schema placeholder only; it is not recovered venue evidence.',
            'Native source context is checked event by event; regular-return coverage requires an actual replay.',
            'Book-only events contain no forecast or decision authority.',
            'Context completeness alone does not establish fills, complete valuations, sufficient days or Sharpe.'])
