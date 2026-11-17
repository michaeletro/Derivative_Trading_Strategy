"""Explicit per-asset research fits without cross-asset parameter fallback.

This registry does not fit or calibrate a model. It validates caller-selected
artifacts and leaves assets without an artifact on their own baseline model.
Loading retains the fitted artifact's research-only execution status.
"""
from __future__ import annotations

from .crypto_assets import asset_spec
from .fitted_parameters import FittedParameterError, FittedParameters, load_fitted_parameters


def _selected_assets(assets):
    if (not isinstance(assets, (tuple, list))
            or any(not isinstance(asset, str) for asset in assets)
            or len(assets) != len(set(assets))):
        raise FittedParameterError('Select distinct supported crypto assets for probability fits')
    try:
        return tuple(asset_spec(asset).symbol for asset in assets)
    except (ValueError, TypeError):
        raise FittedParameterError('Select distinct supported crypto assets for probability fits') from None


def _bindings(bindings, selected):
    if not isinstance(bindings, (tuple, list)):
        raise FittedParameterError('Per-asset probability fits must use ASSET=PATH bindings')
    result = {}
    for binding in bindings:
        if not isinstance(binding, str):
            raise FittedParameterError('Per-asset probability fits must use ASSET=PATH bindings')
        asset, separator, path = binding.partition('=')
        if (not separator or not path or not path.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            raise FittedParameterError('Per-asset probability fits must use ASSET=PATH bindings')
        if asset not in selected:
            raise FittedParameterError('A probability fit must name a selected supported crypto asset')
        if asset in result:
            raise FittedParameterError('Choose only one probability fit for each selected asset')
        result[asset] = path
    return result


def load_asset_probability_fits(single_path=None, bindings=(), assets=('BTC',)) -> dict[str, FittedParameters]:
    """Load an optional legacy artifact plus explicit ``ASSET=PATH`` bindings.

    Paths are read through the established integrity/schema/path validator;
    this function never writes artifacts or accesses account credentials.
    A legacy artifact is keyed by its validated asset metadata, which must be
    selected. The integrity hash is not venue authentication.
    Empty selections are useful to callers whose only selected asset is Gold;
    in that case no crypto fits can be supplied.
    """
    selected = _selected_assets(assets)
    paths = _bindings(bindings, selected)
    result = {}
    if single_path is not None:
        fitted = load_fitted_parameters(single_path)
        if fitted.asset not in selected:
            raise FittedParameterError('The probability fit asset is not selected')
        if fitted.asset in paths:
            raise FittedParameterError('Choose only one probability fit for each selected asset')
        result[fitted.asset] = fitted
    for asset, path in paths.items():
        fitted = load_fitted_parameters(path)
        if fitted.asset != asset:
            raise FittedParameterError('The probability fit artifact does not match its selected asset binding')
        result[asset] = fitted
    return result
