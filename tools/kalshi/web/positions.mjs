// Account/paper snapshots, local sizing sensitivity, and explicitly clicked
// cancellations. Polling and rendering never submit a mutation.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const MANUAL_TICKER = /^KXBTC15M-[A-Za-z0-9_.:-]{1,119}$/;
const ACCOUNT_TICKER = /^KX(?:BTC|ETH|SOL|XRP|GOLD)15M-[A-Za-z0-9_.:-]{1,119}$/;
const supportedTicker = (ticker, source) => typeof ticker === 'string' && ticker.length <= 128
  && (source === 'kalshi_account' ? ACCOUNT_TICKER : MANUAL_TICKER).test(ticker);
const label = (value, name, max = 1000) => {
  if (typeof value !== 'string' || value.length > max || /[\u0000-\u001f\u007f]/u.test(value)) fail(`Invalid ${name}.`);
  return value;
};
function number(value, name, {signed = false, max = 1e12, positive = false, digits = 12} = {}) {
  if (typeof value !== 'string' || !new RegExp(`^${signed ? '-?' : ''}(0|[1-9][0-9]{0,12})(\\.[0-9]{1,${digits}})?$`).test(value)) fail(`Invalid ${name}.`);
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || Math.abs(parsed) > max || (positive && parsed <= 0)) fail(`Invalid ${name}.`);
  return value;
}
const nullable = (value, name, options) => value === null ? null : number(value, name, options);
function timestamp(value) {
  label(value, 'position timestamp', 48);
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) fail('Invalid position timestamp.');
  if (new Date(Date.parse(value)).toISOString().slice(0, 19) !== value.slice(0, 19)) fail('Invalid position timestamp.');
  return value;
}
function acquisition(raw, result) {
  for (const key of ['refreshing', 'refresh_failed']) if (raw[key] !== undefined) {
    if (typeof raw[key] !== 'boolean') fail('Invalid portfolio refresh metadata.');
    result[key] = raw[key];
  }
  return result;
}
export function validatePositionReport(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_position_report' || raw.version !== 1
      || !['paper', 'demo', 'live'].includes(raw.mode) || !['production', 'demo'].includes(raw.environment)
      || raw.mode === 'live' && raw.environment !== 'production' || raw.mode === 'demo' && raw.environment !== 'demo'
      || !['paper_journal', 'kalshi_account'].includes(raw.source)
      || !['ready', 'partial', 'unavailable'].includes(raw.status) || !object(raw.summary)
      || !Array.isArray(raw.positions) || raw.positions.length > 1000) fail('Unsupported position report.');
  const result = {kind: raw.kind, version: 1, mode: raw.mode, environment: raw.environment, source: raw.source,
    status: raw.status, fetched_at: raw.fetched_at === null ? null : timestamp(raw.fetched_at),
    reason: label(raw.reason, 'position status'), summary: {}, positions: []};
  for (const key of ['cash_balance', 'portfolio_value', 'fees_paid']) result.summary[key] = nullable(raw.summary[key], key);
  for (const key of ['realized_pnl', 'unrealized_pnl']) result.summary[key] = nullable(raw.summary[key], key, {signed: true});
  if (raw.holdings_scope !== undefined || raw.accounting_scope !== undefined) {
    if (raw.source !== 'kalshi_account'
        || !(raw.holdings_scope === 'current_nonzero_unsettled' && raw.accounting_scope === 'separate_performance_history'
          || raw.holdings_scope === 'recent_all_settlement_states' && raw.accounting_scope === 'recent_position_rows')) fail('Invalid position accounting scope.');
    result.holdings_scope = raw.holdings_scope; result.accounting_scope = raw.accounting_scope;
    if (raw.accounting_scope === 'separate_performance_history' && (result.summary.realized_pnl !== null || result.summary.fees_paid !== null))
      fail('Current holdings cannot claim broad realized P&L or fee totals.');
  }
  for (const key of ['open_count', 'unknown_intent_count']) {
    const value = raw.summary[key] ?? (key === 'unknown_intent_count' ? 0 : null);
    if (!Number.isSafeInteger(value) || value < 0 || value > 100000) fail('Invalid position count.');
    result.summary[key] = value;
  }
  if (raw.status === 'unavailable' && (raw.positions.length || raw.fetched_at !== null)) fail('Unavailable position report retains live data.');
  if (raw.status !== 'unavailable' && raw.fetched_at === null) fail('Position report has no acquisition time.');
  result.positions = raw.positions.map(row => {
    if (!object(row) || !['yes', 'no'].includes(row.outcome)) fail('Invalid position row.');
    const parsed = {ticker: label(row.ticker, 'contract ticker', 128), outcome: row.outcome,
      quantity: number(row.quantity, 'quantity', {max: 1e8}),
      status: label(row.status, 'position state', 100), mark_basis: label(row.mark_basis, 'mark basis', 150),
      warning: row.warning === null ? null : label(row.warning, 'position warning')};
    if (!supportedTicker(parsed.ticker, raw.source)) fail('Invalid position ticker.');
    for (const key of ['cost_basis', 'marked_value', 'fees_paid']) parsed[key] = nullable(row[key], key);
    for (const key of ['average_entry_price', 'mark_price']) parsed[key] = nullable(row[key], key, {max: 1});
    for (const key of ['unrealized_pnl', 'realized_pnl']) parsed[key] = nullable(row[key], key, {signed: true});
    return parsed;
  });
  return acquisition(raw, result);
}

