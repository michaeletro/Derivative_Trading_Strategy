import {mountHistory} from './history.mjs';
import {mountStorage} from './storage.mjs';
import {mountGreeks} from './greeks.mjs';
import {buildLabel} from './greeks-model.mjs';
import {mountPricing} from './pricing.mjs';
import {POLL_MS, numberText, titleCase, quoteView, sample, validateSnapshot, contractQuery, createApi} from './model.mjs';

const $=id=>document.getElementById(id);
const api=createApi();
const pricingLab=mountPricing(api,error=>lock(error.message));
const greeksLab=mountGreeks(api,error=>lock(error.message));
const historyLab=mountHistory(api,error=>lock(error.message));
const storageLab=mountStorage(api,error=>lock(error.message));
let unlocked=false, busy=false, current=null, currentAt=0, fresh=false, revision=0, timer;
let session='', selected=null, pendingResolution=null, candidates=[], requestedPositions=false;
let histories=new Map(), quoteRows=new Map(), positionSignature='';
const text=(id,value)=>{ $(id).textContent=value; };
const node=(tag,value,className='')=>{ const n=document.createElement(tag); n.textContent=value; n.className=className; return n; };
const notify=message=>{ text('notice',message); $('notice').hidden=!message; };
const ready=()=>unlocked && fresh && current?.broker.state==='ready';
const elapsed=()=>Math.max(0,performance.now()-currentAt);
function emptyRow(body,message,detail='') {
  const row=document.createElement('tr'); row.className='empty'; const cell=node('td',message); cell.colSpan=6;
  if(detail) cell.append(node('small',detail)); row.append(cell); body.replaceChildren(row);
}
function clearSession() {
  selected=null; histories.clear(); quoteRows.clear(); candidates=[]; pendingResolution=null; requestedPositions=false;
  $('quotes-body').replaceChildren(); $('candidates').replaceChildren(); text('resolution-note',''); positionSignature='';
}
function lock(message='Token removed from this tab. The broker session was not disconnected.') {
  revision++; api.clear(); clearTimeout(timer); unlocked=false; busy=false; current=null; fresh=false; session='';
  clearSession(); $('token').value=''; render(); notify(message);
}
function schedule() {
  clearTimeout(timer);
  if(unlocked && !document.hidden) timer=setTimeout(refresh,POLL_MS);
}
function handleError(error) {
  if(error.status===401 || error.status===403) { lock(error.message); return; }
  fresh=false;
  for(const [id,history] of histories) histories.set(id,sample(history,Date.now(),null));
  notify(error.message || 'Operation unavailable. Refresh to inspect the service.');
}
async function readSnapshot(version) {
  const started=performance.now();
  const data=validateSnapshot(await api.request('/api/dashboard'));
  if(version!==revision) return;
  if(data.session_id!==session) { clearSession(); session=data.session_id; }
  current=data; currentAt=started; fresh=true;
  if(data.broker.state!=='ready') clearSession();
  if(data.positions.status!=='unavailable') requestedPositions=true;
  const active=new Set(data.subscriptions.map(s=>s.contract.contract_id));
  for(const id of histories.keys()) if(!active.has(id)) histories.delete(id);
  if(!active.has(selected)) selected=data.subscriptions[0]?.contract.contract_id ?? null;
  for(const row of data.subscriptions) {
    const id=row.contract.contract_id;
    histories.set(id,sample(histories.get(id)??[],Date.now(),quoteView(row.quote,elapsed()).mid));
  }
}
async function checkResolution(version) {
  if(!pendingResolution || !ready()) return;
  const id=pendingResolution;
  const result=await api.request(`/api/contracts/requests/${id}`);
  if(version!==revision || pendingResolution!==id) return;
  if(result.status==='complete') {
    if(!Array.isArray(result.contracts) || result.contracts.length>64 || result.contracts.some(c=>!Number.isSafeInteger(c.contract_id)||c.contract_id<=0))
      throw new Error('Invalid contract results; nothing was selected.');
    candidates=result.contracts; pendingResolution=null;
    text('resolution-note',`${candidates.length} candidate${candidates.length===1?'':'s'} returned. Verify the full contract before subscribing.`);
    renderCandidates();
  } else if(result.status==='failed' || result.status==='unavailable') {
    pendingResolution=null; candidates=[]; renderCandidates();
    text('resolution-note','Resolution failed or returned no supported contracts. Check the gateway and query.');
  } else if(result.status!=='pending') throw new Error('Unknown contract-request state.');
}
async function refresh() {
  if(!unlocked || busy || document.hidden) return;
  const version=revision; busy=true; render();
  try { await readSnapshot(version); await checkResolution(version); }
  catch(error) { if(version===revision) handleError(error); }
  finally { if(version===revision) { busy=false; render(); schedule(); } }
}
async function command(action) {
  if(!unlocked || busy) return;
  const version=revision; clearTimeout(timer); busy=true; render(); notify('');
  try { await action(); if(version===revision) await readSnapshot(version); }
  catch(error) { if(version===revision) handleError(error); }
  finally { if(version===revision) { busy=false; render(); schedule(); } }
}
$('access-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(busy) return;
  revision++; const version=revision; api.setToken($('token').value.trim()); $('token').value='';
  unlocked=true; busy=true; current=null; fresh=false; session=''; clearSession(); notify(''); render();
  try { await readSnapshot(version); }
  catch(error) { if(version===revision) { lock(error.message); } }
  finally { if(version===revision) { busy=false; render(); schedule(); } }
});
$('forget').addEventListener('click',()=>{ lock(); $('token').focus(); });
$('refresh').addEventListener('click',()=>{ notify(''); refresh(); });
$('connect').addEventListener('click',()=>command(()=>api.request('/api/broker/connect','POST')));
$('disconnect').addEventListener('click',()=>$('disconnect-dialog').showModal());
$('keep-connected').addEventListener('click',()=>$('disconnect-dialog').close());
$('confirm-disconnect').addEventListener('click',()=>{
  $('disconnect-dialog').close(); command(()=>api.request('/api/broker/disconnect','POST'));
});
$('snapshot').addEventListener('click',()=>command(async()=>{
  requestedPositions=true; // Never retry a timed-out snapshot request blindly.
  await api.request('/api/positions/refresh','POST');
}));
$('security-type').addEventListener('change',()=>{
  const option=$('security-type').value==='OPT'; $('option-fields').hidden=!option;
  for(const field of ['strike','expiry']) $('contract-form').elements[field].required=option;
});
$('contract-form').addEventListener('submit',event=>{
  event.preventDefault(); if(!ready() || pendingResolution) return;
  let query; try { query=contractQuery(new FormData(event.currentTarget)); } catch(error) { notify(error.message); return; }
  command(async()=>{
    candidates=[]; renderCandidates(); text('resolution-note','Requesting contract candidates…');
    const result=await api.request('/api/contracts/resolve','POST',query);
    if(!Number.isSafeInteger(result.request_id)||result.request_id<=0) throw new Error('Invalid resolution request ID.');
    pendingResolution=result.request_id;
    text('resolution-note',`Request ${pendingResolution} pending. No contract has been selected.`);
  });
});
function renderCandidates() {
  const root=$('candidates'); root.replaceChildren();
  for(const c of candidates) {
    const item=node('article','','candidate'); item.append(node('h3',`${c.symbol} · ${c.security_type} · ${c.contract_id}`));
    item.append(node('p',`${c.exchange} / ${c.currency} / multiplier ${numberText(c.multiplier)}`));
    if(c.security_type==='OPT') item.append(node('p',`${c.expiry} · ${c.right} ${numberText(c.strike)} · exercise: ${c.exercise_style ?? 'unknown'}`));
    const button=node('button',`Subscribe ${c.contract_id}`);
    button.dataset.contractId=c.contract_id;
    button.addEventListener('click',()=>command(async()=>{
      await api.request('/api/subscriptions','POST',{contract_id:c.contract_id});
    }));
    item.append(button);
    if(c.security_type==='STK'&&c.currency==='USD'){
      const historyButton=node('button','Historical bars','secondary');
      historyButton.addEventListener('click',()=>historyLab.selectContract(c));item.append(historyButton);
    }
    root.append(item);
  }
  updateCandidateButtons();
}
function updateCandidateButtons() {
  for(const button of $('candidates').querySelectorAll('button[data-contract-id]')) {
    const subscribed=current?.subscriptions.some(row=>row.contract.contract_id===Number(button.dataset.contractId));
    button.disabled=!ready() || busy || subscribed;
    button.textContent=subscribed ? 'Subscribed' : `Subscribe ${button.dataset.contractId}`;
  }
}
function renderQuotes() {
  const rows=current?.subscriptions??[], body=$('quotes-body');
  if(!rows.length) {
    quoteRows.clear();
    emptyRow(body,fresh?'No active subscriptions.':'No subscriptions loaded.','Resolve an instrument, inspect the candidates, then subscribe.'); return;
  }
  const ids=new Set(rows.map(row=>row.subscription_id));
  for(const [id,row] of quoteRows) if(!ids.has(id)) { row.root.remove(); quoteRows.delete(id); }
  if(body.querySelector('.empty')) body.replaceChildren();
  for(const row of rows) {
    const c=row.contract, id=c.contract_id; let view=quoteRows.get(row.subscription_id);
    if(!view) {
      const root=node('tr',''), cells=Array.from({length:6},()=>node('td',''));
      const choose=node('button','','link'); choose.addEventListener('click',()=>{ selected=id; render(); });
      cells[0].append(choose,node('small',String(id)));
      const remove=node('button','Unsubscribe','link remove'); remove.setAttribute('aria-label',`Unsubscribe ${c.symbol} ${id}`);
      remove.addEventListener('click',()=>command(()=>api.request(`/api/subscriptions/${row.subscription_id}`,'DELETE')));
      cells[5].append(remove); root.append(...cells); body.append(root); view={root,cells,choose,remove}; quoteRows.set(row.subscription_id,view);
    }
    view.choose.textContent=c.symbol+(c.security_type==='OPT'?` ${c.expiry} ${c.right} ${numberText(c.strike)}`:'');
    view.choose.setAttribute('aria-pressed',String(selected===id));
    const q=fresh && ready() ? row.quote : null, quality=quoteView(q,elapsed());
    for(const [index,side] of [[1,q?.bid],[2,q?.ask]]) {
      view.cells[index].replaceChildren(node('span',numberText(side?.price)),node('small',side?`${numberText(Math.max(0,side.receipt_age_ms+elapsed())/1000,1)}s receipt age`:'No side'));
    }
    view.cells[3].textContent=numberText(quality.mid);
    view.cells[4].replaceChildren(node('span',q?quality.feed:'Unavailable'),node('small',fresh?quality.quality:'Service data not current'));
    view.remove.disabled=!ready() || busy; view.choose.disabled=!unlocked;
  }
}
function renderPositions() {
  const view=fresh?current?.positions:null;
  const signature=JSON.stringify(view??null); if(signature===positionSignature) return; positionSignature=signature;
  const body=$('positions-body');
  if(!view || view.status!=='complete') {
    const message=view?.status==='pending' ? 'Snapshot pending — positions are not yet available.' : view?.status==='failed' ? 'Snapshot failed — holdings are unknown, not zero.' : 'Positions unavailable — no completed snapshot.';
    emptyRow(body,message);
    text('position-description','One native snapshot per connection; not continuous reconciliation. No account value or risk estimate is inferred.'); return;
  }
  const rows=view.positions;
  if(!rows.length) emptyRow(body,'Completed snapshot contains no supported nonzero positions.');
  else body.replaceChildren(...rows.slice(0,100).map(position=>{
    const row=node('tr',''), c=position.contract;
    for(const value of [position.account,`${c?.symbol??'Unknown'} / ${c?.contract_id??'—'}`,c?.security_type,numberText(position.quantity,8),numberText(c?.multiplier),c?.currency]) row.append(node('td',String(value??'—')));
    return row;
  }));
  const stamp=Number.isFinite(view.completed_at_unix_ms)?new Date(view.completed_at_unix_ms).toLocaleString():'time unavailable';
  text('position-description',`Completed ${stamp} · ${rows.length} positions${rows.length>100?' (showing first 100)':''}. Snapshot only, not a live portfolio. Reconnect to request another native snapshot.`);
}
function renderChart() {
  const row=current?.subscriptions.find(item=>item.contract.contract_id===selected);
  const points=histories.get(selected)??[], valid=points.filter(p=>p.value!==null);
  const mark=ready()?quoteView(row?.quote,elapsed()).mid:null;
  text('chart-value',numberText(mark));
  text('chart-label',row?`${row.contract.symbol} / ${selected} · browser polling observations, not historical ticks.`:'Select a subscribed instrument. This is not historical tick data.');
  text('chart-time',valid.length?`${valid.length} observations · last ${new Date(valid.at(-1).time).toLocaleTimeString()}`:'No recorded observations');
  $('chart-empty').hidden=valid.length>0;
  const canvas=$('chart'), box=canvas.getBoundingClientRect(), scale=window.devicePixelRatio||1;
  if(!box.width||!box.height) return;
  canvas.width=Math.round(box.width*scale); canvas.height=Math.round(box.height*scale);
  const ctx=canvas.getContext('2d'); ctx.scale(scale,scale);
  canvas.setAttribute('aria-label',valid.length?`${row?.contract.symbol??'Instrument'}: ${valid.length} browser observations. Latest usable midpoint ${numberText(mark)}. Gaps are not interpolated.`:'No midpoint observations yet');
  if(!valid.length) return;
  const css=getComputedStyle(document.documentElement), width=box.width-60, height=box.height-30;
  const ys=valid.map(p=>p.value), lo=Math.min(...ys), hi=Math.max(...ys), padding=Math.max((hi-lo)*.12,Math.abs(hi)*.0001,1e-5);
  const min=lo-padding,max=hi+padding,t0=points[0].time,t1=Math.max(t0+1,points.at(-1).time);
  const x=p=>8+(p.time-t0)/(t1-t0)*width, y=p=>8+(max-p.value)/(max-min)*height;
  ctx.font='10px system-ui'; ctx.textAlign='left';
  for(let i=0;i<4;i++) { const yy=8+i/3*height; ctx.strokeStyle=css.getPropertyValue('--border'); ctx.beginPath();ctx.moveTo(0,yy);ctx.lineTo(width+10,yy);ctx.stroke();ctx.fillStyle=css.getPropertyValue('--muted');ctx.fillText(numberText(max-(max-min)*i/3),width+16,yy+3); }
  ctx.strokeStyle=css.getPropertyValue('--accent');ctx.lineWidth=1.8;ctx.beginPath();let pen=false;
  for(const p of points) { if(p.value===null) { pen=false;continue; } if(!pen) ctx.moveTo(x(p),y(p));else ctx.lineTo(x(p),y(p));pen=true; }
  ctx.stroke();ctx.fillStyle=css.getPropertyValue('--accent');for(const p of valid) {ctx.beginPath();ctx.arc(x(p),y(p),2,0,Math.PI*2);ctx.fill();}
}
function render() {
  pricingLab.setAccess(unlocked);
  greeksLab.setAccess(unlocked);
  storageLab.setAccess(unlocked);
  historyLab.setAccess(unlocked);historyLab.update(fresh?current?.broker.state:null);
  storageLab.update(fresh?current?.storage:null,fresh?current?.broker?.state:null);
  text('build-info',fresh?buildLabel(current?.build,current?.broker?.mode):'Build identity: unlock or refresh to inspect the running server.');
  const broker=fresh?current?.broker:null, state=broker?.state;
  text('session-badge',!unlocked?'LOCKED':!fresh?'DATA UNAVAILABLE':broker?.simulation?'SIMULATION':'READ ONLY');
  $('session-badge').className='badge'+(broker?.simulation?' warning':ready()?' positive':'');
  text('http-status',!unlocked?'Locked':fresh?'Available':busy?'Checking…':'Unavailable');
  text('http-detail',!unlocked?'Unlock to inspect the service':fresh?'Authenticated local API':'Do not treat cached values as current');
  text('broker-status',!unlocked?'Not inspected':broker?titleCase(state):'Unknown');
  text('broker-detail',broker?.simulation?'Offline mock; not IBKR paper trading':broker?.mode==='none'?'Broker disabled in server configuration':broker?.mode==='tws'?'TWS / Gateway · account mode not verified':'No connection opened by this page');
  text('subscription-count',fresh?String(current.subscriptions.length):'—');
  text('snapshot-status',fresh?titleCase(current.positions.status):'Unavailable');
  text('snapshot-detail',current?.positions.status==='complete'&&fresh?'Completed snapshot, not a stream':'Not an empty portfolio');
  const errorCode=broker?.errors?.at(-1)?.code;
  text('connection-note',!unlocked?'Unlock, then connect explicitly to your configured broker.':!fresh?'Service data is not current. Refresh to inspect the connection.':broker?.worker_failed?'Broker worker failed. Inspect the server before reconnecting.':state==='ready'?`Session ready for read-only requests${errorCode?` · last broker code ${errorCode}`:''}.`:state==='connecting'?'Waiting for the broker API handshake. No automatic reconnect.':broker?.mode==='none'?'Broker disabled. Set DTS_BROKER=tws and restart the server to use IBKR.':`Broker ${state}. Connect only to the intended gateway session.`);
  text('poll-badge',!unlocked?'NOT POLLING':document.hidden?'PAUSED':!fresh?'DATA UNAVAILABLE':'POLLING · 2s');
  $('unlock').disabled=busy; $('token').disabled=busy; $('forget').disabled=!unlocked;
  $('refresh').disabled=!unlocked||busy; $('connect').disabled=!unlocked||busy||!fresh||!broker?.enabled||['ready','connecting'].includes(state);
  $('disconnect').disabled=!unlocked||busy||!fresh||!broker?.enabled||state==='disconnected';
  $('contract-fields').disabled=!ready()||busy; $('resolve').disabled=Boolean(pendingResolution);
  $('snapshot').disabled=!ready()||busy||requestedPositions||current?.positions.status!=='unavailable';
  text('snapshot',current?.positions.status==='pending'?'Snapshot pending':requestedPositions?'Snapshot requested':'Request snapshot');
  updateCandidateButtons(); renderQuotes(); renderPositions(); renderChart();
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden) { clearTimeout(timer); fresh=false; render(); }
  else if(unlocked) refresh();
});
window.addEventListener('pagehide',()=>lock(''));
window.addEventListener('pageshow',event=>{ if(event.persisted) lock('Re-enter the token after restoring this page.'); });
new ResizeObserver(()=>renderChart()).observe($('chart').parentElement);
setInterval(()=>{ if(unlocked && !document.hidden) { renderQuotes(); renderChart(); } },1000);
render();
