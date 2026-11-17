#!/usr/bin/env python3
"""Prepare and fit independently bound crypto models without starting trading.

Native historical inputs are opened read-only. Explicit public outcome acquisition
writes raw responses and, after success, enriches only a fresh SQLite backup in
the new output directory. Missing data never creates another asset's fallback.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiError
from kalshi.crypto_assets import asset_spec
from kalshi.forecast import ForecastConfig, _timestamp
from kalshi.history import HistoryArchive
from kalshi.history_features import FeatureError, _SecondGrid, _contracts, _dataset


ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
MAX_DOCUMENT_BYTES = 64_000_000


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--archive', action='append', required=True, metavar='ASSET=PATH')
    result.add_argument('--asset', action='append', choices=ASSETS)
    result.add_argument('--output-dir', type=Path, required=True,
                        help='A new nonexistent output directory outside Git checkouts')
    result.add_argument('--acquire-missing-labels', action='store_true',
                        help='Explicit public market GETs for missing official outcomes; enrich fresh backups only')
    result.add_argument('--start', help='Optional inclusive UTC hour for public labels; requires --end')
    result.add_argument('--end', help='Optional exclusive UTC hour for public labels; requires --start')
    result.add_argument('--timeout', type=float, default=3)
    result.add_argument('--max-pages', type=int, default=100)
    result.add_argument('--readiness-only', action='store_true', help='Inspect/prepare data without fitting or exporting models')
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--recency-weighting', choices=('auto', 'exponential'), default='auto',
                        help='Exponential requires a positive contract-recency half-life; uniform remains a diagnostic control')
    result.add_argument('--recency-half-life-contracts', action='append', type=int,
                        help='Candidate half-life in 15-minute intervals; repeat for validation selection')
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _configuration(args):
    if (not math.isfinite(args.timeout) or not 0 < args.timeout <= 10
            or not 1 <= args.max_pages <= 1000 or not 100 <= args.bootstrap_samples <= 2000
            or not 0 <= args.seed <= 2**32-1):
        raise CLIError('Use bounded public-read timeouts/pages and supported calibration bootstrap settings')
    if (args.start is None) != (args.end is None) or args.start is not None and not args.acquire_missing_labels:
        raise CLIError('Explicit label dates require both --start/--end and --acquire-missing-labels')
    requested_recencies = args.recency_half_life_contracts
    if requested_recencies is not None and (len(requested_recencies) > 5
            or len(set(requested_recencies)) != len(requested_recencies)
            or any(not 1 <= value <= 1000000 for value in requested_recencies)):
        raise CLIError('Choose distinct positive whole 15-minute recency half-lives, at most five')
    recencies = ((None, *requested_recencies) if requested_recencies is not None else
                 (None, 96, 288, 672) if args.recency_weighting == 'exponential' else (None, 96, 384))
    if args.start is not None:
        lower, upper = _timestamp(args.start), _timestamp(args.end)
        if lower % 3600 or upper % 3600 or not 0 < upper-lower <= 31*86400:
            raise CLIError('Public label dates must be UTC hour boundaries within a positive 31-day interval')
    bindings = {}
    for supplied in args.archive:
        asset, separator, path = supplied.partition('=')
        if not separator or asset not in ASSETS or not path or asset in bindings:
            raise CLIError('Provide one distinct historical --archive ASSET=PATH per asset')
        location = _private_path(Path(path))
        if not location.is_file():
            raise CLIError('Every historical archive must already exist')
        bindings[asset] = location
    assets = tuple(args.asset or (asset for asset in ASSETS if asset in bindings))
    if len(set(assets)) != len(assets) or not set(assets) <= set(bindings):
        raise CLIError('Selected assets must be distinct and explicitly bound to native histories')
    output = _private_path(args.output_dir)
    reserved = [Path(str(path)+suffix) for path in bindings.values() for suffix in ('','-wal','-shm','-journal')]
    if output.exists() or any(_overlap(output,path) for path in reserved):
        raise CLIError('Choose a new output directory separate from inputs and SQLite sidecars')
    if any(parent.exists() and not parent.is_dir() for parent in output.parents):
        raise CLIError('The output directory parent must be a directory')
    return dict(archives=bindings, assets=assets, output_dir=output,
                acquire_missing_labels=args.acquire_missing_labels, start=args.start, end=args.end,
                timeout=args.timeout, max_pages=args.max_pages, readiness_only=args.readiness_only,
                bootstrap_samples=args.bootstrap_samples, seed=args.seed,
                recency_weighting=args.recency_weighting, recency_half_lives_contracts=recencies)


def _fit_configuration(config):
    from kalshi.asset_calibration import AssetCalibrationConfig
    options = dict(bootstrap_samples=config['bootstrap_samples'], seed=config['seed'],
                   recency_weighting=config.get('recency_weighting', 'auto'))
    if config.get('recency_half_lives_contracts') is not None:
        options['recency_half_lives_contracts'] = tuple(config['recency_half_lives_contracts'])
    return AssetCalibrationConfig(**options)


def inspect_asset_archive(path, asset):
    """Bounded source identity and official-label coverage; no tick-history scans."""
    profile = asset_spec(asset)
    with HistoryArchive(path, read_only=True) as archive:
        db = archive.db
        acquisitions = [dict(row) for row in db.execute('''SELECT a.id,a.hour,a.status,a.started_at,
            a.received_at,a.payload_sha256 FROM history_acquisitions a JOIN
            (SELECT hour,MAX(id) latest FROM history_acquisitions WHERE index_id=? GROUP BY hour) b
            ON a.id=b.latest ORDER BY a.hour LIMIT 746''', (profile.index_id,))]
        if len(acquisitions) > 744:
            raise FeatureError('The historical asset acquisition view exceeds its supported 31-day bound')
        result = dict(asset=asset, source=profile.source, index_id=profile.index_id,
            source_archive=str(path), read_only=True, acquisition_count=len(acquisitions),
            acquisition_statuses=dict(Counter(row['status'] for row in acquisitions)),
            first_hour=acquisitions[0]['hour'] if acquisitions else None,
            last_hour=acquisitions[-1]['hour'] if acquisitions else None,
            official_market_count=0, official_outcome_count=0, eligible_official_contracts=0,
            status='missing_ticks' if not acquisitions else 'missing_official_labels',
            source_archives_modified=False, receipt_times_fabricated=False)
        document = db.execute("SELECT id,payload_sha256,LENGTH(payload) payload_chars FROM history_documents WHERE kind='kalshi_contracts' AND item_key LIKE ? ORDER BY id DESC LIMIT 1",(asset+':%',)).fetchone()
        document_identity = None
        if document is not None:
            if document['payload_chars'] > MAX_DOCUMENT_BYTES:
                raise FeatureError('The official historical contract document exceeds its size bound')
            saved, dataset = _dataset(db,asset,None)
            contracts, excluded = _contracts(dataset)
            closes=[item[2] for item in contracts]
            result.update(official_market_count=len(dataset['markets']), official_outcome_count=len(dataset['outcomes']),
                eligible_official_contracts=len(contracts), official_exclusions=dict(excluded),
                official_dataset_id=saved['id'], official_document_sha256=saved['payload_sha256'],
                official_document_received_at=saved['received_at'],
                first_contract_close=_iso(min(closes)) if closes else None,
                last_contract_close=_iso(max(closes)) if closes else None,
                status='ready' if acquisitions and contracts else result['status'])
            document_identity=dict(id=saved['id'],payload_sha256=saved['payload_sha256'])
        result['source_view_sha256']=_digest(dict(asset=asset,acquisitions=acquisitions,document=document_identity))
        return result


def _label_dates(config, readiness):
    if config['start'] is not None:
        return config['start'],config['end']
    if readiness['first_hour'] is None or readiness['last_hour'] is None:
        raise FeatureError('A source tick interval is required before official labels can be prepared')
    lower=_timestamp(readiness['first_hour'])+3600
    upper=_timestamp(readiness['last_hour'])+3600
    if not 0 < upper-lower <= 31*86400:
        raise FeatureError('The inferred official-label interval is empty or exceeds 31 days')
    return _iso(lower),_iso(upper)


def acquire_labels_to_fresh_copy(source, asset, directory, config, readiness):
    """Public outcome reads first; only successful data justify copying/enriching."""
    from kalshi.client import KalshiClient
    from kalshi.history_markets import fetch_market_pages
    start,end=_label_dates(config,readiness)
    acquisitions=[]
    raw_dir=directory/'public-outcomes'
    raw_dir.mkdir(mode=0o700)
    def retain(row):
        destination=raw_dir/f'acquisition-{len(acquisitions):04d}.json'
        _write_report(destination,row)
        acquisitions.append(dict(path=str(destination),route=row['route'],page=row['page'],
            started_at=row['started_at'],received_at=row['received_at'],payload_sha256=_digest(row['payload'])))
    with KalshiClient(environment='production',allow_writes=False,timeout=config['timeout']) as client:
        dataset=fetch_market_pages(client,asset=asset,start=start,end=end,
            max_pages=config['max_pages'],on_acquisition=retain)
    _write_report(raw_dir/'contracts-dataset.json',dataset)
    if not dataset['summary']['pagination_complete'] or not dataset['outcomes']:
        raise FeatureError('Public market acquisition returned no complete official-outcome dataset')
    copied=directory/'history-with-official-labels.sqlite3'
    # SQLite backup honors an active WAL; copying a base file alone would not.
    with HistoryArchive(source,read_only=True) as original, closing(sqlite3.connect(copied)) as destination:
        original.db.backup(destination,pages=512,sleep=.01)
    with HistoryArchive(copied) as enriched:
        for acquisition in acquisitions:
            record=json.loads(Path(acquisition['path']).read_text(encoding='utf-8'))
            kind='kalshi_cutoff' if record['route']=='/historical/cutoff' else 'kalshi_market_page'
            enriched.record_document(kind,f'{asset}:{record["route"]}:{record["page"]}',record['payload'],
                record['started_at'],record['received_at'],request_path=record['route'],request_params=record['params'])
        enriched.record_document('kalshi_contracts',f'{asset}:{start}:{end}',dataset,
            min(row['started_at'] for row in acquisitions),max(row['received_at'] for row in acquisitions),
            request_path='/markets+/historical/markets',request_params=dict(asset=asset,start=start,end=end))
    return copied,dict(status='complete',source_modified=False,enriched_copy=str(copied),
        public_reads_only=True,acquisitions=acquisitions,summary=dataset['summary'])


def candidate_volatility_rows(path, preclose_rows, config):
    """Reuse exact five-second increments for every causal history/half-life pair."""
    rows=sorted(preclose_rows,key=lambda row:(row['as_of'],row['ticker']))
    histories=tuple(config.history_seconds)
    half_lives=tuple(config.half_lives_seconds)
    if (not rows or any(type(value) is not int or value <= 0 or value % 5 for value in histories)):
        raise FeatureError('Volatility candidates need nonempty states and five-second-aligned histories')
    maximum=max(histories)
    result={history:[] for history in histories}
    exclusions=Counter()
    with HistoryArchive(path,read_only=True) as archive:
        db=archive.db;db.execute('BEGIN')
        profile=asset_spec(rows[0]['asset'])
        grid=_SecondGrid(db,profile.index_id,min(row['as_of'] for row in rows)-maximum,max(row['as_of'] for row in rows))
        for position,row in enumerate(rows,1):
            clock=row['as_of']
            try:
                if clock!=int(clock) or row.get('model_time')!=clock:
                    raise FeatureError('Volatility states require an exact native source-time second')
                points=grid.window(clock,maximum)
                if points[-1].price!=float(row['spot']):
                    raise FeatureError('Native candidate-volatility spot differs from the preclose state')
                increments=[(points[index].event_time,math.log(points[index].price/points[index-5].price))
                            for index in range(5,len(points),5)]
                weighted={}
                for half_life in half_lives:
                    weighted[half_life]=[(increment,math.exp(-math.log(2)*(clock-stamp)/half_life))
                                         for stamp,increment in increments]
                for history in histories:
                    count=history//5
                    if count<30:
                        raise FeatureError('Volatility candidates need at least thirty nonoverlapping returns')
                    sigmas={}
                    for half_life in half_lives:
                        values=weighted[half_life][-count:]
                        variance=math.fsum(increment*increment*weight for increment,weight in values)/math.fsum(5*weight for _,weight in values)
                        sigmas[format(float(half_life),'.12g')]=math.sqrt(variance)
                    result[history].append(dict(ticker=row['ticker'],asset=row['asset'],as_of=clock,
                        close_time=row['close_time'],spot=float(row['spot']),y=row['y'],sigma_by_half_life=sigmas))
            except FeatureError as error:
                exclusions[str(error)]+=1
            if position%1000==0 or position==len(rows):
                _progress(rows[0]['asset'],'candidate_volatility',processed_rows=position,total_rows=len(rows))
        db.rollback()
    return result,dict(input_rows=len(rows),candidate_rows={str(key):len(value) for key,value in result.items()},
        excluded_rows=dict(exclusions),increment_reuse='exact_nonoverlapping_five_second_returns',
        future_ticks_used=False,selection='common_complete_maximum_history_grid')


def extract_and_fit(path,asset,config,readiness):
    from kalshi.asset_calibration import AssetCalibrationConfig,calibration_rows_from_preclose,fit_asset_calibration
    from kalshi.preclose_history import preclose_rows_from_history
    fit_config=_fit_configuration(config)
    _progress(asset,'preclose_native_features_started')
    prepared=preclose_rows_from_history({asset:path})
    _progress(asset,'preclose_native_features_complete',rows=len(prepared['rows']))
    if not prepared['rows']:
        raise FeatureError('No native preclose rows survive source/rounding evidence checks')
    candidates,coverage=candidate_volatility_rows(path,prepared['rows'],fit_config)
    rows=calibration_rows_from_preclose(prepared['rows'],candidates,config=fit_config)
    _progress(asset,'joint_model_selection_started',rows=len(rows),
        base_geometry_candidates=len(fit_config.history_seconds)*len(fit_config.half_lives_seconds),
        recency_candidates=len(fit_config.recency_half_lives_contracts))
    after=inspect_asset_archive(path,asset)
    if after['source_view_sha256']!=readiness['source_view_sha256']:
        raise FeatureError('The historical source view changed during feature extraction; use a consistent snapshot')
    provenance=dict(source='historical_source_time_reconstruction',source_archive=str(path),
        source_view_sha256=readiness['source_view_sha256'],data_readiness=readiness,
        preclose_provenance=prepared.get('provenance'),preclose_coverage=prepared.get('coverage'),
        volatility_coverage=coverage,receipt_times_fabricated=False,source_archives_modified=False)
    report=fit_asset_calibration(rows,asset=asset,config=fit_config,provenance=provenance)
    report['feature_preparation']=dict(preclose_rows=len(prepared['rows']),common_candidate_rows=len(rows),
        volatility_coverage=coverage)
    return report


def _refresh_command(asset,path,output,config=None):
    command = ['python3','tools/kalshi_calibrate_assets.py','--archive',asset+'='+str(path),
        '--asset',asset,'--output-dir',str(output.parent/(output.name+'-'+asset.lower()+'-refreshed')),
        '--acquire-missing-labels']
    if config is not None:
        command += ['--recency-weighting', config.get('recency_weighting', 'auto')]
        for half in config.get('recency_half_lives_contracts', ()):
            if half is not None:
                command += ['--recency-half-life-contracts', str(half)]
    return command


def _progress(asset,phase,**details):
    """Bounded phase receipts make long offline extraction observable."""
    print(json.dumps(dict(kind='kalshi_asset_calibration_progress',asset=asset,
        phase=phase,recorded_at=_iso(time.time()),**details)),file=sys.stderr,flush=True)


def calibrated_archive_identity(model):
    """Reproduce immutable ShadowArchive experiment identity without opening one."""
    from kalshi.calibrated_model import CalibrationModel,CalibrationModelError
    from kalshi.minute_five import minute_five_model_definition
    if not isinstance(model,CalibrationModel):
        raise CalibrationModelError('Archive identity requires a validated calibration model')
    profile=asset_spec(model.asset)
    bound=ForecastConfig(**model.base_model_definition['config']['forecast'])
    definition=minute_five_model_definition(bound,asset=model.asset,decision_model='calibrated',
        probability_calibration=model)
    experiment=dict(version=1,source=profile.source,model_version=definition['model_version'],
        model=definition['config']['forecast'],model_definition=definition)
    if model.asset!='BTC':
        experiment['asset']=dict(symbol=model.asset,series=profile.series,index_id=profile.index_id)
    identity=dict(source=profile.source,model_version=definition['model_version'],
        model_fingerprint=_digest(experiment))
    return identity,experiment


def _readiness_projection(report,asset,result,*,model_identity=None,artifact_lineage=None,
        model_identity_reason='No validated exported runtime artifact is available'):
    """Project the four disjoint fitting stages into the compact audit schema."""
    splits=report.get('splits') if isinstance(report.get('splits'),dict) else {}
    stages={name:splits.get(name,{}) for name in ('base_train','map_train','validation','test')}
    stages={name:value if isinstance(value,dict) else {} for name,value in stages.items()}
    counts=report.get('input_counts') if isinstance(report.get('input_counts'),dict) else {}
    guards=report.get('sample_guards') if isinstance(report.get('sample_guards'),dict) else {}
    readiness=result.get('prepared_readiness',result.get('readiness',{}))
    readiness=readiness if isinstance(readiness,dict) else {}
    def count(value):
        return value if type(value) is int and value>=0 else 0
    def days(value):
        value=value.get('days')
        return len(set(value)) if isinstance(value,list) and all(isinstance(day,str) for day in value) else 0
    def clock(value):
        if type(value) in (int,float):return value if math.isfinite(value) and value>=0 else None
        try:return _timestamp(value)
        except (ValueError,TypeError,OverflowError):return None
    chronology=True
    for earlier,later in zip(stages,tuple(stages)[1:]):
        last,first=clock(stages[earlier].get('last_close')),clock(stages[later].get('first_as_of'))
        chronology=chronology and last is not None and first is not None and first>=last+900
    for cutoff,stage in (('base_fit_cutoff','map_train'),('map_fit_cutoff','validation'),('selection_cutoff','test')):
        actual,expected=clock(report.get(cutoff)),clock(stages[stage].get('first_as_of'))
        chronology=chronology and actual is not None and actual==expected
    chronology=(chronology and report.get('test_used_for_selection') is False
        and report.get('refit_after_validation') is False and guards.get('chronological_selection',True) is True)
    ordered_days=True
    for earlier,later in zip(stages,tuple(stages)[1:]):
        left,right=stages[earlier].get('days'),stages[later].get('days')
        ordered_days=(ordered_days and days(stages[earlier])>0 and days(stages[later])>0
            and max(left)<min(right))
    chronology=chronology and ordered_days
    configuration=report.get('configuration') if isinstance(report.get('configuration'),dict) else {}
    minimum_stage=count(configuration.get('minimum_contracts_per_stage'))
    minimum_test=count(configuration.get('minimum_test_contracts'))
    minimum_days=count(configuration.get('minimum_test_days'))
    configured_total=(minimum_stage>=20 and minimum_test>=200
        and count(counts.get('contracts'))>=3*minimum_stage+minimum_test
        and all(count(value.get('count_contracts'))>=minimum_stage for value in stages.values()))
    retained_days=(minimum_days>=5 and all(days(stages[name])>=minimum for name,minimum in
        (('base_train',3),('map_train',2),('validation',2),('test',minimum_days))))
    base=report.get('selected_base') if isinstance(report.get('selected_base'),dict) else {}
    provenance=report.get('provenance') if isinstance(report.get('provenance'),dict) else {}
    normalized=dict(kind=report.get('kind'),version=report.get('version'),asset=report.get('asset'),
        fit_status=report.get('fit_status'),synthetic=report.get('synthetic'),
        settlement_grid=report.get('settlement_grid'),
        source_provenance=dict(source=provenance.get('source',report.get('evaluation_clock')),
            asset=report.get('asset'),receipt_times_fabricated=provenance.get('receipt_times_fabricated'),
            source_view_sha256=provenance.get('source_view_sha256'),
            full_fit_provenance_sha256=_digest(provenance),
            official_document_sha256=readiness.get('official_document_sha256'),
            official_document_received_at=readiness.get('official_document_received_at')),
        input_counts=dict(contracts=count(counts.get('contracts')),
            official_outcomes=count(readiness.get('eligible_official_contracts')),
            forecasts=count(counts.get('qualified_rows')),utc_days=count(counts.get('days')),
            train_contracts=sum(count(stages[name].get('count_contracts')) for name in ('base_train','map_train')),
            validation_contracts=count(stages['validation'].get('count_contracts')),
            test_contracts=count(stages['test'].get('count_contracts')),test_days=days(stages['test'])),
        selected_base=dict(history_seconds=base.get('history_seconds'),
            ewma_half_life_seconds=base.get('half_life_seconds'),volatility_scale=base.get('volatility_scale')),
        selected_calibration=report.get('selected_calibration'),selected_recency=report.get('selected_recency'),
        sample_guards={name:guards.get(name) is True for name in
            ('configured_total_sample_met','sufficient_test_contracts','sufficient_test_days','nonsynthetic')},
        normalization=dict(training_count='sum_of_disjoint_base_train_and_map_train_contracts',
            split_contract_counts={name:count(value.get('count_contracts')) for name,value in stages.items()},
            original_sample_guards=guards,chronology_checked_with_purge_seconds=900,
            derived_guards=dict(configured_total_sample_met=configured_total,
                retained_stage_day_minima_met=retained_days,chronological_selection=chronology),
            derivation_scope='Recorded split counts, UTC days, clocks and configured thresholds; source rows are not rescored',
            source_report_sha256=_digest(report)))
    normalized['sample_guards']['chronological_selection']=chronology
    normalized['sample_guards']['configured_total_sample_met']=(configured_total and retained_days
        and guards.get('configured_total_sample_met',True) is True
        and guards.get('retained_stage_day_minima_met',True) is True
        and all(guards.get(name) is True for name in
            ('all_stages_have_twenty_contracts','base_training_has_both_outcomes',
             'map_training_has_both_outcomes','base_effective_sample_met','map_effective_sample_met')))
    normalized['model_identity']=model_identity
    normalized['artifact_lineage']=artifact_lineage
    normalized['model_identity_reason']=model_identity_reason
    return normalized


def write_batch_readiness(directory,*,manifest=None,policy_constraints=None,strategy_parameters=None):
    """Rebuild a completed batch's compact audit without refitting or promotion."""
    from kalshi.calibration_readiness import build_calibration_readiness
    directory=Path(directory)
    if manifest is None:
        manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    assets=manifest['selected_assets']
    reports={};runtime_artifacts={}
    for asset in assets:
        result=manifest['assets'].get(asset,{})
        path=directory/asset/'asset-calibration-report.json'
        if path.is_file():
            if path.stat().st_size>MAX_DOCUMENT_BYTES:
                raise CLIError('The generated fit report exceeds its supported audit bound')
            report=json.loads(path.read_text(encoding='utf-8'))
            identity,lineage,reason=None,None,'No validated exported runtime artifact is available'
            if result.get('artifact_status')=='exported':
                from kalshi.calibrated_model import load_calibration_model,CalibrationModelError,CalibrationModel
                try:
                    # Only the generated per-asset filename is trusted, never a report-supplied path.
                    model=load_calibration_model(directory/asset/'asset-calibration-model.json',asset=asset)
                    if not isinstance(model,CalibrationModel):
                        raise CalibrationModelError('A validated runtime artifact is required')
                    artifact=model.artifact()
                    if (result.get('artifact_sha256')!=model.sha256
                            or report.get('data_fingerprint')!=artifact['data_fingerprint']):
                        raise CalibrationModelError('The exported artifact differs from its batch or fitting lineage')
                    identity,experiment=calibrated_archive_identity(model)
                    lineage=dict(artifact_sha256=model.sha256,data_fingerprint=artifact['data_fingerprint'],
                        base_model_sha256=artifact['base_model_sha256'],
                        calibrated_definition_sha256=_digest(experiment['model_definition']),
                        bound_forecast_sha256=_digest(experiment['model']),archive_experiment=experiment)
                    reason='Derived from the validated exported artifact and exact immutable ShadowArchive experiment'
                except (ValueError,OSError,TypeError,KeyError,OverflowError):
                    reason='Exported runtime artifact or its batch/fitting lineage could not be verified'
            reports[asset]=_readiness_projection(report,asset,result,model_identity=identity,
                artifact_lineage=lineage,model_identity_reason=reason)
            runtime_artifacts[asset]=dict(model_identity=identity,artifact_lineage=lineage,reason=reason,
                artifact_status=result.get('artifact_status'))
    # These are the current user-specified limits and cadence, never fitted values.
    constraints=(dict(shared_capital_cap=500,per_market_cap=300,entry_cap=1)
        if policy_constraints is None else policy_constraints)
    strategy=(dict(entry_cadence_seconds=3) if strategy_parameters is None else strategy_parameters)
    audit=build_calibration_readiness(reports,assets=assets,policy_constraints=constraints,
        strategy_parameters=strategy)
    audit.update(batch_manifest=str(directory/'manifest.json'),
        source_report_paths={asset:str(directory/asset/'asset-calibration-report.json') for asset in reports},
        input_normalization='four_disjoint_stages_to_compact_readiness_v1',
        runtime_artifacts=runtime_artifacts,
        protective_exit_thresholds='manual_user_configuration_not_estimated_by_this_batch')
    path=directory/'calibration-readiness.json';_write_report(path,audit)
    manifest['calibration_readiness']=dict(report=str(path),
        model_parameters_ready_count=audit['model_parameters_ready_count'],
        policy_parameters_ready_count=audit['policy_parameters_ready_count'],
        all_model_parameters_ready=audit['all_model_parameters_ready'],
        all_models_fully_calibrated=False,execution_eligible=False)
    _write_report(directory/'manifest.json',manifest)
    return audit


