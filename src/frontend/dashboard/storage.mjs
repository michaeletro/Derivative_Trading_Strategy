import {historyURL,validatePage,columns} from './storage-model.mjs';

export function mountStorage(api,onAccessError) {
  const $=id=>document.getElementById(id);
  const put=(id,v)=>{$(id).textContent=v;};
  const make=(tag,text)=>{const n=document.createElement(tag);n.textContent=String(text??'—');return n;};
  let unlocked=false,busy=false,revision=0,source=[],seriesCursor='0',historyCursor='0',through='0',nextSeries=false,nextHistory=false,brokerState=null;
  function controls(){
    for(const id of ['storage-load','storage-select','storage-history'])$(id).disabled=!unlocked||busy;
    $('storage-series-next').disabled=!unlocked||busy||!nextSeries;
    $('storage-more').disabled=!unlocked||busy||!nextHistory;
    $('storage-backup').disabled=!unlocked||busy||!brokerState||['ready','connecting'].includes(brokerState);
  }
  function empty(){
    nextHistory=false;historyCursor='0';through='0';$('storage-head').replaceChildren();$('storage-body').replaceChildren();
    put('storage-history-note','Choose a recorded series. These are past observations, never current broker quotes.');
  }
  async function work(fn){
    if(!unlocked||busy)return;busy=true;const version=revision;controls();put('storage-notice','');
    try{await fn(version);}catch(error){if(version===revision){if([401,403].includes(error.status))onAccessError(error);else put('storage-notice',error.message||'Storage operation failed. No retry was sent.');}}
    finally{if(version===revision){busy=false;controls();}}
  }
  async function loadSeries(after,version){
    const page=validatePage(await api.request(`/api/storage/series?limit=100&after_id=${encodeURIComponent(after)}`));
    if(version!==revision)return;
    source=page.rows;seriesCursor=page.next_after_id;nextSeries=page.has_more;
    const select=$('storage-select');select.replaceChildren();
    for(const s of source){const option=make('option',`${s.symbol||'(no symbol)'} · ${s.source} · ${s.kind} · ${s.row_count} observations · series ${s.series_id}`);option.value=s.series_id;select.append(option);}
    empty();put('storage-history-note',source.length?`${source.length} recorded series on this page. Choose one and read its history.`:'No series recorded yet. Subscribe to broker quotes or request bars through /api/assets.');
  }
  async function loadHistory(after,version){
    const selected=$('storage-select').value;const item=source.find(s=>s.series_id===selected);
    if(!item)throw new Error('Load and select a recorded series first');
    const page=validatePage(await api.request(historyURL(selected,after,through)));
    if(version!==revision)return;historyCursor=page.next_after_id;through=page.through_id;nextHistory=page.has_more;
    const keys=columns(item.kind);const heading=document.createElement('tr');
    for(const key of keys)heading.append(make('th',key==='observed_ms'?'Recorded at (UTC)':key.replaceAll('_',' ')));
    $('storage-head').replaceChildren(heading);
    $('storage-body').replaceChildren(...page.rows.map(row=>{
      const tr=document.createElement('tr');for(const key of keys){let v=row[key];if(key==='observed_ms'&&Number.isFinite(v))v=new Date(v).toISOString();tr.append(make('td',v));}return tr;
    }));
    put('storage-history-note',`${item.symbol||'Series'} · ${page.rows.length} rows on this page · snapshot through record ${through}. ${item.kind==='quote'?'Quote validity is evaluated at recording time, not now.':'Source dates, cadence and adjustment basis are not inferred; corrected bars retain revisions.'}`);
  }
  $('storage-load').addEventListener('click',()=>work(v=>loadSeries('0',v)));
  $('storage-series-next').addEventListener('click',()=>work(v=>loadSeries(seriesCursor,v)));
  $('storage-select').addEventListener('change',()=>{empty();controls();});
  $('storage-history').addEventListener('click',()=>{through='0';work(v=>loadHistory('0',v));});
  $('storage-more').addEventListener('click',()=>work(v=>loadHistory(historyCursor,v)));
  $('storage-backup').addEventListener('click',()=>work(async version=>{
    const result=await api.request('/api/storage/backup','POST',{});
    if(version===revision)put('storage-notice',`Consistent backup created: ${result.backup}`);
  }));
  return {
    setAccess(value){
      if(unlocked===value)return;unlocked=value;revision++;busy=false;
      if(!value){source=[];nextSeries=false;empty();$('storage-select').replaceChildren();put('storage-notice','');}
      controls();
    },
    update(status,state){
      brokerState=state;
      put('storage-state',!unlocked?'Locked':!status?'Unavailable':status.failed?'RECORDING FAILED':status.open?'Persistent storage open':'Closed');
      put('storage-counts',unlocked&&status?`${status.quote_count} quote updates · ${status.bar_count} bar versions · ${status.interrupted_runs} interrupted runs detected`:'Unlock to inspect saved data.');
      put('storage-path',unlocked&&status?status.database:'—');
      put('storage-last-backup',unlocked&&status?(status.last_backup||'No completed backup yet. Created on orderly shutdown.'):'—');
      if(status?.failed)put('storage-notice',status.last_error||'Recording failed. Resolve the storage problem and restart.');
      controls();
    }
  };
}
