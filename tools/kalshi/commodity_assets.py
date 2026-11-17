"""Isolated commodity onboarding identities and public metadata inspection.

Listing a series here establishes neither a price-feed binding nor trading
eligibility. Full contract rules and the exact source must be reviewed before
any settlement model is calibrated. This registry is deliberately separate
from the crypto asset registry and has no execution or account dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from types import MappingProxyType
from urllib.parse import urlsplit


class CommodityAssetError(ValueError):
    """Fixed, non-sensitive commodity identity or metadata validation error."""


@dataclass(frozen=True)
class CommodityAssetSpec:
    symbol: str
    name: str
    series: str


COMMODITY_ASSETS = MappingProxyType({
    'WTI': CommodityAssetSpec('WTI', 'WTI crude oil', 'KXWTI15M'),
    'BRENT': CommodityAssetSpec('BRENT', 'Brent crude oil', 'KXBRENT15M'),
    'COPPER': CommodityAssetSpec('COPPER', 'Copper', 'KXCOPPER15M'),
    'NATGAS': CommodityAssetSpec('NATGAS', 'Natural gas', 'KXNATGAS15M'),
    'GOLD': CommodityAssetSpec('GOLD', 'Gold', 'KXGOLD15M'),
    'SILVER': CommodityAssetSpec('SILVER', 'Silver', 'KXSILVER15M'),
})
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_MAX_JSON_BYTES = 8_000_000
_MAX_TIME = 253402300799
_COPPER_NOTICE = {
    'kind': 'feed_transition_notice',
    'effective_event_close': '2026-11-15T23:30:00Z',
    'effective_local_time': '2026-11-15T18:30:00-05:00',
    'timezone': 'America/New_York',
    'previous_feed_label': 'CU/USD',
    'new_feed_label': 'COPPER/USD',
    'previous_feed_schedule': '24/7',
    'new_feed_schedule': '23/5',
    'source_url': 'https://kalshi.com/markets/kxcopper15m/copper-15minute',
    'per_contract_review_required': True,
    'detail': ('Kalshi announces that newly listed events beginning with the '
               'window closing November 15, 2026 at 6:30 p.m. EST use '
               'COPPER/USD. Previously listed events retain CU/USD. '
               'The closing time alone does not prove which feed an event uses.'),
}


def commodity_asset_spec(asset):
    """Resolve only an exact supported symbol; never infer a proxy or alias."""
    if not isinstance(asset, str) or asset not in COMMODITY_ASSETS:
        raise CommodityAssetError('Choose WTI, BRENT, COPPER, NATGAS, GOLD or SILVER')
    return COMMODITY_ASSETS[asset]


def _json_copy(value):
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False,
                             separators=(',', ':'), ensure_ascii=True)
        if len(encoded) > _MAX_JSON_BYTES:
            raise ValueError()
        return json.loads(encoded), encoded
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise CommodityAssetError('Commodity metadata must be bounded finite JSON') from None


def _receipt(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= _MAX_TIME):
        raise CommodityAssetError('A valid commodity metadata receipt time is required')
    return float(value)


def _stamp(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 48:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        stamp = parsed.timestamp()
        if not math.isfinite(stamp) or not 0 < stamp <= _MAX_TIME:
            raise ValueError()
        return stamp
    except (ValueError, TypeError, OverflowError, OSError):
        raise CommodityAssetError('Commodity contract timestamps must be valid UTC') from None


def _number(value, *, positive=False):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError()
        if isinstance(value, str) and len(value) > 80:
            raise ValueError()
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or number > 1_000_000_000
                or positive and number <= 0):
            raise ValueError()
        return number
    except (ValueError, TypeError, InvalidOperation):
        raise CommodityAssetError('Commodity contract price metadata is invalid') from None


def catalog():
    """Return offline onboarding capabilities without guessing any Pyth feed ID."""
    assets = []
    for spec in COMMODITY_ASSETS.values():
        entry = dict(asdict(spec), status='research_onboarding',
            model_status='unavailable', execution_eligible=False,
            settlement_review_required=True, feed_symbol=None, feed_id=None)
        entry['notices'] = [_json_copy(_COPPER_NOTICE)[0]] if spec.symbol == 'COPPER' else []
        assets.append(entry)
    return dict(kind='kalshi_commodity_catalog', version=1, execution_eligible=False,
        algorithm_impact='isolated_research_only', assets=assets)


def _final_result(market, closed, observed_at):
    """Expose a label only with completed official final binary evidence."""
    if (market.get('status') != 'finalized' or closed > observed_at
            or market.get('is_provisional', False) is not False
            or market.get('result') not in ('yes', 'no')):
        return None
    result = market['result']
    payout = market.get('settlement_value_dollars')
    if payout is not None and _number(payout) != (1 if result == 'yes' else 0):
        raise CommodityAssetError('The finalized commodity result and payout disagree')
    settlement_ts = market.get('settlement_ts')
    if settlement_ts is not None and not closed <= _stamp(settlement_ts) <= observed_at:
        raise CommodityAssetError('The finalized commodity settlement time is inconsistent')
    return result


def contract_identity(market, *, asset, observed_at):
    """Validate an individual binary 15-minute identity, without verifying rules.

    ``final_result`` is a historical label only. Even a valid finalized row
    supplies no proof that an independently downloaded price feed matches it.
    """
    spec, receipt = commodity_asset_spec(asset), _receipt(observed_at)
    if not isinstance(market, dict):
        raise CommodityAssetError('Commodity contract metadata must be an object')
    market, _ = _json_copy(market)
    ticker, event = market.get('ticker'), market.get('event_ticker')
    if (not isinstance(ticker, str) or _IDENTIFIER.fullmatch(ticker) is None
            or not ticker.startswith(spec.series + '-')
            or not isinstance(event, str) or _IDENTIFIER.fullmatch(event) is None
            or not event.startswith(spec.series + '-') or not ticker.startswith(event + '-')
            or market.get('series_ticker', spec.series) != spec.series):
        raise CommodityAssetError('Commodity market and event identities differ from the selected series')
    if (market.get('market_type') != 'binary'
            or market.get('strike_type') not in ('greater', 'greater_or_equal')
            or market.get('cap_strike') is not None
            or market.get('settlement_bounds_type', 'default') != 'default'):
        raise CommodityAssetError('Only standard binary commodity threshold contracts are supported')
    if _number(market.get('notional_value_dollars'), positive=True) != 1:
        raise CommodityAssetError('Commodity research requires a one-dollar binary payout')
    strike = _number(market.get('floor_strike'), positive=True)
    opened, closed = _stamp(market.get('open_time')), _stamp(market.get('close_time'))
    if abs(closed - opened - 900) > .000001:
        raise CommodityAssetError('Commodity research requires an exact fifteen-minute contract window')
    status = market.get('status')
    if not isinstance(status, str) or not status or len(status) > 64:
        raise CommodityAssetError('Commodity contract status is unavailable')
    return dict(asset=spec.symbol, series=spec.series, ticker=ticker, event_ticker=event,
        open_time=market['open_time'], close_time=market['close_time'],
        opened_at=opened, closed_at=closed, strike=format(strike.normalize(), 'f'),
        inclusive=market['strike_type'] == 'greater_or_equal', status=status,
        final_result=_final_result(market, closed, receipt),
        settlement_review_required=True, execution_eligible=False)


def _source_issues(sources):
    issues = []
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        return ['Settlement sources are missing or malformed']
    seen, names = set(), {}
    for source in sources:
        if not isinstance(source, dict):
            issues.append('A settlement source is not an object')
            continue
        name, url = source.get('name'), source.get('url')
        try:
            parsed = urlsplit(url) if isinstance(url, str) and len(url) <= 2048 else None
            if (not isinstance(name, str) or not 1 <= len(name) <= 256
                    or parsed is None or parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError()
        except ValueError:
            issues.append('A settlement source name or HTTPS URL is invalid')
            continue
        pair = (name, url)
        if pair in seen:
            issues.append('Duplicate settlement source metadata requires review')
        if name in names and names[name] != url:
            issues.append('Conflicting URLs for the same settlement source require review')
        names[name] = url
        seen.add(pair)
    return sorted(set(issues))


def inspect_metadata(series, markets, *, asset, observed_at):
    """Inspect immutable public snapshots; never auto-approve a feed or model.

    The full series and contract documents are retained in this result. Shape
    validation, an official outcome, or a familiar source name cannot substitute
    for reviewing exact symbols, rounding, candle boundaries and fallback rules.
    """
    spec, receipt = commodity_asset_spec(asset), _receipt(observed_at)
    if (not isinstance(series, dict) or series.get('ticker') != spec.series
            or series.get('frequency') != 'fifteen_min'):
        raise CommodityAssetError('Commodity series identity or fifteen-minute frequency differs')
    if (not isinstance(markets, list) or len(markets) > 1000
            or any(not isinstance(row, dict) for row in markets)):
        raise CommodityAssetError('Commodity inspection requires at most one thousand market objects')
    snapshot, encoded = _json_copy(dict(series=series, markets=markets))
    series, markets = snapshot['series'], snapshot['markets']
    sources = series.get('settlement_sources')
    issues = _source_issues(sources)
    contracts, per_contract, seen = [], [], set()
    for market in markets:
        ticker = market.get('ticker')
        label = ticker if isinstance(ticker, str) and _IDENTIFIER.fullmatch(ticker) else None
        local = []
        if label in seen:
            local.append('Duplicate market snapshots require reconciliation')
        if label is not None:
            seen.add(label)
        try:
            identity = contract_identity(market, asset=asset, observed_at=receipt)
            contracts.append(identity)
        except CommodityAssetError as error:
            local.append(str(error))
        if market.get('status') in ('disputed', 'amended'):
            local.append('Disputed or amended settlement evidence requires review')
        if market.get('is_provisional', False) is not False:
            local.append('Provisional or malformed settlement evidence requires review')
        if market.get('status') == 'finalized' and market.get('result') not in ('yes', 'no'):
            local.append('A finalized commodity contract lacks a binary result')
        if not isinstance(market.get('rules_primary'), str) or not market['rules_primary'].strip():
            local.append('Primary contract rules are missing')
        elif len(market['rules_primary']) > 100_000:
            local.append('Primary contract rules exceed the review bound')
        market_sources = market.get('settlement_sources')
        if market_sources is not None and market_sources != sources:
            local.append('Contract settlement sources differ from the current series snapshot')
        if local:
            per_contract.append(dict(ticker=label, issues=sorted(set(local))))
    if not markets:
        issues.append('No market contracts were included in this snapshot')
    if per_contract:
        issues.append('One or more contracts require metadata reconciliation')
    issues.append('Exact feed, candle convention, comparator, rounding and fallback rules require review')
    notices = [_json_copy(_COPPER_NOTICE)[0]] if spec.symbol == 'COPPER' else []
    return dict(kind='kalshi_commodity_metadata', version=1, asset=spec.symbol,
        series=spec.series, observed_at=receipt,
        observed_at_utc=datetime.fromtimestamp(receipt, timezone.utc).isoformat(),
        status='needs_settlement_review', execution_eligible=False,
        settlement_sources=sources, series_metadata=series, markets=markets,
        contracts=contracts, issues=sorted(set(issues)), per_contract_issues=per_contract,
        notices=notices, metadata_sha256=hashlib.sha256(encoded.encode('utf-8')).hexdigest())
