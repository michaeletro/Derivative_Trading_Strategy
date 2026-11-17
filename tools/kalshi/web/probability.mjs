// Current settlement probability and same-model sensitivities. GET requests only.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fail = message => { throw new Error(message); };
const text = value => typeof value === 'string' && value.length <= 2000 && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail('Invalid model label.');
const number = (value, low = -1e100, high = 1e100) => finite(value) && value >= low && value <= high ? value : fail('Invalid model number.');
const probability = value => number(value, 0, 1);
const same = (left, right, tolerance = 1e-8) => Math.abs(left - right) <= tolerance * Math.max(1, Math.abs(left), Math.abs(right));
const ASSETS = Object.freeze({
  BTC: {series: 'KXBTC15M', index: 'BRTI', source: 'CF_BRTI', digits: 2},
  ETH: {series: 'KXETH15M', index: 'ETHUSD_RTI', source: 'CF_ETHUSD_RTI', digits: 2},
  SOL: {series: 'KXSOL15M', index: 'SOLUSD_RTI', source: 'CF_SOLUSD_RTI', digits: 4},
  XRP: {series: 'KXXRP15M', index: 'XRPUSD_RTI', source: 'CF_XRPUSD_RTI', digits: 4}
});
const GOLD_MODEL_REASON = 'Gold probabilities are unavailable in this CF Monte Carlo panel. The separate Gold adapter uses Pyth Metal.Index.1OZGOLD/USD and its minute-close settlement model; the crypto final-minute-average model does not apply.';
const viewAsset = value => value === 'GOLD' || Object.hasOwn(ASSETS, value);
const assetForTicker = ticker => typeof ticker === 'string' ? Object.keys(ASSETS).find(asset =>
  new RegExp(`^${ASSETS[asset].series}-[A-Za-z0-9_.:-]{1,119}$`).test(ticker)) ?? null : null;
const modelVersion = asset => `cf-${ASSETS[asset].index.toLowerCase().replaceAll('_', '-')}-local-diffusion-mc-v1`;
const IDENTITY = /^[a-f0-9]{64}$/;
const GREEKS = ['delta_per_dollar', 'gamma_per_dollar_squared', 'theta_per_second', 'vega_per_dollar_volatility',
  'delta_pp_per_dollar', 'delta_pp_per_100_dollars', 'probability_diffusion_per_sqrt_second', 'ito_drift_per_second'];

export function validateFittedParameters(raw, asset) {
  if (raw == null) return null;
  if (!object(raw) || raw.asset !== asset || !Object.hasOwn(ASSETS, asset)
      || raw.model_scope !== 'frozen_arithmetic_diffusion'
      || raw.calibration_status !== 'fitted_research_not_live_validated'
      || typeof raw.artifact_sha256 !== 'string' || !IDENTITY.test(raw.artifact_sha256) || typeof raw.created_at !== 'string'
      || raw.created_at.length > 64 || !finite(Date.parse(raw.created_at))) fail('Invalid historical fit metadata.');
  const result = {asset, model_scope: raw.model_scope, calibration_status: raw.calibration_status,
    artifact_sha256: raw.artifact_sha256, created_at: raw.created_at,
    half_life_seconds: number(raw.half_life_seconds, 1, 3600),
    volatility_scale: number(raw.volatility_scale, .5, 2),
    test_contract_count: number(raw.test_contract_count, 20, 100000)};
  if (!Number.isSafeInteger(result.test_contract_count)) fail('Invalid historical fit sample count.');
  if (Object.hasOwn(raw, 'history_window_seconds') || Object.hasOwn(raw, 'min_live_history_seconds')) {
    if (!Number.isSafeInteger(raw.history_window_seconds) || raw.history_window_seconds < 1800
        || raw.history_window_seconds > 3600 || !Number.isSafeInteger(raw.min_live_history_seconds)
        || raw.min_live_history_seconds !== raw.history_window_seconds - 5) fail('Invalid fitted history window.');
    result.history_window_seconds = raw.history_window_seconds;
    result.min_live_history_seconds = raw.min_live_history_seconds;
  }
  for (const key of ['test_baseline_log_loss', 'test_fitted_log_loss', 'test_baseline_brier_score', 'test_fitted_brier_score']) {
    result[key] = number(raw[key], 0, key.includes('brier') ? 1 : 1000000);
  }
  const improved = result.test_fitted_log_loss < result.test_baseline_log_loss
    && result.test_fitted_brier_score < result.test_baseline_brier_score;
  if (typeof raw.out_of_sample_improved !== 'boolean' || raw.out_of_sample_improved !== improved) fail('Inconsistent historical fit comparison.');
  result.out_of_sample_improved = improved;
  return result;
}
const fittedStatus = fit => fit ? `Historical fit active: ${(fit.half_life_seconds / 60).toFixed(2)} min EWMA half-life; volatility x${fit.volatility_scale.toFixed(4)}. ${fit.history_window_seconds === undefined ? '' : `${(fit.history_window_seconds / 60).toFixed(2)} min fitted history window. `}` : '';
const fittedEvidence = fit => fit ? [
  `Held-out test: ${fit.test_contract_count} contracts; lower scores are better. Log loss baseline ${fit.test_baseline_log_loss.toFixed(6)} / fitted ${fit.test_fitted_log_loss.toFixed(6)}; Brier baseline ${fit.test_baseline_brier_score.toFixed(6)} / fitted ${fit.test_fitted_brier_score.toFixed(6)}.`,
  fit.out_of_sample_improved ? 'Fitted research parameters; held-out scores improved on this sample. Live performance remains unvalidated.'
    : 'Fitted research parameters; held-out results did not improve both scores. Live performance remains unvalidated.'
] : [];

