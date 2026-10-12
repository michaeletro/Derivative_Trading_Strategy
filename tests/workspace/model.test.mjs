import test from 'node:test';
import assert from 'node:assert/strict';
import {parseDepth,depthView,appendLiveSample,liveSegments,aggregate,sessionPage,researchRequest,parseJobs,parseResult,replayEventPage,flowNumber} from '../../src/frontend/dashboard/orderbook-model.mjs';
const frame=()=>({schema_version:1,kind:'displayed_depth',complete_exchange_book:false,individual_orders:false,direct_depth_only:true,
  time_basis:'local_callback_receipt',exchange_timestamp:null,source:'ibkr_tws',synthetic:false,available:true,
  request_id:'7',contract_id:'9001',venue:'BATS',requested_rows:10,active:true,structural_valid:true,quality:'two_sided_unverified',
  sequence:'20',epoch:'1',last_receipt_unix_us:'1700000000000000',last_event_age_ms:0,
  book:{bids:[{price:99,size:'30',market_maker:''}],asks:[{price:101,size:'10',market_maker:''}]}});
const session=()=>({session_id:'1',contract_id:'9001',symbol:'SYNTHETIC',currency:'USD',contract_route:'TESTEX',source:'mock',venue:'TESTEX',requested_rows:5,
  started_ms:1700000000000,ended_ms:1700000100000,state:'stop',event_count:'101'});
