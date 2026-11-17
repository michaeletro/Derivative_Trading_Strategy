// Ensemble paper controls and monitoring. No model value is copied into the
// manual order form; the managed runner cannot enable exchange execution.
import {renderStrategyProbabilityChart as renderProbabilityChart, renderStrategyRollingChart as renderRollingChart,
  renderStrategyEdgeChart as renderEdgeChart, renderStrategyPortfolioChart as renderPortfolioChart} from './analytics.mjs';
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const finite = value => typeof value === 'number' && Number.isFinite(value);
const text = (value, name, max = 1000) => typeof value === 'string' && value.length <= max
  && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail(`Invalid ${name}.`);
const probability = (value, name) => finite(value) && value >= 0 && value <= 1 ? value : fail(`Invalid ${name}.`);
const nullableProbability = (value, name) => value === null ? null : probability(value, name);
const ASSETS = Object.freeze({
  BTC: {series: 'KXBTC15M', source: 'CF_BRTI'},
  ETH: {series: 'KXETH15M', source: 'CF_ETHUSD_RTI'},
  SOL: {series: 'KXSOL15M', source: 'CF_SOLUSD_RTI'},
  XRP: {series: 'KXXRP15M', source: 'CF_XRPUSD_RTI'}
});
const VIEW_ASSETS = Object.freeze({...ASSETS, GOLD: {series: 'KXGOLD15M', source: 'PYTH_1OZGOLD_USD'}});
const GOLD_MODEL_NOTE = 'Gold is a market-data view here; algorithmic entries in this CF paper panel are unavailable. Its separate Pyth minute-close runner has its own feed and model.';
const ASSET_FIELDS = Object.freeze(Object.fromEntries(Object.keys(ASSETS).map(asset => [asset, `algorithm-asset-${asset.toLowerCase()}`])));
const ROLLING_DEFAULTS = Object.freeze({window_seconds: 300, min_samples: 30, entry_z: 2, exit_z: 0.5, alpha: 0.05, phase_seconds: 300});
const ROLLING_RSI_DEFAULTS = Object.freeze({rsi_filter: false, rsi_period: 14, rsi_entry: 55});
const ROLLING_FIELDS = Object.freeze({window_seconds: 'algorithm-rolling-window', min_samples: 'algorithm-rolling-min-samples',
  entry_z: 'algorithm-rolling-entry-z', exit_z: 'algorithm-rolling-exit-z', alpha: 'algorithm-rolling-alpha', phase_seconds: 'algorithm-rolling-phase'});
const ROLLING_RSI_FIELDS = Object.freeze({rsi_filter: 'algorithm-rolling-rsi-filter',
  rsi_period: 'algorithm-rolling-rsi-period', rsi_entry: 'algorithm-rolling-rsi-entry'});
const MINUTE_FIVE = Object.freeze({entry_elapsed_seconds: 300, entry_window_seconds: 60, budget: '1',
  hold_to_settlement: true, decision: 'median_settlement_average'});
const MINUTE_FIVE_NOTE = 'At the first fresh sample from 5:00 through 5:59 after opening, buy the outcome selected by the median simulated final-minute settlement average against the contract strike and comparator. The source observation must also be at least 5:00 into the contract. Maximum $1 including entry fees, whole contracts, once per asset/window; hold to official settlement. This uses the settlement-average price median. Kelly, entry-edge and early-exit rules are not used.';
const FIXED_UNUSED_FIELDS = Object.freeze(['algorithm-weight-long-run', 'algorithm-weight-recent', 'algorithm-weight-ewma',
  'algorithm-side', 'algorithm-fraction', 'algorithm-take-profit', 'algorithm-entry-edge', 'algorithm-exit-edge', 'algorithm-exit-fee']);
function minuteFiveSettings(raw = MINUTE_FIVE) {
  if (!object(raw) || Object.keys(raw).length !== Object.keys(MINUTE_FIVE).length
      || Object.keys(MINUTE_FIVE).some(key => raw[key] !== MINUTE_FIVE[key])) fail('Unsupported minute-five timing or budget.');
  return {...MINUTE_FIVE};
}
function rollingDisplayValue(key, value) {
  let rendered = String(value);
  const exponent = /^(\d+)(?:\.(\d+))?e([+-]?\d+)$/i.exec(rendered);
  if (exponent) {
    const digits = exponent[1] + (exponent[2] || ''), point = exponent[1].length + Number(exponent[3]);
    rendered = point <= 0 ? '0.' + '0'.repeat(-point) + digits
      : point >= digits.length ? digits + '0'.repeat(point - digits.length) : digits.slice(0, point) + '.' + digits.slice(point);
  }
  if (key !== 'alpha') return rendered;
  // Shift the decimal text rather than multiplying binary floats: 0.07 must
  // remain the editable value 7%, never 7.000000000000001% after a status poll.
  const [whole, fraction = ''] = rendered.split('.'), digits = (whole + fraction).padEnd(whole.length + 2, '0');
  const rest = digits.slice(whole.length + 2).replace(/0+$/, '');
  return String(Number(digits.slice(0, whole.length + 2))) + (rest ? '.' + rest : '');
}
function rollingInputNumber(raw, key) {
  const value = typeof raw === 'string' ? raw.trim() : '';
  const integer = ['window_seconds', 'min_samples', 'phase_seconds', 'rsi_period'].includes(key);
  const pattern = integer ? /^(0|[1-9][0-9]{0,3})$/ : /^(0|[1-9][0-9]{0,3})(?:\.[0-9]{1,324})?$/;
  if (value.length > 400 || !pattern.test(value) || !finite(Number(value)) || Number(value) > (key === 'alpha' ? 25 : 1000)) fail(`Invalid rolling ${key.replaceAll('_', ' ')}.`);
  return key === 'alpha' ? Number(value) / 100 : Number(value);
}
export function validateRollingSettings(raw = ROLLING_DEFAULTS) {
  const defaults = {...ROLLING_DEFAULTS, ...ROLLING_RSI_DEFAULTS};
  if (!object(raw) || Object.keys(raw).some(key => !Object.hasOwn(defaults, key))) fail('Invalid rolling indicator settings.');
  const value = {...defaults, ...raw};
  if (typeof value.rsi_filter !== 'boolean' || Object.entries(value).some(([key, item]) => key !== 'rsi_filter' && !finite(item))) fail('Rolling indicator settings must be finite numbers with a Boolean RSI filter.');
  const entryPhaseSeconds = value.phase_seconds - (value.phase_seconds === 900 ? 60 : 0);
  const maximumSamples = Math.min(Math.floor(value.window_seconds / 5), entryPhaseSeconds / 5 - 1);
  if (!Number.isInteger(value.window_seconds) || value.window_seconds < 120 || value.window_seconds > 900
      || ![300, 900].includes(value.phase_seconds) || value.window_seconds > value.phase_seconds
      || !Number.isInteger(value.min_samples) || value.min_samples < 20 || value.min_samples > maximumSamples
      || !(value.entry_z > value.exit_z && value.exit_z >= 0 && value.entry_z <= 10)
      || !(value.alpha > 0 && value.alpha <= 0.25)) fail('Use a valid rolling window, warmup count and entry/exit thresholds.');
  if (value.alpha < 1 / (maximumSamples + 1)) fail(`This window cannot reach that tail threshold; the minimum attainable rank is ${(100 / (maximumSamples + 1)).toFixed(2)}%.`);
  if (!Number.isInteger(value.rsi_period) || value.rsi_period < 2 || value.rsi_period > Math.min(50, maximumSamples)
      || !(value.rsi_entry > 50 && value.rsi_entry < 100)) fail('Use an attainable RSI period of 2 through 50 and an entry threshold greater than 50 and below 100.');
  if (Object.entries(ROLLING_RSI_DEFAULTS).every(([key, item]) => value[key] === item)) {
    for (const key of Object.keys(ROLLING_RSI_DEFAULTS)) delete value[key];
  }
  return value;
}
function assetList(raw = ['BTC']) {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > 4 || new Set(raw).size !== raw.length
      || raw.some(asset => !Object.hasOwn(ASSETS, asset))) fail('Choose one or more supported cryptocurrencies.');
  return Object.keys(ASSETS).filter(asset => raw.includes(asset));
}
function tickerAsset(value) {
  if (typeof value !== 'string' || value.length > 128 || !/^KX(?:BTC|ETH|SOL|XRP)15M-[A-Za-z0-9_.:-]{1,119}$/.test(value)) fail('Invalid cryptocurrency contract.');
  return Object.keys(ASSETS).find(asset => value.startsWith(ASSETS[asset].series + '-'));
}
function decimal(value, name, {signed = false, max = 1e12, integer = false, places = 12} = {}) {
  const pattern = integer ? /^(0|[1-9][0-9]{0,12})$/ : new RegExp(`^${signed ? '-?' : ''}(0|[1-9][0-9]{0,12})(\\.[0-9]{1,${places}})?$`);
  if (typeof value !== 'string' || !pattern.test(value) || !Number.isFinite(Number(value)) || Math.abs(Number(value)) > max) fail(`Invalid ${name}.`);
  return value;
}
const nullableDecimal = (value, name, options) => value === null ? null : decimal(value, name, options);
// Portfolio money is serialized on a twelve-decimal grid. Keep conservation
// checks exact even when account totals are too large for float subtraction.
const MONEY_SCALE = 10n ** 12n;
function moneyUnits(value) {
  const negative = value.startsWith('-'), [whole, fractional = ''] = (negative ? value.slice(1) : value).split('.');
  const units = BigInt(whole) * MONEY_SCALE + BigInt(fractional.padEnd(12, '0'));
  return negative ? -units : units;
}
const absUnits = value => value < 0n ? -value : value;
function timestamp(value) {
  text(value, 'algorithm timestamp', 48);
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) fail('Invalid algorithm timestamp.');
  return value;
}
function forecast(raw) {
  if (raw?.kind === 'kalshi_minute_five_forecast') return minuteFiveForecast(raw);
  if (!object(raw) || raw.kind !== 'kalshi_settlement_ensemble' || raw.version !== 1
      || !['ready', 'unavailable'].includes(raw.status) || raw.calibrated !== false || raw.execution_eligible !== false) fail('Unsupported settlement ensemble.');
  const result = {status: raw.status, calibrated: false, execution_eligible: false,
    reason: text(raw.reason, 'forecast reason'), ticker: raw.ticker === null ? null : text(raw.ticker, 'forecast contract', 128),
    as_of: raw.as_of, probability_yes: nullableProbability(raw.probability_yes, 'ensemble probability'),
    conservative_probability_yes: nullableProbability(raw.conservative_probability_yes, 'decision YES probability'),
    conservative_probability_no: nullableProbability(raw.conservative_probability_no, 'decision NO probability'),
    model_disagreement: nullableProbability(raw.model_disagreement, 'model disagreement'),
    monte_carlo_se: nullableProbability(raw.monte_carlo_se, 'simulation standard error'), components: [], contract_identity: null};
  if (!Array.isArray(raw.components) || raw.components.length > 20) fail('Invalid ensemble members.');
  const ids = new Set();
  result.components = raw.components.map(row => {
    if (!object(row) || !['ready', 'disabled', 'unavailable'].includes(row.status)) fail('Invalid ensemble member.');
    const id = text(row.id, 'member ID', 100);
    if (!id.length || ids.has(id)) fail('Duplicate ensemble member.');
    ids.add(id);
    const value = {id, weight: probability(row.weight, 'member weight'),
      probability_yes: nullableProbability(row.probability_yes, 'member probability'), status: row.status};
    if (value.status === 'disabled' && value.weight !== 0 || value.status === 'ready' && value.probability_yes === null) fail('Inconsistent ensemble member.');
    return value;
  });
  if (raw.status === 'ready') {
    const asset = tickerAsset(result.ticker);
    if (raw.source !== ASSETS[asset].source || !finite(result.as_of) || result.as_of <= 0 || result.as_of > 253402300799
        || [result.probability_yes, result.conservative_probability_yes, result.conservative_probability_no,
          result.model_disagreement, result.monte_carlo_se].some(value => value === null)
        || result.conservative_probability_yes > result.probability_yes + 1e-12
        || result.conservative_probability_no > 1 - result.probability_yes + 1e-12
        || Math.abs(result.components.reduce((sum, row) => sum + row.weight, 0) - 1) > 1e-8
        || result.components.some(row => row.weight > 0 && row.status !== 'ready')) fail('Incomplete settlement ensemble.');
    result.asset = asset; result.source = raw.source;
    if (raw.model_version !== undefined) {
      result.model_version = text(raw.model_version, 'model version', 128);
      if (!result.model_version.length) fail('Missing model version.');
    }
    if (raw.config_hash !== undefined) {
      if (typeof raw.config_hash !== 'string' || !/^[a-f0-9]{64}$/.test(raw.config_hash)) fail('Invalid model configuration identifier.');
      result.config_hash = raw.config_hash;
    }
    const identity = raw.contract_identity;
    if (!object(identity) || identity.ticker !== result.ticker || identity.market_type !== 'binary'
        || !['greater', 'greater_or_equal'].includes(identity.strike_type) || identity.cap_strike !== null
        || identity.settlement_bounds_type !== 'default' || Number(identity.notional_value_dollars) !== 1) fail('Invalid ensemble contract.');
    result.contract_identity = {ticker: identity.ticker,
      target_price: decimal(identity.floor_strike, 'ensemble target', {max: 1e8}),
      open_time: timestamp(identity.open_time), close_time: timestamp(identity.close_time)};
    if (Number(result.contract_identity.target_price) <= 0 || Date.parse(result.contract_identity.close_time) - Date.parse(result.contract_identity.open_time) !== 900000) fail('Invalid ensemble window.');
  } else if (result.probability_yes !== null || result.conservative_probability_yes !== null || result.conservative_probability_no !== null) fail('Unavailable ensemble retains a current probability.');
  return result;
}
function minuteFiveForecast(raw) {
  if (!object(raw) || raw.version !== 1 || !['ready', 'unavailable'].includes(raw.status)
      || raw.calibrated !== false || raw.execution_eligible !== false || !Object.hasOwn(ASSETS, raw.asset)
      || raw.source !== ASSETS[raw.asset].source) fail('Unsupported minute-five Monte Carlo forecast.');
  const result = {kind: raw.kind, version: 1, status: raw.status, asset: raw.asset, source: raw.source,
    calibrated: false, execution_eligible: false, reason: text(raw.reason, 'Monte Carlo reason'),
    model_version: text(raw.model_version, 'Monte Carlo model', 128), ticker: raw.ticker === null ? null : text(raw.ticker, 'MC contract', 128),
    as_of: raw.as_of, model_time: raw.model_time, probability_yes: raw.probability_yes, probability_no: raw.probability_no,
    median_settlement_average: raw.median_settlement_average, settlement_centered_median: raw.settlement_centered_median,
    monte_carlo_se: raw.monte_carlo_se, paths: raw.paths, target_price: raw.target_price, strike_type: raw.strike_type,
    conservative_probability_yes: null, conservative_probability_no: null, model_disagreement: null,
    components: [], contract_identity: null};
  if (raw.status === 'unavailable') {
    if ([raw.probability_yes, raw.probability_no, raw.median_settlement_average, raw.settlement_centered_median].some(value => value !== null)) fail('Unavailable MC forecast retains values.');
    return result;
  }
  if (tickerAsset(result.ticker) !== raw.asset || !finite(raw.as_of) || !finite(raw.model_time)
      || raw.model_time > raw.as_of || raw.as_of - raw.model_time > 5 || raw.model_time <= 0
      || !finite(raw.median_settlement_average) || !finite(raw.settlement_centered_median)
      || Math.abs(raw.median_settlement_average) > 1e10 || Math.abs(raw.settlement_centered_median) > 1e10
      || !Number.isSafeInteger(raw.paths) || raw.paths < 1 || raw.paths > 20000
      || !['greater', 'greater_or_equal'].includes(raw.strike_type)) fail('Invalid minute-five MC state.');
  result.probability_yes = probability(raw.probability_yes, 'MC YES probability');
  result.probability_no = probability(raw.probability_no, 'MC NO probability');
  result.monte_carlo_se = probability(raw.monte_carlo_se, 'MC standard error');
  if (Math.abs(result.probability_yes + result.probability_no - 1) > 1e-8 || result.monte_carlo_se > .5) fail('Inconsistent MC probabilities.');
  result.target_price = decimal(raw.target_price, 'MC target', {max: 1e8});
  const identity = raw.contract_identity;
  if (!object(identity) || identity.ticker !== raw.ticker || identity.market_type !== 'binary'
      || identity.strike_type !== raw.strike_type || identity.cap_strike !== null
      || identity.settlement_bounds_type !== 'default' || Number(identity.notional_value_dollars) !== 1) fail('Invalid minute-five contract.');
  result.contract_identity = {ticker: identity.ticker, target_price: decimal(identity.floor_strike, 'MC target', {max: 1e8}),
    open_time: timestamp(identity.open_time), close_time: timestamp(identity.close_time)};
  const opened = Date.parse(result.contract_identity.open_time) / 1000, closed = Date.parse(result.contract_identity.close_time) / 1000;
  if (canonical(result.target_price) !== canonical(result.contract_identity.target_price) || Number(result.target_price) <= 0
      || closed - opened !== 900 || raw.model_time < opened || raw.as_of >= closed
      || Math.abs((raw.median_settlement_average - Number(result.target_price)) - raw.settlement_centered_median) > 1e-6) fail('Inconsistent MC settlement target or clock.');
  return result;
}
const sideEqual = (left, right) => Math.abs(left - right) <= 1e-9 + 2e-12 * Math.max(Math.abs(left), Math.abs(right));
const roundedSideEqual = (left, right, roundingBound) => Math.abs(left - right)
  <= roundingBound + 8 * Number.EPSILON * Math.max(1, Math.abs(left), Math.abs(right));
