"""Small residual neural settlement classifier; comparison only, no execution imports.

The Gaussian skip connection uses the exact T-60,...,T-1 rounded average.
Inference is standard-library-only. Training and portable input provenance are
separate concerns: an artifact hash proves consistency, not predictive skill.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

from .crypto_assets import asset_spec, asset_from_ticker
from .forecast import ForecastConfig, _volatility
from .preclose_probability import preclose_probability, _grid

MODEL_KIND = 'kalshi_neural_comparison_model'
MODEL_ID = 'preclose_residual_mlp_5x8_v1'
GRID = 'preclose_60_seconds_v1'
FEATURE_NAMES = ('baseline_logit', 'remaining_fraction', 'fixed_fraction',
                 'fast_slow_log_sigma_ratio', 'log_slow_sigma')
FEATURE_SCHEMA = dict(version=1, names=list(FEATURE_NAMES), probability_clip=1e-6,
    standardized_clip=8., log_sigma_floor=1e-12, log_ratio_clip=10.,
    time_unit='seconds', volatility_unit='log_return_per_sqrt_second',
    fast_ewma_half_life_seconds=60., slow_ewma_half_life_seconds=300.,
    native_source_grid='complete_integer_seconds_1800_span',
    residual_formula='4*tanh((v@tanh(W@x+b)+c)/4)')
CONFIG = ForecastConfig(min_history_seconds=300)


class NeuralModelError(ValueError):
    """Invalid comparison evidence or bounded model artifact."""


def number(value, name, *, lower=-1e12, upper=1e12):
    if type(value) not in (int,float) or not math.isfinite(value) or not lower<=value<=upper:
        raise NeuralModelError('Invalid neural comparison '+name)
    return float(value)


def digest(raw):
    return hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def baseline_definition(asset):
    profile=asset_spec(asset)
    return dict(model_id='fixed_preclose_gaussian_ewma300_history1800_scale1_v1',
        asset=asset,source=profile.source,settlement_grid=GRID,payoff='rounded_official_terms',
        volatility_scale=1.,round_digits=2 if asset in ('BTC','ETH') else 4,
        comparators=['greater','greater_or_equal'],forecast_config=asdict(CONFIG))


def sigmoid(value):
    if value>=0:return 1/(1+math.exp(-value))
    e=math.exp(value)
    return e/(1+e)


def logit(p):
    p=min(1-1e-6,max(1e-6,p))
    return math.log(p)-math.log1p(-p)


def feature_state(*,asset,ticker,as_of,model_time,close_time,p_gaussian,known_count,
                  fast_sigma,slow_sigma,deterministic=False):
    profile=asset_spec(asset)
    if asset_from_ticker(ticker).symbol!=asset:
        raise NeuralModelError('The neural asset and contract differ')
    as_of=number(as_of,'as-of clock',lower=0,upper=253402300799)
    clock=number(model_time,'source clock',lower=0,upper=253402300799)
    close=number(close_time,'close clock',lower=0,upper=253402300799)
    if not clock<=as_of<close or as_of-clock>5 or not 0<close-clock<=900 or int(clock)!=clock or int(close)!=close:
        raise NeuralModelError('Neural comparison clocks are stale or outside the exact contract grid')
    fixed=sum(close-offset<=clock for offset in range(60,0,-1))
    if type(known_count) is not int or known_count!=fixed or type(deterministic) is not bool:
        raise NeuralModelError('Neural fixing count or deterministic state differs')
    p=number(p_gaussian,'baseline probability',lower=0,upper=1)
    fast=number(fast_sigma,'fast volatility',lower=0,upper=1)
    slow=number(slow_sigma,'slow volatility',lower=0,upper=1)
    if (fast==0)!=(slow==0):
        raise NeuralModelError('One-sided zero volatility cannot define the comparison regime')
    if deterministic!=(slow==0 or fixed==60) or deterministic and p not in (0,1):
        raise NeuralModelError('The deterministic Gaussian state is inconsistent')
    ratio=0. if slow==0 else max(-10.,min(10.,math.log(fast)-math.log(slow)))
    vector=(logit(p),(close-clock)/900.,known_count/60.,ratio,math.log(max(1e-12,slow)))
    return dict(asset=asset,source=profile.source,ticker=ticker,as_of=as_of,model_time=clock,
        close_time=close,p_gaussian=p,known_count=known_count,fast_sigma=fast,slow_sigma=slow,
        deterministic=deterministic,features=list(vector),baseline_sha256=digest(baseline_definition(asset)))


def historical_state(row):
    # Recompute the Gaussian probability from bound original terms and exact
    # known fixings. Never trust an input's supplied p_raw/p_gaussian field.
    from .preclose_history import predict_preclose_gaussian
    if not isinstance(row,dict):raise NeuralModelError('An original preclose state is required')
    prediction=predict_preclose_gaussian(row)
    if (row.get('synthetic',False) is not True
            and row['payoff']['round_digits']!=baseline_definition(row['asset'])['round_digits']):
        raise NeuralModelError('The historical neural payout rounding differs')
    return feature_state(asset=row['asset'],ticker=row['ticker'],as_of=row['as_of'],
        model_time=row['model_time'],close_time=row['close_time'],p_gaussian=prediction['probability_yes'],
        known_count=row['known_count'],fast_sigma=row['fast_sigma'],slow_sigma=row['slow_sigma'],
        deterministic=prediction['variance']==0)


def _vector(values,size,name):
    if not isinstance(values,(list,tuple)) or len(values)!=size:
        raise NeuralModelError('Invalid neural '+name+' shape')
    return tuple(number(v,name,lower=-32,upper=32) for v in values)


def validate_artifact(raw):
    keys={'kind','version','model_id','asset','source','baseline_definition','baseline_sha256',
          'feature_schema','standardization','network','calibration','synthetic','comparison_only',
          'execution_eligible','live_validated','created_at','evidence','artifact_sha256'}
    if (not isinstance(raw,dict) or set(raw)!=keys or raw.get('kind')!=MODEL_KIND
            or type(raw.get('version')) is not int or raw['version']!=1 or raw.get('model_id')!=MODEL_ID
            or raw.get('comparison_only') is not True or raw.get('execution_eligible') is not False
            or raw.get('live_validated') is not False or type(raw.get('synthetic')) is not bool):
        raise NeuralModelError('A version-one comparison-only neural artifact is required')
    profile=asset_spec(raw.get('asset'))
    definition=baseline_definition(profile.symbol)
    if (raw['source']!=profile.source or raw['baseline_definition']!=definition
            or raw['baseline_sha256']!=digest(definition) or raw['feature_schema']!=FEATURE_SCHEMA):
        raise NeuralModelError('The neural baseline, source or feature schema differs')
    if raw['artifact_sha256']!=digest({k:v for k,v in raw.items() if k!='artifact_sha256'}):
        raise NeuralModelError('The neural artifact digest differs')
    number(raw['created_at'],'artifact availability clock',lower=1,upper=253402300799)
    if not isinstance(raw['evidence'],dict):raise NeuralModelError('Neural fit evidence is required')
    scalers=raw['standardization']
    if not isinstance(scalers,dict) or set(scalers)!={'means','scales'}:
        raise NeuralModelError('Invalid neural standardization')
    _vector(scalers['means'],5,'means')
    scales=_vector(scalers['scales'],5,'scales')
    if any(v<1e-8 for v in scales):raise NeuralModelError('Neural scales must be positive')
    net=raw['network']
    if (not isinstance(net,dict) or set(net)!={'hidden_units','input_weights','hidden_bias','output_weights','output_bias','residual_bound'}
            or type(net['hidden_units']) is not int or net['hidden_units']!=8 or net['residual_bound']!=4.):
        raise NeuralModelError('The neural network must have five inputs and eight hidden units')
    if not isinstance(net['input_weights'],list) or len(net['input_weights'])!=8:
        raise NeuralModelError('Invalid neural input layer shape')
    for row in net['input_weights']:_vector(row,5,'input weights')
    _vector(net['hidden_bias'],8,'hidden biases');_vector(net['output_weights'],8,'output weights')
    number(net['output_bias'],'output bias',lower=-32,upper=32)
    from .probability_calibration import calibrated_probability
    mapping=raw['calibration']
    if not isinstance(mapping,dict) or set(mapping)!={'kind','intercept','slope'}:
        raise NeuralModelError('Invalid neural probability calibration')
    if mapping['kind'] not in ('identity','logistic'):
        raise NeuralModelError('Unsupported neural probability calibration')
    number(mapping['intercept'],'map intercept',lower=-30,upper=30)
    number(mapping['slope'],'map slope',lower=0,upper=30)
    if mapping['kind']=='identity' and (mapping['intercept']!=0 or mapping['slope']!=1):
        raise NeuralModelError('Invalid identity calibration')
    calibrated_probability(.5,mapping)
    return deepcopy(raw)


def load_artifact(path,*,asset=None):
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise NeuralModelError('Duplicate neural artifact fields')
            result[k]=v
        return result
    path=Path(path)
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise NeuralModelError('Neural artifact paths cannot use symbolic links')
    with path.open('rb') as stream:encoded=stream.read(256001)
    if len(encoded)>256000:raise NeuralModelError('The neural artifact exceeds its size bound')
    try:
        raw=json.loads(encoded,object_pairs_hook=unique,
            parse_constant=lambda _:(_ for _ in ()).throw(NeuralModelError('Neural JSON must be finite')))
        raw=validate_artifact(raw)
    except (json.JSONDecodeError,RecursionError,OverflowError):
        raise NeuralModelError('Invalid bounded neural artifact JSON') from None
    if asset is not None and raw['asset']!=asset:
        raise NeuralModelError('The neural artifact belongs to another asset')
    return NeuralModel(raw)


class NeuralModel:
    """Validated immutable-by-interface parameters; label-free scalar inference."""
    def __init__(self,raw):
        self._raw=validate_artifact(raw)
        self.asset,self.synthetic,self.created_at=raw['asset'],raw['synthetic'],raw['created_at']
        self.artifact_sha256=raw['artifact_sha256']
        self._means=tuple(raw['standardization']['means']);self._scales=tuple(raw['standardization']['scales'])
        net=raw['network']
        self._weights=tuple(tuple(r) for r in net['input_weights'])
        self._bias=tuple(net['hidden_bias']);self._output=tuple(net['output_weights']);self._output_bias=net['output_bias']
        self._map=deepcopy(raw['calibration'])

    def to_dict(self):return deepcopy(self._raw)

    def predict(self,state,*,now=None,offline=False):
        if not isinstance(state,dict):raise NeuralModelError('A causal feature state is required')
        checked=feature_state(**{key:state[key] for key in ('asset','ticker','as_of','model_time','close_time',
            'p_gaussian','known_count','fast_sigma','slow_sigma','deterministic')})
        if (checked['asset']!=self.asset or state.get('baseline_sha256')!=self._raw['baseline_sha256']
                or checked['features']!=state.get('features') or state.get('source')!=checked['source']):
            raise NeuralModelError('The comparison input differs from its bound feature schema')
        if not offline:
            now=number(now,'current comparison clock',lower=0,upper=253402300799)
            if self.synthetic or now<self.created_at or not 0<=now-state['as_of']<=5 or not 0<=now-state['model_time']<=5 or now>=state['close_time']:
                raise NeuralModelError('The neural artifact or source is synthetic, future-dated or stale')
        p=checked['p_gaussian']
        standardized=tuple(max(-8.,min(8.,(x-m)/s)) for x,m,s in zip(checked['features'],self._means,self._scales))
        clipped=any(abs((x-m)/s)>8 for x,m,s in zip(checked['features'],self._means,self._scales))
        correction=0.
        if not checked['deterministic']:
            hidden=[math.tanh(math.fsum(w*x for w,x in zip(row,standardized))+b) for row,b in zip(self._weights,self._bias)]
            raw=math.fsum(v*h for v,h in zip(self._output,hidden))+self._output_bias
            correction=4*math.tanh(raw/4)
            p=sigmoid(logit(p)+correction)
            from .probability_calibration import calibrated_probability
            calibrated=calibrated_probability(p,self._map)
        else:calibrated=p
        return dict(probability_yes=calibrated,probability_no=1-calibrated,raw_probability_yes=p,
            gaussian_probability_yes=checked['p_gaussian'],logit_correction=correction,
            calibrated=self._map['kind']=='logistic',standardized_features_clipped=clipped,
            selected_outcome='yes' if calibrated>.5 else 'no' if calibrated<.5 else 'tie',
            artifact_sha256=self.artifact_sha256,execution_eligible=False)


def live_comparison(market,observations,now,model,*,asset):
    """Both forecasts use one received source window; no account or order calls."""
    result=dict(kind='kalshi_neural_live_comparison',version=1,status='unavailable',asset=asset,
        as_of=now,model_time=None,ticker=market.get('ticker') if isinstance(market,dict) else None,
        synthetic=False,comparison_only=True,execution_eligible=False,probability_yes=None,
        reason='Neural comparison inputs are unavailable')
    try:
        if not isinstance(model,NeuralModel) or model.asset!=asset:
            raise NeuralModelError('A matching validated neural model is required')
        baseline=preclose_probability(market,observations,now,CONFIG,asset=asset,volatility_scale=1.)
        if baseline['status']!='ready':raise NeuralModelError(baseline['reason'])
        if baseline['payoff_metadata']['round_digits']!=baseline_definition(asset)['round_digits']:
            raise NeuralModelError('The neural contract payout rounding differs')
        points=_grid(observations,now,CONFIG,asset)
        clock=baseline['model_time']
        fast=_volatility(points,clock,ForecastConfig(ewma_half_life_seconds=60),asset=asset)[0]
        slow=baseline['parameters']['raw_log_volatility_per_sqrt_second']
        state=feature_state(asset=asset,ticker=baseline['ticker'],as_of=now,model_time=clock,
            close_time=baseline['parameters']['close_time'],p_gaussian=baseline['probability_yes'],
            known_count=baseline['parameters']['known_count'],fast_sigma=fast,slow_sigma=slow,
            deterministic=baseline['diagnostics']['deterministic'])
        neural=model.predict(state,now=now)
        result.update(status='ready',model_time=clock,reason='Read-only neural/Gaussian settlement comparison',
            **neural,baseline_definition=baseline_definition(asset),baseline_sha256=state['baseline_sha256'],
            feature_state=state,settlement_grid=GRID,source=asset_spec(asset).source,
            latest_received_at=baseline['diagnostics']['latest_received_at'],
            known_count=state['known_count'],future_count=60-state['known_count'],
            gaussian_preclose_proof=baseline['preclose_proof'],
            probability_difference=neural['probability_yes']-baseline['probability_yes'])
    except (ValueError,KeyError,TypeError,ArithmeticError) as error:
        result['reason']=str(error) if isinstance(error,ValueError) else 'Invalid neural comparison evidence'
    return result
