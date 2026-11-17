"""Uniform offline four-asset probability benchmark, with no execution integration."""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import numpy as np

ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (900, 600, 300, 180, 120, 60, 30)
STAGES = ("parent_fit", "model_train", "final_calibration", "validation", "test")
P_CLIP = 1e-6
PRIOR_EXAMINED_THROUGH = 1794714300.


class UniformBenchmarkError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class UniformBenchmarkConfig:
    paths: int = 4096
    seed: int = 7
    bootstrap_samples: int = 500
    minimum_test_days: int = 5
    minimum_stage_contracts: int = 20
    minimum_regime_contracts: int = 50
    minimum_regime_days: int = 3
    purge_seconds: int = 900
    calibration_ridges: tuple = (0., .01, .1, 1., 10.)
    calibration_half_lives: tuple = (None, 96, 288, 672)
    require_fresh_test: bool = False
    strict_receipts: bool = False

    def __post_init__(self):
        for name, low, high in (("paths", 256, 4096), ("seed", 0, 2**32-1),
                ("bootstrap_samples", 100, 2000), ("minimum_test_days", 5, 365),
                ("minimum_stage_contracts", 20, 100000),
                ("minimum_regime_contracts", 20, 100000),
                ("minimum_regime_days", 2, 365), ("purge_seconds", 900, 86400)):
            v = getattr(self, name)
            if type(v) is not int or not low <= v <= high:
                raise UniformBenchmarkError("Invalid bounded benchmark configuration: " + name)
        if self.paths % 2:
            raise UniformBenchmarkError("Use an even antithetic path budget")
        if type(self.strict_receipts) is not bool or type(self.require_fresh_test) is not bool:
            raise UniformBenchmarkError("Availability gates must be explicit booleans")
        if not self.calibration_ridges or any(type(v) not in (float, int) or
                not math.isfinite(v) or v < 0 for v in self.calibration_ridges):
            raise UniformBenchmarkError("Invalid calibration ridge grid")
        if None not in self.calibration_half_lives or any(v is not None and
                (type(v) is not int or v < 1) for v in self.calibration_half_lives):
            raise UniformBenchmarkError("Calibration must include uniform weights")


def contract_weights(rows):
    counts = Counter(r["ticker"] for r in rows)
    return np.array([1. / counts[r["ticker"]] for r in rows], dtype=float)


def map_probability(probabilities, parameters):
    p = np.clip(np.asarray(probabilities, dtype=float), P_CLIP, 1-P_CLIP)
    if parameters["kind"] == "identity":
        return p
    z = np.log(p) - np.log1p(-p)
    v = np.clip(parameters["intercept"] + parameters["slope"] * z, -700., 700.)
    return np.clip(1. / (1. + np.exp(-v)), P_CLIP, 1-P_CLIP)


def fit_probability_map(rows, probabilities, *, ridge=.1, half_life=None):
    """Monotone two-parameter map; all weights and parameters use fit rows only."""
    if len(rows) != len(probabilities) or not rows:
        raise UniformBenchmarkError("Calibration needs paired nonempty fit rows")
    p = np.clip(np.asarray(probabilities, dtype=float), P_CLIP, 1-P_CLIP)
    if not np.isfinite(p).all():
        raise UniformBenchmarkError("Nonfinite probability supplied to calibration")
    z = np.log(p) - np.log1p(-p)
    x = np.column_stack((np.ones(len(rows)), z))
    y = np.array([r["y"] for r in rows], dtype=float)
    w = contract_weights(rows)
    if half_life is not None:
        last = max(r["close_time"] for r in rows)
        w *= np.exp2(-np.array([last-r["close_time"] for r in rows]) / (900*half_life))
    w /= w.sum()
    target = np.array([0., 1.])
    beta = target.copy()
    def objective(b):
        v = x @ b
        return float(np.sum(w*(np.logaddexp(0., v)-y*v)) + .5*ridge*np.sum((b-target)**2))
    old = objective(beta)
    for _ in range(60):
        v = np.clip(x @ beta, -700., 700.)
        q = 1. / (1. + np.exp(-v))
        grad = x.T @ (w*(q-y)) + ridge*(beta-target)
        hessian = (x.T * (w*q*(1-q))) @ x + np.eye(2)*(ridge+1e-10)
        try:
            step = np.linalg.solve(hessian, grad)
        except np.linalg.LinAlgError:
            break
        if np.max(np.abs(step)) < 1e-9:
            break
        accepted = False
        for scale in (1., .5, .25, .125, .0625, .03125, .015625, .0078125):
            proposal = beta-scale*step
            proposal[1] = max(0., proposal[1])
            value = objective(proposal)
            if value <= old + 1e-13:
                beta, old, accepted = proposal, value, True
                break
        if not accepted:
            break
    return dict(kind="monotone_logistic", intercept=float(beta[0]), slope=float(beta[1]),
                ridge=float(ridge), half_life_contracts=half_life,
                fit_contracts=len(set(r["ticker"] for r in rows)),
                fit_last_close=max(r["close_time"] for r in rows))


