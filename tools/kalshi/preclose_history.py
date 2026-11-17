"""Causal retrospective Gaussian states for the reconciled preclose grid.

The settlement samples are T-60,...,T-1. Historical terms are verified from
original native documents. Future outcome fields never enter prediction.
Supplied causal volatility features are reused explicitly, not claimed to be
recomputed; the exact model-clock spot and all fixed settlement ticks are
checked against the bound native archive.
"""
from __future__ import annotations

from collections import Counter,defaultdict
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,localcontext
import hashlib
import json
import math
from pathlib import Path

from .crypto_assets import asset_from_ticker,asset_spec
from .historical_settlement_payoff import enrich_historical_rows,historical_rounded_payoff
from .settlement_payoff import SettlementPayoffError
from .settlement_reconciliation import _NativeWindow
from .stochastic_features import DEFAULT_HORIZONS,_read_only,rows_from_history


GRID='preclose_60_seconds_v1'


class PrecloseHistoryError(ValueError):
    """A causal preclose state, source proof or model binding is invalid."""


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def preclose_model_definition(asset,*,synthetic=False,payoff_mode='historical_rounded'):
    profile=asset_spec(asset)
    if type(synthetic) is not bool or payoff_mode != ('legacy_unrounded' if synthetic else 'historical_rounded'):
        raise PrecloseHistoryError('Native preclose terms or an explicit synthetic unrounded scope are required')
    return dict(kind='kalshi_preclose_gaussian_definition',version=1,asset=profile.symbol,source=profile.source,
        settlement_grid=GRID,payoff_mode=payoff_mode,synthetic=synthetic,
        model_family='frozen_arithmetic_diffusion',estimator='population_gaussian',physical_drift=0,
        volatility=dict(half_life_seconds=300,scale=1.0,history_seconds=1800,feature='slow_sigma',parameters_fitted=False),
        proof_scope='synthetic_unverified' if synthetic else 'original_archived_market_terms',
        sampling=dict(samples=60,first_offset_seconds=-60,last_offset_seconds=-1,step_seconds=1,
                      known_samples='exact_native_samples_at_or_before_model_time'))


def preclose_model_identity(definition):
    if not isinstance(definition,dict):
        raise PrecloseHistoryError('A preclose model definition is required')
    return _hash(definition)


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def _decimal(value,*,zero=False):
    try:
        if type(value) not in (int,float,str,Decimal):
            raise ValueError()
        result=Decimal(str(value))
        if not result.is_finite() or result<0 or not zero and result==0:
            raise ValueError()
        return result
    except (InvalidOperation,ValueError):
        raise PrecloseHistoryError('A source price or fixed settlement sum is invalid') from None


def _payoff_metadata(payoff):
    return dict(kind='kalshi_preclose_historical_rounded_payoff',version=1,
        nominal_strike=str(payoff.nominal_strike),effective_strike=str(payoff.effective_strike),
        round_digits=payoff.round_digits,comparator=payoff.comparator,source_sha256=payoff.source_sha256,
        settlement_grid=GRID,proof_scope='original_archived_market_terms')