const form=()=>({mode:'inspect',quantity:'100',levels:'5',step_seconds:'1',horizon_seconds:'30',lookback_seconds:'30',max_side_age_seconds:'5',target:'buy_cost_bps',synthetic:true});
const shapeForm=()=>({mode:'shape',synthetic:true,shape_levels:'5',shape_step_seconds:'1',shape_max_side_age_seconds:'2',shape_calibration_fraction:'0.5',shape_near_band_bps:'5',shape_imbalance_threshold:'0.2',shape_max_hold_seconds:'60',shape_fee_per_side_usd:'1',shape_slippage_bps:'0',shape_paper_budget_usd:'10000',shape_max_order_notional_usd:'1000',shape_strategy_variant:'compare_regimes'});
test('shape request freezes independent settings and retains affordability as a replay rule',()=>{
  const r=researchRequest({...shapeForm(),shape_paper_budget_usd:'500',shape_max_order_notional_usd:'1000',quantity:'bad'},[session()]);
  assert.equal(r.mode,'shape');assert.equal(r.configuration.paper_budget_usd,500);assert.equal(r.configuration.max_order_notional_usd,1000);assert.equal(r.split,null);
  assert.deepEqual(Object.keys(r.configuration).sort(),['levels','step_seconds','max_side_age_seconds','calibration_fraction','near_band_bps','imbalance_threshold','max_hold_seconds','fee_per_side_usd','slippage_bps','paper_budget_usd','max_order_notional_usd','strategy_variant'].sort());
});
test('shape request rejects unsupported bounds, empty settings and unacknowledged synthetic data',()=>{
  for(const change of [{shape_levels:1},{shape_levels:51},{shape_step_seconds:.5},{shape_max_side_age_seconds:.01},{shape_calibration_fraction:.1},{shape_near_band_bps:101},{shape_imbalance_threshold:1.1},{shape_fee_per_side_usd:-1},{shape_paper_budget_usd:99},{shape_max_order_notional_usd:100001},{shape_strategy_variant:'live'},{shape_slippage_bps:''},{synthetic:false}])assert.throws(()=>researchRequest({...shapeForm(),...change},[session()]));
  assert.throws(()=>researchRequest(shapeForm(),[{...session(),state:'recording'}]));
  assert.equal(parseJobs({schema_version:1,has_more:false,jobs:[{job_id:'a'.repeat(32),mode:'shape',source:'mock',state:'complete',session_ids:['1']}]}).jobs[0].mode,'shape');
});
const emptyShapeDistribution=()=>({count:0,mean:null,std:null,min:null,p05:null,p25:null,median:null,p75:null,p95:null,max:null,skewness:null,excess_kurtosis:null,histogram:{edges:[],counts:[]}});
const emptyShapeProfiles=()=>Object.fromEntries(['bid','ask'].map(side=>[side,{distance_edges_bps:[0,1],samples:0,cdf_mean:[],mass_mean:[]}]));
const shapeResult=()=>({schema_version:1,kind:'orderbook_workspace_result',source:'synthetic',source_hashes:[],request:{mode:'shape',configuration:researchRequest(shapeForm(),[session()]).configuration},artifacts:[],sessions:[{identity:{session_id:'1'},frames:[],clock:{status:'blocked_clock',max_divergence_seconds:2,wall_regressions:0,monotonic_regressions:0},coverage:{grid_points:0,qualified_samples:0,excluded_samples:0}}],shape:{status:'blocked_clock',definitions:{},weighting:'equal_regular_hours_grid_states_and_per_snapshot_size_normalization',coverage:{grid_points:0,qualified_samples:0,excluded_samples:0,exclusions:{blocked_clock:1}},calibration:{status:'insufficient_samples',samples:0,test_samples:0,cutoff_unix_us:null,threshold_tight_bps:null,threshold_loose_bps:null},distributions:{tightness_bps:emptyShapeDistribution()},side_profiles:emptyShapeProfiles(),regimes:Object.fromEntries(['tight','normal','loose'].map(key=>[key,{samples:0,share:null,feature_distributions:{},side_profiles:emptyShapeProfiles(),forward_return_bps:emptyShapeDistribution()}])),transitions:{labels:['tight','normal','loose'],matrix:[[0,0,0],[0,0,0],[0,0,0]]},dwell:Object.fromEntries(['tight','normal','loose'].map(key=>[key,emptyShapeDistribution()])),density_model:{status:'blocked_clock',candidates:[],components:[]},replay:{status:'unavailable_calibration',variant:'compare_regimes',model:'hypothetical_next_grid_crossing',position_size_shares:1,cash_start:10000,cash_end:10000,realized_pnl_usd:0,completed_trades:0,censored_positions:0,affordability_blocks:0,trades:[]}}});
test('shape parser retains clock-blocked audits without fabricated temporal estimates',()=>{assert.equal(parseResult(shapeResult()).shape.status,'blocked_clock');});
test('shape parser retains rejected density fits and refuses an unconverged selected component',()=>{
  const r=shapeResult();r.shape.density_model={status:'unavailable_convergence',selected_components:null,components:[],candidates:[{component_count:1,bic:3,log_likelihood:-1,iterations:1,max_iterations:1,converged:false,eligible_for_selection:false,stop_reason:'iteration_limit'}]};
  assert.equal(parseResult(r).shape.density_model.status,'unavailable_convergence');
  r.shape.density_model.candidates[0].eligible_for_selection=true;assert.throws(()=>parseResult(r));
});
test('shape parser rejects fabricated coverage, normalized profiles, unsafe artifacts and execution modes',()=>{
  for(const change of [r=>r.shape.coverage.qualified_samples=1,r=>r.shape.replay.status='live',r=>r.shape.replay.completed_trades=1,r=>r.shape.side_profiles.bid.cdf_mean=[0,1],r=>r.shape.transitions.matrix[0][0]=-1,r=>r.shape.calibration.threshold_tight_bps=NaN,r=>r.artifacts=[{name:'shape_features',file:'../secrets',bytes:1,rows:0,content_type:'application/gzip',sha256:'a'.repeat(64)}]]){const r=shapeResult();change(r);assert.throws(()=>parseResult(r));}
});
const continuityShapeResult=()=>{
  const r=shapeResult(),h=r.shape,p=h.replay;
  h.status='completed';h.coverage={grid_points:80,qualified_samples:80,excluded_samples:0,exclusions:{}};
  r.sessions[0].coverage={grid_points:80,qualified_samples:80,excluded_samples:0};r.sessions[0].clock.status='qualified_receipt_clocks';r.sessions[0].clock.max_divergence_seconds=0;
  h.calibration={status:'calibrated',samples:40,test_samples:40,cutoff_unix_us:1e9,threshold_tight_bps:1,threshold_loose_bps:3};h.density_model.status='insufficient_calibration';
  const groups=(tight=0,normal=0,loose=0)=>({tight,normal,loose});
  const trade=(cohort,regime,offset)=>({session_id:'1',entry_signal_unix_us:1e9+offset-1e6,entry_unix_us:1e9+offset,entry_price:100.01,entry_regime:regime,
    exit_signal_unix_us:1e9+offset+2e6,exit_unix_us:1e9+offset+3e6,exit_price:99.99,quantity:1,gross_pnl_usd:-.02,fees_usd:2,net_pnl_usd:-2.02,
    exit_reason:cohort==='recovery'?'quality_gap_recovery':'hold_timeout',continuity_cohort:cohort,interruption_count:cohort==='recovery'?1:0,
    recovery_decision_unix_us:cohort==='recovery'?1e9+offset+2e6:null,max_unobserved_seconds:cohort==='recovery'?2:0});
  Object.assign(p,{status:'hypothetical_replay',cash_end:9995.96,realized_pnl_usd:-4.04,completed_trades:2,continuity_schema_version:1,continuity_policy:'retain_position_and_exit_after_soft_recovery',
    interruptions:1,canceled_entry_signals:0,canceled_exit_signals:1,recovery_exit_decisions:1,recovery_completed_trades:1,continuous_completed_trades:1,
    recovery_realized_pnl_usd:-2.02,continuous_realized_pnl_usd:-2.02,continuous_completed_trades_by_entry_regime:groups(1),recovery_completed_trades_by_entry_regime:groups(0,0,1),
    continuous_realized_pnl_by_entry_regime:groups(-2.02),recovery_realized_pnl_by_entry_regime:groups(0,0,-2.02),completed_trades_by_entry_regime:groups(1,0,1),realized_pnl_by_entry_regime:groups(-2.02,0,-2.02),
    trades:[trade('continuous','tight',0),trade('recovery','loose',10e6)],unresolved_positions:[]});
  return r;
};
test('shape continuity parser separates clean and recovery trades while allowing bounded previews',()=>{
  const r=continuityShapeResult();assert.equal(parseResult(r).shape.replay.continuous_completed_trades,1);assert.equal(r.shape.replay.recovery_completed_trades,1);
  r.shape.replay.trades=r.shape.replay.trades.slice(1);assert.equal(parseResult(r).shape.replay.trades[0].continuity_cohort,'recovery');
  r.shape.replay.censored_positions=1;r.shape.replay.unresolved_positions=[{interruption_count:2,recovery_waiting:true}];assert.equal(parseResult(r).shape.replay.censored_positions,1);
});
test('shape continuity parser rejects partial audits, inconsistent cohorts and invented recovery timing',()=>{
  const mutations=[r=>delete r.shape.replay.continuity_schema_version,r=>r.shape.replay.continuity_schema_version=2,r=>delete r.shape.replay.interruptions,
    r=>r.shape.replay.continuity_policy='fill_through_gap',r=>r.shape.replay.continuous_completed_trades=2,r=>r.shape.replay.recovery_realized_pnl_usd=10,
    r=>r.shape.replay.continuous_completed_trades_by_entry_regime={tight:1,normal:0},r=>r.shape.replay.recovery_completed_trades_by_entry_regime.loose=0,
    r=>r.shape.replay.continuous_realized_pnl_by_entry_regime.normal=1,r=>r.shape.replay.completed_trades_by_entry_regime.tight=0,
    r=>r.shape.replay.trades[0].interruption_count=1,r=>r.shape.replay.trades[0].max_unobserved_seconds=1,r=>r.shape.replay.trades[1].interruption_count=0,
    r=>r.shape.replay.trades[1].recovery_decision_unix_us=r.shape.replay.trades[1].exit_unix_us,
    r=>r.shape.replay.trades[1].exit_signal_unix_us=r.shape.replay.trades[1].exit_unix_us,
    r=>r.shape.replay.trades[1].recovery_decision_unix_us--,r=>r.shape.replay.trades[1].exit_reason='hold_timeout',r=>r.shape.replay.trades[0].exit_reason='quality_gap_recovery',
    r=>r.shape.replay.trades[0].entry_signal_unix_us=r.shape.replay.trades[0].entry_unix_us-500000,
    r=>r.shape.replay.trades[0].exit_signal_unix_us=r.shape.replay.trades[0].exit_unix_us-500000,
    r=>r.request.configuration.step_seconds=2,
    r=>r.shape.replay.trades[1].entry_regime='fabricated',r=>r.shape.replay.censored_positions=1,r=>r.shape.replay.unresolved_positions=[{interruption_count:-1,recovery_waiting:true}]];
  for(const change of mutations){const r=continuityShapeResult();change(r);assert.throws(()=>parseResult(r));}
});
test('shape continuity provenance preserves callback reasons without treating them as grid exclusions',()=>{
  const r=continuityShapeResult();r.shape.coverage.continuity_break_counts={crossed_or_locked:123};r.sessions[0].coverage.continuity_break_counts={crossed_or_locked:123};
  assert.equal(parseResult(r).shape.coverage.qualified_samples,80);
  for(const bad of [{reason:-1},{reason:'1'},[],{reason:Infinity}]){r.shape.coverage.continuity_break_counts=bad;assert.throws(()=>parseResult(r));}
});
const event=(n,changes={})=>({event_id:String(n+100),sequence:String(n),kind:n===1?'start':'update',origin:'adapter',
  received_unix_us:String(1700000000000000+n*1000),received_monotonic_ns:String(1000000000+n*1000000),
  operation:0,side:n%2,position:0,price:n%2?99:101,size:'10',market_maker:'',smart_depth:0,code:0,...changes});
