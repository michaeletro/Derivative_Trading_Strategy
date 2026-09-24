import test from 'node:test';
import assert from 'node:assert/strict';
import {sdeRequest,requestFromSdeForm,validateSde,typedReference,comparison,validateTyped,stable} from '../../src/frontend/dashboard/sde-model.mjs';
const model={exercise_style:'european',right:'call',spot:100,strike:100,maturity_years:1,rate:.05,dividend_yield:0,volatility:.2,currency:'USD'};
const cfg={paths:1000,seed:'42',first_steps:2,levels:1};
const req=()=>sdeRequest(model,cfg);
// Labeled presentation fixture. Numerical truth is checked against the real C++
// endpoint in browser/http tests, not by these synthetic display values.
function fixture(){const stat={mean:1,sample_variance:1,standard_error:.1,ci_low:.8,ci_high:1.2};
 const scheme={price:stat,absolute_terminal_error:stat,paired_payoff_bias:stat,rmse_terminal:1.1,nonpositive_steps:0,paths_with_nonpositive:0,bias_resolved_pointwise:true};
 const path=[{time:0,exact:100,euler:100,milstein:100},{time:.5,exact:105,euler:104,milstein:105},{time:1,exact:110,euler:109,milstein:110}];
 return {schema_version:1,kind:'sde_convergence',engine_version:'coupled-gbm-convergence-1',request:req(),input_source:'manual_simulation_not_market_data',analytical_price:10,exact_price:stat,deterministic:false,paths_processed:1000,independent_paths:1000,normal_draws:2000,levels:[{steps:2,step_years:.5,scheme_updates_each:2000,euler:scheme,milstein:structuredClone(scheme),sample_paths:[path,path]}],warnings:[],numerical_sha256:'a'.repeat(64),timing:{total_ms:5,brownian_generation_ms:1,kernels_ms:[{steps:2,euler:1,milstein:1}]},build:{sde_source_sha256:'b'.repeat(64)}};
}
function saved(){const result=fixture();return {kind:'sde_convergence',reference:'sde_convergence:1',immutable:true,name:'Synthetic validation fixture',parent_reference:null,result_sha256:'c'.repeat(64),engine_version:result.engine_version,configuration:result.request,result};}
test('canonical SDE model and dyadic settings',()=>assert.equal(req().simulation.paths,1000));
test('uint64 seeds retained as strings',()=>assert.equal(sdeRequest(model,{...cfg,seed:'18446744073709551615'}).simulation.seed,'18446744073709551615'));
test('numeric seed and overflow rejected',()=>{for(const seed of [42,'18446744073709551616','-1'])assert.throws(()=>sdeRequest(model,{...cfg,seed}));});
test('work and finest grid bounded',()=>{assert.throws(()=>sdeRequest(model,{...cfg,paths:100000,first_steps:1024,levels:2}));assert.throws(()=>sdeRequest(model,{...cfg,first_steps:1024,levels:3}));});
test('non-dyadic and fractional counts rejected',()=>{for(const v of [{first_steps:3},{paths:999},{levels:1.5},{levels:0}])assert.throws(()=>sdeRequest(model,{...cfg,...v}));});
test('blank fields are not zero',()=>{assert.throws(()=>sdeRequest(model,{...cfg,paths:''}));const f=new FormData();for(const [k,v] of Object.entries({...model,...cfg}))f.set(k,String(v));f.set('spot','');assert.throws(()=>requestFromSdeForm(f));});
test('unsupported model conventions rejected',()=>assert.throws(()=>sdeRequest({...model,exercise_style:'american'},cfg)));
test('unexpected configuration keys rejected',()=>assert.throws(()=>sdeRequest(model,{...cfg,auto_trade:true})));
test('valid labeled result accepted',()=>assert.equal(validateSde(fixture(),req()).levels.length,1));
test('mismatched request cannot display as current',()=>{const f=fixture();f.request.simulation.seed='2';assert.throws(()=>validateSde(f,req()));});
test('correlated time steps cannot count as independent paths',()=>{const f=fixture();f.independent_paths=2000;assert.throws(()=>validateSde(f,req()));});
test('nonfinite or missing sensitivity diagnostics rejected',()=>{for(const change of [f=>f.levels[0].euler.price.mean=NaN,f=>delete f.levels[0].euler.paired_payoff_bias,f=>f.build={}]){const f=fixture();change(f);assert.throws(()=>validateSde(f,req()));}});
test('zero observed variance may withhold sampling intervals',()=>{const f=fixture();f.exact_price={mean:0,sample_variance:0,standard_error:null,ci_low:null,ci_high:null};validateSde(f,req());});
test('negative approximate sample states are not rejected or clipped',()=>{const f=fixture();f.levels[0].sample_paths[0][1].euler=-2;assert.equal(validateSde(f,req()).levels[0].sample_paths[0][1].euler,-2);});
test('typed references do not collide and reject traversal',()=>{assert.equal(typedReference('return_volatility:1'),'return_volatility:1');for(const s of ['1','sde_convergence:0','sde_convergence:../1','arbitrary:1'])assert.throws(()=>typedReference(s));});
test('saved binding and parent type enforced',()=>{const a=saved();validateTyped(a);a.configuration={};assert.throws(()=>validateTyped(a));const b=saved();b.parent_reference='option_pricing:1';assert.throws(()=>validateTyped(b));});
test('comparisons omit nondeterministic timing metadata',()=>{const a=saved(),b=saved();b.result.timing.total_ms=100;b.result.created_ms=22;assert.match(comparison(a,b),/match exactly/);});
test('canonical object comparison independent of property order',()=>assert.equal(stable({x:1,y:[2,3]}),stable({y:[2,3],x:1})));