def predict_preclose_gaussian(row):
    """Exact conditional probability for a bound preclose state; no labels read."""
    if not isinstance(row,dict) or row.get('settlement_grid')!=GRID:
        raise PrecloseHistoryError('The exact preclose settlement grid must be explicit')
    profile=asset_spec(row.get('asset'))
    if row.get('source')!=profile.source or asset_from_ticker(row.get('ticker')).symbol!=profile.symbol:
        raise PrecloseHistoryError('The preclose source, asset and ticker do not match')
    synthetic=row.get('synthetic') is True
    expected_provenance='synthetic_demo' if synthetic else 'historical_source_time_reconstruction'
    if row.get('provenance')!=expected_provenance:
        raise PrecloseHistoryError('The preclose source provenance is incompatible')
    definition=preclose_model_definition(profile.symbol,synthetic=synthetic,
        payoff_mode='legacy_unrounded' if synthetic else 'historical_rounded')
    if row.get('model_identity')!=preclose_model_identity(definition):
        raise PrecloseHistoryError('The exact preclose model identity is not bound')
    model_time,as_of,closed=(row.get(name) for name in ('model_time','as_of','close_time'))
    if (not all(_finite(value) for value in (model_time,as_of,closed))
            or not 0<=model_time<=as_of<closed or closed!=int(closed) or closed-model_time>900):
        raise PrecloseHistoryError('The preclose source and forecast clocks are invalid')
    if row.get('history_seconds')!=1800 or not _finite(row.get('slow_sigma')) or row['slow_sigma']<0:
        raise PrecloseHistoryError('The fixed 300-second EWMA and 1800-second history are required')
    if row.get('features_as_of') is not None and (not _finite(row['features_as_of']) or row['features_as_of']>model_time):
        raise PrecloseHistoryError('A supplied feature uses information after the model clock')
    expected=[closed-offset-model_time for offset in range(60,0,-1) if closed-offset>model_time]
    offsets=row.get('future_offsets')
    if (not isinstance(offsets,(list,tuple)) or len(offsets)!=len(expected)
            or any(not _finite(actual) or abs(actual-target)>1e-6 for actual,target in zip(offsets,expected))):
        raise PrecloseHistoryError('Future samples differ from every remaining T-60,...,T-1 observation')
    count=len(offsets)
    if type(row.get('known_count')) is not int or row['known_count']!=60-count:
        raise PrecloseHistoryError('The fixed and future preclose sample counts disagree')
    known=_decimal(row.get('known_sum'),zero=True)
    if row['known_count']==0 and known!=0 or row['known_count']>0 and known<=0:
        raise PrecloseHistoryError('The known preclose sum does not match its sample count')
    spot,strike=_decimal(row.get('spot')),_decimal(row.get('target_price'))
    comparator=row.get('strike_type')
    if comparator not in ('greater','greater_or_equal'):
        raise PrecloseHistoryError('The preclose comparator is unsupported')
    payoff=None
    if not synthetic:
        payoff=historical_rounded_payoff(row)
        if row.get('payoff_status')!='historical_rounded' or row.get('payoff')!=_payoff_metadata(payoff):
            raise PrecloseHistoryError('The compact preclose payout differs from its original native terms')
        threshold=payoff.effective_strike
    else:
        if row.get('payoff_mode')!='legacy_unrounded':
            raise PrecloseHistoryError('Synthetic proof cannot authorize native rounded settlement')
        threshold=strike
    with localcontext() as context:
        context.prec=60
        total_mean=known+count*spot
        mean=total_mean/60
        centered=total_mean-60*threshold
    covariance=math.fsum((2*(count-index)-1)*value for index,value in enumerate(offsets))
    dollar_vol=float(spot)*row['slow_sigma']
    variance=dollar_vol*dollar_vol*covariance/3600
    if not math.isfinite(variance) or variance<0:
        raise PrecloseHistoryError('The preclose Gaussian variance is not finite')
    if variance:
        probability=.5*math.erfc(-float(centered)/(60*math.sqrt(variance)*math.sqrt(2)))
    elif payoff is not None:
        probability=float(payoff.deterministic_outcome(mean))
    else:
        probability=float(centered>=0 if comparator=='greater_or_equal' else centered>0)
    return dict(kind='kalshi_preclose_gaussian_prediction',version=1,asset=profile.symbol,
        settlement_grid=GRID,payoff_mode=definition['payoff_mode'],model_identity=row['model_identity'],
        probability_yes=probability,probability_no=1-probability,mean=float(mean),median=float(mean),
        variance=variance,known_count=row['known_count'],future_count=count,covariance_time_sum=covariance,
        estimator='population_gaussian',research_only=True,execution_eligible=False,calibrated=False,
        synthetic=synthetic,model_uncertainty_included=False)