const eventPage=(rows,more=false,changes={})=>({schema_version:1,recorded_not_live:true,complete_exchange_book:false,exchange_timestamp:null,
  through_id:'106',next_after_id:rows.at(-1).event_id,has_more:more,
  session:{...session(),event_count:'6',last_sequence:'6'},rows,...changes});
test('archive replay carries book across pages and keeps exact metadata',()=>{
  const first=replayEventPage(eventPage([event(1),event(2),event(3)],true));
  assert.equal(first.frames.at(-1).usable,true);assert.equal(first.state.count,3);
  const second=replayEventPage(eventPage([event(4,{operation:1,size:'23',received_unix_us:'1700000003004000'}),event(5,{operation:2}),event(6,{kind:'stop'})]),first.state);
  assert.equal(second.frames[0].asks[0].size,'23');assert.equal(second.state.clockWarning,true);
  assert.equal(second.frames[1].bids.length,0);assert.equal(second.frames[2].asks.length,0);
  assert.equal(first.state.clockWarning,false);assert.equal(first.state.asks[0].size,'10');
});
test('archive clock jumps stay visible without rewriting original events',()=>{
  const rows=[event(1),event(2),event(3,{received_unix_us:'1700000002003000'})];
  const out=replayEventPage(eventPage(rows,true));assert.equal(out.frames[2].usable,true);
  assert.equal(out.frames[2].clockWarning,true);assert.deepEqual(out.page.rows,rows);
});
test('archive missing position invalidates until explicit reset',()=>{
  const rows=[event(1),event(2,{position:2}),event(3),event(4,{kind:'reset'}),event(5),event(6)];
  const out=replayEventPage(eventPage(rows));assert.equal(out.frames[2].reason,'missing_row_position');
  assert.equal(out.frames[2].bids.length,0);assert.equal(out.frames[5].usable,true);
});
test('archive refuses changed identity, missing tails, active sessions, stalled pages',()=>{
  const p=eventPage([event(1),event(2),event(3)],true),first=replayEventPage(p);
  assert.throws(()=>replayEventPage({...p,session:{...p.session,state:'recording'}}));
  assert.throws(()=>replayEventPage({...p,next_after_id:'0'}));
  assert.throws(()=>replayEventPage({...p,has_more:false}));
  assert.throws(()=>replayEventPage({...p,through_id:'999'},first.state));
  assert.throws(()=>replayEventPage(p,first.state));
});
test('archive gaps and post-terminal resets never fabricate a book',()=>{
  let rows=[event(1),event(2),event(4),event(5)];
  let p=eventPage(rows,false,{session:{...session(),event_count:'4',last_sequence:'5'},through_id:'105'});
  assert.equal(replayEventPage(p).frames.at(-1).reason,'local_sequence_gap');
  rows=[event(1),event(2),event(3),event(4,{kind:'stop'}),event(5,{kind:'reset'}),event(6)];
  assert.equal(replayEventPage(eventPage(rows)).frames.at(-1).usable,false);
});
test('valid native payload and simple metrics',()=>{const v=depthView(parseDepth(frame()));assert.equal(v.midpoint,100);assert.equal(v.spread,2);assert.equal(v.imbalance,.5);assert.equal(v.microprice,100.5)});
test('synthetic remains explicitly synthetic',()=>{const f=frame();f.source='mock';f.synthetic=true;assert.match(depthView(parseDepth(f)).reason,/SYNTHETIC/)});
test('source cannot be silently relabeled',()=>{for(const x of [{source:'other'},{source:'mock'},{synthetic:true}])assert.throws(()=>parseDepth({...frame(),...x}))});
test('full book and exchange-time claims rejected',()=>{for(const x of [{complete_exchange_book:true},{time_basis:'exchange'},{individual_orders:true},{venue:'SMART'}])assert.throws(()=>parseDepth({...frame(),...x}))});
test('unknown or malformed rows rejected',()=>{for(const size of ['-1','NaN','1e-99999','1e99','<script>',1]){const f=frame();f.book.bids[0].size=size;assert.throws(()=>parseDepth(f))}});
test('missing is not zero depth',()=>{const f={...frame(),available:false,book:null};assert.equal(depthView(parseDepth(f)).depth,null);assert.throws(()=>parseDepth({...frame(),available:false}))});
test('duplicate-price rows aggregate for display',()=>{const f=frame();f.book.bids.push({price:99,size:'10.5',market_maker:'MM'});const v=depthView(parseDepth(f));assert.equal(v.bids.length,1);assert.equal(v.bids[0].size,40.5)});
test('requested rows are bounded but not padded',()=>{const p=parseDepth(frame());assert.equal(p.book.bids.length,1);assert.equal(p.requested_rows,10);assert.throws(()=>parseDepth({...p,requested_rows:51}))});
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

