import {mountAnalytics, cumulativeDepth} from './analytics.mjs';
import {mountPositions, mountOrders, validateTuning, kellySensitivity} from './positions.mjs';
// Credentials, account checks, risk and order submission stay in the local service.
// This page only requests an order after a separate, explicit confirmation.
export const POLL_MS = 2000;
export const MAX_QUOTE_AGE_MS = 5000;
const PATHS = new Set(['/api/session', '/api/state', '/api/preview', '/api/execute']);
const ASSET_SERIES = Object.freeze({BTC: 'KXBTC15M', ETH: 'KXETH15M', SOL: 'KXSOL15M', XRP: 'KXXRP15M'});
const assetSymbol = value => typeof value === 'string' && Object.hasOwn(ASSET_SERIES, value.replace(/-USD$/, ''))
  ? value.replace(/-USD$/, '') : fail('Choose BTC, ETH, SOL or XRP.');
const tickerAsset = value => Object.keys(ASSET_SERIES).find(asset => typeof value === 'string' && value.startsWith(ASSET_SERIES[asset] + '-'));
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
function text(value, name, limit = 300) {
  if (typeof value !== 'string' || !value.length || value.length > limit || /[\u0000-\u001f\u007f]/u.test(value)) fail(`Invalid ${name}.`);
  return value;
}
function decimal(value, name, {positive = false, max = 1e9, digits = 4} = {}) {
  if (typeof value !== 'string' || !new RegExp(`^(0|[1-9][0-9]{0,9})(\\.[0-9]{1,${digits}})?$`).test(value)) fail(`Invalid ${name}.`);
  const number = Number(value);
  if (!Number.isFinite(number) || number > max || (positive && number <= 0)) fail(`Invalid ${name}.`);
  return value;
}
function stamp(value, name) {
  text(value, name, 40);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) fail(`Invalid ${name}.`);
  return value;
}
function mode(value) {
  if (!['paper', 'demo', 'live'].includes(value.mode) || !['demo', 'production'].includes(value.environment)
      || (value.mode === 'demo' && value.environment !== 'demo') || (value.mode === 'live' && value.environment !== 'production')) fail('Invalid service mode.');
}
const nullableDecimal = (value, name, options) => value === null ? null : decimal(value, name, options);
export function validateState(raw, expectedAsset = null) {
  if (!object(raw) || raw.kind !== 'kalshi_live_state' || raw.version !== 1) fail('Unsupported live state.');
  mode(raw);
  const selectedAsset = raw.selected_asset === undefined
    ? tickerAsset(raw.market?.ticker) || (expectedAsset === null ? 'BTC' : assetSymbol(expectedAsset))
    : assetSymbol(raw.selected_asset);
  if (expectedAsset !== null && selectedAsset !== assetSymbol(expectedAsset)) fail('Quote belongs to a different selected asset.');
  if (raw.manual_order_supported !== undefined && (typeof raw.manual_order_supported !== 'boolean'
      || selectedAsset !== 'BTC' && raw.manual_order_supported)) fail('Invalid manual order capability for this asset.');
  const manualOrderSupported = selectedAsset === 'BTC' && raw.manual_order_supported !== false;
  if (typeof raw.orders_enabled !== 'boolean' || (raw.mode === 'paper' && raw.orders_enabled)
      || ![true, false, null].includes(raw.authenticated) || raw.access_token_required !== true
      || !['ready', 'unavailable', 'loading'].includes(raw.market_status)) fail('Invalid live capabilities.');
  if (!object(raw.limits) || !object(raw.journal)) fail('Missing live limits.');
  const limits = {};
  for (const key of ['bankroll', 'max_order_cost', 'max_total_cost', 'max_market_cost']) limits[key] = decimal(raw.limits[key], key, {positive: true, max: 1e6});
  limits.kelly_fraction = decimal(raw.limits.kelly_fraction, 'Kelly multiplier', {positive: true, max: 1, digits: 6});
  limits.min_edge = decimal(raw.limits.min_edge, 'minimum edge', {positive: true, max: 1});
  if (raw.limits.fee_reserve_per_contract !== undefined) limits.fee_reserve_per_contract = decimal(raw.limits.fee_reserve_per_contract, 'startup fee allowance', {positive: true, max: 1});
  if (!Number.isSafeInteger(raw.limits.max_contracts) || raw.limits.max_contracts < 1 || raw.limits.max_contracts > 10000
      || Number(limits.max_order_cost) > Number(limits.max_total_cost) || Number(limits.max_market_cost) > Number(limits.max_total_cost)) fail('Invalid live risk bounds.');
  limits.max_contracts = raw.limits.max_contracts;
  limits.min_seconds_to_close = raw.limits.min_seconds_to_close ?? 60;
  if (typeof limits.min_seconds_to_close !== 'number' || !Number.isFinite(limits.min_seconds_to_close)
      || limits.min_seconds_to_close <= 0 || limits.min_seconds_to_close > 86400) fail('Invalid entry cutoff.');
  if (typeof raw.journal.halted !== 'boolean' || !Number.isSafeInteger(raw.journal.intent_count) || raw.journal.intent_count < 0 || raw.journal.intent_count > 2000) fail('Invalid journal status.');
  const journal = {committed_cost: decimal(raw.journal.committed_cost, 'committed cost'), halted: raw.journal.halted, intent_count: raw.journal.intent_count};
  if (raw.journal.open_limit_count !== undefined) {
    if (!Number.isSafeInteger(raw.journal.open_limit_count) || raw.journal.open_limit_count < 0 || raw.journal.open_limit_count > 2000) fail('Invalid outstanding limit count.');
    journal.open_limit_count = raw.journal.open_limit_count;
  }
  let market = null;
  if (raw.market_status === 'ready') {
    if (!object(raw.market) || raw.fetched_at === null) fail('Incomplete live quote.');
    const value = raw.market;
    const ticker = text(value.ticker, 'market ticker', 128), event = text(value.event_ticker, 'event ticker', 128);
    const series = ASSET_SERIES[selectedAsset];
    if (!new RegExp(`^${series}-[A-Za-z0-9_.:-]+$`).test(ticker) || !new RegExp(`^${series}-[A-Za-z0-9_.:-]+$`).test(event)
        || !ticker.startsWith(event + '-')) fail('Quote belongs to a different crypto series.');
    if (typeof value.quote_id !== 'string' || !/^[0-9a-f]{64}$/.test(value.quote_id)) fail('Missing contract quote identity.');
    market = {ticker, event_ticker: event, quote_id: value.quote_id, target_price: decimal(value.target_price, 'target price', {positive: true, max: 1e8, digits: 8}),
      open_time: stamp(value.open_time, 'open time'), close_time: stamp(value.close_time, 'close time'),
      fee_reserve: decimal(value.fee_reserve, 'fee reserve', {positive: true, max: 1}),
      fee_type: text(value.fee_type, 'fee type', 80), fee_multiplier: decimal(value.fee_multiplier, 'fee multiplier', {max: 1000, digits: 8})};
    if (value.fee_reserve_lower_bound !== undefined) market.fee_reserve_lower_bound = decimal(value.fee_reserve_lower_bound, 'verified fee allowance bound', {positive: true, max: 1});
    if (value.settlement_source !== undefined) {
      market.settlement_source = text(value.settlement_source, 'settlement source', 100);
      const expectedSource = selectedAsset === 'BTC' ? 'CF_BRTI' : `CF_${selectedAsset}USD_RTI`;
      if (market.settlement_source !== expectedSource) fail('Quote settlement source belongs to a different asset.');
    }
    for (const key of ['yes_bid', 'yes_ask', 'no_bid', 'no_ask']) {
      market[key] = nullableDecimal(value[key], key, {positive: true, max: 1});
      if (market[key] !== null && Number(market[key]) >= 1) fail('Invalid binary quote.');
    }
    for (const key of ['yes_available', 'no_available']) market[key] = nullableDecimal(value[key], key, {positive: true, digits: 2});
    if (Math.abs(Date.parse(market.close_time) - Date.parse(market.open_time) - 900000) > 1) fail('Invalid crypto window.');
    if ((market.yes_bid !== null && market.yes_ask !== null && Number(market.yes_bid) >= Number(market.yes_ask))
        || (market.no_bid !== null && market.no_ask !== null && Number(market.no_bid) >= Number(market.no_ask))) fail('Crossed crypto quote.');
  } else if (raw.market !== null) fail('Unavailable data cannot retain a live quote.');
  if (market && raw.market.orderbook !== undefined) {
    const depth = cumulativeDepth(raw.market.orderbook);
    if (!depth.bids.length || !depth.asks.length || Math.max(...depth.bids.map(row => row[0])) >= Math.min(...depth.asks.map(row => row[0]))) fail('Invalid full book.');
    if (depth.bids[0][0] !== Number(market.yes_bid) || depth.asks[0][0] !== Number(market.yes_ask)) fail('Depth differs from displayed quotes.');
    market.orderbook = {yes_bids: raw.market.orderbook.yes_bids.map(row => [...row]), yes_asks: raw.market.orderbook.yes_asks.map(row => [...row])};
    if (raw.market.metrics !== undefined) {
      const metrics = raw.market.metrics;
      if (!object(metrics) || typeof metrics.imbalance !== 'number' || !Number.isFinite(metrics.imbalance) || Math.abs(metrics.imbalance) > 1) fail('Invalid book metrics.');
      market.metrics = {imbalance: metrics.imbalance, microprice: decimal(metrics.microprice, 'microprice', {positive: true, max: 1, digits: 12}), spread: decimal(metrics.spread, 'spread', {positive: true, max: 1, digits: 12})};
      if (Number(market.metrics.microprice) < Number(market.yes_bid) || Number(market.metrics.microprice) > Number(market.yes_ask)
          || Math.abs(Number(market.metrics.spread) - (Number(market.yes_ask) - Number(market.yes_bid))) > 1e-6) fail('Inconsistent book diagnostics.');
    }
  }
  let quote_history;
  if (raw.quote_history !== undefined) {
    if (!Array.isArray(raw.quote_history) || raw.quote_history.length > 450 || (!market && raw.quote_history.length)) fail('Invalid quote history.');
    quote_history = raw.quote_history.map((row, index) => {
      if (!object(row)) fail('Invalid history observation.');
      const result = {time: stamp(row.time, 'history time'), yes_bid: decimal(row.yes_bid, 'history bid', {positive: true, max: 1}), yes_ask: decimal(row.yes_ask, 'history ask', {positive: true, max: 1}), midpoint: decimal(row.midpoint, 'history midpoint', {positive: true, max: 1, digits: 12})};
      if (Number(result.yes_bid) >= Number(result.yes_ask) || Number(result.yes_ask) >= 1
          || Date.parse(result.time) < Date.parse(market.open_time) || Date.parse(result.time) >= Date.parse(market.close_time)
          || Math.abs(Number(result.midpoint) - (Number(result.yes_bid) + Number(result.yes_ask)) / 2) > 1e-8 || (index && Date.parse(result.time) <= Date.parse(raw.quote_history[index - 1].time))) fail('Invalid history quote.');
      return result;
    });
  }
  return {kind: raw.kind, version: 1, mode: raw.mode, environment: raw.environment,
    selected_asset: selectedAsset, manual_order_supported: manualOrderSupported,
    orders_enabled: raw.orders_enabled, authenticated: raw.authenticated, market_status: raw.market_status,
    fetched_at: raw.fetched_at === null ? null : stamp(raw.fetched_at, 'snapshot time'),
    error: raw.error === null ? null : text(raw.error, 'market error', 600), market, limits, journal, access_token_required: true,
    ...(quote_history === undefined ? {} : {quote_history})};
}
export function validatePreview(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_order_preview' || typeof raw.order_eligible !== 'boolean' || raw.requires_confirmation !== true) fail('Unsupported order preview.');
  mode(raw);
  const result = {kind: raw.kind, mode: raw.mode, environment: raw.environment,
    market_ticker: text(raw.market_ticker, 'preview ticker', 128), order_eligible: raw.order_eligible,
    reason: text(raw.reason, 'preview reason', 1000), requires_confirmation: true};
  result.requested_outcome = raw.requested_outcome === undefined ? 'auto' : raw.requested_outcome;
  if (!['auto', 'yes', 'no'].includes(result.requested_outcome)) fail('Invalid requested contract side.');
  result.order_type = raw.order_type ?? 'ioc';
  result.time_in_force = raw.time_in_force ?? 'immediate_or_cancel';
  result.expiration_time = raw.expiration_time ?? null;
  if (!['ioc', 'limit'].includes(result.order_type)
      || result.time_in_force !== (result.order_type === 'limit' ? 'good_till_canceled' : 'immediate_or_cancel')
      || result.order_type === 'limit' && result.requested_outcome === 'auto'
      || result.order_type === 'ioc' && result.expiration_time !== null) fail('Invalid reviewed order type.');
  if (!/^KXBTC15M-[A-Za-z0-9_.:-]+$/.test(result.market_ticker)) fail('Wrong preview series.');
  result.estimated_cost = decimal(raw.estimated_cost, 'estimated reserved cost');
  result.count = decimal(raw.count, 'preview contracts', {digits: 2, max: 10000});
  if (!raw.order_eligible) {
    if (raw.preview_id !== null || raw.outcome !== null || raw.price !== null || Number(result.count) !== 0 || Number(result.estimated_cost) !== 0 || raw.sizing !== null) fail('Ineligible preview claims an order.');
    return {...result, preview_id: null, expires_at: raw.expires_at === null ? null : stamp(raw.expires_at, 'preview expiry'), outcome: null, price: null, sizing: null};
  }
  if (!['yes', 'no'].includes(raw.outcome) || Number(result.count) < 1 || Number(result.count) % 1 !== 0 || Number(result.estimated_cost) <= 0) fail('Invalid preview position.');
  result.preview_id = text(raw.preview_id, 'preview ID', 256);
  if (result.order_type === 'limit' && (!Number.isSafeInteger(result.expiration_time)
      || result.expiration_time <= 0 || result.expiration_time > 253402300799)) fail('A resting limit requires a valid expiration.');
  result.expires_at = stamp(raw.expires_at, 'preview expiry');
  result.outcome = raw.outcome;
  if (result.requested_outcome !== 'auto' && result.outcome !== result.requested_outcome) fail('Reviewed outcome differs from the selected contract.');
  result.price = decimal(raw.price, 'preview limit price', {positive: true, max: 1});
  if (Number(result.price) >= 1 || !object(raw.sizing) || raw.sizing.mode !== 'kelly') fail('Missing Kelly sizing basis.');
  result.sizing = {mode: 'kelly'};
  for (const key of ['probability', 'full_kelly_fraction', 'kelly_fraction', 'applied_fraction']) result.sizing[key] = decimal(raw.sizing[key], key, {max: 1, digits: 12});
  for (const key of ['effective_cost', 'bankroll', 'target_cost', 'budget_cost']) result.sizing[key] = decimal(raw.sizing[key], key, {positive: true, digits: 12});
  if (Number(result.sizing.effective_cost) >= 1 || Number(result.sizing.probability) <= Number(result.sizing.effective_cost)
      || Number(result.sizing.kelly_fraction) <= 0 || Number(result.sizing.applied_fraction) <= 0
      || Number(result.sizing.budget_cost) > Number(result.sizing.target_cost)
      || Number(result.estimated_cost) > Number(result.sizing.budget_cost) + 1e-10) fail('Inconsistent Kelly position.');
  const appendKeys = ['target_contracts', 'existing_contracts', 'additional_contracts', 'existing_cost', 'remaining_target_cost'];
  if (appendKeys.some(key => raw.sizing[key] !== undefined)) {
    if (!appendKeys.every(key => raw.sizing[key] !== undefined)) fail('Incomplete position sizing evidence.');
    for (const key of appendKeys.slice(0, 3)) {
      result.sizing[key] = decimal(raw.sizing[key], key, {max: 1e10, digits: 2});
      if (Number(result.sizing[key]) % 1 !== 0) fail('Fractional position sizing evidence.');
    }
    for (const key of appendKeys.slice(3)) result.sizing[key] = decimal(raw.sizing[key], key, {digits: 12});
    const units = value => { const [whole, fraction = ''] = value.split('.'); return BigInt(whole) * 1000000000000n + BigInt(fraction.padEnd(12, '0')); };
    const held = BigInt(result.sizing.existing_contracts.split('.')[0]);
    const target = BigInt(result.sizing.target_contracts.split('.')[0]);
    const added = BigInt(result.sizing.additional_contracts.split('.')[0]);
    const targetCost = units(result.sizing.target_cost), existingCost = units(result.sizing.existing_cost);
    if (Number(result.sizing.additional_contracts) !== Number(result.count) || held + added > target
        || target !== targetCost / units(result.sizing.effective_cost)
        || existingCost !== held * units(result.sizing.effective_cost)
        || units(result.sizing.remaining_target_cost) !== (targetCost > existingCost ? targetCost - existingCost : 0n)
        || units(result.sizing.budget_cost) > units(result.sizing.remaining_target_cost)) fail('Inconsistent position top-up.');
  }
  return result;
}
export function probabilityFromPercent(raw) {
  if (typeof raw !== 'string' || !/^(0|[1-9][0-9]?|100)(?:\.[0-9]{1,4})?$/.test(raw.trim())) fail('Enter a probability from 0% to 100%.');
  const [whole, fraction = ''] = raw.trim().split('.');
  const units = Number(whole) * 10000 + Number(fraction.padEnd(4, '0'));
  if (units > 1000000) fail('Enter a probability from 0% to 100%.');
  return (units / 1000000).toFixed(6);
}
export function limitPriceFromDollars(raw) {
  const value = typeof raw === 'string' ? raw.trim() : '';
  if (!/^0\.[0-9]{1,4}$/.test(value) || Number(value) <= 0 || Number(value) >= 1) fail('Enter a limit price between $0 and $1, for example 0.45 for 45 cents.');
  return Number(value).toFixed(4);
}
const dollars = value => value === null ? 'Unavailable' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4});
const targetDollars = (value, asset) => Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD',
  minimumFractionDigits: ['SOL', 'XRP'].includes(asset) ? 4 : 2, maximumFractionDigits: 8});