export function validateOrderReport(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_order_report' || raw.version !== 1
      || !['paper', 'demo', 'live'].includes(raw.mode) || !['production', 'demo'].includes(raw.environment)
      || raw.mode === 'live' && raw.environment !== 'production' || raw.mode === 'demo' && raw.environment !== 'demo'
      || !['paper_journal', 'kalshi_account'].includes(raw.source)
      || !['ready', 'partial', 'unavailable'].includes(raw.status)
      || !Array.isArray(raw.orders) || raw.orders.length > 1000) fail('Unsupported order report.');
  const result = {kind: raw.kind, version: 1, mode: raw.mode, environment: raw.environment, source: raw.source,
    status: raw.status, fetched_at: raw.fetched_at === null ? null : timestamp(raw.fetched_at),
    reason: label(raw.reason, 'order report status'), orders: []};
  if (raw.status === 'unavailable' && (raw.orders.length || raw.fetched_at !== null)) fail('Unavailable order report retains live data.');
  if (raw.status !== 'unavailable' && raw.fetched_at === null) fail('Order report has no acquisition time.');
  result.orders = raw.orders.map(row => {
    if (!object(row) || !['yes', 'no'].includes(row.outcome) || !['buy', 'sell', 'unknown'].includes(row.action)) fail('Invalid order row.');
    const parsed = {ticker: label(row.ticker, 'order ticker', 128), outcome: row.outcome, action: row.action,
      order_id: row.order_id === null ? null : label(row.order_id, 'order ID', 200),
      client_order_id: row.client_order_id === null ? null : label(row.client_order_id, 'client order ID', 200),
      status: label(row.status, 'order state', 100),
      count: number(row.count, 'order quantity', {positive: true, max: 1e8, digits: 2}),
      fill_count: nullable(row.fill_count, 'filled quantity', {max: 1e8, digits: 2}),
      remaining_count: nullable(row.remaining_count, 'remaining quantity', {max: 1e8, digits: 2}),
      price: nullable(row.price, 'order price', {positive: true, max: 1}),
      created_at: row.created_at === null ? null : timestamp(row.created_at)};
    parsed.order_type = row.order_type ?? 'ioc';
    parsed.expiration_time = row.expiration_time ?? null;
    parsed.cancelable = row.cancelable ?? false;
    if (!['ioc', 'limit', ...(raw.source === 'kalshi_account' ? ['unknown'] : [])].includes(parsed.order_type) || typeof parsed.cancelable !== 'boolean'
        || parsed.expiration_time !== null && (!Number.isSafeInteger(parsed.expiration_time)
          || parsed.expiration_time <= 0 || parsed.expiration_time > 253402300799)
        || parsed.cancelable && (!MANUAL_TICKER.test(parsed.ticker) || parsed.order_type !== 'limit' || !['resting', 'paper_resting'].includes(parsed.status)
          || !parsed.client_order_id || !/^[A-Za-z0-9_.:-]{1,200}$/.test(parsed.client_order_id))) fail('Invalid resting-order controls.');
    if (!supportedTicker(parsed.ticker, raw.source)
        || raw.source === 'kalshi_account' && (parsed.fill_count === null || parsed.remaining_count === null)
        || Number(parsed.price) >= 1 || (parsed.fill_count !== null && Number(parsed.fill_count) > Number(parsed.count))
        || (parsed.remaining_count !== null && Number(parsed.remaining_count) > Number(parsed.count))
        || (parsed.fill_count !== null && parsed.remaining_count !== null && Number(parsed.fill_count) + Number(parsed.remaining_count) > Number(parsed.count) + 1e-8)) fail('Inconsistent order quantities or price.');
    return parsed;
  });
  return acquisition(raw, result);
}

export function validateTuning(bankroll, fraction, limits) {
  const selected = {bankroll: number(bankroll.trim(), 'allocated bankroll', {max: 1e6, positive: true, digits: 4}),
    kelly_fraction: number(fraction.trim(), 'Kelly multiplier', {max: 1, positive: true, digits: 6})};
  if (!limits || Number(selected.bankroll) > Number(limits.bankroll)
      || Number(selected.kelly_fraction) > Number(limits.kelly_fraction)) fail('Chosen sizing exceeds the startup policy ceiling.');
  return selected;
}

