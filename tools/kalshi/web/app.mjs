import {mountAnalytics, cumulativeDepth} from './analytics.mjs';
import {mountPositions, mountOrders, validateTuning, kellySensitivity} from './positions.mjs';
// Credentials, account checks, risk and order submission stay in the local service.
// This page only requests an order after a separate, explicit confirmation.
export const POLL_MS = 2000;
export const MAX_QUOTE_AGE_MS = 5000;
const PATHS = new Set(['/api/session', '/api/state', '/api/preview', '/api/execute']);
const ASSET_SERIES = Object.freeze({BTC: 'KXBTC15M', ETH: 'KXETH15M', SOL: 'KXSOL15M', XRP: 'KXXRP15M', GOLD: 'KXGOLD15M'});
const assetSymbol = value => typeof value === 'string' && Object.hasOwn(ASSET_SERIES, value.replace(/-USD$/, ''))
  ? value.replace(/-USD$/, '') : fail('Choose BTC, ETH, SOL, XRP or GOLD.');
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
  if (new Date(Date.parse(value)).toISOString().slice(0, 19) !== value.slice(0, 19)) fail(`Invalid ${name}.`);
  return value;
}
function mode(value) {
  if (!['paper', 'demo', 'live'].includes(value.mode) || !['demo', 'production'].includes(value.environment)
      || (value.mode === 'demo' && value.environment !== 'demo') || (value.mode === 'live' && value.environment !== 'production')) fail('Invalid service mode.');
}
const nullableDecimal = (value, name, options) => value === null ? null : decimal(value, name, options);
function sourceConfiguration(raw, selectedAsset) {
  if (raw === undefined) return undefined;
  const expectedSource = selectedAsset === 'GOLD' ? 'PYTH_1OZGOLD_USD' : selectedAsset === 'BTC' ? 'CF_BRTI' : `CF_${selectedAsset}USD_RTI`;
  if (!object(raw) || raw.kind !== 'kalshi_dashboard_source_configuration' || raw.version !== 1
      || raw.market_quotes !== 'kalshi_rest' || !['coinbase_proxy', 'unavailable'].includes(raw.spot_research)
      || raw.settlement_source !== expectedSource || raw.standalone_runner_connected !== false
      || !['external_paper_report', 'local_managed_paper'].includes(raw.algorithm_monitor)) fail('Invalid dashboard source configuration.');
  const result = {kind: raw.kind, version: 1, market_quotes: raw.market_quotes,
    spot_research: raw.spot_research, settlement_source: raw.settlement_source,
    algorithm_monitor: raw.algorithm_monitor, standalone_runner_connected: false};
  for (const key of ['index_data_configured', 'account_view_enabled', 'probability_fit_loaded',
    'validation_report_configured', 'algorithm_report_configured', 'neutral_report_configured']) {
    if (typeof raw[key] !== 'boolean') fail('Invalid dashboard source capability.');
    result[key] = raw[key];
  }
  if ((raw.probability_fit_loaded && !['BTC', 'ETH', 'SOL', 'XRP'].includes(raw.probability_fit_asset))
      || !raw.probability_fit_loaded && raw.probability_fit_asset !== null
      || raw.algorithm_report_configured !== (raw.algorithm_monitor === 'external_paper_report')
      || raw.spot_research !== (selectedAsset === 'GOLD' ? 'unavailable' : 'coinbase_proxy')) fail('Inconsistent dashboard source configuration.');
  result.probability_fit_asset = raw.probability_fit_asset;
  if (raw.strategy_status_report_count !== undefined) {
    if (!Number.isSafeInteger(raw.strategy_status_report_count) || raw.strategy_status_report_count < 0 || raw.strategy_status_report_count > 8) fail('Invalid producer report count.');
    result.strategy_status_report_count = raw.strategy_status_report_count;
  }
  if (raw.timeseries_archive_count !== undefined) {
    if (!Number.isSafeInteger(raw.timeseries_archive_count) || raw.timeseries_archive_count < 0 || raw.timeseries_archive_count > 8) fail('Invalid historical archive count.');
    result.timeseries_archive_count = raw.timeseries_archive_count;
  }
  if (raw.activity_report_configured !== undefined) {
    if (typeof raw.activity_report_configured !== 'boolean') fail('Invalid activity report capability.');
    result.activity_report_configured = raw.activity_report_configured;
  }
  if (raw.probability_fit_assets !== undefined) {
    const assets = raw.probability_fit_assets;
    if (!Array.isArray(assets) || assets.length > 4 || new Set(assets).size !== assets.length
        || assets.some(asset => !['BTC', 'ETH', 'SOL', 'XRP'].includes(asset))
        || raw.probability_fit_loaded !== (assets.length > 0)
        || assets.length && !assets.includes(raw.probability_fit_asset)) fail('Invalid per-asset research fits.');
    result.probability_fit_assets = [...assets];
  }
  return result;
}
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
    if (selectedAsset === 'GOLD' || value.settlement_source !== undefined) {
      market.settlement_source = text(value.settlement_source, 'settlement source', 100);
      const expectedSource = selectedAsset === 'GOLD' ? 'PYTH_1OZGOLD_USD' : selectedAsset === 'BTC' ? 'CF_BRTI' : `CF_${selectedAsset}USD_RTI`;
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
  const sources = sourceConfiguration(raw.source_configuration, selectedAsset);
  return {kind: raw.kind, version: 1, mode: raw.mode, environment: raw.environment,
    selected_asset: selectedAsset, manual_order_supported: manualOrderSupported,
    orders_enabled: raw.orders_enabled, authenticated: raw.authenticated, market_status: raw.market_status,
    fetched_at: raw.fetched_at === null ? null : stamp(raw.fetched_at, 'snapshot time'),
    error: raw.error === null ? null : text(raw.error, 'market error', 600), market, limits, journal, access_token_required: true,
    ...(quote_history === undefined ? {} : {quote_history}),
    ...(sources === undefined ? {} : {source_configuration: sources})};
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
  const serviceValueIDs = ['bankroll', 'fraction', 'order-limit', 'total-limit', 'committed', 'intent-count', 'dashboard-source-status', 'dashboard-source-summary'];
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
    if (!manualAllowed()) { clear(); set('sensitivity-status', selectedAsset === 'GOLD'
      ? 'Gold is a read-only market view. Its settlement model requires the Pyth 1OZGOLD/USD feed; paper and manual order review are unavailable.'
      : `${selectedAsset} market view. Manual order review currently supports BTC; use the paper strategy for this asset.`); return; }
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
    if (!manualAllowed()) set('notice', selectedAsset === 'GOLD'
      ? 'GOLD market view: live Kalshi quotes, target and order book. Gold uses Pyth 1OZGOLD/USD minute-close settlement; its model feed is not connected, so paper and manual orders are unavailable.'
      : `${selectedAsset} market view. Manual order review currently supports BTC; the selected asset can be managed by the paper strategy.`);
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
    if ($('dashboard-source-status')) {
      const sources = state.source_configuration;
      const fits = sources?.probability_fit_assets || (sources?.probability_fit_loaded ? [sources.probability_fit_asset] : []);
      const fitScope = selectedAsset === 'GOLD' ? 'GOLD has no CF research model in these panels'
        : fits.includes(selectedAsset) ? `${selectedAsset} uses its matching research fit`
        : `${selectedAsset} uses its own default model parameters`;
      const fit = fits.length > 1
        ? `${fits.join(', ')} historical fits loaded; ${fitScope}`
        : sources?.probability_fit_loaded
        ? `${sources.probability_fit_asset} historical fit loaded${sources.probability_fit_asset === selectedAsset ? '' : `; ${fitScope}`}`
        : 'Historical fit not loaded';
      set('dashboard-source-status', sources
        ? `Sources: Kalshi REST quotes; ${sources.spot_research === 'coinbase_proxy' ? 'Coinbase spot proxy for research' : 'compatible underlying research feed unavailable'}; settlement ${sources.settlement_source}. CF index access ${sources.index_data_configured ? 'configured' : 'not configured'}; current availability is reported in each model panel. ${fit}. Account view ${sources.account_view_enabled ? 'configured' : 'not configured'}. Validation report ${sources.validation_report_configured ? 'configured' : 'not configured'}; neutral paper report ${sources.neutral_report_configured ? 'configured' : 'not configured'}. Strategy paper panel: ${sources.algorithm_monitor === 'external_paper_report' ? 'external paper report' : 'local managed paper session'}; standalone trading runners are not monitored by those paper controls. Producer status reports configured: ${sources.strategy_status_report_count ?? 0}. Historical archive files: ${sources.timeseries_archive_count === undefined ? "not reported by this service" : sources.timeseries_archive_count}; see History lab for source-time coverage and availability.`
        : 'Dashboard source configuration is unavailable from this server version. Check each panel’s source and original timestamps.');
      if (sources?.activity_report_configured !== undefined) {
        set('dashboard-source-status', `${$('dashboard-source-status').textContent} Asset comparison activity report ${sources.activity_report_configured ? 'configured' : 'not configured'}; its source clock and reconciliation exclusions appear in Portfolio.`);
      }
      markLastKnown('dashboard-source-status', false, lastServiceRead);
      if ($('dashboard-source-summary')) {
        set('dashboard-source-summary', sources
          ? `CF access ${sources.index_data_configured ? 'configured' : 'not configured'} | Account view ${sources.account_view_enabled ? 'configured' : 'not configured'} | Research fits ${fits.length ? fits.join(', ') : 'none'} | History archives ${sources.timeseries_archive_count === undefined ? "not reported" : sources.timeseries_archive_count} | See each panel for current data availability.`
          : 'Source configuration unavailable; check the original timestamps in each panel.');
        if (sources?.activity_report_configured !== undefined) set('dashboard-source-summary', `${$('dashboard-source-summary').textContent} Asset activity ${sources.activity_report_configured ? 'configured' : 'not configured'}.`);
        markLastKnown('dashboard-source-summary', false, lastServiceRead);
      }
    }
    if ($('manual-execution-scope')) set('manual-execution-scope', manualAllowed()
      ? 'Manual BTC order review. Your estimate and a fresh quote are required before confirmation.'
      : `${selectedAsset} is a read-only market view in this manual console. Its standalone strategy has separate execution controls.`);
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
    if (!PATHS.has(path) && !/^\/api\/state\?asset=(BTC|ETH|SOL|XRP|GOLD)$/.test(path)) fail('Unsupported local request.');
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
      if (value.fetched_at !== null && displayedQuote && displayedQuote.asset === selectedAsset
          && displayedQuote.environment === value.environment && displayedQuote.mode === value.mode
          && Date.parse(value.fetched_at) < Date.parse(displayedQuote.fetched_at)) {
        fail('An older quote response was ignored. A current quote is required before order review.');
      }
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
      error(uncertain ? 'An earlier submission is uncertain. Check the local journal and Kalshi order history.'
        : selectedAsset === 'GOLD' ? `Gold quotes are unavailable. Restart the local Kalshi service to load Gold market support. ${failure.message || ''}`
          : failure.message || 'Unable to read live market data.');
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
function mountOptionalPanel(doc, win, {panel, status, description, method, importModule, asset = 'BTC', onAssetChange = () => {}, mountOptions = {}}) {
  if (typeof doc?.getElementById !== 'function' || !doc.getElementById(panel)) return null;
  let monitor = null, latestState = null, stopped = false, selectedAsset = assetSymbol(asset);
  const unavailable = () => {
    const element = doc.getElementById(status);
    if (element) element.textContent = `Restart the local Kalshi service to enable ${description}.`;
  };
  function setMarketState(value) {
    latestState = value;
    if (!monitor || stopped) return;
    try {
      if (typeof monitor.setServiceState === 'function') monitor.setServiceState(value);
      else monitor.setMarketState(value);
    } catch { unavailable(); }
  }
  function setAsset(value) {
    const next = assetSymbol(value);
    if (stopped || next === selectedAsset) return;
    selectedAsset = next; latestState = null;
    try { monitor?.setAsset?.(next); } catch { unavailable(); }
  }
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module[method](doc, win, {...mountOptions, asset: selectedAsset, onAssetChange});
    if (method !== 'mountPerformance') monitor?.setAsset?.(selectedAsset);
    setMarketState(latestState);
    return monitor;
  }).catch(() => { if (!stopped) unavailable(); return null; });
  function dispose() { stopped = true; monitor?.dispose(); }
  win.addEventListener('pagehide', dispose);
  return {ready, setMarketState, setAsset, getAsset: () => selectedAsset,
    getReport: () => monitor?.getReport?.() ?? null,
    refresh: () => stopped ? undefined : monitor?.refresh?.(),
    poll: () => stopped ? undefined : typeof monitor?.poll === 'function' ? monitor.poll() : monitor?.refresh?.(), dispose};
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
export function mountOptionalValidation(doc = document, win = window, {importModule = () => import('./validation.mjs'), asset = 'BTC'} = {}) {
  return mountOptionalPanel(doc, win, {panel: 'validation-panel', status: 'validation-status', description: 'walk-forward validation',
    method: 'mountValidation', importModule, asset});
}
export function mountOptionalPerformance(doc = document, win = window, {importModule = () => import('./performance.mjs'), autoRefresh = true, autoLoad = true} = {}) {
  return mountOptionalPanel(doc, win, {panel: 'performance-panel', status: 'performance-status', description: 'execution P&L and performance charts',
    method: 'mountPerformance', importModule, mountOptions: {autoRefresh, autoLoad}});
}

