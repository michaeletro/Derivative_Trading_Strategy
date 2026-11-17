#!/usr/bin/env python3
"""Check dashboard read endpoints twice without credentials or exchange writes.

Only fixed loopback GET routes are permitted. Output contains bounded source
status and observation metadata, never response bodies, holdings or balances.
This verifies read delivery, not economic performance or trading eligibility.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import time
import urllib.error
import urllib.request


_ROUTES = (
    ('portfolio', '/api/portfolio-status', 'kalshi_portfolio_status'),
    ('health', '/api/dashboard-health', 'kalshi_dashboard_health'),
    ('positions', '/api/positions?source=account', 'kalshi_position_report'),
    ('performance', '/api/performance?source=account&asset=ALL', 'kalshi_performance_report'),
    ('assets', '/api/asset-performance', 'kalshi_asset_performance'),
    ('model_evidence', '/api/model-evidence?asset=ETH', 'kalshi_model_evidence'),
    ('equity_history', '/api/equity-history?window_seconds=86400&limit=2', 'kalshi_equity_history'),
)
_REQUIRED = ('portfolio', 'health', 'positions', 'performance', 'assets')
_OPTIONAL = ('model_evidence', 'equity_history')
_MAX_BYTES = 8 * 1024 * 1024
_STATUSES = {'ready', 'partial', 'unavailable', 'stale', 'not_started', 'not_configured',
             'pending', 'loading', 'collecting', 'complete'}


class VerificationError(ValueError):
    """A fixed public verifier failure; never includes a response or URL."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise VerificationError('redirect_rejected')


