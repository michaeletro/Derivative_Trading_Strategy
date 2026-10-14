import test from 'node:test';
import assert from 'node:assert/strict';
import {collectionRequest,collectionConfirmation,parseCollection,durationText} from '../../src/frontend/dashboard/collection-model.mjs';
import {mountCollection} from '../../src/frontend/dashboard/collection.mjs';

const selected=()=>({contract_id:9001,symbol:'SYNTHETIC',security_type:'STK',currency:'USD',venue:'BATS'});
const form=()=>({rows:'30',segment_minutes:'15',segments:'1',paper_read_only_confirmed:true});
const segment=()=>({index:1,state:'recording',reason:'',request_id:'7',session_id:'25',started_ms:1791811800000,ended_ms:null,elapsed_seconds:30,
  finalized:false,gap:false,archive_state:'recording',committed_event_count:'1234',committed_sequence:'1234',raw_committed_sequence:'1234',updates_observed:'1200',delivery_observed:true});
const plan=()=>({collection_id:'a'.repeat(32),state:'recording',reason:'',source:'mock',contract_id:'9001',symbol:'SYNTHETIC',currency:'USD',venue:'BATS',rows:30,
  segment_seconds:900,segments_requested:2,segments_completed:0,created_ms:1791811800000,updated_ms:1791811830000,finished_ms:null,paper_read_only_confirmed:true,
  requested_seconds:1800,elapsed_seconds:30,remaining_seconds:1770,current_segment:1,segments:[segment()],warnings:[],book_quality:'two_sided_unverified',last_update_age_seconds:.1});
const status=()=>{const p=plan();return {schema_version:1,active:true,current:p,plans:[p],catalog_limit:20,plan_limit:100,invalid_records:0,automatic_reconnect:false,automatic_resume:false,timer_basis:'steady_clock'};};
test('timed request fixes the chosen direct contract and exact server bounds',()=>{
  assert.deepEqual(collectionRequest(form(),selected()),{schema_version:1,contract_id:'9001',venue:'BATS',rows:30,segment_seconds:900,segments:1,paper_read_only_confirmed:true});
  assert.equal(collectionRequest({...form(),segment_minutes:60,segments:4},selected()).segments,4);
  assert.equal(collectionRequest({...form(),segment_minutes:1,segments:16,rows:50},{...selected(),venue:'IEX'}).venue,'IEX');
});
test('timed collection requires an explicit direct USD equity and fresh paper assertion',()=>{
  for(const changes of [{contract_id:0},{contract_id:'1/../../'},{currency:'EUR'},{security_type:'FUT'},{venue:'SMART'},null])
    assert.throws(()=>collectionRequest(form(),changes===null?null:{...selected(),...changes}));
  for(const confirmation of [undefined,false,'true',1])assert.throws(()=>collectionRequest({...form(),paper_read_only_confirmed:confirmation},selected()));
});
test('timed request refuses empty, fractional, boolean, oversized and total duration inputs',()=>{
  for(const changes of [{rows:''},{rows:0},{rows:51},{rows:1.5},{rows:true},{segment_minutes:''},{segment_minutes:0},{segment_minutes:61},{segment_minutes:1.5},{segments:0},{segments:17},{segments:true},{segment_minutes:60,segments:5}])
    assert.throws(()=>collectionRequest({...form(),...changes},selected()));
});
test('confirmation states boundaries, explicit lifetime and no automatic resumption',()=>{
  const text=collectionConfirmation(collectionRequest({...form(),segments:2},selected()),selected());
  for(const value of ['SYNTHETIC / 9001 on BATS','2 timed recording segments','30m','Boundaries can leave gaps','Closing this tab does not stop','will not reconnect or resume automatically','No orders will be submitted'])assert.ok(text.includes(value),value);
  assert.equal(durationText(14400),'4h 0m 0s');assert.equal(durationText(-1),'0s');assert.equal(durationText(NaN),'—');
});
test('collection status distinguishes active, completed, interrupted and empty history',()=>{
  const r=status();assert.equal(parseCollection(r).current.segments[0].committed_event_count,'1234');
  for(const state of ['starting','stopping']){const r=status();r.current.state=state;assert.equal(parseCollection(r).active,true);}
  const done=status();Object.assign(done.current,{state:'completed',segments_requested:1,requested_seconds:900,segments_completed:1,current_segment:null,finished_ms:1791812700000,remaining_seconds:0});
  Object.assign(done.current.segments[0],{state:'stopped',archive_state:'stop',finalized:true,ended_ms:1791812700000});done.active=false;assert.equal(parseCollection(done).current.state,'completed');
  done.current.state='interrupted';done.current.reason='broker_disconnected';done.current.segments_completed=0;done.current.segments[0].state='interrupted';done.current.segments[0].gap=true;done.current.segments[0].finalized=false;
  assert.equal(parseCollection(done).current.segments[0].gap,true);
  assert.equal(parseCollection({...status(),active:false,current:null,plans:[]}).current,null);
});
test('status rejects fabricated finalization, duplicate plans and broken ownership evidence',()=>{
  const mutations=[r=>r.schema_version=2,r=>r.active=false,r=>r.automatic_resume=true,r=>r.timer_basis='wall_clock',r=>r.catalog_limit=100,r=>r.invalid_records=-1,
    r=>r.current.collection_id='../escape',r=>r.current.contract_id=9001,r=>r.current.rows=51,r=>r.current.requested_seconds=900,r=>r.current.segments_completed=3,
    r=>r.current.elapsed_seconds=NaN,r=>r.current.remaining_seconds=-1,r=>r.current.current_segment=0,r=>r.current.paper_read_only_confirmed=false,
    r=>r.current.segments[0].index=2,r=>r.current.segments[0].committed_event_count=12,r=>r.current.segments[0].raw_committed_sequence='../private',r=>r.current.segments[0].delivery_observed='yes',
    r=>r.current.segments[0].finalized=true,r=>{r.current.state='completed';r.active=false;},r=>r.plans.push(r.current)];
  for(const mutate of mutations){const r=status();mutate(r);assert.throws(()=>parseCollection(r));}
});

