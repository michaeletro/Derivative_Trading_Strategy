// Retrospective walk-forward evaluation. This component makes GET requests only.
const ASSETS = new Set(['BTC', 'ETH', 'SOL', 'XRP']);
const MODELS = ['baseline', 'fitted', 'equal_ensemble', 'weighted_ensemble'];
const MODEL_LABELS = {baseline: 'Baseline', fitted: 'Fitted single model', equal_ensemble: 'Equal-weight ensemble', weighted_ensemble: 'Fitted ensemble weights'};
const COLORS = {baseline: '#94a3b8', fitted: '#e9b96e', equal_ensemble: '#ad9bfa', weighted_ensemble: '#65c9bc'};
const MEMBER_COLORS = ['#79b8ff', '#e9b96e', '#65c9bc', '#ad9bfa', '#e392a6', '#9dca87', '#d8a56d', '#9ca3af'];
const LIMIT = 2_000_000;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };
const number = (value, low = 0, high = 1e12) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fail('Invalid validation number.');
const integer = (value, low = 0, high = 1_000_000) => Number.isSafeInteger(value) && value >= low && value <= high ? value : fail('Invalid validation count.');
const text = (value, maximum = 2000) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail('Invalid validation label.');
const same = (left, right) => Math.abs(left - right) <= 1e-8 * Math.max(1, Math.abs(left), Math.abs(right));
const asset = value => ASSETS.has(value) ? value : fail('Choose BTC, ETH, SOL or XRP.');
const viewAsset = value => value === 'GOLD' ? value : asset(value);
const GOLD_MODEL_REASON = 'Gold historical model validation is unavailable for its dedicated Pyth Metal.Index.1OZGOLD/USD minute-close model. Crypto validation reports are not gold evidence.';
const date = value => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail('Invalid validation date.');
  const stamp = Date.parse(value + 'T00:00:00Z');
  if (!Number.isFinite(stamp) || new Date(stamp).toISOString().slice(0, 10) !== value) fail('Invalid validation date.');
  return value;
};

function scores(raw, details = false) {
  if (!object(raw)) fail('Missing validation scores.');
  const value = {log_loss: number(raw.log_loss), brier_score: number(raw.brier_score, 0, 1),
    count_contracts: integer(raw.count_contracts, 1, 100000), count_snapshots: integer(raw.count_snapshots, 1)};
  if (value.count_snapshots < value.count_contracts) fail('Inconsistent validation sample counts.');
  if (!details) return value;
  if (!object(raw.by_horizon) || !Array.isArray(raw.reliability_bins) || raw.reliability_bins.length !== 10) fail('Missing horizon or reliability diagnostics.');
  const horizons = Object.keys(raw.by_horizon);
  if (!horizons.length || horizons.length > 15 || horizons.some(key => !/^[1-9][0-9]{0,2}$/.test(key) || Number(key) > 900)) fail('Invalid validation horizons.');
  value.by_horizon = Object.fromEntries(horizons.map(key => [key, scores(raw.by_horizon[key])]));
  if (Object.values(value.by_horizon).some(row => row.count_contracts > value.count_contracts)
      || Object.values(value.by_horizon).reduce((sum, row) => sum + row.count_snapshots, 0) !== value.count_snapshots) fail('Horizon coverage does not match overall score coverage.');
  value.reliability_bins = raw.reliability_bins.map((row, index) => {
    if (!object(row) || !same(number(row.lower, 0, 1), index / 10) || !same(number(row.upper, 0, 1), (index + 1) / 10)) fail('Invalid reliability bins.');
    const count = integer(row.count_snapshots), contracts = integer(row.count_contracts, 0, 100000);
    if (contracts > count || contracts > value.count_contracts || count > value.count_snapshots) fail('Inconsistent reliability sample counts.');
    if (count === 0 && (row.mean_probability !== null || row.observed_frequency !== null)) fail('Empty reliability bins retain values.');
    if (count > 0 && (!contracts || row.mean_probability === null || row.observed_frequency === null)) fail('Nonempty reliability bins lack values.');
    const contractWeight = row.contract_weight === undefined ? null : number(row.contract_weight, 0, value.count_contracts);
    if (contractWeight !== null && (count === 0 ? contractWeight !== 0 : contractWeight <= 0 || contractWeight > contracts + 1e-8)) fail('Inconsistent reliability contract weight.');
    return {lower: row.lower, upper: row.upper, count_snapshots: count, count_contracts: contracts, contract_weight: contractWeight,
      mean_probability: count ? number(row.mean_probability, 0, 1) : null,
      observed_frequency: count ? number(row.observed_frequency, 0, 1) : null};
  });
  if (value.reliability_bins.reduce((sum, row) => sum + row.count_snapshots, 0) !== value.count_snapshots) fail('Reliability bins do not cover their snapshots.');
  const weighted = value.reliability_bins.filter(row => row.contract_weight !== null);
  if (weighted.length && (weighted.length !== 10 || !same(weighted.reduce((sum, row) => sum + row.contract_weight, 0), value.count_contracts))) fail('Reliability contract weights do not cover the scored contracts.');
  return value;
}

