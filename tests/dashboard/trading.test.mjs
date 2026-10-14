import test from 'node:test';
import assert from 'node:assert/strict';
import {parseTradingStatus,createTradingController,componentText,ageText,monitorScopes,maskedAccount} from '../../src/frontend/dashboard/trading-model.mjs';

export function fixture() {
  return {schema_version:1,kind:'trading_status',source:'ibkr_tws',synthetic:false,
    broker:{state:'ready',enabled:true,generation:'fixture-1'},order_execution_enabled:false,account_mode:'unverified',strategy_state:'not_configured',accounts_status:'complete',accounts:['FIXTURE-A','FIXTURE-B'],
    monitor:{state:'unavailable',request_id:null,account:null,started_at_unix_ms:null,last_update_unix_ms:null,started_age_seconds:null,last_update_age_seconds:null,start_available:true,restart_requires_reconnect:false,error:null,
      components:{positions:'unavailable',account_summary:'unavailable',open_orders:'unavailable',executions:'unavailable'},
      components_meta:Object.fromEntries(Object.keys(monitorScopes).map(k=>[k,{completed_age_seconds:null,last_update_age_seconds:null,error_code:null}])),
      positions:[],account_values:[],open_orders:[],executions:[],scopes:{...monitorScopes}},blocking_reasons:['order_submission_not_implemented','risk_limits_missing']};
}
const context=(session_id='fixture-1')=>({session_id,broker:{state:'ready'}});
const active=p=>{p.monitor.state='active';p.monitor.request_id='7';p.monitor.account='FIXTURE-B';p.monitor.start_available=false;p.monitor.restart_requires_reconnect=true;return p;};
function controller(request) {const c=createTradingController({request});c.setAccess(true);c.updateContext(context());return c;}

