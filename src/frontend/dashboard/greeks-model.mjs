// Only validation and unit conversion. No option valuation happens in JavaScript.
import {validateRequest as pricingRequest} from './pricing-model.mjs';
export const ENGINE='european-greeks-scenarios-v1';
export const MODEL_FIELDS=['exercise_style','right','spot','strike','maturity_years','rate','dividend_yield','volatility','currency'];
const MC_FIELDS=['draws','seed','pairing','estimator','relative_spot_bump','volatility_bump'];
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const exactKeys=(x,fields)=>x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).length===fields.length&&fields.every(k=>Object.hasOwn(x,k));
const range=(x,lo,hi,name)=>{if(!finite(x)||x<lo||x>hi)throw new Error(`${name} must be between ${lo} and ${hi}.`);};
export function validateModel(m) {
  if(!exactKeys(m,MODEL_FIELDS))throw new Error('Model has missing or unexpected fields.');
  const p=pricingRequest({schema_version:1,...m,paths:1000,seed:'42',method:'plain'});
  return Object.fromEntries(MODEL_FIELDS.map(k=>[k,p[k]]));
}
export function modelFromForm(form) {
  const m=Object.fromEntries(MODEL_FIELDS.map(k=>[k,form.get(k)])); m.exercise_style='european';
  for(const k of ['spot','strike','maturity_years','rate','dividend_yield','volatility']) {
    if(typeof m[k]!=='string'||!m[k].trim())throw new Error(`Supply ${k}.`);
    m[k]=Number(m[k]);
  }
  m.currency=String(m.currency??'').trim().toUpperCase();return validateModel(m);
}
export function greekRequest(model,s) {
  model=validateModel(model);
  if(!exactKeys(s,MC_FIELDS)||!['pathwise','central_crn'].includes(s.estimator))throw new Error('Invalid sensitivity estimator fields.');
  const p=pricingRequest({schema_version:1,...model,paths:s.draws,seed:s.seed,method:s.pairing});
  range(s.relative_spot_bump,1e-6,.1,'Relative spot bump');range(s.volatility_bump,1e-6,.1,'Volatility bump');
  if(s.estimator==='central_crn'&&model.spot>0&&model.volatility>0&&model.maturity_years>0) {
    for(const b of [{...model,spot:model.spot*(1+s.relative_spot_bump)},
      {...model,spot:model.spot*(1-s.relative_spot_bump)},
      {...model,volatility:model.volatility+s.volatility_bump},
      {...model,volatility:model.volatility-s.volatility_bump}]) validateModel(b);
    if(model.volatility-s.volatility_bump<=0)throw new Error('Central differences require a positive lower volatility bump.');
  }
  return {schema_version:1,model,simulation:{...s,seed:p.seed}};
}
export function scenarioRequest(model,shock,grid) {
  model=validateModel(model);
  if(!exactKeys(shock,['relative_spot','volatility','elapsed_years'])||!exactKeys(grid,['spot_span','volatility_span']))throw new Error('Invalid scenario fields.');
  range(shock.relative_spot,-.9,1,'Spot shock');range(shock.volatility,-1,1,'Volatility shock');
  range(shock.elapsed_years,0,model.maturity_years,'Time roll');
  range(grid.spot_span,0,.5,'Spot grid');range(grid.volatility_span,0,.5,'Volatility grid');
  validateModel({...model,spot:model.spot*(1+shock.relative_spot),volatility:model.volatility+shock.volatility,maturity_years:model.maturity_years-shock.elapsed_years});
  return {schema_version:1,model,shock:{...shock},grid:{...grid}};
}
export function displayGreek(value,key) {
  if(!finite(value))return null;
  return value*(key==='vega'||key==='rho'?.01:key==='theta'?1/365:1);
}
const equal=(a,b)=>{
  if(a===b)return true;
  return a&&b&&typeof a==='object'&&typeof b==='object'&&Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>equal(a[k],b[k]));
};
const optional=x=>x===null||finite(x);
function validGreeks(g) {
  if(!g||!finite(g.price)||g.price<0||!['available','unavailable'].includes(g.status))return false;
  return ['delta','gamma','vega','theta','rho'].every(k=>g.status==='available'?finite(g[k]):g[k]===null);
}
function envelope(record,request,kind) {
  if(record?.kind!==`derivative_lab.${kind}_experiment`||record.schema_version!==1||record.engine_version!==ENGINE||
      record.input_source!=='manual_scenario_not_broker_data'||!equal(record.request,request)||
      !/^[a-f0-9]{64}$/.test(record.build?.source_sha256??''))throw new Error('Unexpected or mismatched research response.');
}
export function validateGreekRecord(record,request) {
  envelope(record,request,'greeks');const a=record.analytical,m=record.simulation;
  if(!validGreeks(a)||!m||!['available','unavailable'].includes(m.status)||!finite(m.runtime_ms)||m.runtime_ms<0||
      !Array.isArray(m.warnings)||m.warnings.length>10||m.warnings.some(w=>typeof w!=='string'||w.length>600))throw new Error('Invalid Greek results.');
  if(m.status==='unavailable') {
    if(m.delta!==null||m.vega!==null||m.terminal_draws!==0||m.independent_samples!==0)throw new Error('Invalid unavailable sensitivities.');
  } else {
    const n=request.simulation.draws/(request.simulation.pairing==='antithetic'?2:1);
    if(a.status!=='available'||m.terminal_draws!==request.simulation.draws||m.independent_samples!==n||
       m.bumped_payoff_evaluations!==(request.simulation.estimator==='central_crn'?4*request.simulation.draws:0))throw new Error('Invalid sensitivity sample accounting.');
    for(const k of ['delta','vega']) {
      const e=m[k];
      if(!e||!finite(e.value)||!finite(e.sample_variance)||e.sample_variance<0||!finite(e.sampling_target)||!finite(e.finite_bump_bias)||e.analytical_derivative!==a[k]||
        !((e.standard_error===null&&e.ci_low===null&&e.ci_high===null)||
          (finite(e.standard_error)&&e.standard_error>=0&&finite(e.ci_low)&&finite(e.ci_high)&&e.ci_low<=e.value&&e.ci_high>=e.value)))throw new Error('Invalid sensitivity estimate.');
    }
  }
  return record;
}
function validScenario(s) {
  return s&&typeof s.valid==='boolean'&&typeof s.reason==='string'&&['relative_spot','volatility','elapsed_years'].every(k=>finite(s[k]))&&
    (s.valid?finite(s.shocked_price)&&s.shocked_price>=0&&finite(s.full_change)&&optional(s.approximation)&&optional(s.residual)&&((s.approximation===null)===(s.residual===null)):
      ['shocked_price','full_change','approximation','residual'].every(k=>s[k]===null));
}
export function validateScenarioRecord(record,request) {
  envelope(record,request,'scenario');
  if(!validGreeks(record.base)||!validScenario(record.selected)||!record.selected.valid||!equal(record.grid_shape,[11,21])||
     !Array.isArray(record.grid)||record.grid.length!==231||record.grid.some(s=>!validScenario(s))||
     !Array.isArray(record.residual_curve)||record.residual_curve.length!==41||record.residual_curve.some(s=>!validScenario(s))||
     !Array.isArray(record.spot_curves)||record.spot_curves.length!==3)throw new Error('Invalid scenario results.');
  for(const c of record.spot_curves)if(!finite(c.maturity_years)||!Array.isArray(c.points)||c.points.length!==41||
      c.points.some(p=>typeof p.valid!=='boolean'||!finite(p.spot)||(p.valid?!finite(p.price):p.price!==null)||!['delta','gamma'].every(k=>optional(p[k]))))throw new Error('Invalid scenario curves.');
  return record;
}
export function buildLabel(b,mode) {
  if(!b)return 'Build identity unavailable from this server.';
  const rev=/^[a-f0-9]{40}$/.test(b.git_revision??'')?b.git_revision.slice(0,12):'unavailable (source archive)';
  const hash=/^[a-f0-9]{64}$/.test(b.source_sha256??'')?b.source_sha256.slice(0,12):'unavailable';
  return `Build ${rev} · source ${hash} · v${String(b.application_version??'?').slice(0,20)} · configured dirty: ${b.dirty_worktree??'unknown'} · broker: ${mode??'unknown'} · ${b.sensitivity_engine===ENGINE?ENGINE:'sensitivity engine unavailable'}`;
}
