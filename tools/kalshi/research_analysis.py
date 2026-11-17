"""Read-only descriptive research over private, append-only recorder shards.

Reservations and order acknowledgements are not realized P&L. Financial
summaries are copied only from explicitly covered authoritative activity audits;
their cumulative journal scopes are never summed across runs.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import zlib
from urllib.parse import unquote


class ResearchAnalysisError(ValueError):
    """Fixed, non-sensitive capture/query error."""


_SHARD = re.compile(r'[A-Za-z0-9_-]{1,80}--[a-f0-9]{32}\.sqlite3\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_MAX_PAYLOAD = 8_000_000
_IDENTITY = ('ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
             'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        if len(str(value)) > 100:
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _decimal_text(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        if len(str(value)) > 100:
            return None
        result = Decimal(value)
        if not result.is_finite():
            return None
        digits = result.as_tuple()
        # Bound fixed-point expansion before formatting even a tiny coefficient
        # carrying an enormous negative exponent from an untrusted capture.
        if not -30 <= digits.exponent <= 20 or len(digits.digits) > 100:
            return None
        return format(result, 'f') if result.copy_abs() < Decimal('1e20') else None
    except (ValueError, InvalidOperation, TypeError, OverflowError):
        return None


def _stamp(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def _json(value):
    try:
        return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError):
        raise ResearchAnalysisError('Research JSON is invalid') from None


def _decode(data, sha256, raw_bytes):
    if (not isinstance(sha256, str) or not _HASH.fullmatch(sha256)
            or type(raw_bytes) is not int or not 0 <= raw_bytes <= _MAX_PAYLOAD
            or not isinstance(data, bytes) or len(data) > _MAX_PAYLOAD + 100000):
        raise ResearchAnalysisError('Research payload bounds are invalid')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(data, _MAX_PAYLOAD + 1)
        if (len(raw) != raw_bytes or len(raw) > _MAX_PAYLOAD or not decoder.eof
                or decoder.unconsumed_tail or decoder.unused_data
                or hashlib.sha256(raw).hexdigest() != sha256):
            raise ValueError()
        value = _json(raw)
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')
        if canonical != raw:
            raise ValueError()
        return value
    except (ValueError, zlib.error):
        raise ResearchAnalysisError('Research payload integrity check failed') from None


def latency_summary(values):
    values = sorted(number for value in values if (number := _number(value)) is not None and number >= 0)
    def quantile(q):
        if not values:
            return None
        offset = (len(values)-1)*q
        lower = math.floor(offset)
        return values[lower] + (values[math.ceil(offset)]-values[lower])*(offset-lower)
    return dict(sample_count=len(values), p50_seconds=quantile(.5), p95_seconds=quantile(.95),
        p99_seconds=quantile(.99), max_seconds=max(values) if values else None,
        quantile_method='linear interpolation of ordered observations')


def _request_attempt_group():
    return dict(count=0, retries=0, outcomes=Counter(), errors=Counter(), durations=[],
        http_statuses=Counter(), confirmed_transport_attempt_count=0,
        local_nontransport_attempt_count=0, unknown_transport_attempt_count=0,
        confirmed_transport_http_statuses=Counter(), local_nontransport_http_statuses=Counter(),
        unknown_transport_http_statuses=Counter())


def _record_request_attempt(group, payload):
    """Count completed capture records without inferring unrecorded wire sends."""
    group['count'] += 1
    group['outcomes'][str(payload.get('outcome', 'unknown'))] += 1
    if payload.get('error_code'):
        group['errors'][str(payload['error_code'])] += 1
    if type(payload.get('attempt_index')) is int and payload['attempt_index'] > 1:
        group['retries'] += 1
    status = payload.get('http_status')
    status = str(status) if type(status) is int else 'no_http_status'
    group['http_statuses'][status] += 1
    flag = payload.get('transport_attempted')
    population = ('confirmed_transport' if flag is True else
        'local_nontransport' if flag is False else 'unknown_transport')
    group[population+'_attempt_count'] += 1
    group[population+'_http_statuses'][status] += 1
    duration = _number(payload.get('duration_seconds'))
    if duration is not None and duration >= 0:
        group['durations'].append(duration)


def _request_attempt_summary(group):
    record_scope = 'Completed request_attempt_end event records; not distinct logical requests or deduplicated attempt identities'
    scopes = dict(attempt_count=record_scope, completed_attempt_record_count=record_scope,
        retry_attempt_count='Completed attempt records with integer attempt_index greater than one',
        http_statuses='All completed attempt records, including local and unknown transport flags; no_http_status is an explicit missing-status bucket',
        latency='Finite nonnegative duration_seconds on completed attempt records, including local work; not pure network latency')
    result = dict(attempt_count=group['count'], completed_attempt_record_count=group['count'],
        retry_attempt_count=group['retries'], outcomes=dict(group['outcomes']),
        error_codes=dict(group['errors']), latency=latency_summary(group['durations']),
        http_statuses=dict(group['http_statuses']))
    for population, flag_scope in (('confirmed_transport', 'transport_attempted is exactly true'),
            ('local_nontransport', 'transport_attempted is exactly false'),
            ('unknown_transport', 'transport_attempted is missing or not a boolean')):
        count_key, status_key = population+'_attempt_count', population+'_http_statuses'
        result[count_key] = group[count_key]
        result[status_key] = dict(group[status_key])
        scopes[count_key] = 'Completed attempt records where '+flag_scope
        scopes[status_key] = 'HTTP status buckets within '+count_key+', including no_http_status'
    result['confirmed_transport_rate_limited_count'] = group['confirmed_transport_http_statuses']['429']
    scopes['confirmed_transport_rate_limited_count'] = 'Completed attempt records where transport_attempted is exactly true and http_status is integer 429'
    result['count_scopes'] = scopes
    return result


def _request_asset(event):
    """Use explicit recording context only, without parsing assets from URLs."""
    asset = event.get('asset')
    if asset is None and isinstance(event.get('context'), dict):
        asset = event['context'].get('asset')
    return asset if isinstance(asset, str) and re.fullmatch(r'[A-Z0-9]{1,20}', asset) else None


class ResearchReader:
    """Bounded snapshots of existing shard files; no writers or migrations."""

    def __init__(self, directory, *, run_id=None, assets=(), max_events=100000, max_shards=1024):
        self.directory = Path(directory).expanduser().absolute()
        if (any(path.is_symlink() for path in (self.directory, *self.directory.parents))
                or not self.directory.is_dir()):
            raise ResearchAnalysisError('The existing research directory is unavailable')
        if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id)):
            raise ResearchAnalysisError('The research run identifier is invalid')
        if (not isinstance(assets, (tuple, list)) or any(not isinstance(a, str)
                or not re.fullmatch(r'[A-Z0-9]{1,20}', a) for a in assets)
                or type(max_events) is not int or not 1 <= max_events <= 1000000
                or type(max_shards) is not int or not 1 <= max_shards <= 4096):
            raise ResearchAnalysisError('Research query bounds or assets are invalid')
        self.run_id, self.assets, self.max_events = run_id, set(assets), max_events
        self.shards = sorted(path for path in self.directory.glob('*.sqlite3') if _SHARD.fullmatch(path.name)
            and (run_id is None or path.name.startswith(run_id + '--')))
        if len(self.shards) > max_shards:
            raise ResearchAnalysisError('Research directory exceeds the shard query bound')
        self.runs, self.issues, self.event_count, self.scanned_count, self.truncated = [], [], 0, 0, False
        self._cache, self._cache_bytes = OrderedDict(), 0

    def _payload(self, row):
        sha = row['payload_sha256']
        if row['encoding'] != 'zlib-json-v1':
            raise ResearchAnalysisError('Research blob encoding is unsupported')
        if sha in self._cache:
            if row['raw_bytes'] != self._cache[sha][1]:
                raise ResearchAnalysisError('Research blob size is inconsistent')
            self._cache.move_to_end(sha)
            return self._cache[sha][0]
        payload = _decode(row['data'], sha, row['raw_bytes'])
        size = row['raw_bytes']
        while self._cache and (self._cache_bytes + size > _MAX_PAYLOAD or len(self._cache) >= 64):
            _, (_, removed) = self._cache.popitem(last=False)
            self._cache_bytes -= removed
        self._cache[sha] = (payload, size)
        self._cache_bytes += size
        return payload

    def events(self):
        seen = set()
        for path in self.shards:
            if self.truncated:
                break
            # A hash is unique only inside a shard's stable read transaction.
            # Reverify separately persisted blobs with the same declared hash.
            self._cache.clear()
            self._cache_bytes = 0
            try:
                if (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
                        or any(Path(str(path)+suffix).is_symlink() for suffix in ('-wal', '-shm', '-journal'))):
                    raise ResearchAnalysisError('Research shard is not a regular private file')
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)) as connection:
                    connection.row_factory = sqlite3.Row
                    if hasattr(connection, 'setlimit'):
                        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2*_MAX_PAYLOAD+100000)
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('PRAGMA trusted_schema=OFF')
                    connection.execute('BEGIN')
                    runs = [dict(row) for row in connection.execute('SELECT * FROM research_runs LIMIT 2')]
                    if len(runs) != 1:
                        raise ResearchAnalysisError('Research shard run identity is ambiguous')
                    run = runs[0]
                    if path.name != str(run['run_id'])+'--'+str(run['producer_id'])+'.sqlite3':
                        raise ResearchAnalysisError('Research shard filename and identity differ')
                    health_raw = run.pop('health_json')
                    if not isinstance(health_raw, str) or len(health_raw) > 100000:
                        raise ResearchAnalysisError('Research capture health exceeds its bound')
                    run['health'] = _json(health_raw)
                    if not isinstance(run['health'], dict):
                        raise ResearchAnalysisError('Research capture health is invalid')
                    metadata = connection.execute('SELECT sha256 AS payload_sha256,encoding,raw_bytes,data '
                        'FROM research_blobs WHERE sha256=?', (run['metadata_sha256'],)).fetchone()
                    if metadata is None:
                        raise ResearchAnalysisError('Research run metadata is missing')
                    run['metadata'] = self._payload(metadata)
                    self.runs.append(run)
                    columns = {r[1] for r in connection.execute('PRAGMA table_info(research_events)')}
                    context = 'e.context_json' if 'context_json' in columns else "'{}' AS context_json"
                    query = ('SELECT e.event_id,e.run_id,e.producer_id,e.sequence,e.kind,e.recorded_at,e.observed_at,'
                        'e.trace_id,e.decision_id,e.asset,e.ticker,e.request_id,e.parent_id,e.payload_sha256,'
                        + context + ',b.encoding,b.raw_bytes,b.data FROM research_events e '
                        'LEFT JOIN research_blobs b ON b.sha256=e.payload_sha256 ORDER BY e.sequence')
                    previous, shard_count = 0, 0
                    for row in connection.execute(query):
                        if self.scanned_count >= self.max_events:
                            self.truncated = True
                            break
                        self.scanned_count += 1
                        shard_count += 1
                        if (row['run_id'] != run['run_id'] or row['producer_id'] != run['producer_id']
                                or type(row['sequence']) is not int or row['sequence'] <= previous
                                or not isinstance(row['event_id'], str) or row['event_id'] in seen
                                or not isinstance(row['kind'], str) or not 1 <= len(row['kind']) <= 100):
                            raise ResearchAnalysisError('Research event identity is invalid or duplicated')
                        if row['sequence'] != previous + 1:
                            self.issues.append('A producer sequence has gaps; some capture may be missing')
                        previous = row['sequence']
                        seen.add(row['event_id'])
                        if self.assets and row['asset'] not in self.assets:
                            continue
                        event = {key: row[key] for key in ('event_id','run_id','producer_id','sequence','kind',
                            'recorded_at','observed_at','trace_id','decision_id','asset','ticker','request_id','parent_id','payload_sha256')}
                        if not isinstance(row['context_json'],str) or len(row['context_json']) > 100000:
                            raise ResearchAnalysisError('Research event context exceeds its bound')
                        event['context'] = _json(row['context_json'])
                        if not isinstance(event['context'],dict):
                            raise ResearchAnalysisError('Research event context is invalid')
                        event['payload'] = self._payload(row)
                        self.event_count += 1
                        yield event
                    if not self.truncated and run['health'].get('written_count') != shard_count:
                        self.issues.append('A shard event count differs from its saved capture health')
                    connection.rollback()
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                self.issues.append('A research shard could not be read or failed integrity validation')

    def coverage(self):
        producers, manifests, observed, manifest_issues = [], {}, defaultdict(set), []
        manifest_coordinators = set()
        for row in self.runs:
            health = row['health']
            producers.append(dict(run_id=row['run_id'], producer_id=row['producer_id'], role=row['role'],
                asset=row['asset'], closed=row.get('closed_at') is not None, health=health))
            observed[row['run_id']].add((row['role'], row['asset']))
            metadata = row.get('metadata')
            if not isinstance(metadata,dict) or 'expected_producers' not in metadata:
                continue
            manifest, expected = metadata['expected_producers'], set()
            valid = isinstance(manifest,list) and 1 <= len(manifest) <= 256
            if valid:
                for item in manifest:
                    if (not isinstance(item,dict) or set(item) != {'role','asset'}
                            or not isinstance(item['role'],str)
                            or re.fullmatch('[a-z][a-z0-9_]{0,63}',item['role']) is None
                            or item['asset'] is not None and (not isinstance(item['asset'],str)
                                or re.fullmatch('[A-Z][A-Z0-9_]{0,31}',item['asset']) is None)):
                        valid = False
                        break
                    identity = (item['role'],item['asset'])
                    if identity in expected:
                        valid = False
                        break
                    expected.add(identity)
            if not valid:
                manifest_issues.append('An expected research producer manifest is malformed or exceeds its bound')
                continue
            if row['role'] == 'coordinator':
                manifest_coordinators.add(row['run_id'])
            if row['run_id'] in manifests and manifests[row['run_id']] != expected:
                manifest_issues.append('Expected research producer manifests conflict within a run')
            else:
                manifests[row['run_id']] = expected
        for run_id, expected in manifests.items():
            for role, asset in sorted(expected-observed[run_id],key=lambda item:(item[0],item[1] or '')):
                manifest_issues.append('An expected research producer is missing: '+role+'/'+(asset or 'account'))
        for run_id, identities in observed.items():
            if any(role == 'observation_worker' for role, _ in identities) and run_id not in manifest_coordinators:
                manifest_issues.append('An observation worker run lacks a coordinator with a valid expected producer manifest')
        issues = list(dict.fromkeys(self.issues+manifest_issues))
        complete = (bool(self.shards) and len(self.runs) == len(self.shards) and not issues
            and not self.truncated and all(row['closed'] and row['health'].get('capture_complete') is True
                for row in producers))
        return dict(complete=bool(complete), truncated=self.truncated, selected_event_count=self.event_count,
            scanned_event_count=self.scanned_count,
            shard_count=len(self.shards), readable_run_count=len(self.runs), producers=producers,
            producer_manifest_run_count=len(manifests),
            issues=issues,
            reason='Closed loss-free capture snapshots' if complete else
                'Capture is active, incomplete, truncated, empty, or contains integrity failures')


def _probability_band(value):
    value = _number(value)
    if value is None or not 0 <= value <= 1:
        return 'unknown'
    if value < .5:
        return 'below_50pct'
    for upper, label in ((.6, '50_to_60pct'), (.7, '60_to_70pct'), (.8, '70_to_80pct'),
                         (.9, '80_to_90pct'), (.95, '90_to_95pct')):
        if value < upper:
            return label
    return '95_to_100pct'


def _time_band(value):
    value = _number(value)
    if value is None or value < 0:
        return 'unknown'
    return '0_to_60s' if value <= 60 else '60_to_300s' if value <= 300 else '300_to_600s' if value <= 600 else 'over_600s'


def _decision(event):
    payload = event['payload'] if isinstance(event['payload'], dict) else {}
    observation, execution = payload.get('observation'), payload.get('execution')
    observation = observation if isinstance(observation, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    forecast = observation.get('forecast') or {}
    forecast = forecast if isinstance(forecast, dict) else {}
    side = execution.get('outcome', forecast.get('median_outcome'))
    p_yes = _number(forecast.get('probability_yes'))
    probability = None if p_yes is None else p_yes if side == 'yes' else 1-p_yes if side == 'no' else None
    remaining = execution.get('minutes_remaining')
    remaining = None if _number(remaining) is None else float(remaining)*60
    if remaining is None:
        identity = forecast.get('contract_identity')
        identity = identity if isinstance(identity,dict) else {}
        closed = _stamp(identity.get('close_time'))
        as_of = _stamp(forecast.get('as_of'))
        if closed is not None and as_of is not None:
            remaining = closed-as_of
    return payload, observation, execution, forecast, side, probability, remaining


def _audit(payload, event):
    if not isinstance(payload, dict):
        return None
    report = payload.get('report', payload)
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_scheduled_activity_report'
            or report.get('read_only') is not True or report.get('execution_eligible') is not False
            or report.get('reservations_are_actual_costs') is not False):
        return None
    coverage = report.get('coverage')
    coverage = coverage if isinstance(coverage,dict) else {}
    actual_fees = _decimal_text(report.get('actual_fees'))
    pnl = _decimal_text(report.get('total_realized_pnl'))
    complete = (coverage.get('complete') is True and report.get('fees_complete') is True
        and actual_fees is not None and Decimal(actual_fees) >= 0 and pnl is not None
        and type(report.get('actual_fill_count')) is int and report['actual_fill_count'] >= 0)
    return dict(run_id=event['run_id'], observed_at=event['recorded_at'], journal_id=payload.get('journal_id'),
        accounting_scope=report.get('scope'), cumulative_journal_accounting=True,
        complete=bool(complete), coverage=coverage, actual_fill_count=report.get('actual_fill_count'),
        actual_fees=actual_fees if complete else None,
        total_realized_pnl=pnl if complete else None,
        known_actual_fees=_decimal_text(report.get('known_actual_fees')),
        known_total_realized_pnl=_decimal_text(report.get('known_total_realized_pnl')),
        issues=report.get('issues', []))


def _contract_identity(market):
    from .research_outcomes import contract_identity_diagnostics
    return contract_identity_diagnostics(market)['identity']


class _Calibration:
    """One latest causal forecast per contract/configuration/horizon, not per fill."""
    def __init__(self):
        self.predictions, self.labels, self.conflicts = {}, {}, set()
        self.invalid_labels = 0
        self.invalid_label_reasons, self.schema_normalizations = Counter(), Counter()
        self.rejection_examples = []

    def prediction(self, event, observation):
        if not isinstance(observation, dict) or observation.get('status') != 'ready':
            return
        forecast = observation.get('forecast')
        if not isinstance(forecast, dict) or forecast.get('status') != 'ready':
            return
        identity = _contract_identity(forecast.get('contract_identity'))
        p, source_at, as_of = _number(forecast.get('probability_yes')), _stamp(forecast.get('model_time')), _stamp(forecast.get('as_of'))
        if identity is None or p is None or not 0 <= p <= 1 or source_at is None or as_of is None:
            return
        try:
            from .crypto_assets import asset_spec
            profile = asset_spec(observation.get('asset'))
        except ValueError:
            return
        captured = event['recorded_at']
        if (forecast.get('asset') != profile.symbol or forecast.get('source') != profile.source
                or not identity['ticker'].startswith(profile.series+'-')
                or not identity['open_time'] <= source_at <= as_of <= captured < identity['close_time']):
            return
        model = forecast.get('model_version')
        if not isinstance(model, str) or not model:
            return
        fitted = forecast.get('fitted_parameters')
        fitted = fitted if isinstance(fitted,dict) else {}
        config = forecast.get('config_hash') or fitted.get('artifact_sha256') or ('run:'+event['run_id'])
        if not isinstance(config,str) or len(config)>200:
            return
        band = _time_band(identity['close_time']-source_at)
        key = (profile.symbol,model,config,band,identity['ticker'])
        payload = event['payload']
        source_id = (observation.get('research_observation_id') or payload.get('source_observation_id')
            or event.get('context',{}).get('observation_id') or event['event_id'])
        candidate = dict(identity=identity, probability=p, model_time=source_at, as_of=as_of,
            captured_at=captured, source_observation_id=source_id, event_id=event['event_id'])
        prior = self.predictions.get(key)
        if prior is None or (source_at,as_of,captured) > (prior['model_time'],prior['as_of'],prior['captured_at']):
            self.predictions[key] = candidate

    def outcome(self,event,payload):
        if (payload.get('method') != 'GET' or payload.get('outcome') != 'completed'
                or type(payload.get('http_status')) is not int or not 200 <= payload['http_status'] < 300):
            return
        response = payload.get('response')
        market = response.get('market') if isinstance(response,dict) else None
        if not isinstance(market,dict) or market.get('status') != 'finalized':
            return
        from .research_outcomes import validate_final_market
        checked = validate_final_market(market, observed_at=event.get('recorded_at'))
        path = unquote(str(payload.get('path','')).split('?',1)[0])
        if checked['identity'] is None or path != '/markets/' + checked['identity']['ticker']:
            checked['rejection_codes'].append('point_market_path_mismatch')
            checked['valid'] = False
        self._outcome_evidence(checked, market, source='captured_get_market')

    def outcome_label(self, event, payload):
        """Consume an explicit point-read backfill without backdating availability."""
        from .research_outcomes import validate_final_market
        payload = payload if isinstance(payload, dict) else {}
        market = payload.get('market')
        checked = validate_final_market(market, observed_at=event.get('recorded_at'),
            expected_ticker=payload.get('ticker'), expected_identity=payload.get('contract_identity'))
        if (event.get('kind') != 'market_outcome' or payload.get('source') != 'kalshi_get_market'
                or not isinstance(payload.get('ticker'), str)):
            checked['rejection_codes'].append('backfill_provenance_invalid')
        retrieved = _stamp(payload.get('retrieved_at'))
        recorded = checked['label_observed_at']
        if retrieved is None or retrieved < 0:
            checked['rejection_codes'].append('retrieval_clock_missing_or_invalid')
        elif recorded is None or retrieved > recorded:
            checked['rejection_codes'].append('retrieval_follows_recording')
        elif checked['settlement_at'] is not None and checked['settlement_at'] > retrieved:
            checked['rejection_codes'].append('settlement_follows_retrieval')
        checked['rejection_codes'] = list(dict.fromkeys(checked['rejection_codes']))
        checked['valid'] = not checked['rejection_codes']
        self._outcome_evidence(checked, market, source='outcome_backfill')

    def _outcome_evidence(self, checked, market, *, source):
        """Retain bounded fixed-code diagnostics; never arbitrary venue messages."""
        if not checked['valid']:
            self.invalid_labels += 1
            self.invalid_label_reasons.update(checked['rejection_codes'])
            ticker = market.get('ticker') if isinstance(market, dict) else None
            ticker = ticker if isinstance(ticker, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', ticker) else None
            example = dict(ticker=ticker, source=source, rejection_codes=checked['rejection_codes'])
            if len(self.rejection_examples) < 32 and example not in self.rejection_examples:
                self.rejection_examples.append(example)
            return
        self.schema_normalizations.update(checked['normalizations'])
        identity, ticker = checked['identity'], checked['identity']['ticker']
        value = dict(identity=identity, yes=checked['outcome_yes'],
            observed_at=checked['label_observed_at'], settlement_at=checked['settlement_at'])
        prior = self.labels.get(ticker)
        if prior is not None and (prior['identity'] != identity or prior['yes'] != value['yes']):
            self.conflicts.add(ticker)
        elif prior is None or value['observed_at'] < prior['observed_at']:
            self.labels[ticker] = value

    def graded_samples(self):
        """Expose the same causal contract/horizon samples for temporal scoring."""
        rows = []
        for (asset, model, config, band, ticker), prediction in self.predictions.items():
            label = self.labels.get(ticker)
            if (ticker in self.conflicts or label is None
                    or prediction['identity'] != label['identity']
                    or prediction['captured_at'] >= label['observed_at']):
                continue
            rows.append(dict(asset=asset, ticker=ticker, model_version=model,
                configuration=config, time_remaining_band=band,
                open_time=prediction['identity']['open_time'],
                close_time=prediction['identity']['close_time'],
                model_time=prediction['model_time'], prediction_recorded_at=prediction['captured_at'],
                label_observed_at=label['observed_at'], probability_yes=prediction['probability'],
                outcome_yes=label['yes']))
        return rows

    def summary(self):
        groups = {}
        ungraded = mismatched = 0
        for (asset,model,config,band,ticker), prediction in self.predictions.items():
            label = self.labels.get(ticker)
            if ticker in self.conflicts or label is None:
                ungraded += 1
                continue
            if prediction['identity'] != label['identity'] or prediction['captured_at'] >= label['observed_at']:
                mismatched += 1
                continue
            key = (asset,model,config,band)
            group = groups.setdefault(key,dict(count=0,brier=0.0,log_loss=0.0,reliability={},zero_probability_wrong=0))
            probability, outcome = prediction['probability'],label['yes']
            group['count'] += 1
            group['brier'] += (probability-outcome)**2
            realized_probability = probability if outcome else 1-probability
            if realized_probability == 0:
                group['zero_probability_wrong'] += 1
            else:
                group['log_loss'] -= math.log(realized_probability)
            probability_band = _probability_band(probability)
            bucket = group['reliability'].setdefault(probability_band,dict(count=0,sum_probability=0.0,yes_count=0))
            bucket['count'] += 1
            bucket['sum_probability'] += probability
            bucket['yes_count'] += outcome
        rows=[]
        for (asset,model,config,band),group in sorted(groups.items()):
            rows.append(dict(asset=asset,model_version=model,configuration=config,time_remaining_band=band,
                contract_count=group['count'],brier_score=group['brier']/group['count'],
                log_loss=None if group['zero_probability_wrong'] else group['log_loss']/group['count'],
                infinite_log_loss_contract_count=group['zero_probability_wrong'],
                reliability=[dict(probability_band=name,contract_count=bucket['count'],
                    mean_probability_yes=bucket['sum_probability']/bucket['count'],
                    observed_yes_fraction=bucket['yes_count']/bucket['count'])
                    for name,bucket in sorted(group['reliability'].items())]))
        return dict(status='descriptive' if rows else 'not_computed',groups=rows,
            candidate_contract_horizon_count=len(self.predictions),verified_final_market_count=len(self.labels)-len(self.conflicts),
            ungraded_contract_horizon_count=ungraded,identity_mismatch_count=mismatched,
            conflicting_outcome_market_count=len(self.conflicts),invalid_final_response_count=self.invalid_labels,
            invalid_final_response_reasons=dict(self.invalid_label_reasons),
            invalid_final_response_examples=list(self.rejection_examples),
            outcome_schema_normalizations=dict(self.schema_normalizations),
            sampling_rule='Latest captured pre-expiry forecast by original model_time, one per exact contract/model configuration/time-remaining bucket; parent/worker copies collapse',
            independent_sample_claim=False,reason='Requires exact finalized ordinary-binary market identity, settlement time and binary payout; documented optional omissions are normalized; no confidence intervals, optimization or out-of-sample promotion are implied')


def analyze_research(directory, *, run_id=None, assets=(), max_events=100000, export_stream=None,
                     evaluation_start=None):
    """Summarize every recorded decision, including no-trade opportunities.

    Frequencies are descriptive. Repeated decisions share the same market and
    are not independent trials. Profit attribution is never inferred from them.
    """
    from .research_ledger import ContractLedger
    from .research_scorecard import build_scorecard
    from .research_health import build_recording_health
    if evaluation_start is not None:
        evaluation_start = _stamp(evaluation_start)
        if evaluation_start is None or evaluation_start < 0:
            raise ResearchAnalysisError('Use an evaluation cutoff with an explicit timezone')
    reader = ResearchReader(directory, run_id=run_id, assets=assets, max_events=max_events)
    ledger, last_event_times = ContractLedger(), {}
    kinds, logical_outcomes, attempt_outcomes = Counter(), Counter(), Counter()
    endpoints, buckets, reasons, audits = {}, {}, Counter(), {}
    logical_times, decision_times = [], []
    coordination_times, discovery_counts = [], defaultdict(Counter)
    attempt_statistics = _request_attempt_group()
    asset_attempt_statistics, asset_context_missing_count = defaultdict(_request_attempt_group), 0
    request_starts, request_ends, attempt_starts, attempt_ends = set(), set(), set(), set()
    unique_orders, forecasts = set(), {}
    decisions = ready = no_action_polls = 0
    stages = Counter()
    calibration = _Calibration()
    for event in reader.events():
        ledger.consume(event)
        producer = (event['run_id'], event['producer_id'])
        event_time = _number(event.get('recorded_at'))
        if event_time is not None:
            last_event_times[producer] = max(last_event_times.get(producer, event_time), event_time)
        if export_stream is not None:
            export_stream.write(json.dumps(dict(kind='kalshi_research_export_event', version=1, event=event),
                sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n')
        kinds[event['kind']] += 1
        payload = event['payload'] if isinstance(event['payload'], dict) else {}
        request = event.get('request_id') or payload.get('request_id')
        if not isinstance(request,str):
            request = 'missing:'+event['event_id']
        request_key = (event['run_id'], event['producer_id'], request)
        if event['kind'] == 'request_start':
            request_starts.add(request_key)
        elif event['kind'] == 'request_end':
            request_ends.add(request_key)
            logical_outcomes[str(payload.get('outcome', 'unknown'))] += 1
            duration = _number(payload.get('duration_seconds'))
            if duration is not None and duration >= 0:
                logical_times.append(duration)
        elif event['kind'] in ('request_attempt_start', 'request_attempt_end'):
            attempt_id = payload.get('attempt_id', payload.get('attempt_index'))
            if not isinstance(attempt_id,(str,int)):
                attempt_id = 'missing:'+event['event_id']
            attempt_key = request_key + (attempt_id,)
            if event['kind'] == 'request_attempt_start':
                attempt_starts.add(attempt_key)
            else:
                calibration.outcome(event,payload)
                attempt_ends.add(attempt_key)
                outcome = str(payload.get('outcome', 'unknown'))
                attempt_outcomes[outcome] += 1
                _record_request_attempt(attempt_statistics, payload)
                path = str(payload.get('path', 'unknown'))
                path = path.split('?', 1)[0].split('#', 1)[0]
                path = re.sub(r'^(/(?:historical/)?(?:markets|events|series)/)[^/]+', r'\1{identifier}', path)
                path = re.sub(r'^(/portfolio/orders/)[^/]+', r'\1{identifier}', path)
                endpoint = str(payload.get('method', 'unknown'))+' '+path
                if endpoint not in endpoints:
                    endpoints[endpoint] = dict(attempts=_request_attempt_group(),
                        assets=defaultdict(_request_attempt_group), asset_context_missing_count=0)
                group = endpoints[endpoint]
                _record_request_attempt(group['attempts'], payload)
                asset = _request_asset(event)
                if asset is None:
                    asset_context_missing_count += 1
                    group['asset_context_missing_count'] += 1
                else:
                    _record_request_attempt(asset_attempt_statistics[asset], payload)
                    _record_request_attempt(group['assets'][asset], payload)
                wait = _number(payload.get('read_coordination_delay_seconds'))
                if wait is not None and wait >= 0:
                    coordination_times.append(wait)
        elif event['kind'] == 'decision':
            decisions += 1
            stage = str(payload.get('stage', 'unknown'))
            stages[stage] += 1
            calibration.prediction(event,payload.get('observation'))
            if not isinstance(payload.get('execution'), dict) or not payload['execution']:
                no_action_polls += 1
                continue
            _, observation, execution, forecast, side, probability, remaining = _decision(event)
            ready += observation.get('status') == 'ready'
            duration = _number(payload.get('duration_seconds'))
            if duration is not None and duration >= 0:
                decision_times.append(duration)
            status = execution.get('status', 'unavailable')
            asset = event.get('asset') or observation.get('asset') or 'unknown'
            asset = asset if isinstance(asset,str) else 'unknown'
            key = (stage, asset, side if side in ('yes', 'no') else 'unknown', _time_band(remaining), _probability_band(probability))
            bucket = buckets.setdefault(key, Counter())
            bucket['decisions'] += 1
            bucket[str(status)] += 1
            bucket['ready_observations'] += observation.get('status') == 'ready'
            if status == 'skipped':
                reasons[(stage, asset, str(execution.get('reason', 'No recorded reason'))[:500])] += 1
            client_order = execution.get('client_order_id')
            if isinstance(client_order, str) and client_order:
                unique_orders.add((event['run_id'], client_order))
            ticker = forecast.get('ticker')
            if isinstance(ticker, str):
                forecasts[(asset, ticker)] = True
        elif event['kind'] == 'activity_audit':
            audit = _audit(payload, event)
            if audit is not None and (event['run_id'] not in audits or event['recorded_at'] >= audits[event['run_id']]['observed_at']):
                audits[event['run_id']] = audit
        elif event['kind'] == 'observation':
            calibration.prediction(event,payload.get('observation'))
        elif event['kind'] == 'discovery_cache':
            asset = payload.get('asset')
            asset = asset if isinstance(asset, str) else 'unknown'
            if type(payload.get('hit')) is bool:
                discovery_counts[asset]['hit_count' if payload['hit'] else 'refresh_count'] += 1
            else:
                discovery_counts[asset]['unknown_count'] += 1
        elif event['kind'] == 'market_outcome':
            calibration.outcome_label(event, payload)
        elif event['kind'] == 'capture_shutdown_gap':
            reader.issues.append('Asynchronous capture sources were still active at shutdown')
    if request_starts != request_ends or attempt_starts != attempt_ends:
        reader.issues.append('Some recorded request starts or ends have no matching counterpart; their complete outcomes remain unknown')
    gap_counts = defaultdict(Counter)
    for keys, name in ((request_starts-request_ends, 'logical_missing_end_count'),
            (request_ends-request_starts, 'logical_missing_start_count'),
            (attempt_starts-attempt_ends, 'attempt_missing_end_count'),
            (attempt_ends-attempt_starts, 'attempt_missing_start_count')):
        for key in keys:
            gap_counts[key[:2]][name] += 1
    request_gaps = [dict(run_id=key[0], producer_id=key[1], **counts)
        for key, counts in sorted(gap_counts.items())]
    coverage = reader.coverage()
    ledger_summary = ledger.summary(runs=reader.runs, capture_coverage=coverage)
    calibration_summary = calibration.summary()
    scorecard = build_scorecard(ledger_summary, calibration_summary,
        graded_samples=calibration.graded_samples(), evaluation_start=evaluation_start)
    cohort_rows = []
    for (stage, asset, side, time_band, probability_band), counts in sorted(buckets.items()):
        count = counts['decisions']
        cohort_rows.append(dict(stage=stage, asset=asset, outcome=side, time_remaining_band=time_band,
            selected_probability_band=probability_band, counts=dict(counts),
            skipped_decision_fraction=counts['skipped']/count,
            acknowledged_fill_decision_fraction=counts['filled']/count,
            fractions_are_per_decision_not_independent_trade_win_rates=True))
    asset_scopes = dict(
        asset_breakdown='Completed attempt records with explicit asset recording context; contextless records are not assigned an asset',
        asset_context_missing_count='Completed attempt records without a valid explicit asset recording context')
    endpoint_rows = []
    for name, value in sorted(endpoints.items()):
        row = dict(endpoint=name, **_request_attempt_summary(value['attempts']),
            asset_context_missing_count=value['asset_context_missing_count'],
            asset_breakdown=[dict(asset=asset, **_request_attempt_summary(group))
                for asset, group in sorted(value['assets'].items())])
        row['count_scopes'].update(asset_scopes)
        endpoint_rows.append(row)
    attempt_summary = _request_attempt_summary(attempt_statistics)
    request_count_scopes = dict(attempt_summary['count_scopes'],
        attempt_started_count='Distinct recorded attempt-start identities, keyed by run, producer, request and attempt',
        attempt_completed_count='Distinct recorded attempt-end identities, keyed by run, producer, request and attempt',
        logical_started_count='Distinct recorded logical request-start identities, keyed by run, producer and request',
        logical_completed_count='Distinct recorded logical request-end identities, keyed by run, producer and request',
        **asset_scopes)
    request_count_scopes.pop('attempt_count')
    return dict(kind='kalshi_research_analysis', version=1, execution_eligible=False, read_only=True,
        input_source='local_research_capture', run_filter=run_id, asset_filter=list(assets),
        coverage=coverage, runs=reader.runs, event_kinds=dict(kinds),
        recording_health=build_recording_health(coverage, reader.runs,
            request_gaps=request_gaps, last_event_times=last_event_times),
        ledger=ledger_summary, scorecard=scorecard,
        requests=dict(logical_started_count=len(request_starts), logical_completed_count=len(request_ends),
            logical_missing_end_count=len(request_starts-request_ends),
            logical_missing_start_count=len(request_ends-request_starts),
            attempt_started_count=len(attempt_starts), attempt_completed_count=len(attempt_ends),
            confirmed_transport_attempt_count=attempt_summary['confirmed_transport_attempt_count'],
            local_nontransport_attempt_count=attempt_summary['local_nontransport_attempt_count'],
            unknown_transport_attempt_count=attempt_summary['unknown_transport_attempt_count'],
            completed_attempt_record_count=attempt_summary['completed_attempt_record_count'],
            confirmed_transport_http_statuses=attempt_summary['confirmed_transport_http_statuses'],
            local_nontransport_http_statuses=attempt_summary['local_nontransport_http_statuses'],
            unknown_transport_http_statuses=attempt_summary['unknown_transport_http_statuses'],
            confirmed_transport_rate_limited_count=attempt_summary['confirmed_transport_rate_limited_count'],
            count_scopes=request_count_scopes, asset_context_missing_count=asset_context_missing_count,
            asset_breakdown=[dict(asset=asset, **_request_attempt_summary(group))
                for asset, group in sorted(asset_attempt_statistics.items())],
            attempt_missing_end_count=len(attempt_starts-attempt_ends), logical_outcomes=dict(logical_outcomes),
            attempt_missing_start_count=len(attempt_ends-attempt_starts),
            attempt_outcomes=dict(attempt_outcomes), http_statuses=attempt_summary['http_statuses'],
            read_coordination_wait=latency_summary(coordination_times),
            discovery_cache=[dict(asset=asset, **counts,
                scope='Contract discovery metadata only; cache events are not HTTP requests')
                for asset, counts in sorted(discovery_counts.items())],
            logical_latency=latency_summary(logical_times), attempt_latency=attempt_summary['latency'], endpoints=endpoint_rows),
        decisions=dict(count=decisions, stages=dict(stages), no_action_poll_count=no_action_polls,
            execution_result_count=decisions-no_action_polls, ready_observation_count=ready,
            unique_observed_order_intents=len(unique_orders), distinct_forecast_markets=len(forecasts),
            latency=latency_summary(decision_times), cohorts=cohort_rows,
            skipped_reasons=[dict(stage=key[0], asset=key[1], reason=key[2], count=value) for key,value in reasons.most_common()]),
        financials=dict(basis='Latest covered authoritative activity audit per run; cumulative journal scope',
            audits=list(audits.values()), combined_realized_pnl=None,
            reason='Cumulative audit scopes can overlap across runs and must not be summed; missing/incomplete accounting remains null'),
        calibration=calibration_summary,
        limitations=['Acknowledged fills, canceled attempts and reservations are diagnostic evidence, not actual paid costs or realized returns.',
            'No statistical significance, profitability claim or counterfactual fill is inferred from descriptive decision cohorts.',
            'Asset filtering omits events without that asset context; inspect an unfiltered run for complete request coverage.',
            'Capture hashes establish local integrity, not independent exchange authentication.',
            'Transport durations include local throttle/signing/guard/backoff work and are not pure network round-trip times.',
            'HTTP completion does not establish successful order acknowledgement; response-validation events remain separate.',
            'Live or crashed producer shards and queue/disk losses prevent a complete-capture claim.'])