test('trading contract never accepts writable or inferred account mode',()=>{
  assert.equal(parseTradingStatus(fixture()).account_mode,'unverified');
  for(const update of [{order_execution_enabled:true},{account_mode:'live'},{strategy_state:'armed'},{synthetic:true},{source:'disabled'},{accounts:['FIXTURE-A','FIXTURE-A']}])
    assert.throws(()=>parseTradingStatus({...fixture(),...update}));
  assert.throws(()=>parseTradingStatus({...fixture(),source:'mock',synthetic:true}));
});
test('nullable broker fields, exact quantities and manual signed order IDs remain explicit',()=>{
  const p=active(fixture());p.monitor.components.positions='complete';p.monitor.components.open_orders='complete';p.monitor.components.executions='complete';
  p.monitor.positions=[{contract_id:'42',symbol:'DEMO',currency:'USD',security_type:'STK',model_code:'fixture-model',quantity:'-0.123456789012345678',average_cost:null}];
  p.monitor.open_orders=[{order_id:'-5',perm_id:'8',client_id:'0',contract_id:'42',symbol:'DEMO',action:'SELL',order_type:'LMT',quantity:'0.123456789012345678',limit_price:null,status:'Submitted',filled:null,remaining:null,order_ref:''}];
  p.monitor.executions=[{execution_id:'execution-fixture',order_id:'-5',perm_id:'8',contract_id:'42',symbol:'DEMO',side:'SLD',quantity:'0.01',price:null,time:'broker supplied'}];
  assert.equal(parseTradingStatus(p).monitor.positions[0].quantity,'-0.123456789012345678');
  assert.equal(p.monitor.executions[0].price,null);
  p.monitor.positions[0].average_cost=NaN;assert.throws(()=>parseTradingStatus(p));
});
test('malformed quantities, scopes and snapshot ages invalidate the whole account response',()=>{
  for(const change of [p=>p.monitor.components.positions='empty',p=>p.monitor.scopes.open_orders='continuous',p=>p.monitor.last_update_age_seconds=-1,
    p=>p.monitor.components_meta.positions.completed_age_seconds=Infinity,p=>p.accounts=['account\ntext'],p=>p.monitor.state='armed',
    p=>p.monitor.positions=[{contract_id:'1',symbol:'X',currency:'USD',security_type:'STK',quantity:'NaN',average_cost:1}]]) {
    const p=fixture();change(p);assert.throws(()=>parseTradingStatus(p));
  }
});
test('partial and unavailable snapshots never claim zero exposure; ages preserve missing data',()=>{
  assert.match(componentText('pending',2,'Positions'),/initial snapshot incomplete; rows withheld/);
  assert.equal(componentText('unavailable',0,'Positions'),'Positions: unavailable');
  assert.equal(ageText(null),'Unavailable');assert.equal(ageText(2,1),'3.0s ago');
  assert.equal(maskedAccount('FIXTURE-ACCOUNT'),'••••OUNT');assert.equal(maskedAccount(null),'Unavailable');
});
test('a complete component can be displayed while others remain pending; incomplete rows are rejected',()=>{
  const p=active(fixture());p.monitor.state='pending';p.monitor.components.account_summary='complete';p.monitor.components.positions='pending';
  p.monitor.account_values=[{tag:'NetLiquidation',value:'1.23',currency:'USD'}];
  assert.equal(parseTradingStatus(p).monitor.components.positions,'pending');
  p.monitor.positions=[{contract_id:'42',symbol:'X',currency:'USD',security_type:'STK',quantity:'1',average_cost:1}];assert.throws(()=>parseTradingStatus(p));
});
test('access, context, and account selection do not issue a request; load is explicit',async()=>{
  const calls=[],c=controller(async(...args)=>{calls.push(args);return fixture();});
  c.selectAccount('FIXTURE-B');assert.equal(c.state().selected,'');assert.equal(calls.length,0);
  await c.start();assert.equal(calls.length,0);
  await c.refresh();assert.deepEqual(calls.map(r=>r.slice(0,2)),[['/api/trading/status']]);assert.equal(c.state().selected,'');
  c.selectAccount('not-managed');await c.start();assert.equal(calls.length,1);
});
test('monitor commands use exact chosen account and stop requires a reconnect before restart',async()=>{
  const calls=[];let p=fixture();const c=controller(async(path,method,body)=>{
    calls.push({path,method,body});
    if(path==='/api/trading/monitor')p=active(p);
    if(path==='/api/trading/stop'){p.monitor.state='stopped';p.monitor.components.positions='unavailable';}
    return structuredClone(p);
  });
  await c.refresh();c.selectAccount('FIXTURE-B');await c.start();
  assert.deepEqual(calls.find(r=>r.path==='/api/trading/monitor').body,{account:'FIXTURE-B'});
  await c.stop();await c.start();assert.equal(calls.filter(r=>r.path==='/api/trading/monitor').length,1);
  assert.equal(c.state().data.monitor.state,'stopped');assert.equal(c.state().data.monitor.restart_requires_reconnect,true);
});
test('sign-out suppresses a late successful read and clears selected account',async()=>{
  let finish;const c=controller(()=>new Promise(resolve=>{finish=resolve;}));
  const pending=c.refresh();c.setAccess(false);finish(fixture());await pending;
  assert.equal(c.state().data,null);assert.equal(c.state().selected,'');assert.equal(c.state().loaded,false);
});
test('broker generation changes reject previous in-flight response',async()=>{
  let finish;const c=controller(()=>new Promise(resolve=>{finish=resolve;}));
  const pending=c.refresh();c.updateContext(context('fixture-2'));finish(fixture());await pending;
  assert.equal(c.state().data,null);assert.equal(c.state().busy,false);assert.equal(c.state().loaded,false);
});
test('loss of the main broker view rejects an in-flight first status response',async()=>{
  let finish;const c=controller(()=>new Promise(resolve=>{finish=resolve;}));
  const pending=c.refresh();c.updateContext(null);finish(fixture());await pending;
  assert.equal(c.state().data,null);assert.equal(c.state().contextReady,false);assert.equal(c.state().busy,false);
});
test('mismatched authoritative generation requires refresh rather than publishing stale rows',async()=>{
  const c=controller(async()=>({...fixture(),broker:{state:'ready',enabled:true,generation:'old-instance'}}));
  await c.refresh();assert.equal(c.state().data,null);assert.equal(c.state().loaded,false);assert.match(c.state().message,/generation changed/);
});
test('uncertain monitor mutation is not retried and only an explicit read reconciles it',async()=>{
  const calls=[];let p=fixture();const c=controller(async(path,method)=>{
    calls.push({path,method});if(method==='POST'){p=active(p);throw new Error('transport lost');}return structuredClone(p);
  });
  await c.refresh();c.selectAccount('FIXTURE-B');await c.start();
  assert.equal(c.state().uncertain,true);assert.equal(c.state().data,null);assert.match(c.state().message,/outcome uncertain/);
  await c.start();await c.stop();assert.equal(calls.length,2);
  await c.refresh();assert.equal(c.state().uncertain,false);assert.equal(c.state().data.monitor.state,'active');
  assert.equal(calls.filter(r=>r.method==='POST').length,1);
});
test('acknowledged command followed by failed reconciliation remains uncertain',async()=>{
  let gets=0;const c=controller(async(path,method)=>{if(method==='POST')return {status:'pending'};if(++gets>1)throw new Error('read failed');return fixture();});
  await c.refresh();c.selectAccount('FIXTURE-B');await c.start();assert.equal(c.state().uncertain,true);assert.equal(c.state().data,null);
});
test('duplicate clicks are suppressed while a monitor mutation is in flight',async()=>{
  let release,posts=0;const c=controller(async(path,method)=>{if(method==='POST'){posts++;return new Promise(resolve=>{release=resolve;});}return fixture();});
  await c.refresh();c.selectAccount('FIXTURE-B');const pending=c.start();await c.start();assert.equal(posts,1);release({});await pending;
});
test('authorization rejection clears data and reaches the access handler',async()=>{
  let accessError=null;const c=createTradingController({request:async()=>{throw Object.assign(new Error('rejected'),{status:401});}},()=>{},e=>{accessError=e;c.setAccess(false);});
  c.setAccess(true);c.updateContext(context());await c.refresh();assert.equal(accessError.status,401);assert.equal(c.state().data,null);assert.equal(c.state().access,false);
});