def _loss(rows, probabilities):
    p = np.clip(np.asarray(probabilities), P_CLIP, 1-P_CLIP)
    y = np.array([r["y"] for r in rows])
    w = contract_weights(rows)
    return float(np.sum(w*(-y*np.log(p)-(1-y)*np.log1p(-p))) / w.sum())


def select_calibration(fit_rows, fit_p, validation_rows, validation_p, config):
    identity = dict(kind="identity", intercept=0., slope=1.)
    best, loss = identity, _loss(validation_rows, validation_p)
    candidates = [dict(parameters=identity, validation_log_loss=loss)]
    for half in config.calibration_half_lives:
        for ridge in config.calibration_ridges:
            parameters = fit_probability_map(fit_rows, fit_p, ridge=ridge, half_life=half)
            value = _loss(validation_rows, map_probability(validation_p, parameters))
            candidates.append(dict(parameters=parameters, validation_log_loss=value))
            if value < loss - 1e-12:
                best, loss = parameters, value
    return best, candidates


def _validate_rows(rows, asset):
    seen, labels = set(), {}
    for r in rows:
        if r.get("asset") != asset or not str(r.get("ticker", "")).startswith("KX"+asset+"15M-"):
            raise UniformBenchmarkError("Asset/ticker mismatch in uniform cohort")
        t, close, y = r.get("as_of"), r.get("close_time"), r.get("y")
        if (type(t) not in (int, float) or type(close) not in (int, float)
                or not math.isfinite(t) or not math.isfinite(close)
                or close-t not in HORIZONS or type(y) is not int or y not in (0, 1)):
            raise UniformBenchmarkError("Invalid origin/official label in common cohort")
        key = (r["ticker"], t)
        if key in seen:
            raise UniformBenchmarkError("Duplicate forecast identity")
        seen.add(key)
        label = (close, y)
        if r["ticker"] in labels and labels[r["ticker"]] != label:
            raise UniformBenchmarkError("Conflicting whole-contract labels")
        labels[r["ticker"]] = label
        if r.get("last_history_event", t) > t:
            raise UniformBenchmarkError("A history endpoint follows its origin")
        if not math.isfinite(r.get("g", float("nan"))) or not 0 <= r["g"] <= 1:
            raise UniformBenchmarkError("Missing native Gaussian anchor")