function weights(raw, ids) {
  if (!object(raw) || Object.keys(raw).length !== ids.length || ids.some(id => !Object.hasOwn(raw, id))) fail('Invalid ensemble weight identities.');
  const value = Object.fromEntries(ids.map(id => [id, number(raw[id], 0, 1)]));
  if (!same(Object.values(value).reduce((sum, weight) => sum + weight, 0), 1)) fail('Ensemble weights must sum to one.');
  return value;
}

function fitDiagnostics(raw) {
  if (raw == null) return null;
  if (!object(raw) || typeof raw.converged !== 'boolean'
      || !['converged', 'iteration_limit_reached', 'not_converged'].includes(raw.status)
      || raw.converged !== (raw.status === 'converged')) fail('Invalid optimizer convergence evidence.');
  const value = {converged: raw.converged, status: raw.status,
    iterations: integer(raw.iterations, 0, 100000), maximum_iterations: integer(raw.maximum_iterations, 1, 100000),
    projected_gradient_residual: number(raw.projected_gradient_residual),
    projected_gradient_tolerance: number(raw.projected_gradient_tolerance, Number.MIN_VALUE)};
  if (value.iterations > value.maximum_iterations
      || value.status === 'iteration_limit_reached' && value.iterations !== value.maximum_iterations
      || value.converged && value.projected_gradient_residual > value.projected_gradient_tolerance) fail('Inconsistent optimizer convergence evidence.');
  return value;
}

function uncertainty(raw) {
  if (!object(raw) || raw.method !== 'paired_utc_day_block_bootstrap' || raw.confidence_level !== .95
      || !['available', 'insufficient_days'].includes(raw.status) || !object(raw.comparisons)) fail('Unsupported validation uncertainty.');
  const value = {method: raw.method, confidence_level: .95, status: raw.status, reason: text(raw.reason),
    test_days: integer(raw.test_days, 1, 365), bootstrap_samples: integer(raw.bootstrap_samples, 1, 2000),
    count_contracts: integer(raw.count_contracts, 1, 100000), comparisons: {}};
  if (raw.status === 'available' && value.test_days < 5 || raw.status === 'insufficient_days' && value.test_days >= 5) fail('Inconsistent uncertainty day count.');
  for (const model of MODELS.slice(1)) {
    const comparison = raw.comparisons[model];
    if (!object(comparison)) fail('Missing paired score comparison.');
    value.comparisons[model] = {};
    for (const metric of ['log_loss', 'brier_score']) {
      const row = comparison[metric];
      if (!object(row)) fail('Missing paired score uncertainty.');
      const item = {difference: number(row.difference, -1e12)};
      if (value.status === 'available') {
        item.lower = number(row.lower, -1e12); item.upper = number(row.upper, -1e12);
        if (item.lower > item.upper) fail('Reversed uncertainty interval.');
      } else {
        if (row.lower !== null || row.upper !== null) fail('Insufficient data must not supply a confidence interval.');
        item.lower = item.upper = null;
      }
      value.comparisons[model][metric] = item;
    }
  }
  return value;
}

