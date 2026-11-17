// Read-only artifact evidence. Publication and reported bindings do not verify a
// running process, qualify a model, or authorize promotion or exchange orders.
const ASSETS = ['BTC', 'ETH', 'SOL', 'XRP'];
const KINDS = ['kalshi_probability_calibration_report', 'kalshi_historical_probability_calibration_report', 'kalshi_research_analysis'];
const FLAGS = ['research_only', 'calibrated', 'live_compatible', 'automatic_promotion', 'live_model_updated', 'execution_eligible',
  'independent_training_chronology_verified', 'original_model_training_chronology_verified', 'prospective_holdout_verified',
  'out_of_sample_claim', 'promotion_eligible', 'running_process_configuration_verified', 'confirmed_improvement'];
const COLORS = ['#49acff', '#ffa94d', '#6ed9a1', '#b7a0ff', '#ef96c5', '#94a3b8'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = () => { throw new Error('The model evidence response is invalid.'); };
const label = (value, maximum = 1400) => typeof value === 'string' && value.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(value) ? value : fail();
const number = (value, low = 0, high = 1e15) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fail();
const stamp = value => number(value, 0, 253402300799);
const integer = value => Number.isSafeInteger(value) && value >= 0 && value <= 1e12 ? value : fail();
const bool = value => typeof value === 'boolean' ? value : fail();
const nullable = (value, validate) => value === null ? null : validate(value);
const list = (value, maximum, validate) => Array.isArray(value) && value.length <= maximum ? value.map(row => validate(row)) : fail();
const enumeration = (value, choices) => choices.includes(value) ? value : fail();
const id = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) ? value : fail();
const fingerprint = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : fail();
const asset = value => enumeration(value, ASSETS);
const decimal = value => typeof value === 'string' && value.length <= 160 && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?$/.test(value)
  && Number.isFinite(Number(value)) && Math.abs(Number(value)) <= 1e20 ? value : fail();
const unique = values => new Set(values).size === values.length;
const when = value => value === null || value === undefined ? 'Unavailable' : new Date(value * 1000).toISOString();
const fmt = value => value === null || value === undefined ? 'Unavailable' : Number(value).toLocaleString('en-US', {maximumFractionDigits: 5});
const flag = value => value === null || value === undefined ? 'Unreported' : value ? 'Reported yes' : 'Reported no';
const fraction = value => value === null ? 'Unavailable' : `${fmt(Number(value) * 100)}%`;
const human = value => value.replaceAll('_', ' ');
const cohortRole = partition => ({train: 'Training: fit the map', validation: 'Validation: select the map', test: 'Historical held-out test', descriptive: 'Descriptive source sample'})[partition] || partition || 'Partition unreported';
const horizonLabel = value => value === null ? 'all reported horizons' : /^\d+(?:\.\d+)?$/.test(value) ? `${value}s remaining` : value;

function historicalMetadata(raw) {
  if (raw === undefined || raw === null) return null;
  if (!object(raw) || !['baseline', 'selected', 'guards', 'features', 'selection', 'model_export', 'bootstrap'].every(key => object(raw[key]))) fail();
  const result = {baseline: {}, selected: {}, guards: {}, features: {}, selection: {}, model_export: {}, bootstrap: {}};
  for (const key of ['evaluation_clock', 'payoff_mode', 'fit_status', 'sample_status']) result[key] = nullable(raw[key], value => label(value, 300));
  for (const key of ['historical_label_live_availability_known', 'receipt_times_fabricated', 'finite_path_mc_reproduced']) result[key] = nullable(raw[key], bool);
  for (const key of ['model_family', 'estimator']) result.baseline[key] = nullable(raw.baseline[key], value => label(value, 300));
  for (const key of ['ewma_half_life_seconds', 'volatility_scale']) result.baseline[key] = nullable(raw.baseline[key], number);
  result.baseline.base_parameters_fitted = nullable(raw.baseline.base_parameters_fitted, bool); result.baseline.base_model_sha256 = nullable(raw.baseline.base_model_sha256, fingerprint);
  result.selected.kind = nullable(raw.selected.kind, value => label(value, 160));
  for (const key of ['intercept', 'slope', 'regularization', 'logit_input_clip']) result.selected[key] = nullable(raw.selected[key], value => number(value, key === 'intercept' ? -1e15 : 0));
  result.selected.converged = nullable(raw.selected.converged, bool);
  for (const key of ['configured_total_sample_met', 'sufficient_test_contracts', 'sufficient_test_days', 'nonsynthetic', 'historical_source_time_only', 'historical_live_receipts_verified', 'previously_unexamined_prospective_holdout_verified']) result.guards[key] = nullable(raw.guards[key], bool);
  for (const key of ['eligible_official_contracts', 'accepted_contracts', 'feature_rows', 'history_seconds']) result.features[key] = nullable(raw.features[key], integer);
  result.features.horizons_seconds = nullable(raw.features.horizons_seconds, values => list(values, 16, number));
  for (const key of ['complete_case_selection_bias_possible', 'exact_second_grid_required']) result.features[key] = nullable(raw.features[key], bool);
  for (const key of ['refit_after_validation', 'test_used_for_selection']) result.selection[key] = nullable(raw.selection[key], bool);
  result.selection.purge_seconds = nullable(raw.selection.purge_seconds, number);
  result.model_export = {status: label(raw.model_export.status, 160), artifact_sha256: nullable(raw.model_export.artifact_sha256, fingerprint), model_files_written: nullable(raw.model_export.model_files_written, bool)};
  const bootstrap = raw.bootstrap;
  const unsupportedScope = bootstrap.partition !== 'test' || bootstrap.horizon !== null;
  result.bootstrap = {partition: 'test', horizon: null, status: label(bootstrap.status, 160), method: nullable(bootstrap.method, value => label(value, 160)),
    test_days: nullable(bootstrap.test_days, integer), bootstrap_samples: nullable(bootstrap.bootstrap_samples, integer), confidence_level: nullable(bootstrap.confidence_level, value => number(value, 0, 1)),
    reported_confidence_level: bootstrap.reported_confidence_level === undefined ? null : nullable(bootstrap.reported_confidence_level, value => number(value, 0, 1)), reason: nullable(bootstrap.reason, label), comparisons: []};
  result.bootstrap.comparisons = list(bootstrap.comparisons, 8, row => {
    if (!object(row) || row.comparison !== 'calibrated_minus_raw') fail();
    const item = {comparison: row.comparison, metric: enumeration(row.metric, ['brier_score', 'log_loss']),
      difference: nullable(row.difference, value => number(value, -1e15)), lower_95: nullable(row.lower_95, value => number(value, -1e15)), upper_95: nullable(row.upper_95, value => number(value, -1e15)), interval_crosses_zero: nullable(row.interval_crosses_zero, bool)};
    return item;
  });
  if (!unique(result.bootstrap.comparisons.map(row => row.metric))) fail();
  if (unsupportedScope) {result.bootstrap.status = 'invalid_scope'; result.bootstrap.comparisons = []; result.bootstrap.reason = 'The supplied uncertainty scope is not the aggregate historical test cohort.';}
  return result;
}

