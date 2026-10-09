import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRulePresets,parseRulePreview,dollarsToCents,wholeNumber,createRulePlannerController,reasonText} from '../../src/frontend/dashboard/execution-rules-model.mjs';

export function presets() {
  const policy={declared_balance_cents:50000,budget_cents:50000,cash_reserve_cents:5000,max_order_notional_cents:10000,
    max_gross_exposure_cents:45000,daily_loss_limit_cents:500,fee_reserve_cents:200,max_buy_decisions_per_day:20,max_open_orders:1,
    max_quote_age_ms:2000,max_risk_age_ms:2000,max_spread_bps:10};
  return {schema_version:1,kind:'execution_rule_presets',scenario_only:true,execution_enabled:false,
    profiles:[{...policy,id:'live_500',label:'$500 live draft'},{...policy,id:'paper_500',label:'$500 paper rehearsal'}],limitations:['Scenario only. No orders transmitted.']};
}
export function scenario() {
  return {side:'BUY',contract_id:'756733',symbol:'SPY',security_type:'STK',currency:'USD',instrument_allowed:true,
    bid_cents:999,ask_cents:1001,limit_price_cents:1000,min_tick_cents:1,requested_shares:3,available_cash_cents:50000,
    committed_gross_exposure_cents:0,available_long_shares:0,day_pnl_cents:0,buy_decisions_today:0,open_orders:0,
    quote_age_ms:0,risk_age_ms:0,realtime:true,rth:true,risk_state_known:true};
}
export function preview(profile_id='live_500') {
  return {schema_version:1,kind:'execution_rule_preview',profile_id,scenario_only:true,execution_enabled:false,
    decision:'pass',quantity_shares:3,order_notional_cents:3000,estimated_total_cents:3200,blocking_reasons:[],limitations:['Scenario only.']};
}
const controller=request=>{const c=createRulePlannerController({request});c.setAccess(true);return c;};

