// Read-only account monitoring. This contract never grants order permission.
const states=['unavailable','pending','complete','failed'];
const text=(x,max=256)=>typeof x==='string'&&x.length<=max&&!/[\u0000-\u001f]/.test(x);
const digits=x=>typeof x==='string'&&/^\d{1,20}$/.test(x);
const signedId=x=>typeof x==='string'&&/^-?\d{1,20}$/.test(x);
const decimal=x=>typeof x==='string'&&x.length<=64&&/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(x)&&Number.isFinite(Number(x));
const nullable=(x,check)=>x===null||check(x);
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const age=x=>finite(x)&&x>=0;
const fail=()=>{throw new Error('Incompatible trading status. Account and order data were not accepted.');};
export const componentLabels={positions:'Positions',account_summary:'Account summary',open_orders:'Open orders',executions:'Executions'};
export const monitorScopes={positions:'continuing_updates_after_initial_snapshot',account_summary:'ibkr_periodic_updates',open_orders:'account_wide_snapshot',executions:'request_scoped_broker_history'};
export function parseTradingStatus(p) {
  if(!p||p.schema_version!==1||p.kind!=='trading_status'||!['ibkr_tws','mock','none'].includes(p.source)
    ||p.synthetic!==(p.source==='mock')||p.order_execution_enabled!==false||p.account_mode!=='unverified'||p.strategy_state!=='not_configured'
    ||!['disconnected','connecting','ready','failed'].includes(p.broker?.state)||typeof p.broker.enabled!=='boolean'||!text(p.broker.generation,200)||!p.broker.generation.length
    ||!states.includes(p.accounts_status)||!Array.isArray(p.accounts)||p.accounts.length>1000||p.accounts.some(a=>!text(a,128)||!a.length)
    ||new Set(p.accounts).size!==p.accounts.length||!Array.isArray(p.blocking_reasons)||p.blocking_reasons.length>100||p.blocking_reasons.some(r=>!text(r,512)))fail();
  const m=p.monitor;
  if(!m||!['unavailable','pending','active','failed','stopped'].includes(m.state)||typeof m.start_available!=='boolean'||typeof m.restart_requires_reconnect!=='boolean'
    ||!nullable(m.request_id,digits)||!nullable(m.account,a=>text(a,128)&&a.length>0)
    ||!nullable(m.started_at_unix_ms,digits)||!nullable(m.last_update_unix_ms,digits)||!nullable(m.error,e=>text(e,1024)))fail();
  if((m.receipt_time_basis!==undefined&&m.receipt_time_basis!=='local_wall_time_and_monotonic_age_not_exchange_time')
    ||(m.snapshot_not_execution_risk_state!==undefined&&m.snapshot_not_execution_risk_state!==true)
    ||(m.start_available&&(p.source!=='ibkr_tws'||p.broker.state!=='ready'||p.accounts_status!=='complete')))fail();
  for(const key of Object.keys(componentLabels))if(!states.includes(m.components?.[key])||m.scopes?.[key]!==monitorScopes[key])fail();
  for(const key of ['started_age_seconds','last_update_age_seconds'])if(m[key]!==undefined&&!nullable(m[key],age))fail();
  if(m.components_meta!==undefined)for(const key of Object.keys(componentLabels)) {
    const meta=m.components_meta[key];if(!meta||!nullable(meta.completed_age_seconds,age)||!nullable(meta.last_update_age_seconds,age)
      ||!nullable(meta.error_code,x=>Number.isSafeInteger(x)))fail();
  }
  for(const key of ['positions','account_values','open_orders','executions'])if(!Array.isArray(m[key])||m[key].length>10000)fail();
  for(const [component,rows] of Object.entries({positions:'positions',account_summary:'account_values',open_orders:'open_orders',executions:'executions'}))
    if(m.components[component]!=='complete'&&m[rows].length)fail();
  for(const r of m.positions)if(!r||!digits(r.contract_id)||!text(r.symbol,128)||!text(r.currency,32)||!text(r.security_type,32)||!decimal(r.quantity)||!nullable(r.average_cost,finite)
    ||(r.model_code!==undefined&&!text(r.model_code,128)))fail();
  for(const r of m.account_values)if(!r||!text(r.tag,128)||!text(r.value,1024)||!text(r.currency,32))fail();
  for(const r of m.open_orders)if(!r||!signedId(r.order_id)||!digits(r.perm_id)||!signedId(r.client_id)||!digits(r.contract_id)||!text(r.symbol,128)
    ||!text(r.action,32)||!text(r.order_type,32)||!decimal(r.quantity)||!nullable(r.limit_price,x=>typeof x==='number'&&Number.isFinite(x))
    ||!text(r.status,128)||!nullable(r.filled,decimal)||!nullable(r.remaining,decimal)||!text(r.order_ref,256))fail();
  const executionIds=new Set();
  for(const r of m.executions) {
    if(!r||!text(r.execution_id,256)||!r.execution_id.length||executionIds.has(r.execution_id)||!signedId(r.order_id)||!digits(r.perm_id)||!digits(r.contract_id)
      ||!text(r.symbol,128)||!text(r.side,32)||!decimal(r.quantity)||!nullable(r.price,finite)||!text(r.time,128))fail();
    executionIds.add(r.execution_id);
  }
  if(['pending','active'].includes(m.state)&&(!m.account||!m.request_id))fail();
  return p;
}
export function timestampText(value) {
  if(value===null||value===undefined)return 'Unavailable';
  const stamp=Number(value);return Number.isSafeInteger(stamp)&&stamp>=0&&stamp<=8640000000000000?new Date(stamp).toLocaleString():'Unavailable';
}
export function componentText(status,count,name) {
  if(status==='complete')return `${name}: initial snapshot complete · ${count} returned row${count===1?'':'s'}`;
  if(status==='pending')return `${name}: initial snapshot incomplete; rows withheld until completion`;
  if(status==='failed')return `${name}: failed · completeness unavailable`;
  return `${name}: unavailable`;
}
export function ageText(value,elapsed=0) {return age(value)?`${(value+Math.max(0,elapsed)).toFixed(1)}s ago`:'Unavailable';}
export function maskedAccount(value) {return typeof value==='string'&&value.length?`••••${[...value].slice(-4).join('')}`:'Unavailable';}

