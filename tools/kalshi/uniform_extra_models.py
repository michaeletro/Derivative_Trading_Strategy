"""Additional offline candidates for the uniform native settlement benchmark.

No transport, journal, capture, order or live-loader dependency is present.
GJR uses a stationary Student-t leverage recursion; GARCH-FHS uses empirical
TRAIN-only minute endpoints and Gaussian intraminute bridges. That hybrid is
distinct from the native-one-second EWMA-FHS candidate.
"""
from __future__ import annotations
import hashlib
import json
import math
import numpy as np

from .offset_classifiers import (fit_ridge, predict as ridge_predict,
    contract_weights, offsets, ridge_objective)
from .garch_fit import (fit_garch, filter_garch, _logit, _sigmoid,
    validate_garch_parameters)
from .garch_student_t import (_bfgs, _density_constants, _nll_and_derivatives,
    _current_minute_posterior, student_t_cdf)
from .garch_math import _geometry, _cdf, _partial


STAGES = ('final_calibration', 'validation', 'test')
MODEL_IDS = ('cross_asset_ridge', 'regime_mixture', 'gjr_garch_t', 'garch_fhs')
ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
PERSISTENCE_MAX = .9995


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _parts_validate(parts):
    train = parts.get('model_train', [])
    if len({r['ticker'] for r in train}) < 20:
        raise ValueError('Extra models require at least twenty training contracts')
    end = max(float(r['close_time']) for r in train)
    if any(float(r['as_of']) < end for stage in STAGES for r in parts.get(stage, [])):
        raise ValueError('Evaluation origins must follow the model-training label endpoints')
    return train, min(float(r['as_of']) for r in train), end


def _features(rows):
    x = np.asarray([list(r['state_features']) + list(r['history_features']) for r in rows], float)
    if x.ndim != 2 or not np.isfinite(x).all():
        raise ValueError('Finite native state and history features are required')
    return x


def _p0(rows):
    return np.clip(np.asarray([r['p0'] for r in rows], float), 1e-6, 1-1e-6)


def _cross_index(dataset):
    columns = dataset.get('five_second_return_columns')
    required = ['end_time', 'value', 'segment_id', 'contiguous_returns', 'received_at']
    if columns != required:
        raise ValueError('Cross-asset predictors require native five-second return clocks')
    data = np.asarray(dataset['five_second_returns'], dtype=float)
    if data.ndim != 2 or data.shape[1] != 5 or not np.isfinite(data).all():
        raise ValueError('Invalid cross-asset return grid')
    if np.any(np.diff(data[:, 0]) <= 0):
        raise ValueError('Cross-asset source clocks must be strictly ordered')
    return dict(times=data[:, 0], values=data[:, 1], segments=data[:, 2],
        contiguous=data[:, 3], prefix=np.r_[0., np.cumsum(data[:, 1])])


def cross_asset_features(rows, all_datasets, asset):
    """Other-asset 1/3/5/15-minute returns through the exact origin, never later."""
    if asset not in ASSETS or not isinstance(all_datasets, dict) or set(ASSETS)-set(all_datasets):
        raise ValueError('A joint predictor requires all four native asset grids')
    indexes = {a: _cross_index(all_datasets[a]) for a in ASSETS if a != asset}
    x = _features(rows)
    additional = np.empty((len(rows), 12), dtype=float)
    for i, row in enumerate(rows):
        t, column = float(row['as_of']), 0
        for other in ASSETS:
            if other == asset:
                continue
            grid = indexes[other]
            end = int(np.searchsorted(grid['times'], t, side='right'))
            if end < 180 or grid['times'][end-1] != t or grid['contiguous'][end-1] < 180:
                raise ValueError('Missing point-in-time cross-asset lag endpoint')
            for minutes in (1, 3, 5, 15):
                count = minutes * 12
                first = end-count
                if (grid['times'][first] != t-count*5+5 or
                        grid['segments'][first] != grid['segments'][end-1]):
                    raise ValueError('Cross-asset history crosses an undeclared gap')
                additional[i, column] = grid['prefix'][end]-grid['prefix'][first]
                column += 1
    return np.column_stack((x, additional))


