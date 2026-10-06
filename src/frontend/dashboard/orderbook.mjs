import {parseDepth,depthView,maxDepthRows,appendLiveSample,liveSegments,sessionPage,researchRequest,parseJobs,parseResult,aggregate,id,jobId,terminal,replayEventPage,flowNumber} from './orderbook-model.mjs';
import {numberText} from './model.mjs';

export function mountOrderbook(api,onAccessError) {
  const $=name=>document.getElementById(name), put=(name,value)=>{$(name).textContent=value;};
  const make=(tag,text,cls='')=>{const el=document.createElement(tag);el.textContent=String(text??'—');el.className=cls;return el;};
  let unlocked=false,busy=false,revision=0,context=null,generation=null;
  let live=null,liveAt=0,loaded=false,needsReconcile=false,selected=null,pending=null;
  let sessions=[],cursor='0',hasMore=false,selectedIds=new Set(),jobs=[],capability=null,result=null;
  let liveTimer=null,jobTimer=null,verified=false,previousEvidence=null,liveHistory=[];
  let graphExpanded=false;
  let archive=null,submittedJob=null,flowBinPage=0,resultJob=null,datasetBlockPage=0,datasetPairPage=0;
  const modeLabel=mode=>mode==='describe'?'Depth, slope & distributions':mode==='flow'?'Order flow over time':mode==='dataset'?'Build research dataset':mode==='compare'?'Model comparison':'Timed book diagnostics';
  const graphHomes=new Map();
  for(const name of ['ob-live-charts','ob-live-ladders','ob-capture-actions']) {
    const anchor=document.createComment(name+' original position');$(name).before(anchor);graphHomes.set(name,anchor);
  }
  function expandGraphs(value) {
    const dialog=$('ob-graph-dialog');
    if(value===graphExpanded)return;
    graphExpanded=value;
    if(value) {
      dialog.append($('ob-live-charts'));
      $('ob-expanded-ladders').append($('ob-live-ladders'));
      $('ob-chart-controls').append($('ob-capture-actions'));
      dialog.showModal();
    } else {
      if(dialog.open)dialog.close();
      for(const [name,anchor] of graphHomes)anchor.after($(name));
    }
    put('ob-graph-expand',value?'Collapse graphs':'Expand graphs');
    $('ob-graph-expand').setAttribute('aria-expanded',String(value));
    renderLive();
    $('ob-graph-expand').focus({preventScroll:true});
  }
  $('ob-graph-expand').addEventListener('click',()=>expandGraphs(!graphExpanded));
  $('ob-graph-dialog').addEventListener('close',()=>expandGraphs(false));
  window.addEventListener('resize',()=>{if(unlocked){renderLive();archiveFrame();}});
  const ready=()=>unlocked&&context?.broker?.state==='ready';
  const selectedSessions=()=>sessions.filter(s=>selectedIds.has(s.session_id));
  const notify=t=>put('ob-notice',t);
  function tab(name) {
    for(const value of ['live','recordings','results']) {
      $(`ob-${value}-panel`).hidden=value!==name;
      $(`ob-${value}-tab`).setAttribute('aria-selected',String(value===name));
    }
    if(name==='recordings')archiveFrame();
    if(name==='results'&&result?.request.mode==='describe')descriptionPlots();
    if(name==='results'&&result?.request.mode==='flow')flowPlots();
  }
  for(const name of ['live','recordings','results']) $(`ob-${name}-tab`).addEventListener('click',()=>tab(name));
  function controls() {
    $('ob-refresh').disabled=!unlocked||busy;
    $('ob-watch').disabled=!unlocked;
    $('ob-contract-fields').disabled=!ready()||busy;
    $('ob-resolve').disabled=!!pending;
    $('ob-start').disabled=!ready()||busy||!loaded||needsReconcile||!selected||!!live?.available;
    $('ob-rows').disabled=!ready()||busy||!!live?.available;
    $('ob-stop').disabled=!unlocked||busy||!live?.available||needsReconcile;
    $('ob-more').disabled=!unlocked||busy||!hasMore;
    $('ob-jobs-refresh').disabled=!unlocked||busy;
    $('ob-research-fields').disabled=!unlocked||busy||!loaded||needsReconcile||!capability?.enabled||!!capability?.busy;
    $('ob-run').disabled=!selectedIds.size;
    for(const el of $('ob-sessions').querySelectorAll('input')) el.disabled=!unlocked||busy||!terminal(sessions.find(s=>s.session_id===el.value)?.state);
    for(const el of $('ob-sessions').querySelectorAll('button'))el.disabled=!unlocked||busy||!terminal(sessions.find(s=>s.session_id===el.dataset.session)?.state);
    for(const el of ['ob-archive-reset','ob-archive-scrub','ob-archive-export'])$(el).disabled=!unlocked||busy||!archive;
    $('ob-archive-next').disabled=!unlocked||busy||!archive?.state.more;
    put('ob-selection-note',`${selectedIds.size} completed recording${selectedIds.size===1?'':'s'} selected. ${$('ob-mode').value==='describe'?'All captured levels are described in event order; no time grid or forecast is estimated.':$('ob-mode').value==='flow'?'Each selected recording uses the chosen elapsed-time range and clock policy separately.':$('ob-mode').value==='dataset'?'Fixed one-second measurements and 30-minute blocks; receipt clocks must qualify.':'Quantity and time settings will be frozen in the saved request.'}`);
    $('ob-download').disabled=!unlocked||!result;
    for(const button of $('ob-dataset-exports').querySelectorAll('button'))button.disabled=!unlocked||busy||!resultJob||button.dataset.oversized==='true';
  }
  async function read(path,method='GET',body,rev=revision) {
    const value=await api.request(path,method,body);
    if(rev!==revision||!unlocked) throw new Error('Superseded workspace request.');
    return value;
  }
  function schedule() {
    if(!unlocked||document.hidden) {clearTimeout(liveTimer);clearTimeout(jobTimer);liveTimer=jobTimer=null;return;}
    if($('ob-watch').checked||pending) {
      if(liveTimer===null) liveTimer=setTimeout(()=>{liveTimer=null;work(async rev=>{await current(rev);await resolution(rev);});},1000);
    } else {clearTimeout(liveTimer);liveTimer=null;}
    if(jobs.some(j=>j.state==='running')||capability?.busy) {
      if(jobTimer===null) jobTimer=setTimeout(()=>{jobTimer=null;work(loadJobs);},2000);
    } else {clearTimeout(jobTimer);jobTimer=null;}
  }
  async function work(action,mutation=false) {
    if(!unlocked||busy) {schedule();return;}
    busy=true;const rev=revision;controls();
    try { await action(rev); }
    catch(error) {
      if(rev===revision) {
        if([401,403].includes(error.status)) onAccessError(error);
        else {
          if(mutation && (!error.status || error.status>=500)) needsReconcile=true;
          notify((error.message||'Workspace operation unavailable.')+(mutation?' No action was automatically retried; refresh to reconcile.':''));
        }
      }
    } finally {if(rev===revision){busy=false;controls();schedule();}}
  }
  function blank(body,columns,text) {
    const row=make('tr',''),cell=make('td',text);cell.colSpan=columns;row.append(cell);$(body).replaceChildren(row);
  }
  function ladder(body,rows) {
    if(!rows.length){blank(body,3,'No usable displayed rows');return;}
    $(body).replaceChildren(...rows.map(r=>{const tr=make('tr','');for(const v of [r.price,r.size,r.cumulative])tr.append(make('td',numberText(v,6)));return tr;}));
  }
  function svgNode(svg,tag,attrs,text) {
    const el=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const [name,value] of Object.entries(attrs))el.setAttribute(name,String(value));
    if(text!==undefined)el.textContent=text;svg.append(el);return el;
  }
  function chartBox(svg) {
    const rect=svg.getBoundingClientRect(),width=Math.max(280,rect.width||520),height=Math.max(220,rect.height||360);
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
    return {width,height,left:78,right:width-22,top:26,bottom:height-43};
  }
  function emptyPlot(svg,text) {
    const box=chartBox(svg);
    svg.replaceChildren();svgNode(svg,'text',{x:box.width/2,y:box.height/2,'text-anchor':'middle',class:'ob-chart-empty'},text);
  }
  function axes(svg,box,xLabels,yLabels) {
    svgNode(svg,'line',{x1:box.left,y1:box.bottom,x2:box.right,y2:box.bottom,class:'ob-axis'});
    for(const [text,x,anchor] of xLabels)svgNode(svg,'text',{x,y:box.bottom+27,'text-anchor':anchor},text);
    for(const [text,y] of yLabels) {
      svgNode(svg,'line',{x1:box.left,y1:y,x2:box.right,y2:y,class:'ob-grid'});
      svgNode(svg,'text',{x:box.left-10,y:y+4,'text-anchor':'end'},text);
    }
  }
  function depthGraph(depth,view,empty='Awaiting a fresh, valid two-sided book') {
    if(!view.usable)emptyPlot(depth,empty);
    else {
      depth.replaceChildren();
      const prices=[...view.bids,...view.asks].map(r=>r.price);
      const lo=Math.min(...prices),hi=Math.max(...prices),maxSize=Math.max(view.bids.at(-1).cumulative,view.asks.at(-1).cumulative);
      const box=chartBox(depth),middle=(lo+hi)/2;
      const x=p=>box.left+(box.right-box.left)*(p-lo)/(hi-lo),y=q=>box.bottom-(box.bottom-box.top)*q/maxSize;
      axes(depth,box,[[numberText(lo,4),box.left,'start'],[numberText(middle,4),(box.left+box.right)/2,'middle'],[numberText(hi,4),box.right,'end']],
        [[numberText(maxSize,2),box.top],[numberText(maxSize/2,2),(box.top+box.bottom)/2],['0',box.bottom]]);
      for(const [side,rows] of [['bid',view.bids],['ask',view.asks]]) {
        const points=[`${x(rows[0].price)},${box.bottom}`];let cumulative=0;
        for(const row of rows) {
          points.push(`${x(row.price)},${y(cumulative)}`,`${x(row.price)},${y(row.cumulative)}`);cumulative=row.cumulative;
        }
        svgNode(depth,'polygon',{points:[...points,`${x(rows.at(-1).price)},${box.bottom}`].join(' '),class:`ob-area ob-${side}-series`});
        svgNode(depth,'polyline',{points:points.join(' '),class:`ob-${side}-series`});
        for(const row of rows) {
          const dot=svgNode(depth,'circle',{cx:x(row.price),cy:y(row.cumulative),r:4,class:`ob-${side}-series`});
          svgNode(dot,'title',{},`${side==='bid'?'Bid':'Ask'} ${numberText(row.price,6)} · ${numberText(row.size,6)} reported size · cumulative ${numberText(row.cumulative,6)}`);
        }
      }
    }
  }
  function renderCharts(view) {
    depthGraph($('ob-live-depth'),view);
    const timeline=$('ob-live-timeline');
    const now=Date.now(),valid=liveHistory.filter(f=>f.usable&&f.at>=now-300000);
    if(!valid.length)emptyPlot(timeline,'Waiting for valid display samples');
    else {
      timeline.replaceChildren();
      const prices=valid.flatMap(f=>[f.bid,f.ask]);let lo=Math.min(...prices),hi=Math.max(...prices);
      const pad=Math.max((hi-lo)*.08,Math.abs(hi)*.000001,1e-9);lo-=pad;hi+=pad;
      const span=Math.min(300000,Math.max(10000,now-valid[0].at));
      const box=chartBox(timeline);
      const x=t=>box.left+(box.right-box.left)*(t-(now-span))/span,y=p=>box.bottom-(box.bottom-box.top)*(p-lo)/(hi-lo);
      const ago=ms=>ms>=60000?'−'+numberText(ms/60000,1)+' min':'−'+numberText(ms/1000,0)+' s';
      axes(timeline,box,[[ago(span),box.left,'start'],[ago(span/2),(box.left+box.right)/2,'middle'],['Now',box.right,'end']],
        [[numberText(hi,4),box.top],[numberText((hi+lo)/2,4),(box.top+box.bottom)/2],[numberText(lo,4),box.bottom]]);
      for(const side of ['bid','ask'])for(const samples of liveSegments(liveHistory,side,now)) {
        svgNode(timeline,'polyline',{points:samples.map(f=>`${x(f.at)},${y(f[side])}`).join(' '),class:`ob-${side}-series`});
        const last=samples.at(-1);
        svgNode(timeline,'circle',{cx:x(last.at),cy:y(last[side]),r:2.5,class:`ob-${side}-series`});
      }
    }
    put('ob-chart-note',`${live?.synthetic?'SYNTHETIC · ':''}${view.usable?'Fresh display sample.':'Current book has no usable two-sided sample.'} ${$('ob-watch').checked?'Updates about once per second.':'Display paused; the recorder runs independently.'} Timeline keeps up to five minutes in this tab; gaps are not filled.`);
  }
  function renderRecording() {
    const r=live?.recording;
    let message='Recording status not loaded. Refresh the workspace to check saved-event evidence.';
    if(r) {
      if(!r.available)message='Local recorder unavailable — saving is not confirmed.';
      else if(!r.healthy)message=`Recorder needs attention — ${r.last_error||'saving is not confirmed; inspect recorder health.'}`;
      else if(r.session_id)message=`${r.active?'Saving received callbacks':'Saved capture'} · session ${r.session_id} · ${r.committed_event_count??'unknown'} committed events · ${r.state??'state unavailable'}${r.last_commit_ms?' · archive commit '+new Date(r.last_commit_ms).toLocaleTimeString():''}. Event totals include capture lifecycle records.`;
      else message='Local recorder ready. No matching capture is currently attached to this depth request.';
    }
    if(r?.session_id)message+=r.raw_metadata?.available
      ?` Raw callback metadata available; committed through sequence ${r.raw_metadata.committed_through_sequence??'unavailable'}.`
      :' Raw callback metadata is unavailable for this capture.';
    put('ob-recording-status',message);put('ob-chart-recording',message);
    $('ob-recording-status').classList.toggle('ob-recorder-warning',!!r&&(!r.available||!r.healthy));
    const metadata=live?Object.fromEntries(['source','synthetic','available','request_id','contract_id','venue','requested_rows','active','structural_valid','quality','sequence','epoch','last_receipt_unix_us','last_event_age_ms','sequence_basis','time_basis','exchange_timestamp','source_timestamp_available','complete_exchange_book','individual_orders','direct_depth_only','recording','source_fields','metadata_fields','book'].filter(k=>k in live).map(k=>[k,live[k]])):null;
    put('ob-live-metadata',metadata?JSON.stringify(metadata,null,2):'No metadata loaded.');
  }
  function renderLive() {
    const v=live?depthView(live,Math.max(0,performance.now()-liveAt)):depthView(null);
    if(!v.usable){verified=false;previousEvidence=null;}
    for(const [el,k] of [['ob-spread','spread'],['ob-midpoint','midpoint'],['ob-depth','depth'],['ob-imbalance','imbalance']]) put(el,numberText(v[k],6));
    ladder('ob-bids',v.bids);ladder('ob-asks',v.asks);renderCharts(v);renderRecording();
    $('ob-bids').classList.toggle('ob-ladder-stale',!v.usable);$('ob-asks').classList.toggle('ob-ladder-stale',!v.usable);
    put('ob-source',!live?'NOT LOADED':live.source==='mock'?'SYNTHETIC MOCK':live.source==='ibkr_tws'?'IBKR DISPLAYED DEPTH':'BROKER DISABLED');
    $('ob-source').className='badge'+(live?.source==='mock'?' warning':'');
    put('ob-quality',v.reason+(verified?' · Advancing native callbacks observed in this tab.':' · Native update progression not confirmed.'));
    put('ob-live-identity',live?.available?`${live.venue} · conId ${live.contract_id} · request ${live.request_id} · sequence ${live.sequence} / epoch ${live.epoch} · bid ${live.book.bids.length}/${live.requested_rows} rows received (${v.bids.length} distinct prices), ask ${live.book.asks.length}/${live.requested_rows} rows received (${v.asks.length} distinct prices)`:'No depth request loaded. Resolve and explicitly start capture to receive rows.');
    put('ob-graph-identity',$('ob-live-identity').textContent+' · '+v.reason);
    put('ob-state',!unlocked?'Dashboard locked.':needsReconcile?'Outcome unknown — refresh before another mutation.':document.hidden?'Display paused; recording continues.':busy?'Checking workspace…':$('ob-watch').checked?'Watching snapshots; archival capture is separate.':'Display paused. Capture may still be recording.');
  }
  async function current(rev) {
    try {
      const started=performance.now();
      const p=parseDepth(await read('/api/depth/current','GET',undefined,rev));
      const view=depthView(p,performance.now()-started);
      if(p.available && view.usable && p.source==='ibkr_tws') {
        const key=`${p.request_id}:${p.contract_id}:${p.venue}:${p.epoch}`;
        const e={key,sequence:BigInt(p.sequence),stamp:BigInt(p.last_receipt_unix_us)};
        verified=!!previousEvidence && previousEvidence.key===key&&e.sequence>previousEvidence.sequence&&e.stamp>previousEvidence.stamp;
        if(previousEvidence?.key===key&&e.sequence===previousEvidence.sequence&&e.stamp===previousEvidence.stamp) verified=previousEvidence.verified===true;
        previousEvidence={...e,verified};
      } else {verified=false;previousEvidence=null;}
      liveHistory=appendLiveSample(liveHistory,p,Date.now(),performance.now()-started);
      live=p;liveAt=started;renderLive();
    } catch(e) {if(rev===revision){live=null;liveHistory=[];verified=false;previousEvidence=null;renderLive();}throw e;}
  }
  function renderSessions() {
    if(!sessions.length){blank('ob-sessions',8,'No recordings loaded. No missing data is assumed to be zero.');return;}
    $('ob-sessions').replaceChildren(...sessions.map(s=>{
      const tr=make('tr',''),choice=make('td',''),input=document.createElement('input');input.type='checkbox';input.value=s.session_id;
      input.setAttribute('aria-label',`Select recording ${s.session_id}`);input.checked=selectedIds.has(s.session_id);input.disabled=!terminal(s.state);
      input.addEventListener('change',()=>{if(input.checked)selectedIds.add(s.session_id);else selectedIds.delete(s.session_id);controls();});choice.append(input);tr.append(choice);
      for(const text of [`#${s.session_id} / ${s.symbol}`,`${s.venue} / ${s.source==='mock'?'SYNTHETIC':s.source}`,new Date(s.started_ms).toISOString().replace('T',' ').slice(0,19),s.requested_rows,s.event_count,s.state]) tr.append(make('td',text));
      const cell=make('td',''),open=make('button','View recording','secondary');open.dataset.session=s.session_id;open.disabled=!unlocked||busy||!terminal(s.state);
      open.setAttribute('aria-label',`View recording ${s.session_id}`);
      open.addEventListener('click',()=>work(rev=>openArchive(rev,s.session_id)));cell.append(open);tr.append(cell);
      return tr;
    }));
  }
  async function openArchive(rev,sid,more=false) {
    const prior=more?archive?.state:null;
    if(!more){archive=null;$('ob-archive').hidden=true;}
    const body={session_id:sid,limit:1000};
    if(prior)Object.assign(body,{after_id:prior.eventId,through_id:prior.through});
    const page=await read('/api/depth/events','POST',body,rev);
    if(page.session?.session_id!==sid)throw new Error('Recording identity changed.');
    const next=replayEventPage(page,prior);archive=next;
    $('ob-archive').hidden=false;
    $('ob-archive-scrub').max=String(Math.max(0,next.frames.length-1));
    let chosen=next.frames.findLastIndex(f=>f.usable);if(chosen<0)chosen=Math.max(0,next.frames.length-1);
    $('ob-archive-scrub').value=String(chosen);
    const s=page.session;
    put('ob-archive-title',`${s.symbol} / ${s.venue} · recording ${sid}`);
    put('ob-archive-summary',`${s.source==='mock'?'SYNTHETIC TEST DATA':'IBKR saved depth'} · ${s.event_count} saved events · showing events ${next.initial.count+1}–${next.state.count}. ${next.state.more?'More events remain; use Next 1,000 events.':'End of recording.'}`);
    put('ob-archive-clock',next.state.clockWarning?'Clock discrepancy detected. View follows event sequence only; elapsed-time research remains blocked.':'Event-sequence inspection only. Timing and per-row freshness are not certified by this viewer. Research checks run separately.');
    archiveFrame();if(!more)$('ob-archive').scrollIntoView({block:'start'});
    notify('Saved recording opened directly from the archive. No analysis job or broker download was needed.');
  }
  function archiveFrame() {
    const f=archive?.frames[Number($('ob-archive-scrub').value)];if(!f)return;
    const bids=aggregate(f.bids),asks=aggregate(f.asks);
    depthGraph($('ob-archive-depth'),{usable:f.usable,bids,asks},`No valid two-sided book at this event: ${f.reason}`);
    ladder('ob-archive-bids',bids);ladder('ob-archive-asks',asks);
    put('ob-archive-frame',`Local sequence ${f.event.sequence} · ${f.event.kind} · ${f.reason} · receipt time ${new Date(Number(f.event.received_unix_us)/1000).toISOString()} (unverified local clock)`);
    put('ob-archive-event',JSON.stringify(f.event,null,2));
  }
  $('ob-archive-scrub').addEventListener('input',archiveFrame);
  $('ob-archive-next').addEventListener('click',()=>{if(archive?.state.more)work(rev=>openArchive(rev,archive.page.session.session_id,true));});
  $('ob-archive-reset').addEventListener('click',()=>{if(archive)work(rev=>openArchive(rev,archive.page.session.session_id));});
  $('ob-archive-export').addEventListener('click',()=>{
    if(!unlocked||!archive)return;
    const s=archive.page.session;
    const payload={schema_version:1,kind:'displayed_depth_event_page',complete_session:archive.initial.count===0&&!archive.state.more,
      timing_validated:false,complete_exchange_book:false,time_basis:'local_callback_receipt',session:s,through_id:archive.state.through,
      start_after_id:archive.initial.eventId,next_after_id:archive.state.eventId,has_more:archive.state.more,
      initial_replay_checkpoint:archive.initial,metadata_fields:archive.page.metadata_fields,raw_metadata:archive.page.raw_metadata,events:archive.page.rows};
    const u=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'})),a=document.createElement('a');
    a.href=u;a.download=`depth-session-${s.session_id}-events-${archive.initial.count+1}-${archive.state.count}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  });
  async function catalog(rev,more=false) {
    const after=more?cursor:'0';
    const p=sessionPage(await read('/api/depth/sessions','POST',{after_id:after,limit:50},rev));
    if(more&&p.rows.length&&BigInt(p.next_after_id)<=BigInt(after)) throw new Error('Recording pagination did not advance.');
    const merged=new Map((more?sessions:[]).map(s=>[s.session_id,s]));for(const row of p.rows)merged.set(row.session_id,row);
    if(merged.size>1000) throw new Error('Catalog display reached 1,000 sessions; use the paginated archive tools for older studies.');
    sessions=[...merged.values()];cursor=p.next_after_id;hasMore=p.has_more;
    selectedIds=new Set([...selectedIds].filter(id=>sessions.some(s=>s.session_id===id&&terminal(s.state))));
    renderSessions();put('ob-catalog-note',`${sessions.length} recordings displayed${hasMore?' — more pages remain':''}. Active captures cannot be selected. Start dates are archive metadata, not verified eligible model dates.`);
  }
  function renderJobs() {
    $('ob-jobs').replaceChildren(...jobs.map(j=>{
      const card=make('div','','ob-job'),info=make('div','');info.append(make('p',`${modeLabel(j.mode)} · ${j.state} · ${j.source==='mock'?'SYNTHETIC':'IBKR recorded data'}`),make('small',`${j.job_id} · sessions ${j.session_ids.join(', ')}`));
      if(j.error) {
        info.append(make('small',j.error));
        if(/clock|monotonic/i.test(j.error)&&j.mode!=='describe')info.append(make('p','Timing checks blocked this run. Choose Describe depth, slope & distributions in Recordings & models to study event-ordered book characteristics with the clock warning retained.','ob-recorder-warning'));
      }
      card.append(info);
      if(j.state==='complete'){const b=make('button','Open result','secondary');b.disabled=!unlocked;b.addEventListener('click',()=>work(async rev=>{
        result=parseResult(await read(`/api/depth/research/jobs/${j.job_id}/result`,'GET',undefined,rev));resultJob=j.job_id;renderResult();tab('results');
      }));card.append(b);}
      if(j.state==='running'){const b=make('button','Cancel research','secondary');b.disabled=!unlocked;b.addEventListener('click',()=>{
        if(!window.confirm('Cancel this offline research job? Recording and archived market data will remain unchanged.'))return;
        work(async rev=>{await read(`/api/depth/research/jobs/${j.job_id}/cancel`,'POST',{},rev);await loadJobs(rev);},true);
      });card.append(b);}
      return card;
    }));
  }
  async function loadJobs(rev) {
    const c=await read('/api/depth/research/status','GET',undefined,rev);
    if(c.schema_version!==1||typeof c.enabled!=='boolean'||typeof c.busy!=='boolean')throw new Error('Incompatible research worker status.');
    const list=parseJobs(await read('/api/depth/research/jobs','GET',undefined,rev));capability=c;jobs=list.jobs;renderJobs();
    put('ob-worker-note',c.message);put('ob-job-note',`${jobs.length} recent runs${list.has_more?' — catalog is limited to the most recent 50':''}${list.invalid_records?` · ${list.invalid_records} invalid records not accepted`:''}. Jobs persist on disk; canceling research does not stop recording.`);
    const submitted=jobs.find(j=>j.job_id===submittedJob);
    if(submitted&&submitted.state!=='running') {
      submittedJob=null;
      if(submitted.state==='complete') {
        result=parseResult(await read(`/api/depth/research/jobs/${submitted.job_id}/result`,'GET',undefined,rev));
        resultJob=submitted.job_id;
        renderResult();tab('results');notify(`${modeLabel(submitted.mode)} completed. The saved report is open below the research runs.`);
      } else notify(`${modeLabel(submitted.mode)} ${submitted.state}: ${submitted.error||'No completed report was produced.'}`);
    }
  }
  async function load(rev) {
    await current(rev);await catalog(rev);await loadJobs(rev);loaded=true;needsReconcile=false;notify('Workspace refreshed. Broker connection and capture remain explicit.');
  }
  $('ob-refresh').addEventListener('click',()=>work(load));
  $('ob-more').addEventListener('click',()=>work(rev=>catalog(rev,true)));
  $('ob-jobs-refresh').addEventListener('click',()=>work(loadJobs));
  $('ob-watch').addEventListener('change',()=>{renderLive();schedule();});
  function clearSelection() {pending=null;selected=null;$('ob-candidates').replaceChildren();put('ob-selected','Select a returned USD stock explicitly.');controls();}
  for(const name of ['ob-symbol','ob-venue'])$(name).addEventListener('change',clearSelection);
  $('ob-contract-form').addEventListener('submit',event=>{
    event.preventDefault();if(!ready()||pending)return;
    work(async rev=>{
      clearSelection();const symbol=$('ob-symbol').value.trim(),venue=$('ob-venue').value;
      const value=await read('/api/contracts/resolve','POST',{symbol,exchange:venue,security_type:'STK',currency:'USD'},rev);
      if(!Number.isSafeInteger(value.request_id)||value.request_id<=0)throw new Error('Invalid resolution identifier.');
      pending={id:String(value.request_id),venue};put('ob-resolution','Waiting for candidates; no depth subscription has been started.');
    },true);
  });
  async function resolution(rev) {
    if(!pending||!ready())return;const query={...pending};
    const value=await read(`/api/contracts/requests/${query.id}`,'GET',undefined,rev);
    if(pending?.id!==query.id)return;
    if(value.status==='pending')return;
    pending=null;
    if(value.status!=='complete'){put('ob-resolution','Resolution failed. No contract selected or capture started.');return;}
    if(!Array.isArray(value.contracts)||value.contracts.length>64)throw new Error('Invalid contract candidates.');
    const candidates=value.contracts.filter(c=>c.security_type==='STK'&&c.currency==='USD'&&Number.isSafeInteger(c.contract_id)&&c.contract_id>0);
    $('ob-candidates').replaceChildren(...candidates.map(c=>{
      const b=make('button',`Select ${c.symbol} · USD · conId ${c.contract_id} · ${query.venue}`,'secondary');b.type='button';
      b.addEventListener('click',()=>{selected={...c,venue:query.venue};put('ob-selected',`${c.symbol} · conId ${c.contract_id} · direct ${query.venue} · USD`);controls();});return b;
    }));
    put('ob-resolution',`${candidates.length} supported candidates. Confirm the instrument and direct venue before recording.`);
  }
  $('ob-start').addEventListener('click',()=>{
    if(!selected||!ready()||!loaded||needsReconcile||live?.available)return;
    const rows=Number($('ob-rows').value);if(!Number.isInteger(rows)||rows<1||rows>maxDepthRows){notify(`Request 1..${maxDepthRows} rows per side. Received rows depend on the feed.`);return;}
    const c={...selected};
    if(!window.confirm(`Record displayed depth for ${c.symbol} / ${c.contract_id} on ${c.venue}, up to ${rows} rows per side? This writes market observations to the existing local archive. Closing the page does not stop it. No orders will be submitted.`))return;
    work(async rev=>{
      const ack=await read('/api/depth/subscribe','POST',{contract_id:String(c.contract_id),venue:c.venue,rows},rev);
      if(!id(ack.request_id))throw new Error('Capture outcome unknown: invalid acknowledgement.');
      notify(`Request ${ack.request_id} acknowledged; awaiting actual depth callbacks.`);$('ob-watch').checked=true;
      await current(rev);await catalog(rev);
    },true);
  });
  $('ob-stop').addEventListener('click',()=>{
    if(!live?.available||needsReconcile)return;const request=live.request_id;
    if(!window.confirm(`Stop / clear depth request ${request}? Archived events will remain available for research.`))return;
    work(async rev=>{const value=await read('/api/depth/unsubscribe','POST',{request_id:request},rev);
      await current(rev);await catalog(rev);notify(value.cancelled?'Capture stopped; inspect its completed session.':'Request cleared or already stopped. Inspect the archive state before proceeding.');},true);
  });
  function modeControls() {
    const describe=$('ob-mode').value==='describe',flow=$('ob-mode').value==='flow',dataset=$('ob-mode').value==='dataset',compare=$('ob-mode').value==='compare';
    $('ob-timed-settings').hidden=describe||flow||dataset;
    for(const el of $('ob-timed-settings').querySelectorAll('input,select'))el.disabled=describe||flow||dataset;
    $('ob-flow-settings').hidden=!flow;
    for(const el of $('ob-flow-settings').querySelectorAll('input,select'))el.disabled=!flow;
    $('ob-dataset-settings').hidden=!dataset;
    for(const el of $('ob-dataset-settings').querySelectorAll('input,select'))el.disabled=!dataset;
    $('ob-partitions').hidden=!compare;
    for(const el of $('ob-partitions').querySelectorAll('textarea'))el.disabled=!compare;
    put('ob-mode-note',dataset?'Prepare a research dataset with strict clock checks, coverage flags and next-block target alignment. Review readiness and exclusions before fitting any model.':describe?'Describe all captured price levels in event order. Clock problems are reported; elapsed-time and prediction analyses remain separate.':flow?'Model callback counts and interarrival times, and study depth, slope, spread and imbalance through the chosen time window. Clock checks apply before a time model is calculated.':'Timed diagnostics and prediction models require consistent receipt clocks. Failed clock checks remain enforced.');
    controls();
  }
  $('ob-mode').addEventListener('change',modeControls);
  $('ob-flow-policy').addEventListener('change',()=>{
    const exploratory=$('ob-flow-policy').value==='recorded_monotonic';
    put('ob-flow-policy-note',exploratory?'Exploratory assumption: treat recorded monotonic intervals as seconds, even if receipt clocks disagree. Results are provisional; this does not repair the clock or certify actual elapsed time. Clock reversals still block the model.':'A disagreement between receipt clocks produces an audit report without a time model. Agreement does not certify clock accuracy or exchange event times.');
    $('ob-flow-policy-note').classList.toggle('ob-recorder-warning',exploratory);
  });
  $('ob-research-form').addEventListener('submit',event=>{
    event.preventDefault();if(!capability?.enabled||capability.busy||needsReconcile)return;
    let request;
    try{const f=Object.fromEntries(new FormData(event.currentTarget));f.synthetic=event.currentTarget.elements.synthetic.checked;request=researchRequest(f,selectedSessions());}
    catch(e){notify(e.message);return;}
    if(!window.confirm(`Run ${modeLabel(request.mode)} on ${request.session_ids.length} completed recording(s)? Source: ${request.source==='mock'?'SYNTHETIC MOCK':'IBKR recorded data'}.${request.mode==='flow'&&request.configuration.clock_policy==='recorded_monotonic'?' Exploratory recorded-monotonic timing is an explicit assumption; results are provisional.':''} The request and results are saved. No broker request or order will be sent.`))return;
    work(async rev=>{
      const ack=await read('/api/depth/research/jobs','POST',request,rev);
      if(!jobId(ack.job_id))throw new Error('Job outcome unknown. Refresh the research catalog before retrying.');
      submittedJob=ack.job_id;result=null;resultJob=null;$('ob-result').hidden=true;
      notify(`Research job ${ack.job_id} submitted. Recording continues independently.`);await loadJobs(rev);tab('results');
    },true);
  });
  function plot(name,frames,key) {
    const svg=$(name);svg.replaceChildren();const valid=frames.filter(f=>f.usable&&Number.isFinite(f[key]));
    if(!valid.length)return;
    let lo=Math.min(...valid.map(f=>f[key])),hi=Math.max(...valid.map(f=>f[key]));const pad=Math.max((hi-lo)*.07,Math.abs(hi)*.000001,1e-9);lo-=pad;hi+=pad;
    let points=[];const flush=()=>{if(points.length){const el=document.createElementNS('http://www.w3.org/2000/svg','polyline');el.setAttribute('points',points.join(' '));svg.append(el);points=[];}};
    frames.forEach((f,i)=>{if(i&&f.segment!==frames[i-1].segment)flush();if(!f.usable||!Number.isFinite(f[key])){flush();return;}
      points.push(`${65+445*i/Math.max(1,frames.length-1)},${150-130*(f[key]-lo)/(hi-lo)}`);});flush();
    for(const [v,y] of [[hi,18],[lo,156]]){const el=document.createElementNS('http://www.w3.org/2000/svg','text');el.setAttribute('x','3');el.setAttribute('y',String(y));el.textContent=numberText(v,5);svg.append(el);}
  }
  function frame() {
    if(!result)return;const session=result.sessions[Number($('ob-result-session').value)],f=session?.frames[Number($('ob-result-scrub').value)];
    if(!f){put('ob-result-frame','No replay frames available for this recording.');blank('ob-result-bids',3,'No replay rows');blank('ob-result-asks',3,'No replay rows');return;}
    put('ob-result-frame',`${new Date(f.unix_us/1000).toISOString()}${result.request.mode==='describe'?' (unverified local receipt clock)':''} · ${f.quality} · local sequence ${f.sequence} · recorded, not live`);
    ladder('ob-result-bids',aggregate(f.bids.map(([price,size])=>({price,size}))));ladder('ob-result-asks',aggregate(f.asks.map(([price,size])=>({price,size}))));
  }
  function resultSession() {
    if(!result)return;const s=result.sessions[Number($('ob-result-session').value)];if(!s)return;
    $('ob-result-scrub').max=String(Math.max(0,s.frames.length-1));$('ob-result-scrub').value=String(Math.max(0,s.frames.findIndex(f=>f.usable)));
    const describe=result.request.mode==='describe',flow=result.request.mode==='flow',dataset=result.request.mode==='dataset';
    put('ob-result-coverage',JSON.stringify(dataset?{session:s.identity,status:s.dataset.status,coverage:s.dataset.coverage,clock:s.dataset.clock,exclusion_counts:s.dataset.exclusion_counts}:flow?{session:s.identity,status:s.flow.status,window:s.flow.window,clock:s.flow.clock,warnings:s.flow.warnings}:describe?{session:s.identity,eligible_post_update_states:s.descriptive.eligible_events,event_quality:s.descriptive.quality_counts,clock:s.descriptive.clock}:{session:s.identity,grid_points:s.grid_count,eligible_rows:s.sample_count,grid_quality:s.quality_counts,event_quality:s.event_quality_counts,exclusions:s.missing_targets,partial_session_variation:s.partial_session_variation},null,2));
    if(describe)renderDescription(s);
    if(flow)renderFlow(s);
    if(dataset)renderDataset(s);
    plot('ob-result-price',s.frames,'midpoint');plot('ob-result-imb',s.frames,'depth_imbalance');frame();
  }
  const metricName=key=>({midpoint:'Midpoint (price)',spread:'Spread (price)',spread_bps:'Spread (bps)',
    bid_depth:'Bid depth (reported units)',ask_depth:'Ask depth (reported units)',visible_depth:'Total visible depth (reported units)',
    depth_imbalance:'Full-depth imbalance',top_imbalance:'Top-level imbalance',top_concentration:'Top-level concentration',
    bid_level_count:'Bid price levels',ask_level_count:'Ask price levels',
    bid_slope_bps_per_1000:'Bid slope (bps / 1,000 units)',ask_slope_bps_per_1000:'Ask slope (bps / 1,000 units)',
    bid_size_hhi:'Bid size concentration (HHI)',ask_size_hhi:'Ask size concentration (HHI)',
    bid_mean_distance_bps:'Bid mean distance (bps)',ask_mean_distance_bps:'Ask mean distance (bps)',ofi_event:'Top-of-book OFI per event'}[key]??key.replaceAll('_',' '));
  function descriptionPlots() {
    const d=result?.sessions[Number($('ob-result-session').value)]?.descriptive;
    if(!d)return;
    const key=$('ob-description-metric').value,stat=d.distributions[key],hist=$('ob-description-histogram');
    put('ob-description-definition',result.descriptive.definitions[key]??'No characteristic selected.');
    if(!stat?.count)emptyPlot(hist,'No eligible observations for this characteristic');
    else {
      hist.replaceChildren();const box=chartBox(hist),h=stat.histogram,max=Math.max(...h.counts),lo=h.edges[0],hi=h.edges.at(-1);
      const point=lo===hi,x=v=>point?(box.left+box.right)/2:box.left+(box.right-box.left)*(v-lo)/(hi-lo),y=v=>box.bottom-(box.bottom-box.top)*v/max;
      axes(hist,box,point?[[numberText(lo,4)+' (constant)',x(lo),'middle']]:[[numberText(lo,4),box.left,'start'],[numberText((lo+hi)/2,4),(box.left+box.right)/2,'middle'],[numberText(hi,4),box.right,'end']],
        [[numberText(max,0),box.top],[numberText(max/2,0),(box.top+box.bottom)/2],['0',box.bottom]]);
      for(const [i,count] of h.counts.entries()) {
        const bar=svgNode(hist,'rect',{x:point?x(lo)-12:x(h.edges[i])+1,y:y(count),width:point?24:Math.max(.5,x(h.edges[i+1])-x(h.edges[i])-2),height:box.bottom-y(count),class:'ob-histogram-bar'});
        svgNode(bar,'title',{},`${numberText(h.edges[i],6)} to ${numberText(h.edges[i+1],6)}: ${count} observations`);
      }
    }
    const svg=$('ob-description-profile'),rows=[...d.depth_profile.bid,...d.depth_profile.ask];
    if(!rows.length)emptyPlot(svg,'No eligible depth profile');
    else {
      svg.replaceChildren();const box=chartBox(svg),maxLevel=Math.max(...rows.map(r=>r.level)),max=Math.max(...rows.map(r=>r.mean_cumulative_size));
      const x=v=>box.left+(box.right-box.left)*(v-1)/Math.max(1,maxLevel-1),y=v=>box.bottom-(box.bottom-box.top)*v/Math.max(1,max);
      axes(svg,box,[['Level 1',box.left,'start'],[`Level ${maxLevel}`,box.right,'end']],
        [[numberText(max,2),box.top],[numberText(max/2,2),(box.top+box.bottom)/2],['0',box.bottom]]);
      for(const side of ['bid','ask']) {
        svgNode(svg,'polyline',{points:d.depth_profile[side].map(r=>`${x(r.level)},${y(r.mean_cumulative_size)}`).join(' '),class:`ob-${side}-series`});
        for(const r of d.depth_profile[side]) {
          const dot=svgNode(svg,'circle',{cx:x(r.level),cy:y(r.mean_cumulative_size),r:3,class:`ob-${side}-series`});
          svgNode(dot,'title',{},`${side} level ${r.level}: ${numberText(r.mean_cumulative_size,4)} cumulative; ${r.observations} observations`);
        }
      }
    }
  }
  function renderDescription(s) {
    const d=s.descriptive,c=d.clock,warning=c.status!=='consistent_receipt_clocks';
    put('ob-description-clock',`${warning?'Clock quality warning':'Receipt clocks passed the consistency audit'} · maximum wall/monotonic divergence ${numberText(c.max_divergence_seconds,6)} seconds · wall regressions ${c.wall_regressions} · monotonic regressions ${c.monotonic_regressions}. ${warning?'This report uses event order only. Timing-dependent analyses remain blocked for this recording.':'This report still uses event order; exchange timestamps and per-row freshness are not certified.'}`);
    $('ob-description-clock').classList.toggle('ob-recorder-warning',warning);
    put('ob-description-coverage',d.eligible_events?`${numberText(d.eligible_events,0)} eligible post-update book states from ${numberText(d.total_events,0)} saved events. Statistics describe the recorded displayed book only.`:'No usable two-sided post-update book states were found. No depth or distribution estimate is available. Review the event quality and operation counts below.');
    put('ob-description-warnings',d.warnings.join(' '));
    put('ob-description-exclusions','Excluded from this report: '+result.descriptive.excluded_analyses.join('; ')+'.');
    const prior=$('ob-description-metric').value,keys=Object.keys(d.distributions);
    $('ob-description-metric').replaceChildren(...keys.map(key=>{const o=make('option',metricName(key));o.value=key;return o;}));
    if(keys.includes(prior))$('ob-description-metric').value=prior;
    else if(keys.includes('visible_depth'))$('ob-description-metric').value='visible_depth';
    const fields=['count','mean','std','min','p05','p25','median','p75','p95','max','skewness','excess_kurtosis'];
    $('ob-description-statistics').replaceChildren(...keys.map(key=>{const tr=make('tr','');const name=make('th',metricName(key));name.scope='row';name.title=result.descriptive.definitions[key]??'';tr.append(name);
      for(const f of fields)tr.append(make('td',numberText(d.distributions[key][f],f==='count'?0:5)));return tr;}));
    if(!keys.length)blank('ob-description-statistics',13,'No characteristic distributions available.');
    const profile=[];
    for(const side of ['bid','ask'])for(const row of d.depth_profile[side]) {
      const tr=make('tr','');tr.append(make('th',side==='bid'?'Bid':'Ask'));
      for(const key of ['level','observations','mean_size','mean_cumulative_size','mean_distance_bps'])tr.append(make('td',numberText(row[key],key==='level'||key==='observations'?0:5)));
      profile.push(tr);
    }
    $('ob-description-levels').replaceChildren(...profile);if(!profile.length)blank('ob-description-levels',6,'No eligible depth profile.');
    const counts=$('ob-description-counts');counts.replaceChildren();
    for(const [title,values] of [['Lifecycle and updates',d.event_counts.by_kind??{}],['Displayed row operations',d.event_counts.by_side??{}],['Book-state quality',d.quality_counts]]) {
      const card=make('div','','ob-count-card');card.append(make('h4',title));const dl=make('dl','');
      const entries=(value,prefix='')=>{for(const [key,v] of Object.entries(value)){const label=(prefix?prefix+' · ':'')+key.replaceAll('_',' ');
        if(v&&typeof v==='object')entries(v,label);else{dl.append(make('dt',label),make('dd',numberText(v,0)));}}};
      entries(values);card.append(dl);counts.append(card);
    }
    descriptionPlots();
  }
  $('ob-description-metric').addEventListener('change',descriptionPlots);
  const flowName=key=>({callback_count:'Depth callbacks per bin',callback_rate:'Depth callbacks per recorded second',interarrival_seconds:'Interarrival time (recorded seconds)',ofi_sum:'Top-of-book OFI per bin'}[key]??'Within-bin '+metricName(key));
  const currentFlow=()=>result?.request.mode==='flow'?result.sessions[Number($('ob-result-session').value)]?.flow:null;
  function flowHistogram(svg,stat) {
    if(!stat?.count){emptyPlot(svg,'No eligible observations');return;}
    svg.replaceChildren();const box=chartBox(svg),h=stat.histogram,max=Math.max(...h.counts),lo=h.edges[0],hi=h.edges.at(-1),point=lo===hi;
    const x=v=>point?(box.left+box.right)/2:box.left+(box.right-box.left)*(v-lo)/(hi-lo),y=v=>box.bottom-(box.bottom-box.top)*v/max;
    axes(svg,box,point?[[flowNumber(lo,6)+' (constant)',x(lo),'middle']]:[[flowNumber(lo,6),box.left,'start'],[flowNumber(hi,6),box.right,'end']],[[flowNumber(max,0),box.top],['0',box.bottom]]);
    for(const [i,n] of h.counts.entries()) {
      const bar=svgNode(svg,'rect',{x:point?x(lo)-12:x(h.edges[i])+1,y:y(n),width:point?24:Math.max(.5,x(h.edges[i+1])-x(h.edges[i])-2),height:box.bottom-y(n),class:'ob-histogram-bar'});
      svgNode(bar,'title',{},`${flowNumber(h.edges[i],9)} to ${flowNumber(h.edges[i+1],9)}: ${n} observations`);
    }
  }
  function flowPlots() {
    const f=currentFlow();if(!f||$('ob-flow-content').hidden)return;
    const key=$('ob-flow-series').value,svg=$('ob-flow-timeline');
    const value=b=>!b.eligible?null:key==='callback_rate'?b.callback_count/b.duration_seconds:key==='callback_count'||key==='ofi_sum'?b[key]:b.means[key];
    const values=f.bins.map(value),valid=values.filter(Number.isFinite);
    put('ob-flow-series-note',`${flowName(key)}. ${result.flow.definitions?.[key]??''} ${f.status==='provisional'?'PROVISIONAL: elapsed time is conditional on the recorded-monotonic assumption.':''}`);
    if(!valid.length)emptyPlot(svg,'No eligible complete bins for this series');
    else {
      svg.replaceChildren();const box=chartBox(svg),lo0=Math.min(...valid),hi0=Math.max(...valid),pad=Math.max((hi0-lo0)*.08,Math.abs(hi0)*.000001,1e-9),lo=lo0-pad,hi=hi0+pad;
      const start=f.window.start_seconds,end=f.window.end_seconds,x=v=>box.left+(box.right-box.left)*(v-start)/Math.max(1e-12,end-start),y=v=>box.bottom-(box.bottom-box.top)*(v-lo)/(hi-lo);
      axes(svg,box,[[flowNumber(start,3)+' s',box.left,'start'],[flowNumber((start+end)/2,3)+' s',(box.left+box.right)/2,'middle'],[flowNumber(end,3)+' s',box.right,'end']],[[flowNumber(hi,4),box.top],[flowNumber((lo+hi)/2,4),(box.top+box.bottom)/2],[flowNumber(lo,4),box.bottom]]);
      // At most 10,000 complete bins; the SVG retains every eligible bin and breaks at exclusions.
      let points=[];const flush=()=>{if(points.length){svgNode(svg,'polyline',{points:points.join(' '),class:'ob-bid-series'});if(points.length===1){const [cx,cy]=points[0].split(',');svgNode(svg,'circle',{cx,cy,r:3,class:'ob-bid-series'});}points=[];}};
      for(const [i,b] of f.bins.entries()){if(!Number.isFinite(values[i])){flush();continue;}points.push(`${x((b.start_seconds+b.end_seconds)/2)},${y(values[i])}`);}flush();
    }
    flowHistogram($('ob-flow-interarrival'),f.distributions.interarrival_seconds);
    const pmf=f.model.pmf??[],chart=$('ob-flow-pmf');
    if(!pmf.length)emptyPlot(chart,'No eligible complete bins for a Poisson reference');
    else {
      chart.replaceChildren();const box=chartBox(chart),maximum=Math.max(...pmf.flatMap(p=>[p.empirical_probability,p.poisson_probability]),1e-12),width=(box.right-box.left)/pmf.length,y=v=>box.bottom-(box.bottom-box.top)*v/maximum;
      axes(chart,box,[[pmf[0].label,box.left,'start'],[pmf.at(-1).label,box.right,'end']],[[flowNumber(maximum,4),box.top],['0',box.bottom]]);
      const points=[];
      for(const [i,p] of pmf.entries()) {
        const cx=box.left+width*(i+.5),bar=svgNode(chart,'rect',{x:box.left+width*i+1,y:y(p.empirical_probability),width:Math.max(.5,width-2),height:box.bottom-y(p.empirical_probability),class:'ob-histogram-bar'});
        svgNode(bar,'title',{},`${p.label} callbacks: observed ${flowNumber(p.empirical_probability,6)}; Poisson ${flowNumber(p.poisson_probability,6)}`);
        points.push(`${cx},${y(p.poisson_probability)}`);
      }
      svgNode(chart,'polyline',{points:points.join(' '),class:'ob-ask-series'});
    }
  }
  function flowBins() {
    const f=currentFlow();if(!f)return;
    const start=flowBinPage*200,shown=f.bins.slice(start,start+200);
    $('ob-flow-bins').replaceChildren(...shown.map(b=>{
      const tr=make('tr',''),status=b.eligible?'Eligible':b.exclusion_reasons.join(', ')||'Excluded';
      for(const v of [flowNumber(b.start_seconds,3),flowNumber(b.end_seconds,3),status,...['callback_count','bid_count','ask_count','insert_count','update_count','delete_count','usable_state_count','ofi_transitions'].map(k=>flowNumber(b[k],0)),flowNumber(b.ofi_sum,4)])tr.append(make('td',v));return tr;
    }));
    put('ob-flow-bin-note',`Showing bins ${shown.length?start+1:0}–${start+shown.length} of ${f.bins.length}. Full statistics and every bin are retained in Download result JSON. Partial end bins and unavailable intervals are excluded from the model.`);
    $('ob-flow-more-bins').hidden=f.bins.length<=200;
    put('ob-flow-more-bins',start+200<f.bins.length?'Show next 200 bins':'Back to first 200 bins');
  }
  function renderFlow(s) {
    const f=s.flow,c=f.clock,blocked=['blocked_clock','no_observation_window'].includes(f.status),provisional=f.status==='provisional';
    const heading=f.status==='blocked_clock'?'Time model blocked by receipt-clock audit':f.status==='no_observation_window'?'No observation window available':provisional?'PROVISIONAL — exploratory recorded-monotonic timing':'Receipt clocks passed the consistency audit';
    put('ob-flow-clock',`${heading}. Maximum wall/monotonic disagreement: ${flowNumber(c.max_divergence_seconds,6)} seconds; wall regressions: ${c.wall_regressions}; monotonic regressions: ${c.monotonic_regressions}. ${f.status==='blocked_clock'?'No distribution over time has been estimated. In Recordings & models, choose Explore recorded monotonic time only if you accept a provisional timing assumption; clocks are never repaired or silently overridden.':provisional?'Intervals are treated as seconds by explicit assumption. Actual elapsed time and exchange event times are not certified.':'Clock agreement does not certify accuracy or exchange event times.'}`);
    $('ob-flow-clock').classList.toggle('ob-recorder-warning',blocked||provisional);
    put('ob-flow-coverage',`Session ${s.identity.session_id}: selected ${flowNumber(f.window.start_seconds,3)}–${flowNumber(f.window.end_seconds,3)} elapsed recorded seconds; bin width ${flowNumber(f.window.bin_seconds,3)} seconds. ${f.bins.filter(b=>b.eligible).length} eligible complete bins out of ${f.bins.length}. Each recording is modeled separately.`);
    put('ob-flow-warnings',f.warnings.join(' '));$('ob-flow-content').hidden=blocked;
    const model=$('ob-flow-model');model.replaceChildren();
    for(const [title,keys] of [['Rate and variation',[['eligible_bins','Eligible complete bins'],['lambda_per_bin','Mean callbacks per bin'],['rate_per_recorded_second','Callbacks per recorded second'],['sample_variance','Sample count variance']]],['Clustering and zero counts',[['dispersion_index','Variance / mean'],['observed_zero_probability','Observed zero probability'],['poisson_zero_probability','Poisson zero probability']]]]) {
      const card=make('div','','ob-count-card'),dl=make('dl','');card.append(make('h4',title));for(const [key,label] of keys)dl.append(make('dt',label),make('dd',flowNumber(f.model[key],key==='eligible_bins'?0:6)));card.append(dl);model.append(card);
    }
    const fields=['count','mean','std','min','p05','p25','median','p75','p95','max','skewness','excess_kurtosis'];
    $('ob-flow-statistics').replaceChildren(...Object.entries(f.distributions).map(([key,stat])=>{const tr=make('tr',''),head=make('th',flowName(key));head.scope='row';head.title=result.flow.definitions?.[key]??'';tr.append(head);for(const k of fields)tr.append(make('td',flowNumber(stat[k],k==='count'?0:6)));return tr;}));
    $('ob-flow-autocorrelation').replaceChildren(...(f.model.autocorrelation??[]).map(a=>{const tr=make('tr','');for(const k of ['lag','correlation','pairs'])tr.append(make('td',flowNumber(a[k],k==='correlation'?6:0)));return tr;}));
    flowBinPage=0;flowBins();flowPlots();
  }
  $('ob-flow-series').addEventListener('change',flowPlots);
  $('ob-flow-more-bins').addEventListener('click',()=>{const f=currentFlow();if(!f)return;flowBinPage=(flowBinPage+1)*200<f.bins.length?flowBinPage+1:0;flowBins();});
  window.addEventListener('resize',()=>{if(unlocked&&result?.request.mode==='describe')descriptionPlots();if(unlocked&&result?.request.mode==='flow')flowPlots();});
  const datasetLabels={sessions:'Recordings',blocked_sessions:'Recordings blocked by clocks',trading_days:'Qualified trading days',grid_points:'One-second grid points',qualified_feature_rows:'Qualified book measurements',qualified_return_endpoints:'Qualified return endpoints',qualified_blocks:'Qualified 30-minute blocks',forecast_pairs:'Eligible next-block pairs'};
  const datasetTime=stamp=>new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(stamp/1000));
  const currentDataset=()=>result?.request.mode==='dataset'?result.sessions[Number($('ob-result-session').value)]?.dataset:null;
  function datasetCard(title,values) {
    const card=make('div','','ob-count-card'),dl=make('dl','');card.append(make('h4',title));
    for(const [key,value] of Object.entries(values))dl.append(make('dt',datasetLabels[key]??key.replaceAll('_',' ')),make('dd',flowNumber(value,0)));
    card.append(dl);return card;
  }
  function datasetTables() {
    const d=currentDataset();if(!d)return;
    const blocks=d.blocks.slice(datasetBlockPage*50,datasetBlockPage*50+50);
    $('ob-dataset-blocks').replaceChildren(...blocks.map(b=>{const tr=make('tr','');
      for(const v of [b.session_date,b.block_index+1,datasetTime(b.start_unix_us),datasetTime(b.end_unix_us),b.qualified?'Yes':'No',b.feature_rows,b.return_count,
        ...['rv','bpv','positive_excess','depth_mean','proportional_spread_mean','near_depth_share_mean'].map(k=>flowNumber(b[k],6)),b.reasons.join(', ')||'—'])tr.append(make('td',v));return tr;
    }));
    if(!blocks.length)blank('ob-dataset-blocks',14,'No measurement blocks are available for this recording. Review its clock and coverage audit.');
    put('ob-dataset-block-note',`Showing ${blocks.length?datasetBlockPage*50+1:0}–${datasetBlockPage*50+blocks.length} of ${flowNumber(d.blocks_count,0)} measured blocks (preview limited to 50). The complete blocks export includes every block and its qualification flags.`);
    $('ob-dataset-block-more').hidden=d.blocks.length<=50;put('ob-dataset-block-more',(datasetBlockPage+1)*50<d.blocks.length?'Show next 50 blocks':'Back to first 50 blocks');
    const pairs=d.pairs.slice(datasetPairPage*50,datasetPairPage*50+50);
    $('ob-dataset-pairs').replaceChildren(...pairs.map(p=>{const tr=make('tr','');
      for(const v of [p.session_date,p.origin_block_index+1,p.target_block_index+1,datasetTime(p.forecast_origin_unix_us)])tr.append(make('td',v));
      const td=make('td',''),details=make('details','');details.append(make('summary','View measurements'));
      const dl=make('dl','');for(const [k,v] of Object.entries(p).filter(([k])=>!['session_date','origin_block_index','target_block_index','forecast_origin_unix_us'].includes(k)))
        dl.append(make('dt',k.replaceAll('_',' ')),make('dd',typeof v==='number'?flowNumber(v,6):v===null?'—':typeof v==='object'?JSON.stringify(v):v));
      details.append(dl);td.append(details);tr.append(td);return tr;
    }));
    if(!pairs.length)blank('ob-dataset-pairs',5,'No eligible next-block pairs. No model-ready rows have been inferred from missing or rejected measurements.');
    put('ob-dataset-pair-note',`Showing ${pairs.length?datasetPairPage*50+1:0}–${datasetPairPage*50+pairs.length} of ${flowNumber(d.pairs_count,0)} eligible pairs (preview limited to 50). Download the complete pairs table for all values.`);
    $('ob-dataset-pair-more').hidden=d.pairs.length<=50;put('ob-dataset-pair-more',(datasetPairPage+1)*50<d.pairs.length?'Show next 50 pairs':'Back to first 50 pairs');
  }
  function renderDataset(s) {
    const d=s.dataset,c=d.clock,blocked=d.status==='blocked_clock';
    put('ob-dataset-clock',`${blocked?'Receipt clocks failed consistency checks':'Receipt-clock consistency audit'} · maximum wall/monotonic disagreement ${flowNumber(c.max_divergence_seconds,6)} seconds · wall regressions ${c.wall_regressions} · monotonic regressions ${c.monotonic_regressions}. ${blocked?'This recording supplies no qualified measurements or forecast pairs. Original data remain saved; no clock override is used.':'Clock agreement is required for this dataset; it does not certify exchange timing or local clock accuracy.'}`);
    $('ob-dataset-clock').classList.toggle('ob-recorder-warning',blocked);
    put('ob-dataset-reason',d.reason||(d.coverage.forecast_pairs?'Eligible pairs are available for subsequent research. No forecasting model was fitted.':'No eligible next-block pairs were produced. Review exclusions and collect continuous coverage for adjacent full blocks.'));
    $('ob-dataset-coverage').replaceChildren(datasetCard('Recording coverage',d.coverage),datasetCard('Excluded measurements',d.exclusion_counts));
    datasetBlockPage=datasetPairPage=0;datasetTables();
  }
  function renderDatasetSummary() {
    const d=result.dataset,counts=d.summary,allBlocked=result.sessions.every(s=>s.dataset.status==='blocked_clock');
    put('ob-dataset-readiness',counts.forecast_pairs?`${flowNumber(counts.forecast_pairs,0)} eligible next-block pairs prepared from ${flowNumber(counts.qualified_blocks,0)} qualifying measurement blocks. Review coverage before model development.`:allBlocked?'No eligible research pairs: every selected recording failed receipt-clock consistency checks. The audit and exports remain available; original recordings were preserved.':'No eligible research pairs yet. The report below shows clock checks, measured blocks and the coverage needed to qualify adjacent blocks.');
    if(d.warnings?.length)$('ob-dataset-readiness').append(document.createTextNode(' '+d.warnings.join(' ')));
    $('ob-dataset-readiness').classList.toggle('ob-recorder-warning',!counts.forecast_pairs||!!d.warnings?.length);
    $('ob-dataset-totals').replaceChildren(datasetCard('Dataset totals',counts));
    $('ob-dataset-exports').replaceChildren(...result.artifacts.map(a=>{
      const card=make('div','','ob-count-card'),b=make('button',`Download ${a.file}`,'secondary');b.type='button';b.dataset.oversized=String(a.bytes>64*1024*1024);
      card.append(make('h4',({features:'One-second book measurements',minutes:'Return endpoints',blocks:'30-minute measurement blocks',pairs:'Eligible next-block pairs'})[a.name]),make('p',`${flowNumber(a.rows,0)} rows · ${flowNumber(a.bytes/1024/1024,3)} MiB`,'hint'),b);
      if(a.bytes>64*1024*1024)card.append(make('p','This export exceeds the 64 MiB browser limit; its saved file remains on the local server.','hint'));
      b.addEventListener('click',()=>work(async rev=>{
        const job=resultJob;if(!job)return;put('ob-dataset-download-note',`Downloading and checking ${a.file}…`);
        const blob=await api.downloadResearchArtifact(job,a);if(rev!==revision||!unlocked||job!==resultJob)throw new Error('Dataset download superseded.');
        const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`dataset-${job}-${a.file}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        put('ob-dataset-download-note',`${a.file} downloaded. File size and saved integrity hash verified.`);
      }));return card;
    }));
    put('ob-dataset-download-note','');put('ob-dataset-definitions',JSON.stringify({definitions:d.definitions,conventions:d.conventions,pressure_status:d.pressure_status,overlapping_recording_windows:d.overlapping_recording_windows??0,warnings:d.warnings??[]},null,2));
  }
  $('ob-dataset-block-more').addEventListener('click',()=>{const d=currentDataset();if(!d)return;datasetBlockPage=(datasetBlockPage+1)*50<d.blocks.length?datasetBlockPage+1:0;datasetTables();});
  $('ob-dataset-pair-more').addEventListener('click',()=>{const d=currentDataset();if(!d)return;datasetPairPage=(datasetPairPage+1)*50<d.pairs.length?datasetPairPage+1:0;datasetTables();});
  function renderResult() {
    $('ob-result').hidden=!result;if(!result)return;
    const label=result.source==='synthetic'?'SYNTHETIC DEMONSTRATION — no empirical claim':'IBKR recorded data — offline analysis, not a live model';
    put('ob-result-label',label);$('ob-result-label').className=result.source==='synthetic'?'ob-synthetic':'';
    const describe=result.request.mode==='describe',flow=result.request.mode==='flow',dataset=result.request.mode==='dataset';$('ob-description').hidden=!describe;$('ob-flow').hidden=!flow;$('ob-dataset').hidden=!dataset;$('ob-result-replay').hidden=flow||dataset;
    const c=result.request.configuration;put('ob-result-config',dataset?`Research dataset · ${c.levels} common levels per side · one-second measurements · ${c.return_seconds/60}-minute returns · 30-minute regular-session blocks · maximum side age ${c.max_side_age_seconds}s · strict receipt-clock checks.`:flow?`Order flow over time · ${c.bin_seconds}s bins · start ${c.start_seconds}s · end ${c.end_seconds??'recording end'} · ${c.clock_policy==='recorded_monotonic'?'Explore recorded monotonic time — provisional':'Require consistent receipt clocks'}.`:describe?'Depth, slope & distributions · all captured levels · event-weighted book states · no time grid, freshness certification or prediction model.':`${result.request.mode} · quantity ${c.quantity} · target ${c.target} · horizon ${c.horizon_seconds}s · step ${c.step_seconds}s · ${c.levels} diagnostic levels. Model errors use basis points, not dollar fills.`);
    put('ob-replay-note',describe?'Replay and line charts show a bounded selection of states in event order. Distribution statistics use all eligible states. Gaps are not filled; horizontal position is not elapsed time.':'Replay and charts are decimated for display, never for fitting. Missing/invalid intervals are not fills. The offline report does not receive new live ticks.');
    $('ob-result-session').replaceChildren(...result.sessions.map((s,i)=>{const o=make('option',`Session ${s.identity.session_id} · ${s.identity.symbol} / ${s.identity.venue} · ${s.event_count} events`);o.value=String(i);return o;}));
    const root=$('ob-comparison');root.replaceChildren();const m=result.liquidity;
    if(m?.status==='evaluated') {
      root.append(make('h3',`Validation-selected model: ${m.selected_on_validation}`),make('p','Equal-UTC-date errors on shared eligible observations. History includes current liquidity. The test partition is not used for fitting or selection.','hint'));
      const wrap=make('div','','table-scroll'),table=make('table',''),head=make('tr','');
      for(const v of ['Model','Validation RMSE (bps)','Test RMSE (bps)','Test MAE (bps)'])head.append(make('th',v));table.append(head);
      for(const model of m.models){const tr=make('tr','',model.name===m.selected_on_validation?'ob-selected-model':'');
        for(const v of [model.name,numberText(model.validation.session_equal_rmse,6),numberText(model.test.session_equal_rmse,6),numberText(model.test.session_equal_mae,6)])tr.append(make('td',v));table.append(tr);}
      wrap.append(table);root.append(wrap);
    } else if(dataset)root.append(make('h3','Dataset preparation complete — no forecasting model fitted'));
    else if(flow)root.append(make('h3','Order-flow distribution — descriptive baseline, not predictive validation'));
    else if(describe)root.append(make('h3','Descriptive report — no predictive performance claim'));
    else root.append(make('h3','Diagnostics only — no model fitted'),make('p','Select separate chronological dates and run Compare prediction models to estimate and evaluate the baselines.','hint'));
    put('ob-result-evaluation',JSON.stringify({impact_diagnostic:result.impact,evaluation:result.evaluation??null},null,2));
    $('ob-result-evaluation').closest('details').hidden=describe||flow||dataset;
    $('ob-result-coverage').previousElementSibling.textContent=dataset?'Dataset coverage, exclusions and receipt-clock audit':flow?'Time window, coverage and receipt-clock audit':describe?'Event coverage, quality and receipt-clock audit':'Coverage, exclusions and partial-session variance';
    put('ob-result-provenance',JSON.stringify({request:result.request,source_hashes:result.source_hashes,code_hashes:result.code_hashes,worker_sha256:result.worker_sha256,result_sha256:result.sha256,boundaries:result.boundaries},null,2));
    if(dataset)renderDatasetSummary();resultSession();controls();
  }
  $('ob-result-session').addEventListener('change',resultSession);$('ob-result-scrub').addEventListener('input',frame);
  $('ob-download').addEventListener('click',()=>{if(!unlocked||!result)return;
    const u=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'})),a=document.createElement('a');
    a.href=u;a.download='orderbook-workspace-result.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  });
  function clear() {
    expandGraphs(false);
    revision++;clearTimeout(liveTimer);clearTimeout(jobTimer);liveTimer=jobTimer=null;busy=false;live=null;liveHistory=[];loaded=false;needsReconcile=false;previousEvidence=null;verified=false;
    selected=null;pending=null;sessions=[];cursor='0';hasMore=false;selectedIds.clear();jobs=[];capability=null;result=null;resultJob=null;submittedJob=null;$('ob-watch').checked=false;
    archive=null;$('ob-archive').hidden=true;$('ob-archive-scrub').value='0';
    for(const name of ['ob-archive-title','ob-archive-summary','ob-archive-clock','ob-archive-frame','ob-archive-event','ob-archive-depth','ob-archive-bids','ob-archive-asks'])$(name).replaceChildren();
    $('ob-candidates').replaceChildren();$('ob-jobs').replaceChildren();$('ob-result').hidden=true;$('ob-result-session').replaceChildren();
    for(const el of ['ob-selected','ob-resolution','ob-catalog-note','ob-worker-note','ob-job-note','ob-result-label','ob-result-frame','ob-result-config','ob-result-coverage','ob-result-evaluation','ob-result-provenance'])put(el,'');
    for(const el of ['ob-result-bids','ob-result-asks','ob-result-price','ob-result-imb','ob-comparison'])$(el).replaceChildren();
    $('ob-description').hidden=true;
    for(const el of ['ob-description-clock','ob-description-coverage','ob-description-warnings','ob-description-metric','ob-description-definition','ob-description-histogram','ob-description-profile','ob-description-statistics','ob-description-levels','ob-description-counts','ob-description-exclusions'])$(el).replaceChildren();
    $('ob-flow').hidden=true;flowBinPage=0;
    for(const el of ['ob-flow-clock','ob-flow-coverage','ob-flow-warnings','ob-flow-model','ob-flow-timeline','ob-flow-series-note','ob-flow-pmf','ob-flow-interarrival','ob-flow-statistics','ob-flow-autocorrelation','ob-flow-bins','ob-flow-bin-note'])$(el).replaceChildren();
    $('ob-dataset').hidden=true;datasetBlockPage=datasetPairPage=0;
    for(const el of ['ob-dataset-readiness','ob-dataset-totals','ob-dataset-exports','ob-dataset-download-note','ob-dataset-clock','ob-dataset-reason','ob-dataset-coverage','ob-dataset-block-note','ob-dataset-blocks','ob-dataset-pair-note','ob-dataset-pairs','ob-dataset-definitions'])$(el).replaceChildren();
    renderSessions();renderLive();modeControls();
  }
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){clearTimeout(liveTimer);clearTimeout(jobTimer);liveTimer=jobTimer=null;live=null;liveHistory=[];verified=false;previousEvidence=null;renderLive();}
    else schedule();
  });
  setInterval(()=>{if(unlocked&&!document.hidden)renderLive();},1000);
  clear();
  return {
    setAccess(value){if(unlocked===value)return;unlocked=value;if(!value){context=null;generation=null;clear();notify('Dashboard locked. Capture and any research job were not stopped.');}controls();},
    update(value){
      if(!unlocked)return;
      if(value&&generation!==null&&value.session_id!==generation){clear();notify('Broker/server generation changed. Refresh the workspace before recording or selecting archived data.');}
      context=value;if(value)generation=value.session_id;
      if(!value||value.broker.state!=='ready'){selected=null;pending=null;live=null;liveHistory=[];verified=false;previousEvidence=null;$('ob-candidates').replaceChildren();put('ob-selected','Resolve a contract after the broker becomes ready.');renderLive();}
      controls();
    }
  };
}
