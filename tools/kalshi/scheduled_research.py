"""Offline forecast scoring and native scheduled-policy counterfactuals.

No exchange transport or credentials are constructed. Execution comparisons use
the real scheduled executor with an isolated temporary journal and the recorded
later-book simulator. Missing execution provenance never becomes a synthetic
book, source certificate, settlement, or fill.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import tempfile
from urllib.parse import quote

from .btc15m import verified_crypto_settlement_source, _effective_fee
from .crypto_assets import ASSETS, asset_from_ticker, asset_spec
from .engine import Journal, RiskConfig, _iso, _timestamp, _valid_tick
from .entry_schedule import EntrySchedule
from .minute_five import _identity
from .minute_five_execution import MinuteFiveLiveExecutor
from .settlement_allowance import refresh_settled_allowances
from .time_sizing import UniformSizing
from .value_legacy_replay import _Simulation, _ReadOnly, _ACCOUNT
from .value_replay import ValueReplayConfig, _levels


MAX_EVENTS = 200_000
PURGE_SECONDS = 900


class ScheduledResearchError(ValueError):
    """Invalid recorded research evidence or configuration."""


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise ScheduledResearchError('Recorded timestamps must be finite positive UTC epoch numbers')
    return float(value)


def _probability(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ScheduledResearchError('A recorded probability must be finite and between zero and one')
    return float(value)


def _money(value, *, positive=False):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 60:
        raise ScheduledResearchError('A fixed-point research dollar amount is invalid')
    try:
        amount = Decimal(value)
    except Exception:
        raise ScheduledResearchError('A fixed-point research dollar amount is invalid') from None
    if not amount.is_finite() or amount < 0 or positive and amount <= 0 or amount > 1_000_000:
        raise ScheduledResearchError('A research dollar amount exceeds its supported bounds')
    return amount


@dataclass(frozen=True)
class ScheduledResearchConfig:
    initial_cash: Decimal = Decimal('720')
    uniform_market_budget: Decimal = Decimal('180')
    cadence_seconds: int = 5
    execution_latency_seconds: float = .25
    max_execution_wait_seconds: float = 5
    depth_fraction: Decimal = Decimal('.5')
    minimum_contracts: int = 200
    minimum_days: int = 5
    bootstrap_samples: int = 500
    seed: int = 7
    max_events: int = MAX_EVENTS
    max_actions: int = 20_000

    def __post_init__(self):
        for name in ('initial_cash', 'uniform_market_budget', 'depth_fraction'):
            value = _money(getattr(self, name), positive=True)
            if name == 'depth_fraction' and value > 1:
                raise ScheduledResearchError('The replay depth fraction must not exceed one')
            object.__setattr__(self, name, value)
        if type(self.cadence_seconds) is not int or not 1 <= self.cadence_seconds <= 900:
            raise ScheduledResearchError('Research cadence must be an integer from one through 900')
        for name in ('execution_latency_seconds', 'max_execution_wait_seconds'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
                raise ScheduledResearchError('Research execution timing is invalid')
        if not 0 <= self.execution_latency_seconds <= self.max_execution_wait_seconds or self.max_execution_wait_seconds <= 0:
            raise ScheduledResearchError('Execution wait must cover replay latency')
        for name, low, high in (('minimum_contracts', 3, 100_000), ('minimum_days', 3, 365),
                               ('bootstrap_samples', 100, 2000), ('max_events', 1, MAX_EVENTS),
                               ('max_actions', 1, 100_000)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ScheduledResearchError('Research sample or resource bounds are invalid')
        if type(self.seed) is not int or not 0 <= self.seed <= 2**32-1:
            raise ScheduledResearchError('The research random seed is invalid')


def _recorded_book_fields(row, *, allow_scenario_fees):
    yes, no = _levels(row.get('yes_bids')), _levels(row.get('no_bids'))
    if not yes or not no or Decimal(yes[-1][0])+Decimal(no[-1][0]) >= 1:
        raise ScheduledResearchError('An unlocked recorded executable book is required')
    if row.get('fee_type') not in ('quadratic', 'quadratic_with_maker_fees'):
        raise ScheduledResearchError('Recorded venue fee metadata is unavailable')
    _money(row.get('fee_multiplier'))
    quantum = _money(row.get('fee_quantum'), positive=True)
    if allow_scenario_fees:
        from .value_sizing import ValueSizingConfig
        ValueSizingConfig(fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'],
            fee_quantum=quantum)
    elif quantum != Decimal('.0001'):
        raise ScheduledResearchError('Recorded venue fee rounding must use the supported dollar quantum')
    return yes, no


def validate_records(records, *, max_events=MAX_EVENTS, allow_receipt_revisions=False,
                     allow_scenario_fees=False, allow_book_events=False, allow_preclose=False):
    """Keep local receipt order and bind every label to its original contract."""
    if (type(allow_receipt_revisions) is not bool or type(allow_scenario_fees) is not bool
            or type(allow_book_events) is not bool or type(allow_preclose) is not bool):
        raise ScheduledResearchError('Research validation options must be explicit booleans')
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= max_events:
        raise ScheduledResearchError('Recorded research events must be a nonempty bounded sequence')
    output, identities, outcomes, seen, bindings, book_evidence = [], {}, {}, {}, {}, {}
    previous = 0
    for raw in records:
        if not isinstance(raw, dict) or raw.get('version') != 1 or type(raw.get('version')) is not int:
            raise ScheduledResearchError('A recorded research event is invalid')
        row = deepcopy(raw)
        kind = row.get('kind')
        observation = kind in ('kalshi_scheduled_observation', 'kalshi_value_observation')
        book_only = kind in ('kalshi_scheduled_book', 'kalshi_value_book')
        book_gap = kind in ('kalshi_scheduled_book_gap', 'kalshi_value_book_gap')
        if (row.get('book_evidence_id') is not None or 'book_evidence_quote_time' in row) and not allow_book_events:
            raise ScheduledResearchError('Independent book evidence requires explicit book-event validation')
        if 'book_evidence_quote_time' in row and row.get('book_evidence_id') is None:
            raise ScheduledResearchError('A canonical book clock requires its recorded evidence identity')
        if ((book_only or book_gap) and not allow_book_events or not observation and not book_only and not book_gap
                and kind not in ('kalshi_scheduled_settlement', 'kalshi_value_settlement')):
            raise ScheduledResearchError('Unsupported scheduled research event kind')
        stamp = _time(row.get('observed_at') if observation or book_only or book_gap else row.get('available_at'))
        if stamp < previous:
            raise ScheduledResearchError('Research records must retain receipt order')
        previous = stamp
        ticker = row.get('ticker')
        try:
            profile = asset_from_ticker(ticker)
        except ValueError:
            raise ScheduledResearchError('A research contract belongs to an unsupported asset') from None
        if row.get('asset', profile.symbol) != profile.symbol:
            raise ScheduledResearchError('The recorded asset differs from its contract series')
        identity = row.get('contract_identity')
        try:
            if not isinstance(identity, dict) or identity.get('ticker') != ticker:
                raise ValueError()
            opened, closed = _timestamp(identity['open_time']), _timestamp(identity['close_time'])
            if (abs(closed-opened-900) > 1e-6 or identity.get('market_type') != 'binary'
                    or identity.get('settlement_bounds_type') != 'default'
                    or identity.get('strike_type') not in ('greater', 'greater_or_equal')
                    or identity.get('cap_strike') is not None
                    or _money(identity.get('notional_value_dollars')) != 1
                    or _money(identity.get('floor_strike'), positive=True) <= 0):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ScheduledResearchError('Recorded settlement terms are invalid') from None
        if ticker in identities and identities[ticker] != identity:
            raise ScheduledResearchError('A recorded contract changed its settlement identity')
        identities[ticker] = identity
        row.update(asset=profile.symbol, _time=stamp, _closed=closed, _identity=identity)
        if book_only or book_gap:
            forbidden = {'model_time', 'probability_yes', 'probability_no', 'model_version',
                'model_fingerprint', 'native_snapshot', 'forecast', 'median_outcome'}
            if forbidden.intersection(row):
                raise ScheduledResearchError('Book-only events cannot contain forecast fields')
            quote_time = _time(row.get('quote_time'))
            if not opened <= quote_time <= stamp < closed:
                raise ScheduledResearchError('Book-only receipt clocks differ from their open contract')
            provenance = row.get('recovery_provenance')
            if not isinstance(provenance, dict):
                raise ScheduledResearchError('Book-only events require recorded market-context receipt provenance')
            market = row.get('market')
            if market is not None:
                if not isinstance(market, dict) or _identity(market) != identity:
                    raise ScheduledResearchError('Book-only market metadata differs from its contract')
                context_receipt = _time(provenance.get('market_context_received_at'))
                if context_receipt > stamp:
                    raise ScheduledResearchError('Book market context was received after the book event')
            if book_gap:
                if {'yes_bids', 'no_bids', 'fee_type', 'fee_multiplier', 'fee_quantum'}.intersection(row):
                    raise ScheduledResearchError('Book-gap events cannot invent prices or fees')
                if {'book_evidence_id', 'book_evidence_quote_time'}.intersection(row):
                    raise ScheduledResearchError('Book-gap events cannot alias a different liquidity clock')
                reason = row.get('gap_reason')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                    raise ScheduledResearchError('Book-gap events require a bounded recorded reason')
                row.update(kind='kalshi_value_book_gap', quote_time=quote_time)
            else:
                if market is None or not market.get('price_ranges'):
                    raise ScheduledResearchError('Book-only events require matching recorded market and executable grid')
                if market.get('status') != 'active':
                    raise ScheduledResearchError('Usable book-only events require active recorded market status')
                _valid_tick(Decimal('.5'), market['price_ranges'])
                yes, no = _recorded_book_fields(row, allow_scenario_fees=allow_scenario_fees)
                meta = market.get('_crypto15m') or market.get('_btc15m') or {}
                if not isinstance(meta, dict):
                    raise ScheduledResearchError('Recorded book market fee metadata must be an object')
                evidence = meta.get('settlement_source_evidence') or {}
                if not isinstance(evidence, dict):
                    raise ScheduledResearchError('Recorded book settlement source evidence must be an object')
                series, event = evidence.get('series') or {}, evidence.get('event') or {}
                if not isinstance(series, dict) or not isinstance(event, dict):
                    raise ScheduledResearchError('Recorded book series and event documents must be objects')
                context_fees = (_effective_fee(series, event)
                    if 'fee_type' in series and 'fee_multiplier' in series else
                    (meta.get('fee_type'), meta.get('fee_multiplier')))
                if context_fees[0] != row['fee_type'] or _money(context_fees[1]) != _money(row['fee_multiplier']):
                    raise ScheduledResearchError('Book-only fees differ from the recorded market context')
                row.update(kind='kalshi_value_book', yes_bids=yes, no_bids=no, quote_time=quote_time)
        elif observation:
            model_time, quote_time = _time(row.get('model_time')), _time(row.get('quote_time'))
            p = _probability(row.get('probability_yes'))
            version = row.get('model_version')
            prefix = 'cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-'
            versions = (prefix+'median-v1', prefix+'analytic-v1', prefix+'population-v1', prefix+'preclose-v1', prefix+'calibrated-v1')
            if allow_preclose:
                versions += (prefix+'preclose-paper-v1',)
            if (row.get('source') != profile.source or version not in versions
                    or not opened <= model_time <= stamp < closed or not opened <= quote_time <= stamp):
                raise ScheduledResearchError('Forecast source, model, or receipt clocks differ from the settlement asset')
            if version == prefix+'preclose-paper-v1':
                from .preclose_paper_probability import validate_preclose_forecast
                try:
                    forecast = row['native_snapshot']['forecast']
                    validate_preclose_forecast(forecast, asset=profile.symbol)
                    if (forecast['model_version'] != version or forecast['source'] != row['source']
                            or forecast['model_time'] != model_time or forecast['contract_identity'] != identity
                            or abs(forecast['probability_yes'] - p) > 1e-12
                            or forecast['model_fingerprint'] != row.get('model_fingerprint')):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ScheduledResearchError('Preclose research requires its exact original source-grid forecast proof') from None
            elif version == prefix+'preclose-v1':
                from .preclose_probability import validate_preclose_forecast
                try:
                    forecast = row['native_snapshot']['forecast']
                    validate_preclose_forecast(forecast, asset=profile.symbol)
                    if (forecast['model_version'] != version or forecast['source'] != row['source']
                            or forecast['model_time'] != model_time or forecast['contract_identity'] != identity
                            or abs(forecast['probability_yes'] - p) > 1e-12):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ScheduledResearchError('Preclose research requires its matching native rounded settlement proof') from None
            elif version == prefix+'calibrated-v1':
                from .calibrated_model import validate_calibrated_forecast
                try:
                    forecast = row['native_snapshot']['forecast']
                    validate_calibrated_forecast(forecast, asset=profile.symbol)
                    if (forecast['model_version'] != version or forecast['source'] != row['source']
                            or forecast['model_time'] != model_time or forecast['contract_identity'] != identity
                            or abs(forecast['probability_yes'] - p) > 1e-12):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ScheduledResearchError('Calibrated research requires its matching original forecast and artifact proof') from None
            elif version == prefix+'population-v1':
                from .population_probability import validate_population_forecast
                try:
                    forecast = row['native_snapshot']['forecast']
                    validate_population_forecast(forecast, asset=profile.symbol)
                    if (forecast['model_time'] != model_time or forecast['contract_identity'] != identity
                            or abs(forecast['probability_yes'] - p) > 1e-12):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ScheduledResearchError('Population research requires its matching original source-grid forecast proof') from None
            fingerprint = row.get('model_fingerprint')
            if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
                raise ScheduledResearchError('A recorded model fingerprint is required')
            binding = (version, fingerprint)
            if profile.symbol in bindings and bindings[profile.symbol] != binding:
                raise ScheduledResearchError('Use separate research inputs for different models of one asset')
            bindings[profile.symbol] = binding
            yes, no = _recorded_book_fields(row, allow_scenario_fees=allow_scenario_fees)
            key = (ticker, model_time)
            if key in seen:
                if seen[key] != p:
                    if not allow_receipt_revisions:
                        raise ScheduledResearchError('A first-received forecast was revised for the same source timestamp')
                    row['_receipt_probability_revision'] = True
                else:
                    row['_duplicate_forecast'] = True
            else:
                seen[key] = p
            row.update(kind='kalshi_value_observation', yes_bids=yes, no_bids=no,
                quote_time=quote_time, model_time=model_time, probability_yes=p)
        else:
            settled = _time(row.get('settled_at'))
            result = row.get('result')
            official = row.get('market')
            if (result not in ('yes', 'no') or not closed <= settled <= stamp
                    or not isinstance(official, dict) or _identity(official) != identity
                    or official.get('status') != 'finalized' or official.get('is_provisional', False) is not False
                    or official.get('result') != result or _timestamp(official.get('settlement_ts')) != settled):
                raise ScheduledResearchError('An authoritative final settlement and its receipt time are required')
            old = outcomes.get(ticker)
            if old is not None:
                if old != result:
                    raise ScheduledResearchError('A recorded official settlement outcome changed')
                # First receipt is immutable evidence of when the label became
                # available. A later identical receipt cannot delay that clock.
                continue
            outcomes[ticker] = result
            row.update(kind='kalshi_value_settlement', result=result, settled_at=settled)
        if allow_book_events and (observation or book_only or book_gap) and row.get('book_evidence_id') is not None:
            token = row['book_evidence_id']
            if not isinstance(token, str) or not 1 <= len(token) <= 240 or any(ord(c) < 33 for c in token):
                raise ScheduledResearchError('A bounded recorded book evidence identity is required')
            evidence_clock = _time(row.get('book_evidence_quote_time', row['quote_time']))
            if evidence_clock < opened or evidence_clock > row['quote_time'] or evidence_clock > stamp:
                raise ScheduledResearchError('Recorded book evidence clock is after its quote or receipt')
            signature = ((ticker, evidence_clock, identity, 'gap', row['gap_reason']) if book_gap else
                (ticker, evidence_clock, identity,
                    tuple((Decimal(price), Decimal(quantity)) for price, quantity in row['yes_bids']),
                    tuple((Decimal(price), Decimal(quantity)) for price, quantity in row['no_bids']),
                    row['fee_type'], Decimal(row['fee_multiplier']), Decimal(row['fee_quantum'])))
            if token in book_evidence and book_evidence[token] != signature:
                raise ScheduledResearchError('A recorded book evidence identity changed its contract, time or levels')
            book_evidence[token] = signature
        output.append(row)
    return output


def records_from_archives(bindings, *, max_events=MAX_EVENTS):
    """Read genuine shadow archives without opening an execution journal."""
    if not isinstance(bindings, dict) or not bindings or not set(bindings) <= set(ASSETS):
        raise ScheduledResearchError('Archive bindings require supported asset names')
    records = []
    for asset, supplied in bindings.items():
        path = Path(supplied).expanduser().absolute()
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ScheduledResearchError('The recorded shadow archive is unavailable')
        uri = 'file:' + quote(str(path), safe='/:\\') + '?mode=ro'
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=.25)) as db:
                db.row_factory = sqlite3.Row
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'metadata', 'forecasts', 'outcomes'} <= tables or 'kalshi_meta' in tables:
                    raise ScheduledResearchError('Research requires a shadow archive, not an order journal')
                saved = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
                experiment = json.loads(saved[0]) if saved else {}
                if experiment.get('source') != asset_spec(asset).source:
                    raise ScheduledResearchError('The archive source differs from its selected asset')
                digest = hashlib.sha256(json.dumps(experiment, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                for saved in db.execute('SELECT * FROM forecasts ORDER BY id'):
                    market, book, forecast = (json.loads(saved[key]) for key in ('market', 'book', 'forecast'))
                    completed = forecast.get('forecast_completed_at')
                    if type(completed) not in (int, float):
                        raise ScheduledResearchError('The archive lacks the recorded forecast completion clock')
                    receipt = max(float(saved['as_of']), float(saved['quote_received_at']), completed)
                    native = dict(kind='kalshi_minute_five_observation', version=1, observation_only=True,
                        status='ready', strategy='minute_five', asset=asset, fetched_at=_iso(receipt),
                        forecast=forecast, market_snapshot=dict(market_status='ready',
                            fetched_at=_iso(saved['quote_received_at']), market=dict(ticker=market['ticker'])))
                    meta = market.get('_crypto15m') or market.get('_btc15m') or {}
                    records.append(dict(kind='kalshi_scheduled_observation', version=1, asset=asset,
                        observed_at=receipt, model_time=forecast['model_time'], quote_time=saved['quote_received_at'],
                        ticker=market['ticker'], contract_identity=_identity(market),
                        probability_yes=forecast['probability_yes'], source=forecast['source'],
                        model_version=forecast['model_version'], model_fingerprint=digest,
                        yes_bids=book['orderbook_fp']['yes_dollars'], no_bids=book['orderbook_fp']['no_dollars'],
                        fee_type=meta.get('fee_type'), fee_multiplier=meta.get('fee_multiplier'),
                        fee_quantum='.0001', market=market, native_snapshot=native))
                    if len(records) > max_events:
                        raise ScheduledResearchError('The recorded archive exceeds the research event bound')
                for saved in db.execute('SELECT * FROM outcomes ORDER BY received_at'):
                    market = json.loads(saved['market'])
                    records.append(dict(kind='kalshi_scheduled_settlement', version=1, asset=asset,
                        available_at=saved['received_at'], settled_at=_timestamp(market['settlement_ts']),
                        ticker=saved['ticker'], contract_identity=_identity(market), result=saved['result'], market=market))
                    if len(records) > max_events:
                        raise ScheduledResearchError('The recorded archive exceeds the research event bound')
        except sqlite3.Error:
            raise ScheduledResearchError('The read-only shadow archive could not be read') from None
    records.sort(key=lambda row: row.get('observed_at', row.get('available_at')))
    return validate_records(records, max_events=max_events)


def _metrics(samples):
    if not samples:
        return dict(status='unavailable', count_contracts=0, count_forecasts=0,
                    brier_score=None, log_loss=None, market_brier_score=None, market_log_loss=None, calibration=[])
    counts = Counter(row['ticker'] for row in samples)
    weights = [1/counts[row['ticker']] for row in samples]
    total = math.fsum(weights)
    def loss(key):
        brier, logarithm = [], []
        for row, weight in zip(samples, weights):
            p, y = row[key], row['y']
            clipped = max(1e-12, min(1-1e-12, p))
            brier.append(weight*(p-y)**2)
            logarithm.append(-weight*(y*math.log(clipped)+(1-y)*math.log1p(-clipped)))
        return math.fsum(brier)/total, math.fsum(logarithm)/total
    brier, logloss = loss('p')
    mbrier, mlogloss = loss('market_p')
    bins = []
    for index in range(10):
        chosen = [(r,w) for r,w in zip(samples,weights) if min(9,int(r['p']*10)) == index]
        if chosen:
            size = math.fsum(w for _,w in chosen)
            bins.append(dict(lower=index/10, upper=(index+1)/10,
                count_forecasts=len(chosen), count_contracts=len({r['ticker'] for r,_ in chosen}),
                mean_probability=math.fsum(r['p']*w for r,w in chosen)/size,
                observed_frequency=math.fsum(r['y']*w for r,w in chosen)/size))
    return dict(status='ready', count_contracts=len(counts), count_forecasts=len(samples),
        brier_score=brier, log_loss=logloss, market_brier_score=mbrier, market_log_loss=mlogloss,
        model_minus_market_brier=brier-mbrier, model_minus_market_log_loss=logloss-mlogloss,
        weighting='equal_contract_weight_shared_across_its_forecasts', calibration=bins)


def _score(records, asset, config):
    outcomes = {row['ticker']: row for row in records if row['kind'] == 'kalshi_value_settlement'}
    samples, withheld = [], Counter()
    for row in records:
        if row['kind'] != 'kalshi_value_observation' or row['asset'] != asset:
            continue
        if row.get('_duplicate_forecast'):
            withheld['duplicate_source_timestamp'] += 1
            continue
        outcome = outcomes.get(row['ticker'])
        if outcome is None:
            withheld['missing_official_outcome'] += 1
            continue
        if row['_time']-row['model_time'] > 5 or row['_time']-row['quote_time'] > 5:
            withheld['stale_execution_evidence'] += 1
            continue
        market_p = (float(row['yes_bids'][-1][0])+1-float(row['no_bids'][-1][0]))/2
        samples.append(dict(ticker=row['ticker'], close=row['_closed'], as_of=row['_time'],
            label_available_at=outcome['_time'], day=datetime.fromtimestamp(row['_closed'],timezone.utc).strftime('%Y-%m-%d'),
            p=row['probability_yes'], market_p=market_p, y=int(outcome['result']=='yes'),
            horizon=row['_closed']-row['model_time']))
    groups = defaultdict(list)
    for row in samples:
        groups[row['ticker']].append(row)
    ordered = sorted(groups, key=lambda ticker:(groups[ticker][0]['close'],ticker))
    days = sorted({row['day'] for row in samples})
    result = dict(status='insufficient_data', count_contracts=len(groups), count_utc_days=len(days),
        minimum_contracts=config.minimum_contracts, minimum_days=config.minimum_days,
        withheld=dict(withheld), descriptive_metrics=_metrics(samples), splits={}, split_contracts={},
        deployment_status='offline_research_only', calibrated=False)
    if len(groups)<config.minimum_contracts or len(days)<config.minimum_days:
        result['reason']='More officially settled contracts and independent UTC days are required'
        return result
    # Partition whole UTC days; no contract can cross train/validation/test.
    train_days=max(1,int(len(days)*.6)); valid_days=max(1,int(len(days)*.2))
    train_days=min(train_days,len(days)-2); valid_days=min(valid_days,len(days)-train_days-1)
    sets={'train':set(days[:train_days]),'validation':set(days[train_days:train_days+valid_days]),
          'test':set(days[train_days+valid_days:])}
    parts={name:[row for row in samples if row['day'] in own] for name,own in sets.items()}
    # Remove earlier labelled contracts not safely known before the next block.
    for earlier,later in (('train','validation'),('validation','test')):
        next_start=min(row['as_of'] for row in parts[later])
        allowed={ticker for ticker in ordered if all(row['close']+PURGE_SECONDS<=next_start
            and row['label_available_at']<=next_start for row in groups[ticker])}
        parts[earlier]=[row for row in parts[earlier] if row['ticker'] in allowed]
    if any(not values for values in parts.values()):
        result['reason']='Chronological purge or delayed labels leave an empty evaluation block'
        return result
    result.update(status='ready', reason='Later contracts are scored in chronological blocks; this report does not refit supplied forecasts',
        purge_seconds=PURGE_SECONDS,
        splits={name:_metrics(values) for name,values in parts.items()},
        split_contracts={name:sorted({row['ticker'] for row in values}) for name,values in parts.items()},
        by_horizon={label:_metrics([row for row in parts['test'] if low<row['horizon']<=high])
            for label,low,high in (('0_60',0,60),('60_180',60,180),('180_450',180,450),('450_900',450,900))})
    daily=defaultdict(list)
    for row in parts['test']:
        daily[row['day']].append(row)
    if len(daily)<3:
        result['paired_day_bootstrap']=dict(status='insufficient_data',reason='At least three held-out UTC days are required')
    else:
        values=[_metrics(rows)['model_minus_market_brier'] for rows in daily.values()]
        rng=random.Random(config.seed)
        draws=sorted(math.fsum(rng.choice(values) for _ in values)/len(values) for _ in range(config.bootstrap_samples))
        result['paired_day_bootstrap']=dict(status='ready', metric='equal_day_mean_model_minus_market_brier',
            count_days=len(values), mean=math.fsum(values)/len(values),
            lower_95=draws[int(.025*(len(draws)-1))],upper_95=draws[int(.975*(len(draws)-1))],
            bootstrap_samples=config.bootstrap_samples,seed=config.seed)
    return result


def _native(row):
    native, market = row.get('native_snapshot'),row.get('market')
    if not isinstance(native,dict) or not isinstance(market,dict) or _identity(market)!=row['_identity']:
        raise ScheduledResearchError('Exact policy replay requires the recorded native observation and full market')
    evidence=(market.get('_crypto15m') or {}).get('settlement_source_evidence')
    if not isinstance(evidence,dict):
        raise ScheduledResearchError('Exact policy replay requires recorded series and event settlement-source documents')
    try:
        verified_crypto_settlement_source(evidence['series'],market,evidence['event'],asset=row['asset'])
    except (ValueError,KeyError,TypeError):
        raise ScheduledResearchError('Recorded settlement-source documents are incompatible') from None
    # Source captures may contain only the rule/source fields. Discovery stores
    # the independently verified fees beside that evidence, so use those
    # recorded fields rather than requiring an invented complete API response.
    meta=market.get('_crypto15m') or {}
    fees=evidence['series']
    fee_type,fee_multiplier=(_effective_fee(fees,evidence['event']) if 'fee_type' in fees and 'fee_multiplier' in fees
        else (meta.get('fee_type'),meta.get('fee_multiplier')))
    if fee_type!=row['fee_type'] or _money(fee_multiplier)!=_money(row['fee_multiplier']):
        raise ScheduledResearchError('Portable fees differ from the recorded series fee metadata')
    forecast=native.get('forecast') or {}
    if (native.get('status')!='ready' or native.get('asset')!=row['asset']
            or forecast.get('contract_identity')!=row['_identity'] or forecast.get('ticker')!=row['ticker']
            or forecast.get('source')!=row['source'] or forecast.get('model_version')!=row['model_version']
            or forecast.get('model_time')!=row['model_time']
            or abs(_probability(forecast.get('probability_yes'))-row['probability_yes'])>1e-10
            or _timestamp(native.get('fetched_at'))>row['_time']+1e-6
            or abs(_timestamp((native.get('market_snapshot') or {}).get('fetched_at'))-row['quote_time'])>1e-6):
        raise ScheduledResearchError('Native and portable forecast identities or clocks disagree')
    if not market.get('price_ranges'):
        raise ScheduledResearchError('Exact policy replay requires the recorded executable price grid')
    _valid_tick(Decimal('.5'),market['price_ranges'])
    return deepcopy(native)


class _ScheduledSimulation(_Simulation):
    def get_event(self,ticker):
        row=self._record()
        evidence=row['market']['_crypto15m']['settlement_source_evidence']
        return dict(deepcopy(evidence['event']),event_ticker=ticker,
                    series_ticker=asset_spec(row['asset']).series)


def replay_scheduled(records, *, assets, config, scheduled_policy_config=None):
    """Replay current non-regime native rules; every fill needs a later book."""
    if any(row.get('kind') == 'kalshi_value_observation'
            and str(row.get('model_version', '')).endswith('-minute-five-calibrated-v1') for row in records):
        return dict(status='unavailable',
            reason='Calibrated policy replay requires an explicit trusted artifact configuration; forecast scoring remains available',
            execution_eligible=False, actions=[], realized_pnl=None)
    snapshots={}
    try:
        for index,row in enumerate(records):
            if row['kind']=='kalshi_value_observation':
                snapshots[index]=_native(row)
    except (ValueError,TypeError,KeyError) as error:
        return dict(status='unavailable',reason=str(error) if isinstance(error,ScheduledResearchError) else
            'Exact native replay provenance is incomplete',execution_eligible=False,actions=[],realized_pnl=None)
    if not snapshots:
        return dict(status='unavailable',reason='No recorded native observations are available',execution_eligible=False,actions=[],realized_pnl=None)
    replay_config=ValueReplayConfig(initial_cash=config.initial_cash,
        uniform_market_budget=config.uniform_market_budget,uniform_cadence_seconds=config.cadence_seconds,
        execution_latency_seconds=config.execution_latency_seconds,max_execution_wait_seconds=config.max_execution_wait_seconds,
        depth_fraction=config.depth_fraction,max_events=config.max_events,max_actions=config.max_actions)
    profile=UniformSizing(config.uniform_market_budget)
    schedule=EntrySchedule(0,config.cadence_seconds)
    maximum=max(profile.budgets(schedule))
    if maximum<1:
        return dict(status='unavailable',reason='This native schedule requires at least one dollar per entry allowance',
            execution_eligible=False,actions=[],realized_pnl=None)
    risk=RiskConfig(sizing_mode='fixed',max_total_cost=config.initial_cash,max_order_cost=maximum,
        max_market_cost=profile.budget,max_contracts=min(10000,max(1,int(maximum/Decimal('.03')))),
        min_seconds_to_close=1,max_signal_age_seconds=5,max_quote_age_seconds=5,
        fee_reserve_per_contract='.03',required_series=asset_spec(assets[0]).series if len(assets)==1 else None)
    simulation=_ScheduledSimulation(records,replay_config)
    with tempfile.TemporaryDirectory(prefix='kalshi-scheduled-research-') as folder:
        with Journal(Path(folder)/'simulated.sqlite3',mode='live',environment='production',risk=risk) as journal:
            journal.bind_account(_ACCOUNT)
            simulation.journal=journal
            executor=MinuteFiveLiveExecutor(simulation,journal,risk,assets=assets,clock=lambda:simulation.now,
                bet_budget=format(maximum,'f'),uniform_sizing=profile,entry_schedule='every-minute',
                warmup_seconds=0,cadence_seconds=config.cadence_seconds,allowance_mode='continuous',
                continue_unresolved=True,skip_probability_at_or_above='.90',exit_bid_above='.95',
                exit_seconds_before_close=900,resume_after_exit=True,stop_bid_at_or_below='.05',
                stop_seconds_before_close=180,late_model_exit_probability_above='.90',
                late_model_exit_seconds_before_close=180,
                **({} if scheduled_policy_config is None else dict(scheduled_policy_config=scheduled_policy_config)))
            reader=_ReadOnly(simulation)
            simulation.mark()
            for index,row in enumerate(records):
                when=row['_time']
                if when<simulation.now or index<=simulation.consumed_through:
                    simulation.audit['events_consumed_during_execution']+=1
                    continue
                simulation.advance(when)
                simulation.current_index=index
                with journal.exclusive():
                    refresh_settled_allowances(reader,journal,clock=lambda:simulation.now,max_markets=100)
                if row['kind']!='kalshi_value_observation' or simulation.audit['submitted_orders']>=config.max_actions:
                    simulation.mark();continue
                simulation.audit['decision_observations']+=1
                execution=executor.execute_exit(row['asset'])
                # Mirror the coordinator: execute() owns the model exit pass
                # after an ordinary price check. A protective skipped exit may
                # obtain fresh model evidence, but cannot fall through to entry.
                if execution is None:
                    execution=executor.execute(snapshots[index],price_exits_checked=True)
                elif execution.get('status')=='skipped' and not execution.get('client_order_id'):
                    initial_exit=execution
                    execution=executor.execute_exit(row['asset'],model_snapshot=snapshots[index])
                    if execution is None:
                        execution=initial_exit
                if execution is not None:
                    simulation.reasons[execution.get('reason','Unknown decision')]+=1
                    if simulation.actions and simulation.actions[-1].get('time')==simulation.now:
                        simulation.actions[-1].setdefault('decision_reason',execution.get('reason'))
                simulation.mark()
            audit=dict(simulation.audit,journal_intents=len(journal.intents()),
                active_reserved_cost=format(journal.active_committed(),'f'),
                lifetime_reserved_cost=format(journal.committed(),'f'))
    marked=simulation.mark()
    positions=[dict(ticker=ticker,net_position=quantity) for ticker,quantity in sorted(simulation.positions.items()) if quantity]
    return dict(status='ready',execution_eligible=False,research_only=True,
        native_executor='MinuteFiveLiveExecutor',regime_switching=False,rolling_kelly=False,
        initial_cash=format(simulation.initial,'f'),cash=format(simulation.cash,'f'),
        realized_pnl=format(simulation.realized,'f'),fees_paid=format(simulation.fees,'f'),
        gross_turnover=format(simulation.turnover,'f'),filled_actions=simulation.audit['filled_actions'],
        entry_fills=simulation.entries,exit_fills=simulation.exits,settlement_events=simulation.settlements,
        final_equity=None if positions else format(simulation.cash,'f'),
        marked_equity=None if marked is None else format(marked,'f'),open_positions=positions,
        max_drawdown=format(simulation.drawdown,'f'),max_drawdown_fraction=simulation.drawdown_fraction,
        actions=simulation.actions,decision_reasons=dict(simulation.reasons),audit=audit,
        equity_curve=simulation.equity_curve,omitted_action_rows=simulation.audit['omitted_action_rows'],
        profile=dict(initial_cash=format(config.initial_cash,'f'),uniform_market_budget=format(config.uniform_market_budget,'f'),
            cadence_seconds=config.cadence_seconds,probability_skip_at_or_above='.90',
            profit_bid_above='.95',profit_exit_window_seconds=900,resume_after_exit=True,
            emergency_stop_bid_at_or_below='.05',emergency_stop_window_seconds=180,
            frozen_late_model_probability_above='.90',frozen_late_model_window_seconds=180,
            source_and_quote_freshness_seconds=5,submission_cutoff_seconds=1,
            fee_reserve_per_contract='.03',execution_latency_seconds=config.execution_latency_seconds,
            max_execution_wait_seconds=config.max_execution_wait_seconds,depth_fraction=format(config.depth_fraction,'f')),
        assumptions=['Recorded later-book depth supports counterfactual fills, not guaranteed real executions.',
            'Only books received after fixed order arrival can fill; same-snapshot and already-consumed depth cannot fill twice.',
            'Official settlement cash is applied at recorded receipt, never at a future label lookup.',
            'Manual external trades are absent; all simulated activity belongs to the isolated journal.',
            'Each recorded snapshot is one coordinator visit; snapshots consumed during simulated IOC latency are not replayed.'])


def evaluate_scheduled_research(records, *, assets=None, config=None, synthetic=False):
    config=ScheduledResearchConfig() if config is None else config
    if not isinstance(config,ScheduledResearchConfig) or type(synthetic) is not bool:
        raise ScheduledResearchError('A validated research configuration is required')
    records=validate_records(records,max_events=config.max_events)
    selected=tuple(sorted(assets or {row['asset'] for row in records}))
    if not selected or not set(selected)<=set(ASSETS) or len(set(selected))!=len(selected):
        raise ScheduledResearchError('Select distinct supported research assets')
    records=[row for row in records if row['asset'] in selected]
    if not records:
        raise ScheduledResearchError('No records match the selected research assets')
    scores={asset:_score(records,asset,config) for asset in selected}
    from .scheduled_policy import ScheduledPolicyConfig
    variants={'baseline':None,
        'edge_only':ScheduledPolicyConfig(min_entry_edge='.01',probability_margin='0',slippage='.001'),
        'cooldown_only':ScheduledPolicyConfig(min_exit_cooldown_seconds=20,reentry_probability_move='.01')}
    replay={name:replay_scheduled(records,assets=selected,config=config,scheduled_policy_config=policy)
            for name,policy in variants.items()}
    return dict(kind='kalshi_scheduled_research_report',version=1,status='ready' if all(
        row['status']=='ready' for row in (*scores.values(),*replay.values())) else 'partial',
        execution_eligible=False,live_model_updated=False,research_only=True,
        synthetic=synthetic or any(row.get('synthetic') is True for row in records),
        assets=list(selected),event_count=len(records),forecast_evaluation=scores,policy_replays=replay,
        policy_parameters={name:None if policy is None else policy.to_dict() for name,policy in variants.items()},
        score_selection='whole_contracts_in_whole_UTC_day_blocks_with_900_second_purge',
        limitations=['Repeated forecasts of one contract are not independent trials.',
            'This report does not select or deploy a winning live strategy.',
            'Recorded native snapshots and source documents are mandatory for exact policy replay.',
            'Tick-only history cannot reconstruct historical executable books or fill latency.',
            'A supplied forecast fingerprint binds configurations, not independently verified training chronology.',
            'Forecast scoring uses officially labelled events, whereas execution results remain simulated.'])


def fit_history_archives(bindings):
    """Use existing guarded per-asset fitting with read-only historical archives."""
    from .calibration import CalibrationError,fit_probability_model
    from .history import HistoryArchive,HistoryError
    from .history_features import FeatureError,build_training_rows
    output={}
    for asset,path in bindings.items():
        asset_spec(asset)
        try:
            with HistoryArchive(path,read_only=True) as archive:
                prepared=build_training_rows(archive,asset=asset,history_seconds=1800)
            fitted=fit_probability_model(prepared['rows'],asset=asset)
            output[asset]=dict(status='ready',selected=fitted['selected'],metrics=fitted['metrics'],
                splits=fitted['splits'],counts=fitted['counts'],calibrated=False,
                calibration_status='fitted_research_not_live_validated',live_model_updated=False,
                payoff_scope='existing_unrounded_historical_volatility_model_approximation',
                rounded_live_payoff_validated=False)
        except (CalibrationError,HistoryError,FeatureError) as error:
            output[asset]=dict(status='insufficient_data',reason=str(error),calibrated=False,live_model_updated=False)
    return output


def demo_records(assets=tuple(ASSETS)):
    """Small explicitly synthetic input illustrating all four source bindings."""
    records=[]
    first=datetime(2026,1,1,tzinfo=timezone.utc).timestamp()
    for day in range(6):
        for asset_index,asset in enumerate(assets):
            spec=asset_spec(asset)
            opened=first+day*86400+asset_index*1800
            closed=opened+900
            event=spec.series+'-DEMO-'+str(day)
            digits=2 if asset in ('BTC','ETH') else 4
            market=dict(ticker=event+'-00',event_ticker=event,status='active',market_type='binary',
                settlement_bounds_type='default',notional_value_dollars='1.0000',floor_strike='100',
                cap_strike=None,strike_type='greater_or_equal',open_time=_iso(opened),close_time=_iso(closed),
                rules_primary="Compare the simple average of the sixty seconds of CF Benchmarks' "+spec.index_id+" before expiration against the opening average.",
                rules_secondary='The final value is the average, rounded to the nearest '+str(digits)+' decimal places.',
                custom_strike=dict(round_digits=str(digits)),
                price_ranges=[dict(start='0.0000',end='1.0000',step='0.0001')])
            series=dict(ticker=spec.series,frequency='fifteen_min',fee_type='quadratic',fee_multiplier='1',
                settlement_sources=[dict(name='CF Benchmarks',url='https://www.cfbenchmarks.com/')])
            event_meta=dict(event_ticker=event,series_ticker=spec.series)
            market['_crypto15m']=dict(verified_crypto_settlement_source(series,market,event_meta,asset=asset),
                fee_type='quadratic',fee_multiplier='1',
                settlement_source_evidence=dict(series=series,event=event_meta))
            identity=_identity(market)
            for offset in (60,61,65,66):
                now=opened+offset
                version='cf-'+spec.index_id.lower().replace('_','-')+'-minute-five-median-v1'
                forecast=dict(kind='kalshi_minute_five_forecast',version=1,status='ready',asset=asset,
                    source=spec.source,model_version=version,as_of=now,model_time=now,
                    forecast_completed_at=now,ticker=market['ticker'],probability_yes=.6,probability_no=.4,
                    median_outcome='yes',median_settlement_average=101.,settlement_centered_median=1.,
                    strike_type='greater_or_equal',target_price='100',contract_identity=identity)
                native=dict(kind='kalshi_minute_five_observation',version=1,observation_only=True,
                    status='ready',strategy='minute_five',asset=asset,fetched_at=_iso(now),forecast=forecast,
                    market_snapshot=dict(market_status='ready',fetched_at=_iso(now),market=dict(ticker=market['ticker'])))
                records.append(dict(kind='kalshi_scheduled_observation',version=1,synthetic=True,asset=asset,
                    observed_at=now,model_time=now,quote_time=now,ticker=market['ticker'],
                    contract_identity=identity,probability_yes=.6,yes_bids=[['0.4000','100.00']],
                    no_bids=[['0.5600','100.00']],fee_type='quadratic',fee_multiplier='1',
                    fee_quantum='.0001',source=spec.source,model_version=version,
                    model_fingerprint=hashlib.sha256((asset+'-demo').encode()).hexdigest(),
                    market=deepcopy(market),native_snapshot=native))
            final=dict(market,status='finalized',is_provisional=False,
                result='yes' if day%2 else 'no',settlement_ts=_iso(closed+2),
                settlement_value_dollars='1' if day%2 else '0')
            records.append(dict(kind='kalshi_scheduled_settlement',version=1,synthetic=True,asset=asset,
                available_at=closed+10,settled_at=closed+2,ticker=market['ticker'],
                contract_identity=identity,result=final['result'],market=final))
    records.sort(key=lambda row:row.get('observed_at',row.get('available_at')))
    return records