function sideCandidates(raw) {
  // Older saved reports have no candidate diagnostics. Do not invent them.
  if (raw === undefined) return [];
  if (!Array.isArray(raw) || ![0, 2].includes(raw.length)) fail('Invalid directional entry comparison.');
  const outcomes = new Set();
  const rows = raw.map(row => {
    if (!object(row) || !['yes', 'no'].includes(row.outcome) || outcomes.has(row.outcome)
        || typeof row.eligible !== 'boolean' || !Array.isArray(row.checks) || row.checks.length > 30) fail('Invalid directional candidate.');
    outcomes.add(row.outcome);
    const result = {outcome: row.outcome, eligible: row.eligible, checks: []};
    for (const key of ['raw_probability', 'decision_probability', 'decision_probability_yes']) {
      result[key] = decimal(row[key], `candidate ${key}`, {max: 1});
    }
    for (const key of ['price', 'effective_cost', 'full_kelly_fraction']) {
      result[key] = nullableDecimal(row[key], `candidate ${key}`, {max: 1});
    }
    result.edge = nullableDecimal(row.edge, 'candidate net edge', {signed: true, max: 1});
    result.bankroll = nullableDecimal(row.bankroll, 'candidate bankroll', {max: 1e6});
    for (const key of ['target_contracts', 'existing_contracts', 'additional_contracts']) {
      result[key] = decimal(row[key], `candidate ${key}`, {integer: true});
    }
    for (const key of ['existing_cost', 'remaining_target_cost', 'reserved_cost']) {
      result[key] = decimal(row[key], `candidate ${key}`);
    }
    for (const key of ['bid', 'entry_fee_allowance', 'applied_kelly_fraction', 'yes_equivalent_sell_price']) {
      result[key] = row[key] == null ? null : decimal(row[key], `candidate ${key}`, {max: 1});
    }
    result.pre_fee_edge = row.pre_fee_edge == null ? null : decimal(row.pre_fee_edge, 'candidate pre-fee edge', {signed: true, max: 1});
    for (const key of ['target_cost', 'target_contracts_unrounded']) {
      result[key] = row[key] == null ? null : decimal(row[key], `candidate ${key}`);
    }
    result.direction = row.direction ?? (row.outcome === 'yes' ? 'long_yes' : 'short_yes_via_no');
    if (result.direction !== (row.outcome === 'yes' ? 'long_yes' : 'short_yes_via_no')) fail('Candidate direction contradicts its outcome.');
    result.allocation_reason = row.allocation_reason ?? null;
    if (![null, 'nonpositive_edge', 'below_one_contract', 'target_met', 'positive_target'].includes(result.allocation_reason)) fail('Invalid candidate allocation reason.');
    const checkIDs = new Set();
    result.checks = row.checks.map(check => {
      if (!object(check) || typeof check.passed !== 'boolean') fail('Invalid directional candidate check.');
      const id = text(check.id, 'candidate check ID', 100);
      if (!id.length || checkIDs.has(id)) fail('Duplicate directional candidate check.');
      checkIDs.add(id);
      return {id, label: text(check.label, 'candidate check label', 200), passed: check.passed,
        reason: text(check.reason, 'candidate check reason')};
    });
    const q = Number(result.decision_probability), p = result.price === null ? null : Number(result.price);
    const cost = result.effective_cost === null ? null : Number(result.effective_cost);
    if (!sideEqual(Number(result.decision_probability_yes), row.outcome === 'yes' ? q : 1 - q)
        || q > Number(result.raw_probability) + 1e-9) fail('Inconsistent directional probability.');
    if (p !== null && (!(p > 0 && p < 1) || cost === null || !(cost > 0 && cost < 1)
        || result.edge === null || !sideEqual(Number(result.edge), q - cost)
        || result.full_kelly_fraction === null || !sideEqual(Number(result.full_kelly_fraction), Math.max(0, (q - cost) / (1 - cost))))) fail('Inconsistent directional Kelly cost.');
    if (result.entry_fee_allowance !== null && p !== null && !sideEqual(cost, p + Number(result.entry_fee_allowance))) fail('Inconsistent candidate fee allowance.');
    if (result.pre_fee_edge !== null && p !== null && !sideEqual(Number(result.pre_fee_edge), q - p)) fail('Inconsistent candidate pre-fee edge.');
    if (result.applied_kelly_fraction !== null && result.full_kelly_fraction !== null
        && Number(result.applied_kelly_fraction) > Number(result.full_kelly_fraction) + 1e-9) fail('Inconsistent applied candidate fraction.');
    if (result.target_cost !== null && result.applied_kelly_fraction !== null && result.bankroll !== null
        && !roundedSideEqual(Number(result.target_cost), Number(result.bankroll) * Number(result.applied_kelly_fraction),
          (Number(result.bankroll) + 1) * 1e-12)) fail('Inconsistent candidate target allocation.');
    if (result.target_contracts_unrounded !== null && result.target_cost !== null && cost !== null
        && !roundedSideEqual(Number(result.target_contracts_unrounded), Number(result.target_cost) / cost,
          (1 + 1 / cost) * 1e-12)) fail('Inconsistent continuous candidate quantity.');
    if (result.target_contracts_unrounded !== null) {
      const continuous = Number(result.target_contracts_unrounded), integer = Number(result.target_contracts);
      if (integer > continuous + 1e-8 || integer < continuous - 1 - 1e-8) fail('Inconsistent integer candidate target.');
    }
    if (result.allocation_reason === 'nonpositive_edge' && (result.edge === null || Number(result.edge) > 0
        || Number(result.full_kelly_fraction) !== 0 || Number(result.target_contracts) !== 0)
        || result.allocation_reason === 'below_one_contract' && (result.edge === null || Number(result.edge) <= 0 || Number(result.target_contracts) !== 0)
        || result.allocation_reason === 'target_met' && Number(result.existing_contracts) < Number(result.target_contracts)
        || result.allocation_reason === 'positive_target' && Number(result.target_contracts) < 1) fail('Candidate allocation reason contradicts its sizing.');
    if (row.eligible && (result.checks.some(check => !check.passed) || p === null || Number(result.additional_contracts) < 1
        || Number(result.existing_contracts) + Number(result.additional_contracts) > Number(result.target_contracts))) fail('Inconsistent eligible directional candidate.');
    return result;
  });
  if (rows.length) {
    const yes = rows.find(row => row.outcome === 'yes'), no = rows.find(row => row.outcome === 'no');
    // Each raw complement is independently floored to six places by the server.
    const rawSum = Number(yes.raw_probability) + Number(no.raw_probability);
    if (rawSum < 1 - 2e-6 - 1e-12 || rawSum > 1 + 1e-12
        || Number(yes.decision_probability) + Number(no.decision_probability) > 1 + 1e-9) fail('Inconsistent complementary candidate probabilities.');
    for (const [row, opposite] of [[yes, no], [no, yes]]) {
      if (row.bid !== null && opposite.price !== null && !sideEqual(Number(row.bid), 1 - Number(opposite.price))) fail('Inconsistent executable opposite-side price.');
    }
    if (yes.yes_equivalent_sell_price !== null || no.yes_equivalent_sell_price !== null
        && no.price !== null && !sideEqual(Number(no.yes_equivalent_sell_price), 1 - Number(no.price))) fail('Inconsistent short-YES execution price.');
  }
  return rows.sort((left, right) => left.outcome === 'yes' ? -1 : 1);
}
function eligibility(raw) {
  if (raw?.kind === 'kalshi_fixed_budget_eligibility') return fixedBudgetEligibility(raw);
  if (!object(raw) || raw.kind !== 'kalshi_kelly_eligibility' || raw.version !== 1
      || !['eligible', 'blocked'].includes(raw.status) || !['paper', 'demo', 'live'].includes(raw.mode)
      || typeof raw.paper_eligible !== 'boolean' || raw.live_eligible !== false || raw.execution_eligible !== false
      || ![null, 'yes', 'no'].includes(raw.selected_outcome) || !Array.isArray(raw.checks) || raw.checks.length > 100
      || !object(raw.calibration) || raw.calibration.calibrated !== false || raw.calibration.live_eligible !== false) fail('Unsupported Kelly eligibility.');
  const result = {status: raw.status, mode: raw.mode, paper_eligible: raw.paper_eligible, live_eligible: false,
    selected_outcome: raw.selected_outcome, reason: text(raw.reason, 'eligibility reason'),
    calibration_reason: text(raw.calibration.reason, 'calibration reason'), checks: [], sides: sideCandidates(raw.sides),
    decision_probability: nullableDecimal(raw.decision_probability, 'decision probability', {max: 1}),
    price: nullableDecimal(raw.price, 'order price', {max: 1}),
    reserved_cost: decimal(raw.reserved_cost, 'reserved cost'),
    additional_contracts: decimal(raw.additional_contracts, 'additional contracts', {integer: true}),
    existing_contracts: decimal(raw.existing_contracts, 'existing contracts', {integer: true}),
    target_contracts: decimal(raw.target_contracts, 'target contracts', {integer: true})};
  const ids = new Set();
  result.checks = raw.checks.map(row => {
    if (!object(row) || typeof row.passed !== 'boolean') fail('Invalid eligibility check.');
    const id = text(row.id, 'check ID', 100);
    if (!id.length || ids.has(id)) fail('Duplicate eligibility check.');
    ids.add(id);
    return {id, label: text(row.label, 'check label', 200), passed: row.passed, reason: text(row.reason, 'check reason')};
  });
  if (result.paper_eligible !== (result.status === 'eligible')
      || result.paper_eligible && (!result.selected_outcome || !result.checks.length || result.checks.some(row => !row.passed)
        || Number(result.additional_contracts) < 1 || Number(result.reserved_cost) <= 0 || result.price === null || result.decision_probability === null
        || Number(result.existing_contracts) + Number(result.additional_contracts) > Number(result.target_contracts))) fail('Inconsistent Kelly eligibility.');
  return result;
}
function fixedBudgetEligibility(raw) {
  if (!object(raw) || raw.version !== 1 || raw.mode !== 'paper' || raw.live_eligible !== false
      || raw.execution_eligible !== false || typeof raw.paper_eligible !== 'boolean'
      || !['eligible', 'blocked'].includes(raw.status) || !Object.hasOwn(ASSETS, raw.asset) || !finite(raw.as_of) || raw.as_of <= 0
      || ![null, 'yes', 'no'].includes(raw.selected_outcome) || raw.budget !== '1'
      || raw.entry_elapsed_seconds !== 300 || raw.entry_window_seconds !== 60 || raw.hold_to_settlement !== true
      || !['waiting', 'entry', 'missed', 'held'].includes(raw.phase) || !finite(raw.elapsed_seconds)
      || raw.elapsed_seconds < 0 || raw.elapsed_seconds > 900
      || raw.model_elapsed_seconds !== null && (!finite(raw.model_elapsed_seconds) || raw.model_elapsed_seconds < 0 || raw.model_elapsed_seconds > 900)
      || !Array.isArray(raw.checks) || raw.checks.length > 100) fail('Unsupported fixed-budget eligibility.');
  const result = {kind: raw.kind, version: 1, mode: 'paper', status: raw.status, paper_eligible: raw.paper_eligible,
    live_eligible: false, execution_eligible: false, asset: raw.asset, as_of: raw.as_of, selected_outcome: raw.selected_outcome,
    reason: text(raw.reason, 'fixed-budget reason'), calibration_reason: 'Research Monte Carlo strategy; paper simulation only.',
    budget: '1', entry_elapsed_seconds: 300, entry_window_seconds: 60, hold_to_settlement: true,
    phase: raw.phase, elapsed_seconds: raw.elapsed_seconds, model_elapsed_seconds: raw.model_elapsed_seconds,
    additional_contracts: raw.additional_contracts == null ? '0' : decimal(raw.additional_contracts, 'fixed contract quantity', {integer: true}),
    reserved_cost: raw.reserved_cost == null ? '0' : decimal(raw.reserved_cost, 'fixed all-in spend', {max: 1}),
    price: raw.price == null ? null : decimal(raw.price, 'fixed buy price', {max: 1}),
    sides: [], checks: []};
  const seen = new Set();
  result.checks = raw.checks.map(check => {
    if (!object(check) || typeof check.passed !== 'boolean') fail('Invalid fixed-budget check.');
    const id = text(check.id, 'fixed check ID', 100);
    if (!id || seen.has(id)) fail('Duplicate fixed-budget check.');
    seen.add(id);
    return {id, label: text(check.label, 'fixed check label', 200), passed: check.passed,
      reason: text(check.reason, 'fixed check reason')};
  });
  if (result.paper_eligible !== (result.status === 'eligible') || result.paper_eligible && (
      raw.phase !== 'entry' || raw.elapsed_seconds < 300 || raw.elapsed_seconds >= 360
      || raw.model_elapsed_seconds === null || raw.model_elapsed_seconds < 300 || raw.model_elapsed_seconds > raw.elapsed_seconds
      || !result.selected_outcome || result.checks.some(check => !check.passed)
      || Number(result.additional_contracts) < 1 || Number(result.reserved_cost) <= 0)) fail('Inconsistent fixed-budget entry.');
  return result;
}
function paperOrder(order) {
  if (!object(order) || !['paper_filled', 'rejected', 'skipped', 'none'].includes(order.status)
      || ![null, 'yes', 'no'].includes(order.outcome)) fail('Unsupported paper result.');
  const action = order.action ?? (order.status === 'paper_filled' ? 'buy' : 'hold');
  if (!['buy', 'sell', 'settle', 'hold', 'flip'].includes(action)) fail('Unsupported paper action.');
  const result = {status: order.status, reason: text(order.reason, 'paper result reason'), outcome: order.outcome,
    count: nullableDecimal(order.count, 'paper order quantity', {max: action === 'flip' ? 2e8 : 1e8}),
    reserved_cost: nullableDecimal(order.reserved_cost, 'paper reserved cost'),
    client_order_id: order.client_order_id === null ? null : text(order.client_order_id, 'paper order ID', 200),
    action, price: order.price == null ? null : decimal(order.price, 'paper action price', {max: 1}),
    realized_pnl: order.realized_pnl == null ? null : decimal(order.realized_pnl, 'paper realized P&L', {signed: true}),
    model_probability: order.model_probability == null ? null : decimal(order.model_probability, 'action model probability', {max: 1}),
    market_probability: order.market_probability == null ? null : decimal(order.market_probability, 'action market probability', {max: 1}),
    gap: order.gap == null ? null : decimal(order.gap, 'action probability gap', {signed: true, max: 1}),
    direction: order.direction == null ? null : ['long_yes', 'short_yes_via_no'].includes(order.direction) ? order.direction : fail('Invalid probability direction.')};
  if (result.count !== null && Number(result.count) % 1 !== 0
      || order.status === 'paper_filled' && (!order.outcome || !result.client_order_id || Number(result.count) < 1
        || (action === 'buy' && Number(result.reserved_cost) <= 0))) fail('Inconsistent paper result.');
  if (action === 'flip') {
    if (!['bid', 'ask'].includes(order.execution_side)) fail('Invalid net-order execution side.');
    result.execution_side = order.execution_side;
    for (const key of ['from_signed_position', 'target_signed_position', 'signed_delta']) {
      result[key] = decimal(order[key], key, {signed: true, max: 2e8});
      if (Number(result[key]) % 1 !== 0) fail('Invalid net position quantity.');
    }
    for (const key of ['close_quantity', 'open_quantity']) result[key] = decimal(order[key], key, {integer: true, max: 1e8});
    result.net_cash_flow = decimal(order.net_cash_flow, 'net flip cash flow', {signed: true});
    const from = Number(result.from_signed_position), target = Number(result.target_signed_position), delta = Number(result.signed_delta);
    if (from * target >= 0 || from + delta !== target || Math.abs(delta) !== Number(result.count)
        || Number(result.close_quantity) !== Math.abs(from) || Number(result.open_quantity) !== Math.abs(target)
        || Number(result.close_quantity) + Number(result.open_quantity) !== Number(result.count)
        || (delta > 0 ? result.execution_side !== 'bid' : result.execution_side !== 'ask')
        || (target > 0 ? result.outcome !== 'yes' : result.outcome !== 'no')
        || result.reserved_cost !== null || result.price === null || Number(result.price) <= 0 || Number(result.price) >= 1) fail('Inconsistent signed-delta flip.');
  }
  return result;
}
function paperPortfolio(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_model_portfolio' || raw.version !== 1 || raw.mode !== 'paper'
      || !Array.isArray(raw.positions) || raw.positions.length > 1000 || !Array.isArray(raw.recent_trades)
      || raw.recent_trades.length > 1000) fail('Unsupported model paper portfolio.');
  const result = {kind: raw.kind, version: 1, mode: 'paper', positions: [], recent_trades: []};
  for (const key of ['initial_cash', 'cash', 'open_cost_basis', 'equity_cost_basis', 'fees_paid', 'gross_turnover']) result[key] = decimal(raw[key], key);
  result.realized_pnl = decimal(raw.realized_pnl, 'paper portfolio realized P&L', {signed: true});
  result.marked_equity = nullableDecimal(raw.marked_equity, 'paper marked equity');
  const contract = value => {
    tickerAsset(value);
    return value;
  };
  const asOf = value => value === null ? null : finite(value) && value >= 0 && value <= 253402300799 ? value : fail('Invalid portfolio mark time.');
  result.positions = raw.positions.map(row => {
    if (!object(row) || !['yes', 'no'].includes(row.outcome)) fail('Invalid paper portfolio position.');
    const parsed = {ticker: contract(row.ticker), outcome: row.outcome, quantity: decimal(row.quantity, 'paper holding', {integer: true, max: 1e8}),
      cost_basis: decimal(row.cost_basis, 'paper holding basis'), average_entry_price: decimal(row.average_entry_price, 'average entry', {max: 1}),
      last_bid: nullableDecimal(row.last_bid, 'paper liquidation bid', {max: 1}), marked_value: nullableDecimal(row.marked_value, 'paper holding value'),
      unrealized_pnl: nullableDecimal(row.unrealized_pnl, 'paper holding P&L', {signed: true}), mark_as_of: asOf(row.mark_as_of),
      model_probability: row.model_probability == null ? null : decimal(row.model_probability, 'position model probability', {max: 1}),
      market_probability: row.market_probability == null ? null : decimal(row.market_probability, 'position market probability', {max: 1}),
      gap: row.gap == null ? null : decimal(row.gap, 'position probability gap', {signed: true, max: 1})};
    if (Number(parsed.quantity) <= 0) fail('Invalid paper holding quantity.');
    const quantity = BigInt(parsed.quantity);
    // The backend rounds average basis per contract to its money grid.
    if (absUnits(moneyUnits(parsed.average_entry_price) * quantity - moneyUnits(parsed.cost_basis)) > quantity)
      fail('Paper average entry does not reconcile to its fee-inclusive cost basis.');
    if (parsed.marked_value === null && parsed.unrealized_pnl !== null
        || parsed.marked_value !== null && parsed.unrealized_pnl !== null
          && moneyUnits(parsed.unrealized_pnl) !== moneyUnits(parsed.marked_value) - moneyUnits(parsed.cost_basis))
      fail('Paper unrealized P&L does not reconcile to liquidation value and cost basis.');
    return parsed;
  });
  if (new Set(result.positions.map(row => row.ticker)).size !== result.positions.length)
    fail('Duplicate paper portfolio contract.');
  const basis = result.positions.reduce((sum, row) => sum + moneyUnits(row.cost_basis), 0n);
  if (moneyUnits(result.open_cost_basis) !== basis
      || moneyUnits(result.equity_cost_basis) !== moneyUnits(result.cash) + basis)
    fail('Paper portfolio cash and cost-basis equity do not reconcile.');
  if (result.marked_equity !== null && (result.positions.some(row => row.marked_value === null)
      || moneyUnits(result.marked_equity) !== moneyUnits(result.cash)
        + result.positions.reduce((sum, row) => sum + moneyUnits(row.marked_value), 0n)))
    fail('Paper marked equity does not reconcile to complete liquidation marks.');
  result.recent_trades = raw.recent_trades.map(row => ({...paperOrder(row), ticker: contract(row.ticker), as_of: asOf(row.as_of), fees: decimal(row.fees, 'paper trade fees')}));
  return result;
}
export function validateRollingIndicator(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_rolling_indicator' || raw.version !== 1
      || !['ready', 'warming', 'unavailable', 'sampling_wait'].includes(raw.status)
      || raw.calibrated !== false || raw.execution_eligible !== false) fail('Unsupported rolling indicator.');
  if (!Object.hasOwn(ASSETS, raw.asset) || tickerAsset(raw.ticker) !== raw.asset) fail('Rolling indicator belongs to a different asset.');
  const count = (value, name, minimum = 0, maximum = 10000) => Number.isInteger(value) && value >= minimum && value <= maximum
    ? value : fail(`Invalid rolling ${name}.`);
  const bounded = (value, name, low, high) => finite(value) && value >= low && value <= high ? value : fail(`Invalid rolling ${name}.`);
  const nullable = (value, name, low, high) => value === null ? null : bounded(value, name, low, high);
  const directions = (value, name) => {
    if (!object(value) || typeof value.yes !== 'boolean' || typeof value.no !== 'boolean'
        || Object.keys(value).length !== 2) fail(`Invalid rolling ${name}.`);
    return {yes: value.yes, no: value.no};
  };
  if (!object(raw.thresholds)) fail('Missing rolling thresholds.');
  const thresholds = {entry_z: bounded(raw.thresholds.entry_z, 'entry z', 0, 10),
    exit_z: bounded(raw.thresholds.exit_z, 'exit z', 0, 10), alpha: probability(raw.thresholds.alpha, 'rolling nominal tail threshold')};
  if (!(thresholds.entry_z > thresholds.exit_z && thresholds.alpha > 0 && thresholds.alpha <= 0.25)) fail('Invalid rolling threshold order.');
  const result = {kind: raw.kind, version: 1, status: raw.status, asset: raw.asset, ticker: raw.ticker,
    calibrated: false, execution_eligible: false, reason: text(raw.reason, 'rolling indicator reason'),
    as_of: nullable(raw.as_of, 'as-of time', 0, 253402300799), sample_count: count(raw.sample_count, 'sample count'),
    min_samples: count(raw.min_samples, 'warmup count', 20), window_seconds: count(raw.window_seconds, 'window', 120, 900),
    phase_seconds: count(raw.phase_seconds, 'time phase', 300, 900),
    phase_start: nullable(raw.phase_start, 'phase start', 0, 253402300799),
    seconds_to_close: nullable(raw.seconds_to_close, 'remaining time', 0, 900),
    residual: nullable(raw.residual, 'model-market residual', -1, 1), mean: nullable(raw.mean, 'reference mean', -1, 1),
    sd: nullable(raw.sd, 'reference scale', 0, 2), z: nullable(raw.z, 'standardized residual', -1e100, 1e100),
    stochastic_k: nullable(raw.stochastic_k, 'stochastic position', 0, 100),
    tail_yes: nullableProbability(raw.tail_yes, 'rolling YES nominal tail rank'),
    tail_no: nullableProbability(raw.tail_no, 'rolling NO nominal tail rank'),
    minimum_tail_probability: nullableProbability(raw.minimum_tail_probability, 'minimum attainable tail rank'),
    thresholds, entry_allowed: directions(raw.entry_allowed, 'entry permissions'), exit_allowed: directions(raw.exit_allowed, 'exit permissions')};
  validateRollingSettings({window_seconds: result.window_seconds, min_samples: result.min_samples,
    phase_seconds: result.phase_seconds, ...thresholds});
  const rsiFields = ['rsi', 'rsi_period', 'rsi_filter', 'rsi_entry', 'rsi_sample_count', 'rsi_input', 'baseline_entry_allowed'];
  if (rsiFields.some(key => Object.hasOwn(raw, key))) {
    if (!rsiFields.every(key => Object.hasOwn(raw, key)) || typeof raw.rsi_filter !== 'boolean'
        || raw.rsi_input !== 'model_market_residual') fail('Incomplete rolling RSI diagnostics.');
    validateRollingSettings({window_seconds: result.window_seconds, min_samples: result.min_samples,
      phase_seconds: result.phase_seconds, ...thresholds,
      rsi_filter: raw.rsi_filter, rsi_period: raw.rsi_period, rsi_entry: raw.rsi_entry});
    Object.assign(result, {rsi: nullable(raw.rsi, 'Wilder RSI', 0, 100), rsi_period: raw.rsi_period,
      rsi_filter: raw.rsi_filter, rsi_entry: raw.rsi_entry, rsi_input: raw.rsi_input,
      rsi_sample_count: count(raw.rsi_sample_count, 'RSI seed change count', 0, raw.rsi_period),
      baseline_entry_allowed: directions(raw.baseline_entry_allowed, 'baseline entry permissions')});
    if (result.rsi !== null && result.rsi_sample_count !== result.rsi_period) fail('RSI is available before its seed history.');
  }
  if (result.status === 'ready') {
    if (result.sample_count < result.min_samples || result.as_of === null || result.phase_start === null
        || result.seconds_to_close === null || result.residual === null || result.mean === null || !(result.sd > 0)
        || result.z === null || result.stochastic_k === null || result.tail_yes === null || result.tail_no === null
        || result.minimum_tail_probability === null) fail('Incomplete ready rolling indicator.');
    if (result.entry_allowed.yes && !(result.z >= thresholds.entry_z && result.tail_yes <= thresholds.alpha
          && result.stochastic_k >= 80 && result.residual > 0)
        || result.entry_allowed.no && !(result.z <= -thresholds.entry_z && result.tail_no <= thresholds.alpha
          && result.stochastic_k <= 20 && result.residual < 0)
        || result.exit_allowed.yes && result.z > thresholds.exit_z
        || result.exit_allowed.no && result.z < -thresholds.exit_z) fail('Inconsistent rolling direction trigger.');
    if (result.baseline_entry_allowed) {
      if (result.baseline_entry_allowed.yes && !(result.z >= thresholds.entry_z && result.tail_yes <= thresholds.alpha
            && result.stochastic_k >= 80 && result.residual > 0)
          || result.baseline_entry_allowed.no && !(result.z <= -thresholds.entry_z && result.tail_no <= thresholds.alpha
            && result.stochastic_k <= 20 && result.residual < 0)
          || result.entry_allowed.yes && !result.baseline_entry_allowed.yes
          || result.entry_allowed.no && !result.baseline_entry_allowed.no) fail('Inconsistent rolling baseline comparison.');
      if (result.rsi_filter && (result.entry_allowed.yes && !(result.rsi !== null && result.rsi >= result.rsi_entry)
          || result.entry_allowed.no && !(result.rsi !== null && result.rsi <= 100 - result.rsi_entry))) fail('RSI does not confirm the rolling entry.');
      if (!result.rsi_filter && (result.entry_allowed.yes !== result.baseline_entry_allowed.yes
          || result.entry_allowed.no !== result.baseline_entry_allowed.no)) fail('Disabled RSI changes the baseline entries.');
    }
  } else if (Object.values(result.entry_allowed).some(Boolean) || Object.values(result.exit_allowed).some(Boolean)) fail('Unavailable rolling indicator retains active triggers.');
  if (result.status !== 'ready' && result.baseline_entry_allowed && Object.values(result.baseline_entry_allowed).some(Boolean)) fail('Unavailable RSI comparison retains a baseline trigger.');
  return result;
}