def make_experiment(prepared, *, config=None, prior_cutoffs=None):
    """Freeze a common calendar and common close membership before any model fit."""
    config = UniformBenchmarkConfig() if config is None else config
    if set(prepared) != set(ASSETS):
        raise UniformBenchmarkError("Supply exactly BTC, ETH, SOL and XRP")
    cutoffs = {a: PRIOR_EXAMINED_THROUGH for a in ASSETS}
    if prior_cutoffs:
        cutoffs.update(prior_cutoffs)
    grouped, cover = {}, {}
    for asset in ASSETS:
        rows = prepared[asset]["rows"]
        _validate_rows(rows, asset)
        groups = defaultdict(list)
        for row in rows:
            groups[row["close_time"]].append(row)
        complete = {}
        for close, group in groups.items():
            if (len({r["ticker"] for r in group}) == 1 and len(group) == len(HORIZONS)
                    and {close-r["as_of"] for r in group} == set(HORIZONS)):
                complete[close] = group
        grouped[asset] = complete
        cover[asset] = dict(input_contracts=len(groups), whole_complete_contracts=len(complete),
                            input_origins=len(rows), data_coverage=prepared[asset].get("coverage", {}))
    common = sorted(set.intersection(*(set(grouped[a]) for a in ASSETS)))
    if not common:
        raise UniformBenchmarkError("No common complete official four-asset contract calendar")
    day = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
    days = sorted({day(t) for t in common})
    test_n = max(config.minimum_test_days, math.ceil(.2*len(days)))
    validation_n = max(1, int(.15*len(days)))
    calibration_n = max(2, int(.15*len(days)))
    training_n = len(days)-test_n-validation_n-calibration_n
    parent_n = max(2, int(.35*training_n))
    if training_n-parent_n < 2:
        raise UniformBenchmarkError("Need at least 12 common UTC dates for the exploratory five-part protocol")
    sizes = (parent_n, training_n-parent_n, calibration_n, validation_n, test_n)
    parts = {a: {} for a in ASSETS}
    split_audit, cursor, previous = {}, 0, None
    for stage, size in zip(STAGES, sizes):
        dates = days[cursor:cursor+size]
        cursor += size
        candidates = [t for t in common if day(t) in dates]
        chosen = [t for t in candidates if previous is None or t-max(HORIZONS) >= previous+config.purge_seconds]
        if len(chosen) < config.minimum_stage_contracts:
            raise UniformBenchmarkError("Too few common whole contracts after purge: "+stage)
        for asset in ASSETS:
            parts[asset][stage] = sorted([dict(r) for t in chosen for r in grouped[asset][t]],
                                         key=lambda r: (r["close_time"], r["as_of"]))
        split_audit[stage] = dict(days=dates, contracts_per_asset=len(chosen),
            origins_per_asset=len(chosen)*len(HORIZONS), first_as_of=min(chosen)-max(HORIZONS),
            last_close=max(chosen), purged_contracts_per_asset=len(candidates)-len(chosen),
            close_membership_sha256=fingerprint(chosen))
        previous = max(chosen)
    freshness = {}
    for asset in ASSETS:
        test = parts[asset]["test"]
        eligible = sum(bool(r.get("receipt_time_eligible", r.get("strict_receipt_eligible", r.get("receipts_eligible", False)))) for r in test)
        fresh = min(r["as_of"] for r in test) >= cutoffs[asset]+config.purge_seconds
        freshness[asset] = dict(prior_examined_through=cutoffs[asset],
             test_after_declared_prior_cutoff=fresh,
             receipt_eligible_test_origins=eligible, test_origins=len(test),
             historical_receipt_vintage_verified=eligible == len(test))
    gate_reasons = []
    if config.require_fresh_test and not all(v["test_after_declared_prior_cutoff"] for v in freshness.values()):
        gate_reasons.append("No fresh untouched common test after declared prior exposure")
    if config.strict_receipts:
        for asset in ASSETS:
            flat = [r for stage in STAGES for r in parts[asset][stage]]
            if not all(r.get("receipt_time_eligible", r.get("strict_receipt_eligible", r.get("receipts_eligible", False))) for r in flat):
                gate_reasons.append(asset+": source receipts are later than historical origins")
            for left, right in zip(STAGES, STAGES[1:]):
                boundary = min(r["as_of"] for r in parts[asset][right])
                if any(type(r.get("label_available_at")) not in (int, float)
                        or not math.isfinite(r["label_available_at"])
                        or r["label_available_at"] > boundary for r in parts[asset][left]):
                    gate_reasons.append(asset+": prior-stage official label unavailable at next-stage origin")
    protocol = dict(kind="kalshi_uniform_four_asset_protocol", version=1,
        offline_only=True, execution_eligible=False, config=asdict(config),
        assets=list(ASSETS), horizons_seconds=list(HORIZONS), splits=split_audit,
        common_contract_closes_before_purge=len(common), common_close_days=days,
        source_time_causal_reconstruction=True, coverage=cover, freshness=freshness,
        gate_reasons=gate_reasons,
        parent_anchor_rule="fixed EWMA + L2=.1 monotone map fit parent_fit only",
        parameter_training_rule="model_train only; native future target endpoints must not exceed its end",
        primary_score="contract-balanced calibrated log loss",
        test_selection_forbidden=True, regime_threshold_training="model_train only",
        model_selection="validation only, same maps and candidate registry for all assets",
        limitations=["Common labels span only the intersection of all four assets.",
            "Few validation dates limit hyperparameter and regime selection.",
            "A calendar-date test does not establish untouched outcomes or receipt-vintage availability.",
            "No proper-score result establishes fill quality, net P&L or live readiness."])
    protocol["protocol_sha256"] = fingerprint(protocol)
    return parts, protocol