test('live chart tracks display samples while preserving best bid and ask',()=>{
  const p=frame(),h=appendLiveSample([],p,1000);
  const more=appendLiveSample(h,{...p,sequence:'21'},2000);
  assert.equal(more.length,2);assert.equal(more[0].bid,99);assert.equal(more[0].ask,101);
  assert.equal(liveSegments(more,'bid',2000).length,1);
});
test('live chart never joins stale, reset, regressed or delayed snapshots',()=>{
  let h=appendLiveSample([],frame(),1000);
  h=appendLiveSample(h,{...frame(),last_event_age_ms:6000},2000);
  h=appendLiveSample(h,{...frame(),sequence:'21'},3000);
  assert.equal(h[1].bid,null);assert.equal(h[1].ask,null);assert.equal(liveSegments(h,'bid',3000).length,2);
  h=appendLiveSample(h,{...frame(),epoch:'2',sequence:'22'},4000);
  h=appendLiveSample(h,{...frame(),epoch:'2',sequence:'19'},5000);
  h=appendLiveSample(h,{...frame(),epoch:'2',sequence:'23'},8000);
  assert.equal(liveSegments(h,'bid',8000).length,5);
});
test('live chart bounds both age and sample count and never combines requests',()=>{
  let h=[];for(let i=0;i<1000;i++)h=appendLiveSample(h,frame(),i*1000);
  assert.equal(h.length,301);assert.equal(h[0].at,699000);
  for(let i=0;i<1000;i++)h=appendLiveSample(h,frame(),1000000+i);
  assert.equal(h.length,360);
  assert.equal(appendLiveSample(h,{...frame(),request_id:'8'},1001001).length,1);
  assert.deepEqual(appendLiveSample(h,null,1001001),[]);
  assert.equal(appendLiveSample(h,frame(),1).length,1);
  assert.deepEqual(liveSegments(h,'bid',2000000),[]);
});
test('one-sided or synthetic display history cannot manufacture usable depth',()=>{
  const p=frame();p.book.asks=[];p.quality='one_sided';
  const h=appendLiveSample([],p,1000);
  assert.equal(h[0].usable,false);assert.deepEqual(liveSegments(h,'ask',1000),[]);
  const m=appendLiveSample([],{...frame(),source:'mock',synthetic:true},1000);
  assert.match(m[0].key,/^mock:/);
});

