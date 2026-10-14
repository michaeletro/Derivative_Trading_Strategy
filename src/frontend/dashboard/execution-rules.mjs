import {createRulePlannerController,dollarsToCents,wholeNumber,moneyText,reasonText} from './execution-rules-model.mjs';

export function mountExecutionRules(api,onAccessError) {
  const $=id=>document.getElementById(id),form=$('rules-form'),select=$('rules-profile');
  const controller=createRulePlannerController(api,render,onAccessError);
  let displayedPresets=null,displayedSelection='';
  const make=(tag,text)=>{const element=document.createElement(tag);element.textContent=String(text);return element;};
  const put=(id,text)=>{$(id).textContent=text;};
  function defaults(profile) {
    form.elements.available_cash.value=(profile.budget_cents/100).toFixed(2);
    for(const key of ['committed_gross_exposure','available_long_shares','day_pnl','buy_decisions_today','open_orders','quote_age_ms','risk_age_ms'])form.elements[key].value='0';
    for(const key of ['instrument_allowed','realtime','rth','risk_state_known'])form.elements[key].checked=false;
  }
  function render(s=controller.state()) {
    const p=s.presets?.profiles.find(p=>p.id===s.selected);
    if(s.presets!==displayedPresets) {
      displayedPresets=s.presets;select.replaceChildren(...(s.presets?.profiles.map(p=>{const option=make('option',p.label);option.value=p.id;return option;})??[make('option','Load rules first')]));
    }
    select.value=s.selected;
    if(p&&displayedSelection!==s.selected){defaults(p);displayedSelection=s.selected;}
    if(!p)displayedSelection='';
    $('rules-load').disabled=!s.access||s.busy;select.disabled=!s.access||s.busy||!p;
    $('rules-inputs').disabled=!s.access||s.busy||!p;
    $('rules-reset').disabled=!s.access||s.busy||!p;
    put('rules-note',s.message);
    put('rules-profile-basis',p?`Declared balance: ${moneyText(p.declared_balance_cents)} · scenario budget: ${moneyText(p.budget_cents)}. Selecting this preset does not verify paper/live account mode.`:'Preset balances are planning assumptions supplied by the owner. No broker account is selected by this planner.');
    const limits=p?[
      ['Per-order notional cap',moneyText(p.max_order_notional_cents)],['Cash reserve',moneyText(p.cash_reserve_cents)],
      ['Gross exposure cap',moneyText(p.max_gross_exposure_cents)],['Day-loss gate',moneyText(p.daily_loss_limit_cents)],
      ['Fee allowance per decision',moneyText(p.fee_reserve_cents)],['Daily buy decisions',p.max_buy_decisions_per_day],
      ['Concurrent open orders',p.max_open_orders],['Quote / risk age limits',`${p.max_quote_age_ms} / ${p.max_risk_age_ms} ms`],['Maximum spread',`${p.max_spread_bps} bps`],
    ]:[];
    $('rules-limits').replaceChildren(...limits.map(([name,value])=>{const card=make('article','');card.append(make('span',name),make('strong',value));return card;}));
    $('rules-limitations').replaceChildren(...(s.presets?.limitations??[]).map(value=>make('li',value)));
    const result=s.result;$('rules-result').hidden=!result;
    if(result) {
      const passed=result.decision==='pass';
      put('rules-decision',passed?'Within the supplied scenario limits':'Scenario blocked');
      $('rules-decision').className=passed?'rules-outcome':'rules-outcome warning';
      const basis=result.side==='SELL'?'gross proceeds before fees':'including fee allowance';
      put('rules-sizing',`${result.quantity_shares} whole share${result.quantity_shares===1?'':'s'} · limit notional ${moneyText(result.order_notional_cents)} · ${basis} ${moneyText(result.estimated_total_cents)}.`);
      $('rules-reasons').replaceChildren(...result.blocking_reasons.map(reason=>make('li',reasonText(reason))));
      $('rules-result-limitations').replaceChildren(...result.limitations.map(value=>make('li',value)));
    } else {
      put('rules-decision','No current calculation');put('rules-sizing','');$('rules-reasons').replaceChildren();$('rules-result-limitations').replaceChildren();
    }
  }
  function input() {
    const f=form.elements,money=name=>dollarsToCents(f[name].value),count=(name,label,positive=false)=>wholeNumber(f[name].value,label,{positive});
    const contract=f.contract_id.value.trim(),symbol=f.symbol.value.trim().toUpperCase();
    if(!/^[1-9]\d{0,17}$/.test(contract))throw new Error('Enter the positive conId from an explicitly resolved contract.');
    if(!symbol||symbol.length>32||/[\u0000-\u001f]/.test(symbol))throw new Error('Enter a valid resolved stock/ETF symbol.');
    return {side:f.side.value,contract_id:contract,symbol,security_type:'STK',currency:'USD',instrument_allowed:f.instrument_allowed.checked,
      bid_cents:money('bid'),ask_cents:money('ask'),limit_price_cents:money('limit_price'),min_tick_cents:money('min_tick'),
      requested_shares:count('requested_shares','Maximum shares',true),available_cash_cents:money('available_cash'),
      committed_gross_exposure_cents:money('committed_gross_exposure'),available_long_shares:count('available_long_shares','Available long shares'),
      day_pnl_cents:dollarsToCents(f.day_pnl.value,{signed:true}),buy_decisions_today:count('buy_decisions_today','Buy decisions'),
      open_orders:count('open_orders','Open orders'),quote_age_ms:count('quote_age_ms','Quote age'),risk_age_ms:count('risk_age_ms','Risk-state age'),
      realtime:f.realtime.checked,rth:f.rth.checked,risk_state_known:f.risk_state_known.checked};
  }
  $('rules-load').addEventListener('click',()=>controller.load());
  select.addEventListener('change',()=>controller.selectProfile(select.value));
  $('rules-reset').addEventListener('click',()=>{const p=controller.state().presets?.profiles.find(p=>p.id===controller.state().selected);if(p){defaults(p);controller.invalidate();}});
  form.addEventListener('input',()=>controller.invalidate());
  form.addEventListener('change',()=>controller.invalidate());
  form.addEventListener('submit',event=>{event.preventDefault();try{controller.preview(input());}catch(error){controller.invalidate();put('rules-note',error.message);}});
  render();
  return {setAccess:controller.setAccess};
}