// Explicit commands and reads; the view decides whether to poll. Epoch checks
// prevent sign-out, reconnect and superseded requests from restoring old data.
export function createTradingController(api,onChange=()=>{},onAccessError=()=>{}) {
  let epoch=0,access=false,contextKey=null,contextReady=false;
  let data=null,selected='',busy=false,loaded=false,uncertain=false,message='Load status to inspect available accounts.';
  const state=()=>({access,data,selected,busy,loaded,uncertain,message,contextReady});
  const emit=()=>onChange(state());
  function clear(note) {epoch++;data=null;selected='';busy=false;loaded=false;uncertain=false;message=note;}
  const current=version=>access&&version===epoch;
  function error(error,mutation=false) {
    if([401,403].includes(error.status)){clear('Local access rejected.');onAccessError(error);return;}
    if(mutation&&(!error.status||error.status>=500)){uncertain=true;data=null;message='Monitoring request outcome uncertain. Load / refresh status to reconcile before another action.';}
    else if(mutation)message=`Monitoring request rejected: ${error.message}. Load / refresh status before trying again.`;
    else {data=null;message=error.message||'Trading status unavailable. Load / refresh status to retry.';}
  }
  async function read(version) {
    const response=await api.request('/api/trading/status');
    if(!current(version))return false;
    const next=parseTradingStatus(response),changed=data&&data.broker.generation!==next.broker.generation;
    if(contextKey!==null&&next.broker.generation!==contextKey){clear('Broker generation changed. Refresh the broker view, then load trading status again.');emit();return false;}
    if(changed||!next.accounts.includes(selected))selected='';
    data=next;loaded=true;uncertain=false;
    message=changed?'Broker generation changed. Select the intended account again.':'Status refreshed. Order execution remains disabled.';
    return true;
  }
  async function run(action,mutation=false) {
    if(!access||busy)return false;
    const version=epoch;busy=true;emit();
    try {await action(version);return current(version);}
    catch(e){if(current(version))error(e,mutation);return false;}
    finally {if(current(version)){busy=false;emit();}}
  }
  return {
    state,
    setAccess(value){if(access===value)return;access=value;clear(value?'Load status to inspect available accounts.':'Unlock local access to inspect trading status.');emit();},
    updateContext(value){
      const key=value?.session_id??null,ready=value?.broker?.state==='ready';
      if((key!==null&&contextKey!==null&&key!==contextKey)||(key===null&&(data||contextReady))||(contextReady&&!ready))clear('Broker/server status changed or became unavailable. Load status again before monitoring.');
      if(key!==null)contextKey=key;contextReady=ready;emit();
    },
    selectAccount(value){selected=data?.accounts_status==='complete'&&data.accounts.includes(value)?value:'';emit();},
    refresh:()=>run(read),
    start(){
      if(!access||!contextReady||!data||uncertain||data.source!=='ibkr_tws'||data.broker.state!=='ready'||data.accounts_status!=='complete'||!data.accounts.includes(selected)||!data.monitor.start_available)return Promise.resolve(false);
      const account=selected;return run(async version=>{await api.request('/api/trading/monitor','POST',{account});if(current(version))await read(version);},true);
    },
    stop(){
      if(!access||!data||uncertain||!['pending','active','failed'].includes(data.monitor.state))return Promise.resolve(false);
      return run(async version=>{await api.request('/api/trading/stop','POST',{});if(current(version))await read(version);},true);
    },
  };
}
