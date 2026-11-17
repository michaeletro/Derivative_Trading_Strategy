"""Offline contract-balanced scoring and causal regime diagnostics.

This module neither fits predictive models nor selects test-set winners. A
single paired UTC-day bootstrap is shared by all candidate comparisons; reported
simultaneous intervals cover the candidate-by-score family conditionally on the
already fitted models. Historical receipt eligibility belongs to the caller.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math

import numpy as np


PROBABILITY_EPSILON = 1e-12
AXES = ("horizon", "fixing", "moneyness", "trend", "volatility", "trend_volatility")
NORMAL_ONE_SIGMA_CDF = 0.8413447460685429


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _asset(row):
    value = row.get("asset")
    if isinstance(value, str) and value:
        return value
    ticker = row.get("ticker", "")
    for symbol in ("BTC", "ETH", "SOL", "XRP"):
        if isinstance(ticker, str) and ticker.startswith("KX" + symbol + "15M-"):
            return symbol
    return "unspecified"


def _contract(row):
    ticker = row.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        raise ValueError("Each forecast needs an explicit contract identity")
    return (_asset(row), ticker)


def _day(row):
    close = row.get("close_time")
    if _number(close):
        day = datetime.fromtimestamp(close, timezone.utc).date().isoformat()
        if row.get("day") is not None and row["day"] != day:
            raise ValueError("Contract UTC day disagrees with its close clock")
        return day
    day = row.get("day")
    if not isinstance(day, str):
        raise ValueError("Each contract needs its UTC close day")
    try:
        if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
            raise ValueError()
    except ValueError:
        raise ValueError("Contract close day must be an ISO UTC date") from None
    return day


def _cohort(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > 500000:
        raise ValueError("Use a bounded sequence of common forecast origins")
    keys, days, labels, identities, origins = [], [], [], {}, set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Each forecast origin must be an object")
        key, day = _contract(row), _day(row)
        y = row.get("y", row.get("official_y", row.get("officialy")))
        if type(y) is not int or y not in (0, 1):
            raise ValueError("Each common origin needs a verified binary label")
        if any(row.get(alias) is not None and row[alias] != y
               for alias in ("y", "official_y", "officialy")):
            raise ValueError("Official label aliases disagree")
        identity = (day, y, row.get("close_time"))
        if key in identities and identities[key] != identity:
            raise ValueError("One contract has conflicting labels or close clocks")
        identities[key] = identity
        stamp = row.get("as_of", row.get("model_time"))
        if stamp is not None:
            if not _number(stamp):
                raise ValueError("Forecast origins must use finite clocks")
            if _number(row.get("close_time")) and stamp >= row["close_time"]:
                raise ValueError("Forecast origins must precede contract close")
            origin = (key, stamp)
            if origin in origins:
                raise ValueError("Duplicate common contract/origin identity")
            origins.add(origin)
        keys.append(key)
        days.append(day)
        labels.append(y)
    counts = Counter(keys)
    weights = np.array([1.0 / counts[key] for key in keys], dtype=float)
    return keys, days, np.asarray(labels, dtype=float), weights


def _probabilities(values, length):
    try:
        result = np.asarray(values, dtype=float)
    except (ValueError, TypeError):
        raise ValueError("Every candidate must supply finite common-case probabilities") from None
    if result.shape != (length,) or not np.all(np.isfinite(result)) or np.any((result < 0) | (result > 1)):
        raise ValueError("Every candidate must cover identical origins with probabilities in [0, 1]")
    return result


def _losses(y, p):
    bounded = np.clip(p, PROBABILITY_EPSILON, 1.0 - PROBABILITY_EPSILON)
    return np.column_stack((
        -(y * np.log(bounded) + (1.0 - y) * np.log1p(-bounded)),
        (y - p) ** 2,
    ))


def score(rows, probs, *, reliability=True):
    """Score identical origins with unit aggregate mass per contract."""
    keys, days, y, weights = _cohort(rows)
    p = _probabilities(probs, len(rows))
    result = dict(
        count_contracts=len(set(keys)),
        count_forecasts=len(rows),
        count_days=len(set(days)),
        count_assets=len({key[0] for key in keys}),
        log_loss=None,
        brier_score=None,
        ece=None,
        status="ready" if rows else "insufficient_data",
    )
    if not rows:
        if reliability:
            result["reliability"] = []
        return result
    mass = float(weights.sum())
    losses = _losses(y, p)
    result["log_loss"] = float(weights @ losses[:, 0] / mass)
    result["brier_score"] = float(weights @ losses[:, 1] / mass)
    if reliability:
        bins, ece = [], 0.0
        memberships = np.minimum((p * 10).astype(int), 9)
        for index in range(10):
            selected = np.flatnonzero(memberships == index)
            bin_mass = float(weights[selected].sum())
            mean_p = float(weights[selected] @ p[selected] / bin_mass) if bin_mass else None
            frequency = float(weights[selected] @ y[selected] / bin_mass) if bin_mass else None
            if bin_mass:
                ece += bin_mass / mass * abs(mean_p - frequency)
            bins.append(dict(
                lower=index / 10.0,
                upper=(index + 1) / 10.0,
                upper_inclusive=index == 9,
                count_forecasts=len(selected),
                count_contracts=len({keys[i] for i in selected}),
                weight=bin_mass / mass,
                mean_probability=mean_p,
                observed_frequency=frequency,
            ))
        result.update(ece=float(ece), reliability=bins)
    return result


def _first_number(row, names):
    for name in names:
        value = row.get(name)
        if _number(value):
            return float(value)
    return None


def _volatility(row):
    value = _first_number(row, ("sigma_slow", "slow_sigma", "sigma", "volatility"))
    return value if value is not None and value >= 0 else None


def _weighted_quantiles(values, weights, quantiles):
    order = np.argsort(values, kind="stable")
    ordered, mass = values[order], weights[order]
    coordinates = (np.cumsum(mass) - 0.5 * mass) / mass.sum()
    return [float(np.interp(q, coordinates, ordered)) for q in quantiles]


def fit_regimes(train_rows):
    """Fit only asset-specific volatility thresholds; all other cuts are fixed."""
    keys, days, _, weights = _cohort(train_rows)
    values = defaultdict(list)
    for index, row in enumerate(train_rows):
        value = _volatility(row)
        if value is not None:
            values[_asset(row)].append((value, weights[index], keys[index], days[index]))
    thresholds = {}
    for asset, cells in sorted(values.items()):
        x = np.asarray([cell[0] for cell in cells])
        w = np.asarray([cell[1] for cell in cells])
        low, high = _weighted_quantiles(x, w, (1.0 / 3.0, 2.0 / 3.0))
        thresholds[asset] = dict(
            lower_tercile=low,
            upper_tercile=high,
            degenerate=low == high,
            count_contracts=len({cell[2] for cell in cells}),
            count_forecasts=len(cells),
            count_days=len({cell[3] for cell in cells}),
        )
    provenance = sorted((asset, ticker, day) for (asset, ticker), (day, _, _) in {
        _contract(row): (_day(row), row.get("y"), row.get("close_time")) for row in train_rows
    }.items())
    return dict(
        kind="kalshi_uniform_causal_regimes",
        version=1,
        execution_eligible=False,
        learned_from="training_only",
        volatility_quantiles=[1.0 / 3.0, 2.0 / 3.0],
        volatility_thresholds=thresholds,
        drift_thresholds=[-1.0, 1.0],
        moneyness_thresholds=[1.0 - NORMAL_ONE_SIGMA_CDF, NORMAL_ONE_SIGMA_CDF],
        fit_count_contracts=len(set(keys)),
        fit_count_forecasts=len(train_rows),
        fit_count_days=len(set(days)),
        training_membership_sha256=hashlib.sha256(
            json.dumps(provenance, separators=(",", ":")).encode()
        ).hexdigest(),
        notes=[
            "Regimes describe origin-time state, not future realized returns.",
            "Volatility cuts use contract-balanced training inputs only.",
            "Threshold distance uses the shared raw Gaussian probability or supplied z.",
            "Regimes do not select a candidate or authorize execution.",
        ],
    )


def regime_labels(rows, artifact):
    """Apply frozen train-only regimes without fitting or using outcome labels."""
    if (not isinstance(artifact, dict)
            or artifact.get("kind") != "kalshi_uniform_causal_regimes"
            or artifact.get("version") != 1):
        raise ValueError("A fitted causal regime artifact is required")
    labels = []
    for row in rows:
        remaining = _first_number(row, (
            "remaining_seconds", "seconds_remaining", "remainingseconds", "horizon_seconds"))
        if remaining is None:
            close = _first_number(row, ("close_time",))
            stamp = _first_number(row, ("as_of", "model_time"))
            if close is not None and stamp is not None:
                remaining = close - stamp
        horizon = format(remaining, ".12g") if remaining is not None and remaining > 0 else "unknown"
        count = _first_number(row, ("known_count", "observed_fixing_count"))
        fixing = "unknown" if count is None or count < 0 else ("none" if count == 0 else "partial")
        p = _first_number(row, ("g", "p_raw", "gaussian_probability", "gaussian_p"))
        z = _first_number(row, ("z", "moneyness_z", "standardized_distance"))
        moneyness = "unknown"
        if p is not None and 0 <= p <= 1:
            lower, upper = artifact["moneyness_thresholds"]
            moneyness = "below_threshold" if p < lower else ("above_threshold" if p > upper else "near_threshold")
        elif z is not None:
            moneyness = "below_threshold" if z < -1 else ("above_threshold" if z > 1 else "near_threshold")
        drift = _first_number(row, ("drift_score", "drift_z"))
        trend = "unknown" if drift is None else ("bearish" if drift <= -1 else ("bullish" if drift >= 1 else "neutral"))
        volatility = "unknown"
        cuts = artifact["volatility_thresholds"].get(_asset(row))
        vol = _volatility(row)
        if cuts is not None and vol is not None:
            volatility = ("low" if vol <= cuts["lower_tercile"]
                          else ("medium" if vol <= cuts["upper_tercile"] else "high"))
        joint = "unknown" if "unknown" in (trend, volatility) else trend + "|" + volatility
        labels.append(dict(
            horizon=horizon, fixing=fixing, moneyness=moneyness,
            trend=trend, volatility=volatility, trend_volatility=joint,
        ))
    return labels


def paired_comparisons(rows, predictions, reference="gaussian_ewma", *, samples=500, seed=7):
    """Shared paired-day intervals and conditional max-T family intervals.

    The simultaneous family covers every nonreference candidate and BOTH scores.
    It is not a resampling of model fitting/selection, nor a prospective trial.
    Identical forecasts and degenerate day variation are marked explicitly.
    """
    if type(samples) is not int or not 100 <= samples <= 10000:
        raise ValueError("Use between 100 and 10000 paired bootstrap draws")
    if type(seed) is not int or not 0 <= seed < 2 ** 32:
        raise ValueError("Use a bounded integer bootstrap seed")
    if not isinstance(predictions, dict) or reference not in predictions:
        raise ValueError("An explicit common-case reference candidate is required")
    keys, day_values, y, weights = _cohort(rows)
    arrays = {name: _probabilities(p, len(rows)) for name, p in predictions.items()}
    if any(not isinstance(name, str) or not name for name in arrays):
        raise ValueError("Every candidate needs a stable string identity")
    names = sorted(name for name in arrays if name != reference)
    days = sorted(set(day_values))
    result = dict(
        reference=reference, count_contracts=len(set(keys)), count_forecasts=len(rows),
        count_days=len(days), bootstrap_samples=samples, seed=seed,
        confidence=0.95, few_day_blocks=len(days) < 20,
        status="ready" if len(days) >= 2 else "insufficient_day_blocks",
        differences="candidate_minus_reference",
        simultaneous_family=dict(
            method="paired_day_centered_max_absolute_standardized_bootstrap",
            confidence=0.95, comparisons=len(names) * 2, critical_value=None,
            scores=["log_loss", "brier_score"],
            scope="all_nonreference_candidates_times_both_scores",
        ),
        comparisons={},
        limitations=[
            "Intervals condition on already fitted and selected candidates.",
            "A small number of day blocks limits uncertainty resolution.",
            "A zero interval for identical forecasts is mechanical, not evidence of model equivalence.",
        ],
    )
    if not rows:
        return result
    reference_losses = _losses(y, arrays[reference])
    denominator = float(weights.sum())
    losses = {name: _losses(y, arrays[name]) - reference_losses for name in names}
    points = {name: weights @ delta / denominator for name, delta in losses.items()}
    boots, standard_errors = {}, {}
    if len(days) >= 2 and names:
        day_index = {day: index for index, day in enumerate(days)}
        group = np.asarray([day_index[day] for day in day_values])
        day_mass = np.bincount(group, weights=weights, minlength=len(days))
        draws = np.random.default_rng(seed).integers(0, len(days), size=(samples, len(days)))
        draw_mass = day_mass[draws].sum(axis=1)
        for name in names:
            delta = losses[name]
            day_sum = np.column_stack([
                np.bincount(group, weights=weights * delta[:, j], minlength=len(days))
                for j in range(2)
            ])
            boots[name] = day_sum[draws].sum(axis=1) / draw_mass[:, None]
            standard_errors[name] = boots[name].std(axis=0, ddof=1)
        standardized = []
        for name in names:
            for metric in range(2):
                se = standard_errors[name][metric]
                if se > 1e-15:
                    standardized.append(np.abs((boots[name][:, metric] - points[name][metric]) / se))
        maximum = np.max(np.column_stack(standardized), axis=1) if standardized else np.zeros(samples)
        critical = float(np.quantile(maximum, 0.95))
        result["simultaneous_family"]["critical_value"] = critical
    else:
        maximum = None
        critical = None
    for name in names:
        identical = bool(np.array_equal(arrays[name], arrays[reference]))
        entry = dict(status="identical_forecasts" if identical else
                     ("paired_day_blocks" if len(days) >= 2 else "insufficient_day_blocks"))
        for index, metric in enumerate(("log_loss", "brier_score")):
            difference = float(points[name][index])
            cell = dict(difference=difference, ci95=None, simultaneous_ci95=None,
                        bootstrap_standard_error=None, adjusted_p_value=None)
            if name in boots:
                draws = boots[name][:, index]
                se = float(standard_errors[name][index])
                cell.update(ci95=[float(x) for x in np.quantile(draws, (0.025, 0.975))],
                            bootstrap_standard_error=se,
                            simultaneous_ci95=[difference - critical * se, difference + critical * se])
                if se > 1e-15:
                    observed = abs(difference / se)
                    cell["adjusted_p_value"] = float((1 + np.count_nonzero(maximum >= observed)) / (samples + 1))
                elif not identical:
                    cell["uncertainty_status"] = "degenerate_day_variation"
            if identical:
                cell["uncertainty_status"] = "mechanical_identity"
            entry[metric] = cell
        result["comparisons"][name] = entry
    return result


def supports_with_scores(rows, predictions, regime_artifact, min_contracts=50, min_days=3):
    """Describe all causal slices; never choose a winner from their scores."""
    if type(min_contracts) is not int or min_contracts < 1 or type(min_days) is not int or min_days < 1:
        raise ValueError("Regime support floors must be positive integers")
    _cohort(rows)
    if not isinstance(predictions, dict):
        raise ValueError("Supply explicit common-case candidate predictions")
    arrays = {name: _probabilities(p, len(rows)) for name, p in predictions.items()}
    labels = regime_labels(rows, regime_artifact)
    result = dict(
        minimum_support=dict(contracts=min_contracts, days=min_days),
        descriptive_only=True, winner_selection_performed=False, axes={},
    )
    for axis in AXES:
        groups = defaultdict(list)
        for index, label in enumerate(labels):
            groups[label[axis]].append(index)
        reports = {}
        for label, indices in sorted(groups.items()):
            selected = [rows[index] for index in indices]
            keys, days, _, _ = _cohort(selected)
            counts = dict(count_contracts=len(set(keys)), count_forecasts=len(selected), count_days=len(set(days)))
            unknown = label == "unknown"
            supported = not unknown and counts["count_contracts"] >= min_contracts and counts["count_days"] >= min_days
            reports[label] = dict(
                status="supported" if supported else "insufficient_support",
                reason=None if supported else ("missing_regime_inputs" if unknown else "too_few_contracts_or_UTC_days"),
                **counts,
                scores={name: score(selected, values[indices], reliability=True) for name, values in arrays.items()},
            )
        result["axes"][axis] = reports
    return result
