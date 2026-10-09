import test from 'node:test';
import assert from 'node:assert/strict';
import {resolveRoute,routes} from '../../src/frontend/dashboard/navigation.mjs';

test('legacy and new live URLs select the same preserved order book',()=>{
  for(const hash of ['', '#overview','#live','#orderbook'])assert.equal(resolveRoute(hash).section,'orderbook');
  assert.equal(resolveRoute('#overview').route,'orderbook');
});
test('primary research routes reuse existing book panels without changing their data',()=>{
  for(const key of ['recordings','quality','research','results']){
    const value=resolveRoute('#'+key);assert.equal(value.workspace,key);assert.equal(value.section,'orderbook');
  }
  assert.equal(resolveRoute('#results').orderbookTab,'results');
  assert.equal(resolveRoute('#research').orderbookTab,'recordings');
});
test('old deep links retain their numerical, archive and broker destinations',()=>{
  for(const key of ['trading','history','ticks','replay','storage','positions','pricing','sensitivities','sde','hedging','variation','lab-catalog','roadmap'])assert.equal(resolveRoute('#'+key).section,key);
  assert.equal(resolveRoute('#instruments').section,'market-workspace');
});
test('launch tickets remain exclusively owned by the sign-in handler',()=>{
  for(const value of ['#local-signin='+ 'a'.repeat(64),'#local-signin=bad','#local-signin='])assert.equal(resolveRoute(value),null);
});
test('unknown, encoded, external and prototype routes never select arbitrary elements',()=>{
  for(const value of ['#constructor','#__proto__','#toString','#https://example.com','#%72esults','#results?token=x','#positions/../results'])assert.equal(resolveRoute(value).route,'orderbook');
  assert.equal(resolveRoute(null),null);
  assert.equal(Object.values(routes).every(route=>typeof route.title==='string'&&route.title.length>0),true);
});
