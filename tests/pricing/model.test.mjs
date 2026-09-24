import test from 'node:test';
import assert from 'node:assert/strict';
import {validateRequest,requestFromForm,importRequest,validateRecord,KIND,ENGINE,FIELDS} from '../../src/frontend/dashboard/pricing-model.mjs';
const request=()=>({schema_version:1,exercise_style:'european',right:'call',spot:100,strike:100,maturity_years:1,rate:.05,dividend_yield:0,volatility:.2,paths:100000,seed:'42',method:'plain',currency:'USD'});
const saved=()=>({kind:KIND,schema_version:1,request:request(),model:{engine_version:ENGINE,input_source:'manual_scenario_not_broker_data',scheme:'exact_terminal_gbm'},build:{pricing_source_sha256:'f'.repeat(64)},result:{analytical_price:10.45,monte_carlo:{price:10.5,sample_variance:100,standard_error:.1,ci_low:10.3,ci_high:10.7},runtime_ms:5,deterministic:false,paths_evaluated:100000,independent_samples:100000,positive_payoffs:50000,warnings:[],convergence:[{paths:100000,independent_samples:100000,price:10.5,sample_variance:100,standard_error:.1,ci_low:10.3,ci_high:10.7}]}});
test('decimal form inputs are not percentage-transformed',()=>{
  const data=new Map(Object.entries(request()).map(([k,v])=>[k,String(v)]));
  assert.deepEqual(requestFromForm(data),request());
  data.set('rate','0.031234567890123456');assert.equal(requestFromForm(data).rate,.031234567890123456);
});
test('full uint64 seeds remain strings',()=>{assert.equal(validateRequest({...request(),seed:'18446744073709551615'}).seed,'18446744073709551615');});
test('seed canonicalization',()=>assert.equal(validateRequest({...request(),seed:'00042'}).seed,'42'));
test('numeric and overflowing seeds rejected',()=>{for(const seed of [42,'18446744073709551616','+42','1e3','-1',''])assert.throws(()=>validateRequest({...request(),seed}));});
test('all model domains fail closed',()=>{
  for(const [key,value] of [['spot',-1],['strike',0],['maturity_years',-1],['rate',.6],['volatility',NaN],['dividend_yield',Infinity],['currency','<x>']])assert.throws(()=>validateRequest({...request(),[key]:value}));
});
test('unsupported early exercise and unknown fields rejected',()=>{
  assert.throws(()=>validateRequest({...request(),exercise_style:'american'}));
  assert.throws(()=>validateRequest({...request(),contract_id:123}));
  const r=request();delete r.right;assert.throws(()=>validateRequest(r));
});
test('simulation workload cap and pair count',()=>{
  for(const paths of [1,1000.5,2000001])assert.throws(()=>validateRequest({...request(),paths}));
  assert.throws(()=>validateRequest({...request(),paths:1001,method:'antithetic'}));
});
test('blank numeric form field is not implicitly zero',()=>{
  const form=new Map(Object.entries(request()).map(([k,v])=>[k,String(v)]));
  form.set('dividend_yield','');assert.throws(()=>requestFromForm(form));
});
test('saved file imports inputs only, not hostile outputs',()=>{
  const record=saved();record.result={price:'<img onerror=alert(1)>'};
  assert.deepEqual(importRequest(JSON.stringify(record)),request());
});
test('wrong schema, engine, kind and huge import rejected',()=>{
  assert.throws(()=>importRequest('x'.repeat(131073)));assert.throws(()=>importRequest('{'));
  for(const changes of [{kind:'orders'},{schema_version:2},{model:{engine_version:'heston'}}])assert.throws(()=>importRequest(JSON.stringify({...saved(),...changes})));
});
test('all request fields survive export import roundtrip',()=>{
  const r=saved();r.request.seed='18446744073709551615';r.request.volatility=.23456789012345678;
  const imported=importRequest(JSON.stringify(r));for(const k of FIELDS)assert.equal(imported[k],r.request[k]);
});
test('real result shape accepted with matching inputs',()=>assert.equal(validateRecord(saved(),request()).result.monte_carlo.price,10.5));
test('response must match submitted seed and model inputs',()=>{
  const r=saved();r.request.seed='43';assert.throws(()=>validateRecord(r,request()));
});
test('nonfinite, missing and incomplete results rejected',()=>{
  let r=saved();r.result.monte_carlo.price=Infinity;assert.throws(()=>validateRecord(r,request()));
  r=saved();r.result.convergence=[];assert.throws(()=>validateRecord(r,request()));
  r=saved();r.result.independent_samples=50000;assert.throws(()=>validateRecord(r,request()));
});
test('zero-observed-variance uncertainty may be null',()=>{
  const r=saved();r.result.monte_carlo={price:0,sample_variance:0,standard_error:null,ci_low:null,ci_high:null};
  r.result.convergence=[{...r.result.monte_carlo,paths:100000,independent_samples:100000}];
  assert.equal(validateRecord(r,request()).result.monte_carlo.ci_low,null);
});
test('antithetic result sample count is pairs, not raw payoffs',()=>{
  const r=saved();r.request.method='antithetic';r.result.independent_samples=50000;r.result.convergence[0].independent_samples=50000;
  assert.equal(validateRecord(r,r.request).result.independent_samples,50000);
});