// Minimal DOM for testing async ownership, not the rendered layout (covered by
// Playwright). In particular, reads here can fail after a new access generation.
function harness() {
  class Element {
    constructor(){this.children=[];this.listeners=new Map();this.value='';this.checked=false;this._text='';this.classList={toggle(){},add(){}};}
    get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
    set textContent(value){this._text=String(value);this.children=[];}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this._text='';this.children=nodes;}
    setAttribute(){}
    addEventListener(name,handler){this.listeners.set(name,handler);}
    fire(name){this.listeners.get(name)?.({preventDefault(){}});}
  }
  const priorDocument=globalThis.document,priorWindow=globalThis.window,nodes=new Map();
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  globalThis.document={getElementById:$,createElement:()=>new Element()};globalThis.window={confirm:()=>true};
  $('ob-collection-minutes').value='15';$('ob-collection-segments').value='1';$('ob-rows').value='30';
  let revision=1,workPromise=null;const getQueue=[],messages=[];
  const read=(path,method)=>{
    if(path==='/api/depth/collection'&&method==='GET')return getQueue.shift();
    if(path==='/api/depth/collection/start')return status();
    throw new Error('Unexpected request in collection test');
  };
  const manager=mountCollection({read,isCurrent:rev=>rev===revision,work:action=>{workPromise=action(revision);},afterMutation:async()=>{},notify:value=>messages.push(value)});
  const access={unlocked:true,busy:false,ready:true,loaded:true,reconcile:false,selected:selected(),live:null};manager.update(access);
  return {manager,$,getQueue,access,messages,setRevision:v=>{revision=v;},work:()=>workPromise,
    restore(){globalThis.document=priorDocument;globalThis.window=priorWindow;}};
}
const empty=()=>({...status(),active:false,current:null,plans:[]});
test('late superseded read failure cannot invalidate a newly unlocked generation',async()=>{
  const h=harness();
  try{
    let rejectOld;h.getQueue.push(new Promise((_,reject)=>{rejectOld=reject;}));const old=h.manager.refresh(1);
    h.setRevision(2);h.manager.update({...h.access,unlocked:false});h.manager.clear();
    assert.ok(!h.$('ob-collection-segment-list').textContent.includes('#25'));
    h.setRevision(3);h.manager.update(h.access);h.getQueue.push(status());await h.manager.refresh(3);
    rejectOld(new Error('Superseded workspace request.'));await assert.rejects(old);
    assert.equal(h.manager.active(),true);assert.ok(h.$('ob-collection-health').textContent.includes('refreshed within'));
    assert.ok(!h.$('ob-collection-health').textContent.includes('unavailable'));assert.equal(h.$('ob-collection-stop').disabled,false);
  }finally{h.restore();}
});
test('an older quiet GET cannot overwrite an acknowledged collection mutation',async()=>{
  const h=harness();
  try{
    h.getQueue.push(empty());await h.manager.refresh(1);
    let resolveOld;h.getQueue.push(new Promise(resolve=>{resolveOld=resolve;}));const old=h.manager.refresh(1);
    h.$('ob-collection-paper').checked=true;h.$('ob-collection-form').fire('submit');await h.work();
    assert.equal(h.manager.active(),true);resolveOld(empty());await old;
    assert.equal(h.manager.active(),true);assert.ok(h.$('ob-collection-status').textContent.includes('recording'));
    assert.ok(h.messages.at(-1).includes('acknowledged'));assert.equal(h.$('ob-collection-paper').checked,false);
  }finally{h.restore();}
});
test('background workspace reads do not disable duration entry while mutations stay blocked',async()=>{
  const h=harness();
  try{
    h.getQueue.push(empty());await h.manager.refresh(1);h.manager.update({...h.access,busy:true});
    assert.equal(h.$('ob-collection-fields').disabled,false);assert.equal(h.$('ob-collection-start').disabled,true);assert.equal(h.$('ob-collection-paper').disabled,true);
  }finally{h.restore();}
});
test('recovered checkpoints label duration and update totals as lower bounds',async()=>{
  const h=harness();
  try{
    const r=status();r.active=false;Object.assign(r.current,{state:'interrupted',reason:'server_restart',current_segment:null,checkpoint_metrics_incomplete:true,elapsed_seconds:0});
    Object.assign(r.current.segments[0],{state:'interrupted',reason:'server_restart',gap:true,updates_observed:'0',delivery_observed:false,elapsed_seconds:0});
    h.getQueue.push(r);await h.manager.refresh(1);
    assert.ok(h.$('ob-collection-status').textContent.includes('at least 0s at last durable checkpoint'));
    const evidence=h.$('ob-collection-segment-list').textContent;
    assert.ok(evidence.includes('at least 0 depth updates at checkpoint'));assert.ok(evidence.includes('full delivery unknown'));
    assert.ok(evidence.includes('1234'));assert.ok(!evidence.includes('delivery not observed'));
    r.current.checkpoint_metrics_incomplete='true';assert.throws(()=>parseCollection(r));
  }finally{h.restore();}
});
