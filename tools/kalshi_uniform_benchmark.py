#!/usr/bin/env python3
"""Run the frozen offline BTC/ETH/SOL/XRP model benchmark; no venue or execution mode."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-manifest", type=Path, required=True,
                   help="JSON with explicit frozen dataset and history_archive paths for all four assets")
    p.add_argument("--output-dir", type=Path, required=True, help="New private output directory outside checkout")
    p.add_argument("--paths", type=int, default=4096)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--bootstrap-samples", type=int, default=500)
    p.add_argument("--jobs", type=int, choices=(1, 2, 4), default=1,
                   help="Independent asset workers; shared protocol and deterministic asset-local seeds")
    p.add_argument("--require-fresh-test", action="store_true")
    p.add_argument("--strict-receipts", action="store_true")
    return p


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    if hasattr(value, "item"):
        return json_safe(value.item())
    return value


def write_json(path, value):
    temporary = path.with_name(path.name+".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(json_safe(value), stream, allow_nan=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def progress(phase, **details):
    print(json.dumps(dict(phase=phase, **json_safe(details)), allow_nan=False), flush=True)


def read_json(path, limit=256_000_000):
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Missing or oversized frozen input: "+str(path))
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def configuration(args):
    from kalshi.uniform_benchmark import ASSETS
    manifest = read_json(args.input_manifest, limit=1_000_000)
    if manifest.get("kind") != "kalshi_uniform_benchmark_inputs" or manifest.get("version") != 1:
        raise ValueError("Use a version-one kalshi_uniform_benchmark_inputs manifest")
    if set(manifest.get("assets", {})) != set(ASSETS):
        raise ValueError("Manifest must bind exactly BTC, ETH, SOL and XRP")
    root = Path(__file__).resolve().parents[1]
    out = args.output_dir.resolve()
    if out == root or root in out.parents:
        raise ValueError("Research outputs must be private and outside the checkout")
    if out.exists():
        raise ValueError("Choose a new nonexistent benchmark output directory")
    for asset, item in manifest["assets"].items():
        for key in ("dataset", "history_archive"):
            path = Path(item[key]).resolve()
            if not path.is_file() or path == out or out in path.parents:
                raise ValueError("Each frozen source must exist outside output: "+asset+" "+key)
            item[key] = str(path)
    return manifest, out


def _run_asset(asset, dataset, parts, protocol, inputs, datasets, config, output):
    from kalshi.uniform_benchmark import attach_parent_anchor, evaluate_asset, extra_registry_status
    from kalshi.uniform_native_models import fit_models as native_fit
    from kalshi.uniform_classifier_models import fit_models as classifier_fit
    from kalshi.uniform_extra_models import fit_models as extra_fit
    started = time.monotonic()
    own = output/asset
    own.mkdir()
    callback = lambda phase, **kw: progress(phase, **{**kw, "asset": asset})
    parent = attach_parent_anchor(parts)
    write_json(own/"parent-map.json", parent)
    candidates = {"parent_calibrated_ewma": dict(status="evaluated", reason=None,
        family="gaussian_parent_control", specification={"anchor": "train_internal_parent_map"},
        artifact={"kind": "uniform_parent_ewma_control", "parameters": parent, "offline_only": True,
                  "execution_eligible": False}, probabilities={stage: [r["p0"] for r in parts[stage]]
                     for stage in ("final_calibration", "validation", "test")})}
    for family, call in (
        ("native", lambda: native_fit(dataset, parts, paths=config.paths, seed=config.seed,
                                      archive=Path(inputs["history_archive"]), progress=callback)),
        ("classifiers", lambda: classifier_fit(parts, seed=config.seed, progress=callback)),
        ("extra", lambda: extra_fit(dataset, parts, paths=config.paths, seed=config.seed,
                                    archive=Path(inputs["history_archive"]), all_datasets=datasets,
                                    progress=callback)),
    ):
        progress("fit_family", asset=asset, family=family)
        records = call()
        collisions = set(records).intersection(candidates)
        if collisions:
            raise ValueError("Candidate identity collision: "+str(sorted(collisions)))
        candidates.update(records)
        write_json(own/(family+"-candidate-checkpoint.json"), records)
        progress("family_complete", asset=asset, family=family,
                 statuses=dict(__import__("collections").Counter(r["status"] for r in records.values())))
    candidates.update(extra_registry_status())
    write_json(own/"raw-candidates.json", candidates)
    progress("uniform_calibration_and_scoring", asset=asset, candidate_count=len(candidates))
    report, predicted = evaluate_asset(asset, parts, candidates, protocol, config=config)
    # Select numerical audit origins by clocks only, never labels or model losses.
    from kalshi.uniform_native_models import numerical_audit as native_audit
    from kalshi.uniform_extra_models import numerical_audit as extra_audit
    from kalshi.uniform_benchmark import map_probability
    from kalshi.uniform_benchmark_metrics import score
    first_close = {}
    for row in parts["test"]:
        first_close.setdefault(row["day"], row["close_time"])
    audit_indices = [i for i,r in enumerate(parts["test"]) if r["close_time"] == first_close[r["day"]]]
    audit_rows = [parts["test"][i] for i in audit_indices]
    audit_seed = (config.seed+1001) % (2**32)
    progress("frozen_numerical_audit", asset=asset, origins=len(audit_rows), seed=audit_seed)
    numerical = native_audit(dataset, audit_rows, candidates, paths=config.paths,
                             seed=audit_seed, archive=Path(inputs["history_archive"]))
    numerical.update(extra_audit(dataset, audit_rows, candidates, paths=config.paths,
                                seed=audit_seed, all_datasets=datasets))
    for name, item in numerical.items():
        if item.get("status") == "evaluated" and name in report.get("models", {}):
            frozen_map = report["models"][name]["final_map"]
            alt = map_probability(item["probabilities"], frozen_map).tolist()
            primary = [predicted[i]["probabilities"][name]["calibrated"] for i in audit_indices]
            item["frozen_map_scores"] = score(audit_rows, alt)
            item["main_bank_same_subset_scores"] = score(audit_rows, primary)
            item["max_absolute_probability_change"] = float(max(abs(a-b) for a,b in zip(alt,primary)))
    report["numerical_stability"] = dict(selection="earliest common close per UTC test day, every horizon",
        origins=len(audit_rows), paths=config.paths, seed=audit_seed, refits=False,
        origin_membership=[{"ticker":r["ticker"],"as_of":r["as_of"]} for r in audit_rows], models=numerical)
    report.update(runtime_seconds=time.monotonic()-started, parent_map=parent)
    write_json(own/"report.json", report)
    write_json(own/"test-predictions.json", dict(kind="kalshi_uniform_test_predictions", version=1,
               execution_eligible=False, asset=asset, rows=predicted))
    # Preserve pre-test memberships for independent replay/selection audits.
    write_json(own/"cohort-membership.json", {
        stage: [dict(ticker=r["ticker"], as_of=r["as_of"], close_time=r["close_time"], y=r["y"],
                     day=r["day"]) for r in rows] for stage, rows in parts.items()})
    progress("asset_complete", asset=asset, status=report["status"],
             evaluated=len(report.get("models", {})), seconds=report["runtime_seconds"])
    return report


def render_markdown(report):
    lines = ["# Uniform four-asset model benchmark", "",
             "Offline exploratory research. No orders or live configuration changes.", "",
             "## Frozen calendar", "",
             "| Stage | UTC close dates | Contracts per asset | Origins per asset |",
             "| --- | --- | ---: | ---: |"]
    for name, cell in report["protocol"]["splits"].items():
        lines.append("| "+name+" | "+cell["days"][0]+" to "+cell["days"][-1]+" | "+
                     str(cell["contracts_per_asset"])+" | "+str(cell["origins_per_asset"])+" |")
    lines += ["", "## Validation-selected candidates", "",
              "Test outcomes do not choose the model or its probability map.", "",
              "| Asset | Candidate | Test log loss | Test Brier | Deployment |",
              "| --- | --- | ---: | ---: | --- |"]
    for asset, own in report["assets"].items():
        candidate = own.get("selection", {}).get("validation_selected_model")
        scores = own.get("models", {}).get(candidate, {}).get("test", {}).get("calibrated", {})
        lines.append("| "+asset+" | "+str(candidate)+" | "+str(scores.get("log_loss", "unavailable"))+
                     " | "+str(scores.get("brier_score", "unavailable"))+" | blocked |")
    lines += ["", "## Interpretation", "",
        "- Same official event, horizons, common close membership and calendar stages for all assets.",
        "- Parent Gaussian map, model fits, final calibration and selection have disjoint stage ownership.",
        "- All candidate failures and missing inputs are retained in the registry.",
        "- Regime labels use origin-time information and training-only volatility thresholds.",
        "- Regime scores are descriptive; sparse validation slices do not nominate a regime winner.",
        "- Paired day blocks preserve repeated origins; simultaneous intervals cover candidate comparisons.",
        "- Test exposure and receipt-time eligibility are explicit. Current reused/retrospective data cannot qualify live deployment.",
        "- Probability scores alone do not measure fees, fills, adverse selection, P&L or Sharpe.", "",
        "Detailed candidate configurations, calibration choices, uncertainty and per-regime coverage are in each asset report."]
    return "\n".join(lines)+"\n"


def implementation_seal():
    """Hash the registry and local numerical dependency sources before a future run."""
    import re
    from kalshi.uniform_native_models import MODEL_FAMILIES
    from kalshi.uniform_classifier_models import candidate_registry
    tools = Path(__file__).resolve().parent
    names = ["kalshi_uniform_benchmark.py", "kalshi/uniform_benchmark.py",
             "kalshi/uniform_benchmark_data.py", "kalshi/uniform_benchmark_metrics.py",
             "kalshi/uniform_native_models.py", "kalshi/uniform_classifier_models.py",
             "kalshi/uniform_extra_models.py", "kalshi/uniform_benchmark_exports.py"]
    seen, hashes = set(), {}
    while names:
        name = names.pop()
        if name in seen:
            continue
        seen.add(name)
        path = tools/name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        hashes["tools/"+name] = hashlib.sha256(raw).hexdigest()
        if name.startswith("kalshi/"):
            import ast
            for node in ast.walk(ast.parse(raw.decode("utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                    names.append("kalshi/"+node.module.split(".")[0]+".py")
    return dict(source_sha256=hashes,
        native_registry=dict(MODEL_FAMILIES), classifier_registry=candidate_registry(),
        extra_registry=["gjr_garch_t","garch_fhs","cross_asset_ridge","regime_mixture"],
        parent_control="parent_calibrated_ewma",
        excluded_registry=["causal_cnn","order_flow","legacy_terminal_gbm","market_probability"])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from kalshi.uniform_benchmark import (ASSETS, UniformBenchmarkConfig, make_experiment,
                                               fingerprint, PRIOR_EXAMINED_THROUGH)
        from kalshi.uniform_benchmark_data import prepare_asset
        config = UniformBenchmarkConfig(paths=args.paths, seed=args.seed,
            bootstrap_samples=args.bootstrap_samples, require_fresh_test=args.require_fresh_test,
            strict_receipts=args.strict_receipts)
        manifest, out = configuration(args)
        out.mkdir(parents=True, mode=0o700)
        write_json(out/"input-manifest.json", manifest)
        # This lock is written before source extraction, fitting or evaluation.
        lock = dict(kind="kalshi_uniform_benchmark_lock", version=1,
            created_at=datetime.now(timezone.utc).isoformat(), config=config.__dict__,
            assets=list(ASSETS), offline_only=True, execution_eligible=False,
            numerical_audit={"selection": "earliest common close per UTC test date",
                             "paths": config.paths, "seed": (config.seed+1001) % (2**32), "refits": False},
            note="Retrospective test exposure is disclosed; this is not a pristine preregistration.")
        lock["implementation"] = implementation_seal()
        write_json(out/"experiment-lock.json", lock)
        datasets, prepared, provenance = {}, {}, {}
        for asset in ASSETS:
            item = manifest["assets"][asset]
            path, archive = Path(item["dataset"]), Path(item["history_archive"])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if item.get("dataset_sha256") not in (None, digest):
                raise ValueError("Frozen dataset hash mismatch: "+asset)
            progress("load_and_verify_source", asset=asset, bytes=path.stat().st_size)
            data = read_json(path)
            rows, coverage, audit = prepare_asset(data, archive,
                progress=lambda phase, _asset=asset, **kw: progress(phase, **{**kw, "asset": _asset}))
            datasets[asset] = data
            prepared[asset] = dict(rows=rows, coverage=coverage, official_audit=audit)
            provenance[asset] = dict(dataset_sha256=digest, coverage=coverage, official_audit=audit)
            write_json(out/(asset+"-source-audit.json"), provenance[asset])
        cutoffs = {a: manifest["assets"][a].get("prior_examined_through", PRIOR_EXAMINED_THROUGH) for a in ASSETS}
        parts, protocol = make_experiment(prepared, config=config, prior_cutoffs=cutoffs)
        protocol["input_provenance"] = provenance
        # Compute a second seal including actual source audit evidence.
        protocol["source_bound_protocol_sha256"] = fingerprint(protocol)
        write_json(out/"protocol.json", protocol)
        if protocol["gate_reasons"]:
            write_json(out/"summary.json", dict(status="availability_gate_refused",
                execution_eligible=False, reasons=protocol["gate_reasons"], protocol=protocol))
            progress("availability_gate_refused", reasons=protocol["gate_reasons"])
            return 2
        reports = {}
        if args.jobs == 1:
            for asset in ASSETS:
                reports[asset] = _run_asset(asset, datasets[asset], parts[asset], protocol,
                                           manifest["assets"][asset], datasets, config, out)
        else:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                pending = {pool.submit(_run_asset, a, datasets[a], parts[a], protocol,
                           manifest["assets"][a], datasets, config, out): a for a in ASSETS}
                for future in as_completed(pending):
                    reports[pending[future]] = future.result()
        report = dict(kind="kalshi_uniform_four_asset_report", version=1,
            status="exploratory_evaluated", offline_only=True, execution_eligible=False,
            deployment_eligible=False, venue_requests=0, venue_orders_submitted=0,
            live_runner_changed=False, checkout_switched=False,
            protocol=protocol, assets={a: reports[a] for a in ASSETS})
        from kalshi.uniform_benchmark import aggregate_asset_reports, joint_asset_comparisons
        prediction_rows = {a: read_json(out/a/"test-predictions.json")["rows"] for a in ASSETS}
        report["equal_asset_comparison"] = aggregate_asset_reports(reports, prediction_rows, config=config)
        report["simultaneous_all_asset_comparisons"] = joint_asset_comparisons(reports, prediction_rows, config=config)
        write_json(out/"report.json", report)
        from kalshi.uniform_benchmark_exports import export_tables
        verification = export_tables(out, report, prediction_rows)
        write_json(out/"verification.json", verification)
        (out/"REPORT.md").write_text(render_markdown(report), encoding="utf-8")
        summary = dict(kind="kalshi_uniform_four_asset_summary", version=1,
            status=report["status"], execution_eligible=False, deployment_eligible=False,
            protocol_sha256=protocol["protocol_sha256"], splits=protocol["splits"],
            assets={a: dict(status=reports[a]["status"], evaluated=len(reports[a].get("models", {})),
                selection=reports[a].get("selection", {}),
                scores={n: {k: m["test"]["calibrated"][k] for k in ("log_loss", "brier_score")}
                        for n,m in reports[a].get("models", {}).items()},
                registry=reports[a]["registry"]) for a in ASSETS})
        write_json(out/"summary.json", summary)
        progress("benchmark_complete", output_dir=str(out), status=report["status"])
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, ImportError) as error:
        print("Uniform offline benchmark: "+str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
