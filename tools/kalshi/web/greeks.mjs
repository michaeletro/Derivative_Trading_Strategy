// Read-only local diffusion probabilities and sensitivities. No order capability.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const finite = value => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e100;
const label = (value, name, maximum = 2000) => typeof value === 'string' && value.length <= maximum
  && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail(`Invalid ${name}.`);
const number = (value, name, minimum = -1e100, maximum = 1e100) => finite(value) && value >= minimum && value <= maximum
  ? value : fail(`Invalid ${name}.`);
const prob = (value, name) => number(value, name, 0, 1);
const close = (left, right, tolerance = 1e-8) => Math.abs(left - right) <= tolerance * Math.max(1, Math.abs(left), Math.abs(right));
const GREEKS = ['delta_per_dollar', 'gamma_per_dollar_squared', 'theta_per_second', 'vega_per_dollar_volatility',
  'delta_pp_per_dollar', 'delta_pp_per_100_dollars', 'probability_diffusion_per_sqrt_second', 'ito_drift_per_second'];
const ASSETS = Object.freeze({
  BTC: {series: 'KXBTC15M', index: 'BRTI', source: 'CF_BRTI', shock: '100', digits: 2},
  ETH: {series: 'KXETH15M', index: 'ETHUSD_RTI', source: 'CF_ETHUSD_RTI', shock: '5', digits: 2},
  SOL: {series: 'KXSOL15M', index: 'SOLUSD_RTI', source: 'CF_SOLUSD_RTI', shock: '0.25', digits: 4},
  XRP: {series: 'KXXRP15M', index: 'XRPUSD_RTI', source: 'CF_XRPUSD_RTI', shock: '0.005', digits: 4}
});
const assetForTicker = ticker => typeof ticker === 'string' ? Object.keys(ASSETS).find(asset =>
  new RegExp(`^${ASSETS[asset].series}-[A-Za-z0-9_.:-]{1,119}$`).test(ticker)) ?? null : null;
