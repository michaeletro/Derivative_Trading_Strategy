"""Read-only native crypto minute returns and bound preclose fixing states.

Historical acquisition receipts remain their true later download clocks.  This
module reconstructs source-time numerical states, not past live availability.
Minute prices are exact UTC endpoints, never averages or interpolated values.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

from .crypto_assets import asset_spec
from .forecast import _timestamp
from .history_features import FeatureError, _SecondGrid
from .preclose_history import preclose_rows_from_history, predict_preclose_gaussian
from .stochastic_features import _read_only


ASSET = 'ETH'
SOURCE = asset_spec(ASSET).source
HORIZONS = (900, 600, 300, 180, 120, 60, 30)
FIVE_SECOND_RETURN_COLUMNS = (
    'end_time', 'value', 'segment_id', 'contiguous_returns', 'received_at',
)
CURRENT_MINUTE_OBSERVATION_COLUMNS = ('event_time', 'price', 'received_at')


class GarchDataError(ValueError):
    """A native source grid, forecast state or archived feature is invalid."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _decimal(value, *, asset=ASSET):
    try:
        if type(value) not in (int, float, str, Decimal):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= 100000000:
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise GarchDataError(f'A native {asset} price must be finite and positive') from None


def _source_snapshot(db, *, asset=ASSET):
    profile = asset_spec(asset)
    rows = [dict(row) for row in db.execute('''SELECT a.id,a.hour,a.status,
        a.started_at,a.received_at,a.payload_sha256 FROM history_acquisitions a
        JOIN (SELECT hour,MAX(id) latest FROM history_acquisitions
              WHERE index_id=? GROUP BY hour) b ON a.id=b.latest
        ORDER BY a.hour''', (profile.index_id,))]
    if not rows or len(rows) > 744:
        raise GarchDataError(f'{asset} needs one through 744 original hourly acquisitions')
    clocks = [_timestamp(row['hour']) for row in rows]
    if (any(value != int(value) or value % 3600 for value in clocks)
            or max(clocks)-min(clocks) > 31*86400):
        raise GarchDataError(f'{asset} acquisitions exceed the native thirty-one-day bound')
    for row in rows:
        if (type(row['id']) is not int or row['id'] <= 0
                or not _finite(row['started_at']) or not _finite(row['received_at'])
                or not 0 < row['started_at'] <= row['received_at']
                or not isinstance(row['status'], str) or not row['status']
                or row['status'] != 'error' and (not isinstance(row['payload_sha256'], str)
                    or len(row['payload_sha256']) != 64)):
            raise GarchDataError(f'Native {asset} acquisition metadata is invalid')
    document = db.execute('''SELECT id,received_at,payload_sha256
        FROM history_documents WHERE kind='kalshi_contracts'
        AND item_key LIKE ? ORDER BY id DESC LIMIT 1''', (asset + ':%',)).fetchone()
    identity = dict(acquisitions=rows,
        latest_contract_document=None if document is None else dict(document))
    return rows, clocks, _hash(identity)