test('50 received rows preserve position 49 and all cumulative liquidity',()=>{
  const p=frame();p.requested_rows=50;
  for(const [side,sign] of [['bids',-1],['asks',1]])p.book[side]=Array.from({length:50},(_,i)=>({
    price:100+sign*(i+1)/100,size:String(i+1),market_maker:'MM'+i}));
  const accepted=parseDepth(p),v=depthView(accepted);
  assert.equal(accepted.book.bids[49].market_maker,'MM49');
  assert.equal(v.bids.length,50);assert.equal(v.asks.length,50);
  assert.equal(v.bids[49].cumulative,1275);assert.equal(v.depth,2550);
  p.book.bids.push({price:99.49,size:'1',market_maker:'MM50'});
  assert.throws(()=>parseDepth(p));
});
test('50 requested rows never pad a smaller delivered book',()=>{
  const p=frame();p.requested_rows=50;
  for(const [side,sign] of [['bids',-1],['asks',1]])p.book[side]=Array.from({length:10},(_,i)=>({
    price:100+sign*(i+1)/100,size:'1',market_maker:''}));
  const v=depthView(parseDepth(p));
  assert.equal(p.requested_rows,50);assert.equal(v.bids.length,10);assert.equal(v.asks.length,10);assert.equal(v.depth,20);
});
test('50-row recordings are selectable while research diagnostics retain their own 10-level limit',()=>{
  const s={...session(),requested_rows:50},page={schema_version:1,rows:[s],has_more:false,next_after_id:'1'};
  assert.equal(sessionPage(page).rows[0].requested_rows,50);
  assert.equal(researchRequest({...form(),levels:'10'},[s]).configuration.levels,10);
  assert.throws(()=>researchRequest({...form(),levels:'11'},[s]));
  assert.throws(()=>sessionPage({...page,rows:[{...s,requested_rows:51}]}));
});
test('saved replay frames show all 50 rows and reject oversized arrays',()=>{
  const f={usable:true,unix_us:1700000000000000,bids:Array.from({length:50},(_,i)=>[100-(i+1)/100,'1']),
    asks:Array.from({length:50},(_,i)=>[100+(i+1)/100,'2'])};
  const r={schema_version:1,kind:'orderbook_workspace_result',source:'synthetic',request:{},source_hashes:[],
    sessions:[{identity:{session_id:'1'},frames:[f]}]};
  assert.equal(parseResult(r).sessions[0].frames[0].bids[49][0],99.5);
  f.asks.push([100.51,'2']);assert.throws(()=>parseResult(r));
});

