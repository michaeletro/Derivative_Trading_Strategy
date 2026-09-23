import test from 'node:test';
import assert from 'node:assert/strict';
import {hedgeRequest,requestFromHedgeForm,validateHedge,policyLabel,ENGINE} from '../../src/frontend/dashboard/hedging-model.mjs';
import {typedReference,validateTyped,comparison} from '../../src/frontend/dashboard/sde-model.mjs';
const model={exercise_style:'european',right:'call',spot:100,strike:100,maturity_years:1,rate:.05,dividend_yield:0,volatility:.2,currency:'USD'};
const dynamics={drift:.05,volatility:.2},costs={bps:0,fixed_per_trade:0},simulation={paths:1000,seed:'42',first_steps:1,levels:1};
const req=()=>hedgeRequest(model,dynamics,costs,simulation);
const clone=v=>structuredClone(v);
function fixture(){
  const stat={mean:0,sample_variance:1,standard_error:.1,ci_low:-.196,ci_high:.196};
  const row={time:0,spot:100,cash_previous:10,shares_previous:0,financing:0,cash_before:10,shares_after:0,shares_traded:0,trade_notional:0,cost:0,cash_after_trade:10,settlement:0,cash_after:10,hedge_value:10,liability_value:10,surplus:0,cost_sum:0,costs_at_time:0,balance_residual:0,model_delta:.5};
  const last={...row,time:1,model_delta:null};
  return {schema_version:1,kind:'hedging_replication',engine_version:ENGINE,request:req(),input_source:'synthetic_exact_gbm_not_broker_data',initial_premium:10,path_volatility_bsm_reference:10,
    numerical_sha256:'a'.repeat(64),build:{hedging_source_sha256:'b'.repeat(64)},timing:{runtime_ms:10},warnings:['Synthetic test fixture'],deterministic:false,paths_processed:1000,independent_paths:1000,normal_draws:1000,
    policies:['unhedged','initial_delta','periodic'].map(policy=>({policy,intervals:1,error:{...stat,standard_deviation:1,rmse:1,minimum:-2,maximum:2,q05:-1,q50:0,q95:1,fraction_deficit:.5},paired_minus_initial:stat,histogram:{edges:[-2,2],counts:[1000]},mean_cost:0,mean_terminal_cost:0,mean_turnover:0,mean_trades:0,minimum_cash:0,max_balance_residual:0,sample_ledgers:[[row,last],[clone(row),clone(last)]]}))};
}
function saved(){return {kind:'hedging_replication',reference:'hedging_replication:1',parent_reference:null,immutable:true,name:'Fixture only',configuration:req(),result:fixture(),result_sha256:'c'.repeat(64)};}
test('hedge and path volatility remain separate',()=>{const r=hedgeRequest(model,{...dynamics,volatility:.3},costs,simulation);assert.equal(r.model.volatility,.2);assert.equal(r.dynamics.volatility,.3);});
test('zero path volatility is supported but zero hedge volatility is not',()=>{assert.equal(hedgeRequest(model,{...dynamics,volatility:0},costs,simulation).dynamics.volatility,0);assert.throws(()=>hedgeRequest({...model,volatility:0},dynamics,costs,simulation));});
test('dividends expiry and invalid models refused',()=>{for(const p of [{dividend_yield:.01},{spot:0},{maturity_years:0},{exercise_style:'american'}])assert.throws(()=>hedgeRequest({...model,...p},dynamics,costs,simulation));});
test('reject extra fields and nonfinite dynamics',()=>{assert.throws(()=>hedgeRequest(model,{...dynamics,broker:true},costs,simulation));assert.throws(()=>hedgeRequest(model,{...dynamics,drift:Infinity},costs,simulation));});
test('invalid costs rejected rather than clamped',()=>{for(const p of [{bps:-1},{bps:101},{fixed_per_trade:1001}])assert.throws(()=>hedgeRequest(model,dynamics,{...costs,...p},simulation));});
test('work bounds and noninteger grids enforced',()=>{for(const p of [{paths:999},{paths:50001},{first_steps:3},{levels:2.5},{paths:50000,first_steps:1024}])assert.throws(()=>hedgeRequest(model,dynamics,costs,{...simulation,...p}));});
test('seeds must retain unsigned 64-bit string precision',()=>{assert.equal(hedgeRequest(model,dynamics,costs,{...simulation,seed:'00042'}).simulation.seed,'42');for(const seed of [42,'18446744073709551616'])assert.throws(()=>hedgeRequest(model,dynamics,costs,{...simulation,seed}));});
test('blank input is not silently turned into zero',()=>{const f=new Map(Object.entries({...model,path_drift:'',path_volatility:'.2',cost_bps:'0',fixed_cost:'0',...simulation}).map(([k,v])=>[k,String(v)]));assert.throws(()=>requestFromHedgeForm(f));});
test('matching result and explicit policy labels',()=>{const r=fixture();assert.equal(validateHedge(r,req()),r);assert.equal(policyLabel(r.policies[2]),'Periodic · 1 intervals');});
test('mismatched request and source are rejected',()=>{let r=fixture();r.request.costs.bps=5;assert.throws(()=>validateHedge(r,req()));r=fixture();r.input_source='live';assert.throws(()=>validateHedge(r,req()));});
test('independent count accounting checked',()=>{const r=fixture();r.independent_paths=999;assert.throws(()=>validateHedge(r,req()));});
test('histogram cannot hide missing sample mass',()=>{const r=fixture();r.policies[0].histogram.counts=[999];assert.throws(()=>validateHedge(r,req()));});
test('nonfinite ledger and double-settlement shape refused',()=>{let r=fixture();r.policies[0].sample_ledgers[0][0].cash_after=NaN;assert.throws(()=>validateHedge(r,req()));r=fixture();r.policies[0].sample_ledgers[0][0].settlement=1;assert.throws(()=>validateHedge(r,req()));});
test('invalid terminal delta or residual stock rejected',()=>{const r=fixture();r.policies[0].sample_ledgers[0][1].shares_after=1;assert.throws(()=>validateHedge(r,req()));});
test('quantiles and uncertainty ordering checked',()=>{const r=fixture();r.policies[0].error.q05=5;assert.throws(()=>validateHedge(r,req()));});
test('catalog preserves hedge kind and configuration binding',()=>{const a=saved();assert.equal(typedReference(a.reference),a.reference);assert.equal(validateTyped(a),a);a.configuration.costs.bps=3;assert.throws(()=>validateTyped(a));});
test('comparison ignores timings, not errors',()=>{const a=saved(),b=clone(a);b.result.timing.runtime_ms=100;assert.match(comparison(a,b),/match exactly/);b.result.policies[0].error.mean=.1;assert.match(comparison(a,b),/differ/);});
test('wrong reference kind and mutable records rejected',()=>{const a=saved();a.reference='sde_convergence:1';assert.throws(()=>validateTyped(a));assert.throws(()=>typedReference('hedging_replication:../1'));});
