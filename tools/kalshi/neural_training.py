"""Offline neural fitting and paired chronological evaluation; NumPy is fit-only."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
import math
import random
import time

from .neural_model import (NeuralModel,NeuralModelError,MODEL_KIND,MODEL_ID,FEATURE_SCHEMA,
    baseline_definition,digest,historical_state,number,logit,GRID)
from .asset_calibration import _partition,_scores,_bootstrap
from .probability_calibration import _fit,calibrated_probability


@dataclass(frozen=True)
class NeuralTrainingConfig:
    epochs: int=300
    learning_rate: float=.01
    l2: float=.001
    seed: int=7
    bootstrap_samples: int=500
    minimum_contracts_per_stage: int=20
    minimum_test_contracts: int=200
    minimum_test_days: int=5
    half_life_contracts: float|None=None

    def __post_init__(self):
        for name,low,high in (('epochs',1,2000),('seed',0,2**32-1),('bootstrap_samples',100,2000),
                ('minimum_contracts_per_stage',20,100000),('minimum_test_contracts',200,100000),('minimum_test_days',5,365)):
            v=getattr(self,name)
            if type(v) is not int or not low<=v<=high:raise NeuralModelError('Invalid neural fit '+name)
        number(self.learning_rate,'learning rate',lower=1e-5,upper=.1)
        number(self.l2,'regularization',lower=1e-6,upper=1)
        if self.half_life_contracts is not None:number(self.half_life_contracts,'recency half-life',lower=1,upper=100000)


def _weights(rows):
    counts=Counter(r['ticker'] for r in rows)
    return [1/len(counts)/counts[r['ticker']] for r in rows]


def _loss_grad(x,offset,y,mass,deterministic,parameters,l2):
    """Full-batch weighted Bernoulli loss and analytic MLP derivatives."""
    import numpy as np
    w,b,v,c=parameters
    hidden=np.tanh(x@w.T+b)
    raw=hidden@v+c
    bounded=np.tanh(raw/4.)
    active=1.-deterministic
    scores=offset+4.*bounded*active
    probability=1/(1+np.exp(-scores))
    loss=np.sum(mass*(np.logaddexp(0,scores)-y*scores))+l2/2*sum(np.sum(a*a) for a in parameters)
    dscore=mass*(probability-y)*active
    draw=dscore*(1-bounded*bounded)
    dhidden=draw[:,None]*v[None,:]*(1-hidden*hidden)
    gradients=(dhidden.T@x+l2*w,np.sum(dhidden,axis=0)+l2*b,
        hidden.T@draw+l2*v,np.sum(draw)+l2*c)
    return float(loss),gradients


def _fit_network(rows,config,cutoff):
    try:import numpy as np
    except ImportError:raise NeuralModelError('Offline neural fitting requires NumPy; inference does not') from None
    raw=np.array([r['state']['features'] for r in rows],dtype=float)
    weights=_weights(rows)
    recency=None
    if config.half_life_contracts is not None:
        from .recency_weights import contract_recency_weights
        weights,recency=contract_recency_weights(rows,fit_as_of=cutoff,
            half_life_contracts=config.half_life_contracts,normalize_to='one')
    mass=np.array(weights,dtype=float)
    means=np.sum(raw*mass[:,None],axis=0)
    scales=np.sqrt(np.sum((raw-means)**2*mass[:,None],axis=0))
    scales=np.where(scales>1e-8,scales,1.)
    x=np.clip((raw-means)/scales,-8,8)
    y=np.array([r['y'] for r in rows],dtype=float)
    offset=np.array([logit(r['state']['p_gaussian']) for r in rows],dtype=float)
    deterministic=np.array([r['state']['deterministic'] for r in rows],dtype=float)
    rng=np.random.default_rng(config.seed)
    params=[rng.normal(0,.15,(8,5)),np.zeros(8),np.zeros(8),np.array(0.)]
    first=[np.zeros_like(p) for p in params];second=[np.zeros_like(p) for p in params]
    initial,_=_loss_grad(x,offset,y,mass,deterministic,params,config.l2)
    best=[p.copy() for p in params];best_loss=initial
    for step in range(1,config.epochs+1):
        _,gradients=_loss_grad(x,offset,y,mass,deterministic,params,config.l2)
        for j,g in enumerate(gradients):
            first[j]=.9*first[j]+.1*g;second[j]=.999*second[j]+.001*g*g
            params[j]=np.clip(params[j]-config.learning_rate*(first[j]/(1-.9**step))/(np.sqrt(second[j]/(1-.999**step))+1e-8),-32,32)
        loss,_=_loss_grad(x,offset,y,mass,deterministic,params,config.l2)
        if not math.isfinite(loss):raise NeuralModelError('The neural optimizer became nonfinite')
        if loss<best_loss:best_loss=loss;best=[p.copy() for p in params]
    w,b,v,c=best
    return dict(means=means.tolist(),scales=scales.tolist()),dict(hidden_units=8,
        input_weights=w.tolist(),hidden_bias=b.tolist(),output_weights=v.tolist(),
        output_bias=float(c),residual_bound=4.),dict(optimizer='deterministic_full_batch_adam_float64',
        epochs=config.epochs,best_training_objective=best_loss,initial_training_objective=initial,
        parameter_count=57,training_only_standardization=True,output_initialized_to_baseline=True,
        recency=recency,selection_uses_training_objective_only=True)


def _qualify(rows,asset):
    if not isinstance(rows,list) or not 1<=len(rows)<=200000:raise NeuralModelError('Provide one through 200000 original preclose rows')
    qualified=[];withheld=Counter();seen=set();identities={};marked=None
    for row in rows:
        if not isinstance(row,dict):raise NeuralModelError('Every original feature row must be an object')
        if row.get('asset')!=asset:continue
        synthetic=row.get('synthetic',False)
        if type(synthetic) is not bool or marked is not None and marked is not synthetic:
            raise NeuralModelError('Synthetic and original historical rows cannot be mixed')
        marked=synthetic
        y=row.get('y')
        if type(y) is not int or y not in (0,1) or any(row.get(k) is not None and row[k]!=y for k in ('officialy','official_y')):
            raise NeuralModelError('The neural training label is not a consistent official binary outcome')
        try:state=historical_state(row)
        except (ValueError,TypeError,KeyError,ArithmeticError):
            withheld['invalid_or_unavailable_exact_preclose_state']+=1;continue
        identity=state['ticker'];key=identity,state['model_time']
        if key in seen:raise NeuralModelError('Duplicate neural forecast origins require reconciliation')
        seen.add(key)
        bound=(state['close_time'],y,synthetic)
        if identity in identities and identities[identity]!=bound:
            raise NeuralModelError('A neural contract has contradictory labels or clocks')
        identities[identity]=bound
        received=number(row.get('label_available_at'),'official label receipt',lower=state['close_time'],upper=253402300799)
        day=datetime.fromtimestamp(state['close_time'],timezone.utc).date().isoformat()
        qualified.append(dict(ticker=identity,as_of=state['as_of'],model_time=state['model_time'],
            close_time=state['close_time'],day=day,y=y,state=state,synthetic=synthetic,
            label_available_at=received,provenance=row['provenance']))
    return sorted(qualified,key=lambda r:(r['close_time'],r['ticker'],r['as_of'])),dict(withheld)


def _map(parts,key):
    rows=[dict(r,p=r[key]) for r in parts['map_train'] if not r['state']['deterministic']]
    selected=dict(kind='identity',intercept=0.,slope=1.)
    def apply(records,parameters):
        return [dict(r,p=r[key] if r['state']['deterministic'] else calibrated_probability(r[key],parameters)) for r in records]
    best=_scores(apply(parts['validation'],selected),reliability=False)['log_loss']
    audit=[]
    if len({r['ticker'] for r in rows})>=20:
        for ridge in (0.,.01,.1,1.,10.):
            candidate=_fit(rows,ridge)
            if not candidate['converged']:
                audit.append(dict(regularization=ridge,status='nonconverged'));continue
            mapping={k:candidate[k] for k in ('kind','intercept','slope')}
            loss=_scores(apply(parts['validation'],mapping),reliability=False)['log_loss']
            audit.append(dict(regularization=ridge,validation_log_loss=loss,status='ready'))
            if loss<best-1e-12:selected,best=mapping,loss
    return selected,audit


def train_compare(rows,*,asset='ETH',config=None,created_at=None):
    config=NeuralTrainingConfig() if config is None else config
    if not isinstance(config,NeuralTrainingConfig):raise NeuralModelError('A bounded neural fit configuration is required')
    qualified,withheld=_qualify(rows,asset)
    report=dict(kind='kalshi_neural_training_report',version=1,status='insufficient_data',asset=asset,
        synthetic=qualified[0]['synthetic'] if qualified else False,comparison_only=True,
        execution_eligible=False,live_model_updated=False,venue_orders_submitted=0,
        config=asdict(config),withheld=withheld,count_forecasts=len(qualified),
        count_contracts=len({r['ticker'] for r in qualified}),
        baseline_definition=baseline_definition(asset),
        limitations=['Historical reconstruction does not establish historical receipt latency or prospective predictive skill.',
            'Prior studies have inspected these dates; the test is unused for this fit, not a new prospective holdout.',
            'The classifier predicts official binary settlement, not a contract-price trajectory or trading return.',
            'Day-block intervals omit parameter estimation uncertainty; no fills, P&L or Sharpe are inferred.'])
    if not qualified:return None,report
    parts,splits,reason=_partition(qualified,config)
    if parts is None:report['reason']=reason;return None,report
    stages=('base_train','map_train','validation','test')
    for i,name in enumerate(stages[:-1]):
        cutoff=splits[stages[i+1]]['first_as_of']
        late={r['ticker'] for r in parts[name] if r['provenance']=='recorded_receipts' and r['label_available_at']>cutoff}
        parts[name]=[r for r in parts[name] if r['ticker'] not in late]
        splits[name]['late_recorded_label_contracts_excluded']=len(late)
        splits[name]['count_contracts']=len({r['ticker'] for r in parts[name]})
        splits[name]['count_forecasts']=len(parts[name])
        if splits[name]['count_contracts']<config.minimum_contracts_per_stage:
            report['reason']='Too few contracts after actual label-receipt eligibility';return None,report
    if splits['test']['count_contracts']<config.minimum_test_contracts:
        report['reason']='Too few whole contracts in the locked test';return None,report
    standardization,network,fit=_fit_network(parts['base_train'],config,splits['map_train']['first_as_of'])
    created=time.time() if created_at is None else number(created_at,'artifact clock',lower=1,upper=253402300799)
    latest=max(r['label_available_at'] for r in qualified)
    if created<latest or created<max(r['close_time'] for r in qualified):
        raise NeuralModelError('Artifact creation cannot precede observed training/evaluation labels')
    payload=dict(kind=MODEL_KIND,version=1,model_id=MODEL_ID,asset=asset,
        source=baseline_definition(asset)['source'],baseline_definition=baseline_definition(asset),
        baseline_sha256=digest(baseline_definition(asset)),feature_schema=deepcopy(FEATURE_SCHEMA),
        standardization=standardization,network=network,calibration=dict(kind='identity',intercept=0.,slope=1.),
        synthetic=report['synthetic'],comparison_only=True,execution_eligible=False,live_validated=False,
        created_at=created,evidence=dict(dataset_sha256=digest(qualified),splits=splits,fit=fit,
            labels='synthetic_demo' if report['synthetic'] else 'official_binary_outcomes_from_original_archive',
            retrospective_label_receipts_used_as_live_receipts=False))
    payload['artifact_sha256']=digest(payload)
    provisional=NeuralModel(payload)
    for records in parts.values():
        for r in records:
            r['baseline_raw']=r['state']['p_gaussian']
            r['neural_raw']=provisional.predict(r['state'],offline=True)['raw_probability_yes']
    baseline_map,baseline_audit=_map(parts,'baseline_raw')
    neural_map,neural_audit=_map(parts,'neural_raw')
    payload['calibration']=neural_map
    payload['artifact_sha256']=digest({k:v for k,v in payload.items() if k!='artifact_sha256'})
    scored={}
    for form in ('raw','calibrated'):
        for model,mapping in (('baseline',baseline_map),('neural',neural_map)):
            scored[model+'_'+form]=[dict(r,p=r[model+'_raw'] if form=='raw' or r['state']['deterministic']
                else calibrated_probability(r[model+'_raw'],mapping)) for r in parts['test']]
    report.update(status='ready',reason='Offline fit complete; model remains comparison-only',splits=splits,fit=fit,
        artifact_sha256=payload['artifact_sha256'],maps=dict(baseline=baseline_map,neural=neural_map),
        calibration_selection=dict(baseline=baseline_audit,neural=neural_audit),
        test={name:_scores(records) for name,records in scored.items()},
        paired={form:_bootstrap(scored['baseline_'+form],scored['neural_'+form],config) for form in ('raw','calibrated')},
        by_horizon={str(int(h)):{name:_scores([r for r in records if r['close_time']-r['model_time']==h])
            for name,records in scored.items()} for h in sorted({r['close_time']-r['model_time'] for r in parts['test']})},
        historical_label_receipt_scope='Actual receipts enforced for recorded inputs; historical reconstruction partitions by source/close time without fabricating old receipts')
    return payload,report


def demo_rows(asset='ETH',*,days=20,contracts_per_day=40,seed=7):
    """Fictional correlated origins for plumbing only; never live eligible."""
    from .crypto_assets import asset_spec
    from .preclose_history import preclose_model_definition,preclose_model_identity
    rng=random.Random(seed);output=[];start=1767225600
    definition=preclose_model_definition(asset,synthetic=True,payoff_mode='legacy_unrounded')
    for day in range(days):
        for index in range(contracts_per_day):
            close=start+day*86400+(index+1)*900
            spot=4000.;strike=spot+rng.gauss(0,5);sigma=math.exp(rng.gauss(-9,.4))
            label=None
            for horizon in (300,30):
                clock=close-horizon;known=sum(close-offset<=clock for offset in range(60,0,-1))
                row=dict(asset=asset,source=asset_spec(asset).source,ticker=asset_spec(asset).series+f'-DEMO-{day}-{index}',
                    as_of=clock,model_time=clock,features_as_of=clock,close_time=close,spot=spot,
                    fast_sigma=sigma*math.exp(rng.gauss(0,.2)),slow_sigma=sigma,history_seconds=1800,
                    known_count=known,known_sum=str(known*spot),future_offsets=[close-offset-clock for offset in range(60,0,-1) if close-offset>clock],
                    target_price=str(strike),strike_type='greater_or_equal',synthetic=True,
                    settlement_grid=GRID,payoff_mode='legacy_unrounded',model_identity=preclose_model_identity(definition),
                    provenance='synthetic_demo',label_available_at=close+1)
                if label is None:
                    state=historical_state(row);label=int(rng.random()<state['p_gaussian'])
                row['y']=label;output.append(row)
    return output