// Neutral research has its own source and session. The global single-contract
// selector and manual order state must not alter that independent experiment.
export function mountOptionalNeutral(doc = document, win = window, {importModule = () => import('./neutral.mjs')} = {}) {
  if (typeof doc?.getElementById !== 'function' || !doc.getElementById('neutral-root')) return null;
  let monitor = null, stopped = false;
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module.mountNeutral(doc, win);
    return monitor;
  }).catch(() => {
    if (!stopped) {
      const target = doc.getElementById('neutral-status') || doc.getElementById('neutral-root');
      if (target) target.textContent = 'Restart the local Kalshi console and refresh this page to load the neutral-spread monitor.';
    }
    return null;
  });
  function dispose() { if (!stopped) { stopped = true; monitor?.dispose?.(); } }
  win.addEventListener?.('pagehide', dispose);
  return {ready, refresh: () => monitor?.refresh?.(), getReport: () => monitor?.getReport?.() ?? null, dispose};
}

// Asset history is independent of the selected current trading contract. Large
// historical reads start only when this view is first opened or refreshed.
export function mountOptionalTimeSeries(doc = document, win = window, {importModule = () => import('./timeseries.mjs')} = {}) {
  const panel = doc?.getElementById?.('workspace-history');
  if (!panel || !doc.getElementById('timeseries-root')) return null;
  let monitor = null, stopped = false, started = false;
  function reveal() {
    if (!stopped && monitor && !started && !panel.hidden) { started = true; return monitor.refresh(); }
  }
  doc.addEventListener?.('workspace-tab-change', reveal);
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module.mountTimeSeries(doc, win, {autoLoad: false});
    const status = doc.getElementById('timeseries-load-status'); if (status) status.hidden = true;
    reveal(); return monitor;
  }).catch(() => {
    if (stopped) return null;
    const status = doc.getElementById('timeseries-load-status') || doc.getElementById('timeseries-root');
    if (status) { status.hidden = false; status.textContent = 'Restart the local Kalshi console and refresh this page to enable historical analysis.'; }
    return null;
  });
  function dispose() { if (stopped) return; stopped = true; doc.removeEventListener?.('workspace-tab-change', reveal); monitor?.dispose?.(); }
  win.addEventListener?.('pagehide', dispose);
  return {ready, refresh: () => { started = true; return monitor?.refresh?.(); }, getReport: () => monitor?.getReport?.() ?? null, dispose};
}

