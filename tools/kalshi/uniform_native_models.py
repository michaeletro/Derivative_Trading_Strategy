"""Asset-generic offline process models on one exact native settlement cohort.

No venue, account, live runner, journal or execution imports. Every fitted model
uses only model_train source events. Official labels do not fit process models.
The caller owns calibration, validation selection and receipt-vintage policy.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
import math

import numpy as np

from .autoregressive_mc import (GaussianEndpointLaw, causal_ewma_variances,
    filter_latent_ar, fit_ar, settlement_projection, validate_returns)
from .garch_fit import fit_garch, filter_garch
from .garch_math import (_geometry, _cdf, gaussian_variance_curve_probability,
    garch_settlement_probability, expected_variance_probability)
from .garch_student_t import fit_student_t_garch11, student_t_garch_settlement_probability
from .stochastic_models import FEATURE_NAMES, _integrated_variance_time, _student_t_cdf
from .stochastic_volatility import (_grid, _coefficients, _degree, train_candidates)

ORDERS = (1, 3, 5, 15)
PREDICTION_STAGES = ('final_calibration', 'validation', 'test')
_FROZEN_FHS_POOLS = {}
MODEL_FAMILIES = {
    'ewma_fixed': 'gaussian_ewma', 'ewma_minute': 'gaussian_ewma',
    'dynamic_vol': 'dynamic_gaussian', 'dynamic_vol_drift': 'dynamic_gaussian_drift',
    'dynamic_vol_student_t': 'dynamic_student_t', 'sv_ou': 'stochastic_volatility',
    'garch_gaussian': 'garch_gaussian', 'garch_moment_gaussian': 'garch_moment_gaussian',
    'garch_student_t': 'garch_student_t', 'har_rv': 'har_rv',
    **{'ar_'+str(p): 'ar' for p in ORDERS},
    **{'kalman_ar_'+str(p): 'kalman_ar' for p in ORDERS},
    **{'fhs_'+str(p): 'fhs' for p in (1, 5, 15)},
}


def _json(value):
    if isinstance(value, dict): return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_json(v) for v in value]
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    return value


def _sigmas(row):
    slow = float(row.get('sigma_slow', row.get('slow_sigma', 0.)))
    fast = float(row.get('sigma_fast', row.get('fast_sigma', 0.)))
    if not all(math.isfinite(v) and v > 0 for v in (slow, fast)):
        raise ValueError('Positive causal fast and slow volatility are required')
    return fast, slow


def _feature_vector(row):
    """Original stochastic feature caches use named mappings or ordered vectors."""
    raw=row.get('features')
    if isinstance(raw,dict):
        if set(raw)!=set(FEATURE_NAMES):
            raise ValueError('The original six named causal drift features are required')
        raw=[raw[name] for name in FEATURE_NAMES]
    values=np.asarray(raw,dtype=float)
    if values.shape!=(6,) or not np.isfinite(values).all():
        raise ValueError('The original six causal drift features are required')
    return values


def _seed(seed, row):
    token = (str(seed)+'|'+row['ticker']+'|'+format(row['model_time'], '.6f')).encode()
    return int.from_bytes(hashlib.sha256(token).digest()[:8], 'big')


def _segments(records):
    return [i for i, r in enumerate(records) if i == 0
            or r['segment_id'] != records[i-1]['segment_id']
            or r['start_time'] != records[i-1]['end_time']]


def _minute_index(row, records):
    i = row.get('minute_return_index')
    if (type(i) is not int or not 0 <= i < len(records)
            or records[i]['end_time'] != math.floor(row['model_time']/60)*60
            or records[i]['end_time'] > row['model_time']
            or records[i]['contiguous_returns'] < 60):
        raise ValueError('A complete causal 60-minute source prefix is required')
    return i


def flat_probability(row, variance_per_minute):
    geometry = _geometry(row)
    count = len(geometry['pre_segments'])+1
    return gaussian_variance_curve_probability(row, [float(variance_per_minute)]*count)['probability_yes']


def dynamic_probability(row, *, student_df=None, drift=None):
    """Native T-60,...,T-1 version of the earlier deterministic variance law."""
    geom = _geometry(row)
    fast, slow = _sigmas(row)
    offsets = row['future_offsets']
    n = len(offsets)
    loading = math.fsum((2*(n-i)-1)*_integrated_variance_time(u, fast, slow, .5, 300.)
                        for i, u in enumerate(offsets))
    variance = geom['spot']**2*loading
    numerator = geom['numerator']
    if drift is not None:
        x = _feature_vector(row)
        if x.shape != (6,) or not np.isfinite(x).all():
            raise ValueError('The original six causal drift features are required')
        scale = geom['spot']*math.hypot(fast, slow)/math.sqrt(2*1800.)
        exposure = math.fsum(offsets)
        numerator += scale*exposure*float(x @ np.asarray(drift['coefficients']))
        covariance = np.asarray(drift['parameter_covariance'])
        variance += (scale*exposure)**2*max(0., float(x @ covariance @ x))
    if student_df is not None and variance > 0:
        # One shared variance-one latent path scale, not independent t ticks.
        scale = math.sqrt(variance*(student_df-2)/student_df)
        return _student_t_cdf(numerator/scale, student_df)
    return _cdf(numerator, variance, geom)


@lru_cache(maxsize=32)
def _sv_coefficients(offsets, half_life, xi, paths, seed, negative):
    times, fine, coarse = _grid(offsets, 15., 1024)
    coefficients = _coefficients(times, fine, coarse, math.log(2)/half_life,
                                 xi, paths, seed, negative)
    return np.asarray([x[0] for x in coefficients], dtype=float)


def sv_probability(row, spec, *, audit=False):
    """Rao-Blackwell OU variance mixture on the native fixing grid."""
    geom = _geometry(row)
    fast, slow = _sigmas(row)
    ratio = math.log((.5*fast*fast+.5*slow*slow)/(slow*slow))
    if abs(ratio) > 12:
        raise ValueError('The native OU log variance ratio exceeds its numerical bound')
    degree, bound = _degree(abs(ratio))
    coefficients = _sv_coefficients(tuple(row['future_offsets']),
        spec.variance_half_life_seconds, spec.log_variance_volatility,
        spec.paths, spec.seed, ratio < 0)
    # Taylor coefficients are nonnegative. The existing rigorous truncation bound
    # is used without rounding or bucketing an origin's volatility state.
    values = np.polynomial.polynomial.polyval(abs(ratio), coefficients[:, :degree+1].T)
    scale = (geom['spot']*slow)**2
    if ratio < 0: scale *= math.exp(ratio)
    variances = values*scale
    if np.any(variances <= 0) or not np.isfinite(variances).all():
        raise ValueError('The OU conditional variances are invalid')
    values=np.asarray([.5*math.erfc(-geom['numerator']/math.sqrt(2*v)) for v in variances])
    probability=float(values.mean())
    if not audit:return probability
    pairs=(values[::2]+values[1::2])/2
    return dict(probability_yes=probability,
        monte_carlo_se=float(pairs.std(ddof=1)/math.sqrt(len(pairs))),
        quadrature_error_quantified=False,maximum_relative_Taylor_remainder_bound=bound)


def _fit_drift(rows, seconds, first, last):
    """Ridge on native training fixing averages with parameter-uncertainty floor."""
    selected = []
    for row in rows:
        offsets = row['future_offsets']
        if not offsets or row['model_time']+offsets[-1] > last: continue
        start, end = int(row['model_time'])+1, int(row['model_time']+offsets[-1])
        left = int(np.searchsorted(seconds['times'], start))
        right = int(np.searchsorted(seconds['times'], end, side='right'))
        stamps = seconds['times'][left:right]
        if (len(stamps) != end-start+1 or not np.array_equal(stamps, np.arange(start, end+1))
                or seconds['segments'][left] != seconds['segments'][right-1]):
            continue
        geom = _geometry(row)
        actual = geom['spot']*np.exp(np.cumsum(seconds['returns'][left:right]))
        target_average = (float(row['known_sum'])+float(np.sum(actual[np.asarray(offsets, dtype=int)-1])))/60
        baseline_average = geom['mean']
        fast, slow = _sigmas(row)
        drift_scale = geom['spot']*math.hypot(fast, slow)/math.sqrt(2*1800.)
        exposure = math.fsum(offsets)/60
        x = _feature_vector(row)
        if x.shape != (6,) or not np.isfinite(x).all():
            raise ValueError('The original six causal drift features are unavailable')
        selected.append((row, x, drift_scale*exposure, target_average-baseline_average))
    if len(selected) < 100 or len({r['ticker'] for r, *_ in selected}) < 20:
        raise ValueError('Insufficient complete source-time drift training targets')
    counts = Counter(r['ticker'] for r, *_ in selected)
    weights = np.array([1/counts[r['ticker']] for r, *_ in selected]);weights /= weights.sum()
    x = np.asarray([a for _, a, _, _ in selected]);factor=np.asarray([a for _, _, a, _ in selected])
    y = np.asarray([a for _, _, _, a in selected])
    # Divide by train-only response RMS, preserving each origin's dollar loading.
    rms = max(1e-12, math.sqrt(float(weights @ (y*y))))
    design=x*(factor/rms)[:, None];target=y/rms
    gram=design.T @ (weights[:, None]*design)+.01*np.eye(6)
    inverse=np.linalg.inv(gram);beta=inverse @ (design.T @ (weights*target))
    residual=target-design@beta
    noise=max(1., float(weights @ (residual*residual)))
    covariance=noise*inverse/max(1., len(counts))
    return dict(coefficients=beta.tolist(), parameter_covariance=covariance.tolist(),
        ridge=.01, covariance='training-only ridge posterior floor divided by contract count',
        response='actual source-time arithmetic fixing average minus zero-drift average',
        fitting_origins=len(selected), fitting_contracts=len(counts),
        first_source_origin=min(r['model_time'] for r, *_ in selected),
        last_source_target=max(r['model_time']+r['future_offsets'][-1] for r, *_ in selected),
        official_binary_labels_used=False, target_cutoff=last)


def fit_models(dataset, parts, *, paths=4096, seed=7, archive=None, progress=None):
    """Fit independent offline adapters; one model failure never drops other arms.

    Coefficients/pools use only model_train. Causal variance filters may consume
    completed observations through each evaluation origin, never its future.
    The fixed controls additionally predict parent_fit/model_train for callers
    fitting disjoint parent calibration and residual corrections.
    """
    if type(paths) is not int or not 128 <= paths <= 4096 or paths % 2:
        raise ValueError('Use a declared even path count from 128 through 4096')
    if type(seed) is not int or not 0 <= seed <= 2**32-1:
        raise ValueError('Use a bounded deterministic seed')
    train=parts.get('model_train', [])
    if not train: raise ValueError('The common model-training block is empty')
    first=min(r['model_time'] for r in train);last=max(r['close_time'] for r in train)
    stages={name:parts.get(name, []) for name in PREDICTION_STAGES}
    for values in stages.values():
        if any(r['model_time'] <= last for r in values):
            raise ValueError('All process-model predictions must follow frozen training parameters')
    notify=(lambda *a, **k: None) if progress is None else progress
    result={name:dict(status='missing_input', reason='Not yet attempted', family=family,
                     probabilities={}, artifact={}) for name,family in MODEL_FAMILIES.items()}
    common=dict(execution_eligible=False, offline_only=True, settlement_grid='T-60,...,T-1',
        fit_stage='model_train', fitting_start=first, fitting_end=last,
        parameters_use_test=False, source_time_reconstruction=True,
        source_receipt_eligibility_enforced_by_caller=True, official_labels_fit_process=False)

    def unavailable(names, error, status='missing_input'):
        if status=='failed_fit' and ('Insufficient' in str(error) or 'At least' in str(error)):
            status='insufficient_training'
        for name in names:
            result[name].update(status=status, reason=str(error), artifact=dict(common))
            notify('model_unavailable', model=name, status=status, reason=str(error))

    def evaluate(name, predictor, artifact, *, all_stages=False):
        try:
            probabilities={};numerical=[]
            for stage,rows in (parts.items() if all_stages else stages.items()):
                values=[]
                for i,row in enumerate(rows):
                    estimate=predictor(row)
                    if isinstance(estimate,dict):
                        p=float(estimate['probability_yes'])
                        if 'monte_carlo_se' in estimate:numerical.append(float(estimate['monte_carlo_se']))
                    else:p=float(estimate)
                    if not math.isfinite(p) or not 0 <= p <= 1:
                        raise ValueError('A native process forecast is not a probability')
                    values.append(p)
                    if i and i%1000==0:notify('forecasting', model=name, stage=stage, origins=i)
                probabilities[stage]=values
            artifact=dict(artifact)
            if numerical:artifact['conditional_numerical_se']=dict(count=len(numerical),
                maximum=max(numerical),rms=math.sqrt(math.fsum(v*v for v in numerical)/len(numerical)),
                mean=math.fsum(numerical)/len(numerical),parameter_uncertainty_included=False)
            result[name].update(status='evaluated', reason=None, probabilities=probabilities,
                                artifact=_json(dict(common, **artifact)))
            notify('model_evaluated', model=name)
        except (ValueError, TypeError, KeyError, ArithmeticError, RuntimeError, np.linalg.LinAlgError) as error:
            unavailable([name], error)

    evaluate('ewma_fixed', lambda r:flat_probability(r, 60*_sigmas(r)[1]**2),
        dict(kind='fixed_native_ewma_gaussian',history_seconds=1800,half_life_seconds=300,
             variance_units='squared_minute_log_return',scale=1.),all_stages=True)
    evaluate('dynamic_vol', dynamic_probability,
        dict(kind='native_dynamic_variance_gaussian',fast_weight=.5,variance_half_life_seconds=300))
    evaluate('dynamic_vol_student_t', lambda r:dynamic_probability(r,student_df=8.),
        dict(kind='native_shared_path_scale_student_t',fast_weight=.5,variance_half_life_seconds=300,
             degrees_of_freedom=8.,shared_scale=True,innovation_variance=1.))

    minute_names=['ewma_minute','garch_gaussian','garch_moment_gaussian','garch_student_t',
                  *['ar_'+str(p) for p in ORDERS],*['kalman_ar_'+str(p) for p in ORDERS]]
    records=dataset.get('minute_returns')
    try:
        validate_returns(records)
        minute_q=causal_ewma_variances(records,window=30,half_life_minutes=5.)
        for own in [train,*stages.values()]:
            for r in own:_minute_index(r,records)
        training=[r for r in records if r['start_time'] >= first and r['end_time'] <= last]
        if len(training)<300:raise ValueError('At least 300 complete model-training minute returns are required')
        evaluate('ewma_minute',lambda r:flat_probability(r,float(minute_q[_minute_index(r,records)])),
            dict(kind='matched_minute_ewma_gaussian',history_minutes=30,half_life_minutes=5.,
                 variance_units='squared_minute_log_return',scale=1.))
    except (ValueError,TypeError,KeyError,ArithmeticError,np.linalg.LinAlgError) as error:
        unavailable(minute_names,error);records=None

    if records is not None:
        for order in ORDERS:
            try:
                fitted=fit_ar(records,order=order,first_time=first,last_time=last,ridge=.01,half_life_days=7.)
                for latent in (False,True):
                    name=('kalman_ar_' if latent else 'ar_')+str(order)
                    try:
                        law=GaussianEndpointLaw(tuple(fitted['coefficients']),.1 if latent else 0.)
                        filtered=filter_latent_ar(records,fitted,minute_q,measurement_fraction=.1) if latent else None
                        def forecast(r, *, law=law, filtered=filtered, order=order):
                            i=_minute_index(r,records);q=float(minute_q[i])
                            mean=(filtered[0][i] if filtered is not None else
                                  np.asarray([records[i-j]['value'] for j in range(order)]))
                            covariance=filtered[1][i] if filtered is not None else np.zeros((order,order))
                            steps=int((r['close_time']-math.floor(r['model_time']/60)*60)/60)
                            m,c=law.forecast(mean,covariance,innovation_variance=q,steps=steps)
                            projection=settlement_projection(r,m,c,bridge_variance=q)
                            # Linear-Gaussian projection is integrated analytically,
                            # avoiding MC noise in choosing AR orders and maps.
                            variance=projection['sampled_endpoint_variance']+projection['integrated_variance']
                            if variance>0:return .5*math.erfc(-projection['centered_mean']/math.sqrt(2*variance))
                            return float(projection['centered_mean']>=0 if r['strike_type']=='greater_or_equal'
                                         else projection['centered_mean']>0)
                        evaluate(name,forecast,dict(kind='native_filtered_ar' if latent else 'native_ar',
                            fit=fitted,measurement_fraction=.1 if latent else 0.,
                            origin_innovation_variance='matched_minute_ewma_30m_half5m',
                            numerical_method='analytic_correlated_gaussian_settlement_projection'))
                    except (ValueError,TypeError,KeyError,ArithmeticError,np.linalg.LinAlgError) as error:
                        unavailable([name],error,'failed_fit')
            except (ValueError,TypeError,KeyError,ArithmeticError,np.linalg.LinAlgError) as error:
                unavailable(['ar_'+str(order),'kalman_ar_'+str(order)],error,'failed_fit')
        for student in (False,True):
            names=['garch_student_t'] if student else ['garch_gaussian','garch_moment_gaussian']
            try:
                fit=fit_student_t_garch11 if student else fit_garch
                weights=[math.exp(-math.log(2)*(last-r['end_time'])/(7*86400)) for r in training]
                fitted=fit([r['value'] for r in training],sample_weights=weights,
                    segment_starts=_segments(training),max_iterations=600,starts=5)
                if fitted.get('converged') is not True:
                    raise ValueError('GARCH optimization did not satisfy its convergence gate')
                filtered=filter_garch([r['value'] for r in records],fitted['parameters'],
                    initial_variance=fitted['initial_variance'],segment_starts=_segments(records))
                def forecast(r, *, fitted=fitted,filtered=filtered,student=student):
                    i=_minute_index(r,records);kwargs=dict(current_variance=float(filtered[i+1]),
                        paths=paths,seed=_seed(seed,r))
                    if student:return student_t_garch_settlement_probability(r,fitted['parameters'],
                        degrees_of_freedom=fitted['degrees_of_freedom'],
                        current_minute_observations=r.get('current_minute_observations'),**kwargs)
                    return garch_settlement_probability(r,fitted['parameters'],
                        minute_start_price=r.get('minute_start_price'),**kwargs)
                evaluate(names[0],forecast,dict(kind='native_student_t_garch11' if student else 'native_gaussian_garch11',
                    fit=fitted,variance_units='squared_minute_log_return',paths=paths,seed=seed,
                    return_fit_half_life_days=7.,scale=1.))
                if not student:
                    def moment(r, *, fitted=fitted,filtered=filtered):
                        return expected_variance_probability(r,fitted['parameters'],
                            current_variance=float(filtered[_minute_index(r,records)+1]),
                            minute_start_price=r.get('minute_start_price'))['probability_yes']
                    evaluate('garch_moment_gaussian',moment,dict(kind='native_expected_garch_variance_gaussian',
                        fit=fitted,variance_units='squared_minute_log_return',exact_multistep_probability=False))
            except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
                unavailable(names,error,'failed_fit')

    try:
        proxy=[dict(r,fast_sigma=_sigmas(r)[0],slow_sigma=_sigmas(r)[1]) for r in train]
        spec=train_candidates(proxy,half_lives=(300,),paths=paths,seed=seed)[-1]
        evaluate('sv_ou',lambda r:sv_probability(r,spec,audit=True),dict(kind='native_ou_log_variance_mixture',
            spec=spec.to_dict(),selection='predeclared_half300_full_training_proxy_innovation',
            numerical_method='native_grid_Rao_Blackwell_Taylor_with_truncation_bound'))
    except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
        unavailable(['sv_ou'],error,'failed_fit')

    try:
        from .uniform_benchmark_data import native_seconds_for_asset
        seconds,second_audit=native_seconds_for_asset(dataset,archive)
        if not isinstance(seconds,dict) or not len(seconds['times']):
            raise ValueError('Native one-second source inputs are unavailable')
    except (ValueError,TypeError,KeyError,OSError,ImportError,RuntimeError) as error:
        unavailable(['dynamic_vol_drift','har_rv','fhs_1','fhs_5','fhs_15'],error)
        return result

    try:
        fitted=_fit_drift(train,seconds,first,last)
        evaluate('dynamic_vol_drift',lambda r:dynamic_probability(r,drift=fitted),
            dict(kind='native_dynamic_variance_ridge_drift',fit=fitted,source_seconds=second_audit))
    except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
        unavailable(['dynamic_vol_drift'],error,'failed_fit')

    try:
        from .har_rv import causal_har_features,fit_har,predict_har
        from .variance_data import realized_variance_target
        if records is None:raise ValueError('Native minute histories unavailable for HAR')
        examples=[]
        for row in train:
            feat=causal_har_features(records,_minute_index(row,records),as_of=row['model_time'])
            if feat['first_return_start']<first:continue
            target=realized_variance_target(row,seconds,training_cutoff=last)
            examples.append(dict(as_of=row['model_time'],features=feat,**target))
        fitted=fit_har(examples,first_time=first,last_time=last,ridge=.01,half_life_days=7.)
        def har(r):
            feat=causal_har_features(records,_minute_index(r,records),as_of=r['model_time'])
            return flat_probability(r,predict_har(feat,fitted))
        evaluate('har_rv',har,dict(kind='native_intraday_har_gamma_qlike',fit=fitted,
            variance_units='squared_minute_log_return',source_seconds=second_audit))
    except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
        unavailable(['har_rv'],error,'failed_fit')

    try:
        from .fhs import fit_fhs_pool,FixingFunctionalBank,fhs_probability
        selected=(seconds['times']-1>=first)&(seconds['times']<=last)
        fitting_seconds={k:np.asarray(v)[selected] for k,v in seconds.items()
                         if isinstance(v,np.ndarray) and v.shape==seconds['times'].shape}
        pool,_=fit_fhs_pool(fitting_seconds,first_time=first,last_time=last,block_seconds=(1,5,15),
            window_seconds=3600,half_life_seconds=300.,block_half_life_days=7.)
        metadata=pool.metadata()
        cache_key=hashlib.sha256(json.dumps(metadata['arrays_sha256'],sort_keys=True).encode()).hexdigest()
        _FROZEN_FHS_POOLS[cache_key]=pool
        while len(_FROZEN_FHS_POOLS)>8:_FROZEN_FHS_POOLS.pop(next(iter(_FROZEN_FHS_POOLS)))
        for length in (1,5,15):
            bank=FixingFunctionalBank(pool,block_seconds=length,paths=paths,seed=seed)
            def forecast(r, *, bank=bank):
                return fhs_probability(r,variance_per_second=_sigmas(r)[1]**2,
                    bank=bank,gaussian_weight=.25,probability_floor=None)
            evaluate('fhs_'+str(length),forecast,dict(kind='native_ewma_filtered_historical_blocks',
                block_seconds=length,paths=paths,seed=seed,gaussian_weight=.25,
                variance_units='squared_log_return_per_second',pool_metadata=metadata,frozen_pool_cache_key=cache_key,
                source_seconds=second_audit,live_loader_compatible=False,
                pool_arrays_in_artifact=False,pool_rebuild_required_for_new_predictions=True))
    except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
        unavailable(['fhs_1','fhs_5','fhs_15'],error,'failed_fit')
    return result


def numerical_audit(dataset, rows, candidates, *, paths=4096, seed=1008, archive=None):
    """Replay frozen process fits on caller-selected, label-independent origins.

    Never fits parameters, normalizers, pools, maps or selections. An FHS pool
    must remain in the bounded process-local frozen cache from fit_models; a
    metadata-only exported artifact cannot silently reconstruct/refit that pool.
    The caller freezes probability maps and the origin subset before this check.
    """
    if (type(paths) is not int or not 128<=paths<=65536 or paths%2
            or type(seed) is not int or not 0<=seed<=2**32-1
            or not isinstance(rows,(list,tuple)) or not 1<=len(rows)<=1000
            or not isinstance(candidates,dict)):
        raise ValueError('Use bounded independent replay paths, seed, rows and frozen candidates')
    result={}
    records=dataset.get('minute_returns')
    for name,candidate in candidates.items():
        if name not in ('garch_gaussian','garch_student_t','sv_ou','fhs_1','fhs_5','fhs_15'):
            continue
        entry=dict(status='not_supported',reason=None,probabilities=[],paths=paths,seed=seed,
            parameters_refitted=False,calibration_refitted=False,pool_refitted=False,
            subset_selection_owned_by_caller=True,official_labels_used=False)
        result[name]=entry
        if candidate.get('status')!='evaluated':
            entry['reason']='The primary fitted candidate did not evaluate';continue
        artifact=candidate.get('artifact',{})
        try:
            if any(r['model_time']<=artifact['fitting_end'] for r in rows):
                raise ValueError('A replay origin predates the frozen parameter cutoff')
            if name.startswith('garch_'):
                if not isinstance(records,list):raise ValueError('Native completed minute records unavailable')
                fitted=artifact['fit']
                filtered=filter_garch([r['value'] for r in records],fitted['parameters'],
                    initial_variance=fitted['initial_variance'],segment_starts=_segments(records))
                def predictor(row):
                    kwargs=dict(current_variance=float(filtered[_minute_index(row,records)+1]),
                                paths=paths,seed=_seed(seed,row))
                    if name=='garch_student_t':
                        return student_t_garch_settlement_probability(row,fitted['parameters'],
                            degrees_of_freedom=fitted['degrees_of_freedom'],
                            current_minute_observations=row.get('current_minute_observations'),**kwargs)
                    return garch_settlement_probability(row,fitted['parameters'],
                        minute_start_price=row.get('minute_start_price'),**kwargs)
            elif name=='sv_ou':
                if paths>4096:
                    entry['reason']='The frozen OU numerical kernel supports at most 4096 paths';continue
                from .stochastic_volatility import SVSpec
                raw=artifact['spec']
                spec=SVSpec(**{k:raw[k] for k in ('variance_half_life_seconds','log_variance_volatility',
                    'fast_weight','max_step_seconds','max_grid_points')},paths=paths,seed=seed)
                predictor=lambda row:sv_probability(row,spec,audit=True)
            else:
                from .fhs import FixingFunctionalBank,fhs_probability
                key=artifact.get('frozen_pool_cache_key')
                if key not in _FROZEN_FHS_POOLS:
                    entry['reason']='The exact frozen FHS array pool is not resident; metadata alone cannot replay it';continue
                pool=_FROZEN_FHS_POOLS[key]
                if pool.metadata()['arrays_sha256']!=artifact['pool_metadata']['arrays_sha256']:
                    raise ValueError('Frozen FHS pool identity differs from the fitted artifact')
                bank=FixingFunctionalBank(pool,block_seconds=artifact['block_seconds'],paths=paths,seed=seed)
                predictor=lambda row:fhs_probability(row,variance_per_second=_sigmas(row)[1]**2,
                    bank=bank,gaussian_weight=artifact['gaussian_weight'],probability_floor=None)
                entry['frozen_pool_arrays_sha256']=artifact['pool_metadata']['arrays_sha256']
            estimates=[predictor(row) for row in rows]
            probabilities=[float(e['probability_yes']) for e in estimates]
            if any(not math.isfinite(p) or not 0<=p<=1 for p in probabilities):
                raise ValueError('A frozen replay returned an invalid probability')
            entry.update(status='evaluated',probabilities=probabilities,
                conditional_monte_carlo_se=[float(e.get('monte_carlo_se',0.)) for e in estimates])
        except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
            entry.update(status='failed_replay',reason=str(error))
    return result


def fit_drift_candidate(dataset, parts, *, archive=None, progress=None):
    """Repair/replay only the native drift arm under the original frozen split.

    This is an isolated adapter entry point, not a new fitting method or grid.
    Named-source feature dictionaries are normalized in FEATURE_NAMES order.
    No other process model, calibration map or selection is touched.
    """
    notify=(lambda *a,**k:None) if progress is None else progress
    result=dict(status='missing_input',reason=None,family=MODEL_FAMILIES['dynamic_vol_drift'],
                probabilities={},artifact={})
    train=parts.get('model_train',[])
    if not train:
        result.update(status='insufficient_training',reason='The common model-training block is empty')
        return result
    first=min(r['model_time'] for r in train);last=max(r['close_time'] for r in train)
    common=dict(execution_eligible=False,offline_only=True,settlement_grid='T-60,...,T-1',
        fit_stage='model_train',fitting_start=first,fitting_end=last,parameters_use_test=False,
        source_time_reconstruction=True,source_receipt_eligibility_enforced_by_caller=True,
        official_labels_fit_process=False)
    result['artifact']=common
    try:
        from .uniform_benchmark_data import native_seconds_for_asset
        seconds,second_audit=native_seconds_for_asset(dataset,archive)
    except (ValueError,TypeError,KeyError,OSError,ImportError,RuntimeError) as error:
        result['reason']=str(error);return result
    try:
        fitted=_fit_drift(train,seconds,first,last)
        probabilities={}
        for stage in PREDICTION_STAGES:
            rows=parts.get(stage,[])
            if any(r['model_time']<=last for r in rows):
                raise ValueError('All process-model predictions must follow frozen training parameters')
            values=[float(dynamic_probability(r,drift=fitted)) for r in rows]
            if any(not math.isfinite(p) or not 0<=p<=1 for p in values):
                raise ValueError('A native process forecast is not a probability')
            probabilities[stage]=values
        artifact=dict(common,kind='native_dynamic_variance_ridge_drift',fit=fitted,source_seconds=second_audit)
        result.update(status='evaluated',reason=None,probabilities=probabilities,artifact=_json(artifact))
        notify('model_evaluated',model='dynamic_vol_drift')
    except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError,np.linalg.LinAlgError) as error:
        reason=str(error)
        status='insufficient_training' if 'Insufficient' in reason or 'At least' in reason else 'failed_fit'
        result.update(status=status,reason=reason)
    return result
