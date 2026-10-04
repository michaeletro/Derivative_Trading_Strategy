import test from 'node:test';
import assert from 'node:assert/strict';
import {parseDepth,depthView,aggregate,sessionPage,researchRequest,parseJobs,parseResult} from '../../src/frontend/dashboard/orderbook-model.mjs';
const frame=()=>({schema_version:1,kind:'displayed_depth',complete_exchange_book:false,individual_orders:false,direct_depth_only:true,
  time_basis:'local_callback_receipt',exchange_timestamp:null,source:'ibkr_tws',synthetic:false,available:true,
  request_id:'7',contract_id:'9001',venue:'BATS',requested_rows:10,active:true,structural_valid:true,quality:'two_sided_unverified',
  sequence:'20',epoch:'1',last_receipt_unix_us:'1700000000000000',last_event_age_ms:0,
  book:{bids:[{price:99,size:'30',market_maker:''}],asks:[{price:101,size:'10',market_maker:''}]}});
const session=()=>({session_id:'1',contract_id:'9001',symbol:'SYNTHETIC',currency:'USD',contract_route:'TESTEX',source:'mock',venue:'TESTEX',requested_rows:5,
  started_ms:1700000000000,ended_ms:1700000100000,state:'stop',event_count:'101'});
const form=()=>({mode:'inspect',quantity:'100',levels:'5',step_seconds:'1',horizon_seconds:'30',lookback_seconds:'30',max_side_age_seconds:'5',target:'buy_cost_bps',synthetic:true});
test('valid native payload and simple metrics',()=>{const v=depthView(parseDepth(frame()));assert.equal(v.midpoint,100);assert.equal(v.spread,2);assert.equal(v.imbalance,.5);assert.equal(v.microprice,100.5)});
test('synthetic remains explicitly synthetic',()=>{const f=frame();f.source='mock';f.synthetic=true;assert.match(depthView(parseDepth(f)).reason,/SYNTHETIC/)});
test('source cannot be silently relabeled',()=>{for(const x of [{source:'other'},{source:'mock'},{synthetic:true}])assert.throws(()=>parseDepth({...frame(),...x}))});
test('full book and exchange-time claims rejected',()=>{for(const x of [{complete_exchange_book:true},{time_basis:'exchange'},{individual_orders:true},{venue:'SMART'}])assert.throws(()=>parseDepth({...frame(),...x}))});
test('unknown or malformed rows rejected',()=>{for(const size of ['-1','NaN','1e-99999','1e99','<script>',1]){const f=frame();f.book.bids[0].size=size;assert.throws(()=>parseDepth(f))}});
test('missing is not zero depth',()=>{const f={...frame(),available:false,book:null};assert.equal(depthView(parseDepth(f)).depth,null);assert.throws(()=>parseDepth({...frame(),available:false}))});
test('duplicate-price rows aggregate for display',()=>{const f=frame();f.book.bids.push({price:99,size:'10.5',market_maker:'MM'});const v=depthView(parseDepth(f));assert.equal(v.bids.length,1);assert.equal(v.bids[0].size,40.5)});
test('requested rows are bounded but not padded',()=>{const p=parseDepth(frame());assert.equal(p.book.bids.length,1);assert.equal(p.requested_rows,10);assert.throws(()=>parseDepth({...p,requested_rows:11}))});
test('staleness includes elapsed browser time',()=>{assert.equal(depthView(frame(),5001).midpoint,null);assert.equal(depthView({...frame(),last_event_age_ms:null}).usable,false)});
test('crossed locked unordered zero-size withhold metrics',()=>{for(const price of [101,102]){const f=frame();f.book.bids[0].price=price;assert.equal(depthView(parseDepth(f)).midpoint,null)}const f=frame();f.book.bids[0].size='0';assert.equal(depthView(parseDepth(f)).usable,false)});
test('invalid and terminal states retain no usable metrics',()=>{for(const changes of [{active:false},{structural_valid:false},{quality:'local_sequence_gap'}])assert.equal(depthView({...frame(),...changes}).usable,false)});
test('catalog pagination validated',()=>{const p={schema_version:1,rows:[session()],has_more:true,next_after_id:'1'};assert.equal(sessionPage(p).rows.length,1);assert.throws(()=>sessionPage({...p,next_after_id:'0'}))});
test('mock comparison requires explicit acknowledgement',()=>{assert.throws(()=>researchRequest({...form(),synthetic:false},[session()]));assert.equal(researchRequest(form(),[session()]).source,'mock')});
test('running or duplicate captures cannot be submitted',()=>{assert.throws(()=>researchRequest(form(),[{...session(),state:'recording'}]));assert.throws(()=>researchRequest(form(),[session(),session()]))});
test('mixed feed identities refused',()=>{for(const change of [{venue:'BATS'},{source:'ibkr_tws'},{requested_rows:10},{contract_route:'SMART'}])assert.throws(()=>researchRequest(form(),[session(),{...session(),session_id:'2',...change}]))});
test('bad quantity, grids and time settings fail before requests',()=>{for(const change of [{quantity:'-1'},{quantity:'NaN'},{step_seconds:'7'},{levels:'0'},{horizon_seconds:'0'}])assert.throws(()=>researchRequest({...form(),...change},[session()]))});
test('chronological date partition preserved exactly',()=>{const f={...form(),mode:'compare',train:'2026-10-01, 2026-10-02',validation:'2026-10-03',test:'2026-10-04'};assert.deepEqual(researchRequest(f,[session()]).split.train,['2026-10-01','2026-10-02']);assert.throws(()=>researchRequest({...f,test:'2026-10-02'},[session()]));assert.throws(()=>researchRequest({...f,train:'2026-02-30'},[session()]))});
test('job IDs cannot become paths',()=>{const j={job_id:'a'.repeat(32),state:'complete',mode:'inspect',source:'mock',session_ids:['1']};assert.equal(parseJobs({schema_version:1,jobs:[j],has_more:false}).jobs.length,1);assert.throws(()=>parseJobs({schema_version:1,jobs:[{...j,job_id:'../x'}],has_more:false}))});
test('malformed result cannot be rendered',()=>{assert.throws(()=>parseResult({}));assert.throws(()=>parseResult({schema_version:1,kind:'orderbook_workspace_result',source:'ibkr_tws',sessions:[],request:{},source_hashes:null}))});