def minute_price_series(history_archive, *, asset=ASSET):
    """Return exact minute prices/returns and compact native five-second returns.

    Five-second return tuples have the explicitly returned column schema.
    Gaps reset a segment: neither series ever bridges a missing endpoint.
    Equal adjacent-hour duplicates retain all actual acquisition receipts;
    conflicting, repeated, amended and failed-owner observations are withheld.
    """
    profile = asset_spec(asset)
    with _read_only(Path(history_archive), 'history') as (db, location):
        acquisitions, clocks, snapshot = _source_snapshot(db, asset=asset)
        selected = _SecondGrid(db, profile.index_id, min(clocks), max(clocks))
        frozen = selected.acquisitions
        by_id = {row['id']: row for row in frozen.values()}
        if set(by_id) != {row['id'] for row in acquisitions}:
            raise GarchDataError(f'The native {asset} acquisition selection changed')
        # A failed acquisition must not contribute residual normalized ticks,
        # including its final boundary where no newer owner hour exists.
        identifiers = sorted(key for key, row in by_id.items() if row['status'] != 'error')
        marks = ','.join('?' for _ in identifiers) if identifiers else '-1'
        query = '''SELECT acquisition_id,source_time_ms,value,repeated,amendment
            FROM history_ticks WHERE acquisition_id IN ('''+marks+''')
            AND source_time_ms % 5000=0 ORDER BY source_time_ms,acquisition_id'''
        prices, returns, five_returns = [], [], []
        reasons, minute_reasons = Counter(), Counter()
        previous_five = previous_minute = None
        five_segment = minute_segment = five_count = minute_count = 0
        valid_five = raw_rows = duplicate_rows = 0
        series_digest = hashlib.sha256()
        first_price = last_price = None

        def consume(stamp, variants):
            nonlocal previous_five, previous_minute
            nonlocal five_segment, minute_segment, five_count, minute_count
            nonlocal valid_five, duplicate_rows, first_price, last_price
            if stamp is None:
                return
            duplicate_rows += max(0, len(variants)-1)
            owner = frozen.get(math.floor(stamp/3600)*3600)
            values = [_decimal(item['value'], asset=asset) for item in variants]
            reason = None
            if owner is not None and owner['status'] == 'error':
                reason = 'latest_hour_acquisition_failed'
            elif any(item['repeated'] or item['amendment'] is not None for item in variants):
                reason = 'flagged_or_amended_native_tick'
            elif len(set(values)) != 1:
                reason = 'conflicting_adjacent_hour_native_tick'
            if reason is not None:
                reasons[reason] += 1
                previous_five, five_count = None, 0
                if stamp % 60 == 0:
                    minute_reasons[reason] += 1
                    previous_minute, minute_count = None, 0
                return
            price = values[0]
            receipts = [by_id[item['acquisition_id']]['received_at'] for item in variants]
            received = max(receipts)
            if received < stamp:
                raise GarchDataError(f'A historical acquisition predates its contained {asset} price')
            ids = sorted({item['acquisition_id'] for item in variants})
            native = (stamp, str(price), received, ids)
            series_digest.update(json.dumps(native, separators=(',', ':'), allow_nan=False).encode())
            series_digest.update(b'\n')
            valid_five += 1
            first_price = stamp if first_price is None else first_price
            last_price = stamp
            if previous_five is None or stamp-previous_five[0] != 5:
                five_segment += 1
                five_count = 0
            else:
                five_count += 1
                value = math.log(float(price)/float(previous_five[1]))
                five_returns.append((stamp, value, five_segment, five_count,
                                     max(received, previous_five[2])))
            previous_five = (stamp, price, received)
            if stamp % 60:
                return
            point = dict(time=stamp, price=str(price), received_at=received,
                         acquisition_ids=ids)
            prices.append(point)
            if previous_minute is None or stamp-previous_minute['time'] != 60:
                minute_segment += 1
                minute_count = 0
            else:
                minute_count += 1
                value = math.log(float(price)/float(previous_minute['price']))
                returns.append(dict(start_time=previous_minute['time'], end_time=stamp,
                    value=value, segment_id=minute_segment,
                    contiguous_returns=minute_count, start_price=previous_minute['price'],
                    end_price=str(price), received_at=max(received, previous_minute['received_at'])))
            previous_minute = point

        stamp, variants = None, []
        for saved in db.execute(query, identifiers):
            item = dict(saved)
            event = item['source_time_ms']
            if type(event) is not int or event % 5000:
                raise GarchDataError('A native five-second endpoint is invalid')
            native_time = event//1000
            acquisition = by_id[item['acquisition_id']]
            hour = _timestamp(acquisition['hour'])
            if not hour <= native_time <= hour+3600:
                raise GarchDataError(f'A native {asset} endpoint is outside its acquisition hour')
            if native_time != stamp:
                consume(stamp, variants)
                stamp, variants = native_time, []
            variants.append(item)
            raw_rows += 1
        consume(stamp, variants)
        expected_five = int((max(clocks)+3600-min(clocks))/5)+1
        expected_minute = int((max(clocks)+3600-min(clocks))/60)+1
        return dict(kind=('kalshi_eth_minute_source_series' if asset == ASSET
                          else 'kalshi_crypto_minute_source_series'), version=1 if asset == ASSET else 2, asset=asset,
            source=profile.source, minute_prices=prices, minute_returns=returns,
            five_second_returns=five_returns,
            five_second_return_columns=list(FIVE_SECOND_RETURN_COLUMNS),
            coverage=dict(acquisitions=len(acquisitions),
                acquisition_statuses=dict(Counter(row['status'] for row in acquisitions)),
                raw_five_second_rows=raw_rows,
                valid_five_second_prices=valid_five, native_equal_boundary_duplicate_rows=duplicate_rows,
                five_second_returns=len(five_returns), five_second_segments=five_segment,
                expected_five_second_prices=expected_five,
                missing_five_second_prices=expected_five-valid_five-sum(reasons.values()),
                quarantined_five_second_prices=dict(reasons),
                minute_prices=len(prices), minute_returns=len(returns), minute_segments=minute_segment,
                expected_minute_prices=expected_minute,
                missing_minute_prices=expected_minute-len(prices)-sum(minute_reasons.values()),
                quarantined_minute_prices=dict(minute_reasons),
                first_native_price=first_price, last_native_price=last_price),
            provenance=dict(archive_path=str(location), read_only=True, wal_aware=True,
                snapshot='one_sqlite_read_transaction_latest_native_acquisition_per_hour',
                source_snapshot_sha256=snapshot, native_series_sha256=series_digest.hexdigest(),
                selected_acquisitions=acquisitions, historical_source_time_only=True,
                acquisition_receipts_modified=False, historical_live_availability_verified=False,
                prices_are_exact_utc_endpoints=True, interpolation_used=False,
                native_subsecond_prices_averaged=False,
                receipt_basis='maximum_actual_selected_acquisition_receipt_at_equal_boundary_duplicates'),
            research_only=True, execution_eligible=False, live_model_updated=False)


