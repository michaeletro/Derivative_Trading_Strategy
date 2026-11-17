"""Explicit settlement identities for supported Kalshi crypto 15-minute series.

Kalshi's index-ID reference is authoritative for REST request spelling:
https://docs.kalshi.com/websockets/cfbenchmarks-value
https://docs.kalshi.com/cfbenchmarks/rest-passthrough
Series and current contract rules remain authoritative for market eligibility.
An index's presence here does not establish account entitlement or availability.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    series: str
    index_id: str
    source: str


ASSETS = {
    'BTC': AssetSpec('BTC', 'KXBTC15M', 'BRTI', 'CF_BRTI'),
    'ETH': AssetSpec('ETH', 'KXETH15M', 'ETHUSD_RTI', 'CF_ETHUSD_RTI'),
    'SOL': AssetSpec('SOL', 'KXSOL15M', 'SOLUSD_RTI', 'CF_SOLUSD_RTI'),
    'XRP': AssetSpec('XRP', 'KXXRP15M', 'XRPUSD_RTI', 'CF_XRPUSD_RTI'),
}
# A quote view is not a settlement-model capability. Gold uses a different
# feed and a candle-close event, so it must never enter the CF average models.
VIEW_ASSETS = {
    **ASSETS,
    'GOLD': AssetSpec('GOLD', 'KXGOLD15M', 'Metal.Index.1OZGOLD/USD', 'PYTH_1OZGOLD_USD'),
}
GOLD_MODEL_VERSION = 'pyth-gold-model-unavailable-v1'
GOLD_MODEL_REASON = ('Gold quotes use the Pyth Metal.Index.1OZGOLD/USD settlement feed. '
    'Its one-minute candle-close contract differs from the crypto final-minute average. '
    'The crypto CF probability, Greeks and ensemble panels do not model that settlement. '
    'The separate Pyth Gold runner requires its own verified feed and model.')
_TICKER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def asset_spec(asset='BTC') -> AssetSpec:
    """Resolve an exact uppercase asset symbol; never infer a proxy asset."""
    if not isinstance(asset, str) or asset not in ASSETS:
        raise ValueError('Unsupported crypto asset; choose BTC, ETH, SOL or XRP')
    return ASSETS[asset]


def view_asset_spec(asset='BTC') -> AssetSpec:
    """Resolve a supported public quote view without enabling a model."""
    if not isinstance(asset, str) or asset not in VIEW_ASSETS:
        raise ValueError('Unsupported market asset; choose BTC, ETH, SOL, XRP or GOLD')
    return VIEW_ASSETS[asset]


def view_asset_from_ticker(ticker) -> AssetSpec:
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError('Unsupported fifteen-minute market identity')
    for spec in VIEW_ASSETS.values():
        if ticker.startswith(spec.series + '-') and len(ticker) > len(spec.series) + 1:
            return spec
    raise ValueError('Unsupported fifteen-minute market identity')


def asset_from_ticker(ticker) -> AssetSpec:
    """Resolve only explicitly allowed 15-minute series with a valid suffix."""
    if not isinstance(ticker, str) or not _TICKER.fullmatch(ticker):
        raise ValueError('Unsupported crypto 15-minute market identity')
    for spec in ASSETS.values():
        prefix = spec.series + '-'
        if ticker.startswith(prefix) and len(ticker) > len(prefix):
            return spec
    raise ValueError('Unsupported crypto 15-minute market identity')
