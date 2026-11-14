"""Explicit, read-only loading of a historical arithmetic-model fit.

The SHA256 checks canonical artifact integrity; it is not authentication or
evidence of predictive performance. Loading never changes execution eligibility.
No artifact, credentials, or environment variables are read at import time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
from pathlib import Path
import re

from .calibration import MODEL_VERSION
from .crypto_assets import asset_spec


MAX_ARTIFACT_BYTES = 2_000_000
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_STATUS = 'fitted_research_not_live_validated'


class FittedParameterError(ValueError):
    """Fixed, non-sensitive fitted-artifact or compatibility error."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _created_at(value):
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 48
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError()
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0) or stamp.timestamp() <= 0:
            raise ValueError()
    except (ValueError, TypeError, OverflowError, OSError):
        raise FittedParameterError('Fitted artifact creation time must be a valid UTC timestamp') from None
    return value


def _parameters(asset, half_life_seconds, volatility_scale, artifact_sha256, created_at):
    try:
        asset_spec(asset)
    except ValueError:
        raise FittedParameterError('Fitted artifact asset is unsupported') from None
    if (not _finite(half_life_seconds) or not 1 <= half_life_seconds <= 3600
            or not _finite(volatility_scale) or not .5 <= volatility_scale <= 2):
        raise FittedParameterError('Fitted volatility parameters are outside the supported bounds')
    if not isinstance(artifact_sha256, str) or not _SHA256.fullmatch(artifact_sha256):
        raise FittedParameterError('Fitted artifact integrity hash is missing or invalid')
    _created_at(created_at)


def _scores(count, baseline_log_loss, fitted_log_loss, baseline_brier, fitted_brier):
    if type(count) is not int or not 20 <= count <= 100000:
        raise FittedParameterError('Fitted artifact test contract count is invalid')
    if any(not _finite(value) or value < 0 for value in (baseline_log_loss, fitted_log_loss)):
        raise FittedParameterError('Fitted artifact held-out scores are invalid')
    if any(not _finite(value) or not 0 <= value <= 1 for value in (baseline_brier, fitted_brier)):
        raise FittedParameterError('Fitted artifact held-out scores are invalid')


@dataclass(frozen=True)
class FittedParameters:
    """Immutable applied parameters with compact held-out research evidence."""

    asset: str
    half_life_seconds: float
    volatility_scale: float
    artifact_sha256: str
    created_at: str
    test_contract_count: int
    test_baseline_log_loss: float
    test_fitted_log_loss: float
    test_baseline_brier_score: float
    test_fitted_brier_score: float

    def __post_init__(self):
        _parameters(self.asset, self.half_life_seconds, self.volatility_scale,
                    self.artifact_sha256, self.created_at)
        _scores(self.test_contract_count, self.test_baseline_log_loss,
                self.test_fitted_log_loss, self.test_baseline_brier_score,
                self.test_fitted_brier_score)

    @property
    def out_of_sample_improved(self):
        """Descriptive improvement on both scores, without a significance claim."""
        return (self.test_fitted_log_loss < self.test_baseline_log_loss
                and self.test_fitted_brier_score < self.test_baseline_brier_score)

    def metadata(self):
        """Fresh JSON-safe public metadata; no paths or raw training records."""
        return dict(asset=self.asset, half_life_seconds=self.half_life_seconds,
            volatility_scale=self.volatility_scale, artifact_sha256=self.artifact_sha256,
            created_at=self.created_at, calibration_status=_STATUS,
            model_scope='frozen_arithmetic_diffusion', fitted=True, calibrated=False,
            history_window_seconds=1800, min_live_history_seconds=1795,
            execution_eligible=False, test_contract_count=self.test_contract_count,
            test_baseline_log_loss=self.test_baseline_log_loss,
            test_fitted_log_loss=self.test_fitted_log_loss,
            test_baseline_brier_score=self.test_baseline_brier_score,
            test_fitted_brier_score=self.test_fitted_brier_score,
            out_of_sample_improved=self.out_of_sample_improved)

    def summary(self):
        return self.metadata()


