// Validation/presentation only. All prices, ledgers and statistics come from C++.
import {validateModel,modelFromForm} from './greeks-model.mjs';
export const ENGINE='self-financing-gbm-hedge-1';
const finite=Number.isFinite;
const keys=(v,k)=>v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).length===k.length&&k.every(s=>Object.hasOwn(v,s));
const stable=v=>Array.isArray(v)?'['+v.map(stable).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+stable(v[k])).join(',')+'}':JSON.stringify(v);
const range=(v,a,b)=>finite(v)&&v>=a&&v<=b;
export function hedgeRequest(model,dynamics,costs,simulation) {
  const m=validateModel(model),d={...dynamics},c={...costs},s={...simulation};
  if(!keys(d,['drift','volatility'])||!keys(c,['bps','fixed_per_trade'])||!keys(s,['paths','seed','first_steps','levels']))throw new Error('Unexpected or missing hedge inputs.');
  if(m.spot<=0||!range(m.maturity_years,1e-6,10)||!range(m.volatility,1e-6,3)||m.dividend_yield!==0)throw new Error('Use positive spot, maturity and hedge volatility; dividends must be zero.');
  if(!range(d.drift,-2,2)||!range(d.volatility,0,3)||Math.abs(d.drift)*m.maturity_years>5||d.volatility*Math.sqrt(m.maturity_years)>3)throw new Error('Path dynamics exceed the bounded GBM domain.');
  if(!range(c.bps,0,100)||!range(c.fixed_per_trade,0,1000))throw new Error('Costs must be 0–100 bps and 0–1000 per nonzero trade.');
  if(!['paths','first_steps','levels'].every(k=>Number.isSafeInteger(s[k]))||s.paths<1000||s.paths>50000||s.first_steps<1||s.first_steps>1024||(s.first_steps&(s.first_steps-1))||s.levels<1||s.levels>8)throw new Error('Use 1000–50000 paths, power-of-two first steps, and 1–8 levels.');
  const finest=s.first_steps*2**(s.levels-1),work=s.paths*(finest+s.first_steps*(2**s.levels-1)+4);
  if(finest>1024||work>12000000)throw new Error('12-million-work-unit / 1024-step bound exceeded.');
  if(typeof s.seed!=='string'||!/^\d{1,20}$/.test(s.seed)||BigInt(s.seed)>18446744073709551615n)throw new Error('Seed must be an unsigned 64-bit decimal string.');
  s.seed=BigInt(s.seed).toString();return {schema_version:1,model:m,dynamics:d,costs:c,simulation:s};
}
export function requestFromHedgeForm(form) {
  const number=k=>{const v=form.get(k);if(typeof v!=='string'||!v.trim())throw new Error(`Supply ${k}.`);const n=Number(v);if(!finite(n))throw new Error(`Invalid ${k}.`);return n;};
  return hedgeRequest(modelFromForm(form),{drift:number('path_drift'),volatility:number('path_volatility')},
    {bps:number('cost_bps'),fixed_per_trade:number('fixed_cost')},{paths:number('paths'),seed:form.get('seed'),first_steps:number('first_steps'),levels:number('levels')});
}
function stat(v) {
  return v&&finite(v.mean)&&range(v.sample_variance,0,Number.MAX_VALUE)&&
    (v.standard_error===null?v.ci_low===null&&v.ci_high===null:range(v.standard_error,0,Number.MAX_VALUE)&&finite(v.ci_low)&&finite(v.ci_high)&&v.ci_low<=v.mean&&v.mean<=v.ci_high);
}
export function validateHedge(v,request) {
  if(!v||v.schema_version!==1||v.kind!=='hedging_replication'||v.engine_version!==ENGINE||stable(v.request)!==stable(request)||
    v.input_source!=='synthetic_exact_gbm_not_broker_data'||!finite(v.initial_premium)||v.initial_premium<0||!finite(v.path_volatility_bsm_reference)||
    !/^[a-f0-9]{64}$/.test(v.numerical_sha256??'')||!/^[a-f0-9]{64}$/.test(v.build?.hedging_source_sha256??'')||
    !Array.isArray(v.warnings)||!v.warnings.every(w=>typeof w==='string')||!range(v.timing?.runtime_ms,0,Number.MAX_VALUE))throw new Error('Invalid or mismatched hedge result.');
  const det=request.dynamics.volatility===0,n=det?1:request.simulation.paths,finest=request.simulation.first_steps*2**(request.simulation.levels-1);
  if(v.deterministic!==det||v.paths_processed!==n||v.independent_paths!==(det?0:n)||v.normal_draws!==(det?0:n*finest)||!Array.isArray(v.policies)||v.policies.length!==request.simulation.levels+2)throw new Error('Invalid hedge path accounting.');
  for(const [i,p] of v.policies.entries()) {
    const intervals=i<2?1:request.simulation.first_steps*2**(i-2),kind=i===0?'unhedged':i===1?'initial_delta':'periodic';
    if(p.policy!==kind||p.intervals!==intervals||!stat(p.error)||!stat(p.paired_minus_initial)||
      !['standard_deviation','rmse'].every(k=>range(p.error[k],0,Number.MAX_VALUE))||!['minimum','maximum','q05','q50','q95'].every(k=>finite(p.error[k]))||
      !(p.error.minimum<=p.error.q05&&p.error.q05<=p.error.q50&&p.error.q50<=p.error.q95&&p.error.q95<=p.error.maximum)||!range(p.error.fraction_deficit,0,1)||
      !['mean_cost','mean_terminal_cost','mean_turnover','mean_trades','max_balance_residual'].every(k=>range(p[k],0,Number.MAX_VALUE))||!finite(p.minimum_cash))throw new Error('Invalid hedge diagnostics.');
    const h=p.histogram;
    if(!h||!Array.isArray(h.edges)||!Array.isArray(h.counts)||![1,40].includes(h.counts.length)||h.edges.length!==h.counts.length+1||h.edges.some((e,i)=>!finite(e)||(i&&e<h.edges[i-1]))||
      h.counts.some(c=>!Number.isSafeInteger(c)||c<0)||h.counts.reduce((a,b)=>a+b,0)!==n)throw new Error('Invalid terminal-error histogram.');
    if(!Array.isArray(p.sample_ledgers)||p.sample_ledgers.length!==Math.min(n,2))throw new Error('Missing path ledgers.');
    for(const rows of p.sample_ledgers) {
      if(!Array.isArray(rows)||rows.length!==intervals+1)throw new Error('Invalid ledger length.');
      for(const [j,r] of rows.entries()) {
        if(!r||!['time','spot','cash_previous','shares_previous','financing','cash_before','shares_after','shares_traded','trade_notional','cost','cash_after_trade','settlement','cash_after','hedge_value','liability_value','surplus','cost_sum','costs_at_time','balance_residual'].every(k=>finite(r[k]))||r.spot<=0||r.time<0||r.time>request.model.maturity_years||r.cost<0||r.settlement<0||
          (j&&r.time<=rows[j-1].time)||(j<intervals&&(r.settlement!==0||!finite(r.model_delta)))||(j===intervals&&(r.model_delta!==null||r.shares_after!==0)))throw new Error('Invalid self-financing ledger.');
      }
      if(rows[0].time!==0||rows.at(-1).time!==request.model.maturity_years)throw new Error('Invalid ledger time bounds.');
    }
  }
  return v;
}
export const policyLabel=p=>p.policy==='unhedged'?'Premium in cash':p.policy==='initial_delta'?'Initial delta only':`Periodic · ${p.intervals} intervals`;