test('rule presets require scenario-only disabled execution and consistent finite limits',()=>{
  assert.equal(parseRulePresets(presets()).profiles[0].budget_cents,50000);
  for(const change of [p=>p.execution_enabled=true,p=>p.scenario_only=false,p=>p.profiles[0].cash_reserve_cents=50000,
    p=>p.profiles[0].budget_cents=50001,p=>p.profiles[0].max_order_notional_cents=50000,p=>p.profiles[0].max_spread_bps=NaN,
    p=>p.profiles[0].max_risk_age_ms=-1,p=>p.profiles.push({...p.profiles[0]}),p=>p.limitations=['unsafe\ntext']]) {
    const p=presets();change(p);assert.throws(()=>parseRulePresets(p));
  }
});
test('preview contract never accepts execution permissions or contradictory sizing',()=>{
  const request={...scenario(),profile_id:'live_500'};assert.equal(parseRulePreview(preview(),request).quantity_shares,3);
  for(const change of [p=>p.execution_enabled=true,p=>p.profile_id='paper_500',p=>p.quantity_shares=4,p=>p.order_notional_cents=3001,
    p=>p.estimated_total_cents=2999,p=>p.blocking_reasons=['some_rejection'],p=>p.quantity_shares=0,p=>p.decision='approved',p=>p.scenario_only=false]) {
    const p=preview();change(p);assert.throws(()=>parseRulePreview(p,request));
  }
  const blocked={...preview(),decision:'blocked',quantity_shares:0,order_notional_cents:0,estimated_total_cents:0,blocking_reasons:['whole_share_exceeds_order_cap']};
  assert.equal(parseRulePreview(blocked,request).decision,'blocked');
  assert.throws(()=>parseRulePreview({...blocked,blocking_reasons:[]},request));
  assert.throws(()=>parseRulePreview({...blocked,estimated_total_cents:200},request));
});
test('money inputs retain exact cents and reject fractional cents, exponent notation and unsafe values',()=>{
  assert.equal(dollarsToCents('0.29'),29);assert.equal(dollarsToCents('500'),50000);assert.equal(dollarsToCents('1.2'),120);
  assert.equal(dollarsToCents('-5.01',{signed:true}),-501);
  for(const value of ['1.001','1e3','NaN','Infinity','-1','0x10','9007199254740992'])assert.throws(()=>dollarsToCents(value));
  assert.throws(()=>wholeNumber('1.5','Shares'));assert.throws(()=>wholeNumber('0','Shares',{positive:true}));assert.equal(wholeNumber('0','Orders'),0);
});
test('unlock, preset selection and editing never fetch or submit; explicit load uses only the scenario endpoint',async()=>{
  const calls=[],c=controller(async(...args)=>{calls.push(args);return presets();});
  c.selectProfile('live_500');c.invalidate();assert.equal(calls.length,0);await c.preview(scenario());assert.equal(calls.length,0);
  await c.load();assert.deepEqual(calls,[['/api/trading/rules']]);assert.equal(c.state().selected,'live_500');
  c.selectProfile('paper_500');assert.equal(c.state().selected,'paper_500');assert.equal(calls.length,1);
  c.selectProfile('invalid');assert.equal(c.state().selected,'');assert.equal(calls.length,1);
});
test('preview request contains the chosen policy and supplied scenario without account identity or transmission controls',async()=>{
  const calls=[],c=controller(async(path,method,body)=>{calls.push({path,method,body});return method==='POST'?preview(body.profile_id):presets();});
  await c.load();c.selectProfile('paper_500');await c.preview(scenario());
  assert.deepEqual(calls[1],{path:'/api/trading/rules/preview',method:'POST',body:{...scenario(),profile_id:'paper_500'}});
  assert.equal(c.state().result.profile_id,'paper_500');assert.equal(c.state().result.execution_enabled,false);
  c.invalidate();assert.equal(c.state().result,null);
});
test('sell preview retains its supplied side and accepts gross proceeds before fees',async()=>{
  const c=controller(async(path,method,body)=>method==='POST'?{...preview(body.profile_id),estimated_total_cents:3000}:presets());
  await c.load();await c.preview({...scenario(),side:'SELL',available_long_shares:3});
  assert.equal(c.state().result.side,'SELL');assert.equal(c.state().result.estimated_total_cents,3000);
});
test('sign-out rejects a late load and clears all scenario state',async()=>{
  let finish;const c=controller(()=>new Promise(resolve=>{finish=resolve;}));const pending=c.load();c.setAccess(false);finish(presets());await pending;
  assert.equal(c.state().presets,null);assert.equal(c.state().result,null);assert.equal(c.state().busy,false);
});
test('an edit during a preview rejects the stale response and leaves no current sizing',async()=>{
  let finish;const c=controller(async(path,method)=>method==='POST'?new Promise(resolve=>{finish=resolve;}):presets());
  await c.load();const pending=c.preview(scenario());c.invalidate();finish(preview());await pending;
  assert.equal(c.state().result,null);assert.equal(c.state().busy,false);assert.match(c.state().message,/Scenario changed/);
});
test('duplicate preview clicks are suppressed and failed requests are never retried automatically',async()=>{
  let finish,posts=0;const c=controller(async(path,method)=>{if(method==='POST'){posts++;return new Promise(resolve=>{finish=resolve;});}return presets();});
  await c.load();const pending=c.preview(scenario());await c.preview(scenario());assert.equal(posts,1);finish(preview());await pending;
  const failed=controller(async(path,method)=>{if(method==='POST'){posts++;throw new Error('Transport unavailable');}return presets();});
  await failed.load();await failed.preview(scenario());assert.equal(posts,2);assert.equal(failed.state().result,null);assert.match(failed.state().message,/No order was sent/);
});
test('a failed preset reload clears obsolete presets before any new preview',async()=>{
  let reads=0,posts=0;const c=controller(async(path,method)=>{if(method==='POST'){posts++;return preview();}if(++reads===1)return presets();throw new Error('Preset service failed');});
  await c.load();await c.load();assert.equal(c.state().presets,null);assert.equal(c.state().selected,'');await c.preview(scenario());assert.equal(posts,0);
});
test('authorization rejection clears presets and reaches the dashboard access handler',async()=>{
  let seen;const c=createRulePlannerController({request:async()=>{throw Object.assign(new Error('Rejected'),{status:403});}},()=>{},e=>{seen=e;c.setAccess(false);});
  c.setAccess(true);await c.load();assert.equal(seen.status,403);assert.equal(c.state().presets,null);assert.equal(c.state().access,false);
});
test('unknown rejection codes remain readable without changing the scenario decision',()=>{
  assert.equal(reasonText('new_broker_constraint'),'new broker constraint');assert.match(reasonText('whole_share_exceeds_order_cap'),/No whole share/);
});
