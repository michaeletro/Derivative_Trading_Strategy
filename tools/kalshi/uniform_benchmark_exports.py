"""CSV exports and independent score verification for the offline benchmark.

No model fitting, calibration, ranking or venue interaction occurs here.
Prediction CSVs are read back and independently rescored with stdlib math,
without importing the benchmark's NumPy score implementation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
FORMS = ('raw', 'calibrated')
EPSILON = 1e-12
TOLERANCE = 1e-12


class UniformExportError(ValueError):
    """Exports disagree with their declared identities, coverage or scores."""


def _number(value, label, *, low=-1e20, high=1e20):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise UniformExportError('Invalid finite ' + label)
    return float(value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _validate_rows(asset, rows, names):
    if not isinstance(rows, (list, tuple)):
        raise UniformExportError('Predictions must be a sequence of immutable origins')
    seen, identities = set(), {}
    for row in rows:
        if not isinstance(row, dict) or row.get('asset') != asset:
            raise UniformExportError('Prediction asset differs from report asset')
        ticker = row.get('ticker')
        if not isinstance(ticker, str) or not ticker.startswith('KX'+asset+'15M-'):
            raise UniformExportError('Invalid asset-bound contract ticker')
        stamp = _number(row.get('as_of'), 'origin time', low=0, high=253402300799)
        close = _number(row.get('close_time'), 'close time', low=0, high=253402300799)
        if stamp >= close:
            raise UniformExportError('Origin must precede contract close')
        day = datetime.fromtimestamp(close, timezone.utc).date().isoformat()
        if row.get('day') != day:
            raise UniformExportError('Prediction UTC day disagrees with its close')
        y = row.get('y')
        if type(y) is not int or y not in (0, 1):
            raise UniformExportError('Each origin needs an official binary label')
        if (ticker, stamp) in seen:
            raise UniformExportError('Duplicate prediction contract/origin identity')
        seen.add((ticker, stamp))
        identity = (close, day, y)
        if ticker in identities and identities[ticker] != identity:
            raise UniformExportError('A contract has contradictory labels or clocks')
        identities[ticker] = identity
        probabilities = row.get('probabilities')
        if not isinstance(probabilities, dict) or set(probabilities) != set(names):
            raise UniformExportError('Every evaluated model must cover the exact common origin set')
        for name in names:
            pair = probabilities[name]
            if not isinstance(pair, dict) or set(pair) != set(FORMS):
                raise UniformExportError('Every model needs both raw and calibrated probabilities')
            for form in FORMS:
                _number(pair[form], 'probability', low=0, high=1)
        regimes = row.get('regimes', {})
        if not isinstance(regimes, dict) or any(not isinstance(k, str) or
                not isinstance(v, str) for k, v in regimes.items()):
            raise UniformExportError('Causal regime labels must be explicit strings')
    return identities


def _rescore(rows, name, form):
    """Independent equal-contract score; Brier uses the original probability."""
    grouped = defaultdict(list)
    for row in rows:
        p = float(row['probabilities'][name][form])
        bounded = max(EPSILON, min(1-EPSILON, p))
        y = row['y']
        loss = -y*math.log(bounded)-(1-y)*math.log1p(-bounded)
        grouped[row['ticker']].append((loss, (y-p)**2))
    contracts = len(grouped)
    if not contracts:
        return dict(count_contracts=0, count_forecasts=0, count_days=0,
                    log_loss=None, brier_score=None)
    means = [tuple(math.fsum(x[j] for x in values)/len(values)
                   for j in (0, 1)) for values in grouped.values()]
    return dict(count_contracts=contracts, count_forecasts=len(rows),
        count_days=len({row['day'] for row in rows}),
        log_loss=math.fsum(x[0] for x in means)/contracts,
        brier_score=math.fsum(x[1] for x in means)/contracts)


def _check_score(expected, actual, context):
    if not isinstance(expected, dict):
        raise UniformExportError('Missing central score: ' + context)
    errors = []
    for key in ('count_contracts', 'count_forecasts', 'count_days'):
        if expected.get(key) != actual[key]:
            raise UniformExportError('Coverage mismatch in '+context+': '+key)
    for key in ('log_loss', 'brier_score'):
        value = expected.get(key)
        if actual[key] is None:
            if value is not None:
                raise UniformExportError('Empty score mismatch in '+context)
            continue
        _number(value, context+' '+key)
        difference = abs(float(value)-actual[key])
        if difference >= TOLERANCE:
            raise UniformExportError('Independent score mismatch in '+context+': '+key)
        errors.append(difference)
    return max(errors, default=0.)


def _score_columns(score):
    return {key:score.get(key) for key in
        ('count_contracts', 'count_forecasts', 'count_days', 'log_loss', 'brier_score', 'ece')}


def _write_csv(path, fields, records):
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(records)


def _prediction_table(rows, names):
    axes = sorted({axis for row in rows for axis in row.get('regimes', {})})
    fields = ['asset','ticker','as_of','close_time','day','y','horizon_seconds']
    fields += ['regime_'+axis for axis in axes]
    fields += [name+'__'+form for name in names for form in FORMS]
    records = []
    for row in rows:
        flat = {key:row[key] for key in ('asset','ticker','as_of','close_time','day','y')}
        flat['horizon_seconds'] = row['close_time']-row['as_of']
        flat.update({'regime_'+axis:row.get('regimes', {}).get(axis, '') for axis in axes})
        flat.update({name+'__'+form:row['probabilities'][name][form]
                     for name in names for form in FORMS})
        records.append(flat)
    return fields, records, axes


def _read_predictions(path, asset, names, axes):
    result = []
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise UniformExportError('Prediction CSV header is missing or duplicated')
        for record in reader:
            try:
                row = dict(asset=record['asset'], ticker=record['ticker'],
                    as_of=float(record['as_of']), close_time=float(record['close_time']),
                    day=record['day'], y=int(record['y']),
                    probabilities={name:{form:float(record[name+'__'+form]) for form in FORMS}
                                   for name in names},
                    regimes={axis:record['regime_'+axis] for axis in axes
                             if record['regime_'+axis] != ''})
                if float(record['horizon_seconds']) != row['close_time']-row['as_of']:
                    raise UniformExportError('Prediction CSV horizon changed')
                result.append(row)
            except (KeyError, TypeError, ValueError) as error:
                raise UniformExportError('Prediction CSV lost a required identity or probability') from error
    _validate_rows(asset, result, names)
    return result


def _same_predictions(original, restored, names):
    if len(original) != len(restored):
        raise UniformExportError('Prediction CSV changed origin coverage')
    for left, right in zip(original, restored):
        if any(left[key] != right[key] for key in ('asset','ticker','as_of','close_time','day','y')):
            raise UniformExportError('Prediction CSV changed an official identity or label')
        if left.get('regimes', {}) != right.get('regimes', {}):
            raise UniformExportError('Prediction CSV changed causal regime labels')
        if any(left['probabilities'][name][form] != right['probabilities'][name][form]
               for name in names for form in FORMS):
            raise UniformExportError('Prediction CSV changed a probability')


def _verify_scores(own, rows, names):
    error = 0.
    for name in names:
        for form in FORMS:
            error = max(error, _check_score(own['models'][name]['test'][form],
                _rescore(rows, name, form), name+'/'+form))
    for horizon, models in own.get('by_horizon', {}).items():
        try:
            numeric = float(horizon)
        except (ValueError, TypeError) as invalid:
            raise UniformExportError('Invalid report horizon') from invalid
        subset = [row for row in rows if row['close_time']-row['as_of'] == numeric]
        if set(models) != set(names):
            raise UniformExportError('Horizon model coverage differs from common cohort')
        for name, score in models.items():
            error = max(error, _check_score(score, _rescore(subset, name, 'calibrated'),
                                            'horizon/'+str(horizon)+'/'+name))
    for stage, regime in own.get('regimes', {}).items():
        if stage != 'test':
            continue
        for axis, cells in regime.get('axes', {}).items():
            for label, cell in cells.items():
                subset = [row for row in rows if row.get('regimes', {}).get(axis) == label]
                expected_counts = dict(count_contracts=len({row['ticker'] for row in subset}),
                    count_forecasts=len(subset), count_days=len({row['day'] for row in subset}))
                if any(cell.get(key) != value for key,value in expected_counts.items()):
                    raise UniformExportError('Regime coverage differs from common cohort')
                if set(cell.get('scores', {})) != set(names):
                    raise UniformExportError('Regime model coverage differs from common cohort')
                for name, score in cell['scores'].items():
                    error = max(error, _check_score(score, _rescore(subset, name, 'calibrated'),
                        'regime/'+axis+'/'+label+'/'+name))
    return error


def export_tables(output, report, prediction_rows):
    """Write reusable tables, reread predictions and independently verify scores.

    Existing export paths are refused before any write. Report/model JSON files
    may already exist in output. No candidate is chosen or refitted here.
    """
    output = Path(output)
    assets = report.get('assets')
    if not isinstance(assets, dict) or not isinstance(prediction_rows, dict):
        raise UniformExportError('Supply asset reports and matching prediction rows')
    if set(assets) != set(prediction_rows) or not set(assets) <= set(ASSETS):
        raise UniformExportError('Report and prediction asset coverage differ')
    global_fields = dict(
        scores=['asset','stage','model','family','form','status','count_contracts',
                'count_forecasts','count_days','log_loss','brier_score','ece'],
        registry=['asset','model','family','status','reason','specification'],
        reliability=['asset','horizon_seconds','model','form','lower','upper',
                     'upper_inclusive','count_forecasts','count_contracts','weight',
                     'mean_probability','observed_frequency'],
        regimes=['asset','stage','axis','regime','support_status','reason','model','family',
                 'count_contracts','count_forecasts','count_days','log_loss','brier_score','ece'])
    global_names = dict(scores='scores.csv', registry='registry.csv',
        reliability='reliability-by-horizon.csv', regimes='regime-scores.csv')
    tables = {key:[] for key in global_fields}
    prediction_tables, maximum_error = {}, 0.
    for asset in sorted(assets):
        own, rows = assets[asset], prediction_rows[asset]
        models = own.get('models', {})
        if not isinstance(models, dict) or any(not isinstance(name, str) or
                not name or '\n' in name or '\r' in name for name in models):
            raise UniformExportError('Models need explicit stable identities')
        names = sorted(models)
        _validate_rows(asset, rows, names)
        if names and not rows:
            raise UniformExportError('Evaluated models need a nonempty common test cohort')
        common = own.get('primary_common_case')
        if common is not None:
            actual = dict(contracts=len({row['ticker'] for row in rows}),
                          origins=len(rows), candidate_models=len(names))
            if any(common.get(key) != value for key,value in actual.items()):
                raise UniformExportError('Primary common-case coverage disagrees with predictions')
        maximum_error = max(maximum_error, _verify_scores(own, rows, names))
        prediction_tables[asset] = (names, *_prediction_table(rows, names))
        registry = own.get('registry', {})
        if not isinstance(registry, dict):
            raise UniformExportError('Every asset requires explicit registry statuses')
        if set(names)-set(registry):
            raise UniformExportError('An evaluated model is missing from the registry')
        for name, item in sorted(registry.items()):
            tables['registry'].append(dict(asset=asset, model=name, family=item.get('family'),
                status=item.get('status'), reason=item.get('reason'),
                specification=_json(item.get('specification'))))
        for name, model in sorted(models.items()):
            for stage in ('validation', 'test'):
                for form in FORMS:
                    cell = model.get(stage, {}).get(form)
                    if not isinstance(cell, dict):
                        raise UniformExportError('Model is missing a uniform stage score')
                    tables['scores'].append(dict(asset=asset, stage=stage, model=name,
                        family=model.get('family'), form=form, status=cell.get('status'),
                        **_score_columns(cell)))
        for horizon, cells in sorted(own.get('by_horizon', {}).items(), key=lambda pair:float(pair[0])):
            for name, cell in sorted(cells.items()):
                for bin_cell in cell.get('reliability', []):
                    record = {key:bin_cell.get(key) for key in global_fields['reliability']
                              if key not in ('asset','horizon_seconds','model','form')}
                    tables['reliability'].append(dict(asset=asset, horizon_seconds=horizon,
                        model=name, form='calibrated', **record))
        for stage in ('validation','test'):
            for axis, cells in sorted(own.get('regimes', {}).get(stage, {}).get('axes', {}).items()):
                for label, cell in sorted(cells.items()):
                    for name, score in sorted(cell.get('scores', {}).items()):
                        if name not in models:
                            raise UniformExportError('A regime refers to a missing model')
                        tables['regimes'].append(dict(asset=asset, stage=stage, axis=axis,
                            regime=label, support_status=cell.get('status'), reason=cell.get('reason'),
                            model=name, family=models[name].get('family'), **_score_columns(score)))
    paths = [output/name for name in global_names.values()]
    paths += [output/asset/'test-predictions.csv' for asset in sorted(assets)]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise UniformExportError('Refusing to overwrite an existing benchmark export')
    if output.exists() and not output.is_dir():
        raise UniformExportError('Export output must be a directory')
    if any((output/asset).exists() and not (output/asset).is_dir() for asset in assets):
        raise UniformExportError('Asset export destination must be a directory')
    created = []
    try:
        output.mkdir(parents=True, exist_ok=True)
        for key, filename in global_names.items():
            path = output/filename
            _write_csv(path, global_fields[key], tables[key])
            created.append(path)
        summaries = {}
        for asset, (names, fields, flat, axes) in prediction_tables.items():
            own_dir = output/asset
            own_dir.mkdir(exist_ok=True)
            path = own_dir/'test-predictions.csv'
            _write_csv(path, fields, flat)
            created.append(path)
            restored = _read_predictions(path, asset, names, axes)
            _same_predictions(prediction_rows[asset], restored, names)
            maximum_error = max(maximum_error, _verify_scores(assets[asset], restored, names))
            summaries[asset] = dict(count_contracts=len({row['ticker'] for row in restored}),
                count_forecasts=len(restored), count_days=len({row['day'] for row in restored}),
                count_models=len(names), independent_score_checks=len(names)*len(FORMS),
                independent_test_scores={name:{form:_rescore(restored,name,form) for form in FORMS}
                                         for name in names})
        files = []
        for path in created:
            encoded = path.read_bytes()
            files.append(dict(path=path.relative_to(output).as_posix(), bytes=len(encoded),
                sha256=hashlib.sha256(encoded).hexdigest()))
        return dict(kind='kalshi_uniform_csv_verification', version=1,
            offline_only=True, execution_eligible=False, verified=True,
            method='CSV reread; independent stdlib equal-contract mean losses using math.fsum',
            probability_clip_log_loss=EPSILON, brier_probabilities='unclipped',
            absolute_score_tolerance=TOLERANCE, maximum_absolute_score_error=maximum_error,
            assets=summaries, files=files, table_rows={key:len(value) for key,value in tables.items()},
            refits_performed=False, candidate_selection_performed=False)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
