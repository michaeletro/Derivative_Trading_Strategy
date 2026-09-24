import {hedgeRequest,validateHedge} from './hedging-model.mjs';
import {validateModel,modelFromForm,validateGreekRecord} from './greeks-model.mjs';
import {validateRecord as validatePrice} from './pricing-model.mjs';
import {validateExperiment as validateReplay} from './replay-model.mjs';
export const ENGINE='coupled-gbm-convergence-1';
export const KINDS=['greek_validation','hedging_replication','option_pricing','return_volatility','sde_convergence'];
const finite=Number.isFinite;
const exactKeys=(v,keys)=>v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).length===keys.length&&keys.every(k=>Object.hasOwn(v,k));
export function stable(value) {
  if(Array.isArray(value))return '['+value.map(stable).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stable(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
export function sdeRequest(model,simulation) {
  if(!exactKeys(simulation,['paths','seed','first_steps','levels']))throw new Error('Supply exactly the SDE simulation settings.');
  const m=validateModel(model),s={...simulation};
  for(const key of ['paths','first_steps','levels']) {
    if(typeof s[key]==='string'&&!s[key].trim())throw new Error('Blank simulation fields are not zero.');
    s[key]=Number(s[key]);if(!Number.isSafeInteger(s[key]))throw new Error('Paths and grid sizes must be integers.');
  }
  if(typeof s.seed!=='string'||!/^\d{1,20}$/.test(s.seed)||BigInt(s.seed)>18446744073709551615n)throw new Error('Use an unsigned 64-bit decimal-string seed.');
  s.seed=BigInt(s.seed).toString();
  if(s.paths<1000||s.paths>100000||s.first_steps<1||s.first_steps>1024||(s.first_steps&(s.first_steps-1))||s.levels<1||s.levels>9)
    throw new Error('Use 1000–100000 paths, power-of-two first steps, and 1–9 levels.');
  const finest=s.first_steps*2**(s.levels-1),sum=s.first_steps*(2**s.levels-1);
  if(finest>2048||s.paths*(finest+2*sum)>50000000)throw new Error('50-million-work-unit / 2048-step limit exceeded. Reduce paths or refinement.');
  return {schema_version:1,model:m,simulation:s};
}
export function requestFromSdeForm(form) {
  const m=modelFromForm(form);return sdeRequest(m,Object.fromEntries(['paths','seed','first_steps','levels'].map(k=>[k,form.get(k)])));
}
const nonnegative=x=>finite(x)&&x>=0;
function statistic(v) {
  if(!v||!finite(v.mean)||!nonnegative(v.sample_variance))return false;
  if(v.standard_error===null)return v.ci_low===null&&v.ci_high===null;
  return nonnegative(v.standard_error)&&finite(v.ci_low)&&finite(v.ci_high)&&v.ci_low<=v.mean&&v.mean<=v.ci_high;
}
export function validateSde(v,request) {
  if(!v||v.schema_version!==1||v.kind!=='sde_convergence'||v.engine_version!==ENGINE||stable(v.request)!==stable(request)||
     v.input_source!=='manual_simulation_not_market_data'||!finite(v.analytical_price)||!statistic(v.exact_price)||
     typeof v.deterministic!=='boolean'||!Array.isArray(v.levels)||v.levels.length!==request.simulation.levels||
     !/^[a-f0-9]{64}$/.test(v.build?.sde_source_sha256??'')||!Array.isArray(v.warnings)||!v.warnings.every(x=>typeof x==='string')||!/^[a-f0-9]{64}$/.test(v.numerical_sha256??''))throw new Error('Invalid or mismatched SDE result.');
  const deterministic=request.model.spot===0||request.model.volatility===0||request.model.maturity_years===0;
  const paths=deterministic?1:request.simulation.paths;
  if(v.deterministic!==deterministic||v.paths_processed!==paths||v.independent_paths!==(deterministic?0:paths)||
     v.normal_draws!==(deterministic?0:paths*request.simulation.first_steps*2**(request.simulation.levels-1)))throw new Error('Invalid independent-path accounting.');
  for(const [i,l] of v.levels.entries()) {
    const n=request.simulation.first_steps*2**i;
    if(l.steps!==n||!nonnegative(l.step_years)||l.scheme_updates_each!==paths*n||!Array.isArray(l.sample_paths)||l.sample_paths.length!==Math.min(paths,2))throw new Error('Invalid refinement level.');
    for(const k of ['euler','milstein']) {
      const s=l[k];if(!s||!statistic(s.price)||!statistic(s.absolute_terminal_error)||!statistic(s.paired_payoff_bias)||
        !nonnegative(s.absolute_terminal_error.mean)||!nonnegative(s.rmse_terminal)||!Number.isSafeInteger(s.nonpositive_steps)||s.nonpositive_steps<0||s.nonpositive_steps>paths*n||
        !Number.isSafeInteger(s.paths_with_nonpositive)||s.paths_with_nonpositive<0||s.paths_with_nonpositive>paths||typeof s.bias_resolved_pointwise!=='boolean')throw new Error('Invalid convergence diagnostics.');
    }
    for(const p of l.sample_paths)if(!Array.isArray(p)||p.length!==n+1||p.some((r,j)=>!r||!['time','exact','euler','milstein'].every(k=>finite(r[k]))||r.time<0||(j>0&&r.time<p[j-1].time)))throw new Error('Invalid coupled path preview.');
  }
  if(!v.timing||!nonnegative(v.timing.total_ms)||!nonnegative(v.timing.brownian_generation_ms)||!Array.isArray(v.timing.kernels_ms)||v.timing.kernels_ms.length!==v.levels.length||
     v.timing.kernels_ms.some((t,i)=>t.steps!==v.levels[i].steps||!nonnegative(t.euler)||!nonnegative(t.milstein)))throw new Error('Invalid timing report.');
  return v;
}
export function typedReference(s) {
  if(typeof s!=='string'||!/^(greek_validation|hedging_replication|option_pricing|return_volatility|sde_convergence):[1-9][0-9]{0,17}$/.test(s))throw new Error('Invalid typed experiment reference.');return s;
}
export function validateTyped(v) {
  if(!v||!KINDS.includes(v.kind)||typedReference(v.reference).split(':')[0]!==v.kind||v.immutable!==true||typeof v.name!=='string'||
     !/^[a-f0-9]{64}$/.test(v.result_sha256??''))throw new Error('Invalid saved experiment.');
  if(v.parent_reference!==null&&typedReference(v.parent_reference).split(':')[0]!==v.kind)throw new Error('Invalid parent reference.');
  if(v.kind!=='return_volatility'&&stable(v.configuration)!==stable(v.result?.request))throw new Error('Saved configuration/result binding mismatch.');
  if(v.kind==='hedging_replication')validateHedge(v.result,hedgeRequest(v.result.request.model,v.result.request.dynamics,v.result.request.costs,v.result.request.simulation));
  else if(v.kind==='sde_convergence')validateSde(v.result,sdeRequest(v.result.request.model,v.result.request.simulation));
  else if(v.kind==='option_pricing')validatePrice(v.result,v.result.request);
  else if(v.kind==='greek_validation')validateGreekRecord(v.result,v.result.request);
  else validateReplay(v);
  return v;
}
// This comparison changes presentation/metadata only; no model calculations in JS.
function numerical(value) {
  if(Array.isArray(value))return value.map(numerical);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).filter(([k])=>!['build','timing','runtime_ms','created_ms','created_at_unix_ms','numerical_sha256'].includes(k)).map(([k,v])=>[k,numerical(v)]));
  return value;
}
export function comparison(a,b) {
  validateTyped(a);validateTyped(b);
  if(a.kind!==b.kind)throw new Error('Compare the same experiment type; different units and outputs are not interchangeable.');
  const equal=stable(numerical(a.result))===stable(numerical(b.result));
  const buildChanged=stable(a.result.build)!==stable(b.result.build);
  return `${equal?'Numerical payloads match exactly':'Numerical payloads or configurations differ'}; timing/build metadata excluded. ${buildChanged?'Build metadata differs.':'Same build metadata.'} This is a reproducibility comparison, not evidence of trading performance.`;
}