def _fit_cross_ridge(x, p0, labels, tickers):
    """Same ridge likelihood with explicit 21-input preprocessor, no bound edits."""
    x, labels = np.asarray(x,float),np.asarray(labels,float)
    if (x.ndim!=2 or not 1<=x.shape[1]<=64 or not np.isfinite(x).all() or
            set(labels)!={0.,1.}):
        raise ValueError('Cross-asset ridge requires finite bounded inputs and both classes')
    weights = contract_weights(tickers)
    means = np.sum(weights[:,None]*x,axis=0)
    scales = np.sqrt(np.sum(weights[:,None]*(x-means)**2,axis=0))
    scales = np.where(scales>1e-12,scales,1.)
    pre = dict(means=means.tolist(),scales=scales.tolist(),clip=8.,
        fit_scope='model_train_only')
    design = np.column_stack((np.ones(len(x)),np.clip((x-means)/scales,-8.,8.)))
    initial,beta = offsets(p0),np.zeros(design.shape[1])
    converged = False
    for iteration in range(100):
        old,gradient,hessian = ridge_objective(beta,design,initial,labels,weights,.01)
        if np.max(np.abs(gradient))<1e-9:
            converged=True
            break
        direction = np.linalg.solve(hessian,gradient)
        size = 1.
        for _ in range(40):
            candidate = beta-size*direction
            loss = ridge_objective(candidate,design,initial,labels,weights,.01,derivatives=False)
            if loss<=old-1e-4*size*float(gradient@direction):
                beta=candidate
                break
            size*=.5
        else:
            break
    return dict(kind='offset_ridge_logistic',version=1,preprocessing=pre,
        coefficients=beta.tolist(),ridge=.01,offset_coefficient=1.,
        offset_logit_clip=1e-6,identity=False,execution_eligible=False,research_only=True,
        fit=dict(converged=converged,iterations=iteration+1))


def regime_weights(rows):
    """Fixed causal soft gates; these are observable drift states, not HMM states."""
    d = np.asarray([r['drift_score'] for r in rows], float)
    if not np.isfinite(d).all():
        raise ValueError('Causal drift scores must be finite')
    plus = 1/(1+np.exp(-np.clip((d-1)/.25, -50, 50)))
    minus = 1/(1+np.exp(-np.clip((-d-1)/.25, -50, 50)))
    neutral = np.maximum(0., 1-plus-minus)
    result = np.column_stack((minus, neutral, plus))
    return result/result.sum(axis=1, keepdims=True)


def _coordinates(coordinates, *, derivatives=False):
    level = math.exp(coordinates[0])
    f, a, c, tail = map(_sigmoid, coordinates[1:])
    persistence = PERSISTENCE_MAX*f
    omega = level*(1-persistence)
    alpha, gamma, beta = persistence*a, 2*persistence*(1-a)*c, persistence*(1-a)*(1-c)
    nu = 2.05+197.95*tail
    values = dict(omega=omega, alpha=alpha, beta=beta, gamma=gamma,
        nu=nu, initial_variance=level)
    if not derivatives:
        return values
    dp, da, dc = PERSISTENCE_MAX*f*(1-f), a*(1-a), c*(1-c)
    values['domega'] = np.array([omega, -level*dp, 0., 0., 0.])
    values['dalpha'] = np.array([0., dp*a, persistence*da, 0., 0.])
    values['dgamma'] = np.array([0., 2*dp*(1-a)*c, -2*persistence*da*c,
        2*persistence*(1-a)*dc, 0.])
    values['dbeta'] = np.array([0., dp*(1-a)*(1-c), -persistence*da*(1-c),
        -persistence*(1-a)*dc, 0.])
    values['dnu'] = 197.95*tail*(1-tail)
    return values