function bindHistoricalUncertainty(model) {
  const bootstrap = model.historical?.bootstrap; if (!bootstrap) return;
  if (bootstrap.status !== 'descriptive') {bootstrap.comparisons = []; return;}
  const group = model.score_groups.find(row => row.partition === 'test' && row.horizon === null), split = model.splits.find(row => row.name === 'test');
  const raw = group?.scores.find(row => row.label === 'raw'), calibrated = group?.scores.find(row => row.label === 'calibrated');
  const wrongMetric = bootstrap.comparisons.some(row => {
    const expected = raw?.[row.metric] === null || calibrated?.[row.metric] === null || raw?.[row.metric] === undefined || calibrated?.[row.metric] === undefined ? null : calibrated[row.metric] - raw[row.metric];
    return expected === null || row.difference === null || Math.abs(expected - row.difference) > 1e-12 + 1e-9 * Math.max(Math.abs(expected), Math.abs(row.difference))
      || row.lower_95 === null !== (row.upper_95 === null) || row.lower_95 !== null && (row.lower_95 > row.upper_95 || row.interval_crosses_zero !== (row.lower_95 <= 0 && row.upper_95 >= 0));
  });
  const wrongSplit = split && [['days', 'days'], ['contracts', 'contracts'], ['forecasts', 'forecasts']].some(([a, b]) => split[a] !== null && group?.[b] !== null && split[a] !== group?.[b]);
  if (!group?.paired || group.weighting !== 'equal_contract_weight_shared_across_its_forecasts' || bootstrap.method !== 'paired_utc_day_block_bootstrap' || bootstrap.confidence_level !== .95
      || bootstrap.bootstrap_samples === null || bootstrap.bootstrap_samples < 100 || bootstrap.bootstrap_samples > 2000 || bootstrap.test_days === null || bootstrap.test_days < 5 || bootstrap.test_days !== group.days || wrongSplit || wrongMetric) {
    bootstrap.status = 'invalid_scope'; bootstrap.comparisons = [];
    bootstrap.reason = 'The uncertainty metadata do not verify this matched aggregate TEST cohort, weighting, day sample and 95% UTC-day method. Scores remain available; intervals are withheld.';
  }
}