export function validateWalkForwardReport(raw, expectedAsset = 'BTC') {
  if (!object(raw) || raw.kind !== 'kalshi_walkforward_report' || raw.version !== 1 || raw.status !== 'ready'
      || raw.asset !== asset(expectedAsset) || raw.execution_eligible !== false || raw.calibrated !== false
      || raw.deployment_status !== 'offline_research_only' || raw.model_scope !== 'frozen_arithmetic_ensemble'
      || raw.evaluation_status !== 'retrospective_requires_fresh_holdout') fail('Unsupported walk-forward report.');
  if (!Array.isArray(raw.members) || raw.members.length < 2 || raw.members.length > 8) fail('Invalid ensemble members.');
  const members = raw.members.map(row => {
    if (!object(row) || typeof row.id !== 'string' || !/^[a-z][a-z0-9_]{0,39}$/.test(row.id)) fail('Invalid ensemble member label.');
    return {id: row.id, half_life_seconds: number(row.half_life_seconds, 1, 3600)};
  });
  const ids = members.map(row => row.id);
  if (new Set(ids).size !== ids.length) fail('Duplicate ensemble member.');
  if (!object(raw.counts) || !object(raw.aggregate_metrics) || !Array.isArray(raw.folds) || !raw.folds.length || raw.folds.length > 100) fail('Incomplete walk-forward results.');
  const counts = {};
  for (const key of ['input_contracts', 'input_snapshots', 'utc_days', 'attempted_folds', 'evaluated_folds', 'skipped_folds', 'test_contracts', 'test_snapshots', 'test_days']) counts[key] = integer(raw.counts[key], 0);
  if (counts.evaluated_folds !== raw.folds.length || counts.attempted_folds !== counts.evaluated_folds + counts.skipped_folds
      || !counts.test_contracts || counts.test_contracts > counts.input_contracts || counts.test_snapshots > counts.input_snapshots
      || counts.test_days > counts.utc_days) fail('Inconsistent walk-forward coverage.');
  const aggregate = Object.fromEntries(MODELS.map(model => [model, scores(raw.aggregate_metrics[model], true)]));
  const horizons = Object.keys(aggregate.baseline.by_horizon).sort();
  for (const model of MODELS) {
    if (aggregate[model].count_contracts !== counts.test_contracts || aggregate[model].count_snapshots !== counts.test_snapshots
        || Object.keys(aggregate[model].by_horizon).sort().join(',') !== horizons.join(',')) fail('Compared models use different samples.');
    for (const horizon of horizons) if (aggregate[model].by_horizon[horizon].count_contracts !== aggregate.baseline.by_horizon[horizon].count_contracts
        || aggregate[model].by_horizon[horizon].count_snapshots !== aggregate.baseline.by_horizon[horizon].count_snapshots) fail('Horizon model comparisons use different samples.');
  }
  const folds = raw.folds.map((row, index) => {
    if (!object(row) || !object(row.test_metrics) || !object(row.validation_metrics)
        || !object(row.splits) || !object(row.splits.test) || !Array.isArray(row.test_days) || !row.test_days.length) fail('Incomplete walk-forward fold.');
    const id = integer(row.id, 1, 1000);
    if (index && id <= raw.folds[index - 1].id) fail('Walk-forward folds are not ordered.');
    const testDays = row.test_days.map(date);
    if (testDays.some((day, i) => i && day <= testDays[i - 1])) fail('Test days must be distinct and chronological.');
    if (date(row.train_end_day) >= testDays[0]) fail('Training must precede its outer test period.');
    if (row.splits.test.days !== undefined && (!Array.isArray(row.splits.test.days)
        || row.splits.test.days.length !== testDays.length || row.splits.test.days.some((day, i) => date(day) !== testDays[i]))) fail('Fold test dates disagree with split evidence.');
    if (index && testDays[0] <= raw.folds[index - 1].test_days.at(-1)) fail('Walk-forward test days overlap.');
    return {id, train_end_day: date(row.train_end_day), test_days: testDays,
      weights: weights(row.weights, ids), regularization: number(row.regularization, 0, 1e6),
      weight_fit_diagnostics: fitDiagnostics(row.weight_fit_diagnostics),
      test_metrics: Object.fromEntries(MODELS.map(model => [model, scores(row.test_metrics[model])])),
      validation_metrics: Object.fromEntries(MODELS.map(model => [model, scores(row.validation_metrics[model])]))};
  });
  if (folds.reduce((sum, fold) => sum + fold.test_days.length, 0) !== counts.test_days) fail('Fold test days do not match aggregate coverage.');
  for (const model of MODELS) if (folds.reduce((sum, fold) => sum + fold.test_metrics[model].count_contracts, 0) !== counts.test_contracts
      || folds.reduce((sum, fold) => sum + fold.test_metrics[model].count_snapshots, 0) !== counts.test_snapshots) fail('Fold totals do not match aggregate coverage.');
  for (const fold of folds) for (const model of MODELS) for (const period of ['test_metrics', 'validation_metrics']) {
    if (fold[period][model].count_contracts !== fold[period].baseline.count_contracts
        || fold[period][model].count_snapshots !== fold[period].baseline.count_snapshots) fail('Fold model comparisons use different samples.');
  }
  for (const model of MODELS) for (const metric of ['log_loss', 'brier_score']) {
    const combined = folds.reduce((sum, fold) => sum + fold.test_metrics[model][metric] * fold.test_metrics[model].count_contracts, 0) / counts.test_contracts;
    if (!same(combined, aggregate[model][metric])) fail('Fold scores do not match their aggregate.');
  }
  const comparison = uncertainty(raw.uncertainty);
  if (comparison.test_days !== counts.test_days || comparison.count_contracts !== counts.test_contracts) fail('Uncertainty uses a different sample.');
  for (const model of MODELS.slice(1)) for (const metric of ['log_loss', 'brier_score']) {
    if (!same(comparison.comparisons[model][metric].difference, aggregate[model][metric] - aggregate.baseline[metric])) fail('Inconsistent paired score difference.');
  }
  if (!object(raw.market_benchmark) || raw.market_benchmark.status !== 'unavailable') fail('Unsupported market-price benchmark.');
  if (!Array.isArray(raw.assumptions) || raw.assumptions.length > 40) fail('Invalid evaluation assumptions.');
  if (raw.artifact_sha256 !== undefined && (typeof raw.artifact_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(raw.artifact_sha256))) fail('Invalid evaluation artifact identity.');
  const created = raw.created_at == null ? null : text(raw.created_at, 64);
  if (created !== null && !Number.isFinite(Date.parse(created))) fail('Invalid evaluation creation time.');
  return {kind: raw.kind, version: 1, status: 'ready', asset: expectedAsset, members, counts, folds,
    aggregate_metrics: aggregate, uncertainty: comparison, assumptions: raw.assumptions.map(value => text(value)),
    market_benchmark: {status: 'unavailable', reason: text(raw.market_benchmark.reason)}, created_at: created,
    latest_fit_diagnostics: raw.latest_fit?.source_fold_id === folds.at(-1).id ? fitDiagnostics(raw.latest_fit.weight_fit_diagnostics) : null,
    artifact_sha256: raw.artifact_sha256 ?? null, execution_eligible: false, calibrated: false,
    deployment_status: raw.deployment_status, model_scope: raw.model_scope, evaluation_status: raw.evaluation_status};
}