// Health inspects existing metadata and the recorder; it does not start capture.
export function mountOptionalHealth(doc = document, win = window, {importModule = () => import('./health.mjs')} = {}) {
  const panel = doc?.getElementById?.('workspace-health');
  if (!panel || !doc.getElementById('health-root')) return null;
  let monitor = null, stopped = false;
  function reveal() {
    if (!stopped && monitor && !panel.hidden && doc.visibilityState !== 'hidden') return monitor.poll();
  }
  doc.addEventListener?.('workspace-tab-change', reveal);
  doc.addEventListener?.('visibilitychange', reveal);
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module.mountHealth(doc, win, {autoLoad: false});
    const status = doc.getElementById('health-load-status'); if (status) status.hidden = true;
    reveal(); return monitor;
  }).catch(() => {
    if (stopped) return null;
    const status = doc.getElementById('health-load-status') || doc.getElementById('health-root');
    if (status) { status.hidden = false; status.textContent = 'Restart the local Kalshi console and refresh this page to enable data health.'; }
    return null;
  });
  function dispose() {
    if (stopped) return; stopped = true;
    doc.removeEventListener?.('workspace-tab-change', reveal); doc.removeEventListener?.('visibilitychange', reveal);
    win.removeEventListener?.('pagehide', dispose); monitor?.dispose?.();
  }
  win.addEventListener?.('pagehide', dispose);
  return {ready, refresh: () => monitor?.refresh?.(), getReport: () => monitor?.getReport?.() ?? null, dispose};
}

export function mountOptionalAssetPerformance(doc = document, win = window, {importModule = () => import('./asset-performance.mjs')} = {}) {
  const panel = doc?.getElementById?.('workspace-portfolio');
  if (!panel || !doc.getElementById('asset-performance-root')) return null;
  let monitor = null, stopped = false;
  function reveal() {
    if (!stopped && monitor && !panel.hidden && doc.visibilityState !== 'hidden') return monitor.poll();
  }
  doc.addEventListener?.('workspace-tab-change', reveal);
  doc.addEventListener?.('visibilitychange', reveal);
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module.mountAssetPerformance(doc, win, {autoLoad: false});
    const status = doc.getElementById('asset-performance-load-status'); if (status) status.hidden = true;
    reveal(); return monitor;
  }).catch(() => {
    if (stopped) return null;
    const status = doc.getElementById('asset-performance-load-status') || doc.getElementById('asset-performance-root');
    if (status) { status.hidden = false; status.textContent = 'Restart the local Kalshi console and refresh this page to enable asset comparison.'; }
    return null;
  });
  function dispose() {
    if (stopped) return; stopped = true;
    doc.removeEventListener?.('workspace-tab-change', reveal); doc.removeEventListener?.('visibilitychange', reveal);
    win.removeEventListener?.('pagehide', dispose); monitor?.dispose?.();
  }
  win.addEventListener?.('pagehide', dispose);
  return {ready, refresh: () => monitor?.refresh?.(), getReport: () => monitor?.getReport?.() ?? null, dispose};
}

function mountEvidencePanel(doc, win, {tab, prefix, method, importModule}) {
  const panel = doc?.getElementById?.(`workspace-${tab}`);
  if (!panel || !doc.getElementById(`${prefix}-root`)) return null;
  let monitor = null, stopped = false, serviceState;
  function reveal() {
    if (!stopped && monitor && !panel.hidden && doc.visibilityState !== 'hidden') return monitor.poll();
  }
  doc.addEventListener?.('workspace-tab-change', reveal);
  doc.addEventListener?.('visibilitychange', reveal);
  const ready = Promise.resolve().then(importModule).then(module => {
    if (stopped) return null;
    monitor = module[method](doc, win, {autoLoad: false});
    if (serviceState !== undefined) monitor?.setServiceState?.(serviceState);
    const status = doc.getElementById(`${prefix}-load-status`); if (status) status.hidden = true;
    reveal(); return monitor;
  }).catch(() => {
    if (stopped) return null;
    const status = doc.getElementById(`${prefix}-load-status`) || doc.getElementById(`${prefix}-root`);
    if (status) { status.hidden = false; status.textContent = 'Restart the local Kalshi console and refresh this page to enable this evidence panel.'; }
    return null;
  });
  function dispose() {
    if (stopped) return; stopped = true;
    doc.removeEventListener?.('workspace-tab-change', reveal); doc.removeEventListener?.('visibilitychange', reveal);
    win.removeEventListener?.('pagehide', dispose); monitor?.dispose?.();
  }
  win.addEventListener?.('pagehide', dispose);
  return {ready, refresh: () => monitor?.refresh?.(), getReport: () => monitor?.getReport?.() ?? null,
    setServiceState: state => {serviceState = state; return monitor?.setServiceState?.(state);}, dispose};
}

export function mountOptionalModelEvidence(doc = document, win = window, {importModule = () => import('./model-evidence.mjs')} = {}) {
  return mountEvidencePanel(doc, win, {tab: 'research', prefix: 'model-evidence', method: 'mountModelEvidence', importModule});
}

export function mountOptionalEquityHistory(doc = document, win = window, {importModule = () => import('./equity-history.mjs')} = {}) {
  return mountEvidencePanel(doc, win, {tab: 'portfolio', prefix: 'equity-history', method: 'mountEquityHistory', importModule});
}

