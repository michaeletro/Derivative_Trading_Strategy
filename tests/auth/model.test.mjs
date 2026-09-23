import test from 'node:test';
import assert from 'node:assert/strict';
import {createApi} from '../../src/frontend/dashboard/model.mjs';
import {takeLaunchCode,validSessionStatus} from '../../src/frontend/dashboard/local-signin.mjs';
const code='a'.repeat(64);
test('launch fragment is erased before consumption',()=>{
  let cleaned='';const ticket=takeLaunchCode({hash:'#local-signin='+code,pathname:'/',search:''},{replaceState:(_,__,url)=>cleaned=url});
  assert.equal(ticket,code);assert.equal(cleaned,'/#overview');assert.ok(!cleaned.includes(code));
});
test('malformed launch fragments are erased and refused',()=>{
  let clean=false;assert.throws(()=>takeLaunchCode({hash:'#local-signin=bad&redirect=evil',pathname:'/',search:''},{replaceState:()=>clean=true}));assert.ok(clean);
});
test('normal navigation fragments are not consumed',()=>assert.equal(takeLaunchCode({hash:'#hedging'},{}),null));
test('status must have a known schema and boolean authentication',()=>{
  assert.ok(validSessionStatus({schema_version:1,authenticated:true}));
  assert.ok(!validSessionStatus({schema_version:2,authenticated:true}));
  assert.ok(!validSessionStatus({schema_version:1,authenticated:'yes'}));
});
test('session mode uses same-origin cookies and explicit local-request header without bearer',async()=>{
  let seen;const api=createApi(async(_,options)=>{seen=options;return {ok:true,status:200,json:async()=>({})};});
  api.setToken('manual-fixture');api.useBrowserSession();await api.request('/api/dashboard');
  assert.equal(seen.credentials,'same-origin');assert.equal(seen.headers['X-DTS-Local-Request'],'1');assert.ok(!seen.headers.Authorization);
  assert.equal(seen.mode,'same-origin');assert.equal(seen.redirect,'error');
});
test('clear and manual override do not reuse ambient cookie credentials',async()=>{
  let seen;const api=createApi(async(_,options)=>{seen=options;return {ok:true,status:200,json:async()=>({})};});
  api.useBrowserSession();api.clear();await api.request('/api/dashboard');assert.equal(seen.credentials,'omit');
  api.useBrowserSession();api.setToken('manual-fixture');await api.request('/api/dashboard');
  assert.equal(seen.credentials,'omit');assert.equal(seen.headers.Authorization,'Bearer manual-fixture');
});
test('auto session mode still rejects external, query-token and traversal paths',async()=>{
  let calls=0;const api=createApi(async()=>{calls++;});api.useBrowserSession();
  for(const path of ['https://evil.example/api/dashboard','/api/auth/exchange?code=secret','/api/../auth/session','//evil.example'])await assert.rejects(api.request(path));
  assert.equal(calls,0);
});
