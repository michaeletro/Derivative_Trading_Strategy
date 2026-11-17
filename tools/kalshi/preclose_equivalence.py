"""Numerical equivalence of retrospective preclose features and runtime math.

This audit re-reads complete native second grids and retains the true later
acquisition receipts. Only pure numerical helpers are called: it makes no claim
that a historical download was available to a live forecaster in the past.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from .crypto_assets import asset_spec
from .forecast import ForecastConfig, IndexObservation, _volatility
from .historical_settlement_payoff import historical_rounded_payoff
from .history_features import _SecondGrid
from .preclose_history import preclose_rows_from_history, predict_preclose_gaussian
from .recency_calibration import _qualify
from .stochastic_features import _read_only


class PrecloseEquivalenceError(ValueError):
    """A native source-time numerical audit cannot establish equivalence."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _native_points(grid, model_time, asset):
    """Read full prices and attach actual acquisition receipts without changes."""
    profile = asset_spec(asset)
    raw = grid.window(model_time, 1800)
    points = []
    for point in raw:
        hour = math.floor(point.event_time / 3600) * 3600
        receipts = []
        for candidate in (hour, hour - 3600):
            value = grid._load(candidate).get(int(point.event_time))
            acquisition = grid.acquisitions.get(candidate)
            if (value is not None and float(value) == point.price and acquisition is not None
                    and acquisition['status'] != 'error'):
                receipt = acquisition.get('received_at')
                if (type(receipt) not in (int, float) or not math.isfinite(receipt)
                        or receipt < point.event_time):
                    raise PrecloseEquivalenceError('Native acquisition receipts must retain their actual later clocks')
                receipts.append(receipt)
        if not receipts:
            raise PrecloseEquivalenceError('A source point lacks an original acquisition receipt')
        points.append(IndexObservation(point.event_time, min(receipts), point.price, profile.source))
    if len(points) != 1801 or points[-1].event_time != model_time:
        raise PrecloseEquivalenceError('Every audit state needs a complete 1801-point native grid')
    return points