def gjr_objective_gradient(coordinates, returns, weights, segment_starts=()):
    """Analytic transformed-coordinate Student-t GJR likelihood gradient."""
    if len(coordinates) != 5 or any(not math.isfinite(x) or abs(x) > 16 for x in coordinates):
        return math.inf, [0.]*5
    p = _coordinates(coordinates, derivatives=True)
    h, dh = p['initial_variance'], np.array([p['initial_variance'], 0., 0., 0., 0.])
    loss, gradient = 0., np.zeros(5)
    constants = _density_constants(p['nu'])
    for i, (r, weight) in enumerate(zip(returns, weights)):
        if i in segment_starts:
            h, dh = p['initial_variance'], np.array([p['initial_variance'], 0., 0., 0., 0.])
        if not math.isfinite(h) or h <= 0:
            return math.inf, [0.]*5
        square, leverage = r*r, r*r if r < 0 else 0.
        nll, derivative_h, derivative_nu = _nll_and_derivatives(square, h, p['nu'], constants)
        loss += weight*nll
        gradient += weight*derivative_h*dh
        gradient[4] += weight*derivative_nu*p['dnu']
        dh = (p['domega'] + p['dalpha']*square + p['dgamma']*leverage +
            p['dbeta']*h + p['beta']*dh)
        h = p['omega'] + p['alpha']*square + p['gamma']*leverage + p['beta']*h
    return float(loss), gradient.tolist()


def filter_gjr(returns, parameters, *, segment_starts=()):
    p = parameters
    if (set(p) != {'omega', 'alpha', 'beta', 'gamma'} or
            any(not math.isfinite(v) or v < 0 for v in p.values()) or p['omega'] <= 0 or
            p['alpha']+p['beta']+p['gamma']/2 > PERSISTENCE_MAX):
        raise ValueError('Invalid stationary GJR parameters')
    initial = p['omega']/(1-p['alpha']-p['beta']-p['gamma']/2)
    h, output = initial, [initial]
    for i, r in enumerate(returns):
        if i in segment_starts:
            h = initial
        h = p['omega']+(p['alpha']+p['gamma']*int(r<0))*r*r+p['beta']*h
        if not math.isfinite(h) or h <= 0:
            raise ValueError('Nonfinite GJR filter state')
        output.append(h)
    return output


def fit_gjr(returns, *, segment_starts=(), max_iterations=180, starts=2):
    """Train-only RMS scaling, no future backcast; positive leverage restriction."""
    values = np.asarray(returns, float)
    if values.ndim != 1 or len(values) < 300 or not np.isfinite(values).all():
        raise ValueError('GJR-t requires 300 finite training minute returns')
    scale = float(np.mean(values*values))
    if scale <= 0:
        raise ValueError('GJR-t training returns are degenerate')
    normalized = (values/math.sqrt(scale)).tolist()
    weights = [1/len(values)]*len(values)
    candidates = []
    for persistence, alpha_share, leverage_share, nu in ((.95,.08,.04,8.),(.85,.2,.12,5.))[:starts]:
        point = [0., _logit(persistence/PERSISTENCE_MAX), _logit(alpha_share),
            _logit(leverage_share), _logit((nu-2.05)/197.95)]
        fit = _bfgs(point, lambda x:gjr_objective_gradient(x, normalized, weights,
            set(segment_starts)), max_iterations, 1e-7)
        candidates.append(fit)
    converged = [x for x in candidates if x['converged']]
    chosen = min(converged or candidates, key=lambda x:x['objective'])
    best = min(x['objective'] for x in candidates)
    resolved = chosen['converged'] and chosen['objective']-best <= 1e-6
    transformed = _coordinates(chosen['coordinates'])
    parameters = {k:transformed[k] for k in ('omega','alpha','beta','gamma')}
    parameters['omega'] *= scale
    return dict(kind='uniform_gjr_garch_student_t_fit', version=1, parameters=parameters,
        degrees_of_freedom=transformed['nu'], initial_variance=transformed['initial_variance']*scale,
        converged=bool(resolved), objective=chosen['objective']+.5*math.log(scale),
        iterations=chosen['iterations'], gradient_inf=max(map(abs,chosen['gradient'])),
        starts_converged=len(converged), training_returns=len(values),
        training_sha256=_hash(dict(returns=values.tolist(), segments=list(segment_starts))),
        persistence=parameters['alpha']+parameters['beta']+parameters['gamma']/2,
        innovation_mean=0., innovation_variance=1., variance_units='squared_one_minute_log_return',
        recursion='omega+(alpha+gamma*I[e<0])*e^2+beta*h',
        leverage_constraint='gamma>=0', fourth_moment_guaranteed=False,
        execution_eligible=False)


