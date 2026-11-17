// Read-only neutral-spread research monitor. No order or strategy-control routes.
const NS = 'http://www.w3.org/2000/svg';
const MAX_TIME = 253402300799, MAX_HISTORY = 2000, MAX_ACTIONS = 1000;
const COLORS = {bid: '#8fd8d0', model: '#83b8ef', realized: '#d2b1ef', fee: '#e9bd83', positive: '#8ce5be', negative: '#efb1a8', limit: '#efb1a8'};
const MONEY = ['initial_bankroll', 'cash', 'fair_value_equity', 'liquidation_equity', 'fair_value_pnl', 'liquidation_pnl', 'realized_pnl', 'unrealized_pnl', 'fees', 'turnover', 'return_fraction', 'drawdown_fraction', 'max_drawdown_fraction'];
const COUNTS = ['completed_baskets', 'partial_baskets', 'unfilled_baskets', 'requested_contracts', 'filled_contracts', 'open_contracts', 'open_markets', 'evaluations', 'proposals', 'holds', 'observations', 'outcome_observations'];
const RISK = ['residual_delta', 'residual_gamma', 'worst_case_loss', 'cvar_loss', 'expected_terminal_profit', 'maximum_interim_abs_delta'];
const HISTORY = ['fair_value_equity', 'liquidation_equity', 'fair_value_pnl', 'liquidation_pnl', 'realized_pnl', 'fees', 'drawdown_fraction', 'residual_delta', 'residual_gamma', 'worst_case_loss'];
const LIMITS = ['max_abs_delta', 'max_abs_gamma', 'max_unhedged_delta', 'max_worst_case_loss', 'max_pair_cost', 'max_leg_contracts', 'max_position_contracts', 'kelly_fraction', 'min_expected_improvement', 'min_log_growth'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const label = (value, maximum = 2000) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail('Invalid neutral report label.');
const integer = (value, maximum = 1e9) => Number.isSafeInteger(value) && value >= 0 && value <= maximum ? value : fail('Invalid neutral report count.');
const epoch = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= MAX_TIME ? value : fail('Invalid neutral observation time.');
const nullableEpoch = value => value == null ? null : epoch(value);
function amount(value) {
  if (value == null) return null;
  if (typeof value !== 'string' || value.length > 80 || !/^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?$/.test(value)
      || !Number.isFinite(Number(value)) || Math.abs(Number(value)) > 1e12) fail('Invalid neutral report amount.');
  return value;
}
const probability = value => { const result = amount(value); if (result !== null && (Number(result) < 0 || Number(result) > 1)) fail('Invalid neutral probability.'); return result; };
const boundedList = (value, maximum, name) => Array.isArray(value) && value.length <= maximum ? value : fail(`Invalid neutral ${name}.`);
const same = (a, b) => Math.abs(a - b) <= 1e-7 * Math.max(1, Math.abs(a), Math.abs(b));

function leg(raw, actionRow = false) {
  if (!object(raw)) fail('Invalid neutral leg.');
  const item = {ticker: label(raw.ticker, 160), action: raw.action == null ? null : label(raw.action, 30),
    outcome: raw.outcome == null ? null : label(raw.outcome, 8), count: raw.count == null ? null : integer(raw.count, 1000000),
    requested_count: raw.requested_count == null ? null : integer(raw.requested_count, 1000000),
    filled_count: raw.filled_count == null ? null : integer(raw.filled_count, 1000000),
    status: raw.status == null ? null : label(raw.status, 40), reason: raw.reason == null ? null : label(raw.reason),
    limit_price: probability(raw.limit_price), fill: null};
  if (item.outcome !== null && !['yes', 'no'].includes(item.outcome)) fail('Invalid neutral outcome.');
  if (item.action !== null && !['buy', 'sell'].includes(item.action)) fail('Invalid neutral action.');
  if (actionRow && item.requested_count !== null && item.filled_count !== null && item.filled_count > item.requested_count) fail('Neutral fill exceeds requested quantity.');
  if (raw.fill != null) {
    if (!object(raw.fill)) fail('Invalid neutral fill.');
    item.fill = {action: label(raw.fill.action, 20), outcome: label(raw.fill.outcome, 8),
      count: integer(raw.fill.count, 1000000), gross: amount(raw.fill.gross ?? raw.fill.gross_cost),
      fee: amount(raw.fill.fee ?? raw.fill.fees), average_price: probability(raw.fill.average_price), cash_delta: amount(raw.fill.cash_delta)};
    if (!['buy', 'sell'].includes(item.fill.action) || !['yes', 'no'].includes(item.fill.outcome)) fail('Invalid neutral fill direction.');
    if (item.filled_count !== null && item.fill.count !== item.filled_count) fail('Inconsistent neutral fill quantity.');
  }
  return item;
}
function plan(raw) {
  if (raw == null) return null;
  if (!object(raw)) fail('Invalid neutral decision.');
  const result = {status: label(raw.status ?? 'hold', 50), action: label(raw.action ?? 'hold', 40), reason: label(raw.reason ?? ''),
    legs: boundedList(raw.legs ?? [], 2, 'planned legs').map(row => leg(row)), after: {}, after_observed: {}, diagnostics: {}};
  for (const target of ['after', 'after_observed']) {
    if (raw[target] != null && !object(raw[target])) fail('Invalid neutral planned metrics.');
    for (const key of RISK) result[target][key] = amount(raw[target]?.[key]);
  }
  if (raw.diagnostics != null) {
    if (!object(raw.diagnostics) || raw.diagnostics.rejected != null && !object(raw.diagnostics.rejected)) fail('Invalid neutral decision diagnostics.');
    const entries = Object.entries(raw.diagnostics.rejected ?? {});
    if (entries.length > 40) fail('Too many neutral constraints.');
    result.diagnostics = {candidates_considered: integer(raw.diagnostics.candidates_considered ?? 0),
      rejected: Object.fromEntries(entries.map(([key, value]) => [label(key, 100), integer(value)]))};
  }
  return result;
}

function experiment(raw, report) {
  if (raw == null) return null;
  if (!object(raw) || raw.kind !== 'kalshi_neutral_experiment_scorecard' || raw.version !== 1
      || !['unavailable', 'collecting', 'awaiting_settlement', 'complete', 'blocked'].includes(raw.status)
      || !['synthetic', 'actual_market_data', 'recorded_replay', 'producer_report'].includes(raw.evidence_source)
      || typeof raw.official_outcomes_available !== 'boolean' || typeof raw.execution_totals_complete !== 'boolean'
      || typeof raw.session_resume_supported !== 'boolean'
      || (raw.evidence_source === 'synthetic') !== report.synthetic) fail('Invalid experiment evidence.');
  const value = {kind: raw.kind, version: 1, status: raw.status, evidence_source: raw.evidence_source,
    official_outcomes_available: raw.official_outcomes_available, execution_totals_complete: raw.execution_totals_complete,
    session_resume_supported: raw.session_resume_supported, settlements: [], notes: []};
  value.reconciliation_status = raw.reconciliation_status ?? null;
  if (![null, 'complete', 'pending', 'blocked'].includes(value.reconciliation_status)
      || value.reconciliation_status === 'blocked' && value.status !== 'blocked'
      || value.status === 'complete' && value.reconciliation_status !== null && value.reconciliation_status !== 'complete') fail('Experiment reconciliation is inconsistent.');
  for (const key of ['initial_capital', 'cash', 'fees', 'remaining_basis', 'net_realized_pnl', 'settlement_payouts',
    'settlement_realized_pnl', 'trading_realized_pnl', 'completed_experiment_pnl', 'completed_return_fraction']) value[key] = amount(raw[key]);
  for (const key of ['official_resolved_markets', 'settled_inventory_markets', 'open_markets', 'settled_lot_wins',
    'settled_lot_losses', 'settled_lot_flat']) value[key] = raw[key] == null ? null : integer(raw[key]);
  value.last_outcome_received_at = nullableEpoch(raw.last_outcome_received_at); value.closed_at = nullableEpoch(raw.closed_at);
  const n = key => Number(value[key]);
  if (['initial_capital', 'cash', 'fees', 'remaining_basis', 'net_realized_pnl'].some(k => value[k] === null)
      || n('initial_capital') <= 0 || n('cash') < 0 || n('fees') < 0 || n('remaining_basis') < 0
      || !same(n('net_realized_pnl'), n('cash') + n('remaining_basis') - n('initial_capital'))
      || !same(n('initial_capital'), Number(report.summary.initial_bankroll))
      || !same(n('cash'), Number(report.summary.cash)) || !same(n('fees'), Number(report.summary.fees))
      || !same(n('net_realized_pnl'), Number(report.summary.realized_pnl))
      || value.open_markets !== report.summary.open_markets) fail('Experiment capital does not reconcile.');
  const identities = new Set();
  value.settlements = boundedList(raw.settlements, 256, 'official outcomes').map(row => {
    if (!object(row)) fail('Invalid official outcome.');
    const record = {ticker: label(row.ticker, 128), contract_identity: label(row.contract_identity, 64),
      evidence_sha256: label(row.evidence_sha256, 64), source: label(row.source, 100), status: label(row.status, 30),
      result: label(row.result, 10), synthetic: row.synthetic,
      settled_at: epoch(row.settled_at), received_at: epoch(row.received_at), window_end: epoch(row.window_end), quantity: row.quantity};
    if (identities.has(record.ticker) || !/^[a-f0-9]{64}$/.test(record.contract_identity)
        || !/^[a-f0-9]{64}$/.test(record.evidence_sha256) || record.source !== 'GET /markets/{ticker}'
        || record.status !== 'finalized' || !['yes', 'no'].includes(record.result)
        || typeof record.synthetic !== 'boolean' || record.synthetic && !report.synthetic
        || !Number.isSafeInteger(record.quantity) || Math.abs(record.quantity) > 1e6
        || record.window_end > record.settled_at || record.settled_at > record.received_at
        || report.observed_at === null || record.received_at > report.observed_at) fail('Invalid official outcome provenance.');
    identities.add(record.ticker);
    for (const key of ['yes_payout', 'payout', 'premium', 'entry_fees', 'realized_pnl']) {
      record[key] = amount(row[key]); if (record[key] === null) fail('Incomplete settlement cash evidence.');
    }
    const yes = Number(record.yes_payout), quantity = Math.abs(record.quantity);
    if (yes !== (record.result === 'yes' ? 1 : 0) || Number(record.premium) < 0 || Number(record.premium) > quantity
        || Number(record.entry_fees) < 0 || !quantity && (Number(record.premium) !== 0 || Number(record.entry_fees) !== 0)
        || !same(Number(record.payout), quantity * (record.quantity > 0 ? yes : 1 - yes))
        || !same(Number(record.realized_pnl), Number(record.payout) - Number(record.premium) - Number(record.entry_fees))
        || report.contracts.some(c => c.ticker === record.ticker && c.position !== 0)) fail('Settlement cash or inventory does not reconcile.');
    return record;
  });
  const rows = value.settlements, inventory = rows.filter(row => row.quantity !== 0);
  if (value.official_outcomes_available) {
    const sum = key => rows.reduce((total, row) => total + Number(row[key]), 0);
    if (['settlement_payouts', 'settlement_realized_pnl', 'trading_realized_pnl'].some(k => value[k] === null)
        || value.official_resolved_markets !== rows.length || value.settled_inventory_markets !== inventory.length
        || !same(n('settlement_payouts'), sum('payout')) || !same(n('settlement_realized_pnl'), sum('realized_pnl'))
        || !same(n('trading_realized_pnl') + n('settlement_realized_pnl'), n('net_realized_pnl'))
        || value.settled_lot_wins !== inventory.filter(row => Number(row.realized_pnl) > 0).length
        || value.settled_lot_losses !== inventory.filter(row => Number(row.realized_pnl) < 0).length
        || value.settled_lot_flat !== inventory.filter(row => Number(row.realized_pnl) === 0).length
        || value.last_outcome_received_at !== (rows.length ? Math.max(...rows.map(row => row.received_at)) : null)) fail('Experiment outcome totals do not reconcile.');
  } else if (rows.length || value.status !== 'unavailable' || ['official_resolved_markets', 'settled_inventory_markets',
    'settlement_payouts', 'settlement_realized_pnl', 'trading_realized_pnl', 'settled_lot_wins', 'settled_lot_losses', 'settled_lot_flat'].some(k => value[k] !== null)) fail('Missing official evidence cannot establish outcomes.');
  if (value.closed_at !== null && (report.observed_at === null || value.closed_at > report.observed_at)) fail('Experiment closure is in the future.');
  if (value.status === 'complete') {
    if (!rows.length || value.open_markets !== 0 || report.pending !== null || value.closed_at === null
        || value.completed_experiment_pnl === null || value.completed_return_fraction === null
        || !same(n('completed_experiment_pnl'), n('cash') - n('initial_capital'))
        || !same(n('completed_return_fraction'), n('completed_experiment_pnl') / n('initial_capital')))
      fail('Completed experiment lacks reconciled final cash.');
  } else if (value.completed_experiment_pnl !== null || value.completed_return_fraction !== null) fail('Unresolved experiment cannot establish final returns.');
  value.notes = boundedList(raw.notes ?? [], 20, 'experiment notes').map(note => label(note));
  return value;
}

export function validateNeutralReport(raw, expectedSource = null) {
  if (!object(raw) || raw.kind !== 'kalshi_neutral_monitor' || raw.version !== 1
      || !['ready', 'stale', 'unavailable'].includes(raw.status) || !['report', 'demo'].includes(raw.source)
      || raw.execution_eligible !== false || raw.research_only !== true || typeof raw.synthetic !== 'boolean') fail('Unsupported neutral research report.');
  if (expectedSource !== null && raw.source !== expectedSource) fail('Neutral report belongs to a different source.');
  if (raw.source === 'demo' && !raw.synthetic) fail('Synthetic demo must be labeled synthetic.');
  const result = {kind: raw.kind, version: 1, status: raw.status, source: raw.source,
    execution_eligible: false, research_only: true, synthetic: raw.synthetic, reason: label(raw.reason),
    observed_at: nullableEpoch(raw.observed_at), session_id: raw.session_id == null ? null : label(raw.session_id, 200),
    input_source: label(raw.input_source ?? (raw.source === 'demo' ? 'demo' : 'report'), 80),
    summary: {}, risk: {}, limits: {}, history: [], contracts: [], scenarios: [], actions: [],
    last_decision: null, pending: null, repair_required: raw.repair_required === true,
    config: {}, model: {}, coverage: {}, limitations: [], experiment: null};
  if (raw.status !== 'unavailable' && (result.observed_at === null || !object(raw.summary) || !object(raw.risk) || !object(raw.coverage))) fail('Missing neutral monitoring evidence.');
  const summary = raw.summary ?? {};
  for (const key of MONEY) result.summary[key] = amount(summary[key]);
  for (const key of COUNTS) result.summary[key] = summary[key] == null ? null : integer(summary[key]);
  result.summary.fill_rate = summary.fill_rate == null ? null : typeof summary.fill_rate === 'number' && Number.isFinite(summary.fill_rate) && summary.fill_rate >= 0 && summary.fill_rate <= 1 ? summary.fill_rate : fail('Invalid neutral fill rate.');
  for (const key of ['return_fraction', 'drawdown_fraction', 'max_drawdown_fraction']) {
    const value = result.summary[key]; if (key !== 'return_fraction' && value !== null && (Number(value) < 0 || Number(value) > 1)) fail('Invalid neutral drawdown.');
  }
  for (const key of ['fees', 'turnover', 'cash', 'initial_bankroll']) if (result.summary[key] !== null && Number(result.summary[key]) < 0) fail('Negative neutral cash or costs.');
  for (const [equityKey, pnlKey] of [['fair_value_equity', 'fair_value_pnl'], ['liquidation_equity', 'liquidation_pnl']]) {
    const equity = result.summary[equityKey], pnl = result.summary[pnlKey], initial = result.summary.initial_bankroll;
    if (equity === null && pnl !== null || equity !== null && pnl !== null && initial !== null && !same(Number(pnl), Number(equity) - Number(initial))) fail('Inconsistent neutral P&L basis.');
  }
  if (result.summary.requested_contracts !== null && result.summary.filled_contracts !== null
      && result.summary.filled_contracts > result.summary.requested_contracts) fail('Neutral fills exceed requests.');
  for (const key of RISK) result.risk[key] = amount(raw.risk?.[key]);
  const limits = raw.limits ?? raw.config?.sizing ?? {};
  if (!object(limits)) fail('Invalid neutral risk limits.');
  for (const key of LIMITS) { result.limits[key] = amount(limits[key] == null ? null : String(limits[key])); if (result.limits[key] !== null && Number(result.limits[key]) < 0) fail('Negative neutral risk limit.'); }
  const coverage = raw.coverage ?? {};
  result.coverage = {reason: label(coverage.reason ?? '', 3000), telemetry: coverage.telemetry === true,
    current_risk_available: coverage.current_risk_available === true,
    history_start: nullableEpoch(coverage.history_start), history_points: integer(coverage.history_points ?? 0),
    omitted_history_points: integer(coverage.omitted_history_points ?? 0), omitted_actions: integer(coverage.omitted_actions ?? 0),
    display_omitted_actions: integer(coverage.display_omitted_actions ?? coverage.omitted_actions ?? 0),
    summary_action_count: integer(coverage.summary_action_count ?? raw.actions?.length ?? 0),
    accounting_basis: label(coverage.accounting_basis ?? 'Producer-supplied research snapshot; no external account verification.', 1000),
    history_basis: label(coverage.history_basis ?? 'Snapshot only', 200),
    drawdown_basis: label(coverage.drawdown_basis ?? 'Full-depth liquidation equity', 200)};
  let previousTime = -1, previousSequence = -1;
  result.history = boundedList(raw.history ?? [], MAX_HISTORY, 'history').map(row => {
    if (!object(row)) fail('Invalid neutral history point.');
    const point = {observed_at: epoch(row.observed_at), sequence: integer(row.sequence), observation_kind: row.observation_kind ?? 'market'};
    if (!['market', 'settlement'].includes(point.observation_kind)) fail('Invalid neutral observation kind.');
    if (point.observed_at <= previousTime || point.sequence <= previousSequence || result.observed_at !== null && point.observed_at > result.observed_at) fail('Neutral history must contain ordered distinct observations.');
    previousTime = point.observed_at; previousSequence = point.sequence;
    for (const key of HISTORY) point[key] = amount(row[key]);
    return point;
  });
  result.contracts = boundedList(raw.contracts ?? [], 6, 'contracts').map(row => {
    if (!object(row)) fail('Invalid neutral contract.');
    const position = row.position ?? 0;
    if (!Number.isSafeInteger(position) || Math.abs(position) > 1e6) fail('Invalid neutral position.');
    const item = {ticker: label(row.ticker, 160), position, quantity: integer(row.quantity ?? Math.abs(position), 1e6),
      side: row.side ?? (position > 0 ? 'yes' : position < 0 ? 'no' : null),
      strike: amount(row.strike), probability_yes: probability(row.probability_yes), delta_yes: amount(row.delta_yes), gamma_yes: amount(row.gamma_yes),
      window_end: nullableEpoch(row.window_end), basis: amount(row.basis ?? row.total_basis)};
    if (item.quantity !== Math.abs(position) || !['yes', 'no', null].includes(item.side)
        || item.side !== (position > 0 ? 'yes' : position < 0 ? 'no' : null)) fail('Inconsistent neutral held outcome.');
    return item;
  });
  if (new Set(result.contracts.map(c => c.ticker)).size !== result.contracts.length) fail('Duplicate neutral contract identity.');
  result.scenarios = boundedList(raw.scenarios ?? [], 64, 'scenarios').map(row => {
    if (!object(row)) fail('Invalid neutral scenario.');
    return {name: label(row.name, 160), probability: probability(row.probability), payout: amount(row.payout),
      terminal_wealth: amount(row.terminal_wealth), total_pnl: amount(row.total_pnl), open_lot_pnl: amount(row.open_lot_pnl)};
  });
  result.actions = boundedList(raw.actions ?? [], MAX_ACTIONS, 'actions').map(row => {
    if (!object(row)) fail('Invalid neutral basket action.');
    return {status: label(row.status, 50), action: label(row.action ?? 'unknown', 40), reason: label(row.reason ?? ''),
      intent_time: nullableEpoch(row.intent_time), fill_time: nullableEpoch(row.fill_time), cash_after: amount(row.cash_after),
      force_reduce: row.force_reduce === true, legs: boundedList(row.legs ?? [], 2, 'filled legs').map(item => leg(item, true))};
  });
  result.last_decision = plan(raw.last_decision);
  if (raw.pending != null) {
    if (!object(raw.pending)) fail('Invalid neutral pending basket.');
    result.pending = {action: label(raw.pending.action ?? 'pending', 40), submitted_at: nullableEpoch(raw.pending.submitted_at),
      arrives_at: nullableEpoch(raw.pending.arrives_at), expires_at: nullableEpoch(raw.pending.expires_at),
      legs: boundedList(raw.pending.legs ?? [], 2, 'pending legs').map(item => leg(item))};
  }
  if (object(raw.config)) {
    for (const key of ['latency_seconds', 'max_execution_wait_seconds', 'decision_interval_seconds']) {
      if (raw.config[key] != null) { if (typeof raw.config[key] !== 'number' || !Number.isFinite(raw.config[key]) || raw.config[key] < 0 || raw.config[key] > 3600) fail('Invalid neutral simulation assumption.'); result.config[key] = raw.config[key]; }
    }
    result.config.depth_fraction = probability(raw.config.depth_fraction);
  }
  if (object(raw.model)) {
    for (const key of ['source', 'model_id', 'kind']) if (raw.model[key] != null) result.model[key] = label(raw.model[key], 200);
    for (const key of ['mean', 'standard_deviation', 'mean_spot_slope']) result.model[key] = amount(raw.model[key]);
    result.model.as_of = nullableEpoch(raw.model.as_of);
  }
  result.limitations = boundedList(raw.limitations ?? [], 30, 'limitations').map(item => label(item));
  result.experiment = experiment(raw.experiment, result);
  return result;
}

const dollars = value => value === null || value === undefined ? 'Unavailable' : new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 2}).format(Number(value));
const signedDollars = value => value == null ? 'Unavailable' : `${Number(value) > 0 ? '+' : ''}${dollars(value)}`;
const percentage = value => value == null ? 'Unavailable' : `${(Number(value) * 100).toFixed(2)}%`;
const greek = value => value == null ? 'Unavailable' : Number(value) === 0 ? '0' : Math.abs(Number(value)) < .0001 ? Number(value).toExponential(3) : Number(value).toFixed(5);
const when = value => value == null ? 'Unknown time' : new Date(value * 1000).toLocaleString();
const pretty = value => String(value ?? '').replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());

