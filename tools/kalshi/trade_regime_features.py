"""Past-only entry context from saved, per-order intent snapshots.

These descriptive labels do not certify a policy configuration, a live receipt
replay or a profitable regime. Current report-level settings and latest market
forecasts are deliberately ignored when labeling older orders.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

MAX_JSON_BYTES = 32_000_000
MAX_REPORTS = 128
MAX_INTENTS = 100_000
MAX_FEATURE_AGE_SECONDS = 30.0
_TRENDS = frozenset(('bullish', 'bearish', 'ranging', 'transition'))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _time(value):
    number = _number(value)
    if number is not None:
        return number if number >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        number = parsed.timestamp()
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= 1 else None
    except (InvalidOperation, ValueError):
        return None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _read_report(value):
    if isinstance(value, dict):
        return value, hashlib.sha256(_canonical(value).encode()).hexdigest()
    with Path(value).expanduser().open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('status_report_exceeds_bound')
    report = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(report, dict):
        raise ValueError('status_report_not_object')
    return report, hashlib.sha256(raw).hexdigest()


def build_entry_feature_index(status_report_paths):
    """Index original intent facts from explicit read-only JSON status inputs.

    Inputs may be paths or already-decoded report dictionaries. Duplicate
    snapshots contribute evidence once; absent old fields do not contradict a
    later snapshot. Conflicting non-null facts remain conflicting, not selected
    by filename, publication time or present-day runner settings.
    """
    inputs = ([status_report_paths] if isinstance(status_report_paths, (str, Path, dict))
              else list(status_report_paths))
    if len(inputs) > MAX_REPORTS:
        raise ValueError('Too many explicit status reports')
    entries, issues, seen, scanned = {}, Counter(), set(), 0
    for value in inputs:
        try:
            report, digest = _read_report(value)
            if digest in seen:
                continue
            seen.add(digest)
            intents = report.get('journal', {}).get('intents')
            if not isinstance(intents, list) or len(intents) > MAX_INTENTS:
                raise ValueError('status_intents_missing_or_oversized')
            for intent in intents:
                scanned += 1
                if not isinstance(intent, dict):
                    issues['invalid_intent'] += 1
                    continue
                client, ticker = intent.get('client_order_id'), intent.get('ticker')
                if (not isinstance(client, str) or not client or len(client) > 256
                        or not isinstance(ticker, str) or not ticker or len(ticker) > 256):
                    issues['missing_exact_order_identity'] += 1
                    continue
                sizing = intent.get('sizing')
                if not isinstance(sizing, dict):
                    continue
                row = dict(created_at=_time(intent.get('created_at')), outcome=intent.get('outcome'))
                if row['created_at'] is None or row['outcome'] not in ('yes', 'no'):
                    issues['invalid_intent_clock_or_side'] += 1
                    continue
                row['probability_yes'] = _number(sizing.get('probability_yes'))
                row['sizing_outcome'] = sizing.get('outcome') or sizing.get('execution_outcome')
                row['regime'] = sizing.get('regime')
                row['regime_features'] = sizing.get('regime_features')
                row['regime_locked_at'] = _time(sizing.get('regime_locked_at'))
                row['quote'] = sizing.get('entry_quote_check')
                timing = sizing.get('decision_timing')
                row['seconds_remaining'] = _number(timing.get('seconds_remaining')) if isinstance(timing, dict) else None
                key = (client, ticker)
                record = entries.setdefault(key, {})
                signature = _canonical(row)
                record.setdefault(signature, row)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            issues['status_input_unavailable_or_invalid'] += 1
    return dict(kind='kalshi_entry_feature_index', version=1, entries=entries,
                maximum_feature_age_seconds=MAX_FEATURE_AGE_SECONDS,
                coverage=dict(explicit_input_count=len(inputs), distinct_input_count=len(seen),
                              scanned_intent_count=scanned, indexed_order_count=len(entries),
                              issues=dict(issues)))


def _fact(rows, name):
    facts = {_canonical(row[name]): row[name] for row in rows if row.get(name) is not None}
    return next(iter(facts.values())) if len(facts) == 1 else None


def _missing(result, factor, reason):
    result['factor_status'][factor] = dict(status='unavailable', reason=reason)


def join_entry_features(record, index):
    """Join exact first-entry facts, with every clock bounded by the intent.

    The independent unit remains the completed market. A first-entry feature
    does not attribute all subsequent trades to that entry or that strategy.
    Thirty seconds is this study's entry-context age bound, not proof of the
    original runner's five-second live freshness or a threshold fitted to P&L.
    """
    result = dict(trend_regime='unknown', volatility_regime='unknown',
                  spread_regime='unknown', selected_confidence_band='unknown',
                  projected_volatility_15m=None, direction_score=None,
                  directional_efficiency=None, recorded_spread=None,
                  selected_probability=None, intent_seconds_remaining=None,
                  source_age_at_first_fill_seconds=None, quote_age_at_first_fill_seconds=None,
                  regime_locked_at=None, regime_source_time=None, latest_regime_receipt=None,
                  feature_origin='saved_per_order_intent',
                  label_config_verified=False, source_identity_verified=False,
                  factor_status={})
    for factor in ('trend_regime', 'volatility_regime', 'spread_regime', 'selected_confidence_band'):
        _missing(result, factor, 'exact_first_entry_snapshot_missing')
    client, ticker = record.get('first_client_order_id'), record.get('ticker')
    first_fill, outcome = _time(record.get('first_fill_at')), record.get('first_outcome')
    if (not client or not ticker or first_fill is None or outcome not in ('yes', 'no')
            or record.get('first_entry_ambiguous') is True):
        return result
    rows = list(index.get('entries', {}).get((client, ticker), {}).values())
    if not rows:
        return result
    created, side = _fact(rows, 'created_at'), _fact(rows, 'outcome')
    if created is None or side != outcome or created > first_fill:
        for factor in result['factor_status']:
            _missing(result, factor, 'conflicting_or_future_intent_identity')
        return result
    result['intent_created_at'] = created
    result['intent_seconds_remaining'] = _fact(rows, 'seconds_remaining')
    if result['intent_seconds_remaining'] is not None and not 0 <= result['intent_seconds_remaining'] <= 900:
        result['intent_seconds_remaining'] = None

    # The original regime is a recorded contract-lock label. Its missing
    # historical configuration is explicit, never filled from current defaults.
    regime, features, locked = (_fact(rows, name) for name in
                                ('regime', 'regime_features', 'regime_locked_at'))
    if regime in _TRENDS and isinstance(features, dict) and locked is not None:
        start, end, receipt = (_time(features.get(name)) for name in
                               ('history_start', 'history_end', 'latest_received_at'))
        vol, score, efficiency = (_number(features.get(name)) for name in
                                 ('projected_volatility', 'direction_score', 'directional_efficiency'))
        valid = (None not in (start, end, receipt, vol, score, efficiency)
                 and start < end <= receipt <= locked <= created <= first_fill
                 and 0 < vol <= 1 and 0 <= efficiency <= 1
                 and 0 <= first_fill - end <= MAX_FEATURE_AGE_SECONDS
                 and not (regime == 'bullish' and score <= 0)
                 and not (regime == 'bearish' and score >= 0))
        if valid:
            result.update(trend_regime=regime,
                          volatility_regime='low' if vol <= 0.0025 else ('normal' if vol <= 0.005 else 'high'),
                          projected_volatility_15m=vol, direction_score=score,
                          directional_efficiency=efficiency,
                          source_age_at_first_fill_seconds=first_fill-end,
                          regime_locked_at=locked, regime_source_time=end, latest_regime_receipt=receipt)
            for factor in ('trend_regime', 'volatility_regime'):
                result['factor_status'][factor] = dict(status='available', origin='recorded_contract_lock',
                                                      historical_threshold_config_verified=False)
        else:
            for factor in ('trend_regime', 'volatility_regime'):
                _missing(result, factor, 'regime_clock_feature_or_age_not_verified')
    elif any(row.get('regime') is not None for row in rows):
        for factor in ('trend_regime', 'volatility_regime'):
            _missing(result, factor, 'conflicting_or_incomplete_regime_snapshot')

    quote = _fact(rows, 'quote')
    if isinstance(quote, dict) and quote.get('status') == 'ready':
        begin, completed = (_time(quote.get(name)) for name in ('refresh_started_at', 'refresh_completed_at'))
        bid, ask = (_price(quote.get(name)) for name in ('refreshed_best_bid', 'refreshed_best_ask'))
        clock_valid = (None not in (begin, completed) and begin <= completed <= created
                       and 0 <= first_fill-completed <= MAX_FEATURE_AGE_SECONDS)
        quote_valid = clock_valid and None not in (bid, ask) and bid < ask
        if quote_valid:
            spread = ask-bid
            result.update(recorded_spread=format(spread, 'f'),
                          spread_regime='narrow_le_2c' if spread <= Decimal('0.02') else 'wide_gt_2c',
                          quote_age_at_first_fill_seconds=first_fill-completed)
            result['factor_status']['spread_regime'] = dict(status='available', origin='recorded_final_entry_book')
        else:
            _missing(result, 'spread_regime', 'quote_clock_price_or_age_not_verified')
        probability = _fact(rows, 'probability_yes')
        sizing_side = _fact(rows, 'sizing_outcome')
        model_age = _number(quote.get('source_age_seconds'))
        if (clock_valid and probability is not None and 0 <= probability <= 1
                and sizing_side in (None, outcome) and model_age is not None and model_age >= 0
                and completed-model_age >= 0
                and 0 <= first_fill-(completed-model_age) <= MAX_FEATURE_AGE_SECONDS):
            selected = probability if outcome == 'yes' else 1-probability
            band = ('below_0.5' if selected < .5 else '0.5_to_0.6' if selected < .6
                    else '0.6_to_0.8' if selected < .8 else 'at_least_0.8')
            result.update(selected_probability=selected, selected_confidence_band=band)
            result['factor_status']['selected_confidence_band'] = dict(status='available', origin='recorded_intent_forecast')
        else:
            _missing(result, 'selected_confidence_band', 'forecast_side_clock_or_probability_not_verified')
    elif any(row.get('quote') is not None for row in rows):
        for factor in ('spread_regime', 'selected_confidence_band'):
            _missing(result, factor, 'conflicting_or_incomplete_entry_quote')
    return result