def _beta_fraction_array(a, b, x):
    tiny = 1e-300
    qab, qap, qam = a+b, a+1, a-1
    c = np.ones_like(x)
    d = 1-qab*x/qap
    d = np.where(np.abs(d)<tiny, tiny, d)
    d = 1/d
    result = d.copy()
    for m in range(1, 1501):
        twice = 2*m
        coefficient = m*(b-m)*x/((qam+twice)*(a+twice))
        d, c = 1+coefficient*d, 1+coefficient/c
        d, c = np.where(np.abs(d)<tiny,tiny,d), np.where(np.abs(c)<tiny,tiny,c)
        d = 1/d
        result *= d*c
        coefficient = -(a+m)*(qab+m)*x/((a+twice)*(qap+twice))
        d, c = 1+coefficient*d, 1+coefficient/c
        d, c = np.where(np.abs(d)<tiny,tiny,d), np.where(np.abs(c)<tiny,tiny,c)
        d = 1/d
        change = d*c
        result *= change
        if np.max(np.abs(change-1), initial=0) <= 3e-14:
            return result
    raise ValueError('Vector Student-t integration did not converge')


def student_t_cdf_array(values, nu):
    values = np.asarray(values, float)
    if not math.isfinite(nu) or nu <= 2 or np.isnan(values).any():
        raise ValueError('Invalid Student-t projection')
    x = nu/(nu+np.minimum(np.abs(values),1e150)**2)
    result = np.empty_like(values)
    zero = values == 0
    a, b = nu/2, .5
    regular = (x>0)&(x<1)
    beta = np.zeros_like(x)
    beta[x>=1] = 1.
    logfactor = math.lgamma(a+b)-math.lgamma(a)-math.lgamma(b)
    direct = regular&(x<(a+1)/(a+b+2))
    reverse = regular&~direct
    for mask, aa, bb, xx, invert in (
            (direct,a,b,x[direct],False),(reverse,b,a,1-x[reverse],True)):
        if not len(xx):
            continue
        factor = np.exp(logfactor+aa*np.log(xx)+bb*np.log1p(-xx))
        integral = factor*_beta_fraction_array(aa,bb,xx)/aa
        beta[mask] = 1-integral if invert else integral
    tail = .5*beta
    result = np.where(values>0,1-tail,tail)
    result[zero] = .5
    return np.clip(result,0,1)


def _normal_cdf_array(values):
    a = np.asarray(values, float)
    return np.fromiter((.5*math.erfc(-float(x)/math.sqrt(2)) for x in a.flat),
        dtype=float, count=a.size).reshape(a.shape)


def _origin_seed(seed, row, model):
    # Geometry-only common random numbers; cross-origin numerical errors correlate.
    token = json.dumps([seed,model,float(row['close_time']-row['as_of'])]).encode()
    return int.from_bytes(hashlib.sha256(token).digest()[:8], 'little')