class _PointReader:
    def __init__(self,native):
        self.native,self.cache=native,{}

    def get(self,stamp):
        if stamp in self.cache:
            return self.cache[stamp]
        if not _finite(stamp) or stamp!=int(stamp):
            raise PrecloseHistoryError('The historical model spot requires an exact native second-boundary tick')
        native,hour=self.native,int(stamp//3600*3600)
        owner=native.acquisitions.get(hour)
        if owner is not None and owner['status']=='error':
            raise PrecloseHistoryError('latest_hour_acquisition_failed')
        variants=[]
        for candidate in (hour,hour-3600) if stamp==hour else (hour,):
            acquisition=native.acquisitions.get(candidate)
            if acquisition is None or acquisition['status']=='error':
                continue
            native.used[acquisition['id']]=acquisition
            variants.extend(native.db.execute('''SELECT value,repeated,amendment FROM history_ticks
                WHERE acquisition_id=? AND source_time_ms=?''',(acquisition['id'],int(stamp*1000))).fetchall())
        if not variants:
            raise PrecloseHistoryError('missing_exact_model_spot_tick')
        values=[_decimal(row['value']) for row in variants]
        if any(row['repeated'] or row['amendment'] is not None for row in variants) or len(set(values))!=1:
            raise PrecloseHistoryError('flagged_or_conflicting_model_spot_tick')
        self.cache[stamp]=values[0]
        return values[0]


def preclose_rows_from_history(bindings,*,feature_rows=None,horizons=DEFAULT_HORIZONS,max_rows=200000):
    """Convert source-bound causal states without changing original labels."""
    if (not isinstance(bindings,dict) or not 1<=len(bindings)<=4 or type(max_rows) is not int
            or not 1<=max_rows<=200000 or not isinstance(horizons,(tuple,list)) or not horizons
            or len(set(horizons))!=len(horizons) or any(type(value) is not int or not 1<=value<=900 for value in horizons)):
        raise PrecloseHistoryError('Supply bounded asset archives, integer horizons and row limit')
    for asset in bindings:
        asset_spec(asset)
    reused=feature_rows is not None
    extracted=None
    if feature_rows is None:
        extracted=rows_from_history(bindings,horizons=horizons,max_rows=max_rows)
        feature_rows=extracted['rows']
    if not isinstance(feature_rows,(list,tuple)) or len(feature_rows)>max_rows:
        raise PrecloseHistoryError('Supplied historical features exceed their row bound')
    selected=[]
    for original in feature_rows:
        if not isinstance(original,dict) or original.get('asset') not in bindings:
            raise PrecloseHistoryError('Every feature row requires an explicit matching asset archive')
        if original.get('synthetic',False) is not False:
            raise PrecloseHistoryError('Synthetic or invalid synthetic-marked features cannot become native historical rows')
        if not all(_finite(original.get(key)) for key in ('close_time','as_of','model_time')):
            raise PrecloseHistoryError('Feature clocks are invalid')
        horizon=original['close_time']-original['as_of']
        if horizon in horizons:
            selected.append(dict(original,realized_average=None))
    enriched=enrich_historical_rows(selected,bindings)
    definitions={}
    for asset in bindings:
        definition=preclose_model_definition(asset)
        definitions[preclose_model_identity(definition)]=definition
    output,coverage,provenance=[],{},[]
    for asset,path in bindings.items():
        own=[row for row in enriched['rows'] if row['asset']==asset]
        requested=defaultdict(list)
        for row in own:
            requested[row['ticker']].append(row)
        exclusions=Counter()
        converted=[]
        mismatch_contracts,target_missing=set(),set()
        with _read_only(Path(path),'history') as (db,location):
            native=_NativeWindow(db,asset_spec(asset))
            points=_PointReader(native)
            for ticker,group in requested.items():
                requested_horizons=[row['close_time']-row['as_of'] for row in group]
                if set(requested_horizons)!=set(horizons) or len(requested_horizons)!=len(horizons):
                    exclusions['incomplete_or_duplicate_input_horizon_contract']+=1
                    continue
                closed=group[0]['close_time']
                lookup=native.window(closed)
                samples=[lookup(int((closed-offset)*1000)) for offset in range(60,0,-1)]
                complete_target=all(sample['status']=='ready' for sample in samples)
                target=None
                if complete_target:
                    with localcontext() as context:
                        context.prec=60
                        target=sum((_decimal(sample['value']) for sample in samples),Decimal(0))/60
                else:
                    target_missing.add(ticker)
                prepared=[]
                for original in group:
                    try:
                        if original.get('payoff_status')!='historical_rounded':
                            raise PrecloseHistoryError('original_historical_rounded_terms_unavailable')
                        if original.get('history_seconds')!=1800:
                            raise PrecloseHistoryError('fixed_1800_second_history_required')
                        exact_spot=points.get(original['model_time'])
                        if exact_spot!=_decimal(original['spot']):
                            raise PrecloseHistoryError('model_spot_differs_from_exact_bound_native_tick')
                        known=[sample for sample in samples if sample['event_time_ms']/1000<=original['model_time']]
                        if any(sample['status']!='ready' for sample in known):
                            raise PrecloseHistoryError('fixed_preclose_samples_missing_flagged_or_conflicting')
                        with localcontext() as context:
                            context.prec=60
                            known_sum=sum((_decimal(sample['value']) for sample in known),Decimal(0))
                        future=[closed-offset-original['model_time'] for offset in range(60,0,-1)
                                if closed-offset>original['model_time']]
                        fields=('asset','ticker','source','as_of','model_time','features_as_of','close_time','spot',
                            'fast_sigma','slow_sigma','history_seconds','history_span_seconds','features',
                            'target_price','strike_type','provenance','label_available_at','y','historical_payoff_evidence')
                        row={key:original[key] for key in fields if key in original}
                        row.update(synthetic=False,settlement_grid=GRID,payoff_mode='historical_rounded',
                            model_identity=preclose_model_identity(preclose_model_definition(asset)),
                            baseline_sigma=original['slow_sigma'],known_sum=str(known_sum),known_count=len(known),
                            future_offsets=future,horizon_seconds=closed-original['as_of'],
                            dayUTC=datetime.fromtimestamp(closed,timezone.utc).strftime('%Y-%m-%d'),
                            native_reference={key:value for key,value in original['native_reference'].items() if key!='archive_path'},
                            payoff_status='historical_rounded')
                        payoff=historical_rounded_payoff(row)
                        row['payoff']=_payoff_metadata(payoff)
                        prediction=predict_preclose_gaussian(row)
                        row.update(p_raw=prediction['probability_yes'],p_gaussian=prediction['probability_yes'])
                        target_receipt=None
                        if complete_target:
                            identifiers={identifier for sample in samples for identifier in sample['acquisition_ids']}
                            target_receipt=max(native.used[identifier]['received_at'] for identifier in identifiers)
                        row.update(realized_average=None if target is None else str(target),target_available_at=target_receipt,
                            target_kind='exact_native_preclose_sixty_sample_average',
                            training_available_at=None if target_receipt is None else max(target_receipt,row['label_available_at']))
                        if target is not None:
                            try:
                                agrees=int(payoff.deterministic_outcome(target))==row['y']
                            except SettlementPayoffError:
                                row['source_grid_outcome_consistency']='official_label_retained_unspecified_halfway_rule'
                            else:
                                row['source_grid_outcome_consistency']=agrees
                                if not agrees:
                                    mismatch_contracts.add(ticker)
                                    row.update(reconstructed_preclose_average=str(target),realized_average=None)
                        prepared.append(row)
                    except (ValueError,KeyError,TypeError,OverflowError) as error:
                        exclusions[str(error) if isinstance(error,PrecloseHistoryError) else 'invalid_native_preclose_state']+=1
                        prepared=[]
                        break
                if prepared:
                    converted.extend(prepared)
            output.extend(converted)
            coverage[asset]=dict(status='converted' if converted else 'no_qualified_rows',input_rows=len(own),
                input_contracts=len(requested),converted_rows=len(converted),converted_contracts=len({row['ticker'] for row in converted}),
                excluded_contracts=dict(exclusions),continuous_target_missing_contracts=len(target_missing),
                official_label_source_grid_mismatch_contracts=len(mismatch_contracts),official_labels_modified=False)
            provenance.append(dict(asset=asset,archive_path=str(location),proof_scope='original_archived_market_terms',
                original_document_provenance=[item for item in enriched['provenance'] if item['asset']==asset],
                acquisition_metadata_sha256=_hash(list(native.used.values())),
                acquisition_ids=sorted(native.used),read_only=True,receipt_times_fabricated=False,
                supplied_causal_volatility_features_reused=reused,volatility_history_recomputed=not reused,
                exact_native_model_spot_rechecked=True,settlement_grid=GRID,
                missing_samples_interpolated=False,historical_receive_latency_known=False,
                source_revisions_absent_proven=False))
    return dict(kind='kalshi_stochastic_dataset',version=1,synthetic=False,rows=output,
        coverage=coverage,provenance=provenance,definitions=definitions,
        input_feature_fingerprint=_hash(selected),execution_eligible=False,live_model_updated=False,
        original_extraction_coverage=None if extracted is None else extracted.get('coverage'),
        limitations=['Retrospective original-source reconstruction does not establish historical live receipt latency.',
            'Supplied causal volatility features are reused with their input fingerprint; their full historical return series is not recomputed.',
            'The exact native model-clock spot and fixed settlement samples are rechecked without interpolation.',
            'Official labels are retained irrespective of future continuous-target reconciliation.'])