const modelVersion = asset => `cf-${ASSETS[asset].index.toLowerCase().replaceAll('_', '-')}-frozen-local-diffusion-greeks-v1`;
export function validateFittedParameters(raw, asset) {
  if (raw == null) return null;
  if (!object(raw) || raw.asset !== asset || !Object.hasOwn(ASSETS, asset)
      || raw.model_scope !== 'frozen_arithmetic_diffusion'
      || raw.calibration_status !== 'fitted_research_not_live_validated'
      || typeof raw.artifact_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(raw.artifact_sha256)
      || typeof raw.created_at !== 'string' || raw.created_at.length > 64
      || !Number.isFinite(Date.parse(raw.created_at))) fail('Invalid historical fit metadata.');
  const result = {asset, model_scope: raw.model_scope, calibration_status: raw.calibration_status,
    artifact_sha256: raw.artifact_sha256, created_at: raw.created_at,
    half_life_seconds: number(raw.half_life_seconds, 'fitted half-life', 1, 3600),
    volatility_scale: number(raw.volatility_scale, 'fitted volatility multiplier', .5, 2),
    test_contract_count: number(raw.test_contract_count, 'held-out sample count', 20, 100000)};
  if (!Number.isSafeInteger(result.test_contract_count)) fail('Invalid historical fit sample count.');
  for (const key of ['test_baseline_log_loss', 'test_fitted_log_loss', 'test_baseline_brier_score', 'test_fitted_brier_score']) {
    result[key] = number(raw[key], key, 0, key.includes('brier') ? 1 : 1000000);
  }
  const improved = result.test_fitted_log_loss < result.test_baseline_log_loss
    && result.test_fitted_brier_score < result.test_baseline_brier_score;
  if (typeof raw.out_of_sample_improved !== 'boolean' || raw.out_of_sample_improved !== improved) fail('Inconsistent historical fit comparison.');
  result.out_of_sample_improved = improved;
  return result;
}
const fittedStatus = fit => fit ? `Historical fit active: ${(fit.half_life_seconds / 60).toFixed(2)} min EWMA half-life; volatility x${fit.volatility_scale.toFixed(4)}. ` : '';
const fittedEvidence = fit => fit ? [
  `Held-out test: ${fit.test_contract_count} contracts; lower scores are better. Log loss baseline ${fit.test_baseline_log_loss.toFixed(6)} / fitted ${fit.test_fitted_log_loss.toFixed(6)}; Brier baseline ${fit.test_baseline_brier_score.toFixed(6)} / fitted ${fit.test_fitted_brier_score.toFixed(6)}.`,
  fit.out_of_sample_improved ? 'Fitted research parameters; held-out scores improved on this sample. Live performance remains unvalidated.'
    : 'Fitted research parameters; held-out results did not improve both scores. Live performance remains unvalidated.'
] : [];
function scenario(raw, base) {
  if (!object(raw)) fail('Invalid probability scenario.');
  const result = {};
  for (const key of ['shock_dollars', 'elapsed_seconds', 'shocked_spot', 'exact_change_pp']) result[key] = number(raw[key], key);
  if (result.elapsed_seconds < 0 || result.elapsed_seconds > 30 || result.shocked_spot <= 0) fail('Invalid scenario state.');
  for (const key of ['exact_probability_yes', 'exact_probability_no']) result[key] = prob(raw[key], key);
  if (!close(result.exact_probability_yes + result.exact_probability_no, 1)
      || !close(result.exact_change_pp, 100 * (result.exact_probability_yes - base))) fail('Inconsistent scenario probabilities.');
  const empty = raw.taylor_probability_yes === null;
  for (const key of ['taylor_probability_yes', 'taylor_probability_no', 'taylor_change_pp', 'approximation_error_pp']) {
    result[key] = empty && raw[key] === null ? null : number(raw[key], key);
  }
  result.taylor_in_probability_bounds = raw.taylor_in_probability_bounds;
  if (empty) {
    if (result.taylor_in_probability_bounds !== null && result.taylor_in_probability_bounds !== false) fail('Invalid unavailable Taylor approximation.');
  } else if (typeof result.taylor_in_probability_bounds !== 'boolean'
      || !close(result.taylor_probability_yes + result.taylor_probability_no, 1)
      || result.taylor_in_probability_bounds !== (result.taylor_probability_yes >= 0 && result.taylor_probability_yes <= 1)
      || !close(result.taylor_change_pp, 100 * (result.taylor_probability_yes - base))
      || !close(Math.abs(result.approximation_error_pp), 100 * Math.abs(result.taylor_probability_yes - result.exact_probability_yes))) {
    fail('Inconsistent Taylor approximation.');
  }
  return result;
}
export function validateProbabilityGreeks(raw, expectedAsset = null) {
  if (!object(raw) || raw.kind !== 'kalshi_probability_greeks' || raw.version !== 1
      || !['ready', 'unavailable'].includes(raw.status) || raw.execution_eligible !== false || raw.calibrated !== false) fail('Unsupported probability sensitivities.');
  const asset = raw.asset ?? assetForTicker(raw.ticker) ?? expectedAsset ?? 'BTC';
  if (!Object.hasOwn(ASSETS, asset) || expectedAsset !== null && asset !== expectedAsset
      || raw.model_version !== modelVersion(asset) || raw.source != null && raw.source !== ASSETS[asset].source
      || asset !== 'BTC' && raw.source !== ASSETS[asset].source) fail('Probability sensitivities belong to a different settlement source.');
  const result = {kind: raw.kind, version: 1, status: raw.status, execution_eligible: false, calibrated: false, asset, source: ASSETS[asset].source,
    model_version: label(raw.model_version, 'model version', 150), reason: label(raw.reason, 'model status'),
    quote_id: raw.quote_id == null ? null : label(raw.quote_id, 'contract identity', 64),
    ticker: raw.ticker === null ? null : label(raw.ticker, 'contract ticker', 128),
    as_of: raw.as_of === null ? null : number(raw.as_of, 'as-of timestamp', 0, 253402300799),
    model_time: raw.model_time === null ? null : number(raw.model_time, 'model timestamp', 0, 253402300799),
    probability_yes: null, probability_no: null, parameters: {}, greeks: {}, no_greeks: {}, scenario: null, scenarios: [], assumptions: []};
  result.fitted_parameters = validateFittedParameters(raw.fitted_parameters, asset);
  if (result.ticker !== null && assetForTicker(result.ticker) !== asset) fail('Invalid probability contract.');
  if (result.quote_id !== null && !/^[a-f0-9]{64}$/.test(result.quote_id)) fail('Invalid probability contract identity.');
  if (!Array.isArray(raw.assumptions) || raw.assumptions.length > 40) fail('Invalid model assumptions.');
  result.assumptions = raw.assumptions.map(value => label(value, 'model assumption'));
  if (!object(raw.parameters) || !object(raw.greeks) || !object(raw.no_greeks)
      || !object(raw.units) || !object(raw.diagnostics) || !Array.isArray(raw.scenarios) || raw.scenarios.length > 101) fail('Invalid sensitivity details.');
  if (raw.status === 'unavailable') {
    if (raw.probability_yes !== null || raw.probability_no !== null || raw.model_time !== null
        || raw.scenario !== null || raw.scenarios.length || Object.keys(raw.greeks).length
        || Object.keys(raw.no_greeks).length || Object.keys(raw.parameters).length) fail('Unavailable model retains current sensitivities.');
    return result;
  }
  if (!result.ticker || result.as_of === null || result.model_time === null || result.model_time > result.as_of
      || result.as_of - result.model_time > 5) fail('Invalid current model time.');
  result.probability_yes = prob(raw.probability_yes, 'YES probability');
  result.probability_no = prob(raw.probability_no, 'NO probability');
  if (!close(result.probability_yes + result.probability_no, 1)) fail('Inconsistent outcome probabilities.');
  for (const key of ['spot', 'target', 'close_time', 'seconds_to_close', 'log_volatility_per_sqrt_second',
    'annualized_log_volatility', 'dollar_volatility_per_sqrt_second', 'known_sum', 'known_count', 'future_count',
    'covariance_time_sum', 'max_scenario_elapsed_seconds', 'next_sample_seconds']) result.parameters[key] = number(raw.parameters[key], key, 0);
  result.parameters.drift_dollars_per_second = number(raw.parameters.drift_dollars_per_second, 'model drift');
  const p = result.parameters;
  if (raw.parameters.ewma_half_life_seconds != null) p.ewma_half_life_seconds = number(raw.parameters.ewma_half_life_seconds, 'fitted half-life', 1, 3600);
  if (raw.parameters.volatility_scale != null) p.volatility_scale = number(raw.parameters.volatility_scale, 'fitted volatility multiplier', .5, 2);
  if (result.fitted_parameters && (!close(p.ewma_half_life_seconds, result.fitted_parameters.half_life_seconds)
      || !close(p.volatility_scale, result.fitted_parameters.volatility_scale))) fail('Current sensitivities differ from the historical fit.');
  if (p.spot <= 0 || p.target <= 0 || p.close_time <= result.as_of || p.seconds_to_close <= 0
      || !Number.isInteger(p.known_count) || !Number.isInteger(p.future_count) || p.known_count + p.future_count !== 60
      || p.future_count < 1 || p.next_sample_seconds <= 0 || p.max_scenario_elapsed_seconds > 30
      || p.max_scenario_elapsed_seconds > p.next_sample_seconds) fail('Invalid settlement sensitivity state.');
  const deterministic = p.dollar_volatility_per_sqrt_second === 0;
  for (const key of GREEKS) {
    result.greeks[key] = deterministic && raw.greeks[key] === null ? null : number(raw.greeks[key], key);
    result.no_greeks[key] = deterministic && raw.no_greeks[key] === null ? null : number(raw.no_greeks[key], key);
    if (result.greeks[key] !== null && !close(result.greeks[key], -result.no_greeks[key])) fail('Inconsistent NO sensitivity.');
  }
  if (!deterministic && (result.greeks.delta_per_dollar < 0
      || !close(result.greeks.delta_pp_per_dollar, 100 * result.greeks.delta_per_dollar)
      || !close(result.greeks.delta_pp_per_100_dollars, 10000 * result.greeks.delta_per_dollar))) fail('Inconsistent delta units.');
  result.scenario = scenario(raw.scenario, result.probability_yes);
  result.scenarios = raw.scenarios.map(row => scenario(row, result.probability_yes));
  for (const row of [result.scenario, ...result.scenarios]) {
    if (!close(row.shocked_spot, p.spot + row.shock_dollars) || row.elapsed_seconds >= p.next_sample_seconds) fail('Scenario crosses an unobserved settlement sample.');
  }
  return result;
}

