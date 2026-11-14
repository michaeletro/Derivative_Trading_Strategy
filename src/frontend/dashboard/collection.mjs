import {collectionRequest,collectionConfirmation,parseCollection,durationText} from './collection-model.mjs';

// The order-book workspace supplies its authenticated, generation-checked reader
// and mutation queue. Polling continues on every workspace while unlocked.
export function mountCollection({read,work,afterMutation,notify,isCurrent}) {
  const $=id=>document.getElementById(id),put=(id,value)=>{$(id).textContent=value;};
  const make=(tag,value,cls='')=>{const node=document.createElement(tag);node.textContent=String(value??'—');node.className=cls;return node;};
  const label=value=>String(value??'unknown').replaceAll('_',' ');
  const when=value=>new Date(value).toLocaleString(undefined,{timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})+' ET';
  let access={unlocked:false,busy:false,ready:false,loaded:false,reconcile:false,selected:null,live:null};
  let status=null,lastSuccess=0,readiness=null,inspected=null,warning=null,mutationEpoch=0,daily=null,dailyReadAt=0;
  const acknowledged=new Set(),interruptions=new Map();
  const active=()=>status?.active===true;
  const fresh=()=>lastSuccess>0&&Date.now()-lastSuccess<5000;
  function duration() {
    const minutes=Number($('ob-collection-minutes').value),segments=Number($('ob-collection-segments').value);
    put('ob-collection-duration',Number.isInteger(minutes)&&Number.isInteger(segments)&&minutes>0&&segments>0?
      `${durationText(minutes*60*segments)} total · up to four hours per plan.`:'Choose a segment duration and count.');
  }
  function render() {
    const p=status?.current,unlocked=access.unlocked;
    const scheduleText=!unlocked?'Unlock to inspect the automatic daily schedule.':!daily?'Daily schedule status not loaded.':!daily.configured?'Daily collection is not configured on this server.':daily.available===false?'Daily schedule status unavailable; inspect the local collector.':
      `${daily.enabled?'Enabled':'Paused'} · 9:00 AM–4:30 PM Eastern · ${label(daily.phase)}${daily.reason?' · '+label(daily.reason):''}${daily.next_collection_open_ms?` · Next collection window: ${when(daily.next_collection_open_ms)}`:''}${daily.updated_ms?` · Last checked: ${when(daily.updated_ms)}`:''}. TWS must be logged into the authorized paper account.`;
    put('ob-daily-schedule',scheduleText);
    const setupAllowed=unlocked&&access.ready&&access.loaded&&!access.reconcile&&!active()&&!access.live?.available&&!!access.selected&&fresh();
    const startAllowed=setupAllowed&&!access.busy;
    $('ob-collection-fields').disabled=!setupAllowed;
    $('ob-collection-paper').disabled=!setupAllowed||access.busy;
    $('ob-collection-start').disabled=!startAllowed||!$('ob-collection-paper').checked;
    $('ob-collection-stop').disabled=!unlocked||access.busy||!active()||access.reconcile||!fresh();
    $('ob-collection-refresh').disabled=!unlocked||access.busy;
    $('ob-collection-progress').hidden=!p;
    if(p){$('ob-collection-progress').max=p.requested_seconds;$('ob-collection-progress').value=Math.min(p.requested_seconds,p.elapsed_seconds);}
    put('ob-collection-status',!unlocked?'Unlock to inspect timed collections.':!status?'Timed collection status not loaded.':!p?'No timed collection has been started.':
      `${p.source==='mock'?'SYNTHETIC · ':''}${p.symbol} / ${p.venue} · ${label(p.state)} · ${p.segments_completed}/${p.segments_requested} finalized segments · ${p.checkpoint_metrics_incomplete?'at least ':''}${durationText(p.elapsed_seconds)} ${p.checkpoint_metrics_incomplete?'at last durable checkpoint':'elapsed'}${active()?` · ${durationText(p.remaining_seconds)} remaining`:''}${p.schedule_id?` · Scheduled stop: ${when(p.collection_close_ms)}`:''}${p.reason?` · ${label(p.reason)}`:''}.`);
    const health=[];
    if(unlocked&&status)health.push(fresh()?'Status refreshed within 5 seconds':`Status is stale${lastSuccess?` (${durationText((Date.now()-lastSuccess)/1000)} since refresh)`:''}; collection outcome is not confirmed`);
    if(warning)health.push(warning);
    if(access.reconcile)health.push('An action has an unknown outcome. Use Load / refresh workspace before changing capture.');
    if(p?.checkpoint_metrics_incomplete)health.push('Recovered interruption: duration and delivered-update counts are checkpoint lower bounds. Saved event counts were reconciled from the archive; full elapsed time and delivery cannot be inferred from a zero checkpoint.');
    if(p?.warnings?.length)health.push(...p.warnings.map(label));
    if(Number.isFinite(p?.last_update_age_seconds))health.push(active()?`Latest delivered depth update ${durationText(p.last_update_age_seconds)} ago`:`Depth update age at last inspection: ${durationText(p.last_update_age_seconds)}`);
    if(p?.book_quality)health.push(`Book: ${label(p.book_quality)}`);
    if(typeof readiness?.recorder?.healthy==='boolean')health.push(readiness.recorder.healthy?'Recorder healthy':'Recorder needs attention');
    if(typeof readiness?.clock?.state==='string')health.push(`Clock: ${label(readiness.clock.state)}`);
    put('ob-collection-health',health.join(' · '));
    $('ob-collection-health').classList.toggle('ob-recorder-warning',!!warning||(unlocked&&!!status&&!fresh())||!!p?.warnings?.length);
    const chosen=status?.plans.find(x=>x.collection_id===inspected)??p;
    const rows=(chosen?.segments??[]).map(s=>{
      const tr=make('tr','');
      const evidence=[s.finalized?'Finalized':'Not finalized',s.gap?'gap recorded':null,s.archive_state?`archive ${label(s.archive_state)}`:null,
        `committed sequence ${s.committed_sequence}`,`raw through ${s.raw_committed_sequence??'unavailable'}`,`${chosen.checkpoint_metrics_incomplete?'at least ':''}${s.updates_observed} depth updates${chosen.checkpoint_metrics_incomplete?' at checkpoint':''}`,
        s.delivery_observed?'delivery observed':chosen.checkpoint_metrics_incomplete?'full delivery unknown from checkpoint':'delivery not observed',s.reason?label(s.reason):null].filter(Boolean).join(' · ');
      const identity=make('td',`${s.index} / ${s.session_id?'#'+s.session_id:'pending'}`);
      identity.append(make('small',`${when(s.started_ms)} → ${s.ended_ms?when(s.ended_ms):'ongoing'}`,'ob-collection-times'));tr.append(identity);
      for(const v of [label(s.state),`${chosen.checkpoint_metrics_incomplete?'at least ':''}${durationText(s.elapsed_seconds)}${chosen.checkpoint_metrics_incomplete?' (checkpoint)':''}`,s.committed_event_count,evidence])tr.append(make('td',v));
      if(s.gap||s.state==='interrupted')tr.classList.add('ob-recorder-warning');return tr;
    });
    if(!rows.length){const tr=make('tr',''),td=make('td',unlocked?'No timed segments loaded.':'Unlock to inspect collection segments.');td.colSpan=5;tr.append(td);rows.push(tr);}
    $('ob-collection-segment-list').replaceChildren(...rows);
    put('ob-collection-catalog-note',status?`Newest ${status.plans.length} of at most ${status.plan_limit} saved plans. ${status.invalid_records?`${status.invalid_records} invalid records were not loaded. `:''}Times below are Eastern. ${chosen&&chosen.collection_id!==p?.collection_id?'The segment table shows the selected earlier plan.':'The segment table shows the current or most recent plan.'}`:'');
    $('ob-collection-plans').replaceChildren(...(status?.plans??[]).map(item=>{
      const div=make('div','','ob-collection-plan'),text=make('p',`${item.source==='mock'?'SYNTHETIC · ':''}${when(item.created_ms)} · ${item.symbol}/${item.venue} · ${label(item.state)} · ${item.segments_completed}/${item.segments_requested} finalized${item.reason?' · '+label(item.reason):''}`),button=make('button','Inspect segments','secondary');
      button.type='button';button.disabled=!unlocked||access.busy;button.setAttribute('aria-label',`Inspect collection ${item.collection_id}`);button.addEventListener('click',()=>{inspected=item.collection_id;render();});div.append(text,button);return div;
    }));
    const unacknowledged=[...interruptions.values()].filter(item=>!acknowledged.has(item.collection_id));
    $('collection-interruption').hidden=!unlocked||!unacknowledged.length;
    put('collection-interruption-text',unacknowledged.map(item=>`${item.symbol}/${item.venue} collection interrupted: ${label(item.reason)}. ${item.segments_completed}/${item.segments_requested} segments finalized; inspect its saved segment and gap evidence.`).join(' '));
    $('collection-acknowledge').disabled=!unlocked||access.busy||!fresh();
  }
  function accept(value) {
    const next=parseCollection(value);status=next;lastSuccess=Date.now();warning=null;
    for(const p of [next.current,...next.plans])if(p?.state==='interrupted')interruptions.set(p.collection_id,p);
    render();return next;
  }
  async function refresh(rev) {
    const epoch=mutationEpoch;
    try{
      const value=await read('/api/depth/collection','GET',undefined,rev);
      if(epoch!==mutationEpoch||!isCurrent(rev))return status;
      accept(value);
      if(Date.now()-dailyReadAt>=10000){
        dailyReadAt=Date.now();
        try{const schedule=await read('/api/depth/collection/schedule','GET',undefined,rev);if(epoch===mutationEpoch&&isCurrent(rev)){daily=schedule;render();}}
        catch{if(epoch===mutationEpoch&&isCurrent(rev)){daily={configured:true,available:false};render();}}
      }
      return status;
    }
    catch(error){if(epoch===mutationEpoch&&isCurrent(rev)){warning='Collection status unavailable. Refresh before changing capture.';lastSuccess=0;render();}throw error;}
  }
  function requestStop() {
    if(!active()||access.busy)return;
    if(!fresh()||access.reconcile){notify('Refresh the workspace to confirm the current collection before stopping it.');return;}
    const collectionId=status.current.collection_id;
    if(!window.confirm('Stop this entire timed collection now? The current recording will be finalized if possible, and no later segment will start. Saved events and any gap evidence will remain.'))return;
    work(async rev=>{
      mutationEpoch++;
      const outcome=accept(await read('/api/depth/collection/stop','POST',{collection_id:collectionId},rev));
      await afterMutation(rev,false);notify(`Timed collection ${label(outcome.current?.state)}. Inspect the saved segment evidence; no further segment will start.`);
    },true);
  }
  $('ob-collection-form').addEventListener('submit',event=>{
    event.preventDefault();if(!access.unlocked||access.busy||active()||!fresh()||!access.ready||!access.loaded||access.reconcile||access.live?.available)return;
    let request;const selected=access.selected?{...access.selected}:null;
    try{request=collectionRequest({rows:$('ob-rows').value,segment_minutes:$('ob-collection-minutes').value,segments:$('ob-collection-segments').value,paper_read_only_confirmed:$('ob-collection-paper').checked},selected);}
    catch(error){notify(error.message);return;}
    if(!window.confirm(collectionConfirmation(request,selected)))return;
    $('ob-collection-paper').checked=false;inspected=null;
    work(async rev=>{
      mutationEpoch++;
      const outcome=accept(await read('/api/depth/collection/start','POST',request,rev));await afterMutation(rev,outcome.active);
      notify(outcome.active?'Timed collection acknowledged. Verify advancing delivered updates and saved events; acknowledgement alone does not prove depth delivery.':`Timed collection ${label(outcome.current?.state)}. Inspect its saved evidence before starting another plan.`);
    },true);
  });
  for(const id of ['ob-collection-minutes','ob-collection-segments'])$(id).addEventListener('input',duration);
  $('ob-collection-paper').addEventListener('change',render);
  $('ob-collection-stop').addEventListener('click',requestStop);
  $('ob-collection-refresh').addEventListener('click',()=>work(refresh));
  $('collection-acknowledge').addEventListener('click',()=>{if(!fresh()||access.busy)return;for(const id of interruptions.keys())acknowledged.add(id);render();});
  duration();render();
  return {refresh,active,requestStop,readiness(value){readiness=value;render();},update(value){access=value;render();},
    clear(){mutationEpoch++;status=null;lastSuccess=0;readiness=null;inspected=null;warning=null;daily=null;dailyReadAt=0;acknowledged.clear();interruptions.clear();$('ob-collection-paper').checked=false;render();},tick:render};
}