def verify_preclose_equivalence(bindings, *, feature_rows=None, max_rows=200000):
    """Recompute all supplied states from immutable native source-time data.

    ``feature_rows`` accepts the original feature envelope or its row list.
    ``data_fingerprint`` in each asset result is the same qualified row hash used
    by a single-asset recency report, so evidence cannot silently move cohorts.
    """
    if (not isinstance(bindings, dict) or not 1 <= len(bindings) <= 4
            or type(max_rows) is not int or not 1 <= max_rows <= 200000):
        raise PrecloseEquivalenceError('Supply bounded explicit native history archive bindings')
    if isinstance(feature_rows, dict):
        if feature_rows.get('synthetic') is not False:
            raise PrecloseEquivalenceError('Equivalence requires a native historical feature envelope')
        feature_rows = feature_rows.get('rows')
    if feature_rows is not None and (not isinstance(feature_rows, (list, tuple))
            or not feature_rows or len(feature_rows) > max_rows):
        raise PrecloseEquivalenceError('Historical feature rows must form a nonempty bounded sequence')
    from .preclose_paper_probability import preclose_gaussian_state, preclose_runtime_definition
    input_hash = None if feature_rows is None else _hash(feature_rows)
    options = {}
    if feature_rows is not None:
        horizons = set()
        for row in feature_rows:
            if not isinstance(row, dict):
                raise PrecloseEquivalenceError('Historical feature rows must be objects')
            closed, clock = row.get('close_time'), row.get('as_of')
            if (type(closed) not in (int, float) or type(clock) not in (int, float)
                    or not math.isfinite(closed) or not math.isfinite(clock)
                    or not 1 <= closed-clock <= 900 or closed-clock != int(closed-clock)):
                raise PrecloseEquivalenceError('Feature horizons must be native whole seconds inside a contract')
            horizons.add(int(closed-clock))
        options['horizons'] = tuple(sorted(horizons, reverse=True))
    dataset = preclose_rows_from_history(bindings, feature_rows=feature_rows, max_rows=max_rows, **options)
    report = dict(kind='kalshi_preclose_runtime_equivalence_report', version=1,
        research_only=True, execution_eligible=False, live_model_updated=False,
        historical_source_time_only=True, historical_live_receipt_availability_verified=False,
        acquisition_receipts_modified=False, pure_numerical_helpers_only=True,
        historical_input_rows_sha256=input_hash, assets={},
        limitations=[
            'Later native download receipts are retained; numerical equivalence does not establish past live availability.',
            'Full native grids and original market terms establish the audited reconstruction, not absence of upstream revisions.',
            'The audit establishes numerical equivalence on supplied states and does not establish predictive improvement.'])
    config = ForecastConfig(ewma_half_life_seconds=300, max_history_seconds=1800)
    for asset, archive in bindings.items():
        profile = asset_spec(asset)
        own, withheld = _qualify(dataset, (asset,))
        own = [row for row in own if row['asset'] == asset]
        identities = {row['model_identity'] for row in own}
        if len(identities) != 1:
            raise PrecloseEquivalenceError('Each asset needs one nonempty original preclose model cohort')
        requested = len([row for row in feature_rows or own if row.get('asset') == asset])
        errors = Counter()
        max_sigma_error = max_probability_error = max_variance_error = 0.0
        maximum_receipt = 0.0
        minimum_receipt_after_model = math.inf
        grid_hash = hashlib.sha256()
        tested = 0
        with _read_only(Path(archive), 'history') as (db, location):
            grid = _SecondGrid(db, profile.index_id,
                min(row['model_time'] for row in own) - 1800,
                max(row['model_time'] for row in own))
            for row in own:
                try:
                    points = _native_points(grid, row['model_time'], asset)
                    sigma, _, _ = _volatility(points, row['model_time'], config, asset=asset)
                    original = predict_preclose_gaussian(row)
                    payoff = historical_rounded_payoff(row)
                    state = preclose_gaussian_state(row['spot'], sigma, row['known_sum'],
                        row['future_offsets'], payoff)
                    sigma_error = abs(sigma - row['slow_sigma'])
                    probability_error = abs(state['probability_yes'] - original['probability_yes'])
                    variance_error = abs(state['variance'] - original['variance'])
                    max_sigma_error = max(max_sigma_error, sigma_error)
                    max_probability_error = max(max_probability_error, probability_error)
                    max_variance_error = max(max_variance_error, variance_error)
                    if sigma_error > 1e-15:
                        errors['volatility_feature_mismatch'] += 1
                    if probability_error > 1e-12:
                        errors['probability_mismatch'] += 1
                    if (state['known_count'] != row['known_count']
                            or state['future_count'] != len(row['future_offsets'])
                            or state['covariance_time_sum'] != original['covariance_time_sum']):
                        errors['settlement_state_mismatch'] += 1
                    # Hash every actual source point and receipt in state order;
                    # this does not relabel receipts as old operational clocks.
                    grid_hash.update(json.dumps([[p.event_time, p.received_at, p.price] for p in points],
                        separators=(',', ':'), allow_nan=False).encode())
                    maximum_receipt = max(maximum_receipt, max(p.received_at for p in points))
                    minimum_receipt_after_model = min(minimum_receipt_after_model,
                        min(p.received_at for p in points) - row['model_time'])
                    tested += 1
                except (ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError) as error:
                    errors['native_state_unavailable:' + str(error)[:160]] += 1
            acquisitions = sorted(grid.acquisitions.values(), key=lambda item: item['id'])
        complete = tested == len(own) == requested and not withheld
        result = dict(asset=asset, status='equivalent' if complete and not errors else 'mismatch',
            historical_model_identity=next(iter(identities)),
            runtime_model_sha256=_hash(preclose_runtime_definition(asset)),
            data_fingerprint=_hash(own), historical_input_rows_sha256=input_hash,
            requested_states=requested, qualified_states=len(own), tested_states=tested,
            tested_contracts=len({row['ticker'] for row in own}),
            withheld_rows=withheld, errors=dict(errors),
            max_volatility_abs_error=max_sigma_error, max_probability_abs_error=max_probability_error,
            max_variance_abs_error=max_variance_error,
            full_native_grid_sequence_sha256=grid_hash.hexdigest(),
            acquisition_metadata_sha256=_hash(acquisitions),
            acquisitions=len(acquisitions), actual_latest_acquisition_receipt=maximum_receipt,
            minimum_actual_acquisition_receipt_minus_model_time=(
                None if not tested else minimum_receipt_after_model),
            acquisition_receipts_modified=False, source_time_math_only=True,
            settlement_grid_equivalent=complete and not errors.get('settlement_state_mismatch')
                and not any(key.startswith('native_state_unavailable:') for key in errors),
            rounded_payoff_equivalent=complete and not errors.get('probability_mismatch')
                and not any(key.startswith('native_state_unavailable:') for key in errors),
            volatility_feature_equivalent=complete and not errors.get('volatility_feature_mismatch')
                and not any(key.startswith('native_state_unavailable:') for key in errors))
        report['assets'][asset] = result
    report['status'] = 'equivalent' if all(item['status'] == 'equivalent'
        for item in report['assets'].values()) else 'mismatch'
    report['report_sha256'] = _hash(report)
    return report


def per_asset_equivalence(report):
    """Return export-binding blocks only from a complete, unmodified audit."""
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_preclose_runtime_equivalence_report'
            or type(report.get('version')) is not int or report['version'] != 1
            or report.get('status') != 'equivalent' or report.get('execution_eligible') is not False
            or report.get('research_only') is not True
            or report.get('acquisition_receipts_modified') is not False
            or report.get('historical_live_receipt_availability_verified') is not False
            or report.get('historical_source_time_only') is not True
            or report.get('pure_numerical_helpers_only') is not True
            or report.get('report_sha256') != _hash({key: value for key, value in report.items() if key != 'report_sha256'})
            or not isinstance(report.get('assets'), dict) or not report['assets']):
        raise PrecloseEquivalenceError('Only complete unchanged native numerical-equivalence evidence can bind an export')
    blocks = {}
    for asset, entry in report['assets'].items():
        if (entry.get('status') != 'equivalent' or type(entry.get('tested_states')) is not int
                or entry['tested_states'] <= 0
                or type(entry.get('actual_latest_acquisition_receipt')) not in (int, float)
                or not math.isfinite(entry['actual_latest_acquisition_receipt'])
                or entry['actual_latest_acquisition_receipt'] <= 0
                or any(entry.get(name) is not True for name in ('settlement_grid_equivalent',
                    'rounded_payoff_equivalent', 'volatility_feature_equivalent'))):
            raise PrecloseEquivalenceError('An asset lacks complete exact-model equivalence')
        blocks[asset] = {key: entry[key] for key in ('historical_model_identity', 'runtime_model_sha256',
            'data_fingerprint', 'historical_input_rows_sha256', 'tested_states', 'tested_contracts',
            'actual_latest_acquisition_receipt',
            'settlement_grid_equivalent', 'rounded_payoff_equivalent', 'volatility_feature_equivalent',
            'max_probability_abs_error', 'max_volatility_abs_error')}
        blocks[asset]['evidence_sha256'] = report['report_sha256']
    return blocks