def run_batch(config):
    directory=config['output_dir'];directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    manifest_path=directory/'manifest.json'
    manifest=dict(kind='kalshi_asset_calibration_batch',version=1,status='running',
        created_at=_iso(time.time()),assets={},selected_assets=list(config['assets']),
        source_archives_modified=False,live_model_updated=False,orders_submitted=0,
        all_selected_assets_qualified=False,execution_eligible=False,
        calibration_recency=dict(weighting_mode=config.get('recency_weighting', 'auto'),
            candidate_half_lives_contracts=list(config.get('recency_half_lives_contracts', (None,96,384))),
            interval_seconds=900,uniform_is_diagnostic_only=config.get('recency_weighting')=='exponential'),
        public_label_acquisition_requested=config['acquire_missing_labels'],
        credential_environment_names_present={name:name in os.environ for name in
            ('KALSHI_API_KEY_ID','KALSHI_PRIVATE_KEY_PATH')},
        credential_scope='Presence of names only; public label reads do not load credentials',
        limitations=['Successful fitting does not establish predictive improvement or live profitability.',
            'Missing assets remain blocked; another asset never supplies fallback parameters.',
            'Fees, slippage, order pacing and protective exit thresholds are not fitted by this model batch.'])
    _write_report(manifest_path,manifest)
    for asset in config['assets']:
        path=config['archives'][asset];own=directory/asset;own.mkdir(mode=0o700)
        result=dict(status='checking',source_archive=str(path),artifact_status='not_written',
            refresh_command=_refresh_command(asset,path,directory,config))
        manifest['assets'][asset]=result;_write_report(manifest_path,manifest)
        _progress(asset,'checking_native_data')
        try:
            readiness=inspect_asset_archive(path,asset);result['readiness']=readiness
            if readiness['status']!='ready' and config['acquire_missing_labels'] and readiness['status']=='missing_official_labels':
                try:
                    path,acquired=acquire_labels_to_fresh_copy(path,asset,own,config,readiness)
                    result['label_acquisition']=acquired
                    readiness=inspect_asset_archive(path,asset);result['prepared_readiness']=readiness
                except (ValueError,OSError,sqlite3.Error,KalshiError) as error:
                    result.update(status='data_blocked',label_acquisition=dict(status='unavailable',
                        reason=str(error),source_modified=False,public_reads_only=True))
            if readiness['status']!='ready':
                result.update(status='data_blocked',reason=readiness['status'])
            elif config['readiness_only']:
                result.update(status='data_ready',artifact_status='not_requested')
            else:
                _progress(asset,'extracting_and_fitting',source_archive=str(path))
                report=extract_and_fit(path,asset,config,readiness)
                _progress(asset,'fit_complete',fit_status=report.get('fit_status'))
                report_path=own/'asset-calibration-report.json';_write_report(report_path,report)
                result.update(status='fitted',report=str(report_path))
                from kalshi.calibrated_model import (asset_calibration_model_artifact,
                    calibration_model_from_artifact,CalibrationModelError)
                try:
                    artifact=asset_calibration_model_artifact(report)
                    model=calibration_model_from_artifact(artifact,asset=asset)
                    artifact_path=own/'asset-calibration-model.json';_write_report(artifact_path,artifact)
                    result.update(status='qualified_research',artifact_status='exported',artifact=str(artifact_path),
                        artifact_sha256=model.sha256,calibration_scope='asset_base_parameters_and_probability_map',
                        live_validated=False)
                except CalibrationModelError as error:
                    result.update(status='unqualified_research',artifact_status='withheld',reason=str(error))
        except KeyboardInterrupt:
            result.update(status='interrupted');manifest['status']='interrupted'
            _write_report(manifest_path,manifest);return 130,manifest_path
        except (ValueError,OSError,sqlite3.Error,ImportError,AttributeError,KalshiError) as error:
            result.update(status='unavailable',reason=str(error),artifact_status='not_written')
        _write_report(own/'readiness.json',result)
        _write_report(manifest_path,manifest)
        _progress(asset,'asset_complete',status=result['status'])
    statuses=[row['status'] for row in manifest['assets'].values()]
    qualified=all(status=='qualified_research' for status in statuses)
    manifest.update(status='complete' if qualified or config['readiness_only'] and all(status=='data_ready' for status in statuses) else 'partial',
        all_selected_assets_qualified=qualified,finished_at=_iso(time.time()),
        counts={status:statuses.count(status) for status in sorted(set(statuses))})
    _write_report(manifest_path,manifest)
    write_batch_readiness(directory,manifest=manifest)
    return (0 if manifest['status']=='complete' else 2),manifest_path


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        code,path=run_batch(_configuration(args))
        print(json.dumps(dict(kind='kalshi_asset_calibration_batch_summary',manifest=str(path),
            status='complete' if code==0 else 'interrupted' if code==130 else 'partial',orders_submitted=0,
            live_model_updated=False)))
        return code
    except (ValueError,OSError) as error:
        print('Asset calibration: '+(str(error) if isinstance(error,CLIError) else
            'Native archive inputs or the fresh output directory are unavailable'),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
