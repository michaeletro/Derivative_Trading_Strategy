"""Fixed, proof-carrying rounded preclose probabilities for isolated paper use.

This separately versioned model leaves every production estimator unchanged.
Source receipts remain actual receipts. Proof hashes establish reproducible
self-consistency, not authentication of the underlying feed or trading authority.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re

from .crypto_assets import asset_spec
from .forecast import ForecastConfig, IndexObservation, _finite, _market, _timestamp, _volatility
from .population_probability import _source_grid
from .settlement_payoff import RoundedSettlementPayoff, verified_rounded_payoff


SETTLEMENT_GRID = 'preclose_60_seconds_v1'
MAX_FORECAST_BYTES = 1_000_000
_CONFIG = ForecastConfig(min_history_seconds=300)
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NORMALIZED = 'normalized_float_decimal_spelling'
_NATIVE = 'caller_supplied_native_decimal_values'
_PROOF_FIELDS = {'kind','version','asset','source','model_definition_sha256','as_of',
    'model_time','source_grid','source_grid_sha256','exact_source_prices','price_precision',
    'market','market_sha256','payoff_metadata','settlement_grid','known_sum','known_count',
    'future_offsets','log_volatility_per_sqrt_second','state_sha256'}


class PrecloseProbabilityError(ValueError):
    """Invalid native source, fixed paper configuration or scheduled proof."""


def _canonical(value):
    try:
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw.encode('utf-8'))>MAX_FORECAST_BYTES:
            raise ValueError()
        return raw
    except (ValueError,TypeError,RecursionError,OverflowError):
        raise PrecloseProbabilityError('Preclose paper evidence must be bounded finite JSON') from None


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def preclose_model_version(asset='BTC',*,scheduled=False):
    profile=asset_spec(asset)
    return 'cf-'+profile.index_id.lower().replace('_','-')+(
        '-minute-five-preclose-paper-v1' if scheduled else '-frozen-preclose-paper-rounded-v1')


def preclose_runtime_definition(asset='BTC'):
    profile=asset_spec(asset)
    return dict(model_version=preclose_model_version(asset,scheduled=True),config=dict(
        estimator='preclose_gaussian',settlement_grid=SETTLEMENT_GRID,payoff_mode='verified_rounded',
        source=profile.source,volatility=dict(ewma_half_life_seconds=300,volatility_scale=1.0,
            history_seconds=1800,source_grid='complete_native_seconds')))


def _number(value,name,*,minimum=0,maximum=253402300799):
    if not _finite(value) or not minimum<=value<=maximum:
        raise PrecloseProbabilityError('Invalid preclose paper '+name)
    return float(value)


def _decimal(value,name,*,zero=False):
    if type(value) not in (int,float,str,Decimal):
        raise PrecloseProbabilityError('Invalid preclose paper '+name)
    try:
        result=Decimal(str(value))
        if not result.is_finite() or result<0 or not zero and result==0:
            raise ValueError()
        return result
    except (ValueError,InvalidOperation):
        raise PrecloseProbabilityError('Invalid preclose paper '+name) from None


def _fixed_configuration(config):
    config=_CONFIG if config is None else config
    if (not isinstance(config,ForecastConfig) or config.ewma_half_life_seconds!=300
            or config.max_history_seconds!=1800 or config.min_returns!=30
            or config.max_age_seconds!=5 or config.sample_seconds!=1):
        raise PrecloseProbabilityError('Fixed preclose paper inference requires EWMA 300 seconds, unit scale, complete 1800-second history and five-second freshness')
    return config


def _identity(market):
    result={key:market.get(key) for key in ('ticker','event_ticker','market_type','open_time',
        'close_time','floor_strike','cap_strike','strike_type','notional_value_dollars','settlement_bounds_type')}
    for key in ('floor_strike','notional_value_dollars'):
        result[key]=str(result[key])
    return result


def _contract(market,now,asset):
    _,closed=_market(market,now,asset=asset)
    opened=_timestamp(market['open_time'])
    if (not opened.is_integer() or not closed.is_integer() or market.get('market_type')!='binary'
            or market.get('settlement_bounds_type')!='default'
            or _decimal(market.get('notional_value_dollars'),'notional')!=1
            or market.get('status') not in ('active','open')):
        raise PrecloseProbabilityError('Preclose paper inference requires an active standard one-dollar fifteen-minute binary')
    return opened,closed


def preclose_gaussian_state(spot,sigma,known_sum,future_offsets,payoff):
    """Pure rounded Gaussian law; no labels, receipt clocks or source claims.

    Historical equivalence studies can call this function without manufacturing
    old operational availability. The caller separately verifies grid and payoff.
    """
    if not isinstance(payoff,RoundedSettlementPayoff):
        raise PrecloseProbabilityError('A verified rounded payoff object is required')
    spot=_decimal(spot,'spot')
    known=_decimal(known_sum,'known sum',zero=True)
    if spot>1_000_000_000 or known>60_000_000_000:
        raise PrecloseProbabilityError('Preclose paper source values exceed the numerical bound')
    sigma=_number(sigma,'log volatility',maximum=1)
    if (not isinstance(future_offsets,(list,tuple)) or len(future_offsets)>60
            or any(not _finite(value) or not 0<value<=900 for value in future_offsets)
            or any(second<=first for first,second in zip(future_offsets,future_offsets[1:]))):
        raise PrecloseProbabilityError('Future preclose paper offsets must be positive and strictly increasing')
    count=len(future_offsets)
    if count==60 and known!=0 or count<60 and known<=0:
        raise PrecloseProbabilityError('The known preclose paper sum differs from its sample count')
    with localcontext() as context:
        context.prec=60
        mean=(known+count*spot)/60
        centered=mean-payoff.effective_strike
    covariance=math.fsum((2*(count-index)-1)*value for index,value in enumerate(future_offsets))
    dollar_volatility=float(spot)*sigma
    variance=dollar_volatility*dollar_volatility*covariance/3600
    if not math.isfinite(variance) or variance<0:
        raise PrecloseProbabilityError('The preclose paper variance exceeds its numerical range')
    z=float(centered)/math.sqrt(variance) if variance else None
    probability=.5*math.erfc(-z/math.sqrt(2)) if variance else float(payoff.deterministic_outcome(mean))
    result=dict(probability_yes=probability,probability_no=1-probability,mean=float(mean),
        centered_mean=float(centered),variance=variance,covariance_time_sum=covariance,
        dollar_volatility=dollar_volatility,denominator=60*math.sqrt(variance),z=z,
        known_count=60-count,future_count=count)
    if any(not math.isfinite(value) for key,value in result.items() if value is not None):
        raise PrecloseProbabilityError('The preclose paper sufficient state is not finite')
    return result


def _exact_values(points,supplied):
    if supplied is not None and (not isinstance(supplied,dict) or len(supplied)>100000):
        raise PrecloseProbabilityError('Exact preclose prices must be a bounded timestamp mapping')
    values=[]
    for point in points:
        if supplied is not None and point.event_time not in supplied:
            raise PrecloseProbabilityError('The complete native grid lacks an exact source price')
        raw=point.price if supplied is None else supplied[point.event_time]
        value=_decimal(raw,'exact source price')
        if value>1_000_000_000 or float(value)!=point.price:
            raise PrecloseProbabilityError('An exact source price differs from its normalized native observation')
        values.append(value)
    return values,_NORMALIZED if supplied is None else _NATIVE


def _unavailable(market,now,asset):
    return dict(kind='kalshi_live_probability',version=1,status='unavailable',asset=asset,source=None,
        execution_eligible=False,calibrated=False,model_version=None,model_definition=None,model_fingerprint=None,
        decision_model='preclose',decision_estimator='preclose_gaussian',
        reason='Fixed preclose paper Gaussian probability is unavailable',
        ticker=market.get('ticker') if isinstance(market,dict) else None,
        as_of=now if _finite(now) and 0<=now<=253402300799 else None,model_time=None,
        probability_yes=None,probability_no=None,analytic_probability_yes=None,
        monte_carlo_se=None,sampling_interval=None,preclose_proof=None,
        payoff_mode='verified_rounded',settlement_grid=SETTLEMENT_GRID,
        parameters={},diagnostics={},time={},greeks={},no_greeks={},
        probability_paths=[],probability_fan=[],sensitivity_fan=[],
        assumptions=[
            'Separate paper-only fixed zero-drift arithmetic Gaussian model, EWMA 300 seconds and scale one.',
            'Exactly sixty T-60,...,T-1 samples; the close tick is excluded.',
            'Complete native-second 1800-second history and actual source receipt clocks are required.',
            'Current source documents verify the rounded payout; unspecified deterministic half-ties are withheld.',
            'Caller-supplied native decimal values are checked against normalized observations when available.',
            'Hashes establish reproducible self-consistency, not source authentication or execution eligibility.'])


def preclose_probability(market,observations,now,config=None,*,asset='BTC',exact_prices=None):
    """Compute the fixed rounded preclose law without changing production models."""
    result=_unavailable(market,now,asset)
    try:
        profile=asset_spec(asset)
        definition=preclose_runtime_definition(asset)
        result.update(asset=asset,source=profile.source,model_version=preclose_model_version(asset),
            model_definition=definition,model_fingerprint=_digest(definition))
        _number(now,'as-of timestamp')
        config=_fixed_configuration(config)
        opened,closed=_contract(market,now,asset)
        payoff=verified_rounded_payoff(market,asset=asset)
        _canonical(market)
        points=_source_grid(observations,now,config,asset)
        latest=points[-1]
        model_time=latest.event_time
        if model_time<opened:
            raise PrecloseProbabilityError('A current-contract native-second preclose paper tick is required')
        exact,precision=_exact_values(points,exact_prices)
        by_time={point.event_time:value for point,value in zip(points,exact)}
        sigma,return_count,effective_count=_volatility(points,model_time,config,asset=asset)
        grid=[closed-offset for offset in range(60,0,-1)]
        fixed=[stamp for stamp in grid if stamp<=model_time]
        if any(stamp not in by_time for stamp in fixed):
            raise PrecloseProbabilityError('An exact past preclose paper settlement tick is missing')
        with localcontext() as context:
            context.prec=60
            known=sum((by_time[stamp] for stamp in fixed),Decimal(0))
        offsets=[stamp-model_time for stamp in grid if stamp>model_time]
        state=preclose_gaussian_state(by_time[model_time],sigma,known,offsets,payoff)
        triples=[[point.event_time,point.received_at,point.price] for point in points]
        proof=dict(kind='kalshi_preclose_paper_probability_proof',version=1,asset=asset,source=profile.source,
            model_definition_sha256=_digest(definition),as_of=now,model_time=model_time,
            source_grid=triples,source_grid_sha256=_digest(triples),
            exact_source_prices=[format(value,'f') for value in exact],price_precision=precision,
            market=deepcopy(market),market_sha256=_digest(market),payoff_metadata=payoff.metadata(),
            settlement_grid=SETTLEMENT_GRID,known_sum=format(known,'f'),known_count=len(fixed),
            future_offsets=offsets,log_volatility_per_sqrt_second=sigma)
        proof['state_sha256']=_digest(proof)
        result.update(status='ready',reason='Exact fixed rounded preclose Gaussian probability; paper only',
            model_time=model_time,probability_yes=state['probability_yes'],probability_no=state['probability_no'],
            analytic_probability_yes=state['probability_yes'],preclose_proof=proof,payoff_metadata=payoff.metadata(),
            time=dict(window_start=opened,window_end=closed,elapsed_seconds=now-opened,
                      remaining_seconds=closed-now,progress=(now-opened)/900),
            parameters=dict(spot=float(by_time[model_time]),target=str(market['floor_strike']),
                effective_strike=str(payoff.effective_strike),close_time=closed,
                known_sum=format(known,'f'),known_count=len(fixed),future_count=len(offsets),
                raw_log_volatility_per_sqrt_second=sigma,log_volatility_per_sqrt_second=sigma,
                dollar_volatility_per_sqrt_second=state['dollar_volatility'],
                ewma_half_life_seconds=300,volatility_scale=1.0),
            diagnostics=dict(paths=0,decision_estimator='preclose_gaussian',history_seconds=1800,
                volatility_return_count=return_count,effective_return_count=effective_count,
                sample_count=len(points),latest_event_time=model_time,latest_received_at=latest.received_at,
                source_age_seconds=now-model_time,settlement_observed_count=len(fixed),
                settlement_future_count=len(offsets),settlement_average_mean=state['mean'],
                settlement_average_median=state['mean'],settlement_centered_median=state['centered_mean'],
                analytic_settlement_average_variance=state['variance'],covariance_time_sum=state['covariance_time_sum'],
                deterministic=not state['variance'],complete_native_second_grid=True,
                price_precision=precision,parameter_uncertainty_included=False))
        _canonical(result)
    except (ValueError,TypeError,KeyError,OverflowError,ZeroDivisionError) as error:
        result.update(status='unavailable',reason=str(error),probability_yes=None,probability_no=None,
            analytic_probability_yes=None,preclose_proof=None)
    return result


def scheduled_preclose_forecast(market,observations,now,config=None,*,asset='BTC',
                               forecast_completed_at=None,exact_prices=None):
    """Return a uniquely versioned, observation-only scheduled paper frame."""
    model=preclose_probability(market,observations,now,config,asset=asset,exact_prices=exact_prices)
    if model['status']!='ready':
        model['kind']='kalshi_minute_five_forecast'
        return model
    try:
        completed=now if forecast_completed_at is None else _number(forecast_completed_at,'completion clock')
        if (not now<=completed<model['time']['window_end'] or completed-model['model_time']>5
                or completed-model['diagnostics']['latest_received_at']>5):
            raise PrecloseProbabilityError('Preclose paper completion is stale or outside the contract')
        probability=model['probability_yes']
        outcome='yes' if (probability>=.5 if market['strike_type']=='greater_or_equal' else probability>.5) else 'no'
        result=dict(kind='kalshi_minute_five_forecast',version=1,status='ready',asset=asset,source=model['source'],
            model_version=preclose_model_version(asset,scheduled=True),model_definition=model['model_definition'],
            model_fingerprint=model['model_fingerprint'],decision_model='preclose',decision_estimator='preclose_gaussian',
            payoff_mode='verified_rounded',settlement_grid=SETTLEMENT_GRID,as_of=now,model_time=model['model_time'],
            ticker=market['ticker'],probability_yes=probability,probability_no=model['probability_no'],
            analytic_probability_yes=probability,median_outcome=outcome,
            median_settlement_average=model['diagnostics']['settlement_average_median'],
            settlement_centered_median=model['diagnostics']['settlement_centered_median'],
            target_price=str(market['floor_strike']),strike_type=market['strike_type'],contract_identity=_identity(market),
            paths=0,monte_carlo_se=None,sampling_interval=None,preclose_proof=model['preclose_proof'],
            payoff_metadata=model['payoff_metadata'],forecast_completed_at=completed,
            calibrated=False,execution_eligible=False,reason='Fixed rounded preclose Gaussian; isolated paper forecast')
        validate_preclose_forecast(result,asset=asset)
        return result
    except (ValueError,TypeError,KeyError,OverflowError) as error:
        result=_unavailable(market,now,asset)
        result.update(kind='kalshi_minute_five_forecast',reason=str(error))
        return result


def validate_preclose_forecast(forecast,*,asset):
    """Reproduce fixed EWMA, decimal payout and clocks without granting authority."""
    _canonical(forecast)
    definition=preclose_runtime_definition(asset)
    profile=asset_spec(asset)
    if (not isinstance(forecast,dict) or forecast.get('kind')!='kalshi_minute_five_forecast'
            or type(forecast.get('version')) is not int or forecast['version']!=1
            or forecast.get('status')!='ready' or forecast.get('asset')!=asset
            or forecast.get('source')!=profile.source
            or forecast.get('model_version')!=preclose_model_version(asset,scheduled=True)
            or forecast.get('decision_model')!='preclose' or forecast.get('decision_estimator')!='preclose_gaussian'
            or _canonical(forecast.get('model_definition'))!=_canonical(definition)
            or forecast.get('model_fingerprint')!=_digest(definition)
            or forecast.get('payoff_mode')!='verified_rounded' or forecast.get('settlement_grid')!=SETTLEMENT_GRID
            or type(forecast.get('paths')) is not int or forecast['paths']!=0
            or forecast.get('monte_carlo_se') is not None or forecast.get('sampling_interval') is not None
            or forecast.get('execution_eligible') is not False or forecast.get('calibrated') is not False):
        raise PrecloseProbabilityError('The scheduled fixed preclose paper estimator identity differs')
    proof=forecast.get('preclose_proof')
    if (not isinstance(proof,dict) or set(proof)!=_PROOF_FIELDS
            or proof.get('kind')!='kalshi_preclose_paper_probability_proof'
            or type(proof.get('version')) is not int or proof['version']!=1
            or proof.get('asset')!=asset or proof.get('source')!=profile.source
            or proof.get('model_definition_sha256')!=_digest(definition)
            or proof.get('settlement_grid')!=SETTLEMENT_GRID
            or proof.get('price_precision') not in (_NATIVE,_NORMALIZED)):
        raise PrecloseProbabilityError('The fixed preclose paper source proof is incomplete')
    for field in ('source_grid_sha256','market_sha256','state_sha256'):
        if not isinstance(proof.get(field),str) or not _HASH.fullmatch(proof[field]):
            raise PrecloseProbabilityError('Invalid preclose paper evidence digest')
    state=dict(proof);state.pop('state_sha256')
    if proof['state_sha256']!=_digest(state):
        raise PrecloseProbabilityError('The fixed preclose paper state digest differs')
    triples=proof['source_grid']
    exact=proof['exact_source_prices']
    if (not isinstance(triples,list) or len(triples)!=1801
            or any(not isinstance(value,list) or len(value)!=3 for value in triples)
            or not isinstance(exact,list) or len(exact)!=1801 or any(type(value) is not str for value in exact)
            or proof['source_grid_sha256']!=_digest(triples)
            or proof['market_sha256']!=_digest(proof['market'])):
        raise PrecloseProbabilityError('The fixed preclose paper native grid or market digest differs')
    observations=[IndexObservation(*value,source=profile.source) for value in triples]
    supplied={value[0]:price for value,price in zip(triples,exact)} if proof['price_precision']==_NATIVE else None
    reproduced=preclose_probability(proof['market'],observations,proof['as_of'],asset=asset,exact_prices=supplied)
    if reproduced['status']!='ready' or _canonical(reproduced['preclose_proof'])!=_canonical(proof):
        raise PrecloseProbabilityError('The fixed preclose paper source proof does not reproduce')
    market=proof['market']
    if (_canonical(forecast.get('contract_identity'))!=_canonical(_identity(market))
            or forecast.get('ticker')!=market['ticker'] or forecast.get('target_price')!=str(market['floor_strike'])
            or forecast.get('strike_type')!=market['strike_type']
            or forecast.get('as_of')!=proof['as_of'] or forecast.get('model_time')!=proof['model_time']
            or _canonical(forecast.get('payoff_metadata'))!=_canonical(proof['payoff_metadata'])):
        raise PrecloseProbabilityError('The scheduled preclose paper contract or original source clock differs')
    completed=_number(forecast.get('forecast_completed_at'),'completion clock')
    if (not proof['as_of']<=completed<_timestamp(market['close_time'])
            or completed-proof['model_time']>5 or completed-triples[-1][1]>5):
        raise PrecloseProbabilityError('The scheduled preclose paper completion is stale or outside its contract')
    expected=reproduced['probability_yes']
    for name,value in (('probability_yes',expected),('probability_no',1-expected),('analytic_probability_yes',expected)):
        actual=_number(forecast.get(name),name,maximum=1)
        if abs(actual-value)>1e-12:
            raise PrecloseProbabilityError('The scheduled preclose paper probability differs')
    for name,key in (('median_settlement_average','settlement_average_median'),
                     ('settlement_centered_median','settlement_centered_median')):
        actual=_number(forecast.get(name),name,minimum=-1e12,maximum=1e12)
        value=reproduced['diagnostics'][key]
        if abs(actual-value)>1e-12*max(1,abs(value)):
            raise PrecloseProbabilityError('The scheduled preclose paper sufficient state differs')
    outcome='yes' if (expected>=.5 if market['strike_type']=='greater_or_equal' else expected>.5) else 'no'
    if forecast.get('median_outcome')!=outcome:
        raise PrecloseProbabilityError('The scheduled preclose paper direction differs')
    return proof