function monitorProvenance(raw, result) {
  if (raw.monitor_source != null) {
    if (raw.monitor_source !== 'external_report' || raw.producer_status != null
        && !['idle', 'starting', 'running', 'stopping', 'stopped', 'failed'].includes(raw.producer_status)
        || raw.monitor_session != null && !/^[a-f0-9]{64}$/.test(raw.monitor_session)) fail('Invalid external paper report provenance.');
    result.monitor_source = raw.monitor_source;
    result.producer_status = raw.producer_status ?? null;
    result.monitor_session = raw.monitor_session ?? null;
  }
  return result;
}
export function validateAlgorithmState(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_algorithm_state' || raw.version !== 1 || !['ready', 'unavailable'].includes(raw.status)) fail('Unsupported algorithm state.');
  const result = {kind: raw.kind, version: 1, status: raw.status, reason: text(raw.reason, 'algorithm reason'),
    fetched_at: raw.fetched_at === null ? null : timestamp(raw.fetched_at), forecast: null, eligibility: null, paper_order: null, portfolio: null, market_snapshot: null, indicator: null};
  if (raw.strategy != null) {
    if (!['ensemble', 'rolling', 'minute_five'].includes(raw.strategy)) fail('Unsupported paper strategy.');
    result.strategy = raw.strategy;
  }
  if (raw.status === 'unavailable') {
    if (raw.fetched_at !== null || raw.forecast !== null || raw.eligibility !== null || raw.paper_order !== null || raw.market_snapshot != null || raw.indicator != null) fail('Unavailable algorithm retains current results.');
    if (raw.portfolio != null) result.portfolio = paperPortfolio(raw.portfolio);
    return monitorProvenance(raw, result);
  }
  if (result.fetched_at === null) fail('Algorithm snapshot has no time.');
  result.forecast = raw.forecast === null ? null : forecast(raw.forecast);
  result.eligibility = raw.eligibility === null ? null : eligibility(raw.eligibility);
  const fixed = result.forecast?.kind === 'kalshi_minute_five_forecast' || result.eligibility?.kind === 'kalshi_fixed_budget_eligibility'
    || result.strategy === 'minute_five';
  if (fixed) {
    if (result.strategy !== 'minute_five' || result.forecast?.kind !== 'kalshi_minute_five_forecast'
        || result.eligibility?.kind !== 'kalshi_fixed_budget_eligibility' || result.forecast.asset !== result.eligibility.asset
        || raw.indicator != null) fail('Minute-five report mixes incompatible models or sizing rules.');
    const model = result.forecast, gate = result.eligibility;
    if (model.status === 'ready') {
      const selected = (model.strike_type === 'greater_or_equal' ? model.settlement_centered_median >= 0 : model.settlement_centered_median > 0) ? 'yes' : 'no';
      const opened = Date.parse(model.contract_identity.open_time) / 1000;
      if (gate.selected_outcome !== null && gate.selected_outcome !== selected
          || Math.abs(gate.elapsed_seconds - (gate.as_of - opened)) > 1e-6
          || gate.model_elapsed_seconds !== null && Math.abs(gate.model_elapsed_seconds - (model.model_time - opened)) > 1e-6) fail('Fixed decision does not match the settlement median or clock.');
    }
  }
  if (result.eligibility?.paper_eligible && result.forecast?.status !== 'ready') fail('Eligible order has no ready forecast.');
  if (raw.paper_order !== null) {
    result.paper_order = paperOrder(raw.paper_order);
    if (fixed && result.paper_order.status === 'paper_filled' && (result.paper_order.action === 'buy'
        ? Number(result.paper_order.reserved_cost) > 1 || result.paper_order.outcome !== result.eligibility.selected_outcome
          || result.paper_order.count !== result.eligibility.additional_contracts
          || canonical(result.paper_order.reserved_cost) !== canonical(result.eligibility.reserved_cost)
        : result.paper_order.action !== 'settle')) fail('Minute-five strategy has an unsupported fill or spend.');
  }
  if (raw.portfolio != null) result.portfolio = paperPortfolio(raw.portfolio);
  if (raw.indicator != null) {
    result.indicator = validateRollingIndicator(raw.indicator);
    if (!result.forecast || result.indicator.asset !== result.forecast.asset || result.indicator.ticker !== result.forecast.ticker
        || result.indicator.as_of !== null && Math.abs(result.indicator.as_of - result.forecast.as_of) > 1e-6) fail('Rolling indicator does not match the current forecast.');
  }
  if (raw.market_snapshot != null) {
    const snapshot = raw.market_snapshot, market = snapshot.market, identity = result.forecast?.contract_identity;
    if (!object(snapshot) || snapshot.market_status !== 'ready' || !object(market) || !identity
        || market.ticker !== identity.ticker) fail('Invalid bound model quote.');
    const parsed = {ticker: market.ticker, target_price: decimal(market.target_price, 'model quote target', {max: 1e8}),
      open_time: timestamp(market.open_time), close_time: timestamp(market.close_time),
      yes_ask: decimal(market.yes_ask, 'model YES ask', {max: 1}), no_ask: decimal(market.no_ask, 'model NO ask', {max: 1})};
    if (canonical(parsed.target_price) !== canonical(identity.target_price) || Date.parse(parsed.open_time) !== Date.parse(identity.open_time)
        || Date.parse(parsed.close_time) !== Date.parse(identity.close_time)) fail('Model quote refers to a different contract.');
    result.market_snapshot = {market_status: 'ready', fetched_at: timestamp(snapshot.fetched_at), market: parsed};
  }
  return monitorProvenance(raw, result);
}

export function validateMultiAlgorithmState(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_multi_algorithm_state' || raw.version !== 1
      || !['ready', 'unavailable'].includes(raw.status) || raw.calibrated !== false || raw.live_execution_enabled !== false) fail('Unsupported multi-asset paper report.');
  if (!Array.isArray(raw.assets)) fail('Missing selected cryptocurrencies.');
  const assets = assetList(raw.assets);
  if (!object(raw.markets) || Object.keys(raw.markets).length !== assets.length
      || Object.keys(raw.markets).some(asset => !assets.includes(asset))) fail('Incomplete multi-asset model reports.');
  const markets = Object.fromEntries(assets.map(asset => {
    const value = validateAlgorithmState(raw.markets[asset]);
    if (value.forecast?.status === 'ready' && value.forecast.asset !== asset) fail('Cross-asset forecast mismatch.');
    if (raw.status === 'unavailable' && value.status === 'ready') fail('Unavailable portfolio retains a current model.');
    return [asset, value];
  }));
  const fetched = raw.fetched_at === null ? null : timestamp(raw.fetched_at);
  if ((raw.status === 'ready') !== (fetched !== null)) fail('Invalid multi-asset snapshot time.');
  const portfolio = raw.portfolio == null ? null : paperPortfolio(raw.portfolio);
  if (raw.status === 'ready' && portfolio === null) fail('Multi-asset report requires one shared paper portfolio.');
  if (portfolio && [...portfolio.positions, ...portfolio.recent_trades].some(row => !assets.includes(tickerAsset(row.ticker)))) fail('Shared portfolio contains an unselected asset.');
  return monitorProvenance(raw, {kind: raw.kind, version: 1, status: raw.status, reason: text(raw.reason, 'multi-asset reason'),
    fetched_at: fetched, assets, markets, portfolio, calibrated: false, live_execution_enabled: false});
}
export const validateAlgorithmReport = raw => raw?.kind === 'kalshi_multi_algorithm_state' ? validateMultiAlgorithmState(raw) : validateAlgorithmState(raw);