test('descriptive requests do not depend on hidden invalid time settings',()=>{
  const request=researchRequest({mode:'describe',synthetic:true,quantity:'',step_seconds:'7'},[{...session(),requested_rows:50}]);
  assert.equal(request.mode,'describe');assert.equal(request.split,null);assert.equal(request.configuration.levels,5);
  assert.equal(request.configuration.step_seconds,1);assert.equal(request.source,'mock');
  assert.throws(()=>researchRequest({mode:'describe'},[session()]),/acknowledge synthetic/);
  assert.throws(()=>researchRequest({mode:'describe',synthetic:true},[{...session(),state:'recording'}]));
  assert.throws(()=>researchRequest({mode:'describe',synthetic:true},[session(),{...session(),session_id:'2',venue:'BATS'}]));
});
const descriptionResult=()=>({schema_version:1,kind:'orderbook_workspace_result',source:'synthetic',request:{mode:'describe'},source_hashes:[],
  descriptive:{weighting:'event_weighted',definitions:{visible_depth:'Total reported depth'},excluded_analyses:['elapsed-time forecasts']},
  sessions:[{identity:{session_id:'1'},frames:[],descriptive:{eligible_events:3,total_events:5,event_counts:{by_kind:{start:1,update:3,stop:1},by_side:{}},quality_counts:{two_sided_unverified:3},
    clock:{status:'clock_quality_warning',max_divergence_seconds:12.3,wall_regressions:1,monotonic_regressions:0},warnings:['Clock warning'],
    distributions:{visible_depth:{count:3,mean:20,std:1,min:19,p05:19,p25:19.5,median:20,p75:20.5,p95:21,max:21,skewness:0,excess_kurtosis:-1.5,histogram:{edges:[19,20,21.1],counts:[1,2]}}},
    depth_profile:{bid:[{level:1,observations:3,mean_size:10,mean_cumulative_size:10,mean_distance_bps:1}],ask:[]}}}]});
test('descriptive results preserve clock warnings without imposing elapsed-time eligibility',()=>{
  const r=descriptionResult();assert.equal(parseResult(r).sessions[0].descriptive.clock.max_divergence_seconds,12.3);
  const job={job_id:'b'.repeat(32),state:'complete',mode:'describe',source:'mock',session_ids:['1']};
  assert.equal(parseJobs({schema_version:1,has_more:false,jobs:[job]}).jobs[0].mode,'describe');
});
test('descriptive statistics reject fabricated counts, malformed bins and unbounded profiles',()=>{
  for(const mutate of [r=>r.descriptive.weighting='time_weighted',r=>r.sessions[0].descriptive.eligible_events=6,
    r=>r.sessions[0].descriptive.distributions.visible_depth.histogram.counts=[0,2],r=>r.sessions[0].descriptive.distributions.visible_depth.mean=Infinity,
    r=>r.sessions[0].descriptive.distributions.visible_depth.histogram.edges=[19,19,21],r=>r.sessions[0].descriptive.depth_profile.bid[0].level=2,
    r=>r.sessions[0].descriptive.clock.monotonic_regressions=-1]) {
    const r=descriptionResult();mutate(r);assert.throws(()=>parseResult(r));
  }
});
test('empty descriptive coverage remains a report with unavailable statistics',()=>{
  const r=descriptionResult(),d=r.sessions[0].descriptive;d.eligible_events=0;d.depth_profile={bid:[],ask:[]};
  for(const key of Object.keys(d.distributions.visible_depth))d.distributions.visible_depth[key]=key==='count'?0:key==='histogram'?{edges:[],counts:[]}:null;
  assert.equal(parseResult(r).sessions[0].descriptive.distributions.visible_depth.mean,null);
});
test('constant distributions retain their exact point bin and unavailable standardized moments',()=>{
  const r=descriptionResult(),s=r.sessions[0].descriptive.distributions.visible_depth;
  Object.assign(s,{mean:20,std:0,min:20,p05:20,p25:20,median:20,p75:20,p95:20,max:20,skewness:null,excess_kurtosis:null,histogram:{edges:[20,20],counts:[3]}});
  assert.equal(parseResult(r).sessions[0].descriptive.distributions.visible_depth.std,0);
});

