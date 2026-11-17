"""Contract onboarding independent of trading algorithms and live authorization.

The registry is a discovery allowlist, not proof of an open market or feed access.
Every public acquisition is read-only. Current contract terms and exact source
identities are reported separately from the existing runner's capabilities.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from .btc15m import fee_reserve_lower_bound
from .engine import _levels, _valid_tick


class AssetUniverseError(ValueError):
    """Fixed, non-sensitive onboarding validation failure."""


@dataclass(frozen=True)
class UniverseAssetSpec:
    symbol: str
    name: str
    group: str
    series: str
    feed_family: str
    feed_candidates: tuple[str, ...]
    settlement_statistic: str
    runner_supported: bool
    evidence_urls: tuple[str, ...]


_CF_DOC = 'https://docs.kalshi.com/websockets/cfbenchmarks-value'
_PYTH_DOC = 'https://docs.kalshi.com/websockets/pyth-value'
_COMMODITIES = 'https://kalshi.com/combos/15-min/15-min/commodities'


def _crypto(symbol, name):
    index = 'BRTI' if symbol == 'BTC' else symbol + 'USD_RTI'
    return UniverseAssetSpec(symbol, name, 'crypto', 'KX' + symbol + '15M',
        'cfbenchmarks', (index,), 'final_minute_average',
        symbol in ('BTC', 'ETH', 'SOL', 'XRP'), (_CF_DOC,))


def _commodity(symbol, name, group, *feeds):
    return UniverseAssetSpec(symbol, name, group, 'KX' + symbol + '15M',
        'pyth', tuple(feeds), 'terminal_candle_close', symbol == 'GOLD',
        (_PYTH_DOC, _COMMODITIES))


UNIVERSE = MappingProxyType({spec.symbol: spec for spec in (
    _crypto('BTC', 'Bitcoin'), _crypto('ETH', 'Ethereum'),
    _crypto('SOL', 'Solana'), _crypto('XRP', 'XRP'), _crypto('DOGE', 'Dogecoin'),
    _crypto('BNB', 'BNB'), _crypto('HYPE', 'Hyperliquid'),
    _crypto('NEAR', 'NEAR'), _crypto('ZEC', 'Zcash'),
    _commodity('GOLD', 'Gold', 'metals', 'Metal.Index.1OZGOLD/USD'),
    _commodity('SILVER', 'Silver', 'metals', 'Metal.Index.SILVER/USD', 'Metal.XAG/USD'),
    _commodity('WTI', 'WTI crude oil', 'energy',
        'Commodities.Index.PYTHOIL/USD', 'Commodities.Index.WTI1M/USD'),
    _commodity('BRENT', 'Brent crude oil', 'energy', 'Commodities.Index.BRENT/USD'),
    _commodity('COPPER', 'Copper', 'metals',
        'Commodities.Index.CU/USD', 'Commodities.Index.COPPER/USD'),
    _commodity('NATGAS', 'Natural gas', 'energy', 'Commodities.Index.NATGAS/USD'),
    _commodity('PLATINUM', 'Platinum', 'metals', 'Metal.XPT/USD'),
    _commodity('PALLADIUM', 'Palladium', 'metals', 'Metal.XPD/USD'),
)})

_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_MAX_TIME = 253402300799
_MAX_DOCUMENT_BYTES = 1_000_000
_MAX_EVIDENCE_BYTES = 8_000_000

# Current ETH/SOL/XRP contract rules use these compact CF names, whereas the
# authenticated data API requires the canonical request IDs. Keep the aliases
# explicit: removing underscores from arbitrary identifiers would silently
# approve unreviewed index names or conceal a conflicting settlement source.
_CF_RULE_ALIASES = MappingProxyType({
    'ETHUSDRTI': 'ETHUSD_RTI',
    'SOLUSDRTI': 'SOLUSD_RTI',
    'XRPUSDRTI': 'XRPUSD_RTI',
})
_CF_RULE_INDEX = re.compile(
    r'(?<![A-Za-z0-9_])(?:BRTI|[A-Z][A-Z0-9_]{0,63}USD_?RTI)(?![A-Za-z0-9_])')


def _canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                          allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise AssetUniverseError('Public source evidence must be bounded finite JSON') from None


def _evidence_fingerprint(documents):
    return hashlib.sha256(_canonical_json({name: row['sha256']
        for name, row in documents.items()})).hexdigest()


def _capture_document(evidence, name, payload, received_at, budget):
    if not isinstance(payload, dict):
        raise AssetUniverseError('Public source evidence requires JSON objects')
    encoded = _canonical_json(payload)
    if len(encoded) > _MAX_DOCUMENT_BYTES or len(encoded) > budget[0]:
        raise AssetUniverseError('Public source evidence exceeds its document or acquisition bound')
    copied = json.loads(encoded)
    budget[0] -= len(encoded)
    evidence['documents'][name] = dict(payload=copied,
        sha256=hashlib.sha256(encoded).hexdigest(), received_at=received_at)
    evidence['fingerprint'] = _evidence_fingerprint(evidence['documents'])
    return copied


def universe_asset_spec(asset):
    if not isinstance(asset, str) or asset not in UNIVERSE:
        raise AssetUniverseError('Choose an exact uppercase asset from the onboarding catalog')
    return UNIVERSE[asset]


def universe_asset_from_ticker(ticker):
    if not isinstance(ticker, str) or _IDENTIFIER.fullmatch(ticker) is None:
        raise AssetUniverseError('Unsupported onboarding contract identity')
    for spec in UNIVERSE.values():
        if ticker.startswith(spec.series + '-') and len(ticker) > len(spec.series) + 1:
            return spec
    raise AssetUniverseError('Unsupported onboarding contract identity')


def _entry(spec):
    row = asdict(spec)
    row['feed_candidates'] = list(spec.feed_candidates)
    row['evidence_urls'] = list(spec.evidence_urls)
    row.update(series_identity_status='candidate_requires_current_api_verification',
        runner_integration_required=not spec.runner_supported,
        strategy_capabilities={'recurring_monte_carlo': spec.runner_supported,
            'btc_regime': spec.symbol == 'BTC', 'existing_runner_max_assets': 4},
        current_availability_verified=False, feed_access_verified=False,
        settlement_review_required=True, execution_eligible=False)
    return row


def catalog():
    return dict(kind='kalshi_asset_universe', version=1, algorithm_impact='isolated_onboarding_only',
        not_live_authorization=True, execution_eligible=False,
        assets=[_entry(spec) for spec in UNIVERSE.values()],
        reason='Candidate series and documented feed identities require current public metadata, exact rules and account feed checks')


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= _MAX_TIME:
        raise AssetUniverseError('A valid finite onboarding clock is required')
    return float(value)


def _stamp(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 48:
            raise ValueError()
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError()
        return _clock(result.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        raise AssetUniverseError('Current contract timestamps must be valid UTC') from None


def _number(value, maximum=Decimal('1000000000')):
    try:
        if (isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))
                or isinstance(value, str) and len(value) > 80):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > maximum:
            raise ValueError()
        return number
    except (ValueError, TypeError, InvalidOperation):
        raise AssetUniverseError('Current contract numeric metadata is invalid') from None


def _identity(market, spec, now):
    if not isinstance(market, dict):
        raise AssetUniverseError('Current contract metadata is incomplete')
    ticker, event = market.get('ticker'), market.get('event_ticker')
    if (not isinstance(ticker, str) or _IDENTIFIER.fullmatch(ticker) is None
            or not ticker.startswith(spec.series + '-')
            or not isinstance(event, str) or _IDENTIFIER.fullmatch(event) is None
            or not event.startswith(spec.series + '-') or not ticker.startswith(event + '-')
            or market.get('series_ticker', spec.series) != spec.series
            or market.get('status') != 'active' or market.get('market_type') != 'binary'
            or market.get('settlement_bounds_type') != 'default'
            or market.get('strike_type') not in ('greater', 'greater_or_equal')
            or market.get('cap_strike') is not None
            or _number(market.get('notional_value_dollars')) != 1
            or _number(market.get('floor_strike')) <= 0):
        raise AssetUniverseError('A standard current one-dollar binary contract is required')
    opened, closed = _stamp(market.get('open_time')), _stamp(market.get('close_time'))
    if abs(closed - opened - 900) > .000001 or not opened <= now < closed:
        raise AssetUniverseError('A current exact fifteen-minute contract window is required')
    return dict(ticker=ticker, event_ticker=event, open_time=market['open_time'],
        close_time=market['close_time'], floor_strike=format(_number(market['floor_strike']), 'f'),
        strike_type=market['strike_type'], notional_value_dollars='1',
        settlement_bounds_type='default')


def _rule_text(*metadata):
    parts = []
    for record in metadata:
        for key in ('rules_primary', 'rules_secondary', 'rules'):
            value = record.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 200000:
                raise AssetUniverseError('Current contract rules exceed their supported bound')
            parts.append(value)
    return '\n'.join(parts)


def _source_binding(spec, series, market, event):
    result = dict(status='needs_review', feed_family=spec.feed_family, feed=None,
        settlement_statistic=spec.settlement_statistic, settlement_review_required=True,
        reason='Exact contract source, statistic, rounding and fallback rules require review')
    sources = []
    for metadata in (series, market, event):
        if 'settlement_sources' in metadata:
            value = metadata['settlement_sources']
            if not isinstance(value, list) or not 1 <= len(value) <= 32 or any(not isinstance(row, dict) for row in value):
                return result
            sources.extend(value)
    if not sources:
        return result
    rules = _rule_text(series, market, event)
    parsed = []
    for source in sources:
        url, name = source.get('url'), source.get('name')
        if not isinstance(url, str) or len(url) > 2048 or not isinstance(name, str) or not 1 <= len(name) <= 256:
            return result
        try:
            value = urlsplit(url)
            if (value.scheme != 'https' or not value.hostname or value.username is not None
                    or value.password is not None or value.port not in (None, 443)
                    or value.query or value.fragment):
                return result
            parsed.append((value, name))
        except ValueError:
            return result
    if spec.feed_family == 'pyth':
        feeds = set()
        for value, _ in parsed:
            path = unquote(value.path).rstrip('/')
            if value.hostname != 'app.pyth.com' or not path.startswith('/explore/'):
                return result
            feeds.add(path[len('/explore/'):])
        # A date or familiar commodity name cannot resolve a feed transition.
        mentioned = set(re.findall(r'(?<![A-Za-z0-9_.])(?:Metal|Commodities)\.[A-Za-z0-9_.]{1,120}/(?:USD|USc)(?![A-Za-z0-9_])', rules))
        if len(feeds) != 1 or not feeds <= set(spec.feed_candidates) or mentioned - feeds:
            return result
        result.update(status='exact_matched', feed=next(iter(feeds)),
            reason='Exact Pyth source URL matches one documented candidate; settlement statistic and fallback rules still require review')
    else:
        index = spec.feed_candidates[0]
        mentioned = {_CF_RULE_ALIASES.get(name, name) for name in _CF_RULE_INDEX.findall(rules)}
        if (all((value.hostname == 'cfbenchmarks.com' or value.hostname.endswith('.cfbenchmarks.com'))
                and re.search(r'CF\s*Benchmarks', name, re.I) for value, name in parsed)
                and mentioned == {index}
                and re.search(r'\b(average|mean)\b', rules, re.I)
                and re.search(r'\b(60|sixty)\b', rules, re.I)):
            result.update(status='exact_matched', feed=index,
                reason='CF source and exact contract index/averaging text match; rounding and fallback review remain required')
    return result


def _discover_one(client, spec, now, max_pages, clock, evidence_budget):
    row = _entry(spec)
    evidence = dict(kind='kalshi_asset_evidence', version=1, documents={},
                    fingerprint=_evidence_fingerprint({}))
    row.update(availability='unavailable', discovery_complete=False, market=None,
        discovery_started_at=datetime.fromtimestamp(now, timezone.utc).isoformat(), received_at=None,
        evidence=evidence,
        source_binding=dict(status='needs_review', feed_family=spec.feed_family, feed=None,
            settlement_statistic=spec.settlement_statistic, settlement_review_required=True),
        orderbook_status='unavailable', orderbook_requested_at=None, fee_reserve_lower_bound=None)
    last_receipt = now

    def capture(name, payload):
        nonlocal last_receipt
        receipt = _clock(clock())
        if receipt < last_receipt:
            raise AssetUniverseError('The public acquisition clock moved backwards')
        last_receipt = receipt
        return _capture_document(evidence, name, payload, receipt, evidence_budget)

    try:
        series = capture('series', client.get_series(spec.series))
        if (not isinstance(series, dict) or series.get('ticker') != spec.series
                or series.get('frequency') != 'fifteen_min'):
            raise AssetUniverseError('Current series identity or fifteen-minute frequency differs')
        candidates, tickers, cursors, cursor = [], set(), set(), None
        for _ in range(max_pages):
            page = client.list_markets(series_ticker=spec.series, limit=100, cursor=cursor)
            if (not isinstance(page, dict) or not isinstance(page.get('markets'), list)
                    or len(page['markets']) > 100 or any(not isinstance(item, dict) for item in page['markets'])):
                raise AssetUniverseError('Current market discovery page is incomplete')
            for market in page['markets']:
                ticker = market.get('ticker')
                if isinstance(ticker, str):
                    if ticker in tickers:
                        raise AssetUniverseError('Current market discovery repeated a contract')
                    tickers.add(ticker)
                try:
                    identity = _identity(market, spec, now)
                except AssetUniverseError:
                    continue
                candidates.append((identity, market))
            cursor = page.get('cursor', '')
            if not isinstance(cursor, str) or len(cursor) > 256 or cursor and _IDENTIFIER.fullmatch(cursor) is None:
                raise AssetUniverseError('Current market discovery cursor is invalid')
            if not cursor:
                break
            if not page['markets'] or cursor in cursors:
                raise AssetUniverseError('Current market discovery did not advance')
            cursors.add(cursor)
        else:
            raise AssetUniverseError('Bounded market discovery did not finish')
        row['discovery_complete'] = True
        if not candidates:
            row.update(availability='no_market', reason='No current standard fifteen-minute contract was found in the completed public scan')
            return row
        candidates.sort(key=lambda candidate: (_stamp(candidate[0]['close_time']), candidate[0]['ticker']))
        identity, _ = candidates[0]
        market = capture('market', client.get_market(identity['ticker']))
        if _identity(market, spec, now) != identity:
            raise AssetUniverseError('Current contract terms changed during discovery')
        event = capture('event', client.get_event(identity['event_ticker']))
        if (not isinstance(event, dict) or event.get('event_ticker') != identity['event_ticker']
                or event.get('series_ticker') != spec.series):
            raise AssetUniverseError('Current contract event and series do not match')
        fee = fee_reserve_lower_bound(series, event)
        requested = _clock(clock())
        if requested < last_receipt:
            raise AssetUniverseError('The public acquisition clock moved backwards')
        row['orderbook_requested_at'] = last_receipt = requested
        raw = capture('orderbook', client.get_orderbook(identity['ticker']))
        if not isinstance(raw, dict) or not isinstance(raw.get('orderbook_fp'), dict):
            raise AssetUniverseError('The current fixed-point order book is unavailable')
        yes, no = _levels(raw['orderbook_fp'].get('yes_dollars')), _levels(raw['orderbook_fp'].get('no_dollars'))
        if (not yes or not no or yes[-1][0] + no[-1][0] >= 1
                or not _valid_tick(1 - no[-1][0], market.get('price_ranges'))
                or not _valid_tick(yes[-1][0], market.get('price_ranges'))):
            raise AssetUniverseError('An unlocked current order book on the contract price grid is required')
        row.update(availability='current', current_availability_verified=True,
            series_identity_status='verified_current_api_identity', market=identity,
            source_binding=_source_binding(spec, series, market, event),
            orderbook_status='available', fee_reserve_lower_bound=format(fee, 'f'),
            quote={'yes_ask': format(1 - no[-1][0], 'f'), 'no_ask': format(1 - yes[-1][0], 'f'),
                'yes_available': format(no[-1][1], 'f'), 'no_available': format(yes[-1][1], 'f')},
            reason='Current public contract and book found; this report does not authorize live trading')
    except Exception as error:
        row.update(availability='unavailable', current_availability_verified=False,
            reason=str(error) if isinstance(error, AssetUniverseError) else 'Public contract, fee or order-book acquisition is unavailable')
    finally:
        try:
            received = _clock(clock())
            if received < last_receipt:
                raise AssetUniverseError('The public acquisition clock moved backwards')
            row['received_at'] = datetime.fromtimestamp(received, timezone.utc).isoformat()
            if row['availability'] == 'current' and not (
                    _stamp(row['market']['open_time']) <= received < _stamp(row['market']['close_time'])):
                row.update(availability='unavailable', current_availability_verified=False,
                    orderbook_status='unavailable', quote=None,
                    reason='The contract expired during public acquisition; run discovery again')
        except (AssetUniverseError, ValueError, TypeError, OverflowError, OSError):
            row.update(availability='unavailable', current_availability_verified=False,
                orderbook_status='unavailable', quote=None,
                reason='The public acquisition receipt clock is unavailable or inconsistent')
    return row


def discover_universe(client, assets=None, *, now=None, max_pages=2):
    """Inspect bounded public GETs without credentials, account reads or orders."""
    if getattr(client, 'environment', None) != 'production' or getattr(client, '_allow_writes', None) is not False:
        raise AssetUniverseError('Universe discovery requires a production client with writes disabled')
    frozen = now is not None
    now = _clock(time.time() if now is None else now)
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise AssetUniverseError('Choose one through ten public discovery pages per asset')
    selected = tuple(UNIVERSE) if assets is None else assets
    if (not isinstance(selected, (tuple, list)) or not 1 <= len(selected) <= len(UNIVERSE)
            or any(not isinstance(asset, str) for asset in selected) or len(set(selected)) != len(selected)):
        raise AssetUniverseError('Choose distinct supported onboarding assets')
    specs = [universe_asset_spec(asset) for asset in selected]
    clock = (lambda: now) if frozen else time.time
    evidence_budget = [_MAX_EVIDENCE_BYTES]
    rows = [_discover_one(client, spec, _clock(clock()), max_pages, clock, evidence_budget) for spec in specs]
    return dict(kind='kalshi_universe_discovery', version=1, not_live_authorization=True,
        execution_eligible=False, algorithm_impact='isolated_onboarding_only',
        observed_at=datetime.fromtimestamp(now, timezone.utc).isoformat(), assets=rows,
        current_count=sum(row['availability'] == 'current' for row in rows),
        no_market_count=sum(row['availability'] == 'no_market' for row in rows),
        unavailable_count=sum(row['availability'] == 'unavailable' for row in rows),
        reason='Bounded public discovery only; account feed access, calibration and strategy integration are separate checks')