// Charts retain their own data, units, freshness and zoom policy. This shared
// shell adds inspection and presentation controls without requesting data.
export function mountChartWorkbench(doc = document, win = window) {
  if (typeof doc?.querySelectorAll !== 'function' || typeof doc?.createElement !== 'function' || !doc.body) return null;
  const states = new Map(); let stopped = false, queued = false, expanded = null;
  const listen = (state, node, name, handler) => { node.addEventListener(name, handler); state.listeners.push(() => node.removeEventListener?.(name, handler)); };
  const text = (value, maximum = 3000) => String(value || '').slice(0, maximum);
  function chartName(svg) {
    const heading = svg.closest('article, figure, .greeks-chart-panel')?.querySelector('h3, h4, figcaption')?.textContent;
    return text(heading || svg.getAttribute('aria-label') || 'Chart', 300);
  }
  function detail(node) {
    return text(node.getAttribute?.('data-chart-detail') || node.querySelector?.('title')?.textContent);
  }
  function visiblePoints(state) {
    return state.points.filter(node => !node.closest?.('.chart-series-hidden'));
  }
  function selectionKey(node) {
    if (!node) return null;
    return `${node.closest?.('[data-chart-series]')?.getAttribute('data-chart-series') || ''}\u0000${detail(node)}`;
  }
  function metadata(state, name) {
    return state.svg.getAttribute(name) ?? state.svg.querySelector(`[${name}]`)?.getAttribute(name) ?? null;
  }
  function anchor(node) {
    const shape = ['circle', 'rect'].includes(node.tagName?.toLowerCase()) ? node : node.querySelector?.('circle, rect');
    if (!shape) return null;
    const number = name => shape.hasAttribute(name) ? Number(shape.getAttribute(name)) : NaN;
    const x = shape.tagName.toLowerCase() === 'circle' ? number('cx') : number('x') + number('width') / 2;
    const y = shape.tagName.toLowerCase() === 'circle' ? number('cy') : number('y') + number('height') / 2;
    return Number.isFinite(x) && Number.isFinite(y) ? {x, y} : null;
  }
  function plotBounds(state) {
    const parts = (metadata(state, 'data-chart-bounds') || '').trim().split(/\s+/).map(Number);
    return parts.length === 4 && parts.every(Number.isFinite) && parts[2] > parts[0] && parts[3] > parts[1]
      ? {left: parts[0], top: parts[1], right: parts[2], bottom: parts[3]} : null;
  }
  function pointerPosition(state, event) {
    if (!Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return null;
    try {
      const point = state.svg.createSVGPoint?.(), matrix = state.svg.getScreenCTM?.();
      if (point && matrix) { point.x = event.clientX; point.y = event.clientY; return point.matrixTransform(matrix.inverse()); }
    } catch { /* Detached or hidden plots have no invertible screen matrix. */ }
    const box = state.svg.getBoundingClientRect?.(), view = (state.svg.getAttribute('viewBox') || '').split(/\s+/).map(Number);
    if (!box?.width || !box.height || view.length !== 4 || !view.every(Number.isFinite)) return null;
    return {x: view[0] + (event.clientX - box.left) * view[2] / box.width,
      y: view[1] + (event.clientY - box.top) * view[3] / box.height};
  }
  function crosshair(state, node) {
    const at = node && anchor(node), bounds = plotBounds(state);
    if (metadata(state, 'data-chart-interaction-owner') === 'performance' || !at || !bounds) {
      state.crosshair?.setAttribute('visibility', 'hidden'); return;
    }
    if (!state.crosshair?.parentNode) {
      if (typeof doc.createElementNS !== 'function') return;
      state.crosshair = doc.createElementNS('http://www.w3.org/2000/svg', 'line');
      state.crosshair.setAttribute('data-chart-overlay', 'true'); state.crosshair.setAttribute('aria-hidden', 'true');
      state.crosshair.setAttribute('pointer-events', 'none'); state.crosshair.setAttribute('class', 'chart-workbench-crosshair');
      state.svg.appendChild(state.crosshair);
    }
    const x = Math.max(bounds.left, Math.min(bounds.right, at.x));
    for (const [key, value] of Object.entries({x1: x, x2: x, y1: bounds.top, y2: bounds.bottom, visibility: 'visible'}))
      state.crosshair.setAttribute(key, value);
  }
  function unpin(state) {
    state.pinned = false; state.pin.textContent = 'Pin inspection'; state.pin.setAttribute('aria-pressed', 'false');
  }
  const stalePrefix = state => state.svg.getAttribute('data-stale') === 'true' ? 'Last known snapshot: ' : '';
  function inspect(state, node, {reveal = false, information = null} = {}) {
    if (!node || node.closest?.('.chart-series-hidden')) return;
    information ??= detail(node);
    if (!information) return;
    const value = stalePrefix(state) + information;
    state.selected?.removeAttribute?.('data-chart-selected');
    state.selected = node; node.setAttribute('data-chart-selected', 'true');
    if (state.inspector.textContent !== value) state.inspector.textContent = value;
    state.inspector.setAttribute('data-has-selection', 'true');
    state.pin.disabled = false; crosshair(state, node);
    if (reveal) node.scrollIntoView?.({block: 'nearest', inline: 'nearest', behavior: 'auto'});
  }
  function setExpanded(state, value) {
    if (value && expanded && expanded !== state) setExpanded(expanded, false);
    if (value) {
      state.previousFocus = doc.activeElement; expanded = state;
      state.wrapper.classList.add('chart-workbench-expanded');
      state.wrapper.setAttribute('role', 'dialog'); state.wrapper.setAttribute('aria-modal', 'true');
      state.wrapper.setAttribute('aria-label', state.name);
      state.title.textContent = state.name;
      doc.body.classList.add('chart-workbench-open');
      state.expand.textContent = 'Close expanded view'; state.expand.setAttribute('aria-expanded', 'true');
      state.expand.setAttribute('aria-label', `Close expanded view of ${state.name}`);
      state.expand.focus?.();
    } else {
      state.wrapper.classList.remove('chart-workbench-expanded');
      state.wrapper.removeAttribute('role'); state.wrapper.removeAttribute('aria-modal'); state.wrapper.removeAttribute('aria-label');
      state.expand.textContent = 'Expand'; state.expand.setAttribute('aria-expanded', 'false');
      state.expand.setAttribute('aria-label', `Expand ${state.name}`);
      state.title.textContent = 'Chart controls';
      if (expanded === state) { expanded = null; doc.body.classList.remove('chart-workbench-open'); }
      if (state.previousFocus?.isConnected) state.previousFocus.focus?.();
    }
  }
  function applySeries(state) {
    for (const node of state.svg.querySelectorAll('[data-chart-series]'))
      node.classList.toggle('chart-series-hidden', state.hidden.has(node.getAttribute('data-chart-series')));
    for (const [key, button] of state.seriesButtons) button.setAttribute('aria-pressed', String(!state.hidden.has(key)));
    const points = visiblePoints(state), selectedHidden = state.selected?.closest?.('.chart-series-hidden');
    if (selectedHidden) {
      state.selected.removeAttribute('data-chart-selected'); state.selected = null;
      unpin(state); state.pin.disabled = true; crosshair(state, null);
      state.inspector.textContent = 'Selected series hidden. Hover another plotted point or use Previous / Next point.';
      state.inspector.setAttribute('data-has-selection', 'status');
    }
    state.previous.disabled = points.length === 0; state.next.disabled = points.length === 0;
  }
  function notifySeriesChange(state) {
    const SeriesEvent = typeof win.CustomEvent === 'function' ? win.CustomEvent : globalThis.CustomEvent;
    if (typeof SeriesEvent !== 'function' || typeof state.svg.dispatchEvent !== 'function') return;
    // A renderer may reflow its samples rather than simply hiding their marks.
    // Only user toggles notify it; observer-driven updates must not recurse.
    state.svg.dispatchEvent(new SeriesEvent('chart-series-change', {detail: {hiddenSeries: [...state.hidden]}}));
  }
  function notify(state, name, detail) {
    const ChartEvent = typeof win.CustomEvent === 'function' ? win.CustomEvent : globalThis.CustomEvent;
    if (!stopped && typeof ChartEvent === 'function') state.svg.dispatchEvent(new ChartEvent(name, {detail}));
  }
  function updateDisplayControls(state) {
    let views = [];
    try {
      const values = JSON.parse(metadata(state, 'data-chart-views') || '[]');
      if (Array.isArray(values) && values.length <= 8) {
        const ids = new Set();
        views = values.filter(item => item && typeof item.value === 'string' && /^[a-z][a-z0-9_-]{0,39}$/.test(item.value)
          && typeof item.label === 'string' && item.label.length > 0 && item.label.length <= 80
          && !ids.has(item.value) && ids.add(item.value));
      }
    } catch { /* Malformed optional controls cannot break the underlying plot. */ }
    state.viewLabel.hidden = views.length < 2;
    state.pin.hidden = metadata(state, 'data-chart-interaction-owner') === 'performance';
    const signature = JSON.stringify(views);
    if (signature !== state.viewSignature) {
      for (const option of [...state.view.children]) option.remove();
      for (const item of views) { const option = doc.createElement('option'); option.value = item.value; option.textContent = item.label; state.view.appendChild(option); }
      state.viewSignature = signature;
    }
    const selectedView = metadata(state, 'data-chart-view');
    if (views.some(item => item.value === selectedView)) state.view.value = selectedView;
    else if (views.length) state.view.value = views[0].value;
    const read = name => { const value = metadata(state, name); return value === null ? NaN : Number(value); };
    const low = read('data-chart-x-min'), high = read('data-chart-x-max');
    const enabled = metadata(state, 'data-chart-range-enabled') === 'true' && Number.isFinite(low) && high > low;
    state.rangeControls.hidden = !enabled;
    let start = read('data-chart-range-start'), end = read('data-chart-range-end');
    if (!(start >= 0 && end <= 1 && end > start)) { start = 0; end = 1; }
    state.range = {start, end};
    state.zoomIn.disabled = !enabled || end - start <= .010001;
    state.zoomOut.disabled = !enabled || end - start >= .999999;
    state.panLeft.disabled = !enabled || start <= 0;
    state.panRight.disabled = !enabled || end >= 1;
    state.resetRange.disabled = !enabled || start === 0 && end === 1;
    const count = read('data-chart-visible-count'), total = read('data-chart-total-count');
    state.rangeStatus.textContent = enabled ? `${Math.round((end - start) * 100)}% of range${Number.isSafeInteger(count) && Number.isSafeInteger(total) ? ` · ${count} / ${total} observations` : ''}` : '';
  }
  function changeRange(state, action) {
    let {start, end} = state.range, width = end - start;
    if (action === 'reset') { start = 0; end = 1; }
    else if (action === 'left' || action === 'right') {
      const shift = width * .25 * (action === 'left' ? -1 : 1);
      start = Math.max(0, Math.min(1 - width, start + shift)); end = start + width;
    } else {
      const center = (start + end) / 2; width = Math.max(.01, Math.min(1, width * (action === 'in' ? .5 : 2)));
      start = Math.max(0, Math.min(1 - width, center - width / 2)); end = start + width;
    }
    notify(state, 'chart-range-change', {start, end});
  }
  function nearestPoint(state, event) {
    const at = pointerPosition(state, event), bounds = plotBounds(state);
    if (!at || !bounds || at.x < bounds.left || at.x > bounds.right || at.y < bounds.top || at.y > bounds.bottom) return false;
    const samples = visiblePoints(state).map(node => ({node, at: anchor(node)})).filter(sample => sample.at
      && sample.at.x >= bounds.left && sample.at.x <= bounds.right && sample.at.y >= bounds.top && sample.at.y <= bounds.bottom);
    samples.sort((a, b) => Math.abs(a.at.x - at.x) - Math.abs(b.at.x - at.x) || Math.abs(a.at.y - at.y) - Math.abs(b.at.y - at.y));
    if (!samples.length) return false;
    const first = samples[0];
    // Compare only supplied marks at this same horizontal coordinate. No
    // interpolated price, probability, outcome or time is fabricated here.
    const lines = [detail(first.node)], series = new Set([first.node.closest?.('[data-chart-series]')?.getAttribute('data-chart-series') || '']);
    for (const sample of samples) {
      const key = sample.node.closest?.('[data-chart-series]')?.getAttribute('data-chart-series') || '';
      if (Math.abs(sample.at.x - first.at.x) > .5 || series.has(key)) continue;
      const value = detail(sample.node); if (value && !lines.includes(value)) lines.push(value);
      series.add(key); if (lines.length >= 6) break;
    }
    inspect(state, first.node, {information: lines.join(' | ')}); return true;
  }
  function update(state) {
    const oldName = state.name, oldSelection = selectionKey(state.selected), oldScope = state.scope;
    state.scope = metadata(state, 'data-chart-scope');
    if (oldScope !== state.scope) { state.hidden.clear(); unpin(state); }
    state.name = chartName(state.svg);
    state.freshness.hidden = state.svg.getAttribute('data-stale') !== 'true';
    state.previous.setAttribute('aria-label', `Inspect previous point in ${state.name}`);
    state.next.setAttribute('aria-label', `Inspect next point in ${state.name}`);
    state.expand.setAttribute('aria-label', `${expanded === state ? 'Close expanded view of' : 'Expand'} ${state.name}`);
    if (expanded === state) { state.title.textContent = state.name; state.wrapper.setAttribute('aria-label', state.name); }
    state.selected?.removeAttribute?.('data-chart-selected'); state.selected = null;
    const seen = new Set(); state.points = [];
    const marked = [...state.svg.querySelectorAll('[data-chart-detail], [data-chart-point], [data-performance-event]')];
    for (const candidate of marked.length ? marked : state.svg.querySelectorAll('title')) {
      const node = candidate.tagName?.toLowerCase() === 'title' ? candidate.parentElement : candidate;
      if (!node || node === state.svg || seen.has(node) || node.closest?.('defs, [data-chart-overlay]') || !detail(node)) continue;
      seen.add(node); if (state.points.length < 5000) state.points.push(node);
    }
    const series = new Map();
    for (const node of state.svg.querySelectorAll('[data-chart-series]')) {
      const key = node.getAttribute('data-chart-series');
      if (key && key.length <= 120 && !series.has(key)) series.set(key, text(node.getAttribute('data-chart-label') || key.replaceAll('-', ' '), 80));
    }
    for (const [key, button] of state.seriesButtons) if (!series.has(key)) { button.remove(); state.seriesButtons.delete(key); }
    for (const [key, name] of series) {
      if (state.seriesButtons.has(key)) {
        state.seriesButtons.get(key).textContent = name;
        state.seriesButtons.get(key).setAttribute('aria-label', `Show or hide ${name}`); continue;
      }
      const button = doc.createElement('button'); button.type = 'button'; button.className = 'chart-workbench-series-button'; button.textContent = name;
      button.setAttribute('aria-label', `Show or hide ${name}`);
      button.addEventListener('click', () => {
        if (stopped) return;
        if (state.hidden.has(key)) state.hidden.delete(key); else state.hidden.add(key);
        applySeries(state);
        notifySeriesChange(state);
      });
      state.seriesButtons.set(key, button); state.series.appendChild(button);
    }
    state.series.hidden = series.size === 0;
    state.inspector.textContent = stalePrefix(state) + (state.points.length
      ? 'Hover or tap a plotted point, or focus the chart and use Left / Right to inspect its values.'
      : 'No inspectable points in this view. Availability and coverage are reported below the chart.');
    state.inspector.setAttribute('data-has-selection', state.points.length ? 'false' : 'unavailable');
    state.pin.disabled = true; crosshair(state, null); updateDisplayControls(state);
    applySeries(state);
    // Retain an inspected observation only when its series, full evidence text
    // and chart scope still exist. A poll must not substitute a newer sample.
    if (oldName === state.name && oldScope === state.scope && oldSelection !== null) {
      const restored = visiblePoints(state).find(point => selectionKey(point) === oldSelection);
      if (restored) inspect(state, restored);
      else unpin(state);
    } else unpin(state);
  }
  function create(svg) {
    const wrapper = doc.createElement('div'); wrapper.className = 'chart-workbench';
    const toolbar = doc.createElement('div'); toolbar.className = 'chart-workbench-toolbar'; toolbar.setAttribute('role', 'group'); toolbar.setAttribute('aria-label', 'Chart display and inspection');
    const name = chartName(svg);
    const title = doc.createElement('span'); title.className = 'chart-workbench-title'; title.textContent = 'Chart controls'; toolbar.appendChild(title);
    const freshness = doc.createElement('span'); freshness.className = 'chart-workbench-freshness';
    freshness.textContent = 'Last known data'; freshness.hidden = true; toolbar.appendChild(freshness);
    const button = (label, aria) => { const node = doc.createElement('button'); node.type = 'button'; node.className = 'chart-workbench-button'; node.textContent = label; node.setAttribute('aria-label', aria); toolbar.appendChild(node); return node; };
    const expand = button('Expand', `Expand ${name}`); expand.setAttribute('aria-expanded', 'false');
    const previous = button('Previous point', `Inspect previous point in ${name}`), next = button('Next point', `Inspect next point in ${name}`);
    const pin = button('Pin inspection', `Pin inspected values in ${name}`); pin.setAttribute('aria-pressed', 'false'); pin.disabled = true;
    const viewLabel = doc.createElement('label'); viewLabel.className = 'chart-workbench-view'; viewLabel.textContent = 'View ';
    const view = doc.createElement('select'); view.setAttribute('aria-label', `Plot view for ${name}`); viewLabel.appendChild(view); toolbar.appendChild(viewLabel); viewLabel.hidden = true;
    const rangeControls = doc.createElement('div'); rangeControls.className = 'chart-workbench-range'; rangeControls.setAttribute('role', 'group'); rangeControls.setAttribute('aria-label', `Data range for ${name}`); rangeControls.hidden = true;
    const rangeButton = label => { const node = doc.createElement('button'); node.type = 'button'; node.className = 'chart-workbench-button'; node.textContent = label; node.setAttribute('aria-label', `${label}: ${name}`); rangeControls.appendChild(node); return node; };
    const zoomIn = rangeButton('Zoom in'), zoomOut = rangeButton('Zoom out'), panLeft = rangeButton('Earlier'), panRight = rangeButton('Later'), resetRange = rangeButton('Reset range');
    const rangeStatus = doc.createElement('span'); rangeStatus.className = 'chart-workbench-range-status'; rangeControls.appendChild(rangeStatus); toolbar.appendChild(rangeControls);
    const series = doc.createElement('div'); series.className = 'chart-workbench-series'; series.setAttribute('role', 'group'); series.setAttribute('aria-label', 'Visible chart series'); toolbar.appendChild(series);
    const hint = doc.createElement('span'); hint.className = 'chart-workbench-hint';
    hint.textContent = 'Hover inside the plot to inspect supplied samples; pin to keep a value. Left / Right: inspect points. Home / End: first / last. Available views and zoom recalculate the data range.';
    toolbar.appendChild(hint);
    const plot = doc.createElement('div'); plot.className = 'chart-workbench-plot';
    const inspector = doc.createElement('p'); inspector.className = 'chart-workbench-inspector'; inspector.setAttribute('role', 'status'); inspector.setAttribute('aria-live', 'polite');
    const state = {svg, wrapper, toolbar, title, freshness, expand, previous, next, pin, pinned: false, viewLabel, view, rangeControls, zoomIn, zoomOut, panLeft, panRight, resetRange, rangeStatus,
      series, inspector, name, points: [], selected: null, scope: null,
      priorKeyShortcuts: svg.getAttribute('aria-keyshortcuts'),
      hidden: new Set(), seriesButtons: new Map(), listeners: []};
    states.set(svg, state); svg.parentNode.insertBefore(wrapper, svg); wrapper.appendChild(toolbar); wrapper.appendChild(plot); plot.appendChild(svg); wrapper.appendChild(inspector);
    if (!svg.hasAttribute('tabindex')) svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-keyshortcuts', 'ArrowLeft ArrowRight Home End');
    listen(state, expand, 'click', () => { if (!stopped) setExpanded(state, expanded !== state); });
    listen(state, pin, 'click', () => { if (stopped || !state.selected) return; state.pinned = !state.pinned; pin.textContent = state.pinned ? 'Unpin inspection' : 'Pin inspection'; pin.setAttribute('aria-pressed', String(state.pinned)); });
    listen(state, view, 'change', () => notify(state, 'chart:view', {view: view.value}));
    for (const [control, action] of [[zoomIn, 'in'], [zoomOut, 'out'], [panLeft, 'left'], [panRight, 'right'], [resetRange, 'reset']])
      listen(state, control, 'click', () => { if (!control.disabled && !stopped) changeRange(state, action); });
    const step = direction => {
      const points = visiblePoints(state); if (!points.length || stopped) return;
      const index = points.indexOf(state.selected);
      inspect(state, points[index < 0 ? direction > 0 ? 0 : points.length - 1 : (index + direction + points.length) % points.length], {reveal: true});
    };
    listen(state, previous, 'click', () => step(-1)); listen(state, next, 'click', () => step(1));
    const inspectTarget = target => {
      let node = target;
      while (node && node !== svg) { if (state.points.includes(node)) { inspect(state, node); return; } node = node.parentElement; }
    };
    listen(state, svg, 'pointermove', event => {
      if (stopped || event.buttons || state.pinned || metadata(state, 'data-chart-interaction-owner') === 'performance') return;
      if (!nearestPoint(state, event)) inspectTarget(event.target);
    });
    listen(state, svg, 'click', event => { if (!stopped) inspectTarget(event.target); });
    listen(state, svg, 'keydown', event => {
      if (stopped || event.defaultPrevented || event.target !== svg || event.ctrlKey || event.metaKey || event.altKey) return;
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') step(event.key === 'ArrowLeft' ? -1 : 1);
      else {
        const points = visiblePoints(state);
        inspect(state, event.key === 'Home' ? points[0] : points.at(-1), {reveal: true});
      }
    });
    update(state);
  }
  function disposeState(state) {
    if (expanded === state) setExpanded(state, false);
    for (const remove of state.listeners) remove();
    for (const node of state.svg.querySelectorAll('.chart-series-hidden')) node.classList.remove('chart-series-hidden');
    state.selected?.removeAttribute?.('data-chart-selected');
    state.crosshair?.remove();
    if (state.priorKeyShortcuts === null) state.svg.removeAttribute('aria-keyshortcuts');
    else state.svg.setAttribute('aria-keyshortcuts', state.priorKeyShortcuts);
    if (state.wrapper.parentNode) {
      if (doc.body.contains(state.svg)) state.wrapper.parentNode.insertBefore(state.svg, state.wrapper);
      state.wrapper.remove();
    }
    states.delete(state.svg);
  }
  function refresh() {
    if (stopped) return;
    for (const state of states.values()) if (!doc.body.contains(state.svg)) disposeState(state);
    for (const svg of doc.querySelectorAll('svg[role="img"]')) if (svg.parentNode && !states.has(svg)) create(svg);
  }
  const dirty = new Set();
  const observer = typeof win.MutationObserver === 'function' ? new win.MutationObserver(records => {
    let discover = false;
    for (const record of records) {
      if (record.target?.closest?.('[data-chart-overlay], .perf-linked-cursor')) continue;
      const changed = [...(record.addedNodes || []), ...(record.removedNodes || [])];
      if (changed.length && changed.every(node => node.nodeType === 1 && node.matches?.('[data-chart-overlay], .perf-linked-cursor'))) continue;
      const svg = record.target?.closest?.('svg');
      if (svg && states.has(svg)) dirty.add(states.get(svg));
      for (const node of record.addedNodes || []) if (node.nodeType === 1 && (node.matches?.('svg[role="img"]') || node.querySelector?.('svg[role="img"]'))) discover = true;
      if (record.removedNodes?.length) discover = true;
    }
    if ((!dirty.size && !discover) || queued || stopped) return;
    queued = true; Promise.resolve().then(() => {
      queued = false; if (stopped) return;
      for (const state of dirty) if (states.has(state.svg) && doc.body.contains(state.svg)) update(state);
      dirty.clear(); refresh();
    });
  }) : null;
  const keyboard = event => {
    if (!expanded || stopped) return;
    if (event.key === 'Escape') { event.preventDefault(); setExpanded(expanded, false); return; }
    if (event.key !== 'Tab') return;
    const focusable = [...expanded.wrapper.querySelectorAll('button:not([disabled]), select, [tabindex="0"]')].filter(node => !node.closest('[hidden]'));
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && (doc.activeElement === first || !expanded.wrapper.contains(doc.activeElement))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && (doc.activeElement === last || !expanded.wrapper.contains(doc.activeElement))) { event.preventDefault(); first?.focus(); }
  };
  doc.addEventListener?.('keydown', keyboard);
  const workspaceChange = event => {
    const key = event?.detail?.tab;
    if (!expanded || typeof key !== 'string') return;
    const next = doc.getElementById?.(`workspace-${key}`);
    if (next && !next.contains(expanded.wrapper)) setExpanded(expanded, false);
  };
  doc.addEventListener?.('workspace-will-change', workspaceChange);
  refresh(); observer?.observe(doc.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['data-stale', 'data-chart-scope', 'data-chart-view', 'data-chart-range-start', 'data-chart-range-end']});
  function dispose() {
    if (stopped) return; stopped = true; observer?.disconnect(); dirty.clear();
    doc.removeEventListener?.('keydown', keyboard);
    doc.removeEventListener?.('workspace-will-change', workspaceChange);
    for (const state of [...states.values()]) disposeState(state);
  }
  win.addEventListener?.('pagehide', dispose);
  return {refresh, dispose, getChartCount: () => states.size};
}