function make(doc, tag, attributes = {}, content = null) {
  const node = doc.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) { if (name === 'class') node.className = value; else node.setAttribute(name, String(value)); }
  if (content !== null) node.textContent = String(content);
  return node;
}
function append(parent, ...children) { for (const child of children) parent.appendChild(child); return parent; }
function svgNode(doc, tag, attributes = {}, content = null) {
  const node = doc.createElementNS(NS, tag); for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  if (content !== null) node.textContent = String(content); return node;
}
function title(doc, parent, text) { parent.appendChild(svgNode(doc, 'title', {}, text)); }
function emptyChart(doc, svg, text) { svg.removeAttribute?.('data-chart-bounds'); svg.replaceChildren(); svg.appendChild(svgNode(doc, 'text', {x: 330, y: 120, class: 'neutral-chart-empty'}, text)); }
function extent(values) {
  const valid = values.filter(value => value !== null && value !== undefined && Number.isFinite(Number(value))).map(Number);
  if (!valid.length) return null;
  let low = Math.min(0, ...valid), high = Math.max(0, ...valid), pad = (high - low || Math.max(Math.abs(low), 1)) * .12;
  return [low - pad, high + pad];
}
function axes(doc, svg, bounds, count, yLabel, xLabel, bucketed = false, sequenceLabels = null, formatter = null, allLabels = false) {
  const left = 62, right = 637, top = 25, bottom = 197;
  const y = value => bottom - (Number(value) - bounds[0]) / (bounds[1] - bounds[0]) * (bottom - top);
  const x = index => bucketed ? left + (index + .5) / count * (right - left) : count <= 1 ? (left + right) / 2 : left + index / (count - 1) * (right - left);
  svg.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`);
  svg.replaceChildren();
  for (let step = 0; step <= 4; step++) {
    const value = bounds[0] + step / 4 * (bounds[1] - bounds[0]);
    svg.appendChild(svgNode(doc, 'line', {x1: left, x2: right, y1: y(value), y2: y(value), class: 'neutral-chart-gridline'}));
    const formatted = formatter ? formatter(value) : Math.abs(value) > 0 && Math.abs(value) < .001 ? value.toExponential(1) : value.toFixed(Math.abs(value) < 1 ? 3 : 1);
    svg.appendChild(svgNode(doc, 'text', {x: left - 8, y: y(value) + 3, 'text-anchor': 'end', class: 'neutral-chart-axis'}, formatted));
  }
  if (bounds[0] <= 0 && bounds[1] >= 0) {
    const zero = svgNode(doc, 'line', {x1: left, x2: right, y1: y(0), y2: y(0), class: 'neutral-chart-zero'});
    title(doc, zero, 'Zero reference'); svg.appendChild(zero);
  }
  svg.appendChild(svgNode(doc, 'text', {x: left, y: 13, class: 'neutral-chart-axis'}, yLabel));
  svg.appendChild(svgNode(doc, 'text', {x: (left + right) / 2, y: 232, 'text-anchor': 'middle', class: 'neutral-chart-axis'}, xLabel));
  const indices = allLabels ? Array.from({length: count}, (_, index) => index) : [...new Set([0, Math.floor((count - 1) / 2), count - 1])];
  for (const index of indices) if (index >= 0) svg.appendChild(svgNode(doc, 'text', {x: x(index), y: 215, 'text-anchor': 'middle', class: 'neutral-chart-axis'}, String(sequenceLabels?.[index] ?? index + 1)));
  return {x, y, left, right, top, bottom};
}
function lineChart(doc, svg, rows, series, {yLabel, xLabel, labelPoint, limits = [], hidden = new Set()}) {
  if (!rows.length) { emptyChart(doc, svg, 'No observed history in this session'); return; }
  const bounds = extent([...series.filter(s => !hidden.has(s.key)).flatMap(s => rows.map(row => row[s.key])), ...limits]);
  if (!bounds) {
    emptyChart(doc, svg, hidden.size ? 'Selected series are hidden; use the series controls to restore them' : 'No valid current marks in these observations');
    for (const item of series) svg.appendChild(svgNode(doc, 'g', {'data-chart-series': item.key, 'data-chart-label': item.label}));
    return;
  }
  const grid = axes(doc, svg, bounds, rows.length, yLabel, xLabel, false, rows.map((row, index) => row.sequence ?? index + 1));
  for (const limit of limits) svg.appendChild(svgNode(doc, 'line', {x1: grid.left, x2: grid.right, y1: grid.y(limit), y2: grid.y(limit), stroke: COLORS.limit, 'stroke-width': 1, class: 'neutral-chart-limit'}));
  for (const item of series) {
    const group = svgNode(doc, 'g', {'data-chart-series': item.key, 'data-chart-label': item.label});
    if (hidden.has(item.key)) { svg.appendChild(group); continue; }
    let path = '', previous = false;
    rows.forEach((row, index) => {
      const value = row[item.key]; if (value === null || value === undefined) { previous = false; return; }
      path += `${previous ? 'L' : 'M'}${grid.x(index).toFixed(2)},${grid.y(value).toFixed(2)} `; previous = true;
    });
    if (path) group.appendChild(svgNode(doc, 'path', {d: path.trim(), fill: 'none', stroke: item.color, 'stroke-width': 2}));
    rows.forEach((row, index) => {
      const value = row[item.key]; if (value === null || value === undefined) return;
      const breach = limits.length && (Number(value) < Math.min(...limits) || Number(value) > Math.max(...limits));
      const information = `${item.label}: ${labelPoint(value)}. Sample ${row.sequence ?? index + 1}. Observed ${when(row.observed_at)}.${breach ? ' Outside configured limits.' : ''}`;
      const point = svgNode(doc, 'circle', {cx: grid.x(index), cy: grid.y(value), r: rows.length > 300 ? 2 : 3,
        fill: breach ? COLORS.negative : item.color, 'data-chart-point': true, 'data-chart-detail': information});
      title(doc, point, information); group.appendChild(point);
    });
    svg.appendChild(group);
  }
}

function build(doc, root) {
  const dom = {};
  const el = (tag, id, className, text) => { const item = make(doc, tag, {...(id ? {id} : {}), ...(className ? {class: className} : {})}, text); if (id) dom[id] = item; return item; };
  const shell = el('div', null, 'neutral-shell');
  const heading = el('div', null, 'neutral-heading'), headingText = el('div');
  append(headingText, el('p', null, 'neutral-eyebrow', 'Hourly BTC / KXBTCD / Paper research'),
    el('h2', 'neutral-title', null, 'Hourly neutral-spread experiment'),
    el('p', null, 'neutral-intro', 'A separate experiment in distinct-strike hourly BTC contracts. Inspect simulated executions, repair costs and official-outcome reconciliation independently of the 15-minute strategies and actual account.'));
  append(heading, headingText, el('span', 'neutral-mode', 'neutral-mode', 'PAPER RESEARCH'));
  shell.appendChild(heading);
  const toolbar = el('div', null, 'neutral-toolbar');
  const sourceLabel = el('label', null, 'neutral-control', 'Data source');
  const select = el('select', 'neutral-source');
  append(select, make(doc, 'option', {value: 'report'}, 'Configured paper / replay report'), make(doc, 'option', {value: 'demo'}, 'Synthetic example'));
  select.value = 'report'; sourceLabel.appendChild(select);
  append(toolbar, sourceLabel, el('button', 'neutral-refresh', 'neutral-button', 'Refresh report'), el('button', 'neutral-demo', 'neutral-button secondary', 'Load synthetic example'));
  dom['neutral-refresh'].setAttribute('type', 'button'); dom['neutral-demo'].setAttribute('type', 'button'); shell.appendChild(toolbar);
  const status = el('p', 'neutral-status', 'neutral-status', 'Waiting for a neutral research report.'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  append(shell, status, el('p', 'neutral-provenance', 'neutral-provenance', 'No session loaded.'),
    el('p', 'neutral-evidence', 'neutral-evidence', 'Synthetic data is loaded only when you choose it. No controls in this panel submit exchange orders.'));
  const summary = el('div', null, 'neutral-summary');
  const fields = [
    ['liquidation-pnl', 'Net bid-side P&L', 'An executable mark only when all held contracts have visible depth.'],
    ['realized-pnl', 'Realized P&L', 'Closed lots, after allocated entry and exit fees.'],
    ['fair-pnl', 'Model-marked P&L', 'A model valuation, separately from available sale prices.'],
    ['fees', 'Simulated fees paid', 'Already included in cash and P&L.'],
    ['drawdown', 'Bid-side drawdown', 'From observed full-depth liquidation marks only.'],
    ['delta', 'Current residual delta', '$ payout per $1 BTC move; actual paper inventory.'],
    ['fill-rate', 'Contract fill rate', 'Filled contracts / requested contracts in recorded baskets.'],
    ['repair', 'Basket repair', 'Incomplete fills must be reduced before adding risk.']
  ];
  for (const [id, caption, note] of fields) { const card = el('article', null, 'neutral-kpi'); append(card, el('span', null, 'neutral-kpi-label', caption), el('strong', `neutral-${id}`, null, 'Unavailable'), el('span', id === 'drawdown' ? 'neutral-drawdown-note' : null, 'neutral-kpi-note', note)); summary.appendChild(card); }
  shell.appendChild(summary);
  const experimentSection = el('section', 'neutral-experiment', 'neutral-experiment');
  experimentSection.setAttribute('aria-labelledby', 'neutral-experiment-title');
  append(experimentSection, el('h3', 'neutral-experiment-title', null, 'Experiment scorecard'),
    el('p', 'neutral-experiment-status', 'neutral-evidence', 'Official outcome evidence has not been supplied.'));
  const experimentMetrics = el('div', null, 'neutral-summary neutral-experiment-metrics');
  for (const [id, caption, note] of [
    ['final-pnl', 'Completed experiment P&L', 'Available after closure and reconciliation of all inventory.'],
    ['settlement-pnl', 'Settlement realization', 'Payout minus remaining premium and allocated entry fees.'],
    ['outcome-count', 'Officially resolved markets', 'Thresholds in one event share an outcome; these are not independent trials.'],
    ['remaining-basis', 'Open inventory cost basis', 'Unresolved premium and entry fees still allocated to holdings.']]) {
    const card = el('article', null, 'neutral-kpi');
    append(card, el('span', null, 'neutral-kpi-label', caption), el('strong', `neutral-${id}`, null, 'Unavailable'),
      el('span', null, 'neutral-kpi-note', note)); experimentMetrics.appendChild(card);
  }
  append(experimentSection, experimentMetrics, el('p', 'neutral-experiment-capital', 'neutral-evidence', 'No reconciled experiment capital is available.'),
    el('p', 'neutral-experiment-coverage', 'neutral-evidence', 'A model probability is never a settlement outcome.'));
  shell.appendChild(experimentSection);
  const decisions = el('div', null, 'neutral-decisions');
  const actual = el('article', null, 'neutral-decision'); append(actual, el('h3', null, null, 'Current inventory'), el('strong', 'neutral-inventory', null, 'No recorded observation'), el('p', 'neutral-risk', 'neutral-risk-target', 'Current risk is unavailable.'), el('p', 'neutral-capital', null, 'Cash and starting capital are unavailable.'));
  const proposed = el('article', null, 'neutral-decision'); append(proposed, el('h3', null, null, 'Next decision / not a fill'), el('strong', 'neutral-decision-title', null, 'Waiting'), el('p', 'neutral-decision-reason', null, 'A valid research snapshot is required.'), el('p', 'neutral-proposed', 'neutral-risk-target', 'No proposed basket.'), el('p', 'neutral-constraints', null, 'No candidate diagnostics.'));
  append(decisions, actual, proposed); append(shell, decisions, el('p', 'neutral-activity', 'neutral-evidence', 'No recorded evaluation or basket counts.'));
  const charts = el('div', null, 'neutral-chart-grid');
  function card(id, name, caption, legends = []) {
    const article = el('article', null, 'neutral-chart-card'); article.appendChild(el('h3', null, null, name));
    if (legends.length) { const legend = el('div', null, 'neutral-legend'); for (const [text, color] of legends) { const item = el('span'); item.setAttribute('style', `color:${color}`); append(item, el('i'), el('span', null, null, text)); legend.appendChild(item); } article.appendChild(legend); }
    const svg = svgNode(doc, 'svg', {id, viewBox: '0 0 660 240', role: 'img', 'aria-label': name}); dom[id] = svg; article.appendChild(svg);
    const description = el('p', `${id}-caption`, 'neutral-chart-caption', caption); svg.setAttribute('aria-describedby', `${id}-caption`); article.appendChild(description); charts.appendChild(article); emptyChart(doc, svg, 'Waiting for recorded observations'); return article;
  }
  card('neutral-pnl-chart', 'P&L after fees', 'One step per observed sample, with original times in each point. Missing liquidation marks remain gaps.', [['Net bid mark', COLORS.bid], ['Model mark', COLORS.model], ['Realized', COLORS.realized]]);
  const exposure = card('neutral-exposure-chart', 'Residual exposure & configured limits', 'Current paper inventory exposure. Dashed lines show the configured final absolute limit; candidate targets are not actual fills.', [['Actual exposure', COLORS.model], ['Final limits', COLORS.limit]]);
  const riskLabel = el('label', null, 'neutral-control', 'Exposure metric'), riskSelect = el('select', 'neutral-risk-metric');
  append(riskSelect, make(doc, 'option', {value: 'delta'}, 'Delta / $1 BTC'), make(doc, 'option', {value: 'gamma'}, 'Gamma / $1 BTC squared')); riskSelect.value = 'delta'; riskLabel.appendChild(riskSelect); exposure.appendChild(riskLabel);
  const scenario = card('neutral-scenario-chart', 'Joint settlement states', 'Cash + state payout - initial bankroll. Joint states are supplied by the shared research distribution, not independent contract bets. Missing state values retain their positions.');
  const scenarioLabel = el('label', null, 'neutral-control', 'State metric'), scenarioSelect = el('select', 'neutral-scenario-metric');
  append(scenarioSelect, make(doc, 'option', {value: 'pnl'}, 'Total strategy P&L'), make(doc, 'option', {value: 'probability'}, 'Model state probability')); scenarioSelect.value = 'pnl'; scenarioLabel.appendChild(scenarioSelect); scenario.appendChild(scenarioLabel);
  card('neutral-fee-chart', 'Trading costs by basket', 'Fees on simulated fills, in basket order. Inspect each bar for filled/requested quantity and gross turnover. Fees are not subtracted twice.', [['Fees', COLORS.fee]]);
  shell.appendChild(charts);
  const tables = el('div', null, 'neutral-tables');
  function table(id, caption, headings) {
    const scroll = el('div', null, 'neutral-table-wrap'), table = el('table', null, 'neutral-table'); table.appendChild(el('caption', null, null, caption));
    const head = el('thead'), tr = el('tr'); for (const text of headings) { const th = el('th', null, null, text); th.setAttribute('scope', 'col'); tr.appendChild(th); } head.appendChild(tr);
    const body = el('tbody', id); append(table, head, body); scroll.appendChild(table); tables.appendChild(scroll); return body;
  }
  table('neutral-contracts', 'Contracts, model inputs & actual paper holdings', ['Contract / strike', 'Held outcome', 'Contracts', 'Model YES', 'Held delta', 'Held gamma']);
  table('neutral-actions', 'Recent paper basket outcomes', ['Basket / time', 'Action', 'Filled / requested', 'Outcome', 'Fees', 'Cash after']);
  table('neutral-settlements', 'Official outcomes applied to paper inventory', ['Market / official result', 'Settled holding', 'Payout', 'Remaining basis', 'Realized P&L', 'Outcome received']);
  shell.appendChild(tables);
  const settings = el('details', null, 'neutral-math'); append(settings, el('summary', null, null, 'Model & experiment settings'), el('div', 'neutral-settings', 'neutral-settings'));
  shell.appendChild(settings);
  const maths = el('details', null, 'neutral-math'); maths.appendChild(el('summary', null, null, 'What the charts measure & how the spread is chosen'));
  append(maths, el('code', null, null, 'Delta = Σ position_j × ∂p_j/∂BTC    |    Gamma = Σ position_j × ∂²p_j/∂BTC²'),
    el('p', null, null, 'Distinct BTC thresholds share one settlement distribution. The planner searches whole-contract combinations with opposing delta contributions, fee-adjusted joint utility, final delta/gamma limits and temporary exposure limits for either leg filling alone. Local delta neutrality does not remove jumps, gamma, volatility, basis or legging risk.'),
    el('code', null, null, 'P&L_s = current cash + Σ contract payout in state s − initial bankroll'),
    el('p', null, null, 'Model-marked P&L uses modeled expected payouts. Bid-side P&L values the entire open inventory at visible sale depth after estimated exit fees. Realized P&L includes closed basis and paid fees. The joint-state chart includes current cash, so it differs from remaining-open-lot profit in a standalone payoff report.'),
    el('code', null, null, 'Joint target utility = Σ probability_s × log[B + (terminal wealth_s − B) / Kelly multiplier]'),
    el('p', null, null, 'B is a fixed paper bankroll reference across scans. Evaluating once per second does not require an order each second. An unchanged aggregate target can correctly result in HOLD. The plotted sequence skips no observed samples and fills no missing history.'));
  shell.appendChild(maths);
  const limitationDetails = el('details', null, 'neutral-math');
  const limitations = el('p', 'neutral-limitations', 'neutral-limitations', 'This panel evaluates uncalibrated paper research. It does not demonstrate profitable live execution or authorize exchange orders.');
  append(limitationDetails, el('summary', null, null, 'Research assumptions and limitations'), limitations); shell.appendChild(limitationDetails);
  const footer = el('div', null, 'neutral-footer'); append(footer, el('span', 'neutral-history-coverage', null, 'No history loaded.'), el('span', 'neutral-assumptions', null, 'No simulation assumptions loaded.')); shell.appendChild(footer);
  root.replaceChildren(shell); return dom;
}

export function mountNeutral(doc = document, win = window, options = {}) {
  const root = doc.getElementById('neutral-root'); if (!root) return null;
  const dom = build(doc, root), fetcher = options.fetch ?? win.fetch?.bind(win), pollMs = options.pollMs ?? 2000;
  let selectedSource = 'report', report = null, displayed = null, generation = 0, controller = null, timer = null, stopped = false;
  let cacheKey = null, valueCache = new Map();
  const hiddenSeries = new Map();
  const node = id => dom[`neutral-${id}`];
  const set = (id, value) => { if (node(id)) node(id).textContent = String(value); };
  function chartScope(id, value, detail = null) {
    node(id).setAttribute('data-chart-scope', JSON.stringify(['neutral', value.source, value.input_source,
      value.session_id ?? 'legacy', value.model.kind ?? null, value.model.source ?? null, value.model.model_id ?? null,
      value.summary.initial_bankroll, value.config, value.limits, detail]));
  }
  function stale(element, value, timestamp = null) {
    element?.setAttribute('data-stale', String(value)); if (element) element.title = value && timestamp !== null ? `Last known observation: ${when(timestamp)}` : '';
  }
  function field(id, value, format, observation) {
    const element = node(id); if (!element) return;
    if (value === null || value === undefined) {
      const old = valueCache.get(id);
      if (old) { element.textContent = old.text; stale(element, true, old.time); }
      else { element.textContent = 'Unavailable'; stale(element, false); }
    } else { const text = format(value); element.textContent = text; valueCache.set(id, {text, time: observation}); stale(element, false); }
    element.className = ['liquidation-pnl', 'realized-pnl', 'fair-pnl'].includes(id) && value != null && Number(value) !== 0
      ? Number(value) > 0 ? 'neutral-positive' : 'neutral-negative'
      : ['drawdown', 'fees'].includes(id) && Number(value) > 0 ? 'neutral-limit-crossed' : '';
  }
  function reset() {
    report = displayed = null; cacheKey = null; valueCache = new Map(); hiddenSeries.clear();
    for (const id of ['liquidation-pnl', 'realized-pnl', 'fair-pnl', 'fees', 'drawdown', 'delta', 'fill-rate', 'repair', 'final-pnl', 'settlement-pnl', 'outcome-count', 'remaining-basis']) { set(id, 'Unavailable'); stale(node(id), false); }
    set('mode', 'PAPER RESEARCH'); set('provenance', 'No session loaded.'); set('inventory', 'No recorded observation'); set('risk', 'Current risk is unavailable.'); set('capital', 'Cash and starting capital are unavailable.');
    set('drawdown-note', 'From observed full-depth liquidation marks only.'); node('settings').replaceChildren();
    set('evidence', 'Synthetic data is loaded only when you choose it. No controls in this panel submit exchange orders.');
    set('limitations', 'This panel evaluates uncalibrated paper research. It does not demonstrate profitable live execution or authorize exchange orders.');
    set('decision-title', 'Waiting'); set('decision-reason', 'A valid research snapshot is required.'); set('proposed', 'No proposed basket.'); set('constraints', 'No candidate diagnostics.');
    set('activity', 'No recorded evaluation or basket counts.');
    set('experiment-status', 'Official outcome evidence has not been supplied.');
    set('experiment-capital', 'No reconciled experiment capital is available.');
    set('experiment-coverage', 'A model probability is never a settlement outcome.');
    for (const id of ['pnl-chart', 'exposure-chart', 'scenario-chart', 'fee-chart']) { emptyChart(doc, node(id), 'No observations for this source'); node(id).setAttribute('data-chart-scope', ''); stale(node(id), false); }
    node('contracts').replaceChildren(); node('actions').replaceChildren(); node('settlements').replaceChildren();
    set('history-coverage', 'No history loaded.'); set('assumptions', 'No simulation assumptions loaded.'); stale(node('status'), false); root.setAttribute('data-stale', 'false');
  }
  function retain(reason) {
    report = null;
    if (!displayed) { set('status', reason); return; }
    set('status', `${reason} Last known observation ${when(displayed.observed_at)} is retained for inspection.`);
    stale(node('status'), true, displayed.observed_at); root.setAttribute('data-stale', 'true');
    for (const [id, saved] of valueCache) stale(node(id), true, saved.time);
    for (const id of ['pnl-chart', 'exposure-chart', 'scenario-chart', 'fee-chart', 'contracts', 'actions', 'settlements', 'experiment-status', 'experiment-capital', 'experiment-coverage']) stale(node(id), true, displayed.observed_at);
  }
  function drawScenarios(value) {
    const svg = node('scenario-chart'), rows = value.scenarios, probabilityView = node('scenario-metric').value === 'probability';
    const key = probabilityView ? 'probability' : 'total_pnl';
    chartScope('scenario-chart', value, [key, value.contracts.map(contract => [contract.ticker, contract.strike, contract.window_end]).sort((a, b) => a[0].localeCompare(b[0]))]);
    set('scenario-chart-caption', probabilityView
      ? 'Supplied probabilities under the shared research model, not empirical outcome frequencies. Each state belongs to the same joint distribution. Missing probabilities are marked N/A.'
      : 'Cash + state payout - initial bankroll. Joint states are supplied by the shared research distribution, not independent contract bets. Missing P&L values are marked N/A.');
    if (!rows.length || rows.every(row => row[key] === null)) { emptyChart(doc, svg, probabilityView ? 'No supplied joint state probabilities' : 'No complete joint scenario assessment'); return; }
    const strikes = value.contracts.map(c => c.strike).filter(v => v !== null).map(Number).sort((a, b) => a - b);
    const intervals = rows.map(row => /^threshold_interval_(\d+)$/.exec(row.name));
    const distinctRanks = new Set(intervals.filter(Boolean).map(match => Number(match[1])));
    const thresholdStates = strikes.length > 0 && strikes.length === value.contracts.length && new Set(strikes).size === strikes.length
      && rows.length === strikes.length + 1 && intervals.every(Boolean) && distinctRanks.size === rows.length
      && intervals.every(match => Number(match[1]) >= 0 && Number(match[1]) <= strikes.length);
    const names = rows.map((row, index) => {
      if (!thresholdStates) return {long: row.name, short: row.name};
      const rank = Number(intervals[index][1]), compact = amount => Number(amount).toLocaleString('en-US', {maximumFractionDigits: 2});
      if (rank === 0) return {long: `Below ${dollars(strikes[0])}`, short: `< ${compact(strikes[0])}`};
      if (rank === strikes.length) return {long: `Above ${dollars(strikes.at(-1))}`, short: `> ${compact(strikes.at(-1))}`};
      return {long: `Between ${dollars(strikes[rank - 1])} and ${dollars(strikes[rank])}`, short: `${compact(strikes[rank - 1])}–${compact(strikes[rank])}`};
    });
    const bounds = probabilityView ? [0, 1] : extent(rows.map(row => row[key]));
    const grid = axes(doc, svg, bounds, rows.length, probabilityView ? 'Model state probability (%)' : 'Total strategy P&L ($)', 'Joint settlement state', true,
      names.map((name, index) => rows.length <= 7 && name.short.length <= 23 ? name.short : index + 1), probabilityView ? amount => `${(100 * amount).toFixed(0)}%` : null, rows.length <= 7);
    const width = Math.max(3, Math.min(60, (grid.right - grid.left) / rows.length * .64));
    rows.forEach((row, index) => {
      const detail = `${names[index].long}. Total strategy P&L ${signedDollars(row.total_pnl)}. Model state probability ${percentage(row.probability)}. Terminal wealth ${dollars(row.terminal_wealth)}.${row[key] === null ? ` ${probabilityView ? 'Probability' : 'P&L'} unavailable; no value is imputed.` : ''}`;
      const metadata = {'data-chart-point': true, 'data-chart-detail': detail, 'data-chart-series': probabilityView ? 'scenario-probability' : 'scenario-pnl', 'data-chart-label': probabilityView ? 'Model state probability' : 'Total strategy state P&L'};
      let point;
      if (row[key] === null) {
        point = svgNode(doc, 'text', {x: grid.x(index), y: grid.bottom - 5, 'text-anchor': 'middle', class: 'neutral-chart-axis', ...metadata}, 'N/A');
      } else {
        const amount = Number(row[key]);
        point = svgNode(doc, 'rect', {x: grid.x(index) - width / 2, y: Math.min(grid.y(amount), grid.y(0)), width,
          height: Math.max(1, Math.abs(grid.y(amount) - grid.y(0))), fill: probabilityView ? COLORS.model : amount >= 0 ? COLORS.positive : COLORS.negative, ...metadata});
      }
      title(doc, point, detail); svg.appendChild(point);
    });
  }
  function drawFees(value) {
    const svg = node('fee-chart'), rows = value.actions;
    chartScope('fee-chart', value);
    if (!rows.length) { emptyChart(doc, svg, 'No completed paper basket attempts'); return; }
    const fees = rows.map(row => row.legs.reduce((sum, l) => sum + Number(l.fill?.fee ?? 0), 0));
    const offset = value.coverage.display_omitted_actions;
    const bounds = extent(fees), grid = axes(doc, svg, bounds, rows.length, 'Fees ($)', 'Recorded basket attempt', true, rows.map((_, index) => offset + index + 1));
    const width = Math.max(2, Math.min(40, (grid.right - grid.left) / rows.length * .64));
    rows.forEach((row, index) => {
      const requested = row.legs.reduce((sum, l) => sum + (l.requested_count ?? 0), 0), filled = row.legs.reduce((sum, l) => sum + (l.filled_count ?? 0), 0), turnover = row.legs.reduce((sum, l) => sum + Number(l.fill?.gross ?? 0), 0);
      const information = `Basket ${offset + index + 1}: ${pretty(row.status)}, ${filled}/${requested} contracts. Fees ${dollars(fees[index])}; gross turnover ${dollars(turnover)}. Observed ${when(row.fill_time)}.`;
      const bar = svgNode(doc, 'rect', {x: grid.x(index) - width / 2, y: Math.min(grid.y(fees[index]), grid.y(0)), width,
        height: Math.max(1, Math.abs(grid.y(fees[index]) - grid.y(0))), fill: COLORS.fee,
        'data-chart-point': true, 'data-chart-detail': information, 'data-chart-series': 'basket-fees', 'data-chart-label': 'Fees on filled contracts'});
      title(doc, bar, information); svg.appendChild(bar);
    });
  }
  function drawExposure(value) {
    const metric = node('risk-metric').value === 'gamma' ? 'gamma' : 'delta', limit = value.limits[`max_abs_${metric}`];
    chartScope('exposure-chart', value, metric);
    lineChart(doc, node('exposure-chart'), value.history, [{key: `residual_${metric}`, label: `Actual residual ${metric}`, color: COLORS.model}],
      {yLabel: metric === 'delta' ? '$ payout / $1 BTC' : '$ payout / $1 BTC²', xLabel: 'Observed sample', labelPoint: greek,
        limits: limit === null ? [] : [Number(limit), -Number(limit)], hidden: hiddenSeries.get('exposure-chart') ?? new Set()});
  }
  function drawPnl(value) {
    chartScope('pnl-chart', value);
    lineChart(doc, node('pnl-chart'), value.history,
      [{key: 'liquidation_pnl', label: 'Net bid-side P&L', color: COLORS.bid},
       {key: 'fair_value_pnl', label: 'Model-marked P&L', color: COLORS.model},
       {key: 'realized_pnl', label: 'Realized P&L', color: COLORS.realized}],
      {yLabel: 'P&L ($)', xLabel: 'Observed sample', labelPoint: signedDollars,
        hidden: hiddenSeries.get('pnl-chart') ?? new Set()});
  }
  function cell(row, text, sub = null) { const td = make(doc, 'td', {}, text); if (sub) td.appendChild(make(doc, 'span', {class: 'neutral-subcell'}, sub)); row.appendChild(td); }
  function emptyTable(body, columns, text) { const row = make(doc, 'tr'), td = make(doc, 'td', {colspan: columns, class: 'neutral-empty-cell'}, text); row.appendChild(td); body.replaceChildren(row); }
  function renderTables(value) {
    const body = node('contracts'); body.replaceChildren();
    if (!value.contracts.length) emptyTable(body, 6, 'No normalized contract observations are included in this report.');
    for (const item of value.contracts) {
      const row = make(doc, 'tr'); cell(row, item.ticker, item.strike === null ? 'Strike unavailable' : `Strike ${dollars(item.strike)}`);
      cell(row, item.side === null ? 'Flat' : item.side.toUpperCase()); cell(row, item.quantity); cell(row, percentage(item.probability_yes));
      cell(row, item.delta_yes === null ? 'Unavailable' : greek(Number(item.delta_yes) * item.position)); cell(row, item.gamma_yes === null ? 'Unavailable' : greek(Number(item.gamma_yes) * item.position)); body.appendChild(row);
    }
    const actions = node('actions'); actions.replaceChildren();
    if (!value.actions.length) emptyTable(actions, 6, 'No completed simulated basket attempts. An evaluated plan is not a fill.');
    for (let i = value.actions.length - 1; i >= Math.max(0, value.actions.length - 12); i--) {
      const action = value.actions[i], row = make(doc, 'tr');
      cell(row, `#${value.coverage.display_omitted_actions + i + 1}`, when(action.fill_time)); cell(row, pretty(action.action), action.legs.map(l => l.ticker).join(' · '));
      cell(row, `${action.legs.reduce((s, l) => s + (l.filled_count ?? 0), 0)} / ${action.legs.reduce((s, l) => s + (l.requested_count ?? 0), 0)}`);
      cell(row, pretty(action.status), action.reason); cell(row, dollars(action.legs.reduce((s, l) => s + Number(l.fill?.fee ?? 0), 0))); cell(row, dollars(action.cash_after)); actions.appendChild(row);
    }
    const settlements = node('settlements'); settlements.replaceChildren();
    const rows = value.experiment?.settlements ?? [];
    if (!rows.length) emptyTable(settlements, 6, value.experiment?.official_outcomes_available
      ? 'No finalized exchange outcomes received. Open inventory remains unresolved.'
      : 'This report supplies no official outcome evidence. Forecasts and model marks do not resolve inventory.');
    for (const outcome of [...rows].sort((a, b) => b.received_at - a.received_at).slice(0, 20)) {
      const row = make(doc, 'tr'); row.setAttribute('title', `Official settlement ${when(outcome.settled_at)}; evidence ${outcome.evidence_sha256}`);
      cell(row, outcome.ticker, `Finalized ${outcome.result.toUpperCase()}${outcome.synthetic ? ' / SYNTHETIC' : ''}`);
      cell(row, outcome.quantity ? `${Math.abs(outcome.quantity)} ${outcome.quantity > 0 ? 'YES' : 'NO'}` : 'No held inventory');
      cell(row, dollars(outcome.payout)); cell(row, dollars(Number(outcome.premium) + Number(outcome.entry_fees)), 'Includes allocated entry fees');
      cell(row, signedDollars(outcome.realized_pnl)); cell(row, when(outcome.received_at)); settlements.appendChild(row);
    }
  }
  function renderExperiment(value) {
    const e = value.experiment, at = value.observed_at;
    const states = {unavailable: 'Official outcome evidence unavailable', collecting: 'Collecting paper evidence; official outcomes pending', blocked: 'Official outcome reconciliation blocked; review recorded evidence before interpreting final returns',
      awaiting_settlement: 'Experiment closing; remaining official outcomes or inventory reconciliation pending', complete: 'Closed paper experiment; official outcomes and final cash reconciled'};
    set('experiment-status', `${states[e?.status ?? 'unavailable']}. ${value.synthetic ? 'Synthetic research example.' : 'Simulated fills; official outcomes do not turn these into actual account returns.'}`);
    if (e && e.status !== 'complete') {
      // Explicitly withheld/reviewed evidence invalidates a prior final result.
      // Transport failures still retain the whole last-known report elsewhere.
      valueCache.delete('final-pnl');
      set('final-pnl', e.status === 'blocked' ? 'Under review' : e.status === 'unavailable' ? 'Unavailable' : 'Awaiting settlement');
      node('final-pnl').className = ''; stale(node('final-pnl'), false);
    } else field('final-pnl', e?.completed_experiment_pnl, signedDollars, at);
    field('settlement-pnl', e?.settlement_realized_pnl, signedDollars, at);
    field('outcome-count', e?.official_resolved_markets, number => String(number), at);
    field('remaining-basis', e?.remaining_basis, dollars, at);
    set('experiment-capital', e ? `Initial capital ${dollars(e.initial_capital)} · Cash ${dollars(e.cash)} · Net realized ${signedDollars(e.net_realized_pnl)} · Trading reductions ${signedDollars(e.trading_realized_pnl)} · Settlement payouts ${dollars(e.settlement_payouts)} · Paid fees ${dollars(e.fees)} (already included). Completed return ${percentage(e.completed_return_fraction)}.` : 'No reconciled experiment capital is available.');
    set('experiment-coverage', e ? `${e.open_markets} open markets · ${e.settled_inventory_markets ?? 'Unknown'} markets settled with inventory · ${e.settled_lot_wins ?? 'Unknown'} positive / ${e.settled_lot_losses ?? 'unknown'} negative / ${e.settled_lot_flat ?? 'unknown'} flat settled lots. Lots in the same event are dependent. ${e.execution_totals_complete ? 'Execution counters cover the whole session.' : 'Execution counters cover the retained action window.'} ${e.session_resume_supported ? 'Dedicated paper journal supports restart recovery.' : 'Restart recovery is not established by this report.'} ${e.last_outcome_received_at === null ? 'No official outcome receipt yet.' : `Latest official outcome received ${when(e.last_outcome_received_at)}.`} The table shows the most recent 20 outcome records.` : 'A model probability is never a settlement outcome. Final return requires official outcomes and a closed, flat experiment.');
  }
  function renderSettings(value) {
    const body = node('settings'); body.replaceChildren();
    const rows = [['Model', value.model.model_id ?? 'Unavailable'], ['Settlement source', value.model.source ?? 'Unavailable'],
      ['Shared-average mean', dollars(value.model.mean)], ['Shared-average standard deviation', dollars(value.model.standard_deviation)],
      ['Mean / spot slope', greek(value.model.mean_spot_slope)], ['Kelly multiplier', value.limits.kelly_fraction ?? 'Unavailable'],
      ['Starting paper bankroll', dollars(value.summary.initial_bankroll)], ['Pair debit ceiling', dollars(value.limits.max_pair_cost)],
      ['Worst terminal-loss ceiling', dollars(value.limits.max_worst_case_loss)], ['Temporary delta ceiling', greek(value.limits.max_unhedged_delta)],
      ['Contracts per leg / position', `${value.limits.max_leg_contracts ?? 'Unavailable'} / ${value.limits.max_position_contracts ?? 'Unavailable'}`],
      ['Minimum equivalent improvement', dollars(value.limits.min_expected_improvement)]];
    for (const [name, text] of rows) { const item = make(doc, 'div'); append(item, make(doc, 'span', {}, name), make(doc, 'strong', {}, text)); body.appendChild(item); }
  }
  function render(value) {
    const key = JSON.stringify([value.source, value.session_id ?? 'legacy', value.input_source,
      value.model.model_id ?? null, value.config, value.limits]);
    if (cacheKey !== key || value.session_id === null && displayed?.observed_at != null && value.observed_at < displayed.observed_at) { reset(); cacheKey = key; }
    displayed = value; report = value.status === 'ready' ? value : null;
    const isStale = value.status === 'stale'; root.setAttribute('data-stale', String(isStale));
    set('mode', value.synthetic ? 'SYNTHETIC / PAPER' : value.input_source === 'replay' ? 'RECORDED REPLAY / PAPER' : value.input_source === 'publisher_snapshot' ? 'ACTUAL MARKET DATA / PAPER' : 'RECORDED PAPER RESEARCH');
    set('status', `${value.reason}${value.status === 'stale' ? ' This is last-known research data, not a fresh market signal.' : ''}`); stale(node('status'), isStale, value.observed_at);
    set('provenance', `${value.synthetic ? 'Synthetic example; no account data' : value.input_source === 'replay' ? 'Recorded replay; historical observations' : value.input_source === 'publisher_snapshot' ? 'Producer reports actual market inputs; execution is simulated' : 'Configured paper report; no exchange execution'} · Original observation ${when(value.observed_at)} · Session ${value.session_id ? value.session_id.slice(0, 16) : 'identity unavailable'}${value.model.model_id ? ` · Model ${value.model.model_id}` : ''}`);
    set('evidence', `${value.coverage.reason} ${value.coverage.accounting_basis} Net P&L already includes simulated paid fees. Model marks are not executable sale prices. ${value.experiment?.official_outcomes_available ? 'Official outcomes, when received, resolve only the recorded paper inventory.' : 'This report supplies no official settlement evidence.'}`);
    const s = value.summary, at = value.observed_at;
    field('liquidation-pnl', s.liquidation_pnl, signedDollars, at); field('realized-pnl', s.realized_pnl, signedDollars, at); field('fair-pnl', s.fair_value_pnl, signedDollars, at);
    field('fees', s.fees, dollars, at); field('drawdown', s.drawdown_fraction, percentage, at); field('delta', value.risk.residual_delta, greek, at); field('fill-rate', s.fill_rate, percentage, at);
    set('drawdown-note', `Session maximum ${percentage(s.max_drawdown_fraction)}. Full-depth bid marks only; missing marks remain gaps.`);
    field('repair', value.repair_required, v => v ? 'Repair required' : value.pending ? 'Basket pending' : 'No repair pending', at);
    set('inventory', `${s.open_contracts ?? 'Unknown'} held contracts across ${s.open_markets ?? 'unknown'} markets${value.pending ? ' · pending basket' : ''}`);
    set('risk', `Current delta ${greek(value.risk.residual_delta)} / limit ±${greek(value.limits.max_abs_delta)} · Gamma ${greek(value.risk.residual_gamma)} / limit ±${greek(value.limits.max_abs_gamma)} · Worst terminal loss ${dollars(value.risk.worst_case_loss)} · Modeled CVaR loss ${dollars(value.risk.cvar_loss)} · Modeled expected terminal profit ${signedDollars(value.risk.expected_terminal_profit)}. Near-zero delta does not remove tail loss.`);
    set('capital', `Paper cash ${dollars(s.cash)} · Initial bankroll ${dollars(s.initial_bankroll)} · Gross turnover ${dollars(s.turnover)} · Return on full-depth bid mark ${percentage(s.return_fraction)}.`);
    const decision = value.last_decision;
    set('decision-title', decision ? `${pretty(decision.status)} · ${pretty(decision.action)}` : 'No candidate decision'); set('decision-reason', decision?.reason ?? 'The report contains no candidate decision.');
    set('proposed', decision?.legs.length ? `PROPOSED: ${decision.legs.map(l => `${pretty(l.action)} ${l.count ?? '?'} ${String(l.outcome ?? '').toUpperCase()} ${l.ticker}`).join(' + ')}. Target delta ${greek(decision.after.residual_delta)}; gamma ${greek(decision.after.residual_gamma)}. Not current holdings.` : 'No proposed basket. A HOLD does not create another stake.');
    const rejected = Object.entries(decision?.diagnostics?.rejected ?? {}).sort((a, b) => b[1] - a[1]);
    set('constraints', rejected.length ? `Candidate exclusions: ${rejected.slice(0, 6).map(([name, count]) => `${pretty(name)} ${count}`).join(' · ')}.` : value.pending ? 'Awaiting a later fresh book after modeled execution latency.' : 'No candidate exclusion counts recorded.');
    set('activity', `Evaluations ${s.evaluations ?? 'unavailable'} · Proposals ${s.proposals ?? 'unavailable'} · Holds ${s.holds ?? 'unavailable'} · Completed / partial / unfilled baskets ${s.completed_baskets ?? 'unavailable'} / ${s.partial_baskets ?? 'unavailable'} / ${s.unfilled_baskets ?? 'unavailable'} · Filled / requested contracts ${s.filled_contracts ?? 'unavailable'} / ${s.requested_contracts ?? 'unavailable'} · Outcome observations ${s.outcome_observations ?? 'unavailable'}. Strategy evaluations exclude official outcome observations; basket counts are not settled wins.`);
    drawPnl(value);
    drawExposure(value); drawScenarios(value); drawFees(value); renderTables(value); renderSettings(value); renderExperiment(value);
    set('history-coverage', `${value.history.length} plotted observations${value.coverage.history_start !== null ? ` since ${when(value.coverage.history_start)}` : ''} · ${value.coverage.history_basis}${value.coverage.omitted_history_points ? ` · ${value.coverage.omitted_history_points} older samples omitted` : ''}${value.coverage.omitted_actions ? ` · ${value.coverage.omitted_actions} earlier basket actions omitted` : ''}. Recent table shows at most 12 baskets; the fee chart shows the supplied action window. Summary counters can cover a longer retained window.`);
    set('assumptions', `Paper latency ${value.config.latency_seconds ?? '?'}s · Available depth ${percentage(value.config.depth_fraction)} · Evaluation ${value.config.decision_interval_seconds ?? '?'}s.`);
    set('limitations', value.limitations.length ? value.limitations.join(' ') : 'Research inputs, calibration and basket execution assumptions require validation. This panel never submits exchange orders.');
    for (const id of ['pnl-chart', 'exposure-chart', 'scenario-chart', 'fee-chart', 'contracts', 'actions', 'settlements', 'experiment-status', 'experiment-capital', 'experiment-coverage']) stale(node(id), isStale, at);
    if (isStale) for (const [id, saved] of valueCache) stale(node(id), true, saved.time);
  }
  function schedule() { if (!stopped) timer = win.setTimeout(() => {
    if (win.dashboardPollingActive?.() === false || doc.getElementById('hourly-neutral-research')?.open === false) { schedule(); return; }
    refresh();
  }, pollMs); }
  async function refresh() {
    if (stopped) return;
    const expected = ++generation, source = selectedSource; controller?.abort(); const current = new AbortController(); controller = current;
    if (timer !== null) win.clearTimeout(timer); timer = null;
    try {
      if (typeof fetcher !== 'function') fail('Unable to load the local neutral report.');
      const response = await fetcher(source === 'demo' ? '/api/neutral?source=demo' : '/api/neutral', {method: 'GET', cache: 'no-store', signal: current.signal});
      if (stopped || expected !== generation || source !== selectedSource) return;
      if (!response.ok) fail(response.status === 404 ? 'Restart the local console to load the neutral dashboard API.' : `Neutral report is unavailable (HTTP ${response.status}).`);
      const rawText = await response.text(); if (rawText.length > 8_000_000) fail('Neutral report exceeds the supported size.');
      const value = validateNeutralReport(JSON.parse(rawText), source);
      if (stopped || expected !== generation || source !== selectedSource) return;
      if (value.status === 'unavailable') { retain(value.reason); return; }
      if (displayed && value.session_id !== null && value.session_id === displayed.session_id && value.observed_at < displayed.observed_at) { retain('An older report was ignored.'); return; }
      render(value);
    } catch (error) {
      if (!stopped && expected === generation && source === selectedSource) retain(error?.message || 'Unable to read the neutral report.');
    } finally { if (!stopped && expected === generation) { controller = null; schedule(); } }
  }
  function setSource(value) {
    if (!['report', 'demo'].includes(value) || stopped) return;
    if (selectedSource !== value) { selectedSource = value; generation++; controller?.abort(); reset(); node('source').value = value; set('status', value === 'demo' ? 'Loading the explicitly selected synthetic example.' : 'Loading the configured paper report.'); }
    return refresh();
  }
  node('source').addEventListener('change', () => setSource(node('source').value));
  node('refresh').addEventListener('click', () => refresh()); node('demo').addEventListener('click', () => setSource('demo'));
  node('risk-metric').addEventListener('change', () => { hiddenSeries.delete('exposure-chart'); if (displayed) drawExposure(displayed); });
  node('scenario-metric').addEventListener('change', () => { if (displayed) drawScenarios(displayed); });
  for (const id of ['pnl-chart', 'exposure-chart']) node(id).addEventListener('chart-series-change', event => {
    const keys = event?.detail?.hiddenSeries;
    if (!Array.isArray(keys) || keys.some(key => typeof key !== 'string')) return;
    hiddenSeries.set(id, new Set(keys));
    if (displayed) { if (id === 'pnl-chart') drawPnl(displayed); else drawExposure(displayed); }
  });
  function dispose() { if (stopped) return; stopped = true; generation++; controller?.abort(); if (timer !== null) win.clearTimeout(timer); }
  win.addEventListener?.('pagehide', dispose);
  const ready = refresh();
  return {ready, refresh, dispose, setSource, getSource: () => selectedSource, getReport: () => report, getDisplayedReport: () => displayed};
}