export function kellySensitivity(state, probability, bankroll, chosenFraction, requestedOutcome = 'auto', order = {order_type: 'ioc'}) {
  if (!['auto', 'yes', 'no'].includes(requestedOutcome)) fail('Choose Auto, YES or NO.');
  if (!state?.market || state.market_status !== 'ready') return null;
  if (state.manual_order_supported === false || state.selected_asset && state.selected_asset !== 'BTC'
      || state.market.ticker !== undefined && !MANUAL_TICKER.test(state.market.ticker)) return null;
  const p = Number(number(probability, 'probability', {max: 1, digits: 6}));
  const wealth = Number(number(bankroll, 'allocated bankroll', {positive: true, max: 1e6, digits: 4}));
  const chosen = Number(number(chosenFraction, 'Kelly multiplier', {positive: true, max: 1, digits: 6}));
  const market = state.market, limits = state.limits;
  if (!['limit', 'ioc'].includes(order.order_type) || order.order_type === 'limit' && requestedOutcome === 'auto') fail('Choose an explicit side for a resting limit.');
  const limitPrice = order.order_type === 'limit' ? number(order.limit_price, 'limit price', {positive: true, max: 1, digits: 4}) : null;
  if (limitPrice !== null && Number(limitPrice) >= 1) fail('Limit price must be below one dollar.');
  const possibilities = [['yes', p], ['no', 1 - p]].map(([outcome, win]) => {
    if (requestedOutcome !== 'auto' && outcome !== requestedOutcome) return null;
    const ask = limitPrice ?? market[`${outcome}_ask`], liquidity = order.order_type === 'limit' ? limits.max_contracts : market[`${outcome}_available`];
    if (ask === null || liquidity === null) return null;
    const cost = Number(ask) + Number(market.fee_reserve), edge = win - cost;
    if (cost <= 0 || cost >= 1 || edge < Number(limits.min_edge)) return null;
    return {outcome, cost, edge, liquidity: Math.floor(Number(liquidity)), full_fraction: Math.max(0, edge / (1 - cost))};
  }).filter(Boolean).sort((a, b) => b.edge - a.edge || b.outcome.localeCompare(a.outcome));
  if (!possibilities.length) return {outcome: null, rows: []};
  const best = possibilities[0];
  const remaining = Math.max(0, Number(limits.max_total_cost) - Number(state.journal.committed_cost));
  const base = state.mode === 'paper' ? Math.min(wealth, remaining) : wealth;
  return {outcome: best.outcome, full_fraction: best.full_fraction, rows:
    [...new Set([.1, .125, .25, .5, 1, chosen])].sort((a, b) => a - b).map(multiplier => {
      const target = base * multiplier * best.full_fraction;
      const budget = Math.min(target, Number(limits.max_order_cost), Number(limits.max_market_cost), remaining);
      const count = Math.min(limits.max_contracts, best.liquidity, Math.floor(Math.max(0, budget) / best.cost + 1e-10));
      return {multiplier, count, reserved_cost: count * best.cost,
        applied_fraction: multiplier * best.full_fraction,
        policy_allowed: multiplier <= Number(limits.kelly_fraction) && wealth <= Number(limits.bankroll),
        selected: multiplier === chosen};
    })};
}

