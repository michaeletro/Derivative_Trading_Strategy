"""Execution identities independent of a benchmark's forecasting method.

Adding an identity here permits bounded account/order routing only. The live
observation service must separately validate its exact feed and settlement
model. Crypto CF models deliberately retain their existing asset registry.
"""
from __future__ import annotations

import re
from types import MappingProxyType

from .crypto_assets import ASSETS, view_asset_spec


EXECUTION_ASSETS = MappingProxyType({**ASSETS, 'GOLD': view_asset_spec('GOLD')})
_TICKER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def execution_asset_spec(asset='BTC'):
    """Resolve an exact execution symbol without enabling a feed or model."""
    if not isinstance(asset, str) or asset not in EXECUTION_ASSETS:
        raise ValueError('Select BTC, ETH, SOL, XRP or GOLD for execution')
    return EXECUTION_ASSETS[asset]


def execution_asset_from_ticker(ticker):
    """Resolve a valid market prefix, never a proxy asset or bare series."""
    if not isinstance(ticker, str) or _TICKER.fullmatch(ticker) is None:
        raise ValueError('Unsupported execution market identity')
    for spec in EXECUTION_ASSETS.values():
        prefix = spec.series + '-'
        if ticker.startswith(prefix) and len(ticker) > len(prefix):
            return spec
    raise ValueError('Unsupported execution market identity')