function mountOptionalWorkspace(doc, win) {
  if (!doc?.getElementById?.('workspace-tabs')) return null;
  let controller = null, stopped = false;
  const ready = import('./workspace.mjs').then(module => {
    if (!stopped) controller = module.mountWorkspaceTabs(doc, win);
    if (!stopped && !controller) throw new Error('Workspace markup is incompatible');
    return controller;
  }).catch(() => {
    if (stopped) return null;
    // Updated static files can reach an older running backend. Keep all views
    // accessible until the console is restarted with the new module routes.
    for (const panel of doc.querySelectorAll?.('[data-workspace-panel]') || []) panel.hidden = false;
    const nav = doc.getElementById('workspace-tabs');
    if (nav) nav.hidden = true;
    const status = doc.getElementById('dashboard-source-summary');
    if (status) status.textContent = 'Restart the local console to enable workspace tabs. All views remain available below.';
    return null;
  });
  return {ready, activate: (...args) => controller?.activate(...args),
    getActiveTab: () => controller?.getActiveTab(),
    dispose: () => { stopped = true; controller?.dispose(); }};
}

function mountOptionalStrategyStatus(doc, win) {
  if (!doc?.getElementById?.('strategy-status-content')) return null;
  let panel = null, stopped = false;
  const ready = import('./strategy-status.mjs').then(module => {
    if (!stopped) panel = module.mountStrategyStatus(doc, win);
    return panel?.ready;
  }).catch(() => {
    if (stopped) return;
    const status = doc.getElementById('strategy-status-message');
    if (status) status.textContent = 'Restart the local console to enable read-only producer reports.';
  });
  return {ready, refresh: () => panel?.refresh(), dispose: () => { stopped = true; panel?.dispose(); }};
}