const money = value => value === null ? 'Unavailable' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
// Retained values belong to the display only. They never become a current report.
const displayedAt = new WeakMap();
function markData(node, stale, at = null) {
  if (!node) return;
  const classes = String(node.className || '').split(/\s+/).filter(value => value && value !== 'data-stale');
  if (stale) classes.push('data-stale');
  node.className = classes.join(' ');
  node.title = stale && at ? `STALE: last known value from ${new Date(at).toLocaleString()}.` : '';
  if (node.dataset) { node.dataset.stale = String(stale); if (at) node.dataset.observedAt = at; else delete node.dataset.observedAt; }
}
function displayData(node, value, at, stale = false) {
  if (!node) return;
  node.textContent = value;
  if (at) displayedAt.set(node, at); else displayedAt.delete(node);
  markData(node, stale, at);
}
function staleTree(node, fallbackAt) {
  if (!node) return;
  if (displayedAt.has(node) || node.children?.length) markData(node, true, displayedAt.get(node) || fallbackAt);
  for (const child of Array.from(node.children || [])) staleTree(child, fallbackAt);
}
function retainedValue(cache, key, value, at) {
  if (value !== null && value !== undefined) {
    const entry = {value, at}; cache.set(key, entry); return {...entry, stale: false};
  }
  const previous = cache.get(key);
  return previous ? {...previous, stale: true} : {value: null, at: null, stale: false};
}
const reportContextIdentity = value => `${value.source}|${value.environment}|${value.mode}`;
const reportIdentity = value => `${reportContextIdentity(value)}|${value.holdings_scope ?? ''}|${value.accounting_scope ?? ''}`;
export function mountPositions(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 10000, autoRefresh = true, autoLoad = true} = {}) {
  const $ = id => doc.getElementById(id);
  if (!$('positions-panel') || typeof doc.createElement !== 'function') return null;
  let stopped = false, fetching = false, generation = 0, controller = null, report = null;
  let sourceChosen = false, defaultApplied = false, lastSnapshot = null, identity = null, serviceContext = null;
  const summaryValues = new Map(), rowValues = new Map();
  const metricIDs = ['position-cash', 'position-value', 'position-open', 'position-unrealized', 'position-realized', 'position-fees'];
  const set = (id, value) => { if ($(id)) $(id).textContent = value; };
  const source = () => $('position-source').value === 'account' ? 'account' : 'paper';
  function clear(reason, {reset = false} = {}) {
    report = null;
    if (!reset && lastSnapshot && lastSnapshot.source === (source() === 'account' ? 'kalshi_account' : 'paper_journal')) {
      for (const id of metricIDs) staleTree($(id), lastSnapshot.fetched_at);
      staleTree($('position-rows'), lastSnapshot.fetched_at);
      set('positions-status', `STALE / last known: ${reason}`);
      set('positions-updated', `Last known snapshot ${new Date(lastSnapshot.fetched_at).toLocaleString()} | waiting for a fresh read`);
      return;
    }
    lastSnapshot = null; identity = null; summaryValues.clear(); rowValues.clear();
    $('position-rows').replaceChildren(); markData($('position-rows'), false);
    for (const id of metricIDs) displayData($(id), 'Unavailable', null);
    set('positions-status', reason); set('positions-updated', 'No current position snapshot');
    set('positions-accounting', source() === 'account'
      ? 'Account reads are read-only and require --account-view with local credentials. Unavailable values do not imply a zero balance or zero P&L.'
      : 'Paper positions come from simulated journal fills. Unavailable values do not imply a zero position or zero P&L.');
  }
  function render(value) {
    if (identity !== null && identity !== reportIdentity(value)
        && (value.status !== 'unavailable' || reportContextIdentity(value) !== reportContextIdentity(lastSnapshot))) clear('Loading the selected account context.', {reset: true});
    const account = value.source === 'kalshi_account';
    set('positions-source-label', account ? `${value.environment === 'production' ? 'Production' : 'Demo'} account (read-only)` : 'Paper journal simulation');
    if (value.status === 'unavailable') { clear(value.reason); return; }
    report = value; lastSnapshot = value; identity = reportIdentity(value);
    let retainedFields = 0;
    for (const [key, id, format] of [['cash_balance', 'position-cash', money], ['portfolio_value', 'position-value', money],
      ['open_count', 'position-open', value => String(value)], ['unrealized_pnl', 'position-unrealized', money],
      ['realized_pnl', 'position-realized', money], ['fees_paid', 'position-fees', money]]) {
      const shown = retainedValue(summaryValues, key, value.summary[key], value.fetched_at);
      displayData($(id), format(shown.value), shown.at, shown.stale); retainedFields += Number(shown.stale);
    }
    set('position-realized-label', account ? 'Reported realized P&L' : 'Est. settled net P&L');
    set('position-fees-label', account ? 'Reported fees (separate)' : 'Estimated entry fee allowance');
    set('positions-updated', `Snapshot ${new Date(value.fetched_at).toLocaleTimeString()} | ${autoRefresh ? `refresh every ${pollMs / 1000}s` : 'active Portfolio refresh'}${value.refreshing ? ' | acquisition in progress' : ''}`);
    set('positions-status', `${value.reason}${value.summary.unknown_intent_count ? ` | ${value.summary.unknown_intent_count} uncertain intents require reconciliation.` : ''}`);
    set('positions-accounting', account
      ? value.accounting_scope === 'separate_performance_history'
        ? 'Current nonzero unsettled holdings include closed contracts awaiting settlement. Broader realized P&L and fee totals are unavailable in this view; use Performance for separate accounting history. Open marks use available liquidation bids before exit fees; missing entry basis remains unavailable.'
        : 'Account view is separate from execution mode. Open marks use available liquidation bids before exit fees. Reported realized P&L and fees are shown separately; missing entry basis remains unavailable. This is not a complete lifetime account return.'
      : 'Paper positions use simulated fills and conservative entry fee allowances. Open marks use liquidation bids before exit fees. Pending or unavailable marks remain unavailable; they are not zero.');
    $('position-rows').replaceChildren(); markData($('position-rows'), false);
    const currentKeys = new Set(value.positions.map(row => `${row.ticker}|${row.outcome}`));
    for (const key of rowValues.keys()) if (!currentKeys.has(key)) rowValues.delete(key);
    for (const row of value.positions) {
      const tr = doc.createElement('tr');
      const key = `${row.ticker}|${row.outcome}`;
      if (!rowValues.has(key)) rowValues.set(key, new Map());
      const cache = rowValues.get(key), staleNames = [];
      const values = ['ticker', 'outcome', 'quantity', 'average_entry_price', 'mark_price', 'cost_basis', 'marked_value', 'unrealized_pnl', 'realized_pnl'].map(name => {
        const shown = retainedValue(cache, name, row[name], value.fetched_at);
        if (shown.stale) { retainedFields++; staleNames.push(name); }
        return {...shown, text: ['ticker', 'quantity'].includes(name) ? shown.value : name === 'outcome' ? shown.value.toUpperCase() : money(shown.value)};
      });
      values.push({text: `${row.status} | ${row.mark_basis}${row.warning ? ' | ' + row.warning : ''}${staleNames.length ? ' | STALE retained fields: ' + staleNames.join(', ') : ''}`, at: value.fetched_at});
      values.forEach((shown, index) => {
        const td = doc.createElement('td');
        if (index === 7 && shown.value !== null) td.className = Number(shown.value) >= 0 ? 'pnl-positive' : 'pnl-negative';
        displayData(td, shown.text, shown.at, shown.stale);
        tr.appendChild(td);
      });
      $('position-rows').appendChild(tr);
    }
    if (!value.positions.length) set('positions-status', `${value.reason} No filled positions are available for this view.`);
    if (retainedFields) set('positions-status', `${value.reason} | ${retainedFields} STALE last-known fields retained; hover marked values for their original timestamps.`);
    if (value.refresh_failed) clear('The latest account read failed; the original position snapshot is retained.');
  }
  async function refresh() {
    if (stopped) return;
    if (report?.fetched_at && (now() - Date.parse(report.fetched_at) > 30000 || now() - Date.parse(report.fetched_at) < -1000)) clear('Position snapshot is stale; waiting for a fresh read.');
    if (fetching) return;
    fetching = true; const expected = generation, selected = source(), activeController = new AbortController();
    controller = activeController;
    const timeout = win.setTimeout(() => activeController.abort(), 12000);
    try {
      const response = await fetchImpl(`/api/positions?source=${selected}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: activeController.signal});
      const text = await response.text();
      if (!response.ok || new TextEncoder().encode(text).byteLength > 2_000_000) fail('Position service is unavailable.');
      const value = validatePositionReport(JSON.parse(text));
      if (stopped || expected !== generation) return;
      if (value.source !== (selected === 'account' ? 'kalshi_account' : 'paper_journal')) fail('Position source differs from the selected view.');
      if (serviceContext !== null && `${value.environment}|${value.mode}` !== serviceContext) {
        clear('Position snapshot belongs to a different service environment; waiting for the selected context.'); return;
      }
      if (value.fetched_at !== null && (now() - Date.parse(value.fetched_at) < -1000 || now() - Date.parse(value.fetched_at) > 30000)) fail('Position snapshot is stale.');
      if (value.fetched_at !== null && lastSnapshot && reportContextIdentity(value) === reportContextIdentity(lastSnapshot)
          && Date.parse(value.fetched_at) < Date.parse(lastSnapshot.fetched_at)) {
        clear('An older position snapshot was ignored; waiting for a current read.'); return;
      }
      render(value);
    } catch {
      if (!stopped && expected === generation) clear(selected === 'account'
        ? 'Account positions unavailable. Start the console with --account-view and configured local credentials.'
        : 'Paper positions unavailable. Waiting for a fresh local snapshot.');
    } finally { win.clearTimeout(timeout); if (expected === generation) fetching = false; }
  }
  function changeSource() {
    generation++; controller?.abort(); fetching = false;
    set('positions-source-label', source() === 'account' ? 'Account view selected (read-only)' : 'Paper journal simulation');
    clear('Loading selected position source.', {reset: true}); return refresh();
  }
  function setServiceState(state) {
    if (stopped || !state || !['paper', 'demo', 'live'].includes(state.mode)) return;
    const context = ['production', 'demo'].includes(state.environment) ? `${state.environment}|${state.mode}` : null;
    const previousContext = serviceContext ?? (lastSnapshot ? `${lastSnapshot.environment}|${lastSnapshot.mode}` : null);
    const changed = context !== null && previousContext !== null && context !== previousContext;
    if (context !== null) serviceContext = context;
    if (changed) { generation++; controller?.abort(); fetching = false; clear('Loading the changed account environment.', {reset: true}); }
    if (defaultApplied) {
      if (!sourceChosen && state.account_view_enabled === true && source() !== 'account') { $('position-source').value = 'account'; return changeSource(); }
      return changed ? refresh() : undefined;
    }
    defaultApplied = true;
    if (sourceChosen) return;
    const selected = state.account_view_enabled === true || state.mode !== 'paper' ? 'account' : 'paper';
    if (source() !== selected) { $('position-source').value = selected; return changeSource(); }
    if (changed) return refresh();
  }
  function refreshNow() {
    generation++; controller?.abort(); fetching = false;
    return refresh();
  }
  $('position-source').addEventListener('change', () => { sourceChosen = true; changeSource(); });
  $('positions-refresh').addEventListener('click', () => refreshNow());
  const timer = autoRefresh && pollMs > 0 ? win.setInterval(() => {
    if (report?.fetched_at && now() - Date.parse(report.fetched_at) > 30000) clear('Position snapshot is stale; waiting for a fresh read.');
    refresh();
  }, pollMs) : null;
  function dispose() { stopped = true; generation++; controller?.abort(); win.clearInterval(timer); }
  win.addEventListener('pagehide', dispose);
  const ready = autoLoad ? refresh() : Promise.resolve();
  return {ready, refresh: refreshNow, poll: refresh, dispose, setServiceState, getReport: () => report};
}

export function mountOrders(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 10000, autoRefresh = true, autoLoad = true, onCancel = () => {}} = {}) {
  const $ = id => doc.getElementById(id);
  if (!$('orders-panel') || !$('position-source') || typeof doc.createElement !== 'function') return null;
  const set = (id, value) => { if ($(id)) $(id).textContent = value; };
  const source = () => $('position-source').value === 'account' ? 'account' : 'paper';
  let stopped = false, fetching = false, generation = 0, controller = null, report = null, lastSource = source();
  let lastSnapshot = null, identity = null, serviceContext = null, cancelButtons = [];
  const orderValues = new Map(), retainedOrders = new Set();
  const orderKey = row => row.order_id ? `${row.ticker}|${row.outcome}|order:${row.order_id}`
    : row.client_order_id ? `${row.ticker}|${row.outcome}|client:${row.client_order_id}` : null;
  const cancelling = new Set(), attempted = new Set();
  const notifyCancel = value => { try { Promise.resolve(onCancel(value)).catch(() => {}); } catch {} };
  function clear(reason, {reset = false} = {}) {
    report = null;
    for (const button of cancelButtons) button.disabled = true;
    if (!reset && lastSnapshot && lastSnapshot.source === (source() === 'account' ? 'kalshi_account' : 'paper_journal')) {
      staleTree($('order-rows'), lastSnapshot.fetched_at);
      set('orders-status', `STALE / last known: ${reason} Cancellation requires a fresh order read.`);
      set('orders-updated', `Last known snapshot ${new Date(lastSnapshot.fetched_at).toLocaleString()} | waiting for a fresh read`);
      return;
    }
    lastSnapshot = null; identity = null; orderValues.clear(); retainedOrders.clear(); cancelButtons = [];
    $('order-rows').replaceChildren(); markData($('order-rows'), false);
    set('orders-status', reason); set('orders-updated', 'No current order snapshot');
  }
  function render(value) {
    if (identity !== null && identity !== reportIdentity(value)) clear('Loading the selected order environment.', {reset: true});
    if (value.status === 'unavailable') { clear(value.reason); return; }
    report = value; lastSnapshot = value; identity = reportIdentity(value);
    $('order-rows').replaceChildren(); markData($('order-rows'), false); cancelButtons = []; retainedOrders.clear();
    const currentKeys = new Set(value.orders.map(orderKey).filter(Boolean));
    for (const key of orderValues.keys()) if (!currentKeys.has(key)) orderValues.delete(key);
    let retainedFields = 0;
    for (const row of value.orders) {
      const tr = doc.createElement('tr');
      const key = orderKey(row);
      if (key !== null && !orderValues.has(key)) orderValues.set(key, new Map());
      const cache = key === null ? new Map() : orderValues.get(key);
      const field = (name, format) => {
        const shown = retainedValue(cache, name, row[name], value.fetched_at);
        if (shown.stale) { retainedFields++; if (key !== null) retainedOrders.add(key); }
        return {...shown, text: format(shown.value)};
      };
      const values = [field('created_at', value => value === null ? 'Unavailable' : new Date(value).toLocaleString()),
        {text: row.ticker}, {text: row.action.toUpperCase()}, {text: row.outcome.toUpperCase()}, {text: row.count},
        field('fill_count', value => value ?? 'Unknown'), field('remaining_count', value => value ?? 'Unknown'),
        field('price', money),
        {text: `${row.status}${row.order_type === 'limit' ? ' | resting limit' : row.order_type === 'ioc' ? ' | IOC' : ' | execution type unavailable'}${row.expiration_time ? ' | expires ' + new Date(row.expiration_time * 1000).toLocaleTimeString() : ''}${key !== null && retainedOrders.has(key) ? ' | STALE fields retained' : ''}`},
        {text: row.order_id || row.client_order_id || 'Unavailable'}];
      for (const shown of values) {
        const td = doc.createElement('td'); displayData(td, shown.text, Object.hasOwn(shown, 'at') ? shown.at : value.fetched_at, shown.stale); tr.appendChild(td);
      }
      const action = doc.createElement('td');
      if (row.cancelable) {
        const button = doc.createElement('button'); button.type = 'button'; button.className = 'cancel-order';
        button.textContent = value.source === 'paper_journal' ? 'Cancel paper limit' : 'Cancel remaining';
        button.disabled = attempted.has(row.client_order_id) || cancelling.has(row.client_order_id)
          || row.expiration_time !== null && row.expiration_time * 1000 <= now()
          || key !== null && retainedOrders.has(key);
        button.addEventListener('click', () => cancelOrder(row.client_order_id, value.source)); action.appendChild(button);
        cancelButtons.push(button);
      } else action.textContent = row.status === 'cancel_pending' ? 'Cancellation pending' : '-';
      tr.appendChild(action);
      $('order-rows').appendChild(tr);
    }
    set('orders-status', `${value.reason}${value.orders.length ? '' : ' No orders returned for this view.'}`);
    if (retainedFields) set('orders-status', `${value.reason} | ${retainedFields} STALE last-known fields retained; cancellation requires complete current order data.`);
    set('orders-updated', `${value.source === 'kalshi_account' ? 'Kalshi account' : 'Paper journal'} | snapshot ${new Date(value.fetched_at).toLocaleTimeString()}`);
    if (value.refresh_failed) clear('The latest account read failed; previous orders remain stale and cancellation requires a fresh read.');
  }
  async function cancelOrder(clientOrderId, expectedSource) {
    const expectedContext = report ? reportIdentity(report) : null;
    const row = report?.orders.find(row => row.client_order_id === clientOrderId && row.cancelable);
    if (stopped || !row || report.source !== expectedSource || cancelling.has(clientOrderId) || attempted.has(clientOrderId)
        || retainedOrders.has(orderKey(row))) return;
    if (!report.fetched_at || now() - Date.parse(report.fetched_at) > 30000 || now() - Date.parse(report.fetched_at) < -1000
        || row.expiration_time !== null && row.expiration_time * 1000 <= now()) {
      clear('Order snapshot is stale or expired; read its current state before cancellation.'); await refreshNow(); return;
    }
    cancelling.add(clientOrderId); render(report); notifyCancel({phase: 'starting', client_order_id: clientOrderId});
    set('orders-cancel-result', 'Requesting cancellation of this order remainder. Fills can occur before cancellation is confirmed.');
    const activeController = new AbortController();
    const timeout = win.setTimeout(() => activeController.abort(), 12000);
    let result = null;
    try {
      const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', signal: activeController.signal};
      const session = await fetchImpl('/api/session', {...options, method: 'GET', headers: {Accept: 'application/json'}});
      const sessionText = await session.text();
      if (!session.ok || new TextEncoder().encode(sessionText).byteLength > 4096) fail('Local cancellation session is unavailable.');
      const token = JSON.parse(sessionText)?.token;
      if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{24,256}$/.test(token)) fail('Local cancellation session is invalid.');
      const currentRow = report?.orders.find(item => item.client_order_id === clientOrderId && item.cancelable);
      if (stopped || report?.source !== expectedSource || !currentRow || reportIdentity(report) !== expectedContext
          || retainedOrders.has(orderKey(currentRow)) || !report.fetched_at
          || now() - Date.parse(report.fetched_at) > 30000 || now() - Date.parse(report.fetched_at) < -1000
          || currentRow.expiration_time !== null && currentRow.expiration_time * 1000 <= now()) return;
      attempted.add(clientOrderId);
      const response = await fetchImpl('/api/cancel-order', {...options, method: 'POST',
        headers: {Accept: 'application/json', 'Content-Type': 'application/json', 'X-Kalshi-Token': token},
        body: JSON.stringify({client_order_id: clientOrderId})});
      const text = await response.text();
      if (!response.ok || new TextEncoder().encode(text).byteLength > 2_000_000) fail('Cancellation requires order-state reconciliation.');
      const value = JSON.parse(text);
      if (!object(value) || value.kind !== 'kalshi_execution_result' || !['canceled', 'filled', 'cancel_pending', 'unknown', 'rejected'].includes(value.status)) fail('Cancellation acknowledgement is unavailable.');
      label(value.reason, 'cancellation reason'); result = value;
      if (stopped) return;
      const messages = {canceled: expectedSource === 'paper_journal' ? 'Paper limit cancellation recorded.' : 'Remaining order quantity canceled.',
        filled: 'The order filled before cancellation.', cancel_pending: 'Cancellation pending. The order may still fill until cancellation is confirmed.',
        unknown: 'Cancellation outcome is uncertain. Check order history before another action.', rejected: 'Cancellation was rejected. Check the current order status.'};
      set('orders-cancel-result', `${expectedSource === 'paper_journal' ? 'Paper journal' : 'Kalshi account'} | ${row.ticker}: ${messages[value.status]} ${value.reason}`);
    } catch {
      if (!stopped) set('orders-cancel-result', attempted.has(clientOrderId)
        ? 'Cancellation outcome is uncertain. Check order history; this page will not retry the request.'
        : 'Cancellation was not submitted because the local session is unavailable.');
    } finally {
      win.clearTimeout(timeout); cancelling.delete(clientOrderId);
      notifyCancel({phase: 'finished', client_order_id: clientOrderId, status: result?.status || (attempted.has(clientOrderId) ? 'unknown' : 'rejected')});
      if (!stopped) await refreshNow();
    }
  }
  async function refresh() {
    if (stopped) return;
    if (report?.fetched_at && (now() - Date.parse(report.fetched_at) > 30000 || now() - Date.parse(report.fetched_at) < -1000)) clear('Order snapshot is stale; waiting for a fresh read.');
    if (fetching) return;
    fetching = true; const expected = generation, selected = source(), activeController = new AbortController();
    lastSource = selected; controller = activeController;
    const timeout = win.setTimeout(() => activeController.abort(), 12000);
    try {
      const response = await fetchImpl(`/api/orders?source=${selected}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: activeController.signal});
      const text = await response.text();
      if (!response.ok || new TextEncoder().encode(text).byteLength > 2_000_000) fail('Order history service is unavailable.');
      const value = validateOrderReport(JSON.parse(text));
      if (stopped || expected !== generation) return;
      if (value.source !== (selected === 'account' ? 'kalshi_account' : 'paper_journal')) fail('Order source differs from the selected view.');
      if (serviceContext !== null && `${value.environment}|${value.mode}` !== serviceContext) {
        clear('Order snapshot belongs to a different service environment; waiting for the selected context.'); return;
      }
      if (value.fetched_at !== null && (now() - Date.parse(value.fetched_at) < -1000 || now() - Date.parse(value.fetched_at) > 30000)) fail('Order snapshot is stale.');
      if (value.fetched_at !== null && lastSnapshot && reportIdentity(value) === reportIdentity(lastSnapshot)
          && Date.parse(value.fetched_at) < Date.parse(lastSnapshot.fetched_at)) {
        clear('An older order snapshot was ignored; cancellation requires a current read.'); return;
      }
      render(value);
    } catch {
      if (!stopped && expected === generation) clear(selected === 'account'
        ? 'Account orders unavailable. Waiting for an authenticated read-only snapshot.'
        : 'Paper order history unavailable. Waiting for a fresh local snapshot.');
    } finally { win.clearTimeout(timeout); if (expected === generation) fetching = false; }
  }
  function refreshNow() { generation++; controller?.abort(); fetching = false; return refresh(); }
  function changeSource() { clear('Loading selected order source.', {reset: true}); return refreshNow(); }
  function setServiceState(state) {
    if (stopped) return;
    const context = state && ['production', 'demo'].includes(state.environment) && ['paper', 'demo', 'live'].includes(state.mode)
      ? `${state.environment}|${state.mode}` : null;
    const previousContext = serviceContext ?? (lastSnapshot ? `${lastSnapshot.environment}|${lastSnapshot.mode}` : null);
    const changed = context !== null && previousContext !== null && context !== previousContext;
    if (context !== null) serviceContext = context;
    if (source() !== lastSource || changed) return changeSource();
  }
  $('position-source').addEventListener('change', changeSource);
  $('orders-refresh').addEventListener('click', refreshNow);
  const timer = autoRefresh && pollMs > 0 ? win.setInterval(() => {
    if (report?.fetched_at && now() - Date.parse(report.fetched_at) > 30000) clear('Order snapshot is stale; waiting for a fresh read.');
    refresh();
  }, pollMs) : null;
  function dispose() { stopped = true; generation++; controller?.abort(); win.clearInterval(timer); }
  win.addEventListener('pagehide', dispose);
  const ready = autoLoad ? refresh() : Promise.resolve();
  return {ready, refresh: refreshNow, poll: refresh, setServiceState, dispose, getReport: () => report};
}