def attach_parent_anchor(parts):
    fit = parts["parent_fit"]
    parameters = fit_probability_map(fit, [r["g"] for r in fit], ridge=.1)
    for rows in parts.values():
        for row, p in zip(rows, map_probability([r["g"] for r in rows], parameters)):
            row["p0"] = float(p)
    return parameters


def _valid_probabilities(record, parts):
    supplied = record.get("probabilities", {})
    for stage in ("final_calibration", "validation", "test"):
        p = np.asarray(supplied.get(stage, []), dtype=float)
        if p.shape != (len(parts[stage]),) or not np.isfinite(p).all() or np.any((p < 0) | (p > 1)):
            return False
    return True


def evaluate_asset(asset, parts, candidates, protocol, *, config=None):
    """All selection uses validation; returns immutable test forecasts and diagnostics."""
    from .uniform_benchmark_metrics import (fit_regimes, regime_labels, score,
                                            paired_comparisons, supports_with_scores)
    config = UniformBenchmarkConfig() if config is None else config
    models, predictions, raw_predictions, registry = {}, {}, {}, {}
    for name, record in sorted(candidates.items()):
        registry[name] = {k: record.get(k) for k in ("status", "reason", "family", "specification")}
        if record.get("status") != "evaluated":
            continue
        if not _valid_probabilities(record, parts):
            registry[name].update(status="failed_forecast", reason="Incomplete or invalid common-stage probability vector")
            continue
        raw = record["probabilities"]
        parameters, map_grid = select_calibration(parts["final_calibration"], raw["final_calibration"],
                                                   parts["validation"], raw["validation"], config)
        validation_p = map_probability(raw["validation"], parameters).tolist()
        test_p = map_probability(raw["test"], parameters).tolist()
        predictions[name], raw_predictions[name] = test_p, list(raw["test"])
        models[name] = dict(family=record.get("family"), specification=record.get("specification"),
            final_map=parameters, calibration_candidates=map_grid,
            validation=dict(raw=score(parts["validation"], raw["validation"]),
                            calibrated=score(parts["validation"], validation_p)),
            test=dict(raw=score(parts["test"], raw["test"]), calibrated=score(parts["test"], test_p)),
            artifact=record.get("artifact"))
    if not models:
        return dict(asset=asset, status="no_evaluated_models", registry=registry,
                    execution_eligible=False), []
    reference = "ewma_fixed" if "ewma_fixed" in models else None
    if reference is None:
        return dict(asset=asset, status="missing_gaussian_reference", registry=registry,
                    models=models, execution_eligible=False), []
    chosen = min(models, key=lambda n: (models[n]["validation"]["calibrated"]["log_loss"], n))
    families = defaultdict(list)
    for name, item in models.items():
        families[item["family"]].append(name)
    family_chosen = {family: min(names, key=lambda n: (models[n]["validation"]["calibrated"]["log_loss"], n))
                     for family, names in families.items()}
    regime_artifact = fit_regimes(parts["model_train"])
    test_slices = supports_with_scores(parts["test"], predictions, regime_artifact,
        min_contracts=config.minimum_regime_contracts, min_days=config.minimum_regime_days)
    val_preds = {n: map_probability(candidates[n]["probabilities"]["validation"],
                                   models[n]["final_map"]).tolist() for n in models}
    val_slices = supports_with_scores(parts["validation"], val_preds, regime_artifact,
        min_contracts=config.minimum_regime_contracts, min_days=config.minimum_regime_days)
    regime_selection = {}
    for axis, cells in val_slices.get("axes", {}).items():
        regime_selection[axis] = {}
        for label, cell in cells.items():
            if cell.get("status") in ("supported", "evaluated", "ready"):
                winner = min(models, key=lambda n: (cell["scores"][n]["log_loss"], n))
                regime_selection[axis][label] = dict(status="validation_selected_exploratory", candidate=winner)
            else:
                regime_selection[axis][label] = dict(status="insufficient_support", candidate=None,
                                                     reason=cell.get("reason"))
    pair = paired_comparisons(parts["test"], predictions, reference=reference,
                             samples=config.bootstrap_samples, seed=config.seed)
    horizon_scores = {}
    for h in HORIZONS:
        idx = [i for i,r in enumerate(parts["test"]) if r["close_time"]-r["as_of"] == h]
        subset = [parts["test"][i] for i in idx]
        horizon_scores[str(h)] = {n: score(subset, [p[i] for i in idx]) for n,p in predictions.items()}
    exposure = protocol["freshness"][asset]
    reasons = ["Offline research artifact has no live execution adapter",
               "No executable quote, adverse-selection, fill or fee-adjusted P&L validation"]
    if not exposure["test_after_declared_prior_cutoff"]:
        reasons.append("Test outcomes were previously examined")
    if not exposure["historical_receipt_vintage_verified"]:
        reasons.append("Original receipt-time point-in-time eligibility is not established")
    if len(protocol["splits"]["validation"]["days"]) < 3:
        reasons.append("Fewer than three validation UTC dates")
    report = dict(kind="kalshi_uniform_asset_report", version=1, asset=asset,
        status="exploratory_evaluated", offline_only=True, execution_eligible=False,
        deployment_eligible=False, deployment_blockers=reasons, registry=registry,
        primary_common_case=dict(contracts=len(set(r["ticker"] for r in parts["test"])),
                                 origins=len(parts["test"]), candidate_models=len(models)),
        models=models, selection=dict(validation_selected_model=chosen,
            family_selected_models=family_chosen, test_used_for_selection=False,
            per_regime_validation_candidates=regime_selection),
        paired=pair, feature_ablations=selected_feature_ablations(parts, models, predictions, config),
        regimes=dict(artifact=regime_artifact, validation=val_slices, test=test_slices),
        by_horizon=horizon_scores, protocol_sha256=protocol["protocol_sha256"])
    rows = []
    labels = regime_labels(parts["test"], regime_artifact)
    for i,r in enumerate(parts["test"]):
        rows.append(dict(asset=asset, ticker=r["ticker"], as_of=r["as_of"], close_time=r["close_time"],
                         day=r["day"], y=r["y"], regimes=labels[i],
                         probabilities={n: dict(raw=raw_predictions[n][i], calibrated=predictions[n][i])
                                        for n in models}))
    return report, rows


