"""Bounded, read-only status projections from explicitly selected producer JSON.

This module does not import executors, inspect accounts/journals or discover
processes. A recent producer report is not independently verified feed health,
an active-worker attestation or permission to execute its last decision.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import time


MAX_REPORTS = 8
MAX_BYTES = 12_000_000
MAX_AGE_SECONDS = 60
MAX_ROWS = 5000
ASSETS = ('BTC', 'ETH', 'SOL', 'XRP', 'GOLD')
_ROOT = Path(__file__).resolve().parents[2]
_KINDS = {
    'kalshi_minute_five_live_report': ('live', 'generated_at'),
    'kalshi_minute_five_live_status': ('live', 'generated_at'),
    'kalshi_value_state': (None, 'updated_at'),
    'kalshi_portfolio_state': ('observation', 'observed_at'),
    'kalshi_portfolio_book_observations': ('observation', 'observed_at'),
    'kalshi_asset_observations': ('observation', 'observed_at'),
    'kalshi_shadow_admission_report': ('shadow', 'observed_at'),
}
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_PRIVATE_TEXT = re.compile(
    r'(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|tmp|var|mnt)/|'
    r'(?:api[_ -]?key|authorization|bearer|private[_ -]?key|password|token)\s*[:=])', re.I)


class StrategyStatusError(ValueError):
    """A configured path or supplied producer report is invalid."""


def _label(value, *, maximum=600, nullable=False):
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or len(value) > maximum
            or not value.isprintable() or not value.strip()):
        raise StrategyStatusError('Invalid producer status label')
    if _PRIVATE_TEXT.search(value):
        return 'Producer detail withheld because it contains private fields or paths'
    return value


def _stamp(value):
    if isinstance(value, str):
        if len(value) > 48:
            raise StrategyStatusError('Invalid producer observation time')
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                raise ValueError()
            value = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            raise StrategyStatusError('Explicit UTC producer observation time is required') from None
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 253402300799):
        raise StrategyStatusError('Invalid producer observation time')
    return float(value)


def _rows(value, *, maximum=MAX_ROWS):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(row, dict) for row in value):
        raise StrategyStatusError('Invalid bounded producer rows')
    return value


def _asset(value):
    if not isinstance(value, str) or value not in ASSETS:
        raise StrategyStatusError('Unsupported producer asset identity')
    return value


def _asset_list(raw):
    if 'assets' not in raw:
        return []
    rows = raw['assets']
    if not isinstance(rows, list) or not 0 <= len(rows) <= len(ASSETS):
        raise StrategyStatusError('Invalid producer asset selection')
    result = []
    for row in rows:
        if isinstance(row, str):
            symbol = _asset(row)
        elif isinstance(row, dict):
            symbol = _asset(row.get('symbol', row.get('asset')))
        else:
            raise StrategyStatusError('Invalid producer asset selection')
        if symbol in result:
            raise StrategyStatusError('Repeated producer asset identity')
        result.append(symbol)
    return [asset for asset in ASSETS if asset in result]


def _decision(raw, asset=None):
    if not isinstance(raw, dict):
        raise StrategyStatusError('Invalid producer decision')
    if raw.get('asset') is not None:
        declared = _asset(raw['asset'])
        if asset is not None and declared != asset:
            raise StrategyStatusError('Decision asset differs from its producer selection')
        asset = declared
    result = dict(asset=asset,
        status=_label(raw.get('status'), maximum=64, nullable=True),
        action=_label(raw.get('action'), maximum=48, nullable=True),
        reason=_label(raw.get('reason'), nullable=True))
    ticker = raw.get('ticker', raw.get('market_ticker'))
    if ticker is not None:
        if not isinstance(ticker, str) or not _IDENTIFIER.fullmatch(ticker):
            raise StrategyStatusError('Invalid producer contract identity')
        # Known execution identities must agree with the declared asset. Other
        # onboarding product prefixes remain reported-only and grant no model
        # or execution authority through this projection.
        from .execution_assets import execution_asset_from_ticker
        try:
            ticker_asset = execution_asset_from_ticker(ticker).symbol
        except ValueError:
            ticker_asset = None
        if asset is not None and ticker_asset is not None and ticker_asset != asset:
            raise StrategyStatusError('Decision contract differs from its declared asset')
        result['ticker'] = ticker
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StrategyStatusError('Duplicate JSON producer field')
        result[key] = value
    return result


def _constant(_):
    raise StrategyStatusError('Producer JSON numbers must be finite')


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise StrategyStatusError('Producer JSON numbers must be finite')
    return result


def _project(raw, now):
    if (not isinstance(raw, dict) or raw.get('kind') not in _KINDS
            or type(raw.get('version')) is not int or raw['version'] != 1):
        raise StrategyStatusError('Unsupported producer report kind or version')
    kind = raw['kind']
    mode, clock_field = _KINDS[kind]
    if mode == 'live' and raw.get('mode') != 'live':
        raise StrategyStatusError('Scheduled live report mode differs')
    if kind == 'kalshi_value_state':
        mode = raw.get('mode')
        if mode not in ('paper', 'live'):
            raise StrategyStatusError('Value report mode differs')
    elif mode == 'shadow' and raw.get('mode') != 'shadow':
        raise StrategyStatusError('Shadow report mode differs')
    elif mode == 'observation' and ('mode' in raw and raw['mode'] not in ('observation', 'paper')):
        raise StrategyStatusError('Observation report mode differs')
    if mode in ('observation', 'shadow') and raw.get('execution_eligible') is not False:
        raise StrategyStatusError('Observation report cannot grant order permission')
    environment = raw.get('environment')
    if environment is not None and environment not in ('production', 'demo'):
        raise StrategyStatusError('Invalid producer environment')
    if mode == 'live' and environment == 'demo':
        raise StrategyStatusError('Live producer environment differs')
    synthetic = raw.get('synthetic')
    if synthetic is not None and type(synthetic) is not bool:
        raise StrategyStatusError('Invalid producer synthetic flag')
    if synthetic is True and mode == 'live':
        raise StrategyStatusError('Live report cannot contain synthetic execution evidence')
    stamp = None if raw.get(clock_field) is None else _stamp(raw[clock_field])
    if stamp is not None and stamp > now + .000001:
        raise StrategyStatusError('Producer observation is future dated')
    assets = _asset_list(raw)
    status = _label(raw.get('status'), maximum=64, nullable=True)
    reason = _label(raw.get('reason'), nullable=True)
    decisions, issues, counts = [], [], {}

    if kind in ('kalshi_minute_five_live_report', 'kalshi_minute_five_live_status'):
        if not assets or not isinstance(raw.get('markets'), dict) or len(raw['markets']) > len(assets):
            raise StrategyStatusError('Scheduled report requires selected market evidence')
        if 'halted' in raw and type(raw['halted']) is not bool:
            raise StrategyStatusError('Invalid scheduled halt flag')
        if raw.get('halted') is True:
            status = 'halted'
        for asset, market in raw['markets'].items():
            if asset not in assets or not isinstance(market, dict):
                raise StrategyStatusError('Scheduled market differs from selected assets')
            observation = market.get('observation')
            if observation is not None and (not isinstance(observation, dict)
                    or observation.get('asset', asset) != asset):
                raise StrategyStatusError('Scheduled observation asset differs')
            if market.get('execution') is not None:
                decisions.append(_decision(market['execution'], asset))
        counts.update(asset_reports=len(raw['markets']), decisions=len(decisions))
    elif kind == 'kalshi_value_state':
        asset = _asset(raw.get('asset'))
        if asset == 'GOLD' or assets and assets != [asset]:
            raise StrategyStatusError('Value report requires one supported crypto asset')
        assets = [asset]
        for member in ('config', 'observation'):
            value = raw.get(member)
            if value is not None and (not isinstance(value, dict) or value.get('asset', asset) != asset):
                raise StrategyStatusError('Value report asset evidence differs')
        decision = _decision(raw.get('decision'), asset)
        decisions.append(decision)
        status = status or decision['status']
        portfolio = raw.get('portfolio')
        if portfolio is not None:
            if not isinstance(portfolio, dict) or portfolio.get('mode', mode) != mode:
                raise StrategyStatusError('Value portfolio mode differs')
            if 'positions' in portfolio:
                counts['open_positions'] = len(_rows(portfolio['positions']))
        if 'settlements' in raw:
            counts['settlements'] = len(_rows(raw['settlements']))
    elif kind in ('kalshi_asset_observations', 'kalshi_portfolio_book_observations'):
        rows = _rows(raw.get('assets'), maximum=len(ASSETS))
        if not rows:
            raise StrategyStatusError('Observation report requires asset rows')
        ready = warming = 0
        for row in rows:
            symbol = _asset(row.get('symbol', row.get('asset')))
            ready_flag = row.get('data_ready')
            if type(ready_flag) is not bool:
                raise StrategyStatusError('Observation readiness is not explicit')
            ready += ready_flag
            warming += row.get('status') == 'warming'
            if row.get('model_ready') is not False or row.get('execution_eligible') is not False:
                raise StrategyStatusError('Market-data observations cannot authorize a model or order')
            decisions.append(_decision(dict(status=row.get('status'), reason=row.get('reason')), symbol))
        counts.update(asset_reports=len(rows), data_ready_assets=ready, warming_assets=warming)
    elif kind == 'kalshi_portfolio_state':
        for field, output in (('outstanding_orders', 'outstanding_orders'),
                              ('reservations', 'reservations'), ('ledgers', 'declared_ledgers')):
            if field in raw:
                counts[output] = len(_rows(raw[field]))
        holdings = raw.get('holdings')
        if holdings is not None:
            if not isinstance(holdings, dict):
                raise StrategyStatusError('Invalid portfolio holdings evidence')
            if 'positions' in holdings:
                rows = _rows(holdings['positions'])
                counts['open_positions'] = len(rows)
                for row in rows:
                    if row.get('asset') is not None:
                        asset = _asset(row['asset'])
                        if asset not in assets:
                            assets.append(asset)
        if 'issues' in raw:
            if not isinstance(raw['issues'], list) or len(raw['issues']) > 200:
                raise StrategyStatusError('Invalid portfolio issue evidence')
            issues = [_label(item) for item in raw['issues']]
            counts['issues'] = len(issues)
    else:
        rows = _rows(raw.get('decisions'), maximum=200)
        admitted = 0
        for row in rows:
            if type(row.get('admitted')) is not bool:
                raise StrategyStatusError('Shadow admission result is not explicit')
            admitted += row['admitted']
            decisions.append(_decision(row))
        counts.update(decisions=len(rows), admitted=admitted, rejected=len(rows)-admitted)

    age = None if stamp is None else max(0.0, now-stamp)
    freshness = 'unavailable' if stamp is None else 'ready' if age <= MAX_AGE_SECONDS else 'stale'
    clock_reason = ('Producer has not supplied an observation time' if stamp is None else
        'Producer report timestamp is within 60 seconds; feed health is not independently verified'
        if freshness == 'ready' else 'Saved producer report is older than 60 seconds')
    return dict(kind=kind, producer_mode=mode, environment=environment, synthetic=synthetic,
        assets=[asset for asset in ASSETS if asset in assets], producer_status=status,
        observed_at=stamp, age_seconds=age, freshness=freshness, reason=clock_reason,
        declared_reason=reason, counts=counts, decisions=decisions[:12], issues=issues[:8],
        omitted_decisions=max(0, len(decisions)-12), omitted_issues=max(0, len(issues)-8),
        freshness_basis='producer_report_timestamp_not_verified_feed_health',
        scope='producer_report_only')


def _file_signature(path):
    stat=path.stat()
    return (stat.st_mtime_ns,stat.st_size,stat.st_ino)


def _read_producer_json(path):
    if any(item.is_symlink() for item in (path,*path.parents)) or not path.is_file():
        raise StrategyStatusError('Producer report is unavailable')
    with path.open('rb') as stream:
        content=stream.read(MAX_BYTES+1)
    if len(content)>MAX_BYTES:raise StrategyStatusError('Producer report exceeds the bounded size')
    return json.loads(content,object_pairs_hook=_unique,parse_constant=_constant,parse_float=_float)


def _history_binding(path,raw):
    if (not isinstance(raw,dict) or raw.get('kind')!='kalshi_minute_five_live_report'
            or raw.get('environment')!='production' or raw.get('mode')!='live'):
        return None
    for key in ('journal_revision_id','report_session_id'):
        if not isinstance(raw.get(key),str) or not re.fullmatch('[0-9a-f]{32}',raw[key]):return None
    _stamp(raw['generated_at'])
    return dict(report_name=path.name,revision_id=raw['journal_revision_id'],
        session_id=raw['report_session_id'],environment=raw['environment'],generated_at=raw['generated_at'])


def _compact_status(path,binding,now):
    if binding is None:return None
    sidecar=path.with_name(path.stem+'.status.json')
    try:
        raw=_read_producer_json(sidecar)
        if (not isinstance(raw,dict) or raw.get('kind')!='kalshi_minute_five_live_status'
                or raw.get('version')!=1 or raw.get('mode')!='live'
                or raw.get('journal_history')!=binding
                or raw.get('report_session_id')!=binding['session_id']
                or raw.get('journal_revision_id')!=binding['revision_id']
                or raw.get('environment')!=binding['environment']
                or not _stamp(binding['generated_at'])<=_stamp(raw.get('generated_at'))<=now+.000001):
            return None
        # This remains a producer-only status projection, never execution input.
        _project(raw,now)
        return raw
    except (ValueError,TypeError,KeyError,ArithmeticError,OSError,UnicodeError,RecursionError):
        return None


class StrategyStatusMonitor:
    """Read only explicit, private JSON paths; never discover reports or state."""

    def __init__(self, paths=(), *, clock=time.time):
        if not isinstance(paths, (list, tuple)) or len(paths) > MAX_REPORTS or not callable(clock):
            raise StrategyStatusError('Choose at most eight producer report paths')
        self.clock = clock
        self.paths = []
        self._history_bindings = {}
        seen = set()
        for value in paths:
            path = Path(value).expanduser().absolute()
            if any(item.is_symlink() for item in (path, *path.parents)):
                raise StrategyStatusError('Producer reports cannot use symbolic links')
            resolved = path.resolve()
            if (path.suffix.lower() != '.json' or resolved == _ROOT or _ROOT in resolved.parents
                    or any((parent/'.git').exists() for parent in (resolved.parent, *resolved.parents))):
                raise StrategyStatusError('Keep producer JSON reports outside source checkouts')
            if resolved in seen:
                raise StrategyStatusError('Choose distinct producer report paths')
            seen.add(resolved)
            self.paths.append(resolved)

    def read(self):
        now = _stamp(self.clock())
        reports = []
        for path in self.paths:
            name = path.name if len(path.name) <= 128 and path.name.isprintable() else 'Configured report'
            row = dict(id=hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16], name=name,
                kind=None, producer_mode=None, environment=None, synthetic=None,
                assets=[], producer_status=None, observed_at=None, age_seconds=None,
                freshness='unavailable', reason='Configured producer report is missing, invalid or unsupported',
                declared_reason=None, counts={}, decisions=[], issues=[], omitted_decisions=0,
                omitted_issues=0, freshness_basis='producer_report_timestamp_not_verified_feed_health',
                scope='producer_report_only')
            try:
                if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file():
                    raise StrategyStatusError('Producer report is unavailable')
                signature = _file_signature(path)
                saved = self._history_bindings.get(path)
                raw = (_compact_status(path, saved[1], now)
                       if saved is not None and saved[0] == signature else None)
                if raw is None:
                    raw = _read_producer_json(path)
                    binding = _history_binding(path, raw)
                    if binding is not None and signature == _file_signature(path):
                        self._history_bindings[path] = (signature, binding)
                    else:
                        self._history_bindings.pop(path, None)
                    raw = _compact_status(path, binding, now) or raw
                row.update(_project(raw, now))
            except (ValueError, TypeError, KeyError, ArithmeticError, OSError, UnicodeError, RecursionError):
                pass
            reports.append(row)
        return dict(kind='kalshi_strategy_status_report', version=1, observed_at=now,
            execution_eligible=False, reports=reports,
            limitations=[
                'This view projects explicitly configured producer JSON and never controls a strategy.',
                'Report age is distinct from original feed/model age, current worker activity and execution eligibility.',
                'Producer modes and statuses are reported evidence, not independent source or account verification.',
                'Counts cover supplied arrays only; cash, exposure and P&L are not aggregated across reports.'])