export function scopedPollingWindow(doc, win, keys) {
  if (!doc?.getElementById?.('workspace-tabs') || typeof win?.setInterval !== 'function') return win;
  const methods = new Map();
  return new Proxy(win, {get(target, key) {
    if (key === 'dashboardPollingActive') return () => doc.visibilityState !== 'hidden' && keys.some(name => doc.getElementById(`workspace-${name}`)?.hidden === false);
    if (key === 'setInterval') return (callback, delay, ...args) => target.setInterval(() => {
      if (doc.visibilityState !== 'hidden' && keys.some(name => doc.getElementById(`workspace-${name}`)?.hidden === false)) callback(...args);
    }, delay);
    const value = Reflect.get(target, key, target);
    if (typeof value !== 'function' || !['fetch', 'setTimeout', 'clearTimeout', 'clearInterval', 'addEventListener', 'removeEventListener', 'requestAnimationFrame'].includes(key)) return value;
    if (!methods.has(key)) methods.set(key, value.bind(target));
    return methods.get(key);
  }});
}

export function mountPortfolioUpdates(doc, win, panels, {pollMs = 5000, now = Date.now} = {}) {
  const panel = doc?.getElementById?.('workspace-portfolio');
  if (!panel || typeof win?.setInterval !== 'function') return null;
  let stopped = false, pending = null, capabilitiesAt = null, controller = null;
  const active = () => !stopped && !panel.hidden && doc.visibilityState !== 'hidden';
  const status = message => { const node = doc.getElementById('portfolio-sync-status'); if (node) node.textContent = message; };
  async function capabilities() {
    if (typeof win.fetch !== 'function' || capabilitiesAt !== null && now() - capabilitiesAt < 30000) return;
    controller = new AbortController();
    const timeout = win.setTimeout(() => controller?.abort(), 5000);
    try {
      const response = await win.fetch('/api/portfolio-status', {method:'GET', cache:'no-store', credentials:'same-origin', redirect:'error', signal:controller.signal, headers:{Accept:'application/json'}});
      if (!response.ok) return;
      const raw = await response.text(); if (raw.length > 8192) return;
      const value = JSON.parse(raw);
      if (value.kind !== 'kalshi_portfolio_status' || value.version !== 1 || !['paper','demo','live'].includes(value.mode)
          || !['production','demo'].includes(value.environment) || typeof value.account_view_enabled !== 'boolean') return;
      if (!active()) return;
      capabilitiesAt = now();
      const mode = doc.getElementById('mode'), environment = doc.getElementById('environment'), authentication = doc.getElementById('authentication');
      if (mode) mode.textContent = `${value.mode === 'paper' ? 'Paper execution' : value.mode === 'demo' ? 'Demo console' : 'Live console'} / portfolio monitor`;
      if (environment) environment.textContent = `${value.environment === 'production' ? 'Production' : 'Demo'} service; independent REST snapshots`;
      if (authentication) authentication.textContent = `Account reader ${value.account_view_enabled ? 'configured' : 'not configured'}; acquisition status is shown below.`;
      const acquired = new Set();
      const snapshotKey = report => report ? JSON.stringify([report.source, report.environment, report.mode, report.asset, report.fetched_at]) : null;
      await Promise.allSettled(['positions','orders','performance','equityHistory'].map(async name => {
        const before = panels[name]?.getReport?.();
        await (panels[name]?.setServiceState?.(value) ?? panels[name]?.setMarketState?.(value));
        const after = panels[name]?.getReport?.(), age = after?.fetched_at ? now() - Date.parse(after.fetched_at) : Infinity;
        // A source/environment change can acquire its own snapshot. Reuse that
        // completed read for this cycle; unchanged or stale reports still poll.
        if (after && ['ready','partial'].includes(after.status) && !after.refresh_failed && !after.stale && !after.restored
            && age >= -1000 && age <= (name === 'performance' ? 60000 : 30000) && snapshotKey(after) !== snapshotKey(before)) acquired.add(name);
      }));
      return acquired;
    } catch { /* Existing source selections and snapshots remain usable. */ }
    finally { win.clearTimeout(timeout); controller = null; }
  }
  function refresh() {
    if (!active()) return Promise.resolve();
    if (pending) return pending;
    pending = (async () => {
      const acquired = await capabilities(); if (!active()) return;
      await Promise.allSettled(['positions','orders','performance'].map(name => acquired?.has(name) ? undefined : typeof panels[name]?.poll === 'function' ? panels[name].poll() : panels[name]?.refresh?.()));
      if (!active()) return;
      const evidence = ['positions','orders','performance'].map(name => {
        const report = panels[name]?.getReport?.(), at = report?.fetched_at;
        return `${name}: ${at && Number.isFinite(Date.parse(at)) ? `${Math.max(0, Math.round((now()-Date.parse(at))/1000))}s since acquisition` : 'waiting for a valid snapshot'}`;
      });
      status(`REST account and paper snapshots checked every ${pollMs/1000}s while Portfolio is visible. ${evidence.join(' | ')}. Performance acquisition has its own progress and source timestamps.`);
      const connection = doc.getElementById('connection');
      if (connection) { connection.textContent = 'Portfolio REST checks'; connection.className = 'pill muted'; }
    })().finally(() => { pending = null; });
    return pending;
  }
  const timer = win.setInterval(refresh, pollMs);
  const changed = () => { if (active()) refresh(); else controller?.abort(); };
  doc.addEventListener?.('workspace-tab-change', changed); doc.addEventListener?.('visibilitychange', changed); win.addEventListener?.('focus', changed);
  function dispose() { if (stopped) return; stopped = true; controller?.abort(); win.clearInterval(timer); doc.removeEventListener?.('workspace-tab-change', changed); doc.removeEventListener?.('visibilitychange', changed); win.removeEventListener?.('focus', changed); }
  const ready = refresh();
  return {ready, refresh, dispose};
}

