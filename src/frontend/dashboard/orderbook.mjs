import {parseDepth,depthView,sessionPage,researchRequest,parseJobs,parseResult,aggregate,id,jobId,terminal} from './orderbook-model.mjs';
import {numberText} from './model.mjs';

export function mountOrderbook(api,onAccessError) {
  const $=name=>document.getElementById(name), put=(name,value)=>{$(name).textContent=value;};
  const make=(tag,text,cls='')=>{const el=document.createElement(tag);el.textContent=String(text??'—');el.className=cls;return el;};
  let unlocked=false,busy=false,revision=0,context=null,generation=null;
  let live=null,liveAt=0,loaded=false,needsReconcile=false,selected=null,pending=null;
  let sessions=[],cursor='0',hasMore=false,selectedIds=new Set(),jobs=[],capability=null,result=null;
  let liveTimer=null,jobTimer=null,verified=false,previousEvidence=null;
  const ready=()=>unlocked&&context?.broker?.state==='ready';
  const selectedSessions=()=>sessions.filter(s=>selectedIds.has(s.session_id));
  const notify=t=>put('ob-notice',t);
  function tab(name) {
    for(const value of ['live','recordings','results']) {
      $(`ob-${value}-panel`).hidden=value!==name;
      $(`ob-${value}-tab`).setAttribute('aria-selected',String(value===name));
    }
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
    put('ob-selection-note',`${selectedIds.size} completed recording${selectedIds.size===1?'':'s'} selected. Quantity and time settings will be frozen in the saved request.`);
    $('ob-download').disabled=!unlocked||!result;
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
  function renderLive() {
    const v=live?depthView(live,Math.max(0,performance.now()-liveAt)):depthView(null);
    if(!v.usable){verified=false;previousEvidence=null;}
    for(const [el,k] of [['ob-spread','spread'],['ob-midpoint','midpoint'],['ob-depth','depth'],['ob-imbalance','imbalance']]) put(el,numberText(v[k],6));
    ladder('ob-bids',v.bids);ladder('ob-asks',v.asks);
    $('ob-bids').classList.toggle('ob-ladder-stale',!v.usable);$('ob-asks').classList.toggle('ob-ladder-stale',!v.usable);
    put('ob-source',!live?'NOT LOADED':live.source==='mock'?'SYNTHETIC MOCK':live.source==='ibkr_tws'?'IBKR DISPLAYED DEPTH':'BROKER DISABLED');
    $('ob-source').className='badge'+(live?.source==='mock'?' warning':'');
    put('ob-quality',v.reason+(verified?' · Advancing native callbacks observed in this tab.':' · Native update progression not confirmed.'));
    put('ob-live-identity',live?.available?`${live.venue} · conId ${live.contract_id} · request ${live.request_id} · sequence ${live.sequence} / epoch ${live.epoch} · bid ${live.book.bids.length}/${live.requested_rows} rows (${v.bids.length} distinct prices), ask ${live.book.asks.length}/${live.requested_rows} rows (${v.asks.length} distinct prices)`:'No depth request loaded. Resolve and explicitly start capture to receive rows.');
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
      live=p;liveAt=started;renderLive();
    } catch(e) {if(rev===revision){live=null;verified=false;previousEvidence=null;renderLive();}throw e;}
  }
  function renderSessions() {
    if(!sessions.length){blank('ob-sessions',7,'No recordings loaded. No missing data is assumed to be zero.');return;}
    $('ob-sessions').replaceChildren(...sessions.map(s=>{
      const tr=make('tr',''),choice=make('td',''),input=document.createElement('input');input.type='checkbox';input.value=s.session_id;
      input.setAttribute('aria-label',`Select recording ${s.session_id}`);input.checked=selectedIds.has(s.session_id);input.disabled=!terminal(s.state);
      input.addEventListener('change',()=>{if(input.checked)selectedIds.add(s.session_id);else selectedIds.delete(s.session_id);controls();});choice.append(input);tr.append(choice);
      for(const text of [`#${s.session_id} / ${s.symbol}`,`${s.venue} / ${s.source==='mock'?'SYNTHETIC':s.source}`,new Date(s.started_ms).toISOString().replace('T',' ').slice(0,19),s.requested_rows,s.event_count,s.state]) tr.append(make('td',text));
      return tr;
    }));
  }
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
      const card=make('div','','ob-job'),info=make('div','');info.append(make('p',`${j.mode==='compare'?'Model comparison':'Book inspection'} · ${j.state} · ${j.source==='mock'?'SYNTHETIC':'IBKR recorded data'}`),make('small',`${j.job_id} · sessions ${j.session_ids.join(', ')}`));
      if(j.error)info.append(make('small',j.error));card.append(info);
      if(j.state==='complete'){const b=make('button','Open result','secondary');b.disabled=!unlocked;b.addEventListener('click',()=>work(async rev=>{
        result=parseResult(await read(`/api/depth/research/jobs/${j.job_id}/result`,'GET',undefined,rev));renderResult();tab('results');
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
    const rows=Number($('ob-rows').value);if(!Number.isInteger(rows)||rows<1||rows>10){notify('Request 1..10 rows per side.');return;}
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
  $('ob-mode').addEventListener('change',()=>{$('ob-partitions').hidden=$('ob-mode').value!=='compare';});
  $('ob-research-form').addEventListener('submit',event=>{
    event.preventDefault();if(!capability?.enabled||capability.busy||needsReconcile)return;
    let request;
    try{const f=Object.fromEntries(new FormData(event.currentTarget));f.synthetic=event.currentTarget.elements.synthetic.checked;request=researchRequest(f,selectedSessions());}
    catch(e){notify(e.message);return;}
    if(!window.confirm(`Run ${request.mode==='compare'?'chronological model comparison':'book diagnostics'} on ${request.session_ids.length} completed recording(s)? Source: ${request.source==='mock'?'SYNTHETIC MOCK':'IBKR recorded data'}. Settings and date partitions are saved. No broker request or order will be sent.`))return;
    work(async rev=>{
      const ack=await read('/api/depth/research/jobs','POST',request,rev);
      if(!jobId(ack.job_id))throw new Error('Job outcome unknown. Refresh the research catalog before retrying.');
      await loadJobs(rev);tab('results');notify(`Research job ${ack.job_id} submitted. Recording continues independently.`);
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
    if(!result)return;const session=result.sessions[Number($('ob-result-session').value)],f=session?.frames[Number($('ob-result-scrub').value)];if(!f)return;
    put('ob-result-frame',`${new Date(f.unix_us/1000).toISOString()} · ${f.quality} · local sequence ${f.sequence} · recorded, not live`);
    ladder('ob-result-bids',aggregate(f.bids.map(([price,size])=>({price,size}))));ladder('ob-result-asks',aggregate(f.asks.map(([price,size])=>({price,size}))));
  }
  function resultSession() {
    if(!result)return;const s=result.sessions[Number($('ob-result-session').value)];if(!s)return;
    $('ob-result-scrub').max=String(Math.max(0,s.frames.length-1));$('ob-result-scrub').value=String(Math.max(0,s.frames.findIndex(f=>f.usable)));
    put('ob-result-coverage',JSON.stringify({session:s.identity,grid_points:s.grid_count,eligible_rows:s.sample_count,grid_quality:s.quality_counts,event_quality:s.event_quality_counts,exclusions:s.missing_targets,partial_session_variation:s.partial_session_variation},null,2));
    plot('ob-result-price',s.frames,'midpoint');plot('ob-result-imb',s.frames,'depth_imbalance');frame();
  }
  function renderResult() {
    $('ob-result').hidden=!result;if(!result)return;
    const label=result.source==='synthetic'?'SYNTHETIC DEMONSTRATION — no empirical claim':'IBKR recorded data — offline analysis, not a live model';
    put('ob-result-label',label);$('ob-result-label').className=result.source==='synthetic'?'ob-synthetic':'';
    const c=result.request.configuration;put('ob-result-config',`${result.request.mode} · quantity ${c.quantity} · target ${c.target} · horizon ${c.horizon_seconds}s · step ${c.step_seconds}s · ${c.levels} diagnostic levels. Model errors use basis points, not dollar fills.`);
    $('ob-result-session').replaceChildren(...result.sessions.map((s,i)=>{const o=make('option',`Session ${s.identity.session_id} · ${s.identity.symbol} / ${s.identity.venue} · ${s.event_count} events`);o.value=String(i);return o;}));
    const root=$('ob-comparison');root.replaceChildren();const m=result.liquidity;
    if(m?.status==='evaluated') {
      root.append(make('h3',`Validation-selected model: ${m.selected_on_validation}`),make('p','Equal-UTC-date errors on shared eligible observations. History includes current liquidity. The test partition is not used for fitting or selection.','hint'));
      const wrap=make('div','','table-scroll'),table=make('table',''),head=make('tr','');
      for(const v of ['Model','Validation RMSE (bps)','Test RMSE (bps)','Test MAE (bps)'])head.append(make('th',v));table.append(head);
      for(const model of m.models){const tr=make('tr','',model.name===m.selected_on_validation?'ob-selected-model':'');
        for(const v of [model.name,numberText(model.validation.session_equal_rmse,6),numberText(model.test.session_equal_rmse,6),numberText(model.test.session_equal_mae,6)])tr.append(make('td',v));table.append(tr);}
      wrap.append(table);root.append(wrap);
    } else root.append(make('h3','Diagnostics only — no model fitted'),make('p','Select separate chronological dates and run Compare prediction models to estimate and evaluate the baselines.','hint'));
    put('ob-result-evaluation',JSON.stringify({impact_diagnostic:result.impact,evaluation:result.evaluation??null},null,2));
    put('ob-result-provenance',JSON.stringify({request:result.request,source_hashes:result.source_hashes,code_hashes:result.code_hashes,worker_sha256:result.worker_sha256,result_sha256:result.sha256,boundaries:result.boundaries},null,2));
    resultSession();controls();
  }
  $('ob-result-session').addEventListener('change',resultSession);$('ob-result-scrub').addEventListener('input',frame);
  $('ob-download').addEventListener('click',()=>{if(!unlocked||!result)return;
    const u=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'})),a=document.createElement('a');
    a.href=u;a.download='orderbook-workspace-result.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  });
  function clear() {
    revision++;clearTimeout(liveTimer);clearTimeout(jobTimer);liveTimer=jobTimer=null;busy=false;live=null;loaded=false;needsReconcile=false;previousEvidence=null;verified=false;
    selected=null;pending=null;sessions=[];cursor='0';hasMore=false;selectedIds.clear();jobs=[];capability=null;result=null;$('ob-watch').checked=false;
    $('ob-candidates').replaceChildren();$('ob-jobs').replaceChildren();$('ob-result').hidden=true;$('ob-result-session').replaceChildren();
    for(const el of ['ob-selected','ob-resolution','ob-catalog-note','ob-worker-note','ob-job-note','ob-result-label','ob-result-frame','ob-result-config','ob-result-coverage','ob-result-evaluation','ob-result-provenance'])put(el,'');
    for(const el of ['ob-result-bids','ob-result-asks','ob-result-price','ob-result-imb','ob-comparison'])$(el).replaceChildren();
    renderSessions();renderLive();controls();
  }
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){clearTimeout(liveTimer);clearTimeout(jobTimer);liveTimer=jobTimer=null;live=null;verified=false;previousEvidence=null;renderLive();}
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
      if(!value||value.broker.state!=='ready'){selected=null;pending=null;live=null;verified=false;previousEvidence=null;$('ob-candidates').replaceChildren();put('ob-selected','Resolve a contract after the broker becomes ready.');renderLive();}
      controls();
    }
  };
}