const flowForm=()=>({mode:'flow',synthetic:true,bin_seconds:'1',start_seconds:'0',end_seconds:'',clock_policy:'strict_receipt'});
test('flow formatting distinguishes nonzero sub-microsecond intervals from true timestamp ties',()=>{
  assert.equal(flowNumber(3.7e-7,6),'3.700e-7');assert.equal(flowNumber(0,6),'0');
  assert.equal(flowNumber(6.47e-10,6),'6.470e-10');assert.equal(flowNumber(-1e-8,4),'-1.000e-8');
  assert.equal(flowNumber(1234.5,6),'1,234.5');assert.equal(flowNumber(null,6),'—');assert.equal(flowNumber(NaN,6),'—');
});
test('flow requests freeze chosen range and clock policy without hidden forecasting settings',()=>{
  const r=researchRequest({...flowForm(),quantity:'',step_seconds:'bad',bin_seconds:'0.5',start_seconds:'10',end_seconds:'30'},[session()]);
  assert.deepEqual(r.configuration,{bin_seconds:.5,start_seconds:10,end_seconds:30,clock_policy:'strict_receipt'});
  assert.equal(r.mode,'flow');assert.equal(r.split,null);
  assert.equal(researchRequest(flowForm(),[session()]).configuration.end_seconds,null);
  assert.equal(researchRequest({...flowForm(),clock_policy:'recorded_monotonic'},[session()]).configuration.clock_policy,'recorded_monotonic');
  assert.throws(()=>researchRequest({...flowForm(),synthetic:false},[session()]),/acknowledge synthetic/);
});
test('flow requests reject invalid clocks, empty numeric values and oversized bin ranges',()=>{
  for(const change of [{clock_policy:'auto'},{clock_policy:undefined},{bin_seconds:''},{bin_seconds:'0'},{bin_seconds:'0.09'},
    {bin_seconds:'301'},{bin_seconds:'NaN'},{start_seconds:''},{start_seconds:'-1'},{start_seconds:'86401'},
    {start_seconds:'10',end_seconds:'10'},{end_seconds:'86401'},{end_seconds:'Infinity'},{bin_seconds:'.1',end_seconds:'1000.1'}])
    assert.throws(()=>researchRequest({...flowForm(),...change},[session()]));
});
const flowResult=()=>({schema_version:1,kind:'orderbook_workspace_result',source:'synthetic',source_hashes:[],request:{mode:'flow'},flow:{definitions:{}},
  sessions:[{identity:{session_id:'1'},frames:[],flow:{status:'provisional',clock:{status:'clock_quality_warning',max_divergence_seconds:12.3,wall_regressions:0,monotonic_regressions:0},
    window:{start_seconds:0,end_seconds:1,available_end_seconds:1,bin_seconds:1},warnings:['Recorded monotonic timing is provisional'],
    bins:[{index:0,start_seconds:0,end_seconds:1,duration_seconds:1,full_bin:true,eligible:true,exclusion_reasons:[],callback_count:3,bid_count:2,ask_count:1,
      insert_count:2,update_count:1,delete_count:0,usable_state_count:2,ofi_transitions:1,ofi_sum:0,means:{visible_depth:20}}],distributions:{},
    model:{status:'descriptive_baseline',eligible_bins:1,lambda_per_bin:3,rate_per_recorded_second:3,sample_variance:null,dispersion_index:null,observed_zero_probability:0,poisson_zero_probability:.05,
      pmf:[{lower:0,upper:2,label:'0–2',empirical_probability:0,poisson_probability:.42},{lower:3,upper:null,label:'3+',empirical_probability:1,poisson_probability:.58}],autocorrelation:[]}}}]});
test('flow result retains explicit provisional timing and accepts flow job metadata',()=>{
  const r=parseResult(flowResult());assert.equal(r.sessions[0].flow.status,'provisional');assert.equal(r.sessions[0].flow.clock.max_divergence_seconds,12.3);
  assert.equal(parseJobs({schema_version:1,jobs:[{job_id:'c'.repeat(32),state:'complete',mode:'flow',source:'mock',session_ids:['1']}],has_more:false}).jobs[0].mode,'flow');
});
test('blocked clock report is visible data-quality evidence without a fitted time model',()=>{
  const r=flowResult(),f=r.sessions[0].flow;f.status='blocked_clock';f.bins=[];f.distributions={};f.model={status:'unavailable'};
  assert.equal(parseResult(r).sessions[0].flow.status,'blocked_clock');
  f.model={status:'descriptive_baseline'};assert.throws(()=>parseResult(r));
});
test('flow parser rejects unsupported values, fabricated bin counts and invalid probabilities',()=>{
  for(const change of [f=>f.status='fixed_clock',f=>f.clock.monotonic_regressions=-1,f=>f.bins[0].duration_seconds=2,
    f=>f.bins[0].callback_count=-1,f=>f.bins[0].bid_count=4,f=>f.bins[0].means.visible_depth=Infinity,
    f=>f.bins[0].start_seconds=-1,f=>f.bins[0].index=2,f=>f.model.pmf[0].poisson_probability=1.1,f=>f.model.pmf[0].poisson_probability=.6,
    f=>f.model.autocorrelation=[{lag:1,correlation:2,pairs:2}],f=>f.bins=Array(10001).fill(f.bins[0]),
    f=>f.status='blocked_clock']) {
    const r=flowResult();change(r.sessions[0].flow);assert.throws(()=>parseResult(r));
  }
});