const cents = value => value === null ? 'Unavailable' : `${(Number(value) * 100).toLocaleString('en-US', {maximumFractionDigits: 2})}¢`;
const percent = value => `${(Number(value) * 100).toLocaleString('en-US', {maximumFractionDigits: 4})}%`;
const countdown = value => { const seconds = Math.max(0, Math.ceil(value / 1000)); return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`; };
const identity = state => state?.market ? JSON.stringify([state.market.quote_id, state.market.ticker, state.market.target_price, state.market.open_time, state.market.close_time]) : null;
// Quotes and depth can move while the reviewed limit order stays fixed.
// Contract terms, fee policy and account limits still invalidate that review.
const fingerprint = state => JSON.stringify([state.selected_asset, state.manual_order_supported, state.mode, state.environment, state.orders_enabled,
  identity(state), state.market && [state.market.fee_reserve, state.market.fee_type, state.market.fee_multiplier],
  state.limits, state.journal]);
export function mountLive(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = POLL_MS, onState = () => {}, onExecution = () => {}, asset = 'BTC'} = {}) {
  const $ = id => doc.getElementById(id);
  if (!$('kelly-form') || !$('probability')) return null;
  let state = null, preview = null, token = null, stopped = false, busy = false, refreshing = false, version = 0, uncertain = false;
  let selectedAsset = assetSymbol(asset), readGeneration = 0, readController = null, previewController = null;
  // Remember the draft's verified contract through temporary quote outages.
  let forecastIdentity = null;
  // Values retained here are presentation only. Order review continues to read
  // state and its original freshness timestamps, never this display cache.
  const marketValues = new Map();
  const marketValueIDs = ['target', 'ticker', 'countdown', 'close-time', 'yes-price', 'no-price',
    'yes-bid', 'no-bid', 'yes-available', 'no-available', 'fee-note'];
  const serviceValueIDs = ['bankroll', 'fraction', 'order-limit', 'total-limit', 'committed', 'intent-count'];
  let displayedQuote = null, lastServiceRead = null, displayedSensitivity = null;
  let tuningInitialized = false;
  if ($('contract-side') && !$('contract-side').value) $('contract-side').value = 'auto';
  // Real markup defaults to a resting limit; legacy embedded controls with no
  // order-type value retain their explicitly reviewed IOC behavior.
  if ($('order-type') && !$('order-type').value) $('order-type').value = 'ioc';
  const set = (id, value) => { $(id).textContent = value; };
  const error = message => { $('error').hidden = !message; set('error', message); };
  const fresh = () => state?.market_status === 'ready' && state.market && state.fetched_at
    && now() - Date.parse(state.fetched_at) >= -1000 && now() - Date.parse(state.fetched_at) <= MAX_QUOTE_AGE_MS
    && now() >= Date.parse(state.market.open_time) && now() < Date.parse(state.market.close_time);
  function markLastKnown(id, stale, timestamp) {
    const node = $(id); if (!node) return;
    if (!node.dataset) node.dataset = {};
    node.dataset.stale = stale ? 'true' : 'false';
    node.setAttribute?.('data-stale', stale ? 'true' : 'false');
    node.title = stale && timestamp ? `Last known at ${new Date(timestamp).toLocaleString()}` : '';
  }
  function showMarketValue(id, raw, formatted) {
    const previous = marketValues.get(id);
    if (raw !== null && raw !== undefined && (!previous
        || Date.parse(state.fetched_at) >= Date.parse(previous.timestamp))) {
      const saved = {text: formatted, timestamp: state.fetched_at, current: true};
      marketValues.set(id, saved); set(id, formatted); markLastKnown(id, false, saved.timestamp);
    } else {
      const saved = marketValues.get(id);
      if (saved) {
        saved.current = false; set(id, saved.text); markLastKnown(id, true, saved.timestamp);
      } else { set(id, 'Unavailable'); markLastKnown(id, false, null); }
    }
  }
  function retainMarketDisplay() {
    if (!displayedQuote || displayedQuote.asset !== selectedAsset) return false;
    for (const id of marketValueIDs) {
      const saved = marketValues.get(id);
      if (saved) { set(id, saved.text); markLastKnown(id, true, saved.timestamp); }
    }
    const age = Math.max(0, (now() - Date.parse(displayedQuote.fetched_at)) / 1000);
    set('freshness', `STALE - last known ${new Date(displayedQuote.fetched_at).toLocaleTimeString()} (${age.toFixed(1)}s ago). Fresh quotes required for trading.`);
    $('connection').className = 'pill bad'; set('connection', 'Last known quotes');
    return true;
  }
  const entryOpen = () => fresh() && Date.parse(state.market.close_time) - now() >= state.limits.min_seconds_to_close * 1000;
  const manualAllowed = () => selectedAsset === 'BTC' && state?.manual_order_supported !== false;
  function resetPreview(message = 'No preview') {
    version++; preview = null; $('preview').hidden = true; $('preview-empty').hidden = false;
    $('preview-position').hidden = true; $('confirm').disabled = true; set('review-status', message);
  }
  function selectedTuning() {
    return validateTuning($('tune-bankroll')?.value ?? state?.limits.bankroll,
      $('tune-fraction')?.value ?? state?.limits.kelly_fraction, state?.limits);
  }
  function selectedOutcome() {
    const value = $('contract-side') ? $('contract-side').value : 'auto';
    if (!['auto', 'yes', 'no'].includes(value)) fail('Choose Auto, YES or NO.');
    return value;
  }
  function selectedOrder() {
    const type = $('order-type')?.value || 'ioc';
    if (!['limit', 'ioc'].includes(type)) fail('Choose a resting limit or immediate-or-cancel limit.');
    const side = selectedOutcome();
    if (type === 'limit' && side === 'auto') fail('Choose YES or NO before reviewing a resting limit.');
    return {order_type: type, ...(type === 'limit' ? {limit_price: limitPriceFromDollars($('limit-price')?.value)} : {})};
  }
  function renderOrderHint() {
    const type = $('order-type')?.value || 'ioc';
    if ($('limit-price-fields')) $('limit-price-fields').hidden = type !== 'limit';
    if (!$('order-type-status')) return;
    set('order-type-status', type === 'limit'
      ? 'Resting limit: choose YES or NO and your maximum purchase price. It may fill immediately if marketable; otherwise it rests until filled, canceled or contract expiration. Price is never filled in automatically.'
      : 'Immediate-or-cancel limit: review uses the current executable price. Any unfilled quantity is canceled immediately; it will not rest in the book.');
  }
  function renderSideHint() {
    if (!$('contract-side-status')) return;
    let selected;
    try { selected = selectedOutcome(); } catch (failure) { set('contract-side-status', failure.message); return; }
    if (selected === 'auto') { set('contract-side-status', ($('order-type')?.value || 'ioc') === 'limit' ? 'Choose YES or NO for a resting limit. Auto is available only for immediate-or-cancel review.' : 'Auto compares both sides and reviews the eligible Kelly opportunity.'); return; }
    let probability = '';
    try {
      const yes = Number(probabilityFromPercent($('probability').value));
      probability = ` Your ${selected.toUpperCase()} win estimate is ${percent(String(selected === 'yes' ? yes : 1 - yes))}.`;
    } catch {}
    const ask = fresh() ? state.market[`${selected}_ask`] : null;
    set('contract-side-status', `${selected.toUpperCase()} only.${selected === 'no' ? ' NO uses 100% minus your YES estimate.' : ''}${probability}${ask == null ? '' : ` Current purchase price ${dollars(ask)} per contract, before fees.`} An ineligible selection will not switch to the other side.`);
  }
  function renderSensitivity() {
    if (!$('kelly-sensitivity') || typeof doc.createElement !== 'function') return;
    const context = JSON.stringify([selectedAsset, forecastIdentity, $('probability').value,
      $('tune-bankroll')?.value, $('tune-fraction')?.value, $('contract-side')?.value,
      $('order-type')?.value, $('limit-price')?.value]);
    const retain = () => {
      if (!displayedSensitivity || displayedSensitivity.context !== context) return false;
      markLastKnown('kelly-sensitivity', true, displayedSensitivity.timestamp);
      set('sensitivity-status', `STALE - last known sizing comparison at ${new Date(displayedSensitivity.timestamp).toLocaleString()}. Fresh quotes required for an order review.`);
      return true;
    };
    if (!fresh() && manualAllowed() && retain()) return;
    const clear = () => {
      displayedSensitivity = null; $('kelly-sensitivity').replaceChildren();
      markLastKnown('kelly-sensitivity', false, null);
    };
    if (!fresh()) {
      clear(); set('sensitivity-status', 'Fresh quotes and an estimate are required.'); return;
    }
    if (!manualAllowed()) { clear(); set('sensitivity-status', `${selectedAsset} market view. Manual order review currently supports BTC; use the paper strategy for this asset.`); return; }
    try {
      const probability = probabilityFromPercent($('probability').value);
      const requested = selectedOutcome();
      const order = selectedOrder(), sides = requested === 'auto' ? ['yes', 'no'] : [requested];
      if (order.order_type === 'ioc' && sides.some(side => state.market[`${side}_ask`] === null
          || state.market[`${side}_available`] === null)) {
        if (retain()) return;
        clear(); set('sensitivity-status', 'Fresh executable prices and quantities are required.'); return;
      }
      const result = kellySensitivity(state, probability, $('tune-bankroll').value, $('tune-fraction').value, requested, order);
      if (!result) {
        if (retain()) return;
        clear(); set('sensitivity-status', 'Fresh quotes and an estimate are required.'); return;
      }
      clear();
      if (!result.outcome) { set('sensitivity-status', requested === 'auto' ? 'Neither outcome clears the fee-adjusted minimum edge.' : `Selected ${requested.toUpperCase()} does not clear the fee-adjusted minimum edge.`); return; }
      set('sensitivity-status', `${result.outcome.toUpperCase()} | full Kelly ${(result.full_fraction * 100).toFixed(2)}% before caps. Scenarios are upper bounds until account and current-market checks.`);
      for (const row of result.rows) {
        const tr = doc.createElement('tr');
        for (const value of [`${row.multiplier}${row.selected ? ' (selected)' : ''}`, `${(row.applied_fraction * 100).toFixed(2)}%`,
          String(row.count), dollars(String(row.reserved_cost)), row.policy_allowed ? 'Within startup ceiling' : 'Simulation only: above ceiling']) {
          const td = doc.createElement('td'); td.textContent = value; tr.appendChild(td);
        }
        $('kelly-sensitivity').appendChild(tr);
      }
      displayedSensitivity = {context, timestamp: state.fetched_at};
    } catch { clear(); set('sensitivity-status', 'Enter a valid probability, bankroll and multiplier to compare sizes.'); }
  }
  function controls() {
    let valid = false;
    try { probabilityFromPercent($('probability').value); selectedTuning(); selectedOutcome(); selectedOrder(); valid = true; } catch {}
    if ($('contract-side')) $('contract-side').disabled = busy || stopped || !manualAllowed();
    renderSideHint();
    renderOrderHint();
    if ($('order-type')) $('order-type').disabled = busy || stopped || !manualAllowed();
    if ($('limit-price')) $('limit-price').disabled = busy || stopped || !manualAllowed() || ($('order-type')?.value || 'ioc') !== 'limit';
    $('probability').disabled = busy || forecastIdentity === null || stopped || !manualAllowed();
    for (const id of ['tune-bankroll', 'tune-fraction']) if ($(id)) $(id).disabled = busy || !tuningInitialized || stopped || !manualAllowed();
    $('review').disabled = !valid || !token || !entryOpen() || !manualAllowed() || busy || uncertain || state?.journal.halted || state?.journal.open_limit_count > 0 || stopped;
    $('refresh').disabled = refreshing || busy || stopped;
    const allowed = state?.mode === 'paper' || (state?.orders_enabled === true && state?.authenticated === true);
    $('confirm').hidden = !manualAllowed() || !!state && state.mode !== 'paper' && !state.orders_enabled;
    $('confirm').disabled = !preview?.order_eligible || !entryOpen() || busy || !allowed
      || !manualAllowed() || uncertain || state?.journal.halted || stopped || now() >= Date.parse(preview?.expires_at || 0);
  }
  function renderClock() {
    if (state?.market && fresh()) {
      showMarketValue('countdown', state.market.close_time, countdown(Date.parse(state.market.close_time) - now()));
      const age = state.fetched_at === null ? null : Math.max(0, (now() - Date.parse(state.fetched_at)) / 1000);
      set('freshness', `REST polling · last read ${age === null ? 'unknown' : age.toFixed(1) + 's ago'}${fresh() ? '' : ' · STALE'}`);
      if (!fresh()) {
        $('connection').className = 'pill bad'; set('connection', 'Quote stale');
        if (preview) resetPreview('Quote stale · review again');
      }
      if (manualAllowed() && fresh() && !entryOpen()) set('notice', `Quotes remain live. New entries stop ${state.limits.min_seconds_to_close} seconds before the window closes.`);
    }
    if (!fresh() && retainMarketDisplay()) {
      if (preview) resetPreview('Quotes are stale - review again after a fresh snapshot');
      renderSensitivity();
    } else if (fresh()) {
      for (const [id, saved] of marketValues) markLastKnown(id, !saved.current, saved.timestamp);
      if ([...marketValues.values()].some(saved => !saved.current)) {
        $('connection').className = 'pill muted'; set('connection', 'Partial quotes');
      }
    }
    if (preview?.order_eligible) {
      const remaining = Date.parse(preview.expires_at) - now();
      set('preview-expiry', `${Math.max(0, remaining / 1000).toFixed(1)}s`);
      if (remaining <= 0) resetPreview('Expired · review again');
    }
    if (state?.journal.open_limit_count > 0) set('notice', 'A limit order is outstanding. Track or cancel its remainder in Order history before reviewing another order.');
    if (!manualAllowed()) set('notice', `${selectedAsset} market view. Manual order review currently supports BTC; the selected asset can be managed by the paper strategy.`);
    controls();
  }
  function clearMarketData(forget = false) {
    if (!forget && retainMarketDisplay()) return true;
    displayedQuote = null; marketValues.clear();
    displayedSensitivity = null;
    $('kelly-sensitivity')?.replaceChildren?.(); markLastKnown('kelly-sensitivity', false, null);
    for (const id of marketValueIDs) markLastKnown(id, false, null);
    set('target', '—'); set('countdown', '—'); set('ticker', 'No current market loaded');
    set('close-time', 'Waiting for a current window'); set('freshness', 'REST polling · no current quote');
    for (const id of ['yes-price', 'no-price']) set(id, '—');
    for (const id of ['yes-bid', 'no-bid', 'yes-available', 'no-available']) set(id, 'Unavailable');
    set('fee-note', 'Fee allowance unavailable');
    return false;
  }
  function renderState() {
    if (displayedQuote && (displayedQuote.asset !== selectedAsset
        || displayedQuote.environment !== state.environment || displayedQuote.mode !== state.mode
        || state.market && displayedQuote.identity !== identity(state))) clearMarketData(true);
    lastServiceRead = new Date(now()).toISOString();
    for (const id of serviceValueIDs) markLastKnown(id, false, lastServiceRead);
    if ($('selected-asset-label')) set('selected-asset-label', selectedAsset);
    if ($('market-view-status')) set('market-view-status', `${selectedAsset} quotes, target and charts follow this selected contract.`);
    set('mode', !manualAllowed() ? `${selectedAsset} read-only market view` : state.mode === 'paper' ? 'Paper simulation' : state.mode === 'demo' ? 'Demo orders' : state.orders_enabled ? 'Live orders enabled' : 'Live orders disabled');
    set('environment', `${state.environment === 'production' ? 'Production' : 'Demo'} market data`);
    set('authentication', state.mode === 'paper' ? 'Public quotes · paper execution' : state.authenticated === true ? 'Account authentication verified' : state.authenticated === false ? 'Account authentication failed' : 'Account check occurs during review');
    set('bankroll', dollars(state.limits.bankroll)); set('fraction', `${percent(state.limits.kelly_fraction)} of full Kelly`);
    if (!tuningInitialized) {
      if ($('tune-bankroll')) $('tune-bankroll').value = state.limits.bankroll;
      if ($('tune-fraction')) $('tune-fraction').value = state.limits.kelly_fraction;
      tuningInitialized = true;
    }
    if ($('tuning-status')) {
      try { selectedTuning(); set('tuning-status', `Next review uses your choices, up to ${dollars(state.limits.bankroll)} and multiplier ${state.limits.kelly_fraction}.`); }
      catch (failure) { set('tuning-status', failure.message); }
    }
    renderSensitivity();
    set('order-limit', dollars(state.limits.max_order_cost)); set('total-limit', dollars(state.limits.max_total_cost));
    set('committed', dollars(state.journal.committed_cost)); set('intent-count', String(state.journal.intent_count));
    set('journal-state', state.journal.halted ? 'Halted · reconcile order' : 'Ready for review');
    if (!state.market) {
      const retained = clearMarketData();
      $('connection').className = 'pill bad'; set('connection', retained ? 'Last known quotes' : state.market_status === 'loading' ? 'Loading market' : 'Data unavailable');
      error(uncertain ? 'An earlier submission is uncertain. Check the local journal and Kalshi order history.' : state.error || `Waiting for a fresh ${selectedAsset} 15-minute market snapshot.`);
    } else {
      const market = state.market;
      const displayedTime = displayedQuote && Date.parse(displayedQuote.fetched_at) > Date.parse(state.fetched_at)
        ? displayedQuote.fetched_at : state.fetched_at;
      displayedQuote = {asset: selectedAsset, environment: state.environment, mode: state.mode,
        identity: identity(state), fetched_at: displayedTime};
      showMarketValue('target', market.target_price, targetDollars(market.target_price, selectedAsset));
      showMarketValue('ticker', market.ticker, market.ticker);
      showMarketValue('close-time', market.close_time, `Closes ${new Date(market.close_time).toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short'})}`);
      showMarketValue('yes-price', market.yes_ask, cents(market.yes_ask)); showMarketValue('no-price', market.no_ask, cents(market.no_ask));
      showMarketValue('yes-bid', market.yes_bid, cents(market.yes_bid)); showMarketValue('no-bid', market.no_bid, cents(market.no_bid));
      showMarketValue('yes-available', market.yes_available, `${market.yes_available} contracts`);
      showMarketValue('no-available', market.no_available, `${market.no_available} contracts`);
      set('fee-note', `Fee allowance ${dollars(market.fee_reserve)} / contract · ${market.fee_type}`);
      showMarketValue('fee-note', market.fee_reserve, $('fee-note').textContent);
      $('connection').className = 'pill good'; set('connection', 'Live REST quotes');
      error(uncertain ? 'An earlier submission is uncertain. Check the local journal and Kalshi order history.' : state.journal.halted ? 'The journal has an unresolved order. New submissions are halted until it is reconciled.' : '');
    }
    set('notice', 'Public prices refresh every 2 seconds. Review calculates a position; confirmation is a separate action.');
    if (state.journal.open_limit_count > 0) set('journal-state', 'Limit order outstanding');
    renderClock();
  }
  async function request(path, body, externalController = null) {
    if (!PATHS.has(path) && !/^\/api\/state\?asset=(BTC|ETH|SOL|XRP)$/.test(path)) fail('Unsupported local request.');
    const controller = externalController || new AbortController();
    const timeout = win.setTimeout(() => controller.abort(), 12000);
    try {
      let response;
      try {
        response = await fetchImpl(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', signal: controller.signal,
          headers: body === undefined ? {Accept: 'application/json'} : {Accept: 'application/json', 'Content-Type': 'application/json', 'X-Kalshi-Token': token},
          ...(body === undefined ? {} : {body: JSON.stringify(body)})});
      } catch { fail('Unable to reach the local service.'); }
      const raw = await response.text();
      if (new TextEncoder().encode(raw).byteLength > 2_000_000) fail('Local response exceeds the supported size.');
      let value;
      try { value = JSON.parse(raw); } catch { value = null; }
      if (!response.ok) {
        let reason = `Local request rejected (HTTP ${Number.isInteger(response.status) ? response.status : 'unknown'}).`;
        let detail = null;
        if (object(value) && typeof value.error === 'string' && value.error.length <= 600 && !/[\u0000-\u001f\u007f]/u.test(value.error)) {
          detail = value.error; reason += ` ${detail}`;
        }
        const rejection = new Error(reason); rejection.status = response.status; rejection.detail = detail; throw rejection;
      }
      if (value === null) fail('Local service returned invalid JSON.');
      return value;
    } finally { win.clearTimeout(timeout); }
  }
  async function refresh() {
    if (stopped || refreshing) return;
    const expected = ++readGeneration, expectedAsset = selectedAsset, controller = new AbortController();
    readController = controller;
    refreshing = true; controls();
    try {
      const path = expectedAsset === 'BTC' ? '/api/state' : `/api/state?asset=${expectedAsset}`;
      const value = validateState(await request(path, undefined, controller), expectedAsset);
      if (stopped || expected !== readGeneration || expectedAsset !== selectedAsset) return;
      const nextIdentity = identity(value);
      if (nextIdentity !== null && nextIdentity !== forecastIdentity) {
        $('probability').value = '';
        if ($('limit-price')) $('limit-price').value = '';
        resetPreview(forecastIdentity === null ? 'Enter an estimate' : 'Contract changed - enter a new estimate');
        forecastIdentity = nextIdentity;
      }
      if (state && fingerprint(state) !== fingerprint(value)) resetPreview('Contract, fees or account limits changed - review again');
      state = value; renderState(); onState(state);
    } catch (failure) {
      if (stopped || expected !== readGeneration || expectedAsset !== selectedAsset) return;
      state = null; onState(null); resetPreview('Data unavailable - estimate kept');
      clearMarketData();
      for (const id of serviceValueIDs) markLastKnown(id, true, lastServiceRead);
      $('connection').className = 'pill bad'; set('connection', displayedQuote ? 'Last known quotes' : 'Disconnected');
      set('notice', 'The local service is unavailable. Your estimate is kept; fresh quotes are required to review an order.');
      set('freshness', 'No fresh quote · trading review disabled');
      error(uncertain ? 'An earlier submission is uncertain. Check the local journal and Kalshi order history.' : failure.message || 'Unable to read live market data.');
      if (displayedQuote) retainMarketDisplay();
      renderSensitivity();
    } finally { if (expected === readGeneration) { refreshing = false; readController = null; controls(); } }
  }
  async function review(event) {
    event?.preventDefault();
    if ($('review').disabled || stopped || !manualAllowed()) return;
    let probability, tuning, requested, order;
    try { probability = probabilityFromPercent($('probability').value); tuning = selectedTuning(); requested = selectedOutcome(); order = selectedOrder(); } catch (failure) { error(failure.message); return; }
    resetPreview('Calculating'); const expectedVersion = version, expectedFingerprint = fingerprint(state);
    busy = true; error(''); set('result', ''); controls();
    const controller = new AbortController(); previewController = controller;
    try {
      const value = validatePreview(await request('/api/preview', {probability_yes: probability,
        market_ticker: state.market.ticker, quote_id: state.market.quote_id,
        bankroll: tuning.bankroll, kelly_fraction: tuning.kelly_fraction, requested_outcome: requested, ...order}, controller));
      if (stopped || expectedVersion !== version || !state || fingerprint(state) !== expectedFingerprint) return;
      if (!fresh() || value.market_ticker !== state.market.ticker || value.mode !== state.mode || value.environment !== state.environment) fail('The contract changed. Review a fresh quote.');
      if (value.requested_outcome !== requested || selectedOutcome() !== requested) fail('The reviewed contract side differs from your selection. Refresh and review again.');
      if (value.order_type !== order.order_type || selectedOrder().order_type !== order.order_type) fail('The reviewed order type differs from your selection. Restart the console if its server is older, then review again.');
      if (value.order_eligible && order.order_type === 'limit' && (value.price !== order.limit_price
          || selectedOrder().limit_price !== order.limit_price
          || value.expiration_time !== Math.floor(Date.parse(state.market.close_time) / 1000))) fail('The reviewed limit price or contract expiration differs from your selection.');
      if (value.order_eligible && (Number(value.sizing.kelly_fraction) !== Number(tuning.kelly_fraction)
          || Number(value.sizing.bankroll) > Number(tuning.bankroll))) fail('Reviewed sizing differs from the chosen parameters.');
      if (value.order_eligible && (Date.parse(value.expires_at) <= now() || Date.parse(value.expires_at) - now() > 15000)) fail('The order review is expired or invalid.');
      preview = value; $('preview-empty').hidden = true; $('preview').hidden = false;
      $('preview-position').hidden = !value.order_eligible; set('preview-reason', value.reason);
      set('review-status', value.order_eligible ? 'Ready to confirm' : 'No eligible trade');
      if (value.order_eligible) {
        set('preview-outcome', value.outcome.toUpperCase()); set('preview-count', value.count);
        set('preview-price', dollars(value.price)); set('preview-cost', dollars(value.estimated_cost));
        if ($('preview-order-type')) set('preview-order-type', value.order_type === 'limit' ? 'Resting limit (good till contract close)' : 'Immediate-or-cancel limit');
        if ($('preview-order-expiration')) set('preview-order-expiration', value.order_type === 'limit'
          ? `Order expires at contract close: ${new Date(value.expiration_time * 1000).toLocaleString()}` : 'Any remainder cancels immediately');
        set('preview-payout', dollars(value.count)); set('preview-profit', dollars(String(Number(value.count) - Number(value.estimated_cost))));
        set('preview-loss', `−${dollars(value.estimated_cost)}`);
        set('preview-bankroll', dollars(value.sizing.bankroll)); set('preview-allocation', percent(value.sizing.applied_fraction));
        if ($('preview-existing')) {
          const known = value.sizing.existing_contracts !== undefined;
          set('preview-existing', known ? value.sizing.existing_contracts : 'Unavailable');
          set('preview-target', known ? value.sizing.target_contracts : 'Unavailable');
          set('preview-after', known ? String(Number(value.sizing.existing_contracts) + Number(value.count)) : 'Unavailable');
          set('preview-action', !known ? 'Reviewed order' : Number(value.sizing.existing_contracts) > 0 ? 'Add to existing position' : 'New position order');
        }
        set('preview-contract', value.market_ticker);
        set('confirm', value.mode === 'paper' ? 'Confirm paper simulation' : value.mode === 'demo' ? 'Place demo order' : 'Place live order');
        $('confirm').className = `primary confirm${value.mode === 'live' ? ' live' : ''}`;
        const action = value.mode === 'paper' ? 'Confirmation records a paper simulation. No exchange order is sent.' : value.mode === 'demo' ? 'Confirmation sends this exact order to the demo exchange.' : 'Confirmation sends this exact order to your production Kalshi account.';
        const behavior = value.order_type === 'limit'
          ? 'It may fill immediately at your limit or better; otherwise the remainder rests until filled, canceled or the contract closes. A resting acknowledgement is not a fill. Paper limits remain simulated resting orders with no assumed fills.'
          : 'Any quantity not filled immediately is canceled.';
        set('confirmation-note', `${action} ${behavior} This review keeps its displayed limit price while quotes move; confirmation rechecks the contract, budget and size. Profit and loss figures use the fee allowance; actual charges can differ.`);
      }
      renderClock();
      if (value.order_eligible && value.mode !== 'paper') await refresh();
    } catch (failure) {
      if (!stopped && expectedVersion === version) { resetPreview('Review unavailable'); error(failure.message || 'Unable to calculate an order preview.'); }
    } finally { if (previewController === controller) previewController = null; busy = false; controls(); }
  }
  async function confirm() {
    if (stopped || !manualAllowed() || $('confirm').disabled || !preview?.order_eligible) return;
    if (selectedOutcome() !== preview.requested_outcome) { resetPreview('Contract side changed - review again'); controls(); return; }
    try {
      const order = selectedOrder();
      if (order.order_type !== preview.order_type || order.order_type === 'limit' && order.limit_price !== preview.price) { resetPreview('Order terms changed - review again'); controls(); return; }
    } catch { resetPreview('Order terms changed - review again'); controls(); return; }
    const selected = preview; let execution = null;
    resetPreview('Submitting once'); busy = true; error(''); controls();
    set('result', selected.mode === 'paper' ? 'Recording paper simulation…' : 'Submitting the reviewed order once…');
    try {
      const value = await request('/api/execute', {preview_id: selected.preview_id});
      if (stopped) return;
      if (!object(value) || value.kind !== 'kalshi_execution_result' || !['paper_filled', 'paper_resting', 'resting', 'cancel_pending', 'filled', 'canceled', 'unknown', 'rejected', 'skipped'].includes(value.status)) fail('Invalid execution acknowledgement.');
      const reason = text(value.reason, 'execution reason', 1000);
      if ((selected.mode !== 'paper' && ['paper_filled', 'paper_resting'].includes(value.status)) || (selected.mode === 'paper' && ['filled', 'resting', 'cancel_pending', 'canceled'].includes(value.status))) fail('Execution mode differs from the reviewed order.');
      if (selected.order_type === 'ioc' && ['paper_resting', 'resting', 'cancel_pending'].includes(value.status)) fail('Immediate-or-cancel order unexpectedly remains open.');
      uncertain = value.status === 'unknown';
      execution = value;
      const statusLabel = {paper_filled: 'Paper simulation recorded', paper_resting: 'Paper limit recorded as resting; no fill assumed',
        resting: 'Limit order is resting; unfilled quantity remains open', cancel_pending: 'Cancellation pending; order may still fill',
        filled: 'Exchange fill reported', unknown: 'Order outcome uncertain', canceled: 'Order canceled'};
      set('result', `${statusLabel[value.status] || 'Order not placed'}. ${reason}${selectedAsset === 'BTC' ? '' : ` Contract: ${selected.market_ticker}.`}`);
      set('review-status', uncertain ? 'Reconciliation required' : 'Review completed');
      if (uncertain) error('The order outcome is uncertain. Check the journal before any further submission.');
    } catch (failure) {
      if (!stopped && [400, 403, 404, 409, 413, 415].includes(failure.status)) {
        set('result', `Order not placed. ${failure.detail || failure.message} Review the updated quote before another submission.`);
        set('review-status', 'Not placed · review again');
      } else if (!stopped) {
        uncertain = true; set('result', 'Submission outcome is uncertain. This page will not retry the order.');
        error('Check the local journal and Kalshi order history before trying again.'); set('review-status', 'Reconciliation required');
      }
    } finally {
      busy = false; controls();
      if (!stopped) {
        try { Promise.resolve(onExecution(execution || {status: uncertain ? 'unknown' : 'rejected'})).catch(() => {}); } catch {}
        await refresh();
      }
    }
  }
  $('kelly-form').addEventListener('submit', review);
  $('probability').addEventListener('input', () => { resetPreview('Estimate changed'); error(''); renderSensitivity(); controls(); });
  if ($('contract-side')) $('contract-side').addEventListener('change', () => {
    resetPreview('Contract side changed - review again'); error(''); renderSensitivity(); controls();
  });
  if ($('order-type')) $('order-type').addEventListener('change', () => {
    resetPreview('Order type changed - review again'); error(''); renderSensitivity(); controls();
  });
  if ($('limit-price')) $('limit-price').addEventListener('input', () => {
    resetPreview('Limit price changed - review again'); error(''); renderSensitivity(); controls();
  });
  for (const id of ['tune-bankroll', 'tune-fraction']) if ($(id)) $(id).addEventListener('input', () => {
    resetPreview('Sizing changed - review again'); error('');
    if ($('tuning-status')) {
      try { selectedTuning(); set('tuning-status', 'Sizing draft changed. Create a fresh order review to use it.'); }
      catch (failure) { set('tuning-status', failure.message); }
    }
    renderSensitivity(); controls();
  });
  $('confirm').addEventListener('click', confirm);
  $('refresh').addEventListener('click', () => refresh());
  const poll = win.setInterval(() => refresh(), pollMs);
  const clock = win.setInterval(renderClock, 250);
  function setAsset(value) {
    const next = assetSymbol(value);
    if (stopped || next === selectedAsset) return;
    selectedAsset = next; readGeneration++; readController?.abort(); previewController?.abort();
    refreshing = false; state = null; forecastIdentity = null;
    $('probability').value = ''; if ($('limit-price')) $('limit-price').value = '';
    resetPreview('Asset changed - enter a new estimate'); clearMarketData(true); onState(null);
    if ($('selected-asset-label')) set('selected-asset-label', selectedAsset);
    if ($('market-view-status')) set('market-view-status', `Loading ${selectedAsset} quotes, target and charts.`);
    set('result', ''); error(''); set('notice', `Loading the ${selectedAsset} market view.`);
    controls(); return refresh();
  }
  function dispose() {
    stopped = true; version++; preview = null; token = null;
    readGeneration++; readController?.abort(); previewController?.abort();
    win.clearInterval(poll); win.clearInterval(clock); $('review').disabled = true; $('confirm').disabled = true;
    if ($('contract-side')) $('contract-side').disabled = true;
  }
  win.addEventListener('pagehide', dispose);
  const ready = (async () => {
    try {
      const value = await request('/api/session');
      if (!object(value) || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{24,256}$/.test(value.token)) fail('Invalid local session.');
      if (stopped) return;
      token = value.token; await refresh();
    } catch (failure) {
      if (!stopped) { error(failure.message || 'Unable to start local session.'); set('notice', 'Open this page from the local Kalshi service.'); set('connection', 'Session unavailable'); $('connection').className = 'pill bad'; controls(); }
    }
  })();
  controls();
  return {ready, refresh, dispose, renderClock, setAsset, getAsset: () => selectedAsset,
    invalidateReview: (message = 'Order state changed - review again') => { resetPreview(message); controls(); },
    getState: () => state, getPreview: () => preview};
}
// A running service may serve updated HTML before it has the new static route.
// Optional monitoring must not prevent the manual console from starting.
function mountOptionalPanel(doc, win, {panel, status, description, method, importModule, asset = 'BTC', onAssetChange = () => {}}) {
  if (!doc.getElementById(panel)) return null;
  let monitor = null, latestState = null, stopped = false, selectedAsset = assetSymbol(asset);
  const unavailable = () => {
    const element = doc.getElementById(status);
    if (element) element.textContent = `Restart the local Kalshi service to enable ${description}.`;
  };
  function setMarketState(value) {
    latestState = value;
    if (!monitor || stopped) return;
    try { monitor.setMarketState(value); } catch { unavailable(); }
  }
  function setAsset(value) {
    const next = assetSymbol(value);
    if (stopped || next === selectedAsset) return;
    selectedAsset = next; latestState = null;
    try { monitor?.setAsset?.(next); } catch { unavailable(); }
  }
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module[method](doc, win, {asset: selectedAsset, onAssetChange});
    monitor?.setAsset?.(selectedAsset);
    setMarketState(latestState);
    return monitor;
  }).catch(() => { if (!stopped) unavailable(); return null; });
  function dispose() { stopped = true; monitor?.dispose(); }
  win.addEventListener('pagehide', dispose);
  return {ready, setMarketState, setAsset, getAsset: () => selectedAsset, dispose};
}
export function mountOptionalAlgorithm(doc = document, win = window, {importModule = () => import('./algorithm.mjs'), asset = 'BTC', onAssetChange = () => {}} = {}) {
  return mountOptionalPanel(doc, win, {panel: 'algorithm-panel', status: 'algorithm-status', description: 'the algorithm monitor',
    method: 'mountAlgorithm', importModule, asset, onAssetChange});
}
export function mountOptionalGreeks(doc = document, win = window, {importModule = () => import('./greeks.mjs'), asset = 'BTC'} = {}) {
  return mountOptionalPanel(doc, win, {panel: 'greeks-panel', status: 'greeks-status', description: 'probability sensitivities',
    method: 'mountGreeks', importModule, asset});
}
export function mountOptionalProbability(doc = document, win = window, {importModule = () => import('./probability.mjs'), asset = 'BTC'} = {}) {
  return mountOptionalPanel(doc, win, {panel: 'live-probability-panel', status: 'probability-mc-status', description: 'live Monte Carlo probabilities',
    method: 'mountProbability', importModule, asset});
}
export function mountDashboard(doc = document, win = window, {asset = 'BTC', mounts = {}} = {}) {
  const factories = {analytics: mountAnalytics, live: mountLive, positions: mountPositions, orders: mountOrders,
    algorithm: mountOptionalAlgorithm, greeks: mountOptionalGreeks, probability: mountOptionalProbability, ...mounts};
  let selectedAsset = assetSymbol(asset), stopped = false, live = null;
  const panels = {};
  function setAsset(value) {
    const next = assetSymbol(value);
    if (stopped || next === selectedAsset) return;
    selectedAsset = next;
    // Clear research panels before the selected quote request can complete.
    for (const name of ['analytics', 'algorithm', 'greeks', 'probability']) panels[name]?.setAsset?.(next);
    return live?.setAsset?.(next);
  }
  panels.analytics = factories.analytics(doc, win, {asset: selectedAsset, onAssetChange: setAsset});
  panels.positions = factories.positions(doc, win);
  panels.orders = factories.orders(doc, win, {onCancel: value => {
    live?.invalidateReview('Order cancellation requested - review again');
    if (value.phase === 'finished') return Promise.allSettled([panels.positions?.refresh(), live?.refresh()]);
  }});
  panels.algorithm = factories.algorithm(doc, win, {asset: selectedAsset, onAssetChange: setAsset});
  panels.greeks = factories.greeks(doc, win, {asset: selectedAsset});
  panels.probability = factories.probability(doc, win, {asset: selectedAsset});
  live = factories.live(doc, win, {asset: selectedAsset,
    onState: state => {
      panels.analytics?.setMarketState(state); panels.positions?.setServiceState(state); panels.orders?.setServiceState(state);
      panels.algorithm?.setMarketState(state); panels.greeks?.setMarketState(state); panels.probability?.setMarketState(state);
    },
    onExecution: () => Promise.allSettled([panels.positions?.refresh(), panels.orders?.refresh()])});
  panels.live = live;
  for (const name of ['analytics', 'algorithm', 'greeks', 'probability']) panels[name]?.setAsset?.(selectedAsset);
  function dispose() { if (stopped) return; stopped = true; for (const panel of Object.values(panels)) panel?.dispose?.(); }
  win.addEventListener('pagehide', dispose);
  return {panels, setAsset, getAsset: () => selectedAsset, dispose};
}
if (typeof document !== 'undefined' && typeof window !== 'undefined') mountDashboard();