export function mountDashboard(doc = document, win = window, {asset = 'BTC', mounts = {}} = {}) {
  const factories = {analytics: mountAnalytics, live: mountLive, positions: mountPositions, orders: mountOrders,
    algorithm: mountOptionalAlgorithm, greeks: mountOptionalGreeks, probability: mountOptionalProbability,
    validation: mountOptionalValidation, performance: mountOptionalPerformance, neutral: mountOptionalNeutral, timeseries: mountOptionalTimeSeries, health: mountOptionalHealth, assetPerformance: mountOptionalAssetPerformance,
    modelEvidence: mountOptionalModelEvidence, equityHistory: mountOptionalEquityHistory,
    workbench: mountChartWorkbench, workspace: mountOptionalWorkspace, strategyStatus: mountOptionalStrategyStatus, ...mounts};
  let selectedAsset = assetSymbol(asset), stopped = false, live = null;
  const panels = {};
  const scoped = (...keys) => scopedPollingWindow(doc, win, keys);
  panels.workspace = factories.workspace(doc, win);
  panels.strategyStatus = factories.strategyStatus(doc, scoped('strategies'));
  function setAsset(value) {
    const next = assetSymbol(value);
    if (stopped || next === selectedAsset) return;
    selectedAsset = next;
    // Clear research panels before the selected quote request can complete.
    for (const name of ['analytics', 'algorithm', 'greeks', 'probability', 'validation']) panels[name]?.setAsset?.(next);
    return live?.setAsset?.(next);
  }
  panels.analytics = factories.analytics(doc, scoped('market'), {asset: selectedAsset, onAssetChange: setAsset});
  panels.positions = factories.positions(doc, win, {autoRefresh: false, autoLoad: false, pollMs: 5000});
  panels.orders = factories.orders(doc, win, {autoRefresh: false, autoLoad: false, pollMs: 5000, onCancel: value => {
    live?.invalidateReview('Order cancellation requested - review again');
    if (value.phase === 'finished') return Promise.allSettled([panels.positions?.refresh(), live?.refresh()]);
  }});
  panels.algorithm = factories.algorithm(doc, scoped('strategies'), {asset: selectedAsset, onAssetChange: setAsset});
  panels.greeks = factories.greeks(doc, scoped('models'), {asset: selectedAsset});
  panels.probability = factories.probability(doc, scoped('models'), {asset: selectedAsset});
  panels.validation = factories.validation(doc, scoped('research'), {asset: selectedAsset});
  panels.performance = factories.performance(doc, win, {autoRefresh: false, autoLoad: false});
  panels.neutral = factories.neutral(doc, scoped('research'));
  panels.timeseries = factories.timeseries(doc, win);
  panels.health = factories.health(doc, scoped('health'));
  panels.assetPerformance = factories.assetPerformance(doc, scoped('portfolio'));
  panels.modelEvidence = factories.modelEvidence(doc, scoped('research'));
  panels.equityHistory = factories.equityHistory(doc, scoped('portfolio'));
  live = factories.live(doc, scoped('market','models','strategies','trade'), {asset: selectedAsset,
    onState: state => {
      panels.analytics?.setMarketState(state); panels.positions?.setServiceState(state); panels.orders?.setServiceState(state);
      panels.algorithm?.setMarketState(state); panels.greeks?.setMarketState(state); panels.probability?.setMarketState(state);
      panels.performance?.setMarketState?.(state); panels.equityHistory?.setServiceState?.(state);
    },
    onExecution: () => Promise.allSettled([panels.positions?.refresh(), panels.orders?.refresh(), panels.performance?.refresh?.()])});
  panels.live = live;
  panels.portfolioUpdates = mountPortfolioUpdates(doc, win, panels);
  const catchup = () => {
    if (stopped || doc.visibilityState === 'hidden') return;
    const tab = panels.workspace?.getActiveTab?.();
    const relevant = {market:['live','analytics'], models:['live','greeks','probability'], strategies:['live','algorithm','strategyStatus'], research:['validation','neutral'], trade:['live']}[tab] || [];
    for (const name of relevant) panels[name]?.refresh?.();
  };
  doc.addEventListener?.('workspace-tab-change', catchup); doc.addEventListener?.('visibilitychange', catchup);
  panels.workbench = factories.workbench(doc, win);
  for (const name of ['analytics', 'algorithm', 'greeks', 'probability', 'validation']) panels[name]?.setAsset?.(selectedAsset);
  function dispose() { if (stopped) return; stopped = true; doc.removeEventListener?.('workspace-tab-change', catchup); doc.removeEventListener?.('visibilitychange', catchup); for (const panel of Object.values(panels)) panel?.dispose?.(); }
  win.addEventListener('pagehide', dispose);
  return {panels, setAsset, getAsset: () => selectedAsset, dispose};
}
if (typeof document !== 'undefined' && typeof window !== 'undefined') mountDashboard();
