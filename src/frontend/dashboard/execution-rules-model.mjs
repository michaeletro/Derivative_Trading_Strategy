// Scenario calculations never establish account identity or order permission.
const safeInteger=x=>Number.isSafeInteger(x);
const nonnegative=x=>safeInteger(x)&&x>=0;
const positive=x=>safeInteger(x)&&x>0;
const text=(x,max=512)=>typeof x==='string'&&x.length<=max&&!/[\u0000-\u001f]/.test(x);
const id=x=>text(x,64)&&/^[a-z][a-z0-9_]*$/.test(x);
const messages=x=>Array.isArray(x)&&x.length<=100&&x.every(v=>text(v,1024));
const fail=()=>{throw new Error('Incompatible rule-planner response. No scenario result was accepted.');};
export const policyMoneyFields=['declared_balance_cents','budget_cents','cash_reserve_cents','max_order_notional_cents','max_gross_exposure_cents','daily_loss_limit_cents','fee_reserve_cents'];
export function parseRulePresets(p) {
  if(!p||p.schema_version!==1||p.kind!=='execution_rule_presets'||p.execution_enabled!==false||p.scenario_only!==true
    ||!Array.isArray(p.profiles)||!p.profiles.length||p.profiles.length>32||!messages(p.limitations))fail();
  const ids=new Set();
  for(const r of p.profiles) {
    if(!r||!id(r.id)||ids.has(r.id)||!text(r.label,256)||!r.label.length||policyMoneyFields.some(k=>!nonnegative(r[k]))
      ||!positive(r.budget_cents)||!positive(r.max_order_notional_cents)||!positive(r.max_gross_exposure_cents)||!positive(r.daily_loss_limit_cents)
      ||!positive(r.max_buy_decisions_per_day)||!positive(r.max_open_orders)||!positive(r.max_quote_age_ms)||!positive(r.max_risk_age_ms)
      ||!positive(r.max_spread_bps)||r.max_spread_bps>1000||r.max_quote_age_ms>60000||r.max_risk_age_ms>60000
      ||r.cash_reserve_cents>=r.budget_cents||r.budget_cents>r.declared_balance_cents||r.max_gross_exposure_cents>r.budget_cents
      ||r.max_order_notional_cents>r.max_gross_exposure_cents||r.max_gross_exposure_cents>r.budget_cents-r.cash_reserve_cents
      ||r.fee_reserve_cents>r.max_order_notional_cents||r.daily_loss_limit_cents>r.budget_cents)fail();
    ids.add(r.id);
  }
  return p;
}
export function parseRulePreview(p,request) {
  if(!p||p.schema_version!==1||p.kind!=='execution_rule_preview'||p.execution_enabled!==false||p.scenario_only!==true
    ||p.profile_id!==request.profile_id||!['pass','blocked'].includes(p.decision)||!nonnegative(p.quantity_shares)
    ||p.quantity_shares>request.requested_shares||!nonnegative(p.order_notional_cents)||!nonnegative(p.estimated_total_cents)
    ||!messages(p.blocking_reasons)||!messages(p.limitations)
    ||(p.decision==='blocked'&&(p.quantity_shares!==0||p.order_notional_cents!==0||p.estimated_total_cents!==0||!p.blocking_reasons.length))
    ||(p.decision==='pass'&&(p.quantity_shares===0||p.blocking_reasons.length!==0))
    ||p.order_notional_cents!==p.quantity_shares*request.limit_price_cents||p.estimated_total_cents<p.order_notional_cents)fail();
  return p;
}
export function dollarsToCents(value,{signed=false}={}) {
  const input=String(value).trim(),pattern=signed?/^-?\d+(?:\.\d{1,2})?$/:/^\d+(?:\.\d{1,2})?$/;
  if(!pattern.test(input))throw new Error('Use a dollar amount with at most two decimal places.');
  const negative=input.startsWith('-'),[whole,fraction='']=input.replace(/^-/, '').split('.');
  const cents=BigInt(whole)*100n+BigInt(fraction.padEnd(2,'0'));
  if(cents>BigInt(Number.MAX_SAFE_INTEGER))throw new Error('Dollar amount is too large.');
  return Number(negative?-cents:cents);
}
export function wholeNumber(value,label,{positive:required=false}={}) {
  if(!/^\d+$/.test(String(value).trim()))throw new Error(`${label} must be a whole number.`);
  const n=Number(value);if(!nonnegative(n)||(required&&n===0))throw new Error(`${label} is outside the supported range.`);return n;
}
export function moneyText(cents) {return nonnegative(cents)||safeInteger(cents)?new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',maximumFractionDigits:2}).format(cents/100):'Unavailable';}
export const reasonLabels={
  instrument_not_qualified:'Instrument is not declared eligible for this scenario.',
  unsupported_instrument:'Only USD stock/ETF scenarios are supported.',
  invalid_quote:'The scenario bid and ask must be positive and uncrossed.',
  invalid_limit_tick:'The scenario limit price must match the supplied minimum tick.',
  invalid_requested_quantity:'The share ceiling must be a positive supported whole number.',
  invalid_risk_inputs:'The supplied risk-state inputs are outside the supported range.',
  quote_not_realtime:'The scenario quote is not declared real time.',
  outside_regular_session:'The scenario is outside regular trading hours.',
  risk_state_unknown:'Cash, positions and outstanding orders are not declared known.',
  quote_stale:'The scenario quote exceeds the quote-age limit.',
  risk_state_stale:'The scenario risk state exceeds the risk-age limit.',
  spread_limit:'The scenario spread exceeds the preset limit.',
  limit_price_outside_quote_collar:'The scenario limit price exceeds the allowed quote collar.',
  daily_loss_limit:'The scenario day loss reaches the preset limit.',
  daily_buy_limit:'The scenario reaches the daily buy-decision limit.',
  open_order_limit:'The scenario reaches the open-order limit.',
  whole_share_exceeds_order_cap:'No whole share fits the scenario per-order notional cap.',
  insufficient_gross_capacity:'The scenario has no gross-exposure capacity for a whole share.',
  insufficient_reserved_cash:'No whole share fits the scenario cash limit after the cash and fee reserves.',
  no_available_long_shares:'The scenario has no available long shares to sell.',
  notional_overflow:'The scenario notional exceeds the supported calculation range.',
  total_overflow:'The scenario total including fees exceeds the supported calculation range.',
};
export const reasonText=value=>reasonLabels[value]??value.replaceAll('_',' ');

export function createRulePlannerController(api,onChange=()=>{},onAccessError=()=>{}) {
  let access=false,epoch=0,requestId=0,busy=false,presets=null,selected='',result=null;
  let message='Unlock local access, then load provisional rules.';
  const state=()=>({access,busy,presets,selected,result,message});
  const emit=()=>onChange(state());
  const current=(version,request)=>access&&version===epoch&&request===requestId;
  function clear(note) {epoch++;requestId++;busy=false;presets=null;selected='';result=null;message=note;}
  function error(e,kind) {
    result=null;
    if([401,403].includes(e.status)){clear('Local access rejected.');onAccessError(e);}
    else {
      if(kind==='load'){presets=null;selected='';}
      message=kind==='preview'&&(!e.status||e.status>=500)?'Scenario calculation unavailable. No order was sent. Retry this preview explicitly.':e.message||'Rule planner unavailable. Retry explicitly when ready.';
    }
  }
  async function run(action,kind) {
    if(!access||busy)return false;
    const version=epoch,request=++requestId;busy=true;result=null;emit();
    try {await action(version,request);return current(version,request);}
    catch(e){if(current(version,request))error(e,kind);return false;}
    finally {if(access&&request===requestId){busy=false;emit();}}
  }
  return {
    state,
    setAccess(value){if(access===value)return;access=value;clear(value?'Load provisional rules to calculate a hypothetical scenario.':'Unlock local access, then load provisional rules.');emit();},
    selectProfile(value){if(busy)return;selected=presets?.profiles.some(p=>p.id===value)?value:'';epoch++;result=null;message='Preset selected. Enter a hypothetical scenario and preview it explicitly.';emit();},
    invalidate(){epoch++;result=null;message='Scenario changed. Preview again to calculate the new inputs.';emit();},
    load(){return run(async(version,request)=>{
      const response=await api.request('/api/trading/rules');if(!current(version,request))return;
      presets=parseRulePresets(response);selected=presets.profiles.some(p=>p.id===selected)?selected:presets.profiles[0].id;
      message='Provisional rules loaded. No account values were imported; execution remains disabled.';
    },'load');},
    preview(input){
      if(!presets||!selected)return Promise.resolve(false);
      const body={...input,profile_id:selected};
      return run(async(version,request)=>{
        const response=await api.request('/api/trading/rules/preview','POST',body);if(!current(version,request))return;
        result={...parseRulePreview(response,body),side:body.side};message='Scenario calculated. This result does not transmit or authorize an order.';
      },'preview');
    },
  };
}