export function validateWalkForwardState(raw, expectedAsset = 'BTC') {
  if (!object(raw) || raw.kind !== 'kalshi_walkforward_state' || raw.version !== 1
      || !['ready', 'unavailable'].includes(raw.status) || raw.asset !== asset(expectedAsset)
      || raw.execution_eligible !== false) fail('Unsupported validation response.');
  const result = {kind: raw.kind, version: 1, status: raw.status, asset: expectedAsset, reason: text(raw.reason), execution_eligible: false, report: null};
  if (raw.status === 'unavailable') {
    if (raw.report !== null) fail('Unavailable validation retains a report.');
  } else result.report = validateWalkForwardReport(raw.report, expectedAsset);
  return result;
}

const formatted = value => value.toFixed(6);
const signed = value => `${value >= 0 ? '+' : ''}${formatted(value)}`;

export function mountValidation(options = {}, windowArg = {}, legacyOptions = {}) {
  const legacy = typeof options?.getElementById === 'function';
  const settings = legacy ? {...legacyOptions, document: options, window: windowArg} : options;
  const doc = settings.document ?? globalThis.document, win = settings.window ?? globalThis.window ?? {};
  const root = doc?.getElementById('validation-root');
  if (!root) return {ready: Promise.resolve(), refresh: async () => null, dispose() {}, setAsset() {}, setMarketState() {}, getReport: () => null};
  const fetchImpl = settings.fetch ?? settings.fetchImpl ?? globalThis.fetch;
  const schedule = settings.setTimeout ?? win.setTimeout?.bind(win) ?? globalThis.setTimeout;
  const cancel = settings.clearTimeout ?? win.clearTimeout?.bind(win) ?? globalThis.clearTimeout;
  const now = settings.now ?? Date.now, pollMs = settings.pollMs ?? 15000;
  let selectedAsset = viewAsset(settings.asset ?? 'BTC'), report = null, lastDisplay = null, loadedAt = null, stopped = false, version = 0, timer = null, controller = null;
  let metric = 'log_loss', selectedModel = 'weighted_ensemble', scoreView = 'absolute';
  const getAsset = settings.getAsset ?? (() => selectedAsset);
  selectedAsset = viewAsset(getAsset());
  const nodes = {};
  const create = (tag, contents = null, className = null) => {
    const element = doc.createElement(tag);
    if (contents !== null) element.textContent = contents;
    if (className) element.setAttribute('class', className);
    return element;
  };
  const add = (parent, tag, contents, className) => { const element = create(tag, contents, className); parent.appendChild(element); return element; };
  const track = (name, element) => { element.setAttribute('id', `validation-${name}`); nodes[name] = element; return element; };
  root.replaceChildren();
  const heading = add(root, 'div', null, 'validation-heading');
  const headingText = add(heading, 'div');
  if (!doc.getElementById('validation-title')) track('title', add(headingText, 'h2', 'Walk-forward probability comparison'));
  add(headingText, 'p', 'Chronological forecasts, held-out scores, and learned research weights.', 'hint');
  add(heading, 'span', 'Retrospective research', 'tag validation-research');
  const existingStatus = doc.getElementById('validation-status');
  const status = existingStatus ?? add(root, 'p', 'Loading validation report.', 'hint validation-status');
  track('status', status).setAttribute('role', 'status');
  status.textContent = 'Loading validation report.';
  track('coverage', add(root, 'p', '', 'small'));
  const controls = add(root, 'div', null, 'validation-controls');
  for (const [key, caption, choices] of [
    ['metric', 'Score', [['log_loss', 'Log loss'], ['brier_score', 'Brier score']]],
    ['score-view', 'Chart comparison', [['absolute', 'Absolute score'], ['difference', 'Change versus baseline']]],
    ['model', 'Reliability and uncertainty', MODELS.map(model => [model, MODEL_LABELS[model]])]
  ]) {
    const label = add(controls, 'label', caption);
    const select = track(key, add(label, 'select'));
    for (const [value, caption] of choices) { const option = add(select, 'option', caption); option.setAttribute('value', value); }
    select.value = key === 'metric' ? metric : key === 'score-view' ? scoreView : selectedModel;
  }
  track('refresh', add(controls, 'button', 'Refresh report', 'quiet')).setAttribute('type', 'button');
  const scroll = add(root, 'div', null, 'table-scroll');
  const table = add(scroll, 'table', null, 'data-table validation-score-table');
  track('score-caption', add(table, 'caption', ''));
  const header = add(add(table, 'thead'), 'tr');
  for (const title of ['Model', 'Log loss', 'Brier score', 'Contracts', 'Log-loss change']) {
    const cell = add(header, 'th', title); cell.setAttribute('scope', 'col');
    if (title === 'Log-loss change') track('difference-column', cell);
  }
  track('scores', add(table, 'tbody'));
  const charts = add(root, 'div', null, 'validation-chart-grid');
  for (const [key, title, caption] of [
    ['horizons', 'Scores by time remaining', 'Compare the same forecast horizons; near-expiry predictions can be much easier.'],
    ['reliability', 'Probability reliability', 'Contract-weighted mean forecast against contract-weighted YES frequency. Each contract has total weight one across horizons; bins are not independent trials. The diagonal is a reference; no bin confidence intervals are supplied.'],
    ['folds', 'Later test periods', 'Every point uses a later outer test period; lower scores are better.'],
    ['weights', 'Learned ensemble weights', 'Nonnegative research weights sum to one within each fold.']
  ]) {
    const figure = add(charts, 'figure', null, 'validation-figure');
    add(figure, 'h3', title);
    const chart = track(key, doc.createElementNS ? doc.createElementNS('http://www.w3.org/2000/svg', 'svg') : create('svg'));
    chart.setAttribute('viewBox', '0 0 560 300'); chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', title);
    chart.setAttribute('tabindex', '0'); figure.appendChild(chart);
    add(figure, 'figcaption', caption, 'small');
  }
  track('weight-values', add(root, 'p', '', 'hint'));
  track('uncertainty', add(root, 'p', '', 'validation-uncertainty'));
  track('benchmark', add(root, 'p', '', 'hint'));
  const methods = add(root, 'details', null, 'chart-math validation-methods');
  add(methods, 'summary', 'Scoring method and research assumptions');
  add(methods, 'p', 'p = \u03a3 w_i p_i, with w_i >= 0 and \u03a3 w_i = 1. Log loss = -mean[y log(p) + (1-y) log(1-p)], with equal total weight per contract.', 'validation-math');
  add(methods, 'p', 'Historical outcomes may already have been inspected. These research weights do not change the live strategy; prospective evaluation remains necessary.', 'hint');
  track('assumptions', add(methods, 'ul', null, 'validation-assumptions small'));

  const svg = (parent, tag, attributes, contents = null) => {
    const element = doc.createElementNS ? doc.createElementNS('http://www.w3.org/2000/svg', tag) : create(tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
    if (contents !== null) element.textContent = contents;
    parent.appendChild(element); return element;
  };
  const scoreTick = value => value !== 0 && (Math.abs(value) < .0001 || Math.abs(value) >= 10000)
    ? value.toExponential(1) : value.toLocaleString('en-US', {maximumFractionDigits: 4});
  const percentTick = value => `${(100 * value).toFixed(0)}%`;
  const axes = (parent, maximum, xLabel, yLabel, formatter = scoreTick, minimum = 0) => {
    parent.setAttribute('data-chart-bounds', '52 30 535 220');
    svg(parent, 'line', {x1: 52, y1: 220, x2: 535, y2: 220, class: 'validation-axis'});
    svg(parent, 'line', {x1: 52, y1: 30, x2: 52, y2: 220, class: 'validation-axis'});
    for (const fraction of [0, .25, .5, .75, 1]) {
      const y = 220 - 190 * fraction;
      svg(parent, 'line', {x1: 52, y1: y, x2: 535, y2: y, class: 'validation-grid'});
      svg(parent, 'text', {x: 44, y: y + 4, 'text-anchor': 'end', class: 'validation-label'}, formatter(minimum + fraction * (maximum - minimum)));
    }
    svg(parent, 'text', {x: 293, y: 286, 'text-anchor': 'middle', class: 'validation-label'}, xLabel);
    svg(parent, 'text', {x: 52, y: 15, class: 'validation-label'}, yLabel);
  };
  const legend = parent => MODELS.forEach((model, index) => {
    const x = 62 + index * 121;
    svg(parent, 'line', {x1: x, x2: x + 12, y1: 260, y2: 260, stroke: COLORS[model], 'stroke-width': 2});
    svg(parent, 'text', {x: x + 16, y: 263, class: 'validation-label'}, ['Baseline', 'Single fit', 'Equal mix', 'Fitted mix'][index]);
  });
  function lineChart(parent, xValues, values, xLabel, labels, numeric = false, pointDetail = () => '') {
    parent.replaceChildren();
    const scoreName = metric === 'log_loss' ? 'Log loss' : 'Brier score', relative = scoreView === 'difference';
    const plotted = Object.fromEntries(MODELS.map(model => [model, values[model].map((value, index) => relative ? value - values.baseline[index] : value)]));
    svg(parent, 'title', {}, relative
      ? `${scoreName} change versus baseline on the same held-out contracts: ${xLabel}. Negative changes are better; zero is the baseline. Points are paired score differences, not confidence intervals.`
      : `${scoreName} on held-out contracts: ${xLabel}. Lower scores are better.`);
    let minimum = 0, maximum;
    if (relative) {
      const low = Math.min(0, ...Object.values(plotted).flat()), high = Math.max(0, ...Object.values(plotted).flat()), padding = (high - low || .001) * .08;
      minimum = low - padding; maximum = high + padding;
    } else maximum = Math.max(.001, ...Object.values(plotted).flat()) * 1.08;
    axes(parent, maximum, xLabel, relative ? `${scoreName} change vs baseline` : scoreName, scoreTick, minimum);
    const y = value => 220 - (value - minimum) / (maximum - minimum) * 190;
    if (relative) svg(parent, 'line', {x1: 52, x2: 535, y1: y(0), y2: y(0), class: 'validation-axis', 'stroke-dasharray': '4 3'});
    const x = index => xValues.length === 1 ? 294 : numeric
      ? 52 + 483 * (xValues[index] - xValues[0]) / (xValues.at(-1) - xValues[0])
      : 52 + 483 * index / (xValues.length - 1);
    for (const model of MODELS) {
      const metadata = {'data-chart-series': model, 'data-chart-label': MODEL_LABELS[model]};
      svg(parent, 'polyline', {points: plotted[model].map((value, index) => `${x(index)},${y(value)}`).join(' '), fill: 'none', stroke: COLORS[model], 'stroke-width': 2, ...metadata});
      values[model].forEach((value, index) => {
        const detail = `${MODEL_LABELS[model]} | ${labels[index]} | ${scoreName} ${formatted(value)} | baseline ${formatted(values.baseline[index])}; paired change ${signed(value - values.baseline[index])}${relative ? '; chart shows change, negative is better' : ''} | ${pointDetail(model, index)}`;
        const point = svg(parent, 'circle', {cx: x(index), cy: y(plotted[model][index]), r: 3.5, fill: COLORS[model],
          class: 'validation-point', ...metadata, 'data-chart-detail': detail, 'aria-label': detail});
        svg(point, 'title', {}, detail);
      });
    }
    for (const index of [...new Set([0, Math.floor((labels.length - 1) / 2), labels.length - 1])]) svg(parent, 'text', {x: x(index), y: 232, 'text-anchor': 'middle', class: 'validation-label'}, labels[index]);
    legend(parent);
  }
  function renderCharts(value) {
    const evidence = [value.asset, value.model_scope, value.artifact_sha256 ?? [value.created_at, value.members,
      value.folds.map(fold => [fold.id, fold.train_end_day, fold.test_days]), value.counts]];
    for (const key of ['horizons', 'reliability', 'folds', 'weights']) {
      const view = key === 'reliability' ? selectedModel : key === 'weights' ? 'ensemble-weights' : [metric, scoreView];
      nodes[key].setAttribute('data-chart-scope', JSON.stringify(['validation', evidence, key, view]));
      nodes[key].setAttribute('data-stale', String(report === null));
      if (value.created_at) nodes[key].setAttribute('data-report-created-at', value.created_at);
    }
    const horizons = Object.keys(value.aggregate_metrics.baseline.by_horizon).map(Number).sort((a, b) => b - a);
    lineChart(nodes.horizons, horizons, Object.fromEntries(MODELS.map(model => [model, horizons.map(h => value.aggregate_metrics[model].by_horizon[h][metric])])),
      'Seconds remaining in contract', horizons.map(String), true, (model, index) => {
        const score = value.aggregate_metrics[model].by_horizon[horizons[index]];
        return `${horizons[index]} seconds remaining; ${score.count_contracts} contracts; ${score.count_snapshots} snapshots; held-out historical forecasts`;
      });
    lineChart(nodes.folds, value.folds.map(fold => fold.id), Object.fromEntries(MODELS.map(model => [model, value.folds.map(fold => fold.test_metrics[model][metric])])),
      'Outer test fold, chronological UTC days', value.folds.map(fold => `Fold ${fold.id}`), false, (model, index) => {
        const fold = value.folds[index], score = fold.test_metrics[model];
        return `test UTC ${fold.test_days[0]} to ${fold.test_days.at(-1)}; ${score.count_contracts} contracts; ${score.count_snapshots} snapshots`;
      });
    nodes.reliability.replaceChildren();
    svg(nodes.reliability, 'title', {}, `${MODEL_LABELS[selectedModel]} reliability on held-out historical forecasts. Empty bins are omitted; the diagonal is a reference, not an estimated series.`);
    axes(nodes.reliability, 1, 'Mean forecast probability (%)', 'Observed YES frequency (%)', percentTick);
    svg(nodes.reliability, 'line', {x1: 52, y1: 220, x2: 535, y2: 30, stroke: '#94a3b8', 'stroke-dasharray': '5 5'});
    for (const fraction of [0, .5, 1]) svg(nodes.reliability, 'text', {x: 52 + 483 * fraction, y: 236, 'text-anchor': 'middle', class: 'validation-label'}, percentTick(fraction));
    for (const bin of value.aggregate_metrics[selectedModel].reliability_bins.filter(bin => bin.count_snapshots)) {
      const detail = `${MODEL_LABELS[selectedModel]} | Forecast bin ${percentTick(bin.lower)} to ${percentTick(bin.upper)} | contract-weighted mean forecast ${(100 * bin.mean_probability).toFixed(2)}%; observed YES ${(100 * bin.observed_frequency).toFixed(2)}% | ${bin.count_contracts} contracts / ${bin.count_snapshots} horizon snapshots${bin.contract_weight === null ? '' : `; contract weight ${formatted(bin.contract_weight)}`} | correlated horizons; no bin confidence interval`;
      const dot = svg(nodes.reliability, 'circle', {cx: 52 + 483 * bin.mean_probability, cy: 220 - 190 * bin.observed_frequency,
        r: Math.min(9, 3 + Math.sqrt(bin.count_contracts) / 3), fill: COLORS[selectedModel], opacity: .9,
        class: 'validation-point', 'data-chart-series': selectedModel, 'data-chart-label': MODEL_LABELS[selectedModel],
        'data-chart-detail': detail, 'aria-label': detail});
      svg(dot, 'title', {}, detail);
    }
    nodes.weights.replaceChildren();
    svg(nodes.weights, 'title', {}, 'Research ensemble weights by chronological fold. Every full stacked bar sums to 100%; weights are not returns or live allocations.');
    axes(nodes.weights, 1, 'Outer test fold', 'Ensemble weight (%)', percentTick);
    const width = Math.min(70, 420 / value.folds.length), spread = 483 / value.folds.length;
    value.folds.forEach((fold, index) => {
      let cumulative = 0;
      const x = 52 + index * spread + (spread - width) / 2;
      value.members.forEach((member, memberIndex) => {
        const weight = fold.weights[member.id];
        const detail = `Fold ${fold.id} | ${member.id}, half-life ${member.half_life_seconds} seconds | weight ${(100 * weight).toFixed(2)}% | training through ${fold.train_end_day}; test UTC ${fold.test_days[0]} to ${fold.test_days.at(-1)} | retrospective research weight`;
        const bar = svg(nodes.weights, 'rect', {x, y: 220 - (cumulative + weight) * 190, width, height: weight * 190, fill: MEMBER_COLORS[memberIndex],
          'data-chart-series': member.id, 'data-chart-label': `${member.id} (${member.half_life_seconds}s)`,
          'data-chart-detail': detail, 'aria-label': detail});
        svg(bar, 'title', {}, detail); cumulative += weight;
      });
      if (index === 0 || index === value.folds.length - 1 || value.folds.length < 6) svg(nodes.weights, 'text', {x: x + width / 2, y: 236, 'text-anchor': 'middle', class: 'validation-label'}, `Fold ${fold.id}`);
    });
    const latest = value.folds.at(-1);
    nodes['weight-values'].textContent = `Latest evaluated fold ${latest.id}: ` + value.members.map(member => `${member.id} ${(latest.weights[member.id] * 100).toFixed(2)}%`).join(' · ') + `. Shrinkage λ=${latest.regularization}.`;
    const optimizer = latest.weight_fit_diagnostics ?? value.latest_fit_diagnostics;
    if (optimizer) nodes['weight-values'].textContent += ` Optimizer: ${optimizer.status.replaceAll('_', ' ')} (${optimizer.iterations}/${optimizer.maximum_iterations} iterations; projected-gradient residual ${optimizer.projected_gradient_residual.toExponential(2)}, tolerance ${optimizer.projected_gradient_tolerance.toExponential(2)}).`;
    const comparison = selectedModel === 'baseline' ? null : value.uncertainty.comparisons[selectedModel][metric];
    nodes.uncertainty.textContent = comparison === null ? 'The baseline is the paired reference; select another model to inspect its score difference.'
      : value.uncertainty.status === 'available'
        ? `${MODEL_LABELS[selectedModel]} minus baseline: ${signed(comparison.difference)}; 95% UTC-day block-bootstrap interval [${signed(comparison.lower)}, ${signed(comparison.upper)}]. Lower differences are better. This is retrospective evidence, not a live-profit guarantee.`
        : `${MODEL_LABELS[selectedModel]} minus baseline: ${signed(comparison.difference)}. Insufficient test days for a confidence interval (${value.uncertainty.test_days} UTC days; at least five required). ${value.uncertainty.reason}`;
  }
  function renderScores(value) {
    nodes.scores.replaceChildren();
    const best = Math.min(...MODELS.map(model => value.aggregate_metrics[model][metric]));
    nodes['score-caption'].textContent = `Contract-weighted held-out scores across nonoverlapping outer test periods; lower is better. Highlight: lowest ${metric === 'log_loss' ? 'log loss' : 'Brier score'}.`;
    nodes['difference-column'].textContent = metric === 'log_loss' ? 'Log-loss change' : 'Brier change';
    for (const model of MODELS) {
      const score = value.aggregate_metrics[model], row = add(nodes.scores, 'tr');
      if (same(score[metric], best)) row.setAttribute('class', 'validation-best');
      for (const cell of [MODEL_LABELS[model], formatted(score.log_loss), formatted(score.brier_score), String(score.count_contracts), signed(score[metric] - value.aggregate_metrics.baseline[metric])]) add(row, 'td', cell);
    }
  }
  function render(value) {
    report = lastDisplay = value; loadedAt = now(); root.setAttribute('data-stale', 'false');
    nodes.status.textContent = `${value.asset} retrospective walk-forward report. ${value.counts.evaluated_folds} test folds; no live model promotion.`;
    nodes.coverage.textContent = `${value.counts.test_contracts} distinct test contracts · ${value.counts.test_snapshots} horizon snapshots · ${value.counts.test_days} UTC test days · ${value.counts.skipped_folds} skipped folds`
      + (value.created_at ? ` · Saved ${new Date(value.created_at).toLocaleString('en-US')}` : '');
    renderScores(value);
    nodes.benchmark.textContent = `Market-price benchmark unavailable: ${value.market_benchmark.reason}`;
    nodes.assumptions.replaceChildren();
    for (const assumption of value.assumptions) add(nodes.assumptions, 'li', assumption);
    renderCharts(value);
  }
  function clear(reason, reset = false) {
    report = null;
    nodes.status.textContent = `${selectedAsset}: ${reason}`;
    if (!reset && lastDisplay?.asset === selectedAsset) {
      root.setAttribute('data-stale', 'true');
      for (const key of ['horizons', 'reliability', 'folds', 'weights']) nodes[key].setAttribute('data-stale', 'true');
      nodes.status.textContent = `STALE — ${selectedAsset}: ${reason} Last loaded historical report retained for display.`;
      return;
    }
    lastDisplay = null; loadedAt = null; root.setAttribute('data-stale', 'false');
    for (const key of ['scores', 'horizons', 'reliability', 'folds', 'weights', 'assumptions']) nodes[key].replaceChildren();
    for (const key of ['horizons', 'reliability', 'folds', 'weights']) {
      nodes[key].removeAttribute?.('data-chart-bounds');
      nodes[key].setAttribute('data-chart-scope', JSON.stringify(['validation', selectedAsset, 'unavailable']));
    }
    for (const key of ['coverage', 'weight-values', 'uncertainty', 'benchmark']) nodes[key].textContent = '';
  }
  function selectAsset(value) {
    value = viewAsset(value);
    if (value === selectedAsset) return false;
    selectedAsset = value; version++; controller?.abort(); controller = null;
    clear('Loading historical validation report.', true); return true;
  }
  async function refresh() {
    if (stopped) return null;
    try { selectAsset(getAsset()); } catch { clear('Invalid selected asset.', true); return null; }
    cancel(timer); timer = null; controller?.abort();
    if (selectedAsset === 'GOLD') { clear(GOLD_MODEL_REASON, true); return null; }
    const requestVersion = ++version, requestedAsset = selectedAsset;
    const active = new AbortController(); controller = active;
    const timeout = schedule(() => active.abort(), 10000);
    try {
      const response = await fetchImpl(`/api/validation?asset=${requestedAsset}`, {method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: active.signal});
      if (!response.ok) throw new Error(response.status === 404 ? 'Restart the console to enable historical validation.' : 'Historical validation report is unavailable.');
      const raw = await response.text();
      if (raw.length > LIMIT) fail('Historical validation report exceeds its supported size.');
      const state = validateWalkForwardState(JSON.parse(raw), requestedAsset);
      if (stopped || requestVersion !== version || requestedAsset !== selectedAsset || active.signal.aborted) return null;
      if (state.status === 'ready') render(state.report); else clear(state.reason);
      return report;
    } catch (error) {
      if (!stopped && requestVersion === version && requestedAsset === selectedAsset) clear(error.name === 'AbortError' ? 'Historical report request timed out.' : 'Historical validation report is unavailable or invalid.');
      return null;
    } finally {
      cancel(timeout);
      if (controller === active) controller = null;
      if (!stopped && requestVersion === version) timer = schedule(scheduledRefresh, pollMs);
    }
  }
  function scheduledRefresh() {
    if (stopped) return;
    if (win.dashboardPollingActive?.() === false) { timer = schedule(scheduledRefresh, pollMs); return; }
    refresh();
  }
  function setAsset(value) { return selectAsset(value) ? refresh() : Promise.resolve(report); }
  function setMarketState() {} // Historical evaluation does not depend on a current quote.
  nodes.metric.addEventListener('change', () => {
    if (!['log_loss', 'brier_score'].includes(nodes.metric.value)) return;
    metric = nodes.metric.value; if (lastDisplay) { renderCharts(lastDisplay); renderScores(lastDisplay); }
  });
  nodes.model.addEventListener('change', () => {
    if (!MODELS.includes(nodes.model.value)) return;
    selectedModel = nodes.model.value; if (lastDisplay) renderCharts(lastDisplay);
  });
  nodes['score-view'].addEventListener('change', () => {
    if (!['absolute', 'difference'].includes(nodes['score-view'].value)) return;
    scoreView = nodes['score-view'].value; if (lastDisplay) renderCharts(lastDisplay);
  });
  nodes.refresh.addEventListener('click', () => refresh());
  function dispose() { stopped = true; version++; report = null; loadedAt = null; controller?.abort(); controller = null; cancel(timer); timer = null; }
  win.addEventListener?.('pagehide', dispose);
  const ready = Promise.resolve().then(() => refresh());
  return {ready, refresh, setAsset, setMarketState, dispose, getReport: () => report, getAsset: () => selectedAsset, getLoadedAt: () => loadedAt};
}