export function validateModelEvidenceReport(raw, expected = {}) {
  if (!object(raw) || raw.kind !== 'kalshi_model_evidence' || raw.version !== 1 || raw.read_only !== true || raw.execution_eligible !== false
      || !object(raw.query) || !object(raw.coverage)) fail();
  const generated_at = stamp(raw.generated_at), query = {asset: enumeration(raw.query.asset, ['ALL', ...ASSETS])};
  if (expected.asset !== undefined && expected.asset !== query.asset) fail();
  const sources = list(raw.sources, 8, row => {
    if (!object(row)) fail();
    const value = {id: id(row.id), kind: nullable(row.kind, value => enumeration(value, KINDS)), status: enumeration(row.status, ['available', 'partial', 'unavailable']),
      reported_at: nullable(row.reported_at, stamp), file_modified_at: nullable(row.file_modified_at, stamp), age_seconds: nullable(row.age_seconds, number),
      age_basis: enumeration(row.age_basis, ['reported_at', 'file_modified_at', 'unavailable']), stale: nullable(row.stale, bool), refresh_failed: bool(row.refresh_failed),
      report_fingerprint: nullable(row.report_fingerprint, fingerprint), reason: nullable(row.reason, label)};
    const sourceTime = value.age_basis === 'reported_at' ? value.reported_at : value.age_basis === 'file_modified_at' ? value.file_modified_at : null;
    if (value.age_basis === 'unavailable' ? value.age_seconds !== null : sourceTime === null || sourceTime > generated_at || value.age_seconds === null || Math.abs(generated_at - sourceTime - value.age_seconds) > .01) fail();
    if (value.kind === null && value.status !== 'unavailable') fail(); return value;
  });
  if (!unique(sources.map(row => row.id))) fail();
  const sourceIds = new Set(sources.map(row => row.id)); let groupCount = 0;
  const models = list(raw.models, 512, row => {
    if (!object(row) || !object(row.counts) || !object(row.reported_flags) || !object(row.activation)) fail();
    const value = {id: id(row.id), source_id: id(row.source_id), asset: asset(row.asset), status: label(row.status, 160),
      model_version: nullable(row.model_version, value => label(value, 300)), model_fingerprint: nullable(row.model_fingerprint, fingerprint),
      model_identity_kind: enumeration(row.model_identity_kind, ['reported_experiment_fingerprint', 'unavailable']),
      fit_fingerprint: nullable(row.fit_fingerprint, fingerprint), data_fingerprint: nullable(row.data_fingerprint, fingerprint),
      configuration_id: nullable(row.configuration_id, value => label(value, 300)), configuration_scope: nullable(row.configuration_scope, value => enumeration(value, ['forecast_model'])),
      counts: {}, reported_flags: {}, splits: [], score_groups: [], activation: {}, historical: historicalMetadata(row.historical), reason: nullable(row.reason, label)};
    if (!sourceIds.has(value.source_id) || query.asset !== 'ALL' && value.asset !== query.asset) fail();
    for (const key of ['contracts', 'forecasts', 'days', 'observed_contracts', 'observed_forecasts', 'official_outcomes', 'withheld_forecasts']) value.counts[key] = nullable(row.counts[key], integer);
    for (const key of FLAGS) value.reported_flags[key] = row.reported_flags[key] === undefined ? null : nullable(row.reported_flags[key], bool);
    value.splits = list(row.splits, 16, split => {
      if (!object(split)) fail();
      const item = {name: label(split.name, 160), first_as_of: nullable(split.first_as_of, stamp), last_close: nullable(split.last_close, stamp)};
      for (const key of ['contracts', 'forecasts', 'days', 'purged_contracts']) item[key] = nullable(split[key], integer);
      item.partition_days = split.partition_days === undefined ? null : nullable(split.partition_days, integer);
      item.purge_seconds = split.purge_seconds === undefined ? null : nullable(split.purge_seconds, number);
      if (item.first_as_of !== null && item.last_close !== null && item.last_close < item.first_as_of) fail(); return item;
    });
    value.score_groups = list(row.score_groups, 64, group => {
      if (!object(group)) fail(); groupCount++;
      const item = {id: id(group.id), partition: nullable(group.partition, value => label(value, 160)), horizon: nullable(group.horizon, value => label(String(value), 160)),
        paired: bool(group.paired), weighting: group.weighting === undefined ? null : nullable(group.weighting, value => label(value, 300)), reason: nullable(group.reason, label), contracts: nullable(group.contracts, integer), forecasts: nullable(group.forecasts, integer), days: nullable(group.days, integer), scores: []};
      item.scores = list(group.scores, 16, score => {
        if (!object(score)) fail();
        const result = {label: label(score.label, 160), brier_score: nullable(score.brier_score, value => number(value, 0, 1)), log_loss: nullable(score.log_loss, number),
          infinite_log_loss_count: nullable(score.infinite_log_loss_count, integer), reliability: []};
        result.reliability = list(score.reliability, 64, bin => {
          if (!object(bin)) fail();
          const point = {lower: nullable(bin.lower, value => number(value, 0, 1)), upper: nullable(bin.upper, value => number(value, 0, 1)),
            count: nullable(bin.count, integer), count_kind: enumeration(bin.count_kind, ['forecasts', 'samples', 'contract_horizons']), contract_count: nullable(bin.contract_count, integer),
            mean_probability: nullable(bin.mean_probability, value => number(value, 0, 1)), observed_frequency: nullable(bin.observed_frequency, value => number(value, 0, 1))};
          if (point.lower !== null && point.upper !== null && point.lower > point.upper || point.count === 0 && (point.mean_probability !== null || point.observed_frequency !== null)
              || point.mean_probability === null !== (point.observed_frequency === null)) fail(); return point;
        });
        return result;
      });
      if (!unique(item.scores.map(row => row.label))) fail(); return item;
    });
    if (!unique(value.score_groups.map(row => row.id))) fail();
    bindHistoricalUncertainty(value);
    for (const key of ['producer_identity_reported', 'experiment_fingerprint_match', 'fit_fingerprint_match', 'configuration_match']) value.activation[key] = nullable(row.activation[key], bool);
    if (row.activation.process_verified !== false || row.activation.status !== 'unverified') fail();
    Object.assign(value.activation, {process_verified: false, status: 'unverified', reason: label(row.activation.reason)}); return value;
  });
  if (!unique(models.map(row => row.id)) || groupCount > 512) fail();
  const execution = list(raw.execution, 512, row => {
    if (!object(row)) fail();
    const value = {source_id: id(row.source_id), scope_id: nullable(row.scope_id, id), asset: nullable(row.asset, asset), partition: nullable(row.partition, value => label(value, 160)),
      model_version: nullable(row.model_version, value => label(value, 300)), configuration_id: nullable(row.configuration_id, value => label(value, 300)),
      configuration_scope: nullable(row.configuration_scope, value => enumeration(value, ['recorded_strategy'])), fill_fraction_complete: nullable(row.fill_fraction_complete, bool)};
    if (!sourceIds.has(value.source_id) || query.asset !== 'ALL' && value.asset !== null && value.asset !== query.asset) fail();
    for (const key of ['recorded_orders', 'reconciled_orders', 'unknown_orders', 'actual_fill_records']) value[key] = nullable(row[key], integer);
    for (const key of ['requested_quantity', 'filled_quantity', 'fill_fraction', 'actual_fill_quantity', 'quote_coverage_fraction', 'slippage_including_fees']) value[key] = nullable(row[key], decimal);
    for (const key of ['fill_fraction', 'quote_coverage_fraction']) if (value[key] !== null && (Number(value[key]) < 0 || Number(value[key]) > 1)) fail();
    for (const key of ['requested_quantity', 'filled_quantity', 'actual_fill_quantity']) if (value[key] !== null && Number(value[key]) < 0) fail();
    return value;
  });
  const capture = list(raw.capture, 8, row => {
    if (!object(row)) fail(); const value = {source_id: id(row.source_id), capture_complete: nullable(row.capture_complete, bool)};
    if (!sourceIds.has(value.source_id)) fail();
    for (const key of ['selected_events', 'scanned_events', 'producer_count', 'decisions', 'ready_observations', 'unique_orders', 'logical_requests', 'candidate_contract_horizons', 'verified_final_markets', 'ungraded_contract_horizons', 'identity_mismatches', 'conflicting_outcome_markets', 'invalid_final_responses']) value[key] = nullable(row[key], integer);
    for (const key of ['request_latency_p50', 'request_latency_p95', 'attempt_latency_p95', 'decision_latency_p95']) value[key] = nullable(row[key], number); return value;
  });
  const coverage = {configured_sources: integer(raw.coverage.configured_sources), available_sources: integer(raw.coverage.available_sources), model_rows: integer(raw.coverage.model_rows),
    score_groups: integer(raw.coverage.score_groups), execution_rows: integer(raw.coverage.execution_rows), truncated: bool(raw.coverage.truncated), reason: label(raw.coverage.reason)};
  if (coverage.configured_sources !== sources.length || coverage.available_sources > sources.length || coverage.model_rows !== models.length || coverage.score_groups !== groupCount || coverage.execution_rows !== execution.length) fail();
  return {kind: raw.kind, version: 1, read_only: true, execution_eligible: false, generated_at, query, status: enumeration(raw.status, ['ready', 'partial', 'unavailable']), sources, models, execution, capture, coverage, limitations: list(raw.limitations, 64, label)};
}