def gjr_probability(row, fit, current_variance, *, paths=4096, seed=7):
    if (type(paths) is not int or not 4<=paths<=65536 or paths%2 or
            not math.isfinite(current_variance) or current_variance<=0):
        raise ValueError('Use positive variance and bounded even GJR paths')
    geometry = _geometry(row)
    nu, p = fit['degrees_of_freedom'], fit['parameters']
    posterior = _current_minute_posterior(row,geometry,current_variance,nu,
        row.get('current_minute_observations'))
    spot, count = geometry['spot'], geometry['count']
    final_loading = spot*spot*geometry['covariance']/60
    if not geometry['pre_segments']:
        scale = math.sqrt(final_loading*current_variance*
            posterior['posterior_student_t_scale_factor'])
        if scale == 0:
            return _cdf(geometry['numerator'],0.,geometry),0.
        return student_t_cdf(geometry['numerator']/scale,
            posterior['posterior_degrees_of_freedom']),0.
    pairs = paths//2
    rng = np.random.default_rng(_origin_seed(seed,row,'gjr'))
    plus_h = np.full(pairs,current_variance)
    minus_h = plus_h.copy()
    plus_total, minus_total = np.zeros(pairs), np.zeros(pairs)
    for i, segment in enumerate(geometry['pre_segments']):
        shape = posterior['posterior_shape'] if i==0 else nu/2
        rate = posterior['posterior_scale'] if i==0 else (nu-2)/2
        latent = rate/rng.gamma(shape,1.,pairs)
        z = rng.normal(size=pairs)*np.sqrt(latent*segment[2])
        ep, em = np.sqrt(plus_h)*z,-np.sqrt(minus_h)*z
        plus_total += ep
        minus_total += em
        observed = posterior['observed_partial_return'] if i==0 else 0.
        total_p,total_m = observed+ep,observed+em
        plus_h = p['omega']+(p['alpha']+p['gamma']*(total_p<0))*total_p**2+p['beta']*plus_h
        minus_h = p['omega']+(p['alpha']+p['gamma']*(total_m<0))*total_m**2+p['beta']*minus_h
    plus_n = geometry['numerator']+count*spot*plus_total
    minus_n = geometry['numerator']+count*spot*minus_total
    plus = student_t_cdf_array(plus_n/np.sqrt(final_loading*plus_h*(nu-2)/nu),nu)
    minus = student_t_cdf_array(minus_n/np.sqrt(final_loading*minus_h*(nu-2)/nu),nu)
    values = (plus+minus)/2
    return float(values.mean()),float(values.std(ddof=1)/math.sqrt(pairs))


def garch_fhs_probability(row, parameters, current_variance, residuals, *,
        paths=4096, seed=7):
    """Empirical minute endpoints plus exact conditional Gaussian bridges.

    At a partial origin, posterior endpoint weights use the likelihood of the
    observed partial log return. Under this bridge law the endpoint likelihood
    depends on the last partial observation; earlier native prefix observations
    add only endpoint-independent factors.
    """
    geometry = _geometry(row)
    parameters = validate_garch_parameters(parameters)
    pool = np.asarray(residuals,float)
    if pool.ndim!=1 or len(pool)<120 or not np.isfinite(pool).all():
        raise ValueError('A finite train-only empirical innovation pool is required')
    if paths<128 or not math.isfinite(current_variance) or current_variance<=0:
        raise ValueError('Invalid GARCH-FHS simulation settings')
    rng = np.random.default_rng(_origin_seed(seed,row,'garch_fhs'))
    h, total = np.full(paths,current_variance),np.zeros(paths)
    observed, _ = _partial(geometry,row,row.get('minute_start_price'))
    for i, (_, _, fraction) in enumerate(geometry['pre_segments']):
        if fraction != 1.:
            raise ValueError('GARCH-FHS supports only declared minute origins outside final fixing')
        e = np.sqrt(h)*pool[rng.integers(len(pool),size=paths)]
        total += e
        full = e+observed if i==0 else e
        h = parameters['omega']+parameters['alpha']*full**2+parameters['beta']*h
    offsets = np.asarray(geometry['final_offsets'],float)
    duration = geometry['closed']-geometry['final_start']
    coefficient = float(offsets.sum()/duration)
    bridge_loading = max(0.,float(geometry['covariance']-offsets.sum()**2/duration))/60
    if not geometry['pre_segments'] and row['model_time']%60:
        fraction = (row['model_time']%60)/60
        expected = np.sqrt(current_variance)*pool
        logw = -(observed-fraction*expected)**2/(2*current_variance*fraction*(1-fraction))
        logw -= np.max(logw)
        weights = np.exp(logw)
        weights /= weights.sum()
        endpoint = expected[rng.choice(len(pool),size=paths,p=weights)]-observed
    else:
        endpoint = np.sqrt(h)*pool[rng.integers(len(pool),size=paths)]
    numerator = geometry['numerator']+geometry['spot']*(
        geometry['count']*total+coefficient*endpoint)
    scale = geometry['spot']*np.sqrt(h*bridge_loading)
    values = np.where(scale>0,_normal_cdf_array(numerator/np.maximum(scale,1e-300)),
        (numerator>=0).astype(float))
    return float(values.mean()),float(values.std(ddof=1)/math.sqrt(paths))