const formatted = (value, digits = 3) => value === null ? 'Unavailable' : value.toLocaleString('en-US', {maximumFractionDigits: digits, minimumFractionDigits: digits});
const signed = (value, digits = 3) => value === null ? 'Unavailable' : `${value >= 0 ? '+' : ''}${formatted(value, digits)}`;
const percent = value => value === null ? 'Unavailable' : `${formatted(value * 100, 2)}%`;
const scientific = value => value === null ? 'Unavailable' : value.toExponential(4);
const VALUE_IDS = ['yes', 'no', 'spot', 'target', 'age', 'vol', 'dollar-vol', 'delta', 'gamma', 'theta', 'vega', 'ito'];
export function mountGreeks(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 5000, asset = 'BTC'} = {}) {
  if (!doc.getElementById('greeks-panel')) return null;
  const $ = id => doc.getElementById(`greeks-${id}`), set = (id, value) => { if ($(id)) $(id).textContent = value; };
  if (!Object.hasOwn(ASSETS, asset)) fail('Choose a supported crypto asset.');
  let selectedAsset = asset, explicitlySelected = false, currentTicker = null, currentIdentity = null, market = null, report = null, version = 0, stopped = false, controller = null;
  let lastDisplay = null, lastScenarioText = '';
  const displayedValues = new Map();
  let shock = ASSETS[asset].shock, elapsed = '0', draftNeedsValidation = false;
  const scenarioDrafts = new Map([[asset, {shock, elapsed}]]);
  if (asset !== 'BTC' && $('scenario-shock')) $('scenario-shock').value = shock;
  const fresh = value => market !== null && value.status === 'ready' && value.asset === selectedAsset && value.ticker === currentTicker
    && (currentIdentity === null || value.quote_id === currentIdentity)
    && (market?.target_price == null || Number(market.target_price) === value.parameters.target)
    && (market?.close_time == null || close(Date.parse(market.close_time) / 1000, value.parameters.close_time, 1e-12))
    && value.as_of <= now() / 1000 + 1 && now() / 1000 - value.as_of <= 5
    && now() / 1000 - value.model_time <= 5 && now() / 1000 < value.parameters.close_time;
  function mark(id, stale, description = '') {
    const node = $(id); if (!node) return;
    node.setAttribute?.('data-stale', stale ? 'true' : 'false');
    node.setAttribute?.('title', description);
  }
  function displayedCell(node, key, text, value, available = true) {
    if (!node) return;
    const cached = displayedValues.get(key);
    if (!available && cached) {
      node.textContent = `${cached.text} [stale]`;
      node.setAttribute?.('data-stale', 'true');
      node.setAttribute?.('title', `Last known ${new Date(cached.as_of * 1000).toLocaleTimeString('en-US')}; the current snapshot has no value.`);
      return;
    }
    node.textContent = text; node.setAttribute?.('data-stale', 'false'); node.setAttribute?.('title', '');
    if (available) displayedValues.set(key, {text, as_of: value.as_of});
  }
  const valueCell = (id, text, value, available = true) => displayedCell($(id), id, text, value, available);
  function clear(message, fit = null, {reset = false} = {}) {
    report = null;
    if (!reset && lastDisplay) {
      for (const id of VALUE_IDS) {
        const cached = displayedValues.get(id);
        if (cached) set(id, `${cached.text} [stale]`);
        mark(id, true, `Last known snapshot ${new Date((cached?.as_of ?? lastDisplay.as_of) * 1000).toLocaleTimeString('en-US')}; display only.`);
      }
      set('age', `${formatted(Math.max(0, now() / 1000 - lastDisplay.model_time), 1)}s [stale]`);
      set('status', `${fittedStatus(fit)}STALE / last known. ${message}`);
      set('updated', `Last known CF ${ASSETS[selectedAsset].index} snapshot ${new Date(lastDisplay.as_of * 1000).toLocaleTimeString('en-US')}; source event ${new Date(lastDisplay.model_time * 1000).toLocaleTimeString('en-US')}.`);
      set('scenario-result', `Last known scenario: ${lastScenarioText}`);
      for (const id of ['chart', 'scenario-rows']) mark(id, true, 'Last known snapshot; not current model data.');
      return;
    }
    lastDisplay = null; lastScenarioText = ''; displayedValues.clear();
    for (const id of VALUE_IDS) set(id, 'Unavailable');
    for (const id of [...VALUE_IDS, 'chart', 'scenario-rows']) mark(id, false);
    set('status', fittedStatus(fit) + message); set('updated', 'No fresh model snapshot'); set('scenario-result', 'No current scenario estimate.');
    set('contract', `No current ${selectedAsset} model contract`);
    $('scenario-rows')?.replaceChildren(); $('assumptions')?.replaceChildren(); $('chart')?.replaceChildren();
    for (const content of fittedEvidence(fit)) {
      if (typeof doc.createElement !== 'function') break;
      const element = doc.createElement('li'); element.textContent = content; $('assumptions')?.appendChild(element);
    }
  }
  function chart(value) {
    const svg = $('chart');
    if (!svg || typeof doc.createElementNS !== 'function') return;
    svg.replaceChildren();
    svg.setAttribute('aria-label', `${selectedAsset} settlement probability versus underlying price change; exact model and Taylor approximation`);
    const curveRows = value.scenario.elapsed_seconds === 0 ? [...value.scenarios, value.scenario] : value.scenarios;
    const rows = [...new Map(curveRows.map(row => [row.shock_dollars, row])).values()].sort((a, b) => a.shock_dollars - b.shock_dollars);
    if (rows.length < 2) return;
    const left = rows[0].shock_dollars, right = rows.at(-1).shock_dollars;
    if (left >= right) return;
    const add = (tag, fields, content = '') => {
      const node = doc.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, val] of Object.entries(fields)) node.setAttribute(key, String(val));
      node.textContent = content; svg.appendChild(node); return node;
    };
    const x = value => 52 + 532 * (value - left) / (right - left), y = value => 210 - 180 * value;
    add('text', {x: 52, y: 15, class: 'chart-axis-title'}, `${selectedAsset} P(YES)`);
    add('line', {x1: 178, x2: 198, y1: 11, y2: 11, stroke: '#79b8ff', 'stroke-width': 2});
    add('text', {x: 205, y: 15, class: 'chart-label', fill: '#79b8ff'}, 'Exact model');
    add('line', {x1: 338, x2: 358, y1: 11, y2: 11, stroke: '#e8b86d', 'stroke-width': 2, 'stroke-dasharray': '5 3'});
    add('text', {x: 365, y: 15, class: 'chart-label', fill: '#e8b86d'}, 'Taylor approximation');
    for (const level of [0, .25, .5, .75, 1]) {
      add('line', {x1: 52, y1: y(level), x2: 584, y2: y(level), class: 'chart-grid'});
      add('text', {x: 42, y: y(level) + 4, 'text-anchor': 'end', class: 'chart-label'}, `${level * 100}%`);
    }
    const path = key => {
      let drawing = false;
      return rows.map(row => {
        if (row[key] === null || row[key] < 0 || row[key] > 1) { drawing = false; return ''; }
        const command = `${drawing ? 'L' : 'M'}${x(row.shock_dollars)},${y(row[key])}`;
        drawing = true; return command;
      }).filter(Boolean).join(' ');
    };
    add('path', {d: path('exact_probability_yes'), class: 'chart-line chart-blue'});
    add('path', {d: path('taylor_probability_yes'), class: 'chart-line chart-target'});
    const strikeShock = value.parameters.target - value.parameters.spot;
    if (strikeShock >= left && strikeShock <= right) {
      add('line', {x1: x(strikeShock), y1: 27, x2: x(strikeShock), y2: 210, class: 'chart-target', 'stroke-dasharray': '5 4'});
      add('text', {x: x(strikeShock), y: 27, 'text-anchor': 'middle', class: 'chart-target-label'}, 'Target');
    }
    for (const row of rows) add('circle', {cx: x(row.shock_dollars), cy: y(row.exact_probability_yes), r: 2.5, class: 'chart-point chart-blue'});
    for (const row of rows) if (row.taylor_probability_yes !== null && row.taylor_probability_yes >= 0 && row.taylor_probability_yes <= 1) {
      add('circle', {cx: x(row.shock_dollars), cy: y(row.taylor_probability_yes), r: 2, fill: '#e8b86d'});
    }
    if (value.scenario.elapsed_seconds === 0) add('circle', {cx: x(value.scenario.shock_dollars), cy: y(value.scenario.exact_probability_yes), r: 4, fill: '#79b8ff', stroke: '#e6edf3', 'stroke-width': 1.5});
    for (const row of [...new Set([rows[0], rows[Math.floor(rows.length / 2)], rows.at(-1)])]) {
      add('text', {x: x(row.shock_dollars), y: 230, 'text-anchor': row === rows[0] ? 'start' : row === rows.at(-1) ? 'end' : 'middle', class: 'chart-label'}, `${row.shock_dollars >= 0 ? '+' : '-'}$${formatted(Math.abs(row.shock_dollars), ASSETS[selectedAsset].digits)}`);
    }
    add('text', {x: 318, y: 251, 'text-anchor': 'middle', class: 'chart-axis-title'}, `${selectedAsset} price change (USD); time held fixed`);
  }
  function render(value) {
    report = value;
    lastDisplay = value;
    const p = value.parameters, g = value.greeks;
    set('status', fittedStatus(value.fitted_parameters) + value.reason); set('contract', value.ticker);
    set('source-label', `${selectedAsset} CF ${ASSETS[selectedAsset].index} reference`);
    valueCell('yes', percent(value.probability_yes), value); valueCell('no', percent(value.probability_no), value);
    valueCell('spot', `$${formatted(p.spot, ASSETS[selectedAsset].digits)}`, value);
    valueCell('target', p.target.toLocaleString('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: ASSETS[selectedAsset].digits, maximumFractionDigits: 8}), value);
    valueCell('age', `${formatted(now() / 1000 - value.model_time, 1)}s`, value);
    valueCell('vol', `${formatted(p.annualized_log_volatility * 100, 2)}%`, value);
    valueCell('dollar-vol', `$${formatted(p.dollar_volatility_per_sqrt_second, 3)} / √s`, value);
    valueCell('delta', selectedAsset === 'BTC' ? `${signed(g.delta_pp_per_100_dollars, 3)} pp / $100` : `${signed(g.delta_pp_per_dollar, 4)} pp / USD`, value, g.delta_per_dollar !== null);
    valueCell('gamma', `${scientific(g.gamma_per_dollar_squared)} / $²`, value, g.gamma_per_dollar_squared !== null);
    valueCell('theta', g.theta_per_second === null ? 'Unavailable' : `${signed(g.theta_per_second * 100, 4)} pp / s`, value, g.theta_per_second !== null);
    valueCell('vega', `${scientific(g.vega_per_dollar_volatility)} / ($/√s)`, value, g.vega_per_dollar_volatility !== null);
    valueCell('ito', `${scientific(g.ito_drift_per_second)} / s`, value, g.ito_drift_per_second !== null);
    set('updated', `CF ${ASSETS[selectedAsset].index} snapshot ${new Date(value.as_of * 1000).toLocaleTimeString('en-US')} | ${p.known_count}/60 settlement samples fixed`);
    const row = value.scenario;
    lastScenarioText = `${signed(row.shock_dollars, ASSETS[selectedAsset].digits)} USD after ${formatted(row.elapsed_seconds, 3)}s: P(YES) ${percent(row.exact_probability_yes)} (${signed(row.exact_change_pp, 3)} pp). `
      + (row.taylor_probability_yes === null ? 'Taylor sensitivities unavailable for zero volatility.'
        : `Taylor estimate ${percent(row.taylor_probability_yes)}; absolute error ${formatted(Math.abs(row.approximation_error_pp), 3)} pp${row.taylor_in_probability_bounds ? '.' : '; approximation is outside probability bounds.'}`);
    set('scenario-result', lastScenarioText);
    for (const id of ['chart', 'scenario-rows']) mark(id, false);
    const make = tag => doc.createElement(tag);
    $('scenario-rows')?.replaceChildren();
    for (const row of value.scenarios) {
      const tr = make('tr');
      const texts = [signed(row.shock_dollars, ASSETS[selectedAsset].digits), percent(row.exact_probability_yes), signed(row.exact_change_pp, 3),
        percent(row.taylor_probability_yes), row.approximation_error_pp === null ? 'Unavailable' : formatted(Math.abs(row.approximation_error_pp), 3),
        row.taylor_probability_yes === null ? 'Unavailable' : row.taylor_in_probability_bounds ? 'Within bounds' : 'Outside probability bounds'];
      for (const [index, text] of texts.entries()) {
        const td = make('td');
        displayedCell(td, `scenario:${row.shock_dollars}:${row.elapsed_seconds}:${index}`, text, value,
          index < 3 || row.taylor_probability_yes !== null);
        tr.appendChild(td);
      }
      $('scenario-rows')?.appendChild(tr);
    }
    $('assumptions')?.replaceChildren();
    for (const text of [...value.assumptions, ...fittedEvidence(value.fitted_parameters)]) { const li = make('li'); li.textContent = text; $('assumptions')?.appendChild(li); }
    chart(value);
  }
  function input() {
    const selectedShock = $('scenario-shock')?.value.trim() ?? shock;
    const selectedElapsed = $('scenario-elapsed')?.value.trim() ?? elapsed;
    if (!/^-?(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?$/.test(selectedShock)
        || !/^(?:0|[1-9][0-9]?)(?:\.[0-9]{1,6})?$/.test(selectedElapsed)
        || Math.abs(Number(selectedShock)) > 100000 || Number(selectedElapsed) > 30) fail('Enter a price change within ±$100,000 and elapsed time from 0 to 30 seconds.');
    return [selectedShock, selectedElapsed];
  }
  async function refresh({applyInputs = false} = {}) {
    if (stopped || !currentTicker || !market) return;
    const requestVersion = ++version, ticker = currentTicker, identity = currentIdentity, requestAsset = selectedAsset;
    controller?.abort(); const active = new AbortController(); controller = active;
    try {
      if (applyInputs || draftNeedsValidation) {
        const selected = input();
        if (Number(selected[0]) !== Number(shock) || Number(selected[1]) !== Number(elapsed)) {
          clear('Loading the selected price and time scenario.', null, {reset: true});
        }
        [shock, elapsed] = selected; draftNeedsValidation = false;
      }
      const requestShock = Number(shock), requestElapsed = Number(elapsed);
      const params = new URLSearchParams({market_ticker: ticker, shock_dollars: shock, elapsed_seconds: elapsed});
      if (requestAsset !== 'BTC') params.set('asset', requestAsset);
      const response = await fetchImpl(`/api/greeks?${params}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: active.signal});
      const body = await response.text();
      if (stopped || requestVersion !== version || ticker !== currentTicker || identity !== currentIdentity) return;
      if (response.status === 404) {
        clear('The running backend needs a restart to enable SDE probability and Greeks. Stop the console with Ctrl+C, restart it with the same command, then refresh this page.');
        return;
      }
      if (!response.ok || body.length > 200000) fail('Probability model unavailable.');
      const value = validateProbabilityGreeks(JSON.parse(body), requestAsset);
      if (stopped || requestVersion !== version || ticker !== currentTicker || identity !== currentIdentity) return;
      if (value.status === 'unavailable') { clear(value.reason, value.fitted_parameters); return; }
      if (!fresh(value)) { clear('Model data is stale or belongs to a different contract.'); return; }
      if (!close(value.scenario.shock_dollars, requestShock, 1e-12) || !close(value.scenario.elapsed_seconds, requestElapsed, 1e-12)) fail('Returned probability scenario differs from the selected price and time changes.');
      render(value);
    } catch (error) {
      if (!stopped && requestVersion === version) clear(error.message.startsWith('Enter a price change')
        ? error.message : `Probability model unavailable; refresh after checking CF ${ASSETS[selectedAsset].index} access and scenario inputs.`);
    } finally {
      if (requestVersion === version) controller = null;
    }
  }
  function selectAsset(value, explicit) {
    if (!Object.hasOwn(ASSETS, value)) fail('Choose a supported crypto asset.');
    if (explicit) explicitlySelected = true;
    if (doc.getElementById('greeks-title')) doc.getElementById('greeks-title').textContent = `${value} probability sensitivities`;
    if (doc.getElementById('greeks-chart-heading')) doc.getElementById('greeks-chart-heading').textContent = `${value} probability response to price`;
    if (value === selectedAsset) return;
    scenarioDrafts.set(selectedAsset, {shock: $('scenario-shock')?.value ?? shock, elapsed: $('scenario-elapsed')?.value ?? elapsed});
    selectedAsset = value; currentTicker = null; currentIdentity = null; market = null; version++; controller?.abort(); controller = null;
    const draft = scenarioDrafts.get(value) ?? {shock: ASSETS[value].shock, elapsed: '0'};
    shock = draft.shock; elapsed = draft.elapsed; draftNeedsValidation = true;
    if ($('scenario-shock')) $('scenario-shock').value = shock;
    if ($('scenario-elapsed')) $('scenario-elapsed').value = elapsed;
    clear(`Waiting for a verified ${value} contract.`, null, {reset: true});
  }
  function setAsset(value) { return selectAsset(value, true); }
  function setMarketState(state) {
    const stateAsset = state?.selected_asset ?? assetForTicker(state?.market?.ticker);
    if (!explicitlySelected && Object.hasOwn(ASSETS, stateAsset)) selectAsset(stateAsset, false);
    const ticker = state?.market_status === 'ready' && assetForTicker(state?.market?.ticker) === selectedAsset
      && (state?.selected_asset == null || state.selected_asset === selectedAsset) ? state.market.ticker : null;
    market = ticker ? state.market : null;
    if (!ticker) {
      version++; controller?.abort(); controller = null;
      clear(`Waiting for a verified ${selectedAsset} contract.`);
      return;
    }
    const identity = ticker && typeof state?.market?.quote_id === 'string' && /^[a-f0-9]{64}$/.test(state.market.quote_id) ? state.market.quote_id : null;
    const termsChanged = lastDisplay && ((market.target_price != null && Number(market.target_price) !== lastDisplay.parameters.target)
      || (market.close_time != null && !close(Date.parse(market.close_time) / 1000, lastDisplay.parameters.close_time, 1e-12)));
    if (ticker !== currentTicker || identity !== currentIdentity || termsChanged) {
      currentTicker = ticker; currentIdentity = identity; version++; controller?.abort(); controller = null;
      clear(ticker ? `Loading ${selectedAsset} settlement probability sensitivities.` : `Waiting for a verified ${selectedAsset} contract.`, null, {reset: true});
      set('contract', ticker ?? `Waiting for a verified ${selectedAsset} contract.`);
      if (ticker) return refresh();
    }
    if (!report) return refresh();
    if (report && !fresh(report)) clear('Model snapshot is stale; waiting for a fresh acquisition.');
  }
  $('scenario-form')?.addEventListener('submit', event => { event.preventDefault(); refresh({applyInputs: true}); });
  const timer = win.setInterval(() => refresh(), pollMs);
  const ageTimer = win.setInterval(() => {
    if (report && !fresh(report)) clear('Model snapshot is stale; waiting for a fresh acquisition.');
    else if (report) set('age', `${formatted(now() / 1000 - report.model_time, 1)}s`);
    else if (lastDisplay) set('age', `${formatted(Math.max(0, now() / 1000 - lastDisplay.model_time), 1)}s [stale]`);
  }, 1000);
  function dispose() { stopped = true; version++; report = null; controller?.abort(); controller = null; win.clearInterval(timer); win.clearInterval(ageTimer); }
  win.addEventListener('pagehide', dispose);
  clear(`Waiting for a verified ${selectedAsset} contract.`);
  return {ready: Promise.resolve(), refresh, setAsset, setMarketState, dispose, getReport: () => report, getAsset: () => selectedAsset};
}