const datasetForm=()=>({mode:'dataset',synthetic:true,dataset_levels:'5',return_seconds:'60',dataset_side_age:'5'});
test('dataset requests use fixed common depth and strict preparation settings',()=>{
  const r=researchRequest({...datasetForm(),clock_policy:'recorded_monotonic',bin_seconds:'bad'},[session(),{...session(),session_id:'2',requested_rows:30}]);
  assert.deepEqual(r.configuration,{levels:5,return_seconds:60,max_side_age_seconds:5});assert.equal(r.split,null);assert.equal(r.mode,'dataset');
  assert.equal(researchRequest({...datasetForm(),return_seconds:'120'},[session()]).configuration.return_seconds,120);
  assert.throws(()=>researchRequest({...datasetForm(),synthetic:false},[session()]));
  assert.throws(()=>researchRequest(datasetForm(),[session(),{...session(),session_id:'2',venue:'BATS'}]));
});
test('dataset requests reject incomplete common depth and unsupported return intervals',()=>{
  for(const change of [{dataset_levels:'0'},{dataset_levels:'6'},{dataset_levels:'2.5'},{dataset_levels:''},{return_seconds:'30'},
    {return_seconds:'NaN'},{dataset_side_age:''},{dataset_side_age:'0'},{dataset_side_age:'61'},{dataset_side_age:'Infinity'}])
    assert.throws(()=>researchRequest({...datasetForm(),...change},[session()]));
});
const datasetResult=()=>({schema_version:1,kind:'orderbook_workspace_result',source:'synthetic',source_hashes:[],request:{mode:'dataset'},
  dataset:{definitions:{depth:'Common-level size sum'},conventions:{},pressure_status:'components_only_not_model_eligible',summary:{sessions:1,trading_days:1,qualified_blocks:2,forecast_pairs:1,qualified_feature_rows:3600,qualified_return_endpoints:61}},
  artifacts:[{name:'blocks',file:'blocks.csv',sha256:'a'.repeat(64),bytes:400,rows:2,content_type:'text/csv'}],
  sessions:[{identity:{session_id:'1'},frames:[],dataset:{status:'ready',clock:{status:'consistent_receipt_clocks',max_divergence_seconds:0,wall_regressions:0,monotonic_regressions:0},
    coverage:{grid_points:3600,qualified_feature_rows:3600,qualified_return_endpoints:61,qualified_blocks:2,forecast_pairs:1,trading_days:1},exclusion_counts:{},blocks_count:2,pairs_count:1,
    blocks:[{session_date:'2026-10-05',block_index:0,start_unix_us:1791207000000000,end_unix_us:1791208800000000,qualified:true,reasons:[],feature_rows:1800,return_count:30,
      rv:1e-7,bpv:9e-8,positive_excess:1e-8,depth_mean:1000,proportional_spread_mean:1e-4,near_depth_share_mean:.2,bid_depth_mean:500,ask_depth_mean:500}],
    pairs:[{session_date:'2026-10-05',origin_block_index:0,target_block_index:1,forecast_origin_unix_us:1791208800000000}]}}]});
test('dataset results and job catalogs retain readiness without fitting claims',()=>{
  const r=parseResult(datasetResult());assert.equal(r.sessions[0].dataset.coverage.forecast_pairs,1);
  assert.equal(parseJobs({schema_version:1,has_more:false,jobs:[{job_id:'d'.repeat(32),mode:'dataset',source:'mock',session_ids:['1'],state:'complete'}]}).jobs[0].mode,'dataset');
});
test('dataset clock rejection is a saved audit with no qualified measurements',()=>{
  const r=datasetResult(),d=r.sessions[0].dataset;d.status='blocked_clock';d.clock.status='clock_quality_warning';d.clock.max_divergence_seconds=12.3;
  for(const k of ['qualified_feature_rows','qualified_return_endpoints','qualified_blocks','forecast_pairs']){d.coverage[k]=0;r.dataset.summary[k]=0;}
  d.blocks=[];d.pairs=[];assert.equal(parseResult(r).sessions[0].dataset.status,'blocked_clock');
  d.coverage.forecast_pairs=1;assert.throws(()=>parseResult(r));
});
test('dataset parser rejects unsafe exports and invalid pair alignment',()=>{
  for(const mutate of [r=>r.artifacts[0].name='../private',r=>r.artifacts[0].file='profile.json',r=>r.artifacts[0].bytes=-1,r=>r.artifacts[0].sha256='bad',
    r=>r.sessions[0].dataset.pairs[0].target_block_index=3,r=>r.sessions[0].dataset.blocks[0].rv=Infinity,r=>r.sessions[0].dataset.coverage.qualified_blocks=-1]) {
    const r=datasetResult();mutate(r);assert.throws(()=>parseResult(r));
  }
});
test('dataset provenance warnings remain visible data and reject malformed metadata',()=>{
  const r=datasetResult();r.dataset.warnings=['Recording windows overlap; inspect source coverage.'];r.dataset.overlapping_recording_windows=1;
  assert.equal(parseResult(r).dataset.warnings.length,1);
  r.dataset.warnings='hidden warning';assert.throws(()=>parseResult(r));
  r.dataset.warnings=[];r.dataset.overlapping_recording_windows=-1;assert.throws(()=>parseResult(r));
});