export function validateLiveProbability(raw, expectedAsset = null) {
  if (!object(raw) || raw.kind !== 'kalshi_live_probability' || raw.version !== 1 || !['ready', 'unavailable'].includes(raw.status)
      || raw.execution_eligible !== false || raw.calibrated !== false) fail('Unsupported live probability model.');
  const asset = raw.asset ?? assetForTicker(raw.ticker) ?? expectedAsset ?? 'BTC';
  if (!Object.hasOwn(ASSETS, asset) || expectedAsset !== null && expectedAsset !== asset
      || raw.model_version !== modelVersion(asset) || raw.source != null && raw.source !== ASSETS[asset].source
      || asset !== 'BTC' && raw.source !== ASSETS[asset].source) fail('Live probability belongs to a different settlement source.');
  const result = {kind: raw.kind, version: 1, status: raw.status, execution_eligible: false, calibrated: false, asset, source: ASSETS[asset].source,
    model_version: raw.model_version, reason: text(raw.reason), ticker: raw.ticker == null ? null : text(raw.ticker),
    quote_id: raw.quote_id == null ? null : text(raw.quote_id), as_of: raw.as_of == null ? null : number(raw.as_of, 0, 253402300799),
    model_time: raw.model_time == null ? null : number(raw.model_time, 0, 253402300799),
    probability_yes: null, probability_no: null, monte_carlo_se: null, analytic_probability_yes: null, sampling_interval: null,
    parameters: {}, time: {}, greeks: {}, diagnostics: {}, probability_paths: [], probability_fan: [], sensitivity_fan: [], observed_sensitivities: [], assumptions: []};
  result.fitted_parameters = validateFittedParameters(raw.fitted_parameters, asset);
  if (result.ticker !== null && assetForTicker(result.ticker) !== asset || result.quote_id !== null && !IDENTITY.test(result.quote_id)) fail('Invalid probability contract.');
  if (!Array.isArray(raw.assumptions) || raw.assumptions.length > 40) fail('Invalid probability assumptions.');
  result.assumptions = raw.assumptions.map(text);
  if (!object(raw.parameters) || !object(raw.greeks) || !object(raw.diagnostics)
      || !Array.isArray(raw.probability_paths) || !Array.isArray(raw.probability_fan)) fail('Invalid probability details.');
  if (raw.status === 'unavailable') {
    if (raw.probability_yes !== null || raw.probability_no !== null || raw.monte_carlo_se !== null
        || raw.analytic_probability_yes !== null || raw.sampling_interval !== null || raw.model_time !== null
        || raw.probability_paths.length || raw.probability_fan.length || Object.keys(raw.parameters).length || Object.keys(raw.greeks).length) fail('Unavailable probability retains model values.');
    return result;
  }
  if (!result.ticker || !result.quote_id || result.as_of === null || result.model_time === null
      || result.model_time > result.as_of || result.as_of - result.model_time > 5) fail('Invalid current probability time.');
  result.probability_yes = probability(raw.probability_yes); result.probability_no = probability(raw.probability_no);
  result.analytic_probability_yes = probability(raw.analytic_probability_yes); result.monte_carlo_se = number(raw.monte_carlo_se, 0, .5);
  if (!same(result.probability_yes + result.probability_no, 1)) fail('Inconsistent complementary probabilities.');
  if (!object(raw.sampling_interval) || !['wilson_95', 'deterministic'].includes(raw.sampling_interval.method)) fail('Invalid sampling interval.');
  result.sampling_interval = {method: raw.sampling_interval.method, lower: probability(raw.sampling_interval.lower), upper: probability(raw.sampling_interval.upper)};
  if (result.sampling_interval.lower > result.probability_yes + 1e-12 || result.sampling_interval.upper < result.probability_yes - 1e-12) fail('Sampling interval excludes its estimate.');
  for (const key of ['spot', 'target', 'close_time', 'known_count', 'future_count', 'dollar_volatility_per_sqrt_second']) result.parameters[key] = number(raw.parameters[key], 0);
  const p = result.parameters;
  if (raw.parameters.ewma_half_life_seconds != null) p.ewma_half_life_seconds = number(raw.parameters.ewma_half_life_seconds, 1, 3600);
  if (raw.parameters.volatility_scale != null) p.volatility_scale = number(raw.parameters.volatility_scale, .5, 2);
  if (result.fitted_parameters && (!same(p.ewma_half_life_seconds, result.fitted_parameters.half_life_seconds)
      || !same(p.volatility_scale, result.fitted_parameters.volatility_scale))) fail('Current probability parameters differ from the historical fit.');
  if (p.spot <= 0 || p.target <= 0 || p.close_time <= result.as_of || !Number.isInteger(p.known_count)
      || !Number.isInteger(p.future_count) || p.known_count + p.future_count !== 60 || p.future_count < 1) fail('Invalid current settlement state.');
  if (!object(raw.time)) fail('Missing fifteen-minute window clock.');
  for (const key of ['window_start', 'window_end', 'elapsed_seconds', 'remaining_seconds', 'progress']) result.time[key] = number(raw.time[key], 0);
  const time = result.time;
  if (!same(time.window_end - time.window_start, 900) || !same(time.window_end, p.close_time)
      || !same(time.elapsed_seconds, result.as_of - time.window_start) || !same(time.remaining_seconds, time.window_end - result.as_of)
      || !same(time.elapsed_seconds + time.remaining_seconds, 900) || !same(time.progress, time.elapsed_seconds / 900)
      || result.model_time < time.window_start) fail('Inconsistent fifteen-minute model clock.');
  const deterministic = p.dollar_volatility_per_sqrt_second === 0;
  if ((result.sampling_interval.method === 'deterministic') !== deterministic) fail('Inconsistent deterministic interval.');
  if (deterministic && (!same(result.sampling_interval.lower, result.probability_yes) || !same(result.sampling_interval.upper, result.probability_yes)
      || ![0, 1].includes(result.probability_yes) || result.monte_carlo_se !== 0)) fail('Invalid deterministic probability.');
  for (const key of GREEKS) result.greeks[key] = deterministic && raw.greeks[key] === null ? null : number(raw.greeks[key]);
  if (!deterministic && (result.greeks.delta_per_dollar < 0
      || !same(result.greeks.delta_pp_per_dollar, 100 * result.greeks.delta_per_dollar)
      || !same(result.greeks.delta_pp_per_100_dollars, 10000 * result.greeks.delta_per_dollar))) fail('Invalid probability delta units.');
  const d = raw.diagnostics;
  if (!Number.isSafeInteger(d.paths) || d.paths < 1 || d.paths > 20000 || !Number.isSafeInteger(d.seed) || d.seed < 0) fail('Invalid simulation count or seed.');
  result.diagnostics = {paths: d.paths, seed: d.seed, source_age_seconds: number(d.source_age_seconds, 0, 5),
    latest_received_at: number(d.latest_received_at, 0, result.as_of)};
  if (!same(result.diagnostics.source_age_seconds, result.as_of - result.model_time, 1e-12)
      || result.diagnostics.latest_received_at < result.model_time
      || Object.hasOwn(d, 'latest_event_time') && !same(number(d.latest_event_time, 0, result.as_of), result.model_time, 1e-12)) fail('Inconsistent probability source clocks.');
  if (Object.hasOwn(d, 'successes')) {
    if (!Number.isSafeInteger(d.successes) || d.successes < 0 || d.successes > d.paths
        || !same(result.probability_yes, d.successes / d.paths, 1e-12)
        || !same(result.monte_carlo_se, Math.sqrt(result.probability_yes * (1 - result.probability_yes) / d.paths), 1e-12)) fail('Monte Carlo count and sampling error disagree.');
    result.diagnostics.successes = d.successes;
  }
  if (raw.probability_fan.length < 2 || raw.probability_fan.length > 75 || raw.probability_paths.length < 1 || raw.probability_paths.length > 12) fail('Invalid probability path count.');
  result.probability_fan = raw.probability_fan.map((row, index) => {
    if (!object(row)) fail('Invalid probability fan.');
    const value = {seconds: number(row.seconds, 0, 905), p05: probability(row.p05), p50: probability(row.p50), p95: probability(row.p95)};
    if (value.p05 > value.p50 || value.p50 > value.p95 || index && value.seconds <= raw.probability_fan[index - 1].seconds) fail('Invalid probability quantiles.');
    return value;
  });
  const grid = result.probability_fan.map(row => row.seconds), first = result.probability_fan[0];
  if (grid[0] !== 0 || !same(grid.at(-1), p.close_time - result.model_time)
      || !['p05', 'p50', 'p95'].every(key => same(first[key], result.analytic_probability_yes))) fail('Inconsistent conditional probability horizon.');
  if (Object.hasOwn(d, 'probability_mean_path')) {
    if (!Array.isArray(d.probability_mean_path) || d.probability_mean_path.length !== grid.length) fail('Invalid conditional probability mean.');
    result.diagnostics.probability_mean_path = d.probability_mean_path.map((row, index) => {
      if (!object(row) || !same(row.seconds, grid[index])) fail('Inconsistent conditional probability mean clock.');
      return {seconds: number(row.seconds, 0, 905), probability_yes: probability(row.probability_yes)};
    });
    const mean = result.diagnostics.probability_mean_path;
    if (!same(mean[0].probability_yes, result.analytic_probability_yes)
        || !same(mean.at(-1).probability_yes, result.probability_yes)) fail('Inconsistent conditional probability mean endpoints.');
  }
  const seen = new Set();
  result.probability_paths = raw.probability_paths.map(path => {
    if (!object(path) || typeof path.id !== 'string' || !path.id.length || seen.has(path.id) || !Array.isArray(path.points) || path.points.length !== grid.length) fail('Invalid probability path.');
    seen.add(path.id);
    const points = path.points.map((point, i) => {
      if (!object(point) || !same(point.seconds, grid[i])) fail('Inconsistent probability path grid.');
      return {seconds: number(point.seconds, 0, 905), probability_yes: probability(point.probability_yes)};
    });
    if (!same(points[0].probability_yes, result.analytic_probability_yes) || ![0, 1].includes(points.at(-1).probability_yes)) fail('Inconsistent probability path endpoints.');
    return {id: text(path.id), points};
  });
  if (!Array.isArray(raw.sensitivity_fan) || raw.sensitivity_fan.length !== grid.length) fail('Invalid sensitivity fan.');
  result.sensitivity_fan = raw.sensitivity_fan.map((row, index) => {
    if (!object(row) || !same(row.seconds, grid[index]) || !finite(row.window_elapsed_seconds) || !finite(row.window_remaining_seconds)
        || !same(row.window_elapsed_seconds, result.model_time + row.seconds - time.window_start)
        || !same(row.window_remaining_seconds, time.window_end - result.model_time - row.seconds)) fail('Inconsistent sensitivity clock.');
    const terminal = index === grid.length - 1, resultRow = {seconds: row.seconds, window_elapsed_seconds: row.window_elapsed_seconds, window_remaining_seconds: row.window_remaining_seconds};
    for (const key of ['delta', 'gamma', 'theta', 'vega']) {
      if (terminal || deterministic) {
        if (row[key] !== null) fail('Undefined terminal or deterministic sensitivities must be unavailable.');
        resultRow[key] = null;
      } else {
        if (!object(row[key])) fail('Invalid sensitivity quantiles.');
        const band = Object.fromEntries(['p05', 'p50', 'p95'].map(q => [q, number(row[key][q])]));
        if (band.p05 > band.p50 || band.p50 > band.p95) fail('Unordered sensitivity quantiles.');
        resultRow[key] = band;
      }
    }
    return resultRow;
  });
  if (!deterministic) {
    const keys = {delta: 'delta_per_dollar', gamma: 'gamma_per_dollar_squared',
      theta: 'theta_per_second', vega: 'vega_per_dollar_volatility'};
    for (const [metric, key] of Object.entries(keys)) for (const q of ['p05', 'p50', 'p95']) {
      const value = result.sensitivity_fan[0][metric][q], initial = result.greeks[key];
      if (Math.abs(value - initial) > 1e-12 + 1e-8 * Math.max(Math.abs(value), Math.abs(initial))) fail('Initial sensitivity scenarios differ from the current model.');
    }
  }
  if (raw.observed_sensitivities != null) {
    if (!Array.isArray(raw.observed_sensitivities) || raw.observed_sensitivities.length > 900) fail('Invalid observed sensitivity history.');
    result.observed_sensitivities = raw.observed_sensitivities.map((row, index) => {
      if (!object(row)) fail('Invalid observed sensitivity.');
      const value = {as_of: number(row.as_of, 0, result.as_of), model_time: number(row.model_time, time.window_start, result.model_time),
        window_elapsed_seconds: number(row.window_elapsed_seconds, 0, 900), window_remaining_seconds: number(row.window_remaining_seconds, 0, 900),
        probability_yes: probability(row.probability_yes)};
      if (value.model_time > value.as_of || !same(value.window_elapsed_seconds, value.model_time - time.window_start)
          || !same(value.window_remaining_seconds, time.window_end - value.model_time)
          || index && value.model_time <= raw.observed_sensitivities[index - 1].model_time) fail('Inconsistent observed sensitivity clock.');
      for (const key of ['delta', 'gamma', 'theta', 'vega']) value[key] = row[key] === null ? null : number(row[key]);
      return value;
    });
  }
  return result;
}

