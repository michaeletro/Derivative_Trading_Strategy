"""Source-time retrospective features for the frozen CF settlement diffusion.

Historical retrieval timestamps are evidence, not fabricated past receipts.
Only exact native second-boundary ticks enter this model's specified sampling
approximation. No live freshness or reception check is weakened here.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

from .crypto_assets import asset_spec
from .forecast import ForecastConfig, _averaging_inputs, _market, _timestamp, _volatility
from .history_markets import _label_reason
from .probability_greeks import _state


class FeatureError(ValueError):
    """Fixed, non-sensitive archive or historical feature validation error."""


@dataclass(frozen=True)
class _SourcePoint:
    event_time: float
    price: float


def _key(value):
    return format(float(value), '.12g')


def _hour(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%dT%H:00:00.000Z')


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _configuration(asset, horizons, half_lives, dataset_id, history_seconds=1800):
    try:
        profile = asset_spec(asset)
    except ValueError:
        raise FeatureError('Unsupported historical feature asset') from None
    if (not isinstance(horizons, (tuple, list)) or not 1 <= len(horizons) <= 15
            or any(type(value) is not int or not 1 <= value <= 900 for value in horizons)
            or len(set(horizons)) != len(horizons)):
        raise FeatureError('Choose one to fifteen distinct integer horizons from one to 900 seconds')
    if (not isinstance(half_lives, (tuple, list)) or not 1 <= len(half_lives) <= 12
            or any(not _finite(value) or not 1 <= value <= 3600 for value in half_lives)
            or len({_key(value) for value in half_lives}) != len(half_lives)):
        raise FeatureError('Choose one to twelve distinct volatility half-lives from one to 3600 seconds')
    if dataset_id is not None and (type(dataset_id) is not int or dataset_id <= 0):
        raise FeatureError('Historical dataset ID must be a positive integer')
    if type(history_seconds) is not int or not 1800 <= history_seconds <= 3600:
        raise FeatureError('Historical volatility history must be an integer from 1800 through 3600 seconds')
    return profile, tuple(sorted(horizons, reverse=True)), tuple(sorted(half_lives))


def _dataset(db, asset, dataset_id):
    if dataset_id is None:
        row = db.execute('''SELECT id,received_at,payload_sha256,payload FROM history_documents
            WHERE kind='kalshi_contracts' AND item_key LIKE ? ORDER BY id DESC LIMIT 1''',
            (asset + ':%',)).fetchone()
    else:
        row = db.execute('''SELECT id,received_at,payload_sha256,payload FROM history_documents
            WHERE kind='kalshi_contracts' AND id=?''', (dataset_id,)).fetchone()
    if row is None:
        raise FeatureError('No official contract dataset exists for this asset; download ticks and contracts first')
    raw = row['payload']
    if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != row['payload_sha256']:
        raise FeatureError('Historical contract dataset hash does not match its stored payload')
    try:
        dataset = json.loads(raw)
        summary = dataset['summary']
        if summary['asset'] != asset or summary.get('pagination_complete') is not True:
            raise ValueError()
        markets, outcomes = dataset['markets'], dataset['outcomes']
        if (not isinstance(markets, list) or not isinstance(outcomes, list)
                or len(markets) > 4000 or len(outcomes) > 4000
                or any(not isinstance(item, dict) for item in markets + outcomes)):
            raise ValueError()
        if not _finite(row['received_at']) or row['received_at'] <= 0:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise FeatureError('Historical contract dataset is incomplete or belongs to a different asset') from None
    return row, dataset


def _contracts(dataset):
    markets, labels = {}, {}
    for item in dataset['markets']:
        ticker = item.get('ticker')
        if not isinstance(ticker, str) or ticker in markets:
            raise FeatureError('Historical contract dataset has missing or duplicate market identities')
        markets[ticker] = item
    for item in dataset['outcomes']:
        ticker = item.get('ticker')
        if not isinstance(ticker, str) or ticker in labels:
            raise FeatureError('Historical contract dataset has missing or duplicate outcome identities')
        labels[ticker] = item
    result, exclusions = [], Counter()
    for ticker, label in labels.items():
        market = markets.get(ticker)
        if market is None:
            exclusions['label_missing_market'] += 1
            continue
        try:
            opened, closed = _timestamp(market.get('open_time')), _timestamp(market.get('close_time'))
            receipt = label.get('received_at')
            if not _finite(receipt) or receipt < closed:
                raise ValueError()
            if (label.get('result') not in ('yes', 'no')
                    or market.get('result') != label['result']
                    or type(label.get('binary_y')) is not int
                    or label['binary_y'] != int(label['result'] == 'yes')
                    or _timestamp(label.get('close_time')) != closed
                    or _timestamp(label.get('open_time')) != opened
                    or _label_reason(market, receipt) is not None):
                raise ValueError()
            if (market.get('settlement_bounds_type') != 'default'
                    or Decimal(str(market.get('notional_value_dollars'))) != 1
                    or not isinstance(market.get('rules_primary'), str)
                    or not market['rules_primary'].strip()
                    or closed != int(closed) or opened != int(opened)):
                raise ValueError()
        except (ValueError, TypeError, InvalidOperation):
            exclusions['invalid_official_contract_label_or_terms'] += 1
            continue
        result.append((market, label, closed))
    result.sort(key=lambda item: (item[2], item[0]['ticker']))
    if result and result[-1][2] - result[0][2] > 31 * 86400:
        raise FeatureError('Historical fitting dataset spans more than 31 days')
    return result, exclusions


class _SecondGrid:
    """Frozen acquisition IDs and a bounded cache of exact second samples."""

    def __init__(self, db, index_id, lower, upper):
        self.db, self.cache = db, OrderedDict()
        self.maximum_cache_hours = 0
        # Include the preceding hour's exact closing tick at the first boundary.
        earliest = _hour(math.floor(lower / 3600) * 3600 - 3600)
        latest = _hour(math.floor(upper / 3600) * 3600)
        rows = db.execute('''SELECT a.id,a.hour,a.status,a.started_at,a.received_at,a.payload_sha256
            FROM history_acquisitions a JOIN
            (SELECT hour,MAX(id) AS latest FROM history_acquisitions
             WHERE index_id=? AND hour>=? AND hour<=? GROUP BY hour) b ON a.id=b.latest
            ORDER BY a.hour''', (index_id, earliest, latest)).fetchall()
        if len(rows) > 744:
            raise FeatureError('Historical fitting acquisition snapshot exceeds its supported bound')
        self.acquisitions = {_timestamp(row['hour']): dict(row) for row in rows}

    def _load(self, stamp):
        if stamp in self.cache:
            self.cache.move_to_end(stamp)
            return self.cache[stamp]
        acquisition = self.acquisitions.get(stamp)
        ticks = {}
        if acquisition is not None and acquisition['status'] != 'error':
            for row in self.db.execute('''SELECT source_time_ms,value,repeated,amendment
                    FROM history_ticks WHERE acquisition_id=? AND source_time_ms % 1000=0
                    ORDER BY source_time_ms''', (acquisition['id'],)):
                event = row['source_time_ms']
                if type(event) is not int or not stamp * 1000 <= event <= (stamp + 3600) * 1000:
                    raise FeatureError('Historical second-grid tick timestamp is invalid')
                try:
                    price = Decimal(row['value'])
                    if not price.is_finite() or not 0 < price <= 100000000:
                        raise ValueError()
                except (InvalidOperation, ValueError, TypeError):
                    raise FeatureError('Historical second-grid tick value is invalid') from None
                ticks[event // 1000] = None if row['repeated'] or row['amendment'] is not None else price
        self.cache[stamp] = ticks
        while len(self.cache) > 3:
            self.cache.popitem(last=False)
        self.maximum_cache_hours = max(self.maximum_cache_hours, len(self.cache))
        return ticks

    def window(self, as_of, history_seconds=1800):
        start = as_of - history_seconds
        start_hour, end_hour = math.floor(start / 3600) * 3600, math.floor(as_of / 3600) * 3600
        merged = {}
        # Read one preceding endpoint too, so contradictory adjacent-hour
        # boundary values are quarantined rather than selected arbitrarily.
        for hour in range(int(start_hour - 3600), int(end_hour) + 1, 3600):
            for event, price in self._load(hour).items():
                if start <= event <= as_of:
                    if event in merged and (price is None or merged[event] is None or merged[event] != price):
                        merged[event] = None
                    else:
                        merged[event] = price
        points = []
        for event in range(int(start), int(as_of) + 1):
            # A failed latest hour is not replaced with an earlier acquisition
            # or an adjacent closing tick, even at the hour boundary.
            acquisition = self.acquisitions.get(math.floor(event / 3600) * 3600)
            if acquisition is not None and acquisition['status'] == 'error':
                raise FeatureError('latest_hour_acquisition_failed')
            if event not in merged:
                raise FeatureError('missing_exact_second_tick')
            if merged[event] is None:
                raise FeatureError('flagged_or_conflicting_second_tick')
            points.append(_SourcePoint(float(event), float(merged[event])))
        return points


def build_training_rows(archive, *, asset='BTC', horizons=(840, 600, 300, 120, 60, 30, 10),
                        half_lives=(60, 300, 900), dataset_id=None, progress=None,
                        history_seconds=1800):
    """Return compact retrospective Gaussian states with official binary labels.

    Every accepted contract contributes all requested horizons. Source-time
    filtering prevents future ticks entering a state; revisions downloaded after
    expiry remain explicitly retrospective and cannot establish historical latency.
    The settlement grid is the existing model approximation, not a rules parser.
    """
    profile, horizons, half_lives = _configuration(asset, horizons, half_lives, dataset_id, history_seconds)
    if getattr(archive, 'read_only', False) is not True:
        raise FeatureError('Historical feature extraction requires a read-only archive')
    db = archive.db
    own_transaction = not db.in_transaction
    if own_transaction:
        db.execute('BEGIN')
    try:
        document, dataset = _dataset(db, profile.symbol, dataset_id)
        contracts, exclusions = _contracts(dataset)
        if not contracts:
            raise FeatureError('No eligible official binary contracts are available for fitting')
        lower = contracts[0][2] - max(horizons) - history_seconds
        upper = contracts[-1][2] - min(horizons)
        grid = _SecondGrid(db, profile.index_id, lower, upper)
        configs = [ForecastConfig(ewma_half_life_seconds=value, max_history_seconds=history_seconds)
                   for value in half_lives]
        rows, accepted = [], 0
        for position, (market, label, closed) in enumerate(contracts, 1):
            prepared = []
            try:
                for horizon in horizons:
                    as_of = closed - horizon
                    strike, _ = _market(market, as_of, asset=profile.symbol)
                    points = grid.window(as_of, history_seconds)
                    known, count, future = _averaging_inputs(points, closed, as_of, asset=profile.symbol)
                    offsets = [stamp - as_of for stamp in future]
                    if not offsets or offsets[0] <= 0:
                        raise FeatureError('invalid_future_settlement_grid')
                    _, _, variance_time, numerator, _ = _state(points[-1].price, strike, known,
                        offsets, 1.0, inclusive=market['strike_type'] == 'greater_or_equal')
                    sigmas = {_key(value): _volatility(points, as_of, config,
                                asset=profile.symbol)[0]
                              for value, config in zip(half_lives, configs)}
                    prepared.append(dict(ticker=market['ticker'], as_of=as_of,
                        close_time=closed, day=datetime.fromtimestamp(closed, timezone.utc).strftime('%Y-%m-%d'),
                        horizon_seconds=horizon, y=label['binary_y'], spot=points[-1].price,
                        numerator=numerator, variance_time=variance_time,
                        sigma_by_half_life=sigmas, inclusive=market['strike_type'] == 'greater_or_equal',
                        observed_settlement_count=count, future_settlement_count=len(future)))
                rows.extend(prepared)
                accepted += 1
            except FeatureError as error:
                reason = str(error)
                exclusions[reason] += 1
            except (ValueError, OverflowError, ZeroDivisionError):
                exclusions['unsupported_contract_or_numerical_state'] += 1
            if progress is not None and (position % 25 == 0 or position == len(contracts)):
                progress(dict(stage='historical_features', contracts_processed=position,
                              contracts_total=len(contracts), accepted_contracts=accepted,
                              feature_rows=len(rows), execution_eligible=False))
        acquisition_records = list(grid.acquisitions.values())
        snapshot_text = json.dumps(acquisition_records, allow_nan=False, sort_keys=True, separators=(',', ':'))
        provenance = dict(source='historical_download_not_live_receipt', asset=profile.symbol,
            index_id=profile.index_id, contract_document_id=document['id'],
            contract_document_sha256=document['payload_sha256'],
            contract_document_retrieved_at=document['received_at'], acquisitions=acquisition_records,
            contract_dataset_selection='latest_single_asset_dataset' if dataset_id is None else 'explicit_document_id',
            acquisition_snapshot_sha256=hashlib.sha256(snapshot_text.encode()).hexdigest(),
            receipt_times_fabricated=False, historical_receive_latency_known=False,
            source_time_only_reconstruction=True, historical_revisions_possible=True,
            native_sampling='exact_second_boundary_only_no_averaging_or_interpolation',
            settlement_sampling='model_approximation_equal_weight_T_minus_59_through_T',
            contract_specific_sampling_rules_verified=False)
        summary = dict(asset=profile.symbol, eligible_official_contracts=len(contracts),
            accepted_contracts=accepted, feature_rows=len(rows), excluded_contracts=dict(exclusions),
            horizons_seconds=list(horizons), half_lives_seconds=list(half_lives),
            history_seconds=history_seconds, maximum_cached_hours=grid.maximum_cache_hours,
            complete_case_selection_bias_possible=True,
            exact_second_grid_required=True, execution_eligible=False)
        return dict(rows=rows, summary=summary, provenance=provenance)
    finally:
        if own_transaction:
            db.rollback()