def attach_minute_inputs(rows, series, *, asset=None):
    """Attach only completed-minute/second-return indices, without forecasting."""
    if not isinstance(rows, (tuple, list)) or not isinstance(series, dict):
        raise GarchDataError('Bound preclose rows and a native crypto return series are required')
    selected = series.get('asset', ASSET) if asset is None else asset
    profile = asset_spec(selected)
    if (series.get('asset') != selected or series.get('source') != profile.source
            or series.get('kind') != ('kalshi_eth_minute_source_series' if selected == ASSET
                                      else 'kalshi_crypto_minute_source_series')
            or type(series.get('version')) is not int or series['version'] != (1 if selected == ASSET else 2)):
        raise GarchDataError('The native return series differs from its requested crypto asset')
    prices = {item['time']: item for item in series['minute_prices']}
    minute_returns = {item['end_time']: (index, item)
                      for index, item in enumerate(series['minute_returns'])}
    five_returns = {item[0]: (index, item)
                    for index, item in enumerate(series['five_second_returns'])}
    output = []
    for original in rows:
        row = dict(original)
        clock = row.get('model_time')
        if (row.get('asset') != selected or row.get('source') != profile.source
                or not _finite(clock) or clock != int(clock)):
            raise GarchDataError(f'Every forecast origin must be an exact native {selected} source second')
        boundary = int(clock//60)*60
        minute = prices.get(boundary)
        completed = minute_returns.get(boundary)
        recent_five = five_returns.get(int(clock))
        row.update(minute_start=boundary, minute_start_price=None if minute is None else minute['price'],
            minute_return_index=None if completed is None else completed[0],
            last_completed_minute_return_at=None if completed is None else completed[1]['end_time'],
            contiguous_minute_returns=0 if completed is None else completed[1]['contiguous_returns'],
            five_second_return_index=None if recent_five is None else recent_five[0],
            contiguous_five_second_returns=0 if recent_five is None else recent_five[1][3])
        reason = ('missing_current_minute_start_price' if minute is None else
                  'missing_last_completed_minute_return' if completed is None else
                  'missing_current_five_second_return' if recent_five is None else None)
        if minute is not None:
            spot = _decimal(row.get('spot'), asset=selected)
            if clock == boundary and spot != _decimal(minute['price'], asset=selected):
                raise GarchDataError('A minute-aligned forecast spot differs from the native minute endpoint')
            row['observed_partial_return'] = math.log(float(spot)/float(minute['price']))
        else:
            row['observed_partial_return'] = None
        row['minute_input_status'] = 'ready' if reason is None else reason
        output.append(row)
    return output


def _attach_intraminute_history(rows, db, series, *, asset):
    """Bind exact native prefixes before any label-dependent comparison mask.

    Tick queries are bounded to each prefix, so later seconds cannot affect
    an earlier origin's values or quality mask. An incomplete prefix
    disqualifies every requested horizon of that contract, without inspecting
    the official outcome or the reconstructed final fixing.
    """
    profile = asset_spec(asset)
    acquisitions, clocks, snapshot = _source_snapshot(db, asset=asset)
    if snapshot != series['provenance']['source_snapshot_sha256']:
        raise GarchDataError(f'The historical source view changed during {asset} prefix reconstruction')
    native = _SecondGrid(db, profile.index_id, min(clocks), max(clocks))
    by_id = {item['id']: item for item in native.acquisitions.values()}
    if set(by_id) != {item['id'] for item in acquisitions}:
        raise GarchDataError(f'The native {asset} prefix acquisition selection changed')
    prefixes, used, excluded = {}, {}, set()
    output = []
    digest = hashlib.sha256()
    for original in rows:
        row = dict(original)
        clock = row['model_time']
        boundary = int(clock // 60) * 60
        if clock not in prefixes:
            observations, identifiers, reason = [], set(), 'ready'
            try:
                hour = boundary // 3600 * 3600
                candidates = (hour-3600, hour) if boundary == hour else (hour,)
                selected = {item['id']: item for candidate in candidates
                    if (item := native.acquisitions.get(candidate)) is not None
                    and item['status'] != 'error'}
                marks = ','.join('?' for _ in selected) if selected else '-1'
                observed = {}
                query = '''SELECT acquisition_id,source_time_ms,value,repeated,amendment
                    FROM history_ticks WHERE acquisition_id IN ('''+marks+''')
                    AND source_time_ms>=? AND source_time_ms<=? AND source_time_ms % 1000=0
                    ORDER BY source_time_ms,acquisition_id'''
                for saved in db.execute(query, [*selected, boundary*1000, int(clock)*1000]):
                    event = saved['source_time_ms']
                    acquisition = selected[saved['acquisition_id']]
                    acquisition_hour = _timestamp(acquisition['hour'])
                    if (type(event) is not int
                            or not acquisition_hour*1000 <= event <= (acquisition_hour+3600)*1000):
                        raise GarchDataError('A native intraminute timestamp differs from its acquisition hour')
                    observed.setdefault(event//1000, []).append((dict(saved), acquisition))
                for stamp in range(boundary, int(clock)+1):
                    hour = stamp // 3600 * 3600
                    owner = native.acquisitions.get(hour)
                    if owner is not None and owner['status'] == 'error':
                        raise FeatureError('latest_hour_acquisition_failed')
                    variants = observed.get(stamp, [])
                    if not variants:
                        raise FeatureError('missing_exact_second_tick')
                    if any(item['repeated'] or item['amendment'] is not None for item, _ in variants):
                        raise FeatureError('flagged_or_conflicting_second_tick')
                    try:
                        prices = [_decimal(item['value'], asset=asset) for item, _ in variants]
                    except GarchDataError:
                        raise FeatureError('invalid_exact_second_tick') from None
                    price = prices[0]
                    if any(value != price for value in prices):
                        raise FeatureError('flagged_or_conflicting_second_tick')
                    receipt = max(item['received_at'] for _, item in variants)
                    if receipt < stamp:
                        raise GarchDataError(f'A historical acquisition predates its contained {asset} prefix price')
                    observations.append([stamp, str(price), receipt])
                    for _, item in variants:
                        identifiers.add(item['id'])
                        used[item['id']] = item
            except FeatureError as error:
                reason = str(error)
                observations, identifiers = [], set()
            prefixes[clock] = (observations, sorted(identifiers), reason)
        observations, identifiers, reason = prefixes[clock]
        if reason == 'ready':
            minute_price = row.get('minute_start_price')
            if (minute_price is None or _decimal(observations[0][1], asset=asset)
                    != _decimal(minute_price, asset=asset)
                    or float(observations[-1][1]) != float(row['spot'])):
                raise GarchDataError('The native intraminute prefix differs from its bound forecast endpoints')
        else:
            excluded.add(row['ticker'])
        row.update(current_minute_observations=[list(point) for point in observations],
            current_minute_acquisition_ids=list(identifiers),
            intraminute_source_status=reason, intraminute_input_status=reason)
        output.append(row)
        digest.update(json.dumps([row['ticker'], clock, observations, identifiers, reason],
            separators=(',', ':'), allow_nan=False).encode())
        digest.update(b'\n')
    for row in output:
        if row['ticker'] in excluded and row['intraminute_source_status'] == 'ready':
            row['intraminute_input_status'] = 'contract_intraminute_source_incomplete'
    coverage = dict(rows=len(output), contracts=len({row['ticker'] for row in output}),
        eligible_contracts=len({row['ticker'] for row in output}-excluded),
        excluded_contracts=len(excluded),
        source_status_counts=dict(Counter(row['intraminute_source_status'] for row in output)),
        input_status_counts=dict(Counter(row['intraminute_input_status'] for row in output)),
        contract_source_mask='every_requested_origin_requires_its_complete_exact_native_second_prefix',
        outcomes_used_in_source_mask=False)
    provenance = dict(source_snapshot_sha256=snapshot, native_prefixes_sha256=digest.hexdigest(),
        selected_acquisitions=[used[key] for key in sorted(used)],
        read_only=True, interpolation_used=False, future_source_seconds_used=False,
        actual_acquisition_receipts_retained=True, historical_source_time_only=True,
        historical_live_availability_verified=False,
        receipt_basis='maximum_actual_selected_acquisition_receipt_at_equal_boundary_duplicates')
    return output, coverage, provenance


def build_garch_dataset(history_archive, *, asset=ASSET, feature_rows=None, max_rows=200000,
                        include_intraminute_history=False):
    """Build common official-label geometry plus native minute/five-second data.

    No GARCH or calibration coefficient is fitted here. Input gaps remain
    visible; the caller applies its predeclared whole-contract warm-up mask.
    """
    profile = asset_spec(asset)
    if type(include_intraminute_history) is not bool:
        raise GarchDataError('Intraminute history must be explicitly enabled or disabled')
    if type(max_rows) is not int or not 1 <= max_rows <= 200000:
        raise GarchDataError(f'Use a positive {asset} source-state bound up to 200000')
    if isinstance(feature_rows, dict):
        if feature_rows.get('synthetic') is not False:
            raise GarchDataError('A feature envelope must explicitly contain real historical data')
        feature_rows = feature_rows.get('rows')
    if feature_rows is not None and (not isinstance(feature_rows, (tuple, list))
            or len(feature_rows) > max_rows):
        raise GarchDataError('Historical feature rows exceed their supported bound')
    if feature_rows is not None and any(not isinstance(row, dict) or row.get('asset') != asset
            or row.get('source') != profile.source for row in feature_rows):
        raise GarchDataError('Historical feature rows differ from the requested asset and native benchmark')
    series = minute_price_series(history_archive, asset=asset)
    prepared = preclose_rows_from_history({asset: Path(history_archive)},
        feature_rows=feature_rows, horizons=HORIZONS, max_rows=max_rows)
    rows = []
    for original in prepared['rows']:
        row = dict(original)
        if (row.get('asset') != asset or row.get('source') != profile.source
                or row.get('payoff', {}).get('round_digits') != (2 if asset in ('BTC', 'ETH') else 4)):
            raise GarchDataError('Preclose source or native rounding differs from the requested crypto asset')
        prediction = predict_preclose_gaussian(row)
        row['effective_strike'] = row['payoff']['effective_strike']
        row['payoff_round_digits'] = row['payoff']['round_digits']
        row['p_ewma_legacy_reference'] = prediction['probability_yes']
        # Future continuous targets are irrelevant to inference and are excluded
        # from this dataset rather than left beside causal model inputs.
        for field in ('realized_average', 'target_available_at', 'training_available_at',
                      'reconstructed_preclose_average', 'source_grid_outcome_consistency'):
            row.pop(field, None)
        rows.append(row)
    rows = attach_minute_inputs(rows, series, asset=asset)
    with _read_only(Path(history_archive), 'history') as (db, _):
        if include_intraminute_history:
            rows, intraminute_coverage, intraminute_provenance = _attach_intraminute_history(
                rows, db, series, asset=asset)
        _, _, final_snapshot = _source_snapshot(db, asset=asset)
    if final_snapshot != series['provenance']['source_snapshot_sha256']:
        raise GarchDataError(f'The historical source view changed during {asset} feature reconstruction')
    statuses = Counter(row['minute_input_status'] for row in rows)
    result = dict(kind=('kalshi_eth_garch_dataset' if asset == ASSET else 'kalshi_crypto_garch_dataset'),
        version=1 if asset == ASSET else 2, asset=asset, source=profile.source,
        synthetic=False, rows=rows, minute_prices=series['minute_prices'],
        minute_returns=series['minute_returns'], five_second_returns=series['five_second_returns'],
        five_second_return_columns=series['five_second_return_columns'],
        definitions=prepared['definitions'],
        coverage=dict(preclose=prepared['coverage'], native_return_series=series['coverage'],
            minute_input_status_counts=dict(statuses), rows=len(rows),
            contracts=len({row['ticker'] for row in rows}), warmup_mask_applied=False),
        provenance=dict(minute_source=series['provenance'], preclose=prepared['provenance'],
            input_feature_fingerprint=prepared['input_feature_fingerprint'],
            feature_cache_reused=feature_rows is not None,
            actual_acquisition_receipts_retained=True, source_archives_modified=False,
            historical_source_time_only=True, historical_live_receipt_availability_verified=False),
        mapping=dict(minute_returns='r_b=log(P_b/P_(b-60)); b is an exact UTC minute boundary',
            information_at_origin='Only return endpoints b<=floor(t/60)*60 are available to filtering',
            current_minute='Variance for [b,b+60) is predicted using completed returns through b',
            partial_return='log(P_t/P_b) is observed; the uncompleted full-minute return is never supplied',
            fixing_grid='T-60,...,T-1; sixty exact native second samples; samples at or before t are fixed',
            price_sampling='Exact native endpoints only; no interpolation, hourly averaging or gap compression'),
        limitations=[
            'Historical downloads may contain upstream revisions; original historical receive latency is not established.',
            'Acquisition and series hashes establish consistency, not independent source authentication.',
            'Official outcomes are retained independently of reconstructed continuous-average agreement.',
            'Return gaps and warm-up coverage require the same whole-contract filter for every compared model.'],
        research_only=True, execution_eligible=False, live_model_updated=False)
    if include_intraminute_history:
        result.update(include_intraminute_history=True,
            current_minute_observation_columns=list(CURRENT_MINUTE_OBSERVATION_COLUMNS))
        result['coverage']['intraminute_history'] = intraminute_coverage
        result['provenance']['intraminute_history'] = intraminute_provenance
        result['mapping']['current_minute_observations'] = (
            'Exact native one-second prices from floor(t/60)*60 through t inclusive; '
            'an aligned origin contains one price and zero observed increments')
    return result


def build_eth_garch_dataset(history_archive, *, feature_rows=None, max_rows=200000):
    """Retain the original ETH entry point and version-one dataset identity."""
    return build_garch_dataset(history_archive, asset=ASSET, feature_rows=feature_rows, max_rows=max_rows)