const percent = value => `${(value * 100).toFixed(2)}%`;
const scientific = value => value === null ? 'Unavailable' : value.toExponential(3);
const signed = value => `${value >= 0 ? '+' : ''}${value.toFixed(3)}`;
const VALUES = ['yes', 'no', 'analytic', 'interval', 'se', 'spot', 'target', 'age', 'samples', 'delta', 'gamma', 'theta', 'vega', 'elapsed', 'remaining', 'theta-remaining'];
export function mountProbability(doc = document, win = window, {fetchImpl = win.fetch.bind(win), now = Date.now, pollMs = 5000, asset = 'BTC'} = {}) {
  if (!doc.getElementById('live-probability-panel')) return null;
  const $ = name => doc.getElementById(`probability-mc-${name}`), set = (name, value) => { if ($(name)) $(name).textContent = value; };
  if (!viewAsset(asset)) fail('Choose a supported market asset.');
  let selectedAsset = asset, explicitlySelected = false, environment = null, market = null, report = null, currentIdentity = null, version = 0, fetching = false, controller = null, stopped = false;
  let lastShown = null, staleReason = '', greekChartSnapshot = null;
  let probabilityView = 'future', sensitivityView = 'all';
  const shownFields = new Map();
  let paths = '4000';
  const validMarket = state => (environment === null || environment === 'production')
    && state?.market_status === 'ready' && assetForTicker(state.market?.ticker) === selectedAsset
    && (state.selected_asset == null || state.selected_asset === selectedAsset) && IDENTITY.test(state.market?.quote_id)
    && finite(Date.parse(state.market.open_time)) && finite(Date.parse(state.market.close_time)) && finite(Date.parse(state.fetched_at))
    && now() >= Date.parse(state.market.open_time) && now() < Date.parse(state.market.close_time)
    && now() - Date.parse(state.fetched_at) >= -1000 && now() - Date.parse(state.fetched_at) <= 5000;
  const fresh = value => validMarket(market) && value.asset === selectedAsset && value.ticker === market.market.ticker && value.quote_id === market.market.quote_id
    && (market.market.target_price == null || Number(market.market.target_price) === value.parameters.target)
    && same(Date.parse(market.market.close_time) / 1000, value.parameters.close_time, 1e-12)
    && value.as_of <= now() / 1000 + 1 && now() / 1000 - value.as_of <= 5 && now() / 1000 - value.model_time <= 5
    && now() / 1000 < value.parameters.close_time;
  function mark(name, stale, snapshot = null) {
    const element = $(name); if (!element) return;
    element.setAttribute?.('data-stale', stale ? 'true' : 'false');
    element.title = stale && snapshot
      ? `Last known value from ${new Date(snapshot.as_of * 1000).toLocaleString()}; source observation ${new Date(snapshot.model_time * 1000).toLocaleString()}.`
      : '';
  }
  function show(name, text, value, available = true) {
    const previous = shownFields.get(name);
    if (!available && previous) {
      set(name, `${previous.text} (last known)`); mark(name, true, previous); return;
    }
    set(name, text); mark(name, false);
    if (available) shownFields.set(name, {text, as_of: value.as_of, model_time: value.model_time});
  }
  function retain(message, fit = null) {
    staleReason = message;
    const stamp = new Date(lastShown.as_of * 1000).toLocaleTimeString('en-US');
    const observation = new Date(lastShown.model_time * 1000).toLocaleTimeString('en-US');
    const age = Math.max(0, now() / 1000 - lastShown.model_time).toFixed(1);
    for (const name of VALUES) mark(name, shownFields.has(name), shownFields.get(name));
    mark('chart', true, lastShown); mark('greek-chart', greekChartSnapshot !== null, greekChartSnapshot);
    set('status', `${fittedStatus(fit || lastShown.fitted_parameters)}STALE - showing last known ${selectedAsset} data. ${message}`);
    set('updated', `Last known model snapshot ${stamp}; CF ${ASSETS[selectedAsset].index} observation ${observation} (${age}s old). Waiting for replacement data.`);
    if (greekChartSnapshot) set('greek-status', `STALE - retained sensitivity chart from ${new Date(greekChartSnapshot.as_of * 1000).toLocaleTimeString('en-US')}; no new observations or simulated points have been added.`);
  }
  function clear(message, fit = null, hard = false) {
    report = null;
    if (!hard && lastShown) { retain(message, fit); return; }
    lastShown = null; staleReason = ''; greekChartSnapshot = null; shownFields.clear();
    for (const name of VALUES) { set(name, 'Unavailable'); mark(name, false); }
    mark('chart', false); mark('greek-chart', false);
    set('status', fittedStatus(fit) + message); set('updated', 'No fresh Monte Carlo snapshot');
    set('contract', `No current ${selectedAsset} model contract`);
    set('greek-status', 'No current sensitivity trajectories.');
    $('chart')?.replaceChildren(); $('greek-chart')?.replaceChildren(); $('assumptions')?.replaceChildren();
    for (const content of fittedEvidence(fit)) {
      if (typeof doc.createElement !== 'function') break;
      const element = doc.createElement('li'); element.textContent = content; $('assumptions')?.appendChild(element);
    }
  }
  function chartContext(svg, value, view, views, bounds, metric = '') {
    svg.setAttribute('data-chart-views', JSON.stringify(views));
    svg.setAttribute('data-chart-view', view);
    svg.setAttribute('data-chart-bounds', bounds);
    svg.setAttribute('data-chart-scope', `${environment ?? 'production'}|${selectedAsset}|${value.ticker}|${value.quote_id}|${value.fitted_parameters?.artifact_sha256 ?? 'baseline'}${metric ? `|${metric}` : ''}`);
  }
  function drawProbabilityHistory(value) {
    const svg = $('chart');
    // The producer archives probability_yes (the MC estimate), without each
    // snapshot's simulation count or interval. Do not invent historical CIs.
    const rows = [...new Map([...value.observed_sensitivities,
      {model_time: value.model_time, as_of: value.as_of, probability_yes: value.probability_yes}]
      .map(row => [row.model_time, row])).values()].sort((a, b) => a.model_time - b.model_time);
    const first = rows[0].model_time, last = rows.at(-1).model_time;
    const left = first === last ? first - .5 : first, right = first === last ? last + .5 : last;
    const x = time => 52 + 572 * (time - left) / (right - left), y = p => 210 - 180 * p;
    const add = (tag, fields, content = '') => {
      const node = doc.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, val] of Object.entries(fields)) node.setAttribute(key, String(val));
      node.textContent = content; svg.appendChild(node); return node;
    };
    const detail = (node, description) => {
      node.setAttribute('data-chart-detail', description); node.setAttribute('data-chart-series', 'recorded-probability');
      node.setAttribute('data-chart-label', 'Recorded MC model');
      const title = doc.createElementNS('http://www.w3.org/2000/svg', 'title'); title.textContent = description; node.appendChild(title);
    };
    svg.setAttribute('aria-label', `${selectedAsset} recorded Monte Carlo settlement probability by source observation time`);
    svg.setAttribute('data-chart-x-label', 'Recorded source observation time (UTC)');
    svg.setAttribute('data-chart-y-label', 'Recorded MC YES probability (%)');
    add('text', {x: 52, y: 15, class: 'chart-axis-title'}, `${selectedAsset} P(YES)`);
    add('text', {x: 624, y: 15, class: 'chart-label', 'text-anchor': 'end'}, `${rows.length} recorded model snapshot${rows.length === 1 ? '' : 's'}`);
    for (const level of [0, .25, .5, .75, 1]) {
      add('line', {x1: 52, y1: y(level), x2: 624, y2: y(level), class: 'chart-grid'});
      add('text', {x: 42, y: y(level) + 4, 'text-anchor': 'end', class: 'chart-label'}, `${level * 100}%`);
    }
    const segments = []; let segment = null;
    for (const row of rows) {
      if (!segment || row.model_time - segment.at(-1).model_time > 10) { segment = []; segments.push(segment); }
      segment.push(row);
    }
    detail(add('path', {d: segments.map(points => points.map((row, i) => `${i ? 'L' : 'M'}${x(row.model_time)},${y(row.probability_yes)}`).join(' ')).join(' '),
      class: 'chart-line chart-observed', 'vector-effect': 'non-scaling-stroke'}),
      'Recorded Monte Carlo model probabilities for this contract. Gaps longer than 10 source seconds remain disconnected; this is model history, not observed settlement outcomes. Historical sampling intervals were not recorded.');
    for (const row of rows) {
      const precision = row.model_time === value.model_time
        ? value.sampling_interval.method === 'deterministic' ? 'The current snapshot is deterministic; model uncertainty is unmeasured.'
          : `Current 95% MC sampling interval ${percent(value.sampling_interval.lower)} to ${percent(value.sampling_interval.upper)}; model and calibration uncertainty are unmeasured.`
        : 'A historical sampling interval is unavailable.';
      const description = `${selectedAsset} recorded Monte Carlo model P(YES) ${percent(row.probability_yes)}; source observation ${new Date(row.model_time * 1000).toISOString()}; snapshot ${new Date(row.as_of * 1000).toISOString()}. This is a model estimate, not an observed outcome. ${precision}`;
      detail(add('circle', {cx: x(row.model_time), cy: y(row.probability_yes), r: 2.5, fill: '#e8b86d'}), description);
      detail(add('circle', {cx: x(row.model_time), cy: y(row.probability_yes), r: 6, fill: 'transparent', stroke: 'none',
        class: 'chart-inspect-point', 'pointer-events': 'all'}), description);
    }
    for (const time of [...new Set([first, last])]) add('text', {x: x(time), y: 232,
      'text-anchor': first === last ? 'middle' : time === first ? 'start' : 'end', class: 'chart-label'}, new Date(time * 1000).toISOString().slice(11, 19));
    add('text', {x: 338, y: 252, 'text-anchor': 'middle', class: 'chart-axis-title'}, 'Recorded source observations (UTC); missing history remains gaps');
  }
  function draw(value) {
    const svg = $('chart');
    if (!svg || typeof doc.createElementNS !== 'function') return;
    svg.replaceChildren();
    chartContext(svg, value, probabilityView, [{value: 'future', label: 'Conditional scenarios'},
      {value: 'observed', label: 'Recorded model probability'}], '52 30 624 210');
    if (probabilityView === 'observed') { drawProbabilityHistory(value); return; }
    svg.setAttribute('aria-label', `${selectedAsset} settlement YES probability through the remaining contract window`);
    const horizon = value.probability_fan.at(-1).seconds;
    const x = seconds => 52 + 572 * seconds / horizon, y = p => 210 - 180 * p;
    const add = (tag, fields, label = '') => {
      const element = doc.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, val] of Object.entries(fields)) element.setAttribute(key, String(val));
      element.textContent = label; svg.appendChild(element); return element;
    };
    const detail = (node, description, series, seriesLabel) => {
      node.setAttribute('data-chart-detail', description); node.setAttribute('data-chart-series', series);
      node.setAttribute('data-chart-label', seriesLabel);
      const title = doc.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = description; node.appendChild(title); return node;
    };
    svg.setAttribute('data-chart-x-label', 'Time ahead (seconds)');
    svg.setAttribute('data-chart-y-label', 'Conditional YES probability (%)');
    add('text', {x: 52, y: 15, class: 'chart-axis-title'}, `${selectedAsset} P(YES)`);
    add('text', {x: 624, y: 15, class: 'chart-label', 'text-anchor': 'end'}, 'Conditional model scenarios');
    for (const level of [0, .25, .5, .75, 1]) {
      add('line', {x1: 52, y1: y(level), x2: 624, y2: y(level), class: 'chart-grid'});
      add('text', {x: 42, y: y(level) + 4, 'text-anchor': 'end', class: 'chart-label'}, `${level * 100}%`);
    }
    const path = points => points.map(([seconds, p], index) => `${index ? 'L' : 'M'}${x(seconds).toFixed(2)},${y(p).toFixed(2)}`).join(' ');
    const band = [...value.probability_fan.map(point => [point.seconds, point.p95]),
      ...value.probability_fan.slice().reverse().map(point => [point.seconds, point.p05])];
    detail(add('path', {d: `${path(band)} Z`, fill: '#79b8ff', 'fill-opacity': .10, stroke: 'none'}),
      'Pointwise 5th–95th conditional scenario band; not a confidence interval or Monte Carlo sampling interval.', 'scenario-band', 'Scenario band');
    for (const row of value.probability_paths) detail(add('path', {d: path(row.points.map(point => [point.seconds, point.probability_yes])),
      class: 'chart-line chart-sample', opacity: .20, 'vector-effect': 'non-scaling-stroke'}),
      `Illustrative simulated probability path ${row.id}; not observed market history.`, 'sample-paths', 'Illustrative paths');
    for (const key of ['p05', 'p95', 'p50']) detail(add('path', {d: path(value.probability_fan.map(point => [point.seconds, point[key]])),
      class: key === 'p50' ? 'chart-line chart-blue' : 'chart-line chart-band', 'vector-effect': 'non-scaling-stroke'}),
      `Conditional scenario ${key === 'p50' ? 'median' : key === 'p05' ? '5th percentile' : '95th percentile'}; not a realized future probability.`,
      key === 'p50' ? 'conditional-median' : 'scenario-bounds', key === 'p50' ? 'Conditional median' : 'Scenario bounds');
    const mean = value.diagnostics.probability_mean_path;
    if (mean) {
      detail(add('path', {d: path(mean.map(point => [point.seconds, point.probability_yes])), fill: 'none', stroke: '#6ed9a1',
        'stroke-width': 2, 'vector-effect': 'non-scaling-stroke'}),
        'Mean of the provider simulated conditional YES probabilities at each future checkpoint. This differs from the scenario median and is conditional on the frozen diffusion model.',
        'conditional-mean', 'Conditional mean');
      for (const point of mean) detail(add('circle', {cx: x(point.seconds), cy: y(point.probability_yes), r: 5,
        fill: 'transparent', stroke: 'none', class: 'chart-inspect-point', 'pointer-events': 'all'}),
        `${selectedAsset} conditional mean at +${Number(point.seconds.toFixed(3))} s: ${percent(point.probability_yes)}. ${point.seconds === horizon ? 'At settlement this is the simulated YES outcome frequency; the terminal median is a separate binary quantile. ' : ''}Model observation ${new Date(value.model_time * 1000).toISOString()}.`,
        'conditional-mean', 'Conditional mean');
    }
    for (const point of value.probability_fan) detail(add('circle', {cx: x(point.seconds), cy: y(point.p50), r: 5,
      fill: 'transparent', stroke: 'none', class: 'chart-inspect-point', 'pointer-events': 'all'}),
      `${selectedAsset} conditional scenario at +${Number(point.seconds.toFixed(3))} s: median ${percent(point.p50)}; 5th ${percent(point.p05)}; 95th ${percent(point.p95)}. ${point.seconds === horizon ? 'At settlement each simulated outcome is 0 or 1; the terminal median is a scenario quantile, not the expected YES probability. ' : ''}Model observation ${new Date(value.model_time * 1000).toISOString()}.`,
      'conditional-median', 'Conditional median');
    detail(add('text', {x: 620, y: 202, 'text-anchor': 'end', class: 'chart-label'}, 'Settlement: 0 or 1'),
      'Settlement resolves to 0 or 1. The final scenario median is a quantile of binary simulated outcomes, not an expected future probability or directional forecast.',
      'settlement-outcome', 'Settlement outcomes');
    const sampling = value.sampling_interval, samplingX = x(0);
    const samplingSeries = sampling.method === 'deterministic' ? 'Current deterministic estimate' : 'Current MC estimate / sampling interval';
    const samplingDetail = sampling.method === 'deterministic'
      ? `${selectedAsset} current deterministic estimate ${percent(value.probability_yes)}. Zero modeled volatility; no Monte Carlo sampling error. Model uncertainty is not measured.`
      : `${selectedAsset} current Monte Carlo estimate ${percent(value.probability_yes)} from ${value.diagnostics.paths.toLocaleString()} paths. 95% sampling interval ${percent(sampling.lower)} to ${percent(sampling.upper)}; standard error ${(100 * value.monte_carlo_se).toFixed(3)} percentage points. Sampling error does not measure model or calibration uncertainty. Model observation ${new Date(value.model_time * 1000).toISOString()}.`;
    if (sampling.method !== 'deterministic') {
      for (const fields of [
        {x1: samplingX, x2: samplingX, y1: y(sampling.lower), y2: y(sampling.upper)},
        {x1: samplingX - 4, x2: samplingX + 4, y1: y(sampling.lower), y2: y(sampling.lower)},
        {x1: samplingX - 4, x2: samplingX + 4, y1: y(sampling.upper), y2: y(sampling.upper)}
      ]) detail(add('line', {...fields, stroke: '#e8b86d', 'stroke-width': 2,
        'vector-effect': 'non-scaling-stroke'}), samplingDetail, 'current-estimate', samplingSeries);
    }
    detail(add('circle', {cx: samplingX, cy: y(value.probability_yes), r: 4,
      fill: '#e8b86d', stroke: '#0d141c', 'stroke-width': 1.5,
      class: 'chart-inspect-point', 'pointer-events': 'all'}),
      samplingDetail, 'current-estimate', samplingSeries);
    for (const seconds of [0, horizon / 2, horizon]) {
      const tick = horizon < 120
        ? `+${Number(seconds.toFixed(2))} s`
        : `+${Number((seconds / 60).toFixed(2))} min`;
      add('text', {x: x(seconds), y: 232, 'text-anchor': seconds === 0 ? 'start' : seconds === horizon ? 'end' : 'middle', class: 'chart-label'}, tick);
    }
    add('text', {x: 338, y: 252, 'text-anchor': 'middle', class: 'chart-axis-title'}, 'Time ahead from the current model observation to settlement');
  }
  function render(value) {
    report = value; lastShown = value; staleReason = '';
    const p = value.parameters, g = value.greeks, interval = value.sampling_interval;
    set('status', fittedStatus(value.fitted_parameters) + value.reason); set('contract', value.ticker);
    set('source-label', `${selectedAsset} CF ${ASSETS[selectedAsset].index} reference`);
    show('yes', percent(value.probability_yes), value); show('no', percent(value.probability_no), value); show('analytic', percent(value.analytic_probability_yes), value);
    show('interval', `${percent(interval.lower)} to ${percent(interval.upper)}${interval.method === 'deterministic' ? ' (deterministic)' : ''}`, value);
    show('se', `${(100 * value.monte_carlo_se).toFixed(3)} pp`, value);
    const money = (number, target = false) => Number(number).toLocaleString('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: ASSETS[selectedAsset].digits, maximumFractionDigits: target ? 8 : ASSETS[selectedAsset].digits});
    show('spot', money(p.spot), value); show('target', money(p.target, true), value);
    show('age', `${Math.max(0, now() / 1000 - value.model_time).toFixed(1)}s`, value); show('samples', `${p.known_count}/60 fixed; ${value.diagnostics.paths.toLocaleString()} paths`, value);
    show('delta', g.delta_pp_per_dollar === null ? 'Unavailable' : selectedAsset === 'BTC' ? `${signed(g.delta_pp_per_100_dollars)} pp / $100` : `${signed(g.delta_pp_per_dollar)} pp / USD`, value, g.delta_pp_per_dollar !== null);
    show('gamma', `${scientific(g.gamma_per_dollar_squared)} / USD squared`, value, g.gamma_per_dollar_squared !== null);
    show('theta', g.theta_per_second === null ? 'Unavailable' : `${signed(g.theta_per_second * 100)} pp / s`, value, g.theta_per_second !== null);
    show('vega', `${scientific(g.vega_per_dollar_volatility)} / (USD/sqrt(s))`, value, g.vega_per_dollar_volatility !== null);
    show('elapsed', `${(value.time.elapsed_seconds / 60).toFixed(2)} / 15 min`, value);
    show('remaining', `${(value.time.remaining_seconds / 60).toFixed(2)} min`, value);
    show('theta-remaining', g.theta_per_second === null ? 'Unavailable' : `${signed(-g.theta_per_second * 100)} pp / remaining s`, value, g.theta_per_second !== null);
    set('updated', `CF ${ASSETS[selectedAsset].index} observation ${new Date(value.model_time * 1000).toLocaleTimeString('en-US')}; model snapshot ${new Date(value.as_of * 1000).toLocaleTimeString('en-US')}.`);
    $('assumptions')?.replaceChildren();
    for (const label of [...value.assumptions, ...fittedEvidence(value.fitted_parameters)]) {
      if (typeof doc.createElement !== 'function') break;
      const element = doc.createElement('li'); element.textContent = label; $('assumptions')?.appendChild(element);
    }
    draw(value);
    mark('chart', false);
    drawGreeks(value);
    if (['delta', 'gamma', 'theta', 'vega', 'theta-remaining'].some(name => $(name)?.title)) {
      set('status', fittedStatus(value.fitted_parameters) + value.reason + ' Missing sensitivities retain explicitly marked last known values.');
    }
  }
  function drawGreeks(value) {
    const svg = $('greek-chart');
    if (!svg || typeof doc.createElementNS !== 'function') return;
    const metric = ['delta', 'gamma', 'theta', 'vega'].includes($('greek-metric')?.value) ? $('greek-metric').value : 'delta';
    const fan = sensitivityView === 'observed' ? [] : value.sensitivity_fan;
    const history = sensitivityView === 'future' ? [] : value.observed_sensitivities;
    const rows = fan.filter(row => row[metric] !== null);
    const observed = history.filter(row => row[metric] !== null);
    const factor = metric === 'delta' ? selectedAsset === 'BTC' ? 10000 : 100 : metric === 'theta' ? 100 : 1;
    const units = {delta: selectedAsset === 'BTC' ? 'pp / $100' : 'pp / USD', gamma: 'probability / USD²',
      theta: 'pp / elapsed second', vega: 'probability / (USD/√second)'};
    const display = amount => {
      const scaled = amount * factor;
      return scaled === 0 || Math.abs(scaled) >= .001 && Math.abs(scaled) < 10000 ? scaled.toFixed(3) : scaled.toExponential(3);
    };
    const descriptions = {delta: 'Delta', gamma: 'Gamma', theta: 'Calendar theta', vega: 'Vega'};
    const viewLabel = sensitivityView === 'all' ? 'Full contract' : sensitivityView === 'observed' ? 'Recorded snapshots only' : 'Conditional future only';
    set('greek-status', `${selectedAsset} ${descriptions[metric]}: ${units[metric]}. ${viewLabel}. Amber: ${observed.length} recorded model snapshots for this contract. Blue/dashed: conditional future median / 5th / 95th percentiles. Partial sensitivities hold dollar volatility and realized settlement samples fixed. Terminal sensitivities are unavailable.`);
    chartContext(svg, value, sensitivityView, [{value: 'all', label: 'Full contract'},
      {value: 'observed', label: 'Recorded snapshots only'}, {value: 'future', label: 'Conditional future only'}], '82 30 624 210', metric);
    if (!rows.length && !observed.length) {
      if (greekChartSnapshot?.metric === metric && greekChartSnapshot.view === sensitivityView && sensitivityView !== 'observed') {
        mark('greek-chart', true, greekChartSnapshot);
        set('greek-status', `STALE - last known ${metric} chart from ${new Date(greekChartSnapshot.as_of * 1000).toLocaleTimeString('en-US')}; current sensitivity trajectories are unavailable.`);
      } else {
        svg.replaceChildren(); greekChartSnapshot = null; mark('greek-chart', false);
        set('greek-status', sensitivityView === 'observed' ? 'No recorded snapshots are available for this sensitivity and contract.'
          : 'Sensitivity trajectories are unavailable for deterministic volatility or after settlement.');
      }
      return;
    }
    svg.replaceChildren();
    svg.setAttribute('aria-label', `${selectedAsset} probability sensitivities during the selected 15-minute contract`);
    greekChartSnapshot = {as_of: value.as_of, model_time: value.model_time, metric, view: sensitivityView}; mark('greek-chart', false);
    let low = Math.min(0, ...rows.map(row => row[metric].p05), ...observed.map(row => row[metric])), high = Math.max(0, ...rows.map(row => row[metric].p95), ...observed.map(row => row[metric]));
    if (same(low, high, 1e-20)) { low -= 1e-10; high += 1e-10; }
    const padding = (high - low) * .08; low -= padding; high += padding;
    const sourceElapsed = value.model_time - value.time.window_start;
    let first = sensitivityView === 'future' ? sourceElapsed : sensitivityView === 'observed' ? observed[0].window_elapsed_seconds : 0;
    let last = sensitivityView === 'observed' ? observed.at(-1).window_elapsed_seconds : 900;
    if (first === last) { first = Math.max(0, first - .5); last = Math.min(900, last + .5); }
    const x = elapsed => 82 + 542 * (elapsed - first) / (last - first), y = g => 210 - 180 * (g - low) / (high - low);
    const add = (tag, fields, label = '') => {
      const element = doc.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, val] of Object.entries(fields)) element.setAttribute(key, String(val));
      element.textContent = label; svg.appendChild(element); return element;
    };
    const detail = (node, description, series, seriesLabel) => {
      node.setAttribute('data-chart-detail', description); node.setAttribute('data-chart-series', series);
      node.setAttribute('data-chart-label', seriesLabel);
      const title = doc.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = description; node.appendChild(title); return node;
    };
    svg.setAttribute('data-chart-x-label', 'Elapsed contract time (minutes)');
    svg.setAttribute('data-chart-y-label', `${metric}: ${units[metric]}`);
    add('text', {x: 82, y: 15, class: 'chart-axis-title'}, `${selectedAsset} ${metric} (${units[metric]})`);
    for (const level of [low, (low + high) / 2, high]) {
      add('line', {x1: 82, y1: y(level), x2: 624, y2: y(level), class: 'chart-grid'});
      add('text', {x: 72, y: y(level) + 4, 'text-anchor': 'end', class: 'chart-label'}, display(level));
    }
    const sensitivityPath = points => points.map(([elapsed, sensitivity], index) => `${index ? 'L' : 'M'}${x(elapsed).toFixed(2)},${y(sensitivity).toFixed(2)}`).join(' ');
    const futureSegments = []; let futureSegment = null;
    for (const row of fan) {
      if (row[metric] === null) { futureSegment = null; continue; }
      if (!futureSegment) { futureSegment = []; futureSegments.push(futureSegment); }
      futureSegment.push(row);
    }
    for (const points of futureSegments) if (points.length > 1) {
      const band = [...points.map(row => [row.window_elapsed_seconds, row[metric].p95]),
        ...points.slice().reverse().map(row => [row.window_elapsed_seconds, row[metric].p05])];
      detail(add('path', {d: `${sensitivityPath(band)} Z`, class: 'chart-ribbon', fill: '#79b8ff', stroke: 'none'}),
        `${selectedAsset} conditional ${metric} 5th–95th scenario band in ${units[metric]}; not observed sensitivity history.`, 'scenario-band', 'Scenario band');
    }
    for (const q of rows.length ? ['p05', 'p95', 'p50'] : []) detail(add('path', {d: futureSegments.map(points => sensitivityPath(points.map(row => [row.window_elapsed_seconds, row[metric][q]]))).join(' '),
      class: q === 'p50' ? 'chart-line chart-blue' : 'chart-line chart-band', 'vector-effect': 'non-scaling-stroke'}),
      `Conditional ${metric} ${q === 'p50' ? 'median' : q === 'p05' ? '5th percentile' : '95th percentile'} (${units[metric]}).`,
      q === 'p50' ? 'conditional-median' : 'scenario-bounds', q === 'p50' ? 'Conditional median' : 'Scenario bounds');
    for (const points of futureSegments) if (points.length === 1) detail(add('circle', {cx: x(points[0].window_elapsed_seconds), cy: y(points[0][metric].p50),
      r: 2.5, fill: '#79b8ff', class: 'chart-point chart-blue'}),
      `Single conditional ${metric} median at ${(points[0].window_elapsed_seconds / 60).toFixed(3)} min elapsed: ${display(points[0][metric].p50)} ${units[metric]}.`,
      'conditional-median', 'Conditional median');
    for (const row of rows) detail(add('circle', {cx: x(row.window_elapsed_seconds), cy: y(row[metric].p50), r: 5,
      fill: 'transparent', stroke: 'none', class: 'chart-inspect-point', 'pointer-events': 'all'}),
      `${selectedAsset} conditional ${metric} at ${(row.window_elapsed_seconds / 60).toFixed(3)} min elapsed (+${Number(row.seconds.toFixed(3))} s): median ${display(row[metric].p50)}; 5th ${display(row[metric].p05)}; 95th ${display(row[metric].p95)} ${units[metric]}. Dollar volatility and realized settlement samples held fixed. Source observation ${new Date(value.model_time * 1000).toISOString()}.`,
      'conditional-median', 'Conditional median');
    // A missing sensitivity is an explicit break, even if the surrounding
    // valid observations are close in time. A one-point segment needs a
    // marker because an SVG move command alone is invisible.
    const observedSegments = [];
    let segment = null;
    for (const row of history) {
      if (row[metric] === null) { segment = null; continue; }
      if (!segment || row.model_time - segment.at(-1).model_time > 10) {
        segment = []; observedSegments.push(segment);
      }
      segment.push(row);
    }
    if (observedSegments.length) detail(add('path', {d: observedSegments.map(points => sensitivityPath(points.map(row => [row.window_elapsed_seconds, row[metric]]))).join(' '),
      class: 'chart-line chart-observed', 'vector-effect': 'non-scaling-stroke'}),
      `Recorded ${selectedAsset} model ${metric} snapshots (${units[metric]}); gaps remain disconnected.`, 'observed-sensitivity', 'Recorded snapshots');
    for (const points of observedSegments) if (points.length === 1) {
      detail(add('circle', {cx: x(points[0].window_elapsed_seconds), cy: y(points[0][metric]), r: 3, fill: '#e8b86d', class: 'chart-point chart-observed'}),
        `Recorded ${selectedAsset} model ${metric}: ${display(points[0][metric])} ${units[metric]}; source observation ${new Date(points[0].model_time * 1000).toISOString()}.`,
        'observed-sensitivity', 'Recorded snapshots');
    }
    for (const row of observed) detail(add('circle', {cx: x(row.window_elapsed_seconds), cy: y(row[metric]), r: 5,
      fill: 'transparent', stroke: 'none', class: 'chart-inspect-point', 'pointer-events': 'all'}),
      `${selectedAsset} recorded model ${metric}: ${display(row[metric])} ${units[metric]}; source observation ${new Date(row.model_time * 1000).toISOString()}; ${(row.window_elapsed_seconds / 60).toFixed(3)} min elapsed. Dollar volatility and realized settlement samples held fixed.`,
      'observed-sensitivity', 'Recorded snapshots');
    const ticks = sensitivityView === 'all' ? [0, 300, 600, 900] : [first, (first + last) / 2, last];
    for (const elapsed of ticks) add('text', {x: x(elapsed), y: 232,
      'text-anchor': elapsed === first ? 'start' : elapsed === last ? 'end' : 'middle', class: 'chart-label'}, `${Number((elapsed / 60).toFixed(3))} min`);
    if (sourceElapsed >= first && sourceElapsed <= last) {
      detail(add('line', {x1: x(sourceElapsed), y1: 22, x2: x(sourceElapsed), y2: 210, class: 'chart-target'}),
      `Conditional paths begin at the CF source observation ${new Date(value.model_time * 1000).toISOString()}; report received ${new Date(value.as_of * 1000).toISOString()}.`,
      'source-clock', 'Source observation');
      add('text', {x: x(sourceElapsed), y: 28, 'text-anchor': x(sourceElapsed) > 540 ? 'end' : 'start', class: 'chart-target-label'}, 'Source observation');
    }
    add('text', {x: 353, y: 252, 'text-anchor': 'middle', class: 'chart-axis-title'}, 'Elapsed time since this 15-minute contract opened');
  }
  async function refresh() {
    if (stopped) return;
    if (selectedAsset === 'GOLD') { clear(GOLD_MODEL_REASON, null, true); return; }
    if (fetching || !validMarket(market)) return;
    const generation = ++version, ticker = market.market.ticker, identity = currentIdentity, selectedPaths = paths, requestAsset = selectedAsset;
    fetching = true; controller = new AbortController(); const active = controller;
    const timeout = win.setTimeout(() => active.abort(), 12000);
    try {
      const params = new URLSearchParams({market_ticker: ticker, paths: selectedPaths});
      if (requestAsset !== 'BTC') params.set('asset', requestAsset);
      const response = await fetchImpl(`/api/probability?${params}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store',
        redirect: 'error', referrerPolicy: 'no-referrer', headers: {Accept: 'application/json'}, signal: active.signal});
      const body = await response.text();
      if (stopped || generation !== version || identity !== currentIdentity || paths !== selectedPaths) return;
      if (response.status === 404) { clear('Restart the local Kalshi service to enable the live Monte Carlo probability endpoint, then refresh this page.'); return; }
      if (!response.ok || new TextEncoder().encode(body).byteLength > 500000) fail('Live probability model is unavailable.');
      const value = validateLiveProbability(JSON.parse(body), requestAsset);
      const changedFit = lastShown && (lastShown.fitted_parameters?.artifact_sha256 ?? null) !== (value.fitted_parameters?.artifact_sha256 ?? null);
      if (changedFit) clear('Historical fit changed; waiting for values from the current model.', value.fitted_parameters, true);
      if (value.status !== 'ready') { clear(value.reason, value.fitted_parameters); return; }
      if (!fresh(value)) { clear('Model data is stale or belongs to a different contract.'); return; }
      if (lastShown && (value.model_time < lastShown.model_time || value.as_of < lastShown.as_of)) {
        clear('An older source/model snapshot was ignored; waiting for newer observations.'); return;
      }
      if (value.sampling_interval.method !== 'deterministic' && value.diagnostics.paths !== Number(selectedPaths)) fail('Simulation count changed; refresh the probability model.');
      render(value);
    } catch {
      if (!stopped && generation === version) clear(`Live probability unavailable; check CF ${ASSETS[selectedAsset].index} access and wait for fresh observations.`);
    } finally {
      win.clearTimeout(timeout);
      if (generation === version) { fetching = false; controller = null; }
    }
  }
  function reset(message, hard = false) { version++; controller?.abort(); controller = null; fetching = false; clear(message, null, hard); }
  function selectAsset(value, explicit) {
    if (!viewAsset(value)) fail('Choose a supported market asset.');
    if (explicit) explicitlySelected = true;
    if (doc.getElementById('probability-mc-title')) doc.getElementById('probability-mc-title').textContent = `${value} live Monte Carlo probability`;
    if (doc.getElementById('probability-mc-chart-heading')) doc.getElementById('probability-mc-chart-heading').textContent = `${value} probability through settlement`;
    set('source-label', value === 'GOLD' ? 'Gold / Pyth Metal.Index.1OZGOLD/USD' : `${value} CF ${ASSETS[value].index} reference`);
    for (const id of ['paths', 'greek-metric']) if ($(id)) $(id).disabled = value === 'GOLD';
    if (value === selectedAsset) { if (value === 'GOLD') clear(GOLD_MODEL_REASON, null, true); return; }
    selectedAsset = value; market = null; currentIdentity = null;
    reset(value === 'GOLD' ? GOLD_MODEL_REASON : `Waiting for a fresh verified ${value} contract.`, true);
  }
  function setAsset(value) { return selectAsset(value, true); }
  function setMarketState(value) {
    const stateAsset = value?.selected_asset ?? assetForTicker(value?.market?.ticker);
    if (!explicitlySelected && viewAsset(stateAsset)) selectAsset(stateAsset, false);
    const nextEnvironment = value?.environment;
    if (nextEnvironment != null && nextEnvironment !== environment) {
      environment = nextEnvironment; market = null; currentIdentity = null;
      reset('Market environment changed; waiting for a matching production index snapshot.', true);
    }
    if (environment !== null && environment !== 'production') {
      market = null; currentIdentity = null;
      reset('CF probability research requires production index data; no production model is retained in this environment.', true);
      return;
    }
    if (selectedAsset === 'GOLD') { market = null; currentIdentity = null; reset(GOLD_MODEL_REASON, true); return; }
    market = stateAsset === selectedAsset ? value : null;
    const verified = market?.market_status === 'ready' && assetForTicker(market.market?.ticker) === selectedAsset
      && IDENTITY.test(market.market?.quote_id) ? market.market : null;
    const next = verified?.quote_id ?? null;
    const changedTerms = verified && lastShown && (verified.ticker !== lastShown.ticker
      || verified.target_price != null && Number(verified.target_price) !== lastShown.parameters.target
      || !same(Date.parse(verified.close_time) / 1000, lastShown.parameters.close_time, 1e-12));
    if (verified && (next !== currentIdentity || changedTerms)) {
      currentIdentity = next; reset(`Loading current ${selectedAsset} settlement Monte Carlo probability.`, true);
      set('contract', verified.ticker);
      if (validMarket(market)) return refresh();
    }
    if (!validMarket(market)) {
      reset(`Waiting for fresh verified ${selectedAsset} market data.`); return;
    }
    if (report && !fresh(report)) clear('Model snapshot is stale; waiting for a fresh acquisition.');
    if (!report) return refresh();
  }
  $('paths')?.addEventListener('change', () => {
    const value = $('paths').value;
    if (!['1000', '4000', '10000'].includes(value) || value === paths) return;
    paths = value; reset('Refreshing the Monte Carlo estimate with the selected path count.', true); refresh();
  });
  $('refresh')?.addEventListener('click', () => { reset('Refreshing current Monte Carlo probability.'); refresh(); });
  $('chart')?.addEventListener('chart:view', event => {
    if (!['future', 'observed'].includes(event.detail?.view)) return;
    probabilityView = event.detail.view;
    const value = report || lastShown;
    if (value) { draw(value); if (!report) mark('chart', true, lastShown); }
  });
  $('greek-chart')?.addEventListener('chart:view', event => {
    if (!['all', 'observed', 'future'].includes(event.detail?.view)) return;
    sensitivityView = event.detail.view;
    const value = report || lastShown;
    if (value) { drawGreeks(value); if (!report) retain(staleReason); }
  });
  $('greek-metric')?.addEventListener('change', () => {
    if (report && fresh(report)) drawGreeks(report);
    else if (lastShown) {
      // The retained validated snapshot contains every metric. Replot the
      // requested one from that exact evidence rather than deleting useful
      // history or leaving a different metric under the changed selector.
      drawGreeks(lastShown); retain(staleReason || 'Waiting for fresh model data.');
    }
    else { $('greek-chart')?.replaceChildren(); greekChartSnapshot = null; mark('greek-chart', false); set('greek-status', 'Waiting for fresh data for the selected sensitivity.'); }
  });
  const timer = win.setInterval(refresh, pollMs);
  const ageTimer = win.setInterval(() => {
    if (report && !fresh(report)) clear('Model snapshot is stale; waiting for a fresh acquisition.');
    else if (!report && lastShown) retain(staleReason);
    else if (report) {
      set('age', `${(now() / 1000 - report.model_time).toFixed(1)}s`);
      set('elapsed', `${((now() / 1000 - report.time.window_start) / 60).toFixed(2)} / 15 min`);
      set('remaining', `${((report.time.window_end - now() / 1000) / 60).toFixed(2)} min`);
    }
  }, 1000);
  function dispose() { stopped = true; reset('Probability view closed.'); win.clearInterval(timer); win.clearInterval(ageTimer); }
  win.addEventListener('pagehide', dispose); clear(selectedAsset === 'GOLD' ? GOLD_MODEL_REASON : `Waiting for a fresh verified ${selectedAsset} contract.`);
  if (selectedAsset === 'GOLD') selectAsset(selectedAsset, false);
  return {ready: Promise.resolve(), refresh, setAsset, setMarketState, dispose, getReport: () => report, getAsset: () => selectedAsset};
}