def extra_registry_status():
    return {
        "causal_cnn": dict(status="deferred", family="causal_cnn",
                          reason="Earlier instruction defers CNN until simpler history features justify it"),
        "order_flow": dict(status="missing_input", family="order_flow",
                          reason="No common historical point-in-time order-book/quote feature archive in primary inputs"),
        "legacy_terminal_gbm": dict(status="invalid_target", family="legacy_terminal_gbm",
                          reason="Legacy terminal event is not the official settlement-average event; excluded from primary target"),
        "market_probability": dict(status="missing_input", family="market_probability",
                          reason="Market-implied comparison requires a separate synchronized quote cohort")
    }


def aggregate_asset_reports(asset_reports, prediction_rows, *, config=None):
    """Equal-asset validation choice and synchronized-day uncertainty, without test selection."""
    from .uniform_benchmark_metrics import score, paired_comparisons
    config = UniformBenchmarkConfig() if config is None else config
    if set(asset_reports) != set(ASSETS) or set(prediction_rows) != set(ASSETS):
        raise UniformBenchmarkError("Macro comparison needs all four completed asset reports")
    common = set.intersection(*(set(asset_reports[a].get("models", {})) for a in ASSETS))
    excluded = sorted(set.union(*(set(asset_reports[a].get("registry", {})) for a in ASSETS))-common)
    if "ewma_fixed" not in common:
        return dict(status="missing_common_reference", excluded_candidates=excluded, execution_eligible=False)
    values = {}
    for model in sorted(common):
        values[model] = dict(
            validation_log_loss=float(np.mean([asset_reports[a]["models"][model]["validation"]["calibrated"]["log_loss"]
                                               for a in ASSETS])),
            test_macro_log_loss=float(np.mean([asset_reports[a]["models"][model]["test"]["calibrated"]["log_loss"]
                                               for a in ASSETS])),
            test_macro_brier=float(np.mean([asset_reports[a]["models"][model]["test"]["calibrated"]["brier_score"]
                                           for a in ASSETS])))
    selected = min(common, key=lambda n: (values[n]["validation_log_loss"], n))
    pooled_rows, pooled_predictions = [], {n: [] for n in sorted(common)}
    for a in ASSETS:
        for r in prediction_rows[a]:
            pooled_rows.append({k: r[k] for k in ("asset", "ticker", "as_of", "close_time", "day", "y")})
            for n in common:
                pooled_predictions[n].append(r["probabilities"][n]["calibrated"])
    counts = {a: len({r["ticker"] for r in prediction_rows[a]}) for a in ASSETS}
    equal_counts = len(set(counts.values())) == 1
    if not equal_counts:
        # Never describe a contract-weighted pool as an equal-asset macro estimate.
        return dict(status="unequal_asset_cohorts", scores=values,
            validation_selected_global_candidate=selected, contracts_by_asset=counts,
            pooled_uncertainty=None, execution_eligible=False)
    pooled = paired_comparisons(pooled_rows, pooled_predictions, reference="ewma_fixed",
                               samples=config.bootstrap_samples, seed=config.seed)
    return dict(status="exploratory_evaluated", weighting="equal asset mean",
        common_candidates=sorted(common), excluded_candidates=excluded, scores=values,
        validation_selected_global_candidate=selected, test_used_for_selection=False,
        contracts_by_asset=counts, pooled_uncertainty=pooled,
        day_resampling="synchronized UTC close-day blocks across all assets",
        deployment_eligible=False, execution_eligible=False)