const MEMBER_FIELDS = Object.freeze({long_run: 'algorithm-weight-long-run', recent: 'algorithm-weight-recent', ewma: 'algorithm-weight-ewma'});
const RUNNER_ACTIVE = new Set(['starting', 'running', 'stopping']);
export function validateManagedPaperState(raw) {
  if (!object(raw) || raw.kind !== 'kalshi_managed_paper' || raw.version !== 1
      || !['idle', 'starting', 'running', 'stopping', 'stopped', 'failed'].includes(raw.status)
      || raw.calibrated !== false || raw.live_execution_enabled !== false) fail('Unsupported managed paper runner.');
  const result = {kind: raw.kind, version: 1, status: raw.status, reason: text(raw.reason, 'paper runner status'),
    calibrated: false, live_execution_enabled: false, config: null,
    started_at: raw.started_at === null ? null : timestamp(raw.started_at),
    updated_at: raw.updated_at === null ? null : timestamp(raw.updated_at),
    snapshot: raw.snapshot === null ? null : validateAlgorithmReport(raw.snapshot)};
  if (raw.config !== null) {
    const value = raw.config;
    if (!object(value) || !object(value.weights) || Object.keys(value.weights).length !== 3
        || !['auto', 'yes', 'no'].includes(value.side) || !Number.isSafeInteger(value.paths) || value.paths < 100 || value.paths > 20000) fail('Invalid managed paper configuration.');
    const weights = Object.fromEntries(Object.keys(MEMBER_FIELDS).map(id => [id, probability(value.weights[id], 'paper member weight')]));
    if (Math.abs(Object.values(weights).reduce((sum, weight) => sum + weight, 0) - 1) > 1e-8) fail('Invalid managed paper weights.');
    const bankroll = decimal(value.bankroll, 'paper bankroll', {max: 1e6});
    const fraction = decimal(value.kelly_fraction, 'paper Kelly multiplier', {max: 1});
    if (Number(bankroll) <= 0 || Number(fraction) <= 0) fail('Invalid managed paper sizing.');
    const takeProfit = value.take_profit == null ? null : decimal(value.take_profit, 'paper take-profit fraction', {max: 10});
    if (takeProfit !== null && Number(takeProfit) <= 0) fail('Invalid paper take-profit fraction.');
    const entry = value.entry_edge == null ? null : decimal(value.entry_edge, 'paper entry edge', {max: 1});
    const exit = value.exit_edge == null ? null : decimal(value.exit_edge, 'paper exit edge', {max: 1});
    if ((entry === null) !== (exit === null) || entry !== null && (Number(entry) <= 0 || Number(exit) >= Number(entry))) fail('Invalid probability convergence thresholds.');
    const fees = {};
    for (const key of ['entry_fee', 'exit_fee']) {
      fees[key] = value[key] == null ? null : decimal(value[key], `paper ${key.replace('_', ' ')}`, {max: 1});
      if (fees[key] !== null && (Number(fees[key]) <= 0 || Number(fees[key]) >= 1 || (fees[key].split('.')[1]?.length || 0) > 4)) fail('Invalid paper fee allowance.');
    }
    const strategy = value.strategy ?? 'ensemble';
    if (!['ensemble', 'rolling', 'minute_five'].includes(strategy)) fail('Choose a supported paper strategy.');
    const rolling = strategy === 'rolling' ? object(value.rolling) ? validateRollingSettings(value.rolling)
      : fail('Missing rolling strategy configuration.') : null;
    const minuteFive = strategy === 'minute_five' ? minuteFiveSettings(value.minute_five) : null;
    if (minuteFive && (value.side !== 'auto' || takeProfit !== null || value.rolling != null)) fail('Minute-five configuration cannot force a side or early exit.');
    result.config = {weights, side: value.side, assets: assetList(value.assets), bankroll, kelly_fraction: fraction, paths: value.paths,
      strategy, rolling, take_profit: takeProfit, entry_edge: entry, exit_edge: exit, ...fees,
      ...(minuteFive ? {minute_five: minuteFive} : {})};
  }
  if (RUNNER_ACTIVE.has(result.status) && result.config === null) fail('Active paper runner has no configuration.');
  if (result.config && result.snapshot?.kind === 'kalshi_multi_algorithm_state'
      && result.config.assets.join(',') !== result.snapshot.assets.join(',')) fail('Paper runner asset selection differs from its snapshot.');
  if (result.config && result.snapshot?.forecast?.status === 'ready'
      && !result.config.assets.includes(result.snapshot.forecast.asset)) fail('Paper runner contains an unselected asset forecast.');
  return result;
}

export function managedPaperSettings(values, limits = null) {
  const percentage = (raw, name, maximum, zeroAllowed, digits) => {
    const value = decimal(raw.trim(), name, {max: maximum});
    const [whole, fractionPart = ''] = value.split('.');
    if ((!zeroAllowed && Number(value) <= 0) || fractionPart.length > digits) fail(`Invalid ${name}.`);
    const scale = 10n ** BigInt(digits), denominator = scale * 100n;
    const units = BigInt(whole) * scale + BigInt(fractionPart.padEnd(digits, '0'));
    return `${units / denominator}.${(units % denominator).toString().padStart(digits + 2, '0')}`.replace(/0+$/, '').replace(/\.$/, '');
  };
  const assets = assetList(values.assets);
  const strategy = values.strategy ?? 'ensemble';
  if (!['ensemble', 'rolling', 'minute_five'].includes(strategy)) fail('Choose a supported paper strategy.');
  if (strategy === 'minute_five') {
    const bankroll = decimal(values.bankroll.trim(), 'paper bankroll', {max: 1e6});
    if (Number(bankroll) <= 0 || (bankroll.split('.')[1]?.length || 0) > 4) fail('Enter a positive paper bankroll within supported precision.');
    if (limits && Number(bankroll) > Number(limits.bankroll)) fail('Paper bankroll exceeds the console startup ceiling.');
    const paths = Number(decimal(values.paths == null ? '1000' : values.paths.trim(), 'simulation paths', {max: 20000, integer: true}));
    if (paths < 100) fail('Choose between 100 and 20,000 simulation paths.');
    return {strategy, side: 'auto', assets, bankroll, paths, take_profit: null, minute_five: minuteFiveSettings(),
      entry_fee: percentage(values.entry_fee ?? '3', 'entry fee allowance in cents', 99.99, false, 2)};
  }
  const weights = Object.fromEntries(Object.keys(MEMBER_FIELDS).map(id => [id,
    Number(decimal(values.weights[id].trim(), 'member weight', {max: 100, places: 324}))]));
  if (Object.values(weights).reduce((sum, value) => sum + value, 0) <= 0) fail('At least one model weight must be positive.');
  if (!['auto', 'yes', 'no'].includes(values.side)) fail('Choose automatic, YES or NO paper sizing.');
  const bankroll = decimal(values.bankroll.trim(), 'paper bankroll', {max: 1e6});
  const fraction = decimal(values.kelly_fraction.trim(), 'paper Kelly multiplier', {max: 1});
  if (Number(bankroll) <= 0 || Number(fraction) <= 0 || (bankroll.split('.')[1]?.length || 0) > 4
      || (fraction.split('.')[1]?.length || 0) > 6) fail('Enter positive paper sizing within supported precision.');
  if (limits && (Number(bankroll) > Number(limits.bankroll) || Number(fraction) > Number(limits.kelly_fraction))) fail('Paper sizing exceeds the console startup ceilings.');
  const paths = Number(decimal(values.paths == null ? '1000' : values.paths.trim(), 'simulation paths per member', {max: 20000, integer: true}));
  if (paths < 100) fail('Choose between 100 and 20,000 simulation paths per member.');
  const takeProfit = values.take_profit == null || values.take_profit.trim() === '' ? null : percentage(values.take_profit, 'take-profit percentage', 1000, false, 6);
  const entry = percentage(values.entry_edge ?? '1', 'entry edge percentage', 100, false, 4);
  const exit = percentage(values.exit_edge ?? '0.5', 'exit edge percentage', 100, true, 4);
  const entryFee = percentage(values.entry_fee ?? '3', 'entry fee allowance in cents', 99.99, false, 2);
  const exitFee = percentage(values.exit_fee ?? '3', 'exit fee allowance in cents', 99.99, false, 2);
  if (Number(exit) >= Number(entry)) fail('Exit edge must be below the positive paper entry edge.');
  const rolling = strategy === 'rolling' ? validateRollingSettings(values.rolling) : null;
  return {weights, side: values.side, assets, paths, strategy, rolling, bankroll, kelly_fraction: fraction, take_profit: takeProfit,
    entry_edge: entry, exit_edge: exit, entry_fee: entryFee, exit_fee: exitFee};
}