def _segments(records):
    return [i for i,r in enumerate(records) if i==0 or
        r.get('segment_id')!=records[i-1].get('segment_id') or
        r['start_time']!=records[i-1]['end_time']]


def _minute_data(dataset, first, last):
    all_records = dataset.get('minute_returns',[])
    if not all_records or any(b['end_time']<=a['end_time'] for a,b in zip(all_records,all_records[1:])):
        raise ValueError('A strictly chronological native minute grid is required')
    train = [r for r in all_records if r['start_time']>=first and r['end_time']<=last]
    if len(train)<300:
        raise ValueError('Insufficient complete training minute returns')
    return all_records,train


def _current_variance(row, records, filtered, *, ends=None):
    t = float(row['as_of'])
    if ends is None:
        ends = np.asarray([r['end_time'] for r in records],float)
    count = int(np.searchsorted(ends,t,side='right'))
    expected = math.floor(t/60)*60
    if count==0 or records[count-1]['end_time']!=expected:
        raise ValueError('Missing completed minute variance state')
    return float(filtered[count])


def _result(family, artifact, probabilities):
    return dict(status='evaluated',family=family,reason=None,
        probabilities=probabilities,artifact=artifact)


def fit_models(dataset, parts, *, paths=4096, seed=7, archive=None,
        all_datasets=None, progress=None):
    """Fit every added candidate independently, retaining explicit failure status."""
    if type(paths) is not int or paths<128 or paths>65536 or paths%2:
        raise ValueError('Use a bounded even simulation path budget')
    train, first, last = _parts_validate(parts)
    asset = dataset['asset']
    output = {}
    notify = (lambda *a,**kw:None) if progress is None else progress
    def record(identifier, family, fit):
        notify('extra_model',model=identifier,asset=asset)
        try:
            output[identifier] = fit()
            output[identifier]['artifact'].update(offline_only=True,execution_eligible=False,
                asset=asset,training_first_origin=first,training_last_close=last,
                test_labels_used_for_fitting=False)
        except (ValueError,TypeError,KeyError,RuntimeError,ArithmeticError,np.linalg.LinAlgError) as e:
            output[identifier] = dict(status='missing_input' if identifier=='cross_asset_ridge'
                and all_datasets is None else 'failed_fit',family=family,reason=str(e),
                probabilities={},artifact={})

    def cross():
        x = cross_asset_features(train,all_datasets,asset)
        model = _fit_cross_ridge(x,_p0(train),[r['y'] for r in train],
            [r['ticker'] for r in train])
        probs = {s:ridge_predict(model,cross_asset_features(parts[s],all_datasets,asset),
            _p0(parts[s])).tolist() for s in STAGES}
        return _result('cross_asset',dict(kind='uniform_cross_asset_offset_ridge',
            own_features='state4_history5',other_features='other3_assets_log_returns_1_3_5_15m',
            predictor_assets=list(ASSETS),joint_predictor=True,model=model),probs)
    record('cross_asset_ridge','cross_asset',cross)

    def mixture():
        weights = regime_weights(train)
        labels = np.argmax(weights,axis=1)
        global_fit = fit_ridge(_features(train),_p0(train),[r['y'] for r in train],
            [r['ticker'] for r in train],.01)
        experts,counts,fallback = [],[],[]
        for k in range(3):
            own = [r for r,m in zip(train,labels) if m==k]
            counts.append(len({r['ticker'] for r in own}))
            if counts[-1]<20 or len({r['y'] for r in own})<2:
                experts.append(global_fit);fallback.append(True)
            else:
                experts.append(fit_ridge(_features(own),_p0(own),[r['y'] for r in own],
                    [r['ticker'] for r in own],.01));fallback.append(False)
        probs = {}
        for s in STAGES:
            predictions = np.column_stack([ridge_predict(m,_features(parts[s]),_p0(parts[s]))
                for m in experts])
            probs[s] = np.sum(regime_weights(parts[s])*predictions,axis=1).tolist()
        return _result('regime_mixture',dict(kind='uniform_observable_regime_mixture',
            gate='soft_drift_thresholds_minus1_plus1_width0.25',
            latent_state_model=False,experts=experts,
            training_expert_contract_counts=counts,global_fallback=fallback),probs)
    record('regime_mixture','regime_mixture',mixture)

    def gjr():
        records,training = _minute_data(dataset,first,last)
        fit = fit_gjr([r['value'] for r in training],segment_starts=_segments(training))
        if not fit['converged']:
            raise ValueError('GJR-t optimizer did not establish stationary convergence')
        filtered = filter_gjr([r['value'] for r in records],fit['parameters'],
            segment_starts=_segments(records))
        ends = np.asarray([r['end_time'] for r in records],float)
        probs,se = {},[]
        for s in STAGES:
            probs[s] = []
            for r in parts[s]:
                p,error = gjr_probability(r,fit,_current_variance(r,records,filtered,ends=ends),
                    paths=paths,seed=seed)
                probs[s].append(p);se.append(error)
        fit.update(paths=paths,seed=seed,numerical_method=
            'antithetic_GJR_minute_scale_paths_exact_conditional_student_t_final_minute',
            maximum_conditional_monte_carlo_se=max(se,default=0.),
            geometry='T-60,...,T-1; exact known fixing and native partial-minute scale posterior',
            common_random_numbers='geometry_only_seed; errors correlated across origins')
        return _result('gjr_garch_t',fit,probs)
    record('gjr_garch_t','gjr_garch_t',gjr)

    def hybrid():
        records,training = _minute_data(dataset,first,last)
        fitted = fit_garch([r['value'] for r in training],segment_starts=_segments(training),
            max_iterations=180,starts=3)
        if not fitted.get('converged'):
            raise ValueError('GARCH-FHS conditional variance fit did not converge')
        params = fitted['parameters']
        training_h = filter_garch([r['value'] for r in training],params,
            segment_starts=_segments(training))
        reset = set(_segments(training))
        initial = params['omega']/(1-params['alpha']-params['beta'])
        residuals = np.asarray([r['value']/math.sqrt(initial if i in reset else training_h[i])
            for i,r in enumerate(training)])
        mean,scale = float(residuals.mean()),float(residuals.std())
        if scale<=0:
            raise ValueError('Degenerate training GARCH residuals')
        residuals = (residuals-mean)/scale
        filtered = filter_garch([r['value'] for r in records],params,
            segment_starts=_segments(records))
        ends = np.asarray([r['end_time'] for r in records],float)
        probs,se = {},[]
        for s in STAGES:
            probs[s] = []
            for r in parts[s]:
                p,error = garch_fhs_probability(r,params,_current_variance(r,records,filtered,ends=ends),
                    residuals,paths=paths,seed=seed)
                probs[s].append(p);se.append(error)
        artifact = dict(kind='uniform_garch_minute_endpoint_fhs',parameters=params,
            garch_fit=fitted,residuals=residuals.tolist(),residual_center=mean,residual_scale=scale,
            pool_fit_scope='model_train_only',pool_count=len(residuals),
            empirical_endpoint_law='iid_centered_unit_variance_train_filtered_minute_returns',
            intraminute_law='Gaussian_Brownian_bridge_conditioned_on_endpoint_and_observed_partial',
            native_one_second_fhs=False,geometry='T-60,...,T-1; exact observed partial fixing',
            paths=paths,seed=seed,maximum_conditional_monte_carlo_se=max(se,default=0.),
            common_random_numbers='geometry_only_seed; errors correlated across origins')
        return _result('garch_fhs',artifact,probs)
    record('garch_fhs','garch_fhs',hybrid)
    return output