def joint_asset_comparisons(asset_reports, prediction_rows, *, config=None):
    """Simultaneous asset-by-candidate-by-metric contrasts with synchronized day draws."""
    from .uniform_benchmark_metrics import paired_comparisons
    config = UniformBenchmarkConfig() if config is None else config
    if set(asset_reports) != set(ASSETS) or set(prediction_rows) != set(ASSETS):
        raise UniformBenchmarkError("Joint comparison needs all four asset reports")
    common = set.intersection(*(set(asset_reports[a].get("models", {})) for a in ASSETS))
    if "ewma_fixed" not in common:
        return dict(status="missing_common_reference", execution_eligible=False)
    pooled, baseline, bounds = [], [], {}
    counts = {}
    for asset in ASSETS:
        start = len(pooled)
        pooled.extend(prediction_rows[asset])
        bounds[asset] = (start, len(pooled))
        counts[asset] = len({r["ticker"] for r in prediction_rows[asset]})
        baseline.extend(r["probabilities"]["ewma_fixed"]["calibrated"] for r in prediction_rows[asset])
    contrasts = {"reference": baseline}
    for asset in ASSETS:
        left, right = bounds[asset]
        for name in sorted(common):
            if name == "ewma_fixed":
                continue
            values = list(baseline)
            values[left:right] = [r["probabilities"][name]["calibrated"] for r in prediction_rows[asset]]
            contrasts[asset+"::"+name] = values
    result = paired_comparisons(pooled, contrasts, reference="reference",
                               samples=config.bootstrap_samples, seed=config.seed)
    total = sum(counts.values())
    result.update(
        asset_contrast_scales={a: total/counts[a] for a in ASSETS},
        family_scope="all assets, common candidates and both proper scores",
        interpretation="A contrast changes one asset only. Multiply its pooled score difference and CI by the asset_contrast_scale to recover that asset difference; adjusted p-values are unchanged.",
        synchronized_day_resampling=True, deployment_eligible=False, execution_eligible=False)
    return result


def selected_feature_ablations(parts, models, predictions, config):
    """Select each feature-family configuration on validation, then pair its test scores."""
    from .uniform_benchmark_metrics import paired_comparisons
    selected = {}
    for name in ("ridge_state", "ridge_history", "boosted_state", "boosted_history"):
        candidates = [n for n in models if n.startswith(name+"_")]
        if candidates:
            selected[name] = min(candidates, key=lambda n: (models[n]["validation"]["calibrated"]["log_loss"], n))
    result = {}
    for family in ("ridge", "boosted"):
        left, right = selected.get(family+"_state"), selected.get(family+"_history")
        if left is None or right is None:
            result[family] = dict(status="missing_feature_family")
            continue
        names = {left, right}
        if "parent_calibrated_ewma" in predictions:
            names.add("parent_calibrated_ewma")
        result[family] = dict(selected_state=left, selected_history=right,
            paired=paired_comparisons(parts["test"], {n: predictions[n] for n in names},
                                     reference=left, samples=config.bootstrap_samples, seed=config.seed))
    return result