export function mountModelEvidence(doc = document, win = window, options = {}) {
  const root = doc.getElementById('model-evidence-root'); if (!root) return null;
  const fetchImpl = options.fetch || options.fetchImpl || win.fetch?.bind(win), pollMs = Math.max(5000, options.pollMs ?? 10000);
  let stopped = false, timer = null, deadline = null, controller = null, pending = null, generation = 0, current = null, last = null, environment = null;
  let evaluationChosen = false;
  const bindings = [], charts = {};
  function el(tag, text, className, id) { const node = doc.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; if (id) node.setAttribute('id', id); return node; }
  function on(node, name, listener) {node.addEventListener(name, listener); bindings.push([node, name, listener]);}
  function option(select, value, text) { const node = el('option', text); node.value = value; select.appendChild(node); return node; }
  function control(parent, text, id) {const label = el('label', text), node = el('select', undefined, undefined, id); label.appendChild(node); parent.appendChild(label); return node;}
  root.replaceChildren();
  root.appendChild(el('p', 'Read-only model and execution evidence. Reported identities, calibration flags and recent publication do not verify a running process or activate a model.', 'model-evidence-boundary'));
  const controls = el('div', undefined, 'model-evidence-controls'); root.appendChild(controls);
  const assetSelect = control(controls, 'Evidence asset', 'model-evidence-asset'); for (const value of ['ALL', ...ASSETS]) option(assetSelect, value, value === 'ALL' ? 'All reported crypto assets' : value); assetSelect.value = 'ALL';
  const evaluation = control(controls, 'One reported evaluation / cohort', 'model-evidence-evaluation'); option(evaluation, '', 'No evaluation loaded'); evaluation.value = '';
  const metric = control(controls, 'Proper score (lower is better)', 'model-evidence-metric'); option(metric, 'brier_score', 'Brier score'); option(metric, 'log_loss', 'Log loss'); metric.value = 'brier_score';
  const executionSelect = control(controls, 'One recorded financial scope', 'model-evidence-execution-scope'); option(executionSelect, '', 'No execution evidence loaded'); executionSelect.value = '';
  const refreshButton = el('button', 'Refresh evidence', 'quiet', 'model-evidence-refresh'); refreshButton.type = 'button'; controls.appendChild(refreshButton);
  const status = el('p', 'Open Research to inspect connected artifacts.', 'hint', 'model-evidence-status'); status.setAttribute('role', 'status'); root.appendChild(status);
  const sourceStatus = el('p', undefined, 'hint', 'model-evidence-source-status'); root.appendChild(sourceStatus);
  const identitySummary = el('p', 'No reported model selected.', 'model-evidence-identity', 'model-evidence-selected-model'); root.appendChild(identitySummary);
  const historicalStatus = el('p', undefined, 'model-evidence-historical-note', 'model-evidence-historical-status'); historicalStatus.hidden = true; root.appendChild(historicalStatus);
  const modelRows = el('div', undefined, 'model-evidence-models', 'model-evidence-models'); root.appendChild(modelRows);
  const graphGrid = el('div', undefined, 'model-evidence-chart-grid'); root.appendChild(graphGrid);
  for (const [id, title] of [['scores', 'Scores within the selected evaluation'], ['reliability', 'Reported calibration bins'], ['execution', 'Recorded order quantities within one scope']]) {
    const panel = el('article', undefined, 'chart-panel model-evidence-chart'); panel.appendChild(el('h4', title));
    const note = el('p', 'No selected evidence.', 'chart-status', `model-evidence-${id}-status`); panel.appendChild(note);
    const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('id', `model-evidence-${id}-chart`); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', title); svg.setAttribute('viewBox', '0 0 720 320'); panel.appendChild(svg); graphGrid.appendChild(panel); charts[id] = {svg, note};
  }
  const bootstrapSection = el('section', undefined, 'model-evidence-uncertainty', 'model-evidence-bootstrap'); bootstrapSection.hidden = true;
  bootstrapSection.appendChild(el('h4', 'Paired UTC-day bootstrap: calibrated minus raw'));
  const bootstrapStatus = el('p', undefined, 'hint', 'model-evidence-bootstrap-status'), bootstrapTable = el('div', undefined, undefined, 'model-evidence-bootstrap-table'); bootstrapSection.appendChild(bootstrapStatus); bootstrapSection.appendChild(bootstrapTable); root.appendChild(bootstrapSection);
  const details = el('details', undefined, 'workspace-optional'); details.appendChild(el('summary', 'Artifact clocks, identity claims, splits and capture coverage')); root.appendChild(details);
  const sourceTable = el('div', undefined, undefined, 'model-evidence-sources'), identityTable = el('div', undefined, undefined, 'model-evidence-identities'), splitTable = el('div', undefined, undefined, 'model-evidence-splits'), historicalTable = el('div', undefined, undefined, 'model-evidence-historical-details'), captureTable = el('div', undefined, undefined, 'model-evidence-capture'), executionTable = el('div', undefined, undefined, 'model-evidence-execution-details');
  for (const [name, target] of [['Artifact source clocks', sourceTable], ['Reported identity and readiness', identityTable], ['Selected evaluation splits', splitTable], ['Historical calibration assumptions and guards', historicalTable], ['Source-specific capture and outcome coverage', captureTable], ['Financial execution scopes', executionTable]]) {details.appendChild(el('h4', name)); details.appendChild(target);}
  const limitations = el('div', undefined, 'hint', 'model-evidence-limitations'); root.appendChild(limitations);
  function table(parent, headings, rows) {const wrap = el('div', undefined, 'table-scroll'), node = el('table', undefined, 'data-table model-evidence-table'), head = el('thead'), line = el('tr');
    for (const value of headings) {const cell = el('th', value); cell.setAttribute('scope', 'col'); line.appendChild(cell);} head.appendChild(line); node.appendChild(head); const body = el('tbody');
    for (const row of rows) {const line = el('tr'); for (const value of row) line.appendChild(el('td', String(value))); body.appendChild(line);} node.appendChild(body); wrap.appendChild(node); parent.appendChild(wrap);}
  const groups = report => report.models.flatMap(model => model.score_groups.map(group => ({key: `${model.id}|${group.id}`, model, group})));
  const selectedGroup = () => last ? groups(last).find(row => row.key === evaluation.value) || null : null;
  const scopeKey = (row, index) => `${row.source_id}|${row.scope_id}|${index}`;
  const selectedExecution = () => last?.execution.find((row, index) => scopeKey(row, index) === executionSelect.value) || null;
  const staleSource = sourceId => !last || current === null || last.sources.find(row => row.id === sourceId)?.refresh_failed === true || last.sources.find(row => row.id === sourceId)?.stale === true;
  function startChart(id, sourceId = null, height = 320) {
    const {svg, note} = charts[id]; svg.replaceChildren(); svg.setAttribute('viewBox', `0 0 720 ${height}`); svg.setAttribute('data-chart-range-enabled', 'false'); svg.setAttribute('data-stale', String(current === null || sourceId !== null && staleSource(sourceId)));
    const source = last?.sources.find(row => row.id === sourceId); svg.setAttribute('data-chart-scope', `${last?.query.asset || ''}|${sourceId || ''}|${source?.report_fingerprint || ''}|${id === 'execution' ? executionSelect.value : evaluation.value}|${id === 'scores' ? metric.value : ''}`);
    const shape = (tag, attrs, text) => {const node = doc.createElementNS('http://www.w3.org/2000/svg', tag); for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value)); if (text !== undefined) node.textContent = text; svg.appendChild(node); return node;};
    const label = (x, y, text, anchor = 'middle') => shape('text', {x, y, 'text-anchor': anchor, class: 'model-evidence-axis'}, text);
    return {svg, note, shape, label};
  }
  function drawScores() {
    const selected = selectedGroup(), entries = selected?.group.scores || [], plot = startChart('scores', selected?.model.source_id ?? null, Math.max(280, 80 + entries.length * 42));
    if (!selected || !entries.length) {plot.note.textContent = 'No score group supplied. A missing baseline or test sample is not reconstructed.'; return;}
    const field = metric.value, left = 220, right = 686, top = 25, bottom = Math.max(205, 45 + entries.length * 42), available = entries.filter(row => row[field] !== null), maximum = Math.max(.000001, ...available.map(row => row[field])) * 1.1;
    plot.svg.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`);
    for (let index = 0; index <= 4; index++) {const x = left + index / 4 * (right - left); plot.shape('line', {x1: x, y1: top, x2: x, y2: bottom, class: 'chart-grid'}); plot.label(x, bottom + 24, fmt(maximum * index / 4));}
    entries.forEach((row, index) => {const y = top + 14 + index * 42, short = row.label.length > 28 ? `${row.label.slice(0, 25)}...` : row.label; plot.label(left - 12, y + 11, short, 'end');
      if (row[field] === null) {plot.label(left + 10, y + 11, row.infinite_log_loss_count > 0 && field === 'log_loss' ? 'Infinite loss reported' : 'Unavailable', 'start'); return;}
      const detail = `${selected.model.asset}; ${selected.group.partition || 'partition unreported'} (${cohortRole(selected.group.partition)}); horizon ${horizonLabel(selected.group.horizon)}; ${row.label}; ${field === 'brier_score' ? 'Brier score' : 'log loss'} ${row[field]}; ${fmt(selected.group.contracts)} contracts, ${fmt(selected.group.forecasts)} forecasts, ${fmt(selected.group.days)} days. ${selected.group.paired ? 'Producer reports a paired sample.' : 'No paired-baseline equivalence is verified.'} Lower is better; this does not demonstrate profitable execution or a valid fresh holdout.`;
      const attrs = {fill: COLORS[index % COLORS.length], 'data-chart-series': row.label, 'data-chart-label': row.label, 'data-chart-detail': detail};
      if (row[field] === 0) plot.shape('circle', {...attrs, cx: left, cy: y + 6, r: 4}); else plot.shape('rect', {...attrs, x: left, y, width: row[field] / maximum * (right - left), height: 14});
    });
    plot.note.textContent = `${field === 'brier_score' ? 'Brier score' : 'Log loss'}; lower is better. ${cohortRole(selected.group.partition)} / ${horizonLabel(selected.group.horizon)}. Only this reported cohort is plotted; forecasts and horizons are not independent outcomes. ${selected.group.paired ? 'The producer reports matched scoring observations; holdout independence remains separate.' : 'No paired-baseline comparison is verified.'} Weighting: ${selected.group.weighting ? human(selected.group.weighting) : 'unreported'}. ${selected.group.reason || ''}`;
  }
  function drawReliability() {
    const selected = selectedGroup(), plot = startChart('reliability', selected?.model.source_id ?? null);
    if (!selected) {plot.note.textContent = 'No calibration bins supplied. Brier score alone does not reconstruct a reliability curve.'; return;}
    const left = 74, right = 686, top = 25, bottom = 258, x = value => left + value * (right - left), y = value => bottom - value * (bottom - top);
    plot.svg.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`);
    for (let index = 0; index <= 5; index++) {const value = index / 5; plot.shape('line', {x1: x(value), y1: top, x2: x(value), y2: bottom, class: 'chart-grid'}); plot.shape('line', {x1: left, y1: y(value), x2: right, y2: y(value), class: 'chart-grid'}); plot.label(x(value), bottom + 22, `${index * 20}%`); plot.label(left - 8, y(value) + 4, `${index * 20}%`, 'end');}
    plot.shape('line', {x1: left, y1: bottom, x2: right, y2: top, stroke: '#94a3b8', 'stroke-dasharray': '5 4'});
    plot.label((left + right) / 2, 310, 'Reported mean forecast probability'); plot.label(left, 14, 'Observed outcome frequency', 'start'); let points = 0;
    selected.group.scores.forEach((row, index) => {let previous = null, path = '';
      for (const bin of row.reliability) {if (bin.mean_probability === null || bin.observed_frequency === null || bin.count === 0) {previous = null; continue;} points++;
        const detail = `${selected.model.asset}; ${row.label}; mean forecast ${fraction(bin.mean_probability)}, observed frequency ${fraction(bin.observed_frequency)}; reported bin count ${fmt(bin.count)} ${bin.count_kind}; ${fmt(bin.contract_count)} reported contracts. Interval ${bin.lower === null ? 'unavailable' : fraction(bin.lower)} to ${bin.upper === null ? 'unavailable' : fraction(bin.upper)}. Repeated forecasts are not independent outcomes; the source's weighting convention applies.`;
        plot.shape('circle', {cx: x(bin.mean_probability), cy: y(bin.observed_frequency), r: 4, fill: COLORS[index % COLORS.length], 'data-chart-series': row.label, 'data-chart-label': row.label, 'data-chart-detail': detail});
        path += `${previous !== null && previous.upper !== null && bin.lower !== null && Math.abs(previous.upper - bin.lower) < 1e-8 ? 'L' : 'M'}${x(bin.mean_probability)} ${y(bin.observed_frequency)} `; previous = bin;
      }
      if (path) plot.shape('path', {d: path, fill: 'none', stroke: COLORS[index % COLORS.length], 'stroke-width': 1.5, 'data-chart-series': row.label, 'data-chart-label': row.label});
    });
    plot.note.textContent = points ? `${points} nonempty reported bins in one evaluation. Dashed diagonal is ideal calibration, not a baseline model. Empty or absent bins remain gaps; no confidence interval or independent-sample claim is inferred.` : 'No nonempty reliability bins supplied. No calibration curve or baseline is invented.';
  }
  function drawExecution() {
    const selected = selectedExecution(), plot = startChart('execution', selected?.source_id ?? null);
    if (selectedGroup()?.model.historical && (!selected || selected.source_id !== selectedGroup().model.source_id)) {plot.note.textContent = 'This historical calibration source contains no actual fills or realized P&L. Separately captured execution scopes are not performance of this calibration.'; return;}
    if (!selected) {plot.note.textContent = 'No recorded financial execution scope supplied. Forecast opportunities and captured decisions are not achieved fills.'; return;}
    const left = 190, right = 686, top = 30, bottom = 225, values = [['requested_quantity', 'Recorded requested contracts'], ['filled_quantity', 'Recorded filled contracts']].filter(([key]) => selected[key] !== null), maximum = Math.max(1, ...values.map(([key]) => Number(selected[key]))) * 1.1;
    plot.svg.setAttribute('data-chart-bounds', `${left} ${top} ${right} ${bottom}`);
    for (let index = 0; index <= 4; index++) {const x = left + index / 4 * (right - left); plot.shape('line', {x1: x, y1: top, x2: x, y2: bottom, class: 'chart-grid'}); plot.label(x, bottom + 25, fmt(maximum * index / 4));}
    values.forEach(([key, name], index) => {const y = top + 40 + index * 70, value = Number(selected[key]); plot.label(left - 10, y + 13, name.replace('Recorded ', ''), 'end');
      const attrs = {fill: COLORS[index], 'data-chart-series': key, 'data-chart-label': name, 'data-chart-detail': `${name}: ${selected[key]} (exact contract quantity); asset ${selected.asset || 'unreported'}, partition ${selected.partition || 'unreported'}, scope ${selected.scope_id}. Reconciled fraction complete: ${flag(selected.fill_fraction_complete)}. Other financial scopes are not added to this row. These are reported execution facts, not forecast opportunities or account P&L.`};
      if (value === 0) plot.shape('circle', {...attrs, cx: left, cy: y + 7, r: 4}); else plot.shape('rect', {...attrs, x: left, y, width: value / maximum * (right - left), height: 22});
    });
    plot.note.textContent = values.length ? `One recorded financial scope only. Fill fraction ${fraction(selected.fill_fraction)}; completeness ${flag(selected.fill_fraction_complete)}. Actual fill-record quantity ${fmt(selected.actual_fill_quantity)} is reported separately in diagnostics. Missing quantities and costs are not zero.` : 'This scope supplies no requested or filled contract quantities. Counts and cost coverage remain in the evidence table.';
  }
  function renderHistorical(model) {
    const own = model.historical, bootstrap = own.bootstrap, group = selectedGroup()?.group;
    historicalStatus.textContent = `Historical source-time evaluation: ${own.evaluation_clock || 'clock unreported'}; payoff mode ${own.payoff_mode || 'unreported'}; fit ${own.fit_status || 'unreported'}; sample ${own.sample_status || 'unreported'}. Finite-path Monte Carlo reproduced: ${flag(own.finite_path_mc_reproduced)}. Historical live label availability known: ${flag(own.historical_label_live_availability_known)}. This artifact supplies no contemporaneous executable book, actual fills or realized P&L and does not establish a verified rounded payout estimator or prospective live performance.`;
    table(historicalTable, ['Historical specification', 'Reported evidence'], [['Evaluation clock', own.evaluation_clock || 'Unavailable'], ['Payoff mode', own.payoff_mode || 'Unavailable'],
      ['Baseline law / estimator', `${own.baseline.model_family || 'Unavailable'} / ${own.baseline.estimator || 'Unavailable'}`], ['Baseline EWMA half-life (seconds)', fmt(own.baseline.ewma_half_life_seconds)], ['Baseline volatility scale', fmt(own.baseline.volatility_scale)],
      ['Baseline parameters fitted', flag(own.baseline.base_parameters_fitted)], ['Baseline model hash', own.baseline.base_model_sha256 || 'Unavailable'], ['Selected map', own.selected.kind || 'Unavailable'], ['Map intercept / slope', `${fmt(own.selected.intercept)} / ${fmt(own.selected.slope)}`],
      ['Regularization / logit input clip', `${fmt(own.selected.regularization)} / ${own.selected.logit_input_clip === null ? 'Unavailable' : own.selected.logit_input_clip}`], ['Map optimizer convergence', flag(own.selected.converged)],
      ['Refit after validation', flag(own.selection.refit_after_validation)], ['Test used for map selection', flag(own.selection.test_used_for_selection)], ['Chronological purge (seconds)', fmt(own.selection.purge_seconds)],
      ['Historical live label availability known', flag(own.historical_label_live_availability_known)], ['Receipt times fabricated', flag(own.receipt_times_fabricated)], ['Finite-path MC reproduced', flag(own.finite_path_mc_reproduced)],
      ['Independent improvement claim', flag(model.reported_flags.confirmed_improvement)], ['Historical export status', own.model_export.status], ['Historical export artifact hash (not a loaded-fit identity)', own.model_export.artifact_sha256 || 'Unavailable'], ['Model files written', flag(own.model_export.model_files_written)]]);
    table(historicalTable, ['Historical guard', 'Source report'], Object.entries(own.guards).map(([key, value]) => [human(key), flag(value)]));
    table(historicalTable, ['Feature coverage / selection', 'Source report'], [['Eligible official contracts', fmt(own.features.eligible_official_contracts)], ['Accepted contracts', fmt(own.features.accepted_contracts)], ['Feature rows', fmt(own.features.feature_rows)],
      ['Forecast horizons (seconds remaining)', own.features.horizons_seconds?.map(fmt).join(', ') || 'Unavailable'], ['History window (seconds)', fmt(own.features.history_seconds)], ['Complete-case selection bias possible', flag(own.features.complete_case_selection_bias_possible)], ['Exact second grid required', flag(own.features.exact_second_grid_required)]]);
    const applies = group?.partition === bootstrap.partition && group?.horizon === bootstrap.horizon;
    if (!applies) {
      bootstrapStatus.textContent = 'The supplied UTC-day bootstrap applies only to the aggregate historical TEST cohort. It is not applied to this training, validation, descriptive or single-horizon selection; no interval is shown for this cohort.';
      return;
    }
    if (bootstrap.status !== 'descriptive') {
      const reportedConfidence = bootstrap.reported_confidence_level ?? bootstrap.confidence_level;
      bootstrapStatus.textContent = `Aggregate test uncertainty unavailable (${human(bootstrap.status)}). ${bootstrap.reason || 'No valid paired test interval was supplied.'} ${reportedConfidence === null ? '' : `Source reported confidence ${fraction(reportedConfidence)}; no 95% interval is applied.`} Valid score and reliability views remain available; this is not evidence of confirmed improvement.`;
      return;
    }
    const precise = value => value === null ? 'Unavailable' : value === 0 ? '0' : Math.abs(value) < .001 || Math.abs(value) >= 1e5 ? value.toExponential(6) : value.toPrecision(7);
    bootstrapStatus.textContent = `${bootstrap.method ? human(bootstrap.method) : 'Bootstrap method unavailable'}; ${fmt(bootstrap.test_days)} retained test UTC days; ${fmt(bootstrap.bootstrap_samples)} resamples. Scope: aggregate historical TEST across its reported horizons. Negative calibrated-minus-raw differences favor the map on that score. Day blocks, not forecast ticks, are resampled. No independently confirmed improvement, profitability or promotion is established here. ${bootstrap.reason || ''}`;
    if (bootstrap.comparisons.length) table(bootstrapTable, ['Score', 'Calibrated minus raw', 'Reported 95% UTC-day interval', 'Zero / interpretation'], bootstrap.comparisons.map(row => [row.metric === 'brier_score' ? 'Brier score' : 'Log loss', precise(row.difference), row.lower_95 === null ? 'Unavailable' : `[${precise(row.lower_95)}, ${precise(row.upper_95)}]`, row.interval_crosses_zero === null ? 'Interval unavailable; no zero-crossing claim' : row.interval_crosses_zero ? 'Crosses zero: no confirmed improvement' : row.upper_95 < 0 ? 'Below zero in this descriptive bootstrap; no prospective claim' : 'Above zero in this descriptive bootstrap; no prospective claim']));
    else bootstrapTable.appendChild(el('p', 'No paired test-score intervals are available. Point score differences are not converted into confidence intervals.', 'hint'));
  }
  function renderSelection() {
    const model = selectedGroup()?.model || last?.models[0] || null;
    identityTable.replaceChildren(); splitTable.replaceChildren(); historicalTable.replaceChildren(); bootstrapTable.replaceChildren(); historicalStatus.hidden = bootstrapSection.hidden = !model?.historical;
    if (model) {
      identitySummary.textContent = `${model.asset} / ${model.model_version || 'model version unreported'} / reported state ${human(model.status)}. ${fmt(model.counts.contracts)} reported contracts, ${fmt(model.counts.forecasts)} forecasts, ${fmt(model.counts.days)} UTC days. Runtime process and loaded parameters are unverified.`;
      table(identityTable, ['Identity namespace', 'Reported value'], [['Report-local row', model.id], ['Experiment fingerprint', model.model_fingerprint || 'Unavailable'], ['Identity kind', human(model.model_identity_kind)],
        ['Fit fingerprint', model.fit_fingerprint || 'Unavailable'], ['Data fingerprint', model.data_fingerprint || 'Unavailable'], ['Forecast-model configuration', model.configuration_id || 'Unavailable'], ['Configuration namespace', model.configuration_scope || 'Unavailable']]);
      table(identityTable, ['Readiness claim', 'Source report'], FLAGS.map(key => [human(key), flag(model.reported_flags[key])]));
      table(identityTable, ['Additional sample evidence', 'Reported count'], [['Observed contracts', fmt(model.counts.observed_contracts)], ['Observed forecasts', fmt(model.counts.observed_forecasts)], ['Official outcome events', fmt(model.counts.official_outcomes)], ['Withheld forecasts', fmt(model.counts.withheld_forecasts)]]);
      table(identityTable, ['Reported producer comparison', 'Comparison result'], ['producer_identity_reported', 'experiment_fingerprint_match', 'fit_fingerprint_match', 'configuration_match'].map(key => [human(key), model.activation[key] === null ? 'Unverified' : model.activation[key] ? 'Reported match / present' : 'Reported mismatch / absent']));
      identityTable.appendChild(el('p', model.activation.reason, 'hint')); if (model.reason) identityTable.appendChild(el('p', model.reason, 'hint'));
      if (model.splits.length) table(splitTable, ['Partition / role', 'Contracts', 'Forecasts', 'Retained UTC days', 'Nominal calendar days', 'Purged contracts', 'Purge (seconds)', 'First as-of UTC', 'Last close UTC'], model.splits.map(row => [`${row.name} / ${cohortRole(row.name)}`, fmt(row.contracts), fmt(row.forecasts), fmt(row.days), fmt(row.partition_days), fmt(row.purged_contracts), fmt(row.purge_seconds), when(row.first_as_of), when(row.last_close)]));
      else splitTable.appendChild(el('p', 'Chronological split, label-availability and purge evidence is unavailable. A score alone does not verify a fresh holdout.', 'hint'));
      if (model.historical) renderHistorical(model);
    } else {identitySummary.textContent = 'No reported model or fit identity is available for this selection. Nothing has been activated.'; identityTable.appendChild(el('p', 'Model identity and readiness remain unverified.', 'hint'));}
    drawScores(); drawReliability(); drawExecution();
  }
  function render(report) {
    current = last = report; root.setAttribute('data-stale', 'false');
    status.textContent = `${report.coverage.available_sources} / ${report.coverage.configured_sources} configured artifacts available; ${report.coverage.model_rows} evidence rows, ${report.coverage.score_groups} score groups. These are availability counts, not qualified live models.${report.coverage.truncated ? ' The compact projection is truncated; displayed rows do not cover the whole source.' : ''}`;
    sourceStatus.textContent = `Evidence-view snapshot ${when(report.generated_at)}. Original report and file clocks are listed separately below. Historical evidence age neither invalidates nor qualifies a model; no running process has been independently verified.`;
    if (!report.sources.length) sourceStatus.textContent += ' No evidence reports connected. Add --calibration-report and/or --research-summary to the console launch command.';
    const oldEvaluation = evaluation.value, available = groups(report); evaluation.replaceChildren(); option(evaluation, '', 'No evaluation selected');
    for (const row of available) option(evaluation, row.key, `${row.model.asset} / ${cohortRole(row.group.partition)} / ${horizonLabel(row.group.horizon)} / ${row.model.model_version || row.model.id}`);
    const preferred = available.find(row => row.model.historical && row.group.partition === 'test' && row.group.horizon === null);
    const previousAvailable = available.some(row => row.key === oldEvaluation);
    evaluation.value = evaluationChosen && previousAvailable ? oldEvaluation : preferred?.key || (previousAvailable ? oldEvaluation : available[0]?.key || ''); evaluation.disabled = !available.length;
    const oldScope = executionSelect.value; executionSelect.replaceChildren(); option(executionSelect, '', 'No financial scope selected');
    report.execution.forEach((row, index) => option(executionSelect, scopeKey(row, index), `${row.asset || 'asset unreported'} / ${row.partition || 'partition unreported'} / ${row.scope_id || 'scope unreported'} / ${row.configuration_id || 'strategy configuration unreported'}`));
    executionSelect.value = report.execution.some((row, index) => scopeKey(row, index) === oldScope) ? oldScope : report.execution.length ? scopeKey(report.execution[0], 0) : ''; executionSelect.disabled = !report.execution.length;
    modelRows.replaceChildren();
    if (report.models.length) table(modelRows, ['Asset / reported model', 'Report state', 'Contracts / forecasts / days', 'Calibration claim', 'Live-compatibility claim', 'Runtime verification'], report.models.slice(0, 128).map(row => [`${row.asset} / ${row.model_version || 'unreported'}`, human(row.status), `${fmt(row.counts.contracts)} / ${fmt(row.counts.forecasts)} / ${fmt(row.counts.days)}`, flag(row.reported_flags.calibrated), flag(row.reported_flags.live_compatible), 'Unverified']));
    else modelRows.appendChild(el('p', 'No model evidence rows supplied for this asset. Missing validation is not a zero score, a passing gate or an active model.', 'hint'));
    if (report.models.length > 128) modelRows.appendChild(el('p', `First 128 of ${report.models.length} rows shown in this table; the evaluation selector retains all projected groups.`, 'hint'));
    sourceTable.replaceChildren(); table(sourceTable, ['Artifact', 'Availability', 'Original report UTC', 'File modification UTC', 'Age clock', 'Age (seconds)', 'Refresh failure', 'Evidence'], report.sources.map(row => [`${row.id} / ${row.kind || 'kind unavailable'}`, row.status, when(row.reported_at), when(row.file_modified_at), human(row.age_basis), fmt(row.age_seconds), row.refresh_failed ? 'Retained / latest read failed' : 'No reported read failure', row.reason || 'File-byte fingerprint retained; process activity unverified']));
    captureTable.replaceChildren(); captureTable.appendChild(el('p', 'These are whole-source capture counters; the asset selector does not partition them. Different reports can overlap and are not added together. Contract-horizon observations, markets, requests and orders are different units.', 'hint'));
    if (report.capture.length) {
      table(captureTable, ['Source', 'Capture complete', 'Selected / scanned events', 'Recorded producers', 'Decisions / ready observations', 'Unique order intents', 'Completed logical requests', 'Candidate contract-horizons', 'Verified final markets', 'Ungraded contract-horizons', 'Identity mismatches / conflicting markets'], report.capture.map(row => [row.source_id, flag(row.capture_complete), `${fmt(row.selected_events)} / ${fmt(row.scanned_events)}`, fmt(row.producer_count), `${fmt(row.decisions)} / ${fmt(row.ready_observations)}`, fmt(row.unique_orders), fmt(row.logical_requests), fmt(row.candidate_contract_horizons), fmt(row.verified_final_markets), fmt(row.ungraded_contract_horizons), `${fmt(row.identity_mismatches)} / ${fmt(row.conflicting_outcome_markets)}`]));
      const ms = value => value === null ? 'Unavailable' : fmt(value * 1000);
      table(captureTable, ['Source', 'Logical request P50 (ms)', 'Logical request P95 (ms)', 'HTTP attempt P95 (ms)', 'Decision P95 (ms)', 'Invalid final responses'], report.capture.map(row => [row.source_id, ms(row.request_latency_p50), ms(row.request_latency_p95), ms(row.attempt_latency_p95), ms(row.decision_latency_p95), fmt(row.invalid_final_responses)]));
      captureTable.appendChild(el('p', 'Recorded request durations include local waits; they are not a pure network round-trip measurement or proof that a current feed is healthy.', 'hint'));
    } else captureTable.appendChild(el('p', 'Capture, request latency and official-outcome coverage are unavailable for these sources.', 'hint'));
    executionTable.replaceChildren();
    if (report.execution.length) table(executionTable, ['Source / recorded scope', 'Asset / partition', 'Strategy configuration', 'Orders / reconciled / unknown', 'Requested / filled contracts', 'Fill fraction / complete', 'Actual fill records / quantity', 'Executable-quote coverage', 'Quote slippage + fees (USD/contract)'], report.execution.slice(0, 128).map(row => [`${row.source_id} / ${row.scope_id || 'unreported'}`, `${row.asset || 'unreported'} / ${row.partition || 'unreported'}`, row.configuration_id || 'Unavailable', `${fmt(row.recorded_orders)} / ${fmt(row.reconciled_orders)} / ${fmt(row.unknown_orders)}`, `${fmt(row.requested_quantity)} / ${fmt(row.filled_quantity)}`, `${fraction(row.fill_fraction)} / ${flag(row.fill_fraction_complete)}`, `${fmt(row.actual_fill_records)} / ${fmt(row.actual_fill_quantity)}`, fraction(row.quote_coverage_fraction), fmt(row.slippage_including_fees)]));
    else executionTable.appendChild(el('p', 'Order requests, actual fills, cost and pacing evidence are unavailable. Predicted opportunities are not treated as filled trades.', 'hint'));
    limitations.replaceChildren(); for (const text of report.limitations) limitations.appendChild(el('p', text, 'hint'));
    renderSelection();
  }
  function clear(message) {
    current = last = null; evaluationChosen = false; root.setAttribute('data-stale', 'false'); status.textContent = message; sourceStatus.textContent = ''; identitySummary.textContent = 'No reported model selected.';
    historicalStatus.textContent = ''; historicalStatus.hidden = bootstrapSection.hidden = true;
    for (const node of [modelRows, sourceTable, identityTable, splitTable, historicalTable, bootstrapTable, captureTable, executionTable, limitations]) node.replaceChildren();
    evaluation.replaceChildren(); option(evaluation, '', 'No evaluation loaded'); evaluation.value = ''; executionSelect.replaceChildren(); option(executionSelect, '', 'No financial scope loaded'); executionSelect.value = '';
    for (const {svg, note} of Object.values(charts)) {svg.replaceChildren(); svg.setAttribute('data-stale', 'false'); note.textContent = 'No selected evidence.';}
  }
  function cancel() {generation++; controller?.abort(); controller = null; pending = null; if (timer !== null) win.clearTimeout(timer); if (deadline !== null) win.clearTimeout(deadline); timer = deadline = null; refreshButton.disabled = false;}
  function arm() {if (!stopped) {if (timer !== null) win.clearTimeout(timer); timer = win.setTimeout(poll, pollMs);}}
  async function refresh() {
    if (stopped) return; if (pending) return pending;
    let selected; try {selected = enumeration(assetSelect.value, ['ALL', ...ASSETS]);} catch {status.textContent = 'Choose an available crypto evidence asset.'; return;}
    const request = ++generation, activeController = new AbortController(); controller = activeController;
    if (timer !== null) win.clearTimeout(timer); timer = null; refreshButton.disabled = true;
    const timeout = win.setTimeout(() => activeController.abort(), 10000); deadline = timeout;
    const operation = (async () => {
      try {
        const response = await fetchImpl(`/api/model-evidence?asset=${selected}`, {method: 'GET', cache: 'no-store', credentials: 'same-origin', signal: activeController.signal});
        if (!response.ok) fail(); const body = await response.text(); if (body.length > 2_500_000) fail();
        const report = validateModelEvidenceReport(JSON.parse(body), {asset: selected});
        if (stopped || request !== generation) return;
        if (last && last.query.asset === selected && report.generated_at < last.generated_at) fail(); render(report);
      } catch {
        if (!stopped && request === generation) {current = null; root.setAttribute('data-stale', 'true'); for (const {svg} of Object.values(charts)) svg.setAttribute('data-stale', 'true');
          status.textContent = last ? `Evidence refresh failed. Retaining the last evidence-view snapshot ${when(last.generated_at)} with original artifact clocks; data are stale and process activity remains unverified.` : 'Model evidence is unavailable. Restart the console with the configured model-evidence report options; inspect Data health for source failures.';}
      } finally {win.clearTimeout(timeout); if (request === generation) {deadline = null; controller = null; refreshButton.disabled = false; arm();}}
    })();
    pending = operation; try {await operation;} finally {if (pending === operation) pending = null;}
  }
  function poll() {if (stopped) return Promise.resolve(); if (win.dashboardPollingActive?.() === false || doc.visibilityState === 'hidden') {arm(); return Promise.resolve();} return refresh();}
  on(assetSelect, 'change', () => {cancel(); clear('Loading an independent asset evidence scope.'); return refresh();});
  on(evaluation, 'change', () => {evaluationChosen = true; renderSelection();}); on(metric, 'change', drawScores); on(executionSelect, 'change', drawExecution); on(refreshButton, 'click', refresh);
  function setServiceState(value) {const next = ['production', 'demo'].includes(value?.environment) ? value.environment : null; if (next !== null && environment !== null && next !== environment) {cancel(); clear('Console environment changed. Offline artifact evidence will reload independently of execution state.');} if (next !== null) environment = next;}
  function dispose() {if (stopped) return; stopped = true; cancel(); for (const [node, name, listener] of bindings) node.removeEventListener?.(name, listener); win.removeEventListener?.('pagehide', dispose);}
  win.addEventListener?.('pagehide', dispose);
  const ready = options.autoLoad === false ? Promise.resolve() : poll();
  return {ready, refresh, poll, setServiceState, dispose, getReport: () => current, getLastReport: () => last};
}
