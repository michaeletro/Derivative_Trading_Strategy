"""Offline, base-training-only filtered historical settlement path laws.

Native one-second residuals use a preceding-window EWMA, excluding the return
being standardized. Forecasts freeze the caller's origin variance and start a
fresh independently sampled block at origin+1, including inside a fixing minute.
The arithmetic price proxy and sixty exact T-60,...,T-1 fixing samples agree
with the Gaussian control. Ordinary block draws preserve empirical asymmetry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math

import numpy as np

from .garch_math import _cdf, _geometry


HORIZONS = (900, 600, 300, 180, 120, 60, 30)


class FHSError(ValueError):
    """Invalid native residual pool or conditional fixing state."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _freeze(value, dtype):
    result = np.array(value, dtype=dtype, copy=True)
    result.flags.writeable = False
    return result


def _array_digest(arrays):
    digest = hashlib.sha256()
    for name, value in sorted(arrays.items()):
        array = np.asarray(value)
        descriptor = json.dumps([name, array.dtype.str, list(array.shape)], separators=(',', ':'))
        digest.update(descriptor.encode())
        digest.update(b'\n')
        digest.update(np.ascontiguousarray(array).tobytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class FHSBlockPool:
    seconds: int
    starts: np.ndarray
    means: np.ndarray
    scales: np.ndarray
    covariance: np.ndarray
    weights: np.ndarray = field(repr=False)
    cumulative_weights: np.ndarray = field(repr=False)


@dataclass(frozen=True)
class FHSPool:
    times: np.ndarray
    residuals: np.ndarray
    segments: np.ndarray
    blocks: dict
    fit_audit: dict

    def arrays(self):
        """Numeric arrays for np.savez_compressed; loading needs allow_pickle=False."""
        result = dict(times=self.times, residuals=self.residuals, segments=self.segments)
        for seconds, block in self.blocks.items():
            result.update({f'starts_{seconds}': block.starts,
                f'means_{seconds}': block.means, f'scales_{seconds}': block.scales,
                f'covariance_{seconds}': block.covariance})
        return result

    def metadata(self):
        return dict(self.fit_audit, arrays_sha256=_array_digest(self.arrays()),
            array_storage='numeric NPZ; load with allow_pickle=False',
            block_definitions={str(seconds): dict(seconds=seconds, blocks=len(block.starts),
                weighted_column_means=block.means.tolist(),
                weighted_column_scales=block.scales.tolist(),
                normalized_covariance=block.covariance.tolist(),
                effective_blocks=float(1 / (block.weights @ block.weights)),
                first_block_start=int(self.times[block.starts[0]])-1,
                last_block_end=int(self.times[block.starts[-1]+seconds-1]),
                normalization='each block column: weighted zero mean and unit second moment',
                cross_covariance_preserved=True)
                for seconds, block in self.blocks.items()})


def load_fhs_pool(arrays, metadata):
    """Reconstitute a saved numeric pool and verify its metadata-bound array hash."""
    if (not isinstance(metadata, dict) or metadata.get('kind') != 'kalshi_fhs_frozen_base_pool'
            or metadata.get('version') != 1 or not isinstance(metadata.get('block_seconds'), list)):
        raise FHSError('A versioned FHS pool metadata artifact is required')
    expected = {'times', 'residuals', 'segments'}
    for seconds in metadata['block_seconds']:
        expected.update(f'{name}_{seconds}' for name in ('starts', 'means', 'scales', 'covariance'))
    if set(arrays) != expected or _array_digest(arrays) != metadata.get('arrays_sha256'):
        raise FHSError('Saved FHS arrays differ from the bound pool metadata')
    times = _freeze(arrays['times'], '<i8')
    residuals = _freeze(arrays['residuals'], '<f8')
    segments = _freeze(arrays['segments'], '<i4')
    if (times.ndim != 1 or residuals.shape != times.shape or segments.shape != times.shape
            or len(times) != metadata.get('residuals') or not len(times)
            or np.any(np.diff(times) <= 0) or not np.isfinite(residuals).all()):
        raise FHSError('Saved residual array dimensions or native order are inconsistent')
    blocks = {}
    for seconds in metadata['block_seconds']:
        starts = _freeze(arrays[f'starts_{seconds}'], '<i8')
        means = _freeze(arrays[f'means_{seconds}'], '<f8')
        scales = _freeze(arrays[f'scales_{seconds}'], '<f8')
        covariance = _freeze(arrays[f'covariance_{seconds}'], '<f8')
        if (starts.ndim != 1 or not len(starts) or starts[0] < 0
                or starts[-1]+seconds > len(times) or np.any(np.diff(starts) <= 0)
                or means.shape != (seconds,) or scales.shape != (seconds,)
                or covariance.shape != (seconds, seconds) or not np.isfinite(means).all()
                or not np.isfinite(scales).all() or np.min(scales) <= 0
                or not np.isfinite(covariance).all()
                or np.any(times[starts+seconds-1]-times[starts] != seconds-1)
                or np.any(segments[starts+seconds-1] != segments[starts])):
            raise FHSError('Saved FHS block indices or normalizers are inconsistent')
        ends = starts+seconds-1
        weights = np.exp(-math.log(2)*(float(times[ends[-1]])-times[ends])
                         / (metadata['block_half_life_days']*86400))
        weights /= weights.sum()
        cumulative = np.cumsum(weights)
        cumulative[-1] = 1.
        blocks[seconds] = FHSBlockPool(seconds, starts, means, scales, covariance,
            _freeze(weights, '<f8'), _freeze(cumulative, '<f8'))
    audit = {key: value for key, value in metadata.items()
             if key not in ('arrays_sha256', 'array_storage', 'block_definitions')}
    return FHSPool(times, residuals, segments, blocks, audit)


def fit_fhs_pool(second_returns, *, first_time, last_time,
                 block_seconds=(1, 5, 15), window_seconds=3600,
                 half_life_seconds=300., block_half_life_days=7.,
                 minimum_residuals=300, variance_floor=1e-18):
    """Fit frozen empirical blocks; every response and its warmup are in base train.

    Input times are return END timestamps. Returns span [time-1,time]. Gaps
    reset filtering even when a supplied segment identifier failed to change.
    Pool values outside the fitting interval are never read or standardized.
    The `runs` input, when present, is an extraction audit rather than a filter
    state: native timestamp adjacency and segment identifiers govern resets.
    """
    if (not isinstance(second_returns, dict)
            or not all(_finite(v) for v in (first_time, last_time, half_life_seconds,
                block_half_life_days, variance_floor))
            or first_time >= last_time or half_life_seconds <= 0
            or block_half_life_days <= 0 or variance_floor <= 0
            or type(window_seconds) is not int or not 2 <= window_seconds <= 86400
            or type(minimum_residuals) is not int or minimum_residuals < 2
            or not isinstance(block_seconds, (list, tuple)) or not block_seconds
            or any(type(v) is not int or not 1 <= v <= 60 for v in block_seconds)
            or len(set(block_seconds)) != len(block_seconds)):
        raise FHSError('Invalid bounded native residual fitting configuration')
    try:
        times = np.asarray(second_returns['times'])
        returns = np.asarray(second_returns['returns'])
        segments = np.asarray(second_returns['segments'])
        if (times.ndim != 1 or returns.shape != times.shape or segments.shape != times.shape
                or not 1 <= len(times) <= 31*86400
                or not np.issubdtype(times.dtype, np.integer)
                or not np.issubdtype(segments.dtype, np.integer)
                or np.any(np.diff(times) <= 0) or np.any(segments < 1)):
            raise ValueError()
        selected = (times-1 >= first_time) & (times <= last_time)
        stamps, values, identifiers = times[selected], returns[selected], segments[selected]
        if not len(stamps) or not np.isfinite(values).all():
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise FHSError('Consecutive native one-second returns and segment identifiers are required') from None

    decay = math.exp(-math.log(2)/half_life_seconds)
    expiry = decay**window_seconds
    acc = mass = 0.
    count, previous, segment = 0, None, None
    out_times, residuals, out_segments = [], [], []
    segment_number = 0
    first_warmup_start = None
    for i, (stamp, value, identifier) in enumerate(zip(stamps, values, identifiers)):
        if previous is None or stamp != previous+1 or identifier != segment:
            acc = mass = 0.
            count = 0
            segment_number += 1
        # This is h_(s|s-1). The current return is added only after extraction.
        if count >= window_seconds:
            variance = max(variance_floor, acc/mass)
            out_times.append(stamp)
            residuals.append(float(value)/math.sqrt(variance))
            out_segments.append(segment_number)
            if first_warmup_start is None:
                first_warmup_start = int(stamp)-window_seconds-1
        acc = float(value)*float(value)+decay*acc
        mass = 1+decay*mass
        if count >= window_seconds:
            old = float(values[i-window_seconds])
            acc -= expiry*old*old
            mass -= expiry
        count += 1
        previous, segment = stamp, identifier
    if len(residuals) < minimum_residuals:
        raise FHSError('Insufficient native base-training residuals after causal warmup')
    times = _freeze(out_times, '<i8')
    residuals = _freeze(residuals, '<f8')
    segments = _freeze(out_segments, '<i4')
    if not np.isfinite(residuals).all():
        raise FHSError('The causal standardized residuals exceed their numerical bound')

    blocks = {}
    for seconds in block_seconds:
        starts = np.arange(len(residuals)-seconds+1, dtype='<i8')
        if len(starts):
            ends = starts+seconds-1
            starts = starts[(times[ends]-times[starts] == seconds-1)
                & (segments[ends] == segments[starts])]
        if len(starts) < minimum_residuals:
            raise FHSError('Insufficient complete base-training blocks for '+str(seconds)+' seconds')
        ends = starts+seconds-1
        weights = np.exp(-math.log(2)*(float(times[ends[-1]])-times[ends])
                         / (block_half_life_days*86400))
        weights /= weights.sum()
        means = np.array([weights @ residuals[starts+j] for j in range(seconds)])
        scales = np.sqrt([weights @ np.square(residuals[starts+j]-means[j])
                          for j in range(seconds)])
        if not np.isfinite(scales).all() or np.min(scales) <= 1e-12:
            raise FHSError('A standardized block column has no usable variation')
        covariance = np.zeros((seconds, seconds))
        # Bound memory independently of the number of overlapping source blocks.
        for lower in range(0, len(starts), 16384):
            upper = min(len(starts), lower+16384)
            z = (residuals[starts[lower:upper, None]+np.arange(seconds)]-means)/scales
            covariance += z.T @ (weights[lower:upper, None]*z)
        covariance = (covariance+covariance.T)/2
        cumulative = np.cumsum(weights)
        cumulative[-1] = 1.
        blocks[seconds] = FHSBlockPool(seconds, _freeze(starts, '<i8'),
            _freeze(means, '<f8'), _freeze(scales, '<f8'), _freeze(covariance, '<f8'),
            _freeze(weights, '<f8'), _freeze(cumulative, '<f8'))
    audit = dict(kind='kalshi_fhs_frozen_base_pool', version=1,
        first_time=first_time, last_time=last_time,
        first_residual_end=int(times[0]), last_residual_end=int(times[-1]),
        first_warmup_start=first_warmup_start, residuals=len(residuals),
        residual_segments=len(np.unique(segments)), window_seconds=window_seconds,
        half_life_seconds=half_life_seconds, variance_floor=variance_floor,
        block_half_life_days=block_half_life_days, block_seconds=list(block_seconds),
        residual_filter='prior completed native one-second squared returns; current return excluded',
        source_return_units='one-second log return',
        source_returns_zero_mean_model=True,
        sampling='exponential recency weights on complete block END time relative to final eligible block end',
        fitting_stage='base_train', entire_warmup_inside_base=True,
        labels_used=False, validation_used=False, test_used=False,
        forecast_variance='caller origin baseline variance per second, frozen over future path',
        block_initialization='fresh independent block at origin+1; never continue an observed historical block',
        research_only=True, execution_eligible=False)
    pool = FHSPool(times, residuals, segments, blocks, audit)
    return pool, pool.metadata()


def _offsets(future_offsets):
    if not isinstance(future_offsets, (list, tuple, np.ndarray)):
        raise FHSError('Future fixing offsets must be a native-second sequence')
    values = np.asarray(future_offsets, dtype=float)
    if (values.ndim != 1 or len(values) > 60 or not np.isfinite(values).all()
            or np.any(values != np.floor(values)) or np.any(values <= 0)
            or np.any(values > 899) or np.any(np.diff(values) <= 0)):
        raise FHSError('Future fixing offsets must increase over integer seconds 1 through 899')
    return tuple(int(x) for x in values)


def fixing_weights(future_offsets):
    """Noise in the settlement AVERAGE is sum_j weight_j * one-second return_j."""
    offsets = _offsets(future_offsets)
    if not offsets:
        return np.empty(0)
    seconds = np.arange(1, offsets[-1]+1)
    return (len(offsets)-np.searchsorted(offsets, seconds, side='left'))/60.


class FixingFunctionalBank:
    """Shared, deterministic scalar MC banks conditional only on future geometry.

    The same bank serves every origin of that geometry; numerical errors are
    therefore correlated across origins. Seeds are derived from geometry, so
    cache access order cannot alter draws. No antithetic sign flips are used.
    """
    def __init__(self, pool, *, block_seconds=1, paths=4096, seed=7):
        if (not isinstance(pool, FHSPool) or block_seconds not in pool.blocks
                or type(paths) is not int or not 128 <= paths <= 131072
                or type(seed) is not int or not 0 <= seed <= 2**32-1):
            raise FHSError('A frozen fitted block pool and bounded MC settings are required')
        self.pool, self.block_seconds, self.paths, self.seed = pool, block_seconds, paths, seed
        self._cache, self._audits = {}, {}

    def draws(self, future_offsets):
        key = _offsets(future_offsets)
        if key in self._cache:
            return self._cache[key]
        weights = fixing_weights(key)
        block = self.pool.blocks[self.block_seconds]
        count = (len(weights)+self.block_seconds-1)//self.block_seconds
        coefficients = np.zeros((count, self.block_seconds))
        coefficients.flat[:len(weights)] = weights
        token = json.dumps([self.seed, self.block_seconds, key], separators=(',', ':')).encode()
        seed = int.from_bytes(hashlib.sha256(token).digest()[:16], 'little')
        rng = np.random.default_rng(seed)
        result = np.zeros(self.paths)
        for lower in range(0, self.paths, 512):
            upper = min(self.paths, lower+512)
            choices = np.searchsorted(block.cumulative_weights,
                rng.random((upper-lower, count)), side='right')
            starts = block.starts[choices]
            for column in range(self.block_seconds):
                if count:
                    z = (self.pool.residuals[starts+column]-block.means[column])/block.scales[column]
                    result[lower:upper] += z @ coefficients[:, column]
        variance = float(np.einsum('ij,jk,ik->', coefficients, block.covariance, coefficients))
        gaussian_variance = float(weights @ weights)
        mean = float(result.mean())
        centered = result-mean
        sample_variance = float(centered @ centered/(self.paths-1))
        skew = (float(np.mean(centered**3))/sample_variance**1.5) if sample_variance > 0 else 0.
        result.sort()
        result.flags.writeable = False
        self._cache[key] = result
        self._audits[key] = dict(paths=self.paths, seed=self.seed, block_seconds=self.block_seconds,
            future_samples=len(key), last_future_offset=key[-1] if key else None,
            gaussian_control_variance=gaussian_variance,
            exact_pool_functional_mean=0., exact_pool_functional_variance=variance,
            path_variance_to_iid_ratio=variance/gaussian_variance if gaussian_variance else None,
            sample_mean=mean, sample_variance=sample_variance, sample_skewness=skew,
            sample_quantiles=np.quantile(result, [.001,.01,.5,.99,.999]).tolist(),
            standardized_samples_sha256=hashlib.sha256(result.tobytes()).hexdigest(),
            ordinary_independent_block_draws=True, antithetic_symmetrization=False,
            shared_across_origins=True, monte_carlo_errors_across_origins_independent=False,
            terminal_variance_renormalized=False)
        return result

    def audit(self, future_offsets):
        key = _offsets(future_offsets)
        self.draws(key)
        return dict(self._audits[key])


def sample_banks(pool, *, paths=4096, seed=7):
    """Convenience mapping block seconds -> horizon -> sorted functional draws."""
    result = {}
    for seconds in pool.blocks:
        bank = FixingFunctionalBank(pool, block_seconds=seconds, paths=paths, seed=seed)
        result[seconds] = {horizon: bank.draws([horizon-offset for offset in range(60, 0, -1)
            if horizon-offset > 0]) for horizon in HORIZONS}
    return result


def fhs_probability(row, *, variance_per_second, bank, gaussian_weight=.25,
                    probability_floor=1e-12):
    """Empirical fixing-functional survival, shrunk to the matched Gaussian law."""
    geometry = _geometry(row)
    if (not isinstance(bank, FixingFunctionalBank)
            or not _finite(variance_per_second) or variance_per_second <= 0
            or not _finite(gaussian_weight) or not 0 <= gaussian_weight <= 1
            or probability_floor is not None and (not _finite(probability_floor)
                or not 0 < probability_floor < .5)):
        raise FHSError('A positive origin variance and declared probability mixture are required')
    if geometry['count'] == 0:
        probability = _cdf(geometry['numerator'], 0., geometry)
        return dict(probability_yes=probability, probability_no=1-probability,
            raw_probability_yes=probability, empirical_probability_yes=probability,
            gaussian_probability_yes=probability, monte_carlo_se=0., empirical_monte_carlo_se=0.,
            fully_fixed=True, probability_floor_applied=False, paths=bank.paths,
            research_only=True, execution_eligible=False)
    draws = bank.draws(row['future_offsets'])
    threshold = -geometry['numerator']/(60*geometry['spot']*math.sqrt(variance_per_second))
    left = np.searchsorted(draws, threshold, side='left')
    right = np.searchsorted(draws, threshold, side='right')
    if geometry['rounding_tie_ambiguous'] and gaussian_weight < 1 and left != right:
        raise FHSError('Empirical mass at an unspecified reporting rounding half-boundary')
    side = 'right' if geometry['strike_type'] == 'greater' else 'left'
    empirical = float((len(draws)-(right if side == 'right' else left))/len(draws))
    weights = fixing_weights(row['future_offsets'])
    gaussian = .5*math.erfc(threshold/math.sqrt(2*float(weights @ weights)))
    raw = (1-gaussian_weight)*empirical+gaussian_weight*gaussian
    probability = raw if probability_floor is None else min(1-probability_floor, max(probability_floor, raw))
    empirical_se = math.sqrt(empirical*(1-empirical)/len(draws))
    return dict(probability_yes=probability, probability_no=1-probability,
        raw_probability_yes=raw, empirical_probability_yes=empirical,
        gaussian_probability_yes=gaussian, gaussian_weight=gaussian_weight,
        monte_carlo_se=(1-gaussian_weight)*empirical_se, empirical_monte_carlo_se=empirical_se,
        monte_carlo_se_method='plug-in binomial conditional on frozen residual pool; not unseen-tail or fit uncertainty',
        monte_carlo_errors_across_origins_independent=False, fully_fixed=False,
        probability_floor=probability_floor, probability_floor_applied=probability != raw,
        paths=len(draws), block_seconds=bank.block_seconds,
        variance_per_second=variance_per_second,
        settlement_average_mean=geometry['mean'],
        block_initialization='fresh independent future block at origin+1; observed fixing samples remain fixed',
        research_only=True, execution_eligible=False)