def _read_artifact(path):
    # Apply the established separation from checkout/configuration files, but
    # never create a directory or rewrite the input artifact.
    try:
        from kalshi_bot import _private_path
        location = _private_path(Path(path))
        with location.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    except Exception:
        raise FittedParameterError('Fitted probability artifact is unavailable outside the checkout') from None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise FittedParameterError('Fitted probability artifact exceeds the two MB size bound')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FittedParameterError('Fitted probability artifact contains duplicate JSON fields')
            result[key] = value
        return result

    def no_constant(_):
        raise FittedParameterError('Fitted probability artifact contains nonfinite JSON')

    try:
        artifact = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                              parse_constant=no_constant)
        if not isinstance(artifact, dict):
            raise ValueError()
        expected = artifact.get('artifact_sha256')
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise FittedParameterError('Fitted artifact integrity hash is missing or invalid')
        canonical = json.dumps({key: value for key, value in artifact.items()
                                if key != 'artifact_sha256'},
                               allow_nan=False, sort_keys=True, separators=(',', ':'))
        actual = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise FittedParameterError('Fitted probability artifact integrity check failed')
    except FittedParameterError:
        raise
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise FittedParameterError('Fitted probability artifact is not valid finite JSON') from None
    return artifact


def load_fitted_parameters(path):
    """Load an explicit compatible fit, including one with worse held-out scores.

    A fit's recorded test evidence remains visible. Integrity and compatibility
    checks do not upgrade research status or reinterpret fitted parameters as
    ensemble weights.
    """
    artifact = _read_artifact(path)
    if (artifact.get('kind') != 'kalshi_probability_fit'
            or type(artifact.get('version')) is not int or artifact['version'] != 1
            or artifact.get('model_version') != MODEL_VERSION
            or artifact.get('fitted') is not True
            or artifact.get('calibrated') is not False
            or artifact.get('execution_eligible') is not False
            or artifact.get('deployment_status') != 'offline_research_only'
            or artifact.get('calibration_status') != _STATUS):
        raise FittedParameterError('Fitted probability artifact schema or research status is incompatible')
    try:
        profile = asset_spec(artifact.get('asset'))
        asset = profile.symbol
    except ValueError:
        raise FittedParameterError('Fitted artifact asset is unsupported') from None
    from .live_probability import probability_model_version
    from .probability_greeks import greeks_model_version
    if (artifact.get('target_monte_carlo_model_version') != probability_model_version(asset)
            or artifact.get('target_greeks_model_version') != greeks_model_version(asset)):
        raise FittedParameterError('Fitted artifact target model version is incompatible')
    provenance = artifact.get('data_provenance')
    if (not isinstance(provenance, dict)
            or provenance.get('source') != 'historical_download_not_live_receipt'
            or provenance.get('asset') != asset or provenance.get('index_id') != profile.index_id):
        raise FittedParameterError('Fitted artifact historical source or asset binding is incompatible')
    try:
        selected, counts = artifact['selected'], artifact['counts']
        if (not isinstance(selected, dict) or not isinstance(counts, dict)
                or type(counts.get('input_contracts')) is not int
                or not 200 <= counts['input_contracts'] <= 100000
                or type(counts.get('utc_days')) is not int or not 5 <= counts['utc_days'] <= 365):
            raise ValueError()
        features = artifact.get('features')
        if (not isinstance(features, dict) or features.get('asset') != asset
                or type(features.get('history_seconds')) is not int or features['history_seconds'] != 1800
                or features.get('exact_second_grid_required') is not True
                or not isinstance(features.get('half_lives_seconds'), list)
                or selected.get('half_life_seconds') not in features['half_lives_seconds']):
            raise FittedParameterError('Fitted artifact history window or volatility candidates are incompatible')
        baseline = artifact['metrics']['test']['baseline']
        fitted = artifact['metrics']['test']['selected']
        count = baseline['count_contracts']
        if fitted['count_contracts'] != count or type(fitted['count_contracts']) is not int or count > counts['input_contracts']:
            raise ValueError()
        return FittedParameters(asset=asset,
            half_life_seconds=selected['half_life_seconds'], volatility_scale=selected['volatility_scale'],
            artifact_sha256=artifact['artifact_sha256'], created_at=artifact['created_at'],
            test_contract_count=count, test_baseline_log_loss=baseline['log_loss'],
            test_fitted_log_loss=fitted['log_loss'], test_baseline_brier_score=baseline['brier_score'],
            test_fitted_brier_score=fitted['brier_score'])
    except FittedParameterError:
        raise
    except (KeyError, ValueError, TypeError, OverflowError):
        raise FittedParameterError('Fitted probability artifact parameters or evaluation evidence are incomplete') from None