export function mountManagedPaperControls(doc = document, win = window,
  {fetchImpl = win.fetch.bind(win), pollMs = 5000, onSnapshot = () => {}, onConfiguration = () => {}} = {}) {
  const $ = id => doc.getElementById(id);
  if (!$('algorithm-runner-form') || !$('algorithm-start') || !$('algorithm-stop')) return null;
  const set = (id, value) => { if ($(id)) $(id).textContent = value; };
  const rollingIDs = [...Object.values(ROLLING_FIELDS), ...Object.values(ROLLING_RSI_FIELDS)];
  const inputIDs = [...Object.values(MEMBER_FIELDS), ...Object.values(ASSET_FIELDS), ...rollingIDs,
    'algorithm-strategy', 'algorithm-side', 'algorithm-paths', 'algorithm-bankroll', 'algorithm-fraction', 'algorithm-take-profit', 'algorithm-entry-edge', 'algorithm-exit-edge', 'algorithm-entry-fee', 'algorithm-exit-fee'].filter(id => $(id));
  let stopped = false, runner = null, fetching = false, reading = null, generation = 0,
    actionBusy = false, actionController = null, uncertain = false, limits = null, policyInitialized = false, policyEdited = false,
    feesInitialized = false, feesEdited = false;
  function settings() {
    const fields = Object.entries(ASSET_FIELDS).filter(([, field]) => typeof $(field)?.checked === 'boolean');
    const strategy = $('algorithm-strategy')?.value || 'ensemble';
    let rolling = null;
    if (strategy === 'rolling') {
      rolling = Object.fromEntries(Object.entries(ROLLING_FIELDS).map(([key, field]) => {
        const raw = $(field)?.value ?? String(key === 'alpha' ? ROLLING_DEFAULTS[key] * 100 : ROLLING_DEFAULTS[key]);
        return [key, rollingInputNumber(raw, key)];
      }));
      const rsiMode = $(ROLLING_RSI_FIELDS.rsi_filter)?.value || 'baseline';
      if (!['enabled', 'baseline'].includes(rsiMode)) fail('Choose RSI confirmation or the baseline comparison.');
      rolling.rsi_filter = rsiMode === 'enabled';
      for (const key of ['rsi_period', 'rsi_entry']) {
        const raw = $(ROLLING_RSI_FIELDS[key])?.value || String(ROLLING_RSI_DEFAULTS[key]);
        rolling[key] = rollingInputNumber(raw, key);
      }
    }
    return managedPaperSettings({weights: Object.fromEntries(Object.entries(MEMBER_FIELDS).map(([id, field]) => [id, $(field).value])),
      assets: fields.length ? fields.filter(([, field]) => $(field).checked).map(([asset]) => asset) : ['BTC'],
      side: $('algorithm-side').value || 'auto', paths: $('algorithm-paths')?.value || '1000', bankroll: $('algorithm-bankroll').value,
      kelly_fraction: $('algorithm-fraction').value, take_profit: $('algorithm-take-profit').value,
      entry_edge: $('algorithm-entry-edge').value, exit_edge: $('algorithm-exit-edge').value,
      entry_fee: $('algorithm-entry-fee') ? $('algorithm-entry-fee').value : '3',
      exit_fee: $('algorithm-exit-fee') ? $('algorithm-exit-fee').value : '3', strategy, rolling}, limits);
  }
  function controls() {
    const active = RUNNER_ACTIVE.has(runner?.status);
    const rollingSelected = ($('algorithm-strategy')?.value || 'ensemble') === 'rolling';
    const fixedSelected = $('algorithm-strategy')?.value === 'minute_five';
    if ($('algorithm-rolling-controls')) $('algorithm-rolling-controls').hidden = !rollingSelected;
    if ($('algorithm-minute-five-note')) $('algorithm-minute-five-note').hidden = !fixedSelected;
    if ($('algorithm-convergence-note')) $('algorithm-convergence-note').hidden = fixedSelected;
    set('algorithm-paths-caption', fixedSelected ? 'Monte Carlo simulation paths' : 'Simulation paths per member');
    set('algorithm-minute-five-note', MINUTE_FIVE_NOTE);
    if (fixedSelected && $('algorithm-side')) $('algorithm-side').value = 'auto';
    for (const id of FIXED_UNUSED_FIELDS) {
      const node = $(id); if (node) (node.closest?.('label') || node).hidden = fixedSelected;
    }
    let valid = false, validation = 'Settings apply to the next paper start.';
    try { settings(); valid = true; } catch (error) { validation = error.message || 'Enter valid paper strategy settings.'; }
    set('algorithm-settings-status', active ? 'Current paper settings are fixed. Stop the strategy before changing them.' : validation);
    $('algorithm-start').disabled = stopped || actionBusy || uncertain || !runner || active || !valid;
    $('algorithm-stop').disabled = stopped || actionBusy || (!uncertain && (!runner || !['starting', 'running'].includes(runner.status)));
    for (const id of inputIDs) $(id).disabled = stopped || actionBusy || active || rollingIDs.includes(id) && !rollingSelected
      || fixedSelected && FIXED_UNUSED_FIELDS.includes(id);
  }
  function render(value) {
    runner = value; uncertain = false;
    if (value.config) onConfiguration(value.config, value.status);
    if (RUNNER_ACTIVE.has(value.status) && value.config) {
      // A page reload must show the worker's actual immutable policy. Keep
      // these values after Stop so an unchanged Start resumes that policy.
      for (const [key, id] of Object.entries(MEMBER_FIELDS)) {
        if ($(id)) $(id).value = rollingDisplayValue(key, value.config.weights[key]);
      }
      for (const [key, id] of Object.entries({side: 'algorithm-side', bankroll: 'algorithm-bankroll', kelly_fraction: 'algorithm-fraction'})) {
        if ($(id)) $(id).value = value.config[key];
      }
      if ($('algorithm-take-profit')) $('algorithm-take-profit').value = value.config.take_profit === null ? ''
        : rollingDisplayValue('alpha', Number(value.config.take_profit));
      for (const key of ['entry_edge', 'exit_edge']) {
        const field = $(`algorithm-${key.replace('_', '-')}`);
        if (field && value.config[key] !== null) field.value = rollingDisplayValue('alpha', Number(value.config[key]));
      }
      policyInitialized = true;
      if (value.config.entry_fee !== null && value.config.exit_fee !== null) feesInitialized = true;
      if ($('algorithm-strategy')) $('algorithm-strategy').value = value.config.strategy;
      if (value.config.rolling) for (const [key, id] of Object.entries(ROLLING_FIELDS)) {
        if ($(id)) $(id).value = rollingDisplayValue(key, value.config.rolling[key]);
      }
      if (value.config.rolling) for (const [key, id] of Object.entries(ROLLING_RSI_FIELDS)) {
        if ($(id)) $(id).value = key === 'rsi_filter' ? (value.config.rolling.rsi_filter ? 'enabled' : 'baseline')
          : rollingDisplayValue(key, value.config.rolling[key] ?? ROLLING_RSI_DEFAULTS[key]);
      }
    }
    if (RUNNER_ACTIVE.has(value.status) && value.config) for (const [asset, field] of Object.entries(ASSET_FIELDS)) {
      if ($(field) && typeof $(field).checked === 'boolean') $(field).checked = value.config.assets.includes(asset);
    }
    if (RUNNER_ACTIVE.has(value.status) && value.config) for (const key of ['entry_fee', 'exit_fee']) {
      const field = $(`algorithm-${key.replace('_', '-')}`);
      if (field && value.config[key] !== null) field.value = (Number(value.config[key]) * 100).toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
    }
    const pathControl = $('algorithm-paths');
    if (RUNNER_ACTIVE.has(value.status) && value.config && pathControl) {
      const pathValue = String(value.config.paths);
      if (!Array.from(pathControl.options || pathControl.children || []).some(option => option.value === pathValue)) {
        const option = doc.createElement('option'); option.value = pathValue;
        option.textContent = value.config.paths.toLocaleString('en-US'); pathControl.appendChild(option);
      }
      pathControl.value = pathValue;
    }
    set('algorithm-runner-status', `${value.status[0].toUpperCase() + value.status.slice(1)} | ${value.reason}`);
    set('algorithm-runner-note', value.config?.strategy === 'minute_five'
      ? `Paper only | Minute 5 median settlement-average pick | ${value.config.assets.join(', ')} | shared bankroll $${value.config.bankroll} | maximum $1 per filled entry including fees | ${value.config.paths} Monte Carlo paths | entry fee allowance ${value.config.entry_fee === null ? 'startup default' : (Number(value.config.entry_fee) * 100).toFixed(2) + ' cents'} | hold to official settlement.`
      : value.config
      ? `Paper only | ${value.config.strategy === 'rolling' ? 'Rolling deviation (Kelly sized)' : 'Ensemble convergence'} | ${value.config.assets.join(', ')} | ${value.config.side.toUpperCase()} | shared bankroll $${value.config.bankroll} | Kelly ${value.config.kelly_fraction} | entry ${value.config.entry_edge === null ? 'startup default' : (Number(value.config.entry_edge) * 100).toFixed(2) + '%'} / exit ${value.config.exit_edge === null ? 'startup default' : (Number(value.config.exit_edge) * 100).toFixed(2) + '%'} | fee allowances entry ${value.config.entry_fee === null ? 'startup default' : (Number(value.config.entry_fee) * 100).toFixed(2) + ' cents'} / exit ${value.config.exit_fee === null ? 'startup default' : (Number(value.config.exit_fee) * 100).toFixed(2) + ' cents'} | take profit ${value.config.take_profit === null ? 'disabled' : (Number(value.config.take_profit) * 100).toFixed(2) + '%'} | ${value.config.paths} paths per member | weights ${Object.entries(value.config.weights).map(([id, weight]) => `${id} ${(weight * 100).toFixed(1)}%`).join(', ')}`
      : 'Start records forecasts and simulates Kelly orders. Stop pauses the loop and preserves its journal.');
    if (value.config?.strategy === 'rolling' && value.config.rolling && $('algorithm-runner-note')) {
      const rsi = {...ROLLING_RSI_DEFAULTS, ...value.config.rolling};
      set('algorithm-runner-note', `${$('algorithm-runner-note').textContent} | residual Wilder RSI(${rsi.rsi_period}): ${rsi.rsi_filter
        ? `entry confirmation YES ≥ ${rsi.rsi_entry}, NO ≤ ${100 - rsi.rsi_entry}` : 'diagnostic only; baseline entry rule'}.`);
    }
    if (value.snapshot) onSnapshot(value.snapshot);
    controls();
  }
  async function jsonRequest(path, options, signal) {
    const response = await fetchImpl(path, {credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', ...options, signal});
    const body = await response.text();
    if (new TextEncoder().encode(body).byteLength > 1_000_000) fail('Paper runner response exceeds its supported size.');
    if (!response.ok) {
      const failure = new Error(response.status === 404 ? 'Restart the local Kalshi service to load paper runner controls.'
        : response.status === 403 ? 'Paper runner action rejected. Refresh the console session before trying again.' : `Paper runner request rejected (HTTP ${response.status}).`);
      failure.status = response.status; throw failure;
    }
    try { return JSON.parse(body); } catch { fail('Paper runner returned an invalid response.'); }
  }
  async function refresh() {
    if (stopped || fetching || actionBusy) return;
    fetching = true; const expected = generation, controller = new AbortController(); reading = controller;
    const timeout = win.setTimeout(() => controller.abort(), 12000);
    try {
      const value = validateManagedPaperState(await jsonRequest('/api/algorithm/runner', {method: 'GET', headers: {Accept: 'application/json'}}, controller.signal));
      if (!stopped && expected === generation) render(value);
    } catch (error) {
      if (!stopped && expected === generation) {
        if (!RUNNER_ACTIVE.has(runner?.status)) runner = null;
        set('algorithm-runner-status', `${error.message || 'Paper runner status is unavailable.'}${RUNNER_ACTIVE.has(runner?.status) ? ` Last known state: ${runner.status}; Stop remains available.` : ''}`);
        controls();
      }
    } finally { win.clearTimeout(timeout); if (expected === generation) { fetching = false; reading = null; } }
  }
  async function action(operation) {
    if (stopped || actionBusy || (operation === 'start' ? $('algorithm-start').disabled : $('algorithm-stop').disabled)) return;
    let body; try { body = operation === 'start' ? settings() : {}; } catch (error) { set('algorithm-runner-status', error.message); return; }
    actionBusy = true; generation++; reading?.abort(); fetching = false; reading = null;
    const expected = generation, controller = new AbortController(); actionController = controller;
    const timeout = win.setTimeout(() => controller.abort(), 12000);
    controls(); set('algorithm-runner-result', operation === 'start' ? 'Starting paper strategy…' : 'Stopping paper strategy…');
    let transmitted = false;
    try {
      const session = await jsonRequest('/api/session', {method: 'GET', headers: {Accept: 'application/json'}}, controller.signal);
      if (stopped || expected !== generation) return;
      if (!object(session) || typeof session.token !== 'string' || session.token.length < 20 || session.token.length > 256
          || /[\u0000-\u0020\u007f]/u.test(session.token)) fail('Local session token is unavailable.');
      transmitted = true;
      const value = validateManagedPaperState(await jsonRequest(`/api/algorithm/${operation}`, {method: 'POST',
        headers: {Accept: 'application/json', 'Content-Type': 'application/json', 'X-Kalshi-Token': session.token}, body: JSON.stringify(body)}, controller.signal));
      if (!stopped && expected === generation) {
        render(value); set('algorithm-runner-result', operation === 'start' ? 'Paper strategy start requested.' : 'Paper strategy stop requested.');
      }
    } catch (error) {
      if (!stopped && expected === generation) {
        uncertain = transmitted && ![400, 403, 404, 409, 413, 415].includes(error.status);
        set('algorithm-runner-result', uncertain
          ? 'Paper runner action outcome is unconfirmed. Status will be checked; the action is not retried automatically.'
          : error.message || 'Paper runner action is unavailable.');
      }
    } finally {
      win.clearTimeout(timeout);
      if (expected === generation) { actionBusy = false; actionController = null; controls(); if (!stopped) refresh(); }
    }
  }
  function setServiceState(value) {
    if (value?.limits) {
      limits = value.limits;
      if (!policyInitialized && !policyEdited) {
        $('algorithm-bankroll').value = value.limits.bankroll;
        $('algorithm-fraction').value = value.limits.kelly_fraction;
        const entry = Math.min(.01, Number(value.limits.min_edge ?? '.03'));
        $('algorithm-entry-edge').value = (entry * 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
        $('algorithm-exit-edge').value = (Math.min(.005, Math.floor(entry * 1e6 / 2 + 1e-8) / 1e6) * 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
      }
      policyInitialized = true; controls();
    }
    const startupFee = value?.limits?.fee_reserve_per_contract ?? value?.market?.fee_reserve;
    if (!feesInitialized && !feesEdited && startupFee) {
      const cents = (Number(startupFee) * 100).toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
      for (const id of ['algorithm-entry-fee', 'algorithm-exit-fee']) if ($(id)) $(id).value = cents;
      feesInitialized = true; controls();
    }
    if ($('algorithm-fee-status')) {
      const bound = value?.market?.fee_reserve_lower_bound;
      let asset = value?.selected_asset;
      if (!asset && value?.market?.ticker) { try { asset = tickerAsset(value.market.ticker); } catch {} }
      set('algorithm-fee-status', bound && asset
        ? `${asset} verified minimum allowance: ${(Number(bound) * 100).toFixed(2)} cents per leg. Each strategy coin is checked against its own current fee bound.`
        : 'Paper entry and exit allowances are checked against each strategy coin\'s current fee bound.');
    }
  }
  for (const id of inputIDs) $(id).addEventListener(['algorithm-strategy', 'algorithm-rolling-phase', 'algorithm-rolling-rsi-filter', 'algorithm-side', 'algorithm-paths', ...Object.values(ASSET_FIELDS)].includes(id) ? 'change' : 'input', () => {
    if (['algorithm-bankroll', 'algorithm-fraction', 'algorithm-entry-edge', 'algorithm-exit-edge'].includes(id)) policyEdited = true;
    if (['algorithm-entry-fee', 'algorithm-exit-fee'].includes(id)) feesEdited = true;
    controls();
  });
  $('algorithm-runner-form').addEventListener('submit', event => { event.preventDefault(); return action('start'); });
  $('algorithm-stop').addEventListener('click', () => action('stop'));
  const timer = win.setInterval(refresh, pollMs);
  function dispose() { stopped = true; generation++; reading?.abort(); actionController?.abort(); win.clearInterval(timer); controls(); }
  win.addEventListener('pagehide', dispose);
  controls();
  const ready = refresh();
  return {ready, refresh, start: () => action('start'), stop: () => action('stop'), setServiceState, dispose, getState: () => runner};
}

const percent = value => value === null ? 'Unavailable' : `${(value * 100).toFixed(2)}%`;
const dollars = value => value === null ? 'Unavailable' : Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
const canonical = value => value.replace(/(?:\.0+|(\.[0-9]*?)0+)$/, '$1');
const signedPosition = value => Number(value) > 0 ? `+${value}` : value;
const actionLabel = order => order.action === 'flip' ? `${order.execution_side === 'ask' ? 'SELL' : 'BUY'} ${order.count} YES-scale: ${signedPosition(order.from_signed_position)} → ${signedPosition(order.target_signed_position)} (${order.outcome.toUpperCase()} ${order.open_quantity})`
  : order.action === 'buy' ? `BUY ${order.outcome?.toUpperCase() || ''} (${order.outcome === 'no' ? 'short YES probability' : 'long YES probability'})`
  : order.action === 'sell' ? `SELL ${order.outcome?.toUpperCase() || ''} (reduce / close)`
    : order.action === 'settle' ? `SETTLE ${order.outcome?.toUpperCase() || ''}` : 'HOLD';
export function mountAlgorithm(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 1000, asset = 'BTC', onAssetChange = () => {}} = {}) {
  const $ = id => doc.getElementById(id);
  if (!$('algorithm-panel') || typeof doc.createElement !== 'function') return null;
  const retainedIDs = new Set(['algorithm-raw-p', 'algorithm-yes-p', 'algorithm-no-p', 'algorithm-disagreement',
    'algorithm-paper-cash', 'algorithm-paper-equity', 'algorithm-paper-realized', 'algorithm-paper-fees',
    ...['samples', 'z', 'k', 'rsi', 'tail-yes', 'tail-no', 'residual'].map(key => `algorithm-rolling-${key}`)]);
  const scalarCache = new Map(), positionCache = new Map(), overviewCache = new Map(), overviewRows = new Map();
  const chartLimit = 360, chartWindowSeconds = 1800, chartGapSeconds = 15;
  const chartTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  let probabilityPoints = [], rollingPoints = [], portfolioPoints = [], chartTrades = [],
    chartContext = null, chartPhase = null, chartPolicy = null, chartSides = [], chartRollingThresholds = null,
    lastChartTime = -Infinity, lastForecastTime = -Infinity, lastChartFingerprint = null, lastPortfolioTime = -Infinity,
    probabilityGap = false, rollingGap = false, portfolioGap = false, chartAwaitingSnapshot = false;
  const seenChartTrades = new Set();
  const chartWindows = new Map();
  let detailChartsCurrent = false, detailChartsReason = null, portfolioChartCurrent = false;
  let displayStamp = null, displayFresh = false, detailKey = null, detailStamp = null, portfolioStamp = null,
    lastRolling = null, lastPaperResult = null, configurationKey = null, confirmedDetailKey = null, detailModelKey = null,
    portfolioSummary = null, fixedDisplay = false, monitorContext = null, producerDescription = '';
  const timeLabel = stamp => stamp === null ? 'an earlier snapshot' : new Date(stamp).toLocaleTimeString();
  function mark(node, stale, stamp = null) {
    if (!node) return;
    if (!node.dataset) node.dataset = {};
    if (stale) node.dataset.stale = 'true'; else delete node.dataset.stale;
    node.title = stale ? `Last known at ${timeLabel(stamp)}; waiting for replacement data.` : '';
  }
  const set = (id, value) => {
    const node = $(id); if (!node) return;
    if (!retainedIDs.has(id)) { node.textContent = value; return; }
    const old = scalarCache.get(id);
    if (old && (value === 'Unavailable' || !displayFresh)) { node.textContent = old.text; mark(node, true, old.stamp); return; }
    node.textContent = value;
    mark(node, value !== 'Unavailable' && !displayFresh, displayStamp);
    if (value !== 'Unavailable' && displayStamp !== null && displayFresh) scalarCache.set(id, {text: value, stamp: displayStamp});
  };
  function strategyDisplay(value) {
    const fixed = value.strategy === 'minute_five', rolling = value.strategy === 'rolling' || Boolean(value.indicator);
    fixedDisplay = fixed;
    for (const id of ['algorithm-probability-card', 'algorithm-rolling-card', 'algorithm-edge-card',
      'algorithm-side-comparison-panel', 'algorithm-members-panel', 'algorithm-model-note']) if ($(id)) $(id).hidden = fixed;
    if ($('algorithm-minute-five-status')) $('algorithm-minute-five-status').hidden = !fixed;
    set('algorithm-title', fixed ? 'Minute-five median / fixed-budget paper'
      : rolling ? value.indicator?.rsi_filter ? 'Rolling residual + RSI / Kelly paper' : 'Rolling residual / Kelly paper'
        : 'Settlement ensemble / Kelly paper');
    set('algorithm-raw-caption', fixed ? 'MC standard error' : 'Ensemble P(YES)');
    set('algorithm-yes-caption', fixed ? 'MC P(YES)' : 'Decision P(YES)');
    set('algorithm-no-caption', fixed ? 'MC P(NO)' : 'Decision P(NO)');
    set('algorithm-disagreement-caption', fixed ? 'Median settlement average' : 'Member disagreement');
    set('algorithm-checks-caption', fixed ? 'Fixed-budget entry checks; hold to official settlement' : 'Entry checks; exits are assessed separately');
  }
  function fixedPhase(model, decision, order = null) {
    if (decision?.phase === 'held' || order?.status === 'paper_filled' && order.action === 'buy') return 'held';
    const opened = model?.contract_identity ? Date.parse(model.contract_identity.open_time) / 1000 : null;
    const elapsed = opened === null ? decision?.elapsed_seconds : Math.max(decision?.elapsed_seconds ?? 0, now() / 1000 - opened);
    if (decision?.phase === 'missed' || elapsed >= 360) return 'missed';
    return elapsed < 300 || decision?.phase === 'waiting' || (decision?.model_elapsed_seconds ?? -1) < 300 ? 'waiting' : 'entry';
  }
  function fixedLabel(model, decision, order = null) {
    if (order?.status === 'paper_filled') return order.action === 'settle' ? 'Official paper settlement' : 'Paper fill recorded';
    const phase = fixedPhase(model, decision, order);
    return phase === 'held' ? 'Held to settlement' : phase === 'missed' ? 'Missed minute-5 entry'
      : phase === 'waiting' ? 'Waiting for minute-5 sample' : decision?.paper_eligible ? 'Paper eligible' : 'No entry yet';
  }
  function fixedSummary(model, decision, order, portfolio) {
    const opened = model?.contract_identity ? Date.parse(model.contract_identity.open_time) / 1000 : null;
    const elapsed = opened === null ? decision.elapsed_seconds : Math.max(decision.elapsed_seconds, now() / 1000 - opened);
    const phase = fixedPhase(model, decision, order);
    const trade = order?.status === 'paper_filled' && order.action === 'buy' ? order
      : portfolio?.recent_trades.find(row => row.ticker === model?.ticker && row.action === 'buy' && row.status === 'paper_filled');
    const position = portfolio?.positions.find(row => row.ticker === model?.ticker);
    const count = trade?.count ?? position?.quantity ?? decision.additional_contracts;
    const spend = trade?.reserved_cost ?? position?.cost_basis ?? null;
    const reason = phase === 'missed' && decision.phase === 'entry' ? 'Prior entry checks have expired with the local window.' : decision.reason;
    return `Current median choice ${decision.selected_outcome?.toUpperCase() || 'unavailable'} | median final-minute settlement average ${dollars(model?.median_settlement_average ?? null)} ${model?.strike_type === 'greater_or_equal' ? 'vs ≥' : 'vs >'} strike ${dollars(model?.target_price ?? null)} | P(YES) ${percent(model?.probability_yes ?? null)} | maximum budget $1 including entry fees | ${trade || position ? `filled ${(trade?.outcome ?? position?.outcome)?.toUpperCase()} ` : 'proposed '}${count} whole contracts | actual filled spend ${spend === null ? 'none' : dollars(spend)} | ${phase === 'held' ? 'holding to official settlement' : phase === 'missed' ? 'entry window closed; no late entry' : `entry window ${elapsed < 300 ? `opens in ${Math.ceil(300 - elapsed)}s` : `${Math.max(0, Math.ceil(360 - elapsed))}s remaining`}`} | ${reason}`;
  }
  if (!Object.hasOwn(VIEW_ASSETS, asset)) fail('Choose BTC, ETH, SOL, XRP or GOLD.');
  let stopped = false, fetching = false, generation = 0, controller = null, report = null, marketState = null, latestReadyTime = -Infinity, selectedAsset = asset;
  function resetDetail() {
    detailKey = null; detailStamp = null; lastRolling = null; lastPaperResult = null; detailModelKey = null;
    for (const id of retainedIDs) if (!id.startsWith('algorithm-paper-')) { scalarCache.delete(id); set(id, 'Unavailable'); }
    for (const id of ['algorithm-members', 'algorithm-side-rows']) { $(id)?.replaceChildren(); mark($(id), false); }
    renderRolling(null, false, true);
    resetDetailCharts();
  }
  function resetExperiment() {
    producerDescription = '';
    resetDetail(); scalarCache.clear(); positionCache.clear(); overviewCache.clear(); overviewRows.clear(); portfolioStamp = null; portfolioSummary = null;
    $('algorithm-position-rows')?.replaceChildren(); $('algorithm-trade-rows')?.replaceChildren();
    $('algorithm-asset-rows')?.replaceChildren(); latestReadyTime = -Infinity; report = null;
    portfolioPoints = []; chartTrades = []; seenChartTrades.clear(); lastPortfolioTime = -Infinity; portfolioGap = false; chartPolicy = null;
    chartWindows.delete('algorithm-equity-chart');
    paintPortfolioChart(false);
  }
  function clear(reason, {keepReport = false, keepOverview = false, dropDetail = false} = {}) {
    if (!keepReport) report = null;
    if (dropDetail) resetDetail();
    displayStamp = null; displayFresh = false;
    if (!keepOverview && !overviewCache.size) { if ($('algorithm-overview')) $('algorithm-overview').hidden = true; $('algorithm-asset-rows')?.replaceChildren(); }
    else if (!keepOverview && overviewCache.size) staleOverview(reason);
    for (const id of ['algorithm-raw-p', 'algorithm-yes-p', 'algorithm-no-p', 'algorithm-disagreement']) set(id, 'Unavailable');
    if (detailStamp === null) { $('algorithm-members').replaceChildren(); $('algorithm-side-rows')?.replaceChildren(); }
    mark($('algorithm-members'), detailStamp !== null, detailStamp); mark($('algorithm-side-rows'), detailStamp !== null, detailStamp);
    $('algorithm-checks').replaceChildren();
    set('algorithm-side-comparison-status', detailStamp === null ? 'Waiting for fresh, matching data for both directions.'
      : `STALE: last known at ${timeLabel(detailStamp)}. Current entry comparison is withheld.`);
    set('algorithm-status', producerDescription + (detailStamp === null && portfolioStamp === null ? reason : `STALE: ${reason}`));
    set('algorithm-decision', detailStamp === null ? 'Unavailable' : 'Waiting for fresh data');
    set('algorithm-live-gate', 'Blocked: uncalibrated'); set('algorithm-proposal', 'No current order proposal.');
    if (fixedDisplay) { set('algorithm-live-gate', 'Paper only'); set('algorithm-minute-five-status', `Waiting for fresh minute-five evidence. ${reason}`); }
    set('algorithm-paper-order', lastPaperResult ? `Last known result at ${timeLabel(detailStamp)} (not current): ${lastPaperResult}` : 'No current paper order result.');
    set('algorithm-updated', detailStamp === null ? 'No fresh algorithm snapshot' : `STALE display | last known ${timeLabel(detailStamp)} | ${reason}`);
    if (detailStamp === null) set('algorithm-contract', 'Waiting for a verified contract.');
    for (const id of ['algorithm-paper-cash', 'algorithm-paper-equity', 'algorithm-paper-realized', 'algorithm-paper-fees', 'algorithm-last-action']) set(id, 'Unavailable');
    if (portfolioStamp === null) { $('algorithm-position-rows')?.replaceChildren(); $('algorithm-trade-rows')?.replaceChildren(); }
    mark($('algorithm-position-rows'), portfolioStamp !== null, portfolioStamp); mark($('algorithm-trade-rows'), portfolioStamp !== null, portfolioStamp);
    renderLedgerSummary(null, false);
    renderLedgerEntry(null, false, reason);
    renderRolling(null, false);
    unavailableCharts(reason, !keepReport);
  }

  const trimChart = (points, time) => points.filter(point => point.time >= time - chartWindowSeconds).slice(-chartLimit);
  function paintSvg(id, svg, observations = null, stale = false, stamp = null) {
    const node = $(id); if (!node) return;
    // Only our own renderer supplies SVG markup; report labels never become HTML.
    const inner = /^<svg\b[^>]*>([\s\S]*)<\/svg>\s*$/u.exec(svg);
    if (!inner) fail('Unsupported local strategy chart.');
    node.setAttribute?.('viewBox', '0 0 760 280');
    // The renderer's data domain belongs to the mounted SVG, so range controls
    // inspect the same chart whose children are being displayed.
    const opening = /^<svg\b([^>]*)>/u.exec(svg)?.[1] ?? '';
    for (const name of ['range-enabled', 'x-min', 'x-max', 'window-min', 'window-max', 'visible-count', 'total-count', 'range-start', 'range-end']) {
      const attribute = `data-chart-${name}`, value = new RegExp(`${attribute}="([^"]*)"`, 'u').exec(opening)?.[1];
      if (value !== undefined) node.setAttribute?.(attribute, value);
      else node.removeAttribute?.(attribute);
    }
    node.innerHTML = inner[1];
    if (!node.dataset) node.dataset = {};
    if (observations !== null) node.dataset.observations = String(observations);
    mark(node, stale, stamp);
  }
  function paintDetailCharts(current = false, reason = null) {
    detailChartsCurrent = current; detailChartsReason = reason;
    const options = {timeZone: chartTimeZone, maxGapSeconds: chartGapSeconds};
    const point = probabilityPoints.at(-1), rolling = rollingPoints.at(-1), rollingCurrent = current && rolling?.time === lastChartTime;
    paintSvg('algorithm-probability-chart', renderProbabilityChart(probabilityPoints, {...options, window: chartWindows.get('algorithm-probability-chart')}), probabilityPoints.length, Boolean(point) && !current, point ? point.time * 1000 : null);
    paintSvg('algorithm-rolling-chart', renderRollingChart(rollingPoints, {...options,
      entryZ: chartRollingThresholds?.entry_z ?? null, exitZ: chartRollingThresholds?.exit_z ?? null, window: chartWindows.get('algorithm-rolling-chart')}), rollingPoints.length, Boolean(rolling) && !rollingCurrent, rolling ? rolling.time * 1000 : null);
    paintSvg('algorithm-edge-chart', renderEdgeChart(chartSides, {entryMargin: chartPolicy?.entry_edge ?? null,
      exitFee: chartPolicy?.exit_fee ?? null, exitEdge: chartPolicy?.exit_edge ?? null}), null, chartSides.length > 0 && !current, point ? point.time * 1000 : null);
    const known = current ? '' : 'Last known: ';
    set('algorithm-probability-chart-status', point
      ? `${known}${probabilityPoints.length} chart observation${probabilityPoints.length === 1 ? '' : 's'} from distinct forecast timestamps.${probabilityPoints.length === 1 ? ' Collecting a trace; the first observation is shown.' : ''}${!current ? ` ${reason || 'Waiting for fresh matching data.'}` : ''}`
      : `Waiting for a fresh matched ${selectedAsset} forecast and quote. History starts with the first observed snapshot.`);
    set('algorithm-rolling-chart-status', rolling
      ? `${rollingCurrent ? '' : 'Last known: '}${rollingPoints.length} plotted z-score observation${rollingPoints.length === 1 ? '' : 's'} in this expiry phase.${!rollingCurrent ? ' Waiting for a fresh rolling score.' : ''} Chart observations are separate from the indicator's prior-sample baseline.`
      : 'Collecting usable rolling z-scores in this expiry phase. Warmup observations are not invented as z-scores.');
    set('algorithm-edge-chart-status', chartSides.length
      ? `${known}Outline: raw model edge before sensitivity deductions and fees. Blue/purple: conservative probability minus executable ask and fee allowances.${chartPolicy?.entry_edge === null || chartPolicy === null || chartPolicy.exit_fee === null || chartPolicy.exit_edge === null
        ? ' Round-trip entry threshold is unavailable until a running policy is confirmed.' : ` Entry also requires ${(100 * (chartPolicy.entry_edge + chartPolicy.exit_fee + chartPolicy.exit_edge)).toFixed(2)} pp for the entry margin, exit allowance and exit gap.`}${!current ? ' Current entry evidence is withheld.' : ''}`
      : 'Waiting for both-side executable prices, conservative probabilities and fee allowances.');
    renderEdgeExecution(current, point ? point.time * 1000 : null);
    for (const id of ['algorithm-probability-chart-status', 'algorithm-edge-chart-status']) mark($(id), Boolean(point) && !current, point ? point.time * 1000 : null);
    mark($('algorithm-rolling-chart-status'), Boolean(rolling) && !rollingCurrent, rolling ? rolling.time * 1000 : null);
  }
  function renderEdgeExecution(current, stamp) {
    const yes = chartSides.find(side => side.outcome === 'yes'), no = chartSides.find(side => side.outcome === 'no');
    const pct = value => finite(value) ? `${(value * 100).toFixed(2)}%` : 'Unavailable';
    const price = value => finite(value) ? `$${value.toFixed(4)}` : 'Unavailable';
    const edge = value => finite(value) ? `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)} pp` : 'Unavailable';
    const prefix = chartSides.length && !current ? 'Last known: ' : '';
    const bid = no?.ask === null || no?.ask === undefined ? null : 1 - no.ask;
    set('algorithm-edge-execution', chartSides.length
      ? `${prefix}YES bid ${price(bid)} | YES ask ${price(yes?.ask)} | raw model P(YES) ${pct(yes?.rawProbability)} / P(NO) ${pct(no?.rawProbability)}. Immediate short YES uses the bid; a sale posted at the ask waits for a fill.`
      : 'Waiting for verified execution prices and raw model probabilities.');
    for (const [side, id, label] of [[yes, 'algorithm-edge-long', 'BUY YES / long YES'], [no, 'algorithm-edge-short', 'BUY NO / short YES']]) {
      const raw = side && finite(side.rawProbability) && finite(side.ask) ? side.rawProbability - side.ask : null;
      const haircut = side && finite(side.rawProbability) && finite(side.probability) ? side.rawProbability - side.probability : null;
      const afterEntry = side && finite(side.probability) && finite(side.ask) && finite(side.entryFee) ? side.probability - side.ask - side.entryFee : null;
      const afterBoth = afterEntry !== null && chartPolicy?.exit_fee !== null && chartPolicy?.exit_fee !== undefined ? afterEntry - chartPolicy.exit_fee : null;
      set(id, side ? `${prefix}${label}: raw edge ${edge(raw)}; sensitivity deduction ${haircut === null ? 'Unavailable' : `${(haircut * 100).toFixed(2)} pp`}; entry allowance ${price(side.entryFee)}; after entry ${edge(afterEntry)}; after both allowances ${edge(afterBoth)}.`
        : `${label}: waiting for directional execution data.`);
      mark($(id), Boolean(side) && !current, stamp);
    }
    mark($('algorithm-edge-execution'), chartSides.length > 0 && !current, stamp);
  }
  function resetDetailCharts() {
    chartWindows.delete('algorithm-probability-chart'); chartWindows.delete('algorithm-rolling-chart');
    probabilityPoints = []; rollingPoints = []; chartSides = []; chartContext = null; chartPhase = null; chartRollingThresholds = null;
    lastChartTime = -Infinity; lastForecastTime = -Infinity; lastChartFingerprint = null; probabilityGap = false; rollingGap = false;
    set('algorithm-charts-context', `${selectedAsset} | Waiting for a verified current contract. Observed history is collected in this page; times use ${chartTimeZone}.`);
    paintDetailCharts(false);
  }
  function unavailableCharts(reason, shared = false) {
    probabilityGap = true; rollingGap = true;
    paintDetailCharts(false, reason);
    if (shared) { portfolioGap = true; paintPortfolioChart(false); }
  }
  function observeDetailCharts(value, context, fresh) {
    const model = value.forecast, indicator = value.indicator;
    if (model?.kind === 'kalshi_minute_five_forecast') return;
    if (chartAwaitingSnapshot) { unavailableCharts('Waiting for a snapshot from the confirmed paper experiment.'); return; }
    if (!fresh || model?.status !== 'ready') { unavailableCharts('Waiting for a fresh matching forecast and quote.'); return; }
    const modelContext = JSON.stringify([model.asset, model.source, identityKey(model), model.model_version ?? null,
      model.config_hash ?? model.components.map(row => [row.id, row.weight])]);
    if (chartContext !== null && modelContext !== chartContext) {
      const previousTime = lastChartTime, previousForecastTime = lastForecastTime;
      resetDetailCharts(); lastChartTime = previousTime; lastForecastTime = previousForecastTime;
    }
    chartContext = modelContext;
    const quote = context?.market;
    const number = value => value === null || value === undefined ? null : finite(Number(value)) ? Number(value) : null;
    const noAsk = number(quote?.no_ask);
    const modelTime = model.as_of, quoteTime = Date.parse(context.fetched_at) / 1000;
    // A paired point cannot predate either input. In particular, a quote
    // received after the forecast must not be backdated to the model clock.
    const time = Math.max(modelTime, quoteTime), point = {time, modelTime, quoteTime, model: model.probability_yes,
      lower: model.conservative_probability_yes, upper: 1 - model.conservative_probability_no,
      bid: noAsk === null ? null : 1 - noAsk, ask: number(quote?.yes_ask), context: modelContext};
    const fingerprint = JSON.stringify([point, indicator?.phase_start ?? null, indicator?.z ?? null]);
    if (modelTime < lastForecastTime || modelTime === lastForecastTime && fingerprint !== lastChartFingerprint) {
      unavailableCharts('A revised or older forecast snapshot is withheld until a new observation arrives.'); return;
    }
    if (modelTime > lastForecastTime && time <= lastChartTime) {
      unavailableCharts('The combined model and quote clocks have not advanced; waiting for a new observed sample.'); return;
    }
    if (modelTime > lastForecastTime) {
      probabilityPoints.push({...point, gap: probabilityGap}); probabilityPoints = trimChart(probabilityPoints, time);
      probabilityGap = false; lastChartTime = time; lastForecastTime = modelTime; lastChartFingerprint = fingerprint;
      const phase = indicator?.phase_start === null || indicator?.phase_start === undefined ? null
        : JSON.stringify([modelContext, indicator.phase_start, indicator.window_seconds, indicator.phase_seconds, indicator.thresholds]);
      if (phase !== null && phase !== chartPhase) { rollingPoints = []; rollingGap = false; chartPhase = phase; chartWindows.delete('algorithm-rolling-chart'); }
      if (indicator) chartRollingThresholds = indicator.thresholds;
      if (indicator?.status === 'ready' && finite(indicator.z)) {
        rollingPoints.push({time, modelTime, quoteTime, z: indicator.z, context: chartPhase, gap: rollingGap}); rollingPoints = trimChart(rollingPoints, time); rollingGap = false;
      } else rollingGap = true;
    }
    chartSides = (value.eligibility?.sides || []).map(side => ({outcome: side.outcome,
      probability: number(side.decision_probability), rawProbability: number(side.raw_probability),
      ask: number(side.price), entryFee: number(side.entry_fee_allowance)}));
    const target = Number(model.contract_identity.target_price).toLocaleString('en-US', {maximumFractionDigits: 8});
    set('algorithm-charts-context', `${model.asset} | ${model.source} | ${model.ticker} | target $${target} | ${model.contract_identity.open_time} to ${model.contract_identity.close_time} | chart times ${chartTimeZone}.`);
    paintDetailCharts(true);
    if (indicator) {
      const z = finite(indicator.z) ? `${indicator.z >= 0 ? '+' : ''}${indicator.z.toFixed(2)}` : 'Unavailable';
      const permitted = ['yes', 'no'].filter(side => indicator.entry_allowed[side]).map(side => side.toUpperCase());
      const timing = indicator.status === 'ready'
        ? `Current timing: ${permitted.length ? permitted.join(' / ') + ' statistical entry allowed' : 'no statistical entry allowed'}.`
        : 'Current timing: entries blocked while the rolling reference is not ready.';
      set('algorithm-rolling-chart-status', `${$('algorithm-rolling-chart-status')?.textContent || ''} Reference ${indicator.sample_count}/${indicator.min_samples} prior samples; z ${z}, entry magnitude ${indicator.thresholds.entry_z}. ${timing} ${indicator.reason}`);
    }
  }
  function paintPortfolioChart(current = false) {
    portfolioChartCurrent = current;
    const last = portfolioPoints.at(-1);
    const visibleTrades = last ? chartTrades.filter(trade => trade.time >= portfolioPoints[0].time && trade.time <= last.time) : [];
    paintSvg('algorithm-equity-chart', renderPortfolioChart(portfolioPoints, {trades: visibleTrades, timeZone: chartTimeZone, maxGapSeconds: chartGapSeconds, window: chartWindows.get('algorithm-equity-chart')}), portfolioPoints.length,
      Boolean(last) && !current, last ? last.time * 1000 : null);
    set('algorithm-equity-chart-status', last
      ? `${current ? '' : 'Last known: '}${portfolioPoints.length} shared paper portfolio observation${portfolioPoints.length === 1 ? '' : 's'}; ${visibleTrades.length} executed paper action marker${visibleTrades.length === 1 ? '' : 's'} in the observed time window.${last.equity === null ? ' Current marked equity is unavailable; cash and cost-basis equity remain separate.' : ''}${!current ? ' Waiting for a fresh portfolio snapshot.' : ''}`
      : 'Waiting for an observed shared paper portfolio snapshot. No earlier equity history is synthesized.');
    mark($('algorithm-equity-chart-status'), Boolean(last) && !current, last ? last.time * 1000 : null);
  }
  function observePortfolioCharts(portfolio, time) {
    if (chartAwaitingSnapshot) { portfolioGap = true; paintPortfolioChart(false); return; }
    if (!portfolio || !finite(time)) { portfolioGap = true; paintPortfolioChart(false); return; }
    if (time > lastPortfolioTime) {
      const marksFresh = portfolio.positions.every(position => position.mark_as_of !== null
        && time - position.mark_as_of >= -1 && time - position.mark_as_of <= 5);
      portfolioPoints.push({time, cash: Number(portfolio.cash), equity: portfolio.marked_equity === null || !marksFresh ? null : Number(portfolio.marked_equity),
        costBasis: Number(portfolio.equity_cost_basis), context: 'shared-paper-experiment', gap: portfolioGap});
      portfolioPoints = trimChart(portfolioPoints, time); lastPortfolioTime = time; portfolioGap = false;
      for (const trade of portfolio.recent_trades) {
        if (trade.status !== 'paper_filled' || !finite(trade.as_of) || trade.as_of > time || !['buy', 'sell', 'flip', 'settle'].includes(trade.action)) continue;
        const key = JSON.stringify([trade.client_order_id, trade.as_of, trade.action]);
        if (seenChartTrades.has(key)) continue;
        seenChartTrades.add(key); chartTrades.push({time: trade.as_of, action: trade.action, outcome: trade.outcome, count: Number(trade.count), key});
      }
      chartTrades = trimChart(chartTrades.sort((left, right) => left.time - right.time), time);
      seenChartTrades.clear(); for (const trade of chartTrades) seenChartTrades.add(trade.key);
    }
    const currentMarks = portfolio.positions.every(position => position.mark_as_of !== null
      && now() / 1000 - position.mark_as_of >= -1 && now() / 1000 - position.mark_as_of <= 5);
    paintPortfolioChart(time >= lastPortfolioTime && currentMarks);
  }
  function renderRolling(indicator, current, reset = false) {
    const panel = $('algorithm-rolling-panel');
    if (panel) panel.hidden = indicator === null && (!lastRolling || reset);
    const fields = ['samples', 'z', 'k', 'rsi', 'tail-yes', 'tail-no', 'residual'];
    if (indicator === null) {
      for (const key of fields) set(`algorithm-rolling-${key}`, 'Unavailable');
      set('algorithm-rolling-status', lastRolling && !reset ? `STALE: last known rolling values at ${timeLabel(lastRolling.stamp)}.` : 'Waiting for rolling observations.');
      if (!lastRolling || reset) set('algorithm-rolling-reference', 'No current rolling reference.');
      set('algorithm-rolling-permissions', 'No current statistical entry trigger. Waiting for fresh observations.');
      set('algorithm-rolling-rsi-status', 'Waiting for residual momentum observations.');
      return;
    }
    const signed = value => value === null ? 'Unavailable' : `${value > 0 ? '+' : ''}${Math.abs(value) >= 10000 ? value.toExponential(2) : value.toFixed(2)}`;
    const points = value => value === null ? 'Unavailable' : `${signed(value * 100)} pp`;
    const phase = indicator.phase_seconds === 300 ? '5-minute expiry phase' : 'whole contract';
    set('algorithm-rolling-status', `${current ? '' : 'STALE | Last snapshot (not current): '}${indicator.asset} | ${indicator.reason}`);
    set('algorithm-rolling-samples', `${indicator.sample_count} / ${indicator.min_samples}`);
    set('algorithm-rolling-z', signed(indicator.z));
    set('algorithm-rolling-k', indicator.stochastic_k === null ? 'Unavailable' : `${indicator.stochastic_k.toFixed(1)}%`);
    const hasRSI = Object.hasOwn(indicator, 'rsi');
    set('algorithm-rolling-rsi', hasRSI && indicator.rsi !== null ? indicator.rsi.toFixed(1) : 'Unavailable');
    set('algorithm-rolling-rsi-status', !hasRSI ? 'Legacy snapshot: Wilder RSI was not recorded. Stochastic %K is a separate range-position statistic.'
      : `${current ? '' : 'STALE | Last snapshot: '}Wilder RSI(${indicator.rsi_period}) of the model-minus-midpoint residual, sampled in five-second buckets. ${indicator.rsi === null
        ? `Warming: ${indicator.rsi_sample_count}/${indicator.rsi_period} seed changes (${indicator.rsi_period + 1} contiguous observations needed).`
        : `Momentum confirmation: YES ${indicator.rsi >= indicator.rsi_entry ? 'passes' : 'blocked'} at ≥ ${indicator.rsi_entry}; NO ${indicator.rsi <= 100 - indicator.rsi_entry ? 'passes' : 'blocked'} at ≤ ${100 - indicator.rsi_entry}.`} ${indicator.rsi_filter ? 'RSI filters new entries only.' : 'RSI is diagnostic; baseline entries are unchanged.'} RSI is not a win probability.`);
    set('algorithm-rolling-tail-yes', percent(indicator.tail_yes));
    set('algorithm-rolling-tail-no', percent(indicator.tail_no));
    set('algorithm-rolling-residual', points(indicator.residual));
    const reference = `Prior ${indicator.window_seconds}s within the ${phase} | mean ${points(indicator.mean)} | sample SD ${points(indicator.sd)} | minimum attainable tail rank ${percent(indicator.minimum_tail_probability)} | entry |z| ≥ ${indicator.thresholds.entry_z} / exit z ${indicator.thresholds.exit_z} | nominal tail threshold ${percent(indicator.thresholds.alpha)}`;
    const missingReference = (reference.includes('Unavailable') || !current) && lastRolling?.reference;
    set('algorithm-rolling-reference', missingReference ? lastRolling.reference : reference);
    mark($('algorithm-rolling-reference'), Boolean(missingReference) || !current, missingReference ? lastRolling.stamp : displayStamp);
    if (current && indicator.status === 'ready') lastRolling = {stamp: displayStamp, reference};
    const entries = ['yes', 'no'].filter(side => indicator.entry_allowed[side]).map(side => side.toUpperCase());
    const exits = ['yes', 'no'].filter(side => indicator.exit_allowed[side]).map(side => side.toUpperCase());
    set('algorithm-rolling-permissions', !current ? 'Current statistical triggers are withheld until the forecast and quote are fresh and matched.'
      : indicator.status !== 'ready' ? 'New statistical entries are blocked. Existing risk, profit and model-convergence exits remain available.'
      : `Statistical entry trigger: ${entries.length ? entries.join(', ') : 'none'} | rolling convergence exit: ${exits.length ? exits.join(', ') : 'none'}. ${hasRSI
        ? `Baseline without RSI: ${['yes', 'no'].filter(side => indicator.baseline_entry_allowed[side]).map(side => side.toUpperCase()).join(', ') || 'none'}; ${indicator.rsi_filter ? 'RSI confirmation enabled' : 'RSI confirmation disabled'}. ` : ''}Executable cost and Kelly checks are assessed separately.`);
  }
  function matching(model, context = marketState) {
    const market = context?.market, identity = model?.contract_identity;
    const blocked = (code, reason) => ({matched: false, code, reason});
    if (model?.status !== 'ready' || !identity) return blocked('model_unavailable', 'No ready settlement forecast is available.');
    if (tickerAsset(identity.ticker) === selectedAsset && confirmedDetailKey !== null && identityKey(model) !== confirmedDetailKey) {
      const expected = confirmedDetailKey.split('|'), actual = identityKey(model).split('|');
      if (expected[0] !== actual[0]) return blocked('ticker_mismatch', 'The forecast and selected verified quote refer to different contracts.');
      if (expected[1] !== actual[1]) return blocked('strike_mismatch', 'The forecast and selected verified quote have different target prices.');
      return blocked('window_mismatch', 'The forecast and selected verified quote have different contract windows.');
    }
    if (context?.market_status !== 'ready' || !market) return blocked('quote_unavailable', 'The console has no current verified contract quote.');
    const age = context?.fetched_at ? now() - Date.parse(context.fetched_at) : Infinity;
    const modelAge = finite(model?.as_of) ? now() - model.as_of * 1000 : Infinity;
    if (age < -1000) return blocked('quote_future', 'The console quote timestamp is ahead of the local clock.');
    if (!Number.isFinite(age) || age > 5000) return blocked('quote_stale', 'The console quote is older than the five-second freshness limit.');
    if (modelAge < -1000) return blocked('model_future', 'The forecast timestamp is ahead of the local clock.');
    if (!Number.isFinite(modelAge) || modelAge > 5000) return blocked('model_stale', 'The forecast is older than the five-second freshness limit; waiting for the next model snapshot.');
    if (market.ticker !== identity.ticker) return blocked('ticker_mismatch', 'The forecast and console quote refer to different contracts.');
    if (canonical(market.target_price) !== canonical(identity.target_price)) return blocked('strike_mismatch', 'The forecast and console quote have different target prices.');
    if (Date.parse(market.open_time) !== Date.parse(identity.open_time) || Date.parse(market.close_time) !== Date.parse(identity.close_time)) {
      return blocked('window_mismatch', 'The forecast and console quote have different contract windows.');
    }
    if (now() < Date.parse(identity.open_time)) return blocked('window_pending', 'The forecast contract window has not opened.');
    if (now() >= Date.parse(identity.close_time)) return blocked('window_closed', 'The forecast contract window has closed.');
    return {matched: true, code: 'matched', reason: 'Fresh forecast and console quote match the same contract.'};
  }
  function addRow(body, values, passed = null) {
    if (!$(body)) return;
    const tr = doc.createElement('tr');
    for (const value of values) {
      const td = doc.createElement('td');
      td.textContent = object(value) && Object.hasOwn(value, 'text') ? value.text : value;
        if (object(value) && Object.hasOwn(value, 'text')) {
          mark(td, value.stale, value.stamp);
          if (value.title) td.title = `${td.title ? `${td.title} ` : ''}${value.title}`;
        }
      tr.appendChild(td);
    }
    if (passed !== null) tr.className = passed ? 'algorithm-pass' : 'algorithm-block';
    $(body).appendChild(tr);
    return tr;
  }
  function renderSideComparison(decision, current, mismatchReason) {
    if (!current) {
      mark($('algorithm-side-rows'), detailStamp !== null, detailStamp);
      set('algorithm-side-comparison-status', `${detailStamp === null ? '' : `STALE: last known at ${timeLabel(detailStamp)}. `}Both-side comparison withheld: ${mismatchReason || 'waiting for matching current data.'}`);
      return;
    }
    if (!decision?.sides?.length) {
      mark($('algorithm-side-rows'), detailStamp !== null, detailStamp);
      set('algorithm-side-comparison-status', `${detailStamp === null ? '' : `STALE: last known comparison at ${timeLabel(detailStamp)}. `}This saved report does not include both-side diagnostics. Current entry checks remain below.`);
      return;
    }
    $('algorithm-side-rows')?.replaceChildren(); mark($('algorithm-side-rows'), false);
    set('algorithm-side-comparison-status', 'Both directions are evaluated as potential new purchases. Side checks still require the selected-side policy, shared gates and round-trip entry trigger. Reductions and flips follow separate position logic.');
    const fraction = value => value === null ? 'Unavailable' : `${(Number(value) * 100).toFixed(4)}%`;
    const edge = value => value === null ? 'Unavailable' : `${Number(value) >= 0 ? '+' : ''}${(Number(value) * 100).toFixed(4)} pp`;
    const contracts = value => value === null ? 'Unavailable' : Number(value).toLocaleString('en-US', {maximumFractionDigits: 6});
    for (const row of decision.sides) {
      const reasons = {
        nonpositive_edge: 'Net edge is nonpositive: full Kelly is zero before the multiplier.',
        below_one_contract: 'Kelly target is below one contract after bankroll and multiplier.',
        target_met: 'Existing holdings already meet the integer target.',
        positive_target: 'Positive Kelly target; other entry checks still apply.'
      };
      const firstFailure = row.checks.find(check => !check.passed);
      const explanation = [reasons[row.allocation_reason] || (row.eligible ? 'Side checks pass; shared entry gates still apply.' : 'Inspect the side checks.'),
        firstFailure?.reason].filter(Boolean).join(' ');
      const prices = `Buy ask ${dollars(row.price)} | sell bid ${dollars(row.bid)}`
        + (row.outcome === 'no' && row.yes_equivalent_sell_price !== null
          ? ` | equivalent YES sale at bid ${dollars(row.yes_equivalent_sell_price)}` : '');
      const fee = row.entry_fee_allowance === null ? 'Fee unavailable' : `${(Number(row.entry_fee_allowance) * 100).toFixed(4)} cents / contract`;
      addRow('algorithm-side-rows', [row.outcome === 'yes' ? 'Buy YES / long YES' : 'Buy NO / short YES',
        fraction(row.decision_probability), prices, `${fee} | all-in ${dollars(row.effective_cost)}`,
        `Before fee ${edge(row.pre_fee_edge)} | net ${edge(row.edge)}`,
        `Full ${fraction(row.full_kelly_fraction)} | applied ${fraction(row.applied_kelly_fraction)} | target ${dollars(row.target_cost)}`,
        `${contracts(row.target_contracts_unrounded)} before floor | target ${row.target_contracts} | held ${row.existing_contracts}`,
        explanation]);
    }
  }
  function renderLedgerSummary(portfolio, current, stamp = null) {
    if (portfolio) portfolioSummary = {positions: portfolio.positions.length, actions: portfolio.recent_trades.length, stamp};
    const prefix = current ? '' : 'Last known: ', suffix = current ? '' : ' Waiting for a fresh paper portfolio snapshot.';
    set('algorithm-position-status', portfolioSummary
      ? `${prefix}${portfolioSummary.positions === 0 ? 'No open model positions in this paper experiment.'
        : `${portfolioSummary.positions} open model position${portfolioSummary.positions === 1 ? '' : 's'} in this paper experiment.`}${suffix}`
      : 'Waiting for a paper portfolio snapshot.');
    set('algorithm-trade-status', portfolioSummary
      ? `${prefix}${portfolioSummary.actions === 0 ? 'No executed model actions in this paper experiment yet. HOLD and blocked entry checks do not create action rows.'
        : `${portfolioSummary.actions} recent executed paper action${portfolioSummary.actions === 1 ? '' : 's'} shown.`}${suffix}`
      : 'Waiting for paper action history.');
    for (const id of ['algorithm-position-status', 'algorithm-trade-status']) mark($(id), Boolean(portfolioSummary) && !current, portfolioSummary?.stamp ?? null);
  }
  function renderLedgerEntry(decision, current, reason = null) {
    set('algorithm-ledger-entry-status', current && decision
      ? decision.kind === 'kalshi_fixed_budget_eligibility' ? `Minute-five fixed-budget entry: ${decision.reason}`
        : `Selected-asset entry ${decision.paper_eligible ? 'checks passed' : 'is blocked'}: ${decision.reason}`
      : `Current selected-asset entry checks are unavailable.${reason ? ` ${reason}` : ''}`);
    mark($('algorithm-ledger-entry-status'), false);
  }
  function renderPortfolio(portfolio, stamp = displayStamp) {
    if (!portfolio) {
      for (const id of ['algorithm-paper-cash', 'algorithm-paper-equity', 'algorithm-paper-realized', 'algorithm-paper-fees']) set(id, 'Unavailable');
      mark($('algorithm-position-rows'), portfolioStamp !== null, portfolioStamp); mark($('algorithm-trade-rows'), portfolioStamp !== null, portfolioStamp);
      renderLedgerSummary(null, false);
      return;
    }
    const priorStamp = displayStamp, priorFresh = displayFresh;
    displayStamp = stamp; displayFresh = stamp !== null && now() - stamp <= 10000 && now() - stamp >= -1000;
    if (displayFresh) portfolioStamp = stamp;
    $('algorithm-position-rows')?.replaceChildren(); $('algorithm-trade-rows')?.replaceChildren();
    mark($('algorithm-position-rows'), !displayFresh, stamp); mark($('algorithm-trade-rows'), !displayFresh, stamp);
    renderLedgerSummary(portfolio, displayFresh, stamp);
    const marked = position => position.mark_as_of !== null && now() - position.mark_as_of * 1000 >= -1000 && now() - position.mark_as_of * 1000 <= 5000;
    set('algorithm-paper-cash', dollars(portfolio?.cash ?? null));
    set('algorithm-paper-equity', dollars(portfolio && portfolio.positions.every(marked) ? portfolio.marked_equity : null));
    set('algorithm-paper-realized', dollars(portfolio?.realized_pnl ?? null));
    set('algorithm-paper-fees', dollars(portfolio?.fees_paid ?? null));
    const present = new Set(portfolio.positions.map(position => `${position.ticker}|${position.outcome}|`));
    for (const key of positionCache.keys()) if (![...present].some(prefix => key.startsWith(prefix))) positionCache.delete(key);
    const cell = (position, field, value, format, usable = true) => {
      const key = `${position.ticker}|${position.outcome}|${field}`, saved = positionCache.get(key);
        if (value === null || !usable) return saved ? {...saved, stale: true} : 'Unavailable';
      const text = format(value), fieldStamp = ['last_bid', 'marked_value', 'unrealized_pnl', 'model_probability', 'market_probability', 'gap'].includes(field)
        ? position.mark_as_of * 1000 : stamp;
        const title = `${field === 'average_entry_price' ? 'Fee-inclusive cost basis per contract. ' : ''}${field === 'marked_value' ? 'Visible-depth liquidation value after exit fees. ' : ''}${field === 'unrealized_pnl' ? 'Liquidation value minus fee-inclusive cost basis. ' : ''}${fieldStamp === null ? 'Source timestamp unavailable.' : `Source snapshot ${new Date(fieldStamp).toISOString()}.`}`;
        const rendered = {text, stamp: fieldStamp, title};
        if (displayFresh) positionCache.set(key, rendered);
        return {...rendered, stale: !displayFresh};
    };
    {
      for (const position of portfolio.positions) if ($('algorithm-position-rows')) addRow('algorithm-position-rows',
          [position.ticker, position.outcome.toUpperCase(), cell(position, 'quantity', position.quantity, value => value),
            cell(position, 'average_entry_price', position.average_entry_price, dollars),
            cell(position, 'cost_basis', position.cost_basis, dollars), cell(position, 'last_bid', position.last_bid, dollars, marked(position)),
          cell(position, 'marked_value', position.marked_value, dollars, marked(position)),
          cell(position, 'unrealized_pnl', position.unrealized_pnl, dollars, marked(position)),
          cell(position, 'model_probability', position.model_probability, value => percent(Number(value)), marked(position)),
          cell(position, 'market_probability', position.market_probability, value => percent(Number(value)), marked(position)),
          cell(position, 'gap', position.gap, value => `${(Number(value) * 100).toFixed(2)} pp`, marked(position))]);
      for (const trade of portfolio.recent_trades) if ($('algorithm-trade-rows')) addRow('algorithm-trade-rows',
          [trade.as_of === null ? 'Unavailable' : {text: new Date(trade.as_of * 1000).toLocaleTimeString(), stale: !displayFresh,
            stamp: trade.as_of * 1000, title: `Recorded paper action ${new Date(trade.as_of * 1000).toISOString()}.`}, actionLabel(trade),
          trade.ticker, trade.outcome?.toUpperCase() || '-', trade.count || '-', `${trade.action === 'flip' ? 'YES ' : ''}${dollars(trade.price)}`,
          dollars(trade.action === 'flip' ? trade.net_cash_flow : trade.reserved_cost), dollars(trade.realized_pnl), trade.reason]);
    }
    displayStamp = priorStamp; displayFresh = priorFresh;
  }
  const identityKey = model => model?.contract_identity ? [model.contract_identity.ticker,
    canonical(model.contract_identity.target_price), model.contract_identity.open_time, model.contract_identity.close_time].join('|') : null;
  function staleOverview(reason) {
    if (!$('algorithm-asset-rows') || !overviewRows.size) return;
    $('algorithm-asset-rows').replaceChildren();
    for (const [asset, previous] of overviewRows) {
      const saved = overviewCache.get(asset);
      const row = addRow('algorithm-asset-rows', [asset, saved?.ticker || previous[1], saved?.probability || previous[2], 'STALE / waiting for fresh data',
        'Awaiting current data', reason, saved ? `Last known ${timeLabel(saved.stamp)}` : previous[6]]); mark(row, true, saved?.stamp ?? null);
    }
  }
  function renderDetails(value, portfolio = value.portfolio) {
    const fixed = value.strategy === 'minute_five';
    strategyDisplay(value);
    const age = value.fetched_at === null ? Infinity : now() - Date.parse(value.fetched_at);
    if (value.status === 'unavailable' || age > 10000 || age < -1000) {
      clear(value.status === 'unavailable' ? value.reason : 'Selected asset snapshot is stale; waiting for fresh data.', {keepReport: true, keepOverview: true});
      renderPortfolio(portfolio, value.fetched_at === null ? now() : Date.parse(value.fetched_at)); return;
    }
    const model = value.forecast, decision = value.eligibility, match = matching(model, value.market_snapshot || marketState);
    const key = identityKey(model);
    const modelKey = fixed ? JSON.stringify(['minute_five', model?.model_version ?? null, model?.config_hash ?? null]) : model?.components?.length ? JSON.stringify([model.model_version ?? null, model.config_hash ?? null, model.components.map(row => [row.id, row.weight]), Boolean(value.indicator),
      value.indicator ? [value.indicator.window_seconds, value.indicator.phase_seconds, value.indicator.thresholds] : null]) : null;
    const changedContract = key !== null && detailKey !== null && key !== detailKey;
    const mismatch = ['ticker_mismatch', 'strike_mismatch', 'window_mismatch'].includes(match.code);
    if (changedContract || mismatch || modelKey !== null && detailModelKey !== null && modelKey !== detailModelKey) resetDetail();
    if (!mismatch && modelKey !== null) detailModelKey = modelKey;
    displayStamp = finite(model?.as_of) ? model.as_of * 1000 : Date.parse(value.fetched_at);
    displayFresh = match.matched;
    if (key !== null && !mismatch) { detailKey = key; if (displayFresh) detailStamp = displayStamp; }
    set('algorithm-status', producerDescription + value.reason); set('algorithm-updated', `Snapshot ${new Date(value.fetched_at).toLocaleTimeString()}${finite(model?.as_of) ? ` | forecast ${new Date(model.as_of * 1000).toLocaleTimeString()}` : ''} | read-only monitor`);
    set('algorithm-raw-p', percent(mismatch ? null : (fixed ? model?.monte_carlo_se : model?.probability_yes) ?? null));
    set('algorithm-yes-p', percent(mismatch ? null : (fixed ? model?.probability_yes : model?.conservative_probability_yes) ?? null));
    set('algorithm-no-p', percent(mismatch ? null : (fixed ? model?.probability_no : model?.conservative_probability_no) ?? null));
    set('algorithm-disagreement', fixed ? dollars(mismatch ? null : model?.median_settlement_average ?? null) : percent(mismatch ? null : model?.model_disagreement ?? null));
    if (!mismatch && model?.ticker) set('algorithm-contract', model.ticker);
    if (!mismatch && model?.components?.length && (displayFresh || detailStamp === null)) {
      $('algorithm-members').replaceChildren();
      for (const member of model.components) addRow('algorithm-members', [member.id, percent(member.weight), percent(member.probability_yes), member.status]);
      mark($('algorithm-members'), !displayFresh, displayStamp);
    } else mark($('algorithm-members'), detailStamp !== null, detailStamp);
    $('algorithm-checks').replaceChildren();
    const current = decision && match.matched;
    const fixedExpired = fixed && decision?.phase === 'entry' && fixedPhase(model, decision, value.paper_order) === 'missed';
    observeDetailCharts(value, value.market_snapshot || marketState, match.matched);
    renderLedgerEntry(decision, Boolean(current) && !fixedExpired, fixedExpired ? 'The minute-five entry window closed locally; prior entry checks have expired.'
      : match.matched ? 'No entry checks were supplied.' : match.reason);
    if (!fixed) renderSideComparison(decision, current, match.reason);
    renderRolling(fixed || mismatch ? null : value.indicator, Boolean(current), fixed);
    if (!current) {
      if (detailStamp !== null) {
        set('algorithm-status', `${producerDescription}STALE: ${match.reason} Last known values are retained.`);
        set('algorithm-updated', `STALE display | last known ${timeLabel(detailStamp)} | waiting for fresh data`);
      }
      set('algorithm-decision', 'Waiting for matching data');
      set('algorithm-proposal', `No current paper proposal. ${match.matched ? 'The latest snapshot has no entry checks.' : match.reason}`);
      addRow('algorithm-checks', ['Blocked', 'Current model / quote match', match.matched ? 'The latest snapshot has no entry checks.' : match.reason], false);
      for (const check of decision?.checks || []) addRow('algorithm-checks', [check.passed ? 'Snapshot pass' : 'Snapshot blocked', `Last snapshot: ${check.label}`, check.reason], check.passed);
    } else {
      const exitRecorded = value.paper_order?.status === 'paper_filled' && ['sell', 'settle'].includes(value.paper_order.action);
      const flipRecorded = value.paper_order?.status === 'paper_filled' && value.paper_order.action === 'flip';
      set('algorithm-decision', fixed ? fixedLabel(model, decision, value.paper_order) : flipRecorded ? 'Paper flip recorded' : exitRecorded ? 'Paper exit recorded' : value.paper_order?.status === 'paper_filled' ? 'Paper fill recorded' : decision.paper_eligible ? 'Paper eligible' : 'Paper blocked');
      set('algorithm-proposal', fixed ? fixedSummary(model, decision, value.paper_order, portfolio) : flipRecorded ? `${actionLabel(value.paper_order)} | ${value.paper_order.reason}` : exitRecorded ? `${actionLabel(value.paper_order)} ${value.paper_order.count} | ${value.paper_order.reason}` : decision.selected_outcome
        ? `${decision.selected_outcome.toUpperCase()} | held ${decision.existing_contracts} | target ${decision.target_contracts} | add ${decision.additional_contracts} | reserve ${dollars(decision.reserved_cost)} | price ${dollars(decision.price)}`
        : decision.reason);
      if (fixedExpired) addRow('algorithm-checks', ['Blocked', 'Current minute-five window', 'The local clock has reached 6:00; no late entry is permitted.'], false);
      for (const check of decision.checks) addRow('algorithm-checks', [fixedExpired ? check.passed ? 'Snapshot pass' : 'Snapshot blocked' : check.passed ? 'Pass' : fixed && ['waiting', 'held'].includes(fixedPhase(model, decision, value.paper_order)) ? 'Waiting / held' : 'Blocked', check.label, check.reason], !fixedExpired && check.passed);
    }
    set('algorithm-live-gate', fixed ? 'Paper only' : 'Blocked: uncalibrated');
    if (fixed) set('algorithm-minute-five-status', current ? MINUTE_FIVE_NOTE : `No current minute-five decision. ${match.reason}`);
    const order = value.paper_order;
    set('algorithm-last-action', current ? order ? actionLabel(order) : 'HOLD' : 'Awaiting current data');
    set('algorithm-paper-order', order
      ? `${current ? '' : 'Last snapshot result (not current): '}${order.status === 'paper_filled' ? order.action === 'buy' ? 'Simulated fill' : `Simulated ${order.action}` : order.status === 'none' ? 'No paper order' : `Paper order ${order.status}`}${order.action === 'flip' ? `: ${actionLabel(order)}` : order.outcome && order.count !== null ? `: ${order.outcome.toUpperCase()} ${order.count} contracts` : ''}${order.price === null ? '' : ` | ${order.action === 'flip' ? 'YES-book price' : 'price'} ${dollars(order.price)}`}${order.action === 'flip' ? ` | net cash flow ${dollars(order.net_cash_flow)}` : order.reserved_cost === null ? '' : ` | ${['sell', 'settle'].includes(order.action) ? 'net proceeds' : 'reserved cost'} ${dollars(order.reserved_cost)}`}${order.realized_pnl === null ? '' : ` | realized${order.action === 'flip' ? ' on closed leg' : ''} ${dollars(order.realized_pnl)}`} | ${order.reason}`
      : 'Paper runner result pending.');
    if (current && order) lastPaperResult = $('algorithm-paper-order')?.textContent || null;
    renderPortfolio(portfolio, Date.parse(value.fetched_at));
  }
  function renderOverview(value) {
    if ($('algorithm-overview')) $('algorithm-overview').hidden = false;
    const selector = $('algorithm-view-asset');
    if (selector) {
      selector.replaceChildren();
      for (const asset of Object.keys(VIEW_ASSETS)) {
        const option = doc.createElement('option'); option.value = asset;
        option.textContent = value.assets.includes(asset) ? asset : `${asset} (not in strategy)`; selector.appendChild(option);
      }
      selector.value = selectedAsset;
    }
    $('algorithm-asset-rows')?.replaceChildren();
    for (const key of overviewCache.keys()) if (!value.assets.includes(key)) overviewCache.delete(key);
    for (const key of overviewRows.keys()) if (!value.assets.includes(key)) overviewRows.delete(key);
    for (const asset of value.assets) {
      const item = value.markets[asset], model = item.forecast;
      const age = item.fetched_at === null ? Infinity : now() - Date.parse(item.fetched_at);
      const available = item.status === 'ready' && age >= -1000 && age <= 10000;
      const match = available ? matching(model, item.market_snapshot || marketState) : {matched: false,
        reason: item.status === 'unavailable' ? item.reason : 'Asset snapshot is stale; waiting for fresh data.'};
      const current = available && match.matched;
      const key = identityKey(model), saved = overviewCache.get(asset);
      if (key !== null && saved && key !== saved.key) overviewCache.delete(asset);
      if (current && model) overviewCache.set(asset, {key, ticker: model.ticker, probability: percent(model.probability_yes), stamp: model.as_of * 1000});
      const cached = overviewCache.get(asset);
      const filled = item.paper_order?.status === 'paper_filled';
      const gate = !current ? 'Waiting for fresh data' : item.strategy === 'minute_five' ? fixedLabel(model, item.eligibility, item.paper_order)
        : filled ? 'Paper fill recorded' : item.eligibility?.paper_eligible ? 'Paper eligible' : 'Paper blocked';
      const cells = [asset, model?.ticker || cached?.ticker || 'No current contract',
        current ? percent(model?.probability_yes ?? null) : cached?.probability || (available ? percent(model?.probability_yes ?? null) : 'Unavailable'),
        gate, current ? item.paper_order ? actionLabel(item.paper_order) : 'HOLD' : 'Awaiting current data',
        current ? item.paper_order?.reason || item.eligibility?.reason || item.reason : `${cached ? 'STALE: last known value. ' : ''}${match.reason}`,
        !current && cached ? `Last known ${timeLabel(cached.stamp)}` : item.fetched_at === null ? 'No snapshot' : new Date(item.fetched_at).toLocaleTimeString()];
      overviewRows.set(asset, cells);
      const row = addRow('algorithm-asset-rows', cells, current && (filled || item.eligibility?.paper_eligible));
      mark(row, !current && Boolean(cached), cached?.stamp ?? null);
    }
    if (value.markets[selectedAsset]) renderDetails(value.markets[selectedAsset], value.portfolio);
    else {
      clear(`${selectedAsset} is not selected in the current paper strategy.`, {keepReport: true, keepOverview: true, dropDetail: true});
      set('algorithm-contract', `${selectedAsset}: no strategy sample`); renderPortfolio(value.portfolio);
    }
  }
  function render(value) {
    if (value.monitor_source === 'external_report' || value.status === 'ready') {
      const context = JSON.stringify([value.monitor_source ?? 'managed', value.monitor_session ?? null]);
      if (monitorContext !== null && context !== monitorContext) resetExperiment();
      monitorContext = context;
    }
    producerDescription = value.monitor_source === 'external_report'
      ? `External paper console report${value.producer_status ? ` (${value.producer_status})` : ''}; browser controls manage a separate worker. `
      : value.status === 'ready' ? '' : producerDescription;
    if (selectedAsset === 'GOLD') {
      if (value.status === 'ready') {
        const receivedTime = Date.parse(value.fetched_at);
        if (receivedTime < latestReadyTime) return;
        latestReadyTime = receivedTime; report = value;
        if (now() - receivedTime <= 10000 && now() - receivedTime >= -1000) observePortfolioCharts(value.portfolio, receivedTime / 1000);
      }
      clear(GOLD_MODEL_NOTE, {keepReport: true, dropDetail: true});
      set('algorithm-status', producerDescription + GOLD_MODEL_NOTE);
      set('algorithm-contract', marketState?.market?.ticker?.startsWith('KXGOLD15M-') ? marketState.market.ticker : 'GOLD | market-data view');
      set('algorithm-charts-context', GOLD_MODEL_NOTE);
      for (const name of ['probability', 'rolling', 'edge']) set(`algorithm-${name}-chart-status`, GOLD_MODEL_NOTE);
      if (value.portfolio) renderPortfolio(value.portfolio, value.fetched_at === null ? null : Date.parse(value.fetched_at));
      return;
    }
    if (value.status === 'unavailable') {
      clear(value.reason);
      if (value.portfolio) { report = value; renderPortfolio(value.portfolio, null); }
      return;
    }
    const receivedTime = Date.parse(value.fetched_at);
    if (receivedTime < latestReadyTime || receivedTime === latestReadyTime && report?.status !== 'ready') {
      // Both endpoints contain the same cached experiment. Older responses
      // cannot replace a newer sample or revive a cleared snapshot.
      if (report?.status === 'ready') render(report);
      return;
    }
    if (now() - receivedTime > 10000 || now() - receivedTime < -1000) {
      clear('Algorithm snapshot is stale; waiting for the runner.'); return;
    }
    latestReadyTime = receivedTime; report = value;
    observePortfolioCharts(value.portfolio, receivedTime / 1000);
    if (value.kind === 'kalshi_multi_algorithm_state') renderOverview(value);
    else {
      if ($('algorithm-overview')) $('algorithm-overview').hidden = true;
      $('algorithm-asset-rows')?.replaceChildren();
      if (value.forecast?.asset === selectedAsset || !value.forecast && selectedAsset === 'BTC') renderDetails(value);
      else {
        clear(`${selectedAsset} is not selected in the current paper strategy.`, {keepReport: true, dropDetail: true});
        set('algorithm-contract', `${selectedAsset}: no strategy sample`); renderPortfolio(value.portfolio);
      }
    }
  }
  async function refresh() {
    if (stopped || fetching) return;
    fetching = true; const expected = generation, activeController = new AbortController(); controller = activeController;
    const timeout = win.setTimeout(() => activeController.abort(), 12000);
    try {
      const response = await fetchImpl('/api/algorithm', {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: activeController.signal});
      if (!response.ok) fail(response.status === 404 ? 'Restart the local service to load the algorithm monitor.' : 'Algorithm monitor is unavailable.');
      const body = await response.text();
      if (new TextEncoder().encode(body).byteLength > 4_000_000) fail('Algorithm snapshot exceeds its supported size.');
      const value = validateAlgorithmReport(JSON.parse(body));
      if (stopped || expected !== generation) return;
      render(value);
    } catch (error) {
      if (!stopped && expected === generation) clear(error.message || 'Algorithm monitor is unavailable.');
    } finally { win.clearTimeout(timeout); if (expected === generation) { fetching = false; controller = null; } }
  }
  function setMarketState(value) {
    marketState = value; managed?.setServiceState(value);
    if (selectedAsset === 'GOLD') {
      if (report) render(report);
      else { clear(GOLD_MODEL_NOTE, {dropDetail: true}); set('algorithm-status', GOLD_MODEL_NOTE); }
      return;
    }
    if (value?.market_status === 'ready' && tickerAsset(value.market?.ticker) === selectedAsset) {
      const key = [value.market.ticker, canonical(value.market.target_price), value.market.open_time, value.market.close_time].join('|');
      confirmedDetailKey = key;
      if (detailKey !== null && key !== detailKey) { resetDetail(); set('algorithm-contract', 'Waiting for the newly selected verified contract.'); }
    }
    if (report) render(report);
  }
  function setAsset(value) {
    const next = typeof value === 'string' ? value.replace(/-USD$/, '') : value;
    if (!Object.hasOwn(VIEW_ASSETS, next)) fail('Choose BTC, ETH, SOL, XRP or GOLD.');
    if (stopped || next === selectedAsset) return;
    selectedAsset = next; confirmedDetailKey = null; resetDetail();
    if (report) render(report);
    else clear(selectedAsset === 'GOLD' ? GOLD_MODEL_NOTE : `Waiting for the ${selectedAsset} strategy sample.`);
  }
  function refreshNow() { generation++; controller?.abort(); fetching = false; return refresh(); }
  for (const [id, getPoints] of [
    ['algorithm-probability-chart', () => probabilityPoints],
    ['algorithm-rolling-chart', () => rollingPoints],
    ['algorithm-equity-chart', () => portfolioPoints]
  ]) $(id)?.addEventListener('chart-range-change', event => {
    const {start, end} = event?.detail ?? {}, points = getPoints();
    if (!finite(start) || !finite(end) || start < 0 || end > 1 || start >= end || points.length < 2) return;
    if (start === 0 && end === 1) chartWindows.delete(id);
    else {
      const minimum = points[0].time, maximum = points.at(-1).time;
      if (maximum <= minimum) return;
      chartWindows.set(id, {minimum: minimum + start * (maximum - minimum), maximum: minimum + end * (maximum - minimum)});
    }
    if (id === 'algorithm-equity-chart') paintPortfolioChart(portfolioChartCurrent);
    else paintDetailCharts(detailChartsCurrent, detailChartsReason);
  });
  $('algorithm-refresh').addEventListener('click', refreshNow);
  $('algorithm-view-asset')?.addEventListener('change', () => {
    const asset = $('algorithm-view-asset').value;
    if (Object.hasOwn(VIEW_ASSETS, asset) && asset !== selectedAsset) { setAsset(asset); onAssetChange(asset); }
  });
  const poll = win.setInterval(refresh, pollMs);
  const clock = win.setInterval(() => {
    if (report?.fetched_at && now() - Date.parse(report.fetched_at) > 10000) clear('Algorithm snapshot is stale; waiting for the runner.');
    else if (report) render(report);
  }, 1000);
  function dispose() { stopped = true; generation++; controller?.abort(); win.clearInterval(poll); win.clearInterval(clock); managed?.dispose(); }
  win.addEventListener('pagehide', dispose);
  const ready = refresh();
  const managed = mountManagedPaperControls(doc, win, {fetchImpl, pollMs: Math.max(5000, pollMs), onSnapshot: value => {
    // A managed snapshot is bound to the configuration in the same response.
    // An earlier standalone report has no experiment ID to establish that link.
    if (configurationKey !== null) chartAwaitingSnapshot = false;
    render(value);
  },
    onConfiguration: (value, status) => {
      const key = JSON.stringify(value);
      if (configurationKey !== null && key !== configurationKey) {
        generation++; controller?.abort(); fetching = false;
        resetExperiment(); clear('Loading the selected paper experiment.');
      }
      if (key !== configurationKey) {
        if (configurationKey === null) {
          // Preserve the existing table/scalar monitor while discarding chart
          // observations whose paper experiment has not yet been identified.
          resetDetailCharts(); portfolioPoints = []; chartTrades = []; seenChartTrades.clear();
          chartWindows.delete('algorithm-equity-chart');
          lastPortfolioTime = -Infinity; portfolioGap = false; paintPortfolioChart(false);
        }
        chartAwaitingSnapshot = true;
        set('algorithm-charts-context', `${selectedAsset} | Waiting for a snapshot from the confirmed paper experiment.`);
      }
      configurationKey = key;
      if (RUNNER_ACTIVE.has(status)) {
        chartPolicy = Object.fromEntries(['entry_edge', 'exit_fee', 'exit_edge'].map(field => [field, value[field] === null ? null : Number(value[field])]));
        paintDetailCharts(probabilityPoints.length > 0 && now() - lastChartTime * 1000 <= 5000);
      }
    }});
  const currentReport = () => {
    if (selectedAsset === 'GOLD') return null;
    if (!report || report.status !== 'ready' || now() - Date.parse(report.fetched_at) > 10000) return null;
    const item = report.kind === 'kalshi_multi_algorithm_state' ? report.markets[selectedAsset] : report;
    return item && matching(item.forecast, item.market_snapshot || marketState).matched ? report : null;
  };
  return {ready, refresh: refreshNow, setMarketState, setAsset, getAsset: () => selectedAsset, dispose, getReport: currentReport, runner: managed};
}