def numerical_audit(dataset, rows, candidates, *, paths=4096, seed=1008,
        all_datasets=None):
    """Replay simulation candidates with frozen fitted parameters and new draws.

    `candidates` maps the original fit_models identifiers to their saved entries
    or directly to their artifact dictionaries. The output probabilities align
    exactly with `rows`; no label, fitting or calibration operation is performed.
    """
    if type(paths) is not int or paths<128 or paths>65536 or paths%2:
        raise ValueError('Use a bounded even simulation path budget')
    records = dataset.get('minute_returns',[])
    if not records:
        return {name:dict(status='missing_input',reason='Native minute grid absent',
            probabilities=[]) for name in ('gjr_garch_t','garch_fhs') if name in candidates}
    ends = np.asarray([r['end_time'] for r in records],float)
    if np.any(np.diff(ends)<=0):
        raise ValueError('Numerical replay requires ordered native minute clocks')
    values = [r['value'] for r in records]
    result = {}
    for name in ('gjr_garch_t','garch_fhs'):
        if name not in candidates:
            continue
        candidate = candidates[name]
        artifact = candidate.get('artifact',candidate)
        if candidate.get('status','evaluated')!='evaluated' or not artifact:
            result[name] = dict(status='unavailable',reason='No qualified frozen fit',
                probabilities=[])
            continue
        try:
            params = artifact['parameters']
            if name=='gjr_garch_t':
                filtered = filter_gjr(values,params,segment_starts=_segments(records))
            else:
                filtered = filter_garch(values,params,segment_starts=_segments(records))
            probabilities,errors = [],[]
            for row in rows:
                t = float(row['as_of'])
                count = int(np.searchsorted(ends,t,side='right'))
                if count==0 or records[count-1]['end_time']!=math.floor(t/60)*60:
                    raise ValueError('Missing native minute state in numerical replay')
                h = float(filtered[count])
                if name=='gjr_garch_t':
                    p,error = gjr_probability(row,artifact,h,paths=paths,seed=seed)
                else:
                    p,error = garch_fhs_probability(row,params,h,artifact['residuals'],
                        paths=paths,seed=seed)
                probabilities.append(p);errors.append(error)
            result[name] = dict(status='evaluated',reason=None,probabilities=probabilities,
                paths=paths,seed=seed,fitted_parameters_frozen=True,
                calibration_maps_refit=False,outcome_labels_read=False,
                maximum_conditional_monte_carlo_se=max(errors,default=0.))
        except (ValueError,TypeError,KeyError,ArithmeticError,RuntimeError) as error:
            result[name] = dict(status='failed_prediction',reason=str(error),probabilities=[])
    return result
