import {createTradingController,componentLabels,componentText,timestampText,ageText,maskedAccount} from './trading-model.mjs';
import {numberText,titleCase} from './model.mjs';

export function mountTrading(api,onAccessError) {
  const $=id=>document.getElementById(id),put=(id,value)=>{$(id).textContent=value;};
  let visible=document.body.dataset.workspace==='trading',timer=null,receivedAt=0,lastData=null;
  const controller=createTradingController(api,render,onAccessError);
  const make=(tag,value)=>{const el=document.createElement(tag);el.textContent=String(value??'Unavailable');return el;};
  function rows(id,columns,values,status) {
    const body=$(id);
    if(status!=='complete'||!values.length) {
      const row=make('tr',''),cell=make('td',status==='complete'?'Completed snapshot returned no rows.':status==='pending'?'Initial snapshot incomplete; rows withheld until completion.':status==='failed'?'Snapshot failed; completeness unavailable.':'Unavailable; no completed snapshot.');
      cell.colSpan=columns;row.append(cell);body.replaceChildren(row);return;
    }
    body.replaceChildren(...values.slice(0,100).map(values=>{const row=make('tr','');for(const value of values)row.append(make('td',value));return row;}));
  }
  function schedule() {
    clearTimeout(timer);timer=null;
    const s=controller.state();
    if(visible&&!document.hidden&&s.access&&s.loaded&&!s.uncertain&&!s.busy&&s.data?.broker.enabled)
      timer=setTimeout(()=>{timer=null;controller.refresh();},1000);
  }
  function render(s=controller.state()) {
    const p=s.data,m=p?.monitor;
    if(p!==lastData){receivedAt=performance.now();lastData=p;}
    const elapsed=Math.max(0,(performance.now()-receivedAt)/1000);
    put('trading-note',s.message);
    put('trading-source',!p?'NOT LOADED':p.synthetic?'SYNTHETIC MOCK':p.source==='ibkr_tws'?'IBKR ACCOUNT STATUS':'BROKER DISABLED');
    $('trading-source').className='badge'+(p?.synthetic?' warning':'');
    put('trading-mode','Account mode unverified');
    put('trading-state',m?titleCase(m.state):'Unavailable');
    put('trading-identity',m?.account?`Monitoring account ${maskedAccount(m.account)} · request ${m.request_id??'Unavailable'}`:'No account monitor loaded.');
    put('trading-freshness',m?`Last monitor update: ${ageText(m.last_update_age_seconds,elapsed)} · local receipt, not exchange time. Started: ${ageText(m.started_age_seconds,elapsed)}.`:'Update age unavailable. Local receipt time does not certify market freshness.');
    put('trading-wall-time',m?`Reported receipt: ${timestampText(m.last_update_unix_ms)}. Snapshot ages below are independent of continuing position updates.`:'');
    put('trading-account-note',!p?'Load status to inspect the authoritative managed-account list.':p.accounts_status==='complete'?`${p.accounts.length} managed account${p.accounts.length===1?'':'s'} returned. Select the intended exact account explicitly.`:`Managed accounts ${p.accounts_status}; account selection unavailable.`);
    const select=$('trading-account'),signature=JSON.stringify(p?.accounts_status==='complete'?p.accounts:[]);
    if(select.dataset.accounts!==signature){select.dataset.accounts=signature;const blank=make('option','Select an account');blank.value='';select.replaceChildren(blank,...(p?.accounts_status==='complete'?p.accounts:[]).map(account=>{const option=make('option',account);option.value=account;return option;}));}
    select.value=s.selected;select.disabled=!s.access||s.busy||p?.accounts_status!=='complete'||!m?.start_available;
    $('trading-refresh').disabled=!s.access||s.busy;
    $('trading-start').disabled=!s.access||s.busy||s.uncertain||!s.contextReady||!s.selected||!m?.start_available;
    $('trading-stop').disabled=!s.access||s.busy||s.uncertain||!['pending','active','failed'].includes(m?.state);
    put('trading-restart',m?.restart_requires_reconnect?'Monitoring has used this connection. Reconnect the broker explicitly before starting another monitor or refreshing broker snapshots.':m?.start_available?'One account monitor may be started per broker connection.':'Monitoring start unavailable on this connection.');
    put('trading-error',m?.error??'');$('trading-error').hidden=!m?.error;
    const counts={positions:m?.positions.length??0,account_summary:m?.account_values.length??0,open_orders:m?.open_orders.length??0,executions:m?.executions.length??0};
    for(const [key,label] of Object.entries(componentLabels)) {
      const meta=m?.components_meta?.[key],status=m?.components[key]??'unavailable';
      put(`trading-${key}-status`,componentText(status,counts[key],label)+(counts[key]>100?' · showing first 100':'')
        +` · snapshot completion: ${ageText(meta?.completed_age_seconds,elapsed)} · last component update: ${ageText(meta?.last_update_age_seconds,elapsed)}`
        +(meta?.error_code!==null&&meta?.error_code!==undefined?` · broker error ${meta.error_code}`:''));
    }
    rows('trading-positions-body',7,m?.positions.map(r=>[`${r.symbol} / ${r.contract_id}`,r.security_type,r.model_code||'Account total / no model reported',r.quantity,numberText(r.average_cost),r.currency,'Reported broker position'])??[],m?.components.positions);
    rows('trading-values-body',3,m?.account_values.map(r=>[r.tag,r.value,r.currency])??[],m?.components.account_summary);
    rows('trading-orders-body',10,m?.open_orders.map(r=>[`${r.symbol} / ${r.contract_id}`,r.order_id,r.perm_id,r.action,r.order_type,r.quantity,numberText(r.limit_price),r.status,r.filled??'Unavailable',r.remaining??'Unavailable'])??[],m?.components.open_orders);
    rows('trading-executions-body',7,m?.executions.map(r=>[r.execution_id,`${r.symbol} / ${r.contract_id}`,r.order_id,r.side,r.quantity,numberText(r.price),r.time||'Unavailable'])??[],m?.components.executions);
    $('trading-blockers').replaceChildren(...(p?.blocking_reasons??['Status not loaded; execution remains disabled.']).map(reason=>make('li',reason.replaceAll('_',' '))));
    put('trading-poll',!s.access?'LOCKED':s.uncertain?'RECONCILIATION REQUIRED':s.busy?'CHECKING':!visible||document.hidden?'DISPLAY PAUSED':s.loaded&&p?.broker.enabled?'STATUS POLLS · 1s':'EXPLICIT LOAD REQUIRED');
    schedule();
  }
  $('trading-refresh').addEventListener('click',()=>controller.refresh());
  $('trading-account').addEventListener('change',event=>controller.selectAccount(event.target.value));
  $('trading-start').addEventListener('click',()=>controller.start());
  $('trading-stop').addEventListener('click',()=>controller.stop());
  window.addEventListener('dts:workspacechange',event=>{visible=event.detail?.workspace==='trading';render();});
  document.addEventListener('visibilitychange',()=>render());
  render();
  return {setAccess:controller.setAccess,update:controller.updateContext};
}