def _unique(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise VerificationError('invalid_json')
        output[key] = value
    return output


def _bad_constant(_):
    raise VerificationError('invalid_json')


def _read_json(url, timeout):
    # Disable proxies explicitly: environment proxy settings must not redirect
    # private loopback reads to another host. Redirects are rejected entirely.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, method='GET', headers={'Accept': 'application/json'})
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise VerificationError('http_unavailable')
            if response.headers.get_content_type() != 'application/json':
                raise VerificationError('invalid_content_type')
            raw = response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise VerificationError('response_limit')
        result = json.loads(raw, object_pairs_hook=_unique, parse_constant=_bad_constant)
        if type(result) is not dict:
            raise VerificationError('invalid_report')
        return result
    except VerificationError:
        raise
    except urllib.error.HTTPError:
        raise VerificationError('http_unavailable') from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise VerificationError('connection_unavailable') from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise VerificationError('invalid_json') from None


def _stamp(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) and 0 <= value <= 253402300799 else None
    if type(value) is str and len(value) <= 48:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0:
                return _stamp(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def _count(value):
    return value if type(value) is int and 0 <= value <= 10 ** 12 else None


def _money_exists(value):
    # Used only to distinguish a real zero-position account snapshot from an
    # empty placeholder. The value itself never enters the result.
    if type(value) not in (str, int, float):
        return False
    try:
        return Decimal(str(value)).is_finite()
    except (ValueError, InvalidOperation):
        return False


def _evidence_projection(name, expected, report, now):
    """Project transport and source clocks only; omit all financial/model IDs."""
    if (type(report) is not dict or report.get('kind') != expected or report.get('version') != 1
            or report.get('read_only') is not True or report.get('execution_eligible') is not False
            or report.get('status') not in ('ready', 'partial', 'unavailable')):
        raise VerificationError('invalid_report')
    status, counts = report['status'], {}
    if name == 'model_evidence':
        if type(report.get('query')) is not dict or report['query'].get('asset') != 'ETH':
            raise VerificationError('invalid_report')
        sources, coverage = report.get('sources'), report.get('coverage')
        if type(sources) is not list or len(sources) > 8 or type(coverage) is not dict:
            raise VerificationError('invalid_report')
        for field in ('configured_sources', 'available_sources', 'model_rows', 'score_groups', 'execution_rows'):
            value = _count(coverage.get(field))
            if value is None:
                raise VerificationError('invalid_report')
            counts[field] = value
        if counts['configured_sources'] != len(sources) or counts['available_sources'] > len(sources):
            raise VerificationError('invalid_report')
        artifact_clocks = []
        available = failed = 0
        for source in sources:
            if (type(source) is not dict or source.get('status') not in ('available', 'partial', 'unavailable')
                    or type(source.get('refresh_failed')) is not bool
                    or source.get('age_basis') not in ('reported_at', 'file_modified_at', 'unavailable')
                    or source.get('stale') is not None and type(source.get('stale')) is not bool):
                raise VerificationError('invalid_report')
            basis = source['age_basis']
            at = None if basis == 'unavailable' else _stamp(source.get(basis))
            if basis != 'unavailable' and at is None:
                raise VerificationError('invalid_report')
            available += source['status'] != 'unavailable'
            failed += source['refresh_failed']
            artifact_clocks.append(dict(age_basis=basis, observed_at=_iso(at),
                age_seconds=None if at is None or at > now else round(now-at, 6),
                reported_stale=source.get('stale'), refresh_failed=source['refresh_failed']))
        if available != counts['available_sources']:
            raise VerificationError('invalid_report')
        counts['refresh_failed_sources'] = failed
        observed = _stamp(report.get('generated_at'))
        configured, useful = counts['configured_sources'] > 0, counts['available_sources'] > 0 and counts['model_rows'] > 0
        age = None if observed is None or observed > now else round(now-observed, 6)
        fresh = configured and useful and status in ('ready', 'partial') and age is not None and age <= 60 and not failed
        return dict(reachable=True, valid=True, configured=configured, status=status, observed_at=_iso(observed),
            age_seconds=age, fresh=bool(fresh), useful_observations=useful, in_progress=False,
            complete=None, counts=counts, artifact_clocks=artifact_clocks,
            clock_scope='Current evidence-view delivery; original artifact clocks are separate and do not qualify a model.')
    if (report.get('source') != 'kalshi_account' or report.get('account_scope') != 'primary_subaccount_0'
            or type(report.get('points')) is not list or len(report['points']) > 2
            or type(report.get('coverage')) is not dict or type(report.get('persistence')) is not dict
            or type(report.get('stale')) is not bool or type(report.get('restored')) is not bool):
        raise VerificationError('invalid_report')
    coverage, persistence, points = report['coverage'], report['persistence'], report['points']
    for field in ('stored_points', 'selected_points', 'returned_points', 'gap_count', 'retention_trimmed_points'):
        value = _count(coverage.get(field))
        if value is None:
            raise VerificationError('invalid_report')
        counts[field] = value
    if (counts['returned_points'] != len(points) or counts['selected_points'] < len(points)
            or counts['stored_points'] < counts['selected_points']):
        raise VerificationError('invalid_report')
    previous = None
    for point in points:
        at = _stamp(point.get('observed_at')) if type(point) is dict else None
        if at is None or previous is not None and at <= previous:
            raise VerificationError('invalid_report')
        previous = at
    for field in ('pending', 'dropped_count', 'write_error_count', 'rejected_count', 'saved_count'):
        value = _count(persistence.get(field))
        if value is None:
            raise VerificationError('invalid_report')
        counts[field] = value
    persistence_status = persistence.get('status')
    if persistence_status not in ('disabled', 'pending', 'ready', 'error') or type(persistence.get('scope_verified')) is not bool:
        raise VerificationError('invalid_report')
    observed = _stamp(report.get('fetched_at'))
    if previous is not None and observed != previous:
        raise VerificationError('invalid_report')
    configured, useful = persistence_status != 'disabled', bool(points)
    age = None if observed is None or observed > now else round(now-observed, 6)
    fresh = (configured and useful and age is not None and age <= 30 and status in ('ready', 'partial')
        and not report['stale'] and not report['restored'])
    return dict(reachable=True, valid=True, configured=configured, status=status,
        observed_at=_iso(observed), age_seconds=age, fresh=bool(fresh), useful_observations=useful,
        in_progress=counts['pending'] > 0, complete=None, counts=counts,
        persistence_status=persistence_status, scope_verified=persistence['scope_verified'],
        capture_operational=persistence_status == 'ready' and persistence['scope_verified'],
        clock_scope='Latest original account acquisition; cash, values and returns are omitted.')


def _projection(name, expected, report, now):
    if name in _OPTIONAL:
        return _evidence_projection(name, expected, report, now)
    if type(report) is not dict or report.get('kind') != expected or report.get('version') != 1:
        raise VerificationError('invalid_report')
    status = report.get('status', 'ready' if name in ('portfolio', 'health') else None)
    if status not in _STATUSES:
        raise VerificationError('invalid_report')
    configured = True
    observed = _stamp(report.get('fetched_at', report.get('as_of')))
    counts = {}
    progress = bool(report.get('refreshing') is True)
    useful = False
    complete = None
    stale_after = 60.
    if name == 'portfolio':
        if type(report.get('account_view_enabled')) is not bool:
            raise VerificationError('invalid_report')
        configured = report['account_view_enabled']
        # This endpoint reports capabilities and its request clock only.
        observed = None
    elif name == 'health':
        sources = report.get('sources')
        if type(sources) is not list or len(sources) > 100:
            raise VerificationError('invalid_report')
        scoped = [row for row in sources if type(row) is dict and row.get('scope') in
                  ('account', 'account_primary_0')]
        configured = any(row.get('status') != 'not_configured' for row in scoped)
        clocks = [_stamp(row.get('last_success_at')) for row in scoped]
        clocks = [stamp for stamp in clocks if stamp is not None]
        observed = min(clocks) if clocks else None
        useful = bool(clocks)
        progress = any(row.get('refreshing') is True for row in scoped)
        counts = {'configured_sources': sum(row.get('status') != 'not_configured' for row in scoped),
                  'observed_sources': len(clocks),
                  'stale_sources': sum(row.get('status') == 'stale' for row in scoped)}
        if counts['stale_sources']:
            status = 'stale'
        elif any(row.get('status') in ('unavailable', 'not_started', 'loading', 'pending') for row in scoped):
            status = 'partial'
    elif name == 'positions':
        if report.get('source') != 'kalshi_account' or type(report.get('positions')) is not list:
            raise VerificationError('invalid_report')
        summary = report.get('summary')
        if type(summary) is not dict:
            raise VerificationError('invalid_report')
        counts = {'positions': len(report['positions'])}
        useful = bool(report['positions']) or any(_money_exists(summary.get(key))
                                                  for key in ('cash_balance', 'portfolio_value'))
        configured = status != 'unavailable' or report.get('last_attempted_at') is not None
        stale_after = 30.
    elif name == 'performance':
        if report.get('source') != 'account' or report.get('asset') != 'ALL':
            raise VerificationError('invalid_report')
        summary, coverage = report.get('summary'), report.get('coverage')
        acquisition = report.get('acquisition') or {}
        if type(summary) is not dict or type(coverage) is not dict or type(acquisition) is not dict:
            raise VerificationError('invalid_report')
        for field in ('executions', 'event_count', 'unknown_pnl_events'):
            if field in summary:
                value = _count(summary[field])
                if value is None:
                    raise VerificationError('invalid_report')
                counts[field] = value
        for field in ('pages', 'read_records', 'cached_archive_records'):
            if field in acquisition:
                value = _count(acquisition[field])
                if value is None:
                    raise VerificationError('invalid_report')
                counts[field] = value
        useful = any(counts.get(key, 0) > 0 for key in ('executions', 'event_count', 'read_records')) or (
            coverage.get('complete') is True and coverage.get('history_complete') is True)
        complete = coverage.get('complete') is True
        progress = acquisition.get('in_progress') is True
        configured = status != 'unavailable' or acquisition.get('last_attempt_at') is not None
    elif name == 'assets':
        source, assets = report.get('source'), report.get('assets')
        if type(source) is not dict or type(assets) is not list or len(assets) > 20:
            raise VerificationError('invalid_report')
        observed = _stamp(source.get('observed_at', source.get('generated_at')))
        configured = source.get('configured') is True
        coverage = report.get('coverage') or {}
        if type(coverage) is not dict:
            raise VerificationError('invalid_report')
        counts = {'assets': len(assets)}
        for field in ('total_tickers', 'selected_tickers', 'completed_tickers'):
            if field in coverage:
                count = _count(coverage[field])
                if count is None:
                    raise VerificationError('invalid_report')
                counts[field] = count
        useful = observed is not None and (counts.get('selected_tickers', 0) > 0 or
            any(type(row) is dict and (_count(row.get('completed_tickers')) or 0) > 0
                for row in assets))
        complete = status == 'ready'
        if source.get('stale') is True:
            status = 'stale'
    age = None if observed is None or observed > now else round(now - observed, 6)
    stale_flag = report.get('stale') is True or report.get('restored') is True or report.get('refresh_failed') is True
    fresh = (configured and useful and age is not None and age <= stale_after and status in ('ready', 'partial', 'complete')
             and not stale_flag)
    return dict(reachable=True, valid=True, configured=configured, status=status,
                observed_at=_iso(observed), age_seconds=age, fresh=bool(fresh),
                useful_observations=useful, in_progress=progress, complete=complete, counts=counts)


def verify(*, port=8091, timeout=3., interval_seconds=5., fetcher=None,
           clock=time.time, monotonic=time.monotonic, sleeper=time.sleep):
    if (type(port) is not int or not 1024 <= port <= 65535
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10
            or type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
            or not 0 <= interval_seconds <= 30):
        raise VerificationError('invalid_settings')
    fetcher = _read_json if fetcher is None else fetcher
    started = monotonic()
    samples = []
    for index in range(2):
        if index:
            remaining = 55. - (monotonic() - started)
            if remaining > 0:
                sleeper(min(interval_seconds, remaining))
        endpoints = {}
        for name, route, kind in _ROUTES:
            remaining = 55. - (monotonic() - started)
            try:
                if remaining <= 0:
                    raise VerificationError('verification_deadline')
                report = fetcher(f'http://127.0.0.1:{port}{route}', min(float(timeout), remaining))
                endpoints[name] = _projection(name, kind, report, clock())
            except Exception as error:
                code = str(error) if type(error) is VerificationError else 'request_failed'
                # Only fixed error vocabulary may be displayed.
                if code not in {'invalid_report', 'http_unavailable', 'invalid_content_type', 'response_limit',
                                'redirect_rejected', 'connection_unavailable', 'invalid_json', 'verification_deadline'}:
                    code = 'request_failed'
                endpoints[name] = dict(reachable=code not in ('connection_unavailable', 'request_failed',
                    'verification_deadline'), valid=False, configured=None, status='unavailable',
                    observed_at=None, age_seconds=None, fresh=False, useful_observations=False,
                    in_progress=False, complete=None, counts={}, error=code)
        # The capability endpoint is authoritative for enabled account reads;
        # a warming request clock cannot substitute for this configuration.
        if endpoints['portfolio']['valid']:
            enabled = endpoints['portfolio']['configured']
            for name in ('health', 'positions', 'performance'):
                if endpoints[name]['valid']:
                    endpoints[name]['configured'] = enabled
                    if not enabled:
                        endpoints[name]['fresh'] = False
        samples.append(dict(checked_at=_iso(clock()), endpoints=endpoints))
    changes = {}
    for name, _, _ in _ROUTES:
        before, after = (sample['endpoints'][name] for sample in samples)
        count_changes = {key: after['counts'].get(key, 0) - before['counts'].get(key, 0)
                         for key in before['counts'].keys() | after['counts'].keys()
                         if after['counts'].get(key) != before['counts'].get(key)}
        changed = bool(before['valid'] and after['valid'] and
            (before['observed_at'] != after['observed_at'] or count_changes))
        changes[name] = dict(updating=changed, observation_advanced=bool(before['observed_at']
            and after['observed_at'] and after['observed_at'] > before['observed_at']),
            count_changes=count_changes)
    final = samples[-1]['endpoints']
    valid = all(final[name]['valid'] for name in _REQUIRED)
    configured = final['portfolio']['configured'] is True
    fresh = all(final[name]['fresh'] for name in ('health', 'positions', 'performance', 'assets'))
    successful = valid and configured and fresh
    optional_valid = all(final[name]['valid'] for name in _OPTIONAL)
    optional_verified = (all(final[name]['valid'] and final[name]['configured'] and final[name]['fresh'] for name in _OPTIONAL)
        and final['equity_history'].get('capture_operational') is True)
    optional_status = ('verified' if optional_verified else 'not_configured' if optional_valid and
        all(not final[name]['configured'] for name in _OPTIONAL) else 'partial' if optional_valid else 'unavailable')
    return dict(kind='kalshi_dashboard_runtime_verification', version=1, read_only=True,
        verified=successful, status='verified' if successful else 'partial' if valid else 'unavailable',
        account_configured=configured, fresh_observations=fresh, samples=samples, changes=changes,
        optional_evidence_verified=optional_verified, optional_evidence_status=optional_status,
        interpretation='Unchanged valid snapshots are allowed. Updating describes observed metadata changes, not a guarantee of a continuous stream.',
        scope='verified covers the original five core views; optional_evidence_verified separately covers ETH model-evidence delivery and account-history freshness. Neither establishes accounting completeness, model activation or economic performance.'), (0 if successful else 2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--timeout', type=float, default=3.)
    parser.add_argument('--interval-seconds', type=float, default=5.)
    args = parser.parse_args(argv)
    try:
        result, code = verify(port=args.port, timeout=args.timeout, interval_seconds=args.interval_seconds)
    except VerificationError:
        result, code = dict(kind='kalshi_dashboard_runtime_verification', version=1,
                            read_only=True, verified=False, status='invalid_settings'), 2
    print(json.dumps(result, allow_nan=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())